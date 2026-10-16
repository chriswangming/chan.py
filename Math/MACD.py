"""
MACD 指标（指数平滑异同移动平均线）。

口径与常见行情软件一致（国内习惯乘 2）：
    DIF  = EMA(close, fastperiod) - EMA(close, slowperiod)   # 快慢均线之差
    DEA  = EMA(DIF, signalperiod)                            # DIF 的平滑，即信号线
    MACD = 2 * (DIF - DEA)                                   # 柱子（红绿柱）

用法是流式：每来一根K线就把收盘价喂给 add()，内部按递推公式累积，不做全量重算。
缠论里的"背驰"力度/面积比较就取这里的 macd 柱、DIF、DEA。

注意：本实现不做 EMA 预热（第 0 根直接用收盘价当快慢 EMA 的初值），
所以最前面几十根的值与行情软件的标准算法会有偏差，越往后越收敛。
"""

from typing import List


class CMACD_item:
    """单根K线算完的 MACD 快照，供背驰比较和绘图直接读取。"""

    def __init__(self, fast_ema, slow_ema, DIF, DEA):
        """
        Args:
            fast_ema: 当前 fastperiod 期 EMA 值
            slow_ema: 当前 slowperiod 期 EMA 值
            DIF: fast_ema - slow_ema
            DEA: DIF 的 signalperiod 期 EMA
        """
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.DIF = DIF
        self.DEA = DEA
        # 柱值：国内软件习惯是 (DIF-DEA)*2
        self.macd = 2 * (DIF - DEA)


class CMACD:
    """MACD 计算器：流式累加，每根K线调一次 add()。"""

    def __init__(self, fastperiod=12, slowperiod=26, signalperiod=9):
        """
        Args:
            fastperiod: 快线周期，默认 12
            slowperiod: 慢线周期，默认 26
            signalperiod: DEA 的平滑周期，默认 9
        """
        self.macd_info: List[CMACD_item] = []
        self.fastperiod = fastperiod
        self.slowperiod = slowperiod
        self.signalperiod = signalperiod

    def add(self, value) -> CMACD_item:
        """
        喂入当前K线的价格（缠论里用收盘价），返回这根K线的 MACD 快照。

        Args:
            value: 当前K线价格（通常取收盘价）

        Returns:
            CMACD_item: 该根K线对应的 MACD 三项值
        """
        if not self.macd_info:
            # 第一根：快慢 EMA 都初始化成该价格、DIF/DEA 置 0。
            # 相当于"从第 0 根开始预热"，不做历史回填（见模块说明）。
            self.macd_info.append(CMACD_item(fast_ema=value, slow_ema=value, DIF=0, DEA=0))
        else:
            # EMA(x, N) 递推式：EMA_t = (2*x_t + (N-1)*EMA_{t-1}) / (N+1)
            _fast_ema = (2 * value + (self.fastperiod - 1) * self.macd_info[-1].fast_ema) / (self.fastperiod + 1)
            _slow_ema = (2 * value + (self.slowperiod - 1) * self.macd_info[-1].slow_ema) / (self.slowperiod + 1)
            _dif = _fast_ema - _slow_ema
            # DEA 是 DIF 的 EMA，同一个递推式，只是输入序列换成 DIF
            _dea = (2 * _dif + (self.signalperiod - 1) * self.macd_info[-1].DEA) / (self.signalperiod + 1)
            self.macd_info.append(CMACD_item(fast_ema=_fast_ema, slow_ema=_slow_ema, DIF=_dif, DEA=_dea))
        return self.macd_info[-1]
