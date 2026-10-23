# -*- coding: utf-8 -*-
"""
y75_app.py —— Y75 六维力传感器 上位机（tkinter，纯标准库）

对接协议：AA 55 [CMD] [PAYLOAD] 0D 0A（见 y75_protocol.py / 下位机 rs485_cmd.c）

启动：
    python y75_app.py
    python y75_app.py --port COM28 --baud 460800
    python y75_app.py --sim                       # 直接连本机模拟器

没有硬件时，先起模拟器再让上位机连它：
    python y75_simulator.py --port 15020
    python y75_app.py --sim --sim-port 15020

界面速览：
    左栏      连接 / 设备信息 / 采集控制 / 输出设置 / 零点
    右上      六个通道的大号数值卡片（无数据时显示「—」）
    右中      实时波形 | 解耦矩阵 | 报文日志
    底栏      运行日志 + 开始/停止保存数据到 Excel
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from bisect import bisect_left, bisect_right
from collections import deque
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Deque, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from y75_protocol import (                                     # noqa: E402
    AXIS_NAMES, BAUD_CHOICES, ByteOrder, FORMAT_UNIT, TcpTransport, Y75Client,
    Y75Error, Y75Timeout, fmt_value,
)
from y75_serial import SerialPort, SerialError, list_ports     # noqa: E402
from y75_xlsx import XlsxLimitError, XlsxTableWriter           # noqa: E402

# ---------------------------------------------------------------------
#  外观
# ---------------------------------------------------------------------
BG = "#f5f6f8"
CARD = "#ffffff"
EDGE = "#d8dce2"
TXT = "#22262b"
TXT_DIM = "#707780"
GRID = "#e8ebef"
AXIS = "#c6cbd2"
ACCENT = "#2f6fd0"
COLORS = ("#d94a4a", "#2f9e5f", "#2f6fd0", "#d98a1f", "#8b5cf6", "#0f9b9b")

LEVEL_COLOR = {"info": TXT_DIM, "tx": ACCENT, "rx": "#2f9e5f",
               "warn": "#c47b12", "err": "#d94a4a"}

#: 保存数据写出的 Excel 表头，顺序固定
SAVE_HEADERS = AXIS_NAMES


def axis_units(base: str) -> Tuple[str, ...]:
    """按通道给显示单位：0-2 是力（FX/FY/FZ），3-5 是力矩（TX/TY/TZ）。

    力矩的量纲是 力 × 长度：N → N·m，kg → kg·m；mV 等原始量纲六路一致。
    """
    if base == "N":
        return ("N", "N", "N", "N·m", "N·m", "N·m")
    if base == "kg":
        return ("kg", "kg", "kg", "kg·m", "kg·m", "kg·m")
    return (base,) * 6

#: 连接方式
MODE_SERIAL = "串口 (RS485)"
MODE_TCP = "网络模拟 (TCP)"

#: 输出模式
OUT_HEX = "HEX 标准帧"
OUT_ASC = "ASC 调试帧"

#: 波形刷新率（标签是 fps，值是毫秒周期）。
#: 越高越顺滑、越费 CPU：老机器或同时开很多东西时往下调。
REFRESH_CHOICES = ("20", "25", "30", "40", "50", "60")
REFRESH_DEFAULT = "50"

#: 横轴模式（界面下拉的文字 ↔ WaveCanvas 的 X_TIME / X_POINTS）
X_MODE_LABELS = ("时间 (s)", "点数")
X_MODE_TIME_LABEL, X_MODE_PTS_LABEL = X_MODE_LABELS
#: 时间模式下可选的跨度（秒）
SPAN_SEC_CHOICES = ("1", "2", "5", "10", "20")
#: 点数模式下可选的跨度（点）。上限 20000 = 波形缓冲 WAVE_MAX 的容量，
#: 再大就只能拿到缓冲里剩下的那点，标尺会名不副实。
SPAN_PTS_CHOICES = ("1000", "2000", "5000", "10000", "20000")
SPAN_PTS_DEFAULT = 2000


def refresh_to_ms(label: str) -> int:
    try:
        fps = int(label)
    except (TypeError, ValueError):
        fps = int(REFRESH_DEFAULT)
    return max(10, int(round(1000.0 / max(1, fps))))


# =====================================================================
#  总线仲裁设置：界面的 StringVar → 客户端的 BusGate
# =====================================================================
def apply_bus_cfg(cli, cfg: dict) -> None:
    """把一组（已经脱离 Tk 变量的）纯数值写进客户端的 BusGate。"""
    bus = cli.bus
    bus.enabled = bool(cfg.get("bus_wait", True))
    bus.window_ms = max(0.0, float(cfg.get("bus_window", 0.0) or 0.0))
    bus.max_wait_ms = max(0.0, float(cfg.get("bus_maxwait", 250.0) or 0.0))
    cli.bus_retry = max(0, min(3, int(cfg.get("bus_retry", 0) or 0)))


FMT_CHOICES = (("原始数据 (mV)", 0), ("矩阵解耦 (kg)", 1), ("矩阵解耦 (N)", 2))


def pick_font() -> str:
    """挑一个本机存在的中文字体。"""
    try:
        families = set(tkfont.families())
    except Exception:                                          # pragma: no cover
        families = set()
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei"):
        if name in families:
            return name
    return "TkDefaultFont"


def app_dir() -> str:
    """程序所在目录：打包后是 exe 所在目录，源码运行时是脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# =====================================================================
#  实时波形
# =====================================================================
class WaveCanvas(tk.Canvas):
    """
    六通道实时波形（Canvas 自绘）。

    要让它看起来「柔顺」，光提高帧率没用，得同时压住四个抖动来源：

    1. 时间轴按真实时间匀速滑动
       串口/USB 是按 8~16ms 一批把数据送上来的。如果直接把时间轴右边界
       钉在「最新一帧的到达时刻」上，波形就会一批一批地往前蹦 —— 这是
       同一条数据流在别的串口软件里看着顺、在这里却很卡的主因。
       这里改成「本地时钟 − 显示延迟」匀速回放。

       而且这个延迟**必须自适应**：固定 60ms 时，只要某一批数据晚到
       （USB 驱动 Latency Timer + 固件主循环发送抖动，很容易超），
       右边界就会被兜底夹子钉住 → 停一下、再蹦一下，正是肉眼看到的
       「一卡一卡」。所以这里实测「最新样本的年龄」并跟踪其峰值，
       延迟 = 峰值 + 余量：夹子几乎永不触发，右边界恒速前进；
       链路变干净后峰值缓慢衰减，延迟自己降回来（跟手度不受损失）。

    2. Y 轴不许「呼吸」
       每帧拿可见数据重算 min/max 会让量程上下抽动，波形跟着一起抽。
       改成：立刻扩张（绝不裁掉尖峰）、缓慢收缩（不来回抖）。

    3. 只画可见区间
       调用方（Y75Client.take_wave）已经按时间窗裁过了。如果拿整段
       缓冲去抽稀，落在可视区里的点会只剩一半，曲线又稀又毛糙 ——
       这是「看起来一跳一跳」的第二个来源。

    4. 图元复用
       不再每帧 delete("all") 重建 40 多个图元，只对自己的曲线做
       coords() 更新；网格/坐标轴/图例只在尺寸、时间窗或单位变化时重排。

    横轴有两种模式（界面上可切换）：

    * **时间（s）** —— 窗口 = N 秒，刻度写秒；
    * **点数** —— 窗口 = N 个采样点（1000~20000），刻度直接写点数。
      设备实测节拍并不是协议里写的 500/1000Hz（常在 300~500Hz 之间漂），
      按点数看波形时，一个周期占多少点、两帧之间到底隔几点，都是确定的
      —— 做频谱/找节拍/数点对齐时比秒更直观。

    **两种模式的横轴位置算法完全相同，而且只看点数、完全不看时间戳**：
    维护一个「右边界落后最新样点多少个点」的量，每帧按平滑后的帧周期推进，
    再用速度微调拉回目标落后量（= 显示延迟换算成的点数）。
    这样设备时钟怎么漂、怎么重锚，画面都不会跳 —— 见 draw() 第 1b 节。
    """

    #: 横轴模式
    X_TIME = "time"
    X_POINTS = "pts"

    PAD_L, PAD_R, PAD_T, PAD_B = 66, 14, 16, 28
    #: 两次重画的最小间隔（≈83fps 上限），用来吸收 resize 风暴
    MIN_DT = 0.012
    #: Y 轴收缩速度：每帧向目标量程靠拢这么多（扩张是瞬时的）
    SHRINK_K = 0.10
    #: 显示延迟（秒）：数据先攒进缓冲，波形按本地时钟匀速回放。
    #: 串口驱动是**成批**把字节交上来的（USB 8~16ms 一批），若右边界跟着
    #: 「最新一帧的到达时刻」走，就会停一下、蹦一下。留出延迟、缓冲里始终
    #: 有货，右边界才能匀速前进，与数据到达节奏彻底解耦。
    #: 实际延迟 = max(LATENCY_MIN, 实测样本年龄峰值 + LATENCY_MARGIN)，
    #: 见 draw()。TCP 回环时样本年龄≈0，延迟就取 LATENCY_MIN。
    #:
    #: 30ms 的来历：CH340 把 Latency Timer 调到 1ms 后，实测「接收批间隔」
    #: 只有 9.9ms，样本年龄峰值约 10~13ms，加上 15ms 余量就是 25~28ms。
    #: 原来写 50ms 时白白多压了 20ms 的滞后（手感上很实在）。
    #: 真遇到驱动更糟的机器，自适应会按实测峰值自动抬上去，不会因此卡住。
    LATENCY_MIN = 0.03
    #: 自适应延迟的余量：盖住两次测量之间新出现的抖动，别让夹子擦到边
    LATENCY_MARGIN = 0.015
    #: 样本年龄峰值的衰减时间常数（秒）。链路变干净后延迟按它回落。
    AGE_TAU = 1.2
    #: 自适应延迟的上限：再糟也不让操作者感觉到「明显的滞后」
    LATENCY_MAX = 0.50
    #: 是否启用自适应延迟。基准脚本会把它关掉、退回固定 60ms 做 A/B 对照。
    ADAPTIVE = True

    def __init__(self, master, window_sec: float = 5.0, **kw):
        super().__init__(master, bg=CARD, highlightthickness=1,
                         highlightbackground=EDGE, **kw)
        self.window_sec = float(window_sec)
        #: 横轴模式（X_TIME / X_POINTS）
        self.x_mode = self.X_TIME
        #: 点数模式的窗口点数（相邻点等间距铺满宽度）
        self.window_pts = 2000
        #: 实测采样间隔（秒），点数模式用它把时间换算成点
        self.dt_est = 0.0
        self.enabled = [True] * 6
        self.unit = "N"

        self._font = "TkDefaultFont"
        self._t_end: Optional[float] = None      # 右边界对应的设备时刻（仅统计用）
        #: 时间轴右边界的目标值。只许往前（延迟自适应抖动时不许往回拉）
        self._t_target: Optional[float] = None
        #: 右边界所在的**绝对采样序号**（自本次连接起累计）。横轴位置的唯一
        #: 来源：每帧按速度积分前进，与数据到货节奏、时间戳都无关（draw 1b）
        self._pos_abs: Optional[float] = None
        #: 当前切片首点的绝对序号（= count - n_all）。切片被裁剪时自动补偿
        self._base = 0.0
        #: 右边界落后最新样点多少个点（诊断/统计用）
        self._back: Optional[float] = None
        #: 右边界当前的浮点索引（只给基准脚本/诊断看，绘制不依赖它）
        self._idx_pos = 0.0
        self._mono = 0.0                         # 上次重画的 monotonic 时刻
        #: 平滑后的帧周期（秒）。见 draw()：位移用它，而不是本帧实测间隔
        self._dt_smooth = 0.0
        #: 时间模式定格的窗口点数（见 draw()：不跟 dt_est 每帧抖）
        self._np_time: Optional[float] = None
        self._np_time_src = 0.0
        #: True = 上面那个值是在「还没量到节拍」时用标称 500Hz 凑的，下一帧重定格
        self._np_time_prov = False
        #: 数据速率（点/秒），由**累计帧计数**的增量算出，见 _measure_rate()
        self._rate_pts = 0.0
        self._rate_t: Optional[float] = None
        self._rate_head = 0.0
        #: 自适应显示延迟：最新样本「年龄」的峰值跟踪（秒）
        self._age_peak = 0.0
        self._newest_seen = 0.0
        #: 当前生效的显示延迟（秒）+ 兜底夹子触发统计（给运行统计用）
        self.latency = 0.0
        self.clamp_n = 0
        self.clamp_sec = 0.0
        self.draw_n = 0
        self._lo: Optional[float] = None         # 平滑后的量程
        self._hi: Optional[float] = None
        self._last_samples: List[Tuple[float, Tuple[float, ...]]] = []
        self._items: dict = {}
        self._placed = None                      # 版式签名，变了才重排
        self._ylab_txt: List[Optional[str]] = [None] * 5
        # 图元可见状态也要缓存：itemconfigure(state=...) 即使值没变也会让
        # Tk 重新安排重绘，每帧白调 7 次是纯浪费
        self._curve_on: List[bool] = [False] * 6
        self._hint_on: Optional[bool] = None
        self._hint_txt = ""

        # 实测刷新质量：fps 与帧间隔抖动。给「运行统计」用 ——
        # 卡不卡不该靠感觉吵，两个上位机摆在一起看数字就行。
        self._draw_times: Deque[float] = deque(maxlen=40)
        self.fps = 0.0
        self.jitter_ms = 0.0

        # 窗口拉伸后必须重画，否则曲线停在旧尺寸里、右边留一大块空白
        self.bind("<Configure>", self._on_resize)

    # ------------------------------------------------------------------
    #  对外
    # ------------------------------------------------------------------
    def set_window_sec(self, v: float) -> None:
        """换时间窗：时间轴与量程都重新起算，否则会拿旧量程去套新数据。"""
        if abs(v - self.window_sec) < 1e-9:
            return
        self.window_sec = float(v)
        self.reset(replot=True)

    def set_x_mode(self, mode: str) -> None:
        """切换横轴：时间 ↔ 点数。右边界与量程都重起，避免拿旧标尺套新数据。"""
        if mode not in (self.X_TIME, self.X_POINTS) or mode == self.x_mode:
            return
        self.x_mode = mode
        self.reset(replot=True)

    def set_window_pts(self, n: int) -> None:
        """换点数窗。范围夹到 100~100000，免得手滑输个 0 把除零打出来。"""
        n = int(max(100, min(100000, int(n))))
        if n == self.window_pts:
            return
        self.window_pts = n
        self.reset(replot=True)

    def span_sec(self) -> float:
        """
        当前横轴覆盖多少**秒**。

        时间模式就是 window_sec；点数模式是「点数 × 实测采样间隔」。
        取数据时按它多要一截，保证右边界那段延迟里的点也拿得到。
        """
        if self.x_mode == self.X_TIME:
            return self.window_sec
        dt = self.dt_est if self.dt_est > 0.0 else 1.0 / 500.0
        return max(self.window_pts * dt, 0.001)

    def _measure_rate(self, head_abs: float, now: float) -> None:
        """
        实测数据速率（点/秒）：累计帧计数的增量 ÷ 墙钟增量，EMA 平滑。

        **横轴位置只信这个数，不信 dt_est。** dt_est 是从帧时间戳算出来的，
        而真机时间戳会被两处逻辑动过：`|now − _dev_t| > 0.4s` 就重锚、
        超前 TS_AHEAD 就硬拉回。一旦被带偏，`dt_s/dt_est` 能算错几十倍，
        画面要么狂奔要么冻住。帧计数是单调递增的整数，绝对可靠。
        """
        if self._rate_t is not None:
            span = now - self._rate_t
            if span > 1e-4:
                inst = (head_abs - self._rate_head) / span
                if 0.5 < inst < 200000.0:      # 0.5Hz~200kHz 之外不采信
                    self._rate_pts = (inst if self._rate_pts <= 0.0
                                      else self._rate_pts * 0.9 + inst * 0.1)
        self._rate_t, self._rate_head = now, head_abs

    def _pt_rate(self) -> float:
        """当前数据速率（点/秒）。还没量到时退回 dt_est，再退回标称 500Hz。"""
        if self._rate_pts > 0.0:
            return self._rate_pts
        if self.dt_est > 0.0:
            return 1.0 / self.dt_est
        return 500.0

    def np_win(self) -> float:
        """一屏覆盖多少个采样点 —— 时间/点数两种模式的**唯一口径**。"""
        if self.x_mode == self.X_POINTS:
            return float(max(2, int(self.window_pts)))
        if (self._np_time is not None and not self._np_time_prov
                and self._np_time_src == self.window_sec):
            return self._np_time
        return max(2.0, self.window_sec * self._pt_rate())

    def want_points(self) -> int:
        """
        重绘需要多少个样点。**两种模式都按个数取，绝不按时间戳取。**

        老实现时间模式用 `take_wave(window_sec)`，那是按时间戳切的
        （`t0 = wave[-1][0] − window_sec`）。真机上 `wave[-1][0]` 是设备时钟
        推算值，会被重锚/硬拉 —— 它一跳，切出来的**样本个数**就跟着变，
        draw() 里「绝对序号 → 切片下标」的换算基准 `_base` 随之突跳，
        整条曲线往左甩一截再慢慢爬回来，看着就是「点从最右边重新刷到左边」。
        TCP 模拟器节拍精确、从不重锚，所以永远复现不出来。

        按个数取之后 `n_all` 只跟帧计数有关，`_base` 是纯粹的计数差，
        时间戳怎么跳都动不了画面。
        """
        rate = self._pt_rate()
        # 右边界落后最新点 lag_pts 个点（= 显示延迟 × 速率），上限按
        # LATENCY_MAX 留；再垫 128 个点给抽稀步长和右端补点。
        return int(self.np_win()) + int(self.LATENCY_MAX * rate) + 128

    def reset(self, replot: bool = False) -> None:
        """清掉时间轴与量程（断线、重连、换时间窗时用）。"""
        self._t_end = None
        self._t_target = None
        self._pos_abs = None                    # 横轴位置重新起算（见 draw 1b）
        self._back = None
        self._np_time = None                    # 时间模式窗口点数重新定格
        self._np_time_prov = False
        self._rate_pts = 0.0                    # 数据速率重新实测
        self._rate_t = None
        self._lo = self._hi = None
        self._ylab_txt = [None] * 5
        self._age_peak = 0.0
        self._newest_seen = 0.0
        self.latency = 0.0
        self.clamp_n = 0
        self.clamp_sec = 0.0
        self.draw_n = 0
        if replot and self._last_samples:
            self.draw(self._last_samples, force=True)
        else:
            self._last_samples = []
            self.clear()

    def clear(self) -> None:
        """只负责画「暂无数据」，不动时间轴 / 量程。"""
        if not self._items:
            return
        for i in range(6):
            self._set_curve(i, False)
        w, h = self._size()
        self.coords(self._items["hint"], w / 2, h / 2)
        self._set_hint(True, "暂无数据 —— 点左侧「开始连续」进入采集")

    def _set_curve(self, i: int, on: bool) -> None:
        if self._curve_on[i] == on:
            return
        self._curve_on[i] = on
        self.itemconfigure(self._items["curve"][i],
                           state="normal" if on else "hidden")

    def _set_hint(self, on: bool, text: str = "") -> None:
        if self._hint_on == on and (not on or self._hint_txt == text):
            return
        self._hint_on = on
        self._hint_txt = text
        it = self._items["hint"]
        if on:
            self.itemconfigure(it, state="normal", text=text)
        else:
            self.itemconfigure(it, state="hidden")

    def set_font(self, name: str) -> None:
        self._font = name
        f8 = (name, 8)
        for key in ("ylab", "xlab", "lgtext"):
            for item in self._items.get(key, []):
                self.itemconfigure(item, font=f8)
        if self._items.get("hint") is not None:
            self.itemconfigure(self._items["hint"], font=(name, 11))
        self._placed = None

    def _on_resize(self, _event=None) -> None:
        if self._last_samples:
            self.after_idle(lambda: self.draw(self._last_samples, force=True))

    # ------------------------------------------------------------------
    #  图元：建一次，之后只更新
    # ------------------------------------------------------------------
    def _size(self) -> Tuple[int, int]:
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1:
            w = max(self.winfo_reqwidth(), 400)
        if h <= 1:
            h = max(self.winfo_reqheight(), 200)
        return w, h

    def _build_items(self) -> None:
        it = self._items
        if it:
            return
        f8 = (self._font, 8)
        it["gv"] = [self.create_line(0, 0, 0, 0, fill=GRID) for _ in range(6)]
        it["gh"] = [self.create_line(0, 0, 0, 0, fill=GRID) for _ in range(4)]
        it["ax"] = [self.create_line(0, 0, 0, 0, fill=AXIS) for _ in range(2)]
        it["ylab"] = [self.create_text(0, 0, anchor="e", fill=TXT_DIM,
                                       font=f8, text="") for _ in range(5)]
        it["xlab"] = [self.create_text(0, 0, fill=TXT_DIM, font=f8, text="")
                      for _ in range(6)]
        it["lgbox"] = [self.create_rectangle(0, 0, 0, 0, fill=COLORS[i],
                                             outline="") for i in range(6)]
        it["lgtext"] = [self.create_text(0, 0, anchor="w", fill=TXT, font=f8,
                                         text="") for i in range(6)]
        it["curve"] = [self.create_line(0, 0, 0, 0, fill=COLORS[i], width=2,
                                        capstyle="round", joinstyle="round",
                                        state="hidden") for i in range(6)]
        it["hint"] = self.create_text(0, 0, fill=TXT_DIM, font=(self._font, 11),
                                      text="", state="hidden")

    def _ensure_placed(self, w: int, h: int) -> None:
        """网格/坐标轴/图例只在尺寸、跨度或单位变化时重排（约 35 次 Tcl 调用）。"""
        sig = (w, h, self.window_sec, self.window_pts, self.x_mode, self.unit)
        if sig == self._placed:
            return
        self._placed = sig
        it = self._items
        pad_l, pad_r, pad_t, pad_b = self.PAD_L, self.PAD_R, self.PAD_T, self.PAD_B
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b

        for i, ln in enumerate(it["gv"]):
            x = pad_l + pw * i / 5
            self.coords(ln, x, pad_t, x, pad_t + ph)
        for i, ln in enumerate(it["gh"]):
            y = pad_t + ph * i / 4
            self.coords(ln, pad_l, y, pad_l + pw, y)
        self.coords(it["ax"][0], pad_l, pad_t, pad_l, pad_t + ph)
        self.coords(it["ax"][1], pad_l, pad_t + ph, pad_l + pw, pad_t + ph)

        for i, tx in enumerate(it["ylab"]):
            self.coords(tx, pad_l - 7, pad_t + ph * i / 4)
        for i, tx in enumerate(it["xlab"]):
            self.coords(tx, pad_l + pw * i / 5, pad_t + ph + 13)
            if self.x_mode == self.X_POINTS:
                # 点数轴：右端 = 最新一点（0），往左数。整数，别给小数丢人。
                self.itemconfigure(
                    tx, text=f"{int(round(-self.window_pts + self.window_pts * i / 5))}")
            else:
                rel = -self.window_sec + self.window_sec * i / 5
                self.itemconfigure(tx, text=f"{rel:+.1f}s")

        x = pad_l + 10
        units = axis_units(self.unit)
        for i in range(6):
            self.coords(it["lgbox"][i], x, pad_t + 7, x + 11, pad_t + 17)
            self.coords(it["lgtext"][i], x + 16, pad_t + 12)
            self.itemconfigure(it["lgtext"][i],
                               text=f"{AXIS_NAMES[i]} ({units[i]})")
            x += 82

    def _set_ylabels(self, lo: float, hi: float) -> None:
        for i, tx in enumerate(self._items["ylab"]):
            s = _tick(hi - (hi - lo) * i / 4)
            if self._ylab_txt[i] != s:            # 文字没变就别碰 Tk
                self._ylab_txt[i] = s
                self.itemconfigure(tx, text=s)

    # ------------------------------------------------------------------
    #  绘制
    # ------------------------------------------------------------------
    def draw(self, samples: List[Tuple[float, Tuple[float, ...]]],
             force: bool = False, count: Optional[int] = None) -> None:
        now = time.monotonic()
        if not force and now - self._mono < self.MIN_DT:
            return
        dt = now - self._mono if self._mono else 0.0
        self._mono = now
        # ---- 帧周期平滑 ----
        # Tk 的 after() 在 Windows 上并不准：设定 20ms，实测在 20~42ms 之间
        # 抖（变异系数 20%+）。而屏幕上的位移就是「本帧间隔 × 速度」——
        # 拿实测间隔直接算，等于把定时器的抖动原样画出来：这一帧走 3 像素、
        # 下一帧走 6 像素，眼睛看到的就是「走一格、停一拍」的定格感。
        # 用 EMA 滤掉单帧抖动后，每帧走一样多；长期均值仍等于真实帧周期，
        # 不会与本地时钟漂开（残余误差由下面的 rate 修正兜着）。
        if 0.0 < dt < 0.5:
            self._dt_smooth = (dt if self._dt_smooth <= 0.0
                               else self._dt_smooth * 0.82 + dt * 0.18)
        if self._dt_smooth <= 0.0:
            self._dt_smooth = self.MIN_DT
        dt_s = self._dt_smooth
        self._draw_times.append(now)
        if len(self._draw_times) >= 4:
            ts = list(self._draw_times)
            iv = [b - a for a, b in zip(ts, ts[1:])]
            self.fps = len(iv) / sum(iv)
            mean = sum(iv) / len(iv)
            self.jitter_ms = (sum((x - mean) ** 2 for x in iv) / len(iv)) ** 0.5 * 1000.0

        w, h = self._size()
        self._build_items()
        it = self._items
        pad_l, pad_r, pad_t, pad_b = self.PAD_L, self.PAD_R, self.PAD_T, self.PAD_B
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b
        if pw < 20 or ph < 20:
            return
        self._ensure_placed(w, h)

        active = [i for i in range(6) if self.enabled[i]]
        if not samples:
            self.reset()
            return
        if not active:
            # 只是把所有通道都取消勾选了：保留时间轴，只把曲线藏起来
            for i in range(6):
                self._set_curve(i, False)
            self.coords(it["hint"], w / 2, h / 2)
            self._set_hint(True, "未勾选任何通道")
            return

        self._last_samples = samples
        newest = samples[-1][0]

        # ---- 1. 时间轴：按本地时钟匀速回放 ----
        # 右边界 = 「本地时钟 − 显示延迟」，而不是「最新一帧的到达时刻」。
        #
        # 延迟是**自适应**的。串口路径上，数据先被 USB 驱动攒成 8~16ms 一批
        # （CH340/CP210x/FTDI 的 Latency Timer），再叠加固件主循环发送节拍的
        # 抖动，所以「最新样本有多旧」是浮动的。若延迟写死 60ms，某次成批到达
        # 晚于 60ms，下面的兜底夹子就会把右边界钉住 —— 停一下、再蹦一下，
        # 正是肉眼看到的「一卡一卡」（TCP 回环时样本年龄≈0，所以从不触发，
        # 这就是「网络模拟丝滑、串口卡」的真正原因）。
        #
        # 所以：实测样本年龄，跟踪其峰值；延迟 = 峰值 + 余量。
        #   峰值立刻跟上（一次晚到就把延迟抬够，绝不让夹子咬住），
        #   空闲时按 AGE_TAU 缓慢衰减（链路变干净后延迟自己降回来，不白留滞后）。
        wall = time.time()
        age = wall - newest
        if age < 0.0:
            age = 0.0
        self.draw_n += 1
        fresh = newest > self._newest_seen
        if fresh:
            self._newest_seen = newest
        if fresh and age > self._age_peak:
            self._age_peak = age                    # 立刻跟上，宁可多留一点延迟
        else:
            self._age_peak = max(0.0, self._age_peak
                                 - (dt / self.AGE_TAU) * self._age_peak)
        latency = min(self.LATENCY_MAX,
                      max(self.LATENCY_MIN, self._age_peak + self.LATENCY_MARGIN))
        if not self.ADAPTIVE:
            latency = self.LATENCY_MIN               # 固定延迟（A/B 对照用）
        self.latency = (latency if self.latency <= 0.0
                        else self.latency + (latency - self.latency) * 0.1)

        n_all = len(samples)

        # ---- 1b. 横轴位置：绝对点数积分，不看时间戳、也不看「这批到了几点」 ----
        #
        # 这是「每次都从头重新加载」的根治办法。老实现有三个致命耦合：
        #
        #   ① 右边界 = t_end（**本机时钟**口径），却拿它跟 newest（**设备时钟
        #      推算值**）相夹。真机推流率 300~500Hz 而 sample_dt 按 500Hz 推算，
        #      几秒钟就差几十毫秒 → `t_end = newest` 把右边界整体往回拽 →
        #      整条曲线猛跳一截、再花十几秒爬回去，看着就是「从头重来」。
        #   ② 右边界 = n_all - back。n_all 只在**数据到货**那一刻跳变（USB 驱动
        #      9.9ms 一批、5 帧一跳），所以没有新帧的那几帧位置纹丝不动、到货
        #      那帧整批跳一次 —— 这就是「一格一格 / 像在重新加载」。
        #   ③ 时间模式下 n_all 是**按时间戳切**出来的（见 want_points 的说明），
        #      设备时钟一重锚，切片长度就变，换算基准 _base 跟着突跳 →
        #      「点从最右边重新刷到左边」。这是真机独有、TCP 模拟复现不了的。
        #
        # 现在：样本一律**按个数**取（n_all 只跟帧计数有关），位置是一个
        # **绝对采样序号**的积分，速率也由帧计数实测（_measure_rate）。
        #   * 与时间戳无关 → 设备时钟漂移、重锚、停止/开始，画面都不动；
        #   * 与到货节奏无关 → 数据一批一批来，画面照样匀速走；
        #   * 位置是速度的积分，永远不会突跳（除了断流后重锚）。
        head_abs = (count - 1) if count is not None else (n_all - 1)
        self._measure_rate(head_abs, time.monotonic())
        rate = self._pt_rate()                      # 点/秒，实测帧计数得来
        if self._rate_pts > 0.0:
            # 采样周期只从帧计数导出，**绝不从帧时间戳算**。真机时间戳会被
            # 协议层的兜底路径按 sample_dt*0.05 挤压（设备时钟跑到本机时间
            # 前面时），连续触发就能让「相邻间隔中位数」从 2ms 塌到 0.1ms；
            # 而一屏点数 = window_sec/dt_est，当场从 2500 变成 50000 ——
            # 整条曲线被压到右边缘、再随数据往左扫，就是「点从最右边刷到
            # 左边」。TCP 模拟器时间戳永远规整，所以复现不出来。
            self.dt_est = 1.0 / self._rate_pts
        pts = dt_s * rate                           # 本帧「应该」走过多少个点
        lag_pts = max(6.0, latency * rate)          # 目标落后量（显示延迟换算成点）
        if self._pos_abs is None or head_abs + lag_pts < self._pos_abs - 1.0:
            # 首次，或累计帧数倒退（重连后 count 从 0 重来）→ 重新起算
            self._pos_abs = float(head_abs) - lag_pts
        else:
            err_pts = (head_abs - lag_pts) - self._pos_abs
            # 误差 1 点改 2% 速度，最多 ±35%。长期平均速度 = 数据速率，
            # 所以不会越跑越偏；瞬时速度平滑，屏幕上就是匀速平移。
            speed = 1.0 + max(-0.35, min(0.35, err_pts * 0.02))
            self._pos_abs += pts * speed
        # 绝对序号 → 当前切片里的下标（切片被裁剪时 _base 会跟着变，自动补偿）
        self._base = float(head_abs - (n_all - 1))
        idx_end = self._pos_abs - self._base
        if idx_end > n_all - 1:
            # 数据没跟上（断流 / 掉帧）：停住，右侧不留空白
            idx_end = float(n_all - 1)
            self._pos_abs = idx_end + self._base
            self.clamp_n += 1
            self.clamp_sec += dt
        if idx_end < 0.0:
            idx_end = 0.0
            self._pos_abs = self._base
        self._back = float(head_abs) - self._pos_abs
        if self._back > max(2.0 * rate, lag_pts * 8.0):
            # 断流超过 2 秒：此时屏幕右侧早就空了，重锚不会有可见跳变
            self._back = lag_pts
            self._pos_abs = float(head_abs) - lag_pts
        idx_end = max(0.0, min(idx_end, float(n_all - 1)))
        self._idx_pos = idx_end          # 右边界在缓冲里的浮点索引（诊断用）

        # 抽稀上限：把整个宽度铺满，**一个像素最多一个点**。
        # 超过 1 点/像素的画面对眼睛没有增益，但 Tk 画 2px 宽的折线是按填充多边形
        # 走的，点多一倍重绘就贵一倍（实测 1250 点 6.6ms / 2500 点 22ms）。
        # 上限 1400：4K 屏上波形拉到 3000+ 像素宽时，若无上限 6 条曲线要画
        # 近 2 万个点，draw() 会从 2ms 涨到 20ms+，整帧率直接腰斩 ——
        # Modbus-TCP 版就是用 800 点硬顶住才一直顺的。
        max_pts = min(max(int(pw), 128), 1400)

        def _step(n: int) -> int:
            """
            抽稀步长。

            整数步长有个坑：只要 n 比像素数多一点点，步长就从 1 跳到 2，
            画出来的点当场少一半（1000 点铺 847px 只剩 500 点，肉眼可见变糙）。
            所以先允许到 1.4 点/像素（Tk 这点开销实测只多 1~2ms），
            真超了才按 n/max_pts 整步抽稀。
            """
            if n <= int(max_pts * 1.4):
                return 1
            return max(1, -(-n // max_pts))

        # ---- 2. 横轴：统一按「采样点序号」铺 ----
        # 时间模式和点数模式画法**完全一样**：相邻点严格等间距，区别只在
        # 刻度文字——一个把「第几个点」换算成秒，一个直接写点数。
        #
        # 为什么不拿帧时间戳当 x？（这是「串口卡、TCP 顺」的真正原因）
        # 串口路径上帧时间戳是「设备时钟推算值」。USB 驱动一批 5 帧一起交上来，
        # 推算步长（实测 sample_dt）只要有 1% 误差，几百帧后就与本地时钟错开
        # 几十毫秒，于是被「超前 >80ms 就拉回」的护栏硬拽一下 —— 那一瞬间
        # 相邻点的间距突然变短，整条曲线在水平方向被「收一下再放开」。
        # 单次只有几毫秒，但隔十几秒来一次，看着就是「数据从前面被推到后面」。
        # TCP 回环没有 USB 缓冲，时间戳不起伏，所以从来没这毛病。
        #
        # 按序号铺则彻底免疫：点间距严格相等，右边界是一个匀速推进的**浮点
        # 索引**，屏幕上就是像素级的匀速平移，与数据怎么到达完全无关。
        # 时间模式的窗口点数在「量到速率之后」定格一次，之后不再每帧微抖：
        # 速率的 0.1% 抖动会让 kx（每点像素）变化 → 每个点的取整结果变 →
        # 曲线**形状**每帧都在抖，这就是「擦掉重画感」的最后一块拼图。
        if self.x_mode == self.X_TIME and (
                self._np_time is None or self._np_time_prov
                or self._np_time_src != self.window_sec):
            self._np_time = max(2.0, self.window_sec * rate)
            # 还没实测到速率（第一帧）时先用标称值顶着，量到后立刻重定格一次：
            # 真机推流率常在 300~500Hz，一直用 500Hz 算会让「5 秒」显示成 7.5 秒
            self._np_time_prov = (self._rate_pts <= 0.0)
            self._np_time_src = self.window_sec
        np_win = self.np_win()
        self._idx_pos = idx_end          # 右边界在切片里的浮点索引（诊断用）
        x0 = idx_end - (np_win - 1.0)                    # 左端索引（一屏正好 np_win 个点）
        kx = pw / (np_win - 1.0)                         # 每个点占多少像素

        k_hi = min(n_all, int(idx_end) + 1)              # 右端不越过右边界
        n_vis = k_hi - max(0, int(x0 // 1))
        step = _step(n_vis)
        # 取样起点必须落在 **step 的全局网格** 上。若只对齐到「本帧的 x0」，
        # 相邻两帧同一序号对应的物理点会错开 —— 同序号位移不一致，曲线形状
        # 依然在抖。对齐到网格后，两帧的点集是同一条网格线的整体平移。
        k_lo = max(0, (int(x0 // 1) // step) * step)
        if k_hi <= k_lo:
            k_lo, k_hi = max(0, n_all - 1), n_all
        ks = list(range(k_lo, k_hi, step))
        if ks and ks[-1] != k_hi - 1:
            ks.append(k_hi - 1)                          # 抽稀也要保住最后一个点
        # ---- 整像素量子化：形状稳定的最后一块拼图 ----
        # 每个点的 x = pad_l + (k - x0)·kx，k 是整数、kx 不是，所以 x 的小数
        # 部分**只由 k 决定**。若 x0 也带小数（每帧都在变），Tk 对每个点独立
        # 取整后，相邻两帧的取整结果各不相同 → 曲线**形状**每帧都在轻微抖动，
        # 看起来不像"平移"而像"擦掉重画"。把 x0 量子化到整数像素后，取整
        # 形状完全锁定，每帧只剩一个整体的整数像素平移——这是 Tk 的物理极限，
        # 跨度大、每帧位移不足 1px 时必然一格一格，任何渲染技巧都救不了，
        # 只能把跨度调小让每帧位移 ≥2px 才会真正"丝滑"。
        x0 = round(x0 * kx) / kx
        xs = [pad_l + (k - x0) * kx for k in ks]
        ys = [samples[k][1] for k in ks]

        # ---- 3. 量程：扩张瞬时、收缩缓慢 ----
        lo, hi = float("inf"), float("-inf")
        for vals in ys:
            for i in active:
                v = vals[i]
                if v < lo:
                    lo = v
                if v > hi:
                    hi = v
        if lo == float("inf"):
            lo, hi = -1.0, 1.0
        if hi - lo < 1e-9:
            span = max(abs(hi) * 0.2, 1.0)
            lo, hi = lo - span, hi + span
        margin = (hi - lo) * 0.12
        tlo, thi = lo - margin, hi + margin
        if self._lo is None:
            self._lo, self._hi = tlo, thi
        else:
            self._lo = (tlo if tlo < self._lo
                        else self._lo + (tlo - self._lo) * self.SHRINK_K)
            self._hi = (thi if thi > self._hi
                        else self._hi + (thi - self._hi) * self.SHRINK_K)
        lo, hi = self._lo, self._hi

        self._set_ylabels(lo, hi)
        self._set_hint(False)

        y_scale = ph / (hi - lo)
        for i in range(6):
            if not self.enabled[i]:
                self._set_curve(i, False)
                continue
            coords: List[float] = []
            ap = coords.append
            for x, vals in zip(xs, ys):
                ap(x)
                ap(pad_t + (hi - vals[i]) * y_scale)
            if len(coords) >= 4:
                self.coords(it["curve"][i], *coords)
                self._set_curve(i, True)
            else:
                self._set_curve(i, False)


def _tick(v: float) -> str:
    a = abs(v)
    if a >= 10000 or (0 < a < 0.01):
        return f"{v:.1e}"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.3f}"


# =====================================================================
#  主程序
# =====================================================================
class App:
    #: 波形刷新周期（毫秒）。界面上的「刷新」下拉框会覆盖它。
    #: 30ms ≈ 33fps，实测帧间隔抖动只有 ±2ms，滚动起来足够顺。
    TICK_MS = refresh_to_ms(REFRESH_DEFAULT)
    #: 数值卡片 / 统计文字每 N 个 tick 更新一次（≈11Hz）。
    #: 500Hz 的数据按 33Hz 刷数字只会糊成一片、根本读不出来，反而显得「卡」。
    TEXT_EVERY = 3
    #: 单个 tick 最多往日志文本框写多少行。总线上有异常帧时日志可能每秒几百条，
    #: 逐条 insert + see("end") 足以把界面从 35fps 拖到 23fps（实测）。
    #: 超出的行只丢显示、不丢队列（队列照样抽干，不会积压）。
    LOG_PER_TICK = 8

    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.font = pick_font()
        self.save_dir = args.save_dir or app_dir()

        self.cli: Optional[Y75Client] = None
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.connected = False
        self._port_map = {}

        # 数据保存
        self.saving = False
        self.writer: Optional[XlsxTableWriter] = None
        self.save_path = ""
        self.save_pending: List[Tuple[float, Tuple[float, ...]]] = []
        self.save_lock = threading.Lock()
        self.save_batches = 0

        # 显示状态
        self.last_push_snapshot = -1
        self.rate_hist: List[Tuple[float, int]] = []
        self.frame_rate = 0.0
        self.send_mode = 0
        self.data_format = 1
        self.tx_warned = False
        self._auto_started = False        # --autoconnect 只用一次
        self._tick_n = 0                  # tick 计数，用来给数字显示降频
        self._skipped_logs = 0            # 因限流被合并的日志条数
        self._skip_note_at = 0.0          # 上次提示「已合并」的时刻
        self.tick_ms = self.TICK_MS       # 波形刷新周期，可由界面「刷新」调整

        self._build_ui()
        self._refresh_ports()
        self.root.after(self.tick_ms, self._tick)

        if args.sim or args.host:
            self.mode_var.set(MODE_TCP)
            self.host_var.set(args.host or "127.0.0.1")
            self.tcp_port_var.set(str(args.sim_port))
            self._on_mode_change()
            self.root.after(200, self.on_connect)
        elif args.port:
            self.port_var.set(args.port)
            self.root.after(200, self.on_connect)

    # ==================================================================
    #  界面搭建
    # ==================================================================
    def _build_ui(self) -> None:
        self.root.title("中科米点 六维力传感器上位机")
        self.root.configure(bg=BG)
        self._apply_theme()

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:                                    # pragma: no cover
            pass
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TXT)
        st.configure("TLabelframe", background=BG, bordercolor=EDGE)
        st.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                     font=(self.font, 9, "bold"))
        st.configure("TButton", font=(self.font, 9))
        st.configure("TNotebook", background=BG, bordercolor=EDGE)
        st.configure("TNotebook.Tab", font=(self.font, 9), padding=(14, 6))
        st.configure("TCheckbutton", background=BG, foreground=TXT)
        st.configure("TRadiobutton", background=BG, foreground=TXT)

        root = self.root
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        # ---------- 顶栏 ----------
        top = tk.Frame(root, bg=CARD, highlightthickness=1, highlightbackground=EDGE)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(top, text="中科米点 Y75", bg=CARD, fg=TXT,
                 font=(self.font, 13, "bold")).pack(side="left", padx=14, pady=9)
        tk.Label(top, text="协议: AA 55 [CMD] [PAYLOAD] 0D 0A   ·   默认 460800 8N1",
                 bg=CARD, fg=TXT_DIM, font=(self.font, 9)).pack(side="left")
        self.conn_label = tk.Label(top, text="未连接", bg=CARD, fg=TXT_DIM,
                                   font=(self.font, 10, "bold"))
        self.conn_label.pack(side="right", padx=16)

        # ---------- 左栏（可滚动）----------
        # 左侧各分组是自然高度、不参与伸缩；窗口不够高时最后的分组会被直接裁掉，
        # 所以套一层 Canvas + 滚动条兜底（内容不超高就不显示滚动条）。
        left_outer = tk.Frame(root, bg=BG)
        left_outer.grid(row=1, column=0, sticky="ns")
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)
        # 不加 yscrollincrement：那会把拖滚动条量化成 20px 一跳，手感就是「卡卡的」
        self.left_canvas = tk.Canvas(left_outer, bg=BG, highlightthickness=0, bd=0,
                                     width=316)
        sb = ttk.Scrollbar(left_outer, orient="vertical", command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=sb.set)
        self.left_canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.left_sb = sb
        left = ttk.Frame(self.left_canvas, padding=(10, 10, 6, 10))
        win = self.left_canvas.create_window((0, 0), window=left, anchor="nw")
        # scrollregion 只能取「内容自身尺寸」。用 bbox("all") 会把**已移动**的
        # 内框位置算进去：每拖一格滚动条就改写一次滚动域 → 视图再被夹一次 →
        # 一次鼠标事件里窗口搬好几回（卡顿 + Windows 上留下重影），而且越滚
        # 滚动域越缩、真底部永远滚不到。
        self._left_sr = None
        left.bind("<Configure>", self._sync_left_scrollregion)
        self.left_canvas.bind("<Configure>",
                              lambda e: self.left_canvas.itemconfigure(win, width=e.width))
        self._left_inner = left

        self._build_left(left)

        # ---------- 右主区 ----------
        right = ttk.Frame(root, padding=(8, 8, 10, 6))
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self._build_cards(right)
        self._build_notebook(right)

        # ---------- 底栏 ----------
        self._build_bottom(root)

        self._bind_wheel(left)
        self._update_left_scrollbar()
        self.root.after(120, self._update_left_scrollbar)   # 首次布局完再校一次
        self.root.bind("<Configure>",
                       lambda e: self._update_left_scrollbar() if e.widget is self.root else None)
        self._fit_window()

    # ------------------------------------------------------------------
    def _apply_theme(self) -> None:
        """Windows 高分屏：按系统缩放调整字号，窗口大小等布局算完再贴合内容。"""
        self.scale = 1.0
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
                self.scale = ctypes.WinDLL("shcore").GetScaleFactorForDevice(0) / 100.0
            except Exception:
                self.scale = 1.0
        try:
            self.root.tk.call("tk", "scaling", 1.3333 * self.scale)
        except Exception:
            pass

    def _fit_window(self) -> None:
        """
        窗口尺寸贴合内容，并夹在屏幕工作区内。

        别拍脑袋定一个「1360x880」—— 不同缩放比例下控件自然尺寸差很多，
        硬塞一个过大的值会被系统裁掉，左栏反而被挤窄、按钮被切一半。
        """
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(self.root.winfo_reqwidth() + 24, sw - 32)
        h = min(self.root.winfo_reqheight() + 24, sh - 72)
        w = max(w, int(980 * self.scale))
        h = max(h, int(620 * self.scale))
        w = min(w, sw - 32)
        h = min(h, sh - 72)
        self.root.geometry(f"{int(w)}x{int(h)}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(max(880, int(760 * self.scale)), max(600, int(560 * self.scale)))
        self.root.update_idletasks()

    # ------------------------------------------------------------------
    def _build_left(self, p: ttk.Frame) -> None:
        row = 0

        def group(title: str) -> ttk.Frame:
            nonlocal row
            lf = ttk.Labelframe(p, text=" " + title + " ", padding=(8, 6))
            lf.grid(row=row, column=0, sticky="ew", pady=(0, 8))
            row += 1
            return lf

        # ---- 连接 ----
        g = group("连接")
        g.columnconfigure(0, weight=1)
        self.mode_var = tk.StringVar(value=MODE_SERIAL)
        cb = ttk.Combobox(g, textvariable=self.mode_var, state="readonly",
                          values=(MODE_SERIAL, MODE_TCP), font=(self.font, 9))
        cb.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())

        self.serial_box = ttk.Frame(g)
        self.serial_box.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.serial_box.columnconfigure(0, weight=1)
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(self.serial_box, textvariable=self.port_var,
                                    state="readonly", values=(), font=(self.font, 9))
        self.port_cb.grid(row=0, column=0, sticky="ew")
        ttk.Button(self.serial_box, text="刷新", width=5,
                   command=self._refresh_ports).grid(row=0, column=1, padx=(4, 0))
        br = ttk.Frame(self.serial_box)
        br.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        tk.Label(br, text="波特率", bg=BG, fg=TXT_DIM, font=(self.font, 9)).pack(side="left")
        self.baud_var = tk.StringVar(value=str(self.args.baud))
        ttk.Combobox(br, textvariable=self.baud_var, width=9, state="readonly",
                     values=[str(b) for b in BAUD_CHOICES],
                     font=(self.font, 9)).pack(side="left", padx=(6, 0))
        self.dtr_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.serial_box, text="拉高 DTR/RTS（USB-RS485 转换器取电）",
                        variable=self.dtr_var).grid(row=2, column=0, columnspan=2,
                                                    sticky="w", pady=(2, 0))
        self.port_hint = tk.Label(self.serial_box, text="", bg=BG, fg=TXT_DIM,
                                  font=(self.font, 8), anchor="w", justify="left")
        self.port_hint.grid(row=3, column=0, columnspan=2, sticky="ew")

        self.tcp_box = ttk.Frame(g)
        self.tcp_box.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.tcp_box.columnconfigure(0, weight=1)
        self.tcp_box.columnconfigure(2, weight=1)
        tk.Label(self.tcp_box, text="主机", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).grid(row=0, column=0, sticky="w")
        tk.Label(self.tcp_box, text="端口", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.tcp_port_var = tk.StringVar(value=str(self.args.sim_port))
        tk.Entry(self.tcp_box, textvariable=self.host_var,
                 font=(self.font, 9)).grid(row=1, column=0, sticky="ew")
        tk.Entry(self.tcp_box, textvariable=self.tcp_port_var, width=7,
                 font=(self.font, 9)).grid(row=1, column=1, columnspan=2,
                                           sticky="ew", padx=(6, 0))
        tk.Label(self.tcp_box, text="TCP 模式只用于连本机模拟器，真机走串口",
                 bg=BG, fg=TXT_DIM, font=(self.font, 8),
                 wraplength=250, justify="left").grid(row=2, column=0, columnspan=3,
                                                      sticky="w", pady=(3, 0))

        btns = ttk.Frame(g)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self.btn_connect = ttk.Button(btns, text="连接", command=self.on_connect)
        self.btn_connect.grid(row=0, column=0, sticky="ew")
        self.btn_disconnect = ttk.Button(btns, text="断开", command=self.on_disconnect)
        self.btn_disconnect.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.btn_disconnect.state(["disabled"])

        # ---- 设备信息 ----
        g = group("设备信息")
        g.columnconfigure(1, weight=1)
        self.info_labels = {}
        for i, (key, title) in enumerate((("sn", "序列号"), ("ver", "固件版本"),
                                          ("state", "运行状态"), ("err", "通信错误"))):
            tk.Label(g, text=title, bg=BG, fg=TXT_DIM, font=(self.font, 9)).grid(
                row=i, column=0, sticky="w", pady=1)
            lab = tk.Label(g, text="—", bg=BG, fg=TXT, font=(self.font, 9),
                           anchor="w", justify="left")
            lab.grid(row=i, column=1, sticky="ew", pady=1)
            self.info_labels[key] = lab
        ttk.Button(g, text="读取设备信息", command=self.on_read_info).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # ---- 采集控制 ----
        g = group("采集控制")
        g.columnconfigure(0, weight=1)
        g.columnconfigure(1, weight=1)
        ttk.Button(g, text="开始连续", command=self.on_start).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(g, text="停止", command=self.on_stop).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(g, text="单次采集", command=self.on_single).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        fr = ttk.Frame(g)
        fr.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        tk.Label(fr, text="输出频率", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left")
        self.freq_var = tk.StringVar(value="500")
        ttk.Combobox(fr, textvariable=self.freq_var, width=7, state="readonly",
                     values=("500", "1000"), font=(self.font, 9)).pack(side="left", padx=(6, 0))
        ttk.Button(fr, text="应用", width=6, command=self.on_set_freq).pack(side="left", padx=(4, 0))
        tk.Label(g, text="500 Hz = 每 2 ms 一帧；1000 Hz = 每 1 ms 一帧",
                 bg=BG, fg=TXT_DIM, font=(self.font, 8), anchor="w",
                 justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(3, 0))

        # ---- 总线仲裁（半双工 485）----
        g = group("总线仲裁")
        g.columnconfigure(1, weight=1)
        self.bus_wait_var = tk.BooleanVar(value=True)
        self.bus_window_var = tk.StringVar(value="0")
        self.bus_maxwait_var = tk.StringVar(value="250")
        self.bus_retry_var = tk.StringVar(value="0")

        ttk.Checkbutton(g, text="发送前先等总线空闲（等当前帧收完）",
                        variable=self.bus_wait_var,
                        command=self._apply_bus_cfg).grid(
            row=0, column=0, columnspan=4, sticky="w")

        def num_cell(r: int, label: str, var: tk.StringVar, unit: str,
                     lo: int, hi: int, tip: str = "") -> None:
            tk.Label(g, text=label, bg=BG, fg=TXT_DIM, font=(self.font, 9)).grid(
                row=r, column=0, sticky="w", pady=1)
            sp = ttk.Spinbox(g, textvariable=var, from_=lo, to=hi, increment=1,
                             width=6, font=(self.font, 9),
                             command=self._apply_bus_cfg)
            sp.grid(row=r, column=1, sticky="w", padx=(4, 2))
            sp.bind("<Return>", lambda e: self._apply_bus_cfg())
            sp.bind("<FocusOut>", lambda e: self._apply_bus_cfg())
            tk.Label(g, text=unit, bg=BG, fg=TXT_DIM, font=(self.font, 8)).grid(
                row=r, column=2, sticky="w")
            if tip:
                tk.Label(g, text=tip, bg=BG, fg=TXT_DIM, font=(self.font, 8),
                         anchor="w").grid(row=r, column=3, sticky="w", padx=(6, 0))

        num_cell(1, "帧后窗口", self.bus_window_var, "ms", 0, 20, "0 = 自动")
        num_cell(2, "最多等", self.bus_maxwait_var, "ms", 0, 2000)
        num_cell(3, "无应答重发", self.bus_retry_var, "次", 0, 3)

        self.bus_stat_label = tk.Label(g, text="", bg=BG, fg=TXT_DIM,
                                       font=(self.font, 8), anchor="w",
                                       justify="left", wraplength=250)
        self.bus_stat_label.grid(row=4, column=0, columnspan=4, sticky="ew",
                                 pady=(3, 0))
        tk.Label(g, text="MAX13487 是自动方向芯片，主机没法让总线；\n"
                         "设备推流时只有帧间那 ~1.4 ms 能插话。",
                 bg=BG, fg=TXT_DIM, font=(self.font, 8), anchor="w",
                 justify="left").grid(row=5, column=0, columnspan=4, sticky="w")

        # ---- 输出设置 ----
        g = group("输出设置")
        g.columnconfigure(1, weight=1)
        tk.Label(g, text="数据单位", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).grid(row=0, column=0, sticky="w")
        self.fmt_var = tk.StringVar(value=FMT_CHOICES[2][0])
        ttk.Combobox(g, textvariable=self.fmt_var, state="readonly",
                     values=[n for n, _ in FMT_CHOICES], width=19,
                     font=(self.font, 9)).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(g, text="kg / N 会写进 EEPROM 掉电保持，mV 不会",
                 bg=BG, fg=TXT_DIM, font=(self.font, 8), anchor="w",
                 justify="left").grid(row=1, column=0, columnspan=2, sticky="w")

        tk.Label(g, text="输出模式", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.out_var = tk.StringVar(value=OUT_HEX)
        ttk.Combobox(g, textvariable=self.out_var, state="readonly",
                     values=(OUT_HEX, OUT_ASC), width=19,
                     font=(self.font, 9)).grid(row=2, column=1, sticky="ew",
                                               padx=(4, 0), pady=(5, 0))

        tk.Label(g, text="浮点字节序", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).grid(row=3, column=0, sticky="w", pady=(5, 0))
        self.order_var = tk.StringVar(value=ByteOrder.DEFAULT)
        ttk.Combobox(g, textvariable=self.order_var, state="readonly",
                     values=ByteOrder.NAMES, width=16,
                     font=(self.font, 9)).grid(row=3, column=1, sticky="ew",
                                               padx=(4, 0), pady=(5, 0))
        self.order_hint = tk.Label(g, text="", bg=BG, fg=TXT_DIM, font=(self.font, 8),
                                   anchor="w", justify="left", wraplength=250)
        self.order_hint.grid(row=4, column=0, columnspan=2, sticky="w")
        self.order_var.trace_add("write", lambda *_: self._update_order_hint())
        self._update_order_hint()

        ttk.Button(g, text="应用输出设置", command=self.on_apply_output).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        tk.Label(g, text="设备波特率", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.dev_baud_var = tk.StringVar(value="460800")
        ttk.Combobox(g, textvariable=self.dev_baud_var, state="readonly", width=16,
                     values=[str(b) for b in BAUD_CHOICES],
                     font=(self.font, 9)).grid(row=6, column=1, sticky="ew",
                                               padx=(4, 0), pady=(8, 0))
        ttk.Button(g, text="下发并切换本机串口", command=self.on_set_baud).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        tk.Label(g, text="⚠ 改完设备会立刻换档，上位机要同步切过去",
                 bg=BG, fg="#c47b12", font=(self.font, 8), anchor="w",
                 justify="left", wraplength=250).grid(row=8, column=0, columnspan=2,
                                                      sticky="w")

        # ---- 零点 ----
        g = group("零点")
        g.columnconfigure(0, weight=1)
        g.columnconfigure(1, weight=1)
        ttk.Button(g, text="置零", command=self.on_zero).grid(row=0, column=0, sticky="ew")
        ttk.Button(g, text="取消置零", command=self.on_unzero).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(g, text="置零是异步的：设备内部采 20 点求平均，约 0.5 秒",
                 bg=BG, fg=TXT_DIM, font=(self.font, 8), anchor="w",
                 justify="left", wraplength=250).grid(row=1, column=0, columnspan=2,
                                                      sticky="w", pady=(3, 0))

        # ---- 运行统计 ----
        g = group("运行统计")
        g.columnconfigure(1, weight=1)
        self.stat_labels = {}
        for i, key in enumerate(("rate", "frames", "stream", "resync",
                                 "garbage", "short", "wave", "latency", "batch")):
            title = {"rate": "帧率", "frames": "已收数据帧", "stream": "其中连续帧",
                     "resync": "拆帧重同步", "garbage": "丢弃乱码字节",
                     "short": "空载荷畸形帧", "wave": "波形刷新",
                     "latency": "显示延迟", "batch": "接收批间隔"}[key]
            tk.Label(g, text=title, bg=BG, fg=TXT_DIM, font=(self.font, 8)).grid(
                row=i, column=0, sticky="w")
            lab = tk.Label(g, text="0", bg=BG, fg=TXT, font=(self.font, 9, "bold"),
                           anchor="e")
            lab.grid(row=i, column=1, sticky="e")
            self.stat_labels[key] = lab

    # ------------------------------------------------------------------
    def _build_cards(self, parent: ttk.Frame) -> None:
        wrap = tk.Frame(parent, bg=BG)
        wrap.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for i in range(6):
            wrap.columnconfigure(i, weight=1)
        self.cards = []
        for i, name in enumerate(AXIS_NAMES):
            card = tk.Frame(wrap, bg=CARD, highlightthickness=1,
                            highlightbackground=EDGE)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 5, 0))
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x", padx=9, pady=(7, 0))
            tk.Label(head, text=name, bg=CARD, fg=COLORS[i],
                     font=(self.font, 10, "bold")).pack(side="left")
            unit = tk.Label(head, text=axis_units("N")[i], bg=CARD, fg=TXT_DIM,
                            font=(self.font, 8))
            unit.pack(side="right")
            val = tk.Label(card, text="—", bg=CARD, fg=TXT, anchor="e",
                           font=("Consolas" if sys.platform.startswith("win") else "TkFixedFont",
                                 int(17 * max(1.0, self.scale)), "bold"))
            val.pack(fill="x", padx=9, pady=(0, 8))
            self.cards.append((val, unit))

    # ------------------------------------------------------------------
    def _build_notebook(self, parent: ttk.Frame) -> None:
        nb = ttk.Notebook(parent)
        nb.grid(row=1, column=0, sticky="nsew")
        self.nb = nb

        # ---- 波形 ----
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text="实时波形")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.wave = WaveCanvas(tab, window_sec=5.0)
        self.wave.set_font(self.font)
        self.wave.grid(row=0, column=0, sticky="nsew")

        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        # 第一行：通道开关
        r1 = tk.Frame(bar, bg=BG)
        r1.pack(fill="x")
        tk.Label(r1, text="显示通道", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left")
        self.chan_vars = []
        for i, name in enumerate(AXIS_NAMES):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(r1, text=name, variable=v,
                            command=self._on_channel_change).pack(side="left", padx=(6, 0))
            self.chan_vars.append(v)

        # 第二行：横轴（时间 / 点数）+ 跨度 + 刷新
        r2 = tk.Frame(bar, bg=BG)
        r2.pack(fill="x", pady=(4, 0))
        tk.Label(r2, text="X 轴", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left")
        self.xmode_var = tk.StringVar(value=X_MODE_LABELS[0])
        ttk.Combobox(r2, textvariable=self.xmode_var, width=8, state="readonly",
                     values=X_MODE_LABELS,
                     font=(self.font, 9)).pack(side="left", padx=(4, 0))
        self.xmode_var.trace_add("write", lambda *_: self._on_xmode_change())

        tk.Label(r2, text="跨度", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left", padx=(12, 0))
        self.win_var = tk.StringVar(value="5")
        self.span_box = ttk.Combobox(r2, textvariable=self.win_var, width=7,
                                     state="readonly", values=SPAN_SEC_CHOICES,
                                     font=(self.font, 9))
        self.span_box.pack(side="left", padx=(4, 0))
        self.win_var.trace_add("write", lambda *_: self._on_window_change())

        # 点数模式下顺带告诉操作者「这一屏大约是多少秒」—— 点数是为了对齐
        # 采样节拍，但人看波形还是习惯有个秒数做参照。
        self.span_hint = tk.Label(r2, text="", bg=BG, fg=TXT_DIM,
                                  font=(self.font, 8))
        self.span_hint.pack(side="left", padx=(6, 0))

        tk.Label(r2, text="刷新", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left", padx=(16, 0))
        self.refresh_var = tk.StringVar(value=REFRESH_DEFAULT)
        ttk.Combobox(r2, textvariable=self.refresh_var, width=5, state="readonly",
                     values=REFRESH_CHOICES,
                     font=(self.font, 9)).pack(side="left", padx=(4, 0))
        self.refresh_var.trace_add("write", lambda *_: self._on_refresh_change())

        # ---- 矩阵 ----
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="解耦矩阵")
        tab.columnconfigure(0, weight=1)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew")
        tk.Label(top, text="6 × 6 解耦矩阵（行 = 输出轴 FX…TZ，列 = 输入通道 1…6）",
                 bg=BG, fg=TXT_DIM, font=(self.font, 9)).pack(side="left")
        grid = ttk.Frame(tab)
        grid.grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.mat_entries: List[List[tk.Entry]] = []
        for c in range(6):
            tk.Label(grid, text=f"CH{c + 1}", bg=BG, fg=TXT_DIM,
                     font=(self.font, 8)).grid(row=0, column=c + 1, padx=3)
        for r in range(6):
            tk.Label(grid, text=AXIS_NAMES[r], bg=BG, fg=COLORS[r],
                     font=(self.font, 9, "bold")).grid(row=r + 1, column=0, padx=(0, 6))
            row_e = []
            for c in range(6):
                e = tk.Entry(grid, width=12, font=("Consolas", int(10 * max(1.0, self.scale))),
                             bg=CARD, fg=TXT, relief="solid", bd=1,
                             highlightthickness=0)
                e.grid(row=r + 1, column=c + 1, padx=2, pady=2)
                row_e.append(e)
            self.mat_entries.append(row_e)
            ttk.Button(grid, text="↑", width=3,
                       command=lambda r=r: self.on_upload_row(r)).grid(
                row=r + 1, column=7, padx=(8, 0))

        mb = ttk.Frame(tab)
        mb.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(mb, text="读取设备矩阵", command=self.on_read_matrix).pack(side="left")
        ttk.Button(mb, text="上传全部 6 行", command=self.on_upload_all).pack(
            side="left", padx=(6, 0))
        ttk.Button(mb, text="填入单位矩阵", command=lambda: self._fill_matrix("identity")).pack(
            side="left", padx=(6, 0))
        ttk.Button(mb, text="全部清零", command=lambda: self._fill_matrix("zero")).pack(
            side="left", padx=(6, 0))
        ttk.Button(mb, text="复制到剪贴板", command=self._copy_matrix).pack(
            side="left", padx=(6, 0))
        self.mat_hint = tk.Label(tab, text="「↑」= 只上传该行。上传会写进设备 Flash，"
                                           "掉电保持。",
                                 bg=BG, fg=TXT_DIM, font=(self.font, 8), anchor="w",
                                 justify="left")
        self.mat_hint.grid(row=3, column=0, sticky="w", pady=(6, 0))

        # ---- 报文日志 ----
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="报文日志")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tb = ttk.Frame(tab)
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.log_raw_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tb, text="记录报文", variable=self.log_raw_var,
                        command=self._on_toggle_raw).pack(side="left")
        self.log_stream_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tb, text="连推流帧也记（500Hz 会刷屏，慎用）",
                        variable=self.log_stream_var,
                        command=self._on_toggle_stream_log).pack(side="left", padx=(12, 0))
        ttk.Button(tb, text="清空", command=lambda: self._clear_text(self.raw_text)).pack(
            side="right")
        self.raw_frame = self._make_text(tab, 14)
        self.raw_text = self.raw_frame.text
        self.raw_frame.grid(row=1, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    def _build_bottom(self, root: tk.Tk) -> None:
        bar = tk.Frame(root, bg=CARD, highlightthickness=1, highlightbackground=EDGE)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bar.columnconfigure(1, weight=1)

        btnbox = tk.Frame(bar, bg=CARD)
        btnbox.grid(row=0, column=0, sticky="w", padx=10, pady=8)
        self.btn_save = ttk.Button(btnbox, text="开始保存数据", command=self.on_save_toggle)
        self.btn_save.pack(side="left")
        ttk.Button(btnbox, text="链路体检", command=self.on_diagnose).pack(
            side="left", padx=(6, 0))
        ttk.Button(btnbox, text="清空日志", command=self.on_clear_log).pack(
            side="left", padx=(6, 0))
        ttk.Button(btnbox, text="日志另存", command=self.on_save_log).pack(
            side="left", padx=(6, 0))

        self.status_label = tk.Label(bar, text="就绪", bg=CARD, fg=TXT_DIM,
                                     font=(self.font, 9), anchor="w")
        self.status_label.grid(row=0, column=1, sticky="ew", padx=10)

        self.log_frame = self._make_text(bar, 4)
        self.log_text = self.log_frame.text
        self.log_frame.grid(row=1, column=0, columnspan=2, sticky="ew",
                            padx=10, pady=(0, 8))
        for lvl, col in LEVEL_COLOR.items():
            self.log_text.tag_configure(lvl, foreground=col)

    # ------------------------------------------------------------------
    def _make_text(self, parent, height: int = 14) -> tk.Frame:
        """带滚动条的只读文本框，返回**外框**（`frame.text` 才是 Text）。"""
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        txt = tk.Text(frame, height=height, wrap="none", bg=CARD, fg=TXT,
                      relief="solid", bd=1, font=("Consolas", int(9 * max(1.0, self.scale))),
                      state="disabled")
        txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        txt.configure(yscrollcommand=sb.set)
        frame.text = txt                                 # type: ignore[attr-defined]
        return frame

    # ------------------------------------------------------------------
    def _bind_wheel(self, widget) -> None:
        """滚轮必须递归绑到左栏里每个子控件 —— Tk 的滚轮事件不会冒泡。"""
        def on_wheel(event):
            if self.left_sb.winfo_ismapped():
                step = int(24 * self.scale)          # 一格滚轮 ≈ 一行控件的高度
                self.left_canvas.yview_scroll(-step if event.delta > 0 else step,
                                              "units")
            return "break"

        def walk(w):
            w.bind("<MouseWheel>", on_wheel, add="+")
            for child in w.winfo_children():
                walk(child)

        walk(widget)

    def _sync_left_scrollregion(self, _event=None) -> None:
        """滚动域 = 内框自然尺寸；尺寸没变就什么都不做（移动事件也会进这里）。"""
        sr = (0, 0, self._left_inner.winfo_reqwidth(),
              self._left_inner.winfo_reqheight())
        if sr != self._left_sr:
            self._left_sr = sr
            self.left_canvas.configure(scrollregion=sr)

    def _update_left_scrollbar(self) -> None:
        """
        内容不超高就把滚动条收起来（不占位），超高才显示。
        注意：加/撤滚动条会改变 Canvas 宽度，内容换行后高度可能跟着变，
        所以最后必须再 update_idletasks 一次，winfo_ismapped() 才是真的。
        重入保护：改尺寸会再触发 <Configure>，不加锁会递归下去。
        """
        if getattr(self, "_in_layout", False):
            return
        self._in_layout = True
        try:
            self.root.update_idletasks()
            content_h = self._left_inner.winfo_reqheight()
            view_h = self.left_canvas.winfo_height()
            # 宽度取「内容自然宽度」，别硬塞一个固定值 —— 窄了会把按钮裁掉一半
            req_w = self._left_inner.winfo_reqwidth()
            self.left_canvas.configure(width=max(int(300 * self.scale),
                                                 min(req_w, int(372 * self.scale))))
            if content_h > view_h:
                self.left_sb.grid()
                self.left_canvas.configure(height=view_h)
            else:
                self.left_sb.grid_remove()
                self.left_canvas.configure(height=max(content_h, 1))
            self.root.update_idletasks()
        finally:
            self._in_layout = False

    # ==================================================================
    #  连接
    # ==================================================================
    def _refresh_ports(self) -> None:
        ports = list_ports()
        self._port_map = {}
        display = []
        for com, desc in ports:
            label = desc or com
            display.append(label)
            self._port_map[label] = com
        self.port_cb.configure(values=display)
        if display and not self.port_var.get():
            self.port_var.set(display[0])
        if not display:
            self.port_hint.configure(text="没找到串口：请插好 USB-RS485 转换器并装驱动")
        else:
            names = "、".join(c for c, _ in ports)
            self.port_hint.configure(text=f"发现 {len(ports)} 个：{names}")

    def _selected_port(self) -> str:
        label = self.port_var.get()
        return self._port_map.get(label, label)

    def _on_mode_change(self) -> None:
        serial = self.mode_var.get() == MODE_SERIAL
        if serial:
            self.serial_box.grid()
            self.tcp_box.grid_remove()
        else:
            self.serial_box.grid_remove()
            self.tcp_box.grid()
        self._update_left_scrollbar()

    def on_connect(self) -> None:
        if self.connected:
            return
        # 注意：Tk 的变量只能在主线程读，工作线程里碰 var.get() 会直接报
        # 「main thread is not in main loop」。所以先把设置读成普通字段再交给线程。
        try:
            settings = self._conn_settings()
        except Exception as exc:                              # noqa: BLE001
            self._log_ui("err", f"连接参数不合法：{exc}")
            return
        self._run_async(lambda: self._do_connect(settings), self._after_connect,
                        "正在连接", require_conn=False)

    def _conn_settings(self) -> dict:
        mode = self.mode_var.get()
        base = {"order": self.order_var.get(),
                "stream_every": 50 if self.log_stream_var.get() else 0,
                **self._bus_cfg()}
        if mode == MODE_SERIAL:
            port = self._selected_port()
            if not port:
                raise RuntimeError("没有选中串口，请点「刷新」后选择")
            base.update(mode=MODE_SERIAL, port=port,
                        baud=int(self.baud_var.get()), dtr=bool(self.dtr_var.get()))
        else:
            base.update(mode=MODE_TCP, host=self.host_var.get().strip(),
                        port=int(self.tcp_port_var.get()), baud=0, dtr=True)
        return base

    def _do_connect(self, cfg: dict) -> str:
        if cfg["mode"] == MODE_SERIAL:
            tr = SerialPort(cfg["port"], cfg["baud"], dtr_rts=cfg["dtr"])
        else:
            tr = TcpTransport(cfg["host"], cfg["port"], timeout=3.0)
        cli = Y75Client(tr, byte_order=cfg["order"])
        cli.log_stream_every = cfg["stream_every"]
        apply_bus_cfg(cli, cfg)
        cli.on_log = lambda lvl, msg: self.ui_queue.put(("log", lvl, msg, ""))
        cli.on_state = lambda st, d: self.ui_queue.put(("state", st, d, ""))
        cli.on_push = self._on_push_thread
        cli.connect()
        self.cli = cli
        return tr.name

    def _after_connect(self, name: str) -> None:
        self.connected = True
        self.tx_warned = False
        self.conn_label.configure(text=f"已连接  {name}", fg="#2f9e5f")
        self.btn_connect.state(["disabled"])
        self.btn_disconnect.state(["!disabled"])
        self._set_status(f"已连接 {name}")
        self._read_device_info()
        self._update_bus_stat()
        # --autoconnect：连上后自动进入连续采集（演示 / 自检用）
        if getattr(self.args, "autoconnect", False) and not self._auto_started:
            self._auto_started = True
            self.root.after(300, self.on_start)

    def on_disconnect(self) -> None:
        if self.saving:
            self._stop_saving()
        self._run_async(self._do_disconnect, self._after_disconnect,
                        "正在断开", require_conn=False)

    def _do_disconnect(self) -> None:
        cli, self.cli = self.cli, None
        if cli is not None:
            try:
                cli.disconnect()
            except Exception:
                pass

    def _after_disconnect(self, _res=None) -> None:
        self.connected = False
        self.conn_label.configure(text="未连接", fg=TXT_DIM)
        self.btn_connect.state(["!disabled"])
        self.btn_disconnect.state(["disabled"])
        for lab in self.info_labels.values():
            lab.configure(text="—")
        for v, _u in self.cards:
            v.configure(text="—")
        self.wave.reset()                  # 时间轴与量程一并清掉
        self._set_status("已断开")
        self._update_bus_stat()

    # ==================================================================
    #  异步执行
    # ==================================================================
    def _run_async(self, fn: Callable, on_ok: Optional[Callable] = None,
                   desc: str = "", require_conn: bool = True) -> bool:
        if self.busy:
            self._log_ui("warn", f"上一条命令还没执行完（{desc} 被忽略）")
            return False
        cli = self.cli
        if require_conn and (cli is None or not cli.is_connected()):
            self._log_ui("warn", "设备未连接")
            return False
        self.busy = True
        if desc:
            self._set_status(f"{desc} …")

        def work() -> None:
            try:
                res = fn()
                self.ui_queue.put(("async", on_ok, res, None))
            except Exception as exc:                          # noqa: BLE001
                self.ui_queue.put(("async", None, None, exc))

        threading.Thread(target=work, name="y75-cmd", daemon=True).start()
        return True

    def _drain_queue(self) -> None:
        logs = 0
        skipped = 0
        while True:
            try:
                kind, a, b, c = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                if logs < self.LOG_PER_TICK:
                    logs += 1
                    self._log_ui(a, b)
                else:
                    skipped += 1        # 只丢显示，继续把队列抽干
            elif kind == "state":
                self._on_state(a, b)
            elif kind == "async":
                self.busy = False
                if c is not None:
                    self._on_async_error(c)
                elif callable(a):
                    try:
                        a(b)
                    except Exception as exc:                  # noqa: BLE001
                        self._on_async_error(exc)
                if not self.busy:
                    self._set_status("就绪")

        if skipped:
            # 提示本身也降频到每秒一条，别让它自己变成新的洪水源
            self._skipped_logs += skipped
            now = time.monotonic()
            if now - self._skip_note_at >= 1.0:
                self._skip_note_at = now
                n, self._skipped_logs = self._skipped_logs, 0
                self._log_ui("warn", f"… 另有 {n} 条日志因刷新太快被合并"
                                     f"（勾选「记录报文」可看原始帧）")

    def _on_async_error(self, exc: Exception) -> None:
        if isinstance(exc, Y75Timeout):
            self._log_ui("err", f"超时：{exc}")
            self._warn_timeout_once()
        elif isinstance(exc, (Y75Error, SerialError)):
            self._log_ui("err", str(exc))
        else:
            self._log_ui("err", f"{type(exc).__name__}: {exc}")

    def _warn_timeout_once(self) -> None:
        if self.tx_warned:
            return
        self.tx_warned = True
        self._log_ui("warn", "提示：一直超时请依次检查 —— ① 串口号选对没 "
                             "② 波特率是否是设备当前档位（默认 460800）"
                             "③ A/B 是否接反 ④ 设备是否在跑本协议固件")

    def _on_state(self, state: str, detail: str) -> None:
        if state == "error":
            self._log_ui("err", f"链路异常：{detail}")
            self._after_disconnect()
        elif state in ("closed", "disconnected"):
            if self.connected:
                self._after_disconnect()

    # ==================================================================
    #  命令按钮
    # ==================================================================
    def on_read_info(self) -> None:
        self._run_async(self._read_device_info_sync, self._apply_info, "读取设备信息")

    def _read_device_info(self) -> None:
        self._run_async(self._read_device_info_sync, self._apply_info, "读取设备信息")

    def _read_device_info_sync(self) -> dict:
        cli = self.cli
        assert cli is not None
        out = {}
        try:
            out["sn"] = cli.query_sn()
        except Exception as exc:                              # noqa: BLE001
            out["sn"] = f"读取失败（{exc}）"
        try:
            out["ver"] = cli.query_ver()
        except Exception as exc:                              # noqa: BLE001
            out["ver"] = f"读取失败（{exc}）"
        try:
            st = cli.query_status()
            out["status"] = st
            # 顺手同步「标称采样周期」：波形时间戳按它等间隔推算
            cli.note_freq_hz(int(st.get("freq_hz") or 500))
        except Exception as exc:                              # noqa: BLE001
            out["status"] = None
            out["status_err"] = str(exc)
        try:
            out["unit"] = cli.query_unit()
        except Exception as exc:                              # noqa: BLE001
            out["unit"] = None
        return out

    def _apply_info(self, info: dict) -> None:
        self.info_labels["sn"].configure(text=info.get("sn") or "—")
        self.info_labels["ver"].configure(text=info.get("ver") or "—")
        st = info.get("status")
        if st:
            # 轮询模式下设备报告的是「单次」(send_mode=2)，但主机侧语义是
            # 「正在连续取数」——host 状态以轮询线程为准，别被设备状态盖掉。
            polling = bool(self.cli and self.cli.is_polling())
            self.send_mode = 1 if polling else int(st["send_mode"])
            self.data_format = int(st["data_format"])
            mode_txt = "连续采集（轮询）" if polling else st["send_mode_text"]
            self.info_labels["state"].configure(
                text=f"{mode_txt} · {st['data_format_text']} · "
                     f"{st['freq_hz']}Hz · {st['debug_mode_text']}")
            self.info_labels["err"].configure(text=f"{st['comm_error']}")
            self.freq_var.set(str(st["freq_hz"]))
            for name, code in FMT_CHOICES:
                if code == self.data_format:
                    self.fmt_var.set(name)
            self.out_var.set(OUT_ASC if st["debug_mode"] == 1 else OUT_HEX)
        else:
            self.info_labels["state"].configure(
                text=f"读取失败（{info.get('status_err', '')}）")
            self.info_labels["err"].configure(text="—")
        # 0x0E 是单位的最直接口径（最低位 1=kg 0=N）：读到了就用它覆盖。
        # mV 只有 0x07 能表达（0x0E 在 mV 模式也回 N），所以 0x07 说 mV 时
        # 保留 mV，不被 0x0E 顶掉。
        unit_q = info.get("unit")
        if unit_q in ("kg", "N") and self.data_format != 0:
            self.data_format = 1 if unit_q == "kg" else 2
            for name, code in FMT_CHOICES:
                if code == self.data_format:
                    self.fmt_var.set(name)
        self._set_units(FORMAT_UNIT.get(self.data_format, ""))

    def _set_units(self, unit: str) -> None:
        units = axis_units(unit or "N")
        for (_v, u), txt in zip(self.cards, units):
            u.configure(text=txt)
        self.wave.unit = unit or "N"

    def on_start(self) -> None:
        def done(_res):
            self.send_mode = 1
            self._log_ui("info", "已开始连续采集（每 50 ms 发一条 0x03 轮询，"
                                 "点「停止」即停发）")
        self._run_async(lambda: self.cli.start(), done, "开始连续采集")

    def on_stop(self) -> None:
        # 设备正在推流时，停止命令不能立刻拍下去 —— 必须等当前这一帧在线上
        # 走完（MAX13487 自动方向，主机没法让总线，帧中间插话会撞车）。
        # 真正的等待在协议层 BusGate 里做，这里只负责把状态说清楚。
        cli = self.cli
        streaming = cli is not None and cli.bus.enabled and self.send_mode == 1
        if streaming:
            self._log_ui("info", "等当前这一帧收完再发停止命令（避开 485 正在发送的时段）…")

        def done(_res):
            self.send_mode = 0
            self._log_ui("info", "已停止连续采集")
        self._run_async(lambda: self.cli.stop(), done,
                        "等帧间空隙后停止" if streaming else "停止连续采集")

    def on_single(self) -> None:
        self._run_async(self._do_single, self._apply_single, "单次采集")

    def _do_single(self):
        cli = self.cli
        assert cli is not None
        return cli.single()

    def _apply_single(self, vals) -> None:
        self._show_values(vals)
        self._log_ui("info", "单次采集：" + ", ".join(fmt_value(v) for v in vals))

    def on_set_freq(self) -> None:
        hz = int(self.freq_var.get())
        self._run_async(lambda: self.cli.set_freq(hz),
                        lambda _r: self._log_ui("info", f"输出频率已设为 {hz} Hz"),
                        f"设置输出频率 {hz}Hz")

    def on_apply_output(self) -> None:
        fmt = dict(FMT_CHOICES)[self.fmt_var.get()]
        asc = self.out_var.get() == OUT_ASC
        order = self.order_var.get()
        if self.cli is not None:
            self.cli.byte_order = order

        def work():
            cli = self.cli
            assert cli is not None
            cli.byte_order = order
            cli.set_format(fmt)
            if asc:
                cli.set_asc_mode()
            else:
                cli.set_hex_mode()
            return cli.query_status()

        def done(st):
            # 轮询进行中时设备状态是「单次」，别用它覆盖主机的连续采集标记
            if not (self.cli and self.cli.is_polling()):
                self.send_mode = int(st["send_mode"])
            self.data_format = int(st["data_format"])
            self._set_units(FORMAT_UNIT.get(self.data_format, ""))
            self._log_ui("info", f"输出设置已生效：{st['data_format_text']} · "
                                 f"{st['debug_mode_text']} · 字节序 {order}")

        self._run_async(work, done, "应用输出设置")

    def on_set_baud(self) -> None:
        baud = int(self.dev_baud_var.get())
        if baud == int(self.baud_var.get()):
            self._log_ui("warn", "设备波特率与当前串口波特率相同，无需切换")
            return
        if self.mode_var.get() != MODE_SERIAL:
            self._log_ui("warn", "网络模拟模式下不支持改波特率")
            return
        if not messagebox.askyesno(
                "确认改波特率",
                f"将把设备波特率改成 {baud} bps。\n\n"
                "设备会立刻换档；如果上位机没同步切过去，之后所有命令都会超时。\n"
                "（设备把新波特率写进了 EEPROM，掉电也不会恢复默认。）\n\n"
                "要继续吗？"):
            return
        self._run_async(lambda: self.cli.set_baud(baud), lambda _r: self._after_set_baud(baud),
                        f"下发波特率 {baud}")

    def _after_set_baud(self, baud: int) -> None:
        """设备已按旧波特率回过 ACK，现在把本机串口切到新档位再重连。"""
        self.baud_var.set(str(baud))
        self.dev_baud_var.set(str(baud))
        self._log_ui("info", f"设备已切到 {baud} bps，正在把本机串口同步切过去…")
        self._do_disconnect()
        self._after_disconnect()
        self.root.after(200, self.on_connect)

    def on_zero(self) -> None:
        self._run_async(lambda: self.cli.zero(),
                        lambda _r: self._log_ui("info", "已下发置零，设备正在采 20 点求平均"),
                        "执行置零")

    def on_unzero(self) -> None:
        self._run_async(lambda: self.cli.unzero(),
                        lambda _r: self._log_ui("info", "已取消置零"),
                        "取消置零")

    # ---- 矩阵 ----
    def on_read_matrix(self) -> None:
        # 0x38 二进制逐行读（行号 1..6）：比 0x37 的裸 ASCII 抗总线噪声，
        # 每行都有行号回显，错不了位。
        def work():
            return [self.cli.query_matrix_row(r + 1) for r in range(6)]
        self._run_async(work, self._apply_matrix, "读取解耦矩阵")

    def _apply_matrix(self, mat) -> None:
        for r in range(6):
            for c in range(6):
                e = self.mat_entries[r][c]
                e.delete(0, "end")
                e.insert(0, f"{mat[r][c]:.6f}")
        self._log_ui("info", "已读取设备矩阵：" +
                     " | ".join(f"{mat[i][0]:.4f}…" for i in range(6)))

    def _read_matrix_entries(self) -> List[List[float]]:
        out = []
        for r in range(6):
            row = []
            for c in range(6):
                txt = self.mat_entries[r][c].get().strip()
                if not txt:
                    raise ValueError(f"第 {r + 1} 行第 {c + 1} 列为空")
                try:
                    row.append(float(txt))
                except ValueError:
                    raise ValueError(f"第 {r + 1} 行第 {c + 1} 列不是数字：{txt!r}")
            out.append(row)
        return out

    def on_upload_row(self, row: int) -> None:
        try:
            vals = [float(self.mat_entries[row][c].get().strip() or "0") for c in range(6)]
        except ValueError as exc:
            messagebox.showerror("数值不合法", str(exc))
            return
        self._run_async(lambda: self.cli.set_matrix_row(row, vals),
                        lambda _r: self._log_ui("info", f"第 {row + 1} 行已上传"),
                        f"上传矩阵第 {row + 1} 行")

    def on_upload_all(self) -> None:
        try:
            mat = self._read_matrix_entries()
        except ValueError as exc:
            messagebox.showerror("数值不合法", str(exc))
            return

        def work():
            cli = self.cli
            assert cli is not None
            for r in range(6):
                cli.set_matrix_row(r, mat[r])      # 必须在同一个任务里连做，别拆成 6 次
            return True

        self._run_async(work, lambda _r: self._log_ui("info", "已上传全部 6 行矩阵"),
                        "上传全部矩阵")

    def _fill_matrix(self, kind: str) -> None:
        for r in range(6):
            for c in range(6):
                v = 0.0
                if kind == "identity" and r == c:
                    v = 1.0
                elif kind == "identity":
                    v = 0.0
                e = self.mat_entries[r][c]
                e.delete(0, "end")
                e.insert(0, f"{v:.6f}")

    def _copy_matrix(self) -> None:
        try:
            mat = self._read_matrix_entries()
        except ValueError as exc:
            messagebox.showerror("数值不合法", str(exc))
            return
        text = "\n".join("\t".join(f"{v:.6f}" for v in row) for row in mat)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log_ui("info", "矩阵已复制到剪贴板（制表符分隔，可直接粘进 Excel）")

    # ==================================================================
    #  数据保存
    # ==================================================================
    def on_save_toggle(self) -> None:
        if self.saving:
            self._stop_saving()
            return
        if not self.connected:
            self._log_ui("warn", "先连接设备再保存数据")
            return
        default = os.path.join(self.save_dir,
                               time.strftime("Y75_force_%Y%m%d_%H%M%S.xlsx"))
        path = filedialog.asksaveasfilename(
            title="保存六维力数据", defaultextension=".xlsx",
            initialdir=self.save_dir, initialfile=os.path.basename(default),
            filetypes=[("Excel 工作簿", "*.xlsx"), ("所有文件", "*.*")])
        if not path:
            return
        self._start_saving(path)

    def _start_saving(self, path: str) -> None:
        if self.saving:
            return
        try:
            self.writer = XlsxTableWriter(path, headers=SAVE_HEADERS,
                                          sheet_name="六维力数据")
        except OSError as exc:
            messagebox.showerror("无法写入", f"打开文件失败：{exc}\n"
                                             f"请换一个目录，或确认没有同名的 Excel 正开着。")
            return
        self.save_path = path
        self.save_batches = 0
        with self.save_lock:
            self.save_pending.clear()
        self.saving = True
        self.btn_save.configure(text="停止保存数据")
        self._log_ui("info", f"开始保存数据 → {path}")

        # 设备没在推流的话，一帧都存不到；顺手把连续采集打开
        if self.send_mode != 1:
            def work():
                cli = self.cli
                assert cli is not None
                cli.start()
                return True
            self._run_async(work, lambda _r: setattr(self, "send_mode", 1),
                            "开始连续采集以便保存数据")

    def _stop_saving(self) -> None:
        if not self.saving:
            return
        self.saving = False
        self.btn_save.configure(text="开始保存数据")
        self._drain_save_buffer()               # 先把队列里剩的写完
        w, self.writer = self.writer, None
        if w is None:
            return
        try:
            rows = w.close()
        except Exception as exc:                              # noqa: BLE001
            self._log_ui("err", f"保存失败：{exc}")
            return
        msg = f"已保存 {rows} 行 → {self.save_path}"
        if w.skipped_rows or w.blank_cells:
            msg += f"（跳过整行无效 {w.skipped_rows} 行，留空 {w.blank_cells} 格）"
        self._log_ui("info", msg)
        self._set_status(f"数据已保存：{os.path.basename(self.save_path)}")

    def _on_push_thread(self, vals, ts: float) -> None:
        """推流回调 —— 在**接收线程**里跑，只允许入队，绝不能写盘。"""
        if not self.saving:
            return
        with self.save_lock:
            self.save_pending.append((ts, vals))

    def _drain_save_buffer(self) -> None:
        """落盘在界面线程做，且要在「是否连接」判断之外 —— 断线时也要收尾。"""
        with self.save_lock:
            if not self.save_pending:
                return
            batch = self.save_pending
            self.save_pending = []
        w = self.writer
        if w is None:
            return
        try:
            for _ts, vals in batch:
                w.add_row(list(vals))
            self.save_batches += len(batch)
        except XlsxLimitError as exc:
            self._log_ui("err", str(exc))
            self._stop_saving()
        except Exception as exc:                              # noqa: BLE001
            self._log_ui("err", f"写 Excel 失败：{exc}")
            self._stop_saving()

    # ==================================================================
    #  界面刷新
    # ==================================================================
    def _tick(self) -> None:
        t_start = time.perf_counter()
        try:
            self._drain_queue()
            self._drain_save_buffer()          # 必须在 connected 判断之外
            if self.saving and self.writer is not None:
                extra = (f" · 已存 {self.writer.rows} 行"
                         if self.writer else "")
                self.btn_save.configure(text=f"停止保存数据{extra}")
            if self.connected and self.cli is not None:
                self._update_wave()            # 波形每个 tick 都刷
                self._tick_n += 1
                if self._tick_n % self.TEXT_EVERY == 0:
                    self._update_values()      # 数字降频，免得闪到看不清
                    self._update_stats()
        finally:
            # 固定周期重排。试过两种花活，实测都更差：
            #   * 「按绝对时刻补帧」：系统整体拖后几毫秒时立刻补一帧，
            #     帧间隔在 15~42ms 之间乱跳（变异系数 35%）；
            #   * 「按本帧耗时扣减」：等待时间短于 17ms 时 Tk 的定时器
            #     反而会多等一个系统 tick，周期照样漂。
            # 老老实实固定间隔，帧间隔抖动最小（实测 11%），看着反而最稳。
            self.root.after(self.tick_ms, self._tick)

    def _update_values(self) -> None:
        cli = self.cli
        if cli is None:
            return
        self._show_values(cli.latest)

    def _show_values(self, vals) -> None:
        if vals is None:
            return
        for i, (lab, _u) in enumerate(self.cards):
            lab.configure(text=fmt_value(vals[i] if i < len(vals) else None))

    def _update_wave(self) -> None:
        cli = self.cli
        if cli is None:
            return
        w = self.wave
        # 两种模式都**按个数**取（见 WaveCanvas.want_points 的说明）：按时间戳
        # 取的话，真机设备时钟一重锚，切片长度就变，横轴位置跟着突跳。
        count, samples = cli.take_wave(max_points=w.want_points())
        if not samples:
            # 这一 tick 没有新帧（USB 驱动在攒批，或设备短暂停顿）。
            # **千万不要就此不重绘**：时间轴是按本地时钟匀速推进的，
            # 一旦把「重绘」和「有没有新数据」耦合起来，串口路径就退化成
            # 「来一批跳一下、不来就冻住」—— 而 TCP 回环因为每 tick 都有新帧，
            # 恰好躲开了这个耦合，所以看着丝滑。这正是「网络模拟顺、串口卡」
            # 的根源。这里改成：没新数据也照原样重绘一次，让时间轴继续走。
            samples = self.wave._last_samples
            if not samples:
                return
        self.wave.draw(samples, count=count)

    def _update_stats(self) -> None:
        cli = self.cli
        if cli is None:
            return
        now = time.time()
        self.rate_hist.append((now, cli.push_count))
        while len(self.rate_hist) > 2 and now - self.rate_hist[0][0] > 1.2:
            self.rate_hist.pop(0)
        if len(self.rate_hist) > 1:
            t0, n0 = self.rate_hist[0]
            if now - t0 > 0.2:
                self.frame_rate = (cli.push_count - n0) / (now - t0)
        self.stat_labels["rate"].configure(
            text=f"{self.frame_rate:.0f} Hz" if self.frame_rate > 0.5 else "—")
        self.stat_labels["frames"].configure(text=str(cli.push_count))
        self.stat_labels["stream"].configure(text=str(cli.stream_count))
        self.stat_labels["resync"].configure(text=str(cli.frame_errors))
        self.stat_labels["garbage"].configure(text=str(cli.garbage_bytes))
        # 空载荷畸形帧（AA 55 [cmd] 0D 0A）：为 0 说明总线干净；
        # 持续增长说明设备/线缆在发短帧，或驱动把帧截断了。
        nshort = cli.short_frames
        self.stat_labels["short"].configure(
            text=str(nshort), fg=(LEVEL_COLOR["warn"] if nshort else TXT))
        # 波形刷新质量：实测 fps + 帧间隔抖动。卡不卡不用吵，和 Modbus 版
        # 摆一起对数字就行（那一版实际只有 ~21fps / 1.2ms 抖动）。
        w = self.wave
        self._update_span_hint()         # 点数模式下「≈ 多少秒」随实测节拍更新
        if w.fps > 0.5:
            self.stat_labels["wave"].configure(
                text=f"{w.fps:.0f} fps ±{w.jitter_ms:.1f} ms", fg=TXT)
        else:
            self.stat_labels["wave"].configure(text="—", fg=TXT_DIM)
        # 显示延迟：自己会随链路抖动自适应。稳定在 50ms（下限）左右说明链路
        # 干净；跟着数据成批节奏涨到 100ms+ 说明驱动/固件在攒批，是「卡」的
        # 源头，该去调 USB 转串口的 Latency Timer 了。
        if w.latency <= 0.0:
            self.stat_labels["latency"].configure(text="—", fg=TXT_DIM)
        else:
            busier = w.latency > w.LATENCY_MIN + 0.05
            self.stat_labels["latency"].configure(
                text=f"{w.latency * 1000:.0f} ms",
                fg=(LEVEL_COLOR["warn"] if busier else TXT))
        # 接收批间隔：一次读到多少、隔多久读到一次。这是「串口比网络卡」的
        # 物理根源所在 —— 间隔 8ms 以上、每批好几帧，就说明 USB 转串口驱动
        # 在按 Latency Timer 攒批，去设备管理器把它从 16ms 改成 1ms 立竿见影。
        if cli.rx_batches < 5 or cli.batch_iv <= 0.0:
            self.stat_labels["batch"].configure(text="—", fg=TXT_DIM)
        else:
            ms = cli.batch_iv * 1000.0
            self.stat_labels["batch"].configure(
                text=f"{ms:.1f} ms / {cli.batch_frames:.0f}帧",
                fg=(LEVEL_COLOR["warn"] if ms >= 8.0 else TXT))
        self._update_bus_stat()

    def on_diagnose(self) -> None:
        """一键链路体检：把定位「卡」所需的数字一次性打进日志。

        判读要点（结论也会打印出来）：
          · 帧率远低于 330Hz        → 数据端本身就慢：查波特率 / 固件发送节拍
          · 接收批间隔 ≥8ms、每批多帧 → USB 转串口驱动在按 Latency Timer 攒批，
                                     设备管理器里把它从 16ms 改成 1ms
          · 显示延迟远高于 50ms      → 数据到达太成批（上一行的后果）
          · 波形刷新低于设定值       → 瓶颈在本机显示端
        """
        cli = self.cli
        if cli is None or not self.connected:
            self._log_ui("warn", "还没连上设备 —— 先「连接」并跑几秒数据再体检。")
            return
        w = self.wave
        fps_set = self.refresh_var.get()
        iv_ms = cli.batch_iv * 1000.0
        dt_ms = cli.sample_dt * 1000.0
        baud = getattr(cli.transport, "baud", None)
        self._log_ui("info", "———— 链路体检 ————")
        self._log_ui("info", f"接收帧率 {self.frame_rate:.1f} Hz"
                             f"（累计 {cli.push_count} 帧，连续帧 {cli.stream_count}）"
                             f"｜设备实测节拍 {dt_ms:.2f} ms = "
                             f"{1000.0 / dt_ms if dt_ms > 0 else 0:.0f} Hz")
        if cli.rx_batches >= 5 and iv_ms > 0.0:
            hint = ("← 驱动在攒批，去设备管理器把 USB 转串口的高级选项里 "
                    "Latency Timer 改成 1ms" if iv_ms >= 8.0
                    else "← 粒度正常，基本是一帧一读")
            self._log_ui("info", f"接收批间隔 {iv_ms:.1f} ms / 每批 "
                                 f"{cli.batch_frames:.1f} 帧   {hint}")
        else:
            self._log_ui("info", "接收批间隔 —— 数据太少，多跑几秒再体检")
        self._log_ui("info", f"波形刷新 {w.fps:.0f} fps ±{w.jitter_ms:.1f} ms"
                             f"（设定 {fps_set} fps）｜显示延迟 "
                             f"{w.latency * 1000:.0f} ms｜兜底夹子 "
                             f"{w.clamp_n}/{w.draw_n} 帧")
        self._log_ui("info", f"异常帧：拆帧重同步 {cli.frame_errors}"
                             f"，丢弃乱码 {cli.garbage_bytes} 字节"
                             f"，空载荷畸形帧 {cli.short_frames}"
                             f"｜波特率 {baud if baud else '—'}")
        self._log_ui("info", cli.bus.summary())

        # ---- 结论 ----
        bad = []
        if 0.0 < self.frame_rate < 150.0:
            bad.append(f"帧率仅 {self.frame_rate:.0f}Hz，数据端本身就慢："
                       f"查波特率（{baud if baud else '—'}）是否够、"
                       f"以及固件 rs485_send_continuous 的主循环节拍")
        if cli.rx_batches >= 5 and iv_ms >= 8.0:
            bad.append(f"驱动攒批 {iv_ms:.0f}ms/批（每批 {cli.batch_frames:.0f} 帧）："
                       f"把 USB 转串口的 Latency Timer 调到 1ms，"
                       f"这一项对「串口比网络卡」最立竿见影")
        if w.draw_n and w.clamp_n / max(1, w.draw_n) > 0.05:
            bad.append(f"时间轴兜底夹子触发 "
                       f"{100.0 * w.clamp_n / max(1, w.draw_n):.0f}%："
                       f"数据到达严重不均，先解决上面两条")
        if w.fps > 0.5 and fps_set.isdigit() and w.fps < int(fps_set) * 0.7:
            bad.append(f"波形刷新只有 {w.fps:.0f}fps（设定 {fps_set}）："
                       f"瓶颈在本机 CPU / 显示端")
        if cli.bus.enabled and (cli.bus.timeouts or cli.bus.blind > 3):
            bad.append(f"总线仲裁有 {cli.bus.blind} 次看不到帧边界、"
                       f"{cli.bus.timeouts} 次硬超时：串口驱动多半在攒批"
                       f"（帧被一次性塞过来，看不到「一帧收完」的瞬间），"
                       f"把 USB 转串口的 Latency Timer 调到 1ms；"
                       f"或者设备降频到 500Hz 让帧间空隙变大")
        if bad:
            for i, msg in enumerate(bad, 1):
                self._log_ui("warn", f"体检结论 {i}/{len(bad)}：{msg}")
        else:
            self._log_ui("info", "体检结论：链路与显示都正常 —— 如果还觉得卡，"
                                 "把上面这几行发我。")

    def _on_channel_change(self) -> None:
        for i, v in enumerate(self.chan_vars):
            self.wave.enabled[i] = bool(v.get())

    def _on_xmode_change(self) -> None:
        """
        切换横轴（时间 ↔ 点数）。

        跨度下拉框的候选值和当前值要一起换：时间模式是 1/2/5/10/20 秒，
        点数模式是 1000~20000 点。不换候选值的话，用户会在点数模式下
        看到「5 点」这种荒唐的档位。
        """
        pts = self.xmode_var.get() == X_MODE_PTS_LABEL
        self.wave.set_x_mode(WaveCanvas.X_POINTS if pts else WaveCanvas.X_TIME)
        if pts:
            self.span_box.configure(values=SPAN_PTS_CHOICES)
            self.win_var.set(str(self.wave.window_pts))
            self._log_ui("info", f"横轴改为「点数」：显示最近 {self.wave.window_pts} 个"
                                 f"采样点（相邻点等间距）")
        else:
            self.span_box.configure(values=SPAN_SEC_CHOICES)
            self.win_var.set(f"{self.wave.window_sec:g}")
            self._log_ui("info", f"横轴改为「时间」：显示最近 {self.wave.window_sec:g} 秒")
        self._update_span_hint()

    def _on_window_change(self) -> None:
        """跨度改变：按当前横轴模式解释输入（秒 or 点）。"""
        txt = self.win_var.get().strip()
        if self.wave.x_mode == WaveCanvas.X_POINTS:
            try:
                self.wave.set_window_pts(int(float(txt)))
            except (TypeError, ValueError):
                pass
            self._update_span_hint()
        else:
            try:
                self.wave.set_window_sec(float(txt))
            except (TypeError, ValueError):
                pass

    def _update_span_hint(self) -> None:
        """点数模式下显示「这一屏约等于多少秒」，用的是实测采样间隔。"""
        if self.wave.x_mode != WaveCanvas.X_POINTS:
            if self.span_hint.cget("text"):
                self.span_hint.configure(text="")
            return
        dt = self.wave.dt_est
        if dt <= 0.0:
            text = "≈ 采样率未知"
        else:
            text = f"≈ {self.wave.window_pts * dt:.1f} s @ {1.0 / dt:.0f} Hz"
        if text != self.span_hint.cget("text"):
            self.span_hint.configure(text=text)

    def _on_refresh_change(self) -> None:
        """改波形刷新率。下一次 tick 生效，不用重启。"""
        self.tick_ms = refresh_to_ms(self.refresh_var.get())
        self._log_ui("info", f"波形刷新 {self.refresh_var.get()} fps"
                             f"（每 {self.tick_ms}ms 一帧）")

    # ------------------------------------------------------------------
    #  总线仲裁
    # ------------------------------------------------------------------
    def _bus_cfg(self) -> dict:
        """从界面变量读出一份纯数值配置（只能在主线程调用）。"""
        def num(var, default: float, lo: float, hi: float) -> float:
            try:
                return min(hi, max(lo, float(str(var.get()).strip() or default)))
            except (TypeError, ValueError):
                return default

        return {
            "bus_wait": bool(self.bus_wait_var.get()),
            "bus_window": num(self.bus_window_var, 0.0, 0.0, 20.0),
            "bus_maxwait": num(self.bus_maxwait_var, 250.0, 0.0, 2000.0),
            "bus_retry": int(num(self.bus_retry_var, 0, 0, 3)),
        }

    def _apply_bus_cfg(self) -> None:
        """改完立刻作用于当前连接，不用重连。"""
        cfg = self._bus_cfg()
        cli = self.cli
        if cli is not None:
            apply_bus_cfg(cli, cfg)
        if not cfg["bus_wait"]:
            self._log_ui("warn", "总线仲裁已关闭：命令可能落在设备正在发的那一帧里，"
                                 "485 上会撞车")
        else:
            self._log_ui("info", f"总线仲裁：收到一帧后 "
                                 f"{cfg['bus_window'] or '自动'} ms 内发"
                                 f"（0 = 按实测帧周期自动），最多等 "
                                 f"{cfg['bus_maxwait']:.0f} ms，"
                                 f"无应答重发 {cfg['bus_retry']} 次")
        self._update_bus_stat()

    def _update_bus_stat(self) -> None:
        lab = getattr(self, "bus_stat_label", None)
        if lab is None:
            return
        cli = self.cli
        if cli is None or not self.connected:
            lab.configure(text="未连接", fg=TXT_DIM)
            return
        bus = cli.bus
        if not bus.enabled:
            lab.configure(text="关闭中：收到就发（有撞车风险）",
                          fg=LEVEL_COLOR["warn"])
            return
        txt = (f"帧后 {bus.window_s() * 1000:.2f} ms 内发 ｜ 等过 "
               f"{bus.gate_count} 次，均 {bus.avg_wait_ms():.2f} / "
               f"最长 {bus.wait_ms_max:.2f} ms")
        if bus.blind or bus.timeouts:
            txt += (f"\n⚠ {bus.blind} 次看不到帧边界、{bus.timeouts} 次硬超时"
                    f"（串口驱动可能在攒批）")
        lab.configure(text=txt,
                      fg=(LEVEL_COLOR["warn"] if (bus.blind or bus.timeouts)
                          else TXT_DIM))

    def _update_order_hint(self) -> None:
        # 这个下拉框只作用于**数据帧**（0x02/0x03 的 6×float32）。
        # 矩阵行（0x20/0x38）在协议层固定大端，不受这里影响，写清楚免得误会。
        self.order_hint.configure(
            text=f"{ByteOrder.HINT.get(self.order_var.get(), '')}"
                 "（数据帧用；矩阵行固定大端 3F 80 00 00）")

    def _on_toggle_raw(self) -> None:
        if self.cli is not None:
            self.cli.log_raw = bool(self.log_raw_var.get())
        self._log_ui("info", "报文日志已" + ("打开" if self.log_raw_var.get() else "关闭"))

    def _on_toggle_stream_log(self) -> None:
        """开启后每 N 帧记一条推流帧 —— 只用于短时间抓包，避免刷屏。"""
        every = 50 if self.log_stream_var.get() else 0
        if self.cli is not None:
            self.cli.log_stream_every = every
        self._log_ui("info", "推流帧记录：" + (f"每 {every} 帧记一条"
                                            if every else "已关闭"))

    # ==================================================================
    #  日志
    # ==================================================================
    def _log_ui(self, level: str, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}\n"
        self._append_text(self.log_text, line, level)

        # 报文日志：只记命令/ACK 这类结构化的行
        if self.log_raw_var.get():
            if level in ("tx", "rx") or (level == "warn" and "帧" in msg):
                self._append_text(self.raw_text, f"[{stamp}] {msg}\n", level)

    def _append_text(self, widget: tk.Text, text: str, tag: str = "") -> None:
        widget.configure(state="normal")
        widget.insert("end", text, tag)
        # 超长时裁掉前面的行，避免越跑越卡
        limit = 400 if widget is self.raw_text else 800
        if int(widget.index("end-1c").split(".")[0]) > limit:
            widget.delete("1.0", f"{int(widget.index('end-1c').split('.')[0]) - limit}.0")
        widget.see("end")
        widget.configure(state="disabled")

    def _clear_text(self, widget: tk.Text) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def on_clear_log(self) -> None:
        self._clear_text(self.log_text)
        self._clear_text(self.raw_text)

    def on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="日志另存为", defaultextension=".txt",
            initialdir=self.save_dir,
            initialfile=time.strftime("Y75_log_%Y%m%d_%H%M%S.txt"),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("===== 运行日志 =====\n")
                fh.write(self.log_text.get("1.0", "end"))
                fh.write("\n===== 报文日志 =====\n")
                fh.write(self.raw_text.get("1.0", "end"))
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._log_ui("info", f"日志已另存 → {path}")

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    # ==================================================================
    def on_close(self) -> None:
        self.saving = False
        self._drain_save_buffer()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None
        try:
            if self.cli is not None:
                self.cli.disconnect()          # 关窗时只收尾，不再给设备发指令
        except Exception:
            pass
        self.root.destroy()


# =====================================================================
#  入口
# =====================================================================
def raise_timer_resolution() -> None:
    """
    Windows 默认的定时器粒度约 15.6ms，Tk 的 after(30) 实际会在 30~46ms 之间
    乱跳 —— 波形的推进节奏跟着一顿一顿的，看着就是「卡」。
    把它提到 1ms，帧间隔才稳得住。进程退出时系统会自动还原。
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except Exception:
        pass


# =====================================================================
#  窗口 / exe 图标
# =====================================================================
#: 图标文件名。和 y75_app.py 放同一目录；打包时 build_exe.bat 用
#: --icon 把它塞进 exe 资源（资源管理器/任务栏），再用 --add-data 把
#: 同一份文件解到 _MEIPASS（Tk 设窗口角标要读文件，不能从 exe 资源读）。
ICON_NAME = "zkmd.ico"


def app_dir() -> str:
    """脚本所在目录；PyInstaller onefile 运行时是临时解包目录 _MEIPASS。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def icon_path() -> Optional[str]:
    p = os.path.join(app_dir(), ICON_NAME)
    return p if os.path.exists(p) else None


def apply_window_icon(root: tk.Tk) -> bool:
    """
    把 zkmd.ico 设为窗口左上角角标（同时影响任务栏和 messagebox）。

    Tk 的 default=... 设的是「本进程所有顶层窗口的默认图标」，弹窗也跟着换；
    个别 Tk 版本只认单参数形式，所以两条都试。最后再兜一层「直接从 exe 里
    取图标资源」——万一打包时漏了 --add-data，界面也不会退回 Tk 的羽毛。
    """
    p = icon_path()
    if p:
        try:
            root.iconbitmap(default=p)
            return True
        except tk.TclError:
            pass
        try:
            root.iconbitmap(p)
            return True
        except tk.TclError:
            pass
    exe = sys.executable
    if exe.lower().endswith(".exe") and os.path.exists(exe):
        try:
            root.iconbitmap(default=exe)      # WM_SETICON 从 exe 的 RT_GROUP_ICON 取
            return True
        except tk.TclError:
            pass
    return False


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Y75 六维力传感器上位机（RS485，协议 AA 55 [CMD] [PAYLOAD] 0D 0A）")
    ap.add_argument("--port", default="", help="串口名，如 COM28")
    ap.add_argument("--baud", type=int, default=460800, help="串口波特率（默认 460800）")
    ap.add_argument("--host", default="", help="模拟器地址（填了就按 TCP 模式启动）")
    ap.add_argument("--sim", action="store_true", help="直接连本机模拟器 127.0.0.1")
    ap.add_argument("--sim-port", type=int, default=15020, help="模拟器端口（默认 15020）")
    ap.add_argument("--autoconnect", action="store_true",
                    help="连上后自动开始连续采集（配合 --sim / --port 使用）")
    ap.add_argument("--save-dir", default="", help="数据/日志默认目录（默认程序目录）")
    ap.add_argument("--selftest", action="store_true",
                    help="只建界面不连设备，用来验证打包后的 exe 能起来")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    raise_timer_resolution()          # 波形刷新要稳，先把定时器精度提上去
    try:
        root = tk.Tk()
    except tk.TclError as exc:                                # pragma: no cover
        print(f"无法创建窗口：{exc}")
        return 3

    app = App(root, args)
    icon_ok = apply_window_icon(root)      # 左上角角标 / 任务栏 / 弹窗都用 zkmd.ico
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    if args.selftest:
        root.update_idletasks()
        root.update()
        print("[ OK ] 界面已建立：控件数 =", len(root.winfo_children()))
        print("[ OK ] tkinter 可用，字体 =", app.font)
        print("[ OK ] 窗口图标 =", "zkmd.ico" if icon_ok else "未设置（找不到图标文件）")
        root.destroy()
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
