"""
数字货币交易所数据源模块（CCXT）。

通过开源库 ccxt 聚合全球数字货币交易所行情，默认实现取的是 **Binance 现货**。
适合给缠论引擎喂 BTC/ETH 这类 7x24 标的（注意数字货币没有开盘/停牌概念）。

前置条件（坑点）：
    - 需要 pip install ccxt；模块顶层直接 import ccxt，没装会在 import 阶段就失败；
    - 每次取数都要联网；币安对中国大陆 IP 有访问限制，必要时自行换交易所实例
      （把 get_kl_data 里的 ccxt.binance() 换成别的 exchange 即可）；
    - 没有登录态，因此 do_init / do_close 都是空实现，CChan 的建连/释放对它没有副作用。

取数能力：
    - 只取 OHLC 五字段（time, open, high, low, close），成交量/成交额/换手率都没有，
      因此依赖 TRADE_INFO 的背驰指标（volume/turnover/turnrate 类）用不了；
    - 单次请求没有传 limit，走的是交易所默认返回条数（币安现货默认 500 根），
      也没有翻页，所以取不到很长的历史 —— 长周期请自行加 limit/分页改造。

时间处理：
    ccxt 返回的是毫秒时间戳，这里用 datetime.fromtimestamp 转成 **本机时区**的
    本地时间字符串 "%Y-%m-%d %H:%M:%S"，再交给 parse_time_column 解析。
    auto 参数显式写成 `auto=not kltype_lt_day(self.k_type)`：
        天级别及以上 -> auto=True  -> hour/minute 为 0 时被折算成当天 23:59，
                                      保证日线时间戳大于当天所有分钟K线；
        天级别以下   -> auto=False -> 按真实时分秒算，语义是这根K线的**结束**时间。
"""
from datetime import datetime

import ccxt

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from Common.func_util import kltype_lt_day, str2float
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi


def GetColumnNameFromFieldList(fileds: str):
    """
    把 "time,open,high,low,close" 这样的字段名串翻译成 DATA_FIELD 列表。

    Args:
        fileds: 英文逗号分隔的字段名，取值只能是映射表里已有的 key

    Returns:
        List[str]: 对应的 DATA_FIELD 常量列表，顺序与入参一致

    Raises:
        KeyError: 出现映射表不认识的字段名（例如 misspell 成 "Time"）
    """
    _dict = {
        "time": DATA_FIELD.FIELD_TIME,
        "open": DATA_FIELD.FIELD_OPEN,
        "high": DATA_FIELD.FIELD_HIGH,
        "low": DATA_FIELD.FIELD_LOW,
        "close": DATA_FIELD.FIELD_CLOSE,
    }
    return [_dict[x] for x in fileds.split(",")]


class CCXT(CCommonStockApi):
    """
    基于 ccxt（默认 Binance 现货）的数字货币K线数据源。

    Note:
        is_connect 只是占位类变量，ccxt 本身是无状态的 REST 调用，
        本实现没有真正的连接需要维护。
    """

    is_connect = None

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        """
        Args:
            code: ccxt 的交易对写法，如 "BTC/USDT"
            k_type: KL_TYPE
            begin_date: 起始日期，取数时会被拼成 ISO8601 的 since 参数；
                        None 会让 busca 失败，调用前请保证传入
            end_date: 结束日期；**本实现没有用它**，取多少由交易所默认条数决定
            autype: 数字货币没有除权概念，该参数传进来后并未使用

        Raises:
            ccxt.BaseError 类异常：交易对不存在、网络不通、被限流等，均为原生透传
        """
        super(CCXT, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self):
        """
        一次性拉取 OHLCV 数据（默认 Binance，单次请求不分页），逐根产出 CKLine_Unit。

        Yields:
            CKLine_Unit: autofix=True，允许 CKLine_Unit 自动修正高低价越界的脏数据

        Note:
            第 6 列成交量被丢弃了，只保留前 5 列 OHLC；
            取数范围只由 since(=begin_date) 决定，往后默认返回交易所默认条数。
        """
        fields = "time,open,high,low,close"
        exchange = ccxt.binance()
        timeframe = self.__convert_type()
        # since 必须是 ISO8601：这里直接给到当天零点
        since_date = exchange.parse8601(f'{self.begin_date}T00:00:00')
        data = exchange.fetch_ohlcv(self.code, timeframe, since=since_date)

        for item in data:
            # item = [毫秒时间戳, open, high, low, close, volume]
            # fromtimestamp 落的是本机时区，与交易所 UTC 语义并不一致
            time_obj = datetime.fromtimestamp(item[0] / 1000)
            time_str = time_obj.strftime('%Y-%m-%d %H:%M:%S')
            item_data = [
                time_str,
                item[1],
                item[2],
                item[3],
                item[4]
            ]
            yield CKLine_Unit(self.create_item_dict(item_data, GetColumnNameFromFieldList(fields)), autofix=True)

    def SetBasciInfo(self):
        """
        数字货币交易对没有额外基本信息可查，留空实现。
        """
        pass

    @classmethod
    def do_init(cls):
        """
        ccxt 无需登录/建连，留空实现。
        """
        pass

    @classmethod
    def do_close(cls):
        """
        没有连接需要释放，留空实现。
        """
        pass

    def __convert_type(self):
        """
        把 KL_TYPE 映射成 ccxt/binance 的 timeframe 字符串。

        Returns:
            str: '1d' / '1w' / '1M' / '5m' / '15m' / '30m' / '1h'

        Raises:
            KeyError: 本表没有覆盖的级别（如 K_1M、K_3M、K_10M、季/年线）

        Note:
            '1M' 是大写 M 表示月线，'1m' 才是 1 分钟线，ccxt 里大小写敏感，别写反。
        """
        _dict = {
            KL_TYPE.K_DAY: '1d',
            KL_TYPE.K_WEEK: '1w',
            KL_TYPE.K_MON: '1M',
            KL_TYPE.K_5M: '5m',
            KL_TYPE.K_15M: '15m',
            KL_TYPE.K_30M: '30m',
            KL_TYPE.K_60M: '1h',
        }
        return _dict[self.k_type]

    def parse_time_column(self, inp):
        """
        把 "%Y-%m-%d %H:%M:%S" 格式的时间串解析成 CTime。

        Args:
            inp: 时间字符串，10 / 17 / 19 三种长度

        Returns:
            CTime: auto 与否由当前级别决定（见本方法最后一行）

        Raises:
            Exception: 三种长度都不匹配时抛出
        """
        if len(inp) == 10:
            year = int(inp[:4])
            month = int(inp[5:7])
            day = int(inp[8:10])
            hour = minute = 0
        elif len(inp) == 17:
            year = int(inp[:4])
            month = int(inp[4:6])
            day = int(inp[6:8])
            hour = int(inp[8:10])
            minute = int(inp[10:12])
        elif len(inp) == 19:
            year = int(inp[:4])
            month = int(inp[5:7])
            day = int(inp[8:10])
            hour = int(inp[11:13])
            minute = int(inp[14:16])
        else:
            raise Exception(f"unknown time column from TradingView:{inp}")
        # 天级别及以上 -> auto=True（00:00 折算成 23:59，好包住当天分钟K线）；
        # 天级别以下   -> auto=False（按这根K线的结束时间原样取）
        return CTime(year, month, day, hour, minute, auto=not kltype_lt_day(self.k_type))

    def create_item_dict(self, data, column_name):
        """
        把一行 OHLC 原始数据转成 CKLine_Unit 需要的字段字典（就地转换）。

        Args:
            data: [时间字符串, open, high, low, close]；会被就地替换成 CTime / float
            column_name: DATA_FIELD 列表，与 data 等长

        Returns:
            Dict[str, Any]: DATA_FIELD -> 已转换的值

        Note:
            用下标 i == 0 判定时间列，隐含约定 fields 串里 time 必须排在最前。
        """
        for i in range(len(data)):
            data[i] = self.parse_time_column(data[i]) if i == 0 else str2float(data[i])
        return dict(zip(column_name, data))
