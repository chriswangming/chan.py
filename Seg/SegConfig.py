"""
线段的配置模块：CSegConfig。

它只决定两件事：用哪种算法切线段（seg_algo），
以及尾部凑不满线段的笔怎么收尾（left_method）。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 线段（算法由本配置选择） -> 中枢 -> 买卖点

Note:
    本模块只是把 ChanConfig 里的字符串翻译成内部枚举并做合法性检查，不做计算。
"""
from Common.CEnum import LEFT_SEG_METHOD
from Common.ChanException import CChanException, ErrCode


class CSegConfig:
    """
    线段的配置项集合。

        seg_algo   ：'chan' 特征序列法（默认）、'1+1' 都业华法、'break' 线段破坏法
        left_method：'peak' 遇到可靠极值就多切一段（默认）、'all' 把剩余笔全部并入上一段
    """

    def __init__(self, seg_algo="chan", left_method="peak"):
        """
        Args:
            seg_algo: 线段算法名，取值 'chan' / '1+1' / 'break'
            left_method: 补收尾方式，取值 'peak' / 'all'

        Raises:
            CChanException: left_method 取值不合法（ErrCode.PARA_ERROR）
        """
        self.seg_algo = seg_algo
        if left_method == "all":
            self.left_method = LEFT_SEG_METHOD.ALL
        # 默认策略：宁可多切一段（把极值处收成一段），也不要把走势糊在一起
        elif left_method == "peak":
            self.left_method = LEFT_SEG_METHOD.PEAK
        else:
            raise CChanException(f"unknown left_seg_method={left_method}", ErrCode.PARA_ERROR)
