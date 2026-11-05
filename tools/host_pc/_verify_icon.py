# -*- coding: utf-8 -*-
"""临时脚本：验证 exe 图标（文件图标 / 标题栏 / 任务栏），纯标准库。

用法：
    python _verify_icon.py            # 只查 exe 里的图标资源 + 资源管理器显示
    python _verify_icon.py --run      # 再拉起 exe，截全屏看任务栏和标题栏
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "Y75_Host.exe")
PORT = 15077

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

sys.path.insert(0, HERE)
import make_icon  # noqa: E402  (复用 write_png / grab_window)


# ---------------------------------------------------------------------
def ico_entries(path: str):
    blob = open(path, "rb").read()
    _, kind, count = struct.unpack("<HHH", blob[:6])
    out = []
    for i in range(count):
        off = 6 + 16 * i
        w, h, cc, res, planes, bpp, size, offset = struct.unpack("<BBBBHHII", blob[off:off + 16])
        out.append((w or 256, bpp, blob[offset:offset + size]))
    return out


def check_resources() -> int:
    """exe 里是否原样含有 zkmd.ico 的每一档尺寸。"""
    exe = open(EXE, "rb").read()
    bad = 0
    print(f"exe = {EXE}  {len(exe):,} 字节")
    for w, bpp, blob in ico_entries(os.path.join(HERE, "zkmd.ico")):
        ok = blob in exe
        print(f"  [{'OK' if ok else 'MISS'}] {w}x{w} {bpp}bpp  {len(blob):,} 字节")
        bad += 0 if ok else 1
    n = shell32.ExtractIconExW(ctypes.c_wchar_p(EXE), -1, None, None, 0)
    print(f"  图标组数量 = {n}（应为 1）")
    bad += 0 if n == 1 else 1
    return bad


def hicon_to_rows(hicon: int, size: int):
    hdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, size, size)
    gdi32.SelectObject(mdc, bmp)
    brush = gdi32.CreateSolidBrush(0xFFFFFF)
    rect = wt.RECT(0, 0, size, size)
    user32.FillRect(mdc, ctypes.byref(rect), brush)
    user32.DrawIconEx(mdc, 0, 0, hicon, size, size, 0, None, 0x0003)

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BMI()
    bi.biSize = ctypes.sizeof(BMI)
    bi.biWidth = size
    bi.biHeight = -size
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(size * size * 4)
    gdi32.GetDIBits(mdc, bmp, 0, size, buf, ctypes.byref(bi), 0)

    raw = buf.raw
    rows = []
    for y in range(size):
        off = y * size * 4
        line = bytearray(size * 3)
        line[0::3] = raw[off:off + size * 4:4]
        line[1::3] = raw[off + 1:off + size * 4:4]
        line[2::3] = raw[off + 2:off + size * 4:4]
        rows.append(bytes(line))

    gdi32.DeleteObject(brush)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(0, hdc)
    return rows


def shell_icon_png(path: str, out: str, size: int = 32) -> str:
    """资源管理器（shell）此刻会画出来的图标。"""
    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [("hIcon", wt.HICON), ("iIcon", ctypes.c_int),
                    ("dwAttributes", wt.DWORD), ("szDisplayName", wt.WCHAR * 260),
                    ("szTypeName", wt.WCHAR * 80)]

    info = SHFILEINFOW()
    flags = 0x100 | (0x0 if size > 16 else 0x1)          # SHGFI_ICON | LARGE/SMALL
    shell32.SHGetFileInfoW(ctypes.c_wchar_p(os.path.abspath(path)), 0,
                           ctypes.byref(info), ctypes.sizeof(info), flags)
    if not info.hIcon:
        raise RuntimeError("SHGetFileInfo 没拿到图标")
    rows = hicon_to_rows(info.hIcon, size)
    user32.DestroyIcon(info.hIcon)
    return make_icon.write_png(out, size, size, rows)


# ---------------------------------------------------------------------
def grab_screen_rect(left: int, top: int, w: int, h: int, out: str) -> str:
    hdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, hdc, left, top, 0x00CC0020)   # SRCCOPY

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BMI()
    bi.biSize = ctypes.sizeof(BMI)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    raw = buf.raw
    rows = []
    for y in range(h):
        off = y * w * 4
        line = bytearray(w * 3)
        line[0::3] = raw[off:off + w * 4:4]
        line[1::3] = raw[off + 1:off + w * 4:4]
        line[2::3] = raw[off + 2:off + w * 4:4]
        rows.append(bytes(line))
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(0, hdc)
    return make_icon.write_png(out, w, h, rows)


def find_window(title: str):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, b, n + 1)
                if title.lower() in b.value.lower():
                    found.append(hwnd)
        return True

    user32.EnumWindows(cb, None)
    return found


def run_and_capture() -> int:
    subprocess.run(["taskkill", "/F", "/IM", os.path.basename(EXE)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    sim = subprocess.Popen([sys.executable, "y75_simulator.py", "--port", str(PORT), "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    app = subprocess.Popen([EXE, "--sim", "--sim-port", str(PORT), "--autoconnect"])
    hwnd = None
    for _ in range(40):
        time.sleep(0.5)
        wins = find_window("六维力传感器上位机")
        if wins:
            hwnd = wins[0]
            break
    if not hwnd:
        print("[FAIL] 没找到窗口")
        app.kill(); sim.kill()
        return 1
    time.sleep(4.0)

    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print("窗口位置", rect.left, rect.top, rect.right, rect.bottom)
    # 标题栏左上角（角标所在）
    grab_screen_rect(rect.left, rect.top, 260, 34, os.path.join(HERE, "chk_titlebar.png"))

    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    # 任务栏：底部 48px 右侧 1/3（按钮区）
    grab_screen_rect(int(sw * 0.55), sh - 48, int(sw * 0.45), 48,
                     os.path.join(HERE, "chk_taskbar.png"))
    app.kill(); sim.kill()
    app.wait(); sim.wait()
    print("截图完成")
    return 0


def main() -> int:
    os.chdir(HERE)
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass
    bad = check_resources()
    shell_icon_png(EXE, os.path.join(HERE, "chk_shell_32.png"), 32)
    shell_icon_png(EXE, os.path.join(HERE, "chk_shell_16.png"), 16)
    print("资源管理器图标 -> chk_shell_32.png / chk_shell_16.png")
    if "--run" in sys.argv:
        bad += run_and_capture()
    print("=====", "全部通过" if bad == 0 else f"{bad} 项失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
