# 合并后的K线 CKLine（简称 KLC）。
#
# 链路位置：CKLine_Unit（单根原始K线） -> CKLine（做包含关系处理，把若干根同方向 KLU 并成一根） -> CKLine_List（某级别整段序列）。
#
# 它承上启下：
#   向上给 CKLine_List 提供用于判笔的材料（分型 fx、能否成笔 check_fx_valid、是否为跳空 has_gap_with_next）；
#   包含处理本身、区间端点定位（get_peak_klu）等能力继承自 CKLine_Combiner。
from Combiner.KLine_Combiner import CKLine_Combiner
from Common.CEnum import FX_CHECK_METHOD, FX_TYPE, KLINE_DIR
from Common.ChanException import CChanException, ErrCode
from Common.func_util import has_overlap
from KLine.KLine_Unit import CKLine_Unit


# 合并后的K线
class CKLine(CKLine_Combiner[CKLine_Unit]):
    """
    包含处理后的单根合并K线，是判分型、判笔的基本单位。

    它自己只负责"序号 / 级别 / 展开成子级别KLC / 校验两点能否连成笔"这几件事，
    高低价、lst、dir、fx 都由父类 CKLine_Combiner 维护：
      - lst       ：里面装的都是同级别的 CKLine_Unit
      - fx        ：分型类型（TOP/BOTTOM/UNKNOWN），由外部拿到前后邻居后调 update_fx 算出
      - get_peak_klu：笔端点常常需要精确到"是哪一根原始 KLU 打出的极值"，
        于是它要在 lst 里从后往前扫描找到等于区间 high/low 的那根，而不是直接用合并K线的时间。
    """

    def __init__(self, kl_unit: CKLine_Unit, idx, _dir=KLINE_DIR.UP):
        """
        Args:
            kl_unit: 作为本 KLC 第一根成员的原始K线
            idx: 本 KLC 在 CKLine_List 中的下标
            _dir: 本 KLC 的合并方向（KLINE_DIR.UP/DOWN），决定后续包含处理时攒 high 还是攒 low

        Note:
            最后一行 kl_unit.set_klc(self) 是副作用：让这根 KLU 反向指回本 KLC；
            后续被合并进来的 KLU 由 try_add 负责做同样的反向设置。
        """
        super(CKLine, self).__init__(kl_unit, _dir)
        self.idx: int = idx
        self.kl_type = kl_unit.kl_type
        kl_unit.set_klc(self)

    def __str__(self):
        """
        Returns:
            str: "序号th[分型标记]:起~止(级别|含几根原始K线) low=.. high=.."，
                顶分型标记为 ^、底分型标记为 _，无分型时为空。
        """
        fx_token = ""
        if self.fx == FX_TYPE.TOP:
            fx_token = "^"
        elif self.fx == FX_TYPE.BOTTOM:
            fx_token = "_"
        return f"{self.idx}th{fx_token}:{self.time_begin}~{self.time_end}({self.kl_type}|{len(self.lst)}) low={self.low} high={self.high}"

    def GetSubKLC(self):
        """
        生成本 KLC 在次级别对应的那些合并K线（去相邻重复）。

        Yields:
            CKLine: 次级别的合并K线

        Note:
            做法是遍历本 KLC 内每根 KLU 的次级别 KLU，再取它们各自的 klc；
            因为子级别做包含合并时可能跨越了父 KLC 的时间边界，
            相邻两个父 KLC 有可能拿到同一根子 KLC，所以用 last_klc 只保留首次出现的那次。
        """
        # 可能会出现相邻的两个KLC的子KLC会有重复
        # 因为子KLU合并时正好跨过了父KLC的结束时间边界
        last_klc = None
        for klu in self.lst:
            for sub_klu in klu.get_children():
                if sub_klu.klc != last_klc:
                    last_klc = sub_klu.klc
                    yield sub_klu.klc

    def get_klu_max_high(self) -> float:
        """
        Returns:
            float: 本 KLC 内所有原始K线的最高价的最大值。

        Note:
            取的是原始 KLU 的 high，与 self.high 不一定相等：
            向下合并时 KLC.high 会被压成 min，于是必须回到 lst 里重算真实的极值区间，
            判跳空用的就是这个"真实区间"。
        """
        return max(x.high for x in self.lst)

    def get_klu_min_low(self) -> float:
        """
        Returns:
            float: 本 KLC 内所有原始K线的最低价的最小值（理由同 get_klu_max_high）。
        """
        return min(x.low for x in self.lst)

    def has_gap_with_next(self) -> bool:
        """
        判断本 KLC 与后一根 KLC 之间是否存在跳空（价格区间完全没有交集）。

        Returns:
            bool: True 表示有缺口

        Raises:
            AssertionError: 没有后一根 KLC 时调用

        Note:
            equal=True 表示"边界刚好相等也算重叠"，即端点相接不算跳空。
        """
        assert self.next is not None
        # 相同也算重叠，也就是没有gap
        return not has_overlap(self.get_klu_min_low(), self.get_klu_max_high(), self.next.get_klu_min_low(), self.next.get_klu_max_high(), equal=True)

    def check_fx_valid(self, item2: "CKLine", method, for_virtual=False):
        """
        判断"self 和 item2 这一对（顶/底）分型之间是否够格连成一笔"。

        Args:
            item2: 另一端分型所在的 KLC，必须在 self 之后且 fx 与 self 相反（虚笔除外）
            method: FX_CHECK_METHOD，决定参与比较高低的"邻居范围"：
                LOSS 只看两根分型 KLC 本身（最松）；
                HALF 各带一侧邻居；
                STRICT 两侧邻居都带上（最严，但仍是区间比较）；
                TOTALLY 直接要求两段区间完全不重叠
            for_virtual: True 表示在试探虚笔，此时不要求 item2 已经是反向分型，
                只要求它的方向合适（self 是顶，item2 必须往下走）

        Returns:
            bool: True 表示这两点可以连成一笔

        Note:
            核心思想是把比较对象从"单个点"升级成"一段区间"（self 及其邻居构成的区间、
            item2 及其邻居构成的区间），避免用单点极值误判；method 越严格，
            参与极值的邻居越多，越不容易误连成笔。

        Raises:
            CChanException: method 配置非法（ErrCode.CONFIG_ERROR），
                或 self 本身不是顶/底分型（ErrCode.BI_ERR）
            AssertionError: 前置条件不成立（缺前驱/后继、item2 不在 self 之后等）
        """
        # for_virtual: 虚笔时使用
        assert self.next is not None and item2.pre is not None
        assert self.pre is not None
        assert item2.idx > self.idx
        if self.fx == FX_TYPE.TOP:
            assert for_virtual or item2.fx == FX_TYPE.BOTTOM
            # 虚笔也要方向对：顶分型后面必须是往下走的一段
            if for_virtual and item2.dir != KLINE_DIR.DOWN:
                return False
            # 四种 method 的唯一差别就是"取哪几根 KLC 的高低点来比较"，
            # 邻居取得越多，比较的区间越宽，成笔越难（越严格）
            if method == FX_CHECK_METHOD.HALF:  # 检测前两KLC
                item2_high = max([item2.pre.high, item2.high])
                self_low = min([self.low, self.next.low])
            elif method == FX_CHECK_METHOD.LOSS:  # 只检测顶底分形KLC
                item2_high = item2.high
                self_low = self.low
            elif method in (FX_CHECK_METHOD.STRICT, FX_CHECK_METHOD.TOTALLY):
                if for_virtual:
                    item2_high = max([item2.pre.high, item2.high])
                else:
                    assert item2.next is not None
                    item2_high = max([item2.pre.high, item2.high, item2.next.high])
                self_low = min([self.pre.low, self.low, self.next.low])
            else:
                raise CChanException("bi_fx_check config error!", ErrCode.CONFIG_ERROR)
            if method == FX_CHECK_METHOD.TOTALLY:
                # 最严：只要求"顶分型整体区间的最低点 高于 底分型整体区间的最高点"
                return self.low > item2_high
            else:
                # 其余 method：既要求顶比对方区间高，也要求对方的低点比自己的区间更低
                return self.high > item2_high and item2.low < self_low
        elif self.fx == FX_TYPE.BOTTOM:
            assert for_virtual or item2.fx == FX_TYPE.TOP
            # 虚笔方向对齐：底分型后面必须是往上走的一段
            if for_virtual and item2.dir != KLINE_DIR.UP:
                return False
            if method == FX_CHECK_METHOD.HALF:
                item2_low = min([item2.pre.low, item2.low])
                cur_high = max([self.high, self.next.high])
            elif method == FX_CHECK_METHOD.LOSS:
                item2_low = item2.low
                cur_high = self.high
            elif method in (FX_CHECK_METHOD.STRICT, FX_CHECK_METHOD.TOTALLY):
                if for_virtual:
                    item2_low = min([item2.pre.low, item2.low])
                else:
                    assert item2.next is not None
                    item2_low = min([item2.pre.low, item2.low, item2.next.low])
                cur_high = max([self.pre.high, self.high, self.next.high])
            else:
                raise CChanException("bi_fx_check config error!", ErrCode.CONFIG_ERROR)
            if method == FX_CHECK_METHOD.TOTALLY:
                # 与顶分型分支对称：要求底分型整体区间完全高于顶分型整体区间
                return self.high < item2_low
            else:
                return self.low < item2_low and item2.high > cur_high
        else:
            raise CChanException("only top/bottom fx can check_valid_top_button", ErrCode.BI_ERR)
