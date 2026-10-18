# -*- coding: utf-8 -*-
"""
y75_serial.py —— Windows 串口后端（纯 ctypes，零第三方依赖）

为什么不用 pyserial：
    上位机要保持「纯标准库 + 单文件打包」。pyserial 虽然成熟，但会引入
    一个额外依赖；而本工具只需要「打开一个 COM 口 + 读写字节」，
    直接调 kernel32/SetupAPI 就是一百多行的事，打包时也不用额外处理。

对外只暴露两样东西：
    list_ports()      → [(端口名, 友好名), ...]
    SerialPort        → Transport 实现（open / close / recv / write）

注意：本文件只在 Windows 上有意义，其它平台 import 会失败（提示清楚）。
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from ctypes import wintypes
from typing import List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")

# =====================================================================
#  常量
# =====================================================================
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080

PURGE_TXABORT = 0x0001
PURGE_RXABORT = 0x0002
PURGE_TXCLEAR = 0x0004
PURGE_RXCLEAR = 0x0008

DTR_CONTROL_ENABLE = 1
RTS_CONTROL_ENABLE = 1

#: 一次 ReadFile 请求的字节数。太小 → 读得勤但 CPU 高；
#: 太大 → 低帧率下延迟明显。512 在 460800bps 下约 11ms 凑满，够灵敏。
#: 但这只是「背锅值」：真正的返回时机由下面的间隔超时决定。
#: 取 256（≈8.8 帧 @29 字节）：即使间隔超时因为驱动原因不生效，
#: 单次延迟也被硬限在 ~18ms 以内，不会攒出 35~50ms 一大坨。
READ_CHUNK = 256

#: 读超时（毫秒）：
#: ReadIntervalTimeout 是「收到第一个字节后，还愿意等多久下一个字节」。
#: 取 1ms 很关键：设备是**逐帧连续**发的（500Hz 时帧间隔才 2ms），
#: 若这个值大于帧间隔，驱动上就永远凑不出一次「空隙」，ReadFile 只能
#: 一直等到 READ_CHUNK 攒满（512 字节 ≈ 35ms @500Hz）才把数据交上来 ——
#: 上位机于是每 35~50ms 才收到一大坨，画面怎么可能顺。
#: 1ms < 帧间隔，所以一批到齐后立刻返回，粒度就是「一帧」。
#: 更小的粒度只有靠把 USB 转串口的 Latency Timer 从默认 16ms 调到 1ms
#: （设备管理器 → 端口 → 属性 → 高级），这个驱动级的延迟程序改不了。
READ_INTERVAL_TIMEOUT = 1
#: 线上没数据时多久返回一次空包（让读线程有机会响应关闭请求）。
#: 只在这条线安静时生效，不影响正常收数据的延迟。
READ_TOTAL_TIMEOUT = 30
WRITE_TOTAL_TIMEOUT = 2000


class SerialError(RuntimeError):
    """串口操作失败。"""


# =====================================================================
#  kernel32 / setupapi 绑定
# =====================================================================
if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)

    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.ReadFile.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _kernel32.ReadFile.restype = wintypes.BOOL

    _kernel32.WriteFile.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.SetCommState.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    _kernel32.SetCommState.restype = wintypes.BOOL
    _kernel32.GetCommState.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    _kernel32.GetCommState.restype = wintypes.BOOL
    _kernel32.SetCommTimeouts.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    _kernel32.SetCommTimeouts.restype = wintypes.BOOL
    _kernel32.SetupComm.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    _kernel32.PurgeComm.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class _SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID),
                    ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

    #: GUID_DEVINTERFACE_COMPORT
    _GUID_COMPORT = _GUID(0x86E0D1E0, 0x8089, 0x11D0,
                          (ctypes.c_ubyte * 8)(0x9C, 0xE4, 0x08, 0x00, 0x3E, 0x30, 0x1F, 0x73))

    _setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    _setupapi.SetupDiGetClassDevsW.argtypes = (
        ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD)
    _setupapi.SetupDiEnumDeviceInfo.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA))
    _setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    _setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD))
    _setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = (wintypes.HANDLE,)

    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10
    SPDRP_FRIENDLYNAME = 0x0C


def _win_error(prefix: str) -> SerialError:
    err = ctypes.get_last_error()
    try:
        text = ctypes.FormatError(err).strip()
    except Exception:                                    # pragma: no cover
        text = ""
    return SerialError(f"{prefix}（Win32 错误 {err}：{text}）")


# =====================================================================
#  端口枚举
# =====================================================================
def _ports_from_setupapi() -> List[Tuple[str, str]]:
    """用 SetupAPI 拿到「COM 口 + 友好名」，例如 ('COM3', 'USB-SERIAL CH340 (COM3)')。"""
    import re
    found: List[Tuple[str, str]] = []
    hdev = _setupapi.SetupDiGetClassDevsW(
        ctypes.byref(_GUID_COMPORT), None, None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        return found
    try:
        info = _SP_DEVINFO_DATA()
        info.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        idx = 0
        buf = ctypes.create_unicode_buffer(512)
        while _setupapi.SetupDiEnumDeviceInfo(hdev, idx, ctypes.byref(info)):
            idx += 1
            need = wintypes.DWORD(0)
            ok = _setupapi.SetupDiGetDeviceRegistryPropertyW(
                hdev, ctypes.byref(info), SPDRP_FRIENDLYNAME, None,
                buf, ctypes.sizeof(buf), ctypes.byref(need))
            name = buf.value if ok else ""
            m = re.search(r"\((COM\d+)\)", name)
            if not m:
                continue
            com = m.group(1)
            found.append((com, name))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(hdev)

    def key(item: Tuple[str, str]) -> int:
        return int(item[0][3:]) if item[0][3:].isdigit() else 9999

    return sorted(found, key=key)


def _ports_from_registry() -> List[Tuple[str, str]]:
    """SetupAPI 不可用时的回退：读注册表的 SERIALCOMM 键。"""
    try:
        import winreg
    except ImportError:                                  # pragma: no cover
        return []
    out: List[Tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    dev, port, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                out.append((port, f"{port}  ({dev})"))
    except OSError:
        return []

    def key_of(item: Tuple[str, str]) -> int:
        return int(item[0][3:]) if item[0][3:].isdigit() else 9999

    return sorted(out, key=key_of)


def list_ports() -> List[Tuple[str, str]]:
    """枚举本机可用串口，返回 [(端口名, 显示名)]，按端口号排序。"""
    if not IS_WINDOWS:
        return []
    try:
        ports = _ports_from_setupapi()
        if ports:
            return ports
    except Exception:
        pass
    try:
        return _ports_from_registry()
    except Exception:
        return []


# =====================================================================
#  SerialPort
# =====================================================================
class SerialPort:
    """
    一个已打开的串口。实现 Transport 协议（open / close / recv / write）。

    :param port:  端口名，'COM3' 或 '\\\\.\\COM10' 都行，内部会补前缀
    :param baud:  波特率
    :param dtr_rts: 是否拉高 DTR/RTS。USB-RS485 转换器多数靠它们取电或
                   使能收发器，所以默认打开；个别用 RTS 做方向控制的
                   转换器则需要关掉，此时改 False。
    """

    def __init__(self, port: str, baud: int = 460800, dtr_rts: bool = True,
                 byte_size: int = 8, parity: int = 0, stop_bits: int = 0):
        if not IS_WINDOWS:
            raise SerialError("串口功能目前只在 Windows 上实现")
        self.port = port.strip().upper()
        self.baud = int(baud)
        self.dtr_rts = bool(dtr_rts)
        self.byte_size = int(byte_size)      # 8
        self.parity = int(parity)            # 0 = none
        self.stop_bits = int(stop_bits)      # 0 = 1 stop bit
        self._handle = None
        self.rx_bytes = 0
        self.tx_bytes = 0

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return f"{self.port} @ {self.baud} bps"

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def _device_path(self) -> str:
        p = self.port
        if p.startswith("\\\\."):
            return p
        num = p[3:] if p.startswith("COM") else ""
        if num.isdigit() and int(num) > 9:
            return "\\\\.\\" + p            # COM10 及以上必须走这个前缀
        return p

    def open(self) -> None:
        if self._handle is not None:
            return
        handle = _kernel32.CreateFileW(
            self._device_path(), GENERIC_READ | GENERIC_WRITE, 0, None,
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise _win_error(f"打开串口 {self.port} 失败")

        try:
            self._setup_comm(handle)
        except Exception:
            _kernel32.CloseHandle(handle)
            raise
        self._handle = handle

    def _setup_comm(self, handle) -> None:
        _kernel32.SetupComm(handle, 4096, 4096)
        _kernel32.PurgeComm(handle, PURGE_RXCLEAR | PURGE_TXCLEAR)

        # ---- DCB：28 字节，字段顺序固定，用 struct 直接拼最省事 ----
        flags = 0x00000001                       # fBinary = 1
        if self.dtr_rts:
            flags |= DTR_CONTROL_ENABLE << 4     # fDtrControl
            flags |= RTS_CONTROL_ENABLE << 12    # fRtsControl
        flags |= 1 << 7                          # fTXContinueOnXoff
        dcb = struct.pack("<IIIHHHBBBcccccH",
                          28, self.baud, flags,
                          0, 0, 0,
                          self.byte_size, self.parity, self.stop_bits,
                          b"\x11", b"\x13", b"\x00", b"\x00", b"\x00", 0)
        dcb_buf = ctypes.create_string_buffer(dcb, len(dcb))
        if not _kernel32.SetCommState(handle, dcb_buf):
            raise _win_error(f"配置串口 {self.port} 参数失败")
        # 回读一次，确认驱动真的接受了（有些 USB 转串口会静默改参数）
        back = ctypes.create_string_buffer(28)
        if _kernel32.GetCommState(handle, back):
            real = struct.unpack("<IIIHHHBBBcccccH", back.raw)[1]
            if real != self.baud:
                raise SerialError(
                    f"串口 {self.port} 实际波特率是 {real}，与请求的 {self.baud} 不一致")

        # ---- 超时：有数据立刻返回，没数据最多等 READ_TOTAL_TIMEOUT ----
        timeouts = struct.pack("<IIIII",
                               READ_INTERVAL_TIMEOUT, 0, READ_TOTAL_TIMEOUT,
                               0, WRITE_TOTAL_TIMEOUT)
        if not _kernel32.SetCommTimeouts(handle, ctypes.create_string_buffer(timeouts, 20)):
            raise _win_error(f"设置串口 {self.port} 超时失败")

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                _kernel32.PurgeComm(handle, PURGE_RXABORT | PURGE_TXABORT |
                                    PURGE_RXCLEAR | PURGE_TXCLEAR)
            except Exception:
                pass
            _kernel32.CloseHandle(handle)

    def flush_input(self) -> None:
        if self._handle:
            _kernel32.PurgeComm(self._handle, PURGE_RXCLEAR)

    # ------------------------------------------------------------------
    def recv(self, timeout: float = 0.05) -> bytes:
        """
        读一包数据。

        `timeout` 参数被**忽略** —— 真正的等待策略在打开串口时就写进了
        DCB 超时（字节间隔超 1ms 或总共 30ms 就收手）。这样既不空转，
        也不会把已经拿到的帧再压后一段时间才交上去。
        """
        if self._handle is None:
            raise SerialError("串口未打开")
        buf = ctypes.create_string_buffer(READ_CHUNK)
        got = wintypes.DWORD(0)
        ok = _kernel32.ReadFile(self._handle, buf, READ_CHUNK,
                                ctypes.byref(got), None)
        if not ok:
            raise _win_error(f"读串口 {self.port} 失败")
        if got.value == 0:
            return b""
        self.rx_bytes += got.value
        return buf.raw[:got.value]

    def write(self, data: bytes) -> None:
        if self._handle is None:
            raise SerialError("串口未打开")
        if not data:
            return
        payload = ctypes.create_string_buffer(bytes(data), len(data))
        written = wintypes.DWORD(0)
        if not _kernel32.WriteFile(self._handle, payload, len(data),
                                   ctypes.byref(written), None):
            raise _win_error(f"写串口 {self.port} 失败")
        if written.value != len(data):
            raise SerialError(f"写串口 {self.port} 只发出 {written.value}/{len(data)} 字节")
        self.tx_bytes += written.value


# =====================================================================
#  自检 / 命令行
# =====================================================================
def _main(argv: List[str]) -> int:                       # pragma: no cover
    if "--selftest" in argv:
        bad = 0

        def check(name, cond, detail=""):
            nonlocal bad
            print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
            if not cond:
                bad += 1

        ports = list_ports()
        check("能枚举串口（列表可为空）", isinstance(ports, list), f"{len(ports)} 个")
        p = SerialPort("COM199", 460800)
        check("端口名规整", p.port == "COM199")
        check("COM10 以上加前缀", SerialPort("COM12")._device_path() == "\\\\.\\COM12")
        check("COM3 不加前缀", SerialPort("COM3")._device_path() == "COM3")
        try:
            p.open()
        except SerialError as exc:
            check("打开不存在的端口报错清楚", "COM199" in str(exc), str(exc)[:60])
        else:
            check("打开不存在的端口应当失败", False, "竟然成功了")
            p.close()
        check("未打开时 write 会报错",
              _raises(SerialError, lambda: p.write(b"\x00")))
        check("未打开时 recv 会报错",
              _raises(SerialError, lambda: p.recv(0.01)))
        print(f"\n===== 串口层自检：{'全部通过' if bad == 0 else f'{bad} 项失败'} =====")
        return 0 if bad == 0 else 1

    # 真机连通性检查：打开串口、静读 1 秒、**不发任何字节**
    if "--open-test" in argv:
        i = argv.index("--open-test")
        port = argv[i + 1] if len(argv) > i + 1 else ""
        baud = int(argv[argv.index("--baud") + 1]) if "--baud" in argv else 460800
        if not port:
            print("用法：python y75_serial.py --open-test COM28 [--baud 460800]")
            return 2
        sp = SerialPort(port, baud)
        try:
            sp.open()
        except SerialError as exc:
            print(f"[FAIL] {exc}")
            return 1
        print(f"[ OK ] 已打开 {sp.name}（DTR/RTS 已拉高）")
        try:
            import time as _t
            total = 0
            end = _t.time() + 1.0
            while _t.time() < end:
                chunk = sp.recv(0.05)
                total += len(chunk)
            print(f"[ OK ] 静读 1 秒成功，收到 {total} 字节 "
                  f"（未发任何命令，正常应为 0；有数据说明总线上有别的东西在说）")
        finally:
            sp.close()
        print("[ OK ] 关闭串口成功")
        return 0

    if "--list" in argv or len(argv) == 1:
        ports = list_ports()
        if not ports:
            print("没有发现任何串口。请确认 USB-RS485 转换器已插好并装好驱动。")
            return 1
        print("本机可用串口：")
        for dev, desc in ports:
            print(f"  {dev:<8} {desc}")
        return 0

    print(__doc__)
    return 0


def _raises(exc_type, fn) -> bool:                       # pragma: no cover
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":                               # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
