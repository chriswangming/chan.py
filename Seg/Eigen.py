"""
特征序列元素（CEigen）模块。

CEigen 就是"由若干根笔合并而成的特征序列元素"：
它复用 Combiner/KLine_Combiner.py 的包含合并逻辑，只是被合并的元素从原始K线换成了笔。
若干 CEigen 排成一列，由 Seg/EigenFX.py 组装成特征序列并判定分型。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 【本模块：特征序列元素】
    -> 特征序列分型 -> 线段 -> 中枢 -> 买卖点

Note:
    与合并K线 CKLine 的差别只有两点：
        1) 元素类型是 CBi 而不是 CKLine_Unit；
        2) 额外维护一个 gap 标志，记录它和前一个元素之间有没有"跳空缺口"。
"""
from typing import Self

from Bi.Bi import CBi
from Combiner.KLine_Combiner import CKLine_Combiner
from Common.CEnum import BI_DIR, FX_TYPE


class CEigen(CKLine_Combiner[CBi]):
    """
    一个特征序列元素（若干根同方向笔按包含关系合并后的产物）。

    继承来的 high/low/dir/fx/lst 含义与合并K线完全一致（只是元素是笔），
    本类只额外加了一个 gap 标志。
    """

    def __init__(self, bi, _dir):
        """
        Args:
            bi: 本元素的第一根笔
            _dir: 合并方向（KLINE_DIR.UP/DOWN），由上层按线段方向给出

        Note:
            gap 初始为 False，要等分型判定出来之后（update_fx）才有意义。
        """
        super(CEigen, self).__init__(bi, _dir)
        self.gap = False

    def update_fx(self, _pre: Self, _next: Self, exclude_included=False, allow_top_equal=None):
        """
        先按普通（或特征序列）口径判分型，再补判"与前一元素之间是否存在缺口"。

        Args:
            _pre: 前一个特征序列元素
            _next: 后一个特征序列元素
            exclude_included: 被包含时是否不合并（特征序列永远传 True）
            allow_top_equal: 顶/底相等时的口径，含义见 KLine_Combiner.test_combine

        Note:
            缺口的定义是"两个元素的价格区间完全没有交集"：
                顶分型的元素，其低点还高过前一元素的高点；
                底分型的元素，其高点还低过前一元素的低点。
            有缺口的分型不能直接终结线段（标准做法要求后面再出现反向分型），
            所以 EigenFX.can_be_end 会另外去找那个反向分型。
        """
        super(CEigen, self).update_fx(_pre, _next, exclude_included, allow_top_equal)
        if (self.fx == FX_TYPE.TOP and _pre.high < self.low) or \
           (self.fx == FX_TYPE.BOTTOM and _pre.low > self.high):
            self.gap = True

    def __str__(self):
        """调试输出：`首笔idx~末笔idx gap=是否有缺口 fx=分型`。"""
        return f"{self.lst[0].idx}~{self.lst[-1].idx} gap={self.gap} fx={self.fx}"

    def GetPeakBiIdx(self):
        """
        取线段终点应该落在哪一根笔上。

        Returns:
            int: 线段终点笔的下标

        Note:
            取法：在本元素内找到造出极值的那根笔，再往前退一根（idx-1）。
            理由（以"上升线段的元素都是向下笔"为例）：
            极值笔是那个最低点所在的向下笔，而它前面那根是向上笔，
            向上笔的终点（高点）才是这条上升线段真正的终点 —— 所以取 idx-1。
            下降线段同理，只是方向反过来。
        """
        assert self.fx != FX_TYPE.UNKNOWN
        bi_dir = self.lst[0].dir
        if bi_dir == BI_DIR.UP:  # 下降线段
        # 元素是向上笔（属于下降线段）：取最低那根，再退一根就是终点向前走的最后一根
            return self.get_peak_klu(is_high=False).idx-1
        else:
            return self.get_peak_klu(is_high=True).idx-1
