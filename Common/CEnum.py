"""
全局枚举与常量定义模块。

集中定义缠论计算全流程共用的枚举类型：数据源（DATA_SRC）、K线级别（KL_TYPE）、
K线方向与分型、笔方向与笔种类、买卖点类型、复权方式、均线/通道类型、线段语义、
MACD 力度算法等；另含数据源解析用的字段名常量 DATA_FIELD。

Note:
    KL_TYPE 的枚举值就是「级别序号」：数值越大级别越高（K_1S=1 < K_DAY=15 < K_YEAR=19），
    Common/func_util.py 的 kltype_lt_day / check_kltype_order 直接依赖这个数值序。
"""
from enum import Enum, auto
from typing import Literal


class DATA_SRC(Enum):
    """K线数据源类型；CChan.GetStockAPI 依此决定实例化哪个取数类。"""

    # 证券宝 baostock，A股免费数据源
    BAO_STOCK = auto()
    # ccxt，数字货币交易所聚合数据源
    CCXT = auto()
    # 本地 CSV 文件数据源
    CSV = auto()
    # akshare 数据源（GetStockAPI 中已注释，暂未启用）
    AKSHARE = auto()
    # 富途 OpenAPI，需本机运行 FutuOpenD
    FUTU = auto()


class KL_TYPE(Enum):
    """
    K线级别。

    成员值从 1 递增到 19，值越大级别越高；秒/分钟级别属于「天级别以下」，
    其时间戳表示的是该K线的结束时间（详见 Common/CTime.py 的 auto 约定）。
    """

    # 1秒线
    K_1S = 1
    # 3秒线
    K_3S = 2
    # 5秒线
    K_5S = 3
    # 10秒线
    K_10S = 4
    # 15秒线
    K_15S = 5
    # 20秒线
    K_20S = 6
    # 30秒线
    K_30S = 7
    # 1分钟线
    K_1M = 8
    # 3分钟线
    K_3M = 9
    # 5分钟线
    K_5M = 10
    # 10分钟线
    K_10M = 11
    # 15分钟线
    K_15M = 12
    # 30分钟线
    K_30M = 13
    # 60分钟线
    K_60M = 14
    # 日线：天级别分界线，func_util.kltype_lte_day 以它的值为界判定「<= 日线」
    K_DAY = 15
    # 周线
    K_WEEK = 16
    # 月线
    K_MON = 17
    # 季线
    K_QUARTER = 18
    # 年线
    K_YEAR = 19


class KLINE_DIR(Enum):
    """相邻K线的相对位置关系，用于K线包含处理（KLine_Combiner.test_combine）。"""

    # 创新高：高低点同时抬升，无需合并
    UP = auto()
    # 创新低：高低点同时下移，无需合并
    DOWN = auto()
    # 存在包含关系，需按当前方向合并成一根
    COMBINE = auto()
    # 被完全包含；exclude_included=True 时返回它表示「不合并」
    INCLUDED = auto()


class FX_TYPE(Enum):
    """分型类型（顶/底）。"""

    # 底分型
    BOTTOM = auto()
    # 顶分型
    TOP = auto()
    # 尚未确认
    UNKNOWN = auto()


class BI_DIR(Enum):
    """笔的方向。"""

    # 向上笔
    UP = auto()
    # 向下笔
    DOWN = auto()


class BI_TYPE(Enum):
    """笔的分类标签。当前实现只在 CBi 构造时固定置为 STRICT，其余成员为预留/说明性定义。"""

    # 未分类
    UNKNOWN = auto()
    # 严格笔：不依赖任何放宽条件
    STRICT = auto()
    SUB_VALUE = auto()  # 次高低点成笔
    # 跳空成笔（按跳空缺口根数判定）
    TIAOKONG_THRED = auto()
    # 大横：长时间横盘时成笔
    DAHENG = auto()
    # 推笔（推演出的笔）
    TUIBI = auto()
    # 宽松笔
    UNSTRICT = auto()
    # 跳空成笔（按跳空幅度判定）
    TIAOKONG_VALUE = auto()


# 买卖点主类型：Literal 约束下的 '1'/'2'/'3'，由 BSP_TYPE.main_type() 取首字符得到
BSP_MAIN_TYPE = Literal['1', '2', '3']


class BSP_TYPE(Enum):
    """买卖点的细分类型，值字符串的首字符即主类型（1/2/3 类）。"""

    # 1类买卖点
    T1 = '1'
    # 盘整背驰形成的1类买卖点
    T1P = '1p'
    # 2类买卖点
    T2 = '2'
    # 类二买卖点
    T2S = '2s'
    T3A = '3a'  # 中枢在1类后面
    T3B = '3b'  # 中枢在1类前面

    def main_type(self) -> BSP_MAIN_TYPE:
        """
        取买卖点主类型。

        Returns:
            BSP_MAIN_TYPE: 类型字符串的首字符，如 '1p' -> '1'、'2s' -> '2'
        """
        return self.value[0]  # type: ignore


class AUTYPE(Enum):
    """复权方式，原样透传给数据源的取数接口。"""

    # 前复权（CChan 默认）
    QFQ = auto()
    # 后复权
    HFQ = auto()
    # 不复权
    NONE = auto()


class TREND_TYPE(Enum):
    """均线 / 通道类型，供 CKLine_Unit 计算 trend 特征及绘图使用。"""

    # 均值，即 mean_metrics 配置的简单均线
    MEAN = "mean"
    # 近 T 根K线的最高价（上轨）
    MAX = "max"
    # 近 T 根K线的最低价（下轨）
    MIN = "min"


class TREND_LINE_SIDE(Enum):
    """趋势线（CTrendLine）的取点侧，决定用笔的起点还是终点去拟合直线。"""

    # 取内侧点（笔起点），线段用它算支撑线
    INSIDE = auto()
    # 取外侧点（笔终点），线段用它算压力线
    OUTSIDE = auto()


class LEFT_SEG_METHOD(Enum):
    """线段列表收尾时，剩余的、无法归入已确定线段的笔如何处理（seg.left_method）。"""

    # 全部并入最后一段方向正确的笔
    ALL = auto()
    # 出现可靠的新极值就拆成两段（默认）
    PEAK = auto()


class FX_CHECK_METHOD(Enum):
    """笔的顶底分型是否成立的检查强度（BiConfig.bi_fx_check，字符串默认 "strict"）。"""

    # 取三根KLC（自身+前后各一）构成最宽区间比较，最难满足
    STRICT = auto()
    # 只取分型KLC自身，区间最窄，较容易满足
    LOSS = auto()
    # 取两根KLC（自身+后继），严格程度介于 STRICT 与 LOSS 之间
    HALF = auto()
    # 判据与其余三种不同：要求两个分型区间完全不重叠（见 KLine.check_valid_top_button）
    TOTALLY = auto()


class SEG_TYPE(Enum):
    """线段所在的层级语义，决定该序列是「由笔构段」还是「由线段构段」。"""

    # 由笔构成的线段
    BI = auto()
    # 由线段构成的线段（线段的线段）
    SEG = auto()


class MACD_ALGO(Enum):
    """买卖点背驰判断所用的力度指标算法（ChanConfig 的 macd_algo）。"""

    # 对应笔区间内的 MACD 红/绿柱面积（只算同色柱）
    AREA = auto()
    # 红绿柱峰值（取绝对值）
    PEAK = auto()
    # 对应笔区间内的 MACD 全柱面积
    FULL_AREA = auto()
    # 笔区间内 MACD 柱高的最大值与最小值之差
    DIFF = auto()
    # 笔的斜率
    SLOPE = auto()
    # 笔的涨跌幅
    AMP = auto()
    # 笔上所有K线成交量之和
    VOLUMN = auto()
    # 笔上所有K线成交额之和
    AMOUNT = auto()
    # 笔上K线的平均成交量
    VOLUMN_AVG = auto()
    # 笔上K线的平均成交额
    AMOUNT_AVG = auto()
    # 笔上K线的平均换手率
    TURNRATE_AVG = auto()
    # 笔上 RSI 极值：向上笔取最大 RSI，向下笔取 10000/(最小 RSI) 的倒数量纲
    RSI = auto()


class DATA_FIELD:
    """数据源返回的K线字段名常量，统一在这里约定取数用的键名。"""

    # 时间
    FIELD_TIME = "time_key"
    # 开盘价
    FIELD_OPEN = "open"
    # 最高价
    FIELD_HIGH = "high"
    # 最低价
    FIELD_LOW = "low"
    # 收盘价
    FIELD_CLOSE = "close"
    FIELD_VOLUME = "volume"  # 成交量
    FIELD_TURNOVER = "turnover"  # 成交额
    FIELD_TURNRATE = "turnover_rate"  # 换手率


# 交易（量能）信息字段，用于判断某级别K线是否带量能数据、以及参与背驰计算
TRADE_INFO_LST = [DATA_FIELD.FIELD_VOLUME, DATA_FIELD.FIELD_TURNOVER, DATA_FIELD.FIELD_TURNRATE]
