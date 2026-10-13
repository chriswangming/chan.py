"""
A股缠论买点扫描器 - Powered by chan.py

功能说明:
    - 批量扫描A股市场，自动识别近期出现买点的股票
    - 支持单只股票的缠论分析和图表展示
    - 可视化显示K线、笔、线段、中枢、买卖点、MACD等

数据来源:
    - 股票列表: akshare 的东财实时行情（stock_zh_a_spot_em）
    - 历史K线: 腾讯财经 fqkline 接口直连（httpx），不经过 akshare。
      逻辑参考 stock-chanlun/backend/services/akshare_service.py 的 get_kline_hist，
      单次约 0.2s（akshare 东财接口约 3~5s），扫描全市场时是数量级的差距。

过滤规则:
    - 剔除ST股票、科创板(688)、北交所、B股
    - 剔除停牌股票和新股

依赖:
    - PyQt6: GUI框架
    - matplotlib: 图表绑定
    - akshare: A股数据接口
    - chan.py: 缠论分析核心库

使用方法:
    python App/ashare_bsp_scanner_gui.py
"""
import sys
from pathlib import Path

# 将项目根目录加入路径，以便导入 chan.py 核心模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import httpx

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QGroupBox,
    QMessageBox, QStatusBar, QSplitter, QTableWidget, QTableWidgetItem,
    QProgressBar, QHeaderView, QTextEdit
)
from PyQt6.QtCore import QDate, Qt, QThread, pyqtSignal

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

import akshare as ak
import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE


def get_tradable_stocks():
    """
    获取所有可交易的A股股票列表

    过滤条件:
        1. 剔除ST股票（名称包含ST）
        2. 剔除科创板（688开头）
        3. 剔除北交所（8开头、43开头）
        4. 剔除B股（200开头深圳B股、900开头上海B股）
        5. 剔除存托凭证CDR（920开头）
        6. 剔除停牌股票（成交量为0）
        7. 剔除异常股票（最新价<=0）

    Returns:
        pd.DataFrame: 包含 ['代码', '名称', '最新价', '涨跌幅'] 列的股票列表
                      获取失败时返回空 DataFrame
    """
    try:
        # 获取A股实时行情
        df = ak.stock_zh_a_spot_em()

        # 过滤条件
        # 1. 剔除ST股票（名称包含ST）
        df = df[~df['名称'].str.contains('ST', case=False, na=False)]

        # 2. 剔除科创板（688开头）
        df = df[~df['代码'].str.startswith('688')]

        # 3. 剔除北交所（8开头，以43、83、87开头的也是北交所）
        df = df[~df['代码'].str.startswith('8')]
        df = df[~df['代码'].str.startswith('43')]

        # 4. 剔除B股（200开头深圳B股，900开头上海B股）
        df = df[~df['代码'].str.startswith('200')]
        df = df[~df['代码'].str.startswith('900')]

        # 5. 剔除存托凭证CDR（920开头）
        df = df[~df['代码'].str.startswith('920')]

        # 6. 剔除停牌股票（成交量为0或涨跌幅为空）
        df = df[df['成交量'] > 0]

        # 7. 剔除新股（上市不足60天的，这里简化处理，只保留有数据的）
        df = df[df['最新价'] > 0]

        return df[['代码', '名称', '最新价', '涨跌幅']].reset_index(drop=True)
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
#  K线获取 —— 腾讯财经 fqkline 直连
#
#  接口、代码规范化与返回体解析都对齐 stock-chanlun/backend/services/
#  akshare_service.py 的 get_kline_hist；差别在于缓存：那里是全站共享的
#  进程级缓存，这里只留最近 64 只（全市场扫描是一次性遍历，大缓存只会吃内存），
#  够「单只分析」反复点同一只股票时复用即可。
#
#  为什么不继续用 akshare：DATA_SRC.AKSHARE 走东财 stock_zh_a_hist，单次约
#  3~5s，扫 5000 只需要数小时；腾讯这个接口单次约 0.2s，且 httpx 显式
#  trust_env=False，不吃本机代理环境变量，不会偶发 ProxyError。
# ══════════════════════════════════════════════════════════════════

# chan.py 的 CSV 数据源固定读 {项目根}/{code}_{周期}.csv（见 DataAPI/csvAPI.py）
CHAN_ROOT = Path(__file__).resolve().parent.parent

_QQ_PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}
_QQ_ADJUST = {"qfq": "qfq", "hfq": "hfq", "": "", None: ""}

_http_client: httpx.Client | None = None
_http_client_lock = threading.RLock()

# 小容量 TTL 缓存：key=(code, period, adjust, limit)
_kline_cache: OrderedDict = OrderedDict()
_kline_cache_lock = threading.RLock()
_KLINE_CACHE_MAX = 64
_KLINE_CACHE_TTL = 300  # 秒


def _cache_get(key):
    with _kline_cache_lock:
        entry = _kline_cache.get(key)
        if entry is None:
            return None
        df, ts = entry
        if time.time() - ts < _KLINE_CACHE_TTL:
            _kline_cache.move_to_end(key)
            return df
        _kline_cache.pop(key, None)
        return None


def _cache_put(key, df):
    with _kline_cache_lock:
        _kline_cache[key] = (df, time.time())
        _kline_cache.move_to_end(key)
        while len(_kline_cache) > _KLINE_CACHE_MAX:
            _kline_cache.popitem(last=False)


def _get_client() -> httpx.Client:
    """共享 httpx 客户端：trust_env=False 绕开代理，连接池复用。"""
    global _http_client
    with _http_client_lock:
        if _http_client is None:
            _http_client = httpx.Client(
                timeout=15.0,
                follow_redirects=True,
                trust_env=False,  # 关键：不吃 HTTP_PROXY/HTTPS_PROXY 环境变量
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"),
                    "Referer": "https://finance.qq.com/",
                    "Accept": "*/*",
                },
            )
        return _http_client


def normalize_stock_code(code: str) -> tuple[str, str]:
    """规范化股票代码，返回 (6位代码, 交易所 sz/sh/bj)。"""
    code = str(code).strip().zfill(6)
    if code.startswith(("00", "30", "002", "003")):
        return code, "sz"
    if code.startswith(("60", "68", "500", "501")):
        return code, "sh"
    if code.startswith(("8", "4")):
        return code, "bj"
    return code, "sz"


def get_kline_hist(
    code: str,
    period: str = "daily",
    start_date: str | None = None,
    end_date: str | None = None,
    adjust: str = "qfq",
    limit: int = 500,
    retry: int = 2,
) -> pd.DataFrame:
    """
    获取A股历史K线（腾讯财经 fqkline 直连）。

    Args:
        code: 股票代码，如 '000001' / 'sh600000'
        period: daily / weekly / monthly
        start_date / end_date: 'YYYY-MM-DD'，在此区间内裁剪（含端点）
        adjust: qfq 前复权 / hfq 后复权 / '' 不复权
        limit: 最多取多少根（腾讯按最近 N 根返回，上限约 2000）
        retry: 失败重试次数

    Returns:
        pd.DataFrame: 列 [date, open, close, high, low, volume]，date 为 datetime64；
                      失败或无数据时返回空 DataFrame（不抛异常，便于扫描逐只容错）。
    """
    limit = max(20, min(int(limit), 2000))
    adjust = _QQ_ADJUST.get(adjust, "qfq")
    sym, mkt = normalize_stock_code(code)
    period_qq = _QQ_PERIOD.get(period, "day")

    cache_key = (sym, period_qq, adjust, limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?_var=kline_{period_qq}{adjust}&param={mkt}{sym},{period_qq},,,{limit},{adjust}")

    text = None
    last_err = None
    for attempt in range(retry + 1):
        try:
            resp = _get_client().get(url)
            text = resp.text
            break
        except Exception as e:      # 网络抖动：退避后重试
            last_err = e
            time.sleep(0.3 * (attempt + 1))
    if text is None:
        print(f"K线获取失败 {code}: {last_err}")
        return pd.DataFrame()

    # 返回体形如 kline_dayqfq={...}
    json_match = re.search(r"=({.+})", text)
    if not json_match:
        return pd.DataFrame()
    try:
        payload = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return pd.DataFrame()

    key = f"{mkt}{sym}"
    node = payload.get("data", {}).get(key)
    if not node:
        return pd.DataFrame()

    # 复权数据的键名不固定（实测是 qfqday 而不是 dayqfq），逐个试
    klines = node.get(f"{period_qq}{adjust}", [])
    if not klines:
        for k in (f"{adjust}{period_qq}", f"{period_qq}{adjust}", period_qq, "day",
                  "qfqday", "hfqday", "dayqfq", "dayhfq"):
            if node.get(k):
                klines = node[k]
                break
    if not klines:
        return pd.DataFrame()

    records = []
    for kl in klines:
        if len(kl) < 6:
            continue
        try:
            records.append({
                "date": kl[0],
                "open": float(kl[1]),
                "close": float(kl[2]),
                "high": float(kl[3]),
                "low": float(kl[4]),
                "volume": float(kl[5]),
            })
        except (ValueError, IndexError):
            continue

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"])
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df["date"] <= pd.Timestamp(end_date)]
    if len(df) > limit:
        df = df.tail(limit)
    df = df.reset_index(drop=True)

    _cache_put(cache_key, df)
    return df


def save_klines_to_csv(code: str, df: pd.DataFrame, k_type: str = "day") -> Path:
    """
    按 chan.py CSV_API 的约定把K线写成 {项目根}/{code}_{k_type}.csv。

    CSV_API 只认 5 列（time,open,high,low,close），且跳过首行表头；日期必须是
    'YYYY-MM-DD'（它按长度 10 解析）。列多一个少一个都会抛 SRC_DATA_FORMAT_ERROR。
    """
    path = CHAN_ROOT / f"{code}_{k_type}.csv"
    sub = df[["date", "open", "high", "low", "close"]].copy()
    sub["date"] = pd.to_datetime(sub["date"]).dt.strftime("%Y-%m-%d")
    sub.to_csv(path, index=False)
    return path


# CSV 的文件名是按 code 写死的（{code}_day.csv），所以：
#   · 同一只股票必须串行：两个线程同时分析它会互相覆盖/提前删除文件
#   · 不同股票之间完全并行，锁只保护「写文件」「删文件」这两个瞬时操作，
#     最耗时的缠论计算留在锁外，线程池才有意义
_csv_lock = threading.RLock()
_inflight_codes: set[str] = set()
_inflight_cv = threading.Condition()

_AUTYPE_ADJUST = {
    AUTYPE.QFQ: "qfq",
    AUTYPE.HFQ: "hfq",
    AUTYPE.NONE: "",
}


def _acquire_code(code: str):
    """串行化同一只股票的 build_chan（不同股票不互相阻塞）。"""
    with _inflight_cv:
        while code in _inflight_codes:
            _inflight_cv.wait()
        _inflight_codes.add(code)


def _release_code(code: str):
    with _inflight_cv:
        _inflight_codes.discard(code)
        _inflight_cv.notify_all()


def build_chan(
    code: str,
    config,
    begin_time: str | None = None,
    end_time: str | None = None,
    days: int = 365,
    autype: AUTYPE = AUTYPE.QFQ,
    lv_list: list | None = None,
) -> CChan:
    """
    拉取K线并用它构造 CChan。

    走 DATA_SRC.CSV：先把腾讯拿到的K线写成 chan.py 约定的临时 CSV，
    交给 CChan 读完再删掉。这样绕开 DATA_SRC.AKSHARE，
    但缠论计算本身完全不变（同一套 CSV_API 输入）。

    Raises:
        ValueError: 拿不到K线数据（调用方按「无数据」处理）
    """
    lv_list = lv_list or [KL_TYPE.K_DAY]
    k_type = lv_list[0].name[2:].lower()       # KL_TYPE.K_DAY -> 'day'
    adjust = _AUTYPE_ADJUST.get(autype, "qfq")
    if begin_time is None:
        begin_time = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    if end_time is None:
        end_time = datetime.now().strftime("%Y-%m-%d")

    # 按交易日折算根数（一年约 250 个交易日），多取一些让 begin_time 过滤后仍够长
    limit = max(60, int(days * 250 / 365) + 30)

    df = get_kline_hist(code, period="daily", start_date=begin_time,
                        end_date=end_time, adjust=adjust, limit=limit)
    if df.empty:
        raise ValueError(f"{code} 无K线数据")

    _acquire_code(code)
    try:
        with _csv_lock:
            path = save_klines_to_csv(code, df, k_type=k_type)
        try:
            return CChan(
                code=code,
                begin_time=begin_time,
                end_time=end_time,
                data_src=DATA_SRC.CSV,
                lv_list=lv_list,
                config=config,
                autype=autype,
            )
        finally:
            with _csv_lock:
                try:
                    path.unlink()
                except OSError:
                    pass
    finally:
        _release_code(code)


# 扫描并发数：K线下载是IO、缠论计算受 GIL 制约，8~12 之间性价比最高，
# 再往上腾讯接口和 CPU 都会成为瓶颈
DEFAULT_SCAN_WORKERS = 8


class ScanThread(QThread):
    """
    批量扫描股票的后台线程

    在独立线程中遍历股票列表，对每只股票进行缠论分析，
    检测最近3天内是否出现买点。

    Signals:
        progress: (int, int, str) 当前进度、总数、当前股票信息
        found_signal: (dict) 发现买点时发出，包含股票详情和 CChan 对象
        finished: (int, int) 扫描完成，返回成功数和失败数
        log_signal: (str) 日志消息
    """
    progress = pyqtSignal(int, int, str)
    found_signal = pyqtSignal(dict)
    finished = pyqtSignal(int, int)
    log_signal = pyqtSignal(str)

    def __init__(self, stock_list, config, days=365, workers=DEFAULT_SCAN_WORKERS):
        """
        初始化扫描线程

        Args:
            stock_list: pd.DataFrame, 待扫描的股票列表
            config: CChanConfig, 缠论配置
            days: int, 获取多少天的历史数据，默认365天
            workers: int, 并发线程数，默认 DEFAULT_SCAN_WORKERS
        """
        super().__init__()
        self.stock_list = stock_list
        self.config = config
        self.days = days
        self.workers = max(1, int(workers))
        self.is_running = True

    def stop(self):
        """停止扫描，设置标志位让 run() 循环退出"""
        self.is_running = False

    def run(self):
        """
        线程主函数：用线程池并发扫描股票列表

        每只股票的处理逻辑（在 worker 里跑）:
            1. 跳过无K线数据的股票
            2. 跳过停牌超过15天的股票
            3. 检测最近3天内是否出现买点
            4. 发现买点时通过 found_signal 发出通知

        并发说明:
            K线下载是网络IO、缠论计算是CPU，两者交替，实测 8 个 worker 能到
            3~4 倍加速（再往上受 GIL 与腾讯接口限制收益递减）。
            同一只股票内部由 build_chan 串行保护，不会自己踩自己。
            点「停止」后不再派新任务，已在跑的最多再跑完当前这一批。
        """
        begin_time = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")
        end_time = datetime.now().strftime("%Y-%m-%d")
        total = len(self.stock_list)
        success_count = 0
        fail_count = 0

        rows = [(r['代码'], r['名称'], r['最新价'], r['涨跌幅'])
                for _, r in self.stock_list.iterrows()]

        def scan_one(item):
            """处理一只股票，返回结构化结果（不碰信号，交给主线程统一 emit）。

            已停止时返回 None：让「还没轮到的」任务立刻空转返回，
            run() 才能尽快收尾（正在跑的那批最多再跑完自己这一只）。
            """
            code, name, price, change = item
            if not self.is_running:
                return None
            try:
                chan = build_chan(
                    code=code,
                    config=self.config,
                    begin_time=begin_time,
                    end_time=end_time,
                    days=self.days,
                )
            except Exception as e:
                return {'status': 'fail', 'code': code, 'name': name,
                        'msg': f"❌ {code} {name}: {str(e)[:50]}"}

            # 检查最近15天是否有数据
            if len(chan[0]) == 0:
                return {'status': 'fail', 'code': code, 'name': name,
                        'msg': f"⏭️ {code} {name}: 无K线数据"}
            last_klu = chan[0][-1][-1]
            last_time = last_klu.time
            last_date = datetime(last_time.year, last_time.month, last_time.day)
            if (datetime.now() - last_date).days > 15:
                return {'status': 'fail', 'code': code, 'name': name,
                        'msg': f"⏸️ {code} {name}: 停牌超过15天"}

            # 检查是否有买点（只找最近3天内出现的买点）
            bsp_list = chan.get_latest_bsp(number=0)
            cutoff_date = datetime.now() - timedelta(days=3)
            buy_points = [
                bsp for bsp in bsp_list
                if bsp.is_buy and datetime(bsp.klu.time.year, bsp.klu.time.month, bsp.klu.time.day) >= cutoff_date
            ]

            if buy_points:
                # 获取最近的买点
                latest_buy = buy_points[0]
                return {
                    'status': 'ok', 'code': code, 'name': name,
                    'price': price, 'change': change,
                    'msg': f"✅ {code} {name}: 发现买点 {latest_buy.type2str()}",
                    'bsp_type': latest_buy.type2str(),
                    'bsp_time': str(latest_buy.klu.time),
                    'chan': chan,
                }
            return {'status': 'ok', 'code': code, 'name': name,
                    'msg': f"➖ {code} {name}: 无近期买点"}

        done = 0
        workers = max(1, min(self.workers, max(len(rows), 1)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="scan") as pool:
            futures = [pool.submit(scan_one, r) for r in rows]
            try:
                for fut in as_completed(futures):
                    if not self.is_running:
                        break
                    try:
                        res = fut.result()
                    except Exception as e:      # worker 内未覆盖的异常，不能让整个扫描挂掉
                        fail_count += 1
                        done += 1
                        self.log_signal.emit(f"❌ 分析异常: {str(e)[:60]}")
                        continue

                    if res is None:             # 停止后被跳过的任务，不计入统计
                        continue

                    done += 1
                    self.progress.emit(done, total, f"{res['code']} {res['name']}")
                    self.log_signal.emit(res['msg'])

                    if res['status'] == 'fail':
                        fail_count += 1
                        continue

                    success_count += 1
                    if res.get('bsp_type'):
                        self.found_signal.emit({
                            'code': res['code'],
                            'name': res['name'],
                            'price': res['price'],
                            'change': res['change'],
                            'bsp_type': res['bsp_type'],
                            'bsp_time': res['bsp_time'],
                            'chan': res['chan'],
                        })
            finally:
                # 停止后撤掉还没开始的任务，避免后台空跑
                if not self.is_running:
                    for f in futures:
                        f.cancel()

        self.finished.emit(success_count, fail_count)


class SingleAnalysisThread(QThread):
    """
    单只股票分析的后台线程

    用于分析用户手动输入的股票代码，避免阻塞 UI。

    Signals:
        finished: (CChan) 分析完成，返回 CChan 对象
        error: (str) 分析出错时返回错误信息
    """
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, code, config, days=365):
        """
        初始化分析线程

        Args:
            code: str, 股票代码（如 '000001'）
            config: CChanConfig, 缠论配置
            days: int, 获取多少天的历史数据
        """
        super().__init__()
        self.code = code
        self.config = config
        self.days = days

    def run(self):
        """执行缠论分析，完成后通过信号返回结果"""
        try:
            begin_time = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")
            end_time = datetime.now().strftime("%Y-%m-%d")

            chan = build_chan(
                code=self.code,
                config=self.config,
                begin_time=begin_time,
                end_time=end_time,
                days=self.days,
            )
            self.finished.emit(chan)
        except Exception as e:
            self.error.emit(str(e))


class ChanPlotCanvas(FigureCanvas):
    """
    嵌入 PyQt 的 Matplotlib 画布

    用于在 GUI 中显示缠论分析图表，包括K线、笔、线段、中枢等。

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


class AkshareGUI(QMainWindow):
    """
    A股缠论买点扫描器主窗口

    主要功能:
        - 批量扫描: 自动获取所有可交易股票，逐一分析寻找买点
        - 单股分析: 手动输入股票代码进行缠论分析
        - 图表显示: 可视化展示K线、笔、线段、中枢、买卖点、MACD

    界面布局:
        - 左侧面板: 扫描控制、单股输入、买点列表、扫描日志
        - 右侧面板: 图表显示区域，支持缩放和导航
    """

    def __init__(self):
        """初始化主窗口"""
        super().__init__()
        self.chan = None  # 当前分析的 CChan 对象
        self.scan_thread = None  # 批量扫描线程
        self.analysis_thread = None  # 单股分析线程
        self.stock_cache = {}  # 缓存已分析的股票 {code: CChan}
        self.init_ui()

    def init_ui(self):
        """初始化用户界面"""
        self.setWindowTitle('A股缠论买点扫描器 - Powered by chan.py')
        self.setGeometry(100, 100, 1600, 900)

        # 创建中央 widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QHBoxLayout(central_widget)

        # 左侧控制面板和股票列表
        left_panel = self.create_left_panel()

        # 右侧图表区域
        right_panel = self.create_chart_panel()

        # 使用分割器
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([450, 1150])

        main_layout.addWidget(splitter)

        # 状态栏
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage('就绪 - 点击"开始扫描"分析所有股票')

    def create_left_panel(self):
        """创建左侧面板"""
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # 扫描控制
        scan_group = QGroupBox("扫描设置")
        scan_layout = QVBoxLayout(scan_group)

        # 笔严格模式
        self.bi_strict_cb = QCheckBox("笔严格模式")
        self.bi_strict_cb.setChecked(True)
        scan_layout.addWidget(self.bi_strict_cb)

        # 扫描按钮
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

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        scan_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        scan_layout.addWidget(self.progress_label)

        layout.addWidget(scan_group)

        # 单只股票分析
        single_group = QGroupBox("单只股票分析")
        single_layout = QVBoxLayout(single_group)

        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("股票代码:"))
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("如: 000001")
        code_row.addWidget(self.code_input)

        self.analyze_btn = QPushButton("分析")
        self.analyze_btn.clicked.connect(self.analyze_single)
        code_row.addWidget(self.analyze_btn)
        single_layout.addLayout(code_row)

        layout.addWidget(single_group)

        # 买点股票列表
        list_group = QGroupBox("买点股票列表")
        list_layout = QVBoxLayout(list_group)

        self.stock_table = QTableWidget()
        self.stock_table.setColumnCount(5)
        self.stock_table.setHorizontalHeaderLabels(['代码', '名称', '现价', '涨跌%', '买点'])
        self.stock_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stock_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stock_table.cellClicked.connect(self.on_stock_clicked)
        list_layout.addWidget(self.stock_table)

        # 清空按钮
        self.clear_list_btn = QPushButton("清空列表")
        self.clear_list_btn.clicked.connect(self.clear_stock_list)
        list_layout.addWidget(self.clear_list_btn)

        layout.addWidget(list_group)

        # 日志区域
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

        # 绘图配置
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

        # 刷新按钮
        self.refresh_btn = QPushButton("刷新图表")
        self.refresh_btn.clicked.connect(self.refresh_chart)
        config_layout.addWidget(self.refresh_btn)

        layout.addLayout(config_layout)

        # matplotlib 画布
        self.canvas = ChanPlotCanvas(panel, width=12, height=8)
        self.toolbar = NavigationToolbar(self.canvas, panel)

        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

        return panel

    def get_chan_config(self):
        """
        获取缠论分析配置

        Returns:
            CChanConfig: 包含笔严格模式、买卖点类型等配置的对象
        """
        return CChanConfig({
            "bi_strict": self.bi_strict_cb.isChecked(),  # 笔严格模式
            "trigger_step": False,  # 不启用逐步触发模式
            "skip_step": 0,
            "divergence_rate": float("inf"),  # 背驰比率
            "bsp2_follow_1": False,  # 二类买卖点不跟随一类
            "bsp3_follow_1": False,  # 三类买卖点不跟随一类
            "min_zs_cnt": 0,  # 最小中枢数量
            "bs1_peak": False,
            "macd_algo": "peak",  # MACD 算法
            "bs_type": "1,1p,2,2s,3a,3b",  # 启用的买卖点类型
            "print_warning": False,
            "zs_algo": "normal",  # 中枢算法
        })

    def get_plot_config(self):
        """
        获取图表绑定配置

        Returns:
            dict: 包含各图层显示开关的配置字典
        """
        return {
            "plot_kline": self.plot_kline_cb.isChecked(),  # 显示K线
            "plot_kline_combine": True,  # 显示合并K线
            "plot_bi": self.plot_bi_cb.isChecked(),  # 显示笔
            "plot_seg": self.plot_seg_cb.isChecked(),  # 显示线段
            "plot_zs": self.plot_zs_cb.isChecked(),  # 显示中枢
            "plot_macd": self.plot_macd_cb.isChecked(),  # 显示MACD
            "plot_bsp": self.plot_bsp_cb.isChecked(),  # 显示买卖点
        }

    def start_scan(self):
        """开始批量扫描所有可交易股票"""
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.stock_cache.clear()

        self.statusBar.showMessage('正在获取股票列表...')
        QApplication.processEvents()

        # 获取股票列表
        stock_list = get_tradable_stocks()
        if stock_list.empty:
            QMessageBox.warning(self, "警告", "获取股票列表失败")
            self.scan_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.progress_bar.setVisible(False)
            return

        self.statusBar.showMessage(f'获取到 {len(stock_list)} 只可交易股票，开始扫描...')
        self.progress_bar.setMaximum(len(stock_list))

        # 启动扫描线程
        config = self.get_chan_config()
        self.scan_thread = ScanThread(stock_list, config, days=365)
        self.scan_thread.progress.connect(self.on_scan_progress)
        self.scan_thread.found_signal.connect(self.on_buy_point_found)
        self.scan_thread.finished.connect(self.on_scan_finished)
        self.scan_thread.log_signal.connect(self.on_log_message)
        self.scan_thread.start()

    def stop_scan(self):
        """停止扫描"""
        if self.scan_thread:
            self.scan_thread.stop()
        self.statusBar.showMessage('正在停止扫描...')

    def on_scan_progress(self, current, total, stock_info):
        """扫描进度更新"""
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"进度: {current}/{total} - {stock_info}")

    def on_log_message(self, msg):
        """显示日志消息"""
        self.log_text.append(msg)

    def on_buy_point_found(self, data):
        """
        发现买点的回调函数

        Args:
            data: dict, 包含股票代码、名称、价格、买点类型、CChan对象等信息
        """
        row = self.stock_table.rowCount()
        self.stock_table.insertRow(row)
        self.stock_table.setItem(row, 0, QTableWidgetItem(data['code']))
        self.stock_table.setItem(row, 1, QTableWidgetItem(data['name']))
        self.stock_table.setItem(row, 2, QTableWidgetItem(f"{data['price']:.2f}"))
        self.stock_table.setItem(row, 3, QTableWidgetItem(f"{data['change']:.2f}%"))
        self.stock_table.setItem(row, 4, QTableWidgetItem(f"{data['bsp_type']} ({data['bsp_time']})"))

        # 缓存 chan 对象
        self.stock_cache[data['code']] = data['chan']

    def on_scan_finished(self, success_count, fail_count):
        """扫描完成"""
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        found_count = self.stock_table.rowCount()
        self.statusBar.showMessage(f'扫描完成: 成功{success_count}只, 跳过{fail_count}只, 发现{found_count}只买点股票')
        self.progress_label.setText(f"完成: 成功{success_count}, 跳过{fail_count}, 买点{found_count}")

    def on_stock_clicked(self, row, col):
        """点击股票列表"""
        code = self.stock_table.item(row, 0).text()
        name = self.stock_table.item(row, 1).text()

        if code in self.stock_cache:
            self.chan = self.stock_cache[code]
            self.plot_chart()
            self.statusBar.showMessage(f'显示: {code} {name}')
        else:
            # 重新分析
            self.analyze_stock(code)

    def analyze_single(self):
        """分析单只股票"""
        code = self.code_input.text().strip()
        if not code:
            QMessageBox.warning(self, "警告", "请输入股票代码")
            return
        self.analyze_stock(code)

    def analyze_stock(self, code):
        """分析指定股票"""
        self.analyze_btn.setEnabled(False)
        self.statusBar.showMessage(f'正在分析 {code}...')

        config = self.get_chan_config()
        self.analysis_thread = SingleAnalysisThread(code, config, days=365)
        self.analysis_thread.finished.connect(self.on_analysis_finished)
        self.analysis_thread.error.connect(self.on_analysis_error)
        self.analysis_thread.start()

    def on_analysis_finished(self, chan):
        """单只股票分析完成"""
        self.chan = chan
        self.analyze_btn.setEnabled(True)
        self.plot_chart()
        self.statusBar.showMessage(f'分析完成: {chan.code}')

    def on_analysis_error(self, error_msg):
        """分析出错"""
        self.analyze_btn.setEnabled(True)
        QMessageBox.critical(self, "分析错误", error_msg)
        self.statusBar.showMessage('分析失败')

    def plot_chart(self):
        """
        绑定当前股票的缠论分析图表

        使用 CPlotDriver 生成图表，显示K线、笔、线段、中枢等元素。
        图表大小会根据画布宽度自动调整。
        """
        if not self.chan:
            return

        try:
            from Plot.PlotDriver import CPlotDriver

            # 关闭旧的 figure 释放内存
            plt.close('all')

            plot_config = self.get_plot_config()

            # 获取控件宽度，计算合适的图表尺寸
            canvas_width = self.canvas.width()
            dpi = 100
            fig_width = canvas_width / dpi
            fig_height = fig_width * 0.5  # 宽高比 2:1

            plot_para = {
                "figure": {
                    "x_range": 200,
                    "w": fig_width,
                    "h": fig_height,
                }
            }

            plot_driver = CPlotDriver(self.chan, plot_config=plot_config, plot_para=plot_para)

            self.canvas.fig = plot_driver.figure
            self.canvas.figure = plot_driver.figure
            self.canvas.draw()
            self.toolbar.update()
        except Exception as e:
            QMessageBox.critical(self, "绑定错误", str(e))

    def refresh_chart(self):
        """刷新图表"""
        self.plot_chart()

    def clear_stock_list(self):
        """清空股票列表"""
        self.stock_table.setRowCount(0)
        self.stock_cache.clear()
        self.statusBar.showMessage('列表已清空')


def main():
    """程序入口函数，创建并运行 GUI 应用"""
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # 使用 Fusion 风格，跨平台一致性好

    window = AkshareGUI()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
