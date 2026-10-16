"""
线段算法之一：都业华的「1+1 终结法」（seg_algo='1+1'，已不推荐使用）。

思路：以连续三根笔为一组，用两个固定形态（situation1 / situation2）判断
    "这一组走完了没有"：
        situation1：中间那笔先被后面那笔反向覆盖（1+1 终结的第一类形态）；
        situation2：后面那笔直接把前面那笔的端点也越过（第二类形态）。
    命中其一，就认为前面的走势到中间那笔为止成一条线段。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 【本模块：1+1 法切线段】 -> 中枢 -> 买卖点

Note:
    与特征序列法的区别：它不做包含合并、不看缺口，只看这三笔的固定位置关系，
    规则简单直观但对"趋势延续"的容忍度更低，容易把长线段切碎。
"""
from Bi.BiList import CBiList
from Common.CEnum import BI_DIR, SEG_TYPE

from .SegConfig import CSegConfig
from .SegListComm import CSegListComm


def situation1(cur_bi, next_bi, pre_bi):
    """
    1+1 终结的形态一：中间那笔没创新低（趋势变弱），后面那笔整体把它包住。

    Args:
        cur_bi: 中间那根笔
        next_bi: 后面那根笔
        pre_bi: 前面隔一根的同向笔

    Returns:
        bool: 命中形态一则为 True
    """
    if cur_bi.is_down() and cur_bi._low() > pre_bi._low():
        if next_bi._high() < cur_bi._high() and next_bi._low() < cur_bi._low():
            return True
    elif cur_bi.is_up() and cur_bi._high() < pre_bi._high():
        if next_bi._low() > cur_bi._low() and next_bi._high() > cur_bi._high():
            return True
    return False


def situation2(cur_bi, next_bi, pre_bi):
    """
    1+1 终结的形态二：中间那笔创新低（趋势还在），但后面那笔直接越过了前面的端点。

    Args:
        cur_bi: 中间那根笔
        next_bi: 后面那根笔
        pre_bi: 前面隔一根的同向笔

    Returns:
        bool: 命中形态二则为 True
    """
    if cur_bi.is_down() and cur_bi._low() < pre_bi._low():
        if next_bi._high() < cur_bi._high() and next_bi._low() < pre_bi._low():
            return True
    elif cur_bi.is_up() and cur_bi._high() > pre_bi._high():
        if next_bi._low() > cur_bi._low() and next_bi._high() > pre_bi._high():
            return True
    return False


class CSegListDYH(CSegListComm):
    """
    用 1+1 终结法切分线段的线段列表实现（seg_algo='1+1'）。

    Note:
        与 SegListDef 一样已被标注为不再维护，新代码请用 SegListChan。
    """

    def __init__(self, seg_config=CSegConfig(), lv=SEG_TYPE.BI):
        """
        Args:
            seg_config: 线段配置 CSegConfig
            lv: 层级语义（SEG_TYPE.BI / SEG_TYPE.SEG）

        Note:
            sure_seg_update_end 表示"确认线段的终点还允许往后延伸"，
            本实现里恒为 False，即确认线段不再被改动。
        """
        super(CSegListDYH, self).__init__(seg_config=seg_config, lv=lv)
        self.sure_seg_update_end = False

    def update(self, bi_lst: CBiList):
        """
        重算线段列表（对基类抽象方法 update 的实现）。

        Args:
            bi_lst: 本次参与计算的笔列表

        Note:
            顺序是有讲究的：
                1) cal_bi_sure         —— 先按 1+1 形态切出确认线段；
                2) try_update_last_seg —— 已确认线段的尾部若创了新极值，把它往后挪（并降为未确认）；
                3) left_bi_break       —— 若有笔突破了最后一条线段，再算一版未确认线段；
                4) collect_left_seg    —— 最后照例补收尾。
        """
        self.do_init()
        self.cal_bi_sure(bi_lst)
        self.try_update_last_seg(bi_lst)
        if self.left_bi_break(bi_lst):
            self.cal_bi_unsure(bi_lst)
        self.collect_left_seg(bi_lst)

    def cal_bi_sure(self, bi_lst):
        """
        按 1+1 形态扫描笔列表、切出确认线段。

        Args:
            bi_lst: 本次参与计算的笔列表

        Note:
            扫描时有三道过滤：
                1) 只处理下标 >=2 且右边还有两根笔的位置（三根一组需要完整）；
                2) 方向必须与上一条线段的终点笔一致（线段是交替推进的）；
                3) 新线段与上一条线段终点笔之间至少要隔 4 根笔，避免切得太碎。
        """
        BI_LEN = len(bi_lst)
        next_begin_bi = bi_lst[0]
        for idx, bi in enumerate(bi_lst):
            # 三笔一组：左边要有两根、右边也要有后两根，位置不够就跳过
            if idx + 2 >= BI_LEN or idx < 2:
                continue
            # 只处理与上一条线段终点笔同向的笔（线段交替推进，反向的不参与）
            if len(self) > 0 and bi.dir != self[-1].end_bi.dir:
                continue
            # 与上一段终点之间被完全隔断（不重叠）的笔，不构成连续走势，跳过
            if bi.is_down() and bi_lst[idx-1]._high() < next_begin_bi._low():
                continue
            if bi.is_up() and bi_lst[idx-1]._low() > next_begin_bi._high():
                continue
            # 确认线段的终点若还允许延伸：创了新极值就把终点挪过去
            if self.sure_seg_update_end and len(self) and ((bi.is_down() and bi._low() < self[-1].end_bi._low()) or (bi.is_up() and bi._high() > self[-1].end_bi._high())):
                self[-1].end_bi = bi
                if idx != BI_LEN-1:
                    next_begin_bi = bi_lst[idx+1]
                    continue
            # 与上一条线段至少隔 4 根笔，且当前这组三笔命中 1+1 的两个形态之一
            if (len(self) == 0 or bi.idx - self[-1].end_bi.idx >= 4) and (situation1(bi, bi_lst[idx + 2], bi_lst[idx - 2]) or situation2(bi, bi_lst[idx + 2], bi_lst[idx - 2])):
                self.add_new_seg(bi_lst, idx-1)
                next_begin_bi = bi

    def cal_bi_unsure(self, bi_lst: CBiList):
        """
        在最后一条线段被突破的前提下，推算一条未确认线段。

        Args:
            bi_lst: 本次参与计算的笔列表

        Note:
            只在与最后一条线段反向的笔里找最极端的那根作为终点：
            上升线段之后要找最低的那根下降笔，下降线段之后要找最高的那根上升笔。
        """
        if len(self) == 0:
            return
        last_seg_dir = self[-1].end_bi.dir
        end_bi = None
        peak_value = float("inf") if last_seg_dir == BI_DIR.UP else float("-inf")
        for bi in bi_lst[self[-1].end_bi.idx+3:]:
            if bi.dir == last_seg_dir:
                continue
            cur_value = bi._low() if last_seg_dir == BI_DIR.UP else bi._high()
            if (last_seg_dir == BI_DIR.UP and cur_value < peak_value) or \
               (last_seg_dir == BI_DIR.DOWN and cur_value > peak_value):
                end_bi = bi
                peak_value = cur_value
        if end_bi:
            self.add_new_seg(bi_lst, end_bi.idx, is_sure=False)

    def try_update_last_seg(self, bi_lst: CBiList):
        """
        把最后一条（已确认）线段的终点往后挪到同方向笔的新极值处，并降级为未确认。

        Args:
            bi_lst: 本次参与计算的笔列表

        Note:
            被延伸的线段会同时被置为 is_sure=False：
            终点变了，这条线段就不再算"已被后续走势确认"。
        """
        if len(self) == 0:
            return
        last_bi = self[-1].end_bi
        peak_value = last_bi.get_end_val()
        new_peak_bi = None
        for bi in bi_lst[self[-1].end_bi.idx+1:]:
            if bi.dir != last_bi.dir:
                continue
            if bi.is_down() and bi._low() < peak_value:
                peak_value = bi._low()
                new_peak_bi = bi
            elif bi.is_up() and bi._high() > peak_value:
                peak_value = bi._high()
                new_peak_bi = bi
        if new_peak_bi:
            self[-1].end_bi = new_peak_bi
            self[-1].is_sure = False
