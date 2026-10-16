"""
缠论计算的**全局配置**模块：把用户传入的一个扁平 dict（通常来自命令行 -c 参数或策略脚本）
拆解、校验、分发到各个子模块的配置对象里。

配置分发的层次：
    CChanConfig
      ├─ bi_conf      -> Bi/BiConfig.py      笔的判定参数
      ├─ seg_conf     -> Seg/SegConfig.py    线段算法与左侧线段方法
      ├─ zs_conf      -> ZS/ZSConfig.py      中枢的合并/扩展参数
      ├─ bs_point_conf       -> BuySellPoint/BSPointConfig.py  **笔级别**买卖点（买侧 b_conf / 卖侧 s_conf）
      └─ seg_bs_point_conf   -> BuySellPoint/BSPointConfig.py  **线段级别**买卖点（同上分买卖两侧）

配置键的命名约定（见 set_bsp_config）：
    裸键名        同时作用于买侧与卖侧
    xxx-buy       只作用于笔级别的买点
    xxx-sell      只作用于笔级别的卖点
    xxx-segbuy    只作用于线段级别的买点
    xxx-segsell   只作用于线段级别的卖点
    xxx-seg       同时作用于线段级别的买与卖

注意：所有"被识别并读取过"的键都会从 dict 里删掉，最后 check() 时若 dict 非空即说明
存在拼写错误或不支持的参数，直接抛异常，避免配错项却被静默忽略。
"""

from typing import List

from Bi.BiConfig import CBiConfig
from BuySellPoint.BSPointConfig import CBSPointConfig
from Common.CEnum import TREND_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.func_util import _parse_inf
from Math.BOLL import BollModel
from Math.Demark import CDemarkEngine
from Math.KDJ import KDJ
from Math.MACD import CMACD
from Math.RSI import RSI
from Math.TrendModel import CTrendModel
from Seg.SegConfig import CSegConfig
from ZS.ZSConfig import CZSConfig


class CChanConfig:
    """缠论计算的全局配置对象：持有各子模块配置，并负责把扁平 dict 分发下去。"""

    def __init__(self, conf=None):
        """
        用一个扁平 dict 初始化全部配置。

        Args:
            conf: 配置字典；None 等价于空字典（全部走默认值）。
                  字典会被**就地消费**（读取过的键会被删除），调用后原 dict 基本被清空。

        Raises:
            CChanException: 存在无法识别的配置键（ErrCode.PARA_ERROR）

        Note:
            构造顺序是有讲究的：先由各子模块的 conf.get(...) 把"自己认识的键"取走，
            剩下的键全部交给 set_bsp_config 处理（买卖点参数支持 -buy/-sell 等后缀），
            最后 conf.check() 兜底——还有剩的就是非法键。
        """
        if conf is None:
            conf = {}
        conf = ConfigWithCheck(conf)
        self.bi_conf = CBiConfig(
            bi_algo=conf.get("bi_algo", "normal"),
            is_strict=conf.get("bi_strict", True),
            bi_fx_check=conf.get("bi_fx_check", "strict"),
            gap_as_kl=conf.get("gap_as_kl", False),
            bi_end_is_peak=conf.get('bi_end_is_peak', True),
            bi_allow_sub_peak=conf.get("bi_allow_sub_peak", True),
        )
        self.seg_conf = CSegConfig(
            seg_algo=conf.get("seg_algo", "chan"),
            left_method=conf.get("left_seg_method", "peak"),
        )
        self.zs_conf = CZSConfig(
            need_combine=conf.get("zs_combine", True),
            zs_combine_mode=conf.get("zs_combine_mode", "zs"),
            one_bi_zs=conf.get("one_bi_zs", False),
            zs_algo=conf.get("zs_algo", "normal"),
        )

        # 以下几项控制"逐步计算"（边喂K线边出结果）与数据校验的行为
        self.trigger_step = conf.get("trigger_step", False)
        self.skip_step = conf.get("skip_step", 0)

        # K线数据的合法性校验与容错阈值；auto_skip_illegal_sub_lv 用于"级别不齐"的数据源
        self.kl_data_check = conf.get("kl_data_check", True)
        self.max_kl_misalgin_cnt = conf.get("max_kl_misalgin_cnt", 2)
        self.max_kl_inconsistent_cnt = conf.get("max_kl_inconsistent_cnt", 5)
        self.auto_skip_illegal_sub_lv = conf.get("auto_skip_illegal_sub_lv", False)
        self.print_warning = conf.get("print_warning", True)
        self.print_err_time = conf.get("print_err_time", True)

        # 附加指标：均线周期列表 / 趋势线周期列表；后者每个周期会生成 MAX 与 MIN 两个模型
        self.mean_metrics: List[int] = conf.get("mean_metrics", [])
        self.trend_metrics: List[int] = conf.get("trend_metrics", [])
        self.macd_config = conf.get("macd", {"fast": 12, "slow": 26, "signal": 9})
        # 下面三个指标默认关闭，打开后会逐根K线计算并挂在 CKLine_Unit 上
        self.cal_demark = conf.get("cal_demark", False)
        self.cal_rsi = conf.get("cal_rsi", False)
        self.cal_kdj = conf.get("cal_kdj", False)
        self.rsi_cycle = conf.get("rsi_cycle", 14)
        self.kdj_cycle = conf.get("kdj_cycle", 9)
        self.demark_config = conf.get("demark", {
            'demark_len': 9,
            'setup_bias': 4,
            'countdown_bias': 2,
            'max_countdown': 13,
            'tiaokong_st': True,
            'setup_cmp2close': True,
            'countdown_cmp2close': True,
        })
        self.boll_n = conf.get("boll_n", 20)

        self.set_bsp_config(conf)

        # 兜底：凡是没被任何 conf.get / items 取走的键，都视为非法参数
        conf.check()

    def GetMetricModel(self):
        """
        按配置创建**一根全新**的指标模型列表。

        Returns:
            List[CMACD | CTrendModel | BollModel | CDemarkEngine | RSI | KDJ]:
            依次是 1 个 MACD + n 个均线模型 + 每个趋势周期 2 个（MAX/MIN）趋势模型
            + 1 个 BOLL + 可选的 Demark / RSI / KDJ。

        Note:
            这些模型都是**有状态的递推对象**（每喂一根K线就 update 一次），
            所以每个级别/每次重算都必须重新调本方法拿全新实例，不能复用。
            MACD 与 BOLL 是恒开启的（背驰力度与布林判定依赖它们）。
        """
        res: List[CMACD | CTrendModel | BollModel | CDemarkEngine | RSI | KDJ] = [
            CMACD(
                fastperiod=self.macd_config['fast'],
                slowperiod=self.macd_config['slow'],
                signalperiod=self.macd_config['signal'],
            )
        ]
        res.extend(CTrendModel(TREND_TYPE.MEAN, mean_T) for mean_T in self.mean_metrics)

        # 趋势线：同一周期需要"峰值连线"与"谷值连线"两个模型，故每项 append 两次
        for trend_T in self.trend_metrics:
            res.append(CTrendModel(TREND_TYPE.MAX, trend_T))
            res.append(CTrendModel(TREND_TYPE.MIN, trend_T))
        res.append(BollModel(self.boll_n))
        if self.cal_demark:
            res.append(CDemarkEngine(
                demark_len=self.demark_config['demark_len'],
                setup_bias=self.demark_config['setup_bias'],
                countdown_bias=self.demark_config['countdown_bias'],
                max_countdown=self.demark_config['max_countdown'],
                tiaokong_st=self.demark_config['tiaokong_st'],
                setup_cmp2close=self.demark_config['setup_cmp2close'],
                countdown_cmp2close=self.demark_config['countdown_cmp2close'],
            ))
        if self.cal_rsi:
            res.append(RSI(self.rsi_cycle))
        if self.cal_kdj:
            res.append(KDJ(self.kdj_cycle))
        return res

    def set_bsp_config(self, conf):
        """
        解析买卖点相关配置，生成**笔级别**与**线段级别**两套买卖点配置，各分买侧/卖侧。

        Args:
            conf: ConfigWithCheck 实例（此时只剩买卖点相关的键了）

        Raises:
            CChanException: 键既不带 -buy/-sell/-seg* 后缀、又不在默认参数表里
                            （ErrCode.PARA_ERROR）

        Note:
            1) para_dict 里的值同时作为笔级别的默认；线段级别在此基础上覆写三项：
               macd_algo 改为 "slope"（线段跨度大，用斜率比峰值更稳）、
               bsp1_only_multibi_zs 改为 False（线段级别允许单笔中枢出 1 买）。
            2) 覆写发生在解析 suffix 键**之前**，所以用户可以再用 -segbuy / -seg 后缀改回来。
            3) 字符串值要手动加引号再 exec —— exec 执行的是字面量表达式。
            4) parse_target_type() 必须在所有 set() 之后调用，否则改过 bs_type 也不生效。
        """
        para_dict = {
            "divergence_rate": float("inf"),
            "min_zs_cnt": 1,
            "bsp1_only_multibi_zs": True,
            "max_bs2_rate": 0.9999,
            "macd_algo": "peak",
            "bs1_peak": True,
            "bs_type": "1,1p,2,2s,3a,3b",
            "bsp2_follow_1": True,
            "bsp3_follow_1": True,
            "bsp3_peak": False,
            "bsp2s_follow_2": False,
            "max_bsp2s_lv": None,
            "strict_bsp3": False,
            "bsp3a_max_zs_cnt": 1,
        }
        args = {para: conf.get(para, default_value) for para, default_value in para_dict.items()}
        self.bs_point_conf = CBSPointConfig(**args)

        # 线段级别：力度口径换斜率、允许单笔中枢出 1 买
        self.seg_bs_point_conf = CBSPointConfig(**args)
        self.seg_bs_point_conf.b_conf.set("macd_algo", "slope")
        self.seg_bs_point_conf.s_conf.set("macd_algo", "slope")
        self.seg_bs_point_conf.b_conf.set("bsp1_only_multibi_zs", False)
        self.seg_bs_point_conf.s_conf.set("bsp1_only_multibi_zs", False)

        for k, v in conf.items():
            if isinstance(v, str):
                v = f'"{v}"'
            # 把 'inf' / '-inf' 这类字符串还原成真正的无穷大，供 exec 求值
            v = _parse_inf(v)
            if k.endswith("-buy"):
                prop = k.replace("-buy", "")
                exec(f"self.bs_point_conf.b_conf.set('{prop}', {v})")
            elif k.endswith("-sell"):
                prop = k.replace("-sell", "")
                exec(f"self.bs_point_conf.s_conf.set('{prop}', {v})")
            elif k.endswith("-segbuy"):
                prop = k.replace("-segbuy", "")
                exec(f"self.seg_bs_point_conf.b_conf.set('{prop}', {v})")
            elif k.endswith("-segsell"):
                prop = k.replace("-segsell", "")
                exec(f"self.seg_bs_point_conf.s_conf.set('{prop}', {v})")
            elif k.endswith("-seg"):
                # 无买卖后缀的 -seg：线段级别的买与卖一起设
                prop = k.replace("-seg", "")
                exec(f"self.seg_bs_point_conf.b_conf.set('{prop}', {v})")
                exec(f"self.seg_bs_point_conf.s_conf.set('{prop}', {v})")
            elif k in args:
                # 裸键名：笔级别的买与卖一起设
                exec(f"self.bs_point_conf.b_conf.set({k}, {v})")
                exec(f"self.bs_point_conf.s_conf.set({k}, {v})")
            else:
                raise CChanException(f"unknown para = {k}", ErrCode.PARA_ERROR)
        # 前面可能 set 过 bs_type，必须在全部 set 完成后再解析目标买卖点类型
        self.bs_point_conf.b_conf.parse_target_type()
        self.bs_point_conf.s_conf.parse_target_type()
        self.seg_bs_point_conf.b_conf.parse_target_type()
        self.seg_bs_point_conf.s_conf.parse_target_type()


class ConfigWithCheck:
    """
    配置 dict 的**一次性消费包装**：读一个键就删一个键，最后 check() 时残留即非法。

    目的是让"配错参数名"立刻暴露，而不是被默认值悄悄吞掉。
    """

    def __init__(self, conf):
        """
        Args:
            conf: 原始配置字典（会被就地修改：读取过的键将被删除）
        """
        self.conf = conf

    def get(self, k, default_value=None):
        """
        取值并从 dict 中移除该键（无论键是否真的存在）。

        Args:
            k: 配置键名
            default_value: 键不存在时的默认值

        Returns:
            配置值，或 default_value

        Note:
            即使键不存在也会执行 del —— 因为有 `if k in self.conf` 保护，所以是安全的。
        """
        res = self.conf.get(k, default_value)
        if k in self.conf:
            del self.conf[k]
        return res

    def items(self):
        """
        产出剩余的 (键, 值)，消费完毕后把产出过的键全部删除。

        Yields:
            Tuple[str, Any]: 剩余的键值对

        Note:
            这是个生成器：**只有被完整消费完**才会走到第二个循环删键。
            若调用方中途 break，已 yield 的键不会从 conf 中删除，
            随后的 check() 会把它们误报成非法参数。
        """
        visit_keys = set()
        for k, v in self.conf.items():
            yield k, v
            visit_keys.add(k)
        for k in visit_keys:
            del self.conf[k]

    def check(self):
        """
        确认配置已被全部消费；仍有残留说明存在无法识别的参数。

        Raises:
            CChanException: 残留键非空（ErrCode.PARA_ERROR）
        """
        if len(self.conf) > 0:
            invalid_key_lst = ",".join(list(self.conf.keys()))
            raise CChanException(f"invalid CChanConfig: {invalid_key_lst}", ErrCode.PARA_ERROR)
