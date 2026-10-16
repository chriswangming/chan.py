"""
A股数据源模块（baostock / 证券宝）。

baostock 是一个免费、免注册的A股历史行情库，核心接口是
`query_history_k_data_plus`（按 标的+级别+日期区间 一次返回全部K线）和
`query_stock_basic`（查标的类型/名称）。本模块把它们包成
DataAPI/CommonStockAPI.py 里 CCommonStockApi 的四条契约。

代码格式：
    baostock 用的是小写「市场.代码」，和常见行情软件相反：
        "sz.000001" 平安银行 / "sh.600000" 浦发银行 / "sh.000001" 上证指数
    self.code 会被**原样**透传给 baostock，本模块不做任何归一化，
    所以传错前缀只会在查询阶段表现为「查不到数据」。

前置条件与坑：
    - 需要 pip install baostock；本模块顶层直接 import，没装在 import 阶段就失败；
    - 不用起本地服务，但要联网；do_init() 里 bs.login() 建会话，
      do_close() 里 bs.logout() 释放（CChan.load 的 finally 保证执行到）；
    - 频率限制宽松但没有重试/退避，网络抖动会直接把异常抛到 CChan 外层；
    - 分钟线只有 OHLC 五字段，baostock 不返回分钟级 volume/amount/turn，
      因此依赖 TRADE_INFO 的 volume/turnover/turnrate 类背驰指标在分钟线上是空的；
    - 指数没有分钟级别数据，这里在取数前用 is_stock 提前挡掉并给出明确异常。

级别支持：
    日/周/月 + 5/15/30/60 分钟；其余级别（1M、3M、10M、季线、年线等）会抛
    CChanException(ErrCode.PARA_ERROR)，给出「不支持该级别」的明确提示。

异常约定：
    本模块的所有失败统一抛 CChanException 并带上 ErrCode，便于调用方分类处理：
        PARA_ERROR              —— 不支持的级别 / 复权方式
        SRC_DATA_NOT_FOUND      —— 查不到标的基本信息（可按 auto_skip_illegal_sub_lv 跳过）
        SRC_DATA_FORMAT_ERROR   —— 取数游标中途出错、时间串格式无法识别
        NO_DATA                 —— 查询成功但一行数据都没有
        ENV_CONF_ERR            —— 登录失败

时间字段构造（重点）：
    baostock 给两种时间串：天级别 "2021-09-13"（len=10），
    分钟级 "20210902113000000"（len=17，精确到毫秒）。
    parse_time_column() 按长度分支解析后交给 CTime，**没有显式传 auto**，
    即沿用 CTime 默认的 auto=True：
        天级别   -> hour/minute 都是 0，触发自适应落到当天 23:59，
                    保证日线时间戳大于当天任意分钟K线，正是项目要的语义；
        分钟级别 -> hour/minute 不同时为 0，auto 不生效，按真实时分算。
    baostock 分钟级 time 本身就是这根K线的**结束**时间
    （5 分钟线首根是 09:35，覆盖 09:30~09:35），与项目
    「天级别以下时间戳 = 结束时间」的约定天然一致，无需再补一个周期。
"""
import baostock as bs

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Common.func_util import kltype_lt_day, str2float
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi


def create_item_dict(data, column_name):
    """
    把 baostock 的一行原始字符串转成 CKLine_Unit 需要的字段字典。

    Args:
        data: rs.get_row_data() 返回的一行，所有元素都是字符串
        column_name: DATA_FIELD 常量列表，由 GetColumnNameFromFieldList 依 fields 顺序生成

    Returns:
        dict: {DATA_FIELD: 值}；时间字段已是 CTime，价格/成交字段是 float

    Note:
        约定第 0 列一定是时间列（天级别是 date、分钟级是 time），
        所以只有 i==0 走 parse_time_column，其余列统一 str2float。
    """
    for i in range(len(data)):
        data[i] = parse_time_column(data[i]) if i == 0 else str2float(data[i])
    return dict(zip(column_name, data))


def parse_time_column(inp):
    """
    把 baostock 的时间字符串解析成 CTime。

    按长度区分三种形态：
        len==10 -> "2021-09-13"            天级别
        len==17 -> "20210902113000000"     分钟级别，精确到毫秒
        len==19 -> "2021-09-13 11:30:00"   带分隔符的完整时间戳

    Args:
        inp: baostock 返回的时间字符串

    Returns:
        CTime: 天级别不带时分（auto 默认 True，会被折算到当天 23:59）；
            分钟级别带真实时分，语义是这根K线的结束时间

    Raises:
        CChanException: 长度不属于上述三种之一（ErrCode.SRC_DATA_FORMAT_ERROR）

    Note:
        auto 是**显式**传入的：只有天级别串（len==10）才启用 CTime 的
        「0 点折算到当日 23:59」自适应；分钟串一律 auto=False。
        这样即使某根分钟K线的结束时间恰好是 00:00（夜盘/期货场景），
        也不会被误判成天级别而被折到 23:59 —— 那会让它小于当日日线，
        破坏「日线时间戳大于当日所有分钟线」这个跨级别比较的前提。
    """
    # 20210902113000000
    # 2021-09-13
    # 天级别：带分隔线的 "YYYY-MM-DD"，时分置 0 后由 auto 折算成 23:59
    if len(inp) == 10:
        year = int(inp[:4])
        month = int(inp[5:7])
        day = int(inp[8:10])
        hour = minute = 0
    # 分钟级：紧凑串 YYYYMMDDHHMMSSsss，没有分隔符，所以切片全是 2 位对齐
    elif len(inp) == 17:
        year = int(inp[:4])
        month = int(inp[4:6])
        day = int(inp[6:8])
        hour = int(inp[8:10])
        minute = int(inp[10:12])
    # 完整时间戳 "YYYY-MM-DD HH:MM:SS"，分隔符各占一位，切片位置与上面两种都不同
    elif len(inp) == 19:
        year = int(inp[:4])
        month = int(inp[5:7])
        day = int(inp[8:10])
        hour = int(inp[11:13])
        minute = int(inp[14:16])
    # 其余长度一律不认：静默返回 0 会让脏时间混进K线序列，宁可直接抛错
    else:
        raise CChanException(f"unknown time column from baostock:{inp}", ErrCode.SRC_DATA_FORMAT_ERROR)
    # 只有天级别串才启用 auto（0点 -> 当日23:59）；分钟串显式关掉，避免 00:00 收盘被误折
    return CTime(year, month, day, hour, minute, auto=(len(inp) == 10))


def GetColumnNameFromFieldList(fileds: str):
    """
    把 "date,open,high,..." 这样的 baostock 字段名串翻译成 DATA_FIELD 列表。

    Args:
        fileds: 英文逗号分隔的 baostock 字段名，取值只能是下面映射表里已有的 key

    Returns:
        List[str]: 对应的 DATA_FIELD 常量列表，顺序与入参严格一致
            （顺序很重要，create_item_dict 靠 zip 按位置配对）

    Raises:
        KeyError: 出现映射表不认识的字段名（比如大小写写成 "Date"）

    Note:
        baostock 的时间字段有两种名字：天级别叫 date、分钟级叫 time，
        这里统一映射成同一个 DATA_FIELD.FIELD_TIME；
        amount 是成交额 -> FIELD_TURNOVER，turn 是换手率 -> FIELD_TURNRATE。
    """
    _dict = {
        "time": DATA_FIELD.FIELD_TIME,
        "date": DATA_FIELD.FIELD_TIME,
        "open": DATA_FIELD.FIELD_OPEN,
        "high": DATA_FIELD.FIELD_HIGH,
        "low": DATA_FIELD.FIELD_LOW,
        "close": DATA_FIELD.FIELD_CLOSE,
        "volume": DATA_FIELD.FIELD_VOLUME,
        "amount": DATA_FIELD.FIELD_TURNOVER,
        "turn": DATA_FIELD.FIELD_TURNRATE,
    }
    return [_dict[x] for x in fileds.split(",")]


class CBaoStock(CCommonStockApi):
    """
    证券宝（baostock）A股数据源，实现 CCommonStockApi 的四条契约。

    Note:
        登录态挂在**类变量** is_connect 上：do_init/do_close 都是 classmethod，
        而 CChan 多级别取数时会为每个级别各 new 一个实例，
        只有用类变量才能让整轮 load 只登录、只登出一次。
    """
    is_connect = None

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        """
        Args:
            code: baostock 格式代码，如 "sz.000001" / "sh.600000"；原样透传不做归一化
            k_type: KL_TYPE，默认日线；只支持 日/周/月/5M/15M/30M/60M
            begin_date: 起始日期 "YYYY-MM-DD"，None 表示不限
            end_date: 结束日期 "YYYY-MM-DD"，None 表示取到最新
            autype: AUTYPE 复权方式，默认前复权 QFQ

        Note:
            只是把参数透传给基类；基类末尾会自动调 SetBasciInfo()，
            所以构造完成的瞬间就已经联网查过一次基本信息了。
        """
        super(CBaoStock, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self):
        """
        取数主入口：向 baostock 查历史K线，按时间从旧到新逐根产出。

        Yields:
            CKLine_Unit: 一根K线；idx 与 kl_type 由上层 CChan.load_stock_data 统一回填

        Raises:
            CChanException: baostock 返回的 error_code 不是 '0'（ErrCode.SRC_DATA_FORMAT_ERROR）；
                分钟级别但标的不是股票（指数没有分钟线数据，ErrCode.SRC_DATA_NOT_FOUND）；
                不支持的级别或复权方式（ErrCode.PARA_ERROR）；
                查询成功但一行数据都没有（ErrCode.NO_DATA）

        Note:
            rs 是 baostock 的一次性游标，只能顺序消费一次；
            本函数是生成器，每次被迭代都会重新发一次查询，
            配合 Chan 的「边取边算」，无论历史多长内存都不线性膨胀。
        """
        # 天级别以上才有详细交易信息
        if kltype_lt_day(self.k_type):
            # is_stock 由 SetBasciInfo 查得；baostock 的指数根本没有分钟线
            if not self.is_stock:
                raise CChanException("没有获取到数据，注意指数是没有分钟级别数据的！", ErrCode.SRC_DATA_NOT_FOUND)
            # 分钟级 baostock 只给这五列，量/额/换手要了也返回不到
            fields = "time,open,high,low,close"
        else:
            # 天级别字段才全：volume 成交量 / amount 成交额 / turn 换手率
            fields = "date,open,high,low,close,volume,amount,turn"
        # baostock 的 adjustflag：1=后复权 2=前复权 3=不复权，顺序和 AUTYPE 对不上，别写反
        autype_dict = {AUTYPE.QFQ: "2", AUTYPE.HFQ: "1", AUTYPE.NONE: "3"}
        # 裸字典直查会把非法 AUTYPE 变成 KeyError，这里统一成带 ErrCode 的异常
        if self.autype not in autype_dict:
            raise CChanException(f"baostock 不支持的复权方式: {self.autype}", ErrCode.PARA_ERROR)
        rs = bs.query_history_k_data_plus(
            code=self.code,
            fields=fields,
            start_date=self.begin_date,
            end_date=self.end_date,
            frequency=self.__convert_type(),
            adjustflag=autype_dict[self.autype],
        )
        # error_code 是字符串，'0' 才算成功
        if rs.error_code != '0':
            raise CChanException(f"baostock 查询{self.code}失败: {rs.error_msg}", ErrCode.SRC_DATA_FORMAT_ERROR)
        # 原写法 `while rs.error_code == '0' and rs.next()` 在游标中途出错时会静默终止循环
        # （既不抛错也不补齐），表现为"数据悄悄少了一截"。改成先推进游标再单独判错。
        got_any = False
        while rs.next():
            if rs.error_code != '0':
                raise CChanException(f"baostock 读取{self.code}数据中断: {rs.error_msg}", ErrCode.SRC_DATA_FORMAT_ERROR)
            got_any = True
            # 一行字符串 -> 字段字典（第 0 列按约定是时间）-> K线单元
            yield CKLine_Unit(create_item_dict(rs.get_row_data(), GetColumnNameFromFieldList(fields)))
        # 查询成功但一行都没有：早一点报出来，别让上层拿着空序列报更模糊的错
        if not got_any:
            raise CChanException(
                f"baostock 未取到 {self.code} 的 {self.k_type} 数据（{self.begin_date}~{self.end_date}）",
                ErrCode.NO_DATA,
            )

    def SetBasciInfo(self):
        """
        查标的基本信息，填 self.name 与 self.is_stock。

        Raises:
            CChanException: baostock 返回的 error_code 不是 '0'（ErrCode.SRC_DATA_FORMAT_ERROR）；
                error_code 是 '0' 但结果为空，通常是代码不存在或已退市
                （ErrCode.SRC_DATA_NOT_FOUND，可被 auto_skip_illegal_sub_lv 跳过）

        Note:
            baostock 的 stock_type：1=普通股 / 2=指数 / 3=其它 / 4=可转债 / 5=ETF，
            只有股票（'1'）才有分钟级别数据，所以 is_stock 是 get_kl_data 的前置闸门；
            由基类 __init__ 末尾自动调用，即**每实例化一次就联网查一次**。
        """
        rs = bs.query_stock_basic(code=self.code)
        if rs.error_code != '0':
            raise CChanException(f"baostock 查询{self.code}基本信息失败: {rs.error_msg}", ErrCode.SRC_DATA_FORMAT_ERROR)
        # 原先直接 6 元解包：代码不存在/退市时 baostock 会返回 error_code='0' 且结果为空，
        # 这时抛的是莫名其妙的 "not enough values to unpack"。先确认真有一行数据再解包。
        if not rs.next():
            raise CChanException(f"baostock 查不到标的信息（代码不存在或已退市）: {self.code}", ErrCode.SRC_DATA_NOT_FOUND)
        # 固定 6 列：代码/名称/上市日/退市日/类型/状态
        code, code_name, ipoDate, outDate, stock_type, status = rs.get_row_data()
        self.name = code_name
        self.is_stock = (stock_type == '1')

    @classmethod
    def do_init(cls):
        """
        建立 baostock 会话（CChan.load 取数前调用一次，靠 is_connect 做幂等）。

        Raises:
            CChanException: 登录失败（ErrCode.ENV_CONF_ERR）

        Note:
            bs.login() 返回的是 baostock 的结果对象（带 error_code/error_msg）而不是布尔，
            所以必须判 error_code —— 只看返回值真假的话，登录失败会静默通过，
            一直拖到第一次取数才炸，且错误信息与真实原因（未登录）对不上。
        """
        if not cls.is_connect:
            lg = bs.login()
            if getattr(lg, "error_code", '0') != '0':
                cls.is_connect = None
                raise CChanException(f"baostock 登录失败: {getattr(lg, 'error_msg', lg)}", ErrCode.ENV_CONF_ERR)
            cls.is_connect = True

    @classmethod
    def do_close(cls):
        """
        登出 baostock（挂在 CChan.load 的 finally 里，必定执行到）。

        Note:
            登出失败只打印告警不抛异常 —— 到这里取数流程已经结束，
            没必要因为释放资源失败而让整个 CChan.load 报错；
            但无论成功与否都会把 is_connect 置回 None，
            保证下一次 CChan.load 会重新走一遍 do_init 建会话。
        """
        if cls.is_connect:
            lo = bs.logout()
            if getattr(lo, "error_code", '0') != '0':
                print(f"[WARNING] baostock 登出失败: {getattr(lo, 'error_msg', lo)}")
            cls.is_connect = None

    def __convert_type(self):
        """
        把项目的 KL_TYPE 映射成 baostock 的 frequency 参数。

        Returns:
            str: 天/周/月是 'd'/'w'/'m'，分钟线是 '5'/'15'/'30'/'60'

        Raises:
            CChanException: baostock 不支持的级别（K_1M / K_3M / K_10M / 季线 / 年线等），
                ErrCode.PARA_ERROR
        """
        # baostock 的 frequency：天/周/月用字母，分钟线用数字字符串
        _dict = {
            KL_TYPE.K_DAY: 'd',
            KL_TYPE.K_WEEK: 'w',
            KL_TYPE.K_MON: 'm',
            KL_TYPE.K_5M: '5',
            KL_TYPE.K_15M: '15',
            KL_TYPE.K_30M: '30',
            KL_TYPE.K_60M: '60',
        }
        if self.k_type not in _dict:
            raise CChanException(f"baostock 不支持的K线级别: {self.k_type}", ErrCode.PARA_ERROR)
        return _dict[self.k_type]
