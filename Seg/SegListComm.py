"""
线段列表的公共基类模块。

CSegListComm 是三种线段算法（chan 特征序列法 / 1+1 都业华法 / break 线段破坏法）
的共同基类，它自己不负责"怎么切线段"，只负责三件公共的事：
    1. 容器能力（lst 列表 + __iter__/__getitem__/__len__）；
    2. "补收尾"（collect_left_seg 系列）：把还没被任何线段吞掉的尾部笔，
       按配置的方式收成一条未确认线段；
    3. 真正往列表里加线段的两个动作：try_add_new_seg（含首段拆分）与 add_new_seg（带容错）。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 【线段（本基类 + 三个算法子类）】 -> 中枢 -> 买卖点

Note:
    "补收尾"之所以必要：线段必须由后续走势确认，行情最新那一截笔往往凑不出
    一条合格线段，如果直接丢掉，中枢和买卖点就会缺失最新信息。
"""
import abc
from typing import Generic, List, TypeVar, Union, overload

from Bi.Bi import CBi
from Bi.BiList import CBiList
from Common.CEnum import BI_DIR, LEFT_SEG_METHOD, SEG_TYPE
from Common.ChanException import CChanException, ErrCode

from .Seg import CSeg
from .SegConfig import CSegConfig

SUB_LINE_TYPE = TypeVar('SUB_LINE_TYPE', CBi, "CSeg")


class CSegListComm(Generic[SUB_LINE_TYPE]):
    """
    线段序列的公共基类（泛型：SUB_LINE_TYPE 是本层线段由什么元素构成，笔或线段）。

    子类只要实现 update(bi_lst) 这一个抽象方法即可：
    三个子类 SegListChan / SegListDYH / SegListDef 分别对应三种线段算法。

    Note:
        lst 里存放的 CSeg 是按 idx 递增排列的，链表指针 pre/next 由 try_add_new_seg 串接。
    """

    def __init__(self, seg_config=CSegConfig(), lv=SEG_TYPE.BI):
        """
        Args:
            seg_config: 线段配置 CSegConfig（算法名 + 补收尾方式）
            lv: 层级语义，SEG_TYPE.BI 表示本序列由笔构成，SEG_TYPE.SEG 表示由线段构成

        Note:
            注意 do_init() 在 config 赋值之前就被调用了一次，
            所以各子类的 do_init 实现不能依赖 self.config。
        """
        self.lst: List[CSeg[SUB_LINE_TYPE]] = []
        self.lv = lv
        self.do_init()
        self.config = seg_config

    def do_init(self):
        """清空线段列表（各子类会重写它，顺便把笔的 parent_seg 等反向引用清掉）。"""
        self.lst = []

    def __iter__(self):
        """按顺序迭代每一条线段。"""
        yield from self.lst

    # 下面三个 __getitem__ 组成一组重载：带 @overload 的两个只在类型检查阶段生效，
    # 运行时会被最后那个带实现的版本覆盖
    @overload
    def __getitem__(self, index: int) -> CSeg[SUB_LINE_TYPE]: ...

    @overload
    def __getitem__(self, index: slice) -> List[CSeg[SUB_LINE_TYPE]]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CSeg[SUB_LINE_TYPE]], CSeg[SUB_LINE_TYPE]]:
        """
        按下标/切片取线段。

        Args:
            index: int 或 slice

        Returns:
            CSeg 或 List[CSeg]
        """
        return self.lst[index]

    def __len__(self):
        """Returns: int: 线段的条数（含尾部未确认线段）。"""
        return len(self.lst)

    def left_bi_break(self, bi_lst: CBiList):
        # 最后一个确定线段之后的笔有突破该线段最后一笔的
        """
        判断最后一条线段之后的笔，有没有突破这条线段的尾部（线段可能被破坏）。

        Args:
            bi_lst: 完整的笔列表

        Returns:
            bool: True 表示后面有笔突破了最后一条线段终点笔的高/低点

        Note:
            只有都业华法（CSegListDYH）会用到它：突破意味着尾部笔有能力构成新线段，
            值得再跑一次未确认线段的推算（cal_bi_unsure）。
        """
        if len(self) == 0:
            return False
        last_seg_end_bi = self[-1].end_bi
        # 注意这里直接拿笔的 idx 当列表下标用，依赖「笔 idx == 列表下标」这条性质
        for bi in bi_lst[last_seg_end_bi.idx+1:]:
            if last_seg_end_bi.is_up() and bi._high() > last_seg_end_bi._high():
                return True
            elif last_seg_end_bi.is_down() and bi._low() < last_seg_end_bi._low():
                return True
        return False

    def collect_first_seg(self, bi_lst: CBiList):
        """
        处理"还没有任何线段"时的补收尾：把开头的笔收成第一条（未确认）线段。

        Args:
            bi_lst: 完整的笔列表

        Raises:
            CChanException: left_method 配置非法（ErrCode.PARA_ERROR）

        Note:
            两种策略：
                PEAK：先判断开头这一段整体是往上还是往下（离起点更远的那端），
                      然后在对应方向找极值笔，以它为终点收成一段，剩下的递归处理；
                ALL ：不管三七二十一，把从起点到最后一根笔全部收成一段。
        """
        if len(bi_lst) < 3:
            return
        # 注意：这里读的是 self.config，而 __init__ 里 do_init 是在赋值 config 之前调用的
        if self.config.left_method == LEFT_SEG_METHOD.PEAK:
            # 哪一端离第一笔的起点更远，就认为开头这一段是朝那个方向走的
            _high = max(bi._high() for bi in bi_lst)
            _low = min(bi._low() for bi in bi_lst)
            if abs(_high-bi_lst[0].get_begin_val()) >= abs(_low-bi_lst[0].get_begin_val()):
                peak_bi = FindPeakBi(bi_lst, is_high=True)
                assert peak_bi is not None
                self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=BI_DIR.UP, split_first_seg=False, reason="0seg_find_high")
            else:
                peak_bi = FindPeakBi(bi_lst, is_high=False)
                assert peak_bi is not None
                self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=BI_DIR.DOWN, split_first_seg=False, reason="0seg_find_low")
            self.collect_left_as_seg(bi_lst)
        elif self.config.left_method == LEFT_SEG_METHOD.ALL:
            _dir = BI_DIR.UP if bi_lst[-1].get_end_val() >= bi_lst[0].get_begin_val() else BI_DIR.DOWN
            self.add_new_seg(bi_lst, bi_lst[-1].idx, is_sure=False, seg_dir=_dir, split_first_seg=False, reason="0seg_collect_all")
        else:
            raise CChanException(f"unknown seg left_method = {self.config.left_method}", ErrCode.PARA_ERROR)

    def collect_left_seg_peak_method(self, last_seg_end_bi, bi_lst):
        """
        PEAK 策略下的递归收尾：反复"在新的方向上找极值笔 -> 收成一段"，直到无可再收。

        Args:
            last_seg_end_bi: 上一条线段的终点笔
            bi_lst: 完整的笔列表

        Note:
            找极值笔时要求它与上一条线段终点笔至少相隔 3 根笔（+3 是为了跨过
            中间的反向笔），否则认为这段太短、不值得单独成段。
        """
        find_new_seg = False
        if last_seg_end_bi.is_down():
            peak_bi = FindPeakBi(bi_lst[last_seg_end_bi.idx+3:], is_high=True)
            if peak_bi and peak_bi.idx - last_seg_end_bi.idx >= 3:
                self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=BI_DIR.UP, reason="collectleft_find_high")
                find_new_seg = True
        else:
            peak_bi = FindPeakBi(bi_lst[last_seg_end_bi.idx+3:], is_high=False)
            if peak_bi and peak_bi.idx - last_seg_end_bi.idx >= 3:
                self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=BI_DIR.DOWN, reason="collectleft_find_low")
                find_new_seg = True
        last_seg_end_bi = self[-1].end_bi
        # 找不到新极值了，就把剩下的笔一股脑并入上一条线段；还能找到则继续递归
        if not find_new_seg:
            self.collect_left_as_seg(bi_lst)
        else:
            self.collect_left_seg_peak_method(last_seg_end_bi, bi_lst)

    def collect_segs(self, bi_lst):
        """
        处理"已经有线段、但尾部还有剩余笔"的收尾。

        Args:
            bi_lst: 完整的笔列表

        Raises:
            CChanException: left_method 配置非法（ErrCode.PARA_ERROR）

        Note:
            先处理两种"强制成段"的情形：剩余笔已经在上一条线段的反方向上走到
            与它同高度甚至超过它，说明上一条线段肯定该结束了，这时必须切成新段。
            剩下的情形才交给 PEAK / ALL 两种策略。
        """
        last_bi = bi_lst[-1]
        last_seg_end_bi = self[-1].end_bi
        if last_bi.idx-last_seg_end_bi.idx < 3:
            return
        # 剩余笔在上一条线段的同方向上创了新低/新高 -> 上一条线段到此为止，必须切新段
        if last_seg_end_bi.is_down() and last_bi.get_end_val() <= last_seg_end_bi.get_end_val():
            if peak_bi := FindPeakBi(bi_lst[last_seg_end_bi.idx+3:], is_high=True):
                self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=BI_DIR.UP, reason="collectleft_find_high_force")
                self.collect_left_seg(bi_lst)
        elif last_seg_end_bi.is_up() and last_bi.get_end_val() >= last_seg_end_bi.get_end_val():
            if peak_bi := FindPeakBi(bi_lst[last_seg_end_bi.idx+3:], is_high=False):
                self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=BI_DIR.DOWN, reason="collectleft_find_low_force")
                self.collect_left_seg(bi_lst)
        # 剩下线段的尾部相比于最后一个线段的尾部，高低关系和最后一个虚线段的方向一致
        # ALL 会把剩余笔全部并进上一段，代价是最新的二类买卖点容易漏掉
        elif self.config.left_method == LEFT_SEG_METHOD.ALL:  # 容易找不到二类买卖点！！
            self.collect_left_as_seg(bi_lst)
        elif self.config.left_method == LEFT_SEG_METHOD.PEAK:
            self.collect_left_seg_peak_method(last_seg_end_bi, bi_lst)
        else:
            raise CChanException(f"unknown seg left_method = {self.config.left_method}", ErrCode.PARA_ERROR)

    def collect_left_seg(self, bi_lst: CBiList):
        """
        补收尾的总入口：把还没被任何线段覆盖的尾部笔收成一条未确认线段。

        Args:
            bi_lst: 完整的笔列表

        Note:
            这是三种算法共用的最后一步，保证任何算法下 bi_lst 里的笔
            都会被某条线段覆盖（哪怕只是一条未确认线段）。
        """
        if len(self) == 0:
            self.collect_first_seg(bi_lst)
        else:
            self.collect_segs(bi_lst)
            # 收完还是剩了超过 2 根笔没被最后一条（未确认）线段吃掉，
            # 说明这条线段收得不够好，撤掉它重收一次
            if len(bi_lst) > 0 and len(self.lst) > 0 and bi_lst[-1].idx - self.lst[-1].bi_list[-1].idx > 2 and not self.lst[-1].is_sure:
                # 每次只撤一条，递归重收；撤掉的是未确认线段，不会破坏已确认的部分
                self.lst = self.lst[:-1]
                self.collect_left_seg(bi_lst)

    def collect_left_as_seg(self, bi_lst: CBiList):
        """
        ALL 策略：把上一条线段之后剩余的笔，全部收成一条未确认线段。

        Args:
            bi_lst: 完整的笔列表

        Note:
            终点笔的下标要按"剩余笔与上一条线段终点笔是否同向"来定：
            同向时取倒数第二根笔，避免新线段与上一条线段共用同一方向的端点笔。
        """
        last_bi = bi_lst[-1]
        last_seg_end_bi = self[-1].end_bi
        if last_seg_end_bi.idx+1 >= len(bi_lst):
            return
        if last_seg_end_bi.dir == last_bi.dir:
            self.add_new_seg(bi_lst, last_bi.idx-1, is_sure=False, reason="collect_left_1")
        else:
            self.add_new_seg(bi_lst, last_bi.idx, is_sure=False, reason="collect_left_0")

    def try_add_new_seg(self, bi_lst, end_bi_idx: int, is_sure=True, seg_dir=None, split_first_seg=True, reason="normal"):
        """
        真正往 lst 里加一条线段（可能顺带把首段拆成两段）。

        Args:
            bi_lst: 完整的笔列表
            end_bi_idx: 新线段的终点笔下标
            is_sure: 新线段是否是确认线段
            seg_dir: 新线段的方向；None 时取终点笔方向
            split_first_seg: 添加"第一条线段"时，是否允许把它拆成两段
            reason: 生成理由（便于调试回溯）

        Note:
            split_first_seg 的动机：第一条线段没有"上一条线段"作参照，
            如果开头的走势其实已经走出了一高一低两段，直接收成一段会丢失一整段，
            所以当能在开头找到更极端的反向笔时，就把首段拆成两段。
        """
        if len(self) == 0 and split_first_seg and end_bi_idx >= 3:
            # 在第一条线段起点之前、往回找更极端的反向笔（切片是倒序的）
            if peak_bi := FindPeakBi(bi_lst[end_bi_idx-3::-1], bi_lst[end_bi_idx].is_down()):
                if (peak_bi.is_down() and (peak_bi._low() < bi_lst[0]._low() or peak_bi.idx == 0)) or \
                   (peak_bi.is_up() and (peak_bi._high() > bi_lst[0]._high() or peak_bi.idx == 0)):  # 要比第一笔开头还高/低（因为没有比较到）
                    self.add_new_seg(bi_lst, peak_bi.idx, is_sure=False, seg_dir=peak_bi.dir, reason="split_first_1st")
                    self.add_new_seg(bi_lst, end_bi_idx, is_sure=False, reason="split_first_2nd")
                    return
        # 新线段的起点：第一条线段从 0 号笔开始，否则接在上一条线段终点笔之后
        bi1_idx = 0 if len(self) == 0 else self[-1].end_bi.idx+1
        bi1 = bi_lst[bi1_idx]
        bi2 = bi_lst[end_bi_idx]
        self.lst.append(CSeg(len(self.lst), bi1, bi2, is_sure=is_sure, seg_dir=seg_dir, reason=reason))

        if len(self.lst) >= 2:
            self.lst[-2].next = self.lst[-1]
            self.lst[-1].pre = self.lst[-2]
        self.lst[-1].update_bi_list(bi_lst, bi1_idx, end_bi_idx)

    def add_new_seg(self, bi_lst: CBiList, end_bi_idx: int, is_sure=True, seg_dir=None, split_first_seg=True, reason="normal"):
        """
        try_add_new_seg 的容错包装。

        Args:
            与 try_add_new_seg 完全相同

        Returns:
            bool: 是否成功添加了线段

        Note:
            唯一的容错是把"第一条线段方向与首尾值异常"（ErrCode.SEG_END_VALUE_ERR）
            吞掉并返回 False：这属于行情开头常见的、可接受的异常，
            调用方拿到 False 会直接从下一个下标重算。其余异常一律原样抛出。
        """
        try:
            self.try_add_new_seg(bi_lst, end_bi_idx, is_sure, seg_dir, split_first_seg, reason)
        except CChanException as e:
            if e.errcode == ErrCode.SEG_END_VALUE_ERR and len(self.lst) == 0:
                return False
            raise e
        except Exception as e:
            raise e
        return True

    # 由三个算法子类实现：根据本层的元素序列重算线段列表
    @abc.abstractmethod
    def update(self, bi_lst: CBiList):
        ...

    def exist_sure_seg(self):
        """Returns: bool: 是否至少有一条已确认线段。"""
        return any(seg.is_sure for seg in self.lst)


def FindPeakBi(bi_lst: Union[CBiList, List[CBi]], is_high):
    """
    在一堆笔里找"最极端的那一根"（最高点所在的向上笔 / 最低点所在的向下笔）。

    Args:
        bi_lst: 候选笔（列表或 CBiList）
        is_high: True 找最高，False 找最低

    Returns:
        Optional[CBi]: 找到的极值笔；没有符合的则返回 None

    Note:
        同值时取更靠后的那根（比较用的是 >= / <=）；
        并且会排除掉"前面隔一根的同向笔更极端"的情况，
        避免把一波趋势中间的小回调当成极值。
    """
    peak_val = float("-inf") if is_high else float("inf")
    peak_bi = None
    for bi in bi_lst:
        if (is_high and bi.get_end_val() >= peak_val and bi.is_up()) or (not is_high and bi.get_end_val() <= peak_val and bi.is_down()):
            # 前面隔一根（即同向的前一根）如果比它还极端，说明它不是真正的极值，跳过
            if bi.pre and bi.pre.pre and ((is_high and bi.pre.pre.get_end_val() > bi.get_end_val()) or (not is_high and bi.pre.pre.get_end_val() < bi.get_end_val())):
                continue
            peak_val = bi.get_end_val()
            peak_bi = bi
    return peak_bi
