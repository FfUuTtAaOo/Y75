# -*- coding: utf-8 -*-
"""
_bench_real.py —— 直接测**真实上位机**（完整界面 + 真 TCP 收包）的波形滚动平顺度。

和 _bench_ab.py 的区别：这里跑的是 y75_app.App 本人 —— 左栏几十个控件、
六个数值卡片、日志文本框、Notebook 全都在，_tick 走的也是真实路径。
如果「卡」来自完整界面的开销，这里才量得出来。

用法（先起模拟器）：
    python y75_simulator.py --port 15020 --freq 500 --burst-ms 12 --quiet
    python _bench_real.py --secs 12 --sim-port 15020
"""

from __future__ import annotations

import argparse
import os
import statistics as st
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import y75_app                                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=12.0)
    ap.add_argument("--sim-port", type=int, default=15020)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--window", default="5", help="波形时间窗秒数")
    ap.add_argument("--rawlog", action="store_true", help="打开报文日志")
    ap.add_argument("--save", action="store_true", help="同时保存数据到 Excel")
    ap.add_argument("--legacy-latency", action="store_true",
                    help="退回固定 60ms 显示延迟（自适应延迟的 A/B 对照）")
    ap.add_argument("--diagnose", action="store_true",
                    help="结束时调一次「链路体检」并打印日志内容")
    args = ap.parse_args()

    if args.legacy_latency:
        y75_app.WaveCanvas.ADAPTIVE = False
        y75_app.WaveCanvas.LATENCY_MIN = 0.06

    y75_app.raise_timer_resolution()
    root = tk.Tk()
    root.geometry("1440x940")
    app_args = y75_app.build_parser().parse_args(
        ["--sim", "--sim-port", str(args.sim_port), "--autoconnect"])
    app = y75_app.App(root, app_args)
    root.protocol("WM_DELETE_WINDOW", lambda: None)

    if args.window != "5":
        app.win_var.set(args.window)
        app._on_window_change()
    if args.rawlog:
        app.log_stream_var.set(True)          # 推流帧也写报文日志（最重的那个开关）
        app._on_toggle_stream_log()
    if args.save:
        import tempfile
        _sp = os.path.join(tempfile.gettempdir(), "y75_bench_save.xlsx")
        root.after(3000, lambda: app._start_saving(_sp))

    rec = []                      # (墙钟, 右边界秒, 可见点数, draw 耗时)
    orig_draw = app.wave.draw
    n_calls = [0]
    n_forced = [0]

    def draw(samples, force=False):
        n_calls[0] += 1
        if force:
            n_forced[0] += 1
        n0 = app.wave.draw_n
        a = time.perf_counter()
        orig_draw(samples, force)
        took = time.perf_counter() - a
        # 只记「真正重画了」的那些：被 MIN_DT 门限挡掉的调用不推进时间轴，
        # 记进去会把 dte/dt 算成 0，制造假的抖动。
        if app.wave.draw_n != n0:
            # 曲线最右端 x：这才是眼睛真正在追的东西。网格/坐标轴可以匀速，
            # 但如果曲线尾巴跟着数据到达节奏一跳一跳，照样看着卡。
            it = app.wave._items
            tip = 0.0
            if it:
                c = app.wave.coords(it["curve"][0])
                if len(c) >= 2:
                    tip = c[-2]
            rec.append((time.monotonic(), app.wave._t_end,
                        len(app.wave._last_samples), took, tip))

    app.wave.draw = draw

    ticks = []
    orig_tick = app._tick

    def tick():
        ticks.append(time.monotonic())
        orig_tick()

    app._tick = tick

    t0 = time.monotonic()
    state = {"done": False}
    log_dump = []

    def stop():
        if args.diagnose:
            try:
                app.on_diagnose()
                log_dump.append(app.log_text.get("1.0", "end"))
            except Exception as exc:                       # noqa: BLE001
                log_dump.append(f"体检失败：{exc}")
        state["done"] = True
        root.quit()

    root.after(int(args.secs * 1000), stop)
    root.mainloop()
    # 诊断：右端空白有多大？（t_end 应该 <= newest，曲线才会贴满右边）
    diag = ""
    try:
        wv = app.wave
        newest = app.cli.wave[-1][0] if (app.cli and app.cli.wave) else 0.0
        wall = time.time()
        coords = wv.coords(wv._items["curve"][2]) if wv._items else []
        last_x = coords[-2] if len(coords) >= 2 else 0.0
        diag = (f"t_end-newest={wv._t_end - newest:+.3f}s  "
                f"wall-newest={wall - newest:+.3f}s  "
                f"自适应延迟={wv.latency * 1000:.0f}ms  "
                f"曲线最右x={last_x:.0f} / "
                f"画布宽={wv.winfo_width()}  (右边界≈{wv.winfo_width() - wv.PAD_R})")
        # 竖条检测：时间戳间隔若塌缩到远小于帧周期，波形里就会出现竖线。
        cli = app.cli
        if cli and len(cli.wave) > 50:
            tw = [t for t, _ in cli.wave]
            gp = [b - a for a, b in zip(tw, tw[1:])]
            dt = cli.sample_dt
            bars = sum(1 for g in gp if g < 0.3 * dt)
            diag += (f"  |  帧间隔 min/中位={min(gp) * 1000:.2f}/"
                     f"{st.median(gp) * 1000:.2f}ms (dt={dt * 1000:.2f}ms)  "
                     f"竖条帧数={bars}")
    except Exception as exc:
        diag = f"诊断失败: {exc}"
    try:
        wave_txt = app.stat_labels["wave"].cget("text")
    except Exception:
        wave_txt = "?"
    # 尺寸要在窗口销毁前读出来
    geom = (root.winfo_width(), root.winfo_height(),
            app.wave.winfo_width(), app.wave.winfo_height())
    try:
        app.cli and app.cli.disconnect()
    except Exception:
        pass
    root.destroy()

    ticks = [t for t in ticks if t - t0 > args.warmup]
    rec = [r for r in rec if r[0] - t0 > args.warmup]
    if len(rec) < 20:
        print(f"有效帧太少（{len(rec)}）—— 模拟器没连上？")
        return 1

    span = rec[-1][0] - rec[0][0]
    dt = [rec[i][0] - rec[i - 1][0] for i in range(1, len(rec))]
    dte = [rec[i][1] - rec[i - 1][1] for i in range(1, len(rec))]
    vel = [dte[i] / dt[i] for i in range(len(dt)) if dt[i] > 1e-6]
    w = app.wave
    px_per_sec = (geom[2] - w.PAD_L - w.PAD_R) / w.window_sec
    vpx = [v * px_per_sec for v in vel]

    tdt = [ticks[i] - ticks[i - 1] for i in range(1, len(ticks))]

    print(f"\n===== 真实上位机（完整界面 + TCP 收包） =====")
    print(f"窗口 {geom[0]}x{geom[1]}   "
          f"波形 {geom[2]}x{geom[3]}   "
          f"时间窗 {w.window_sec:.0f}s")
    print(f"tick 计数         : {len(ticks)}  间隔 均值/σ/最大 "
          f"{st.mean(tdt)*1000:.2f} / {st.pstdev(tdt)*1000:.2f} / "
          f"{max(tdt)*1000:.2f} ms")
    print(f"实际重绘帧率      : {len(rec)/span:6.1f} fps")
    print(f"draw 调用/强制重排 : {n_calls[0]} / {n_forced[0]} 次   "
          f"（真重画 {w.draw_n} 次 = {w.draw_n/span:.1f} fps；"
          f"MIN_DT={w.MIN_DT*1000:.0f}ms tick={app.tick_ms}ms）")
    print(f"重绘间隔 均值/σ/最大: {st.mean(dt)*1000:6.2f} / "
          f"{st.pstdev(dt)*1000:5.2f} / {max(dt)*1000:6.2f} ms")
    print(f"滚动速度 均值     : {st.mean(vpx):7.1f} px/s")
    print(f"滚动速度 σ        : {st.pstdev(vpx):7.1f} px/s  ← 越小越顺")
    print(f"滚动速度 最大偏离 : {max(abs(v - st.mean(vpx)) for v in vpx):7.1f} px/s")
    print(f"每帧可见点数 均值 : {st.mean([r[2] for r in rec]):6.0f}")
    tipx = [r[4] for r in rec]
    right_edge = geom[2] - w.PAD_R
    print(f"曲线端点 x 相对右轴: 均值 {st.mean(tipx) - right_edge:+6.1f} px  "
          f"σ {st.pstdev(tipx):5.1f} px  最右 {max(tipx) - right_edge:+6.1f} px  "
          f"（>0 = 曲线跑到了时间轴右边）")
    print(f"draw() 耗时 均值/最大: "
          f"{st.mean([r[3] for r in rec])*1000:5.2f} / "
          f"{max(r[3] for r in rec)*1000:5.2f} ms")
    print(f"接收帧率          : {app.frame_rate:6.1f} Hz   "
          f"累计 {app.cli.push_count if app.cli else 0} 帧")
    print(f"自适应显示延迟    : {w.latency * 1000:6.0f} ms  "
          f"（样本年龄峰值 {w._age_peak * 1000:.0f} ms，上限 "
          f"{w.LATENCY_MAX * 1000:.0f} ms）")
    print(f"右边界兜底夹子    : {w.clamp_n:5d}/{w.draw_n:<5d} 帧 "
          f"（{100.0 * w.clamp_n / max(1, w.draw_n):4.1f}%）"
          f"  累计停住 {w.clamp_sec * 1000:6.0f} ms  ← 越少越顺")
    print("右端诊断          :", diag)
    print(f"统计栏「波形刷新」  : {wave_txt}")
    if log_dump:
        print("\n----- 「链路体检」日志内容 -----")
        print(log_dump[0].strip()[-1600:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
