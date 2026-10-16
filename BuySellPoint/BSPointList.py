"""
买卖点列表（BSPointList）模块：缠论计算链路的最后一环。

CBSPointList 是"某个级别上所有买卖点"的容器兼计算器：它拿着上一环算好的笔/线段序列与中枢，
逐条线段判定各类买卖点，按类型分桶存起来，并负责"行情尾部被重算时把失效买卖点撤掉"的去重与更新。

链路位置：
    原始K线 -> 包含合并 -> 分型 -> 笔 -> 线段 -> 中枢 -> 【本模块：买卖点】

六类买卖点的判定要点（细节见各 cal_ / treat_ 函数）：
    T1  (1类)  ：线段末笔跌破/涨破最后一个中枢，且相对"进中枢那笔"背驰（趋势背驰）
    T1P (盘整1类)：中枢不够格（一笔中枢或数量不足）时，改用"末笔 vs 前一同向笔"比力度（盘整背驰）
    T2  (2类)  ：1类之后的第一根回撤笔，且回撤幅度 <= max_bs2_rate
    T2S (类二) ：2类之后、仍在同一线段内且区间与2类那笔重叠的后续同向笔
    T3A (3类A)：中枢在1类"之后" —— 下一段的中枢被突破后，回抽不回中枢的那一笔
    T3B (3类B)：中枢在1类"之前" —— 本段最后一个中枢之后的回抽笔

Note:
    - 计算是幂等重算：每次 cal() 都先撤销尾部失效买卖点，再从 last_sure_seg_idx 起重算，
      所以"逐步计算（回放模式每根K线算一次）"与"一次性算完"的结果是一致的；
      两者的差别只在上游（KLine_List 是否在尾部补虚笔），本模块不受影响。
    - 同一根笔上只会有一个 CBS_Point 实例，多类型通过 add_type 叠加。
    - 笔级与线段级买卖点共用这一份实现，靠泛型 LINE_TYPE 区分（配置不同而已）。
"""
from typing import Dict, Generic, Iterable, List, Optional, Tuple, TypeVar

from Bi.Bi import CBi
from Bi.BiList import CBiList
from Common.CEnum import BSP_TYPE
from Common.func_util import has_overlap
from Seg.Seg import CSeg
from Seg.SegListComm import CSegListComm
from ZS.ZS import CZS

from .BS_Point import CBS_Point
from .BSPointConfig import CBSPointConfig, CPointConfig

# 笔级买卖点：LINE_TYPE=CBi、LINE_LIST_TYPE=CBiList
# 线段级买卖点：LINE_TYPE=CSeg、LINE_LIST_TYPE=CSegListComm，两级共用一份实现
LINE_TYPE = TypeVar('LINE_TYPE', CBi, CSeg[CBi])
LINE_LIST_TYPE = TypeVar('LINE_LIST_TYPE', CBiList, CSegListComm[CBi])


class CBSPointList(Generic[LINE_TYPE, LINE_LIST_TYPE]):
    """
    某个级别上全部买卖点的容器 + 计算器。

    存储结构：
        bsp_store_dict      ：{BSP_TYPE: (卖点列表, 买点列表)}，每个列表内部按笔 idx 递增
        bsp_store_flat_dict ：{笔idx: CBS_Point}，跨类型的平铺索引，用于去重与 __len__
        bsp1_list/bsp1_dict ：1类买卖点（T1/T1P）的单独索引，2/3类靠它反查"对应的1类"

    计算游标：
        last_sure_pos     ：定稿线 —— 最后一个确认线段的"终笔起点K线" idx
        last_sure_seg_idx ：最后一个确认线段的下标，重算从这里开始

    Note:
        买卖点不是"只增不减"的：行情尾部还没被确认的线段/中枢会随着新K线被重算，
        其上推导出来的买卖点可能失效，所以每次重算前必须先撤销尾部（clear_*_end）。
    """

    def __init__(self, bs_point_config: CBSPointConfig):
        """
        Args:
            bs_point_config: 买卖点配置 CBSPointConfig（内含买侧/卖侧两份 CPointConfig）

        Note:
            last_sure_pos 初值 -1、last_sure_seg_idx 初值 0，表示"一切都没定稿"，
            第一次 cal() 会从头算一遍并把游标推到真正的定稿位置。
        """
        self.bsp_store_dict: Dict[BSP_TYPE, Tuple[List[CBS_Point[LINE_TYPE]], List[CBS_Point[LINE_TYPE]]]] = {}
        self.bsp_store_flat_dict: Dict[int, CBS_Point[LINE_TYPE]] = {}

        self.bsp1_list: List[CBS_Point[LINE_TYPE]] = []
        self.bsp1_dict: Dict[int, CBS_Point[LINE_TYPE]] = {}

        self.config = bs_point_config
        self.last_sure_pos = -1
        self.last_sure_seg_idx = 0

    def store_add_bsp(self, bsp_type: BSP_TYPE, bsp: CBS_Point[LINE_TYPE]):
        """
        把一个买卖点登记进"按类型分桶"的存储。

        Args:
            bsp_type: 登记在哪个类型桶下（一个买卖点只登记一次，取它首次命中的类型）
            bsp: 待登记的买卖点

        Note:
            下标直接吃布尔值：bsp.is_buy 为 False 时取元组第 0 项（卖点列表），
            True 时取第 1 项（买点列表），所以元组的顺序是 (卖点列表, 买点列表)。
        """
        if bsp_type not in self.bsp_store_dict:
            self.bsp_store_dict[bsp_type] = ([], [])
        # 同一桶内必须按笔序递增，bsp_iter_v2 的多路归并依赖这个前提
        if len(self.bsp_store_dict[bsp_type][bsp.is_buy]) > 0:
            assert self.bsp_store_dict[bsp_type][bsp.is_buy][-1].bi.idx < bsp.bi.idx, f"{bsp_type}, {bsp.is_buy} {self.bsp_store_dict[bsp_type][bsp.is_buy][-1].bi.idx} {bsp.bi.idx}"
        self.bsp_store_dict[bsp_type][bsp.is_buy].append(bsp)
        self.bsp_store_flat_dict[bsp.bi.idx] = bsp

    def add_bsp1(self, bsp: CBS_Point[LINE_TYPE]):
        """
        单独登记一个1类买卖点（T1/T1P）。

        Args:
            bsp: 1类买卖点

        Note:
            1类要单独存一份，是因为2类/3类都要"回溯对应的1类"（bsp1_dict 按笔 idx 反查），
            而且1类里还有一类"非目标买卖点"（没通过背驰/极值检查、只是用于内部串联的），
            它们不会进 bsp_store_dict，只存在于这里。
        """
        if len(self.bsp1_list) > 0:
            assert self.bsp1_list[-1].bi.idx < bsp.bi.idx
        self.bsp1_list.append(bsp)
        self.bsp1_dict[bsp.bi.idx] = bsp

    def clear_store_end(self):
        """
        撤销"尾部失效"的买卖点（分桶存储那一侧）。

        Note:
            为什么需要撤销：每来一根新K线，尾部的线段/中枢都可能被重算，
            原先推导出来的买卖点可能压根不该存在、或者会挪位置；
            如果不先撤掉就重算，会残留一批"幽灵买卖点"。

            撤销到哪为止：从每个桶的尾部往前删，直到遇到"结束K线 idx <= last_sure_pos"
            的买卖点为止 —— 那之前的都已被确认线段定稿，不会再变，必须保留，
            否则同一个买卖点会被反复删除重建（而且1类的 add_bsp1 断言会炸）。
        """
        for bsp_list in self.bsp_store_dict.values():
            for is_buy in [True, False]:
                while len(bsp_list[is_buy]) > 0:
                    if bsp_list[is_buy][-1].bi.get_end_klu().idx <= self.last_sure_pos:
                        break
                    del self.bsp_store_flat_dict[bsp_list[is_buy][-1].bi.idx]
                    # 同时把失效买卖点从Bi删除
                    bsp_list[is_buy][-1].bi.bsp = None
                    bsp_list[is_buy].pop()

    def clear_bsp1_end(self):
        """
        撤销"尾部失效"的1类买卖点（bsp1_list 那一侧），规则与 clear_store_end 完全一致。

        Note:
            调用顺序有讲究：cal() 先 clear_store_end 再 clear_bsp1_end。
            目标买卖点在两步里都会被撤（它同时存在于两个索引），
            而它的 bi.bsp 反向指针是在 clear_store_end 里摘掉的，
            所以这里不必再摘一次。
        """
        while len(self.bsp1_list) > 0:
            if self.bsp1_list[-1].bi.get_end_klu().idx <= self.last_sure_pos:
                break
            del self.bsp1_dict[self.bsp1_list[-1].bi.idx]
            self.bsp1_list.pop()

    def bsp_iter(self) -> Iterable[CBS_Point[LINE_TYPE]]:
        """
        按"类型桶"顺序遍历全部买卖点（每个桶内先买后卖，各桶按插入顺序）。

        Yields:
            CBS_Point: 每一个已登记的目标买卖点

        Note:
            这里的顺序是"按类型分桶"，不是时间序，画图/回测要时间序请用 getSortedBspList()。
        """
        for bsp_list in self.bsp_store_dict.values():
            yield from bsp_list[True]
            yield from bsp_list[False]

    def bsp_iter_v2(self) -> Iterable[CBS_Point[LINE_TYPE]]:
        """
        按笔 idx 从新到旧归并遍历全部买卖点（多桶 k 路归并）。

        Yields:
            CBS_Point: 从最新一个买卖点开始，逐个往前产出

        Note:
            每一轮线性扫描所有桶的当前游标，挑出 bi.idx 最大的那个产出并把它的游标前移，
            复杂度 O(桶数 × 总数)；它依赖"每个桶内部按 idx 递增"这一前提
            （由 store_add_bsp 里的断言保证）。之所以要归并而不是排序，
            是因为 get_latest_bsp 常常只取最近几个，没必要把全部结果排一遍。
        """
        # 每个元素 = [类型, 是否买点, 该桶当前游标]；游标从桶尾（最新）起步，逐轮往前挪
        list_indices = []
        for bsp_type, bsp_list in self.bsp_store_dict.items():
            if bsp_list[True]:
                list_indices.append([bsp_type, True, len(bsp_list[True]) - 1])
            if bsp_list[False]:
                list_indices.append([bsp_type, False, len(bsp_list[False]) - 1])

        # 每轮挑出"当前笔序号最大"的那个桶游标产出；游标走完(<0)就把该桶踢出归并
        while list_indices:
            max_idx = -1
            max_bi_idx = -1
            max_bsp = None

            for i, (bsp_type, is_buy, idx) in enumerate(list_indices):
                if idx >= 0:
                    bsp = self.bsp_store_dict[bsp_type][is_buy][idx]
                    if bsp.bi.idx > max_bi_idx:
                        max_bi_idx = bsp.bi.idx
                        max_idx = i
                        max_bsp = bsp

            if max_bsp is None:
                break

            yield max_bsp

            list_indices[max_idx][2] -= 1
            if list_indices[max_idx][2] < 0:
                list_indices.pop(max_idx)

    def __len__(self):
        """
        Returns:
            int: 当前持有的买卖点个数（= 去重后涉及到的笔数，等价于 len(bsp_store_flat_dict)）
        """
        return len(self.bsp_store_flat_dict)

    def cal(self, bi_list: LINE_LIST_TYPE, seg_list: CSegListComm[LINE_TYPE]):
        """
        计算入口：把买卖点重算到最新（幂等，可反复调用）。

        Args:
            bi_list: 笔（或线段）序列，买卖点最终落在其中的某根元素上
            seg_list: 线段（或线段段）序列，1/2/3类判定都是以"线段"为单位组织的

        Note:
            顺序不能换：
              1) 先撤销尾部失效的旧买卖点（两个索引都要清）；
              2) 再按 1类 -> 2类 -> 3类 的顺序算 —— 2/3类要靠 bsp1_dict 找对应的1类，
                 所以1类必须先算完；
              3) 最后更新定稿游标 last_sure_pos，供下一次重算判断"撤到哪为止"。
            每次都从 last_sure_seg_idx 之后的线段重算，已定稿的部分不重复劳动。
        """
        self.clear_store_end()
        self.clear_bsp1_end()
        self.cal_seg_bs1point(seg_list, bi_list)
        self.cal_seg_bs2point(seg_list, bi_list)
        self.cal_seg_bs3point(seg_list, bi_list)

        self.update_last_pos(seg_list)

    def update_last_pos(self, seg_list: CSegListComm):
        """
        更新定稿游标：从后往前找最后一个"确认线段"，把它的位置记下来。

        Args:
            seg_list: 线段序列

        Note:
            last_sure_pos 取的是"该线段终笔的起点K线 idx"而不是终点，
            也就是说最后一条确认线段的收尾部分仍被视为未定稿、每次都会重算
            （因为它的中枢/背驰还可能随新K线变化）。
            一个确认线段都找不到时保持 last_sure_pos=-1，表示全部都未定稿。
        """
        self.last_sure_pos = -1
        self.last_sure_seg_idx = 0
        seg_idx = len(seg_list)-1
        while seg_idx >= 0:
            seg = seg_list[seg_idx]
            if seg.is_sure:
                self.last_sure_pos = seg.end_bi.get_begin_klu().idx
                self.last_sure_seg_idx = seg.idx
                return
            seg_idx -= 1

    def seg_need_cal(self, seg: CSeg):
        """
        判断这条线段是否还需要重算买卖点。

        Args:
            seg: 待判断的线段

        Returns:
            bool: 线段的结束K线还在定稿线之后（未定稿）则为 True
        """
        return seg.end_bi.get_end_klu().idx > self.last_sure_pos

    def add_bs(
        self,
        bs_type: BSP_TYPE,
        bi: LINE_TYPE,
        relate_bsp1: Optional[CBS_Point],
        is_target_bsp: bool = True,
        feature_dict=None,
    ):
        """
        登记（或合并）一个买卖点 —— 全模块唯一的"去重 + 入库"入口。

        Args:
            bs_type: 本次命中的买卖点类型
            bi: 买卖点所在的那根笔/线段
            relate_bsp1: 对应的1类买卖点（2/3类传进来，1类传 None）
            is_target_bsp: 调用方给出的"资格"判定（中枢个数够不够、背驰成不成立…）
            feature_dict: 本类买卖点的专属特征，会合并进该买卖点的特征表

        Note:
            处理顺序（每一步都可能提前 return）：
              1) 买/卖由笔方向推出，不查配置；
              2) 若这根笔上已有买卖点，就只"叠加类型 + 合并特征"后返回 —— 这就是去重，
                 保证"一根笔一个实例"，也保证 bi.bsp 反向指针不会被后来者悄悄替换；
              3) 类型不在配置的 target_types 里 -> 降级成"非目标买卖点"；
              4) 非目标且不是1类 -> 直接丢弃（非目标的2/3类没有存在意义）；
              5) 目标买卖点进分桶存储；非目标的要把 bi.bsp 摘掉，避免污染笔上的展示；
              6) 1类（T1/T1P）无论是否目标，都额外登记进 bsp1_list / bsp1_dict。
        """
        # 向下笔结束在低点 -> 买点；向上笔结束在高点 -> 卖点
        is_buy = bi.is_down()
        # 同一根笔上已经有买卖点了：只叠加类型与特征，不新建对象
        if exist_bsp := self.bsp_store_flat_dict.get(bi.idx):
            assert exist_bsp.is_buy == is_buy
            exist_bsp.add_another_bsp_prop(bs_type, relate_bsp1)
            if feature_dict is not None:
                exist_bsp.add_feat(feature_dict)
            return
        # 用户没要这类买卖点 -> 降级；仍可能被建出来（1类），但只作内部串联用
        if bs_type not in self.config.GetBSConfig(is_buy).target_types:
            is_target_bsp = False

        if is_target_bsp or bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P]:
            bsp = CBS_Point[LINE_TYPE](
                bi=bi,
                is_buy=is_buy,
                bs_type=bs_type,
                relate_bsp1=relate_bsp1,
                feature_dict=feature_dict,
            )
        else:
            return
        if is_target_bsp:
            self.store_add_bsp(bs_type, bsp)
        else:
            bsp.bi.bsp = None
        if bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P]:
            self.add_bsp1(bsp)

    def cal_seg_bs1point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        """
        逐条线段算1类买卖点（T1 趋势背驰 / T1P 盘整背驰）。

        Args:
            seg_list: 线段序列
            bi_list: 笔（或线段）序列，盘整背驰要用到前一同向笔

        Note:
            只从 last_sure_seg_idx 起遍历，且跳过已经定稿的线段（seg_need_cal）。
        """
        for seg in seg_list[self.last_sure_seg_idx:]:
            if not self.seg_need_cal(seg):
                continue
            self.cal_single_bs1point(seg, bi_list)

    def cal_single_bs1point(self, seg: CSeg[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        """
        算单条线段上的1类买卖点：先决定"够不够格"，再分派到趋势背驰或盘整背驰。

        Args:
            seg: 当前线段（注意：向下线段对应买点，所以配置按 seg.is_down() 取）
            bi_list: 笔（或线段）序列

        Note:
            第一道门槛是中枢个数：min_zs_cnt <= 0 表示不做限制，否则线段内中枢数要达标
            （bsp1_only_multibi_zs 决定"数全部中枢"还是"只数非一笔中枢"）。
            中枢不够格时照样会把1类算出来，但标成非目标买卖点（只用于内部串联）。
        """
        BSP_CONF = self.config.GetBSConfig(seg.is_down())
        zs_cnt = seg.get_multi_bi_zs_cnt() if BSP_CONF.bsp1_only_multibi_zs else len(seg.zs_lst)
        is_target_bsp = (BSP_CONF.min_zs_cnt <= 0 or zs_cnt >= BSP_CONF.min_zs_cnt)
        # 走"趋势背驰1类"要同时满足四个条件：
        # 1) 线段里得有中枢；
        # 2) 最后一个中枢不能是一笔中枢（一笔中枢没有"进中枢/出中枢"的结构，没法比背驰）；
        # 3) 线段末笔就是离开中枢的那一笔（bi_out 还没定时，退而用中枢内最后一根笔的编号判断）；
        # 4) 末笔与进中枢那笔至少隔 2 根笔 —— 保证是"进中枢 -> 中枢 -> 出中枢"的完整结构。
        # 任一条不满足就退化成"盘整背驰1类"：拿末笔和前一同向笔直接比力度。
        if len(seg.zs_lst) > 0 and \
           not seg.zs_lst[-1].is_one_bi_zs() and \
           ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[-1].idx >= seg.end_bi.idx) \
           and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx > 2:
            self.treat_bsp1(seg, BSP_CONF, is_target_bsp)
        else:
            self.treat_pz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp)

    def treat_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, is_target_bsp: bool):
        """
        趋势背驰1类买卖点（T1）：末笔突破线段最后一个中枢，且相对"进中枢那笔"背驰。

        Args:
            seg: 当前线段，买卖点落在 seg.end_bi 上
            BSP_CONF: 该方向（买/卖）的配置
            is_target_bsp: 调用方算出的中枢个数资格

        Note:
            两道附加门槛，任一不满足就降级为"非目标买卖点"（照样建出来，但只作内部串联）：
              1) bs1_peak：要求出中枢那笔确实是这段行情的极值（CZS.out_bi_is_peak）；
              2) 背驰：CZS.is_divergence 判"突破中枢"与"出/进两笔力度比 <= divergence_rate"。
            特征里记了 divergence_rate（实际力度比）与 zs_cnt（线段内中枢个数）。
        """
        last_zs = seg.zs_lst[-1]
        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if BSP_CONF.bs1_peak and not break_peak:
            is_target_bsp = False
        is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)
        if not is_diver:
            is_target_bsp = False
        feature_dict = {
            'divergence_rate': divergence_rate,
            'zs_cnt': len(seg.zs_lst),
        }
        self.add_bs(bs_type=BSP_TYPE.T1, bi=seg.end_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def treat_pz_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, bi_list: LINE_LIST_TYPE, is_target_bsp):
        """
        盘整背驰1类买卖点（T1P）：中枢不够格时，改用"末笔 vs 前一同向笔"直接比力度。

        Args:
            seg: 当前线段
            BSP_CONF: 该方向（买/卖）的配置
            bi_list: 笔（或线段）序列，用来取前一同向笔
            is_target_bsp: 调用方算出的中枢个数资格

        Note:
            四道前置条件（不满足就直接放弃，连非目标买卖点都不建）：
              1) 末笔与前一同向笔必须同属一条线段（跨段不成盘整）；
              2) 末笔方向必须与线段方向一致（线段末笔本来就应与线段同向）；
              3) 向下笔必须创新低 / 向上笔必须创新高，否则谈不上"背驰"；
              4) 力度比 out_metric/in_metric 要 <= divergence_rate。
            比力度时 in_metric 正序算、out_metric 倒序算（is_reverse=True），
            这样两端取的都靠近"两笔交界处"，可比性最好。
        """
        last_bi = seg.end_bi
        # 取前一根"同向"笔：笔序上要隔一根（idx-2），与末笔方向相同
        pre_bi = bi_list[last_bi.idx-2]
        if last_bi.seg_idx != pre_bi.seg_idx:
            return
        if last_bi.dir != seg.dir:
            return
        # 这两行是在要求"必须创新低/创新高"：没创新低（低点比前一同向笔还高）就不成立
        if last_bi.is_down() and last_bi._low() > pre_bi._low():  # 创新低
            return
        if last_bi.is_up() and last_bi._high() < pre_bi._high():  # 创新高
            return
        in_metric = pre_bi.cal_macd_metric(BSP_CONF.macd_algo, is_reverse=False)
        out_metric = last_bi.cal_macd_metric(BSP_CONF.macd_algo, is_reverse=True)
        is_diver, divergence_rate = out_metric <= BSP_CONF.divergence_rate*in_metric, out_metric/(in_metric+1e-7)
        if not is_diver:
            is_target_bsp = False
        # 纯粹为了让类型检查器在"笔级"分支里把类型收窄成 CBi，运行时没有副作用
        if isinstance(bi_list, CBiList):
            assert isinstance(last_bi, CBi) and isinstance(pre_bi, CBi)
        feature_dict = {
            'divergence_rate': divergence_rate,
            'bsp1_bi_amp': last_bi.amp(),
        }
        self.add_bs(bs_type=BSP_TYPE.T1P, bi=last_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def cal_seg_bs2point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        """
        逐条线段算2类买卖点（T2）与类二买卖点（T2S）。

        Args:
            seg_list: 线段序列
            bi_list: 笔（或线段）序列

        Note:
            先按配置过滤：用户既没要 T2 也没要 T2S 的线段直接跳过，省掉后面一堆判定。
        """
        for seg in seg_list[self.last_sure_seg_idx:]:
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T2 not in config.target_types and BSP_TYPE.T2S not in config.target_types:
                continue
            if not self.seg_need_cal(seg):
                continue
            self.treat_bsp2(seg, seg_list, bi_list)

    def treat_bsp2(self, seg: CSeg, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        """
        2类买卖点（T2）：1类之后的第一根回撤笔，回撤幅度不超过 max_bs2_rate。

        Args:
            seg: 当前线段
            seg_list: 线段序列（只有一条线段时要走退化分支）
            bi_list: 笔（或线段）序列

        Note:
            两个分支的"角色"不一样，取配置的方向也相反：
              - 有多条线段时：1类就是本线段的终笔（seg.end_bi），
                它后面第 1 根笔是"突破笔"（break_bi，与1类同向延续），
                第 2 根笔是"回撤笔"（bsp2_bi，方向相反 -> 买点/卖点由它决定），
                所以配置按 seg.is_down() 取（回撤笔与线段同向）。
              - 只有一条线段时（行情刚开始、还没有1类可依）：
                退化成用 bi_list[0] 当突破笔、bi_list[1] 当回撤笔，
                此时回撤笔与线段反向，所以配置要按 seg.is_up() 取才对得上。
            回撤比例 = 回撤笔振幅 / 突破笔振幅，<= max_bs2_rate 才算2类。
            若2类没成立且 bsp2s_follow_2=True，就直接放弃继续找类二。
        """
        if len(seg_list) > 1:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            bsp1_bi = seg.end_bi
            real_bsp1 = self.bsp1_dict.get(bsp1_bi.idx)
            # 后面至少还得有两根笔（突破笔 + 回撤笔）才谈得上2类
            if bsp1_bi.idx + 2 >= len(bi_list):
                return
            break_bi = bi_list[bsp1_bi.idx + 1]
            bsp2_bi = bi_list[bsp1_bi.idx + 2]
        else:
            BSP_CONF = self.config.GetBSConfig(seg.is_up())
            bsp1_bi, real_bsp1 = None, None
            if len(bi_list) == 1:
                return
            bsp2_bi = bi_list[1]
            break_bi = bi_list[0]
        # bsp2_follow_1：1类那笔上必须真有一个已入库的买卖点，否则不认这个2类
        if BSP_CONF.bsp2_follow_1 and (not bsp1_bi or bsp1_bi.idx not in self.bsp_store_flat_dict):
            return
        retrace_rate = bsp2_bi.amp()/break_bi.amp()
        bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate
        if bsp2_flag:
            feature_dict = {
                'bsp2_retrace_rate': retrace_rate,
                'bsp2_break_bi_amp': break_bi.amp(),
                'bsp2_bi_amp': bsp2_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T2, bi=bsp2_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore
        elif BSP_CONF.bsp2s_follow_2:
            # 2类没成立，且配了"类二必须跟在2类之后" —— 就此打住，不再往后找类二
            return
        if BSP_TYPE.T2S not in self.config.GetBSConfig(seg.is_down()).target_types:
            return
        self.treat_bsp2s(seg_list, bi_list, bsp2_bi, break_bi, real_bsp1, BSP_CONF)  # type: ignore

    def treat_bsp2s(
        self,
        seg_list: CSegListComm,
        bi_list: LINE_LIST_TYPE,
        bsp2_bi: LINE_TYPE,
        break_bi: LINE_TYPE,
        real_bsp1: Optional[CBS_Point],
        BSP_CONF: CPointConfig,
    ):
        """
        类二买卖点（T2S）：2类之后，仍在同一线段内、且区间与2类那笔有重叠的后续同向笔。

        Args:
            seg_list: 线段序列，用来判断"还在不在这条线段里"
            bi_list: 笔（或线段）序列
            bsp2_bi: 2类所在的那根笔，类二以它为基准往后找
            break_bi: 1类后面那根突破笔，回撤比例以它的振幅为分母
            real_bsp1: 对应的1类买卖点
            BSP_CONF: 该方向（买/卖）的配置

        Note:
            从 bias=2 起每隔一根笔取一根（步长 2 保证取到的都是与2类同向的笔），逐个判定：
              - 层级 lv = bias/2，超过 max_bsp2s_lv 就停；
              - 一旦跳出当前线段（且不是"刚跨到下一段尾部"这种可接受的跨界）就停；
              - 区间必须还与2类那笔（或此前累积下来的重叠区间）有重叠，否则说明已经走远；
              - 已经跌破/涨破突破笔端点的就不再是回撤，停；
              - 回撤比例（端点价差 / 突破笔振幅）超过 max_bs2_rate 就停。
            每根满足条件的笔都会被登记成一个类二买卖点，层级记在特征 bsp2s_lv 里。
        """
        # bias 是相对2类那笔的偏移量，步长 2 保证取到的都是与2类同向的笔；层级 lv = bias/2
        bias = 2
        _low, _high = None, None
        while bsp2_bi.idx + bias < len(bi_list):  # 计算类二
            bsp2s_bi = bi_list[bsp2_bi.idx + bias]
            assert bsp2s_bi.seg_idx is not None and bsp2_bi.seg_idx is not None
            if BSP_CONF.max_bsp2s_lv is not None and bias/2 > BSP_CONF.max_bsp2s_lv:
                break
            # 跨段判定：只允许"刚好跨到下一段最末尾"这种情形继续；跨得更远、或原线段已确认就停
            if bsp2s_bi.seg_idx != bsp2_bi.seg_idx and (bsp2s_bi.seg_idx < len(seg_list)-1 or bsp2s_bi.seg_idx - bsp2_bi.seg_idx >= 2 or seg_list[bsp2_bi.seg_idx].is_sure):
                break
            if bias == 2:
                # 第一层：拿类二笔与2类笔求交集，作为后续各层比较的"基准重叠区间"
                if not has_overlap(bsp2_bi._low(), bsp2_bi._high(), bsp2s_bi._low(), bsp2s_bi._high()):
                    break
                _low = max([bsp2_bi._low(), bsp2s_bi._low()])
                _high = min([bsp2_bi._high(), bsp2s_bi._high()])
            elif not has_overlap(_low, _high, bsp2s_bi._low(), bsp2s_bi._high()):
                # 后续各层：必须还与前面累积下来的重叠区间有交集
                break

            # 已经跌破/涨破突破笔的端点 —— 那是新一轮趋势，不是回撤
            if bsp2s_break_bsp1(bsp2s_bi, break_bi):
                break
            # 类二的回撤比例按"端点价差"算（与2类那笔的"振幅比"是两种不同口径）
            retrace_rate = abs(bsp2s_bi.get_end_val()-break_bi.get_end_val())/break_bi.amp()
            if retrace_rate > BSP_CONF.max_bs2_rate:
                break
            feature_dict = {
                'bsp2s_retrace_rate': retrace_rate,
                'bsp2s_break_bi_amp': break_bi.amp(),
                'bsp2s_bi_amp': bsp2s_bi.amp(),
                'bsp2s_lv': bias/2,
            }
            self.add_bs(bs_type=BSP_TYPE.T2S, bi=bsp2s_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore
            bias += 2

    def cal_seg_bs3point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        """
        逐条线段算3类买卖点：T3A（中枢在1类之后）+ T3B（中枢在1类之前）。

        Args:
            seg_list: 线段序列
            bi_list: 笔（或线段）序列

        Note:
            3类买卖点强依赖中枢，所以两条路径都要先拿到"参照中枢"：
              - T3A 走 treat_bsp3_after：看下一段（next_seg）里的中枢；
              - T3B 走 treat_bsp3_before：看本段（seg）最后一个中枢。
            退化情形：只有一条线段时没有"下一段"也没有1类，
            bsp1_bi 置 None —— 若 bsp3_follow_1=True（默认）会直接被跳过，
            只有关掉 bsp3_follow_1 才会真的在这条唯一线段上找3类。
        """
        for seg in seg_list[self.last_sure_seg_idx:]:
            if not self.seg_need_cal(seg):
                continue
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T3A not in config.target_types and BSP_TYPE.T3B not in config.target_types:
                continue
            if len(seg_list) > 1:
                bsp1_bi = seg.end_bi
                bsp1_bi_idx = bsp1_bi.idx
                BSP_CONF = self.config.GetBSConfig(seg.is_down())
                real_bsp1 = self.bsp1_dict.get(bsp1_bi.idx)
                next_seg_idx = seg.idx+1
                # next_seg 可能为 None（本段就是最后一段），所以 next_seg_idx 未必等于 next_seg.idx
                next_seg = seg.next  # 可能为None, 所以并不一定可以保证next_seg_idx == next_seg.idx
            else:
                next_seg = seg
                next_seg_idx = seg.idx
                bsp1_bi, real_bsp1 = None, None
                bsp1_bi_idx = -1
                BSP_CONF = self.config.GetBSConfig(seg.is_up())
            if BSP_CONF.bsp3_follow_1 and (not bsp1_bi or bsp1_bi.idx not in self.bsp_store_flat_dict):
                continue
            if next_seg:
                self.treat_bsp3_after(seg_list, next_seg, BSP_CONF, bi_list, real_bsp1, bsp1_bi_idx, next_seg_idx)
            self.treat_bsp3_before(seg_list, seg, next_seg, bsp1_bi, BSP_CONF, bi_list, real_bsp1, next_seg_idx)

    def treat_bsp3_after(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        next_seg: CSeg[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp1,
        bsp1_bi_idx,
        next_seg_idx
    ):
        """
        3A 类买卖点（T3A）：中枢在1类"之后" —— 下一段里的中枢被突破后，回抽不回中枢的那一笔。

        Args:
            seg_list: 线段序列
            next_seg: 1类所在线段的下一段（只有一条线段时退化成它自己）
            BSP_CONF: 该方向（买/卖）的配置
            bi_list: 笔（或线段）序列
            real_bsp1: 对应的1类买卖点
            bsp1_bi_idx: 1类那根笔的 idx
            next_seg_idx: 下一段的预期下标

        Note:
            判定链条（任一环不满足就跳过这个中枢，继续看下一个）：
              1) 下一段里必须有"非一笔中枢"；
              2) strict_bsp3 时要求该中枢的进笔正好是1类那笔的后一根（中枢必须紧贴1类）；
              3) 只考察前 bsp3a_max_zs_cnt 个中枢，且中枢必须有出笔、出笔之后还得有笔；
              4) 回抽笔（中枢出笔的下一根）要与下一段反向，且归属检查通过；
              5) 回抽笔不能又回到中枢区间里（bsp3_back2zs）—— 这是3类的核心定义；
              6) bsp3_peak 打开时还要求它突破中枢极值（bsp3_break_zspeak）。
            特征里记了中枢相对高度 (high-low)/low 与回抽笔振幅。
        """
        first_zs = next_seg.get_first_multi_bi_zs()
        if first_zs is None:
            return
        if BSP_CONF.strict_bsp3 and first_zs.get_bi_in().idx != bsp1_bi_idx+1:
            return

        # 注意：这里按 next_seg 的方向另取了一份配置来读 bsp3a_max_zs_cnt，
        # 与入参 BSP_CONF（按前一段方向取的）通常不是同一份买/卖配置
        config = self.config.GetBSConfig(next_seg.is_down())
        bsp3a_max_zs_cnt = config.bsp3a_max_zs_cnt
        for zs_idx, zs in enumerate(next_seg.get_multi_bi_zs_lst()):
            if zs_idx >= bsp3a_max_zs_cnt:
                break
            # 中枢还没出笔，或出笔后面没有笔了 —— 找不到回抽笔
            if zs.bi_out is None or zs.bi_out.idx+1 >= len(bi_list):
                break
            bsp3_bi = bi_list[zs.bi_out.idx+1]
            if bsp3_bi.parent_seg is None:
                # 回抽笔还没归属任何线段：只有当下一段就是最后一段（尾部未定稿）时才容忍
                if next_seg.idx != len(seg_list)-1:
                    break
            elif bsp3_bi.parent_seg.idx != next_seg.idx:
                # 回抽笔落到了别的线段上：只有那个段还不足3根笔（只是过渡段）时才容忍
                if len(bsp3_bi.parent_seg.bi_list) >= 3:
                    break
            # 必须与下一段反向：3类是"回抽"，同向就成了趋势延续
            if bsp3_bi.dir == next_seg.dir:
                break
            # 段号对不上时，只有已经走到最后两段（尾部尚未定稿）才容忍
            if bsp3_bi.seg_idx != next_seg_idx and next_seg_idx < len(seg_list)-2:
                break
            # 回抽又跌回/涨回中枢区间里 —— 不构成3类，换下一个中枢再看
            if bsp3_back2zs(bsp3_bi, zs):
                continue
            bsp3_peak_zs = bsp3_break_zspeak(bsp3_bi, zs)
            if BSP_CONF.bsp3_peak and not bsp3_peak_zs:
                continue
            feature_dict = {
                'bsp3_zs_height': (zs.high - zs.low)/zs.low,
                'bsp3_bi_amp': bsp3_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T3A, bi=bsp3_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore

    def treat_bsp3_before(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        seg: CSeg[LINE_TYPE],
        next_seg: Optional[CSeg[LINE_TYPE]],
        bsp1_bi: Optional[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp1,
        next_seg_idx
    ):
        """
        3B 类买卖点（T3B）：中枢在1类"之前" —— 本段最后一个中枢之后的回抽笔。

        Args:
            seg_list: 线段序列
            seg: 1类所在的线段，参照中枢取自它（本段最后一个非一笔中枢）
            next_seg: 下一条线段，用来确定回抽笔的考察上界；None 表示没有下一段
            bsp1_bi: 1类那根笔；为 None 时直接放弃（没有1类就无从谈起3类）
            BSP_CONF: 该方向（买/卖）的配置
            bi_list: 笔（或线段）序列
            real_bsp1: 对应的1类买卖点
            next_seg_idx: 下一段的预期下标

        Note:
            与 T3A 的区别：这里的中枢在1类之前，回抽笔从1类那笔之后隔一根开始找
            （步长 2 => 取的都是与1类同向的笔），上界由 cal_bsp3_bi_end_idx(next_seg) 给出，
            并且只取第一个满足条件的笔（命中即 break）。
            另外这里没有 T3A 那道 bsp3_peak（突破中枢极值）门槛。
        """
        cmp_zs = seg.get_final_multi_bi_zs()
        if cmp_zs is None:
            return
        if not bsp1_bi:
            return
        if BSP_CONF.strict_bsp3 and (cmp_zs.bi_out is None or cmp_zs.bi_out.idx != bsp1_bi.idx):
            return
        # 回抽笔的考察上界：没有下一段（或下一段还没中枢且是最后一段）时是不受限的 inf
        end_bi_idx = cal_bsp3_bi_end_idx(next_seg)
        # 从1类那笔之后隔一根取一根 —— 取到的都是与1类同向的笔
        for bsp3_bi in bi_list[bsp1_bi.idx+2::2]:
            if bsp3_bi.idx > end_bi_idx:
                break
            assert bsp3_bi.seg_idx is not None
            # 段号对不上、而且还没走到最末尾那段（尾部未定稿区）-> 停
            if bsp3_bi.seg_idx != next_seg_idx and bsp3_bi.seg_idx < len(seg_list)-1:
                break
            # 回抽又回到中枢区间里就不算3类，继续看下一根同向笔
            if bsp3_back2zs(bsp3_bi, cmp_zs):  # type: ignore
                continue
            feature_dict = {
                'bsp3_zs_height': (cmp_zs.high - cmp_zs.low)/cmp_zs.low,
                'bsp3_bi_amp': bsp3_bi.amp(),
            }
            self.add_bs(bs_type=BSP_TYPE.T3B, bi=bsp3_bi, relate_bsp1=real_bsp1, feature_dict=feature_dict)  # type: ignore
            # 3B 只取第一个满足条件的回抽笔
            break

    def getSortedBspList(self) -> List[CBS_Point[LINE_TYPE]]:
        """
        取全部买卖点，按时间（笔 idx 升序）排好序。

        Returns:
            List[CBS_Point]: 从旧到新的买卖点列表

        Note:
            bsp_iter() 是按类型分桶产出的，顺序没有时间含义，所以这里必须再排一次序。
        """
        return sorted(self.bsp_iter(), key=lambda bsp: bsp.bi.idx)

    def get_latest_bsp(self, number: int) -> List[CBS_Point[LINE_TYPE]]:
        """
        取最近的若干个买卖点。

        Args:
            number: 要取几个；0 表示取全部

        Returns:
            List[CBS_Point]: 从最新到最旧排序的买卖点列表（最多 number 个）
        """
        res = []
        for bsp in self.bsp_iter_v2():
            res.append(bsp)
            if number != 0 and len(res) >= number:
                break
        return res


def bsp2s_break_bsp1(bsp2s_bi: LINE_TYPE, bsp2_break_bi: LINE_TYPE) -> bool:
    """
    判断类二那笔是否已经"跌破/涨破"了突破笔的端点。

    Args:
        bsp2s_bi: 待判定的类二笔
        bsp2_break_bi: 1类后面那根突破笔

    Returns:
        bool: True 表示已经破掉端点 —— 那是新一轮趋势而不是回撤，应停止继续往后找类二

    Note:
        向下笔比最低点、向上笔比最高点；都不取绝对值，方向语义由 is_down/is_up 保证。
    """
    return (bsp2s_bi.is_down() and bsp2s_bi._low() < bsp2_break_bi._low()) or \
           (bsp2s_bi.is_up() and bsp2s_bi._high() > bsp2_break_bi._high())


def bsp3_back2zs(bsp3_bi: LINE_TYPE, zs: CZS) -> bool:
    """
    判断回抽笔是否又回到了中枢区间里（3类买卖点的核心否决条件）。

    Args:
        bsp3_bi: 回抽笔
        zs: 参照中枢

    Returns:
        bool: True 表示"回到中枢里"，该笔不构成3类买卖点

    Note:
        门槛刻意不对称：向下笔只要最低点还没站上中枢上沿（low < zs.high）就算回了中枢，
        向上笔只要最高点还没跌破中枢下沿（high > zs.low）就算 —— 这正是缠论里
        "3买/3卖要求次级别回抽不触及中枢区间"的判定。
    """
    return (bsp3_bi.is_down() and bsp3_bi._low() < zs.high) or (bsp3_bi.is_up() and bsp3_bi._high() > zs.low)


def bsp3_break_zspeak(bsp3_bi: LINE_TYPE, zs: CZS) -> bool:
    """
    判断这一笔是否突破了中枢的"极值区间"（peak_low/peak_high，比中枢区间 [low, high] 更宽）。

    Args:
        bsp3_bi: 待判定的笔
        zs: 参照中枢

    Returns:
        bool: True 表示突破（向下笔的起点不低于中枢最高点 / 向上笔的起点不高于中枢最低点）

    Note:
        这是 bsp3_peak 打开时附加在 T3A 上的门槛：它要求回抽笔"从中枢另一侧之外出发"，
        即整段回抽都发生在中枢的一侧，属于更强势的3类。用 >= / <= 是允许端点相接。
    """
    return (bsp3_bi.is_down() and bsp3_bi._high() >= zs.peak_high) or (bsp3_bi.is_up() and bsp3_bi._low() <= zs.peak_low)


def cal_bsp3_bi_end_idx(seg: Optional[CSeg[LINE_TYPE]]):
    """
    算 T3B 回抽笔的考察上界（笔 idx）。

    Args:
        seg: 下一条线段；None 表示当前段已经是最后一段

    Returns:
        int 或 float("inf")：回抽笔 idx 的上界（含）；inf 表示不限制

    Note:
        两种"不限制"的情形：没有下一段；下一段还没有非一笔中枢、且它自己是最后一段
        （此时中枢还没走出来，任何有限上界都会误伤）。
        否则默认上界是"下一段终笔的前一根笔"，再按下一段里第一个有出笔的中枢把上界
        收紧到该中枢的出笔 —— 意思是"中枢被突破之后的回抽"才可能是3B。
    """
    if not seg:
        return float("inf")
    if seg.get_multi_bi_zs_cnt() == 0 and seg.next is None:
        return float("inf")
    # 默认上界：下一段终笔的前一根（-1 是因为终笔本身与线段同向，不可能是回抽笔）
    end_bi_idx = seg.end_bi.idx-1
    for zs in seg.zs_lst:
        # 一笔中枢没有"进/出中枢"的结构，跳过
        if zs.is_one_bi_zs():
            continue
        if zs.bi_out is not None:
            end_bi_idx = zs.bi_out.idx
            break
    return end_bi_idx
