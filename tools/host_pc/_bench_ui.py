# -*- coding: utf-8 -*-
"""
真实 mainloop 下的波形性能基准（用完可删）。

注意：**不能用 `while True: root.update()` 去压测 Tk**。
`update()` 会把定时器和空闲重绘混在一起处理，一旦每帧都有重绘待办，
它就可能长时间不返回，测出来的「单次 update 82 秒」纯属脚手架假象。
必须老老实实跑 mainloop，再用 after 心跳量真实的调度抖动。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 15200 + (int(sys.argv[1]) if len(sys.argv) > 1 else 0)


def cpu_time():
    """本进程累计 CPU 时间（秒）。"""
    import ctypes

    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p      # 不设会按 c_int 截断句柄
    h = k.GetCurrentProcess()
    c, e, kt, ut = FT(), FT(), FT(), FT()
    k.GetProcessTimes(ctypes.c_void_p(h), ctypes.byref(c), ctypes.byref(e),
                      ctypes.byref(kt), ctypes.byref(ut))
    return ((kt.hi << 32) | kt.lo) / 1e7 + ((ut.hi << 32) | ut.lo) / 1e7


def main() -> int:
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    tick = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    extra = os.environ.get("Y75_SIM_EXTRA", "").split()

    sim = subprocess.Popen([sys.executable, "y75_simulator.py", "--port", str(PORT),
                            "--freq", "500", "--quiet"] + extra,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    import tkinter as tk
    from y75_app import App, WaveCanvas, raise_timer_resolution

    raise_timer_resolution()
    App.TICK_MS = tick

    n_draw = {"n": 0, "pts": 0}
    orig = WaveCanvas.draw

    def hook(self, samples, force=False):
        n_draw["n"] += 1
        n_draw["pts"] = len(samples)
        return orig(self, samples, force=force)

    WaveCanvas.draw = hook

    class A:
        port = ""
        baud = 460800
        host = ""
        sim = True
        sim_port = PORT
        save_dir = ""
        selftest = False
        autoconnect = True

    root = tk.Tk()
    root.geometry("1400x900+40+40")
    app = App(root, A())

    # 心跳：期望 tick ms 一次，量实际间隔
    beats = []

    def beat():
        beats.append(time.monotonic())
        root.after(tick, beat)

    root.after(200, beat)
    root.after(13000, root.quit)

    c0 = cpu_time()
    t0 = time.time()
    root.mainloop()
    wall = time.time() - t0
    cpu = cpu_time() - c0

    gaps = [(beats[i] - beats[i - 1]) * 1000 for i in range(1, len(beats))]
    gaps = [g for g in gaps if 0 < g < 500]
    gaps.sort()
    q = lambda p: gaps[min(len(gaps) - 1, int(len(gaps) * p))]

    print(f"\n===== TICK={tick}ms  运行 {wall:.1f}s =====")
    print(f"  波形重绘次数        {n_draw['n']}  →  {n_draw['n'] / wall:.1f} fps")
    print(f"  平均每帧样本数      {n_draw['pts']}")
    print(f"  帧间隔  期望 {tick}ms  平均 {sum(gaps) / len(gaps):.1f}  "
          f"P50 {q(0.5):.1f}  P95 {q(0.95):.1f}  最大 {gaps[-1]:.1f} ms")
    print(f"  超过 {tick * 2}ms 的比例   "
          f"{sum(1 for g in gaps if g > tick * 2) / len(gaps) * 100:.0f}%")
    print(f"  每秒掉帧抖动(标准差) {_std(gaps):.1f} ms")
    print(f"  进程 CPU 占用        {cpu / wall * 100:.0f}% (单核)")
    cli = app.cli
    if cli is not None:
        print(f"  协议统计   数据帧 {cli.push_count}  命令错误 {cli.cmd_errors}  "
              f"重同步 {cli.frame_errors}  乱码字节 {cli.garbage_bytes}")
    print(f"  界面队列残留        {app.ui_queue.qsize()} 项"
          f"（越接近 0 越好；持续增长说明事件产出比消费快）")

    root.destroy()
    sim.kill()
    sim.wait()
    return 0


def _std(xs) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


if __name__ == "__main__":
    sys.exit(main())
