"""
CCombine_Item —— 「包含关系判定用的统一取点适配器」。

缠论的包含处理和笔/线段算法需要反复比较两个元素的 high/low 与时间，
但参与比较的元素可能是三种完全不同的类型（原始K线 / 笔 / 线段），
它们的"时间"和"高低点"取法各不相同，于是统一在这里适配成
(time_begin, time_end, high, low) 四元组，供 CKLine_Combiner 等直接使用。

注意：这里的 high/low 对笔和线段取的是"端点值"（`_high()`/`_low()`），
时间取的是"合并K线的 idx"而不是真实日期 —— 因为包含处理只需要相对先后。
"""

from Common.ChanException import CChanException, ErrCode


class CCombine_Item:
    """把 CKLine_Unit / CBi / CSeg 适配成统一的 时间+高低点 视图。"""

    def __init__(self, item):
        """
        Args:
            item: 待适配的元素，可以是 CKLine_Unit、CBi、CSeg

        Raises:
            CChanException: item 是上述三种之外的类型（ErrCode.COMMON_ERROR）

        Note:
            三个 import 放在函数内是为了避免 Bi/Seg/KLine 之间的循环导入。
        """
        from Bi.Bi import CBi
        from KLine.KLine_Unit import CKLine_Unit
        from Seg.Seg import CSeg
        if isinstance(item, CBi):
            # 笔：时间用两端合并K线的 idx，高低点用笔的端点值
            self.time_begin = item.begin_klc.idx
            self.time_end = item.end_klc.idx
            self.high = item._high()
            self.low = item._low()
        elif isinstance(item, CKLine_Unit):
            # 原始K线：起止时间都是它自己的时间
            self.time_begin = item.time
            self.time_end = item.time
            self.high = item.high
            self.low = item.low
        elif isinstance(item, CSeg):
            # 线段：时间取首尾笔所连合并K线的 idx，高低点取线段两端值
            self.time_begin = item.start_bi.begin_klc.idx
            self.time_end = item.end_bi.end_klc.idx
            self.high = item._high()
            self.low = item._low()
        else:
            raise CChanException(f"{type(item)} is unsupport sub class of CCombine_Item", ErrCode.COMMON_ERROR)
