"""
FutuAPI —— 用富途 OpenD（futu-api）给 chan.py 提供行情数据

仿照 DataAPI/AkshareAPI.py 的结构实现，契约完全一致（继承 CCommonStockApi）：
    SetBasciInfo()  → 解析代码、判定是否为指数
    get_kl_data()   → 产出 Iterable[CKLine_Unit]
    do_init()       → 建连接
    do_close()      → 释放（本实现按线程复用连接，这里不真正关闭）

使用前先启动 FutuOpenD 并登录，默认监听 127.0.0.1:11111。

用法一（不改项目任何文件，推荐）：
    from Chan import CChan
    from Common.CEnum import AUTYPE, KL_TYPE
    chan = CChan(code="HK.00700",
                 begin_time="2025-01-01", end_time="2026-10-10",
                 data_src="custom:FutuAPI.CFutu",      # ← 走 custom 通道
                 lv_list=[KL_TYPE.K_DAY], autype=AUTYPE.QFQ)

用法二（想把 FUTU 做成一等公民枚举）：
    在 Common/CEnum.py 的 DATA_SRC 里加 FUTU = auto()，
    再在 Chan.py 的 GetStockAPI() 里加
        elif self.data_src == DATA_SRC.FUTU:
            from DataAPI.FutuAPI import CFutu
            _dict[DATA_SRC.FUTU] = CFutu

代码写法（会自动补全市场前缀，三种都能用）：
    港股  HK.00700 / hk00700 / 00700
    A股   SH.600000 / sh600000 / 600000 ； SZ.000001 / 000001
    美股  US.AAPL / AAPL
    指数  SH.000001(上证) SZ.399001(深成) HK.800000(恒生) US.DJI(道指)

注意：
  - futu 单次最多返回 1000 根，本实现用 page_req_key 自动翻页合并。
  - futu 的复权数据偶有瑕疵（实测出现过 open < low 的畸形K线），
    CKLine_Unit 默认用 autofix=True 自动修正；想严格校验请 set_futu_config(autofix=False)。
  - do_close() 故意不关闭连接：chan.py 每构造一次 CChan 就 do_init/do_close 一轮，
    若真关掉会变成"每只股票重连一次"。连接按线程复用，进程退出时由 atexit 兜底关闭，
    也可显式调用 close_futu()。
  - 连接是"每线程一条"，所以并发线程数不要开太大（OpenD 有连接数上限），建议 ≤8。
  - futu SDK 会起一个非 daemon 线程，不 close 会让 Python 退出时卡死；
    本模块在建连接时把 futu 自己的新线程设成 daemon，不会卡退出。
"""

import atexit
import math
import re
import socket
import threading
from datetime import datetime

from futu import AuType, KLType, Market, OpenQuoteContext, RET_OK, SecurityType

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Common.func_util import kltype_lt_day
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11111

# 全局配置：连接参数 + 取数行为
_FUTU_CFG = {
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "connect_timeout": 5,    # 探活 socket 超时(秒)
    "max_count": 1000,       # 单次请求最大K线数（futu 上限就是 1000）
    "max_page": 200,         # 翻页保护上限
    "autofix": True,         # 是否自动修正 open<low 这类畸形K线
}

# 按线程缓存 OpenQuoteContext：futu 的 context 不跨线程安全，且创建开销不小
_TLS = threading.local()
_ALL_CTX_LOCK = threading.Lock()
_CREATE_LOCK = threading.Lock()
_ALL_CTX = set()


# ======================================================================
# 一、连接管理
# ======================================================================
def set_futu_config(host=None, port=None, autofix=None, max_count=None, connect_timeout=None):
    """设置 OpenD 地址 / 取数行为（建议在创建 CChan 之前调用，全局生效）"""
    if host:
        _FUTU_CFG["host"] = host
    if port:
        _FUTU_CFG["port"] = int(port)
    if autofix is not None:
        _FUTU_CFG["autofix"] = bool(autofix)
    if max_count:
        _FUTU_CFG["max_count"] = int(max_count)
    if connect_timeout:
        _FUTU_CFG["connect_timeout"] = float(connect_timeout)


def get_futu_config():
    """返回当前配置副本"""
    return dict(_FUTU_CFG)


def check_opend_alive(host=None, port=None, timeout=None):
    """
    探活 OpenD。

    futu SDK 在 OpenD 没起时是"阻塞重试"而不是报错，所以先用裸 socket 探一下，
    探不通就直接抛错，避免整个扫描卡死。
    """
    host = host or _FUTU_CFG["host"]
    port = int(port or _FUTU_CFG["port"])
    timeout = float(timeout or _FUTU_CFG["connect_timeout"])
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_ctx(host, port):
    """
    创建 OpenQuoteContext。

    futu SDK 会起一个非 daemon 的 CallbackExecutor 线程（futu.common.callback_executor），
    如果连接一直不 close，Python 退出时会卡在 threading._shutdown 等这个线程，
    连 atexit 都来不及执行 —— 表现就是"程序跑完不退出"。
    这里在创建的一瞬间只把 futu 自己的新线程设成 daemon：daemon 只影响退出时是否等待，
    不影响正常取数。真正的关闭仍由 close_futu / atexit 正常完成。
    """
    orig_start = threading.Thread.start

    def _start(self, *args, **kwargs):
        try:
            fn = getattr(self, "_target", None)
            if str(getattr(fn, "__module__", "")).startswith("futu"):
                self.daemon = True
        except Exception:
            pass
        return orig_start(self, *args, **kwargs)

    with _CREATE_LOCK:
        threading.Thread.start = _start
        try:
            return OpenQuoteContext(host=host, port=port)
        finally:
            threading.Thread.start = orig_start


def get_ctx():
    """获取当前线程的 OpenQuoteContext（不存在则建）"""
    ctx = getattr(_TLS, "ctx", None)
    if ctx is not None:
        return ctx
    host, port = _FUTU_CFG["host"], int(_FUTU_CFG["port"])
    if not check_opend_alive(host, port):
        raise CChanException(
            f"无法连接富途 OpenD：{host}:{port}，请先启动 FutuOpenD 并确认已登录",
            ErrCode.SRC_DATA_NOT_FOUND,
        )
    ctx = _create_ctx(host, port)
    _TLS.ctx = ctx
    with _ALL_CTX_LOCK:
        _ALL_CTX.add(ctx)
    return ctx


def close_futu():
    """关闭当前线程的连接"""
    ctx = getattr(_TLS, "ctx", None)
    if ctx is None:
        return
    try:
        ctx.close()
    except Exception:
        pass
    _TLS.ctx = None
    with _ALL_CTX_LOCK:
        _ALL_CTX.discard(ctx)


def close_all_futu():
    """关闭所有线程创建的连接（atexit 兜底）"""
    with _ALL_CTX_LOCK:
        ctxs = list(_ALL_CTX)
        _ALL_CTX.clear()
    for ctx in ctxs:
        try:
            ctx.close()
        except Exception:
            pass


atexit.register(close_all_futu)


# ======================================================================
# 二、代码 / 周期 / 复权 的映射
# ======================================================================
def normalize_code(code):
    """
    归一化成 futu 的 市场.代码 格式。

    支持 HK.00700 / hk00700 / 00700 / SH.600000 / sh600000 / 600000 / US.AAPL / AAPL
    注意：美股代码可能以 sh/sz/hk 开头（如 SHW），所以带市场前缀的简写
    只在后面跟纯数字时才认，避免误判。
    """
    c = str(code).strip()
    if not c:
        raise CChanException("股票代码为空", ErrCode.PARA_ERROR)

    # 标准写法 HK.00700 / US.AAPL
    m = re.fullmatch(r"([A-Za-z_]+)\.(.+)", c)
    if m:
        return f"{m.group(1).upper()}.{m.group(2)}"

    # 简写 hk00700 / sh600000 / sz000001 / us.AAPL
    m = re.fullmatch(r"(hk)\.?(\d{1,5})", c, re.I)
    if m:
        return f"HK.{m.group(2).zfill(5)}"
    m = re.fullmatch(r"(sh|sz)\.?(\d{6})", c, re.I)
    if m:
        return f"{m.group(1).upper()}.{m.group(2)}"
    m = re.fullmatch(r"(us)\.?([A-Za-z][\w.\-]*)", c, re.I)
    if m:
        return f"US.{m.group(2).upper()}"

    # 纯数字裸代码
    if c.isdigit():
        if len(c) == 5:
            return f"HK.{c}"                                   # 00700 港股
        if len(c) < 5:
            return f"HK.{c.zfill(5)}"                          # 700 → 00700
        if len(c) == 6:
            return f"{'SH' if c[0] in '569' else 'SZ'}.{c}"    # 6/9/5→沪，其余→深
        return f"HK.{c}"

    return f"US.{c.upper()}"                                   # 非数字默认美股


# 美股指数代码（futu 里和个股没有形态区别，只能枚举）
_US_INDEX_CODES = {"DJI", "IXIC", "INX", "SPX", "NDX", "RUT", "VIX", "DXY", "GSPC"}


def is_index_code(futu_code):
    """
    判断是否为指数。

    港股指数的代码段以 8 开头（HK.800000 恒生），沪市 000xxx，深市 399xxx。
    """
    market, _, num = futu_code.partition(".")
    if market == "HK":
        return num.startswith("8")
    if market == "SH":
        return num.startswith("000")
    if market == "SZ":
        return num.startswith("399")
    if market == "US":
        return num.upper() in _US_INDEX_CODES
    return False


_KTYPE_MAP = {
    KL_TYPE.K_1M: KLType.K_1M,
    KL_TYPE.K_3M: KLType.K_3M,
    KL_TYPE.K_5M: KLType.K_5M,
    KL_TYPE.K_10M: KLType.K_10M,
    KL_TYPE.K_15M: KLType.K_15M,
    KL_TYPE.K_30M: KLType.K_30M,
    KL_TYPE.K_60M: KLType.K_60M,
    KL_TYPE.K_DAY: KLType.K_DAY,
    KL_TYPE.K_WEEK: KLType.K_WEEK,
    KL_TYPE.K_MON: KLType.K_MON,
    KL_TYPE.K_QUARTER: KLType.K_QUARTER,
    KL_TYPE.K_YEAR: KLType.K_YEAR,
}

_AUTYPE_MAP = {
    AUTYPE.QFQ: AuType.QFQ,
    AUTYPE.HFQ: AuType.HFQ,
    AUTYPE.NONE: AuType.NONE,
}


def _convert_ktype(k_type):
    if k_type not in _KTYPE_MAP:
        raise CChanException(f"富途不支持{k_type}级别的K线数据", ErrCode.PARA_ERROR)
    return _KTYPE_MAP[k_type]


def _convert_autype(autype):
    return _AUTYPE_MAP.get(autype, AuType.QFQ)


def _norm_date(date_str, default):
    """把 20250101 / 2025-01-01 / 2025/01/01 统一成 2025-01-01"""
    if date_str is None or str(date_str).strip() in ("", "None"):
        return default
    s = str(date_str).strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _to_float(x, default=0.0):
    """futu 偶尔给 NaN / None，统一兜底成数字"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


# ======================================================================
# 三、行 → CKLine_Unit 所需字典
# ======================================================================
def parse_futu_time(inp, k_type=KL_TYPE.K_DAY):
    """
    解析 futu 的 time_key。

    日线及以上返回 'YYYY-MM-DD'，分钟线返回 'YYYY-MM-DD HH:MM:SS'。
    日线及以上传 auto=True（chan.py 会把它对齐到当日 23:59），分钟线传 auto=False，
    与项目自带的 ccxt 数据源处理方式保持一致。
    """
    s = str(inp)
    if len(s) >= 19:
        year, month, day = int(s[0:4]), int(s[5:7]), int(s[8:10])
        hour, minute = int(s[11:13]), int(s[14:16])
        return CTime(year, month, day, hour, minute, auto=not kltype_lt_day(k_type))
    if len(s) >= 10:
        return CTime(int(s[0:4]), int(s[5:7]), int(s[8:10]), 0, 0, auto=not kltype_lt_day(k_type))
    raise CChanException(f"富途返回的时间格式无法解析: {inp}", ErrCode.SRC_DATA_FORMAT_ERROR)


def create_item_dict(row, k_type=KL_TYPE.K_DAY):
    """将 futu 返回的 DataFrame 行转换为K线单元所需的字典格式"""
    item = {}

    item[DATA_FIELD.FIELD_TIME] = parse_futu_time(row["time_key"], k_type)
    item[DATA_FIELD.FIELD_OPEN] = _to_float(row["open"])
    item[DATA_FIELD.FIELD_HIGH] = _to_float(row["high"])
    item[DATA_FIELD.FIELD_LOW] = _to_float(row["low"])
    item[DATA_FIELD.FIELD_CLOSE] = _to_float(row["close"])
    item[DATA_FIELD.FIELD_VOLUME] = _to_float(row.get("volume"))
    item[DATA_FIELD.FIELD_TURNOVER] = _to_float(row.get("turnover"))

    # 换手率对指数恒为 0，缺失时补 0
    if DATA_FIELD.FIELD_TURNRATE in row.index:
        item[DATA_FIELD.FIELD_TURNRATE] = _to_float(row[DATA_FIELD.FIELD_TURNRATE])

    return item


# ======================================================================
# 四、拉K线（可单独复用，扫描选股时直接调这个）
# ======================================================================
def fetch_kline(code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
    """
    拉取单只标的的历史K线，返回 DataFrame。

    已处理：日期格式、page_req_key 翻页、按 time_key 去重、按日期裁剪。
    返回的列：code / name / time_key / open / close / high / low / pe_ratio /
              turnover_rate / volume / turnover / change_rate / last_close
    """
    import pandas as pd

    futu_code = normalize_code(code)
    ktype = _convert_ktype(k_type)
    au = _convert_autype(autype)
    start = _norm_date(begin_date, "1990-01-01")
    end = _norm_date(end_date, datetime.now().strftime("%Y-%m-%d"))

    ctx = get_ctx()
    frames = []
    page_key = None
    for _ in range(_FUTU_CFG["max_page"]):
        ret, data, page_key = ctx.request_history_kline(
            futu_code,
            start=start,
            end=end,
            ktype=ktype,
            autype=au,
            max_count=_FUTU_CFG["max_count"],
            page_req_key=page_key,
        )
        if ret != RET_OK:
            raise CChanException(f"富途获取 {futu_code} K线失败: {data}", ErrCode.SRC_DATA_NOT_FOUND)
        if data is None or len(data) == 0:
            break
        frames.append(data)
        if page_key is None:   # 没有下一页
            break

    if not frames:
        raise CChanException(f"富途未返回 {futu_code} 的K线数据（{start}~{end}）", ErrCode.SRC_DATA_NOT_FOUND)

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["time_key"]).sort_values("time_key").reset_index(drop=True)

    # futu 的时间边界按时间戳切，这里再按日期裁一次，保证和 begin_date/end_date 严格一致
    day = df["time_key"].astype(str).str[:10]
    df = df[(day >= start) & (day <= end)].reset_index(drop=True)
    if len(df) == 0:
        raise CChanException(f"富途未返回 {futu_code} 的K线数据（{start}~{end}）", ErrCode.SRC_DATA_NOT_FOUND)
    return df


# ======================================================================
# 五、数据源主体
# ======================================================================
class CFutu(CCommonStockApi):
    """使用富途 OpenD 获取A股/港股/美股/指数数据"""

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super(CFutu, self).__init__(code, k_type, begin_date, end_date, autype)

    def SetBasciInfo(self):
        """解析代码：得到 futu 格式的 futu_code，并判定是否为指数"""
        self.futu_code = normalize_code(self.code)
        self.is_stock = not is_index_code(self.futu_code)
        self.name = self.futu_code

    def get_kl_data(self):
        """获取K线数据"""
        df = fetch_kline(self.futu_code, self.k_type, self.begin_date, self.end_date, self.autype)

        # futu 会顺带返回股票名，用它覆盖默认的代码名，画图时更好认
        if len(df) and "name" in df.columns:
            self.name = str(df["name"].iloc[0])

        # 富途复权数据偶有 open<low 这类畸形K线，autofix=True 时自动修正
        autofix = _FUTU_CFG["autofix"]
        for _, row in df.iterrows():
            yield CKLine_Unit(create_item_dict(row, self.k_type), autofix)

    @classmethod
    def do_init(cls):
        """把连接建起来。OpenD 没起时在这里就报错，不要等到遍历数据时才炸"""
        get_ctx()

    @classmethod
    def do_close(cls):
        """
        故意不关连接。

        chan.py 的 load() 是 do_init → 取数 → finally do_close，
        每构造一个 CChan 就走一轮；真关掉会变成"每只股票重连一次"。
        连接按线程复用，进程退出由 atexit 兜底，也可显式调用 close_futu()。
        """
        pass


# ======================================================================
# 六、附加工具（选股/扫描时用，非 CCommonStockApi 契约要求）
# ======================================================================
_MARKET_MAP = {
    "HK": Market.HK,
    "SH": Market.SH,
    "SZ": Market.SZ,
    "US": Market.US,
}

_SECTYPE_MAP = {
    "STOCK": SecurityType.STOCK,
    "IDX": SecurityType.IDX,
    "ETF": SecurityType.ETF,
    "WARRANT": SecurityType.WARRANT,
    "BOND": SecurityType.BOND,
    "PLATE": SecurityType.PLATE,
}


def get_stock_list(market="HK", sec_type="STOCK"):
    """
    取某个市场的标的代码清单。

    >>> get_stock_list("HK", "STOCK")   # 约 3800 条
    >>> get_stock_list("SH", "IDX")
    """
    mkt = _MARKET_MAP.get(str(market).upper())
    st = _SECTYPE_MAP.get(str(sec_type).upper())
    if mkt is None or st is None:
        raise CChanException(f"未知的市场或证券类型: {market}/{sec_type}", ErrCode.PARA_ERROR)
    ctx = get_ctx()
    ret, data = ctx.get_stock_basicinfo(mkt, st)
    if ret != RET_OK:
        raise CChanException(f"富途获取{market}清单失败: {data}", ErrCode.SRC_DATA_NOT_FOUND)
    return data


def get_snapshot(codes):
    """
    批量取快照（自动按 futu 的 400 上限分批）。

    返回 DataFrame，常用列：code / name / last_price / turnover / volume /
    suspension / listing_date / lot_size ...
    注意：suspension 字段实测恒为字符串 'N/A'，不能当停牌标志用，
          要判停牌请用 volume==0 或 last_price 为空。
    """
    import pandas as pd

    codes = [normalize_code(c) for c in codes]
    if not codes:
        return pd.DataFrame()
    ctx = get_ctx()
    frames = []
    for i in range(0, len(codes), 400):
        ret, data = ctx.get_market_snapshot(codes[i:i + 400])
        if ret != RET_OK:
            raise CChanException(f"富途获取快照失败: {data}", ErrCode.SRC_DATA_NOT_FOUND)
        if data is not None and len(data):
            frames.append(data)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
