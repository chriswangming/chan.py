"""
RSI（相对强弱指标），Wilder 口径。

    diff  = close_t - close_{t-1}
    上涨力度 up   = diff 中为正的部分
    下跌力度 down = diff 中为负的部分取绝对值
    前 period 根：up/down 用"简单平均"（每次在全量 diff 上重算）
    之后：up_t = (up_{t-1}*(period-1) + 本根 up) / period   （Wilder 平滑递推）
    RS  = up / down
    RSI = 100 - 100 / (1 + RS)

边界处理：down 为 0 且 up > 0 返回 100；down 为 0 且 up 也为 0 返回 0。
"""


class RSI:
    """RSI 计算器：流式喂入收盘价。"""

    def __init__(self, period: int = 14):
        """
        Args:
            period: 平滑周期，默认 14
        """
        super(RSI, self).__init__()   # 实际是 object.__init__，无副作用
        self.close_arr = []
        self.period = period
        self.diff = []    # 每根K线的价格变化 close_t - close_{t-1}
        self.up = []      # 平滑后的上涨力度序列
        self.down = []    # 平滑后的下跌力度序列

    def add(self, close):
        """
        喂入一根K线的收盘价并返回当前 RSI。

        Args:
            close: 收盘价

        Returns:
            float: 当前 RSI（0~100）

        Note:
            序列里第一根没有前一根可比，直接返回中性值 50。
        """
        self.close_arr.append(close)
        if len(self.close_arr) == 1:
            # 只有一根K线，没有涨跌，取中性值
            return 50.0

        self.diff.append(self.close_arr[-1] - self.close_arr[-2])

        if len(self.diff) < self.period:
            # 样本不足 period：用简单平均，且每次都在全量 diff 上重新求和
            up_sum = sum(x for x in self.diff if x > 0)
            down_sum = sum(-x for x in self.diff if x < 0)
            self.up.append(up_sum / len(self.diff))
            self.down.append(down_sum / len(self.diff))
        else:
            # 样本够了，转入 Wilder 平滑递推
            if self.diff[-1] > 0:
                upval = self.diff[-1]
                downval = 0.0
            else:
                # 包含 diff == 0 的情况：此时上涨力度与下跌力度都记 0
                upval = 0.0
                downval = -self.diff[-1]

            self.up.append((self.up[-1] * (self.period - 1) + upval) / self.period)
            self.down.append((self.down[-1] * (self.period - 1) + downval) / self.period)

        if self.down[-1] == 0:
            # 区间内没有下跌：全上涨给 100，全平（up 也为 0）给 0
            return 100.0 if self.up[-1] > 0 else 0.0

        rs = self.up[-1] / self.down[-1]
        rsi = 100.0 - 100.0 / (1.0 + rs)
        return rsi
