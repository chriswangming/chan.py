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

# FutuOpenD 的默认监听地址/端口（OpenD 配置文件里可改，改了就用 set_futu_config 同步）
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
    # 裸 socket 探活：futu SDK 在 OpenD 没起时是「长时间阻塞重试」而不是立刻报错，
    # 先用带超时的 socket 探一次，探不通就当没起，避免整轮扫描卡死
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
        """
        替换 threading.Thread.start 的钩子：只把 futu 自己起的线程设成 daemon。

        Note:
            靠 _target.__module__ 是否以 "futu" 开头判断是不是 futu 起的线程，
            只改 daemon 标志 —— daemon 只决定「进程退出时要不要等这个线程」，
            不影响正常取数；整体用 try/except 包住，
            宁可漏设 daemon 也不能让建连接这件事失败。
        """
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
    """
    获取当前线程的 OpenQuoteContext（不存在则建，已断线则重建）。

    Raises:
        CChanException: 连不上 OpenD（ErrCode.SRC_DATA_NOT_FOUND）

    Note:
        连接按线程复用，但 OpenD 中途重启后旧 ctx 会变成一条死连接 ——
        原来只判断「ctx 是否存在」，会一直复用死连接直到手动 close_futu()，
        表现为"某次扫描开始后所有请求都失败"。这里在复用前先探一次端口，
        探不通就丢弃重建（只影响当前线程，别的线程不受影响）。
    """
    host, port = _FUTU_CFG["host"], int(_FUTU_CFG["port"])
    ctx = getattr(_TLS, "ctx", None)
    if ctx is not None:
        if check_opend_alive(host, port):
            return ctx
        # OpenD 已经不在了：先把这条死连接从 TLS 与全局集合里摘掉，再往下重建
        close_futu()
    if not check_opend_alive(host, port):
        raise CChanException(
            f"无法连接富途 OpenD：{host}:{port}，请先启动 FutuOpenD 并确认已登录",
            ErrCode.SRC_DATA_NOT_FOUND,
        )
    # 本模块不做任何请求限频/退避：futu 对历史K线有频率上限（如 30 秒内若干次），
    # 批量扫描时超限会拿到限频错误并直接抛异常，节奏需要调用方自己控制
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
    # close 抛异常也要继续往下清 TLS/集合，否则下次取数会拿到一条已经死掉的连接
    try:
        ctx.close()
    except Exception as e:
        # 原来是裸 except: pass，连接泄漏/关闭失败完全没有可观测信号，这里补一条告警
        print(f"[WARNING] 关闭富途连接失败: {e}")
    _TLS.ctx = None
    with _ALL_CTX_LOCK:
        _ALL_CTX.discard(ctx)


def close_all_futu():
    """关闭所有线程创建的连接（atexit 兜底）"""
    with _ALL_CTX_LOCK:
        ctxs = list(_ALL_CTX)
        _ALL_CTX.clear()
    # 先拷一份再清空：避免遍历过程中集合被别的线程改动
    for ctx in ctxs:
        # 逐条 close，任一条失败不影响其余连接被回收
        try:
            ctx.close()
        except Exception as e:
            print(f"[WARNING] 关闭富途连接失败: {e}")


# 进程退出兜底：即使调用方忘了 close_futu()，atexit 也会把所有线程的连接收掉
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
        # 6 位按首位判沪深：6/9 是沪市（含 B 股）、5 是沪市 ETF/基金，其余（0/3 等）归深市
        if len(c) == 6:
            return f"{'SH' if c[0] in '569' else 'SZ'}.{c}"    # 6/9/5→沪，其余→深
        # 7 位以上纯数字不是任何市场的合法代码，原先一律兜底成 HK.{c} 会把错误代码
        # 静默变成一只港股（表现为"查无此股"），这里直接报错更好排障
        raise CChanException(f"无法识别的股票代码（纯数字但位数非法）: {code}", ErrCode.PARA_ERROR)

    return f"US.{c.upper()}"                                   # 非数字默认美股


# 美股指数代码（futu 里和个股没有形态区别，只能枚举）
_US_INDEX_CODES = {"DJI", "IXIC", "INX", "SPX", "NDX", "RUT", "VIX", "DXY", "GSPC"}


def is_index_code(futu_code):
    """
    判断是否为指数。

    港股指数的代码段以 8 开头（HK.800000 恒生），沪市 000xxx，深市 399xxx。
    """
    # futu 的 basicinfo 里没有「是否指数」这种字段，只能靠代码段特征按市场分别判定。
    # 注意这是**枚举式**判定：未收录的指数段（以及 _US_INDEX_CODES 之外的美股指数）
    # 会被当成个股，误判不报错，只在后续取数/过滤时才可能暴露。
    # 接入新市场的指数时记得同步扩充下面的分支与 _US_INDEX_CODES。
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


# 项目 KL_TYPE -> futu KLType：两边命名几乎一致，但 futu 最短只到 1 分钟线，
# 所以项目里的秒线级别（K_1S/K_3S/K_5S...）在表里没有对应项，会在 _convert_ktype 被挡掉
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

# AUTYPE -> futu AuType，三项一一对应：QFQ 前复权 / HFQ 后复权 / NONE 不复权
_AUTYPE_MAP = {
    AUTYPE.QFQ: AuType.QFQ,
    AUTYPE.HFQ: AuType.HFQ,
    AUTYPE.NONE: AuType.NONE,
}


def _convert_ktype(k_type):
    """
    把项目的 KL_TYPE 映射成 futu 的 KLType。

    Args:
        k_type: KL_TYPE

    Returns:
        KLType: 对应的 futu 周期常量

    Raises:
        CChanException: futu 不支持该级别（ErrCode.PARA_ERROR）。
            futu 最短只到 1 分钟线，所以所有秒线级别（K_1S/K_3S/...）都在这里被挡掉
    """
    if k_type not in _KTYPE_MAP:
        raise CChanException(f"富途不支持{k_type}级别的K线数据", ErrCode.PARA_ERROR)
    return _KTYPE_MAP[k_type]


def _convert_autype(autype):
    """
    把项目的 AUTYPE 映射成 futu 的 AuType。

    Args:
        autype: AUTYPE，QFQ=前复权 / HFQ=后复权 / NONE=不复权

    Returns:
        AuType: 对应的 futu 复权常量

    Raises:
        CChanException: 传入的 autype 不在映射表里（ErrCode.PARA_ERROR）

    Note:
        原来是 `_AUTYPE_MAP.get(autype, AuType.QFQ)`，传错或传 None 会**静默退化成
        前复权**，用户拿到与预期不符的价格却不自知。改成显式报错更符合"配置错了要尽早炸"。

        复权只重算价格序列（open/high/low/close），成交量不受影响；
        富途的复权价偶有瑕疵（实测出现过 open < low 的畸形K线），
        由下游 CKLine_Unit(autofix=True) 兜底修正，见 _FUTU_CFG["autofix"]。
    """
    if autype not in _AUTYPE_MAP:
        raise CChanException(f"富途不支持的复权方式: {autype}", ErrCode.PARA_ERROR)
    return _AUTYPE_MAP[autype]


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

    Note（待确认，未改动行为）：
        本函数假定 futu 分钟线的 time_key 就是这根K线的**结束**时间，
        与项目「天级别以下时间戳 = 结束时间」的约定一致（baostock 那边已确认是结束时间）。
        若 futu 实际返回的是**开始**时间，则所有分钟线会比真实结束时间早一个周期，
        跨级别父子包含会整体错位一根 —— 需要用一根已知的 5 分钟线实测确认
        （首根应为 09:35 而非 09:30）后再决定是否要补一个周期。

        另一个边界：若某根分钟线 time_key 落在 00:00（美股/夜盘），auto=False 会让
        它的时间戳为当日 00:00，从而小于前一日日线的 23:59，跨级别比较会错乱。
        A股/港股不会触发，美股夜盘场景需注意。
    """
    s = str(inp)
    # 分钟线 time_key 形如 "2025-01-02 09:30:00"：带时分，就是这根K线的时间戳
    if len(s) >= 19:
        year, month, day = int(s[0:4]), int(s[5:7]), int(s[8:10])
        hour, minute = int(s[11:13]), int(s[14:16])
        return CTime(year, month, day, hour, minute, auto=not kltype_lt_day(k_type))
    # 日线及以上 time_key 只有 "2025-01-02"：时分补 0，靠 auto=True 落到 23:59
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
    # 用 row.get：指数/部分标的没有 volume、turnover，缺列时 _to_float 兜底成 0.0
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
    # 不限起始日时给一个足够早的日期，让 futu 从它能给的最早数据开始返回
    start = _norm_date(begin_date, "1990-01-01")
    # 不限结束日时取**本机**当天。注意全模块无时区处理：
    # 若 OpenD 在香港/美东时区而本机在东八区，跨时区可能差一天（美股日线最明显）。
    # 有跨时区需求时请显式传 end_date，不要依赖这个默认值。
    end = _norm_date(end_date, datetime.now().strftime("%Y-%m-%d"))

    # 连接按线程复用；OpenD 没起时 get_ctx 会先用 socket 探活再抛错，不会卡死
    ctx = get_ctx()
    frames = []
    page_key = None
    # futu 单次请求最多返回 1000 根，靠上一页回传的 page_req_key 翻页；
    # max_page 是防死循环的保护上限，跑满即退出
    pages = 0
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
        # 失败（含 OpenD 限频、订阅权限不足）不重试也不退避，直接抛给上层
        if ret != RET_OK:
            raise CChanException(f"富途获取 {futu_code} K线失败: {data}", ErrCode.SRC_DATA_NOT_FOUND)
        # 空页 = 这段区间真的没有数据，提前结束而不是继续翻
        if data is None or len(data) == 0:
            break
        frames.append(data)
        pages += 1
        # page_req_key 为 None 是唯一的「没有下一页」信号，也是正常退出的唯一出口
        if page_key is None:   # 没有下一页
            break
    else:
        # for-else：循环正常跑完（没有 break）说明还有下一页，却被 max_page 截断了。
        # 原来这里静默返回已取到的部分，超长历史会被悄悄截断；改成显式告警。
        print(f"[WARNING] 富途 {futu_code} 翻页达到上限 {_FUTU_CFG['max_page']}，"
              f"数据可能被截断（已取 {pages} 页），请缩小日期区间或调大 max_page")

    if not frames:
        raise CChanException(f"富途未返回 {futu_code} 的K线数据（{start}~{end}）", ErrCode.SRC_DATA_NOT_FOUND)

    df = pd.concat(frames, ignore_index=True)
    # 翻页接缝处可能返回重复行，先按 time_key 去重再排序，保证时间严格递增
    # （Chan 侧对非单调递增的K线会直接抛 KL_NOT_MONOTONOUS）
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
        """
        Args:
            code: 标的代码，"HK.00700" / "hk00700" / "00700" / "SH.600000" /
                "600000" / "US.AAPL" / "AAPL" 等写法都认，由 normalize_code 统一归一化
            k_type: KL_TYPE，默认日线；futu 最短只支持 K_1M
            begin_date: 起始日期，"20250101" / "2025-01-01" / "2025/01/01" 都接受；None 视为 1990-01-01
            end_date: 结束日期，格式同 begin_date；None 取**本机当天**
            autype: AUTYPE 复权方式，默认前复权

        Note:
            基类 __init__ 末尾会自动调 SetBasciInfo()，那里完成代码归一化与
            指数判定，所以取数真正用的 self.futu_code 在构造完成时就已就绪。
        """
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
            # 原来直接 str(...)：futu 返回 NaN 时会变成字符串 "nan"，且不判空值。
            # 拿不到有效名字时保持 SetBasciInfo 里设的代码名，比显示 "nan" 好。
            _n = df["name"].iloc[0]
            if _n is not None and str(_n).strip() not in ("", "nan", "NaN", "None", "null"):
                self.name = str(_n)

        # 富途复权数据偶有 open<low 这类畸形K线，autofix=True 时自动修正
        autofix = _FUTU_CFG["autofix"]
        # df 已经全量在内存里，这里逐行转 KLU 后 yield，保持 Chan 边算边推进的节奏
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
    # futu 单次快照最多 400 个代码，超了要自己分批；批内失败整批抛错，不做重试
    for i in range(0, len(codes), 400):
        ret, data = ctx.get_market_snapshot(codes[i:i + 400])
        if ret != RET_OK:
            raise CChanException(f"富途获取快照失败: {data}", ErrCode.SRC_DATA_NOT_FOUND)
        if data is not None and len(data):
            frames.append(data)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
