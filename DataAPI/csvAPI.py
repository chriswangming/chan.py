"""
本地 CSV 文件数据源模块（CSV_API）。

没有网络请求、不需要任何第三方依赖，纯粹把本地行情文件喂给缠论引擎。
适合：回测结果复现、单标的离线调试、以及把自己系统导出的数据按约定格式接进来。

文件路径约定（见 get_kl_data）：
    文件名 = {code}_{k_type}.csv
        code   —— 就是传给 CChan 的 code 原样（含市场前缀，如 sz.000001、HK.00700）
        k_type —— 级别名去掉 "K_" 前缀再转小写：K_DAY->day、K_60M->60m、K_WEEK->week
    目录   —— 仓库根目录：代码里是 f"{cur_path}/../..."，cur_path 是本文件所在目录，
              即 DataAPI 的上一级。例：<repo>/sz.000001_day.csv
    文件不存在时抛 ErrCode.SRC_DATA_NOT_FOUND。

列格式约定（由 __init__ 里的 self.columns 决定，**顺序固定**）：
    默认只要 5 列：time, open, high, low, close
    多余的列（volume / turnover / turnrate）目前被注释掉了：
    要用就必须同时放开头部的注释，并保证 CSV 里也有同数量更多列；
    列数与 self.columns 长度不一致会抛 ErrCode.SRC_DATA_FORMAT_ERROR。
    分隔符是英文逗号，不含引号.

时间列（time）支持三种写法，由 parse_time_column 负责识别：
    "2021-09-13"           10 位 —— 只有日期，整天数据
    "20210902113000000"    17 位 —— 紧凑串，精确到分钟（末尾多余字符被忽略）
    "2021-09-02 11:30:00"  19 位 —— 标准日期时间串
    其它长度直接抛异常。

Note:
    - headers_exist 默认 True（第一行当表头跳过）；若你的文件没有表头，
      实例化后把它置成 False 再取数。
    - begin_date / end_date 是按**字符串大小**比较来裁剪的，
      所以传入的日期格式必须与 CSV 里时间列的格式一致，否则过滤会失效甚至全部漏掉。
    - 本数据源不做任何复权处理：文件里是什么价格，算出来的就是什么价格。
"""
import os

from Common.CEnum import DATA_FIELD, KL_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Common.func_util import str2float
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi


def create_item_dict(data, column_name):
    """
    把一行 CSV 的字符串数组转成 CKLine_Unit 需要的字段字典。

    Args:
        data: 一行被 split(",") 后的原始字符串列表；本函数会**就地**把元素换成
              CTime（时间列）或 float（其余列）
        column_name: 与 data 等长的 DATA_FIELD 列表，即 self.columns

    Returns:
        Dict[DATA_FIELD, Any]: key 为 DATA_FIELD 常量，value 已转成对应类型

    Note:
        用 `column_name[i] == DATA_FIELD.FIELD_TIME` 判断时间列，所以时间列不一定要
        排在第一位（csvAPI 的 self.columns 也确实把它放在首位）。
    """
    for i in range(len(data)):
        data[i] = parse_time_column(data[i]) if column_name[i] == DATA_FIELD.FIELD_TIME else str2float(data[i])
    return dict(zip(column_name, data))


def parse_time_column(inp):
    """
    解析 CSV 时间列的三种写法，返回 CTime。

    Args:
        inp: 时间列原始字符串

    Returns:
        CTime: 只到日期时 hour/minute 为 0；带时分秒时按原值填入

    Raises:
        Exception: 字符串长度不是 10 / 17 / 19 时抛出（未知格式）

    Note:
        这里没有给 auto 传参，用的是 CTime 默认的 auto=True：天级别数据
        （hour=minute=0）会被折算成当天 23:59，从而天然大于当天的分钟K线；
        分钟级别数据只要不是恰好 00:00，就按真实时分算，语义也正确。
    """
    # 20210902113000000
    # 2021-09-13
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
        raise Exception(f"unknown time column from csv:{inp}")
    return CTime(year, month, day, hour, minute)


class CSV_API(CCommonStockApi):
    """
    本地 CSV 数据源：按约定文件名从仓库根目录读取某标的某级别的K线。

    用法：CChan(code="SZ.000001", data_src=DATA_SRC.CSV, lv_list=[...])，
    对应的文件需是 <repo>/SZ.000001_day.csv 这样。
    """

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=None):
        """
        Args:
            code: 标的代码，直接参与文件名拼接（含市场前缀）
            k_type: KL_TYPE，同时决定文件名的级别后缀
            begin_date: 起始日期字符串，格式要与 CSV 时间列一致；None 不过滤
            end_date: 结束日期字符串，同上；None 不过滤
            autype: 复权方式，CSV 源忽略它（默认为 None 即可）

        Note:
            self.columns 必须在 super().__init__ 之前设定：父类构造里会用到
            time_column_idx（列数不匹配校验依赖它）。
        """
        self.headers_exist = True  # 第一行是否是标题，如果是数据，设置为False
        self.columns = [
            DATA_FIELD.FIELD_TIME,
            DATA_FIELD.FIELD_OPEN,
            DATA_FIELD.FIELD_HIGH,
            DATA_FIELD.FIELD_LOW,
            DATA_FIELD.FIELD_CLOSE,
            # DATA_FIELD.FIELD_VOLUME,
            # DATA_FIELD.FIELD_TURNOVER,
            # DATA_FIELD.FIELD_TURNRATE,
        ]  # 每一列字段
        self.time_column_idx = self.columns.index(DATA_FIELD.FIELD_TIME)
        super(CSV_API, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self):
        """
        按行读取 CSV 并逐根产出 CKLine_Unit。

        Yields:
            CKLine_Unit: 一根K线，字段由 create_item_dict 根据 self.columns 组装

        Raises:
            CChanException: 文件不存在（SRC_DATA_NOT_FOUND），
                或某行列数与 self.columns 不一致（SRC_DATA_FORMAT_ERROR）

        Note:
            起止时间是在**字符串层面**比较的，且第 i 行的时间继续往前读、不满足
            区间只是 continue，因此 CSV 里的数据应当按时间升序排列。
        """
        cur_path = os.path.dirname(os.path.realpath(__file__))
        # K_DAY -> day、K_60M -> 60m：级别名去掉 "K_" 前缀再小写，拼成文件名后缀
        k_type = self.k_type.name[2:].lower()
        file_path = f"{cur_path}/../{self.code}_{k_type}.csv"
        if not os.path.exists(file_path):
            raise CChanException(f"file not exist: {file_path}", ErrCode.SRC_DATA_NOT_FOUND)

        for line_number, line in enumerate(open(file_path, 'r')):
            if self.headers_exist and line_number == 0:
                continue
            data = line.strip("\n").split(",")
            if len(data) != len(self.columns):
                raise CChanException(f"file format error: {file_path}", ErrCode.SRC_DATA_FORMAT_ERROR)
            # 时间区间过滤：原始字符串比较，格式必须与 begin_date/end_date 一致
            if self.begin_date is not None and data[self.time_column_idx] < self.begin_date:
                continue
            if self.end_date is not None and data[self.time_column_idx] > self.end_date:
                continue
            yield CKLine_Unit(create_item_dict(data, self.columns))

    def SetBasciInfo(self):
        """
        CSV 源没有可查的基本信息，留空实现。

        Note:
            契约要求必须实现；这里 name / is_stock 保持基类给的 None。
        """
        pass

    @classmethod
    def do_init(cls):
        """
        读本地文件无需任何初始化，留空实现。
        """
        pass

    @classmethod
    def do_close(cls):
        """
        没有连接需要释放，留空实现。
        """
        pass
