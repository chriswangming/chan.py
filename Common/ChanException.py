"""
错误码与自定义异常模块。

ErrCode 用 IntEnum 按业务域分段编号：缠论计算错误 0~99、交易错误 100~199、
K线数据错误 200~299；段首段尾的 _XXX_BEGIN / _XXX_END 是哨兵位，不参与实际抛错，
只用于 CChanException.is_chan_err / is_kldata_err 做区间归属判断。

CChanException 是全项目统一抛出的异常类型，调用方通过 errcode 区分处理策略
（例如数据类错误在批量选股时可跳过该标的，而配置类错误应直接中断）。

Note:
    IntEnum 的 str() 在 python3.8 下输出 "ErrCode.CONFIG_ERROR"，
    python3.11 起输出数字 "17"，跨版本打印错误码时需注意。
"""
from enum import IntEnum


class ErrCode(IntEnum):
    """全局错误码，按 0~99 / 100~199 / 200~299 三段划分业务域。"""

    # chan err
    # 缠论计算过程中的通用错误；段内首尾两个哨兵位不参与抛错
    # 哨兵：缠论错误段的起始边界（开区间，不包含）
    _CHAN_ERR_BEGIN = 0
    # 未分类的通用错误
    COMMON_ERROR = 1
    # 数据源里找不到该标的数据（CChan 可按 auto_skip_illegal_sub_lv 跳过级别）
    SRC_DATA_NOT_FOUND = 3
    # data_src 字符串无法解析成取数类
    SRC_DATA_TYPE_ERR = 4
    # 入参非法
    PARA_ERROR = 5
    # 多出/重复了一根K线（如重复喂同一时间的K线）
    EXTRA_KLU_ERR = 6
    # 线段终点值异常
    SEG_END_VALUE_ERR = 7
    # 线段特征（特征值）计算异常
    SEG_EIGEN_ERR = 8
    # 笔的计算异常
    BI_ERR = 9
    # K线包含处理异常
    COMBINER_ERR = 10
    # 绘图异常
    PLOT_ERR = 11
    # 模型（买卖点/策略模型）异常
    MODEL_ERROR = 12
    # 线段长度异常（如长度不足或超出预期）
    SEG_LEN_ERR = 13
    # 运行环境/依赖配置异常
    ENV_CONF_ERR = 14
    # 未知的数据库类型
    UNKNOWN_DB_TYPE = 15
    # K线特征提取异常
    FEATURE_ERROR = 16
    # CChanConfig 配置项非法
    CONFIG_ERROR = 17
    # 数据源返回的字段格式/内容非法
    SRC_DATA_FORMAT_ERROR = 18
    # 哨兵：缠论错误段的结束边界（开区间，不包含）
    _CHAN_ERR_END = 99

    # Trade Error
    # 交易下单/持仓相关错误
    # 哨兵：交易错误段起始边界
    _TRADE_ERR_BEGIN = 100
    # 信号已存在，重复录入
    SIGNAL_EXISTED = 101
    # 查不到对应记录
    RECORD_NOT_EXIST = 102
    # 记录已经开仓，重复开仓
    RECORD_ALREADY_OPENED = 103
    # 额度/资金不足
    QUOTA_NOT_ENOUGH = 104
    # 记录尚未开仓就执行了平仓等操作
    RECORD_NOT_OPENED = 105
    # 交易解锁失败
    TRADE_UNLOCK_FAIL = 106
    # 下单失败
    PLACE_ORDER_FAIL = 107
    # 查询订单列表失败
    LIST_ORDER_FAIL = 108
    # 撤单失败
    CANDEL_ORDER_FAIL = 109
    # 获取富途报价失败
    GET_FUTU_PRICE_FAIL = 110
    # 获取富途每手股数失败
    GET_FUTU_LOT_SIZE_FAIL = 111
    # 已开仓记录没有被监控（watching）中
    OPEN_RECORD_NOT_WATCHING = 112
    # 获取持仓数量失败
    GET_HOLDING_QTY_FAIL = 113
    # 记录已平仓
    RECORD_CLOSED = 114
    # 请求交易日历失败
    REQUEST_TRADING_DAYS_FAIL = 115
    # 平仓订单号不唯一
    COVER_ORDER_ID_NOT_UNIQUE = 116
    # 信号已交易过
    SIGNAL_TRADED = 117
    # 哨兵：交易错误段结束边界
    _TRADE_ERR_END = 199

    # KL data Error
    # K线数据本身的问题
    # 哨兵：K线数据错误段起始边界
    _KL_ERR_BEGIN = 200
    # 价格小于等于0
    PRICE_BELOW_ZERO = 201
    # 父子级别K线数量/时间对不齐（次级别找不到对应K线）
    KL_DATA_NOT_ALIGN = 202
    # K线数据非法（如 high < low）
    KL_DATA_INVALID = 203
    # 父子级别K线不在同一天
    KL_TIME_INCONSISTENT = 204
    # 量能数据中出现过多的0，疑似该品种无量能
    TRADEINFO_TOO_MUCH_ZERO = 205
    # K线时间非严格递增
    KL_NOT_MONOTONOUS = 206
    # 快照数据异常
    SNAPSHOT_ERR = 207
    SUSPENSION = 208  # 疑似停牌
    # 标的上市时间晚于回测起始时间
    STOCK_IPO_TOO_LATE = 209
    # 指定区间内一根数据都没有
    NO_DATA = 210
    # 标的交易不活跃
    STOCK_NOT_ACTIVE = 211
    # 标的价格长期不变，不活跃
    STOCK_PRICE_NOT_ACTIVE = 212
    # 哨兵：K线数据错误段结束边界
    _KL_ERR_END = 299


class CChanException(Exception):
    """缠论库统一异常；在 message 之外额外携带 ErrCode 便于调用方分类处理。"""

    def __init__(self, message, code=ErrCode.COMMON_ERROR):
        """
        Args:
            message: 人类可读的错误描述，同时也会传给基类 Exception
            code: ErrCode 错误码，默认 COMMON_ERROR
        """
        self.errcode = code
        self.msg = message
        Exception.__init__(self, message)

    def is_kldata_err(self):
        """
        判断是否为K线数据类错误。

        Returns:
            bool: errcode 落在 (_KL_ERR_BEGIN, _KL_ERR_END) 开区间内即为 True
        """
        return ErrCode._KL_ERR_BEGIN < self.errcode < ErrCode._KL_ERR_END

    def is_chan_err(self):
        """
        判断是否为缠论计算类错误。

        Returns:
            bool: errcode 落在 (_CHAN_ERR_BEGIN, _CHAN_ERR_END) 开区间内即为 True
        """
        return ErrCode._CHAN_ERR_BEGIN < self.errcode < ErrCode._CHAN_ERR_END


if __name__ == "__main__":
    def foo():
        """演示用：抛出一个带 CONFIG_ERROR 错误码的异常。"""
        raise CChanException("XXX", ErrCode.CONFIG_ERROR)

    try:
        foo()
    except CChanException as e:
        print(str(e.errcode))
        # python3.8 结果为： ErrCode.CONFIG_ERROR
        # python3.11 结果为：17

        print(e.errcode.name, type(e.errcode.name))
