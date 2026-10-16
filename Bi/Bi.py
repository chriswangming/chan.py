"""
笔（Bi）模块：缠论计算链路中「分型 -> 笔」这一环。

一根笔 CBi 就是"从一个分型 KLC 走到另一个反向分型 KLC"的一段走势：
方向由起点分型的类型唯一决定（底分型 -> 向上笔，顶分型 -> 向下笔），
两个端点分别是这一段走势的最高点与最低点。

链路位置：
    原始K线 -> 包含合并(CKLine) -> 分型 -> 【本模块：笔】 -> 线段(Seg) -> 中枢(ZS) -> 买卖点

本模块负责三件事：
    1. 维护端点：既有合并K线粒度（begin_klc/end_klc），
       也有"真正打出极值的那根原始K线"粒度（get_begin_klu/get_end_klu）；
    2. 派生量的计算：高低点、中位价、振幅、所含K线根数，以及买卖点背驰比较
       要用的力度口径（MACD 面积/峰值/差值/斜率、涨跌幅、RSI、成交量额）；
    3. 维护虚笔状态：尾部尚未被走势确认的笔可以被反复延长或回退，
       update_virtual_end / restore_from_virtual_end 就是这一对"降级-还原"操作。

Note:
    - "哪些K线能成笔、旧笔要不要回收"由兄弟模块 Bi/BiList.py 决定，
      本模块只关心单根笔自身的属性，不参与成笔判定。
    - 所有派生量都用 @make_cache 按实例缓存，因此端点一旦变化必须调 clean_cache()，
      否则会一直读到旧值（见 set()/update_new_end()）。
"""
from typing import List, Optional

from Common.cache import make_cache
from Common.CEnum import BI_DIR, BI_TYPE, DATA_FIELD, FX_TYPE, MACD_ALGO
from Common.ChanException import CChanException, ErrCode
from KLine.KLine import CKLine
from KLine.KLine_Unit import CKLine_Unit


class CBi:
    """
    一根笔。

    由起点分型合并K线 begin_klc 与终点分型合并K线 end_klc 界定，
    方向在 set() 里由起点分型的类型推出来（底分型 -> UP，顶分型 -> DOWN）。

    除了端点，本类还提供：
        - 区间端点：_high()/_low()/_mid() 与振幅 amp()；
        - 精确端点：get_begin_klu()/get_end_klu()（造出极值的那根原始K线）；
        - 计数：get_klu_cnt()/get_klc_cnt()；
        - 力度：cal_macd_metric() 及其背后的 Cal_MACD_* / Cal_Rsi 系列；
        - 归属：seg_idx（所属线段下标）、parent_seg/bsp（反向指针）、pre/next（链表）。

    Note:
        笔有两种身份：
            - 确认笔 is_sure=True：尾部已被后续走势确认，不会再被往回改；
            - 虚笔     is_sure=False：尾部还会随后续K线被改写（update_virtual_end）。
        虚笔被改写前，会把当前尾部 KLC 压进 sure_end 栈，
        Bi/BiList.py 的 delete_virtual_bi 靠它把笔还原成原来那根确认笔。
    """

    def __init__(self, begin_klc: CKLine, end_klc: CKLine, idx: int, is_sure: bool):
        """
        Args:
            begin_klc: 笔的起点合并K线，必须已经判定出分型（TOP/BOTTOM），它决定笔的方向
            end_klc: 笔的终点合并K线
            idx: 笔的序号，从 0 起按出现顺序递增
            is_sure: True 表示这是已被后续走势确认的确认笔；False 表示虚笔

        Note:
            - 方向不是入参，而是在 set() 里由 begin_klc.fx 推出来的，
              所以创建笔时传进来的起点必须已经是分型K线。
            - Seg / BuySellPoint 都是延后到函数体内 import 的：
              这两个模块反向依赖 CBi，放到文件头会造成循环引用。
            - pre/next 链表指针这里只是留占位，真正的串接由 BiList.add_new_bi 完成。
        """
        # self.__begin_klc = begin_klc
        # self.__end_klc = end_klc
        self.__dir = None
        self.__idx = idx
        self.__type = BI_TYPE.STRICT

        # 端点、方向的自洽检查和缓存清理都收敛在 set() 里，别绕过它直接改私有字段
        self.set(begin_klc, end_klc)

        self.__is_sure = is_sure
        self.__used_to_be_sure = is_sure
        self.__sure_end: List[CKLine] = []

        self.__seg_idx: Optional[int] = None

        # 下面两个反向指针所属的模块都依赖 CBi，只能在函数体内延迟 import
        from Seg.Seg import CSeg
        self.parent_seg: Optional[CSeg[CBi]] = None  # 在哪个线段里面

        from BuySellPoint.BS_Point import CBS_Point
        self.bsp: Optional[CBS_Point] = None  # 尾部是不是买卖点

        self.next: Optional[CBi] = None
        self.pre: Optional[CBi] = None

    def clean_cache(self):
        """清空 @make_cache 缓存：端点/区间被改写后必须调一次，否则会读到旧值。"""
        self._memoize_cache = {}

    # 笔的起点合并K线（一定是有分型的那根 KLC）
    @property
    def begin_klc(self): return self.__begin_klc

    # 笔的终点合并K线；虚笔时它会被 update_virtual_end 反复往后改
    @property
    def end_klc(self): return self.__end_klc

    # 笔的方向：BI_DIR.UP / BI_DIR.DOWN
    @property
    def dir(self): return self.__dir

    # 笔的序号，由 BiList.add_new_bi 用 len(bi_list) 赋给新笔（详见 BiList 里对 idx 的说明）
    @property
    def idx(self): return self.__idx

    # 笔的类型，构造时恒定为 BI_TYPE.STRICT（严格笔），目前没有其它取值被写入
    @property
    def type(self): return self.__type

    # 是否是确认笔；False 表示尾部还没被后续走势确认的虚笔
    @property
    def is_sure(self): return self.__is_sure

    # 是否曾经是确认笔：变虚之后依然要记住「曾经成立过」，买卖点去重就靠它
    @property
    def used_to_be_sure(self): return self.__used_to_be_sure

    # 现在或曾经是确认笔（True 表示这根笔的形态曾经成立过，可用于回溯判断）
    @property
    def is_used_to_be_sure(self): return self.is_sure or self.used_to_be_sure

    # 曾被确认过的尾部 KLC 栈：每次被改写为虚笔前，当前尾部都会被压进来
    @property
    def sure_end(self): return self.__sure_end

    @property
    def klc_lst(self):
        """
        顺序产出本笔覆盖的合并K线（含首尾两根）。

        Yields:
            CKLine: 从 begin_klc 顺着走到 end_klc 的每一根合并K线

        Note:
            它是沿着 KLC 的 next 链表边走边产出的（以 idx 越界作为终止条件），
            不是某个时刻的静态快照，所以虚笔延长后再次迭代会自然得到更长的序列。
        """
        klc = self.begin_klc
        while True:
            yield klc
            klc = klc.next
            if not klc or klc.idx > self.end_klc.idx:
                break

    @property
    def klc_lst_re(self):
        """
        逆序产出本笔覆盖的合并K线（从尾部倒着走），用途与 klc_lst 相反。

        Yields:
            CKLine: 从 end_klc 倒回到 begin_klc 的每一根合并K线

        Note:
            Cal_MACD_half_reverse 用它从尾端倒着累加 MACD 面积。
        """
        klc = self.end_klc
        while True:
            yield klc
            klc = klc.pre
            if not klc or klc.idx < self.begin_klc.idx:
                break

    # 所属线段的下标；由 KLine/KLine_List.py 的 cal_seg 回写，None 表示还没归属任何线段
    @property
    def seg_idx(self): return self.__seg_idx

    def set_seg_idx(self, idx):
        """
        Args:
            idx: 所属线段的下标

        Note:
            这是被外部（KLine_List.cal_seg）回写的字段，每次重算线段时会整刷一遍，
            本类自己不会去维护它。
        """
        self.__seg_idx = idx

    def __str__(self):
        """调试输出：`方向|起点KLC ~ 终点KLC`，两端 KLC 各自再展开成它们自己的 __str__。"""
        return f"{self.dir}|{self.begin_klc} ~ {self.end_klc}"

    def check(self):
        """
        自检笔的方向与首尾位置是否自洽，不自洽就抛异常。

        Raises:
            CChanException: 向下笔却没有走低（起点高 <= 终点低），或向上笔却没有走高
                （起点低 >= 终点高）时抛出（ErrCode.BI_ERR）

        Note:
            这里只校验"方向 vs 端点高低"这一个维度；
            笔长够不够、和前后笔有无重叠，那些约束由 BiList 负责。
        """
        try:
            if self.is_down():
                assert self.begin_klc.high > self.end_klc.low
            else:
                assert self.begin_klc.low < self.end_klc.high
        except Exception as e:
            raise CChanException(f"{self.idx}:{self.begin_klc[0].time}~{self.end_klc[-1].time}笔的方向和收尾位置不一致!", ErrCode.BI_ERR) from e

    def set(self, begin_klc: CKLine, end_klc: CKLine):
        """
        唯一允许改写端点的入口，内部顺序：写端点 -> 定方向 -> 自检 -> 清缓存。

        Args:
            begin_klc: 新的起点合并K线，必须已经判定出 TOP/BOTTOM 分型
            end_klc: 新的终点合并K线

        Raises:
            CChanException: 起点不是分型（无法确定方向）时抛出（ErrCode.BI_ERR）

        Note:
            四步必须成套出现：漏掉 clean_cache，所有 @make_cache 的派生量
            （_high/amp/力度…）都会留在旧值上。
        """
        self.__begin_klc: CKLine = begin_klc
        self.__end_klc: CKLine = end_klc
        # 方向唯一地由起点分型决定：起点是底分型 -> 这笔向上；是顶分型 -> 这笔向下
        if begin_klc.fx == FX_TYPE.BOTTOM:
            self.__dir = BI_DIR.UP
        elif begin_klc.fx == FX_TYPE.TOP:
            self.__dir = BI_DIR.DOWN
        else:
            raise CChanException("ERROR DIRECTION when creating bi", ErrCode.BI_ERR)
        self.check()
        self.clean_cache()

    @make_cache
    def get_begin_val(self):
        """
        笔起点的价格值。

        Returns:
            float: 向上笔取起点 KLC 的最低价 low；向下笔取起点 KLC 的最高价 high

        Note:
            为什么跟方向有关：笔的起点必须是这一段的极值点 ——
            向上笔是从最低处起步的，所以取 low；向下笔从最高处起步，取 high。
            这样 get_end_val()-get_begin_val() 天然带符号且与方向一致。
        """
        return self.begin_klc.low if self.is_up() else self.begin_klc.high

    @make_cache
    def get_end_val(self):
        """
        笔终点的价格值。

        Returns:
            float: 向上笔取终点 KLC 的最高价 high；向下笔取终点 KLC 的最低价 low

        Note:
            与 get_begin_val() 严格对称：向上笔"低进高出"，向下笔"高进低出"。
        """
        return self.end_klc.high if self.is_up() else self.end_klc.low

    @make_cache
    def get_begin_klu(self) -> CKLine_Unit:
        """
        笔真正的起点原始K线。

        Returns:
            CKLine_Unit: 向上笔取起点 KLC 里打出最低价的那根K线；向下笔取打出最高价的那根

        Note:
            为什么不能直接用 begin_klc.lst[0]：起点 KLC 是若干根K线的合并体，
            极值未必出在第一根（一字K线、长上下影等形态尤其明显），
            必须回到 KLC 内部定位"造出这个极值的那根"（get_peak_klu）。
        """
        if self.is_up():
            return self.begin_klc.get_peak_klu(is_high=False)
        else:
            return self.begin_klc.get_peak_klu(is_high=True)

    @make_cache
    def get_end_klu(self) -> CKLine_Unit:
        """
        笔真正的终点原始K线。

        Returns:
            CKLine_Unit: 向上笔取终点 KLC 里打出最高价的那根K线；向下笔取打出最低价的那根

        Note:
            笔的时间跨度（get_klu_cnt）与力度指标的时间窗口，
            都以这两根 KLU 的 idx 为准，而不是首尾 KLC 的 idx。
        """
        if self.is_up():
            return self.end_klc.get_peak_klu(is_high=True)
        else:
            return self.end_klc.get_peak_klu(is_high=False)

    @make_cache
    def amp(self):
        """
        笔的振幅（绝对价差）。

        Returns:
            float: |get_end_val() - get_begin_val()|，与方向无关、恒非负

        Note:
            这是"多少钱"的口径；要跨价位标的比较，请用百分比口径 Cal_MACD_amp()。
        """
        return abs(self.get_end_val() - self.get_begin_val())

    @make_cache
    def get_klu_cnt(self):
        """
        本笔覆盖了多少根原始K线。

        Returns:
            int: 首尾 KLU 的 idx 之差再 +1（首尾两根都算在内）
        """
        return self.get_end_klu().idx - self.get_begin_klu().idx + 1

    @make_cache
    def get_klc_cnt(self):
        """
        本笔覆盖了多少根合并K线。

        Returns:
            int: 首尾 KLC 的 idx 之差再 +1

        Note:
            开头两条断言确认"首尾两根 KLU 确实落在首尾两根 KLC 上"，
            否则用 KLC 的 idx 相减得到的根数就没有意义。
        """
        assert self.end_klc.idx == self.get_end_klu().klc.idx
        assert self.begin_klc.idx == self.get_begin_klu().klc.idx
        return self.end_klc.idx - self.begin_klc.idx + 1

    @make_cache
    def _high(self):
        """
        笔区间的最高价。

        Returns:
            float: 向上笔 -> end_klc.high（顶在这笔的终点）；
                   向下笔 -> begin_klc.high（顶在这笔的起点）

        Note:
            取哪一端完全取决于方向：一笔的两个端点总是一个最高、一个最低，
            向上笔最低点在前、最高点在后，向下笔正好反过来。
            中枢重叠判定用的就是 _high()/_low()。
        """
        return self.end_klc.high if self.is_up() else self.begin_klc.high

    @make_cache
    def _low(self):
        """
        笔区间的最低价。

        Returns:
            float: 向上笔 -> begin_klc.low（底在这笔的起点）；
                   向下笔 -> end_klc.low（底在这笔的终点）

        Note:
            与 _high() 互为反向，理由同源。
        """
        return self.begin_klc.low if self.is_up() else self.end_klc.low

    @make_cache
    def _mid(self):
        """笔区间的中位价 (_high()+_low())/2，粗略代表这笔的"重心高度"。"""
        return (self._high() + self._low()) / 2  # 笔的中位价

    @make_cache
    def is_down(self):
        """是否是向下笔（dir == BI_DIR.DOWN）。"""
        return self.dir == BI_DIR.DOWN

    @make_cache
    def is_up(self):
        """是否是向上笔（dir == BI_DIR.UP）。"""
        return self.dir == BI_DIR.UP

    def update_virtual_end(self, new_klc: CKLine):
        """
        把尾部改写到 new_klc，同时把这笔降级为虚笔。

        Args:
            new_klc: 新的（尚待确认的）尾部合并K线

        Note:
            顺序很重要：先把"当前的尾部"压进 sure_end 栈再改写，
            BiList.delete_virtual_bi 才有东西可以还原；
            used_to_be_sure 保留降级前的 is_sure，保证"曾经确认过"不被抹掉。
        """
        self.append_sure_end(self.end_klc)
        self.update_new_end(new_klc)
        self.__used_to_be_sure = self.__is_sure
        self.__is_sure = False

    def restore_from_virtual_end(self, sure_end: CKLine):
        """
        把虚笔还原成确认笔：尾部退回曾经被确认过的那个 KLC。

        Args:
            sure_end: 要退回的尾部合并K线（BiList 传的是 sure_end 栈里最早的那个）

        Note:
            这里无条件把 is_sure/used_to_be_sure 都置 True 并清空 sure_end 栈；
            栈里其余的确认尾点，由 BiList 以"再补一根确认笔"的方式加回来。
        """
        self.__is_sure = True
        self.__used_to_be_sure = True
        self.update_new_end(new_klc=sure_end)
        self.__sure_end = []

    def append_sure_end(self, klc: CKLine):
        """
        把一个"曾经成立过"的尾部 KLC 压入 sure_end 栈。

        Args:
            klc: 即将被改写掉、但确实曾经被确认过的尾部合并K线

        Note:
            栈按时间顺序保存，BiList.delete_virtual_bi 取 [0] 做还原，
            其余元素逐个补成新的确认笔。
        """
        self.__sure_end.append(klc)

    def update_new_end(self, new_klc: CKLine):
        """
        单纯把尾部换成新的 KLC，不改变确认状态。

        Args:
            new_klc: 新的尾部合并K线

        Note:
            与 update_virtual_end 的区别：这里既不压 sure_end 栈也不动 is_sure，
            适用于"确认笔的尾巴创了新高/新低"这种本来就该延伸、无需降级的场景。
        """
        self.__end_klc = new_klc
        self.check()
        self.clean_cache()

    def cal_macd_metric(self, macd_algo, is_reverse):
        """
        按配置指定的算法算出本笔的"力度"标量，用于和相邻的反向笔做背驰比较。

        Args:
            macd_algo: MACD_ALGO 枚举，决定用哪种口径：
                AREA   -> Cal_MACD_half（半面积）
                PEAK   -> Cal_MACD_peak（同色柱峰值）
                FULL_AREA -> Cal_MACD_area（面积，只算同色柱）
                DIFF   -> Cal_MACD_diff（最大最小柱之差）
                SLOPE  -> Cal_MACD_slope（斜率）
                AMP    -> Cal_MACD_amp（涨跌幅，与 MACD 无关，只是共用这套分派）
                AMOUNT / VOLUMN -> Cal_MACD_trade_metric（成交额 / 成交量，不取平均）
                VOLUMN_AVG / AMOUNT_AVG / TURNRATE_AVG -> 同上三个字段，取平均
                RSI    -> Cal_Rsi
            is_reverse: 只对面积类（Cal_MACD_half）生效：
                False 从笔头顺着算，True 从笔尾倒着算

        Returns:
            float: 力度值。所有口径都约定"值越大表示这笔越有力"，
                这样买卖点处才能统一用 out_metric/in_metric 做比值比较

        Raises:
            CChanException: macd_algo 不在上述列表中（ErrCode.PARA_ERROR）

        Note:
            向下笔的"力度"已通过取绝对值（峰值/面积）或取倒数（Cal_Rsi）
            统一到"越大越强"的方向上；不同口径之间不可比。
        """
        if macd_algo == MACD_ALGO.AREA:
            return self.Cal_MACD_half(is_reverse)
        elif macd_algo == MACD_ALGO.PEAK:
            return self.Cal_MACD_peak()
        elif macd_algo == MACD_ALGO.FULL_AREA:
            return self.Cal_MACD_area()
        elif macd_algo == MACD_ALGO.DIFF:
            return self.Cal_MACD_diff()
        elif macd_algo == MACD_ALGO.SLOPE:
            return self.Cal_MACD_slope()
        elif macd_algo == MACD_ALGO.AMP:
            return self.Cal_MACD_amp()
        elif macd_algo == MACD_ALGO.AMOUNT:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_TURNOVER, cal_avg=False)
        elif macd_algo == MACD_ALGO.VOLUMN:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_VOLUME, cal_avg=False)
        elif macd_algo == MACD_ALGO.VOLUMN_AVG:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_VOLUME, cal_avg=True)
        elif macd_algo == MACD_ALGO.AMOUNT_AVG:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_TURNOVER, cal_avg=True)
        elif macd_algo == MACD_ALGO.TURNRATE_AVG:
            return self.Cal_MACD_trade_metric(DATA_FIELD.FIELD_TURNRATE, cal_avg=True)
        elif macd_algo == MACD_ALGO.RSI:
            return self.Cal_Rsi()
        else:
            raise CChanException(f"unsupport macd_algo={macd_algo}, should be one of area/full_area/peak/diff/slope/amp", ErrCode.PARA_ERROR)

    @make_cache
    def Cal_Rsi(self):
        """
        笔区间的 RSI 极值力度。

        Returns:
            float: 向下笔 -> 10000/(区间最小 RSI)；向上笔 -> 区间最大 RSI

        Note:
            两个方向都统一到"值越大表示这笔越有力"：
            向上笔的力量体现在 RSI 打得高（取 max），
            向下笔力量体现在 RSI 压得低，于是用 10000/min 把"越小"翻转成"越大"。
            +1e-7 是为了避免 min 为 0 时除零。
        """
        rsi_lst: List[float] = []
        for klc in self.klc_lst:
            rsi_lst.extend(klu.rsi for klu in klc.lst)
        return 10000.0/(min(rsi_lst)+1e-7) if self.is_down() else max(rsi_lst)

    @make_cache
    def Cal_MACD_area(self):
        """
        笔区间内 MACD 同色柱的面积之和（对应 MACD_ALGO.FULL_AREA）。

        Returns:
            float: 向下笔只累加负柱、向上笔只累加正柱的 |macd| 之和（再垫一个 1e-7 的底）

        Note:
            - 只看同色柱，是为了只统计"顺着这笔方向"的那部分动能，反向柱子不计入；
            - 1e-7 是刻意的底：下游要把两个面积相除比较背驰，垫个正值可避免除零。
        """
        _s = 1e-7
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        for klc in self.klc_lst:
            for klu in klc.lst:
                # 首尾两根 KLC 里可能含有落在笔之外的原始K线，这里按 KLU 的 idx 裁掉
                if klu.idx < begin_klu.idx or klu.idx > end_klu.idx:
                    continue
                # 只累加与笔方向同色的柱：向下笔看绿柱(<0)，向上笔看红柱(>0)
                if (self.is_down() and klu.macd.macd < 0) or (self.is_up() and klu.macd.macd > 0):
                    _s += abs(klu.macd.macd)
        return _s

    @make_cache
    def Cal_MACD_peak(self):
        """
        笔区间内 MACD 同色柱的峰值（绝对值）。

        Returns:
            float: |macd| 的最大值（初值 1e-7，即"极小正值"）；向下笔只认负柱、向上笔只认正柱

        Note:
            与 Cal_MACD_area 是同一口径的两种取法：一个求面积、一个求峰值，
            峰值更敏感，面积更能反映持续时间。
        """
        peak = 1e-7
        for klc in self.klc_lst:
            for klu in klc.lst:
                if abs(klu.macd.macd) > peak:
                    if self.is_down() and klu.macd.macd < 0:
                        peak = abs(klu.macd.macd)
                    elif self.is_up() and klu.macd.macd > 0:
                        peak = abs(klu.macd.macd)
        return peak

    def Cal_MACD_half(self, is_reverse):
        """
        MACD 面积类的分派入口（对应 MACD_ALGO.AREA）。

        Args:
            is_reverse: False -> Cal_MACD_half_obverse，从笔头顺着算；
                        True  -> Cal_MACD_half_reverse，从笔尾倒着算

        Returns:
            float: 对应方向上的"半个面积"

        Note:
            背驰比较时要区分正反：进入中枢的那一笔用正序算，
            离开中枢的那一笔要用倒序算，两者都取"靠近中枢的那一端"作为累加起点才可比。
        """
        if is_reverse:
            return self.Cal_MACD_half_reverse()
        else:
            return self.Cal_MACD_half_obverse()

    @make_cache
    def Cal_MACD_half_obverse(self):
        """
        从笔头开始顺着累加 MACD 面积，直到柱子变色为止（所谓的"半面积"）。

        Returns:
            float: 从起点 KLU 起、与起点 macd 同色的那一段柱子面积之和

        Note:
            "半"的含义是只统计穿越行情中同色的前一半：
            一旦遇到与起点符号相反的柱子就立刻收手，结果只反映这一笔前半程的动能。
        """
        _s = 1e-7
        begin_klu = self.get_begin_klu()
        peak_macd = begin_klu.macd.macd
        # 下面这对 for-else 的语义：内层若把某一根 KLC 内的K线都扫完也没 break，
        # 就走 else -> continue 继续看下一根 KLC；一旦在内层 break（遇到变色），
        # 外层的 break 立刻收工
        # 起点 KLC 里、位于笔真正起点之前的原始K线要跳过
        for klc in self.klc_lst:
            for klu in klc.lst:
                if klu.idx < begin_klu.idx:
                    continue
                if klu.macd.macd*peak_macd > 0:
                    _s += abs(klu.macd.macd)
                else:
                    break
            else:  # 没有被break，继续找写一个KLC
                continue
            break
        return _s

    @make_cache
    def Cal_MACD_half_reverse(self):
        """
        倒序版的半面积：从笔尾往前累加，遇到与尾端柱子异色的柱就停。

        Returns:
            float: 从终点 KLU 倒着算回来的那一段同色柱面积

        Note:
            与 Cal_MACD_half_obverse 完全对称（klc_lst_re + 倒序扫 KLU），
            区别只在于基准 macd 取的是尾端那一根。
        """
        _s = 1e-7
        begin_klu = self.get_end_klu()
        peak_macd = begin_klu.macd.macd
        # 与 obverse 同款的 for-else；这里从后往前扫，先跳过笔真正终点之后的原始K线
        for klc in self.klc_lst_re:
            for klu in klc[::-1]:
                if klu.idx > begin_klu.idx:
                    continue
                if klu.macd.macd*peak_macd > 0:
                    _s += abs(klu.macd.macd)
                else:
                    break
            else:  # 没有被break，继续找写一个KLC
                continue
            break
        return _s

    @make_cache
    def Cal_MACD_diff(self):
        """
        macd红绿柱最大值最小值之差
        """
        _max, _min = float("-inf"), float("inf")
        for klc in self.klc_lst:
            for klu in klc.lst:
                macd = klu.macd.macd
                if macd > _max:
                    _max = macd
                if macd < _min:
                    _min = macd
        return _max-_min

    @make_cache
    def Cal_MACD_slope(self):
        """
        笔的斜率：单位K线上的相对涨幅（注意与 MACD 无关）。

        Returns:
            float: 向上笔 -> (终点高-起点低)/终点高/K线根数；
                   向下笔 -> (起点高-终点低)/起点高/K线根数

        Note:
            分母刻意取"另外一个端点"（向上笔除以终点高、向下笔除以起点高），
            与 Cal_MACD_amp 除以"出发点"的口径不同，两者不能直接换算；
            除以 K线根数 是为了让长短笔之间可比。
        """
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        if self.is_up():
            return (end_klu.high - begin_klu.low)/end_klu.high/(end_klu.idx - begin_klu.idx + 1)
        else:
            return (begin_klu.high - end_klu.low)/begin_klu.high/(end_klu.idx - begin_klu.idx + 1)

    @make_cache
    def Cal_MACD_amp(self):
        """
        笔的涨跌幅（百分比口径）。

        Returns:
            float: 向下笔 -> (起点高-终点低)/起点高；向上笔 -> (终点高-起点低)/起点低

        Note:
            两个分支的分母都是"这一笔的出发点"（向下笔从高点出发就除以高点，
            向上笔从低点出发就除以低点），结果统一为非负比例值，跨价位标的才可比。
        """
        begin_klu = self.get_begin_klu()
        end_klu = self.get_end_klu()
        if self.is_down():
            return (begin_klu.high-end_klu.low)/begin_klu.high
        else:
            return (end_klu.high-begin_klu.low)/begin_klu.low

    def Cal_MACD_trade_metric(self, metric: str, cal_avg=False) -> float:
        """
        量能类力度：成交额 / 成交量 / 换手率在笔区间内的合计或均值。

        Args:
            metric: DATA_FIELD 里的量能字段名（FIELD_VOLUME/FIELD_TURNOVER/FIELD_TURNRATE）
            cal_avg: True 返回按K线根数的平均值，False 返回总和

        Returns:
            float: 合计值或均值；只要区间内有任意一根K线缺这个字段，就直接返回 0.0

        Note:
            缺字段时直接整笔返回 0（而不是跳过那一根），
            是为了让不同数据源下"有没有量能"这件事保持一致。
        """
        _s = 0
        for klc in self.klc_lst:
            for klu in klc.lst:
                metric_res = klu.trade_info.metric[metric]
                if metric_res is None:
                    return 0.0
                _s += metric_res
        # 取平均时除以的是笔的原始K线根数，这样长短笔之间才可比
        return _s / self.get_klu_cnt() if cal_avg else _s

    # def set_klc_lst(self, lst):
    #     self.__klc_lst = lst
