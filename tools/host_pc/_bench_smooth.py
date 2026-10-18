"""
波形平滑度基准 —— 专门针对「串口驱动成批上报」这个真机场景。

真机上 USB-RS485 转换器（CH340/FTDI/CP210x）不会一帧一帧把字节交给应用层，
而是每 8~16ms 攒一批再交上来。这个脚本让模拟器按同样的方式发送（--burst-ms），
然后量两个东西：

  1. 时间轴偏离：波形的右边界 t_end 与「本地时钟 - LATENCY」差多少。
     差值在 0 附近平稳 = 匀速滚动；在 0~16ms 之间来回跳 = 一顿一顿。
  2. 帧时间戳间隔：样本时间戳的相邻差。等间隔 = 采样轴均匀；
     一批内 0、跨批 16ms = 时间轴本身是阶梯状的。

对照用法：
    python _bench_smooth.py            # 现在（本地时钟 + 固定显示延迟 + 设备时钟时间戳）
    python _bench_smooth.py --legacy   # 旧行为（贴着最新数据走 + 收到的时刻当时间戳）
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", action="store_true", help="复现旧行为做对照")
    ap.add_argument("--burst-ms", type=float, default=16.0, help="模拟的 USB 批量周期")
    ap.add_argument("--secs", type=float, default=12.0, help="测量时长")
    ap.add_argument("--port", type=int, default=15220)
    ap.add_argument("--freq", type=int, default=500)
    args = ap.parse_args()

    import tkinter as tk
    import y75_app as app_mod
    from y75_app import App, WaveCanvas
    from y75_protocol import TcpTransport

    if args.legacy:
        WaveCanvas.LATENCY_MIN = 0.0        # 贴着最新数据走
        WaveCanvas.LATENCY_MARGIN = 0.0
        app_mod.REFRESH_DEFAULT = "40"

    sim = subprocess.Popen(
        [sys.executable, "y75_simulator.py", "--port", str(args.port),
         "--freq", str(args.freq), "--burst-ms", str(args.burst_ms), "--quiet"],
        cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    # ---- 记录每批到达的字节数（证明「批量上报」真的发生了）----
    recvs = []
    orig_recv = TcpTransport.recv

    def recv_hook(self, timeout):
        d = orig_recv(self, timeout)
        if d:
            recvs.append(len(d))
        return d

    TcpTransport.recv = recv_hook

    # ---- 记录每次重画后的时间轴位置 ----
    draws = []
    orig_draw = WaveCanvas.draw

    def draw_hook(self, samples, force=False):
        r = orig_draw(self, samples, force=force)
        if self._t_end is not None:
            draws.append((time.time(), self._t_end))
        return r

    WaveCanvas.draw = draw_hook

    class Args:
        port = ""
        baud = 460800
        host = ""
        sim = True
        sim_port = args.port
        save_dir = ""
        selftest = False
        autoconnect = True

    root = tk.Tk()
    root.geometry("1400x900+40+40")
    app = App(root, Args())
    if args.legacy:
        # 旧行为：帧时间戳直接就是收到的那一刻（同批全挤在一个毫秒里）
        def force_legacy_ts():
            if app.cli is not None:
                app.cli.sample_dt = 0.0
            root.after(200, force_legacy_ts)

        root.after(200, force_legacy_ts)

    root.after(int(args.secs * 1000), root.quit)
    t0 = time.time()
    root.mainloop()
    elapsed = time.time() - t0

    cli = app.cli
    wave = list(cli.wave) if cli else []
    push = cli.push_count if cli else 0
    root.destroy()
    sim.kill()
    sim.wait()

    mode = "旧行为（贴最新数据 / 收到时刻当时间戳）" if args.legacy else "现在（本地时钟 + 显示延迟）"
    print(f"\n=== {mode} ===")
    print(f"时长 {elapsed:.1f}s   收到数据帧 {push} 帧   "
          f"实际速率 {push / max(elapsed, 0.01):.0f} Hz")

    # ---- 1. 批量到达情况 ----
    batch = [n // 29 for n in recvs if n >= 29]
    if batch:
        print(f"接收批次 {len(batch)} 次，每批 {min(batch)}~{max(batch)} 帧"
              f"（平均 {sum(batch) / len(batch):.1f}）"
              f"  —— 一次 recv 拿到这么多帧，说明驱动是攒批上报的")

    # ---- 2. 时间轴偏离 ----
    lat = WaveCanvas.LATENCY_MIN
    errs = [abs((w - lat) - t) for w, t in draws]
    if errs:
        n = len(errs)
        mean = sum(errs) / n
        var = sum((e - mean) ** 2 for e in errs) / n
        print(f"时间轴偏离 |(本地时钟-{lat:g}s) - 右边界|："
              f"平均 {mean * 1000:.1f}ms  最大 {max(errs) * 1000:.1f}ms  "
              f"标准差 {var ** 0.5 * 1000:.1f}ms   （越小越匀速）")

    # ---- 3. 帧时间戳间隔 ----
    gaps = [(wave[i][0] - wave[i - 1][0]) * 1000
            for i in range(1, len(wave))]
    if gaps:
        gaps_s = sorted(gaps)
        n = len(gaps)
        mean = sum(gaps) / n
        var = sum((g - mean) ** 2 for g in gaps) / n
        zero = sum(1 for g in gaps if g < 0.01)
        print(f"帧时间戳间隔：平均 {mean:.3f}ms  标准差 {var ** 0.5:.3f}ms  "
              f"P50 {gaps_s[n // 2]:.3f}  最大 {gaps_s[-1]:.3f}ms")
        print(f"  其中「与上一帧时间戳几乎相同」的帧：{zero} 个"
              f"（{zero / n * 100:.0f}%）—— 这些帧在时间轴上是重叠的")

    # ---- 4. 波形有效帧率 ----
    print(f"重画次数 {len(draws)} 次，约 {len(draws) / max(elapsed, 0.01):.1f} fps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
