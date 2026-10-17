# 某一级别的完整K线序列 CKLine_List：合并K线列表 + 笔/线段/线段段 + 各类中枢 + 买卖点容器。
#
# 链路位置：CKLine_Unit -> CKLine -> CKLine_List，
# CChan 每从数据源拿到一根该级别的 KLU 就调 add_single_klu 做增量推进（包含处理 -> 分型 -> 笔 -> 线段 -> 中枢），
# 全部数据喂完（或回放模式下每根K线）再调 cal_seg_and_zs 统一结算线段/中枢/买卖点。
#
# 文件底部的两个模块级函数 cal_seg / update_zs_in_seg 是"由下层元素序列算上层元素"的通用工具，
# 在 (笔 -> 线段) 和 (线段 -> 线段段) 两个层级上各被调用一次，逻辑完全同构。
import copy
from typing import List, Union, overload

from Bi.Bi import CBi
from Bi.BiList import CBiList
from BuySellPoint.BSPointList import CBSPointList
from ChanConfig import CChanConfig
from Common.CEnum import KLINE_DIR, SEG_TYPE
from Common.ChanException import CChanException, ErrCode
from Seg.Seg import CSeg
from Seg.SegConfig import CSegConfig
from Seg.SegListComm import CSegListComm
from ZS.ZSList import CZSList

from .KLine import CKLine
from .KLine_Unit import CKLine_Unit


def get_seglist_instance(seg_config: CSegConfig, lv) -> CSegListComm:
    """
    按配置里的线段算法名，造出对应实现的"线段列表"对象。

    Args:
        seg_config: 线段配置，取其中的 seg_algo 字段
        lv: 本序列的层级语义，SEG_TYPE.BI 表示"由笔构成的线段"，
            SEG_TYPE.SEG 表示"由线段构成的线段段"

    Returns:
        CSegListComm: 三种实现（chan / 1+1 / break）的共同基类实例

    Raises:
        CChanException: seg_algo 无法识别（ErrCode.PARA_ERROR）

    Note:
        1+1 与 break 两种老算法已经不再维护，选择它们会打印告警但仍然返回实例。
        具体实现类在分支内部 import，避免未被使用的算法把无关依赖拉进来。
    """
    if seg_config.seg_algo == "chan":
        from Seg.SegListChan import CSegListChan
        return CSegListChan(seg_config, lv)
    # elif seg_config.seg_algo == "1+1":
    #     print(f'Please avoid using seg_algo={seg_config.seg_algo} as it is deprecated and no longer maintained.')
    #     from Seg.SegListDYH import CSegListDYH
    #     return CSegListDYH(seg_config, lv)
    # elif seg_config.seg_algo == "break":
    #     print(f'Please avoid using seg_algo={seg_config.seg_algo} as it is deprecated and no longer maintained.')
    #     from Seg.SegListDef import CSegListDef
    #     return CSegListDef(seg_config, lv)
    else:
        raise CChanException(f"unsupport seg algoright:{seg_config.seg_algo}", ErrCode.PARA_ERROR)


class CKLine_List:
    """
    一个级别的全部计算产物容器，也是"增量推进"的执行者。

    它同时持有四个层级的元素序列和三类派生容器：
        lst          ：合并K线序列（基础，别的都由它推出来）
        bi_list      ：笔序列
        seg_list     ：线段序列（由笔构成）
        segseg_list  ：线段段序列（由线段构成）
        zs_list / segzs_list ：笔中枢 / 线段中枢
        bs_point_lst / seg_bs_point_lst ：笔级与线段级买卖点

    推进节奏：add_single_klu 负责"新K线进来后把 lst/分型/笔更新到最新"，
    cal_seg_and_zs 负责"把线段及其上的中枢、买卖点结算到最新"。
    """

    def __init__(self, kl_type, conf: CChanConfig):
        """
        Args:
            kl_type: 本序列对应的级别（KL_TYPE）
            conf: 全局配置 CChanConfig，各级子配置与指标模型都从这里取

        Note:
            构造只建空容器，不算任何东西；真正的计算从 add_single_klu 开始。
            last_sure_seg_*_start_bi_idx 是"增量游标"：记录上一次已确认线段的起始笔下标，
            下次重算线段时比它更旧的部分就不再重刷。
        """
        self.kl_type = kl_type
        self.config = conf
        self.lst: List[CKLine] = []  # K线列表，可递归  元素KLine类型
        self.bi_list = CBiList(bi_conf=conf.bi_conf)
        self.seg_list: CSegListComm[CBi] = get_seglist_instance(seg_config=conf.seg_conf, lv=SEG_TYPE.BI)
        self.segseg_list: CSegListComm[CSeg[CBi]] = get_seglist_instance(seg_config=conf.seg_conf, lv=SEG_TYPE.SEG)

        self.zs_list = CZSList(zs_config=conf.zs_conf)
        self.segzs_list = CZSList(zs_config=conf.zs_conf)

        self.bs_point_lst = CBSPointList[CBi, CBiList](bs_point_config=conf.bs_point_conf)
        self.seg_bs_point_lst = CBSPointList[CSeg, CSegListComm](bs_point_config=conf.seg_bs_point_conf)

        # 指标算子列表：每个级别一份，是有状态的流式对象，会随K线逐步推进
        self.metric_model_lst = conf.GetMetricModel()

        # 是否需要"每来一根K线就尝试算一次线段/中枢"（回放模式需要，一次性算完的模式不需要）
        self.step_calculation = self.need_cal_step_by_step()

        # 笔层面的增量游标：上一次已确认线段的起始笔下标，-1 表示还没确认过任何线段
        self.last_sure_seg_start_bi_idx = -1
        # 线段段层面的增量游标，含义同上，只是对象换成了线段
        self.last_sure_segseg_start_bi_idx = -1

    def __deepcopy__(self, memo):
        """
        自定义深拷贝：手工重建 lst，再深拷贝各派生容器。

        Args:
            memo: deepcopy 共享备忘录（id(原对象) -> 副本）

        Returns:
            CKLine_List: 与原对象等价的副本

        Note:
            为什么要手写：KLC 与 KLU 之间、KLC 与 KLC 之间都是双向引用，
            默认深拷贝会把同一根 KLU 复制成多份，破坏 klu.klc / klc.next 的一致性。
            这里按"先造 KLC、逐个补 KLU 反向引用、再串 pre/next"的顺序重建，
            memo 保证同一个 KLU 只被拷贝一次（klu.pre 可能落在上一根 KLC 里，靠 memo 取回副本）。
            注意 config 是共享引用而非深拷贝，两个副本会共用同一份配置。
        """
        new_obj = CKLine_List(self.kl_type, self.config)
        memo[id(self)] = new_obj
        for klc in self.lst:
            klus_new = []
            for klu in klc.lst:
                new_klu = copy.deepcopy(klu, memo)
                memo[id(klu)] = new_klu
                if klu.pre is not None:
                    new_klu.set_pre_klu(memo[id(klu.pre)])
                klus_new.append(new_klu)

            # 首根 KLU 由构造函数收纳，其余重现喂一遍；
            # skip_update_input=True 是因为上面已经手动设过 klu.__klc，不必再设一次
            new_klc = CKLine(klus_new[0], idx=klc.idx, _dir=klc.dir)
            new_klc.set_fx(klc.fx)
            new_klc.kl_type = klc.kl_type
            for idx, klu in enumerate(klus_new):
                klu.set_klc(new_klc)
                if idx != 0:
                    new_klc.try_add(klu, skip_update_input=True)
            memo[id(klc)] = new_klc
            if new_obj.lst:
                new_obj.lst[-1].set_next(new_klc)
                new_klc.set_pre(new_obj.lst[-1])
            new_obj.lst.append(new_klc)
        new_obj.bi_list = copy.deepcopy(self.bi_list, memo)
        new_obj.seg_list = copy.deepcopy(self.seg_list, memo)
        new_obj.segseg_list = copy.deepcopy(self.segseg_list, memo)
        new_obj.zs_list = copy.deepcopy(self.zs_list, memo)
        new_obj.segzs_list = copy.deepcopy(self.segzs_list, memo)
        new_obj.bs_point_lst = copy.deepcopy(self.bs_point_lst, memo)
        new_obj.metric_model_lst = copy.deepcopy(self.metric_model_lst, memo)
        new_obj.step_calculation = copy.deepcopy(self.step_calculation, memo)
        new_obj.seg_bs_point_lst = copy.deepcopy(self.seg_bs_point_lst, memo)
        return new_obj

    # 下面三个 __getitem__ 组成一组重载：带 @overload 的两个只在类型检查阶段生效、
    # 运行时会被最后那个带实现的版本覆盖；实现直接透传到内部 lst，
    # 所以 kl_list[0]（取单根）和 kl_list[0:3]（取一段）都可用。
    @overload
    def __getitem__(self, index: int) -> CKLine: ...

    @overload
    def __getitem__(self, index: slice) -> List[CKLine]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CKLine], CKLine]:
        """
        直接透传到内部的 lst，所以 kl_list[0] 和 kl_list[0:3] 都能用。

        Args:
            index: int 或 slice

        Returns:
            CKLine 或 List[CKLine]
        """
        return self.lst[index]

    def __len__(self):
        """
        Returns:
            int: 本级合并K线的根数（不是原始K线根数）
        """
        return len(self.lst)

    def cal_seg_and_zs(self):
        """
        把"线段及其上的中枢、买卖点"结算到最新，是重活，分两级同构做两遍。

        流程（第一遍，笔 -> 线段 -> 笔中枢）：
            1. 非逐步模式下先补一根虚笔，让最后一笔有终点可用；
            2. cal_seg：让 seg_list 依据 bi_list 重新切分线段，并给每根笔回写 seg_idx；
            3. zs_list.cal_bi_zs：依据笔和线段重算笔中枢；
            4. update_zs_in_seg：把中枢挂进所属线段，并回填中枢的 bi_in/bi_out/bi_lst。
        第二遍把上述过程在"线段 -> 线段段 -> 线段中枢"上原样重放一遍，
        最后才算线段级与笔级的买卖点（买卖点依赖中枢，必须放在最后）。

        Note:
            这是一个"重算到最新"的幂等操作，不依赖调用次数：
            内部各子步骤都带增量游标（last_sure_*），只重算尚未定稿的尾部。
            之所以在数据喂完后才由外部调用（见 CChan.load / CChan.trigger_load），
            是因为线段、中枢都必须等后续K线出现才能被确认，边喂边算只能得到中间态；
            而回放模式（step_calculation=True）才需要每根K线都调一次，以便对外输出逐步快照。
        """
        if not self.step_calculation:
            self.bi_list.try_add_virtual_bi(self.lst[-1])
        # 1) 笔 -> 线段：重切线段并把游标推进到最后一个"已确认"线段的起点笔
        self.last_sure_seg_start_bi_idx = cal_seg(self.bi_list, self.seg_list, self.last_sure_seg_start_bi_idx)
        self.zs_list.cal_bi_zs(self.bi_list, self.seg_list)
        update_zs_in_seg(self.bi_list, self.seg_list, self.zs_list)  # 计算seg的zs_lst，以及中枢的bi_in, bi_out

        # 第二遍在同一套逻辑上重放：线段 -> 线段段 -> 线段中枢，各层元素整体升一级
        self.last_sure_segseg_start_bi_idx = cal_seg(self.seg_list, self.segseg_list, self.last_sure_segseg_start_bi_idx)
        self.segzs_list.cal_bi_zs(self.seg_list, self.segseg_list)
        update_zs_in_seg(self.seg_list, self.segseg_list, self.segzs_list)  # 计算segseg的zs_lst，以及中枢的bi_in, bi_out

        # 计算买卖点
        self.seg_bs_point_lst.cal(self.seg_list, self.segseg_list)  # 线段线段买卖点
        self.bs_point_lst.cal(self.bi_list, self.seg_list)  # 再算笔买卖点

    def need_cal_step_by_step(self):
        """
        判断本序列是否需要在每根新K线到来时就重算线段/中枢。

        Returns:
            bool: 等于 config.trigger_step

        Note:
            回放（trigger_step=True）必须逐步算，因为每一步都要对外给出当时的线段/中枢；
            一次性加载的场景则把重活攒到最后统一做一次，省掉大量中间态计算。
        """
        return self.config.trigger_step

    def add_single_klu(self, klu: CKLine_Unit):
        """
        喂入一根新的原始K线，把 lst / 分型 / 笔推进一格，必要时顺带结算线段与中枢。

        Args:
            klu: 本级别新来的 CKLine_Unit（时间必须晚于 lst 里已有的K线）

        Note:
            完整流程：
              1) 先算指标（MACD/BOLL 等），指标是流式的，必须按时间顺序逐根喂；
              2) 若 lst 为空，直接开第一根 KLC；
              3) 否则让最后一根 KLC 尝试合并：
                 - 返回 COMBINE 表示新K线被并进最后一根 KLC，lst 长度不变，
                   此时确认性信息没变，只在回放模式下试着刷新一下虚笔终点；
                 - 返回 UP/DOWN 表示不能合并，需要新开一根 KLC；
              4) 新 KLC 出现后，倒数第二根 KLC 的前后邻居才齐备，这时才能判它的分型；
              5) 有了分型才谈得上成笔，笔变了（真实新笔或虚笔变动）才值得重算线段/中枢。
            非回放模式下这些"重算"都会被跳过，留到最后由 cal_seg_and_zs 统一结算。
        """
        klu.set_metric(self.metric_model_lst)
        if len(self.lst) == 0:
            self.lst.append(CKLine(klu, idx=0))
        else:
            # 返回值既表示"能否合并"，也用来决定新开的 KLC 的合并方向
            _dir = self.lst[-1].try_add(klu)
            if _dir != KLINE_DIR.COMBINE:  # 不需要合并K线
                # idx 取"append 之前的长度"，正好等于新K线在 lst 里的下标
                self.lst.append(CKLine(klu, idx=len(self.lst), _dir=_dir))
                if len(self.lst) >= 3:
                    # 只有存在左右两根邻居时才能判中间这根的分型
                    # （注意 update_fx 只更新倒数第二根，最新那根尚未成型）
                    self.lst[-2].update_fx(self.lst[-3], self.lst[-1])
                # update_bi 返回 True 表示笔的形态发生了变化，才值得重算线段/中枢
                if self.bi_list.update_bi(self.lst[-2], self.lst[-1], self.step_calculation) and self.step_calculation:
                    self.cal_seg_and_zs()
            # 被合并的情况下笔不会有确定性变化，但虚笔（未确认笔）的终点可能被刷新，
            # 所以先删掉旧虚笔再重试；这里的必要性参见issue#175
            elif self.step_calculation and self.bi_list.try_add_virtual_bi(self.lst[-1], need_del_end=True):  # 这里的必要性参见issue#175
                self.cal_seg_and_zs()

    def klu_iter(self, klc_begin_idx=0):
        """
        把合并K线序列展开成扁平的原始K线流，用于只关心单根K线的场景。

        Args:
            klc_begin_idx: 从第几根 KLC 开始展开，用于只看尾部增量

        Yields:
            CKLine_Unit: 依次吐出每根 KLC 里的原始K线（合并关系被抹平）
        """
        for klc in self.lst[klc_begin_idx:]:
            yield from klc.lst


def cal_seg(bi_list, seg_list: CSegListComm, last_sure_seg_start_bi_idx):
    """
    由下层元素序列重算上层序列，并给下层每个元素回写"我属于上层第几段"。

    两个层级共用这一个函数：(bi_list=笔列表, seg_list=线段列表) 算线段，
    或 (bi_list=线段列表, seg_list=线段段列表) 算线段段，因此下面统一用 bi 命名下层元素。

    Args:
        bi_list: 下层元素列表（支持 len()/[idx]，元素含 idx 属性与 set_seg_idx 方法）
        seg_list: 上层序列，函数内会先调它的 update() 增量重算
        last_sure_seg_start_bi_idx: 上次算出的"最后一个已确认上层的起始下标"，
            比它更旧的下层元素 seg_idx 不会再变，可据此提前结束循环（增量优化）

    Returns:
        int: 新的游标值——从后往前找到的第一个 is_sure 上层元素的起始下标；一个都没有则返回 -1

    Note:
        函数内部顺序：先 update 得到最新上层序列 -> 再倒着给下层元素贴 seg_idx -> 最后重算游标。
        倒序扫描是为了"上层序列从最后一段往前退着匹配"：随着下标减小，当前段只会往更早的段移动，
        所以整个循环是 O(n) 而不是嵌套查找。
    """
    seg_list.update(bi_list)

    if len(seg_list) == 0:
        # 一根上层元素都没成型：所有下层元素统一归属第0段，游标重置为 -1
        for bi in bi_list:
            bi.set_seg_idx(0)
        return -1

    cur_seg: CSeg = seg_list[-1]

    bi_idx = len(bi_list) - 1
    while bi_idx >= 0:
        bi = bi_list[bi_idx]
        if bi.seg_idx is not None and bi.idx < last_sure_seg_start_bi_idx:
            # 比上次确认点更旧的部分，seg_idx 已经定稿，直接收工
            break
        if bi.idx > cur_seg.end_bi.idx:
            # 位于最后一段之后：还没有任何段覆盖它，先临时记成"下一段"（idx+1）
            bi.set_seg_idx(cur_seg.idx+1)
            bi_idx -= 1
            continue
        if bi.idx < cur_seg.start_bi.idx:
            # 退到了当前段起始之前，沿着链表把当前段往前挪一格
            assert cur_seg.pre
            cur_seg = cur_seg.pre
        bi.set_seg_idx(cur_seg.idx)
        bi_idx -= 1

    # 从最后一段往前找第一个"已确认"（is_sure）的段，把游标推到它的起始元素
    last_sure_seg_start_bi_idx = -1
    seg = seg_list[-1]
    while seg:
        if seg.is_sure:
            last_sure_seg_start_bi_idx = seg.start_bi.idx
            break
        seg = seg.pre
    return last_sure_seg_start_bi_idx


def update_zs_in_seg(bi_list, seg_list, zs_list):
    """
    双向绑定上层线段与中枢：把中枢挂到所属线段上，并给每个中枢回填入/出笔和笔列表。

    Args:
        bi_list: 下层元素列表（笔或线段），给中枢回填 bi_in/bi_out/bi_lst 时按 idx 直接切它
        seg_list: 上层元素列表（线段或线段段），中枢会被挂到匹配的 seg 上
        zs_list: 中枢列表（笔中枢或线段中枢），本函数只补它的关联信息，不新建/删除中枢

    Note:
        处理顺序是"从新到旧"，且每个 seg 都先 clear_zs_lst 再重新灌，保证这个操作是幂等的
        （可以随 cal_seg_and_zs 反复调用而不产生重复挂载）：
          1) 倒着遍历 seg，一旦遇到 ele_inside_is_sure=True 就停——
             从该段起（含）往后的段内元素已定稿，不需要再算；
          2) 段内倒着扫中枢：中枢是按时间有序的（比较的两端都是原始K线的 idx），
             一旦发现它整体早于本段起点，再往前扫也不会有命中；
          3) 每个被访问到的中枢都顺带更新 bi_in/bi_out/bi_lst（与所属段无关，谁先访问到谁更新）；
          4) 统计从尾部开始"已确认段"的数量，超过 2 段才把当前段的 ele_inside_is_sure 置 True，
             即需要后面再出现两段确认，才认为这一段内部的元素不会再变。
    """
    sure_seg_cnt = 0
    seg_idx = len(seg_list) - 1
    while seg_idx >= 0:
        seg = seg_list[seg_idx]
        if seg.ele_inside_is_sure:
            # 这段及其之前都已定稿（上次已经算过），不必再往下扫
            break
        if seg.is_sure:
            sure_seg_cnt += 1
        # 先清空，保证重复调用不会重复挂载
        seg.clear_zs_lst()
        _zs_idx = len(zs_list) - 1
        while _zs_idx >= 0:
            zs = zs_list[_zs_idx]
            if zs.end.idx < seg.start_bi.get_begin_klu().idx:
                # 中枢的结束K线早于本段起始K线，且中枢按时间有序，再往前扫也不会命中
                break
            if zs.is_inside(seg):
                seg.add_zs(zs)
            # 中枢至少从第 1 根元素开始（CZSList 里禁止第一根就构成中枢），
            # 所以 begin_bi.idx-1 一定是个合法的"进入中枢的那一笔"
            assert zs.begin_bi.idx > 0
            zs.set_bi_in(bi_list[zs.begin_bi.idx-1])
            if zs.end_bi.idx+1 < len(bi_list):
                # 中枢结束之后还有元素时才有"离开中枢的那一笔"，否则留空
                zs.set_bi_out(bi_list[zs.end_bi.idx+1])
            zs.set_bi_lst(list(bi_list[zs.begin_bi.idx:zs.end_bi.idx+1]))
            _zs_idx -= 1

        if sure_seg_cnt > 2:
            # 尾部已有超过两段确认，说明本段的元素不会再被回溯改动了
            if not seg.ele_inside_is_sure:
                seg.ele_inside_is_sure = True
        seg_idx -= 1
