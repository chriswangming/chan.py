"""
笔列表（BiList）模块：缠论链路中"哪些K线能成笔、旧笔要不要回收"的判定者。

链路位置：
    原始K线 -> 包含合并(CKLine) -> 分型 -> 【Bi/Bi.py 单根笔】 + 【BiList 成笔规则】
    -> 线段(Seg) -> 中枢(ZS) -> 买卖点

上游入口是 KLine/KLine_List.py：每当最新那根 KLC 拿到右邻居、可以判分型时，
它就调本模块的 update_bi(倒数第二根KLC, 倒数第一根KLC, 是否算虚笔) 推进一笔。

本模块负责四件事：
    1. 成笔判定：跨度够不够（satisfy_bi_span）、分型配不配（check_fx_valid）、
       端点是不是区间极值（end_is_peak），三关都过才成笔；
    2. 终点延伸：最后一笔的尾部创了新高/新低时，把尾巴挪过去（try_update_end），
       必要时代替掉原来的笔（update_peak，即"次高点/次低点"处理）；
    3. 虚笔的增删：尾部尚未确认的笔可以被创建（try_add_virtual_bi）与删除
       （delete_virtual_bi），删除时要把以前确认过的尾部还原回去；
    4. 对外提供笔序列容器（__iter__/__getitem__/__len__）。

Note:
    笔的 idx 由 add_new_bi 用 len(bi_list) 赋给新笔，而删除操作只发生在列表尾部，
    所以"笔的 idx"与"它在 bi_list 里的下标"在正常运行中是相等的 ——
    Seg/ZS 里大量形如 bi_lst[bi.idx] 的写法都依赖这个性质。
"""
from typing import List, Optional, Union, overload

from Common.CEnum import FX_TYPE, KLINE_DIR
from KLine.KLine import CKLine

from .Bi import CBi
from .BiConfig import CBiConfig


class CBiList:
    """
    一个级别的笔序列容器 + 成笔状态机。

    持有：
        bi_list        ：笔序列（尾部可能是虚笔）
        last_end       ：最后一笔的尾部 KLC，成笔判定的锚点
        free_klc_lst   ：第一笔成型之前，暂时攒着的分型 KLC
        config         ：CBiConfig，决定成笔的三条判定规则

    Note:
        bi_list 里始终只保留"时间上有序、方向交替"的笔；
        尾部那根可能只是虚笔（is_sure=False），它会随后续K线被不断改写或删除。
    """

    def __init__(self, bi_conf=CBiConfig()):
        """
        Args:
            bi_conf: 笔的配置 CBiConfig（成笔跨度、分型检查强度、是否把缺口当K线等）
        """
        self.bi_list: List[CBi] = []
        self.last_end = None  # 最后一笔的尾部
        self.config = bi_conf

        # 第一笔还没画出来时，先把出现过的分型 KLC 攒在这里，攒够条件再一次性开第一笔
        # （只影响结果精度，不影响后续流程）
        self.free_klc_lst = []  # 仅仅用作第一笔未画出来之前的缓存，为了获得更精准的结果而已，不加这块逻辑其实对后续计算没太大影响

    def __str__(self):
        """调试输出：每根笔一行。"""
        return "\n".join([str(bi) for bi in self.bi_list])

    def __iter__(self):
        """按时间顺序迭代每一根笔。"""
        yield from self.bi_list

    # 下面三个 __getitem__ 组成一组重载：带 @overload 的两个只在类型检查阶段生效，
    # 运行时会被最后那个带实现的版本覆盖
    @overload
    def __getitem__(self, index: int) -> CBi: ...

    @overload
    def __getitem__(self, index: slice) -> List[CBi]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CBi], CBi]:
        """
        按下标/切片取笔。

        Args:
            index: int 或 slice

        Returns:
            CBi 或 List[CBi]
        """
        return self.bi_list[index]

    def __len__(self):
        """Returns: int: 笔的根数（含尾部虚笔）。"""
        return len(self.bi_list)

    def try_create_first_bi(self, klc: CKLine) -> bool:
        """
        尝试画出整个序列的第一笔。

        Args:
            klc: 刚拿到右邻居、可以判分型的那根 KLC

        Returns:
            bool: 是否成功画出了第一笔

        Note:
            第一笔没有"上一笔的尾部"作锚，所以要把此前攒下的分型 KLC（free_klc_lst）
            逐个拿来跟 klc 配对，找到第一个满足成笔条件的就成笔。
            注意无论成没成笔，klc 都会成为新的 last_end（没成笔时它自己进 free_klc_lst）。
        """
        for exist_free_klc in self.free_klc_lst:
            # 同向的两个分型不可能连成一笔，直接跳过
            if exist_free_klc.fx == klc.fx:
                continue
            if self.can_make_bi(klc, exist_free_klc):
                self.add_new_bi(exist_free_klc, klc)
                self.last_end = klc
                return True
        self.free_klc_lst.append(klc)
        self.last_end = klc
        return False

    def update_bi(self, klc: CKLine, last_klc: CKLine, cal_virtual: bool) -> bool:
        """
        推进笔的主入口：先更新确认笔，再按需追加虚笔。

        Args:
            klc: 倒数第二根 KLC（它刚拿到右邻居，分型才被判定出来）
            last_klc: 倒数第一根（最新）KLC，虚笔的终点候选
            cal_virtual: 是否要算虚笔（只有回放模式才为 True）

        Returns:
            bool: 笔的形态是否发生了变化（有新笔或虚笔变了都算）

        Note:
            为什么用倒数第二根而不是最新那根：判定分型需要右邻居，
            最新那根的分型还没定，且 check_fx_valid 的 STRICT 口径要读它的下一根，
            这些都要求"当前处理的 KLC 的右边还有东西"。
        """
        # klc: 倒数第二根klc
        # last_klc: 倒数第1根klc
        flag1 = self.update_bi_sure(klc)
        if cal_virtual:
            flag2 = self.try_add_virtual_bi(last_klc)
            return flag1 or flag2
        else:
            return flag1

    def can_update_peak(self, klc: CKLine):
        """
        判断能否把"最后一笔"整个撤掉、把倒数第二笔延长到 klc（次高点/次低点处理）。

        Args:
            klc: 新出现的极值 KLC 候选

        Returns:
            bool: True 表示可以做这次"撤笔 + 延长"

        Note:
            适用情形：最后一笔只是小幅折返，而后面的走势又在倒数第二笔的方向上创了新极值，
            这时把最后一笔并入倒数第二笔更符合走势本意。
            四道关卡依次是：
                1) 配置了 bi_allow_sub_peak（为 True 时直接返回 False 不做这件事）或笔数不足 2；
                2) 新极值必须比最后一笔的起点更极端；
                3) klc 必须是从倒数第二笔起点到它本身的区间极值；
                4) 最后一笔没有破坏倒数第二笔的起点。
        """
        if self.config.bi_allow_sub_peak or len(self.bi_list) < 2:
            return False
        # 新极值必须比最后一笔的起点更极端：向下笔时要比它的高点还高，向上笔时要比它的低点还低
        if self.bi_list[-1].is_down() and klc.high < self.bi_list[-1].get_begin_val():
            return False
        if self.bi_list[-1].is_up() and klc.low > self.bi_list[-1].get_begin_val():
            return False
        # 从倒数第二笔的起点一路扫到 klc，klc 必须是这段里的真极值
        if not end_is_peak(self.bi_list[-2].begin_klc, klc):
            return False
        # 最后一笔不能跌破/涨破倒数第二笔的起点，否则这两笔的关系就不是「小幅折返」了
        if self[-1].is_down() and self[-1].get_end_val() < self[-2].get_begin_val():
            return False
        if self[-1].is_up() and self[-1].get_end_val() > self[-2].get_begin_val():
            return False
        return True

    def update_peak(self, klc: CKLine, for_virtual=False):
        """
        执行"撤掉最后一笔 + 把倒数第二笔延长到 klc"。

        Args:
            klc: 新的极值 KLC
            for_virtual: True 表示这次延长发生在虚笔上

        Returns:
            bool: 是否执行成功

        Note:
            先弹出最后一笔再尝试延长，失败了要原样塞回去（顺序不能变）；
            虚笔情形下被弹掉那笔的尾部要记进新笔的 sure_end 栈，便于日后还原。
        """
        if not self.can_update_peak(klc):
            return False
        _tmp_last_bi = self.bi_list[-1]
        self.bi_list.pop()
        if not self.try_update_end(klc, for_virtual=for_virtual):
            self.bi_list.append(_tmp_last_bi)
            return False
        else:
            if for_virtual:
                self.bi_list[-1].append_sure_end(_tmp_last_bi.end_klc)
            return True

    def update_bi_sure(self, klc: CKLine) -> bool:
        """
        更新确认笔：先清掉尾部虚笔，再按 klc 的分型决定是新开一笔还是延伸最后一笔。

        Args:
            klc: 倒数第二根 KLC（刚被判定出分型的那根）

        Returns:
            bool: 笔的形态是否发生变化

        Note:
            返回值是拿"删虚笔前后的最后一笔终点 KLU"比对出来的：
            即使没有新笔，虚笔被删掉也算"变了"，上游据此决定是否要重算线段/中枢。
        """
        # klc: 倒数第二根klc
        _tmp_end = self.get_last_klu_of_last_bi()
        self.delete_virtual_bi()
        # 返回值：是否出现新笔
        # 不是分型的 KLC 不可能成笔/延伸，只看删虚笔这个动作有没有改变现状
        if klc.fx == FX_TYPE.UNKNOWN:
            return _tmp_end != self.get_last_klu_of_last_bi()  # 虚笔是否有变
        # 还没有任何笔：只能走第一笔的创建流程
        if self.last_end is None or len(self.bi_list) == 0:
            return self.try_create_first_bi(klc)
        # 同向分型 -> 不是新笔，只是最后一笔的尾巴可能要延长
        if klc.fx == self.last_end.fx:
            return self.try_update_end(klc)
        # 反向分型且满足成笔三关 -> 新开一笔
        elif self.can_make_bi(klc, self.last_end):
            self.add_new_bi(self.last_end, klc)
            self.last_end = klc
            return True
        # 成不了笔，就试试「撤掉最后一笔改延长上一笔」这条路
        elif self.update_peak(klc):
            return True

        # 走到这里说明什么都没做，仍要如实汇报「删虚笔」带来的变化
        return _tmp_end != self.get_last_klu_of_last_bi()

    def delete_virtual_bi(self):
        """
        删除尾部的虚笔，并把虚笔此前确认过的尾部逐一还原成确认笔。

        Note:
            虚笔的 sure_end 栈里存着"每次被改写前的尾部"，所以还原的动作是：
                1) 先把自己退回到最早那个确认尾部 [0]（同时恢复 is_sure=True）；
                2) 其余的确认尾部 [1:] 逐个作为新的确认笔补回来。
            若一个确认尾部都没有，说明这笔自始至终都是虚的，直接整根删掉。
            最后还要把 last_end 回退、并断开新尾部的 next 指针（防止指向已删的笔）。
        """
        if len(self) > 0 and not self.bi_list[-1].is_sure:
            sure_end_list = [klc for klc in self.bi_list[-1].sure_end]
            if len(sure_end_list):
                self.bi_list[-1].restore_from_virtual_end(sure_end_list[0])
                self.last_end = self[-1].end_klc
                # 剩下的确认尾部，每个都单独成一根新的确认笔
                for sure_end in sure_end_list[1:]:
                    self.add_new_bi(self.last_end, sure_end, is_sure=True)
                    self.last_end = self[-1].end_klc
            else:
                del self.bi_list[-1]
        self.last_end = self[-1].end_klc if len(self) > 0 else None
        # 断掉指向被删虚笔的后继指针，避免遍历时走到已删除的笔上
        if len(self) > 0:
            self[-1].next = None

    def try_add_virtual_bi(self, klc: CKLine, need_del_end=False):
        """
        尝试把笔延伸到最新走势上，形成（或更新）尾部虚笔。

        Args:
            klc: 最新的 KLC（虚笔终点的候选）
            need_del_end: True 时会先删掉现有虚笔再重来

        Returns:
            bool: 虚笔是否发生了变化

        Note:
            need_del_end 用于"最新 KLC 因为被合并而就地变高/变低"的场景（参见 issue#175）：
            此时上一轮算出来的虚笔终点是拿这根 KLC 的旧区间算的，已经失真，
            必须先删掉旧虚笔、用新区间重新推一遍，否则虚笔会停在错误的终点上。
        """
        if need_del_end:
            self.delete_virtual_bi()
        if len(self) == 0:
            return False
        # 最新 KLC 已经就是最后一笔的尾部，没有可推进的东西
        if klc.idx == self[-1].end_klc.idx:
            return False
        # 最新 KLC 在最后一笔的方向上创新高/新低：直接把尾巴挪过去
        if (self[-1].is_up() and klc.high >= self[-1].end_klc.high) or (self[-1].is_down() and klc.low <= self[-1].end_klc.low):
            # 更新最后一笔
            self.bi_list[-1].update_virtual_end(klc)
            return True
        # 否则从最新 KLC 往回找：第一个能和当前尾部凑成新笔（或能触发次高点处理）的位置
        _tmp_klc = klc
        while _tmp_klc and _tmp_klc.idx > self[-1].end_klc.idx:
            assert _tmp_klc is not None
            # 注意这里锚点用的是最后一笔的尾部 KLC，而不是 last_end
            if self.can_make_bi(_tmp_klc, self[-1].end_klc, for_virtual=True):
                # 新增一笔
                self.add_new_bi(self.last_end, _tmp_klc, is_sure=False)
                return True
            elif self.update_peak(_tmp_klc, for_virtual=True):
                return True
            _tmp_klc = _tmp_klc.pre
        return False

    def add_new_bi(self, pre_klc, cur_klc, is_sure=True):
        """
        追加一根新笔并串好链表。

        Args:
            pre_klc: 新笔的起点 KLC
            cur_klc: 新笔的终点 KLC
            is_sure: True 为确认笔，False 为虚笔

        Note:
            笔的 idx 用的是"追加前的列表长度"，即它最终在 bi_list 里的下标；
            由于所有删除都只发生在尾部，idx 与下标始终保持一致，
            Seg/ZS 里 bi_lst[bi.idx] 这种写法才能成立。
        """
        self.bi_list.append(CBi(pre_klc, cur_klc, idx=len(self.bi_list), is_sure=is_sure))
        if len(self.bi_list) >= 2:
            self.bi_list[-2].next = self.bi_list[-1]
            self.bi_list[-1].pre = self.bi_list[-2]

    def satisfy_bi_span(self, klc: CKLine, last_end: CKLine):
        """
        判断两个分型之间的跨度够不够成笔。

        Args:
            klc: 候选的终点分型 KLC
            last_end: 起点分型 KLC（上一笔的尾部）

        Returns:
            bool: 跨度是否达标

        Note:
            严格模式（is_strict）只要求"相隔至少 4 根 KLC"；
            非严格模式要求"至少 3 根 KLC，且中间夹着至少 3 根原始K线"，
            后者是为了让"笔看起来确实走完了一段"，而不是三根K线就凑一笔。
        """
        bi_span = self.get_klc_span(klc, last_end)
        # 严格模式：只看合并K线的间隔，>=4 就够（也就是至少相隔 4 根 KLC）
        if self.config.is_strict:
            return bi_span >= 4
        uint_kl_cnt = 0
        tmp_klc = last_end.next
        while tmp_klc:
            uint_kl_cnt += len(tmp_klc.lst)
            # 已经扫到最新那根 KLC 了还没凑够原始K线数，说明这段还没走完 -> 不成立
            if not tmp_klc.next:  # 最后尾部虚笔的时候，可能klc.idx == last_end.idx+1
                return False
            if tmp_klc.next.idx < klc.idx:
                tmp_klc = tmp_klc.next
            else:
                break
        return bi_span >= 3 and uint_kl_cnt >= 3

    def get_klc_span(self, klc: CKLine, last_end: CKLine) -> int:
        """
        计算两个 KLC 之间的跨度，配置允许时把跳空缺口也计作K线。

        Args:
            klc: 终点 KLC
            last_end: 起点 KLC

        Returns:
            int: 跨度（KLC 数量，+ 中间每处跳空各记 1）

        Note:
            配 gap_as_kl 时，跳空被当作"少掉的那几根K线也算进去了"，
            所以跳空多的走势更容易够着成笔的跨度门槛。
            跨度已经 >=4 时直接返回不再细算：成笔只要求 >=4，再精确也没用，算这个是纯优化。
        """
        span = klc.idx - last_end.idx
        if not self.config.gap_as_kl:
            return span
        # 成笔最多只要求跨度 4，超过 4 之后精确值没有意义，直接短路省掉逐根扫缺口的开销
        if span >= 4:  # 加速运算，如果span需要真正精确的值，需要去掉这一行
            return span
        tmp_klc = last_end
        while tmp_klc and tmp_klc.idx < klc.idx:
            if tmp_klc.has_gap_with_next():
                span += 1
            tmp_klc = tmp_klc.next
        return span

    def can_make_bi(self, klc: CKLine, last_end: CKLine, for_virtual: bool = False):
        """
        成笔判定：三关全过才成笔。

        Args:
            klc: 候选终点分型 KLC
            last_end: 起点分型 KLC
            for_virtual: True 表示在试探虚笔（会放宽部分要求）

        Returns:
            bool: 这两点是否可以连成一笔

        Note:
            三关分别是：
                1) 跨度关 satisfy_bi_span：相隔的K线够不够多（bi_algo='fx' 时直接跳过这一关）；
                2) 分型关 last_end.check_fx_valid：两个分型间的区间关系是否达标（见 KLine）；
                3) 极值关 end_is_peak：终点必须是这段区间里真正的极值。
        """
        satisify_span = True if self.config.bi_algo == 'fx' else self.satisfy_bi_span(klc, last_end)
        if not satisify_span:
            return False
        if not last_end.check_fx_valid(klc, self.config.bi_fx_check, for_virtual):
            return False
        if self.config.bi_end_is_peak and not end_is_peak(last_end, klc):
            return False
        return True

    def try_update_end(self, klc: CKLine, for_virtual=False) -> bool:
        """
        尝试把最后一笔的尾巴延伸到 klc（最后一笔在新方向上创了新高/新低时）。

        Args:
            klc: 新的极值 KLC 候选
            for_virtual: True 表示这是虚笔的延伸（用 KLC 的方向判定，
                         False 则要求它已经是真正的顶/底分型）

        Returns:
            bool: 是否延伸成功

        Note:
            两个版本的判定口径不同：确认笔要求 klc 已经是正式分型（fx == TOP/BOTTOM），
            虚笔只要求它当前是朝着那个方向走的（dir == UP/DOWN），
            因为虚笔本来就是要提前把还没定型的走势画出来。
        """
        def check_top(klc: CKLine, for_virtual):
            """判定 klc 够不够格当「顶」：虚笔看方向 dir，确认笔看是否已是顶分型。"""
            if for_virtual:
                return klc.dir == KLINE_DIR.UP
            else:
                return klc.fx == FX_TYPE.TOP

        def check_bottom(klc: CKLine, for_virtual):
            """判定 klc 够不够格当「底」：虚笔看方向 dir，确认笔看是否已是底分型。"""
            if for_virtual:
                return klc.dir == KLINE_DIR.DOWN
            else:
                return klc.fx == FX_TYPE.BOTTOM

        if len(self.bi_list) == 0:
            return False
        last_bi = self.bi_list[-1]
        # 只有「方向对 + 确实在最后一笔的方向上创了新高/新低」才延伸
        if (last_bi.is_up() and check_top(klc, for_virtual) and klc.high >= last_bi.get_end_val()) or \
           (last_bi.is_down() and check_bottom(klc, for_virtual) and klc.low <= last_bi.get_end_val()):
            last_bi.update_virtual_end(klc) if for_virtual else last_bi.update_new_end(klc)
            self.last_end = klc
            return True
        else:
            return False

    def get_last_klu_of_last_bi(self) -> Optional[int]:
        """
        取最后一笔终点的原始K线 idx，用于比对"删虚笔前后状态是否变化"。

        Returns:
            Optional[int]: 最后一笔终点 KLU 的 idx；没有笔时返回 None
        """
        return self.bi_list[-1].get_end_klu().idx if len(self) > 0 else None


def end_is_peak(last_end: CKLine, cur_end: CKLine) -> bool:
    """
    判断从 last_end 到 cur_end 这一段里，cur_end 是不是真正的区间极值。

    Args:
        last_end: 起点分型 KLC
        cur_end: 候选的终点 KLC

    Returns:
        bool: True 表示 cur_end 确实是这段区间的最高/最低点

    Note:
        起点是底分型时，要求中间没有任何一根 KLC 的高点高过 cur_end 的高点；
        起点是顶分型时则要求没有更低点。否则说明后面还藏着更极端的点，
        把笔画到 cur_end 上就不合适了。
        起点既不是顶也不是底分型时（比如虚笔的情形）直接返回 True。
    """
    if last_end.fx == FX_TYPE.BOTTOM:
        cmp_thred = cur_end.high  # 或者严格点选择get_klu_max_high()
        klc = last_end.get_next()
        while True:
            if klc.idx >= cur_end.idx:
                return True
            if klc.high > cmp_thred:
                return False
            klc = klc.get_next()
    # 起点是顶分型 -> 要找的是最低点，对称处理
    elif last_end.fx == FX_TYPE.TOP:
        cmp_thred = cur_end.low  # 或者严格点选择get_klu_min_low()
        klc = last_end.get_next()
        while True:
            if klc.idx >= cur_end.idx:
                return True
            if klc.low < cmp_thred:
                return False
            klc = klc.get_next()

    # 起点没有有效分型（比如虚笔场景），无从判断，放行
    return True
