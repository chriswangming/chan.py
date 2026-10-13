"""
PlotlyDriver.py — plotly 版缠论画图引擎（可交互）

仿照 Plot/PlotDriver.py（matplotlib 版）的实现思路，用 plotly 输出可以在浏览器里
缩放 / 平移 / 悬浮查看数值的动态图表，绘制元素与 matplotlib 版保持一致：

  - x 轴语义：横坐标 = 原始K线下标（klu.idx），等间距排列（周末/停牌不留空档），
    刻度标签为日期字符串，与 PlotDriver 的 set_x_tick 一致
  - y 轴范围：由可见范围内合并K线极值决定，并同样会被 boll / 买卖点箭头扩展
  - MACD 副图与主图共用同一条 x 轴：缩放联动，鼠标竖线（spike）同时出现在两个子图上；
    柱太密被压到不足 1px 时自动补同色描边，避免被抗锯齿淡化到看不见
  - 元素开关（plot_kline/plot_kline_combine/plot_bi/...）与 plot_para 参数完全复用
  - 涨红跌绿（rugd=True 默认）；上涨K线空心、下跌实心，与 matplotlib 版一致
  - 买卖点箭头 / 自定义 marker 箭头：长度按 y_range 的比例（数据坐标），同 matplotlib

与 CPlotDriver 的接口差别：
  - `figure` 是 plotly.graph_objects.Figure（支持 .show() / .write_html()）
  - 各 draw_xxx 的第二个参数是 subplot 行号 row(int)，不再是 Axes
  - 新增 to_html()/to_json()；save2img 需要 `pip install kaleido`

交互说明：
  - 悬浮：默认 hovermode='x unified'，十字定位线（spike）跟随鼠标
  - 缩放：框选 / 滚轮；双击或点模式栏 "Reset axes" 恢复到 PlotDriver 同款的初始视野
  - y 轴默认固定为 PlotDriver 计算出的范围（保证与 matplotlib 版一致）；
    传 figure_config={'auto_y': True} 可改为自动量程

注意：本模块复用 PlotDriver 的配置解析/元数据构造，因此 import 它会连带引入
matplotlib（chan.py 本身的既有依赖）。
"""

from typing import Dict, List, Literal, Optional, Tuple, Union

import plotly.graph_objects as go

from Chan import CChan
from Common.CEnum import BI_DIR, FX_TYPE, KL_TYPE, KLINE_DIR, TREND_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Math.Demark import T_DEMARK_INDEX, CDemarkEngine

from .PlotDriver import GetPlotMeta, cal_x_limit, get_colormap, parse_plot_config
from .PlotMeta import CBi_meta, CChanPlotMeta, CZS_meta

try:  # 颜色解析：优先 matplotlib（与 PlotDriver 完全同源的配色语义）
    from matplotlib.colors import to_rgba
except ImportError:  # pragma: no cover - 极简兜底，覆盖默认参数用到的颜色
    _NAMED = {
        'r': (1, 0, 0), 'g': (0, 0.5, 0), 'b': (0, 0, 1), 'k': (0, 0, 0), 'w': (1, 1, 1),
        'y': (0.75, 0.75, 0), 'c': (0, 0.75, 0.75), 'm': (0.75, 0, 0.75),
        'red': (1, 0, 0), 'green': (0, 0.5, 0), 'blue': (0, 0, 1), 'black': (0, 0, 0),
        'white': (1, 1, 1), 'orange': (1, 0.647, 0), 'purple': (0.5, 0, 0.5),
        'brown': (0.647, 0.165, 0.118), 'pink': (1, 0.753, 0.796), 'yellow': (1, 1, 0),
        'gray': (0.5, 0.5, 0.5), 'grey': (0.5, 0.5, 0.5), 'cyan': (0, 1, 1),
        'magenta': (1, 0, 1),
    }

    def to_rgba(color, alpha=None):
        if isinstance(color, str):
            c = color.strip()
            if c.startswith('#') and len(c) == 7:
                r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
            elif c in _NAMED:
                r, g, b = _NAMED[c]
            else:
                raise CChanException(f"unsupport color={color}", ErrCode.PLOT_ERR)
        else:
            r, g, b = color[:3]
        a = 1.0 if alpha is None else alpha
        return (r, g, b, a)


def c2rgba(color, alpha: float = 1.0) -> str:
    """matplotlib 颜色（含 'r'/'g'/'#FFA500' 等简写） -> plotly 的 rgba() 字符串"""
    r, g, b, a = to_rgba(color)
    if alpha is None:
        alpha = a
    return f"rgba({int(round(r * 255))},{int(round(g * 255))},{int(round(b * 255))},{round(alpha * a, 4)})"


def cal_y_range(meta: CChanPlotMeta, x_begin: float):
    """与 PlotDriver.cal_y_range 相同：可见范围内合并K线的极值（x_begin 显式传入）"""
    y_min = float("inf")
    y_max = float("-inf")
    for klc_meta in meta.klc_list:
        if klc_meta.klu_list[-1].idx < x_begin:
            continue  # 不绘制范围外的
        if klc_meta.high > y_max:
            y_max = klc_meta.high
        if klc_meta.low < y_min:
            y_min = klc_meta.low
    return (y_min, y_max)


def GetRealXrange(figure_config, meta: CChanPlotMeta):
    # 与 PlotDriver.CPlotDriver.GetRealXrange 逻辑保持一致（复制以避免依赖其实例）
    x_range = figure_config.get("x_range", 0)
    bi_cnt = figure_config.get("x_bi_cnt", 0)
    seg_cnt = figure_config.get("x_seg_cnt", 0)
    x_begin_date = figure_config.get("x_begin_date", 0)
    if x_range != 0:
        assert bi_cnt == 0 and seg_cnt == 0 and x_begin_date == 0, "x_range/x_bi_cnt/x_seg_cnt/x_begin_date can not be set at the same time"
        return x_range
    if bi_cnt != 0:
        assert x_range == 0 and seg_cnt == 0 and x_begin_date == 0, "x_range/x_bi_cnt/x_seg_cnt/x_begin_date can not be set at the same time"
        X_LEN = meta.klu_len
        if len(meta.bi_list) < bi_cnt:
            return 0
        x_range = X_LEN - meta.bi_list[-bi_cnt].begin_x
        return x_range
    if seg_cnt != 0:
        assert x_range == 0 and bi_cnt == 0 and x_begin_date == 0, "x_range/x_bi_cnt/x_seg_cnt/x_begin_date can not be set at the same time"
        X_LEN = meta.klu_len
        if len(meta.seg_list) < seg_cnt:
            return 0
        x_range = X_LEN - meta.seg_list[-seg_cnt].begin_x
        return x_range
    if x_begin_date != 0:
        assert x_range == 0 and bi_cnt == 0 and seg_cnt == 0, "x_range/x_bi_cnt/x_seg_cnt/x_begin_date can not be set at the same time"
        x_range = 0
        for date_tick in meta.datetick[::-1]:
            if date_tick >= x_begin_date:
                x_range += 1
            else:
                break
        return x_range
    return x_range


def set_grid_config(config):
    """与 PlotDriver.set_grid 相同的取值校验：None/'x'/'y'/'xy' -> (show_x, show_y)"""
    if config is None:
        return (False, False)
    if config == "xy":
        return (True, True)
    if config in ("x", "y"):
        return (config == "x", config == "y")
    raise CChanException(f"unsupport grid config={config}", ErrCode.PLOT_ERR)


def _axis_key(axis_id: str) -> str:
    """plotly 轴 id -> layout key：'x'/'y' -> 'xaxis'/'yaxis'，'x2' -> 'xaxis2'"""
    return axis_id[0] + "axis" + axis_id[1:]


class _RowCtx:
    """单个 subplot 行的上下文（对应 matplotlib 版里的一个 Axes）"""

    __slots__ = ("row", "lv", "kind", "xref", "yref", "datetick", "cat", "x_begin")

    def __init__(self, row: int, lv: KL_TYPE, kind: str, xref: Optional[str] = None):
        self.row = row
        self.lv = lv
        self.kind = kind  # 'main' | 'macd'
        # xref 可显式指定：macd 副图与主图共用同一条 x 轴（缩放联动 + spike 竖线跨子图）
        self.xref = xref or ("x" if row == 1 else f"x{row}")
        self.yref = "y" if row == 1 else f"y{row}"
        self.datetick: List[str] = []
        self.cat = True   # x 轴是否为 category（datetick 无重复时）
        self.x_begin = 0


class CPlotlyDriver:
    """plotly 版画图器，构造参数与 CPlotDriver 完全一致"""

    def __init__(self, chan: CChan, plot_config: Union[str, dict, list] = '', plot_para=None):
        if plot_para is None:
            plot_para = {}
        self.plot_para = plot_para
        figure_config: dict = plot_para.get('figure', {})

        plot_config = parse_plot_config(plot_config, chan.lv_list)
        plot_metas = GetPlotMeta(chan, figure_config)
        self.lv_lst = chan.lv_list[:len(plot_metas)]

        x_range = GetRealXrange(figure_config, plot_metas[0])
        plot_macd: Dict[KL_TYPE, bool] = {kl_type: conf.get("plot_macd", False) for kl_type, conf in plot_config.items()}

        self.figure, self.rows, self.lv_rows = self._create_figure(plot_macd, figure_config, self.lv_lst, plot_metas)

        sseg_begin = 0
        slv_seg_cnt = plot_para.get('seg', {}).get('sub_lv_cnt', None)
        sbi_begin = 0
        slv_bi_cnt = plot_para.get('bi', {}).get('sub_lv_cnt', None)
        srange_begin = 0
        assert slv_seg_cnt is None or slv_bi_cnt is None, "you can set at most one of seg_sub_lv_cnt/bi_sub_lv_cnt"

        for meta, lv in zip(plot_metas, self.lv_lst):  # type: ignore
            row = self.lv_rows[lv][0]
            macd_row = self.lv_rows[lv][1]
            ctx = self._row(row)
            show_x, show_y = set_grid_config(figure_config.get("grid", "xy"))
            self._update_axis(ctx.yref, dict(showgrid=show_y, gridcolor=c2rgba('#b0b0b0', 0.4), gridwidth=0.5))
            self._set_title(row, f"{chan.code}/{lv.name.split('K_')[1]}")

            x_limits = cal_x_limit(meta, x_range)
            if lv != self.lv_lst[0]:
                if sseg_begin != 0 or sbi_begin != 0:
                    x_limits[0] = max(sseg_begin, sbi_begin)
                elif srange_begin != 0:
                    x_limits[0] = srange_begin
            self._set_x_tick(row, meta, x_limits, figure_config.get('x_tick_num', 10))
            if macd_row:
                self._set_x_tick(macd_row, meta, x_limits, figure_config.get('x_tick_num', 10))
            self.y_min, self.y_max = cal_y_range(meta, x_limits[0])  # 需要先设置 x_tick后计算

            self.DrawElement(plot_config[lv], meta, row, lv, plot_para, macd_row, x_limits)

            if lv != self.lv_lst[-1]:
                if slv_seg_cnt is not None:
                    sseg_begin = meta.sub_last_kseg_start_idx(slv_seg_cnt)
                if slv_bi_cnt is not None:
                    sbi_begin = meta.sub_last_kbi_start_idx(slv_bi_cnt)
                if x_range != 0:
                    srange_begin = meta.sub_range_start_idx(x_range)

            if figure_config.get("auto_y", False):
                self._update_axis(ctx.yref, dict(autorange=True))
            else:
                self._update_axis(ctx.yref, dict(range=[self.y_min, self.y_max], autorange=False))

    # ────────────────────────────────────────────────────────────
    #  布局与底层绘图原语
    # ────────────────────────────────────────────────────────────
    def _create_figure(self, plot_macd, figure_config, lv_lst, plot_metas):
        macd_h_ration = figure_config.get('macd_h', 0.3)
        dpi = figure_config.get('dpi', 72)
        # 与 PlotDriver 单位一致：w/h（也接受 width/height 别名）单位是英寸，
        # 实际像素 = 值 * dpi。要直接给像素就设 figure_config={'dpi': 1}
        width = figure_config.get('width', figure_config.get('w', 24)) * dpi
        row_h = figure_config.get('height', figure_config.get('h', 10)) * dpi  # 每个主图行的高度

        rows: List[Tuple[KL_TYPE, str, float]] = []  # (lv, kind, 相对高度)
        for lv in lv_lst:
            rows.append((lv, "main", 1.0))
            if plot_macd[lv]:
                rows.append((lv, "macd", macd_h_ration))

        gap_px = figure_config.get('gap', 46)
        heights_px = [max(60, row_h * h) for _, _, h in rows]
        total_h = sum(heights_px) + gap_px * (len(rows) - 1)

        fig = go.Figure()
        lv_rows: Dict[KL_TYPE, Tuple[Optional[int], Optional[int]]] = {}
        row_ctxs: Dict[int, _RowCtx] = {}
        lv_xref: Dict[KL_TYPE, str] = {}
        x_serial = 0

        # 先分配行号与 xref：同一级别的主图和 macd 副图共用一条 x 轴，
        # 这样缩放会联动，且鼠标竖线（spike）会同时出现在两个子图上
        for i, (lv, kind, _) in enumerate(rows, start=1):
            lv_rows.setdefault(lv, (None, None))
            if kind == "main":
                x_serial += 1
                lv_xref[lv] = "x" if x_serial == 1 else f"x{x_serial}"
                lv_rows[lv] = (i, lv_rows[lv][1])
            else:
                lv_rows[lv] = (lv_rows[lv][0], i)
            row_ctxs[i] = _RowCtx(i, lv, kind, xref=lv_xref[lv])

        top_frac = 1.0
        for i, (lv, kind, _) in enumerate(rows, start=1):
            hh = heights_px[i - 1] / total_h
            bottom = top_frac - hh
            ctx = row_ctxs[i]

            fig.layout[_axis_key(f"y{i}")] = go.layout.YAxis(
                domain=[bottom, top_frac],
                anchor=ctx.xref,
                zeroline=False,
                showline=True,
                linecolor=c2rgba('#d0d7de'),
                mirror=False,
                tickfont=dict(size=10, color='#57606a'),
            )
            top_frac = bottom - gap_px / total_h

        self._row_ctxs = row_ctxs
        self._sec_y_created = set()
        for i, (lv, kind, _) in enumerate(rows, start=1):
            meta = plot_metas[lv_lst.index(lv)]
            ctx = row_ctxs[i]
            ctx.datetick = [klu.time.to_str() for klu in meta.klu_iter()]
            # 存在重复日期标签时退回线性轴（仍等间距，只是悬浮显示下标）
            ctx.cat = len(set(ctx.datetick)) == len(ctx.datetick)
            if kind != "main":
                continue  # x 轴每条只建一次，副图沿用主图的
            fig.layout[_axis_key(ctx.xref)] = go.layout.XAxis(
                anchor=f"y{i}",
                type="category" if ctx.cat else "linear",
                rangeslider=dict(visible=False),
                showspikes=True,
                spikemode="across",
                spikethickness=1,
                spikedash="dot",
                spikecolor=c2rgba('#8b949e'),
                ticks="outside",
                ticklen=3,
                tickcolor=c2rgba('#d0d7de'),
                tickfont=dict(size=10, color='#57606a'),
                linecolor=c2rgba('#d0d7de'),
            )

        fig.update_layout(
            width=int(width),
            height=int(total_h),
            template=figure_config.get("template", "plotly_white"),
            hovermode="x unified",
            margin=dict(l=10, r=10, t=28, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
            showlegend=figure_config.get("show_legend", True),
        )
        return fig, row_ctxs, {lv: tuple(v) for lv, v in lv_rows.items()}

    def _row(self, row: int) -> _RowCtx:
        return self._row_ctxs[row]

    def _xv(self, ctx: _RowCtx, idx):
        """trace 用的 x 值：category 轴 -> 日期字符串；linear 轴 -> 下标"""
        if idx is None:
            return None
        return ctx.datetick[int(idx)] if ctx.cat else int(idx)

    def _xs(self, ctx: _RowCtx, idx_list):
        return [self._xv(ctx, i) for i in idx_list]

    def _trace(self, tr, row: int, yaxis: Optional[str] = None, **kwargs):
        ctx = self._row(row)
        tr.xaxis = ctx.xref
        tr.yaxis = yaxis if yaxis is not None else ctx.yref
        for k, v in kwargs.items():
            setattr(tr, k, v)
        self.figure.add_trace(tr)

    def _shape(self, row: int, shape_dict: dict, yref: Optional[str] = None):
        ctx = self._row(row)
        shape_dict.setdefault("xref", ctx.xref)
        shape_dict.setdefault("yref", yref if yref is not None else ctx.yref)
        shape_dict.setdefault("layer", "below")
        self.figure.add_shape(shape_dict)

    def _anno(self, row: int, **kwargs):
        ctx = self._row(row)
        kwargs.setdefault("xref", ctx.xref)
        kwargs.setdefault("yref", ctx.yref)
        kwargs.setdefault("showarrow", False)
        self.figure.add_annotation(kwargs)

    def _update_axis(self, axis_id: str, conf: dict):
        self.figure.layout[_axis_key(axis_id)].update(conf)

    def _set_title(self, row: int, text: str):
        ctx = self._row(row)
        self.figure.add_annotation({
            "xref": f"{ctx.xref} domain", "yref": f"{ctx.yref} domain",
            "x": 0, "y": 1.02, "xanchor": "left", "yanchor": "bottom",
            "text": text, "showarrow": False,
            "font": dict(size=16, color="red"),
        })

    def _set_x_tick(self, row: int, meta: CChanPlotMeta, x_limits, x_tick_num: int):
        assert x_tick_num > 1
        ctx = self._row(row)
        ctx.x_begin = x_limits[0]
        step = max([1, int((x_limits[1] - x_limits[0]) / float(x_tick_num))])
        tick_vals = list(range(x_limits[0], x_limits[1], step))
        self._update_axis(ctx.xref, dict(
            tickmode="array",
            tickvals=tick_vals,
            ticktext=[meta.datetick[i] for i in tick_vals],
            range=[x_limits[0] - 0.5, x_limits[1] + 0.5],  # 同 matplotlib 的 set_xlim(x0, x1+1)
            autorange=False,
        ))

    def _x_begin(self, row: int) -> float:
        return self._row(row).x_begin

    def _txt_h(self, fontsize: float = 12) -> float:
        """数据坐标下的文本高度估算（matplotlib getTextBox 的近似）"""
        return 0.0167 * (self.y_max - self.y_min) * fontsize / 12.0

    def _arrow(self, row: int, x, y_from, y_to, head_len, head_w, color):
        """数据坐标箭头：从 (x, y_from) 指向 (x, y_to)，头部占 head_len 个数据单位。

        与 matplotlib ax.arrow(x, y, 0, dy, head_width, head_length) 的几何一致：
        可见线段止于 y_to - head_len*d，三角头部从该处延伸到 y_to - head_len*d .. tip。
        """
        d = 1.0 if y_to > y_from else -1.0
        tip = y_to - head_len * d
        base = tip - head_len * d
        self._shape(row, dict(
            type="line", x0=x, y0=y_from, x1=x, y1=base,
            line=dict(color=color, width=1),
        ))
        self._shape(row, dict(
            type="path",
            path=f"M {x - head_w / 2},{base} L {x + head_w / 2},{base} L {x},{tip} Z",
            fillcolor=color,
            line=dict(width=0),
        ))

    def _fill_vregion(self, row: int, x0, x1, facecolor: str, alpha: float):
        """整行高度的半透明背景（对应 matplotlib 的 fill_between(x, ylim0, ylim1)）"""
        ctx = self._row(row)
        self._shape(row, dict(
            type="rect", x0=x0, x1=x1, y0=0, y1=1,
            yref=f"{ctx.yref} domain",
            fillcolor=c2rgba(facecolor, alpha),
            line=dict(width=0),
        ))

    def _set_y_range(self, row: int):
        self._update_axis(self._row(row).yref, dict(range=[self.y_min, self.y_max], autorange=False))

    # ────────────────────────────────────────────────────────────
    #  元素分发（与 PlotDriver.DrawElement 一致）
    # ────────────────────────────────────────────────────────────
    def DrawElement(self, plot_config: Dict[str, bool], meta: CChanPlotMeta, row: int, lv, plot_para, macd_row: Optional[int], x_limits):
        if plot_config.get("plot_kline", False):
            self.draw_klu(meta, row, **plot_para.get('kl', {}))
        if plot_config.get("plot_kline_combine", False):
            self.draw_klc(meta, row, **plot_para.get('klc', {}))
        if plot_config.get("plot_bi", False):
            self.draw_bi(meta, row, lv, **plot_para.get('bi', {}))
        if plot_config.get("plot_seg", False):
            self.draw_seg(meta, row, lv, **plot_para.get('seg', {}))
        if plot_config.get("plot_segseg", False):
            self.draw_segseg(meta, row, **plot_para.get('segseg', {}))
        if plot_config.get("plot_eigen", False):
            self.draw_eigen(meta, row, **plot_para.get('eigen', {}))
        if plot_config.get("plot_segeigen", False):
            self.draw_segeigen(meta, row, **plot_para.get('segeigen', {}))
        if plot_config.get("plot_zs", False):
            self.draw_zs(meta, row, **plot_para.get('zs', {}))
        if plot_config.get("plot_segzs", False):
            self.draw_segzs(meta, row, **plot_para.get('segzs', {}))
        if plot_config.get("plot_macd", False):
            assert macd_row is not None
            self.draw_macd(meta, macd_row, x_limits, **plot_para.get('macd', {}))
        if plot_config.get("plot_mean", False):
            self.draw_mean(meta, row, **plot_para.get('mean', {}))
        if plot_config.get("plot_channel", False):
            self.draw_channel(meta, row, **plot_para.get('channel', {}))
        if plot_config.get("plot_boll", False):
            self.draw_boll(meta, row, **plot_para.get('boll', {}))
        if plot_config.get("plot_bsp", False):
            self.draw_bs_point(meta, row, **plot_para.get('bsp', {}))
        if plot_config.get("plot_segbsp", False):
            self.draw_seg_bs_point(meta, row, **plot_para.get('seg_bsp', {}))
        if plot_config.get("plot_demark", False):
            self.draw_demark(meta, row, **plot_para.get('demark', {}))
        if plot_config.get("plot_marker", False):
            self.draw_marker(meta, row, **plot_para.get('marker', {'markers': {}}))
        if plot_config.get("plot_rsi", False):
            self.draw_rsi(meta, row, **plot_para.get('rsi', {}))
        if plot_config.get("plot_kdj", False):
            self.draw_kdj(meta, row, **plot_para.get('kdj', {}))

    # ────────────────────────────────────────────────────────────
    #  各元素绘制（与 PlotDriver 同名方法参数一致）
    # ────────────────────────────────────────────────────────────
    def draw_klu(self, meta: CChanPlotMeta, row: int, width=0.4, rugd=True, plot_mode="kl"):
        """width 参数仅为与 PlotDriver 签名对齐保留；plotly 的蜡烛宽度自适应排布"""
        ctx = self._row(row)
        x_begin = self._x_begin(row)
        up_color = 'r' if rugd else 'g'
        down_color = 'g' if rugd else 'r'

        klus = [kl for kl in meta.klu_iter() if kl.idx + width >= x_begin]
        if plot_mode == "kl":
            self._trace(go.Candlestick(
                x=self._xs(ctx, [kl.idx for kl in klus]),
                open=[kl.open for kl in klus],
                high=[kl.high for kl in klus],
                low=[kl.low for kl in klus],
                close=[kl.close for kl in klus],
                name="K线",
                increasing=dict(line=dict(color=c2rgba(up_color), width=1),
                                fillcolor=c2rgba(up_color, 0.0)),   # 上涨空心，同 matplotlib fill=False
                decreasing=dict(line=dict(color=c2rgba(down_color), width=1),
                                fillcolor=c2rgba(down_color)),
                showlegend=False,
            ), row)
        elif plot_mode in ("close", "high", "low", "open"):
            field = plot_mode
            self._trace(go.Scatter(
                x=self._xs(ctx, [kl.idx for kl in klus]),
                y=[getattr(kl, field) for kl in klus],
                mode="lines",
                line=dict(color=c2rgba(up_color), width=1.2),
                name=f"kl-{field}",
                showlegend=False,
            ), row)
        else:
            raise CChanException(f"unknow plot mode={plot_mode}, must be one of kl/close/open/high/low", ErrCode.PLOT_ERR)

    def draw_klc(self, meta: CChanPlotMeta, row: int, width=0.4, plot_single_kl=True):
        color_type = {FX_TYPE.TOP: 'red', FX_TYPE.BOTTOM: 'blue', KLINE_DIR.UP: 'green', KLINE_DIR.DOWN: 'green'}
        x_begin = self._x_begin(row)
        for klc_meta in meta.klc_list:
            if klc_meta.klu_list[-1].idx + width < x_begin:
                continue  # 不绘制范围外的
            if klc_meta.end_idx == klc_meta.begin_idx and not plot_single_kl:
                continue
            self._shape(row, dict(
                type="rect",
                x0=klc_meta.begin_idx - width, x1=klc_meta.end_idx + width,
                y0=klc_meta.low, y1=klc_meta.high,
                line=dict(color=c2rgba(color_type[klc_meta.type]), width=1),
            ))

    def draw_bi(
        self,
        meta: CChanPlotMeta,
        row: int,
        lv,
        color='black',
        show_num=False,
        num_fontsize=15,
        num_color="red",
        sub_lv_cnt=None,
        facecolor='green',
        alpha=0.1,
        disp_end=False,
        end_color='black',
        end_fontsize=10,
    ):
        x_begin = self._x_begin(row)
        ctx = self._row(row)
        _xs = {"solid": [], "dash": []}
        _ys = {"solid": [], "dash": []}
        for bi_idx, bi in enumerate(meta.bi_list):
            if bi.end_x < x_begin:
                continue
            key = "solid" if bi.is_sure else "dash"
            _xs[key].extend([self._xv(ctx, bi.begin_x), self._xv(ctx, bi.end_x), None])
            _ys[key].extend([bi.begin_y, bi.end_y, None])
            if show_num and bi.begin_x >= x_begin:
                self._anno(row, x=(bi.begin_x + bi.end_x) / 2, y=(bi.begin_y + bi.end_y) / 2,
                           text=f'{bi.idx}', font=dict(size=num_fontsize, color=c2rgba(num_color)))
            if disp_end:
                self._bi_text(row, bi_idx, bi, end_fontsize, end_color)
        for key, x_arr in _xs.items():
            if x_arr:
                self._trace(go.Scatter(
                    x=x_arr, y=_ys[key], mode="lines", name="bi",
                    line=dict(color=c2rgba(color), width=1.4, dash=key),
                    hoverinfo="skip", showlegend=False,
                ), row)
        if sub_lv_cnt is not None and len(self.lv_lst) > 1 and lv != self.lv_lst[-1]:
            if sub_lv_cnt >= len(meta.bi_list):
                return
            begin_idx = meta.bi_list[-sub_lv_cnt].begin_x
            x_end = meta.klu_len - 1
            self._fill_vregion(row, begin_idx, x_end, facecolor, alpha)

    def draw_seg(
        self,
        meta: CChanPlotMeta,
        row: int,
        lv,
        width=5,
        color="g",
        sub_lv_cnt=None,
        facecolor='green',
        alpha=0.1,
        disp_end=False,
        end_color='g',
        end_fontsize=13,
        plot_trendline=False,
        trendline_color='r',
        trendline_width=3,
        show_num=False,
        num_fontsize=25,
        num_color="blue",
    ):
        x_begin = self._x_begin(row)
        ctx = self._row(row)
        _xs = {"solid": [], "dash": []}
        _ys = {"solid": [], "dash": []}
        for seg_idx, seg_meta in enumerate(meta.seg_list):
            if seg_meta.end_x < x_begin:
                continue
            key = "solid" if seg_meta.is_sure else "dash"
            _xs[key].extend([self._xv(ctx, seg_meta.begin_x), self._xv(ctx, seg_meta.end_x), None])
            _ys[key].extend([seg_meta.begin_y, seg_meta.end_y, None])
            if disp_end:
                self._bi_text(row, seg_idx, seg_meta, end_fontsize, end_color)
            if plot_trendline:
                if seg_meta.tl.get('support'):
                    tl_meta = seg_meta.format_tl(seg_meta.tl['support'])
                    self._shape(row, dict(type="line", x0=tl_meta[0], y0=tl_meta[1], x1=tl_meta[2], y1=tl_meta[3],
                                          line=dict(color=c2rgba(trendline_color), width=trendline_width)))
                if seg_meta.tl.get('resistance'):
                    tl_meta = seg_meta.format_tl(seg_meta.tl['resistance'])
                    self._shape(row, dict(type="line", x0=tl_meta[0], y0=tl_meta[1], x1=tl_meta[2], y1=tl_meta[3],
                                          line=dict(color=c2rgba(trendline_color), width=trendline_width)))
            if show_num and seg_meta.begin_x >= x_begin:
                self._anno(row, x=(seg_meta.begin_x + seg_meta.end_x) / 2, y=(seg_meta.begin_y + seg_meta.end_y) / 2,
                           text=f'{seg_meta.idx}', font=dict(size=num_fontsize, color=c2rgba(num_color)))
        for key, x_arr in _xs.items():
            if x_arr:
                self._trace(go.Scatter(
                    x=x_arr, y=_ys[key], mode="lines", name="seg",
                    line=dict(color=c2rgba(color), width=width, dash=key),
                    hoverinfo="skip", showlegend=False,
                ), row)
        if sub_lv_cnt is not None and len(self.lv_lst) > 1 and lv != self.lv_lst[-1]:
            if sub_lv_cnt >= len(meta.seg_list):
                return
            begin_idx = meta.seg_list[-sub_lv_cnt].begin_x
            x_end = meta.klu_len - 1
            self._fill_vregion(row, begin_idx, x_end, facecolor, alpha)

    def draw_segseg(
        self,
        meta: CChanPlotMeta,
        row: int,
        width=7,
        color="brown",
        disp_end=False,
        end_color='brown',
        end_fontsize=15,
        show_num=False,
        num_fontsize=30,
        num_color="blue",
    ):
        x_begin = self._x_begin(row)
        ctx = self._row(row)
        _xs = {"solid": [], "dash": []}
        _ys = {"solid": [], "dash": []}
        for seg_idx, seg_meta in enumerate(meta.segseg_list):
            if seg_meta.end_x < x_begin:
                continue
            key = "solid" if seg_meta.is_sure else "dash"
            _xs[key].extend([self._xv(ctx, seg_meta.begin_x), self._xv(ctx, seg_meta.end_x), None])
            _ys[key].extend([seg_meta.begin_y, seg_meta.end_y, None])
            if disp_end:
                if seg_idx == 0:
                    self._anno(row, x=seg_meta.begin_x, y=seg_meta.begin_y,
                               text=f'{seg_meta.begin_y:.2f}',
                               font=dict(size=end_fontsize, color=c2rgba(end_color)),
                               yanchor="top" if seg_meta.dir == BI_DIR.UP else "bottom",
                               xanchor="center")
                self._anno(row, x=seg_meta.end_x, y=seg_meta.end_y,
                           text=f'{seg_meta.end_y:.2f}',
                           font=dict(size=end_fontsize, color=c2rgba(end_color)),
                           yanchor="top" if seg_meta.dir == BI_DIR.DOWN else "bottom",
                           xanchor="center")
            if show_num and seg_meta.begin_x >= x_begin:
                self._anno(row, x=(seg_meta.begin_x + seg_meta.end_x) / 2, y=(seg_meta.begin_y + seg_meta.end_y) / 2,
                           text=f'{seg_meta.idx}', font=dict(size=num_fontsize, color=c2rgba(num_color)))
        for key, x_arr in _xs.items():
            if x_arr:
                self._trace(go.Scatter(
                    x=x_arr, y=_ys[key], mode="lines", name="segseg",
                    line=dict(color=c2rgba(color), width=width, dash=key),
                    hoverinfo="skip", showlegend=False,
                ), row)

    def _plot_single_eigen(self, eigenfx_meta, row: int, color_top, color_bottom, alpha, only_peak):
        x_begin = self._x_begin(row)
        color = color_top if eigenfx_meta.fx == FX_TYPE.TOP else color_bottom
        for idx, eigen_meta in enumerate(eigenfx_meta.ele):
            if eigen_meta.begin_x + eigen_meta.w < x_begin:
                continue
            if only_peak and idx != 1:
                continue
            self._shape(row, dict(
                type="rect",
                x0=eigen_meta.begin_x, x1=eigen_meta.begin_x + eigen_meta.w,
                y0=eigen_meta.begin_y, y1=eigen_meta.begin_y + eigen_meta.h,
                fillcolor=c2rgba(color, alpha),
                line=dict(width=0),
            ))

    def draw_eigen(self, meta: CChanPlotMeta, row: int, color_top="r", color_bottom="b", aplha=0.5, only_peak=False):
        # aplha 沿用 PlotDriver 的拼写
        for eigenfx_meta in meta.eigenfx_lst:
            self._plot_single_eigen(eigenfx_meta, row, color_top, color_bottom, aplha, only_peak)

    def draw_segeigen(self, meta: CChanPlotMeta, row: int, color_top="r", color_bottom="b", aplha=0.5, only_peak=False):
        for eigenfx_meta in meta.seg_eigenfx_lst:
            self._plot_single_eigen(eigenfx_meta, row, color_top, color_bottom, aplha, only_peak)

    def draw_zs(
        self,
        meta: CChanPlotMeta,
        row: int,
        color='orange',
        linewidth=2,
        sub_linewidth=0.5,
        show_text=False,
        fontsize=14,
        text_color='orange',
        draw_one_bi_zs=False,
    ):
        linewidth = max(linewidth, 2)
        x_begin = self._x_begin(row)
        for zs_meta in meta.zs_lst:
            if not draw_one_bi_zs and zs_meta.is_onebi_zs:
                continue
            if zs_meta.begin + zs_meta.w < x_begin:
                continue
            dash = "solid" if zs_meta.is_sure else "dash"
            self._shape(row, dict(
                type="rect",
                x0=zs_meta.begin, x1=zs_meta.begin + zs_meta.w,
                y0=zs_meta.low, y1=zs_meta.low + zs_meta.h,
                line=dict(color=c2rgba(color), width=linewidth, dash=dash),
            ))
            for sub_zs_meta in zs_meta.sub_zs_lst:
                self._shape(row, dict(
                    type="rect",
                    x0=sub_zs_meta.begin, x1=sub_zs_meta.begin + sub_zs_meta.w,
                    y0=sub_zs_meta.low, y1=sub_zs_meta.low + sub_zs_meta.h,
                    line=dict(color=c2rgba(color), width=sub_linewidth, dash=dash),
                ))
            if show_text:
                self._zs_text(row, zs_meta, fontsize, text_color)
                for sub_zs_meta in zs_meta.sub_zs_lst:
                    self._zs_text(row, sub_zs_meta, fontsize, text_color)

    def draw_segzs(self, meta: CChanPlotMeta, row: int, color='red', linewidth=10, sub_linewidth=4):
        linewidth = max(linewidth, 2)
        x_begin = self._x_begin(row)
        for zs_meta in meta.segzs_lst:
            if zs_meta.begin + zs_meta.w < x_begin:
                continue
            dash = "solid" if zs_meta.is_sure else "dash"
            self._shape(row, dict(
                type="rect",
                x0=zs_meta.begin, x1=zs_meta.begin + zs_meta.w,
                y0=zs_meta.low, y1=zs_meta.low + zs_meta.h,
                line=dict(color=c2rgba(color), width=linewidth, dash=dash),
            ))
            for sub_zs_meta in zs_meta.sub_zs_lst:
                self._shape(row, dict(
                    type="rect",
                    x0=sub_zs_meta.begin, x1=sub_zs_meta.begin + sub_zs_meta.w,
                    y0=sub_zs_meta.low, y1=sub_zs_meta.low + sub_zs_meta.h,
                    line=dict(color=c2rgba(color), width=sub_linewidth, dash=dash),
                ))

    def draw_macd(self, meta: CChanPlotMeta, row: int, x_limits, width=0.4):
        """MACD 副图

        柱子保持 width（默认 0.4 格）的细柱形态；K线很多时（如 30M 级别上千根）
        每根柱不足 1px，抗锯齿会把颜色冲淡到几乎看不见，此时补一圈同色描边兜底
        （描边宽度是屏幕像素，不随缩放变细），既能看见又不变成粗柱。
        """
        ctx = self._row(row)
        macd_lst = [klu.macd for klu in meta.klu_iter()]
        assert macd_lst[0] is not None, "you can't draw macd until you delete macd_metric=False"

        x_begin = x_limits[0]
        x_idx = list(range(len(macd_lst)))[x_begin:]
        dif_line = [macd.DIF for macd in macd_lst[x_begin:]]
        dea_line = [macd.DEA for macd in macd_lst[x_begin:]]
        macd_bar = [macd.macd for macd in macd_lst[x_begin:]]
        y_min = min([min(dif_line), min(dea_line), min(macd_bar)])
        y_max = max([max(dif_line), max(dea_line), max(macd_bar)])

        xs = self._xs(ctx, x_idx)
        colors = [c2rgba("r") if m >= 0 else c2rgba("#006400") for m in macd_bar]
        # 柱被压到不足 ~1.5px 时补一圈同色描边（描边以屏幕像素计，不随缩放变细）
        px_per_bar = (self.figure.layout.width or 1000) / max(1, len(macd_bar))
        outline_w = 1 if px_per_bar * width < 1.5 else 0
        self._trace(go.Bar(
            x=xs, y=macd_bar,
            marker=dict(color=colors, line=dict(color=colors, width=outline_w)),
            width=width, name="macd", showlegend=False,
        ), row)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=dif_line, mode="lines",
                               line=dict(color=c2rgba("#FFA500"), width=1.2), name="DIF", showlegend=False), row)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=dea_line, mode="lines",
                               line=dict(color=c2rgba("#0000ff"), width=1.2), name="DEA", showlegend=False), row)
        self._update_axis(ctx.yref, dict(range=[y_min, y_max], autorange=False))

    def draw_mean(self, meta: CChanPlotMeta, row: int):
        ctx = self._row(row)
        mean_lst = [klu.trend[TREND_TYPE.MEAN] for klu in meta.klu_iter()]
        Ts = list(mean_lst[0].keys())
        # 直接取 matplotlib 的 hsv colormap，保证与 CPlotDriver.draw_mean 配色逐条一致
        cmap = get_colormap('hsv', max([10, len(Ts)]))
        for cmap_idx, T in enumerate(Ts):
            mean_arr = [mean_dict[T] for mean_dict in mean_lst]
            self._trace(go.Scatter(
                x=self._xs(ctx, range(len(mean_arr))), y=mean_arr,
                mode="lines", line=dict(width=1.2, color=c2rgba(cmap(cmap_idx))),
                name=f'{T} meanline', showlegend=True,
            ), row)

    def draw_channel(self, meta: CChanPlotMeta, row: int, T=None, top_color="r", bottom_color="b", linewidth=3, linestyle="solid"):
        ctx = self._row(row)
        max_lst = [klu.trend[TREND_TYPE.MAX] for klu in meta.klu_iter()]
        min_lst = [klu.trend[TREND_TYPE.MIN] for klu in meta.klu_iter()]
        config_T_lst = sorted(list(max_lst[0].keys()))
        if T is None:
            T = config_T_lst[-1]
        elif T not in max_lst[0]:
            raise CChanException(f"plot channel of T={T} is not setted in CChanConfig.trend_metrics = {config_T_lst}", ErrCode.PLOT_ERR)
        top_array = [_d[T] for _d in max_lst]
        bottom_array = [_d[T] for _d in min_lst]
        dash = "solid" if linestyle in ("solid", "-") else "dash"
        self._trace(go.Scatter(x=self._xs(ctx, range(len(top_array))), y=top_array, mode="lines",
                               line=dict(color=c2rgba(top_color), width=linewidth, dash=dash),
                               name=f'{T}-TOP-channel', showlegend=True), row)
        self._trace(go.Scatter(x=self._xs(ctx, range(len(bottom_array))), y=bottom_array, mode="lines",
                               line=dict(color=c2rgba(bottom_color), width=linewidth, dash=dash),
                               name=f'{T}-BUTTOM-channel', showlegend=True), row)

    def draw_boll(self, meta: CChanPlotMeta, row: int, mid_color="black", up_color="blue", down_color="purple"):
        ctx = self._row(row)
        x_begin = int(self._x_begin(row))
        try:
            ma = [klu.boll.MID for klu in meta.klu_iter()][x_begin:]
            up = [klu.boll.UP for klu in meta.klu_iter()][x_begin:]
            down = [klu.boll.DOWN for klu in meta.klu_iter()][x_begin:]
        except AttributeError as e:
            raise CChanException("you can't draw boll until you set boll_n in CChanConfig", ErrCode.PLOT_ERR) from e

        x_idx = list(range(x_begin, x_begin + len(ma)))
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=ma, mode="lines",
                               line=dict(color=c2rgba(mid_color), width=1.2), name="boll-MID",
                               showlegend=False), row)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=up, mode="lines",
                               line=dict(color=c2rgba(up_color), width=1.2), name="boll-UP",
                               showlegend=False), row)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=down, mode="lines",
                               line=dict(color=c2rgba(down_color), width=1.2), name="boll-DOWN",
                               showlegend=False), row)
        self.y_min = min([self.y_min, min(down)])
        self.y_max = max([self.y_max, max(up)])

    def bsp_common_draw(self, bsp_list, row: int, buy_color, sell_color, fontsize, arrow_l, arrow_h, arrow_w):
        x_begin = self._x_begin(row)
        y_range = self.y_max - self.y_min
        for bsp in bsp_list:
            if bsp.x < x_begin:
                continue
            color = buy_color if bsp.is_buy else sell_color
            arrow_dir = 1 if bsp.is_buy else -1
            arrow_len = arrow_l * y_range
            arrow_head = arrow_len * arrow_h
            text_y = bsp.y - arrow_len * arrow_dir

            self._arrow(row, bsp.x, text_y, bsp.y, arrow_head, arrow_w, c2rgba(color))
            self._anno(row, x=bsp.x, y=text_y,
                       text=f'{bsp.desc()}', font=dict(size=fontsize, color=c2rgba(color)),
                       yanchor="top" if bsp.is_buy else "bottom", xanchor="center")
            if text_y < self.y_min:
                self.y_min = text_y
            if text_y > self.y_max:
                self.y_max = text_y

    def draw_bs_point(self, meta: CChanPlotMeta, row: int, buy_color='r', sell_color='g', fontsize=15, arrow_l=0.15, arrow_h=0.2, arrow_w=1):
        self.bsp_common_draw(
            bsp_list=meta.bs_point_lst,
            row=row,
            buy_color=buy_color,
            sell_color=sell_color,
            fontsize=fontsize,
            arrow_l=arrow_l,
            arrow_h=arrow_h,
            arrow_w=arrow_w,
        )

    def draw_seg_bs_point(self, meta: CChanPlotMeta, row: int, buy_color='r', sell_color='g', fontsize=18, arrow_l=0.2, arrow_h=0.25, arrow_w=1.2):
        self.bsp_common_draw(
            bsp_list=meta.seg_bsp_lst,
            row=row,
            buy_color=buy_color,
            sell_color=sell_color,
            fontsize=fontsize,
            arrow_l=arrow_l,
            arrow_h=arrow_h,
            arrow_w=arrow_w,
        )

    def draw_marker(
        self,
        meta: CChanPlotMeta,
        row: int,
        markers: Dict[Union[CTime, str], Tuple[str, Literal['up', 'down'], str]],
        arrow_l=0.15,
        arrow_h_r=0.2,
        arrow_w=1,
        fontsize=14,
        default_color='b',
    ):
        # {'2022/03/01': ('xxx', 'up', 'red'), '2022/03/02': ('yyy', 'down')}
        ctx = self._row(row)
        x_begin, x_end = self._x_begin(row), meta.klu_len - 1
        datetick_dict = {date: idx for idx, date in enumerate(meta.datetick)}

        new_marker = {}
        for klu in meta.klu_iter():
            for date, marker in markers.items():
                date_str = date.to_str() if isinstance(date, CTime) else date
                if klu.include_sub_lv_time(date_str) and klu.time.to_str() != date_str:
                    new_marker[klu.time.to_str()] = marker
        new_marker.update(markers)

        kl_dict = dict(enumerate(meta.klu_iter()))
        y_range = self.y_max - self.y_min
        arrow_len = arrow_l * y_range
        arrow_h = arrow_len * arrow_h_r
        for date, marker in new_marker.items():
            if isinstance(date, CTime):
                date = date.to_str()
            if date not in datetick_dict:
                continue
            x = datetick_dict[date]
            if x < x_begin or x > x_end:
                continue
            if len(marker) == 2:
                color = default_color
                marker_content, position = marker
            else:
                assert len(marker) == 3
                marker_content, position, color = marker
            assert position in ['up', 'down']
            _dir = -1 if position == 'up' else 1
            bench = kl_dict[x].high if position == 'up' else kl_dict[x].low
            text_y = bench - arrow_len * _dir
            self._arrow(row, x, text_y, bench, arrow_h, arrow_w, c2rgba(color))
            self._anno(row, x=x, y=text_y, text=marker_content,
                       font=dict(size=fontsize, color=c2rgba(color)),
                       yanchor="bottom" if position == 'up' else "top",
                       xanchor="center")

    def draw_rsi(self, meta: CChanPlotMeta, row: int, color='b'):
        ctx = self._row(row)
        data = [klu.rsi for klu in meta.klu_iter()]
        x_begin, x_end = int(self._x_begin(row)), meta.klu_len
        x_idx = list(range(x_begin, x_end))
        sec = self._secondary_y(row)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=data[x_begin:x_end], mode="lines",
                               line=dict(color=c2rgba(color), width=1.2), name="RSI",
                               showlegend=False), row, yaxis=sec)

    def draw_kdj(self, meta: CChanPlotMeta, row: int, k_color='orange', d_color='blue', j_color='pink'):
        ctx = self._row(row)
        kdj = [klu.kdj for klu in meta.klu_iter()]
        x_begin, x_end = int(self._x_begin(row)), meta.klu_len
        x_idx = list(range(x_begin, x_end))
        sec = self._secondary_y(row)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=[x.k for x in kdj][x_begin:x_end], mode="lines",
                               line=dict(color=c2rgba(k_color), width=1.2), name="K",
                               showlegend=True), row, yaxis=sec)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=[x.d for x in kdj][x_begin:x_end], mode="lines",
                               line=dict(color=c2rgba(d_color), width=1.2), name="D",
                               showlegend=True), row, yaxis=sec)
        self._trace(go.Scatter(x=self._xs(ctx, x_idx), y=[x.j for x in kdj][x_begin:x_end], mode="lines",
                               line=dict(color=c2rgba(j_color), width=1.2), name="J",
                               showlegend=True), row, yaxis=sec)

    def draw_demark(
        self,
        meta: CChanPlotMeta,
        row: int,
        setup_color='b',
        countdown_color='r',
        fontsize=12,
        min_setup=9,
        max_countdown_background='yellow',
        begin_line_color: Optional[str] = 'purple',
        begin_line_style='dashed',
    ):
        x_begin = self._x_begin(row)
        text_height: Optional[float] = None
        plot_begin_set = set()  # 同一线条的 TDST 横线只画一次（matplotlib 版在循环内重置，此处外提，视觉效果相同）
        for klu in meta.klu_iter():
            if klu.idx < x_begin:
                continue
            under_bias, upper_bias = 0, 0
            for demark_idx in klu.demark.get_setup():
                if demark_idx['series'].idx < min_setup or not demark_idx['series'].setup_finished:
                    continue
                self._demark_begin_line(row, begin_line_color, plot_begin_set, begin_line_style, demark_idx)
                txt_y = klu.low - under_bias if demark_idx['dir'] == BI_DIR.DOWN else klu.high + upper_bias
                self._anno(row, x=klu.idx, y=txt_y,
                           text=str(demark_idx['idx']),
                           font=dict(size=fontsize, color=c2rgba(setup_color)),
                           yanchor="top" if demark_idx['dir'] == BI_DIR.DOWN else "bottom",
                           xanchor="center")
                if demark_idx['dir'] == BI_DIR.DOWN:
                    under_bias += self._txt_h(fontsize)
                else:
                    upper_bias += self._txt_h(fontsize)
            for demark_idx in klu.demark.get_countdown():
                box_bias = 0.5 * text_height if text_height is not None and demark_idx['idx'] == CDemarkEngine.MAX_COUNTDOWN else 0
                txt_y = klu.low - under_bias - box_bias if demark_idx['dir'] == BI_DIR.DOWN else klu.high + upper_bias + box_bias
                bg = None
                if demark_idx['idx'] == CDemarkEngine.MAX_COUNTDOWN:
                    bg = c2rgba(max_countdown_background)
                self._anno(row, x=klu.idx, y=txt_y,
                           text=str(demark_idx['idx']),
                           font=dict(size=fontsize, color=c2rgba(countdown_color)),
                           bgcolor=bg,
                           yanchor="top" if demark_idx['dir'] == BI_DIR.DOWN else "bottom",
                           xanchor="center")
                if text_height is None:
                    text_height = self._txt_h(fontsize)
                if demark_idx['dir'] == BI_DIR.DOWN:
                    under_bias += self._txt_h(fontsize)
                else:
                    upper_bias += self._txt_h(fontsize)

    # ────────────────────────────────────────────────────────────
    #  辅助
    # ────────────────────────────────────────────────────────────
    def _secondary_y(self, row: int) -> str:
        """懒创建某行的副 y 轴（对应 matplotlib ax.twinx()），返回轴 id"""
        sec_id = f"y{row + 100}"
        if sec_id not in self._sec_y_created:
            ctx = self._row(row)
            self.figure.layout[_axis_key(sec_id)] = go.layout.YAxis(
                overlaying=ctx.yref,
                side="right",
                anchor=ctx.xref,
                showgrid=False,
                zeroline=False,
                showline=False,
                tickfont=dict(size=10, color='#57606a'),
            )
            self._sec_y_created.add(sec_id)
        return sec_id

    def _bi_text(self, row: int, bi_idx: int, bi: CBi_meta, end_fontsize, end_color):
        if bi_idx == 0:
            self._anno(row, x=bi.begin_x, y=bi.begin_y, text=f'{bi.begin_y:.5f}',
                       font=dict(size=end_fontsize, color=c2rgba(end_color)),
                       yanchor="top" if bi.dir == BI_DIR.UP else "bottom", xanchor="center")
        self._anno(row, x=bi.end_x, y=bi.end_y, text=f'{bi.end_y:.5f}',
                   font=dict(size=end_fontsize, color=c2rgba(end_color)),
                   yanchor="top" if bi.dir == BI_DIR.DOWN else "bottom", xanchor="center")

    def _zs_text(self, row: int, zs_meta: CZS_meta, fontsize, text_color):
        self._anno(row, x=zs_meta.begin, y=zs_meta.low, text=f'{zs_meta.low:.2f}',
                   font=dict(size=fontsize, color=c2rgba(text_color)),
                   yanchor="top", xanchor="center")
        self._anno(row, x=zs_meta.begin + zs_meta.w, y=zs_meta.low + zs_meta.h,
                   text=f'{zs_meta.low + zs_meta.h:.2f}',
                   font=dict(size=fontsize, color=c2rgba(text_color)),
                   yanchor="bottom", xanchor="center")

    def _demark_begin_line(self, row: int, begin_line_color, plot_begin_set: set, linestyle: str, demark_idx: T_DEMARK_INDEX):
        if begin_line_color is not None and demark_idx['series'].TDST_peak is not None and id(demark_idx['series']) not in plot_begin_set:
            if demark_idx['series'].countdown is not None:
                end_idx = demark_idx['series'].countdown.kl_list[-1].idx
            else:
                end_idx = demark_idx['series'].kl_list[-1].idx
            dash = "solid" if linestyle in ("solid", "-") else "dash"
            self._shape(row, dict(
                type="line",
                x0=demark_idx['series'].kl_list[CDemarkEngine.SETUP_BIAS].idx,
                x1=end_idx,
                y0=demark_idx['series'].TDST_peak,
                y1=demark_idx['series'].TDST_peak,
                line=dict(color=c2rgba(begin_line_color), width=1.2, dash=dash),
            ))
            plot_begin_set.add(id(demark_idx['series']))

    # ────────────────────────────────────────────────────────────
    #  对外输出
    # ────────────────────────────────────────────────────────────
    def show(self):
        """在浏览器中打开交互图表"""
        self.figure.show()

    def to_html(self, path, include_plotlyjs="inline", full_html=True, auto_open=False):
        """导出为独立 HTML 文件（include_plotlyjs: 'inline'离线可用 / 'cdn'体积小）"""
        self.figure.write_html(path, include_plotlyjs=include_plotlyjs,
                               full_html=full_html, auto_open=auto_open)
        return path

    def to_json(self):
        return self.figure.to_json()

    def save2img(self, path, width=None, height=None, scale=1):
        """导出静态图片（png/svg/pdf...），需要 `pip install kaleido`"""
        try:
            self.figure.write_image(path, width=width, height=height, scale=scale)
        except Exception as e:  # noqa: BLE001 - 统一给出可操作的提示
            raise CChanException(
                f"save2img failed: {e}; plotly 静态导出需要 kaleido，请先 `pip install kaleido`，"
                f"或改用 to_html() 导出交互网页",
                ErrCode.PLOT_ERR,
            ) from e
        return path
