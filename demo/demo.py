import sys
from pathlib import Path
# 将项目根目录加入路径，以便导入 chan.py 核心模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, KL_TYPE, DATA_SRC
from Plot.AnimatePlotDriver import CAnimateDriver
from Plot.PlotDriver import CPlotDriver
from Plot.PlotlyDriver import CPlotlyDriver

config = CChanConfig({})  # 缠论计算配置，见后文

chan = CChan(
    code="sz.300308",
    begin_time="2026-05-01",
    end_time=None,
    data_src=DATA_SRC.BAO_STOCK,
    lv_list=[KL_TYPE.K_DAY],  # 多级别可以从大到小传入
    config=config,
    autype=AUTYPE.QFQ
)

plot_config = {
    "plot_kline": True,
    "plot_kline_combine": True,
    "plot_bi": True,
    "plot_seg": True,
    "plot_eigen": True,
    "plot_zs": True,
    "plot_macd": True,
    "plot_mean": False,
    "plot_channel": False,
    "plot_bsp": True,
    "plot_cbsp": True,
    "plot_extrainfo": False,
}  # 绘图元素开关，详见后文

plot_para = {
    "seg": {
        "plot_trendline":False,  # 绘制趋势线
    },
    "bi": {
        "show_num": False,  # 笔是否显示序号
        "disp_end": False,  # 是否显示首尾价格
    },
    "figure": {
      "width": 24,
    },
    "cbsp": {
        "plot_cover": True,  # 绘制平仓操作
    },
}  # 空格绘图元素详细配置，详见后文

plot_driver = CPlotDriver(
    chan,
    plot_config=plot_config,
    plot_para=plot_para,
)
plot_driver.figure.show()
plot_driver.save2img("./demo.png")
# plot_driver = CPlotlyDriver(
#     chan,
#     plot_config=plot_config,
#     plot_para=plot_para,
# )
# plot_driver.figure.show()
# plot_driver.to_html("./demo.html")

