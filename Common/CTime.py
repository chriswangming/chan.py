"""
自定义时间类型模块（CTime）。

缠论里K线时间有两类语义，不能用 datetime 直接替代：
    1. 天级别以下（分钟/秒线）：时间戳描述的是这根K线的**结束时间**，
       例如 60 分钟线每天的第一根是 10:30 而不是 9:30，最后一根是 15:00；
    2. 天级别及以上（日/周/月/季/年线）：只给到年月日，不带时分，
       为了让「日线」能包住当天的所有分钟K线，auto=True 时把它映射到当天 23:59
       （见 set_timestamp），这样日线时间戳恒大于当天任意分钟线时间戳。

auto 标志就是这个「对天的自适应理解」开关：
    auto=True  —— 遇到 hour==0 and minute==0 就当作天级别，落到 23:59；
    auto=False —— 老老实实按 00:00 算，toDate() 需要这个语义所以显式传 False。

Note:
    时间比较全部走内部数值字段 ts（float 时间戳），因此跨月、跨年、
    跨时区都不需要手写进位/借位逻辑，datetime 构造时自然处理；
    但反过来，非法日期（如 2 月 30 日）会由 datetime 抛 ValueError。
"""
from datetime import datetime


class CTime:
    """缠论专用的K线时间：支持「天级别结束时间」语义，且只按 ts 比较大小。"""

    def __init__(self, year, month, day, hour, minute, second=0, auto=True):
        """
        Args:
            year: 年
            month: 月
            day: 日
            hour: 时；天级别K线传 0
            minute: 分；天级别K线传 0
            second: 秒，默认 0
            auto: 是否启用「天级别自适应」；True 时 hour/minute 均为 0 会按当天 23:59 计算
        """
        self.year = year
        self.month = month
        self.day = day
        self.hour = hour
        self.minute = minute
        self.second = second
        self.auto = auto  # 自适应对天的理解
        self.set_timestamp()  # set self.ts

    def __str__(self):
        """
        转成人类可读字符串。

        Returns:
            str: 天级别（时分均为0）输出 "YYYY/MM/DD"，否则输出 "YYYY/MM/DD HH:MM"
        """
        if self.hour == 0 and self.minute == 0:
            return f"{self.year:04}/{self.month:02}/{self.day:02}"
        else:
            return f"{self.year:04}/{self.month:02}/{self.day:02} {self.hour:02}:{self.minute:02}"

    def to_str(self):
        """
        与 __str__ 同格式的字符串输出，供需要显式调用（如日志、图例）的场景使用。

        Returns:
            str: "YYYY/MM/DD" 或 "YYYY/MM/DD HH:MM"
        """
        if self.hour == 0 and self.minute == 0:
            return f"{self.year:04}/{self.month:02}/{self.day:02}"
        else:
            return f"{self.year:04}/{self.month:02}/{self.day:02} {self.hour:02}:{self.minute:02}"

    def toDateStr(self, splt=''):
        """
        只取日期部分拼接成字符串，常用于拼数据源的日期参数或文件名。

        Args:
            splt: 年月日之间的分隔符，默认空串（即 "20240131"）；传 '-' 得 "2024-01-31"

        Returns:
            str: 形如 "YYYYspltMMspltdd" 的日期串
        """
        return f"{self.year:04}{splt}{self.month:02}{splt}{self.day:02}"

    def toDate(self):
        """
        退化成「纯日期」的 CTime。

        Note:
            这里必须 auto=False，否则 00:00 会被 set_timestamp 当成天级别
            再折回 23:59，就得不到「当天零点」这个语义了。

        Returns:
            CTime: 时分秒归零、且不启用天级别自适应的新对象
        """
        return CTime(self.year, self.month, self.day, 0, 0, auto=False)

    def set_timestamp(self):
        """
        根据当前字段重算 self.ts（浮点时间戳），构造后每次改字段都需重新调用。

        Note:
            hour/minute 均为 0 且 auto=True 时按当天 23:59 计，
            目的是让天级别K线的时间戳大于同日所有分钟级别K线，
            这样父子级别的时间比较（如 K线归属、时间单调性检查）才成立。
        """
        if self.hour == 0 and self.minute == 0 and self.auto:
            # 天级别：取当天最后一分钟，保证「父级别K线时间 > 当日子K线时间」
            date = datetime(self.year, self.month, self.day, 23, 59, self.second)
        else:
            date = datetime(self.year, self.month, self.day, self.hour, self.minute, self.second)
        self.ts = date.timestamp()

    def __gt__(self, t2):
        """
        大于比较：直接比较内部时间戳 ts。

        Args:
            t2: 另一个 CTime

        Returns:
            bool: self 严格晚于 t2
        """
        return self.ts > t2.ts

    def __ge__(self, t2):
        """
        大于等于比较：直接比较内部时间戳 ts。

        Args:
            t2: 另一个 CTime

        Returns:
            bool: self 晚于或等于 t2
        """
        return self.ts >= t2.ts
