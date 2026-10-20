# -*- coding: utf-8 -*-
"""
y75_simulator.py —— Y75 六维力传感器 **设备模拟器**（纯标准库）

作用：
    没有真机（或真机不在手边）时，让上位机可以完整跑通、可以回归测试。
    同时它也是 Y75 协议的**参照实现** —— 把「设备到底怎么回」写死在一处，
    免得散布在测试脚本里。

与真机的差别：
    真机走 RS485（USART1，默认 460800 8N1），模拟器走 TCP（默认 127.0.0.1:15020）。
    协议字节流完全一致，所以上位机只换一个 Transport 就能对接。

用法：
    python y75_simulator.py                       # 127.0.0.1:15020
    python y75_simulator.py --port 15021 --freq 1000
    python y75_simulator.py --order ABCD          # 换一种 float 字节序
    python y75_simulator.py --matrix-gap 0.02     # 6 行矩阵分开送，测文本拼接
    python y75_simulator.py --silent 0x37         # 指定命令不回复，测超时路径
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from y75_protocol import (
    ASC_SENTINEL, BAUD_CODES, ByteOrder, CMD_ASC_SEND, CMD_HEX_SEND,
    CMD_QUERY_MATRIX, CMD_QUERY_SN, CMD_QUERY_STATUS, CMD_QUERY_VER, CMD_SET_BAUD,
    CMD_SET_FREQ, CMD_SET_KG, CMD_SET_MATRIX, CMD_SET_MV, CMD_SET_N, CMD_SINGLE,
    CMD_START, CMD_STOP, CMD_UNZERO, CMD_ZERO, EOF, RESP_FLAG, SOF, dump_matrix,
    frame_text, hexs,
)

#: 六路出厂级信号：幅度、频率、相位、偏置（模拟一点真实的耦合与漂移）
SIGNAL = (
    (5.00, 0.7, 0.00, 0.20),
    (3.20, 1.1, 0.80, -0.15),
    (8.00, 0.4, 1.70, 0.35),
    (0.55, 1.9, 0.30, 0.02),
    (0.42, 2.3, 1.10, -0.03),
    (0.31, 2.9, 2.40, 0.01),
)

DEFAULT_SN = "HS-01234567"
DEFAULT_FW = 0x00010702            # V1.7.2
#: 畸形帧：只有 AA 55 [02] 和帧尾 0D 0A，**没有 24 字节载荷**。
#: 真机上偶尔能抓到（干扰截断、或固件在某些状态下发的短帧），
#: 上位机必须能识别并跳过，绝不能被它拖住。
SHORT_FRAME = bytes((0xAA, 0x55, 0x02, 0x0D, 0x0A))
DEFAULT_MATRIX = [[0.298418, -0.211604, 0.015321, -0.004102, 0.007733, 0.001204],
                  [-0.189923, 0.305117, -0.010845, 0.006311, -0.002871, 0.003902],
                  [0.011204, -0.009933, 0.287651, -0.013882, 0.004455, -0.002116],
                  [0.000921, 0.001877, -0.012205, 0.198433, -0.003317, 0.000944],
                  [-0.001588, 0.002245, 0.004917, -0.002088, 0.196720, 0.001576],
                  [0.000743, -0.001326, -0.002055, 0.001194, -0.001662, 0.201338]]


class Device:
    """一台虚拟 Y75。所有状态变更都在锁里做，推流线程与命令线程共用。"""

    def __init__(self, order: str = ByteOrder.DEFAULT, freq: int = 500,
                 noise: float = 0.02, matrix_gap: float = 0.0,
                 silent: Optional[set] = None, no_ack: Optional[set] = None,
                 verbose: bool = True, burst_ms: float = 0.0,
                 short_every: int = 0, rate_hz: float = 0.0,
                 collide: bool = False, baud: int = 460800):
        self.lock = threading.RLock()
        self.order = order
        self.matrix_gap = max(0.0, matrix_gap)
        self.noise = max(0.0, noise)
        self.silent = silent or set()      # 完全不回应的命令（测超时）
        self.no_ack = no_ack or set()      # 不回 ACK（仍做动作，测裸发）
        self.verbose = verbose
        #: 模拟 USB 串口驱动的「批量上报」：攒够这么多毫秒的帧再一次性发出。
        #: 真实驱动（CH340/FTDI/CP210x）默认就是 8~16ms 才把字节交给应用层，
        #: 0 表示逐帧发（走 TCP 时的理想情况）。
        self.burst_ms = max(0.0, burst_ms)
        #: 每 N 个推流帧后面插一个「空载荷」的畸形帧 AA 55 02 0D 0A，
        #: 用来验证上位机对总线畸形帧的鲁棒性（0 = 不插）。
        self.short_every = max(0, int(short_every))
        #: 强制推流节拍（Hz）。真机推流跑在固件主循环里，实测常落在 300~500Hz，
        #: 并不是协议里写的 500/1000 两档；用它复现「节拍不是整数档」的真机条件。
        self.rate_hz = max(0.0, float(rate_hz))

        # ---- 半双工总线冲突模拟（--collide）----
        #: 真机上 MAX13487 是自动方向芯片，设备正在发推流帧时如果主机的命令
        #: 也上总线，两个驱动器一起拉 → 双方都收到垃圾，设备侧整帧丢弃、
        #: 自然也不会回 ACK。打开这个开关，模拟器就按「线上实际占用时间」
        #: 判定冲突，用来验证上位机的总线仲裁到底有没有用。
        self.collide = bool(collide)
        self.baud = max(1200, int(baud))
        #: 本设备在线上"发"这一批字节的起止时刻（monotonic）
        self._tx_busy_from = 0.0
        self.tx_busy_until = 0.0
        self.collisions = 0                # 撞车次数（命令被丢弃）
        self.cmd_total = 0                 # 收到的命令总数
        #: 模拟「线上发完才到应用层」的延迟交付队列（只有 --collide 用）
        self._tx_q: List[Tuple[float, bytes]] = []
        self._tx_cv = threading.Condition()

        # ---- 运行状态（对应 g_sys） ----
        self.send_mode = 0                 # 0 停 / 1 连续 / 2 单次
        self.data_format = 2               # 断电重启后从 EEPROM 读；默认 N
        self.freq_mode = 0 if int(freq) == 500 else 1
        self.debug_mode = 0                # 0 = HEX 帧，1 = ASC 裸帧
        self.zero_busy = 0
        self.comm_error = 0
        self.status_flags = 0              # BIT0 自动启动, BIT1 运行中
        self.zero_offset = [0.0] * 6

        self.sn = DEFAULT_SN
        self.fw_version = DEFAULT_FW
        self.matrix = [row[:] for row in DEFAULT_MATRIX]

        # ---- 采样 ----
        self.t0 = time.monotonic()
        self.seq = 0
        self._clients: List[socket.socket] = []
        self._stop = threading.Event()
        if self.collide:
            # 必须在 _stop 建好之后才能起泵线程（循环里要用它判断退出）
            threading.Thread(target=self._tx_pump, name="tx-wire",
                             daemon=True).start()

    # ------------------------------------------------------------------
    #  采样与推流
    # ------------------------------------------------------------------
    def sample(self, elapsed: Optional[float] = None) -> Tuple[float, ...]:
        """
        按给定时刻算一组六维力值。

        推流时**必须逐帧用不同的时刻重算**，否则整段推流发的是同一份数值，
        波形会变成一条直线、导出的表格每行一模一样。
        传 None 表示「就按现在算」。
        """
        t = (time.monotonic() - self.t0) if elapsed is None else float(elapsed)
        scale = 1.0
        if self.data_format == 0:
            scale = 200.0                 # mV：原始信号放大
        elif self.data_format == 1:
            scale = 1.0 / 9.8             # kg
        with self.lock:
            offs = list(self.zero_offset)
        out = []
        for i, (amp, f, ph, bias) in enumerate(SIGNAL):
            v = amp * math.sin(2.0 * math.pi * f * t + ph)
            v += bias
            if self.noise:
                v += random.uniform(-self.noise, self.noise)
            out.append(round(v * scale - offs[i], 6))
        return tuple(out)

    def data_payload(self, elapsed: Optional[float] = None) -> bytes:
        return ByteOrder.encode6(self.sample(elapsed), self.order)

    def push_frame(self, cmd: int, elapsed: Optional[float] = None) -> bytes:
        """按 debug_mode 组一帧数据：HEX 标准帧 或 ASC 裸帧。"""
        if self.debug_mode == 0:
            return SOF + bytes((cmd,)) + self.data_payload(elapsed) + EOF
        return self.data_payload(elapsed) + ASC_SENTINEL

    def ack(self, cmd: int, data: bytes = b"") -> bytes:
        return SOF + bytes(((cmd | RESP_FLAG) & 0xFF,)) + data + EOF

    # ------------------------------------------------------------------
    #  命令分派（严格照抄 rs485_cmd.c::rs485_cmd_dispatch 的行为）
    # ------------------------------------------------------------------
    def handle(self, cmd: int) -> Optional[List[bytes]]:
        """
        返回要发回去的字节块列表；None 表示这条命令不回复。
        列表可能有多个元素（矩阵是 6 次独立发送）。
        """
        if cmd in self.silent:
            self.log(f"收到 0x{cmd:02X} {frame_text(cmd)} → 静默（--silent 设定）")
            return None

        def with_ack(body: List[bytes]) -> List[bytes]:
            if cmd in self.no_ack:
                return body
            return [self.ack(cmd)] + body

        # ---- 0x01 停止连续发送 ----
        if cmd == CMD_STOP:
            with self.lock:
                self.send_mode = 0
                self.status_flags &= ~0x0002
            self.log("收到 0x01 停止连续发送 → 回 ACK")
            return with_ack([])

        # ---- 0x02 开始连续发送 ----
        if cmd == CMD_START:
            with self.lock:
                self.send_mode = 1
                self.status_flags |= 0x0002
            self.log("收到 0x02 开始连续发送 → 回 ACK，随后按 "
                     f"{1000 if self.freq_mode else 500}Hz 推流")
            return with_ack([])

        # ---- 0x03 单次采集：直接回一帧数据，没有 ACK ----
        if cmd == CMD_SINGLE:
            with self.lock:
                self.send_mode = 2
            self.log("收到 0x03 单次采集 → 直接回一帧数据帧")
            return [self.push_frame(CMD_SINGLE)]

        # ---- 0x04 设置波特率（载荷由 handle_payload 处理，这里只管空载荷） ----
        if cmd == CMD_SET_BAUD:
            self.comm_error = (self.comm_error + 1) & 0xFFFF
            self.log("0x04 缺少载荷 → 不回 ACK，错误计数 +1")
            return None

        # ---- 0x05 查序列号：裸 ASCII ----
        if cmd == CMD_QUERY_SN:
            self.log(f"收到 0x05 查序列号 → 回裸 ASCII {self.sn!r}")
            return [self.sn.encode("ascii")]

        # ---- 0x06 查版本：3 字节 ----
        if cmd == CMD_QUERY_VER:
            v = self.fw_version
            data = bytes(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
            self.log(f"收到 0x06 查版本 → {data[0]}.{data[1]}.{data[2]}")
            return [self.ack(cmd, data)]

        # ---- 0x07 查状态：8 字节 ----
        if cmd == CMD_QUERY_STATUS:
            with self.lock:
                data = bytes((self.send_mode, self.data_format, self.freq_mode,
                              self.debug_mode,
                              self.status_flags & 0xFF, (self.status_flags >> 8) & 0xFF,
                              self.comm_error & 0xFF, (self.comm_error >> 8) & 0xFF))
            self.log("收到 0x07 查状态 → " + hexs(data))
            return [self.ack(cmd, data)]

        # ---- 0x08 设置频率（空载荷 → 当成非法） ----
        if cmd == CMD_SET_FREQ:
            self.comm_error = (self.comm_error + 1) & 0xFFFF
            self.log("0x08 缺少载荷 → 不回 ACK，错误计数 +1")
            return None

        # ---- 0x20 上传矩阵行（空载荷 → 当成非法） ----
        if cmd == CMD_SET_MATRIX:
            self.comm_error = (self.comm_error + 1) & 0xFFFF
            self.log("0x20 缺少载荷 → 不回 ACK，错误计数 +1")
            return None

        # ---- 0x30 置零 ----
        if cmd == CMD_ZERO:
            with self.lock:
                self.zero_busy = 1
            baseline = self.sample()
            threading.Timer(0.4, self._finish_zero, args=(baseline,)).start()
            self.log("收到 0x30 置零 → 回 ACK，0.4s 后完成 20 点平均")
            return with_ack([])

        # ---- 0x31 / 0x32 输出模式 ----
        if cmd == CMD_HEX_SEND:
            with self.lock:
                self.debug_mode = 0
            self.log("收到 0x31 → 切换为 HEX 标准帧输出")
            return with_ack([])
        if cmd == CMD_ASC_SEND:
            with self.lock:
                self.debug_mode = 1
            self.log("收到 0x32 → 切换为 ASC 裸帧输出（28 字节 + 哨兵）")
            return with_ack([])

        # ---- 0x33 / 0x34 / 0x35 数据单位 ----
        if cmd in (CMD_SET_MV, CMD_SET_KG, CMD_SET_N):
            fmt = {CMD_SET_MV: 0, CMD_SET_KG: 1, CMD_SET_N: 2}[cmd]
            with self.lock:
                self.data_format = fmt
            persist = "（已写 EEPROM，掉电保持）" if cmd in (CMD_SET_KG, CMD_SET_N) else "（不保存）"
            self.log(f"收到 0x{cmd:02X} → 数据单位改为 {['mV', 'kg', 'N'][fmt]} {persist}")
            return with_ack([])

        # ---- 0x36 取消置零 ----
        if cmd == CMD_UNZERO:
            with self.lock:
                self.zero_offset = [0.0] * 6
                self.zero_busy = 0
            self.log("收到 0x36 取消置零 → 回 ACK")
            return with_ack([])

        # ---- 0x37 查矩阵：6 行裸 ASCII ----
        if cmd == CMD_QUERY_MATRIX:
            self.log("收到 0x37 查矩阵 → 回 6 行裸 ASCII")
            lines = dump_matrix(self.matrix).split("\n")
            return [(ln + "\r\n").encode("ascii") for ln in lines]

        self.comm_error = (self.comm_error + 1) & 0xFFFF
        self.log(f"收到未知命令 0x{cmd:02X} → 只累加错误计数，不回复")
        return None

    def _finish_zero(self, baseline: Tuple[float, ...]) -> None:
        """置零收尾：把这批采样值累计进零点偏置，之后输出就归零了。"""
        with self.lock:
            self.zero_offset = [self.zero_offset[i] + baseline[i] for i in range(6)]
            self.zero_busy = 0
        self.log("置零完成（zero_busy → 0），零点偏置已更新")

    # ------------------------------------------------------------------
    #  带载荷的命令（0x04 / 0x08 / 0x20 需要在解析完载荷后再处理）
    # ------------------------------------------------------------------
    def handle_payload(self, cmd: int, payload: bytes) -> Optional[List[bytes]]:
        if cmd == CMD_SET_BAUD:
            code = payload[0] if payload else 0
            baud = BAUD_CODES.get(code)
            if baud is None:
                self.comm_error = (self.comm_error + 1) & 0xFFFF
                self.log(f"0x04 波特率码 0x{code:02X} 非法 → 不回 ACK，错误计数 +1")
                return None
            self.log(f"0x04 设置波特率 → {baud} bps，先按旧波特率回 ACK")
            return None if cmd in self.no_ack else [self.ack(cmd)]

        if cmd == CMD_SET_FREQ:
            mode = 0 if (payload and payload[0] == 0) else 1
            with self.lock:
                self.freq_mode = mode
            self.log(f"0x08 设置频率 → {1000 if mode else 500} Hz")
            return None if cmd in self.no_ack else [self.ack(cmd)]

        if cmd == CMD_SET_MATRIX:
            if len(payload) < 25:
                self.comm_error = (self.comm_error + 1) & 0xFFFF
                self.log(f"0x20 载荷只有 {len(payload)} 字节（需 25）→ 不处理")
                return None
            row = payload[0]
            if row > 5:
                self.comm_error = (self.comm_error + 1) & 0xFFFF
                self.log(f"0x20 行号 {row} 越界 → 不处理")
                return None
            vals = ByteOrder.decode6(payload[1:25], self.order)
            with self.lock:
                self.matrix[row] = list(vals)
            self.log(f"0x20 上传矩阵第 {row + 1} 行 → " +
                     ", ".join(f"{v:.6f}" for v in vals))
            # 真机回的是 ack(cmd | row)，行号带在命令码里
            if cmd in self.no_ack:
                return None
            return [self.ack(cmd | row)]

        return self.handle(cmd)

    # ------------------------------------------------------------------
    #  客户端与推流循环
    # ------------------------------------------------------------------
    def add_client(self, sock: socket.socket) -> None:
        with self.lock:
            self._clients.append(sock)

    def remove_client(self, sock: socket.socket) -> None:
        with self.lock:
            if sock in self._clients:
                self._clients.remove(sock)

    def broadcast(self, chunks: List[bytes]) -> None:
        blob = b"".join(chunks)
        if not blob:
            return
        if not self.collide:
            self._send_all(blob)
            return
        # 线上占用窗口 = [现在, 现在 + 传输时间]，10 bit/字节
        # （1 起始 + 8 数据 + 1 停止），无校验。
        # 字节**推迟到窗口结束才交付**：UART 边发边转发，最后一个字节到达
        # 应用层时线上正好发完。不能在这里 sleep —— 那会把推流节拍拖崩，
        # 反而造出「帧帧背靠背」的假象。交给 _tx_pump 按到期时间发。
        wire = len(blob) * 10.0 / self.baud
        now = time.monotonic()
        start = max(now, self.tx_busy_until)
        self._tx_busy_from = start
        self.tx_busy_until = start + wire
        with self._tx_cv:
            self._tx_q.append((start + wire, blob))
            self._tx_cv.notify()

    def _tx_pump(self) -> None:
        """按「线上发完」的时刻把字节交出去（模拟 UART 的边发边转发）。"""
        while not self._stop.is_set():
            with self._tx_cv:
                if not self._tx_q:
                    self._tx_cv.wait(0.002)
                    continue
                due, blob = self._tx_q[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._tx_cv.wait(wait)
                    continue
                self._tx_q.pop(0)
            self._send_all(blob)

    def _send_all(self, blob: bytes) -> None:
        with self.lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(blob)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)

    def push_loop(self) -> None:
        """
        连续推流：真机是在 TIM2 中断里按 500/1000Hz 发帧，这里用独立线程模拟。

        用「计划时间 + 追赶」而不是「睡一帧发一帧」：
        Windows 的 sleep 粒度默认约 15.6ms，直接按 2ms 睡会掉到几十 Hz。
        追赶模型下即使系统调度抖动，发送节奏和每帧的时间戳依然是对的。
        """
        next_t: Optional[float] = None
        pend: List[bytes] = []            # 攒着等成批发出（模拟串口驱动的批量上报）
        flush_at = 0.0
        while not self._stop.is_set():
            with self.lock:
                running = (self.send_mode == 1)
                # --rate-hz 优先：真机的节拍由主循环决定，不一定正好 500/1000Hz
                interval = (1.0 / self.rate_hz if self.rate_hz > 0.0 else
                            1.0 / (1000.0 if self.freq_mode else 500.0))
                burst = self.burst_ms / 1000.0

            if not running:
                next_t = None
                if pend:                  # 停机时把攒下的发出去，不丢数据
                    self.broadcast(pend)
                    pend.clear()
                flush_at = 0.0
                self._stop.wait(0.02)
                continue

            now = time.monotonic()
            if next_t is None or next_t < now - 0.5:      # 刚开始 / 落后太多就重排
                next_t = now

            if next_t > now:
                self._stop.wait(min(0.002, next_t - now))
                continue

            n = 0
            while next_t <= now and n < 200:              # 单轮最多补 200 帧，防雪崩
                self.seq += 1
                pend.append(self.push_frame(CMD_START, next_t - self.t0))
                if self.short_every and self.seq % self.short_every == 0:
                    pend.append(SHORT_FRAME)              # 插入畸形帧
                next_t += interval
                n += 1

            if burst <= 0.0:
                if pend:                                      # 零延迟：逐帧发
                    self.broadcast(pend)
                    pend.clear()
            else:
                if flush_at == 0.0:
                    flush_at = now + burst
                elif now >= flush_at:
                    if pend:                                  # 攒够一个「USB 帧」再交上去
                        self.broadcast(pend)
                        pend.clear()
                    while flush_at <= now:                    # 保持批量周期的相位
                        flush_at += burst
            if n == 0:
                self._stop.wait(0.002)

    def serve_client(self, sock: socket.socket) -> None:
        sock.settimeout(0.2)
        buf = bytearray()
        peer = sock.getpeername()
        self.log(f"── 客户端接入 {peer[0]}:{peer[1]}")
        self.add_client(sock)
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                self._consume(sock, buf)
        finally:
            self.remove_client(sock)
            try:
                sock.close()
            except OSError:
                pass
            self.log(f"── 客户端断开 {peer[0]}:{peer[1]}")

    def _consume(self, sock: socket.socket, buf: bytearray) -> None:
        """按 AA 55 ... 0D 0A 切出命令帧；支持一次收到多条命令（粘包）。"""
        while True:
            start = buf.find(SOF)
            if start < 0:
                if len(buf) > 1:
                    del buf[:len(buf) - 1]
                return
            if start > 0:
                del buf[:start]
            end = bytes(buf).find(EOF, 3)
            if end < 0:
                if len(buf) > 300:
                    del buf[:len(buf) - 3]
                return
            frame = bytes(buf[:end + 2])
            del buf[:end + 2]
            cmd = frame[2]
            payload = frame[3:end]
            self.cmd_total += 1

            # ---- 半双工撞车：命令正好落在设备往线上发字节的那一段 ----
            if (self.collide
                    and self._tx_busy_from <= time.monotonic() <= self.tx_busy_until):
                self.collisions += 1
                self.log(f"!! {hexs(frame)} 撞车（设备正在发送），命令被丢弃")
                # 两边驱动器对着拉，主机收到的也会是垃圾字节
                try:
                    sock.sendall(b"\xA5\x5A")
                except OSError:
                    return
                continue

            self.log(f"← {hexs(frame)}   [{frame_text(cmd)}]")
            try:
                chunks = self.handle_payload(cmd, payload)
            except Exception as exc:                     # pragma: no cover
                self.log(f"!! 处理 {frame_text(cmd)} 出错：{exc}")
                continue
            if not chunks:
                continue
            for i, ch in enumerate(chunks):
                # 回复同样占线路：真机是单发射器，ACK 会挤进帧间空隙，
                # 而不是「凭空出现在总线上」
                self.broadcast([ch])
                if self.matrix_gap and cmd == CMD_QUERY_MATRIX and i < len(chunks) - 1:
                    time.sleep(self.matrix_gap)           # 人为拉开，测上位机文本拼接

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def stop(self) -> None:
        self._stop.set()


# =====================================================================
#  命令行
# =====================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Y75 六维力传感器 RS485 协议设备模拟器（TCP 版）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=15020, help="监听端口（默认 15020）")
    ap.add_argument("--freq", type=int, choices=(500, 1000), default=500,
                    help="开机默认推流频率（默认 500）")
    ap.add_argument("--order", default=ByteOrder.DEFAULT, choices=ByteOrder.NAMES,
                    help=f"数据帧 float 字节序（默认 {ByteOrder.DEFAULT}）")
    ap.add_argument("--noise", type=float, default=0.02, help="叠加噪声幅度（默认 0.02）")
    ap.add_argument("--matrix-gap", type=float, default=0.0,
                    help="矩阵 6 行之间的间隔秒数，用来测上位机跨批次拼文本（默认 0）")
    ap.add_argument("--silent", default="",
                    help="完全不理会的命令码，逗号分隔，如 0x37,0x05（测超时路径）")
    ap.add_argument("--no-ack", default="",
                    help="只做事不回 ACK 的命令码，逗号分隔（测裸发路径）")
    ap.add_argument("--quiet", action="store_true", help="不打印每条报文")
    ap.add_argument("--burst-ms", type=float, default=0.0,
                    help="模拟 USB 串口驱动的批量上报：攒够 N 毫秒的帧再一次性发出"
                         "（CH340/FTDI 默认约 16ms；0=逐帧发）")
    ap.add_argument("--short-frames", type=int, default=0, metavar="N",
                    help="每 N 个推流帧后插一个畸形帧 AA 55 02 0D 0A（无载荷），"
                         "用来验证上位机的鲁棒性（0=不插）")
    ap.add_argument("--rate-hz", type=float, default=0.0, metavar="HZ",
                    help="强制推流节拍（Hz），覆盖 --freq。真机推流跑在固件主循环里，"
                         "实测常在 330Hz 左右而不是 500Hz，用它可以复现真机的节拍")
    ap.add_argument("--selftest", action="store_true", help="跑内部自检后退出")
    ap.add_argument("--collide", action="store_true",
                    help="模拟半双工总线冲突：设备正在往线上发字节时收到的命令"
                         "一律丢弃（不回 ACK）并往流里插垃圾字节 —— 用来验证"
                         "上位机的「发送前等总线空闲」到底有没有用")
    ap.add_argument("--baud", type=int, default=460800,
                    help="--collide 判定「线上占用时间」用的波特率（默认 460800）")
    return ap


def _parse_cmds(text: str) -> set:
    out = set()
    for piece in text.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        out.add(int(piece, 0) & 0xFF)
    return out


def _raise_timer_resolution() -> None:
    """Windows 的等待粒度默认约 15.6ms，会把 1000Hz 推流压成几十 Hz。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except Exception:
        pass


def run(args) -> int:
    _raise_timer_resolution()
    dev = Device(order=args.order, freq=args.freq, noise=args.noise,
                 matrix_gap=args.matrix_gap,
                 silent=_parse_cmds(args.silent), no_ack=_parse_cmds(args.no_ack),
                 verbose=not args.quiet, burst_ms=args.burst_ms,
                 short_every=args.short_frames, rate_hz=args.rate_hz,
                 collide=args.collide, baud=args.baud)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(4)
    srv.settimeout(0.3)
    dev.log(f"设备模拟器已启动：{args.host}:{args.port}  "
            f"字节序={args.order}  默认频率={args.freq}Hz  噪声={args.noise}"
            + (f"  批量上报={args.burst_ms:g}ms" if args.burst_ms > 0 else "")
            + (f"  畸形帧=每{args.short_frames}帧插1个" if args.short_frames else ""))
    if args.collide:
        dev.log(f"⚠ 总线冲突模拟已开启：设备发送窗口内收到的命令一律丢弃"
                f"（按 {args.baud} 波特率折算线上占用时间）")
    dev.log("协议：AA 55 [CMD] [PAYLOAD] 0D 0A   —— 真机走 RS485，"
            "这里走 TCP，字节流一致")

    push = threading.Thread(target=dev.push_loop, name="push", daemon=True)
    push.start()
    workers: List[threading.Thread] = []
    try:
        while True:
            try:
                sock, _ = srv.accept()
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break
            t = threading.Thread(target=dev.serve_client, args=(sock,), daemon=True)
            t.start()
            workers.append(t)
    except KeyboardInterrupt:
        dev.log("收到 Ctrl+C，退出")
    finally:
        dev.stop()
        try:
            srv.close()
        except OSError:
            pass
    return 0


# =====================================================================
#  自检：不起网络，直接调 Device 验证协议行为
# =====================================================================
def _selftest() -> int:                                  # pragma: no cover
    bad = 0

    def check(name, cond, detail=""):
        nonlocal bad
        print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
        if not cond:
            bad += 1

    dev = Device(verbose=False, noise=0.0)
    check("0x05 回裸序列号", dev.handle(0x05) == [DEFAULT_SN.encode("ascii")])
    check("0x06 回 3 字节版本",
          dev.handle(0x06)[0] == b"\xAA\x55\x86\x01\x07\x02\x0D\x0A",
          hexs(dev.handle(0x06)[0]))
    st = dev.handle(0x07)[0]
    check("0x07 回 8 字节状态",
          st[:3] == b"\xAA\x55\x87" and len(st) == 5 + 8, hexs(st))
    check("0x07 载荷长度正确", len(st[3:11]) == 8)
    check("0x02 先 ACK 再启动", dev.handle(0x02) == [b"\xAA\x55\x82\x0D\x0A"])
    check("0x02 后 send_mode=1", dev.send_mode == 1)
    check("0x01 停止并 ACK", dev.handle(0x01) == [b"\xAA\x55\x81\x0D\x0A"] and
          dev.send_mode == 0)

    # 单次：只有一帧数据，没有 ACK
    chunks = dev.handle(0x03)
    check("0x03 只回一帧且无 ACK", len(chunks) == 1, f"{len(chunks)} 块")
    fr = chunks[0]
    check("0x03 帧格式正确", fr[:3] == b"\xAA\x55\x03" and fr[-2:] == EOF and
          len(fr) == 29, f"{len(fr)} 字节")
    check("0x03 载荷可解出 6 个有限值",
          all(abs(v) < 1e6 for v in ByteOrder.decode6(fr[3:27])))

    # 频率 / 单位
    dev.handle_payload(0x08, b"\x01")
    check("0x08 切 1000Hz", dev.freq_mode == 1 and dev.handle(0x07)[0][5] == 1)
    dev.handle(0x33)
    check("0x33 单位 kg", dev.data_format == 1)
    dev.handle(0x35)
    check("0x35 单位 N（持久化）", dev.data_format == 2)
    dev.handle(0x34)
    check("0x34 单位 mV", dev.data_format == 0)

    # 波特率
    check("0x04 合法码回 ACK",
          dev.handle_payload(0x04, b"\x01") == [b"\xAA\x55\x84\x0D\x0A"])
    before = dev.comm_error
    check("0x04 非法码不回 ACK 且计错",
          dev.handle_payload(0x04, b"\x7F") is None and dev.comm_error == before + 1)

    # 矩阵
    chunks = dev.handle(0x37)
    check("0x37 回 6 行", len(chunks) == 6, f"{len(chunks)} 行")
    check("0x37 每行以 CRLF 结尾", all(c.endswith(b"\r\n") for c in chunks))
    check("0x37 行首是 matrixN:", all(c.startswith(f"matrix{i+1}:".encode())
                                     for i, c in enumerate(chunks)),
          chunks[0][:16].decode())
    row5 = [5.0, 5.1, 5.2, 5.3, 5.4, 5.5]
    ack = dev.handle_payload(0x20, bytes((5,)) + ByteOrder.encode6(row5))
    check("0x20 行 ACK 带行号", ack == [b"\xAA\x55\xA5\x0D\x0A"], hexs(ack[0]))
    check("0x20 矩阵已更新（float32 精度内）",
          all(abs(a - b) < 1e-6 for a, b in zip(dev.matrix[5], row5)),
          str(dev.matrix[5]))
    check("0x20 载荷不足时拒绝", dev.handle_payload(0x20, b"\x00\x01") is None)

    # ASC 模式
    dev.handle(0x32)
    raw = dev.handle(0x03)[0]
    check("ASC 模式数据帧 28 字节且带哨兵",
          len(raw) == 28 and raw[24:] == ASC_SENTINEL, f"{len(raw)} 字节")
    dev.handle(0x31)
    check("0x31 切回 HEX 帧",
          dev.handle(0x03)[0][:3] == b"\xAA\x55\x03")

    # 置零：异步
    dev.zero_offset = [0.0] * 6
    dev.handle(0x30)
    check("0x30 置零后 busy=1", dev.zero_busy == 1)
    time.sleep(0.55)
    check("0x30 置零 0.4s 后自动完成", dev.zero_busy == 0)

    # 未知命令只计错
    before = dev.comm_error
    check("未知命令不回复且计错",
          dev.handle(0xEE) is None and dev.comm_error == before + 1)

    # 静默 / 无 ACK 开关
    dev2 = Device(verbose=False, silent={0x37}, no_ack={0x01})
    check("--silent 命令完全不回", dev2.handle(0x37) is None)
    dev2.send_mode = 1
    check("--no-ack 命令只做事不回",
          not dev2.handle(0x01) and dev2.send_mode == 0, f"send_mode={dev2.send_mode}")

    # 推流帧随时间变化（波形不能是直线）
    dev3 = Device(verbose=False, noise=0.0)
    a = ByteOrder.decode6(dev3.data_payload(0.00))
    b = ByteOrder.decode6(dev3.data_payload(0.06))
    check("不同时刻的两帧数值不同（不是直线）", a != b, f"{a[0]} vs {b[0]}")

    # 字节序开关真的生效，且解出来是同一组值
    d_a = Device(order="ABCD", verbose=False, noise=0.0)
    d_b = Device(order="DCBA", verbose=False, noise=0.0)
    check("--order 改变线上字节",
          d_a.data_payload(0.25) != d_b.data_payload(0.25))
    check("--order 只改排列不改数值",
          ByteOrder.decode6(d_a.data_payload(0.25), "ABCD") ==
          ByteOrder.decode6(d_b.data_payload(0.25), "DCBA"))

    print(f"\n===== 模拟器自检：{'全部通过' if bad == 0 else f'{bad} 项失败'} =====")
    return 0 if bad == 0 else 1


if __name__ == "__main__":                               # pragma: no cover
    parser = build_parser()
    ns = parser.parse_args()
    if ns.selftest:
        sys.exit(_selftest())
    sys.exit(run(ns))
