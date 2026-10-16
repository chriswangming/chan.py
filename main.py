"""
命令行示例入口：跑一次完整的缠论计算并画图。

演示链路：数据源取K线 → CChan 计算（包含合并/分型/笔/线段/中枢/买卖点） → 绘图。
把本文件底部的 code / begin_time / lv_list / config 改成自己的即可直接运行。

两种运行模式由 config["trigger_step"] 决定：
    False（默认）—— 一次性算完后出静态图（CPlotDriver），并另存 ./test.png
    True         —— 逐步计算出动画图（CAnimateDriver），用来观察笔/线段随行情推进而变化
"""

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE
from Plot.AnimatePlotDriver import CAnimateDriver
from Plot.PlotDriver import CPlotDriver

if __name__ == "__main__":
    # 标的代码：baostock 源要求"小写市场前缀.六位代码"格式，如 sz.000001 / sh.600000
    code = "sz.000001"
    begin_time = "2018-01-01"
    # None 表示取到最新
    end_time = None
    data_src = DATA_SRC.BAO_STOCK
    # 只算日线；若要多级别联立（如日线+30分钟），按从大到小写成 [KL_TYPE.K_DAY, KL_TYPE.K_30M]
    lv_list = [KL_TYPE.K_DAY]

    config = CChanConfig({
        "bi_strict": True,
        "trigger_step": False,
        "skip_step": 0,
        # inf 表示不限制背驰力度比，即"只要背驰就算"
        "divergence_rate": float("inf"),
        # 关掉"2买必须跟在1买后"之类的连带限制，让各类买卖点独立出现，方便观察
        "bsp2_follow_1": False,
        "bsp3_follow_1": False,
        # 允许中枢个数为 0（即不要求至少一段趋势）
        "min_zs_cnt": 0,
        # 1买不要求端点必须是极值点
        "bs1_peak": False,
        "macd_algo": "peak",
        "bs_type": '1,2,3a,1p,2s,3b',
        "print_warning": True,
        "zs_algo": "normal",
    })

    # 绘图开关：要画什么
    plot_config = {
        "plot_kline": True,
        "plot_kline_combine": True,
        "plot_bi": True,
        "plot_seg": True,
        "plot_eigen": False,
        "plot_zs": True,
        "plot_macd": False,
        "plot_mean": False,
        "plot_channel": False,
        "plot_bsp": True,
        "plot_extrainfo": False,
        "plot_demark": False,
        "plot_marker": False,
        "plot_rsi": False,
        "plot_kdj": False,
    }

    # 绘图参数：按元素类型细分；注释掉的都是可选项（打开即生效）
    plot_para = {
        "seg": {
            # "plot_trendline": True,
        },
        "bi": {
            # "show_num": True,
            # "disp_end": True,
        },
        "figure": {
            # 一屏显示多少根K线（横向视野）
            "x_range": 200,
        },
        "marker": {
            # "markers": {  # text, position, color
            #     '2023/06/01': ('marker here', 'up', 'red'),
            #     '2023/06/08': ('marker here', 'down')
            # },
        }
    }
    chan = CChan(
        code=code,
        begin_time=begin_time,
        end_time=end_time,
        data_src=data_src,
        lv_list=lv_list,
        config=config,
        autype=AUTYPE.QFQ,
    )

    if not config.trigger_step:
        plot_driver = CPlotDriver(
            chan,
            plot_config=plot_config,
            plot_para=plot_para,
        )
        plot_driver.figure.show()
        plot_driver.save2img("./test.png")
    else:
        # 逐步计算模式：内部会持续触发计算并刷新画面
        CAnimateDriver(
            chan,
            plot_config=plot_config,
            plot_para=plot_para,
        )
