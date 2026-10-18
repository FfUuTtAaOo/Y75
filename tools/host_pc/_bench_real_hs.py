# -*- coding: utf-8 -*-
"""
_bench_real_hs.py —— 和 _bench_real.py 完全等价的指标，测的是 hs 上位机本体。
用来做真正公平的对照：同一台机器、同一套指标、同样的推流速率。

用法（先起模拟器）：
    python hs_simulator.py --port 15502 --rate 500 --quiet
    python _bench_real_hs.py --secs 14 --port 15502
"""

from __future__ import annotations

import argparse
import os
import statistics as st
import sys
import time
import tkinter as tk

HS_DIR = r"D:\ST\workspace\vscode\hs_modbus_tcp\tools\host_pc"
sys.path.insert(0, HS_DIR)

import hs_app                                     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=12.0)
    ap.add_argument("--port", type=int, default=15502)
    ap.add_argument("--warmup", type=float, default=3.0)
    args = ap.parse_args()

    hs_app.enable_dpi_awareness()
    root = tk.Tk()
    root.geometry("1440x940")
    win = hs_app.MainWindow(root, "127.0.0.1", args.port,
                            hs_app.ByteOrder.POLL_DEFAULT,
                            hs_app.ByteOrder.WRITE_DEFAULT, None)
    root.protocol("WM_DELETE_WINDOW", lambda: None)

    rec = []
    ticks = []
    wave = win.wave
    orig_draw = wave.draw

    def draw(samples, force=False):
        a = time.perf_counter()
        orig_draw(samples, force)
        edge = samples[-1][0] if samples else None
        rec.append((time.monotonic(), edge, len(samples),
                    time.perf_counter() - a))

    wave.draw = draw

    orig_tick = win._tick

    def tick():
        ticks.append(time.monotonic())
        orig_tick()

    win._tick = tick

    # 自动连上并开始连续转换
    root.after(150, win.on_connect)
    root.after(2500, win.on_start)
    t0 = time.monotonic()

    def stop():
        root.quit()

    root.after(int(args.secs * 1000), stop)
    root.mainloop()
    geom = (root.winfo_width(), root.winfo_height(),
            wave.winfo_width(), wave.winfo_height())
    try:
        win.cli.close()
    except Exception:
        pass
    root.destroy()

    ticks = [t for t in ticks if t - t0 > args.warmup]
    rec = [r for r in rec if r[0] - t0 > args.warmup and r[1] is not None]
    if len(rec) < 20:
        print(f"有效帧太少（{len(rec)}）—— 模拟器没连上？")
        return 1

    span = rec[-1][0] - rec[0][0]
    dt = [rec[i][0] - rec[i - 1][0] for i in range(1, len(rec))]
    dte = [rec[i][1] - rec[i - 1][1] for i in range(1, len(rec))]
    vel = [dte[i] / dt[i] for i in range(len(dt)) if dt[i] > 1e-6]
    px_per_sec = (geom[2] - 62 - 12) / wave.window_sec
    vpx = [v * px_per_sec for v in vel]
    tdt = [ticks[i] - ticks[i - 1] for i in range(1, len(ticks))]

    print(f"\n===== hs 上位机（完整界面 + Modbus-TCP 收包） =====")
    print(f"窗口 {geom[0]}x{geom[1]}   波形 {geom[2]}x{geom[3]}   "
          f"时间窗 {wave.window_sec:.0f}s   tick 40ms")
    print(f"tick 计数         : {len(ticks)}  间隔 均值/σ/最大 "
          f"{st.mean(tdt)*1000:.2f} / {st.pstdev(tdt)*1000:.2f} / "
          f"{max(tdt)*1000:.2f} ms")
    print(f"实际重绘帧率      : {len(rec)/span:6.1f} fps")
    print(f"重绘间隔 均值/σ/最大: {st.mean(dt)*1000:6.2f} / "
          f"{st.pstdev(dt)*1000:5.2f} / {max(dt)*1000:6.2f} ms")
    print(f"滚动速度 均值     : {st.mean(vpx):7.1f} px/s")
    print(f"滚动速度 σ        : {st.pstdev(vpx):7.1f} px/s  ← 越小越顺")
    print(f"滚动速度 最大偏离 : {max(abs(v - st.mean(vpx)) for v in vpx):7.1f} px/s")
    print(f"每帧点数 均值     : {st.mean([r[2] for r in rec]):6.0f}")
    print(f"draw() 耗时 均值/最大: "
          f"{st.mean([r[3] for r in rec])*1000:5.2f} / "
          f"{max(r[3] for r in rec)*1000:5.2f} ms")
    print(f"接收帧率          : {win.sample_rate:6.1f} Hz   "
          f"累计 {win.cli.push_count} 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
