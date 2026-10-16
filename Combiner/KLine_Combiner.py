"""
K线包含处理（缠论最基础的一步）。

本类是整个缠论算法的公共底座，被两个场景复用（泛型参数 T 不同）：
    - `KLine/KLine.py` 的 CKLine(CKLine_Combiner[CKLine_Unit])：原始K线按包含关系合并成"合并K线(KLC)"；
    - `Seg/Eigen.py` 的 CEigen(CKLine_Combiner[CBi])：笔再按包含关系合并成"特征序列元素"。

核心概念：
    1. 「包含关系」：相邻两个元素，一个的 [low, high] 完全罩住另一个，这时必须合并成一根，
       合并方向不再是重新判断出来的，而是**沿用当前 KLC 已有的方向 dir**：
            dir = UP   → 取「高高」：high = max(high1, high2)，low = max(low1, low2)
            dir = DOWN → 取「低低」：high = min(high1, high2)，low = min(low1, low2)
       方向本身在**新元素加入的那一刻**由 `test_combine` 的返回值决定（见 try_add 注释）。
    2. 「分型」：连续三根 KLC 中间那根的最高价最高（顶分型）/最低价最低（底分型）。
       中间那根的分型必须等它**右侧的邻居出现后**才能判定，所以是 `update_fx` 在前一根 KLC 上被调用。

一句话记住数据流：`test_combine` 判断"能不能合" → `try_add` 决定"合还是新开一根，新的方向是什么"
→ 新 KLC 出现后回头给前一根 `update_fx` 定分型。
"""

from typing import Generic, Iterable, List, Optional, Self, TypeVar, Union, overload

from Common.cache import make_cache
from Common.CEnum import FX_TYPE, KLINE_DIR
from Common.ChanException import CChanException, ErrCode
from KLine.KLine_Unit import CKLine_Unit

from .Combine_Item import CCombine_Item

T = TypeVar('T')


class CKLine_Combiner(Generic[T]):
    """
    一根"合并K线"的通用实现，T 是本级别被合并的元素类型（CKLine_Unit 或 CBi）。

    对外暴露只读属性 time_begin/time_end/high/low/lst/dir/fx，
    内部状态全部私有（`__high` 等名字被 Python 改写成 `_CKLine_Combiner__high`）。
    """

    def __init__(self, kl_unit: T, _dir):
        """
        用第一个元素开出一根新的合并K线。

        Args:
            kl_unit: 第一个被合并的元素（CKLine_Unit 或 CBi）
            _dir: 这根合并K线的调整方向，KLINE_DIR.UP / DOWN
                  （由上一根 KLC 的 try_add 返回值给出，第一根则显式指定）

        Note:
            这里只做了「赋值」，没有设置 kl_unit 指向本 KLC 的反向指针；
            反向指针由 CKLine.__init__ 或后续 try_add 负责写回，
            原因见 KLine/KLine_Unit.py 的 set_klc 说明。
        """
        item = CCombine_Item(kl_unit)   # 统一成 时间+高低点 视图（兼容 KLU/CBi/CSeg）
        self.__time_begin = item.time_begin
        self.__time_end = item.time_end
        self.__high = item.high
        self.__low = item.low

        self.__lst: List[T] = [kl_unit]  # 本级别每一根单位K线

        self.__dir = _dir
        self.__fx = FX_TYPE.UNKNOWN       # 分型，要等右侧邻居出现后才由 update_fx 赋值
        self.__pre: Optional[Self] = None
        self.__next: Optional[Self] = None

    def clean_cache(self):
        """清空 @make_cache 的缓存 —— 任何会改动本对象状态的路径都必须调它。"""
        self._memoize_cache = {}

    # 以下属性都是只读的：外部想改状态必须走 try_add/update_fx/set_* 这套方法
    # 合并K线的起始时间（第一根元素的起点）
    @property
    def time_begin(self): return self.__time_begin

    # 合并K线的结束时间（被并入的最后一根元素的终点）
    @property
    def time_end(self): return self.__time_end

    # 合并区间的高点（不是简单取 max，取决于合并方向 dir，见模块说明）
    @property
    def high(self): return self.__high

    # 合并区间的低点（同上）
    @property
    def low(self): return self.__low

    # 被合并进来的元素列表，lst[0] 是第一根、lst[-1] 是最后一根
    @property
    def lst(self): return self.__lst

    # 本合并K线的调整方向（UP/DOWN），决定后续包含合并时取「高高」还是「低低」
    @property
    def dir(self): return self.__dir

    # 分型：FX_TYPE.TOP / BOTTOM / UNKNOWN，由 update_fx 判定
    @property
    def fx(self): return self.__fx

    # 前一根合并K线（第一根没有前驱，取用会断言失败）
    @property
    def pre(self) -> Self:
        assert self.__pre is not None
        return self.__pre

    # 后一根合并K线，还没有右侧邻居时为 None（所以不能断言非空）
    @property
    def next(self): return self.__next

    def get_next(self) -> Self:
        """取后一根合并K线，并断言它确实存在。"""
        assert self.next is not None
        return self.next

    def test_combine(self, item: CCombine_Item, exclude_included=False, allow_top_equal=None):
        """
        判断「新元素 item 与当前 KLC」的位置关系，也就是能不能合并。

        Args:
            item: 待判定的新元素（已适配成 CCombine_Item）
            exclude_included: 被完全包含时是否单独返还 INCLUDED 表示"不合并"
                              （特征序列 CEigen 用它来保留被包含的笔）
            allow_top_equal: 顶/底相等时是否仍视为不合并：
                              1  = 顶部相等（且底部更高）时返回 DOWN，不合并
                              -1 = 底部相等（且顶部更高）时返回 UP，不合并
                              None = 普通模式（相等就按包含处理）

        Returns:
            KLINE_DIR: COMBINE 需要合并；UP/DOWN 不需要合并（并决定了新 KLC 的方向）；
                       INCLUDED 仅在 exclude_included=True 且被完全包含时返回

        Raises:
            CChanException: 两个区间不满足以上任何关系（ErrCode.COMBINER_ERR）

        Note:
            判定顺序有讲究：先判「互相包含」，再判「整体上移/下移」，
            所以写的是独立的 if 而不是 if/elif 链。
        """
        if (self.high >= item.high and self.low <= item.low):
            # 当前 KLC 罩住了新元素 → 包含，需要合并
            return KLINE_DIR.COMBINE
        if (self.high <= item.high and self.low >= item.low):
            # 反过来：新元素罩住当前 KLC（"被包含"）
            if allow_top_equal == 1 and self.high == item.high and self.low > item.low:
                # 顶相等但底更高：不合并，改用新元素开一根向下的 KLC
                return KLINE_DIR.DOWN
            elif allow_top_equal == -1 and self.low == item.low and self.high < item.high:
                # 底相等但顶更高：不合并，改用新元素开一根向上的 KLC
                return KLINE_DIR.UP
            return KLINE_DIR.INCLUDED if exclude_included else KLINE_DIR.COMBINE
        if (self.high > item.high and self.low > item.low):
            # 高低点整体下移：无包含关系，方向向下
            return KLINE_DIR.DOWN
        if (self.high < item.high and self.low < item.low):
            # 高低点整体上移：无包含关系，方向向上
            return KLINE_DIR.UP
        else:
            raise CChanException("combine type unknown", ErrCode.COMBINER_ERR)

    def set_fx(self, fx: FX_TYPE):
        """直接写分型值，仅供 deepcopy 复原时使用（正常运行路径请走 update_fx）。"""
        # only for deepcopy
        self.__fx = fx

    def try_add(self, unit_kl: T, exclude_included=False, allow_top_equal=None, skip_update_input=False):
        """
        尝试把新元素并入当前 KLC；能合就合，不能合就原样返回方向让调用方新开一根。

        Args:
            unit_kl: 新元素（CKLine_Unit 或 CBi）
            exclude_included: 传给 test_combine，被包含时不合并
            allow_top_equal: 传给 test_combine，顶/底相等时不合并
            skip_update_input: True 时不做"反向指针"回写（KLU 已有归属时避免重复设置）

        Returns:
            KLINE_DIR: COMBINE 表示已并入；UP/DOWN 表示没并入，
                       调用方应拿它当作下一根 KLC 的调整方向

        Raises:
            CChanException: 本 KLC 的方向非法（既不是 UP 也不是 DOWN）时无法合并（ErrCode.COMBINER_ERR）
        """
        # allow_top_equal = None普通模式
        # allow_top_equal = 1 被包含，顶部相等不合并
        # allow_top_equal = -1 被包含，底部相等不合并
        combine_item = CCombine_Item(unit_kl)
        _dir = self.test_combine(combine_item, exclude_included, allow_top_equal)
        if _dir == KLINE_DIR.COMBINE:
            self.__lst.append(unit_kl)
            if isinstance(unit_kl, CKLine_Unit) and not skip_update_input:
                # 让 KLU 反指回本 KLC（Bi/Seg 没有这个反向指针，所以先判类型）
                unit_kl.set_klc(self)
            if self.dir == KLINE_DIR.UP:
                # 向上合并取「高高」：只在新元素是"一字K线且高点恰好等于本KLC高点"时跳过，
                # 否则 low 会被抬到 high 上，把合并区间压成一条线
                if combine_item.high != combine_item.low or combine_item.high != self.high:  # 处理一字K线
                    self.__high = max([self.high, combine_item.high])
                    self.__low = max([self.low, combine_item.low])
            elif self.dir == KLINE_DIR.DOWN:
                # 向下合并取「低低」，一字K线的对称处理
                if combine_item.high != combine_item.low or combine_item.low != self.low:  # 处理一字K线
                    self.__high = min([self.high, combine_item.high])
                    self.__low = min([self.low, combine_item.low])
            else:
                raise CChanException(f"KLINE_DIR = {self.dir} err!!! must be {KLINE_DIR.UP}/{KLINE_DIR.DOWN}", ErrCode.COMBINER_ERR)
            self.__time_end = combine_item.time_end
            self.clean_cache()   # high/low 变了，缓存的极值定位必须失效
        # 返回UP/DOWN/COMBINE给KL_LIST，设置下一个的方向
        return _dir

    def get_peak_klu(self, is_high) -> T:
        """
        取"真正造出本 KLC 极值"的那一根元素。

        Args:
            is_high: True 取最高价所在的元素，False 取最低价所在的元素

        Returns:
            T: 极值所在的那根元素
        """
        # 获取最大值 or 最小值所在klu/bi
        return self.get_high_peak_klu() if is_high else self.get_low_peak_klu()

    @make_cache
    def get_high_peak_klu(self) -> T:
        """
        找最高价所在元素。

        Note:
            从 lst 的**最后一根往前**扫，返回最近一根造出该最高价的元素 ——
            这样笔的端点在"高点被重复触及"时落在更靠后的那根K线上，
            与 chan.py 的 get_peak_klu 口径一致。

        Raises:
            CChanException: 找不到等于 self.high 的元素（合并后不应发生，ErrCode.COMBINER_ERR）
        """
        for kl in self.lst[::-1]:
            if CCombine_Item(kl).high == self.high:
                return kl
        raise CChanException("can't find peak...", ErrCode.COMBINER_ERR)

    @make_cache
    def get_low_peak_klu(self) -> T:
        """
        找最低价所在元素，同样从后往前取最近一根。

        Raises:
            CChanException: 找不到等于 self.low 的元素（ErrCode.COMBINER_ERR）
        """
        for kl in self.lst[::-1]:
            if CCombine_Item(kl).low == self.low:
                return kl
        raise CChanException("can't find peak...", ErrCode.COMBINER_ERR)

    def update_fx(self, _pre: Self, _next: Self, exclude_included=False, allow_top_equal=None):
        """
        用左右两根邻居判定**自身**的分型，并顺便把前后链表接上。

        Args:
            _pre: 左邻居（时间上更早的 KLC）
            _next: 右邻居（时间上更晚的 KLC，它刚出现，正是判定时机）
            exclude_included: 用"被包含不合并"的口径判定（特征序列走这条路）
            allow_top_equal: 顶/底相等时是否放宽判定，含义同 test_combine

        Note:
            - 顶分型要求自身高点高于左右邻居的高点，且自身低点也高于左右邻居的低点；
              底分型三个不等号全部反向。两个条件都查是为了避免"被包含"的伪分型。
            - `exclude_included=True` 的分支用的是 `<=` / `>=`（把相等算作包含），
              因此还要用 allow_top_equal 额外区分"顶相等但仍算顶分型"的情况。
            - 本函数只写 `self.__fx`，不会把已定的分型改回 UNKNOWN；
              调用方需保证一根 KLC 只会被判定一次（它成为"倒数第二根"的那一刻）。
        """
        # allow_top_equal = None普通模式
        # allow_top_equal = 1 被包含，顶部相等不合并
        # allow_top_equal = -1 被包含，底部相等不合并
        self.set_next(_next)
        self.set_pre(_pre)
        _next.set_pre(self)   # 新邻居的前驱指向自己；自己已经指向它了，双向链表到此闭合
        if exclude_included:
            # 特征序列口径：允许"被包含"的元素存在，所以用 <= / >= 放宽比较
            if _pre.high < self.high and _next.high <= self.high and _next.low < self.low:
                # 顶分型；allow_top_equal==1 时，右邻居高点与本根相等也认可
                if allow_top_equal == 1 or _next.high < self.high:
                    self.__fx = FX_TYPE.TOP
            elif _next.high > self.high and _pre.low > self.low and _next.low >= self.low:
                # 底分型；allow_top_equal==-1 时，右邻居低点与本根相等也认可
                if allow_top_equal == -1 or _next.low > self.low:
                    self.__fx = FX_TYPE.BOTTOM
        elif _pre.high < self.high and _next.high < self.high and _pre.low < self.low and _next.low < self.low:
            # 普通口径：三根K线中间最高（且最低也最高）→ 顶分型
            self.__fx = FX_TYPE.TOP
        elif _pre.high > self.high and _next.high > self.high and _pre.low > self.low and _next.low > self.low:
            # 三根K线中间最低（且最高也最低）→ 底分型
            self.__fx = FX_TYPE.BOTTOM
        self.clean_cache()

    def __str__(self):
        """调试输出：`起始时间~结束时间 低点->高点`。"""
        return f"{self.time_begin}~{self.time_end} {self.low}->{self.high}"

    # 类型声明用的重载桩，运行时不会走到，实际实现是下面那个合并签名
    @overload
    def __getitem__(self, index: int) -> T: ...

    # 同上：切片形式的重载桩
    @overload
    def __getitem__(self, index: slice) -> List[T]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[T], T]:
        """
        下标访问，直接转发给 lst。

        Args:
            index: 整数下标或切片

        Returns:
            T 或 List[T]
        """
        return self.lst[index]

    def __len__(self):
        """本合并K线里包含多少个元素。"""
        return len(self.lst)

    def __iter__(self) -> Iterable[T]:
        """按顺序迭代被合并的元素。"""
        yield from self.lst

    def set_pre(self, _pre: Self | None):
        """
        设置前驱（允许 None，用于链表首尾/序列化前后断开重连）。

        Note:
            只改自己的指针，不会去改对端的 next —— 双向一致性由调用方（update_fx、序列化复原）保证。
        """
        self.__pre = _pre
        self.clean_cache()

    def set_next(self, _next: Self | None):
        """设置后继（允许 None），同样只改自己的指针。"""
        self.__next = _next
        self.clean_cache()
