"""
中枢的配置模块：CZSConfig。

它决定中枢"怎么构造、要不要合并"。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 线段 -> 中枢（规则由本配置控制） -> 买卖点

Note:
    本模块没有任何计算逻辑，只是原样保存 ChanConfig 里传进来的几个开关。
"""

class CZSConfig:
    """
    中枢的配置项集合。

        need_combine   ：新中枢成型后是否尝试与前一个中枢合并
        zs_combine_mode：合并口径，'zs' 按中枢区间重叠、'peak' 按极值区间重叠
        one_bi_zs      ：是否允许"一笔中枢"（由单根笔构成的中枢）
        zs_algo        ：构造模式，'normal' / 'over_seg' / 'auto'
    """

    def __init__(self, need_combine=True, zs_combine_mode="zs", one_bi_zs=False, zs_algo="normal"):
        """
        Args:
            need_combine: 是否开启中枢合并
            zs_combine_mode: 合并口径，'zs' 或 'peak'
            one_bi_zs: 是否允许一笔中枢
            zs_algo: 构造模式，'normal' / 'over_seg' / 'auto'
        """
        self.need_combine = need_combine
        self.zs_combine_mode = zs_combine_mode
        self.one_bi_zs = one_bi_zs
        self.zs_algo = zs_algo
