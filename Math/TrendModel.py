"""
趋势模型：对某一数值序列做「固定窗口滑动统计」，可选用均值/最大值/最小值。

用途：给 BI 等对象挂一组多周期统计值（如 N 日均线、近 T 根K线的区间高/低点），
供买卖点判定与绘图读取。配置入口是 CChanConfig 的 mean_metrics / trend_metrics。
"""

from Common.CEnum import TREND_TYPE
from Common.ChanException import CChanException, ErrCode


class CTrendModel:
    """单个周期的滑动统计器，窗口长度固定为 T。"""

    def __init__(self, trend_type: TREND_TYPE, T: int):
        """
        Args:
            trend_type: 统计类型，TREND_TYPE.MEAN / MAX / MIN
            T: 窗口长度
        """
        self.T = T
        self.arr = []
        self.type = trend_type

    def add(self, value) -> float:
        """
        喂入一个样本并返回当前窗口的统计值。

        Args:
            value: 样本值

        Returns:
            float: 窗口内的均值 / 最大值 / 最小值

        Raises:
            CChanException: trend_type 不是 MEAN/MAX/MIN（ErrCode.PARA_ERROR）

        Note:
            窗口不足 T 个样本时用现有全部样本统计。
        """
        self.arr.append(value)
        if len(self.arr) > self.T:
            # 滑动窗口：只保留最近 T 个样本
            self.arr = self.arr[-self.T:]
        if self.type == TREND_TYPE.MEAN:
            return sum(self.arr)/len(self.arr)
        elif self.type == TREND_TYPE.MAX:
            return max(self.arr)
        elif self.type == TREND_TYPE.MIN:
            return min(self.arr)
        else:
            raise CChanException(f"Unknown trendModel Type = {self.type}", ErrCode.PARA_ERROR)
