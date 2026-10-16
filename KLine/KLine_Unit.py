# 单根（未经包含关系处理的）K线单元 CKLine_Unit，简称 KLU，是整条缠论计算链路的最底层。
#
# 链路位置：数据源 -> CKLine_Unit -> CKLine（若干根 KLU 合并成一根 KLC）-> CKLine_List（某级别整段序列，含笔/线段/中枢容器）。
#
# KLU 之间靠三组指针表达关系：
#   pre / next    ：同一级别相邻 KLU 的双向链表（由 set_pre_klu 建立，pickle 存取时会断开重建）
#   sup_kl/sub_kl_list：父级别 KLU / 次级别 KLU 列表，跨级别的包含关系
#   __klc         ：反向指针，指向"我被合并进了哪根 CKLine"
import copy
from typing import Dict, Optional

from Common.CEnum import DATA_FIELD, TRADE_INFO_LST, TREND_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Math.BOLL import BOLL_Metric, BollModel
from Math.Demark import CDemarkEngine, CDemarkIndex
from Math.KDJ import KDJ
from Math.MACD import CMACD, CMACD_item
from Math.RSI import RSI
from Math.TrendModel import CTrendModel

from .TradeInfo import CTradeInfo


class CKLine_Unit:
    """
    单根K线单元：时间、开高低收 + 成交明细 + 各类指标，并维系本级别链表和跨级别父子关系。

    它的 high/low 是被保证过的（low <= open/close <= high），
    因此上层做包含处理、判分型时可以直接依赖这个前提。
    """

    def __init__(self, kl_dict, autofix=False):
        """
        Args:
            kl_dict: 至少包含 FIELD_TIME/FIELD_OPEN/FIELD_CLOSE/FIELD_HIGH/FIELD_LOW 的 dict
            autofix: True 时把不合规的 high/low 自动掰回去，False 时直接抛异常

        Note:
            idx 初始化为 -1 表示"还没分配"，后续由 CChan.load_stock_data 或
            CChan.try_set_klu_idx 补上真正的下标；kl_type 同理由外部写入。

        Raises:
            CChanException: autofix=False 且 high/low 不是四价极值（ErrCode.KL_DATA_INVALID）
        """
        # _time, _close, _open, _high, _low, _extra_info={}
        self.kl_type = None
        self.time: CTime = kl_dict[DATA_FIELD.FIELD_TIME]
        self.close = kl_dict[DATA_FIELD.FIELD_CLOSE]
        self.open = kl_dict[DATA_FIELD.FIELD_OPEN]
        self.high = kl_dict[DATA_FIELD.FIELD_HIGH]
        self.low = kl_dict[DATA_FIELD.FIELD_LOW]

        # 必须先做价格自洽性检查：后面所有指标和分型运算都依赖 low<=四价<=high 这个前提
        self.check(autofix)

        self.trade_info = CTradeInfo(kl_dict)

        self.demark: CDemarkIndex = CDemarkIndex()

        self.sub_kl_list = []  # 次级别KLU列表
        self.sup_kl: Optional[CKLine_Unit] = None  # 指向更高级别KLU

        # 局部导入：KLine 反过来要 import CKLine_Unit，放这里避免循环依赖
        from KLine.KLine import CKLine
        self.__klc: Optional[CKLine] = None  # 指向KLine

        # self.macd: Optional[CMACD_item] = None
        # self.boll: Optional[BOLL_Metric] = None
        self.trend: Dict[TREND_TYPE, Dict[int, float]] = {}  # int -> float

        self.limit_flag = 0  # 0:普通 -1:跌停，1:涨停
        self.pre: Optional[CKLine_Unit] = None
        self.next: Optional[CKLine_Unit] = None

        # -1 是个哨兵值，表示"本K线还没被分配下标"，
        # 由 CChan.load_stock_data / CChan.try_set_klu_idx 补真实值
        self.set_idx(-1)

    def __deepcopy__(self, memo):
        """
        自定义深拷贝：只拷贝本K线自身的数据，不搬运运行时的链接关系。

        Args:
            memo: deepcopy 共享备忘录（id(原对象) -> 副本）

        Returns:
            CKLine_Unit: 数据等价的副本

        Note:
            pre/next/sup_kl/sub_kl_list/klc 这些指针在这里一律不重建：
            它们由 CKLine_List.__deepcopy__（本级别链表、KLC 反向引用）
            和 CChan.__deepcopy__（跨级别父子关系）统一串回去。
            macd/boll 在默认配置下必然存在（CChanConfig.GetMetricModel 总带 MACD 与 BOLL），
            这里因此没有做 hasattr 保护，而 rsi/kdj 是可选指标，故需要 hasattr 判断。
        """
        _dict = {
            DATA_FIELD.FIELD_TIME: self.time,
            DATA_FIELD.FIELD_CLOSE: self.close,
            DATA_FIELD.FIELD_OPEN: self.open,
            DATA_FIELD.FIELD_HIGH: self.high,
            DATA_FIELD.FIELD_LOW: self.low,
        }
        for metric in TRADE_INFO_LST:
            if metric in self.trade_info.metric:
                _dict[metric] = self.trade_info.metric[metric]
        obj = CKLine_Unit(_dict)
        obj.demark = copy.deepcopy(self.demark, memo)
        obj.trend = copy.deepcopy(self.trend, memo)
        obj.limit_flag = self.limit_flag
        obj.macd = copy.deepcopy(self.macd, memo)
        obj.boll = copy.deepcopy(self.boll, memo)
        if hasattr(self, "rsi"):
            obj.rsi = copy.deepcopy(self.rsi, memo)
        if hasattr(self, "kdj"):
            obj.kdj = copy.deepcopy(self.kdj, memo)
        obj.set_idx(self.idx)
        memo[id(self)] = obj
        return obj

    @property
    def klc(self):
        """
        反向取到自己所属的那根合并K线 CKLine。

        Returns:
            CKLine

        Raises:
            AssertionError: 该 KLU 还没被任何 CKLine 收纳（__klc 仍为 None）
        """
        assert self.__klc is not None
        return self.__klc

    def set_klc(self, klc):
        """
        写入反向引用：标记"我这根原始K线被合并进了哪根 CKLine"。

        Args:
            klc: 目标 CKLine

        Note:
            CKLine.__init__ 会为它的首根 KLU 调一次，后续被合并进来的 KLU 由
            CKLine_Combiner.try_add 再调；因此同一根 KLU 可能被重复调用多次，
            这里是直接覆盖而不是追加。
        """
        self.__klc = klc

    @property
    def idx(self):
        """
        本K线在本级别里的顺序下标。

        Returns:
            int: 从 0 开始递增；-1 表示尚未分配
        """
        return self.__idx

    def set_idx(self, idx):
        """
        设置/改写本K线的下标。

        Args:
            idx: 新的下标值

        Note:
            除了一次性赋值外没有别的副作用，不会自动前移后一根K线的 idx，
            因此调用方要自己保证同级别 idx 的连续单调递增。
        """
        self.__idx: int = idx

    def __str__(self):
        """
        Returns:
            str: "idx:时间/级别 open=.. close=.. high=.. low=.. 成交信息" 的一行摘要
        """
        return f"{self.idx}:{self.time}/{self.kl_type} open={self.open} close={self.close} high={self.high} low={self.low} {self.trade_info}"

    def check(self, autofix=False):
        """
        校验开高低收是否自洽：low 必须是四价最小值，high 必须是四价最大值。

        Args:
            autofix: True 时把越界的 low/high 修正回极值，False 时抛异常

        Note:
            两条检查的顺序是有意为之：先把 low 压到四价最小值，
            再用"修正后的 low"参与 high 的 max 计算。
            这样即使数据源把 high/low 传反（high < low），也能分两步把它掰回来：
            第一步 low 被压成原来的 high，第二步 high 被抬成四价最大值，最终恢复 low <= high。
            注意这里只修 low/high 两个端点，open/close 落在区间内是 min/max 的自然结果。

        Raises:
            CChanException: autofix=False 且数据不自洽（ErrCode.KL_DATA_INVALID）
        """
        if self.low > min([self.low, self.open, self.high, self.close]):
            if autofix:
                self.low = min([self.low, self.open, self.high, self.close])
            else:
                raise CChanException(f"{self.time} low price={self.low} is not min of [low={self.low}, open={self.open}, high={self.high}, close={self.close}]", ErrCode.KL_DATA_INVALID)
        # 走到这里 self.low 已是修正后的值，用它参与 max 才能保证最终 low <= high
        if self.high < max([self.low, self.open, self.high, self.close]):
            if autofix:
                self.high = max([self.low, self.open, self.high, self.close])
            else:
                raise CChanException(f"{self.time} high price={self.high} is not max of [low={self.low}, open={self.open}, high={self.high}, close={self.close}]", ErrCode.KL_DATA_INVALID)

    def add_children(self, child):
        """
        把一根次级别K线登记为自己的子K线。

        Args:
            child: 次级别的 CKLine_Unit

        Note:
            这里是单向追加（父 -> 子），不会顺手把 child.sup_kl 指回自己；
            反向引用要另调 set_parent。CChan.set_klu_parent_relation 就是成对调用这两者的地方。
        """
        self.sub_kl_list.append(child)

    def set_parent(self, parent: 'CKLine_Unit'):
        """
        把一根父级别K线登记为自己的上级（反向：子 -> 父）。

        Args:
            parent: 父级别的 CKLine_Unit

        Note:
            只写自己的 sup_kl，不会往 parent.sub_kl_list 里加。
        """
        self.sup_kl = parent

    def get_children(self):
        """
        Yields:
            CKLine_Unit: 本K线对应的所有次级别K线，按加入顺序
        """
        yield from self.sub_kl_list

    def _low(self):
        """
        统一取值接口：Bi/Seg 等上层元素同样实现了 _low/_high，
        这样"取区间最低价"之类的逻辑（如中枢判定）不必关心对象的类型。

        Returns:
            float: 本K线最低价
        """
        return self.low

    def _high(self):
        """
        同 _low，返回本K线最高价。

        Returns:
            float: 本K线最高价
        """
        return self.high

    def set_metric(self, metric_model_lst: list) -> None:
        """
        按顺序把各个指标漏斗喂一根收盘价（或高低收），并把结果写回自身属性。

        Args:
            metric_model_lst: 指标算子列表，元素类型决定走哪个分支
                （CMACD/CTrendModel/BollModel/CDemarkEngine/RSI/KDJ）

        Note:
            属性是"懒创建"的：macd/trend/boll/demark/rsi/kdj 只有对应的算子被配置时才会出现，
            所以读这些属性前要么确保配置齐全，要么先 hasattr。
            指标算子本身是有状态的流式对象，被 CKLine_List.metric_model_lst 长期持有，
            这里每根K线调一次即为"逐步推进"。
        """
        for metric_model in metric_model_lst:
            if isinstance(metric_model, CMACD):
                self.macd: CMACD_item = metric_model.add(self.close)
            elif isinstance(metric_model, CTrendModel):
                if metric_model.type not in self.trend:
                    self.trend[metric_model.type] = {}
                self.trend[metric_model.type][metric_model.T] = metric_model.add(self.close)
            elif isinstance(metric_model, BollModel):
                self.boll: BOLL_Metric = metric_model.add(self.close)
            elif isinstance(metric_model, CDemarkEngine):
                self.demark = metric_model.update(idx=self.idx, close=self.close, high=self.high, low=self.low)
            elif isinstance(metric_model, RSI):
                self.rsi = metric_model.add(self.close)
            elif isinstance(metric_model, KDJ):
                self.kdj = metric_model.add(self.high, self.low, self.close)

    def get_parent_klc(self):
        """
        取父级别K线所属的合并K线。

        Returns:
            CKLine: 父 KLU 所在的 CKLine

        Note:
            本仓库内目前没有调用方，属于对外暴露的便捷接口；
            由于依赖 sup_kl，只有非最高级别的K线才能调。

        Raises:
            AssertionError: 本K线没有父级别K线（最高级别的K线调这个必挂）
        """
        assert self.sup_kl is not None
        return self.sup_kl.klc

    def include_sub_lv_time(self, sub_lv_t: str) -> bool:
        """
        判断某个次级别时间是否落在本K线（及其子孙K线）覆盖的时间范围内。

        Args:
            sub_lv_t: 待判断的时间字符串，格式需与 CTime.to_str() 的输出一致

        Returns:
            bool: 命中则返回 True

        Note:
            逐层向下递归：先看自己，再逐一看子K线，子K线再递归它自己的子K线，
            因此可以支持任意多级联查；没命中返回 False。
        """
        if self.time.to_str() == sub_lv_t:
            return True
        for sub_klu in self.sub_kl_list:
            if sub_klu.time.to_str() == sub_lv_t:
                return True
            if sub_klu.include_sub_lv_time(sub_lv_t):
                return True
        return False

    def set_pre_klu(self, pre_klu: Optional['CKLine_Unit']):
        """
        把本K线接到某根前驱K线之后，建立双向链表。

        Args:
            pre_klu: 前驱K线；None 表示没有前驱（本级别第一根），此时直接返回、什么都不做

        Note:
            副作用有两个：pre_klu.next 指向自己，自己的 pre 指向 pre_klu。
            它不会清理双方原有的指针，所以重复调用或者在同一条链上乱序调用会造成链断裂，
            调用方（CChan.load_iterator）保证按时间严格递增顺序逐根调用。
        """
        if pre_klu is None:
            return
        pre_klu.next = self
        self.pre = pre_klu
