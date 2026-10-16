"""
数据源抽象基类模块（CCommonStockApi）。

这里定义了 chan.py 与所有行情数据源之间的唯一契约层：
CChan 只认这个基类，完全不关心底层是 baostock / ccxt / 本地 CSV / 富途，
还是用户自己写的取数类。想接新数据源，继承本类并实现下面四个成员即可。

契约四条（子类必须照做）：
    1. get_kl_data()  —— 唯一的取数出口，必须实现成生成器，按时间从旧到新
                        逐根 yield CKLine_Unit；
    2. SetBasciInfo() —— 填标的基本信息（self.name / self.is_stock 等），
                        由 __init__ 末尾自动调用，取不到就直接 pass；
    3. do_init()      —— classmethod，CChan.load() 在取数前统一调用一次，
                        负责登录 / 建立连接（见 CChan.load 的第一句）；
    4. do_close()     —— classmethod，CChan.load() 的 finally 里必定调用，
                        负责关闭连接，所以它可以假设自己一定会被执行到。

自定义数据源接入方式：继承本类写好实现，然后在构造 CChan 时传
data_src="custom:模块名.类名"（模块需放在 DataAPI/ 下，见 Chan.GetStockAPI）。

Note:
    - get_kl_data 必须是生成器（函数体里有 yield）：Chan.py 是边取边算的，
      这样无论历史多长内存都不会线性膨胀；
    - 本基类不检查 __init__ 签名，但 CChan.get_load_stock_iter 固定用
      code / k_type / begin_date / end_date / autype 五个关键字参数实例化，
      子类 __init__ 既要接住这五个名字，也要记得调 super().__init__ 落库；
    - 基类的四个方法都是 @abc.abstractmethod，只实现部分的话实例化会直接 TypeError；
    - 多级别求值时 CChan 会为每个级别各 new 一个实例，因此取数过程中发生的
      IO/登录成本应尽量放在 do_init 里做一次。
"""

import abc
from typing import Iterable

from KLine.KLine_Unit import CKLine_Unit


class CCommonStockApi:
    """
    行情数据源抽象基类：单个标的单个级别的取数契约。

    代表「一只代码 + 一个 KL_TYPE 级别 + 一段日期区间」的一次取数；
    实例一旦构造完成就可以被 get_kl_data() 反复迭代（一次性消费）。

    Note:
        带 @abc.abstractmethod 的方法未实现时，实例化会抛 TypeError，
        相当于由 abc 替本模块做「子类必须实现什么」的静态校验。
    """

    def __init__(self, code, k_type, begin_date, end_date, autype):
        """
        Args:
            code: 标的代码，格式由具体数据源自行约定（如 "sz.000001"、"HK.00700"、"BTC/USDT"）
            k_type: KL_TYPE，本次要取的K线级别，决定周期参数与时间字段语义
            begin_date: 起始时间字符串，格式由数据源解释；None 表示不限
            end_date: 结束时间字符串，规则同 begin_date；None 表示取到最新
            autype: AUTYPE 复权方式；不支持复权的数据源可以直接忽略它

        Note:
            末尾会调用 SetBasciInfo()，所以子类若在 Super 之前没准备好必要字段，
            或因查询失败需要在 SetBasciInfo 里访问 self.code 之外的东西，要注意顺序：
            本基类只保证了「调用 SetBasciInfo 时 code 已经被赋值」。
        """
        self.code = code
        self.name = None
        self.is_stock = None
        self.k_type = k_type
        self.begin_date = begin_date
        self.end_date = end_date
        self.autype = autype
        # 基本信息统一在最后查：保证子类构造时 self.code/k_type 等已就位
        self.SetBasciInfo()

    @abc.abstractmethod
    def get_kl_data(self) -> Iterable[CKLine_Unit]:
        """
        取数主入口（抽象方法，子类必须重写）。

        Yields:
            CKLine_Unit: 一根K线，必须按时间严格递增；idx 与 kl_type 由上层
                Chan.load_stock_data 统一回填，这里不用管。

        Note:
            时间字段要用 CTime 构造，且留意 auto 语义：天级别以下（分钟/秒线）
            描述的是这根K线的**结束**时间，天级别及以上只到日期
            （详见 Common/CTime.py）；
            产出顺序不满足严格递增时，Chan 会抛 ErrCode.KL_NOT_MONOTONOUS。
        """
        pass

    @abc.abstractmethod
    def SetBasciInfo(self):
        """
        查询并填充标的基本信息（抽象方法，子类必须重写）。

        Note:
            在 __init__ 里被自动调用；约定至少维护两个字段：
                self.name     —— 标的名称，用于绘图标题
                self.is_stock —— 是否是普通股票（False 通常是指数/ETF，
                                 如 baostock 的指数没有分钟级别数据，取数时要先判它）
            不需要联网查询的数据源实现为空函数 pass 即可（如 csvAPI）。
        """
        pass

    @classmethod
    @abc.abstractmethod
    def do_init(cls):
        """
        整体取数前的初始化钩子（抽象方法，子类必须重写）。

        Note:
            由 CChan.load() 在实例化数据源之前调用一次，且调用方是**类**而不是实例，
            所以只能操作类属性；典型实现是把 login / 建立连接的结果挂在类变量上，
            并用类变量做幂等判断（见 CBaoStock.do_init 的 is_connect）。
        """
        pass

    @classmethod
    @abc.abstractmethod
    def do_close(cls):
        """
        取数结束后的清理钩子（抽象方法，子类必须重写）。

        Note:
            挂在 CChan.load() 的 finally 里，无论中间是否抛异常都会执行到，
            所以这里是释放连接/退出的唯一可靠位置；同样只能操作类属性。
            不需要释放资源的数据源实现为空函数 pass 即可。
        """
        pass
