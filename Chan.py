import copy
import datetime
import pickle
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Union

from BuySellPoint.BS_Point import CBS_Point
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Common.func_util import check_kltype_order, kltype_lte_day
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_List import CKLine_List
from KLine.KLine_Unit import CKLine_Unit


class CChan:
    """
    缠论计算的顶层入口。

    职责链路：拉数据源 -> 按级别组装 CKLine_Unit -> 逐级喂给 CKLine_List 增量算笔/线段/中枢，
    并对外提供买卖点查询、逐步回放（step_load）和 pickle 持久化能力。
    """

    def __init__(
        self,
        code,
        begin_time=None,
        end_time=None,
        data_src: Union[DATA_SRC, str] = DATA_SRC.BAO_STOCK,
        lv_list=None,
        config=None,
        autype: AUTYPE = AUTYPE.QFQ,
    ):
        """
        Args:
            code: 标的代码，格式由数据源类决定（如 "sz.000001" / "HK.00700"）
            begin_time: 起始时间；datetime.date 会被转成字符串，None 表示不限
            end_time: 结束时间，规则同 begin_time
            data_src: 数据源，DATA_SRC 枚举 / "custom:包名.类名" 字符串
            lv_list: 级别列表，必须从大级别排到小级别，默认 [日线, 60分钟]
            config: CChanConfig 实例，None 则用默认配置
            autype: 复权方式（AUTYPE.QFQ/HFQ/NONE）

        Note:
            构造完即完成一次完整计算；若 config.trigger_step=True 则需外部
            再调 step_load()/trigger_load() 驱动。

        Raises:
            CChanException: 级别顺序非法，或最高级别一根数据都没取到（ErrCode.NO_DATA）
        """
        if lv_list is None:
            lv_list = [KL_TYPE.K_DAY, KL_TYPE.K_60M]
        check_kltype_order(lv_list)  # lv_list顺序从高到低
        self.code = code
        self.begin_time = str(begin_time) if isinstance(begin_time, datetime.date) else begin_time
        self.end_time = str(end_time) if isinstance(end_time, datetime.date) else end_time
        self.autype = autype
        self.data_src = data_src
        self.lv_list: List[KL_TYPE] = lv_list

        if config is None:
            config = CChanConfig()
        self.conf = config

        self.kl_misalign_cnt = 0
        self.kl_inconsistent_detail = defaultdict(list)

        self.g_kl_iter = defaultdict(list)

        self.do_init()

        if not config.trigger_step:
            for _ in self.load():
                ...

    def __deepcopy__(self, memo):
        """
        自定义深拷贝。

        K线/合并K线/笔/线段之间由 pre/next、sup_kl/sub_kl_list 双向互指，
        默认 deepcopy 会把同一个 Elements 复制成多份从而打断这些关系；
        这里先复用 memo 保证同 id 只拷一次，拷完再统一重建子K线到父K线的反向引用。

        Args:
            memo: deepcopy 的共享备忘录（id(原对象) -> 副本）

        Returns:
            CChan: 与原对象等价、链接关系完好的副本
        """
        cls = self.__class__
        obj: CChan = cls.__new__(cls)
        memo[id(self)] = obj
        obj.code = self.code
        obj.begin_time = self.begin_time
        obj.end_time = self.end_time
        obj.autype = self.autype
        obj.data_src = self.data_src
        obj.lv_list = copy.deepcopy(self.lv_list, memo)
        obj.conf = copy.deepcopy(self.conf, memo)
        obj.kl_misalign_cnt = self.kl_misalign_cnt
        obj.kl_inconsistent_detail = copy.deepcopy(self.kl_inconsistent_detail, memo)
        obj.g_kl_iter = copy.deepcopy(self.g_kl_iter, memo)
        if hasattr(self, 'klu_cache'):
            obj.klu_cache = copy.deepcopy(self.klu_cache, memo)
        if hasattr(self, 'klu_last_t'):
            obj.klu_last_t = copy.deepcopy(self.klu_last_t, memo)
        obj.kl_datas = {}
        for kl_type, ckline in self.kl_datas.items():
            obj.kl_datas[kl_type] = copy.deepcopy(ckline, memo)
        for kl_type, ckline in self.kl_datas.items():
            for klc in ckline:
                for klu in klc.lst:
                    assert id(klu) in memo
                    if klu.sup_kl:
                        memo[id(klu)].sup_kl = memo[id(klu.sup_kl)]
                    memo[id(klu)].sub_kl_list = [memo[id(sub_kl)] for sub_kl in klu.sub_kl_list]
        return obj

    def do_init(self):
        """
        为每个级别创建空的 CKLine_List 并重置 kl_datas。

        Note:
            step_load() 重跑前会再调一次，用来清空上一次的计算结果。
        """
        self.kl_datas: Dict[KL_TYPE, CKLine_List] = {}
        for idx in range(len(self.lv_list)):
            self.kl_datas[self.lv_list[idx]] = CKLine_List(self.lv_list[idx], conf=self.conf)

    def load_stock_data(self, stockapi_instance: CCommonStockApi, lv) -> Iterable[CKLine_Unit]:
        """
        把数据源吐出的原始K线包装成带 idx 和级别的 CKLine_Unit。

        Args:
            stockapi_instance: 已实例化的数据源对象（CCommonStockApi 子类）
            lv: 本批数据所属的 KL_TYPE

        Yields:
            CKLine_Unit: 已写入 kl_type，idx 从 0 起按读取顺序递增的K线单元
        """
        for KLU_IDX, klu in enumerate(stockapi_instance.get_kl_data()):
            klu.set_idx(KLU_IDX)
            klu.kl_type = lv
            yield klu

    def get_load_stock_iter(self, stockapi_cls, lv):
        """
        实例化数据源并拿到某个级别的K线迭代器。

        Args:
            stockapi_cls: 数据源类（通常来自 GetStockAPI）
            lv: KL_TYPE

        Returns:
            Iterable[CKLine_Unit]: 该级别的K线迭代器

        Raises:
            CChanException: 数据源实例化失败，如 ErrCode.SRC_DATA_NOT_FOUND
        """
        stockapi_instance = stockapi_cls(code=self.code, k_type=lv, begin_date=self.begin_time, end_date=self.end_time, autype=self.autype)
        return self.load_stock_data(stockapi_instance, lv)

    def add_lv_iter(self, lv_idx, iter):
        """
        登记某个级别的K线迭代器，同一级别可登记多个、按登记先后顺序消费。

        Args:
            lv_idx: 级别下标（int）或 KL_TYPE
            iter: CKLine_Unit 迭代器
        """
        if isinstance(lv_idx, int):
            self.g_kl_iter[self.lv_list[lv_idx]].append(iter)
        else:
            self.g_kl_iter[lv_idx].append(iter)

    def get_next_lv_klu(self, lv_idx):
        """
        取该级别的下一根K线；当前迭代器取完后自动切到同级别的下一个迭代器。

        Args:
            lv_idx: 级别下标（int）或 KL_TYPE

        Returns:
            CKLine_Unit

        Raises:
            StopIteration: 该级别所有迭代器都已耗尽
        """
        if isinstance(lv_idx, int):
            lv_idx = self.lv_list[lv_idx]
        if len(self.g_kl_iter[lv_idx]) == 0:
            raise StopIteration
        try:
            return self.g_kl_iter[lv_idx][0].__next__()
        except StopIteration:
            self.g_kl_iter[lv_idx] = self.g_kl_iter[lv_idx][1:]
            if len(self.g_kl_iter[lv_idx]) != 0:
                return self.get_next_lv_klu(lv_idx)
            else:
                raise

    def step_load(self):
        """
        回放模式：每喂入一根最高级别K线就对外产出一个快照，便于回测逐步推进。

        Yields:
            CChan: 每次推进后的自身快照，前 conf.skip_step 步会被跳过不产出

        Note:
            要求 conf.trigger_step=True；若一次都没产出，最后会兜底 yield 一次 self。
        """
        assert self.conf.trigger_step
        self.do_init()  # 清空数据，防止再次重跑没有数据
        yielded = False  # 是否曾经返回过结果
        for idx, snapshot in enumerate(self.load(self.conf.trigger_step)):
            if idx < self.conf.skip_step:
                continue
            yield snapshot
            yielded = True
        if not yielded:
            yield self

    def trigger_load(self, inp):
        """
        外部喂K线入口：不经数据源自拉，直接把已在内存的K线灌进来并一次性算完。

        Args:
            inp: Dict[KL_TYPE, List[CKLine_Unit]]，允许只给部分级别，但最高级别必须有数据

        Note:
            非回放模式（conf.trigger_step=False）下，全部喂完后会对每个级别补算一次线段和中枢。

        Raises:
            CChanException: 最高级别没有传入数据（ErrCode.NO_DATA）
        """
        # {type: [klu, ...]}
        if not hasattr(self, 'klu_cache'):
            self.klu_cache: List[Optional[CKLine_Unit]] = [None for _ in self.lv_list]
        if not hasattr(self, 'klu_last_t'):
            self.klu_last_t = [CTime(1980, 1, 1, 0, 0) for _ in self.lv_list]
        for lv_idx, lv in enumerate(self.lv_list):
            if lv not in inp:
                if lv_idx == 0:
                    raise CChanException(f"最高级别{lv}没有传入数据", ErrCode.NO_DATA)
                continue
            for klu in inp[lv]:
                klu.kl_type = lv
            assert isinstance(inp[lv], list)
            self.add_lv_iter(lv, iter(inp[lv]))
        for _ in self.load_iterator(lv_idx=0, parent_klu=None, step=False):
            ...
        if not self.conf.trigger_step:  # 非回放模式全部算完之后才算一次中枢和线段
            for lv in self.lv_list:
                self.kl_datas[lv].cal_seg_and_zs()

    def init_lv_klu_iter(self, stockapi_cls):
        """
        为每个级别创建取数迭代器，取不到数据的级别可按配置跳过。

        Args:
            stockapi_cls: 数据源类

        Returns:
            List[Iterable[CKLine_Unit]]: 与裁剪后的 self.lv_list 一一对应

        Note:
            捕获到 ErrCode.SRC_DATA_NOT_FOUND 且 conf.auto_skip_illegal_sub_lv=True 时，
            打印告警并把该级别从 kl_datas 与 lv_list 中剔除，从而支持"级别不齐"的数据源。
        """
        # 为了跳过一些获取数据失败的级别
        lv_klu_iter = []
        valid_lv_list = []
        for lv in self.lv_list:
            try:
                lv_klu_iter.append(self.get_load_stock_iter(stockapi_cls, lv))
                valid_lv_list.append(lv)
            except CChanException as e:
                if e.errcode == ErrCode.SRC_DATA_NOT_FOUND and self.conf.auto_skip_illegal_sub_lv:
                    if self.conf.print_warning:
                        print(f"[WARNING-{self.code}]{lv}级别获取数据失败，跳过")
                    del self.kl_datas[lv]
                    continue
                raise e
        self.lv_list = valid_lv_list
        return lv_klu_iter

    def GetStockAPI(self):
        """
        根据 data_src 解析出对应的取数类。

        Returns:
            CCommonStockApi 的子类；内置源延迟 import，避免无关依赖被提前引入。

        Raises:
            CChanException: 无法识别的数据源类型（ErrCode.SRC_DATA_TYPE_ERR）
        """
        _dict = {}
        if self.data_src == DATA_SRC.BAO_STOCK:
            from DataAPI.BaoStockAPI import CBaoStock
            _dict[DATA_SRC.BAO_STOCK] = CBaoStock
        elif self.data_src == DATA_SRC.CCXT:
            from DataAPI.ccxt import CCXT
            _dict[DATA_SRC.CCXT] = CCXT
        elif self.data_src == DATA_SRC.CSV:
            from DataAPI.csvAPI import CSV_API
            _dict[DATA_SRC.CSV] = CSV_API
        # elif self.data_src == DATA_SRC.AKSHARE:
        #     from DataAPI.AkshareAPI import CAkshare
        #     _dict[DATA_SRC.AKSHARE] = CAkshare
        elif self.data_src == DATA_SRC.FUTU:
            from DataAPI.FutuAPI import CFutu
            _dict[DATA_SRC.FUTU] = CFutu
        if self.data_src in _dict:
            return _dict[self.data_src]
        assert isinstance(self.data_src, str)
        if self.data_src.find("custom:") < 0:
            raise CChanException("load src type error", ErrCode.SRC_DATA_TYPE_ERR)
        package_info = self.data_src.split(":")[1]
        package_name, cls_name = package_info.split(".")
        import importlib
        module = importlib.import_module(f"DataAPI.{package_name}")
        return getattr(module, cls_name)

    def load(self, step=False):
        """
        主加载流程：初始化数据源 -> 登记各级迭代器 -> 逐级喂K线计算 -> 收尾算线段和中枢。

        Args:
            step: True 表示每喂一根最高级别K线就 yield 一次 self（回放模式）

        Yields:
            CChan: 仅 step=True 时有产出

        Raises:
            CChanException: 最高级别一根数据都没取到（ErrCode.NO_DATA）

        Note:
            无论中途是否抛错，最后都会调用数据源的 do_close() 释放连接。
        """
        stockapi_cls = self.GetStockAPI()
        try:
            stockapi_cls.do_init()
            for lv_idx, klu_iter in enumerate(self.init_lv_klu_iter(stockapi_cls)):
                self.add_lv_iter(lv_idx, klu_iter)
            self.klu_cache: List[Optional[CKLine_Unit]] = [None for _ in self.lv_list]
            self.klu_last_t = [CTime(1980, 1, 1, 0, 0) for _ in self.lv_list]

            yield from self.load_iterator(lv_idx=0, parent_klu=None, step=step)  # 计算入口
            if not step:  # 非回放模式全部算完之后才算一次中枢和线段
                for lv in self.lv_list:
                    self.kl_datas[lv].cal_seg_and_zs()
        except Exception:
            raise
        finally:
            stockapi_cls.do_close()
        if len(self[0]) == 0:
            raise CChanException("最高级别没有获得任何数据", ErrCode.NO_DATA)

    def set_klu_parent_relation(self, parent_klu, kline_unit, cur_lv, lv_idx):
        """
        建立父/子级别K线的双向引用，并在开启数据校验时先核对两者是否同一天。

        Args:
            parent_klu: 父级别的 CKLine_Unit
            kline_unit: 当前（子）级别的 CKLine_Unit
            cur_lv: 当前级别的 KL_TYPE
            lv_idx: 当前级别下标，用于取它在 lv_list 中的父级别

        Raises:
            CChanException: 日期不一致条目超限时由 check_kl_consitent 抛出
        """
        if self.conf.kl_data_check and kltype_lte_day(cur_lv) and kltype_lte_day(self.lv_list[lv_idx-1]):
            self.check_kl_consitent(parent_klu, kline_unit)
        parent_klu.add_children(kline_unit)
        kline_unit.set_parent(parent_klu)

    def add_new_kl(self, cur_lv: KL_TYPE, kline_unit):
        """
        把一根K线喂给对应级别的 CKLine_List —— 包含处理、笔/线段的增量更新都在这里触发。

        Args:
            cur_lv: 目标级别
            kline_unit: 待加入的 CKLine_Unit

        Raises:
            原样抛出计算过程中的异常；conf.print_err_time=True 时会先打印出错的K线时间。
        """
        try:
            self.kl_datas[cur_lv].add_single_klu(kline_unit)
        except Exception:
            if self.conf.print_err_time:
                print(f"[ERROR-{self.code}]在计算{kline_unit.time}K线时发生错误!")
            raise

    def try_set_klu_idx(self, lv_idx: int, kline_unit: CKLine_Unit):
        """
        给缺失 idx 的K线补下标：已有 idx(>=0) 的原样保留，否则接在同级最后一根之后。

        Args:
            lv_idx: 级别下标
            kline_unit: 待补 idx 的 CKLine_Unit

        Note:
            trigger_load 等外部喂数渠道送进来的K线通常没设 idx，靠这里兜住。
        """
        if kline_unit.idx >= 0:
            return
        if len(self[lv_idx]) == 0:
            kline_unit.set_idx(0)
        else:
            kline_unit.set_idx(self[lv_idx][-1][-1].idx + 1)

    def load_iterator(self, lv_idx, parent_klu, step):
        """
        单级别的K线消费循环，也是整个计算的递归主体。

        流程：取一根K线 -> 校验时间单调递增 -> 喂给本级 CKLine_List -> 递归驱动次级别
        -> 校验级别对齐；若该K线时间已经越过父级别K线，则缓存留待下一轮父K线再处理。

        Args:
            lv_idx: 当前级别下标
            parent_klu: 父级别 CKLine_Unit，最高级别为 None
            step: True 时每处理一根最高级别K线 yield 一次 self

        Yields:
            CChan: 仅 lv_idx==0 且 step=True 时产出快照

        Raises:
            CChanException: K线时间非递增（ErrCode.KL_NOT_MONOTONOUS）
        """
        # K线时间天级别以下描述的是结束时间，如60M线，每天第一根是10点30的
        # 天以上是当天日期
        cur_lv = self.lv_list[lv_idx]
        pre_klu = self[lv_idx][-1][-1] if len(self[lv_idx]) > 0 and len(self[lv_idx][-1]) > 0 else None
        while True:
            if self.klu_cache[lv_idx]:
                kline_unit = self.klu_cache[lv_idx]
                assert kline_unit is not None
                self.klu_cache[lv_idx] = None
            else:
                try:
                    kline_unit = self.get_next_lv_klu(lv_idx)
                    self.try_set_klu_idx(lv_idx, kline_unit)
                    if not kline_unit.time > self.klu_last_t[lv_idx]:
                        raise CChanException(f"kline time err, cur={kline_unit.time}, last={self.klu_last_t[lv_idx]}, or refer to quick_guide.md, try set auto=False in the CTime returned by your data source class", ErrCode.KL_NOT_MONOTONOUS)
                    self.klu_last_t[lv_idx] = kline_unit.time
                except StopIteration:
                    break

            if parent_klu and kline_unit.time > parent_klu.time:
                self.klu_cache[lv_idx] = kline_unit
                break
            kline_unit.set_pre_klu(pre_klu)
            pre_klu = kline_unit
            self.add_new_kl(cur_lv, kline_unit)
            if parent_klu:
                self.set_klu_parent_relation(parent_klu, kline_unit, cur_lv, lv_idx)
            if lv_idx != len(self.lv_list)-1:
                for _ in self.load_iterator(lv_idx+1, kline_unit, step):
                    ...
                self.check_kl_align(kline_unit, lv_idx)
            if lv_idx == 0 and step:
                yield self

    def check_kl_consitent(self, parent_klu, sub_klu):
        """
        校验父子级别K线是否落在同一天（年/月/日都要一致），不一致会累加记录。

        Args:
            parent_klu: 父级别 CKLine_Unit
            sub_klu: 子级别 CKLine_Unit

        Raises:
            CChanException: 不一致条数达到 conf.max_kl_inconsistent_cnt（ErrCode.KL_TIME_INCONSISTENT）
        """
        if parent_klu.time.year != sub_klu.time.year or \
           parent_klu.time.month != sub_klu.time.month or \
           parent_klu.time.day != sub_klu.time.day:
            self.kl_inconsistent_detail[str(parent_klu.time)].append(sub_klu.time)
            if self.conf.print_warning:
                print(f"[WARNING-{self.code}]父级别时间是{parent_klu.time}，次级别时间却是{sub_klu.time}")
            if len(self.kl_inconsistent_detail) >= self.conf.max_kl_inconsistent_cnt:
                raise CChanException(f"父&子级别K线时间不一致条数超过{self.conf.max_kl_inconsistent_cnt}！！", ErrCode.KL_TIME_INCONSISTENT)

    def check_kl_align(self, kline_unit, lv_idx):
        """
        校验一根父级别K线是否在次级别找到了子K线（sub_kl_list 非空）。

        Args:
            kline_unit: 父级别 CKLine_Unit
            lv_idx: 父级别下标，用于取它的次级别

        Raises:
            CChanException: 缺失条数达到 conf.max_kl_misalgin_cnt（ErrCode.KL_DATA_NOT_ALIGN）
        """
        if self.conf.kl_data_check and len(kline_unit.sub_kl_list) == 0:
            self.kl_misalign_cnt += 1
            if self.conf.print_warning:
                print(f"[WARNING-{self.code}]当前{kline_unit.time}没在次级别{self.lv_list[lv_idx+1]}找到K线！！")
            if self.kl_misalign_cnt >= self.conf.max_kl_misalgin_cnt:
                raise CChanException(f"在次级别找不到K线条数超过{self.conf.max_kl_misalgin_cnt}！！", ErrCode.KL_DATA_NOT_ALIGN)

    def __getitem__(self, n) -> CKLine_List:
        """
        取某个级别的 CKLine_List：chan[KL_TYPE.K_DAY] 按级别取，chan[0] 按下标取（0 为最高级别）。

        Args:
            n: KL_TYPE 或级别下标 int

        Returns:
            CKLine_List

        Raises:
            CChanException: 查询类型既非 KL_TYPE 也非 int（ErrCode.COMMON_ERROR）
        """
        if isinstance(n, KL_TYPE):
            return self.kl_datas[n]
        elif isinstance(n, int):
            return self.kl_datas[self.lv_list[n]]
        else:
            raise CChanException("unspoourt query type", ErrCode.COMMON_ERROR)

    # def get_bsp(self, idx=None) -> List[CBS_Point]:
    #     """
    #     [已废弃] 请改用 get_latest_bsp。

    #     Args:
    #         idx: 指定级别（KL_TYPE 或下标）；None 时要求只有一个级别

    #     Returns:
    #         List[CBS_Point]: 按时间排序的买卖点列表
    #     """
    #     print('[deprecated] use get_latest_bsp instead')
    #     if idx is not None:
    #         return self[idx].bs_point_lst.getSortedBspList()
    #     assert len(self.lv_list) == 1
    #     return self[0].bs_point_lst.getSortedBspList()

    def get_latest_bsp(self, idx=None, number=1) -> List[CBS_Point]:
        """
        取最近的若干个买卖点。

        Args:
            idx: 指定级别（KL_TYPE 或下标）；None 时要求只有一个级别
            number: 取几个，从最新到最旧排序；number=0 表示取全部

        Returns:
            List[CBS_Point]
        """
        # number=0则取全部bsp，从最新到最旧排序
        if idx is not None:
            return self[idx].bs_point_lst.get_latest_bsp(number)
        assert len(self.lv_list) == 1
        return self[0].bs_point_lst.get_latest_bsp(number)

    def chan_dump_pickle(self, file_path):
        """
        把整个计算结果序列化到 pickle 文件。

        Args:
            file_path: 输出路径

        Note:
            对象内部的 pre/next 双向链表会让默认递归深度爆栈，所以先抬高上限、
            存盘前把所有 pre/next 断开，写完立即调 chan_pickle_restore() 复原内存里的对象。
        """
        _pre_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(0x100000)
        for kl_list in self.kl_datas.values():
            for klc in kl_list.lst:
                for klu in klc.lst:
                    klu.pre = None
                    klu.next = None
                klc.set_pre(None)
                klc.set_next(None)
            for bi in kl_list.bi_list:
                bi.pre = None
                bi.next = None
            for seg in kl_list.seg_list:
                seg.pre = None
                seg.next = None

            for segseg in kl_list.segseg_list:
                segseg.pre = None
                segseg.next = None

        with open(file_path, "wb") as f:
            pickle.dump(self, f)

        sys.setrecursionlimit(_pre_limit)
        self.chan_pickle_restore()

    @staticmethod
    def chan_load_pickle(file_path) -> 'CChan':
        """
        从 pickle 文件反序列化一个 CChan，并自动恢复各级对象的 pre/next 链接。

        Args:
            file_path: pickle 路径

        Returns:
            CChan: 链接关系已复原、可直接使用的计算结果
        """
        with open(file_path, "rb") as f:
            chan = pickle.load(f)
        chan.chan_pickle_restore()

        return chan

    def chan_pickle_restore(self):
        """
        重新串接各级对象的双向链表（K线、合并K线、笔、线段、线段段的 pre/next）。

        Note:
            反序列化后必须先调用它，否则所有依赖前驱/后继的遍历逻辑都会失效。
        """
        for kl_list in self.kl_datas.values():
            last_klu = None
            last_klc = None
            last_bi = None
            last_seg = None
            last_segseg = None
            for klc in kl_list.lst:
                for klu in klc.lst:
                    klu.pre = last_klu
                    if last_klu:
                        last_klu.next = klu
                    last_klu = klu
                klc.set_pre(last_klc)
                if last_klc:
                    last_klc.set_next(klc)
                last_klc = klc
            for bi in kl_list.bi_list:
                bi.pre = last_bi
                if last_bi:
                    last_bi.next = bi
                last_bi = bi
            for seg in kl_list.seg_list:
                seg.pre = last_seg
                if last_seg:
                    last_seg.next = seg
                last_seg = seg
            for segseg in kl_list.segseg_list:
                segseg.pre = last_segseg
                if last_segseg:
                    last_segseg.next = segseg
                last_segseg = segseg
