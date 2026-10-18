# -*- coding: utf-8 -*-
"""
y75_sniff.py —— 总线嗅探器：把串口线上真实在传的每一帧列出来。

回答的就是这类问题：
    「我在串口助手里看到 AA 55 02 0D 0A 一闪而过，它到底是不是一帧？」
    「设备发的数据很快，为什么上位机跟不上？」

它会按协议把字节流拆成帧，逐个报出**原始字节、命令码、载荷长度、是否合法**，
最后给一份统计：帧率、帧间隔、各命令码计数、异常帧明细。

用法：
    :: 串口（真机）—— 只读，不发任何命令
    python y75_sniff.py --port COM28 --baud 460800 --secs 10

    :: 先让设备开始连续推流，再嗅探
    python y75_sniff.py --port COM28 --start --secs 10

    :: 不接硬件也能试（连模拟器）
    python y75_sniff.py --tcp 15020 --secs 10

    :: 逐帧打印（默认只打印异常帧 + 统计）
    python y75_sniff.py --port COM28 --all
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from y75_protocol import (  # noqa: E402
    FrameAssembler, SOF, build_cmd, CMD_START, EOF, frame_text, hexs,
    looks_like_text, resp_kind)

TEXT_GAP = 0.12          # 裸文本的静默判定（和客户端一致）
READ_CHUNK = 512


def _label(cmd: int) -> str:
    """命令码在**响应方向**上的含义（和发起方向不同）。"""
    if cmd == 0x02:
        return "连续推流数据帧"
    if cmd == 0x03:
        return "单次采集数据帧"
    return frame_text(cmd)


def _setup_stdout() -> None:
    """Windows 控制台默认是 GBK，直接输出 UTF-8 中文会乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class Sniffer:
    """被动拆帧 + 统计。不认识的东西一律计入异常，绝不猜测。"""

    def __init__(self, show_all: bool = False, show_bad: int = 20,
                 raw_limit: int = 40):
        self.asm = FrameAssembler()
        self.show_all = show_all
        self.show_bad = show_bad
        self.raw_limit = raw_limit

        self.n_frame = 0
        self.n_bytes = 0
        self.n_garbage = 0
        self.t_first: Optional[float] = None
        self.t_last: Optional[float] = None
        self.gaps: List[float] = []
        self.by_cmd: Dict[int, Dict] = {}
        self.bad: List[Tuple[float, str, bytes]] = []
        self.texts: List[Tuple[float, bytes]] = []
        self._last_data = 0.0
        self.t0 = time.monotonic()

    # ------------------------------------------------------------------
    def feed(self, data: bytes, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.n_bytes += len(data)
        self._last_data = now
        self.asm.feed(data)
        self._drain(now)

    def idle(self, now: Optional[float] = None) -> None:
        """总线静默时调用：收尾裸文本、凑半帧。"""
        now = time.monotonic() if now is None else now
        self._drain(now)

    # ------------------------------------------------------------------
    def _drain(self, now: float) -> None:
        while True:
            buf = self.asm._buf
            if not buf:
                return
            # 1) 以 AA 55 开头 → 按协议拆帧
            if self.asm.head() == SOF:
                f = self.asm.pull_frame()
                if f is None:
                    if self.asm.moved:
                        continue
                    return
                self._count(f, now)
                continue
            # 2) 裸文本（序列号 / 矩阵回复，没有帧头帧尾）
            chunk = self.asm.peek_text()
            if chunk and looks_like_text(chunk):
                self.asm.drop(len(chunk))
                self.texts.append((now, bytes(chunk)))
                continue
            # 3) 其余都是垃圾字节
            if buf[0] == 0xAA and len(buf) == 1:
                return                       # 半截帧头，等下一批
            self.asm.drop(1)
            self.n_garbage += 1

    # ------------------------------------------------------------------
    def _count(self, f, now: float) -> None:
        self.n_frame += 1
        if self.t_first is None:
            self.t_first = now
        if self.t_last is not None:
            self.gaps.append(now - self.t_last)
        self.t_last = now

        _kind, expect = resp_kind(f.cmd)
        actual = len(f.payload)
        rec = self.by_cmd.setdefault(
            f.cmd, {"n": 0, "lens": {}, "expect": expect, "bad": 0})
        rec["n"] += 1
        rec["lens"][actual] = rec["lens"].get(actual, 0) + 1

        reason = ""
        if expect is not None and actual != expect:
            rec["bad"] += 1
            reason = (f"期望 {expect} 字节载荷，实际 {actual} 字节"
                      + ("（只有帧头帧尾，没有数据段）" if actual == 0 else ""))
        if reason:
            self.bad.append((now - self.t0, reason, f.raw))
        if self.show_all or reason:
            print(f"  [{now - self.t0:8.3f}s] {hexs(f.raw, self.raw_limit)}"
                  f"   ({len(f.raw)} 字节, {_label(f.cmd)})"
                  f"{'   ← ' + reason if reason else ''}", flush=True)

    # ------------------------------------------------------------------
    def report(self, target: str) -> str:
        out: List[str] = []
        span = (self.t_last - self.t_first) if self.t_first and self.t_last else 0.0
        out.append("")
        out.append("=" * 78)
        out.append(f"总线嗅探报告   {target}")
        out.append("=" * 78)
        out.append(f"  收到 {self.n_bytes} 字节 / {self.n_frame} 帧    "
                   f"跨度 {span:.2f}s   平均 {self.n_frame / span if span else 0:.1f} 帧/秒")
        if self.gaps:
            g = sorted(self.gaps)
            q = lambda p: g[min(len(g) - 1, int(len(g) * p))]
            out.append(f"  帧间隔  平均 {sum(g) / len(g) * 1000:.3f}ms   "
                       f"P50 {q(0.5) * 1000:.3f}   P95 {q(0.95) * 1000:.3f}   "
                       f"最大 {g[-1] * 1000:.3f}ms")
        if self.n_garbage:
            out.append(f"  丢弃乱码字节：{self.n_garbage}"
                       f"（{self.n_garbage / max(1, self.n_bytes) * 100:.2f}% —— "
                       f"不为 0 说明线路有干扰、或波特率/接线不对）")
        out.append("")
        out.append("  帧类型明细：")
        for cmd in sorted(self.by_cmd):
            r = self.by_cmd[cmd]
            lens = "  ".join(f"{L}({n})" for L, n in sorted(r["lens"].items()))
            exp = r["expect"] if r["expect"] is not None else "不限"
            flag = "   ← 有异常" if r["bad"] else ""
            out.append(f"    0x{cmd:02X}  {_label(cmd)}  |  "
                       f"期望载荷 {exp}  |  实际 {lens}  |  共 {r['n']} 个{flag}")
        if self.texts:
            out.append("")
            out.append(f"  裸 ASCII 回复 {len(self.texts)} 段"
                       f"（序列号 / 矩阵，无帧头帧尾）：")
            for t, raw in self.texts[:5]:
                out.append(f"    [{t:7.3f}s] {raw[:80]!r}")
        if self.bad:
            out.append("")
            out.append(f"  异常帧明细（共 {len(self.bad)} 个，显示前 "
                       f"{min(len(self.bad), self.show_bad)} 个）：")
            for t, why, raw in self.bad[:self.show_bad]:
                out.append(f"    [{t:7.3f}s] {hexs(raw, self.raw_limit):<44} {why}")
            if len(self.bad) > self.show_bad:
                out.append(f"    …… 其余 {len(self.bad) - self.show_bad} 个略")
        else:
            out.append("")
            out.append("  没有发现长度异常的帧 —— 总线上所有帧都是完整的。")
        out.append("=" * 78)
        return "\n".join(out)


# =====================================================================
def main() -> int:
    _setup_stdout()
    ap = argparse.ArgumentParser(description="Y75 总线嗅探器（只读）")
    ap.add_argument("--port", help="串口号，如 COM28")
    ap.add_argument("--baud", type=int, default=460800)
    ap.add_argument("--tcp", type=int, help="改为连 TCP 端口（对模拟器，如 15020）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--secs", type=float, default=10.0, help="抓多少秒（默认 10）")
    ap.add_argument("--start", action="store_true",
                    help="先发一条「开始连续推流」命令（会往总线上写数据）")
    ap.add_argument("--all", action="store_true", help="逐帧打印原始字节")
    ap.add_argument("--bad", type=int, default=20, help="最多列几条异常帧明细")
    ap.add_argument("--raw-limit", type=int, default=40, help="HEX 打印的最大字节数")
    args = ap.parse_args()

    if not args.port and not args.tcp:
        print("请用 --port COMx（真机）或 --tcp PORT（模拟器）指定目标")
        print("不知道串口号？跑：python y75_serial.py --list")
        return 2

    sn = Sniffer(show_all=args.all, show_bad=args.bad, raw_limit=args.raw_limit)
    closer = None
    if args.tcp:
        sock = socket.create_connection((args.host, args.tcp), timeout=3.0)
        sock.settimeout(0.05)
        target = f"TCP {args.host}:{args.tcp}"

        def read(_n=READ_CHUNK) -> bytes:
            try:
                return sock.recv(READ_CHUNK)
            except socket.timeout:
                return b""

        def write(d: bytes) -> None:
            sock.sendall(d)

        def closer():
            sock.close()
    else:
        from y75_serial import SerialPort
        sp = SerialPort(args.port, args.baud)
        target = f"{args.port} @ {args.baud}"

        def read(_n=READ_CHUNK) -> bytes:
            return sp.recv()

        def write(d: bytes) -> None:
            sp.write(d)

        def closer():
            sp.close()

    print(f"嗅探 {target}，抓 {args.secs:g} 秒"
          + ("（先发一条开始连续推流）" if args.start else "（只读，不发命令）"))
    if args.start:
        try:
            write(build_cmd(CMD_START))
            time.sleep(0.2)
        except Exception as exc:                     # noqa: BLE001
            print(f"发送「开始连续」失败：{exc}")

    t0 = time.monotonic()
    last_report = 0.0
    try:
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= args.secs:
                break
            data = read()
            if data:
                sn.feed(data)
            else:
                sn.idle()
            if elapsed - last_report >= 2.0 and not args.all:
                last_report = elapsed
                print(f"  … 已抓 {elapsed:.0f}s：{sn.n_frame} 帧"
                      f"（异常 {len(sn.bad)}）", flush=True)
    except KeyboardInterrupt:
        print("\n用户中断，出报告：")
    finally:
        if closer:
            closer()

    print(sn.report(target))
    if sn.bad:
        print("\n结论：总线上**确实存在长度异常的帧**（上面列了明细）。")
        print("      上位机已能识别并跳过它们，但建议排查线缆/接地/波特率，")
        print("      或确认设备固件是否在某些状态下会发这种短帧。")
    elif sn.n_frame == 0:
        print("\n结论：一个帧都没抓到。检查串口号、波特率、A/B 是否接反，")
        print("      以及设备是否已经在推流（可加 --start）。")
    else:
        print("\n结论：所有帧长度都正常。如果界面上仍卡，问题不在数据本身，")
        print("      跑 python _bench_smooth.py --burst-ms 16 对比一下渲染侧。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
