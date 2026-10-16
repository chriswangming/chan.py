"""
布林线（BOLL）指标。

    MID  = N 个样本的均值
    UP   = MID + 2 * 标准差
    DOWN = MID - 2 * 标准差

标准差用"总体标准差"（分母为 N，不是 N-1），与多数行情软件的 BOLL 口径一致。
_truncate() 把 0 换成一个极小正数，避免后续绘图/求比率时出现除零。
"""

import math


def _truncate(x):
    """把 0 替换成 1e-7（规避除零）；非 0 原样返回。"""
    return x if x != 0 else 1e-7


class BOLL_Metric:
    """单根K线的布林三轨值。"""

    def __init__(self, ma, theta):
        """
        Args:
            ma: N 个样本的均值（中轨）
            theta: N 个样本的标准差
        """
        self.theta = _truncate(theta)
        self.UP = ma + 2*theta
        # 下轨为 0 时同样抬到 1e-7
        self.DOWN = _truncate(ma - 2*theta)
        self.MID = ma


class BollModel:
    """布林线计算器：流式喂值，内部只保留最近 N 个样本。"""

    def __init__(self, N=20):
        """
        Args:
            N: 计算窗口长度，默认 20
        """
        assert N > 1
        self.N = N
        self.arr = []

    def add(self, value) -> BOLL_Metric:
        """
        喂入一个样本并返回当前窗口的布林轨。

        Args:
            value: 样本值

        Returns:
            BOLL_Metric: 该样本对应的上/中/下轨

        Note:
            样本不足 N 个时用现有的全部样本计算，所以最前面几根的轨宽偏大。
        """
        self.arr.append(value)
        if len(self.arr) > self.N:
            # 滑动窗口：只保留最近 N 个
            self.arr = self.arr[-self.N:]
        ma = sum(self.arr)/len(self.arr)
        # 总体标准差（分母是 len，不是 len-1）
        theta = math.sqrt(sum((x-ma)**2 for x in self.arr) / len(self.arr))
        return BOLL_Metric(ma, theta)
