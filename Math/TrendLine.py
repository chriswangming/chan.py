"""
趋势线拟合（用于线段的支撑线/压力线）。

调用点只有一处：`Seg/Seg.py:update_bi_list()`，当线段内的笔数 >= 3 时算两条线：
    support_trend_line    = CTrendLine(bi_list, TREND_LINE_SIDE.INSIDE)   取笔的「起点」
    resistance_trend_line = CTrendLine(bi_list, TREND_LINE_SIDE.OUTSIDE)  取笔的「终点」
结果只用于绘图（Plot/PlotMeta.py 读取 .line 画出来），不参与买卖点判定。

算法（简化版的最优趋势线搜索）：
    1. 取点：从最后一根笔往前**隔一根**取（`lst[-1::-2]`），这样取到的是同向笔的端点，
       注意因此 points[0] 是时间上**最晚**的那个端点；
    2. 筛选：以 points[0] 为锚点，只保留与「线段方向同向」的候选点
       （向上段忽略斜率 < 0 的点，向下段忽略斜率 > 0 的点）；
    3. 选斜率：在内侧/外侧两种口径下取最陡或最平的斜率（见 cal_tl）；
    4. 收缩：每轮用「锚点到最优点的直线」到**全部点**的距离和作为评分，
       记录迄今评分最小的那条线；然后把最优点的下标之前（更早）的点丢掉，再迭代，
       直到只剩一个点为止。这样得到的是"贴合度最好"的一条支撑/压力线。
"""

import copy
from dataclasses import dataclass
from math import sqrt

from Common.CEnum import BI_DIR, TREND_LINE_SIDE


@dataclass
class Point:
    """拟合用的二维点：x 取合并K线的 idx（相对序号），y 取笔的端点价。"""

    x: int
    y: float

    def cal_slope(self, p):
        """
        计算 self -> p 的斜率。

        Args:
            p: 另一个点

        Returns:
            float: (self.y - p.y) / (self.x - p.x)；两点 x 相同时返回正无穷（垂直线）
        """
        return (self.y-p.y)/(self.x-p.x) if self.x != p.x else float("inf")


@dataclass
class Line:
    """一条候选趋势线：过点 p、斜率为 slope。"""

    p: Point
    slope: float

    def cal_dis(self, p):
        """
        计算点 p 到本直线的距离（点到直线距离公式，直线写作 slope*x - y + c = 0）。

        Args:
            p: 待测点

        Returns:
            float: 垂直距离（非负）
        """
        return abs(self.slope*p.x - p.y + self.p.y - self.slope*self.p.x) / sqrt(self.slope**2 + 1)


class CTrendLine:
    """趋势线拟合器：构造时直接完成计算，结果放在 `self.line`。"""

    def __init__(self, lst, side=TREND_LINE_SIDE.OUTSIDE):
        """
        Args:
            lst: 线段内的笔列表（CBi 序列），长度建议 >= 3（只有 1~2 根笔会取不到足够点，见 cal）
            side: 取点侧，INSIDE 取笔起点、OUTSIDE 取笔终点
        """
        self.line = None
        self.side = side
        self.cal(lst)

    def cal(self, lst):
        """
        搜索评分最优的趋势线并写入 self.line。

        Args:
            lst: 笔列表

        Note:
            `bench` 是迄今最小的"点到线距离之和"，初始为 +inf，保证第一轮必定被采纳。
            每轮把点集从最优点开始截断重试，是让锚点逐步向更早的极值移动。
            对本项目当前唯一的调用点（线段内笔数 >= 3）而言取点数恒 >= 2，
            因此不存在单点导致 `c_p[idx:]` 变空的情况。
        """
        bench = float('inf')
        if self.side == TREND_LINE_SIDE.INSIDE:
            # 内侧：取每根笔的起点，隔一根取一次 → points[0] 是最晚的那个起点
            all_p = [Point(bi.get_begin_klu().idx, bi.get_begin_val()) for bi in lst[-1::-2]]
        else:
            # 外侧：取每根笔的终点，同样是隔一根取一次
            all_p = [Point(bi.get_end_klu().idx, bi.get_end_val()) for bi in lst[-1::-2]]
        c_p = copy.copy(all_p)   # 每轮迭代用的候选点集（浅拷贝，Point 复用即可）
        while True:
            # 用 c_p[0] 作锚点，挑出这一轮的最优斜率点（idx 是它在 c_p 中的下标）
            line, idx = cal_tl(c_p, lst[-1].dir, self.side)
            # 评分：这条线到**全部**点的距离之和，越小越贴合
            dis = sum(line.cal_dis(p) for p in all_p)
            if dis < bench:
                bench = dis
                self.line = line
            c_p = c_p[idx:]      # 丢掉最优点之前的点，锚点前移后继续搜
            if len(c_p) == 1:
                break


def init_peak_slope(_dir, side):
    """
    给斜率比较设一个"尽可能宽松"的初值，使得第一个通过的候选一定能被选中。

    Args:
        _dir: 线段方向，BI_DIR.UP / DOWN
        side: 取点侧

    Returns:
        float: 内侧固定 0（从水平线开始比较）；外侧向上取 +inf、向下取 -inf，
               等价于"还没有任何候选"
    """
    if side == TREND_LINE_SIDE.INSIDE:
        return 0
    elif _dir == BI_DIR.UP:
        return float("inf")
    else:
        return -float("inf")


def cal_tl(c_p, _dir, side):
    """
    以 c_p[0] 为锚点，在其余点里挑一个斜率最合适的，返回直线与最优点下标。

    Args:
        c_p: 候选点集，c_p[0] 是锚点
        _dir: 线段方向，决定哪些斜率是"顺向"的
        side: 取点侧，决定挑最陡还是最平的斜率

    Returns:
        tuple[Line, int]: 直线，以及最优点在 c_p 里的下标（初始为 1，
                          没有任何候选通过筛选时就保持 1，斜率取 init_peak_slope 的初值）

    Note:
        斜率筛选规则：
            - 向上段只保留斜率 >= 0 的点，向下段只保留斜率 <= 0 的点（反向点直接跳过）；
            - 内侧（INSIDE）：向上取**最大**斜率（更陡），向下取最小斜率；
            - 外侧（OUTSIDE）：向上取**最小**斜率（更平），向下取最大斜率。
    """
    p = c_p[0]
    peak_slope = init_peak_slope(_dir, side)
    idx = 1
    for point_idx, p2 in enumerate(c_p[1:]):
        slope = p.cal_slope(p2)
        # 与线段方向相反的候选点直接淘汰
        if (_dir == BI_DIR.UP and slope < 0) or (_dir == BI_DIR.DOWN and slope > 0):
            continue
        if side == TREND_LINE_SIDE.INSIDE:
            # 内侧找更陡的线
            if (_dir == BI_DIR.UP and slope > peak_slope) or (_dir == BI_DIR.DOWN and slope < peak_slope):
                peak_slope = slope
                idx = point_idx+1
        else:
            # 外侧找更平的线
            if (_dir == BI_DIR.UP and slope < peak_slope) or (_dir == BI_DIR.DOWN and slope > peak_slope):
                peak_slope = slope
                idx = point_idx+1
    return Line(p, peak_slope), idx
