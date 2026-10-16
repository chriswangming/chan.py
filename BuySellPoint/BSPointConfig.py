"""
买卖点配置（BSPointConfig）模块：缠论链路中「中枢 -> 买卖点」这一环的算法参数。

CPointConfig 是"单侧"的一份配置（买侧或卖侧各一份），CBSPointConfig 把这两份打包在一起，
因为同一个标的的买点判据和卖点判据常常要分别调（例如涨得猛的票卖点条件要放宽）。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 线段 -> 中枢 -> 【本模块：买卖点算法参数】

Note:
    - 配置的默认值来自 ChanConfig.set_bsp_config：先铺一份默认参数表，
      再用 conf 里带 "-buy"/"-sell"/"-segbuy"/"-segsell"/"-seg" 后缀的键做定向覆盖，
      最后统一调 parse_target_type() 把 "1,2,3a" 这类字符串解析成 BSP_TYPE 列表。
    - 笔级买卖点用 bs_point_conf，线段级买卖点用 seg_bs_point_conf；
      后者默认把 macd_algo 改成 slope、bsp1_only_multibi_zs 改成 False（线段没法算MACD面积）。
    - 参数的实际消费方是 BuySellPoint/BSPointList.py 与 ZS/ZS.py（背驰判定）。
"""
from typing import Dict, List, Optional

from Common.CEnum import BSP_TYPE, MACD_ALGO
from Common.func_util import _parse_inf


class CBSPointConfig:
    """
    买卖点配置的容器：同时持有买侧（b_conf）与卖侧（s_conf）两套 CPointConfig。

    Note:
        两套配置的字段完全一样、只是取值可以不同；业务代码不直接访问 b_conf/s_conf，
        一律通过 GetBSConfig(is_buy) 取，避免把"买用卖的配置"这类错误散落到各处。
    """

    def __init__(self, **args):
        """
        Args:
            **args: 原样透传给两侧 CPointConfig 的构造参数（见 CPointConfig.__init__）

        Note:
            这里买/卖共享同一份入参，所以构造阶段两边一定相同；
            想让两边不同，要在构造之后用 b_conf.set() / s_conf.set() 单独改
            （ChanConfig.set_bsp_config 就是这么做的）。
        """
        self.b_conf = CPointConfig(**args)
        self.s_conf = CPointConfig(**args)

    def GetBSConfig(self, is_buy):
        """
        按买卖方向取对应的那份配置。

        Args:
            is_buy: True 取买点配置，False 取卖点配置

        Returns:
            CPointConfig: 对应方向的配置对象
        """
        return self.b_conf if is_buy else self.s_conf


class CPointConfig:
    """
    单侧（买或卖）的买卖点算法参数集合。

    Note:
        构造时只校验两项（max_bs2_rate <= 1、bsp3a_max_zs_cnt >= 1），其余参数都是
        "用到时才生效"，取值范围由调用方保证；bs_type 只是暂存的原始字符串，
        必须再调 parse_target_type() 才会变成判定逻辑真正使用的 target_types 列表。
    """

    def __init__(self,
                 divergence_rate,
                 min_zs_cnt,
                 bsp1_only_multibi_zs,
                 max_bs2_rate,
                 macd_algo,
                 bs1_peak,
                 bs_type,
                 bsp2_follow_1,
                 bsp3_follow_1,
                 bsp3_peak,
                 bsp2s_follow_2,
                 max_bsp2s_lv,
                 strict_bsp3,
                 bsp3a_max_zs_cnt,
                 ):
        """
        Args:
            divergence_rate: 背驰力度比阈值（float，>0）。判据是"出中枢笔力度 / 进中枢笔力度 <= 该值"，
                值越小越严格（要求出中枢那笔明显乏力），越大越宽松；默认 float("inf") 即基本不限制。
                配成 >100 时 ZS.is_divergence 会走"保送"分支：只要突破中枢就算背驰，完全跳过力度比较。
            min_zs_cnt: 1类买卖点要求线段内至少有几个中枢（int）；<=0 表示不做这个限制。默认 1。
            bsp1_only_multibi_zs: 统计中枢个数时是否只数"非一笔中枢"（bool）。
                True -> 用 seg.get_multi_bi_zs_cnt()，False -> 直接 len(seg.zs_lst)。默认 True，
                线段级配置（seg_bs_point_conf）默认被改成 False，因为线段内中枢本来就少。
            max_bs2_rate: 2类买卖点允许的最大回撤比例 = bsp2笔振幅 / 突破笔振幅，取值 (0, 1]。
                默认 0.9999（几乎不限制）；调小则只认"浅回撤"，例如 0.5 表示回撤不到一半才算2类。
            macd_algo: 力度口径（str），决定背驰比较时"力度"怎么算，见 SetMacdAlgo 的取值表。默认 "peak"。
            bs1_peak: 是否要求1类买卖点"突破中枢的那笔创了这段行情的极值"（bool，对应 CZS.out_bi_is_peak）。
                默认 True；关掉则允许"出中枢那笔并不是最低/最高点"的1类。
            bs_type: 需要计算的买卖点类型（逗号分隔字符串如 "1,2,3a"，或元素为字符串的列表）。
                合法元素只有 '1'/'2'/'3a'/'2s'/'1p'/'3b'，默认六类全开。
            bsp2_follow_1: 2类买卖点是否必须跟在已有的1类之后（bool）。默认 True，
                即"1类那笔上不存在买卖点时就不出2类"。
            bsp3_follow_1: 3类买卖点是否必须跟在已有的1类之后（bool）。默认 True。
            bsp3_peak: 3A 买卖点是否额外要求"突破中枢极值"（bool，对应 bsp3_break_zspeak）。默认 False。
            bsp2s_follow_2: "类二买卖点"是否必须先有2类（bool）。默认 False，
                即 2类没成立时仍会继续往后找类二。
            max_bsp2s_lv: 类二买卖点允许的最大层级（int 或 None），第 n 个类二的层级是 n（= bias/2）；
                None 表示不限层数。默认 None。
            strict_bsp3: 3类买卖点是否要求中枢的进/出笔与1类那笔严格相邻（bool）。默认 False。
            bsp3a_max_zs_cnt: 3A 最多考察下一段里的前几个中枢（int，>=1）。默认 1（只看第一个中枢）。

        Raises:
            AssertionError: max_bs2_rate > 1，或 bsp3a_max_zs_cnt < 1

        Note:
            所有参数都能在构造后用 set() 改写，唯一例外是 bs_type：
            改它之后必须重新调一次 parse_target_type()，否则 target_types 不会更新。
        """
        self.divergence_rate = divergence_rate
        self.min_zs_cnt = min_zs_cnt
        self.bsp1_only_multibi_zs = bsp1_only_multibi_zs
        self.max_bs2_rate = max_bs2_rate
        # 回撤比例超过1就没有筛选意义了（等于不限制），视为配置写错
        assert self.max_bs2_rate <= 1
        self.SetMacdAlgo(macd_algo)
        self.bs1_peak = bs1_peak
        # bs_type 只是"用户原始输入"的暂存，真正供判定使用的枚举列表要等 parse_target_type
        self.tmp_target_types = bs_type
        self.target_types: List[BSP_TYPE] = []
        self.bsp2_follow_1 = bsp2_follow_1
        self.bsp3_follow_1 = bsp3_follow_1
        self.bsp3_peak = bsp3_peak
        self.bsp2s_follow_2 = bsp2s_follow_2
        self.max_bsp2s_lv: Optional[int] = max_bsp2s_lv
        self.strict_bsp3 = strict_bsp3
        self.bsp3a_max_zs_cnt = bsp3a_max_zs_cnt
        # 一个都不看的话3A买卖点永远不会产生，属于无效配置
        assert self.bsp3a_max_zs_cnt >= 1

    def parse_target_type(self):
        """
        把 bs_type（字符串或字符串列表）解析成 BSP_TYPE 枚举列表，写入 target_types。

        Raises:
            AssertionError: 出现了 '1'/'2'/'3a'/'2s'/'1p'/'3b' 以外的类型名

        Note:
            必须由 ChanConfig 在配置装载完毕后调用一次；在此之前 target_types 一直是空列表，
            会导致所有类型都被判成"非目标买卖点"（只有1类还会被建出来用于内部串联）。
        """
        _d: Dict[str, BSP_TYPE] = {x.value: x for x in BSP_TYPE}
        if isinstance(self.tmp_target_types, str):
            self.tmp_target_types = [t.strip() for t in self.tmp_target_types.split(",")]
        for target_t in self.tmp_target_types:
            # 白名单校验：类型名写错时立刻炸，否则后面的判定会静默地一个买卖点都算不出来
            assert target_t in ['1', '2', '3a', '2s', '1p', '3b']
        self.target_types = [_d[_type] for _type in self.tmp_target_types]

    def SetMacdAlgo(self, macd_algo):
        """
        把字符串形式的力度口径翻译成 MACD_ALGO 枚举。

        Args:
            macd_algo: 口径名，取值与含义：
                area        -> MACD 半面积（从笔的一端累加到柱子变色为止）
                peak        -> 同色柱峰值（最敏感，默认）
                full_area   -> 笔区间内所有同色柱面积之和
                diff        -> 柱高最大值与最小值之差
                slope       -> 笔的斜率（单位K线上的相对涨幅，线段级买卖点默认用它）
                amp         -> 笔的涨跌幅（百分比口径，跨价位可比）
                amount      -> 笔上成交额之和
                volumn      -> 笔上成交量之和
                amount_avg  -> 笔上成交额均值
                volumn_avg  -> 笔上成交量均值
                turnrate_avg-> 笔上换手率均值
                rsi         -> 笔上 RSI 极值（向下笔取 10000/min 翻转成"越大越强"）

        Raises:
            KeyError: 传入了上表以外的名字

        Note:
            所有口径都约定"值越大表示这笔越有力"，买卖点处才能统一用
            out_metric/in_metric 做比值比较；不同口径之间不可比。
        """
        _d = {
            "area": MACD_ALGO.AREA,
            "peak": MACD_ALGO.PEAK,
            "full_area": MACD_ALGO.FULL_AREA,
            "diff": MACD_ALGO.DIFF,
            "slope": MACD_ALGO.SLOPE,
            "amp": MACD_ALGO.AMP,
            "amount": MACD_ALGO.AMOUNT,
            "volumn": MACD_ALGO.VOLUMN,
            "amount_avg": MACD_ALGO.AMOUNT_AVG,
            "volumn_avg": MACD_ALGO.VOLUMN_AVG,
            "turnrate_avg": MACD_ALGO.TURNRATE_AVG,
            "rsi": MACD_ALGO.RSI,
        }
        self.macd_algo = _d[macd_algo]

    def set(self, k, v):
        """
        改写单个参数（配置装载 / 回测调参的入口）。

        Args:
            k: 参数名，即 CPointConfig 上的属性名；"macd_algo" 会走专门的翻译分支
            v: 新值。注意实现是 exec(f"self.{k} = {v}")，所以 v 会被当成"源码字面量"求值：
                传字符串要自己带引号（ChanConfig 里就是先 f'"{v}"' 再传的），
                传 float("inf") 会先被 _parse_inf 转成 'float("inf")' 字面量。

        Raises:
            KeyError: macd_algo 取值非法（由 SetMacdAlgo 抛出）
            NameError / SyntaxError: v 是没带引号的字符串等非法字面量

        Note:
            用 exec 而非 setattr 是为了让 "inf"、表达式这类写法也能直接用；
            代价是键名与值都来自配置且不做白名单校验，前提是配置文件可信。
        """
        v = _parse_inf(v)
        if k == "macd_algo":
            self.SetMacdAlgo(v)
        else:
            exec(f"self.{k} = {v}")
