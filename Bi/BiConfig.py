"""
笔的配置模块：CBiConfig。

它集中存放"能不能成笔"的三条判据的开关，被 Bi/BiList.py 的 can_make_bi 逐条消费。

链路位置：
    原始K线 -> 包含合并(CKLine) -> 分型 -> 笔(Bi，规则由本配置控制) -> 线段 -> 中枢 -> 买卖点

Note:
    本模块只是把 ChanConfig 里那些以字符串/布尔形式给出的配置项，
    翻译成内部枚举（FX_CHECK_METHOD）并做一次合法性检查，不做任何计算。
"""
from Common.CEnum import FX_CHECK_METHOD
from Common.ChanException import CChanException, ErrCode


class CBiConfig:
    """
    笔的配置项集合。

    六个开关分别控制：
        bi_algo         ：成笔算法（'fx' 时跳过跨度检查，只看分型）
        is_strict       ：跨度门槛走严格（>=4 根 KLC）还是宽松（>=3 根 KLC + >=3 根原始K线）
        bi_fx_check     ：分型配对检查的强度（strict/half/loss/totally）
        gap_as_kl       ：跳空缺口是否按K线计入跨度
        bi_end_is_peak  ：笔的终点是否必须是区间真极值
        bi_allow_sub_peak：是否允许"次高点/次低点"式的撤笔延长
    """

    def __init__(
        self,
        bi_algo="normal",
        is_strict=True,
        bi_fx_check="half",
        gap_as_kl=True,
        bi_end_is_peak=True,
        bi_allow_sub_peak=True,
    ):
        """
        Args:
            bi_algo: 成笔算法名，'fx' 表示只看分型、不检查跨度
            is_strict: 跨度门槛是否用严格口径
            bi_fx_check: 分型检查强度，取值 'strict'/'loss'/'half'/'totally'
            gap_as_kl: 跳空缺口是否折算成K线计入跨度
            bi_end_is_peak: 是否要求笔的终点是区间极值
            bi_allow_sub_peak: 是否允许次高点/次低点处理

        Raises:
            CChanException: bi_fx_check 取值不合法（ErrCode.PARA_ERROR）
        """
        self.bi_algo = bi_algo
        self.is_strict = is_strict
        if bi_fx_check == "strict":
            self.bi_fx_check = FX_CHECK_METHOD.STRICT
        elif bi_fx_check == "loss":
            self.bi_fx_check = FX_CHECK_METHOD.LOSS
        # 默认口径：分型 KLC 各带一侧邻居参与比较，严格程度居中
        elif bi_fx_check == "half":
            self.bi_fx_check = FX_CHECK_METHOD.HALF
        elif bi_fx_check == 'totally':
            self.bi_fx_check = FX_CHECK_METHOD.TOTALLY
        else:
            raise CChanException(f"unknown bi_fx_check={bi_fx_check}", ErrCode.PARA_ERROR)

        # 剩下的三个开关直接存成布尔值，供 BiList 成笔判定时取用
        self.gap_as_kl = gap_as_kl
        self.bi_end_is_peak = bi_end_is_peak
        self.bi_allow_sub_peak = bi_allow_sub_peak
