"""
中枢列表（ZSList）模块：决定"哪些笔/线段该构成一个中枢、中枢要不要合并"。

CZSList 维护一个中枢序列，逐个把元素（笔或线段）喂进来：
    1. 先试着并入上一个中枢（中枢延伸）；
    2. 并入失败就攒在 free_item_lst 里，攒够 2~3 个再试着构造一个新中枢；
    3. 新中枢成型后，再按配置尝试和前一个中枢合并（中枢扩展/扩展成更大级别）。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 线段 -> 【本模块：中枢列表】 -> 买卖点

Note:
    单个中枢的计算在 ZS/ZS.py，本模块只负责"构造与合并"的决策。
    两种构造模式由 zs_algo 决定：
        normal  ：按线段一段一段地扫（中枢不跨线段）；
        over_seg：允许中枢跨线段（此时要求关闭一笔中枢）。
"""
from typing import List, Union, overload

from Bi.Bi import CBi
from Bi.BiList import CBiList
from Common.func_util import revert_bi_dir
from Seg.Seg import CSeg
from Seg.SegListComm import CSegListComm
from ZS.ZSConfig import CZSConfig

from .ZS import CZS


class CZSList:
    """
    一个级别的中枢序列容器 + 构造状态机。

    核心状态：
        zs_lst       ：已成型的中枢
        free_item_lst：还没凑成中枢的元素（笔或线段）
        config       ：CZSConfig（是否合并、合并口径、是否允许一笔中枢、构造模式）
        last_sure_pos：增量游标 —— 最后一个确认线段起始笔的 idx，比它更旧的中枢不会再改
        last_seg_idx ：增量游标 —— 最后一个确认线段的 idx
    """

    def __init__(self, zs_config=CZSConfig()):
        """
        Args:
            zs_config: 中枢配置 CZSConfig
        """
        self.zs_lst: List[CZS] = []

        self.config = zs_config
        self.free_item_lst = []

        self.last_sure_pos = -1
        self.last_seg_idx = 0

    def update_last_pos(self, seg_list: CSegListComm):
        """
        把增量游标推进到"最后一个确认线段"的位置。

        Args:
            seg_list: 线段列表

        Note:
            从后往前找第一个确认线段，把它的 idx 记为 last_seg_idx、
            起始笔 idx 记为 last_sure_pos；一个确认线段都没有时两者分别是 0 与 -1。
            重算时只看比它更新的部分。
        """
        self.last_sure_pos = -1
        self.last_seg_idx = 0
        _seg_idx = len(seg_list) - 1
        while _seg_idx >= 0:
            seg = seg_list[_seg_idx]
            if seg.is_sure:
                self.last_sure_pos = seg.start_bi.idx
                self.last_seg_idx = seg.idx
                return
            _seg_idx -= 1

    def seg_need_cal(self, seg: CSeg):
        """
        判断这条线段是否还需要参与本次重算。

        Args:
            seg: 待判断的线段

        Returns:
            bool: 起点还在游标之后（含）才需要重算
        """
        return seg.start_bi.idx >= self.last_sure_pos

    def add_to_free_lst(self, item, is_sure, zs_algo):
        """
        把一个元素塞进"待构造"列表，够条件就构造一个新中枢。

        Args:
            item: 新来的元素（笔或线段）
            is_sure: 它是否是确认元素（决定构造出来的中枢是不是确认中枢）
            zs_algo: 构造模式，'normal' 或 'over_seg'

        Note:
            开头那个替换动作是为了防止"笔的尾部被更新（新高/新低）"时
            同一个下标的元素被重复塞进来。
            构造成功后，第一笔不能是中枢的起点（否则"进中枢的那一笔"不存在）。
        """
        if len(self.free_item_lst) != 0 and item.idx == self.free_item_lst[-1].idx:
            # 防止笔新高或新低的更新带来bug
            self.free_item_lst = self.free_item_lst[:-1]
        self.free_item_lst.append(item)
        res = self.try_construct_zs(self.free_item_lst, is_sure, zs_algo)  # 可能是一笔中枢
        # 中枢必须从第 1 号元素之后才开始：否则找不到「进入中枢的那一笔」
        if res is not None and res.begin_bi.idx > 0:  # 禁止第一笔就是中枢的起点
            self.zs_lst.append(res)
            self.clear_free_lst()
            self.try_combine()

    def clear_free_lst(self):
        """清空"待构造"列表（换一段线段时避免把上一段的笔带过去）。"""
        self.free_item_lst = []

    def update(self, bi: CBi, is_sure=True):
        """
        喂进一个元素：先尝试中枢延伸，不行再攒着等构造新中枢。

        Args:
            bi: 新来的元素（笔或线段）
            is_sure: 它是否是确认元素

        Note:
            只有当"待构造列表为空"时才尝试延伸，
            否则新元素可能会越过待构造列表直接并入上一个中枢。
        """
        if len(self.free_item_lst) == 0 and self.try_add_to_end(bi):
            # zs_combine_mode=peak合并模式下会触发生效，=zs合并一定无效返回
            self.try_combine()  # 新形成的中枢尝试和之前的中枢合并
            return
        self.add_to_free_lst(bi, is_sure, "normal")

    def try_add_to_end(self, bi):
        """
        尝试让新元素直接并入最后一个中枢（中枢延伸）。

        Args:
            bi: 新来的元素（笔或线段）

        Returns:
            bool: 并入成功则为 True
        """
        return False if len(self.zs_lst) == 0 else self[-1].try_add_to_end(bi)

    def add_zs_from_bi_range(self, seg_bi_lst: list, seg_dir, seg_is_sure):
        """
        把一个线段区间内的元素逐个喂进来，构造该线段内的中枢。

        Args:
            seg_bi_lst: 本线段包含的元素（笔）
            seg_dir: 线段方向
            seg_is_sure: 线段是否确认

        Note:
            与线段同向的元素会被跳过：中枢是由"相邻反向元素"的区间重叠构成的，
            同向的那些元素已经算在中枢内部了。
            第一个元素直接进"待构造"列表，之后的才走 update（可延伸可构造），
            这是为了防止延伸动作跨到上一条线段的中枢里去。
        """
        deal_bi_cnt = 0
        for bi in seg_bi_lst:
            # 与线段同向的元素不参与构造中枢（中枢由反向元素的区间重叠构成）
            if bi.dir == seg_dir:
                continue
            # 区间内第一个元素直接进待构造列表，避免延伸动作跑到上一条线段的中枢里
            if deal_bi_cnt < 1:  # 防止try_add_to_end执行到上一个线段的中枢里面去
                self.add_to_free_lst(bi, seg_is_sure, "normal")
                deal_bi_cnt += 1
            else:
                self.update(bi, seg_is_sure)

    def try_construct_zs(self, lst, is_sure, zs_algo):
        """
        用攒下来的元素试着构造一个中枢。

        Args:
            lst: 待构造列表里的元素
            is_sure: 构造出来的中枢是否是确认中枢
            zs_algo: 'normal' 取末尾 2 个（或 1 个，允许一笔中枢时）元素构造；
                     'over_seg' 取末尾 3 个元素构造

        Returns:
            Optional[CZS]: 构造成功返回新中枢，否则返回 None

        Note:
            最终的交集检查是 has_overlap 的另一种写法：
            min(各元素高点) > max(各元素低点) 才说明它们真的有重叠。
        """
        if zs_algo == "normal":
            if not self.config.one_bi_zs:
                if len(lst) == 1:
                    return None
                else:
                    lst = lst[-2:]
        elif zs_algo == "over_seg":
            if len(lst) < 3:
                return None
            lst = lst[-3:]
            # 最旧那根元素与上一条线段同向（说明这三根跨了线段）：丢掉它，本次不构造
            if lst[0].dir == lst[0].parent_seg.dir:
                lst = lst[1:]
                return None
        # 取交集：最低高点 > 最高低点 才说明这些元素确实有重叠
        min_high = min(item._high() for item in lst)
        max_low = max(item._low() for item in lst)
        return CZS(lst, is_sure=is_sure) if min_high > max_low else None

    def cal_bi_zs(self, bi_lst: Union[CBiList, CSegListComm], seg_lst: CSegListComm):
        """
        重算中枢列表的总入口。

        Args:
            bi_lst: 本层的元素列表（笔列表或线段列表）
            seg_lst: 上层的线段列表（用于按段扫描、以及增量游标）

        Raises:
            Exception: zs_algo 配置的取值不合法

        Note:
            开头先把"起点还在游标之后"的最新中枢撤掉（它们会被重新算出来）。
            三种模式：
                normal  ：按线段逐段扫，段与段之间清空待构造列表，
                          最后再单独处理"还没成线段"的那截元素；
                over_seg：不按线段，直接从上次位置往后扫，允许中枢跨线段；
                auto    ：有确认线段时按段扫，一旦遇到未确认线段就切成 over_seg 模式。
        """
        while self.zs_lst and self.zs_lst[-1].begin_bi.idx >= self.last_sure_pos:
            self.zs_lst.pop()
        # normal：按线段一段一段扫，中枢不跨线段
        if self.config.zs_algo == "normal":
            for seg in seg_lst[self.last_seg_idx:]:
                if not self.seg_need_cal(seg):
                    continue
                self.clear_free_lst()
                seg_bi_lst = bi_lst[seg.start_bi.idx:seg.end_bi.idx+1]
                self.add_zs_from_bi_range(seg_bi_lst, seg.dir, seg.is_sure)

            # 处理未生成新线段的部分
            if len(seg_lst):
                self.clear_free_lst()
                self.add_zs_from_bi_range(bi_lst[seg_lst[-1].end_bi.idx+1:], revert_bi_dir(seg_lst[-1].dir), False)
        # over_seg：允许中枢跨线段，代价是必须先关掉一笔中枢
        elif self.config.zs_algo == "over_seg":
            assert self.config.one_bi_zs is False
            self.clear_free_lst()
            begin_bi_idx = self.zs_lst[-1].end_bi.idx+1 if self.zs_lst else 0
            for bi in bi_lst[begin_bi_idx:]:
                self.update_overseg_zs(bi)
        # auto：能按线段算就按线段算，遇到未确认线段就退化成 over_seg
        elif self.config.zs_algo == "auto":
            sure_seg_appear = False
            exist_sure_seg = seg_lst.exist_sure_seg()
            for seg in seg_lst[self.last_seg_idx:]:
                if seg.is_sure:
                    sure_seg_appear = True
                if not self.seg_need_cal(seg):
                    continue
                if seg.is_sure or (not sure_seg_appear and exist_sure_seg):
                    self.clear_free_lst()
                    self.add_zs_from_bi_range(bi_lst[seg.start_bi.idx:seg.end_bi.idx+1], seg.dir, seg.is_sure)
                else:
                    self.clear_free_lst()
                    for bi in bi_lst[seg.start_bi.idx:]:
                        self.update_overseg_zs(bi)
                    break
        else:
            raise Exception(f"unknown zs_algo {self.config.zs_algo}")
        self.update_last_pos(seg_lst)

    def update_overseg_zs(self, bi: CBi | CSeg):
        """
        over_seg 模式下喂进一个元素。

        Args:
            bi: 新来的元素（笔或线段）

        Note:
            比 normal 模式多一道判断：只有当"元素与上一个中枢尾部相邻（间隔 <=1）
            且它的下一根也还在中枢区间内"时，才允许延伸 ——
            否则会造出跨度过大的中枢。
        """
        if len(self.zs_lst) and len(self.free_item_lst) == 0:
            if bi.next is None:
                return
            if bi.idx - self.zs_lst[-1].end_bi.idx <= 1 and self.zs_lst[-1].in_range(bi.next) and self.zs_lst[-1].try_add_to_end(bi):
                return
        if len(self.zs_lst) and len(self.free_item_lst) == 0 and self.zs_lst[-1].in_range(bi) and bi.idx - self.zs_lst[-1].end_bi.idx <= 1:
            return
        # 其余情况一律走「攒着等构造」，交给 try_construct_zs 的 3 元素规则
        self.add_to_free_lst(bi, bi.is_sure, zs_algo="over_seg")

    def __iter__(self):
        """按时间顺序迭代每一个中枢。"""
        yield from self.zs_lst

    def __len__(self):
        """Returns: int: 中枢的个数。"""
        return len(self.zs_lst)

    # 下面三个 __getitem__ 组成一组重载：带 @overload 的两个只在类型检查阶段生效，
    # 运行时会被最后那个带实现的版本覆盖
    @overload
    def __getitem__(self, index: int) -> CZS: ...

    @overload
    def __getitem__(self, index: slice) -> List[CZS]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CZS], CZS]:
        """
        按下标/切片取中枢。

        Args:
            index: int 或 slice

        Returns:
            CZS 或 List[CZS]
        """
        return self.zs_lst[index]

    def try_combine(self):
        """
        尝试把最后两个中枢合并（中枢的扩展/合并）。

        Note:
            合并成功后被吃掉的是"后面那个"，所以要把 zs_lst 的尾部删掉一个；
            用 while 是因为合并完可能又能和更前面的中枢合并，要一路并上去。
        """
        if not self.config.need_combine:
            return
        # 一路并下去：合并后新中枢可能又能和更前面的中枢重叠
        while len(self.zs_lst) >= 2 and self.zs_lst[-2].combine(self.zs_lst[-1], combine_mode=self.config.zs_combine_mode):
            self.zs_lst = self.zs_lst[:-1]  # 合并后删除最后一个
