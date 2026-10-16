"""
线段（Seg）模块：缠论链路中「笔 -> 线段」这一环。

一条线段 CSeg 由若干根笔（LINE_TYPE = CBi）构成，反过来它自己也可以被
更高一层的"线段的线段"（LINE_TYPE = CSeg）当成元素 —— 这两层共用同一个类，
靠泛型参数 LINE_TYPE 区分，所以下面统一把构成线段的那些元素叫做"笔"。

链路位置：
    原始K线 -> 包含合并(CKLine) -> 分型 -> 笔(Bi) -> 【本模块：线段】 -> 中枢(ZS) -> 买卖点

本模块负责：
    1. 线段自身的描述：起止笔、方向、端点 KLU、振幅/斜率等派生量；
    2. 与中枢的关联：本线段内部的中枢列表 zs_lst、以及各类"非一笔中枢"的查询；
    3. 线段的线段：本线段作为元素的层级（seg_idx / parent_seg）。

Note:
    "哪些笔该切成一段"不是本模块的事，由 Seg/SegListChan.py、SegListDef.py、
    SegListDYH.py 三种算法实现决定，它们都继承 Seg/SegListComm.py 的公共基类。
"""
from typing import Generic, List, Optional, Self, TypeVar

from Bi.Bi import CBi
from Common.CEnum import BI_DIR, MACD_ALGO, TREND_LINE_SIDE
from Common.ChanException import CChanException, ErrCode
from KLine.KLine_Unit import CKLine_Unit
from Math.TrendLine import CTrendLine

from .EigenFX import CEigenFX

LINE_TYPE = TypeVar('LINE_TYPE', CBi, "CSeg")


class CSeg(Generic[LINE_TYPE]):
    """
    一条线段（或一条"线段的线段"，取决于泛型 LINE_TYPE）。

    核心状态：
        start_bi / end_bi：首尾两根笔（它们的 idx 差就是线段长度 -1）
        dir              ：线段方向，默认取终笔方向
        is_sure          ：是否已被后续走势确认；不足 3 根笔的线段永远不能是确认线段
        zs_lst           ：落在本线段里的中枢（按时间递增排列）
        bi_list          ：本线段包含的笔，只能通过 update_bi_list 更新
        eigen_fx         ：只有特征序列法（CSegListChan）会填，记录判定它的那个特征序列

    Note:
        与 CBi 一样，线段也有"确认/未确认"两种身份：
        只有被后续走势验证过的线段才是确认线段，买卖点、中枢归属都以它为准。
    """

    def __init__(self, idx: int, start_bi: LINE_TYPE, end_bi: LINE_TYPE, is_sure=True, seg_dir=None, reason="normal"):
        """
        Args:
            idx: 线段的序号
            start_bi: 起点笔
            end_bi: 终点笔
            is_sure: 是否是确认线段
            seg_dir: 线段方向；None 时取终点笔的方向
            reason: 生成这条线段的理由（一段标识字符串，便于调试/回溯）

        Note:
            开头这条断言是线段方向自洽的前提：线段必须以"与终笔同向的笔"结束
            （起点笔是序列第 0 号时无法比较方向，未确认线段则放宽）。
            同理，笔数不足 3 根的线段只可能是未确认线段，所以后面会强制把 is_sure 置 False。
        """
        assert start_bi.idx == 0 or start_bi.dir == end_bi.dir or not is_sure, f"{start_bi.idx} {end_bi.idx} {start_bi.dir} {end_bi.dir}"
        self.idx = idx
        self.start_bi = start_bi
        self.end_bi = end_bi
        self.is_sure = is_sure
        self.used_to_be_sure = is_sure
        # 方向默认取终笔方向；但像「补收尾」这类场景需要外部显式指定
        self.dir = end_bi.dir if seg_dir is None else seg_dir

        from ZS.ZS import CZS
        self.zs_lst: List[CZS[LINE_TYPE]] = []

        self.eigen_fx: Optional[CEigenFX] = None
        self.seg_idx = None  # 线段的线段用
        self.parent_seg: Optional[CSeg] = None  # 在哪个线段里面
        self.pre: Optional[Self] = None
        self.next: Optional[Self] = None

        from BuySellPoint.BS_Point import CBS_Point
        self.bsp: Optional[CBS_Point] = None  # 尾部是不是买卖点

        self.bi_list: List[LINE_TYPE] = []  # 仅通过self.update_bi_list来更新
        self.reason = reason
        self.support_trend_line = None
        self.resistance_trend_line = None
        # 少于 3 根笔的线段只是半成品，不能算确认线段
        if end_bi.idx - start_bi.idx < 2:
            self.is_sure = False
        self.check()

        self.ele_inside_is_sure = False

    def set_seg_idx(self, idx):
        """记录本线段在"线段的线段"里的序号（由上层 cal_seg 回写）。"""
        self.seg_idx = idx

    def check(self):
        """
        自检：端点高低要和方向一致、长度要够。

        Raises:
            CChanException: 下降线段却没走低 / 上升线段却没走高（ErrCode.SEG_END_VALUE_ERR），
                或线段不足 3 根笔（ErrCode.SEG_LEN_ERR）

        Note:
            只对确认线段做检查：未确认线段本来就可能是"半截"，自检没有意义。
        """
        if not self.is_sure:
            return
        if self.is_down():
            if self.start_bi.get_begin_val() < self.end_bi.get_end_val():
                raise CChanException(f"下降线段起始点应该高于结束点! idx={self.idx}", ErrCode.SEG_END_VALUE_ERR)
        elif self.start_bi.get_begin_val() > self.end_bi.get_end_val():
            raise CChanException(f"上升线段起始点应该低于结束点! idx={self.idx}", ErrCode.SEG_END_VALUE_ERR)
        # 线段至少要由 3 根笔构成（起、中、终），否则不成其为线段
        if self.end_bi.idx - self.start_bi.idx < 2:
            raise CChanException(f"线段({self.start_bi.idx}-{self.end_bi.idx})长度不能小于2! idx={self.idx}", ErrCode.SEG_LEN_ERR)

    def __str__(self):
        """调试输出：`首笔idx->终笔idx: 方向  是否确认`。"""
        return f"{self.start_bi.idx}->{self.end_bi.idx}: {self.dir}  {self.is_sure}"

    def add_zs(self, zs):
        """
        把一个中枢挂到本线段上。

        Args:
            zs: 落在本线段区间内的中枢

        Note:
            调用方（KLine_List.update_zs_in_seg）是按"从新到旧"的顺序遍历中枢的，
            这里把新中枢放在最前面，等遍历完，zs_lst 自然就是时间递增的。
        """
        self.zs_lst = [zs] + self.zs_lst  # 因为中枢是反序加入的

    def cal_klu_slope(self):
        """
        线段的"斜率"：单位K线上的相对涨幅。

        Returns:
            float: (终点值-起点值)/K线根数/起点值

        Note:
            这里除以的是原 K线 序号之差（不含 +1），与 CBi.get_klu_cnt 的口径差 1。
        """
        assert self.end_bi.idx >= self.start_bi.idx
        return (self.get_end_val()-self.get_begin_val())/(self.get_end_klu().idx-self.get_begin_klu().idx)/self.get_begin_val()

    def cal_amp(self):
        """线段的涨跌幅：端点价差 / 起点价。"""
        return (self.get_end_val()-self.get_begin_val())/self.get_begin_val()

    def cal_bi_cnt(self):
        """本线段包含多少根笔。"""
        return self.end_bi.idx-self.start_bi.idx+1

    def clear_zs_lst(self):
        """清空本线段的中枢列表（重复调用 update_zs_in_seg 时用来保证幂等）。"""
        self.zs_lst = []

    def _low(self):
        """
        线段区间的最低价。

        Note:
            与 CBi._low 同理：下降线段的低点在终点，上升线段的低点在起点。
        """
        return self.end_bi.get_end_klu().low if self.is_down() else self.start_bi.get_begin_klu().low

    def _high(self):
        """
        线段区间的最高价。

        Note:
            与 CBi._high 同理：上升线段的高点在终点，下降线段的高点在起点。
        """
        return self.end_bi.get_end_klu().high if self.is_up() else self.start_bi.get_begin_klu().high

    def is_down(self):
        """是否是下降线段。"""
        return self.dir == BI_DIR.DOWN

    def is_up(self):
        """是否是上升线段。"""
        return self.dir == BI_DIR.UP

    def get_end_val(self):
        """线段的终点价格值（终笔的终点值）。"""
        return self.end_bi.get_end_val()

    def get_begin_val(self):
        """线段的起点价格值（首笔的起点值）。"""
        return self.start_bi.get_begin_val()

    # 现在或曾经是确认线段（True 表示这条线段的形态曾经成立过）
    @property
    def is_used_to_be_sure(self) -> bool:
        """Returns: bool: 是否是（或曾经是）确认线段。"""
        return self.is_sure or self.used_to_be_sure

    def amp(self):
        """线段的振幅（端点价差的绝对值）。"""
        return abs(self.get_end_val() - self.get_begin_val())

    def get_end_klu(self) -> CKLine_Unit:
        """线段的终点原始K线（终笔的终点 KLU）。"""
        return self.end_bi.get_end_klu()

    def get_begin_klu(self) -> CKLine_Unit:
        """线段的起点原始K线（首笔的起点 KLU）。"""
        return self.start_bi.get_begin_klu()

    def get_klu_cnt(self):
        """本线段覆盖了多少根原始K线（首尾都算）。"""
        return self.get_end_klu().idx - self.get_begin_klu().idx + 1

    def cal_macd_metric(self, macd_algo, is_reverse):
        """
        算本线段的力度标量（用于线段级别的背驰比较）。

        Args:
            macd_algo: 只支持 MACD_ALGO.SLOPE（斜率）与 MACD_ALGO.AMP（涨跌幅）
            is_reverse: 线段上的力度是"价格型"口径，与正序/倒序无关，仅为保持签名一致

        Returns:
            float: 力度值

        Raises:
            CChanException: 传入了这两种以外的算法（ErrCode.PARA_ERROR）

        Note:
            线段是笔的上一级，MACD 柱子是按笔算的，所以这里不做面积类口径。
        """
        if macd_algo == MACD_ALGO.SLOPE:
            return self.Cal_MACD_slope()
        elif macd_algo == MACD_ALGO.AMP:
            return self.Cal_MACD_amp()
        else:
            raise CChanException(f"unsupport macd_algo={macd_algo} of Seg, should be one of slope/amp", ErrCode.PARA_ERROR)

    def Cal_MACD_slope(self):
        """
        线段的斜率：单位K线上的相对涨幅（与 CBi.Cal_MACD_slope 同一口径）。

        Returns:
            float: 向上 -> (终点高-起点低)/终点高/K线根数；
                   向下 -> (起点高-终点低)/起点高/K线根数
        """
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        if self.is_up():
            return (end_klu.high - begin_klu.low)/end_klu.high/(end_klu.idx - begin_klu.idx + 1)
        else:
            return (begin_klu.high - end_klu.low)/begin_klu.high/(end_klu.idx - begin_klu.idx + 1)

    def Cal_MACD_amp(self):
        """
        线段的涨跌幅（与 CBi.Cal_MACD_amp 同一口径）。

        Returns:
            float: 向下 -> (起点高-终点低)/起点高；向上 -> (终点高-起点低)/起点低
        """
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        if self.is_down():
            return (begin_klu.high-end_klu.low)/begin_klu.high
        else:
            return (end_klu.high-begin_klu.low)/begin_klu.low

    def update_bi_list(self, bi_lst, idx1, idx2):
        """
        把 [idx1, idx2] 区间内的笔登记为本线段的成员。

        Args:
            bi_lst: 完整的笔列表
            idx1: 起始笔的 idx
            idx2: 终止笔的 idx（含）

        Note:
            这里是直接 append，调用方必须保证同一段不会被重复登记，否则 bi_list 会重复。
            成员凑够 3 根笔之后才会去拟合内部的支撑/压力趋势线。
        """
        for bi_idx in range(idx1, idx2+1):
            bi_lst[bi_idx].parent_seg = self
            self.bi_list.append(bi_lst[bi_idx])
        # 至少要 3 根笔才谈得上拟合趋势线
        if len(self.bi_list) >= 3:
            self.support_trend_line = CTrendLine(self.bi_list, TREND_LINE_SIDE.INSIDE)
            self.resistance_trend_line = CTrendLine(self.bi_list, TREND_LINE_SIDE.OUTSIDE)

    def get_first_multi_bi_zs(self):
        """
        取本线段内第一个"真正的中枢"（不是由单根笔构成的中枢）。

        Returns:
            Optional[CZS]: 没有则返回 None
        """
        return next((zs for zs in self.zs_lst if not zs.is_one_bi_zs()), None)

    def get_multi_bi_zs_lst(self):
        """取本线段内所有非一笔中枢。"""
        return [zs for zs in self.zs_lst if not zs.is_one_bi_zs()]

    def get_final_multi_bi_zs(self):
        """
        取本线段内最后一个"真正的中枢"。

        Returns:
            Optional[CZS]: 从后往前找第一个非一笔中枢，没有则返回 None
        """
        zs_idx = len(self.zs_lst) - 1
        while zs_idx >= 0:
            zs = self.zs_lst[zs_idx]
            if not zs.is_one_bi_zs():
                return zs
            zs_idx -= 1
        return None

    def get_multi_bi_zs_cnt(self):
        """本线段内非一笔中枢的个数。"""
        return sum(not zs.is_one_bi_zs() for zs in self.zs_lst)
