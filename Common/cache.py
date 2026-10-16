"""
实例级方法结果缓存（memoize）模块。

提供一个装饰器类 make_cache：被装饰的方法在**同一个实例**上第一次调用后结果被存下来，
之后直接返回缓存值。缠论里笔/合并K线的端点、涨跌幅、K线根数等属性会被反复读取
（买卖点、特征、画图都要用），且一旦笔的端点确定就不变，所以很适合这样缓存。

用法：
    class CBi:
        @make_cache
        def get_begin_val(self): ...

        def clean_cache(self):
            # 端点被 update_new_end 修改后必须手动清空，否则会拿到旧值
            self._memoize_cache = {}

Note:
    缓存键只用了函数对象字符串 str(func)，不包含调用参数，
    因此它只适用于「除 self 外无入参」的方法。
"""
import inspect
import types


class make_cache:
    """装饰器（描述符）类：把方法的返回值按实例缓存到 instance._memoize_cache 上。"""

    def __init__(self, func):
        """
        Args:
            func: 被装饰的函数对象

        Raises:
            Exception: 被装饰对象不是「只接受 self」的实例方法时报错
                       （提示文案仍写着旧名 @memoize）
        """
        self.func = func

        fargspec = inspect.getfullargspec(func)
        if len(fargspec.args) != 1 or fargspec.args[0] != "self":
            raise Exception("@memoize must be `(self)`")

        # set key for this function
        # 缓存键只区分函数本身，不区分调用参数：同一实例同一函数永远只存一份结果
        self.func_key = str(func)

    def __get__(self, instance, cls):
        """
        描述符取值：把自身绑定到 instance 上，使后续调用能拿到实例。

        Args:
            instance: 访问该属性的实例；通过类访问时为 None
            cls: 拥有该属性的类

        Returns:
            types.MethodType: 绑定了 instance 的可调用对象

        Raises:
            Exception: 通过类而不是实例访问时报错

        Note:
            顺手在实例上懒创建 _memoize_cache 字典；外部可把它置空来主动失效缓存。
        """
        if instance is None:
            raise Exception("@memoize's must be bound")

        if not hasattr(instance, "_memoize_cache"):
            setattr(instance, "_memoize_cache", {})

        return types.MethodType(self, instance)

    def __call__(self, *args, **kwargs):
        """
        实际调用入口：命中缓存直接返回，未命中则算一次并写回缓存。

        Args:
            *args: 第一个元素是 instance（由 __get__ 绑定进来）
            **kwargs: 透传给原函数

        Returns:
            原方法的返回值（可能是本次计算值，也可能是缓存值）
        """
        instance = args[0]
        cache = instance._memoize_cache

        if self.func_key in cache:
            return cache[self.func_key]

        result = self.func(*args, **kwargs)
        cache[self.func_key] = result
        return result
