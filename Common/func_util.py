"""
通用小工具函数模块。

放一些跨模块复用、又不值得单独成类的纯函数：K线级别的大小与顺序判定、
笔方向取反、区间是否重叠、字符串转浮点，以及把 inf 转成可写回源码字面量的小工具。

Note:
    级别比较依赖 Common/CEnum.py 里 KL_TYPE 的整数值序（值越大级别越高），
    因此级别判定本质上就是整数比较，改动 KL_TYPE 的取值会直接影响这里。
"""
from .CEnum import BI_DIR, KL_TYPE


def kltype_lt_day(_type: KL_TYPE):
    """
    判断级别是否严格小于日线（即秒/分钟级别）。

    Args:
        _type: KL_TYPE

    Returns:
        bool: 天级别以下为 True（K_60M 等），日线及以上为 False
    """
    return _type.value < KL_TYPE.K_DAY.value


def kltype_lte_day(_type: KL_TYPE):
    """
    判断级别是否小于等于日线。

    Args:
        _type: KL_TYPE

    Returns:
        bool: 日线（K_DAY）本身也返回 True，周线及以上为 False

    Note:
        与 kltype_lt_day 的差别就在是否包含日线；CChan.check_kl_consitent
        用「父子级别都 <= 日线」来筛选需要校验同一天的级别对。
    """
    return _type.value <= KL_TYPE.K_DAY.value


def check_kltype_order(type_list):
    """
    校验级别列表是否严格从大级别排到小级别。

    Args:
        type_list: List[KL_TYPE]，如 [KL_TYPE.K_DAY, KL_TYPE.K_60M]

    Raises:
        AssertionError: 顺序不是严格递减，或列表为空时取 type_list[0] 越界
    """
    last_lv = type_list[0].value
    for kl_type in type_list[1:]:
        assert kl_type.value < last_lv, "lv_list的顺序必须从大级别到小级别"
        last_lv = kl_type.value


def revert_bi_dir(dir):
    """
    笔方向取反。

    Args:
        dir: BI_DIR.UP 或 BI_DIR.DOWN

    Returns:
        BI_DIR: UP -> DOWN，DOWN -> UP
    """
    return BI_DIR.DOWN if dir == BI_DIR.UP else BI_DIR.UP


def has_overlap(l1, h1, l2, h2, equal=False):
    """
    判断两个价格区间 [l1, h1] 与 [l2, h2] 是否有交集，用于中枢重叠、笔的重叠等判定。

    Args:
        l1: 区间1下沿
        h1: 区间1上沿
        l2: 区间2下沿
        h2: 区间2上沿
        equal: True 表示允许端点相等也算重叠（>=），False 表示必须严格越过（>）

    Returns:
        bool: 有交集为 True

    Note:
        三元表达式优先级低于 and，实际等价于
        `(h2 >= l1 and h1 >= l2) if equal else (h2 > l1 and h1 > l2)`。
    """
    return h2 >= l1 and h1 >= l2 if equal else h2 > l1 and h1 > l2


def str2float(s):
    """
    把字符串安全转成 float，失败时退化成 0.0（数据源里常见空串/占位符）。

    Args:
        s: 待转换的值，通常是数据源返回的原始字符串

    Returns:
        float: 转换成功返回对应值；ValueError 时返回 0.0

    Note:
        只捕获 ValueError，传 None 会抛 TypeError；
        "nan"/"inf" 这类字符串会原样转成对应的浮点特殊值而非 0.0。
    """
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_inf(v):
    """
    把正负无穷转成可写回源码的字面量字符串，供配置导出/代码生成场景使用。

    Args:
        v: 任意值，通常先经过 str2float 处理

    Returns:
        原值；若 v 是 float('inf') 返回 'float("inf")'，是 float('-inf') 返回 'float("-inf")'

    Note:
        第一个分支命中后 v 已被改成字符串，第二个 if 再用它跟浮点数比较必然为假，
        两个分支互斥，所以这里用 if...if 而非 elif 不会出错，只是可读性稍差。
    """
    if isinstance(v, float):
        if v == float("inf"):
            v = 'float("inf")'
        if v == float("-inf"):
            v = 'float("-inf")'
    return v
