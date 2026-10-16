# 单根K线的附属交易信息（成交量、成交额、换手率等）容器。
#
# 在计算链路中的位置：CKLine_Unit -> CTradeInfo。
# 它是"只读旁路"数据：由数据源给的原始 dict 抽取而来，挂在 klu.trade_info 上，
# 供后续指标计算、选股逻辑和画图展示使用，不参与包含处理、分型、笔/线段等核心运算。
from typing import Dict, Optional

from Common.CEnum import TRADE_INFO_LST


class CTradeInfo:
    """
    一根K线的成交类指标集合。

    键值固定为 TRADE_INFO_LST 里枚举的那些字段，缺哪个就存 None，
    这样上层取值时不用先判断 key 是否存在。
    """

    def __init__(self, info: Dict[str, float]):
        """
        Args:
            info: 数据源给出的原始K线 dict，key 为 TRADE_INFO_LST 中的枚举值

        Note:
            只读遍历 TRADE_INFO_LST 而不是遍历 info，
            因此 info 里多出来的字段会被忽略，缺失的字段统一补 None。
        """
        self.metric: Dict[str, Optional[float]] = {}
        for metric_name in TRADE_INFO_LST:
            self.metric[metric_name] = info.get(metric_name)

    def __str__(self):
        """
        把所有指标按 "名字:值" 的形式拼成一行字符串。

        Returns:
            str: 空格分隔的键值对，未取到的字段显示为 None
        """
        return " ".join([f"{metric_name}:{value}" for metric_name, value in self.metric.items()])
