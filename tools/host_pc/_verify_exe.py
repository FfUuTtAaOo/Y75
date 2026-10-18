# -*- coding: utf-8 -*-
"""临时脚本：拉起模拟器 + 启动打包好的 exe，截图验证。验证完可删。"""
import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
EXE = os.path.join(HERE, "dist", "Y75_Host.exe")
PORT = 15077
#: 设 Y75_BURST_MS=16 可让模拟器像真机驱动那样成批上报，验证打包产物的抗抖动
BURST = os.environ.get("Y75_BURST_MS", "0")
#: 额外模拟器参数，如 Y75_SIM_EXTRA="--short-frames 7"
EXTRA = os.environ.get("Y75_SIM_EXTRA", "").split()


def _pid_name(pid: int) -> str:
    """按 PID 查进程名（onefile 的窗口在 bootloader 的子进程里，不能直接比 PID）。"""
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value) if ok else "?"
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def main() -> int:
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    import make_icon

    # 先清掉可能残留的旧实例：按标题找窗口时，绝不能抓到上一轮的窗口
    subprocess.run(["taskkill", "/F", "/IM", os.path.basename(EXE)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)

    cmd = [PY, "y75_simulator.py", "--port", str(PORT), "--quiet"]
    if float(BURST) > 0:
        cmd += ["--burst-ms", BURST]
    cmd += EXTRA
    sim = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("simulator pid =", sim.pid, " burst =", BURST, "ms  extra =", EXTRA)
    time.sleep(1.5)

    app = subprocess.Popen([EXE, "--sim", "--sim-port", str(PORT), "--autoconnect"])
    print("exe pid =", app.pid)

    hwnd = None
    for _ in range(60):
        time.sleep(0.5)
        hwnd = make_icon.find_window_by_title("Y75 六维力传感器上位机")
        if hwnd:
            break
    if not hwnd:
        print("[FAIL] 没找到 exe 的窗口")
        app.kill(); sim.kill()
        return 1
    # 确认窗口确实属于我们的 exe（onefile 的窗口在 bootloader 子进程里，
    # 所以只校验进程名，不比 PID —— PID 会是 bootloader 的子进程）
    pid_buf = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
    owner = _pid_name(pid_buf.value)
    if owner != os.path.basename(EXE):
        print(f"[FAIL] 找到的窗口属于 {owner}({pid_buf.value})，"
              f"而不是 {os.path.basename(EXE)}（可能有旧实例残留）")
        app.kill(); sim.kill()
        return 1
    print("hwnd =", hwnd, "窗口进程 =", owner, f"(pid {pid_buf.value}, "
          f"bootloader pid {app.pid})")

    # 让连接 + 推流跑一会儿，波形里才有数据
    time.sleep(6.0)

    out = os.path.join(HERE, "exe_screenshot.png")
    make_icon.grab_window(hwnd, out)
    print("[ OK ] 截图 ->", out, os.path.getsize(out), "字节")

    alive = app.poll() is None
    print("exe 仍在运行 =", alive)
    app.kill(); sim.kill()
    app.wait(); sim.wait()
    return 0 if alive else 2


if __name__ == "__main__":
    sys.exit(main())
