# -*- coding: utf-8 -*-
"""
test_link.py —— 协议层回归测试（不需要硬件）

用法：
    先起模拟器（另开一个窗口）：
        python y75_simulator.py --port 15120
    再跑本脚本：
        python test_link.py --port 15120

也可以让本脚本自己起模拟器（进程内、随机选空闲端口，最省事）：
        python test_link.py --auto

覆盖点：
  * 帧编解码 / 字节序
  * 连接、命令收发、超时
  * 连续推流（帧率、数值随时间变化、缓冲不泄漏）
  * 停止→开始：波形时间基准不许漂（时间戳不冲进未来、节拍不被断流带偏）
  * 单次采集
  * 序列号 / 版本 / 状态（含裸 ASCII 回复）
  * 频率、单位、输出模式（HEX ↔ ASC）
  * 6×6 矩阵读写
  * 置零（异步）
  * 粘包：一次读到多帧
  * 断线清理
"""

from __future__ import annotations

import argparse
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from y75_protocol import (  # noqa: E402
    ASC_FRAME_LEN, ASC_SENTINEL, ByteOrder, BusGate, CMD_QUERY_MATRIX, CMD_START,
    CMD_STOP, EOF, SOF, TcpTransport, Y75Client, Y75Error, Y75Timeout, build_cmd,
    dump_matrix, fmt_value, hexs, parse_matrix_text,
)

PASS = FAIL = 0
FAILED_NAMES: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


# =====================================================================
#  辅助：连到模拟器并等它进入干净状态
# =====================================================================
def make_client(host: str, port: int, order: str = ByteOrder.DEFAULT) -> Y75Client:
    cli = Y75Client(TcpTransport(host, port, timeout=3.0), byte_order=order)
    cli.connect()
    assert wait_until(lambda: cli.is_connected(), 2.0), "连接失败"
    try:
        cli.stop()                      # 上一轮可能还在推流
    except Y75Error:
        pass
    return cli


def wait_until(cond, timeout: float = 2.0, interval: float = 0.01) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def wait_push(cli: Y75Client, base: int, timeout: float = 2.0) -> bool:
    return wait_until(lambda: cli.push_count > base, timeout)


# =====================================================================
#  测试项
# =====================================================================
def test_connect_and_basics(cli: Y75Client) -> None:
    section("1. 连接与基础命令")
    check("已连接", cli.is_connected(), cli.transport.name)

    cli.stop()
    time.sleep(0.1)
    st = cli.query_status()
    check("查询状态：发送模式=已停止", st["send_mode_text"] == "已停止", str(st["send_mode_text"]))
    check("查询状态：单位是 mV/kg/N 之一",
          st["data_format"] in (0, 1, 2), str(st["data_format_text"]))
    check("查询状态：频率是 500 或 1000", st["freq_hz"] in (500, 1000), f"{st['freq_hz']}Hz")
    check("查询状态：输出模式是 HEX", st["debug_mode_text"] == "HEX 标准帧",
          str(st["debug_mode_text"]))

    ver = cli.query_ver()
    check("查询版本：格式 Vx.y.z", ver.startswith("V") and ver.count(".") == 2, ver)

    sn = cli.query_sn()
    check("查询序列号：拿到非空 ASCII", bool(sn) and sn.isprintable(), repr(sn))
    check("查询序列号：与设备一致（HS- 开头）", sn.startswith("HS-"), sn)

    # 0x0E 查单位：最低位 1=kg 0=N，与 0x07 的 data_format 应当一致
    st2 = cli.query_status()
    unit = cli.query_unit()
    expect = {1: "kg", 2: "N"}.get(int(st2["data_format"]))
    if expect is None:                        # 模拟器在 mV 模式时 0x0E 回 N
        expect = "N"
    check("查询单位：与状态里的单位一致", unit == expect,
          f"0x0E={unit}, 0x07={st2['data_format_text']}")
    check("查询序列号：与设备一致（HS- 开头）", sn.startswith("HS-"), sn)


def test_single(cli: Y75Client) -> None:
    section("2. 单次采集")
    cli.stop()
    time.sleep(0.1)
    vals = cli.single()
    check("单次返回 6 个通道", len(vals) == 6, str(len(vals)))
    check("单次返回的数值全部有效",
          all(v is not None and math.isfinite(v) for v in vals),
          ", ".join(fmt_value(v) for v in vals))
    a = cli.single()
    time.sleep(0.05)
    b = cli.single()
    check("两次单次的数值不同（真的在采样）", a != b, f"{fmt_value(a[0])} vs {fmt_value(b[0])}")


def test_stream(cli: Y75Client) -> None:
    section("3. 连续采集（先 0x0F 点灯，再每 50ms 一条 0x03 轮询）")
    cli.stop()
    time.sleep(0.1)
    base = cli.push_count
    logs: List[Tuple[str, str]] = []
    prev_on_log = cli.on_log
    cli.on_log = lambda lv, m: (logs.append((lv, m)),
                                prev_on_log and prev_on_log(lv, m))
    try:
        cli.start()
    finally:
        cli.on_log = prev_on_log
    check("开始连续：轮询线程已启动", cli.is_polling())
    check("开始连续：先发送了 0x0F 心跳灯",
          any(lv == "tx" and "心跳灯" in m for lv, m in logs),
          str([m for lv, m in logs if lv == "tx"][:3]))
    check("模拟器识别 0x0F（没有出现无应答警告）",
          not any("0x0F 心跳灯无应答" in m for lv, m in logs))
    check("开始连续：1 秒内就有数据帧（实际 50ms 内）", wait_push(cli, base, 1.0))

    n0 = cli.push_count
    t0 = time.time()
    time.sleep(1.0)
    dt = time.time() - t0
    rate = (cli.push_count - n0) / dt
    check("轮询速率 ≈ 20 Hz", 14 <= rate <= 26, f"实测 {rate:.1f} Hz")

    # 已在轮询时再点一次「开始」：不得叠加第二个轮询线程
    cli.start()
    n1 = cli.push_count
    time.sleep(1.0)
    rate2 = (cli.push_count - n1) / dt
    check("重复点开始不叠加轮询（速率不翻倍）", 14 <= rate2 <= 26,
          f"实测 {rate2:.1f} Hz")

    # 数值必须随时间变化，否则波形是直线
    time.sleep(0.2)
    seen = [v for _, v in list(cli.wave)[-20:]]
    distinct = len({tuple(round(x, 5) for x in s) for s in seen})
    check("轮询数值逐帧变化（不是同一份数据重发）", distinct >= 6,
          f"{len(seen)} 帧里有 {distinct} 种取值")

    check("波形缓冲有上限（不会无限增长）",
          len(cli.wave) <= cli.WAVE_MAX, f"{len(cli.wave)} ≤ {cli.WAVE_MAX}")

    cnt0, snap = cli.take_wave()
    check("take_wave 首次有数据", bool(snap), f"{len(snap)} 点")
    _, snap2 = cli.take_wave()
    check("take_wave 无新数据时返回空（界面不必重绘）", snap2 == [])

    # 点数横轴按「个数」取：切到点数模式后，界面要的是一条定长尾巴
    time.sleep(0.05)
    _, snap3 = cli.take_wave(max_points=50)
    check("take_wave(max_points) 按个数取，且取的是最新的一段",
          len(snap3) == min(50, len(cli.wave)) and snap3[-1] == cli.wave[-1],
          f"{len(snap3)} 点，与缓冲尾部一致={bool(snap3) and snap3[-1] == cli.wave[-1]}")
    time.sleep(0.05)
    _, snap4 = cli.take_wave(999.0, max_points=30)
    check("同时给 window_sec 和 max_points 时以点数为准",
          len(snap4) == min(30, len(cli.wave)), f"{len(snap4)} 点")

    check("轮询期间 latest 一直有值",
          all(v is not None for v in cli.latest), str(cli.latest and cli.latest[0]))

    cli.stop()
    check("停止后轮询线程退出", not cli.is_polling())
    time.sleep(0.25)
    n1 = cli.push_count
    time.sleep(0.4)
    check("停止后不再有帧", cli.push_count == n1, f"{cli.push_count} == {n1}")


def test_stream_clock(cli: Y75Client) -> None:
    """
    起停几次推流，检查波形的「时间基准」有没有被断流带坏。

    回归的现象：点「停止」→「开始」之后实时波形只画一半，再来一次只剩
    四分之一。两处毛病叠在一起：

    * `_dev_t` 停在断流那一刻，重启后第一帧只要差值 <0.4s 就不重锚，
      这份滞后会被一路带着走（波形右端永远缺一截）；
    * 节拍统计窗横跨空洞，0.5s 窗里只剩重启后那几十帧，量出的 `sample_dt`
      是真实值的几倍；它是等间隔推算的步长，于是时间戳直奔未来，
      可视窗里只剩旧数据 —— 剩下的就是「一半 / 四分之一」。
    """
    section("3b. 停止→开始：波形时间基准不许漂")
    cli.stop()
    time.sleep(0.2)

    for _ in range(3):
        base = cli.push_count
        cli.start()
        wait_push(cli, base, 1.0)
        time.sleep(0.5)
        cli.stop()
        time.sleep(0.9)              # 人工点「停止」「开始」大概就是这个间隔

    base = cli.push_count
    cli.start()
    wait_push(cli, base, 1.0)
    time.sleep(1.4)                       # 20Hz 轮询：1.4s ≈ 28 帧

    lag = time.time() - cli.wave[-1][0]
    check("最新样本时间戳与本机时间对齐（没冲进未来）",
          -cli.TS_AHEAD - 0.02 <= lag <= 0.6,
          f"lag={lag * 1000:+.0f}ms（允许超前 {cli.TS_AHEAD * 1000:.0f}ms）")
    check("实测节拍没被断流空洞带偏（≈2ms）",
          0.0014 <= cli.sample_dt <= 0.0032, f"{cli.sample_dt * 1000:.3f}ms")

    # 只看「最后一次开始」之后的那一段：跨过断流点的样本中间是真空白
    # （那 0.9s 设备确实没发帧），跨度自然对不上，不是 bug。
    # 轮询 20Hz，取 25 点（≈1.2s）；等间距按 50ms 铺格子。
    _, samples = cli.take_wave(max_points=25)
    ivs = sorted(b[0] - a[0] for a, b in zip(samples, samples[1:]))
    med = ivs[len(ivs) // 2] if ivs else 0.0
    span = samples[-1][0] - samples[0][0] if samples else 0.0
    expect = (len(samples) - 1) * med if samples else 0.0
    check("重启后最近 25 点是等间距的一段（点数横轴按它铺格子）",
          len(samples) == 25 and expect > 0 and abs(span - expect) / expect < 0.08,
          f"{len(samples)} 点跨 {span:.2f}s，中位间隔 {med * 1000:.2f}ms")

    cli.stop()
    time.sleep(0.2)


def test_freq_and_format(cli: Y75Client) -> None:
    section("4. 频率 / 数据单位 / 输出模式")
    cli.set_freq(1000)
    st = cli.query_status()
    check("设置 1000Hz 生效", st["freq_hz"] == 1000, str(st["freq_hz"]))
    cli.set_freq(500)
    st = cli.query_status()
    check("设置 500Hz 生效", st["freq_hz"] == 500, str(st["freq_hz"]))

    for fmt, name in ((2, "N"), (1, "kg"), (0, "mV")):
        cli.set_format(fmt)
        st = cli.query_status()
        check(f"设置数据单位 {name} 生效", st["data_format"] == fmt,
              str(st["data_format_text"]))
    cli.set_format(2)                 # 收尾回默认

    # 0x0E 查单位跟着切：kg→01、N→00（mV 也回 N，协议只有两态）
    cli.set_format(1)
    u = cli.query_unit()
    check("0x0E 在 kg 模式回 kg", u == "kg", u)
    cli.set_format(2)
    u = cli.query_unit()
    check("0x0E 在 N 模式回 N", u == "N", u)

    # ---- ASC 裸帧模式 ----
    cli.set_asc_mode()
    check("切 ASC 后客户端标记 asc_mode", cli.asc_mode)
    base = cli.push_count
    cli.start()
    ok = wait_push(cli, base, 1.5)
    check("ASC 模式下仍能收到数据帧", ok)
    check("ASC 模式下 latest 有值",
          all(v is not None for v in cli.latest), str(fmt_value(cli.latest[0])))
    cli.stop()
    time.sleep(0.15)

    # ASC 模式下 ACK 仍是标准帧 → 命令要照样能用
    try:
        st = cli.query_status()
        check("ASC 模式下 ACK 类命令仍正常", st["debug_mode"] == 1,
              str(st["debug_mode_text"]))
    except (Y75Timeout, Y75Error) as exc:
        check("ASC 模式下 ACK 类命令仍正常", False, str(exc))

    cli.set_hex_mode()
    st = cli.query_status()
    check("切回 HEX 模式生效", st["debug_mode"] == 0, str(st["debug_mode_text"]))
    check("切回 HEX 后客户端标记清掉", not cli.asc_mode)


def test_matrix(cli: Y75Client) -> None:
    section("5. 6×6 解耦矩阵")
    mat = cli.query_matrix()
    check("读到 6×6 矩阵", len(mat) == 6 and all(len(r) == 6 for r in mat))
    check("矩阵数值都在合理范围",
          all(abs(v) < 100 for r in mat for v in r),
          f"首元素 {mat[0][0]:.6f}")

    # ---- 字节序：矩阵行是大端，和数据帧（小端）不是一个口径 ----
    # 固件按大端解 0x20 的载荷，1.0 必须是 3F 80 00 00；发成小端
    # （00 00 80 3F）设备会读成 4.6e-41，矩阵等于被写成 0。
    sent: List[bytes] = []
    tx = cli.transport
    orig_write = tx.write

    def tap(data: bytes) -> None:
        sent.append(bytes(data))
        orig_write(data)

    tx.write = tap                                    # type: ignore[method-assign]
    try:
        cli.set_matrix_row(0, (1.0, 0.5, -2.0, 0.0, 0.0, 0.0))
    finally:
        tx.write = orig_write                         # type: ignore[method-assign]
    cli.set_matrix_row(0, mat[0])                     # 恢复原值，免得影响下面
    fr = None
    for b in sent:
        if len(b) == 30 and b[:3] == b"\xAA\x55\x20":
            fr = b                                    # 重发时取最后一帧
    check("0x20 帧 = AA 55 20 [行号+6×f32] 0D 0A（30 字节）",
          fr is not None and fr[28:] == b"\x0D\x0A" and fr[3] == 0,
          hexs(fr) if fr else "没抓到 0x20 帧")
    body = fr[4:28] if fr else b""
    check("写矩阵 float32 是大端：1.0 → 3F 80 00 00",
          body[:4] == b"\x3F\x80\x00\x00", hexs(body[:4]) if body else "—")
    check("0.5 → 3F 00 00 00、-2.0 → C0 00 00 00",
          body[4:8] == b"\x3F\x00\x00\x00" and body[8:12] == b"\xC0\x00\x00\x00",
          hexs(body[4:12]) if body else "—")

    row = [0.314159, -0.271828, 0.161803, -0.057721, 0.086656, 0.007297]
    cli.set_matrix_row(4, row)
    back = cli.query_matrix()
    check("上传第 5 行后读回一致",
          all(abs(a - b) < 1e-5 for a, b in zip(back[4], row)),
          ", ".join(f"{v:.6f}" for v in back[4]))
    check("只有第 5 行被改动",
          all(abs(a - b) < 1e-5 for a, b in zip(back[0], mat[0])),
          f"第 1 行 {back[0][0]:.6f}")

    # 每行都传一遍，验证行号编码（0x20 | row → ACK 0xA0|row）
    ok_rows = 0
    for r in range(6):
        vals = [float(r) * 0.1 + c * 0.01 for c in range(6)]
        try:
            cli.set_matrix_row(r, vals)
            ok_rows += 1
        except (Y75Timeout, Y75Error):
            pass
    check("6 行逐行上传全部拿到行号 ACK", ok_rows == 6, f"{ok_rows}/6")
    back2 = cli.query_matrix()
    check("逐行上传后矩阵正确",
          all(abs(back2[r][c] - (r * 0.1 + c * 0.01)) < 1e-5
              for r in range(6) for c in range(6)),
          dump_matrix(back2).split("\n")[0])

    # 0x38 二进制逐行读：行号 1..6（01=第 1 行，与 0x20 写入的 0..5 差 1）
    row38 = cli.query_matrix_row(1)
    check("0x38 读第 1 行与 0x37 的 matrix1 一致",
          all(abs(a - b) < 1e-5 for a, b in zip(row38, back2[0])),
          f"{row38[0]:.6f} vs {back2[0][0]:.6f}")
    row38 = cli.query_matrix_row(6)
    check("0x38 读第 6 行与 0x37 的 matrix6 一致",
          all(abs(a - b) < 1e-5 for a, b in zip(row38, back2[5])),
          f"{row38[0]:.6f} vs {back2[5][0]:.6f}")
    try:
        cli.query_matrix_row(0)
        check("0x38 行号 0 被拒绝", False, "竟然没抛")
    except Y75Error:
        check("0x38 行号 0 被拒绝", True)
    try:
        cli.query_matrix_row(7)
        check("0x38 行号 7 被拒绝", False, "竟然没抛")
    except Y75Error:
        check("0x38 行号 7 被拒绝", True)

    cli.set_matrix_row(0, mat[0])      # 恢复第 1 行
    bad = 0
    for r, vals in ((6, [0.0] * 6), (0, [0.0] * 5)):
        try:
            cli.set_matrix_row(r, vals)
        except Y75Error:
            bad += 1
    check("非法行号/长度在上位机侧就被拦下", bad == 2, f"{bad}/2")


def test_zero(cli: Y75Client) -> None:
    section("6. 置零 / 取消置零")
    cli.stop()
    try:
        cli.zero()
        check("置零命令拿到 ACK（异步开始）", True)
    except (Y75Timeout, Y75Error) as exc:
        check("置零命令拿到 ACK（异步开始）", False, str(exc))
    try:
        cli.unzero()
        check("取消置零拿到 ACK", True)
    except (Y75Timeout, Y75Error) as exc:
        check("取消置零拿到 ACK", False, str(exc))


def test_sticky_frames(host: str, port: int) -> None:
    """一帧里塞多条命令 —— 设备必须逐条处理（真机的粘包能力）。"""
    section("7. 粘包：一次写多条命令")
    s = socket.create_connection((host, port), timeout=2.0)
    try:
        blob = build_cmd(0x07) * 3          # 三条查询状态
        s.sendall(blob)
        time.sleep(0.3)
        s.settimeout(1.0)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        got = data.count(b"\xAA\x55\x87")
        check("一帧塞 3 条命令收到 3 个响应", got == 3, f"收到 {got} 个")
    finally:
        s.close()


def test_bad_frames(host: str, port: int) -> None:
    """乱码 / 半帧不能让连接崩掉。"""
    section("8. 脏数据与半帧容错")
    s = socket.create_connection((host, port), timeout=2.0)
    try:
        s.sendall(b"\x00\x11\x22\x33" + build_cmd(0x07)[:3])
        time.sleep(0.2)
        s.sendall(build_cmd(0x07)[3:])
        s.settimeout(1.0)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        check("脏字节 + 半帧切分后仍能正确响应", b"\xAA\x55\x87" in data,
              f"收到 {len(data)} 字节")
    finally:
        s.close()


def test_short_frames(host: str, port: int) -> None:
    """
    总线里混入空载荷畸形帧（AA 55 02 0D 0A）时，数据链路必须照常工作。

    这是真机上真实存在的形态：驱动把帧截断了，或固件在某些状态下发短帧。
    早期版本会把每个畸形帧当成「长度对不上」，丢 1 字节重同步、再逐帧记一条
    warn 日志 —— 实测把界面从 35fps 拖到 23fps、CPU 从 58% 顶到 79%。
    """
    section("9. 空载荷畸形帧（总线干扰）的鲁棒性")
    sim_port = port + 137
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "y75_simulator.py"),
         "--port", str(sim_port), "--short-frames", "1", "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.3)
        cli = Y75Client(TcpTransport(host, sim_port, timeout=1.0))
        cli.connect()
        try:
            cli.start()
            time.sleep(1.5)
            base = cli.push_count
            time.sleep(1.0)
            got = cli.push_count - base
            check("畸形帧混流下轮询仍能正常接收", got >= 12, f"1 秒内收到 {got} 帧")
            check("畸形帧被识别并计数", cli.short_frames >= 10, f"short={cli.short_frames}")
            check("畸形帧不触发拆帧重同步", cli.frame_errors == 0,
                  f"resync={cli.frame_errors}")
            check("有效数据不被当成乱码丢掉", cli.garbage_bytes == 0,
                  f"garbage={cli.garbage_bytes}")
            vals = cli.latest
            check("数值仍然有效（不是全 0 / NaN）",
                  any(v is not None and abs(v) > 1e-9 for v in vals), str(vals))
            cli.stop()
        finally:
            cli.disconnect()
    finally:
        proc.kill()
        proc.wait()


class _RecTransport(TcpTransport):
    """
    记录每次 write 的时刻，以及「写之前最近一次收到字节」的时刻。

    用来验证出站字节确实走了总线仲裁：写下去的那一刻，距最近一次收到
    字节必须已经静默够久（说明当前那一帧收完了），而不是帧发到一半。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.writes: List[Tuple[float, bytes, float]] = []
        self.last_rx_t = 0.0

    def recv(self, timeout: float) -> bytes:
        data = super().recv(timeout)
        if data:
            self.last_rx_t = time.time()
        return data

    def write(self, data: bytes) -> None:
        self.writes.append((time.time(), bytes(data), self.last_rx_t))
        super().write(data)


def test_bus_gate(host: str, port: int) -> None:
    """
    半双工 485 的「发言权」：推流中发命令不能撞车。

    真机现象：MAX13487 是自动方向芯片，主机没法控制总线方向。设备在连续
    推流时如果上位机把命令拍下去，两个驱动器会同时拉总线 —— 逻辑分析仪上
    能看到收发重叠，设备侧拆帧错乱（严重时进 HardFault），上位机这边则是
    命令没应答。

    这里用模拟器的 --collide 模式复现同一件事：设备「正在线上发字节」的
    那 0.63ms 窗口内收到的命令一律丢弃、并往流里插垃圾字节。于是
    「关掉仲裁会撞车、打开仲裁不撞车」就成了可量化的断言。
    """
    section("11. 半双工总线仲裁（推流中不撞车）")

    # ---- 11a 纯逻辑：阈值与放行条件 ----
    g = BusGate()
    g.sample_dt = 0.002
    check("500Hz 帧后窗口 ≈ 0.7ms（0.35 个帧周期）",
          abs(g.window_s() * 1000.0 - 0.7) < 0.06, f"{g.window_s() * 1000:.3f} ms")
    g.sample_dt = 0.001
    check("1000Hz 帧后窗口 ≈ 0.35ms", abs(g.window_s() * 1000.0 - 0.35) < 0.06,
          f"{g.window_s() * 1000:.3f} ms")
    check("发送后保护窗口 = 2 个帧周期",
          abs(g.guard_s() * 1000.0 - 2.0) < 0.15, f"{g.guard_s() * 1000:.3f} ms")
    check("静默判据 = 4 个帧周期（下限 4ms）",
          abs(g.idle_s() * 1000.0 - 4.0) < 0.2, f"{g.idle_s() * 1000:.2f} ms")

    g = BusGate()
    g.enabled = False
    t0 = time.time()
    g.wait_idle(lambda: True)
    check("关掉仲裁后立刻放行（回到老行为）", (time.time() - t0) * 1000.0 < 1.0)

    # 有帧流时：必须被「帧收完」这个事件唤醒，而不是睡到某个固定时刻。
    # reaction = 从「收完整帧」到 wait_idle 返回的时间差 —— 越小越好，
    # 因为帧间空隙只有 1.37ms，反应慢就直接踩进下一帧。
    g = BusGate()
    g.sample_dt = 0.002

    class _Feeder(threading.Thread):
        def __init__(self, gate, period=0.002):
            super().__init__(daemon=True)
            self.gate, self.period = gate, period
            self.stop_ev = threading.Event()
            self.last = 0.0

        def run(self):
            while not self.stop_ev.is_set():
                self.gate.note_frame(data=True)      # 数据帧才产生「可发」边界
                self.last = time.time()
                time.sleep(self.period)

    f = _Feeder(g)
    f.start()
    time.sleep(0.03)
    waits, reacts = [], []
    for _ in range(15):
        t0 = time.time()
        g.wait_idle(lambda: False)
        waits.append((time.time() - t0) * 1000.0)
        reacts.append((time.time() - f.last) * 1000.0)
        time.sleep(0.0037)                  # 故意错开相位，避免每次都撞在同一相位
    f.stop_ev.set()

    check("有帧流时每次都在 1 个帧周期左右拿到空隙",
          max(waits) <= 3.0, f"最长 {max(waits):.2f} ms / 帧周期 2.00 ms")
    check("拿到空隙的反应延迟足够小（≤0.5ms，否则会踩进下一帧）",
          sorted(reacts)[len(reacts) // 2] <= 0.5,
          f"中位 {sorted(reacts)[len(reacts) // 2]:.3f} ms，最大 {max(reacts):.3f} ms")
    check("干净帧流下不会走到 blind / timeout",
          g.blind == 0 and g.timeouts == 0,
          f"blind={g.blind} timeout={g.timeouts}")

    # 只收到字节、攒不成整帧（半帧）→ 不能发，等够 3 帧认了
    g = BusGate()
    g.sample_dt = 0.002
    g.note_rx()
    t0 = time.time()
    g.wait_idle(lambda: True)
    dt = (time.time() - t0) * 1000.0
    check("帧没收完就不发：等到 3 帧才认（计入 blind）",
          4.0 <= dt <= 14.0 and g.blind == 1, f"等了 {dt:.1f} ms, blind={g.blind}")

    # 自己刚发过 → guard 内不发
    g = BusGate()
    g.sample_dt = 0.002
    f = _Feeder(g)
    f.start()
    time.sleep(0.01)
    g.note_tx()
    t0 = time.time()
    g.wait_idle(lambda: False)
    dt = (time.time() - t0) * 1000.0
    f.stop_ev.set()
    check("自己刚发过时先等够 guard（2 个帧周期）再发", dt >= 3.0, f"等了 {dt:.2f} ms")

    # 总线长时间静默 → 立刻放行
    g = BusGate()
    g.note_frame(time.time() - 0.1)
    g.note_rx(time.time() - 0.1)
    t0 = time.time()
    g.wait_idle(lambda: True)
    dt = (time.time() - t0) * 1000.0
    check("总线静默超过 4 个帧周期时立刻放行（设备没在推流）", dt < 2.0,
          f"等了 {dt:.2f} ms")

    # ---- 11b 实测：会撞车的模拟器 ----
    sim_port = port + 149
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "y75_simulator.py"),
         "--port", str(sim_port), "--collide", "--rate-hz", "250", "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tr = None
    try:
        if not wait_port(host, sim_port, 6.0):
            check("撞车模拟器启动", False, "起不来")
            return
        tr = _RecTransport(host, sim_port, timeout=1.0)
        cli = Y75Client(tr)
        cli.connect()
        try:
            check("默认开启总线仲裁", cli.bus.enabled)
            # 轮询模式下主机不再发 0x02，设备不会自行推流；撞车场景改用
            # 裸写把模拟器推进推流态（等价于老固件残留 / 「自动启动」标志）。
            cli._bus_write(build_cmd(CMD_START))
            time.sleep(1.0)
            dt_ms = cli.sample_dt * 1000.0
            cli.bus.reset()                 # 只统计下面这 8 条命令

            # (1) 仲裁开着：推流中连发 8 条命令，应当全部拿到应答
            ok = 0
            for _ in range(8):
                try:
                    cli.query_ver(timeout=0.30)
                    ok += 1
                except (Y75Timeout, Y75Error):
                    pass
            gb = cli.garbage_bytes
            # 这是实时性断言，不是逻辑断言：Windows 线程调度本身就有几百微秒
            # 的抖动，而 250Hz 下的帧间空隙是 3.4ms。允许 8 条里有 1 条被调度
            # 抖动耽误（那一条会在后面的「关闭仲裁」对照里更明显地暴露出来）。
            check("推流中 8 条命令基本都能拿到应答（≤1 条被抖动耽误）", ok >= 7,
                  f"{ok}/8")
            check("总线上基本没有撞车垃圾字节（≤2 字节）", gb <= 2,
                  f"garbage={gb} 字节")
            check("每次都找到了干净的帧边界（没走到 blind / timeout）",
                  cli.bus.blind == 0 and cli.bus.timeouts == 0,
                  f"blind={cli.bus.blind} timeout={cli.bus.timeouts}")

            # 写下去的那一刻，离最近一次收到字节有多远？
            gaps = [(t - rx) * 1000.0 for t, _, rx in tr.writes[-8:] if rx > 0.0]
            if gaps:
                check("写入紧跟在「一帧收完」之后（≤1.2 个帧周期，含调度抖动）",
                      max(gaps) <= dt_ms * 1.2,
                      f"最大 {max(gaps):.2f} ms / 帧周期 {dt_ms:.2f} ms")
            else:
                check("写入紧跟在「一帧收完」之后", False, "没记录到接收时刻")

            # (2) 关掉仲裁：同样的命令应当开始撞车
            cli.bus.enabled = False
            gb0 = cli.garbage_bytes
            bad = 0
            for _ in range(12):
                # 仲裁关掉后，命令就在「用户随手点一下」的时刻拍下去 ——
                # 相对推流帧的相位是随机的，正中设备发送窗口的概率约 30%
                time.sleep(random.uniform(0.0, cli.sample_dt))
                try:
                    cli.query_ver(timeout=0.25)
                except (Y75Timeout, Y75Error):
                    bad += 1
            gb1 = cli.garbage_bytes
            check("关掉仲裁后撞车明显变多（证明上面的「没撞」不是碰巧）",
                  bad >= 3 and bad > (8 - ok),
                  f"关闭：失败 {bad}/12、垃圾 +{gb1 - gb0} 字节；"
                  f"开启：失败 {8 - ok}/8、垃圾 {gb} 字节")

            # (3) 推流中「停止」：这才是用户遇到的场景
            cli.bus.enabled = True
            cli.stop()
            n0 = cli.push_count
            time.sleep(0.35)
            check("推流中「停止」能落地（设备真的停下来了）",
                  cli.push_count - n0 <= 2,
                  f"停止后 0.35s 内又来了 {cli.push_count - n0} 帧")
        finally:
            cli.disconnect()
    finally:
        if proc is not None:
            proc.kill()
            proc.wait()


def test_disconnect(cli: Y75Client, host: str, port: int) -> None:
    section("12. 断线与清理")
    cli.disconnect()
    check("断开后 is_connected=False", not cli.is_connected())
    try:
        cli.start()
        check("断开后发命令应当报错", False, "竟然没报错")
    except Y75Error:
        check("断开后发命令报错清楚", True)
    except Exception as exc:                        # pragma: no cover
        check("断开后发命令报错清楚", False, f"抛的是 {type(exc).__name__}")

    # 连一个不存在的端口
    dead = Y75Client(TcpTransport(host, 65000, timeout=0.4))
    try:
        dead.connect()
        check("连接不存在的端口应当失败", False, "竟然连上了")
    except OSError:
        check("连接不存在的端口抛 OSError", True)
    except Exception as exc:                        # pragma: no cover
        check("连接不存在的端口抛 OSError", False, f"抛的是 {type(exc).__name__}")
    finally:
        dead.disconnect()


def test_by_order(host: str, port: int) -> None:
    """
    字节序选项必须真的生效，而且设错了要能被一眼看出来。

    注意：这一节的前半段**故意用已知浮点构造载荷**，不依赖在线数据。
    早期的写法拿模拟器的实时噪声值去断言「错误字节序必然解出 >1e6 的量级」，
    这个命题本身不成立 —— 传感器值接近 0 时（例如 0.02），指数位集中在一个
    字节里，换序后仍是个很小的数（如 -1.1e-41），断言随机失败。
    """
    section("10. 字节序选项生效性（纯逻辑 + 在线）")

    # ---- 10a 纯逻辑：已知浮点 → 组帧 → 反解，只有正确顺序能还原 ----
    # 用相对容差：float32 在 1e30 这种量级上的表示误差本身就有 1e7 左右，
    # 写死绝对值容差会把「正确还原」误判成失败。
    def close(a: float, b: float) -> bool:
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))

    truth = (12.3456, -7.5, 100.0, 0.5, -1234.5, 1e30)
    for order in ByteOrder.NAMES:
        back = ByteOrder.decode6(ByteOrder.encode6(truth, order), order)
        check(f"{order} 编码后能原样解回",
              all(close(a, b) for a, b in zip(back, truth)),
              fmt_value(back[0]))

    raw = ByteOrder.encode6(truth, "DCBA")
    wrong = ByteOrder.decode6(raw, "ABCD")
    check("DCBA 的载荷按 ABCD 解会得到完全不同的值",
          any(abs(a - b) > 1.0 for a, b in zip(wrong, truth)),
          f"真值 {fmt_value(truth[0])} → 按 ABCD 解出 {fmt_value(wrong[0])}")
    check("错误字节序在正常量级的通道上会出现离谱数值（可提示用户改设置）",
          any(abs(v) > 1e6 for v in wrong),
          f"最大绝对值 {max(abs(v) for v in wrong):.3g}")

    # ---- 10b 在线：同一台设备，两种字节序解出的值不同 ----
    c1 = make_client(host, port, "DCBA")
    v1 = c1.single()
    c1.disconnect()
    c2 = make_client(host, port, "ABCD")
    v2 = c2.single()
    c2.disconnect()
    check("在线换字节序后解出来的值不同", v1 != v2,
          f"DCBA→{fmt_value(v1[0])}  ABCD→{fmt_value(v2[0])}")


# =====================================================================
#  入口
# =====================================================================
def wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection((host, port), timeout=0.3)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Y75 协议层回归测试")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15120)
    ap.add_argument("--auto", action="store_true",
                    help="自己拉起模拟器（进程内子进程，随机端口）")
    args = ap.parse_args()

    host, port = args.host, args.port
    proc = None
    if args.auto:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "y75_simulator.py"),
             "--host", host, "--port", str(port), "--quiet"],
            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if not wait_port(host, port, 6.0):
            print("模拟器起不来，退出")
            proc.kill()
            return 2
        print(f"（已自动拉起模拟器，端口 {port}）")

    if not wait_port(host, port, 2.0):
        print(f"连不上 {host}:{port}。请先启动模拟器：\n"
              f"    python y75_simulator.py --port {port}\n"
              f"或直接：python test_link.py --auto")
        return 2

    print("=" * 66)
    print(f"Y75 协议层回归测试   目标 {host}:{port}")
    print("=" * 66)

    cli = make_client(host, port)
    try:
        test_connect_and_basics(cli)
        test_single(cli)
        test_stream(cli)
        test_stream_clock(cli)
        test_freq_and_format(cli)
        test_matrix(cli)
        test_zero(cli)
        test_sticky_frames(host, port)
        test_bad_frames(host, port)
        test_by_order(host, port)
        test_short_frames(host, port)
        test_bus_gate(host, port)
        test_disconnect(cli, host, port)
    except Exception as exc:                         # pragma: no cover
        import traceback
        traceback.print_exc()
        check(f"测试过程未抛异常（{type(exc).__name__}）", False, str(exc))
    finally:
        cli.disconnect()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    print("\n" + "=" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED_NAMES:
        print("失败项：")
        for n in FAILED_NAMES:
            print(f"  - {n}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
