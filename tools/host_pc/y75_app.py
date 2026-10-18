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

#: 连接方式
MODE_SERIAL = "串口 (RS485)"
MODE_TCP = "网络模拟 (TCP)"

#: 输出模式
OUT_HEX = "HEX 标准帧"
OUT_ASC = "ASC 调试帧"

#: 波形刷新率（标签是 fps，值是毫秒周期）。
#: 越高越顺滑、越费 CPU：老机器或同时开很多东西时往下调。
REFRESH_CHOICES = ("20", "25", "30", "40", "50")
REFRESH_DEFAULT = "40"


def refresh_to_ms(label: str) -> int:
    try:
        fps = int(label)
    except (TypeError, ValueError):
        fps = int(REFRESH_DEFAULT)
    return max(10, int(round(1000.0 / max(1, fps))))


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
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 66, 14, 16, 28
    #: 两次重画的最小间隔（≈45fps 上限），用来吸收 resize 风暴
    MIN_DT = 0.020
    #: Y 轴收缩速度：每帧向目标量程靠拢这么多（扩张是瞬时的）
    SHRINK_K = 0.10
    #: 显示延迟（秒）：数据先攒进缓冲，波形按本地时钟匀速回放。
    #: 串口驱动是**成批**把字节交上来的（USB 8~16ms 一批），若右边界跟着
    #: 「最新一帧的到达时刻」走，就会停一下、蹦一下。留出延迟、缓冲里始终
    #: 有货，右边界才能匀速前进，与数据到达节奏彻底解耦。
    #: 实际延迟 = max(LATENCY_MIN, 实测样本年龄峰值 + LATENCY_MARGIN)，
    #: 见 draw()。TCP 回环时样本年龄≈0，延迟就取 LATENCY_MIN。
    LATENCY_MIN = 0.05
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
        self.enabled = [True] * 6
        self.unit = "N"

        self._font = "TkDefaultFont"
        self._t_end: Optional[float] = None      # 平滑推进的时间轴右边界
        self._mono = 0.0                         # 上次重画的 monotonic 时刻
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

    def reset(self, replot: bool = False) -> None:
        """清掉时间轴与量程（断线、重连、换时间窗时用）。"""
        self._t_end = None
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
        """网格/坐标轴/图例只在尺寸、时间窗或单位变化时重排（约 35 次 Tcl 调用）。"""
        sig = (w, h, self.window_sec, self.unit)
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
            rel = -self.window_sec + self.window_sec * i / 5
            self.coords(tx, pad_l + pw * i / 5, pad_t + ph + 13)
            self.itemconfigure(tx, text=f"{rel:+.1f}s")

        x = pad_l + 10
        for i in range(6):
            self.coords(it["lgbox"][i], x, pad_t + 7, x + 11, pad_t + 17)
            self.coords(it["lgtext"][i], x + 16, pad_t + 12)
            self.itemconfigure(it["lgtext"][i], text=f"{AXIS_NAMES[i]} ({self.unit})")
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
             force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._mono < self.MIN_DT:
            return
        dt = now - self._mono if self._mono else 0.0
        self._mono = now
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

        target = wall - latency
        if self._t_end is None:
            self._t_end = min(newest, target)
        else:
            self._t_end += dt                      # 按真实经过时间自然前进
            err = target - self._t_end
            if abs(err) > 0.4:
                self._t_end += err                 # 断流 / 系统时钟跳变 → 重新对齐
            else:
                self._t_end += err * 0.25          # 其余柔性纠偏，不产生可见跳变
            if self._t_end > newest:
                # 兜底：链路出现远超近期峰值的尖峰停顿（如固件写 Flash）。
                # 自适应延迟下这里几乎不会触发；真触发了也只说明「确实没有新数据」。
                self._t_end = newest               # 数据不够新 → 停住，右侧不留空白
                self.clamp_n += 1
                self.clamp_sec += dt
        t_end = self._t_end
        t_start = t_end - self.window_sec

        # 裁出可视区间。两头都要裁：
        #   左边 = 时间窗左端；
        #   右边 = **时间轴右端**，不是「最新样点」。
        # 不裁右边的话，曲线会一直画到最新样点（比时间轴超前 LATENCY），
        # 实测能画出右边界之外 18px 并被控件边缘硬切掉 —— 网格在匀速滑、
        # 曲线尾巴却顶在窗口最右边不动，两者对不上，看着就别扭。
        # 因为上面已保证 t_end ≤ newest，裁到 t_end 绝不会在右边留空白。
        i0 = bisect_left(samples, (t_start,))
        i1 = bisect_right(samples, (t_end,))
        if i1 <= i0:
            i1 = min(len(samples), i0 + 1)
        vis = samples[i0:i1]
        if not vis:
            vis = samples[-1:]

        # ---- 2. 抽稀：把整个宽度铺满，**一个像素最多一个点** ----
        # 超过 1 点/像素的画面对眼睛没有增益，但 Tk 画 2px 宽的折线是按填充多边形
        # 走的，点多一倍重绘就贵一倍（实测 1250 点 6.6ms / 2500 点 22ms）。
        # 上限 1400：4K 屏上波形拉到 3000+ 像素宽时，若无上限 6 条曲线要画
        # 近 2 万个点，draw() 会从 2ms 涨到 20ms+，整帧率直接腰斩 ——
        # Modbus-TCP 版就是用 800 点硬顶住才一直顺的。
        n = len(vis)
        max_pts = min(max(int(pw), 128), 1400)
        step = max(1, -(-n // max_pts))
        if step == 1:
            pts = vis
        else:
            pts = vis[::step]
            if (len(pts) - 1) * step != n - 1:
                pts = pts + vis[-1:]

        # ---- 3. 量程：扩张瞬时、收缩缓慢 ----
        lo, hi = float("inf"), float("-inf")
        for _t, vals in pts:
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

        px_per_sec = pw / self.window_sec
        y_scale = ph / (hi - lo)
        for i in range(6):
            if not self.enabled[i]:
                self._set_curve(i, False)
                continue
            coords: List[float] = []
            ap = coords.append
            for t, vals in pts:
                ap(pad_l + (t - t_start) * px_per_sec)
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
        self.root.title("Y75 六维力传感器上位机  ·  RS485  AA 55 … 0D 0A")
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
        tk.Label(top, text="Y75 六维力传感器上位机", bg=CARD, fg=TXT,
                 font=(self.font, 13, "bold")).pack(side="left", padx=14, pady=9)
        tk.Label(top, text="协议：AA 55 [CMD] [PAYLOAD] 0D 0A   ·   默认 460800 8N1",
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
        self.left_canvas = tk.Canvas(left_outer, bg=BG, highlightthickness=0, bd=0,
                                     width=316, yscrollincrement=20)
        sb = ttk.Scrollbar(left_outer, orient="vertical", command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=sb.set)
        self.left_canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.left_sb = sb
        left = ttk.Frame(self.left_canvas, padding=(10, 10, 6, 10))
        win = self.left_canvas.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>",
                  lambda e: self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all")))
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
            unit = tk.Label(head, text="N", bg=CARD, fg=TXT_DIM, font=(self.font, 8))
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
        tk.Label(bar, text="显示通道", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left")
        self.chan_vars = []
        for i, name in enumerate(AXIS_NAMES):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text=name, variable=v,
                            command=self._on_channel_change).pack(side="left", padx=(6, 0))
            self.chan_vars.append(v)
        tk.Label(bar, text="时间窗", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left", padx=(16, 0))
        self.win_var = tk.StringVar(value="5")
        ttk.Combobox(bar, textvariable=self.win_var, width=5, state="readonly",
                     values=("1", "2", "5", "10", "20"),
                     font=(self.font, 9)).pack(side="left", padx=(4, 0))
        self.win_var.trace_add("write", lambda *_: self._on_window_change())

        tk.Label(bar, text="刷新", bg=BG, fg=TXT_DIM,
                 font=(self.font, 9)).pack(side="left", padx=(16, 0))
        self.refresh_var = tk.StringVar(value=REFRESH_DEFAULT)
        ttk.Combobox(bar, textvariable=self.refresh_var, width=5, state="readonly",
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
                self.left_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def walk(w):
            w.bind("<MouseWheel>", on_wheel, add="+")
            for child in w.winfo_children():
                walk(child)

        walk(widget)

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
                "stream_every": 50 if self.log_stream_var.get() else 0}
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
        return out

    def _apply_info(self, info: dict) -> None:
        self.info_labels["sn"].configure(text=info.get("sn") or "—")
        self.info_labels["ver"].configure(text=info.get("ver") or "—")
        st = info.get("status")
        if st:
            self.send_mode = int(st["send_mode"])
            self.data_format = int(st["data_format"])
            unit = FORMAT_UNIT.get(self.data_format, "")
            self.info_labels["state"].configure(
                text=f"{st['send_mode_text']} · {st['data_format_text']} · "
                     f"{st['freq_hz']}Hz · {st['debug_mode_text']}")
            self.info_labels["err"].configure(text=f"{st['comm_error']}")
            self.freq_var.set(str(st["freq_hz"]))
            for name, code in FMT_CHOICES:
                if code == self.data_format:
                    self.fmt_var.set(name)
            self.out_var.set(OUT_ASC if st["debug_mode"] == 1 else OUT_HEX)
            self._set_units(unit)
        else:
            self.info_labels["state"].configure(
                text=f"读取失败（{info.get('status_err', '')}）")
            self.info_labels["err"].configure(text="—")

    def _set_units(self, unit: str) -> None:
        for _v, u in self.cards:
            u.configure(text=unit or "N")
        self.wave.unit = unit or "N"

    def on_start(self) -> None:
        def done(_res):
            self.send_mode = 1
            self._log_ui("info", "已开始连续采集")
        self._run_async(lambda: self.cli.start(), done, "开始连续采集")

    def on_stop(self) -> None:
        def done(_res):
            self.send_mode = 0
            self._log_ui("info", "已停止连续采集")
        self._run_async(lambda: self.cli.stop(), done, "停止连续采集")

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
        self._run_async(lambda: self.cli.query_matrix(), self._apply_matrix, "读取解耦矩阵")

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
        # 只取可视时间窗内的样本，别每帧都复制整段缓冲。
        # 多要 1s：时间轴右边界比最新数据晚 LATENCY，左边界会往前多探一点。
        count, samples = cli.take_wave(self.wave.window_sec + 1.0)
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
        self.wave.draw(samples)

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
        if bad:
            for i, msg in enumerate(bad, 1):
                self._log_ui("warn", f"体检结论 {i}/{len(bad)}：{msg}")
        else:
            self._log_ui("info", "体检结论：链路与显示都正常 —— 如果还觉得卡，"
                                 "把上面这几行发我。")

    def _on_channel_change(self) -> None:
        for i, v in enumerate(self.chan_vars):
            self.wave.enabled[i] = bool(v.get())

    def _on_window_change(self) -> None:
        try:
            self.wave.set_window_sec(float(self.win_var.get()))
        except ValueError:
            pass

    def _on_refresh_change(self) -> None:
        """改波形刷新率。下一次 tick 生效，不用重启。"""
        self.tick_ms = refresh_to_ms(self.refresh_var.get())
        self._log_ui("info", f"波形刷新 {self.refresh_var.get()} fps"
                             f"（每 {self.tick_ms}ms 一帧）")

    def _update_order_hint(self) -> None:
        self.order_hint.configure(text=f"{ByteOrder.HINT.get(self.order_var.get(), '')}"
                                       "（Y75 固件用标准小端 = DCBA）")

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
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    if args.selftest:
        root.update_idletasks()
        root.update()
        print("[ OK ] 界面已建立：控件数 =", len(root.winfo_children()))
        print("[ OK ] tkinter 可用，字体 =", app.font)
        root.destroy()
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
