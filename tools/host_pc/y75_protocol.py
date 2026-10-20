# -*- coding: utf-8 -*-
"""
y75_protocol.py —— Y75 六维力传感器 RS485 协议层（纯标准库，不依赖 tkinter）

协议来源：下位机源码
    Core/Inc/rs485.h       帧格式 AA 55 [CMD] [PAYLOAD...] 0D 0A
    Core/Inc/rs485_cmd.h   命令码表
    Core/Src/rs485_cmd.c   各命令的实际实现（**以这个为准**）
    Core/Src/rs485.c       收帧 / 发帧
    Core/Src/main.c        USART1、默认 460800 8N1、500/1000Hz 推流

------------------------------------------------------------------
真机行为要点（读源码得到，若与协议文档有出入，以源码为准）
------------------------------------------------------------------
1. 数据帧 = AA 55 [02|03] [6 × float32 小端] 0D 0A，共 29 字节。
   02 = 连续推流帧，03 = 单次请求的应答帧，两者载荷完全一样。
2. 0x05 查序列号、0x37 查矩阵，回复是**裸 ASCII，没有 AA55 帧头帧尾**，
   只能靠「总线静默间隔」判定一条回复结束。
3. 0x32 切到 ASC(调试) 输出后，数据帧变成**裸 28 字节**：
   [6 × float32 小端] + 00 00 80 7F（+Inf 哨兵），连 AA55 都没有；
   但该模式下的 ACK 仍是标准 AA55 帧，两种格式并存。
4. 0x33 / 0x35 把单位写进 EEPROM 掉电保持；0x34 只改内存，不保存。
5. 0x04 改波特率：先按**旧波特率**回 ACK，再切新波特率，
   上位机必须收到 ACK 之后自己切过去。
6. 协议里**没有任何事务号（TID）**，响应只能靠命令码区分。
   因此这里**不做超时自动重试** —— 迟到的响应会被下一个等待者错认，
   比直接报超时危险得多。
7. 0x30 置零是异步的：回 ACK 后设备内部采 20 点求平均，期间 zero_calib_busy=1。

脱离 GUI 单独使用：

    from y75_protocol import Y75Client, TcpTransport
    cli = Y75Client(TcpTransport("127.0.0.1", 15020))
    cli.connect()
    cli.start()
    print(cli.latest)
"""

from __future__ import annotations

import struct
import threading
import time
from bisect import bisect_left
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

# =====================================================================
#  帧结构常量
# =====================================================================
SOF = b"\xAA\x55"
EOF = b"\x0D\x0A"
RESP_FLAG = 0x80                  # 响应帧命令码 = 请求码 | 0x80

AXIS_NAMES = ("FX", "FY", "FZ", "TX", "TY", "TZ")
RESP_AXIS_NAMES = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")

#: ASC(调试) 模式数据帧结尾的 +Inf 哨兵（float 0x7F800000 的小端字节）
ASC_SENTINEL = b"\x00\x00\x80\x7F"
ASC_FRAME_LEN = 28                # 24 字节数据 + 4 字节哨兵

# =====================================================================
#  命令码（rs485_cmd.h）
# =====================================================================
CMD_STOP = 0x01
CMD_START = 0x02
CMD_SINGLE = 0x03
CMD_SET_BAUD = 0x04
CMD_QUERY_SN = 0x05
CMD_QUERY_VER = 0x06
CMD_QUERY_STATUS = 0x07
CMD_SET_FREQ = 0x08
CMD_SET_MATRIX = 0x20          # 上传解耦矩阵的某一行
CMD_ZERO = 0x30
CMD_HEX_SEND = 0x31            # 输出模式：HEX（标准帧，默认）
CMD_ASC_SEND = 0x32            # 输出模式：ASC（裸帧 + 哨兵）
CMD_SET_KG = 0x33              # 数据单位：kg（掉电保持）
CMD_SET_MV = 0x34              # 数据单位：mV（不保存）
CMD_SET_N = 0x35               # 数据单位：N（掉电保持）
CMD_UNZERO = 0x36
CMD_QUERY_MATRIX = 0x37

#: 命令码 → (中文名, 一句话说明)
COMMANDS: Dict[int, Tuple[str, str]] = {
    CMD_STOP:         ("停止连续发送", "设备回复 ACK，停止 500/1000Hz 推流"),
    CMD_START:        ("开始连续发送", "设备回复 ACK，随后按设定频率推流"),
    CMD_SINGLE:       ("单次采集", "设备直接回一帧数据（无 ACK），与推流帧同格式"),
    CMD_SET_BAUD:     ("设置波特率", "载荷 1 字节码：1=460800 2=256000 3=115200 4=19200 5=9600"),
    CMD_QUERY_SN:     ("查询序列号", "回复为裸 ASCII，无帧头帧尾"),
    CMD_QUERY_VER:    ("查询固件版本", "回复 3 字节：主.次.修订"),
    CMD_QUERY_STATUS: ("查询运行状态", "回复 8 字节，见 parse_status()"),
    CMD_SET_FREQ:     ("设置输出频率", "载荷 1 字节：0=500Hz 1=1000Hz"),
    CMD_SET_MATRIX:   ("上传矩阵行", "载荷 = 行号 + 6×float32 小端，共 25 字节"),
    CMD_ZERO:         ("执行置零", "设备采 20 点求平均作为零点，异步完成"),
    CMD_HEX_SEND:     ("HEX 输出模式", "标准 AA55 帧（默认）"),
    CMD_ASC_SEND:     ("ASC 输出模式", "裸 28 字节帧，结尾 00 00 80 7F"),
    CMD_SET_KG:       ("单位设为 kg", "写 EEPROM，掉电保持"),
    CMD_SET_MV:       ("单位设为 mV", "仅当前运行有效，不保存"),
    CMD_SET_N:        ("单位设为 N", "写 EEPROM，掉电保持"),
    CMD_UNZERO:       ("取消置零", "清除零点偏置"),
    CMD_QUERY_MATRIX: ("查询矩阵", "回复 6 行裸 ASCII：matrixN: a, b, c, d, e, f"),
}

#: 波特率码 ↔ 实际波特率（rs485_cmd.c::baud_from_code）
BAUD_CODES = {0x01: 460800, 0x02: 256000, 0x03: 115200, 0x04: 19200, 0x05: 9600}
BAUD_TO_CODE = {v: k for k, v in BAUD_CODES.items()}
BAUD_CHOICES = (460800, 256000, 115200, 19200, 9600)

#: send_mode 取值
SEND_STOP, SEND_CONT, SEND_SINGLE = 0, 1, 2

#: 数据单位码（g_sys.data_format）
FORMAT_LABEL = {0: "原始数据 (mV)", 1: "矩阵解耦 (kg)", 2: "矩阵解耦 (N)"}
FORMAT_UNIT = {0: "mV", 1: "kg", 2: "N"}

#: status_flags 位定义（sensor_types.h）
STATUS_BITS = ((0, "自动启动"), (1, "运行中"))

#: 固件里 debug_mode 非法（既不是 0 也不是 1）时**一帧都不发**
ASC_ONLY = 1


def build_cmd(cmd: int, payload: bytes = b"") -> bytes:
    """组一个下发给设备的命令帧：AA 55 [CMD] [PAYLOAD] 0D 0A。"""
    return SOF + bytes((cmd & 0xFF,)) + bytes(payload) + EOF


def hexs(data: bytes, limit: Optional[int] = None) -> str:
    """字节串 → 空格分隔的大写十六进制，用于日志。

    limit 不为空时最多显示这么多字节，其余用 `…(+N)` 表示（嗅探器要看长载荷）。
    """
    if limit is not None and len(data) > limit:
        head = " ".join(f"{b:02X}" for b in data[:limit])
        return f"{head} …(+{len(data) - limit})"
    return " ".join(f"{b:02X}" for b in data)


def frame_text(cmd: int) -> str:
    """命令码的可读名字。"""
    if cmd in COMMANDS:
        return COMMANDS[cmd][0]
    if 0xA0 <= cmd <= 0xA5:
        return f"矩阵第 {cmd - 0xA0 + 1} 行 ACK"
    base = cmd & 0x7F
    if (cmd & RESP_FLAG) and base in COMMANDS:
        return COMMANDS[base][0] + " (ACK)"
    return f"未知命令 0x{cmd:02X}"


# =====================================================================
#  float32 字节序
# =====================================================================
class ByteOrder:
    """
    浮点数 4 字节的排列方式。

    名字沿用 Modbus 圈的叫法（A = 最高字节 … D = 最低字节）：

        ABCD  标准大端          DCBA  标准小端  ← Y75 设备固件用的就是这个
        BADC  字节交换          CDAB  字交换（低字在前）

    Y75 固件 rs485_cmd.c 的 float_to_le() / le_to_float() 读写两边都是标准小端，
    自洽，所以默认 DCBA；留出下拉框只是为了兼容以后改版的设备。
    """

    NAMES = ("DCBA", "ABCD", "BADC", "CDAB")
    DEFAULT = "DCBA"
    HINT = {"DCBA": "标准小端（Y75 固件默认）", "ABCD": "标准大端",
            "BADC": "字节交换", "CDAB": "字交换"}

    #: 名字 → 重排 4 字节的下标顺序（重排后按大端解析）
    _PERM = {
        "ABCD": (0, 1, 2, 3),
        "DCBA": (3, 2, 1, 0),
        "BADC": (1, 0, 3, 2),
        "CDAB": (2, 3, 0, 1),
    }

    @classmethod
    def _perm(cls, order: str) -> Tuple[int, int, int, int]:
        return cls._PERM.get(order, cls._PERM[cls.DEFAULT])

    @classmethod
    def decode4(cls, raw: bytes, order: str = DEFAULT) -> float:
        p = cls._perm(order)
        return struct.unpack(">f", bytes((raw[p[0]], raw[p[1]], raw[p[2]], raw[p[3]])))[0]

    @classmethod
    def encode4(cls, value: float, order: str = DEFAULT) -> bytes:
        big = struct.pack(">f", float(value))
        p = cls._perm(order)
        inv = [0, 0, 0, 0]
        for i, src in enumerate(p):
            inv[src] = i
        return bytes((big[inv[0]], big[inv[1]], big[inv[2]], big[inv[3]]))

    @classmethod
    def decode6(cls, raw: bytes, order: str = DEFAULT) -> Tuple[float, ...]:
        return tuple(cls.decode4(raw[i * 4:i * 4 + 4], order) for i in range(6))

    @classmethod
    def encode6(cls, values: Sequence[float], order: str = DEFAULT) -> bytes:
        return b"".join(cls.encode4(v, order) for v in values)


# =====================================================================
#  响应帧分类与长度表
# =====================================================================
#: 收到的命令码 → (类型, 载荷字节数)。载荷为 None 表示长度不固定。
_RESP_TABLE: Dict[int, Tuple[str, Optional[int]]] = {
    CMD_START:          ("data", 24),        # 0x02 连续推流帧
    CMD_SINGLE:         ("data", 24),        # 0x03 单次应答帧
    CMD_QUERY_VER:      ("ver", 3),          # 0x86
    CMD_QUERY_STATUS:   ("status", 8),       # 0x87
    CMD_STOP:           ("ack", 0),          # 0x81
    CMD_SET_BAUD:       ("ack", 0),          # 0x84
    CMD_SET_FREQ:       ("ack", 0),          # 0x88
    CMD_ZERO:           ("ack", 0),          # 0xB0
    CMD_HEX_SEND:       ("ack", 0),          # 0xB1
    CMD_ASC_SEND:       ("ack", 0),          # 0xB2
    CMD_SET_KG:         ("ack", 0),          # 0xB3
    CMD_SET_MV:         ("ack", 0),          # 0xB4
    CMD_SET_N:          ("ack", 0),          # 0xB5
    CMD_UNZERO:         ("ack", 0),          # 0xB6
}


def resp_kind(cmd: int) -> Tuple[str, Optional[int]]:
    """
    收到的命令码 → (类型, 期望载荷长度)。

    注意区分「带 0x80 标志的 ACK」和「不带标志的数据帧」：
    0x02 是连续推流数据帧（24 字节载荷），0x82 只是 ACK（0 字节）。
    两者只差一个 bit，混掉就会拆错帧。
    """
    if 0xA0 <= cmd <= 0xA5:                  # 0x20 | 0x80 | row
        return "matrixack", 0
    if cmd & RESP_FLAG:
        kind, plen = _RESP_TABLE.get(cmd & 0x7F, ("ack", 0))
        if kind == "data":                   # 0x82 / 0x83 是 ACK，没有载荷
            return "ack", 0
        return kind, plen
    return _RESP_TABLE.get(cmd, ("unknown", None))


class Frame:
    """一个解析出来的响应帧。"""

    __slots__ = ("cmd", "payload", "kind", "raw")

    def __init__(self, cmd: int, payload: bytes, raw: bytes = b""):
        self.cmd = cmd
        self.payload = bytes(payload)
        self.kind, _ = resp_kind(cmd)
        self.raw = raw

    def __repr__(self) -> str:                       # pragma: no cover
        return f"<Frame {frame_text(self.cmd)} kind={self.kind} len={len(self.payload)}>"


def looks_like_text(chunk: bytes) -> bool:
    """
    判断一段字节是不是「裸 ASCII 文本」。

    用于把 0x05 序列号 / 0x37 矩阵这类无帧头回复，与二进制垃圾区分开。
    判据是**全部字节**都是可打印字符 —— 二进制 float 恰好整段可打印的
    概率约 0.37^n，n≥16 时已经可以忽略。
    """
    if not chunk:
        return False
    for b in chunk:
        if b in (0x09, 0x0A, 0x0D):
            continue
        if not 0x20 <= b < 0x7F:
            return False
    return True


class FrameAssembler:
    """
    字节流 → 响应帧 的拆帧器。

    必须能处理「一次读到半帧 / 一帧半 / 好几帧」的情况 ——
    设备连续推流时 500~1000 帧/秒，一次 recv 拿到几十帧是常态。
    优先按**已知长度**取帧，长度对不上再退回「向后找 0D 0A」，
    这样即使浮点载荷里恰好出现 AA 55 / 0D 0A 也不会切错。
    """

    #: 兜底缓冲上限，防止对端乱发数据把内存吃光
    MAX_BUFFER = 1 << 16

    def __init__(self):
        self._buf = bytearray()
        self.overflow = 0        # 因缓冲超限被丢掉的字节数
        self.resync = 0          # 因长度对不上而重新同步的次数
        #: 只带帧头帧尾、没有载荷的畸形帧（AA 55 [cmd] 0D 0A）计数
        self.short_frames = 0
        #: 上一次 pull_frame() 是否有进展（丢弃了噪声字节）。
        #: 调用方据此判断「该继续解析」还是「该等下一批数据」。
        self.moved = False

    # ------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self._buf += data
        if len(self._buf) > self.MAX_BUFFER:
            drop = len(self._buf) - self.MAX_BUFFER
            del self._buf[:drop]
            self.overflow += drop

    def pending(self) -> int:
        return len(self._buf)

    def drop(self, n: int) -> None:
        del self._buf[:n]

    def head(self) -> bytes:
        return bytes(self._buf[:2])

    def peek_text(self) -> bytes:
        """
        窥探缓冲区开头的裸文本，**不消费**。遇到 AA 55 就停下。
        末尾单个 0xAA 也不返回（它可能是被切断的帧头）。
        """
        if not self._buf:
            return b""
        cut = self._buf.find(SOF)
        if cut == 0:
            return b""
        if cut < 0:
            cut = len(self._buf)
            if self._buf[-1] == 0xAA:
                cut -= 1
        return bytes(self._buf[:cut])

    def pull_frame(self) -> Optional[Frame]:
        """
        解析并取出一帧；数据不够返回 None（不乱丢字节）。

        返回 None 时用 `self.moved` 告诉调用方本次**有没有进展**（丢掉了
        噪声/畸形字节）：有进展就该继续解析，而不是傻等下一批数据 ——
        否则一次 recv 只前进 1 个字节，整条流会被畸形数据拖住。
        """
        self.moved = False
        buf = self._buf
        n = len(buf)
        if n < 4:
            return None

        # ---- 1. 对齐到 AA 55 ----
        start = buf.find(SOF)
        if start < 0:
            # 只有一个孤零零的 0xAA 时留着，等下一批字节来判断
            if n > 1:
                del buf[:n - 1]
                self.moved = True
            return None
        if start > 0:
            del buf[:start]
            self.moved = True

        n = len(buf)
        if n < 5:
            return None
        cmd = buf[2]
        _, plen = resp_kind(cmd)

        # ---- 2. 空载荷的短帧：AA 55 [cmd] 0D 0A ----
        # 真机上偶尔能抓到（干扰截断、或固件在某些状态下发的短帧）。
        # 只在该命令「本应带载荷」时才算畸形，否则会误伤 0 载荷的 ACK
        # —— ACK 也是 AA 55 [cmd] 0D 0A 这个长相。
        # 这里整帧跳过：若交给下面「长度对不上就丢 1 字节重同步」处理，
        # 每个畸形帧要试 5 次才挪得过去，会连带把正常帧的解析一起拖慢。
        if plen and bytes(buf[3:5]) == EOF:
            raw = bytes(buf[:5])
            del buf[:5]
            self.short_frames += 1
            self.moved = True
            return Frame(cmd, b"", raw)

        # ---- 3. 优先按已知长度取帧 ----
        if plen is not None:
            total = 3 + plen + 2
            if n < total:
                return None
            if bytes(buf[total - 2:total]) == EOF:
                payload = bytes(buf[3:3 + plen])
                raw = bytes(buf[:total])
                del buf[:total]
                return Frame(cmd, payload, raw)
            # 长度对不上：多半是把载荷里的 AA 55 当成了帧头，丢 1 字节重同步
            self.resync += 1
            del buf[:1]
            self.moved = True
            return None

        # ---- 4. 长度未知：向后找 0D 0A ----
        end = bytes(buf).find(EOF, 3)
        if end < 0:
            if n > self.MAX_BUFFER // 4:
                del buf[:n - 3]
                self.moved = True
            return None
        total = end + 2
        payload = bytes(buf[3:end])
        raw = bytes(buf[:total])
        del buf[:total]
        return Frame(cmd, payload, raw)


# =====================================================================
#  传输层
# =====================================================================
class Transport:
    """字节流传输抽象：串口 / TCP。只负责收发字节，不碰协议。"""

    name = "transport"

    def open(self) -> None:                       # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:                      # pragma: no cover - 抽象
        raise NotImplementedError

    @property
    def is_open(self) -> bool:                    # pragma: no cover - 抽象
        raise NotImplementedError

    def recv(self, timeout: float) -> bytes:      # pragma: no cover - 抽象
        """最多等 timeout 秒，返回读到的字节；超时返回 b""。"""
        raise NotImplementedError

    def write(self, data: bytes) -> None:         # pragma: no cover - 抽象
        raise NotImplementedError


class TcpTransport(Transport):
    """
    TCP 客户端。真机是 RS485，这个后端只给**设备模拟器**用，
    目的是让整套上位机在没有硬件、没有串口线的机器上也能跑通、能回归。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 15020, timeout: float = 3.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._sock = None

    @property
    def name(self) -> str:
        return f"TCP {self.host}:{self.port}"

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(0.05)
        self._sock = s

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.shutdown(2)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def recv(self, timeout: float) -> bytes:
        if self._sock is None:
            return b""
        try:
            self._sock.settimeout(timeout)
            return self._sock.recv(4096)
        except (TimeoutError, OSError) as exc:
            err = getattr(exc, "errno", None)
            if err in (None, 10060, 10035, 10053, 10054, 11001):
                return b""
            raise

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("TCP 未连接")
        self._sock.sendall(bytes(data))


# =====================================================================
#  异常
# =====================================================================
class Y75Timeout(TimeoutError):
    """等待设备响应超时。"""


class Y75Error(RuntimeError):
    """协议层错误。"""


class _Waiter:
    __slots__ = ("key", "ev", "frame", "text")

    def __init__(self, key: Tuple[str, object]):
        self.key = key
        self.ev = threading.Event()
        self.frame: Optional[Frame] = None
        self.text: bytes = b""


# =====================================================================
#  总线仲裁：半双工 485 上的「发言权」
# =====================================================================
class BusGate:
    """
    判定「现在能不能往 485 总线上发东西」。

    ## 为什么需要它

    MAX13487 是**自动方向**的收发器（没有 DE 引脚可以软件控制），主机没法
    主动把总线让出来。设备连续推流时总线几乎一直被它占着，只有**帧与帧
    之间**那一小段是空的：

        460800 波特率下，29 字节数据帧在线上占 29x10/460800 ≈ 0.63 ms；
        500 Hz 推流时帧周期 2 ms → 每帧之后有约 1.37 ms 空隙；
        一条 8 字节命令只占 0.17 ms，塞进空隙绰绰有余。

    命令只要落在空隙里就不会撞车。一旦落在设备**正在发的那一帧**中间，
    两个驱动器同时拉总线 → 双方收到的都是垃圾：设备侧拆帧错乱（严重时
    进 HardFault），主机侧也等不到 ACK。

    ## 什么时候发：帧刚收完的那一瞬间

    关键的一点（也是老版本做错的地方）：**必须在「刚刚收完一整帧」的那一刻
    立刻发，而不是先等一小段静默再发**。

    设接收链路延迟为 d（USB 转串口的 Latency Timer、驱动缓冲、线程调度都算
    在里面）。你在主机上看到「第 N 帧收完」时，总线其实已经走到
    「第 N 帧结束 + d」。下一帧在线上从「第 N 帧结束 + 1.37ms」开始，
    也就是说：**从观察到帧结束的那一刻算起，只剩 1.37 - d 毫秒的空隙**。

    - d 很小时（TCP、或 Latency Timer 调到 1ms 的串口）：余量足足有 1ms 上下；
    - 如果先等 0.8ms 静默再发，等于把这 0.8ms 从余量里白扣掉，
      d 稍大一点就会正好踩进下一帧 —— 这正是「等静默」策略的致命缺陷，
      实测撞车率约 30%（用模拟器的 --collide 模式量出来的）。

    所以规则是：**看到一整帧收完（且缓冲里没有半帧）就立刻发**，越早越好。
    帧后窗口 `window` 只是用来判断「这个帧结束还算不算新鲜」——超过窗口
    就说明我们已经错过了这段空隙，不如等下一帧。

    ## 放行条件

    1. 距自己上一次发送已经过了 `guard`（默认 2 个帧周期）——
       别在设备正要回 ACK 的窗口里又插一帧；
    2. 并且满足下面任一条：
       a. 最近一帧收完的时刻距今 ≤ `window`，且拆帧缓冲里没有半帧
          （= 正处在帧间空隙里，这是正常路径）；
       b. 总线已经静默 `idle_s`（默认 4 个帧周期，至少 4ms）——
          设备根本没在推流，等下去没有意义，立刻发。

    实在等不到（比如串口驱动在攒批，压根看不到帧边界）就按 `soft_deadline`
    （3 个帧周期）放行，计入 `blind`；超过 `max_wait_ms` 还没成，计入
    `timeouts`。两者在界面上都能看到。
    """

    #: 轮询步长（秒）。比帧后窗口小一截，才抓得住空隙
    POLL = 0.00015
    #: 帧后窗口 = 帧周期 x 这个系数（0.35 帧：够覆盖一次轮询抖动，
    #: 又不至于把余量耗光）
    WINDOW_K = 0.35
    WINDOW_MIN = 0.00020         # 0.20 ms 下限
    WINDOW_MAX = 0.00150         # 1.50 ms 上限
    #: guard = 帧周期 x 这个系数（2 帧：设备一般下一帧就回 ACK）
    GUARD_K = 2.0
    #: 静默多久算「设备没在推流」：4 个帧周期，且不少于 4ms
    IDLE_K = 4.0
    IDLE_MIN = 0.004

    def __init__(self):
        #: 关掉就完全回到「立刻发」的老行为（排查用）
        self.enabled = True
        #: 帧后窗口，毫秒；0 = 按实测帧周期自动
        self.window_ms = 0.0
        #: 最多等这么久，超时也发，毫秒
        self.max_wait_ms = 250.0
        #: 发送后的自我保护窗口，毫秒；0 = 自动
        self.guard_ms = 0.0
        #: 实测帧周期（秒），由客户端同步进来
        self.sample_dt = 1.0 / 500.0

        self._last_rx = 0.0          # 最近一次收到字节的时刻（time.time）
        self._last_frame = 0.0       # 最近一帧完整收完的时刻（任何帧）
        self._last_data = 0.0        # 最近一个**数据帧**收完的时刻
        self._last_tx = 0.0          # 自己上一次发完的时刻
        #: 收完一整帧就 +1 并 set —— 让等总线的一方**立刻**被唤醒，
        #: 而不是睡到下一个轮询点（轮询的 0.15ms 抖动足以吃掉帧间余量）
        self._frame_seq = 0
        self._frame_ev = threading.Event()

        # ---- 统计（界面显示用）----
        self.gate_count = 0          # 被拦下来等过的次数
        self.wait_ms_sum = 0.0
        self.wait_ms_max = 0.0
        self.wait_ms_last = 0.0
        self.timeouts = 0            # 等到硬超时也没等到空隙的次数
        self.blind = 0               # 看不到帧边界、等够 3 帧就发的次数

    # ------------------------------------------------------------------
    #  接收线程上报
    # ------------------------------------------------------------------
    def note_rx(self, t: Optional[float] = None) -> None:
        self._last_rx = time.time() if t is None else t

    def note_frame(self, t: Optional[float] = None, data: bool = False) -> None:
        """
        收完一整帧。

        data=True 表示这是**数据帧**（0x02 推流 / 0x03 单次应答）。
        只有数据帧才被当作「总线进入空隙」的可靠信号：

        设备推流是周期性的（500Hz 每 2ms 一帧、帧本身占 0.63ms），所以每个
        数据帧后面必然跟着 1.37ms 的空隙。而 ACK / 状态回复是主循环里插空
        发出来的，它前面后面都没有固定节拍 —— ACK 收完的下一微秒就可能正好
        接上一个推流帧。把 ACK 当成「可以发」的信号，就是撞车的来源。
        """
        now = time.time() if t is None else t
        self._last_frame = now
        self._last_rx = now
        if not data:
            return
        self._last_data = now
        self._frame_seq += 1
        self._frame_ev.set()         # 唤醒正在等「帧间空隙」的发送方

    def note_tx(self, t: Optional[float] = None) -> None:
        self._last_tx = time.time() if t is None else t

    def reset(self) -> None:
        self._last_rx = self._last_frame = self._last_data = self._last_tx = 0.0
        self._frame_ev.clear()
        self.gate_count = 0
        self.wait_ms_sum = self.wait_ms_max = self.wait_ms_last = 0.0
        self.timeouts = 0
        self.blind = 0

    # ------------------------------------------------------------------
    def _dt(self) -> float:
        return max(self.sample_dt, 1e-5)

    def window_s(self) -> float:
        if self.window_ms > 0.0:
            return self.window_ms / 1000.0
        return min(self.WINDOW_MAX, max(self.WINDOW_MIN, self.WINDOW_K * self._dt()))

    def guard_s(self) -> float:
        if self.guard_ms > 0.0:
            return self.guard_ms / 1000.0
        return min(0.050, max(0.0015, self.GUARD_K * self._dt()))

    def idle_s(self) -> float:
        return max(self.IDLE_MIN, self.IDLE_K * self._dt())

    def soft_deadline_s(self) -> float:
        """等不到帧边界时的「认了」时长：3 个帧周期。"""
        return max(0.004, 3.0 * self._dt())

    def busy_now(self, is_partial: bool) -> bool:
        """此刻总线是否「不该发」。给界面做提示用。"""
        now = time.time()
        rx_gap = (now - self._last_rx) if self._last_rx > 0.0 else 1e9
        if rx_gap >= self.idle_s():
            return False
        since_frame = (now - self._last_frame) if self._last_frame > 0.0 else 1e9
        return bool(is_partial) or since_frame > self.window_s()

    # ------------------------------------------------------------------
    def wait_idle(self, is_partial: Any, now: Optional[float] = None) -> float:
        """
        阻塞到「可以发」为止，返回**等了多久（毫秒）**。关掉时立刻返回 0。

        is_partial：无参可调用对象，返回 True 表示拆帧缓冲里还有半帧
                    （只能由客户端提供，因为缓冲在它手里）。

        等待分两段：
          1. 先等够 guard（自己上一次发言后的保护期，别踩到设备正在回的 ACK）；
          2. 再等**下一个**帧边界 —— 收包线程一旦收完整帧就 set 事件，
             这里立刻返回，所以「帧收完 → 真正 write」只差一次线程唤醒
             （几十微秒），帧间余量不会被轮询抖动吃掉。
        """
        if not self.enabled:
            return 0.0
        t0 = time.time() if now is None else now
        hard = t0 + max(0.0, self.max_wait_ms) / 1000.0
        soft = t0 + self.soft_deadline_s()
        guard = self.guard_s()
        idle = self.idle_s()
        window = self.window_s()
        seq = self._frame_seq
        gave_up = 0                     # 0 = 正常等到，1 = 软超时，2 = 硬超时

        while True:
            t = time.time()
            rx_gap = (t - self._last_rx) if self._last_rx > 0.0 else 1e9
            tx_gap = (t - self._last_tx) if self._last_tx > 0.0 else 1e9

            if tx_gap >= guard:
                since_data = ((t - self._last_data)
                              if self._last_data > 0.0 else 1e9)
                if since_data >= idle and rx_gap >= idle:
                    # 设备没在推流：总线上没有周期性的数据帧，不必等边界
                    break
                if self._frame_seq != seq:
                    # 边界还必须**新鲜**：如果刚才在等 guard，等到 guard 到期
                    # 时手上这个边界可能已经是 1ms 前的旧事了，那时候空隙
                    # 早走完了，照着旧边界拍下去正好压在下一帧的起点上。
                    stale = (t - self._last_data) > window
                    if not is_partial() and not stale:
                        break           # 干净的一帧刚收完 —— 就是现在
                    seq = self._frame_seq     # 旧边界 / 半帧边界都不算，等下一个

            if t >= hard:
                gave_up = 2
                break
            if t >= soft:
                gave_up = 1
                break
            self._frame_ev.wait(0.0005)
            self._frame_ev.clear()

        waited = (time.time() - t0) * 1000.0
        self.wait_ms_last = waited
        if gave_up == 2:
            self.timeouts += 1
        elif gave_up == 1:
            self.blind += 1
        if waited >= 0.2:                     # 有意义的等待才计入统计
            self.gate_count += 1
            self.wait_ms_sum += waited
            self.wait_ms_max = max(self.wait_ms_max, waited)
        return waited

    # ------------------------------------------------------------------
    def avg_wait_ms(self) -> float:
        return self.wait_ms_sum / self.gate_count if self.gate_count else 0.0

    def summary(self) -> str:
        if not self.enabled:
            return "总线仲裁：关闭（收到就发）"
        s = (f"总线仲裁：帧后 {self.window_s() * 1000:.2f} ms 内发，"
             f"等过 {self.gate_count} 次 / 均 {self.avg_wait_ms():.2f} ms / "
             f"最长 {self.wait_ms_max:.2f} ms")
        if self.blind:
            s += f" / 看不到帧边界 {self.blind} 次"
        if self.timeouts:
            s += f" / 硬超时 {self.timeouts} 次"
        return s


# =====================================================================
#  客户端
# =====================================================================
class Y75Client:
    """
    Y75 设备客户端。

    latest        最近一帧 6 个通道的数值（元组，取值可能为 None）
    push_count    收到的数据帧总数（连续推流 + 单次）
    wave          最近若干帧 (时间, 数值元组)，供波形显示

    on_push / on_log / on_state 三个回调都在**接收线程**里执行，
    界面只能往队列里塞，绝不能直接碰 Tk 控件。
    """

    #: 波形缓冲上限。必须覆盖最大的时间窗 × 最高的推流速率：
    #: 20s × 1000Hz = 20000 帧。之前是 6000（=12s@500Hz），
    #: 结果把时间窗拉到 10s / 20s 时左边会空一大块。
    WAVE_MAX = 20000
    #: 一次多留这么多帧再统一裁剪，避免每帧都做一次 O(n) 的列表搬移
    WAVE_SLACK = 2048
    #: 裸文本回复的静默判定间隔（秒）
    TEXT_GAP = 0.12
    #: 数据帧载荷字节数
    DATA_LEN = 24
    #: 同类告警日志的最小间隔（秒）。畸形帧/干扰可能每秒几百个，
    #: 逐条记日志会把界面的文本控件刷爆（实测帧率掉三分之一）。
    LOG_GATE_SEC = 1.0
    #: 设备时钟（等间隔推算）允许超前本机时间多少秒。
    #: USB 驱动一次交上来 8~16ms 的一批帧，批内每帧都 +sample_dt，所以
    #: 时钟短暂超前本机时间是**正常**的，容差要盖住一批的最大跨度。
    #: 但超过这个量就说明 sample_dt 被高估了（典型：统计窗跨了
    #: 「停止→开始」的空洞，量出几倍于真实帧周期的假节拍），必须拉回。
    TS_AHEAD = 0.08

    def __init__(self, transport: Transport, byte_order: str = ByteOrder.DEFAULT,
                 log_raw: bool = True):
        self.transport = transport
        self.byte_order = byte_order if byte_order in ByteOrder.NAMES else ByteOrder.DEFAULT
        self.log_raw = log_raw
        #: >0 时每 N 帧推流帧也写一条报文日志（只用于短时间抓包，避免刷屏）
        self.log_stream_every = 0

        self._asm = FrameAssembler()
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._cmd_lock = threading.RLock()
        #: 真正往总线写字节的那一段：总线仲裁 + write，独立于命令锁。
        #: 加锁顺序恒为 _cmd_lock → _bus_lock，不会反过来。
        self._bus_lock = threading.Lock()
        #: 半双工总线的时间窗判定器（见 BusGate）
        self.bus = BusGate()
        #: 无应答时自动重发几次。0 = 不重发（默认）。
        #: 协议没有事务号，重发存在「把上一次的迟到响应认成这一次」的风险，
        #: 所以默认关掉；只在「命令丢了、总线又确实空」时人工调大更稳。
        self.bus_retry = 0
        self._waiter: Optional[_Waiter] = None
        self._text_buf = bytearray()
        self._last_rx = 0.0
        self._push_snapshot = 0
        #: 日志限流的门闸：key -> (上次记录时刻, 期间被压掉的条数)
        self._log_gate: Dict[str, Tuple[float, int]] = {}

        #: 标称采样周期（秒）。推流帧的时间戳按它等间隔推算，
        #: 用来抹平「USB 串口按 8~16ms 成批上报」造成的同批时间戳重叠。
        self._nominal_dt = 1.0 / 500.0
        self.sample_dt = self._nominal_dt
        #: 实测帧周期的 EWMA。真机的推流跑在固件主循环里，实测常落在
        #: 300~500Hz，并不正好是协议里的 500/1000 两档（见 _tune_rate）。
        self.meas_dt = 0.0
        self._rate_t0 = 0.0
        self._rate_n0 = 0
        #: 推算出来的设备时间轴（epoch 基准），只在推流帧上推进。
        #: 对钟规则见 _drain：批边界挪一小步，批内帧间隔永远等于 sample_dt。
        self._dev_t = 0.0

        #: 驱动「攒批」体检：非空 recv 的次数、批间隔与每批帧数的 EWMA。
        #: 批间隔 ≈16ms 说明是 USB 转串口的 Latency Timer 在攒批（TCP 回环
        #: 时就是发送节拍本身，几百微秒~几毫秒）—— 这是「网络模拟丝滑、
        #: 串口卡」最常见的物理根源，也是唯一需要在设备管理器里改的项。
        self.rx_batches = 0
        self.batch_iv = 0.0            # 秒
        self.batch_frames = 0.0        # 每批帧数
        self._batch_t0 = 0.0

        # ---- 对外状态 ----
        self.latest: Tuple[Optional[float], ...] = (None,) * 6
        self.push_count = 0            # 数据帧累计
        self.stream_count = 0          # 其中来自连续推流的帧数
        self.frame_errors = 0          # 拆帧重同步次数
        self.cmd_errors = 0            # 协议层命令错误次数
        self.garbage_bytes = 0         # 丢弃的乱码字节数
        # 用 list 而不是 deque：波形要按时间戳二分查找可视区间，
        # deque 的随机下标访问是 O(n)，二分反而比直接复制还慢。
        # 时间戳单调递增，bisect 才成立。
        self.wave: List[Tuple[float, Tuple[float, ...]]] = []

        #: ASC(调试) 模式下数据帧是裸 28 字节，需要换一套识别方式
        self.asc_mode = False
        #: 收到的裸文本（最近若干条）
        self.text_log: Deque[str] = deque(maxlen=50)

        # ---- 回调（接收线程内执行，只允许入队）----
        self.on_push: Optional[Callable[[Tuple[float, ...], float], None]] = None
        self.on_log: Optional[Callable[[str, str], None]] = None      # (level, msg)
        self.on_state: Optional[Callable[[str, str], None]] = None    # (state, detail)

    # ------------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------------
    def connect(self) -> None:
        if self._reader is not None:
            return
        self.transport.open()
        self._asm = FrameAssembler()
        self._text_buf.clear()
        self.text_log.clear()
        self.push_count = self.stream_count = self.frame_errors = 0
        self.cmd_errors = self.garbage_bytes = 0
        self.latest = (None,) * 6
        self.wave.clear()
        self._dev_t = 0.0                  # 设备时钟重锚
        self.rx_batches = 0
        self.batch_iv = 0.0
        self.batch_frames = 0.0
        self._batch_t0 = 0.0
        self.sample_dt = self._nominal_dt  # 实测节拍也重置，重连后重新量
        self.meas_dt = 0.0
        self.bus.reset()                   # 总线时间窗重锚，别拿上次的陈旧时刻判断
        self.bus.sample_dt = self.sample_dt
        self._rate_t0 = 0.0
        self._rate_n0 = 0
        self._log_gate.clear()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="y75-rx", daemon=True)
        self._reader.start()
        self._log("info", f"已连接 {self.transport.name}")
        self._state("connected", self.transport.name)

    def disconnect(self) -> None:
        reader, self._reader = self._reader, None
        self._stop.set()
        try:
            self.transport.close()
        except Exception:
            pass
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.6)
        self._state("disconnected", "")

    def is_connected(self) -> bool:
        return self._reader is not None and self.transport.is_open

    # ------------------------------------------------------------------
    #  接收线程
    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.transport.recv(0.05)
            except Exception as exc:                     # 串口被拔、网线断了
                if not self._stop.is_set():
                    self._log("err", f"读取失败：{exc}")
                    self._state("error", str(exc))
                break

            if data:
                self._last_rx = time.time()
                self.bus.note_rx(self._last_rx)      # 总线仲裁：有线上的字节在动
                # ---- 攒批体检：一次 recv 拿到多少、隔多久拿到一次 ----
                t = self._last_rx
                self.rx_batches += 1
                if self._batch_t0 > 0.0:
                    iv = t - self._batch_t0
                    if 0.0 < iv < 1.0:
                        self.batch_iv = (iv if self.batch_iv <= 0.0
                                         else self.batch_iv * 0.85 + iv * 0.15)
                self._batch_t0 = t
                n0 = self.push_count
                self._asm.feed(data)
                self._drain()
                added = self.push_count - n0
                if added:
                    self.batch_frames = (float(added) if self.batch_frames <= 0.0
                                         else self.batch_frames * 0.85 + added * 0.15)
            else:
                self._drain()                # 静默 tick：收尾文本、凑半帧
                self._flush_text()

            self.frame_errors = self._asm.resync

        self._state("closed", "")

    # ---- 拆帧主循环 ----------------------------------------------------
    def _drain(self) -> None:
        # 批级对钟：每批只把设备时钟朝真实时间挪一小步（≤半个帧周期）。
        # 批内帧间隔永远严格等于 sample_dt —— 时间轴的均匀度就是画面的
        # 平滑度，绝不允许被对钟压扁。
        #
        # 老版本在这里算出 _corr 后让批内**每一帧**都加一份：纠偏增益 =
        # 5% × 批内帧数。TCP 每批 1 帧（增益 0.05）稳如泰山；真实串口上
        # USB 驱动 + 固件主循环把数据攒成 20~60 帧一大批，增益超过 1 就
        # 振荡，纠偏量一旦超过帧周期，批内时间戳原地踏步被钳成
        # 「上一帧 + 1µs」—— 整批点画在同一个 x 上，波形里就是一根根竖条。
        if self._dev_t > 0.0:
            lim = 0.5 * self.sample_dt
            self._dev_t += max(-lim, min(lim, 0.05 * (time.time() - self._dev_t)))
        while True:
            buf = self._asm._buf
            if not buf:
                return

            # 1) 缓冲区以 AA 55 开头 → 走标准帧
            if self._asm.head() == SOF:
                frame = self._asm.pull_frame()
                if frame is None:
                    if self._asm.moved:
                        continue        # 刚丢掉噪声/畸形字节，后面可能还有帧
                    return              # 真的数据不够，等下一批
                self._handle_frame(frame)
                continue

            # 2) ASC 调试模式：找 +Inf 哨兵
            if self.asc_mode:
                frame, progressed = self._try_asc_frame()
                if frame is not None:
                    self._handle_frame(frame)
                    continue
                if progressed:
                    continue

            # 3) 裸文本（0x05 序列号 / 0x37 矩阵）：整段可打印才认
            cand = self._asm.peek_text()
            if cand and looks_like_text(cand):
                self._asm.drop(len(cand))
                self._text_buf += cand
                if b"\n" in cand:
                    self._flush_text(force=True)
                continue

            # 4) 其余都是脏字节，丢 1 字节重新同步
            if buf[0] == 0xAA and len(buf) == 1:
                return                       # 半截帧头，等下一批字节
            if self.asc_mode and len(buf) < ASC_FRAME_LEN:
                return                       # ASC 帧最长 28 字节，还没收全
            self._asm.drop(1)
            self.garbage_bytes += 1

    def _try_asc_frame(self) -> Tuple[Optional[Frame], bool]:
        """
        ASC 模式下在缓冲区里找 00 00 80 7F 哨兵。
        返回 (帧 or None, 本轮是否有进展)。哨兵在帧尾，所以它前面正好 24 字节。
        """
        buf = self._asm._buf
        pos = buf.find(ASC_SENTINEL)
        if pos < 0:
            return None, False
        if pos < 24:
            # 前面凑不够 24 字节 → 帧头早就丢了，连同这个哨兵一起跳过
            del buf[:pos + 4]
            self.garbage_bytes += pos + 4
            return None, True
        del buf[:pos - 24]
        raw = bytes(buf[:ASC_FRAME_LEN])
        del buf[:ASC_FRAME_LEN]
        return Frame(CMD_SINGLE, raw[:24], raw), True

    def _flush_text(self, force: bool = False) -> None:
        """总线静默超过 TEXT_GAP 就把攒下的裸文本当成一条回复送出。"""
        if not self._text_buf:
            return
        if not force and (time.time() - self._last_rx) < self.TEXT_GAP:
            return
        raw = bytes(self._text_buf)
        self._text_buf.clear()
        text = raw.decode("utf-8", "replace")
        self.text_log.append(text)
        self._log_every(f"text{len(raw)}", "rx",
                        f"文本回复 {len(raw)} 字节：{text.strip()[:200]!r}")
        self._serve(("text", None), text=raw)

    def _handle_frame(self, frame: Frame) -> None:
        # 只有数据帧才算「总线进入空隙」的可靠信号（见 BusGate.note_frame）。
        # 时间戳必须用**收包时刻**（recv 返回的那一瞬），不能用现在：
        # 拆帧、回调、日志都在收包线程里做，等这一帧解析完可能已经过去 1ms，
        # 拿解析时刻当「帧结束时刻」会让仲裁以为自己离帧边界还很近。
        self.bus.note_frame(t=self._last_rx, data=(frame.kind == "data"))
        if self.log_raw and frame.kind != "data":
            self._log("rx", f"← {hexs(frame.raw)}   [{frame_text(frame.cmd)}]")

        if frame.kind == "data":
            if len(frame.payload) != self.DATA_LEN:
                self.cmd_errors += 1
                if not frame.payload:
                    # 空载荷畸形帧（AA 55 [cmd] 0D 0A）：解析器已整帧跳过，
                    # 这里只做限流告警，不然每秒几百条会把界面刷爆。
                    self._log_every(
                        "empty",
                        "warn",
                        f"总线上出现「空载荷帧」AA 55 {frame.cmd:02X} 0D 0A"
                        f"（没有 24 字节数据段），已累计跳过 {self._asm.short_frames} 个")
                else:
                    self._log_every(
                        "badlen",
                        "warn",
                        f"数据帧长度异常（{len(frame.payload)} ≠ 24 字节），已丢弃")
                return
            vals = ByteOrder.decode6(frame.payload, self.byte_order)
            now = time.time()
            if frame.cmd == CMD_START:
                # 推流帧：时间戳 = 纯等间隔推算的设备时钟，批内绝不增减。
                # 直接用 time.time() 会把同一批的几十帧压在同一个毫秒里；
                # 按「每帧加批纠偏量」对钟又会在大批时振荡出竖条（见 _drain）。
                # 与真实时间的长期对齐交给 _tune_rate 实测节拍 + 这里的批级
                # 小步对钟；短期误差由波形层的自适应显示延迟吸收。
                if self._dev_t <= 0.0 or abs(now - self._dev_t) > 0.4:
                    self._dev_t = now                  # 首帧 / 断流太久 / 频率档对不上
                else:
                    self._dev_t += self.sample_dt
                    if self._dev_t > now + self.TS_AHEAD:
                        # 等间隔推算跑到了本机时间前面。只要超前量在 TS_AHEAD
                        # 以内，这是「一批帧同时到达」的正常现象（批内每帧都
                        # +sample_dt，本机时间还没跟上），不必管。
                        # 但越过 TS_AHEAD 就意味着 sample_dt 被高估了 —— 常见于
                        # 统计窗跨过「停止→开始」的空洞（那 0.5s 里只有重启后
                        # 的几十帧，算出来的帧周期大好几倍）。一旦时钟超前，
                        # 下面那条「单调兜底」会一直在未来的时间位上继续累加，
                        # 越加越远：实测点两次停止/开始就能超前 5s，波形上只剩
                        # 左边一小截（最新数据全在可视窗右边界之外）。
                        # 所以这里硬拉回本机时间，让推算重新对齐。
                        self._dev_t = now
                ts = self._dev_t
                self.stream_count += 1
            else:
                ts = now
                self._dev_t = now                      # 单次应答帧把设备时钟拉到当前
            if self.wave and ts <= self.wave[-1][0]:
                # 时间戳必须严格递增（bisect 依赖）。兜底也保持等间隔——
                # 老版本的 +1e-6 会把点画成竖条。
                ts = self.wave[-1][0] + self.sample_dt
                if ts > now + self.TS_AHEAD:
                    # 缓冲尾部已经在未来（时钟基准坏过一次）：这里只保证单调，
                    # 用很小的步长挤过去，让本机时间自己追上来。否则每帧都按
                    # 整拍往前推，超前的雪球会越滚越大。
                    ts = self.wave[-1][0] + self.sample_dt * 0.05
                    self._dev_t = now
            self.latest = vals
            self.push_count += 1
            if frame.cmd == CMD_START:
                self._tune_rate(now)                   # 实测节拍，修正 sample_dt
            self.wave.append((ts, vals))
            # 攒够一批再裁，别每帧都搬一次列表
            if len(self.wave) >= self.WAVE_MAX + self.WAVE_SLACK:
                del self.wave[:len(self.wave) - self.WAVE_MAX]
            if self.log_raw and self.log_stream_every and \
                    self.push_count % self.log_stream_every == 0:
                self._log("rx", f"← {hexs(frame.raw)}   [推流帧 #{self.push_count}]")
            if self.on_push is not None:
                try:
                    self.on_push(vals, ts)
                except Exception:            # 回调里的异常不许拖垮收包
                    pass
            self._serve(("data", frame.cmd), frame=frame)
            return

        if frame.kind == "unknown":
            self.cmd_errors += 1
            self._log("warn", f"收到无法识别的内容：{hexs(frame.raw[:24])}")
            return

        self._serve((frame.kind, frame.cmd), frame=frame)

    def _serve(self, key: Tuple[str, object], frame: Optional[Frame] = None,
               text: bytes = b"") -> None:
        w = self._waiter
        if w is None or w.ev.is_set() or w.key != key:
            return
        w.frame = frame
        w.text = text
        w.ev.set()

    # ------------------------------------------------------------------
    #  发送（所有出站字节都必须走这里）
    # ------------------------------------------------------------------
    def _bus_write(self, data: bytes) -> float:
        """
        等总线空闲再写下去，返回等待时间（毫秒）。

        这是**唯一的出站入口**。绕过去直接 transport.write() 就等于放弃了
        半双工仲裁 —— 设备正在发推流帧时插一帧进去，两边都会收到垃圾。
        """
        with self._bus_lock:
            waited = self.bus.wait_idle(lambda: self._asm.pending() > 0)
            self.transport.write(data)
            self.bus.note_tx()
        return waited

    # ------------------------------------------------------------------
    #  请求 / 响应
    # ------------------------------------------------------------------
    def _transact(self, cmd: int, expect_kind: str, payload: bytes = b"",
                  timeout: float = 1.0, want_text: bool = False,
                  resp_cmd: Optional[int] = None) -> Optional[Frame]:
        if not self.is_connected():
            raise Y75Error("设备未连接")
        key = ("text", None) if want_text else \
            (expect_kind, resp_cmd if resp_cmd is not None else (cmd | RESP_FLAG))
        attempts = 1 + max(0, int(self.bus_retry))
        with self._cmd_lock:
            for attempt in range(attempts):
                frame, ok = self._transact_once(cmd, key, payload, timeout)
                if ok:
                    return frame
                self.cmd_errors += 1
                if attempt + 1 < attempts:
                    self._log("warn",
                              f"{frame_text(cmd)} 无应答，等够帧间空隙后重发"
                              f"（第 {attempt + 1}/{attempts - 1} 次）")
                    continue
                raise Y75Timeout(f"{frame_text(cmd)}：等待响应超时（{timeout:g}s）")
        return None

    def _transact_once(self, cmd: int, key: Tuple[str, object], payload: bytes,
                       timeout: float) -> Tuple[Optional[Frame], bool]:
        """发一帧并等指定应答，返回 (帧, 是否收到)。不等重发，由调用方决定。"""
        w = _Waiter(key)
        self._waiter = w
        try:
            req = build_cmd(cmd, payload)
            self._log("tx", f"→ {hexs(req)}   [{frame_text(cmd)}]")
            waited = self._bus_write(req)
            if waited >= 0.5:
                self._log("tx", f"   等总线空闲 {waited:.2f} ms 才发出"
                                 f"（避开设备正在发的那一帧）")
            ok = w.ev.wait(timeout)      # 必须先等，再取 frame（frame 是等待中途填的）
            return w.frame, ok
        finally:
            self._waiter = None

    # ------------------------------------------------------------------
    #  具体命令
    # ------------------------------------------------------------------
    def stop(self) -> None:
        try:
            self._transact(CMD_STOP, "ack", timeout=1.0)
        finally:
            # 超时也一样要重锚：命令可能已经到了设备，流真的停了
            self._break_stream_clock()

    def start(self) -> None:
        try:
            self._transact(CMD_START, "ack", timeout=1.0)
        finally:
            self._break_stream_clock()

    def _break_stream_clock(self) -> None:
        """
        起停推流 = 时间轴断档，把「设备时钟」和「节拍统计窗」都重锚。

        设备时钟是「上一帧的时间 + sample_dt」推算出来的，中间停了几百
        毫秒到几秒：这段时间既没有帧，也没有节拍样本，会同时坏两件事。

        1. `_dev_t` 停在断流那一刻。重启后第一帧只有差值 >0.4s 才重锚，
           差值小于它就一路带着这个滞后往前走 —— 波形右端永远缺一截，
           看起来就是「只显示一半」。
        2. `_rate_t0/_rate_n0` 横跨空洞。0.5s 统计窗里只剩重启后的几十帧，
           量出来的帧周期是真实值的几倍，`sample_dt` 被高估；而它正是
           等间隔推算的步长，于是时间戳一路冲进未来，可视窗里只剩旧数据。

        清掉这两样，下一帧会立刻按本机时间重新对钟、重新起统计窗。
        """
        self._dev_t = 0.0
        self._rate_t0 = 0.0
        self._rate_n0 = self.push_count

    def single(self, timeout: float = 1.0) -> Tuple[Optional[float], ...]:
        """单次采集：设备直接回一帧 cmd=0x03 的数据，没有 ACK。"""
        if not self.is_connected():
            raise Y75Error("设备未连接")
        base = self.push_count
        with self._cmd_lock:
            self._waiter = w = _Waiter(("data", CMD_SINGLE))
            try:
                req = build_cmd(CMD_SINGLE)
                self._log("tx", f"→ {hexs(req)}   [单次采集]")
                waited = self._bus_write(req)
                if waited >= 0.5:
                    self._log("tx", f"   等总线空闲 {waited:.2f} ms 才发出"
                                     f"（避开设备正在发的那一帧）")
                got = w.ev.wait(timeout)
            finally:
                self._waiter = None
        if not got:
            if self.push_count != base:
                return self.latest    # 设备正在推流，拿到的也是刚采的值
            raise Y75Timeout(f"单次采集等待数据帧超时（{timeout:g}s）")
        return ByteOrder.decode6(w.frame.payload, self.byte_order) if w.frame else self.latest

    def query_sn(self, timeout: float = 1.2) -> str:
        while self.text_log:
            self.text_log.pop()
        self._text_buf.clear()
        self._transact(CMD_QUERY_SN, "text", timeout=timeout, want_text=True)
        return self.text_log[-1].strip() if self.text_log else ""

    def query_ver(self, timeout: float = 1.0) -> str:
        f = self._transact(CMD_QUERY_VER, "ver", timeout=timeout)
        if f is None or len(f.payload) < 3:
            raise Y75Error("固件版本回复长度不足")
        major, minor, patch = f.payload[0], f.payload[1], f.payload[2]
        return f"V{major}.{minor}.{patch}" if major else f"V{minor}.{patch}"

    def query_status(self, timeout: float = 1.0) -> Dict[str, object]:
        f = self._transact(CMD_QUERY_STATUS, "status", timeout=timeout)
        if f is None or len(f.payload) < 8:
            raise Y75Error("运行状态回复长度不足")
        return parse_status(f.payload)

    def set_freq(self, hz: int) -> None:
        self._transact(CMD_SET_FREQ, "ack", bytes((0 if int(hz) == 500 else 1,)))
        self.note_freq_hz(hz)

    def note_freq_hz(self, hz: int) -> None:
        """同步「标称采样周期」，推流帧的时间戳按它等间隔推算。"""
        self._nominal_dt = 1.0 / (1000.0 if int(hz) == 1000 else 500.0)
        self.sample_dt = self._nominal_dt
        self.meas_dt = 0.0                 # 换档后重新实测
        self._rate_t0 = 0.0
        self._dev_t = 0.0                  # 换档也等于一次断档，设备时钟重锚
        self.bus.sample_dt = self.sample_dt

    def _tune_rate(self, now: float) -> None:
        """
        实测推流节拍，自动修正 sample_dt（0.5s 一个窗，EWMA 平滑）。

        协议里只有 500/1000Hz 两档，但真机的推流节拍由固件主循环决定
        （rs485_send_continuous 用 `g_last = g_tick_ms` 而不是 `+= interval`，
        主循环一旦慢于 2ms，实际推流率就跟着掉），实测常落在 300~500Hz。
        如果硬按 500Hz 推算设备时钟，时钟就只能靠 5% 的柔性纠偏慢慢追，
        稳态落后真实时间几十毫秒 —— 波形的显示延迟被迫做大，跟手性变差。
        """
        if self._rate_t0 <= 0.0:
            self._rate_t0, self._rate_n0 = now, self.push_count
            return
        span = now - self._rate_t0
        if span < 0.5:
            return
        n = self.push_count - self._rate_n0
        self._rate_t0, self._rate_n0 = now, self.push_count
        if n < 25 or span <= 0.0:
            return
        if span > 2.0:
            # 这一窗横跨了断流（暂停/重连/切到单次采集）：期间没有帧，
            # 拿 span/n 算出来的只是「空洞 + 一小段推流」的平均，不是节拍。
            # 信了它，sample_dt 会被放大好几倍，时间戳跟着冲进未来。
            return
        meas = span / n                                    # 实测帧周期
        if not 0.0005 <= meas <= 0.05:                     # 20Hz ~ 2kHz 之外不采信
            return
        self.meas_dt = (meas if self.meas_dt <= 0.0
                        else self.meas_dt * 0.7 + meas * 0.3)
        self.sample_dt = self.meas_dt
        self.bus.sample_dt = self.sample_dt    # 帧间空隙的判定跟着实测节拍走

    def set_baud(self, baud: int) -> int:
        """下发新波特率，返回波特率码。调用方收到 ACK 后需自行切换串口参数。"""
        code = BAUD_TO_CODE.get(int(baud))
        if code is None:
            raise Y75Error(f"设备不支持 {baud}，可选 {list(BAUD_CHOICES)}")
        self._transact(CMD_SET_BAUD, "ack", bytes((code,)))
        return code

    def zero(self) -> None:
        """置零：设备异步采 20 点，ACK 只代表「已开始」。"""
        self._transact(CMD_ZERO, "ack", timeout=3.0)

    def unzero(self) -> None:
        self._transact(CMD_UNZERO, "ack")

    def set_hex_mode(self) -> None:
        self._transact(CMD_HEX_SEND, "ack")
        self.asc_mode = False

    def set_asc_mode(self) -> None:
        self._transact(CMD_ASC_SEND, "ack")
        self.asc_mode = True
        self._log("warn", "已切到 ASC 调试输出：数据帧变成裸 28 字节，无帧头帧尾")

    def set_format(self, fmt: int) -> None:
        cmd = {0: CMD_SET_MV, 1: CMD_SET_KG, 2: CMD_SET_N}.get(int(fmt))
        if cmd is None:
            raise Y75Error(f"未知数据单位：{fmt}")
        self._transact(cmd, "ack")

    def set_matrix_row(self, row: int, values: Sequence[float]) -> None:
        """上传解耦矩阵的一行（0 起）。载荷 = 行号 + 6×float32 = 25 字节。"""
        if not 0 <= int(row) <= 5:
            raise Y75Error("矩阵行号必须是 0..5")
        if len(values) != 6:
            raise Y75Error("上传矩阵行必须正好 6 个数")
        payload = bytes((int(row),)) + ByteOrder.encode6(values, self.byte_order)
        self._transact(CMD_SET_MATRIX, "matrixack", payload, timeout=2.0,
                       resp_cmd=(0xA0 | int(row)))

    def query_matrix(self, timeout: float = 2.0) -> List[List[float]]:
        """读取 6×6 解耦矩阵。设备回 6 行裸 ASCII。"""
        attempts = 1 + max(0, int(self.bus_retry))
        last_text: List[str] = []
        for attempt in range(attempts):
            while self.text_log:
                self.text_log.pop()
            self._text_buf.clear()
            req = build_cmd(CMD_QUERY_MATRIX)
            with self._cmd_lock:
                self._log("tx", f"→ {hexs(req)}   [查询矩阵]")
                waited = self._bus_write(req)
                if waited >= 0.5:
                    self._log("tx", f"   等总线空闲 {waited:.2f} ms 才发出"
                                     f"（避开设备正在发的那一帧）")
            deadline = time.time() + timeout
            while time.time() < deadline:
                if parse_matrix_text("\n".join(self.text_log)) is not None:
                    break
                time.sleep(0.03)
            mat = parse_matrix_text("\n".join(self.text_log))
            if mat is not None:
                return mat
            last_text = list(self.text_log)
            if attempt + 1 < attempts:
                self._log("warn", "矩阵回复没解析出来，等够帧间空隙后重试一次")
        raise Y75Error("矩阵回复解析失败，收到的文本：" +
                       " | ".join(t.strip().replace("\r", "") for t in last_text))

    def send_raw(self, data: bytes, note: str = "手动发送") -> None:
        """直接把一串字节写下去（「自定义报文」用）。"""
        if not self.is_connected():
            raise Y75Error("设备未连接")
        with self._cmd_lock:        # 别插进别人的「发一条等一条」中间
            self._log("tx", f"→ {hexs(data)}   [{note}]")
            waited = self._bus_write(data)
            if waited >= 0.5:
                self._log("tx", f"   等总线空闲 {waited:.2f} ms 才发出"
                                 f"（避开设备正在发的那一帧）")

    # ------------------------------------------------------------------
    #  波形辅助
    # ------------------------------------------------------------------
    def take_wave(self, window_sec: Optional[float] = None,
                  max_points: int = 0
                  ) -> Tuple[int, List[Tuple[float, Tuple[float, ...]]]]:
        """
        取波形快照，返回 (push_count, 样本列表)。

        界面拿计数判断「这次有没有新数据」，没变就不用重绘。

        window_sec 不为空时，只返回「最新样点往前 window_sec 秒」这一段。
        max_points 不为 0 时改按**个数**取最近这么多个点（点数横轴用）。

        波形缓冲是 20s × 1000Hz = 2 万帧，如果每帧都把整段复制一遍，
        白白花掉大量时间在复制不可见的旧数据上 —— 这也是波形发卡的一个来源。
        """
        n = self.push_count
        if n == self._push_snapshot:
            return n, []
        self._push_snapshot = n
        wave = self.wave
        if not wave:
            return n, []
        if max_points and max_points > 0:
            # 按点数取：切片只复制引用，2 万个元素也不到 0.2ms
            return n, (wave[-max_points:] if len(wave) > max_points
                       else list(wave))
        if window_sec and window_sec > 0:
            t0 = wave[-1][0] - window_sec
            return n, wave[bisect_left(wave, (t0,)):]
        return n, list(wave)

    @property
    def short_frames(self) -> int:
        """解析器跳过的「空载荷畸形帧」数（AA 55 [cmd] 0D 0A）。"""
        return self._asm.short_frames

    def _log(self, level: str, msg: str) -> None:
        if self.on_log is not None:
            try:
                self.on_log(level, msg)
            except Exception:
                pass

    def _log_every(self, key: str, level: str, msg: str) -> None:
        """
        同类日志限流：同一 key 最多每 `LOG_GATE_SEC` 秒记一条。

        总线上的畸形帧/干扰可能每秒几百个，逐个记日志会把界面的文本控件
        刷爆 —— 实测能让重绘帧率掉掉三分之一、CPU 从 58% 涨到 79%。
        被压掉的条数会合并进下一条日志里，信息不丢。
        """
        now = time.time()
        last, n = self._log_gate.get(key, (0.0, 0))
        if last and now - last < self.LOG_GATE_SEC:
            self._log_gate[key] = (last, n + 1)     # 压掉，只累加计数
            return
        if n:
            msg = f"{msg}（另有 {n} 条同类已合并）"
        self._log_gate[key] = (now, 0)
        self._log(level, msg)

    def _state(self, state: str, detail: str) -> None:
        if self.on_state is not None:
            try:
                self.on_state(state, detail)
            except Exception:
                pass


# =====================================================================
#  解析辅助
# =====================================================================
def parse_status(payload: bytes) -> Dict[str, object]:
    """
    0x07 的状态回复（8 字节，全部小端）：
        send_mode / data_format / freq_mode / debug_mode
        status_flags(2) / comm_error_cnt(2)
    """
    d = bytes(payload)
    while len(d) < 8:
        d += b"\x00"
    send_mode, data_format, freq_mode, debug_mode = d[0], d[1], d[2], d[3]
    flags = d[4] | (d[5] << 8)
    errs = d[6] | (d[7] << 8)
    active = [name for bit, name in STATUS_BITS if flags & (1 << bit)]
    return {
        "send_mode": send_mode,
        "send_mode_text": {0: "已停止", 1: "连续发送中", 2: "单次"}.get(send_mode, "未知"),
        "data_format": data_format,
        "data_format_text": FORMAT_LABEL.get(data_format, "未知"),
        "freq_mode": freq_mode,
        "freq_hz": 1000 if freq_mode else 500,
        "debug_mode": debug_mode,
        "debug_mode_text": {0: "HEX 标准帧", 1: "ASC 裸帧"}.get(debug_mode, f"其它({debug_mode})"),
        "status_flags": flags,
        "status_text": "、".join(active) if active else "—",
        "comm_error": errs,
    }


def parse_matrix_text(text: str) -> Optional[List[List[float]]]:
    """
    解析 0x37 的回复：

        matrix1: 0.298418, -0.211604, ...
        ……共 6 行

    行号不齐、数量不足、数值非法都返回 None。
    """
    rows: Dict[int, List[float]] = {}
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line.lower().startswith("matrix"):
            continue
        head, sep, body = line.partition(":")
        if not sep:
            continue
        idx = head[6:].strip()
        if not idx.isdigit():
            continue
        row = int(idx) - 1
        if not 0 <= row <= 5:
            continue
        vals: List[float] = []
        for piece in body.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                vals.append(float(piece))
            except ValueError:
                vals = []
                break
        if len(vals) == 6:
            rows[row] = vals
    if len(rows) != 6:
        return None
    return [rows[i] for i in range(6)]


def dump_matrix(mat: Sequence[Sequence[float]]) -> str:
    """矩阵 → 6 行可读文本。"""
    return "\n".join(
        f"matrix{i + 1}: " + ", ".join(f"{v: .6f}" for v in mat[i]) for i in range(6))


def fmt_value(v: Optional[float], digits: int = 4) -> str:
    """数值显示：无效值一律显示「—」，绝不编造。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f or f in (float("inf"), float("-inf")):
        return "—"
    return f"{f:.{digits}f}"


# =====================================================================
#  自检：不连设备的纯逻辑测试
# =====================================================================
def _selftest() -> int:                              # pragma: no cover
    import math
    bad = 0

    def check(name, cond, detail=""):
        nonlocal bad
        print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
        if not cond:
            bad += 1

    # ---- 组帧 ----
    check("组帧 停止", build_cmd(CMD_STOP) == b"\xAA\x55\x01\x0D\x0A",
          hexs(build_cmd(CMD_STOP)))
    check("组帧 设置频率", build_cmd(CMD_SET_FREQ, b"\x00") == b"\xAA\x55\x08\x00\x0D\x0A")

    # ---- 字节序 ----
    check("DCBA(小端) 编码 1000.0",
          ByteOrder.encode4(1000.0, "DCBA") == b"\x00\x00\x7A\x44",
          hexs(ByteOrder.encode4(1000.0, "DCBA")))
    check("ABCD(大端) 编码 1000.0",
          ByteOrder.encode4(1000.0, "ABCD") == b"\x44\x7A\x00\x00",
          hexs(ByteOrder.encode4(1000.0, "ABCD")))
    for order in ByteOrder.NAMES:
        back = ByteOrder.decode4(ByteOrder.encode4(-12.5, order), order)
        check(f"字节序 {order} 往返一致", abs(back + 12.5) < 1e-6, f"{back}")

    # ---- 拆帧：粘包 / 半包 / 脏数据 ----
    data = SOF + bytes((CMD_START,)) + ByteOrder.encode6((1, 2, 3, 4, 5, 6)) + EOF
    asm = FrameAssembler()
    asm.feed(data * 3)
    frames = []
    while True:
        f = asm.pull_frame()
        if f is None:
            break
        frames.append(f)
    check("一次喂 3 帧能全部拆出", len(frames) == 3, f"得到 {len(frames)}")
    check("帧载荷解码正确",
          bool(frames) and abs(ByteOrder.decode6(frames[0].payload)[3] - 4.0) < 1e-6)

    asm = FrameAssembler()
    asm.feed(data[:10])
    check("半包不产出帧", asm.pull_frame() is None, f"pending={asm.pending()}")
    asm.feed(data[10:])
    check("补齐后产出 1 帧", asm.pull_frame() is not None)

    asm = FrameAssembler()
    asm.feed(b"\x11\x22\x33" + data)
    f = asm.pull_frame()
    check("前导脏字节能对齐", f is not None and f.cmd == CMD_START)

    # 载荷里混入 AA 55 与 0D 0A —— 已知长度表必须扛得住
    tricky = bytearray(ByteOrder.encode6((1.0, 2.0, 3.0, 4.0, 5.0, 6.0)))
    tricky[5:9] = b"\xAA\x55\x0D\x0A"
    asm = FrameAssembler()
    asm.feed(SOF + bytes((CMD_START,)) + bytes(tricky) + EOF + build_cmd(CMD_START | RESP_FLAG))
    f1 = asm.pull_frame()
    f2 = asm.pull_frame()
    check("载荷含 AA55/0D0A 时不误切帧",
          f1 is not None and f1.cmd == CMD_START and len(f1.payload) == 24 and
          f2 is not None and f2.cmd == CMD_START | RESP_FLAG,
          f"{f1} / {f2}")

    # ---- 空载荷畸形帧：AA 55 [cmd] 0D 0A ----
    # 真机上偶尔能抓到（干扰截断 / 固件在某些状态下发的短帧）。必须整帧跳过，
    # 否则每个畸形帧要走 5 次「丢 1 字节重同步」，把正常帧的解析一起拖慢。
    asm = FrameAssembler()
    asm.feed(b"\xAA\x55\x02\x0D\x0A")
    f = asm.pull_frame()
    check("空载荷帧被识别出来（而不是当成长度异常）",
          f is not None and f.cmd == CMD_START and f.payload == b"", str(f))
    check("空载荷帧计入 short_frames", asm.short_frames == 1, f"{asm.short_frames}")
    check("空载荷帧不触发重同步", asm.resync == 0, f"resync={asm.resync}")
    check("空载荷帧消费后缓冲干净", asm.pending() == 0, f"pending={asm.pending()}")

    asm = FrameAssembler()
    asm.feed(b"\xAA\x55\x02\x0D\x0A" * 3 + data)
    got = []
    while True:
        fr = asm.pull_frame()
        if fr is None:
            break
        got.append(fr)
    check("畸形帧后面紧跟的正常帧照样能解析",
          len(got) == 4 and got[-1].cmd == CMD_START and len(got[-1].payload) == 24,
          f"得到 {len(got)} 帧")
    check("3 个畸形帧全部计入", asm.short_frames == 3, f"{asm.short_frames}")

    # ACK 也是 AA 55 [cmd] 0D 0A 的长相，绝不能被误当成畸形帧
    asm = FrameAssembler()
    asm.feed(b"\xAA\x55\x81\x0D\x0A")
    f = asm.pull_frame()
    check("0 载荷的 ACK 不会被误判为畸形帧",
          f is not None and f.cmd == 0x81 and asm.short_frames == 0,
          f"{f} short={asm.short_frames}")

    # moved：调用方据此决定「继续解析」还是「等下一批数据」
    asm = FrameAssembler()
    asm.feed(b"\xAA\x55\x02" + bytes(24) + b"\x00\x00")   # 帧尾不是 0D 0A
    f = asm.pull_frame()
    check("帧尾不对时丢 1 字节并标记有进展",
          f is None and asm.moved is True and asm.resync == 1,
          f"moved={asm.moved} resync={asm.resync}")
    asm = FrameAssembler()
    asm.feed(data[:10])
    asm.pull_frame()
    check("数据不够时 moved 为假（该等下一批）", asm.moved is False)

    # ---- ACK 与裸文本分离 ----
    asm = FrameAssembler()
    asm.feed(b"HS-01234567")
    check("裸文本能被取出", asm.peek_text() == b"HS-01234567", asm.peek_text())
    asm.drop(11)
    check("消费后缓冲为空", asm.pending() == 0)
    asm = FrameAssembler()
    asm.feed(b"\xAA\x55\x82\x0D\x0A")
    check("以帧头开头时不误判为文本", asm.peek_text() == b"")
    check("文本判定：ASCII 通过", looks_like_text(b"matrix1: 1.0, 2.0\r\n"))
    check("文本判定：二进制被拒", not looks_like_text(bytes(range(0, 32))))

    # ---- 响应分类：0x02 与 0x82 只差一个 bit，绝不能混 ----
    check("0x02 是数据帧", resp_kind(0x02) == ("data", 24), str(resp_kind(0x02)))
    check("0x82 只是 ACK", resp_kind(0x82) == ("ack", 0), str(resp_kind(0x82)))
    check("0x03 是数据帧", resp_kind(0x03) == ("data", 24), str(resp_kind(0x03)))
    check("0x83 只是 ACK", resp_kind(0x83) == ("ack", 0), str(resp_kind(0x83)))
    check("0x86 是版本", resp_kind(0x86) == ("ver", 3), str(resp_kind(0x86)))
    check("0x87 是状态", resp_kind(0x87) == ("status", 8), str(resp_kind(0x87)))
    check("0xA3 是矩阵行 ACK", resp_kind(0xA3) == ("matrixack", 0), str(resp_kind(0xA3)))
    check("0xB0 是置零 ACK", resp_kind(0xB0) == ("ack", 0), str(resp_kind(0xB0)))
    check("带标志的未知码当成普通 ACK（按协议约定：响应 = 请求|0x80）",
          resp_kind(0xEE) == ("ack", 0), str(resp_kind(0xEE)))
    check("不带标志的未知码才是 unknown",
          resp_kind(0x1F) == ("unknown", None), str(resp_kind(0x1F)))
    asm = FrameAssembler()
    asm.feed(b"\xAA\x55\x82\x0D\x0A" + b"\xAA\x55\x87" + bytes(8) + b"\x0D\x0A")
    fa = asm.pull_frame()
    fb = asm.pull_frame()
    check("0x82 与 0x87 相邻时都能正确切出",
          fa is not None and fa.cmd == 0x82 and fb is not None and
          fb.cmd == 0x87 and len(fb.payload) == 8)

    # ---- 状态解析 ----
    st = parse_status(bytes((SEND_CONT, 2, 1, 0, 0x03, 0x00, 34, 0x00)))
    check("状态 发送模式", st["send_mode_text"] == "连续发送中", str(st["send_mode_text"]))
    check("状态 数据单位", str(st["data_format_text"]).endswith("(N)"), str(st["data_format_text"]))
    check("状态 输出频率", st["freq_hz"] == 1000)
    check("状态 标志位", "自动启动" in str(st["status_text"]) and "运行中" in str(st["status_text"]))
    check("状态 错误计数", st["comm_error"] == 34)

    # ---- 矩阵文本 ----
    lines = "\r\n".join(f"matrix{i + 1}: " + ", ".join(f"{i + j * 0.1:.6f}" for j in range(6))
                        for i in range(6))
    mat = parse_matrix_text(lines)
    check("矩阵文本解析 6×6", mat is not None and len(mat) == 6 and len(mat[0]) == 6)
    check("矩阵首元素正确", bool(mat) and abs(mat[0][0]) < 1e-9)
    check("矩阵行号不全会失败", parse_matrix_text("matrix1: 1,2,3,4,5,6") is None)
    check("矩阵数值非法会失败", parse_matrix_text(lines.replace("0.000000", "abc", 1)) is None)

    # ---- ASC 裸帧 ----
    cli = Y75Client(TcpTransport())
    cli.asc_mode = True
    got: List[Tuple[float, ...]] = []
    cli.on_push = lambda vals, ts: got.append(vals)
    raw = ByteOrder.encode6((1, 2, 3, 4, 5, 6)) + ASC_SENTINEL
    cli._asm.feed(raw * 2)
    cli._drain()
    check("ASC 裸帧识别 2 帧", len(got) == 2, f"得到 {len(got)}")
    check("ASC 裸帧数值正确", bool(got) and abs(got[1][5] - 6.0) < 1e-6)
    check("ASC 解析后缓冲清空", cli._asm.pending() == 0, str(cli._asm.pending()))

    cli._asm.feed(raw[:10])
    cli._drain()
    check("ASC 半帧不误判为文本", cli._asm.pending() == 10 and not cli._text_buf,
          f"pending={cli._asm.pending()} text={len(cli._text_buf)}")
    cli._asm.feed(raw[10:])
    cli._drain()
    check("ASC 半帧补齐后成帧", len(got) == 3, f"得到 {len(got)}")

    # ---- HEX 模式下 ACK 与 ASC 混流 ----
    cli2 = Y75Client(TcpTransport())
    got2: List[Tuple[float, ...]] = []
    cli2.on_push = lambda vals, ts: got2.append(vals)
    cli2._asm.feed(build_cmd(CMD_START | RESP_FLAG))
    cli2._drain()
    check("HEX 模式 ACK 不进数据缓冲", not got2 and cli2._asm.pending() == 0)

    # ---- 成批到达：设备时钟不许塌缩（波形「竖条」修复） ----
    # 复现老 bug：先喂 1 帧锚定设备时钟，再一口气喂 30 帧（时钟被推到未来
    # 60ms），隔 10ms 再喂 30 帧 —— 老版本按批算出的纠偏量为负且超过帧
    # 周期，批内时间戳原地踏步，画出来就是竖条。
    cli3 = Y75Client(TcpTransport())
    cli3.note_freq_hz(500)
    f6 = SOF + bytes((CMD_START,)) + ByteOrder.encode6((1, 2, 3, 4, 5, 6)) + EOF
    cli3._asm.feed(f6)
    cli3._drain()
    time.sleep(0.02)                       # 让锚定时刻落在真实时间后面一点
    cli3._asm.feed(f6 * 30)                # 一大批：设备时钟被推快约 60ms
    cli3._drain()
    time.sleep(0.01)
    cli3._asm.feed(f6 * 30)                # 再来一批：老版本这里就出竖条
    cli3._drain()
    ts_w = [t for t, _ in cli3.wave]
    gaps = [b - a for a, b in zip(ts_w, ts_w[1:])]
    dt = cli3.sample_dt
    check("成批到达下帧间隔不塌缩（无竖条）",
          len(gaps) > 50 and gaps and min(gaps) >= 0.5 * dt,
          f"帧数={len(gaps)} min_gap={min(gaps) * 1000 if gaps else -1:.3f}ms "
          f"dt={dt * 1000:.3f}ms")
    check("帧间隔也未被拉大（时间轴匀速）",
          bool(gaps) and max(gaps) <= 1.6 * dt,   # 批边界含 ±0.5dt 的对钟步长
          f"max_gap={max(gaps) * 1000 if gaps else -1:.3f}ms")
    check("时间戳严格递增", all(b > a for a, b in zip(ts_w, ts_w[1:])))

    # ---- 显示格式 ----
    check("无效值显示 —", fmt_value(None) == "—" and fmt_value(float("nan")) == "—" and
          fmt_value(float("inf")) == "—")
    check("正常值 4 位小数", fmt_value(1.234567) == "1.2346", fmt_value(1.234567))
    check("float32 精度还原", abs(math.pi - ByteOrder.decode4(ByteOrder.encode4(math.pi))) < 1e-7)

    print(f"\n===== 协议层自检：{'全部通过' if bad == 0 else f'{bad} 项失败'} =====")
    return 0 if bad == 0 else 1


if __name__ == "__main__":                           # pragma: no cover
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
