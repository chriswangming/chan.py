"""
线段算法之一：「线段破坏法」（seg_algo='break'，已不推荐使用）。

思路：不构造特征序列，而是直接看"后面那笔有没有破坏前面那笔的端点" ——
    对于向上线段，只要后面隔一根的笔比它更高（is_up_seg），就说明走势还在推进，
    于是把线段一路延长；直到出现反向的破坏，才在极值笔处收一条线段。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 【本模块：线段破坏法切线段】 -> 中枢 -> 买卖点

Note:
    与特征序列法（SegListChan）的区别：它只看笔之间的高低破坏关系，
    不做包含合并、不判缺口，所以更粗糙，也更容易在震荡行情里多切线段。
"""
from Bi.BiList import CBiList
from Common.CEnum import SEG_TYPE

from .SegConfig import CSegConfig
from .SegListComm import CSegListComm


def is_up_seg(bi, pre_bi):
    """
    判断 bi 是否"破坏"了前面隔一根的笔 pre_bi 的高点。

    Args:
        bi: 当前笔
        pre_bi: 前两根（同向）的笔

    Returns:
        bool: bi 的高点更高则为 True
    """
    return bi._high() > pre_bi._high()


def is_down_seg(bi, pre_bi):
    """
    判断 bi 是否"破坏"了前面隔一根的笔 pre_bi 的低点。

    Args:
        bi: 当前笔
        pre_bi: 前两根（同向）的笔

    Returns:
        bool: bi 的低点更低则为 True
    """
    return bi._low() < pre_bi._low()


class CSegListDef(CSegListComm):
    """
    用线段破坏法切分线段的线段列表实现（seg_algo='break'）。

    Note:
        算法已被标注为不再维护，新代码请用 SegListChan（seg_algo='chan'）。
    """

    def __init__(self, seg_config=CSegConfig(), lv=SEG_TYPE.BI):
        """
        Args:
            seg_config: 线段配置 CSegConfig
            lv: 层级语义（SEG_TYPE.BI / SEG_TYPE.SEG）

        Note:
            sure_seg_update_end 这个开关表示"确认线段的终点还允许往后延伸"，
            本实现里恒为 False，即确认线段不再被改动。
        """
        super(CSegListDef, self).__init__(seg_config=seg_config, lv=lv)
        self.sure_seg_update_end = False

    def update(self, bi_lst: CBiList):
        """
        重算线段列表（对基类抽象方法 update 的实现）。

        Args:
            bi_lst: 本次参与计算的笔列表

        Note:
            与前两种算法一样，最后必须用 collect_left_seg 补收尾，
            否则最新那截凑不满线段的笔会被整个丢掉。
        """
        self.do_init()
        self.cal_bi_sure(bi_lst)
        self.collect_left_seg(bi_lst)

    def update_last_end(self, bi_lst, new_endbi_idx: int):
        """
        把最后一条线段的终点笔往后挪到 new_endbi_idx。

        Args:
            bi_lst: 完整的笔列表
            new_endbi_idx: 新的终点笔下标

        Note:
            断言要求新终点至少比旧终点大 2 —— 线段必须由奇数根笔开头、
            且延伸后方向不翻转，中间至少要跨过两根笔。
            因为 update_bi_list 是 append 语义，这里会把新增的那几根笔追加进去。
        """
        last_endbi_idx = self[-1].end_bi.idx
        assert new_endbi_idx >= last_endbi_idx + 2
        self[-1].end_bi = bi_lst[new_endbi_idx]
        self.lst[-1].update_bi_list(bi_lst, last_endbi_idx, new_endbi_idx)

    def cal_bi_sure(self, bi_lst):
        """
        扫描笔列表，边推进边在"被破坏"的位置切出线段。

        Args:
            bi_lst: 本次参与计算的笔列表

        Note:
            主循环里维护一个 peak_bi（当前方向上最极端的那根笔）：
                1) 同方向又创新高的笔 -> 直接把 peak_bi 换成它（线段跟着延长）；
                2) 反向笔破坏了 peak_bi 的端点 -> 说明这条线段该结束了，
                   就在 peak_bi 处切一条线段，再把 peak_bi 指向新的方向上。
            循环结束后若 peak_bi 还挂着，说明最后的走势还没被破坏，
            以它收一条未确认线段。
        """
        peak_bi = None
        if len(bi_lst) == 0:
            return
        for idx, bi in enumerate(bi_lst):
            if idx < 2:
                continue
            # 同方向又创新高/新低 -> 候选终点直接换成它
            if peak_bi and ((bi.is_up() and peak_bi.is_up() and bi._high() >= peak_bi._high()) or (bi.is_down() and peak_bi.is_down() and bi._low() <= peak_bi._low())):
                peak_bi = bi
                continue
            # 确认线段的终点若还允许延伸：同方向笔创了新极值就把线段终点挪过去
            if self.sure_seg_update_end and len(self) and bi.dir == self[-1].dir and ((bi.is_up() and bi._high() >= self[-1].end_bi._high()) or (bi.is_down() and bi._low() <= self[-1].end_bi._low())):
                self.update_last_end(bi_lst, bi.idx)
                peak_bi = None
                continue
            # 与前面隔一根的同向笔比较，看这一笔有没有破坏它的端点
            pre_bi = bi_lst[idx-2]
            if (bi.is_up() and is_up_seg(bi, pre_bi)) or \
               (bi.is_down() and is_down_seg(bi, pre_bi)):
                # 破坏发生了：先定下这条线段的候选终点，再决定要不要切
                if peak_bi is None:
                    if len(self) == 0 or bi.dir != self[-1].dir:
                        peak_bi = bi
                        continue
                # 方向已经翻转：说明上一条线段该收了，但至少要满 3 根笔才收
                elif peak_bi.dir != bi.dir:
                    if bi.idx - peak_bi.idx <= 2:
                        continue
                    self.add_new_seg(bi_lst, peak_bi.idx)
                    peak_bi = bi
                    continue
        # 最后还没被破坏的那部分，收成一条未确认线段
        if peak_bi is not None:
            self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False)
