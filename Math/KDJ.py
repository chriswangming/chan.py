"""
KDJ（随机指标）。

    RSV = (close - 窗口内最低) / (窗口内最高 - 窗口内最低) * 100
    K   = 2/3 * 前一根K + 1/3 * RSV
    D   = 2/3 * 前一根D + 1/3 * K
    J   = 3K - 2D

初值取 K=D=J=50（中性），与常见行情软件一致。
窗口内最高 == 最低（一字线）时分母为 0，RSV 约定取 0。
"""


class KDJ_Item:
    """单根K线的 K/D/J 三项值。"""

    def __init__(self, k, d, j):
        """
        Args:
            k: K 值
            d: D 值
            j: J 值
        """
        self.k = k
        self.d = d
        self.j = j


class KDJ:
    """KDJ 计算器：流式喂入每根K线的最高/最低/收盘。"""

    def __init__(self, period: int = 9):
        """
        Args:
            period: RSV 的窗口长度，默认 9
        """
        super(KDJ, self).__init__()
        self.arr = []
        self.period = period
        # 初值取 50，与常见软件一致
        self.pre_kdj = KDJ_Item(50, 50, 50)

    def add(self, high, low, close) -> KDJ_Item:
        """
        喂入一根K线的最高/最低/收盘并返回该根K线的 KDJ。

        Args:
            high: 最高价
            low: 最低价
            close: 收盘价

        Returns:
            KDJ_Item: 该根K线的 K/D/J
        """
        # 只缓存 high/low（计算 RSV 需要），close 每根单独作为参数传入
        self.arr.append({
            'high': high,
            'low': low,
        })
        if len(self.arr) > self.period:
            # 滑动窗口：只保留最近 period 根
            self.arr.pop(0)

        hn = max([x['high'] for x in self.arr])
        ln = min([x['low'] for x in self.arr])
        cn = close
        # 一字线（hn == ln）分母为 0，RSV 约定取 0
        rsv = 100 * (cn - ln) / (hn - ln) if hn != ln else 0.0

        cur_k = 2 / 3 * self.pre_kdj.k + 1 / 3 * rsv
        cur_d = 2 / 3 * self.pre_kdj.d + 1 / 3 * cur_k
        cur_j = 3 * cur_k - 2 * cur_d
        cur_kdj = KDJ_Item(cur_k, cur_d, cur_j)
        self.pre_kdj = cur_kdj

        return cur_kdj
