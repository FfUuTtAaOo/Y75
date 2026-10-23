# -*- coding: utf-8 -*-
"""
make_icon.py —— 生成程序图标(.ico) 与界面截图(.png)，纯标准库

为什么不用 Pillow：
    整套工程坚持「零第三方依赖 + 单文件打包」。画一个图标和截一张窗口图，
    用 ctypes + zlib 就够了，没必要为此多背一个 Pillow。

图标设计：
    深蓝圆角底 + 六根高低不一的柱子（对应六个力通道）+ 底部基线。
    256/128/64/48/32/16 六种尺寸一起打进同一个 .ico，小尺寸下也清楚。

截图：
    PrintWindow(hwnd, hdc, 2) 取窗口**合成后**的画面（比 BitBlt 稳，
    不受动画重绘影响）→ GetDIBits 取 32bpp BGRA → 压成 PNG。

用法：
    python make_icon.py --ico zkmd.ico
    python make_icon.py --shot ui.png --title "Y75"
    python make_icon.py --selftest
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import struct
import sys
import zlib
from typing import Callable, List, Optional, Tuple

# 与界面同一套配色，图标和界面看起来是一家的
BG_TOP = (0x2F, 0x6F, 0xD0)
BG_BOTTOM = (0x1B, 0x3E, 0x77)
BARS = ((0xD9, 0x4A, 0x4A), (0x2F, 0x9E, 0x5F), (0x2F, 0x6F, 0xD0),
        (0xD9, 0x8A, 0x1F), (0x8B, 0x5C, 0xF6), (0x0F, 0x9B, 0x9B))
BAR_HEIGHTS = (0.42, 0.62, 0.86, 0.52, 0.70, 0.34)

ICON_SIZES = (256, 128, 64, 48, 32, 16)


# =====================================================================
#  画图标
# =====================================================================
def render_rgba(size: int) -> List[List[Tuple[int, int, int, int]]]:
    """
    画一张 size×size 的图标，返回逐行逐像素的 RGBA 二维表（左上角为原点）。
    """
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    radius = size * 0.22
    # 圆角矩形 + 竖直渐变
    for y in range(size):
        t = y / max(1, size - 1)
        base = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(size):
            if not _inside_round_rect(x, y, size, radius):
                continue
            px[y][x] = (base[0], base[1], base[2], 255)

    # 六根柱子
    margin = size * 0.16
    usable = size - margin * 2
    gap = usable * 0.10
    bar_w = (usable - gap * 5) / 6.0
    base_y = size - margin
    for i in range(6):
        x0 = margin + i * (bar_w + gap)
        h = usable * BAR_HEIGHTS[i] * 0.92
        y0 = base_y - h
        col = BARS[i]
        for y in range(int(round(y0)), int(round(base_y))):
            for x in range(int(round(x0)), int(round(x0 + bar_w))):
                if 0 <= x < size and 0 <= y < size and px[y][x][3]:
                    px[y][x] = (col[0], col[1], col[2], 255)

    # 底下一条白色基线，视觉上把六根柱子"焊"在一起
    line_h = max(1, int(round(size * 0.035)))
    for y in range(int(round(base_y)), int(round(base_y)) + line_h):
        for x in range(int(round(margin)), int(round(margin + usable))):
            if 0 <= x < size and 0 <= y < size and px[y][x][3]:
                px[y][x] = (255, 255, 255, 255)
    return px


def _inside_round_rect(x: float, y: float, size: float, r: float) -> bool:
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r + 0.5


def _bmp_bits(px: List[List[Tuple[int, int, int, int]]]) -> bytes:
    """RGBA 表 → ICO 里的 32bpp BGRA 像素数据（**自下而上**，带 alpha）。"""
    size = len(px)
    out = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            r, g, b, a = px[y][x]
            out += bytes((b, g, r, a))
    return bytes(out)


def build_ico(path: str, sizes=ICON_SIZES) -> str:
    """写出一个多尺寸 .ico。"""
    entries = []
    images = []
    for s in sizes:
        px = render_rgba(s)
        xor = _bmp_bits(px)
        # AND 掩码：1bpp，每行补齐到 4 字节；32bpp 有 alpha 时全 0 即可
        and_row = ((s + 31) // 32) * 4
        and_mask = bytes(and_row * s)
        header = struct.pack("<IiiHHIIiiII",
                             40, s, s * 2, 1, 32, 0, len(xor) + len(and_mask),
                             0, 0, 0, 0)
        images.append(header + xor + and_mask)

    # ICONDIR
    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, img in zip(sizes, images):
        w = 0 if s >= 256 else s
        bpp = 32
        out += struct.pack("<BBBBHHII", w, w, 0, 0, 1, bpp, len(img), offset)
        offset += len(img)
    for img in images:
        out += img

    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


# =====================================================================
#  PNG 写出
# =====================================================================
def write_png(path: str, width: int, height: int, rgb_rows: List[bytes]) -> str:
    """rgb_rows: 每行 width*3 字节的 RGB 数据（已按自上而下排列）。"""
    raw = b"".join(b"\x00" + row for row in rgb_rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8bit truecolor
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(blob)
    return path


# =====================================================================
#  窗口截图（Windows）
# =====================================================================
def grab_window(window, path: str) -> str:
    """
    截取窗口（含标题栏）。

    :param window: Tk 窗口对象（用 winfo_id()）或直接的 HWND 整数
    """
    if not sys.platform.startswith("win"):
        raise RuntimeError("截图目前只在 Windows 上实现")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    # 截屏进程自己要先声明 DPI 感知，否则坐标系不匹配、图会被裁掉
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass

    hwnd = window.winfo_id() if hasattr(window, "winfo_id") else int(window)
    root_hwnd = user32.GetAncestor(hwnd, 2)          # GA_ROOT
    if root_hwnd:
        hwnd = root_hwnd

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("取窗口尺寸失败")
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"窗口尺寸异常：{w}×{h}（窗口是不是被最小化了？）")

    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memdc, 2):     # 2 = PW_RENDERFULLCONTENT
            raise RuntimeError("PrintWindow 失败")
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth = w
        bih.biHeight = -h          # 负高度本身就是自上而下，不要再翻转
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(memdc, bitmap, 0, h, buf, ctypes.byref(bih), 0)
        if not got:
            raise RuntimeError("GetDIBits 失败")
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)

    # BGRA → 每像素 **3 字节** 的 RGB。写成 4 字节会造成整图斜纹噪声。
    raw = bytes(buf)
    rows: List[bytes] = []
    for y in range(h):
        off = y * w * 4
        line = bytearray(w * 3)
        line[0::3] = raw[off:off + w * 4:4]
        line[1::3] = raw[off + 1:off + w * 4:4]
        line[2::3] = raw[off + 2:off + w * 4:4]
        rows.append(bytes(line))
    return write_png(path, w, h, rows)


def find_window_by_title(title: str) -> Optional[int]:
    """按标题关键词找顶层窗口（给命令行截图用）。"""
    if not sys.platform.startswith("win"):
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: List[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title.lower() in buf.value.lower():
                    found.append(hwnd)
        return True

    user32.EnumWindows(cb, None)
    return found[0] if found else None


# =====================================================================
#  自检
# =====================================================================
def _selftest() -> int:                                    # pragma: no cover
    import tempfile
    bad = 0

    def check(name, cond, detail=""):
        nonlocal bad
        print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
        if not cond:
            bad += 1

    px = render_rgba(256)
    check("画布尺寸正确", len(px) == 256 and len(px[0]) == 256)
    check("四角透明（圆角生效）", px[0][0][3] == 0 and px[0][255][3] == 0)
    check("中心不透明", px[128][128][3] == 255)
    check("底部基线是白色", px[256 - int(256 * 0.16) + 1][128] == (255, 255, 255, 255),
          str(px[256 - int(256 * 0.16) + 1][128]))
    check("六根柱子颜色各不相同",
          len({BARS[i] for i in range(6)}) == 6)

    tmp = tempfile.mkdtemp(prefix="y75_icon_")
    ico = os.path.join(tmp, "t.ico")
    build_ico(ico)
    size = os.path.getsize(ico)
    check("ico 已生成且大小合理", size > 5000, f"{size} 字节")
    with open(ico, "rb") as fh:
        head = fh.read(6)
    reserved, kind, count = struct.unpack("<HHH", head)
    check("ico 头正确", reserved == 0 and kind == 1 and count == len(ICON_SIZES),
          f"kind={kind} count={count}")

    png = os.path.join(tmp, "t.png")
    rows = [bytes([255, 0, 0] * 4) for _ in range(4)]
    write_png(png, 4, 4, rows)
    with open(png, "rb") as fh:
        sig = fh.read(8)
    check("png 签名正确", sig == b"\x89PNG\r\n\x1a\n")
    check("png 非空", os.path.getsize(png) > 50, f"{os.path.getsize(png)} 字节")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n===== 图标/截图工具自检：{'全部通过' if bad == 0 else f'{bad} 项失败'} =====")
    return 0 if bad == 0 else 1


def main(argv: List[str]) -> int:                          # pragma: no cover
    ap = argparse.ArgumentParser(description="生成 .ico 图标 / 截取窗口 PNG")
    ap.add_argument("--ico", default="", help="输出 .ico 路径")
    ap.add_argument("--shot", default="", help="输出 .png 路径（截取窗口）")
    ap.add_argument("--title", default="Y75", help="要截图的窗口标题关键词")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.ico:
        print("已生成图标：", build_ico(args.ico))
    if args.shot:
        hwnd = find_window_by_title(args.title)
        if not hwnd:
            print(f"没找到标题含「{args.title}」的窗口")
            return 2
        print("已截图：", grab_window(hwnd, args.shot))
    if not args.ico and not args.shot:
        print(__doc__)
    return 0


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main(sys.argv[1:]))
