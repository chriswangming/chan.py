"""
Demark（TD Sequential，德马克序列）指标。

用来在一个线段/趋势的尾部数"衰竭计数"，帮助判断趋势是否接近尽头。分两个阶段：

    Setup（准备阶段，默认 9 计数）
        向下 Setup：某根K线的收盘价 < **4 根之前**那根的收盘价，就算一个计数；
        向上 Setup 同理（收盘价 > 4 根前）。连续数到 9 根即 Setup 完成。
        中途若出现反向 Setup，当前 Setup 立刻作废（setup_finished = True）。

    Countdown（倒计时阶段，默认 13 计数，Setup 完成后才启动）
        向下 Countdown：收盘价 < 2 根之前那根的价格（COUNTDOWN_CMP2CLOSE 决定比收盘还是比极值）；
        向上 Countdown 同理。累计到 13 即完成。
        期间若价格反向突破 TDST 线（Setup 区间的极值），Countdown 立即作废。

配置项（CDemarkEngine 的类属性，构造时会被覆盖）：
    DEMARK_LEN=9        Setup 需要数到的个数
    SETUP_BIAS=4        Setup 与几根之前比较
    COUNTDOWN_BIAS=2    Countdown 与几根之前比较
    MAX_COUNTDOWN=13    Countdown 需要数到的个数
    TIAOKONG_ST=True    跳空时是否把前一根收盘价并入 TDST 极值
    SETUP_CMP2CLOSE / COUNTDOWN_CMP2CLOSE=True  比较时用收盘价（False 则用方向侧的极值价）

结构关系：CDemarkEngine 是驱动器，内部维护多个并存的 CDemarkSetup（一个方向一个），
每个 Setup 又可能带一个 CDemarkCountdown；每根K线产生的结果记录在 CDemarkIndex 里。
"""

import copy
from dataclasses import dataclass
from typing import List, Literal, Optional, TypedDict

from Common.CEnum import BI_DIR


@dataclass
class C_KL:
    """Demark 计算用的K线最小快照（只保留要参与比较的字段）。"""

    idx: int
    close: float
    high: float
    low: float

    def v(self, is_close: bool, _dir: BI_DIR) -> float:
        """
        取参与比较的价格。

        Args:
            is_close: True 用收盘价；False 用方向侧的极值价
            _dir: 序列方向，BI_DIR.UP 取 high，BI_DIR.DOWN 取 low

        Returns:
            float: 比较用的价格
        """
        if is_close:
            return self.close
        return self.high if _dir == BI_DIR.UP else self.low


T_DEMARK_TYPE = Literal['setup', 'countdown']


class T_DEMARK_INDEX(TypedDict):
    """一条 Demark 计数记录：哪个方向、哪个阶段、数到几。"""

    type: T_DEMARK_TYPE
    dir: BI_DIR
    idx: int
    series: 'CDemarkSetup'


class CDemarkIndex:
    """在某一根K线上产生的 Demark 计数集合（可能同时来自多个 series）。"""

    def __init__(self):
        """初始化一个空的计数集合（data 里每条记录是一条 Setup/Countdown 计数）。"""
        self.data: List[T_DEMARK_INDEX] = []

    def add(self, _dir: BI_DIR, _type: T_DEMARK_TYPE, idx: int, series: 'CDemarkSetup'):
        """
        追加一条计数记录。

        Args:
            _dir: 计数方向
            _type: 'setup' 或 'countdown'
            idx: 当前数到第几个
            series: 产生这条记录的 Setup 对象（回指，方便绘图/排查）
        """
        self.data.append({"dir": _dir, "idx": idx, "type": _type, "series": series})

    def get_setup(self) -> List[T_DEMARK_INDEX]:
        """取出所有 Setup 阶段的计数记录。"""
        return [info for info in self.data if info['type'] == 'setup']

    def get_countdown(self) -> List[T_DEMARK_INDEX]:
        """取出所有 Countdown 阶段的计数记录。"""
        return [info for info in self.data if info['type'] == 'countdown']

    def update(self, demark_index: 'CDemarkIndex'):
        """
        合并另一个索引对象里的全部记录（直接 extend，不查重）。

        Args:
            demark_index: 另一个 CDemarkIndex
        """
        self.data.extend(demark_index.data)


class CDemarkCountdown:
    """倒计时阶段：Setup 完成后继续数 13 个数，或中途被反向突破作废。"""

    def __init__(self, _dir: BI_DIR, kl_list: List[C_KL], TDST_peak: float):
        """
        Args:
            _dir: 计数方向
            kl_list: 起始K线序列（Setup 的最后几根，深拷贝保存，避免被外部改动影响）
            TDST_peak: TDST 极值线，价格反向突破它就作废
        """
        self.dir = _dir
        self.kl_list: List[C_KL] = copy.deepcopy(kl_list)
        self.idx = 0
        self.TDST_peak = TDST_peak
        self.finish = False

    def update(self, kl: C_KL) -> bool:
        """
        喂入一根K线，推进倒计时。

        Args:
            kl: 新K线

        Returns:
            bool: 这一根是否产生了新的计数（True 才需要往 CDemarkIndex 里记一条）

        Note:
            判断顺序是「已结束 → 出局」「数满 → 结束」「被 TDST 反向突破 → 作废」，
            最后才做"与 2 根之前比较"的有效计数判断。
        """
        if self.finish:
            return False
        self.kl_list.append(kl)
        if len(self.kl_list) <= CDemarkEngine.COUNTDOWN_BIAS:
            # 样本还不够回溯 COUNTDOWN_BIAS 根，这一根不计数
            return False
        if self.idx == CDemarkEngine.MAX_COUNTDOWN:
            # 已经数满 MAX_COUNTDOWN 个，再遇到新K线就收尾
            self.finish = True
            return False
        if (self.dir == BI_DIR.DOWN and kl.high > self.TDST_peak) or (self.dir == BI_DIR.UP and kl.low < self.TDST_peak):
            # 价格反向突破 TDST 线（向下序列被最高价突破 / 向上序列被最低价跌破）→ 倒计时作废
            self.finish = True
            return False
        # 向下计数：收盘价低于 2 根之前 → 计一个数；向上计数对称
        if self.dir == BI_DIR.DOWN and self.kl_list[-1].close < self.kl_list[-1 - CDemarkEngine.COUNTDOWN_BIAS].v(CDemarkEngine.COUNTDOWN_CMP2CLOSE, self.dir):
            self.idx += 1
            return True
        if self.dir == BI_DIR.UP and self.kl_list[-1].close > self.kl_list[-1 - CDemarkEngine.COUNTDOWN_BIAS].v(CDemarkEngine.COUNTDOWN_CMP2CLOSE, self.dir):
            self.idx += 1
            return True
        return False


class CDemarkSetup:
    """准备阶段：连续数 Setup 计数，数满后创建并驱动 CDemarkCountdown。"""

    def __init__(self, _dir: BI_DIR, kl_list: List[C_KL], pre_kl: C_KL):
        """
        Args:
            _dir: 计数方向
            kl_list: 初始的 SETUP_BIAS 根K线（深拷贝保存）
            pre_kl: kl_list 再往前的那一根，只在跳空判断时用到
        """
        self.dir = _dir
        self.kl_list: List[C_KL] = copy.deepcopy(kl_list)
        self.pre_kl = pre_kl  # 跳空时用
        assert len(self.kl_list) == CDemarkEngine.SETUP_BIAS
        self.countdown: Optional[CDemarkCountdown] = None
        self.setup_finished = False
        self.idx = 0
        self.TDST_peak: Optional[float] = None

        self.last_demark_index = CDemarkIndex()  # 缓存用

    def update(self, kl: C_KL) -> CDemarkIndex:
        """
        喂入一根K线，推进 Setup / Countdown，并返回这一根上产生的计数。

        Args:
            kl: 新K线

        Returns:
            CDemarkIndex: 这一根K线产生的记录（可能为空）
        """
        self.last_demark_index = CDemarkIndex()
        if not self.setup_finished:
            self.kl_list.append(kl)
            if self.dir == BI_DIR.DOWN:
                # 向下 Setup：收盘价低于 SETUP_BIAS 根之前 → 计一个数，否则 Setup 终止
                if self.kl_list[-1].close < self.kl_list[-1-CDemarkEngine.SETUP_BIAS].v(CDemarkEngine.SETUP_CMP2CLOSE, self.dir):
                    self.add_setup()
                else:
                    self.setup_finished = True
            elif self.kl_list[-1].close > self.kl_list[-1-CDemarkEngine.SETUP_BIAS].v(CDemarkEngine.SETUP_CMP2CLOSE, self.dir):
                self.add_setup()
            else:
                self.setup_finished = True
        if self.idx == CDemarkEngine.DEMARK_LEN and not self.setup_finished and self.countdown is None:
            # Setup 数满（且没有中途作废）→ 用前 DEMARK_LEN 根建立倒计时。
            # 注意这里传的是 kl_list[:-1]，当前这根随后会由下面的 countdown.update 加进去
            self.countdown = CDemarkCountdown(self.dir, self.kl_list[:-1], self.cal_TDST_peak())
        if self.countdown is not None and self.countdown.update(kl):
            # 倒计时每产生一个数就记一条
            self.last_demark_index.add(self.dir, 'countdown', self.countdown.idx, self)
        return self.last_demark_index

    def add_setup(self):
        """Setup 计数 +1，并记录一条 setup 记录。"""
        self.idx += 1
        self.last_demark_index.add(self.dir, 'setup', self.idx, self)

    def cal_TDST_peak(self) -> float:
        """
        计算 TDST 极值线（Setup 区间的高/低点，用于判断 Countdown 是否作废）。

        Returns:
            float: 向下序列返回区间最高价（阻力），向上序列返回区间最低价（支撑）

        Note:
            TIAOKONG_ST 为真时考虑跳空：向下序列若第一根的最高价已低于前一根收盘价
            （向下跳空），把前一根收盘价也纳入极值。
        """
        assert len(self.kl_list) == CDemarkEngine.SETUP_BIAS+CDemarkEngine.DEMARK_LEN
        # 取最后 DEMARK_LEN 根，即 Setup 数出来的那 9 根
        arr = self.kl_list[CDemarkEngine.SETUP_BIAS:CDemarkEngine.SETUP_BIAS+CDemarkEngine.DEMARK_LEN]
        assert len(arr) == CDemarkEngine.DEMARK_LEN
        if self.dir == BI_DIR.DOWN:
            res = max(kl.high for kl in arr)
            if CDemarkEngine.TIAOKONG_ST and arr[0].high < self.pre_kl.close:
                res = max(res, self.pre_kl.close)
        else:
            res = min(kl.low for kl in arr)
            if CDemarkEngine.TIAOKONG_ST and arr[0].low > self.pre_kl.close:
                res = min(res, self.pre_kl.close)
        self.TDST_peak = res
        return res


class CDemarkEngine:
    """
    Demark 驱动器：逐根K线喂入，内部维护方向相反的多个 CDemarkSetup 并存。

    注意所有配置都写在**类属性**上（构造时直接赋值给 CDemarkEngine.XXX），
    属于全局生效的写法，详见 __init__ 说明。
    """

    DEMARK_LEN = 9
    SETUP_BIAS = 4
    COUNTDOWN_BIAS = 2
    MAX_COUNTDOWN = 13
    TIAOKONG_ST = True  # 第一根跳空时是否跟前一根的close比
    SETUP_CMP2CLOSE = True
    COUNTDOWN_CMP2CLOSE = True

    def __init__(
        self,
        demark_len=9,
        setup_bias=4,
        countdown_bias=2,
        max_countdown=13,
        tiaokong_st=True,
        setup_cmp2close=True,
        countdown_cmp2close=True
    ):
        """
        Args:
            demark_len: Setup 计数目标，默认 9
            setup_bias: Setup 回溯根数，默认 4
            countdown_bias: Countdown 回溯根数，默认 2
            max_countdown: Countdown 计数目标，默认 13
            tiaokong_st: 跳空时是否把前一根收盘价并入 TDST 极值
            setup_cmp2close: Setup 比较是否用收盘价（False 用方向侧极值）
            countdown_cmp2close: Countdown 比较是否用收盘价

        Note:
            参数写的是**类属性**（CDemarkEngine.XXX），不是实例属性：
            因此后创建的引擎会覆盖先创建的引擎的配置，属于全局状态。
        """
        CDemarkEngine.DEMARK_LEN = demark_len
        CDemarkEngine.SETUP_BIAS = setup_bias
        CDemarkEngine.COUNTDOWN_BIAS = countdown_bias
        CDemarkEngine.MAX_COUNTDOWN = max_countdown
        CDemarkEngine.TIAOKONG_ST = tiaokong_st
        CDemarkEngine.SETUP_CMP2CLOSE = setup_cmp2close
        CDemarkEngine.COUNTDOWN_CMP2CLOSE = countdown_cmp2close

        self.kl_lst: List[C_KL] = []
        self.series: List[CDemarkSetup] = []

    def update(self, idx: int, close: float, high: float, low: float) -> CDemarkIndex:
        """
        喂入一根K线，推进所有 series 并返回这一根上产生的计数。

        Args:
            idx: K线序号
            close: 收盘价
            high: 最高价
            low: 最低价

        Returns:
            CDemarkIndex: 这一根K线产生的记录（数据不足时为空）
        """
        self.kl_lst.append(C_KL(idx, close, high, low))
        if len(self.kl_lst) <= CDemarkEngine.SETUP_BIAS+1:
            # 回溯 SETUP_BIAS 根还需要更多历史，先不算
            return CDemarkIndex()

        if self.kl_lst[-1].close < self.kl_lst[-1-self.SETUP_BIAS].close:
            # 出现向下的比较 → 可能启动/延续向下 Setup
            if not any(series.dir == BI_DIR.DOWN and not series.setup_finished for series in self.series):
                # 当前没有"活着的向下 series"才新建；初始序列是最近 SETUP_BIAS 根，
                # pre_kl 再往前一根（跳空判断要用）
                self.series.append(CDemarkSetup(BI_DIR.DOWN, self.kl_lst[-CDemarkEngine.SETUP_BIAS-1:-1], self.kl_lst[-CDemarkEngine.SETUP_BIAS-2]))
            for series in self.series:
                # 反向（向上）且还没进入倒计时的 series 被这次向下 Setup 打断
                if series.dir == BI_DIR.UP and series.countdown is None and not series.setup_finished:
                    series.setup_finished = True
        elif self.kl_lst[-1].close > self.kl_lst[-1-self.SETUP_BIAS].close:
            # 向上分支，逻辑与上面完全对称
            if not any(series.dir == BI_DIR.UP and not series.setup_finished for series in self.series):
                self.series.append(CDemarkSetup(BI_DIR.UP, self.kl_lst[-CDemarkEngine.SETUP_BIAS-1:-1], self.kl_lst[-CDemarkEngine.SETUP_BIAS-2]))
            for series in self.series:
                if series.dir == BI_DIR.DOWN and series.countdown is None and not series.setup_finished:
                    series.setup_finished = True

        self.clear()                            # 先清掉上一轮已经无效的 series
        self.clean_series_from_setup_finish()   # 再驱动各 series 吃到这一根K线
        # 先汇总结果（cal_result 依赖各 series 刚更新的 last_demark_index），再清一次无效 series
        result = self.cal_result()
        self.clear()
        return result

    def cal_result(self) -> CDemarkIndex:
        """
        汇总所有 series 在这一根K线上产生的计数。

        Returns:
            CDemarkIndex: 合并后的记录集合
        """
        demark_index = CDemarkIndex()
        for series in self.series:
            demark_index.update(series.last_demark_index)
        return demark_index

    def clear(self):
        """清掉两类失效 series：Setup 已终止且没进倒计时的、倒计时已结束的。"""
        invalid_series = [series for series in self.series if series.setup_finished and series.countdown is None]
        for s in invalid_series:
            self.series.remove(s)
        invalid_series = [series for series in self.series if series.countdown is not None and series.countdown.finish]
        for s in invalid_series:
            self.series.remove(s)

    def clean_series_from_setup_finish(self):
        """
        让每个 series 都吃掉最新这根K线，并在 Setup 数满时"只保留这一个 series"。

        Note:
            数满 Setup（idx == DEMARK_LEN）的那根K线上只允许有一个 series 完成，
            否则下面的 assert 会失败（两个方向不可能同时数满，所以正常数据下成立）。
        """
        finished_setup: Optional[int] = None
        for series in self.series:
            demark_idx = series.update(self.kl_lst[-1])
            for setup_idx in demark_idx.get_setup():
                if setup_idx['idx'] == CDemarkEngine.DEMARK_LEN:
                    assert finished_setup is None
                    finished_setup = id(series)
        if finished_setup is not None:
            # Setup 完成时把其它 series 全部丢弃，只留数满的那个
            self.series = [series for series in self.series if id(series) == finished_setup]
