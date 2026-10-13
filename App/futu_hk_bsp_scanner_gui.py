"""
港股缠论买点扫描器 (富途 OpenD 版) - Powered by chan.py

功能说明:
    - 批量扫描港股市场，自动识别近期出现买点的股票
    - 支持单只股票的缠论分析和图表展示
    - 可视化显示K线、笔、线段、中枢、买卖点、MACD等

数据来源:
    - 全部走本地 FutuOpenD（默认 127.0.0.1:11111），不依赖任何第三方行情源
    - 股票池: get_stock_basicinfo(Market.HK, SecurityType.STOCK)
    - 行情快照: get_market_snapshot（单次最多 400 只，自动分批）
    - 历史K线: request_history_kline（单页 1000 根，自动翻页）

与A股版(App/ashare_bsp_scanner_gui.py)的结构对应关系:
    get_tradable_stocks()        -> get_hk_universe()
    DATA_SRC.AKSHARE             -> data_src="custom:FutuHKAPI.CFutuHk"
    ScanThread / SingleAnalysisThread / ChanPlotCanvas / 主窗口布局
                                 -> 思路完全一致，仅按港股特点做适配

港股特有的过滤规则(A股版没有):
    - 港股仙股/僵尸股极多(实测中位成交额不足10万港元)，必须按成交额+股价过滤
    - 剔除停牌股(suspension / sec_status=SUSPENDED)
    - 可选剔除创业板 GEM
    - 可选剔除次新股(上市不足N天)
    - 按成交额从高到低取 Top N，避免全市场3800只逐个取K线

涨跌幅配色遵循A股/港股习惯: 涨红跌绿

依赖:
    - PyQt6: GUI框架
    - matplotlib: 图表显示
    - futu-api: 富途行情 SDK
    - chan.py: 缠论分析核心库

使用方法:
    需要先启动 FutuOpenD，然后:
    python App/futu_hk_bsp_scanner_gui.py
"""
import socket
import sys
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

# 将项目根目录加入路径，以便导入 chan.py 核心模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QGroupBox,
    QMessageBox, QStatusBar, QSplitter, QTableWidget, QTableWidgetItem,
    QProgressBar, QHeaderView, QTextEdit, QSpinBox, QDoubleSpinBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

import pandas as pd

from futu import OpenQuoteContext, Market, SecurityType, KLType, AuType, RET_OK

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from Common.func_util import str2float
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_Unit import CKLine_Unit


# ==========================================================================
# 一、连接参数与线程局部上下文
# ==========================================================================
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 11111

#: get_market_snapshot 单次请求的股票数上限（实测超过会报"请求股票个数超过限制400"）
SNAPSHOT_BATCH = 400

#: 全局连接参数。CFutuHk 由 CChan 内部实例化，无法把 host/port 通过构造函数传进去，
#: 因此放到模块级共享变量里，线程在跑之前调用 set_endpoint() 设置。
_FUTU_CFG = {
    'host': DEFAULT_HOST,
    'port': DEFAULT_PORT,
    # 富途复权数据偶有瑕疵(如 open 比 low 更低)，autofix=True 时自动把 low/high 修正到合法区间，
    # 而不是把整只股票判为失败。chan.py 自带的 ccxt 数据源也是这么处理的。
    'autofix': True,
}

#: futu 的 context 内部有后台收发线程，重复创建/销毁开销大且会占满 OpenD 连接数。
#: 这里按"每线程一个 context"缓存起来，线程结束时显式关闭。
_thread_state = threading.local()


class FutuConnectionError(Exception):
    """富途 OpenD 连接相关异常"""


def check_opend(host, port, timeout=2):
    """
    探测 OpenD 端口是否可达

    futu SDK 在 OpenD 没启动时是**无限阻塞**而不是抛错，
    所以建连接前必须先用裸 socket 探活，避免整个界面卡死。

    Args:
        host: str, OpenD 地址
        port: int, OpenD 端口
        timeout: float, 探活超时秒数

    Raises:
        FutuConnectionError: 端口不可达
    """
    sk = socket.socket()
    sk.settimeout(timeout)
    try:
        sk.connect((host, port))
    except Exception as e:
        raise FutuConnectionError(
            f"无法连接 FutuOpenD {host}:{port} ({e})\n"
            f"请先启动富途牛牛客户端并登录，再打开「设置-开放接口」启动 OpenD"
        )
    finally:
        sk.close()


def set_endpoint(host, port, autofix=None):
    """
    设置运行时参数（线程开始前调用）

    Args:
        host: str, OpenD 地址
        port: int, OpenD 端口
        autofix: bool, 是否自动修正畸形K线（None 表示不改动当前值）
    """
    _FUTU_CFG['host'] = host
    _FUTU_CFG['port'] = port
    if autofix is not None:
        _FUTU_CFG['autofix'] = autofix


def get_ctx():
    """
    获取当前线程复用的 OpenQuoteContext（惰性创建）

    Returns:
        OpenQuoteContext: 行情上下文

    Raises:
        FutuConnectionError: OpenD 不可达
    """
    ctx = getattr(_thread_state, 'ctx', None)
    if ctx is None:
        host, port = _FUTU_CFG['host'], _FUTU_CFG['port']
        check_opend(host, port)  # 必须先探活，否则 SDK 会无限阻塞
        ctx = OpenQuoteContext(host=host, port=port)
        _thread_state.ctx = ctx
    return ctx


def close_ctx():
    """关闭当前线程的 OpenQuoteContext（线程结束前必须调用，否则进程退不出）"""
    ctx = getattr(_thread_state, 'ctx', None)
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
        _thread_state.ctx = None


def normalize_hk_code(code):
    """
    归一化港股代码为 'HK.00700' 格式

    Args:
        code: str, 支持 '700' / '0700' / '00700' / 'HK.00700'

    Returns:
        str: 'HK.00700' 形态的代码；纯数字不足则左侧补零到5位

    Raises:
        ValueError: 输入不含任何数字
    """
    s = str(code).strip().upper()
    for prefix in ('HK.', 'HK'):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.lstrip('.:')
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        raise ValueError(f"无法识别的港股代码: {code}")
    return f"HK.{digits.zfill(5)}"


def _unpack3(res):
    """
    兼容 request_history_kline 返回 2 元组/3 元组两种版本

    新版 SDK 多返回一个 page_req_key，按 2 个解包会 ValueError。

    Args:
        res: tuple, 接口返回值

    Returns:
        tuple: (ret_code, data, page_req_key)
    """
    r = list(res)
    if len(r) >= 3:
        return r[0], r[1], r[2]
    return r[0], r[1], None


# ==========================================================================
# 二、行情获取（股票池 / 快照 / K线）
# ==========================================================================
def _to_bool(v):
    """把 'True'/'False'/bool/NaN 统一转成 bool"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == 'true'


def get_hk_universe(min_turnover=5e7, min_price=1.0, include_gem=False,
                    min_list_days=60, top_n=200, progress=None, log=None):
    """
    获取待扫描的港股股票池

    两步走: 先取全市场正股清单，再分批取快照做流动性过滤。
    港股仙股极多，不过滤的话几千只逐个拉K线会拖死扫描。

    Args:
        min_turnover: float, 最低成交额(港元)，默认5000万
        min_price: float, 最低股价(港元)，默认1.0
        include_gem: bool, 是否包含创业板GEM，默认剔除
        min_list_days: int, 上市至少N天，默认60
        top_n: int, 按成交额排序后最多取前N只
        progress: callable, 进度回调 (done, total, msg)
        log: callable, 日志回调

    Returns:
        pd.DataFrame: ['代码','名称','最新价','涨跌幅','成交额']；失败返回空 DataFrame
    """
    ctx = get_ctx()

    def _log(msg):
        if log:
            log(msg)

    ret, basic = ctx.get_stock_basicinfo(Market.HK, SecurityType.STOCK)
    if ret != RET_OK:
        _log(f"❌ 获取港股清单失败: {basic}")
        return pd.DataFrame()

    # 过滤退市(该字段是字符串形式的 True/False)
    if 'delisting' in basic.columns:
        basic = basic[~basic['delisting'].map(_to_bool)]
    # 可选剔除创业板
    if not include_gem and 'exchange_type' in basic.columns:
        basic = basic[basic['exchange_type'] != 'HK_GEMBOARD']
    codes = basic['code'].tolist()
    _log(f"📋 全市场正股 {len(codes)} 只，开始取快照分批过滤...")

    # 分批取快照
    frames = []
    total = len(codes)
    for i in range(0, total, SNAPSHOT_BATCH):
        batch = codes[i:i + SNAPSHOT_BATCH]
        r, snap = ctx.get_market_snapshot(batch)
        if r != RET_OK:
            _log(f"⚠️ 第 {i // SNAPSHOT_BATCH + 1} 批快照失败: {str(snap)[:80]}")
            continue
        frames.append(snap)
        done = min(i + SNAPSHOT_BATCH, total)
        if progress:
            progress(done, total, f"取快照 {done}/{total}")
    if not frames:
        _log("❌ 没有任何快照数据")
        return pd.DataFrame()
    snap = pd.concat(frames, ignore_index=True)

    n_before = len(snap)

    # ---- 过滤 ----
    # 1) 停牌
    if 'suspension' in snap.columns:
        snap = snap[~snap['suspension'].map(_to_bool)]
    if 'sec_status' in snap.columns:
        snap = snap[~snap['sec_status'].astype(str).str.contains('SUSPEND', case=False, na=False)]
    # 2) 价格有效
    snap['last_price'] = pd.to_numeric(snap['last_price'], errors='coerce')
    snap['prev_close_price'] = pd.to_numeric(snap['prev_close_price'], errors='coerce')
    snap['turnover'] = pd.to_numeric(snap['turnover'], errors='coerce')
    snap['volume'] = pd.to_numeric(snap['volume'], errors='coerce')
    snap = snap[snap['last_price'] > 0]
    snap = snap[snap['prev_close_price'] > 0]
    # 3) 股价下限（剔除仙股）+ 流动性下限
    snap = snap[snap['last_price'] >= min_price]
    snap = snap[snap['turnover'] >= min_turnover]
    # 4) 有成交
    snap = snap[snap['volume'] > 0]
    # 5) 次新股
    if min_list_days > 0 and 'listing_date' in snap.columns:
        def _ok(d):
            try:
                dt = datetime.strptime(str(d)[:10], '%Y-%m-%d')
            except Exception:
                return True  # 1970-01-01 这类占位值无法判断，放行
            if dt.year < 1990:
                return True
            return (datetime.now() - dt).days >= min_list_days
        snap = snap[snap['listing_date'].map(_ok)]

    _log(f"🔎 过滤后 {len(snap)} 只（快照 {n_before} 只，成交额≥{min_turnover / 1e4:,.0f}万、股价≥{min_price}）")
    if snap.empty:
        return pd.DataFrame()

    # 涨跌幅
    snap['涨跌幅'] = (snap['last_price'] - snap['prev_close_price']) / snap['prev_close_price'] * 100
    snap = snap.sort_values('turnover', ascending=False).head(top_n)

    out = pd.DataFrame({
        '代码': snap['code'],
        '名称': snap['name'],
        '最新价': snap['last_price'],
        '涨跌幅': snap['涨跌幅'],
        '成交额': snap['turnover'],
    }).reset_index(drop=True)
    _log(f"✅ 待扫描 {len(out)} 只（按成交额 Top {top_n}）")
    return out


def _map_ktype(k_type):
    """把 chan.py 的 KL_TYPE 映射到富途 KLType"""
    _d = {
        KL_TYPE.K_DAY: KLType.K_DAY,
        KL_TYPE.K_WEEK: KLType.K_WEEK,
        KL_TYPE.K_MON: KLType.K_MON,
        KL_TYPE.K_1M: KLType.K_1M,
        KL_TYPE.K_5M: KLType.K_5M,
        KL_TYPE.K_15M: KLType.K_15M,
        KL_TYPE.K_30M: KLType.K_30M,
        KL_TYPE.K_60M: KLType.K_60M,
    }
    if k_type not in _d:
        raise ValueError(f"富途数据源暂不支持 {k_type} 级别的K线")
    return _d[k_type]


def _map_autype(autype):
    """把 chan.py 的 AUTYPE 映射到富途 AuType"""
    _d = {AUTYPE.QFQ: AuType.QFQ, AUTYPE.HFQ: AuType.HFQ, AUTYPE.NONE: AuType.NONE}
    return _d.get(autype, AuType.QFQ)


def fetch_kline(code, begin_date=None, end_date=None, k_type=KL_TYPE.K_DAY,
                autype=AUTYPE.QFQ, max_pages=10):
    """
    取单只港股的历史K线（自动翻页）

    Args:
        code: str, 形如 'HK.00700'
        begin_date: str, 'YYYY-MM-DD'，None 表示不限制
        end_date: str, 'YYYY-MM-DD'，None 表示不限制
        k_type: KL_TYPE, K线级别
        autype: AUTYPE, 复权方式
        max_pages: int, 最多翻多少页（每页1000根）

    Returns:
        pd.DataFrame: 富途原始K线数据
    """
    ctx = get_ctx()
    ktype, atype = _map_ktype(k_type), _map_autype(autype)
    # 注意: 富途要的是 'YYYY-MM-DD'，不像 akshare 那样用 'YYYYMMDD'
    start = begin_date or '1980-01-01'
    end = end_date or datetime.now().strftime('%Y-%m-%d')

    frames, key = [], None
    for _ in range(max_pages):
        ret, data, key = _unpack3(ctx.request_history_kline(
            code, start=start, end=end, ktype=ktype, autype=atype,
            max_count=1000, page_req_key=key,
        ))
        if ret != RET_OK:
            raise RuntimeError(str(data)[:120])
        if data is None or len(data) == 0:
            break
        frames.append(data)
        if key is None:      # 没有续页标记，说明已经取完
            break
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=['time_key'])


def create_item_dict(row, autype):
    """
    把富途K线的一行转成 chan.py 需要的字典

    Args:
        row: pd.Series, 富途K线一行
        autype: AUTYPE, 复权方式（此处仅做签名兼容）

    Returns:
        dict: CKLine_Unit 所需字段
    """
    item = {}
    ts = pd.Timestamp(row['time_key'])
    # 日内级别的圆整到整分时；日线及以上小时分钟为0
    item[DATA_FIELD.FIELD_TIME] = CTime(ts.year, ts.month, ts.day, ts.hour, ts.minute)
    item[DATA_FIELD.FIELD_OPEN] = str2float(row['open'])
    item[DATA_FIELD.FIELD_HIGH] = str2float(row['high'])
    item[DATA_FIELD.FIELD_LOW] = str2float(row['low'])
    item[DATA_FIELD.FIELD_CLOSE] = str2float(row['close'])
    item[DATA_FIELD.FIELD_VOLUME] = str2float(row['volume'])
    item[DATA_FIELD.FIELD_TURNOVER] = str2float(row['turnover'])
    item[DATA_FIELD.FIELD_TURNRATE] = str2float(row.get('turnover_rate', 0))
    return item


# ==========================================================================
# 三、chan.py 数据源插件 + 虚拟模块注册
# ==========================================================================
class CFutuHk(CCommonStockApi):
    """
    chan.py 的自定义数据接口实现（港股 / 富途 OpenD）

    CChan 通过 data_src="custom:FutuHKAPI.CFutuHk" 找到本类，
    调用流程: do_init() -> get_kl_data() -> do_close()
    """

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None,
                 autype=AUTYPE.QFQ):
        super(CFutuHk, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self):
        """按 K 线生成器方式逐根产出CKLine_Unit"""
        df = fetch_kline(self.code, self.begin_date, self.end_date, self.k_type, self.autype)
        autofix = _FUTU_CFG.get('autofix', True)
        for _, row in df.iterrows():
            yield CKLine_Unit(create_item_dict(row, self.autype), autofix=autofix)

    def SetBasciInfo(self):
        """设置基本信息"""
        self.name = self.code
        self.is_stock = True  # 港股全部按个股处理（指数请用 code 前缀过滤）

    @classmethod
    def do_init(cls):
        """
        初始化

        CChan 每构造一次就会走到这里，而 OpenQuoteContext 的创建开销较大，
        所以这里刻意什么都不做，真正的连接由 get_ctx() 惰性建立并按线程复用。
        """
        pass

    @classmethod
    def do_close(cls):
        """
        收尾

        同理不能在这里 close()，否则每只股票都会重连一次 OpenD。
        实际关闭时机: 扫描线程/分析线程结束时调用 close_ctx()。
        """
        pass


_MOD_NAME = 'DataAPI.FutuHKAPI'
_CLS_NAME = 'CFutuHk'


def register_data_api():
    """
    把本文件里的 CFutuHk 注册成一个虚拟模块，让 CChan 的 custom: 机制能识别

    chan.py 在 GetStockAPI() 里写死了 `importlib.import_module(f"DataAPI.{包名}")`，
    即它只会去 DataAPI/ 目录找模块。为了保持「单文件交付」、不改动 chan.py 源码，
    这里用 sys.modules 注入一个虚拟模块 —— import_module 命中 sys.modules 后会直接返回。

    Returns:
        str: data_src 字符串，形如 "custom:FutuHKAPI.CFutuHk"
    """
    if _MOD_NAME in sys.modules:
        return f"custom:{_MOD_NAME.split('.')[1]}.{_CLS_NAME}"
    mod = types.ModuleType(_MOD_NAME)
    mod.CFutuHk = CFutuHk
    sys.modules[_MOD_NAME] = mod
    return f"custom:{_MOD_NAME.split('.')[1]}.{_CLS_NAME}"


DATA_SRC_FUTU_HK = register_data_api()

# CChan / 画图这两个模块要等上面的虚拟模块注册完才能安全导入
from Chan import CChan            # noqa: E402
from ChanConfig import CChanConfig  # noqa: E402


# ==========================================================================
# 四、后台线程
# ==========================================================================
class ScanThread(QThread):
    """
    批量扫描股票的后台线程

    在独立线程中遍历股票列表，每只股票做一次缠论分析，
    检测最近N天内是否出现买点。

    Signals:
        progress: (int, int, str) 当前进度、总数、当前股票信息
        found_signal: (dict) 发现买点
        universe_signal: (int) 股票池规模确定后发出，用于刷新进度条上限
        finished: (int, int) 完成，返回成功数和失败数
        log_signal: (str) 日志消息
        error_signal: (str) 致命错误（如 OpenD 未启动）
    """
    progress = pyqtSignal(int, int, str)
    found_signal = pyqtSignal(dict)
    universe_signal = pyqtSignal(int)
    finished = pyqtSignal(int, int)
    log_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, stock_list, config, days=365, bsp_days=3, interval_ms=30):
        """
        Args:
            stock_list: pd.DataFrame, 待扫描股票列表 ['代码','名称','最新价','涨跌幅','成交额']
            config: CChanConfig, 缠论配置
            days: int, 回看多少天历史数据
            bsp_days: int, 只看最近多少天内的买点
            interval_ms: int, 每只股票之间的请求间隔，用于规避富途接口频率限制
        """
        super().__init__()
        self.stock_list = stock_list
        self.config = config
        self.days = days
        self.bsp_days = bsp_days
        self.interval_ms = interval_ms
        self.is_running = True

    def stop(self):
        """停止扫描，设置标志位让 run() 循环退出"""
        self.is_running = False

    def run(self):
        """
        线程主函数

        流程:
            1. 建立线程专属的 OpenQuoteContext
            2. 逐只取K线 -> 缠论分析 -> 判断买点
            3. 无论正常结束还是异常，最后都关闭 context
        """
        begin_time = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")
        end_time = datetime.now().strftime("%Y-%m-%d")
        total = len(self.stock_list)
        success_count = 0
        fail_count = 0

        try:
            get_ctx()  # 先把连接建起来，OpenD 没起的话这里就会抛错
        except FutuConnectionError as e:
            self.error_signal.emit(str(e))
            self.finished.emit(0, 0)
            return
        except Exception as e:  # OpenD 拒绝连接(如连接数超限)时要给出明确提示
            self.error_signal.emit(f"连接 FutuOpenD 失败: {e}")
            self.finished.emit(0, 0)
            return

        try:
            for idx, row in self.stock_list.iterrows():
                if not self.is_running:
                    break

                code = row['代码']
                name = row['名称']
                self.progress.emit(idx + 1, total, f"{code} {name}")
                self.log_signal.emit(f"🔍 扫描 {code} {name}...")

                try:
                    chan = CChan(
                        code=code,
                        begin_time=begin_time,
                        end_time=end_time,
                        data_src=DATA_SRC_FUTU_HK,
                        lv_list=[KL_TYPE.K_DAY],
                        config=self.config,
                        autype=AUTYPE.QFQ,
                    )

                    # 检查有没有K线数据
                    if len(chan[0]) == 0:
                        fail_count += 1
                        self.log_signal.emit(f"⏭️ {code} {name}: 无K线数据")
                        continue
                    # 检查是否长期停牌
                    last_klu = chan[0][-1][-1]
                    last_time = last_klu.time
                    last_date = datetime(last_time.year, last_time.month, last_time.day)
                    if (datetime.now() - last_date).days > 15:
                        fail_count += 1
                        self.log_signal.emit(f"⏸️ {code} {name}: 停牌超过15天")
                        continue

                    success_count += 1

                    # 只看最近 N 天内出现的买点
                    bsp_list = chan.get_latest_bsp(number=0)  # 返回顺序: 最新 -> 最旧
                    cutoff_date = datetime.now() - timedelta(days=self.bsp_days)
                    buy_points = [
                        bsp for bsp in bsp_list
                        if bsp.is_buy
                        and datetime(bsp.klu.time.year, bsp.klu.time.month, bsp.klu.time.day) >= cutoff_date
                    ]

                    if buy_points:
                        latest_buy = buy_points[0]
                        self.log_signal.emit(f"✅ {code} {name}: 发现买点 {latest_buy.type2str()}")
                        self.found_signal.emit({
                            'code': code,
                            'name': name,
                            'price': row['最新价'],
                            'change': row['涨跌幅'],
                            'turnover': row['成交额'],
                            'bsp_type': latest_buy.type2str(),
                            'bsp_time': str(latest_buy.klu.time),
                            'chan': chan,
                        })
                    else:
                        self.log_signal.emit(f"➖ {code} {name}: 无近期买点")
                except Exception as e:
                    fail_count += 1
                    self.log_signal.emit(f"❌ {code} {name}: {str(e)[:60]}")
                    continue

                # 富途接口有频率配额，稍微喘一口气
                if self.interval_ms > 0 and self.is_running:
                    time.sleep(self.interval_ms / 1000.0)
        finally:
            close_ctx()

        self.finished.emit(success_count, fail_count)


class SingleAnalysisThread(QThread):
    """
    单只股票分析的后台线程

    用于分析用户手动输入的股票代码，避免阻塞 UI。

    Signals:
        finished: (str, object) 分析完成，返回 (代码, CChan)
        error: (str) 出错信息
    """
    finished = pyqtSignal(str, object)
    error = pyqtSignal(str)

    def __init__(self, code, config, days=365):
        """
        Args:
            code: str, 形如 'HK.00700'
            config: CChanConfig, 缠论配置
            days: int, 回看天数
        """
        super().__init__()
        self.code = code
        self.config = config
        self.days = days

    def run(self):
        """执行缠论分析"""
        try:
            get_ctx()
        except FutuConnectionError as e:
            self.error.emit(str(e))
            return
        except Exception as e:
            self.error.emit(f"连接 FutuOpenD 失败: {e}")
            return
        try:
            begin_time = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")
            end_time = datetime.now().strftime("%Y-%m-%d")
            chan = CChan(
                code=self.code,
                begin_time=begin_time,
                end_time=end_time,
                data_src=DATA_SRC_FUTU_HK,
                lv_list=[KL_TYPE.K_DAY],
                config=self.config,
                autype=AUTYPE.QFQ,
            )
            self.finished.emit(self.code, chan)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            close_ctx()


class ChanPlotCanvas(FigureCanvas):
    """
    嵌入 PyQt 的 Matplotlib 画布

    Args:
        parent: 父控件
        width: 图表宽度（英寸）
        height: 图表高度（英寸）
    """

    def __init__(self, parent=None, width=12, height=8):
        self.fig = Figure(figsize=(width, height), dpi=100)
        super().__init__(self.fig)
        self.setParent(parent)
        self.setMinimumHeight(400)

    def clear(self):
        """清空画布内容"""
        self.fig.clear()
        self.draw()


# ==========================================================================
# 五、主窗口
# ==========================================================================
#: 涨红跌绿（A股/港股习惯）
COLOR_UP = QColor('#D32F2F')
COLOR_DOWN = QColor('#2E7D32')


class FutuHKGUI(QMainWindow):
    """
    港股缠论买点扫描器主窗口

    布局与A股版保持一致:
        - 左侧面板: OpenD连接、扫描设置、单股输入、买点列表、扫描日志
        - 右侧面板: 图表显示区域，支持缩放和导航
    """

    def __init__(self):
        super().__init__()
        self.chan = None
        self.scan_thread = None
        self.analysis_thread = None
        self.stock_cache = {}  # {代码: CChan}
        self.init_ui()

    # ---------------- UI ----------------
    def init_ui(self):
        """初始化用户界面"""
        self.setWindowTitle('港股缠论买点扫描器 - Powered by chan.py + 富途 OpenD')
        self.setGeometry(100, 100, 1600, 900)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        left_panel = self.create_left_panel()
        right_panel = self.create_chart_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([460, 1140])
        main_layout.addWidget(splitter)

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage('就绪 - 确认 OpenD 已启动后点击"开始扫描"')

    def create_left_panel(self):
        """创建左侧面板"""
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # ===== OpenD 连接 =====
        conn_group = QGroupBox("富途 OpenD 连接")
        conn_layout = QVBoxLayout(conn_group)
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("地址:"))
        self.host_input = QLineEdit(DEFAULT_HOST)
        self.host_input.setFixedWidth(110)
        conn_row.addWidget(self.host_input)
        conn_row.addWidget(QLabel("端口:"))
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(DEFAULT_PORT)
        conn_row.addWidget(self.port_input)
        self.test_conn_btn = QPushButton("测试连接")
        self.test_conn_btn.clicked.connect(self.test_connection)
        conn_row.addWidget(self.test_conn_btn)
        conn_layout.addLayout(conn_row)
        self.conn_label = QLabel("未检测")
        conn_layout.addWidget(self.conn_label)
        layout.addWidget(conn_group)

        # ===== 扫描设置 =====
        scan_group = QGroupBox("扫描设置")
        scan_layout = QVBoxLayout(scan_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("最小成交额(万港元):"))
        self.min_turnover_input = QDoubleSpinBox()
        self.min_turnover_input.setRange(0, 1e7)
        self.min_turnover_input.setDecimals(0)
        self.min_turnover_input.setValue(5000)   # 5000万港元
        row1.addWidget(self.min_turnover_input)
        scan_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("最低股价(港元):    "))
        self.min_price_input = QDoubleSpinBox()
        self.min_price_input.setRange(0, 1000)
        self.min_price_input.setDecimals(2)
        self.min_price_input.setValue(1.0)
        row2.addWidget(self.min_price_input)
        scan_layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("扫描只数(Top):     "))
        self.topn_input = QSpinBox()
        self.topn_input.setRange(1, 3000)
        self.topn_input.setValue(200)
        row3.addWidget(self.topn_input)
        scan_layout.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("回看天数:          "))
        self.days_input = QSpinBox()
        self.days_input.setRange(60, 2000)
        self.days_input.setValue(365)
        row4.addWidget(self.days_input)
        scan_layout.addLayout(row4)

        row5 = QHBoxLayout()
        row5.addWidget(QLabel("买点窗口(N天内):   "))
        self.bsp_days_input = QSpinBox()
        self.bsp_days_input.setRange(1, 60)
        self.bsp_days_input.setValue(3)
        row5.addWidget(self.bsp_days_input)
        scan_layout.addLayout(row5)

        row6 = QHBoxLayout()
        row6.addWidget(QLabel("请求间隔(ms):      "))
        self.interval_input = QSpinBox()
        self.interval_input.setRange(0, 2000)
        self.interval_input.setValue(30)
        row6.addWidget(self.interval_input)
        scan_layout.addLayout(row6)

        self.gem_cb = QCheckBox("包含创业板(GEM)")
        self.gem_cb.setChecked(False)
        scan_layout.addWidget(self.gem_cb)

        self.new_stock_cb = QCheckBox("剔除次新股(上市<60天)")
        self.new_stock_cb.setChecked(True)
        scan_layout.addWidget(self.new_stock_cb)

        self.bi_strict_cb = QCheckBox("笔严格模式")
        self.bi_strict_cb.setChecked(True)
        scan_layout.addWidget(self.bi_strict_cb)

        # 富途复权数据偶有瑕疵(实测出现过 open=24.50 < low=24.56 的畸形K线)，
        # 勾选时自动修正后继续分析；不勾选则按 chan.py 的严格校验把该股票判为失败并跳过。
        self.autofix_cb = QCheckBox("自动修正异常K线(推荐)")
        self.autofix_cb.setChecked(True)
        self.autofix_cb.setToolTip("富途复权数据偶有 open<low 这类畸形K线，勾选后自动修正，避免整只股票被跳过")
        scan_layout.addWidget(self.autofix_cb)

        self.custom_codes_input = QLineEdit()
        self.custom_codes_input.setPlaceholderText("自选代码，逗号分隔，如 700,1024,9988（留空=扫描全市场）")
        scan_layout.addWidget(self.custom_codes_input)

        btn_layout = QHBoxLayout()
        self.scan_btn = QPushButton("开始扫描")
        self.scan_btn.clicked.connect(self.start_scan)
        self.scan_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-size: 14px;
                padding: 10px;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #45a049; }
            QPushButton:disabled { background-color: #cccccc; }
        """)
        btn_layout.addWidget(self.scan_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_scan)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                font-size: 14px;
                padding: 10px;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #da190b; }
            QPushButton:disabled { background-color: #cccccc; }
        """)
        btn_layout.addWidget(self.stop_btn)
        scan_layout.addLayout(btn_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        scan_layout.addWidget(self.progress_bar)
        self.progress_label = QLabel("")
        scan_layout.addWidget(self.progress_label)

        layout.addWidget(scan_group)

        # ===== 单只股票分析 =====
        single_group = QGroupBox("单只股票分析")
        single_layout = QVBoxLayout(single_group)
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("股票代码:"))
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("如: 00700 或 700 或 HK.00700")
        code_row.addWidget(self.code_input)
        self.analyze_btn = QPushButton("分析")
        self.analyze_btn.clicked.connect(self.analyze_single)
        code_row.addWidget(self.analyze_btn)
        single_layout.addLayout(code_row)
        layout.addWidget(single_group)

        # ===== 买点列表 =====
        list_group = QGroupBox("买点股票列表")
        list_layout = QVBoxLayout(list_group)
        self.stock_table = QTableWidget()
        self.stock_table.setColumnCount(6)
        self.stock_table.setHorizontalHeaderLabels(['代码', '名称', '现价', '涨跌%', '成交额(万)', '买点'])
        self.stock_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.stock_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stock_table.cellClicked.connect(self.on_stock_clicked)
        list_layout.addWidget(self.stock_table)
        self.clear_list_btn = QPushButton("清空列表")
        self.clear_list_btn.clicked.connect(self.clear_stock_list)
        list_layout.addWidget(self.clear_list_btn)
        layout.addWidget(list_group)

        # ===== 日志 =====
        log_group = QGroupBox("扫描日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        log_layout.addWidget(self.log_text)
        clear_log_btn = QPushButton("清空日志")
        clear_log_btn.clicked.connect(lambda: self.log_text.clear())
        log_layout.addWidget(clear_log_btn)
        layout.addWidget(log_group)

        return panel

    def create_chart_panel(self):
        """创建右侧图表面板"""
        panel = QWidget()
        layout = QVBoxLayout(panel)

        config_layout = QHBoxLayout()
        self.plot_kline_cb = QCheckBox("K线")
        self.plot_kline_cb.setChecked(True)
        config_layout.addWidget(self.plot_kline_cb)
        self.plot_bi_cb = QCheckBox("笔")
        self.plot_bi_cb.setChecked(True)
        config_layout.addWidget(self.plot_bi_cb)
        self.plot_seg_cb = QCheckBox("线段")
        self.plot_seg_cb.setChecked(True)
        config_layout.addWidget(self.plot_seg_cb)
        self.plot_zs_cb = QCheckBox("中枢")
        self.plot_zs_cb.setChecked(True)
        config_layout.addWidget(self.plot_zs_cb)
        self.plot_bsp_cb = QCheckBox("买卖点")
        self.plot_bsp_cb.setChecked(True)
        config_layout.addWidget(self.plot_bsp_cb)
        self.plot_macd_cb = QCheckBox("MACD")
        self.plot_macd_cb.setChecked(True)
        config_layout.addWidget(self.plot_macd_cb)
        config_layout.addStretch()
        self.refresh_btn = QPushButton("刷新图表")
        self.refresh_btn.clicked.connect(self.refresh_chart)
        config_layout.addWidget(self.refresh_btn)
        layout.addLayout(config_layout)

        self.canvas = ChanPlotCanvas(panel, width=12, height=8)
        self.toolbar = NavigationToolbar(self.canvas, panel)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)
        return panel

    # ---------------- 配置 ----------------
    def get_chan_config(self):
        """
        获取缠论分析配置（与A股版一致）

        Returns:
            CChanConfig: 笔严格模式、买卖点类型等配置
        """
        return CChanConfig({
            "bi_strict": self.bi_strict_cb.isChecked(),
            "trigger_step": False,
            "skip_step": 0,
            "divergence_rate": float("inf"),
            "bsp2_follow_1": False,
            "bsp3_follow_1": False,
            "min_zs_cnt": 0,
            "bs1_peak": False,
            "macd_algo": "peak",
            "bs_type": "1,1p,2,2s,3a,3b",
            "print_warning": False,
            "zs_algo": "normal",
        })

    def get_plot_config(self):
        """获取图表绘制配置"""
        return {
            "plot_kline": self.plot_kline_cb.isChecked(),
            "plot_kline_combine": True,
            "plot_bi": self.plot_bi_cb.isChecked(),
            "plot_seg": self.plot_seg_cb.isChecked(),
            "plot_zs": self.plot_zs_cb.isChecked(),
            "plot_macd": self.plot_macd_cb.isChecked(),
            "plot_bsp": self.plot_bsp_cb.isChecked(),
        }

    # ---------------- 连接测试 ----------------
    def test_connection(self):
        """测试 OpenD 是否可达"""
        host = self.host_input.text().strip() or DEFAULT_HOST
        port = self.port_input.value()
        try:
            check_opend(host, port)
        except FutuConnectionError as e:
            self.conn_label.setText("❌ 未连接")
            self.conn_label.setStyleSheet("color:#D32F2F;")
            QMessageBox.critical(self, "连接失败", str(e))
            return
        self.conn_label.setText(f"✅ {host}:{port} 已连接")
        self.conn_label.setStyleSheet("color:#2E7D32;")

    # ---------------- 扫描 ----------------
    def _build_universe(self):
        """
        构造待扫描股票池（主线程执行，快照分批期间会 processEvents 保持界面响应）

        Returns:
            pd.DataFrame or None: 股票池；失败返回 None
        """
        set_endpoint(self.host_input.text().strip() or DEFAULT_HOST,
                     self.port_input.value(),
                     autofix=self.autofix_cb.isChecked())
        custom = self.custom_codes_input.text().strip()
        if custom:
            codes, bad = [], []
            for item in custom.replace('，', ',').split(','):
                item = item.strip()
                if not item:
                    continue
                try:
                    codes.append(normalize_hk_code(item))
                except ValueError:
                    bad.append(item)
            if bad:
                self.log_message(f"⚠️ 忽略无法识别的代码: {', '.join(bad)}")
            if not codes:
                QMessageBox.warning(self, "警告", "没有可扫描的自选代码")
                return None
            return pd.DataFrame({'代码': codes, '名称': [''] * len(codes),
                                 '最新价': [0.0] * len(codes), '涨跌幅': [0.0] * len(codes),
                                 '成交额': [0.0] * len(codes)})

        try:
            # 先确保 OpenD 通，避免主线程被 SDK 无限阻塞卡死
            check_opend(_FUTU_CFG['host'], _FUTU_CFG['port'])
        except FutuConnectionError as e:
            QMessageBox.critical(self, "连接失败", str(e))
            return None

        close_ctx()  # 主线程可能残留上次测试用的 context
        try:
            get_ctx()
            df = get_hk_universe(
                min_turnover=self.min_turnover_input.value() * 1e4,
                min_price=self.min_price_input.value(),
                include_gem=self.gem_cb.isChecked(),
                min_list_days=60 if self.new_stock_cb.isChecked() else 0,
                top_n=self.topn_input.value(),
                progress=lambda d, t, m: (self.progress_bar.setMaximum(t),
                                          self.progress_bar.setValue(d),
                                          self.progress_label.setText(m),
                                          QApplication.processEvents()),
                log=self.log_message,
            )
        finally:
            close_ctx()
        if df.empty:
            QMessageBox.warning(self, "警告", "没有找到符合条件的股票，试试放宽成交额/股价限制")
            return None
        return df

    def start_scan(self):
        """开始批量扫描"""
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.stock_cache.clear()

        self.statusBar.showMessage('正在获取港股快照列表...')
        QApplication.processEvents()

        stock_list = self._build_universe()
        if stock_list is None:
            self._reset_scan_buttons()
            return

        n = len(stock_list)
        self.statusBar.showMessage(f'获取到 {n} 只待扫描股票，开始缠论分析...')
        self.progress_bar.setMaximum(n)
        self.progress_bar.setValue(0)

        config = self.get_chan_config()
        self.scan_thread = ScanThread(
            stock_list, config,
            days=self.days_input.value(),
            bsp_days=self.bsp_days_input.value(),
            interval_ms=self.interval_input.value(),
        )
        self.scan_thread.progress.connect(self.on_scan_progress)
        self.scan_thread.found_signal.connect(self.on_buy_point_found)
        self.scan_thread.finished.connect(self.on_scan_finished)
        self.scan_thread.log_signal.connect(self.on_log_message)
        self.scan_thread.error_signal.connect(self.on_scan_error)
        self.scan_thread.start()

    def stop_scan(self):
        """停止扫描"""
        if self.scan_thread:
            self.scan_thread.stop()
        self.statusBar.showMessage('正在停止扫描...')

    def _reset_scan_buttons(self):
        """恢复按钮状态"""
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)

    def on_scan_progress(self, current, total, stock_info):
        """扫描进度更新"""
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"进度: {current}/{total} - {stock_info}")

    def on_log_message(self, msg):
        """显示日志"""
        self.log_message(msg)

    def log_message(self, msg):
        """追加一条日志"""
        self.log_text.append(msg)

    def on_scan_error(self, msg):
        """扫描线程的致命错误"""
        QMessageBox.critical(self, "错误", msg)
        self.log_message(f"❌ {msg}")
        self._reset_scan_buttons()
        self.statusBar.showMessage('扫描中断')

    def on_buy_point_found(self, data):
        """
        发现买点，插入列表

        Args:
            data: dict, 含代码/名称/价格/买点类型/成交额/CChan
        """
        row = self.stock_table.rowCount()
        self.stock_table.insertRow(row)
        self.stock_table.setItem(row, 0, QTableWidgetItem(data['code']))
        self.stock_table.setItem(row, 1, QTableWidgetItem(data['name'] or data['code']))
        self.stock_table.setItem(row, 2, QTableWidgetItem(f"{data['price']:.2f}"))
        self.stock_table.setItem(row, 3, QTableWidgetItem(f"{data['change']:.2f}%"))
        self.stock_table.setItem(row, 4, QTableWidgetItem(f"{data['turnover'] / 1e4:,.0f}"))
        self.stock_table.setItem(row, 5, QTableWidgetItem(f"{data['bsp_type']} ({data['bsp_time']})"))

        # 涨红跌绿
        color = COLOR_UP if data['change'] > 0 else (COLOR_DOWN if data['change'] < 0 else QColor('#333333'))
        for col in (2, 3):
            self.stock_table.item(row, col).setForeground(QBrush(color))

        self.stock_cache[data['code']] = data['chan']

    def on_scan_finished(self, success_count, fail_count):
        """扫描完成"""
        self._reset_scan_buttons()
        found_count = self.stock_table.rowCount()
        self.statusBar.showMessage(
            f'扫描完成: 成功{success_count}只, 跳过{fail_count}只, 发现{found_count}只买点股票')
        self.progress_label.setText(f"完成: 成功{success_count}, 跳过{fail_count}, 买点{found_count}")

    def on_stock_clicked(self, row, col):
        """点击股票列表，切换右侧图表"""
        code = self.stock_table.item(row, 0).text()
        name = self.stock_table.item(row, 1).text()
        if code in self.stock_cache:
            self.chan = self.stock_cache[code]
            self.plot_chart()
            self.statusBar.showMessage(f'显示: {code} {name}')
        else:
            self.analyze_stock(code)

    # ---------------- 单股分析 ----------------
    def analyze_single(self):
        """分析输入框里的股票"""
        raw = self.code_input.text().strip()
        if not raw:
            QMessageBox.warning(self, "警告", "请输入股票代码")
            return
        try:
            code = normalize_hk_code(raw)
        except ValueError:
            QMessageBox.warning(self, "警告", f"无法识别的代码: {raw}")
            return
        self.analyze_stock(code)

    def analyze_stock(self, code):
        """分析指定股票"""
        self.analyze_btn.setEnabled(False)
        self.statusBar.showMessage(f'正在分析 {code}...')
        # 单股分析走独立线程，同样要先把连接参数同步过去
        set_endpoint(self.host_input.text().strip() or DEFAULT_HOST,
                     self.port_input.value(),
                     autofix=self.autofix_cb.isChecked())
        config = self.get_chan_config()
        self.analysis_thread = SingleAnalysisThread(code, config, days=self.days_input.value())
        self.analysis_thread.finished.connect(self.on_analysis_finished)
        self.analysis_thread.error.connect(self.on_analysis_error)
        self.analysis_thread.start()

    def on_analysis_finished(self, code, chan):
        """单股分析完成"""
        self.chan = chan
        self.stock_cache[code] = chan
        self.analyze_btn.setEnabled(True)
        self.plot_chart()
        self.statusBar.showMessage(f'分析完成: {code}')

    def on_analysis_error(self, error_msg):
        """单股分析出错"""
        self.analyze_btn.setEnabled(True)
        QMessageBox.critical(self, "分析错误", error_msg)
        self.statusBar.showMessage('分析失败')

    # ---------------- 绘图 ----------------
    def plot_chart(self):
        """
        绘制当前股票的缠论图表

        使用 CPlotDriver 生成，含K线、笔、线段、中枢、买卖点、MACD。
        """
        if not self.chan:
            return
        try:
            from Plot.PlotDriver import CPlotDriver

            plt.close('all')
            plot_config = self.get_plot_config()
            canvas_width = self.canvas.width()
            dpi = 100
            fig_width = canvas_width / dpi
            fig_height = fig_width * 0.5
            plot_para = {"figure": {"x_range": 200, "w": fig_width, "h": fig_height}}

            plot_driver = CPlotDriver(self.chan, plot_config=plot_config, plot_para=plot_para)
            self.canvas.fig = plot_driver.figure
            self.canvas.figure = plot_driver.figure
            self.canvas.draw()
            self.toolbar.update()
        except Exception as e:
            QMessageBox.critical(self, "绘图错误", str(e))

    def refresh_chart(self):
        """刷新图表"""
        self.plot_chart()

    def clear_stock_list(self):
        """清空股票列表"""
        self.stock_table.setRowCount(0)
        self.stock_cache.clear()
        self.statusBar.showMessage('列表已清空')

    def closeEvent(self, event):
        """窗口关闭前清理线程资源，避免非 daemon 线程让进程退不掉"""
        for th in (self.scan_thread, self.analysis_thread):
            if th is not None and th.isRunning():
                stop = getattr(th, 'stop', None)  # SingleAnalysisThread 没有 stop()
                if stop:
                    stop()
                th.wait(3000)
        close_ctx()
        super().closeEvent(event)


def main():
    """程序入口"""
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = FutuHKGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
