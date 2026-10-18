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
import socket
import subprocess
import sys
import threading
import time
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from y75_protocol import (  # noqa: E402
    ASC_FRAME_LEN, ASC_SENTINEL, ByteOrder, CMD_QUERY_MATRIX, EOF, SOF, TcpTransport,
    Y75Client, Y75Error, Y75Timeout, build_cmd, dump_matrix, fmt_value,
    parse_matrix_text,
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
    section("3. 连续推流")
    cli.stop()
    time.sleep(0.1)
    base = cli.push_count
    cli.start()
    check("开始连续发送：收到 ACK 后立刻有帧", wait_push(cli, base, 1.0))

    n0 = cli.push_count
    t0 = time.time()
    time.sleep(1.0)
    dt = time.time() - t0
    rate = (cli.push_count - n0) / dt
    check("推流速率接近 500 Hz", 250 <= rate <= 800, f"实测 {rate:.0f} Hz")

    # 数值必须随时间变化，否则波形是直线
    seen = [v for _, v in list(cli.wave)[-40:]]
    distinct = len({tuple(round(x, 5) for x in s) for s in seen})
    check("推流数值逐帧变化（不是同一份数据重发）", distinct >= 10,
          f"{len(seen)} 帧里有 {distinct} 种取值")

    check("波形缓冲有上限（不会无限增长）",
          len(cli.wave) <= cli.WAVE_MAX, f"{len(cli.wave)} ≤ {cli.WAVE_MAX}")

    cnt0, snap = cli.take_wave()
    check("take_wave 首次有数据", bool(snap), f"{len(snap)} 点")
    _, snap2 = cli.take_wave()
    check("take_wave 无新数据时返回空（界面不必重绘）", snap2 == [])

    check("推流期间 latest 一直有值",
          all(v is not None for v in cli.latest), str(cli.latest and cli.latest[0]))

    cli.stop()
    time.sleep(0.25)
    n1 = cli.push_count
    time.sleep(0.4)
    check("停止后不再有帧", cli.push_count == n1, f"{cli.push_count} == {n1}")


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
            check("畸形帧混流下推流仍能正常接收", got > 200, f"1 秒内收到 {got} 帧")
            check("畸形帧被识别并计数", cli.short_frames > 100, f"short={cli.short_frames}")
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


def test_disconnect(cli: Y75Client, host: str, port: int) -> None:
    section("10. 断线与清理")
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
        test_freq_and_format(cli)
        test_matrix(cli)
        test_zero(cli)
        test_sticky_frames(host, port)
        test_bad_frames(host, port)
        test_by_order(host, port)
        test_short_frames(host, port)
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
