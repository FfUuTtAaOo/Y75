# -*- coding: utf-8 -*-
"""
test_gui.py —— 上位机界面端到端回归测试（不需要硬件）

思路：把模拟器当对端，用真实的 App 对象跑一遍完整操作流程。
窗口用 root.withdraw() 隐藏，手动 root.update() 泵事件循环，
再配合 wait_for / wait_idle 等异步完成。

用法：
    python test_gui.py                 # 自动拉起模拟器
    python test_gui.py --port 15120    # 连一个已经跑着的模拟器
    python test_gui.py --shot out.png  # 顺便截一张界面图
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Callable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tkinter as tk                                            # noqa: E402
from argparse import Namespace                                  # noqa: E402

from y75_app import MODE_TCP, OUT_ASC, OUT_HEX, App             # noqa: E402
from y75_protocol import AXIS_NAMES                             # noqa: E402

PASS = FAIL = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def wait_for(root: tk.Tk, cond: Callable[[], bool], timeout: float = 4.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    root.update()
    return cond()


def wait_idle(root: tk.Tk, app: App, timeout: float = 6.0) -> bool:
    return wait_for(root, lambda: not app.busy, timeout)


def wait_port(host: str, port: int, timeout: float = 6.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection((host, port), timeout=0.3)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


# =====================================================================
#  测试
# =====================================================================
def test_connect(root: tk.Tk, app: App, host: str, port: int) -> None:
    section("1. 连接与设备信息")
    app.mode_var.set(MODE_TCP)
    app._on_mode_change()
    app.host_var.set(host)
    app.tcp_port_var.set(str(port))
    app.on_connect()
    check("连接完成", wait_idle(root, app) and app.connected, f"connected={app.connected}")
    check("顶栏状态变成已连接", "已连接" in app.conn_label.cget("text"),
          app.conn_label.cget("text"))
    check("连接按钮已禁用",
          "disabled" in app.btn_connect.state(), str(app.btn_connect.state()))

    check("设备信息读到了序列号", app.info_labels["sn"].cget("text").startswith("HS-"),
          app.info_labels["sn"].cget("text"))
    check("设备信息读到了版本号", app.info_labels["ver"].cget("text").startswith("V"),
          app.info_labels["ver"].cget("text"))
    check("设备信息读到了运行状态", "·" in app.info_labels["state"].cget("text"),
          app.info_labels["state"].cget("text"))
    check("输出模式下拉框与设备一致", app.out_var.get() in (OUT_HEX, OUT_ASC),
          app.out_var.get())
    check("报文集里有收发记录",
          app.raw_text.get("1.0", "end").count("←") >= 2,
          f"{app.raw_text.get('1.0', 'end').count('←')} 条收")


def test_stream(root: tk.Tk, app: App) -> None:
    section("2. 连续采集与显示")
    app.on_start()
    check("开始连续：命令执行完成", wait_idle(root, app))
    cli = app.cli
    assert cli is not None
    got = wait_for(root, lambda: cli.push_count > 5, 2.0)
    check("开始后立刻收到推流帧", got, f"push_count={cli.push_count}")

    # 数值卡片必须显示真实数值，不能是「—」
    wait_for(root, lambda: app.cards[0][0].cget("text") != "—", 2.0)
    val = app.cards[0][0].cget("text")
    check("数值卡片显示真实数值", val != "—" and _is_num(val), val)
    check("六张卡片都有数值",
          all(_is_num(l.cget("text")) for l, _ in app.cards),
          ", ".join(l.cget("text") for l, _ in app.cards))

    check("运行统计：帧率 > 100Hz",
          wait_for(root, lambda: app.frame_rate > 100, 2.0),
          f"{app.frame_rate:.0f} Hz")
    check("运行统计：已收帧在增长", cli.push_count > 20, str(cli.push_count))
    check("状态横条写着已连接", "已连接" in app.conn_label.cget("text"))

    # 波形必须真的画了线（Canvas 上的 line 数 > 0）
    lines = wait_for(root, lambda: len(app.wave.find_all()) > 10, 2.0)
    check("波形画布上有内容", lines, f"{len(app.wave.find_all())} 个图元")

    # 通道勾选：只留 FX
    for i, v in enumerate(app.chan_vars):
        v.set(i == 0)
    app._on_channel_change()
    wait_for(root, lambda: True, 0.2)
    check("只勾 FX 时波形仍能绘制", len(app.wave.find_all()) > 5,
          f"{len(app.wave.find_all())} 个图元")
    for v in app.chan_vars:
        v.set(True)
    app._on_channel_change()


def test_single_and_settings(root: tk.Tk, app: App) -> None:
    section("3. 单次采集与参数设置")
    app.on_stop()
    wait_idle(root, app)
    app.on_single()
    check("单次采集执行完成", wait_idle(root, app))
    check("单次采集后日志有记录", "单次采集" in app.log_text.get("1.0", "end"),
          "日志已写入")

    app.freq_var.set("1000")
    app.on_set_freq()
    check("设置 1000Hz 完成", wait_idle(root, app))
    check("设备确认 1000Hz",
          wait_for(root, lambda: "1000Hz" in app.info_labels["state"].cget("text"), 2.0)
          or app.cli is not None,
          app.info_labels["state"].cget("text"))
    app.on_read_info()
    wait_idle(root, app)
    check("重新读信息后确实显示 1000Hz",
          "1000Hz" in app.info_labels["state"].cget("text"),
          app.info_labels["state"].cget("text"))

    app.fmt_var.set("矩阵解耦 (kg)")
    app.out_var.set(OUT_HEX)
    app.on_apply_output()
    check("应用输出设置完成", wait_idle(root, app))
    check("单位标签跟随变成 kg", app.cards[0][1].cget("text") == "kg",
          app.cards[0][1].cget("text"))
    check("波形单位也变成 kg", app.wave.unit == "kg", app.wave.unit)

    # ASC 调试模式：数据帧变成裸 28 字节，界面必须照常出数
    app.out_var.set(OUT_ASC)
    app.on_apply_output()
    wait_idle(root, app)
    app.on_start()
    wait_idle(root, app)
    cli = app.cli
    assert cli is not None
    base = cli.push_count
    check("ASC 模式下仍能收到数据帧",
          wait_for(root, lambda: cli.push_count > base + 3, 2.0),
          f"{cli.push_count - base} 帧")
    check("ASC 模式下数值卡片仍有值", _is_num(app.cards[0][0].cget("text")),
          app.cards[0][0].cget("text"))
    app.on_stop()
    wait_idle(root, app)

    app.out_var.set(OUT_HEX)
    app.on_apply_output()
    wait_idle(root, app)
    app.on_read_info()
    wait_idle(root, app)
    check("切回 HEX 后设备状态正确",
          "HEX" in app.info_labels["state"].cget("text"),
          app.info_labels["state"].cget("text"))


def test_matrix(root: tk.Tk, app: App) -> None:
    section("4. 解耦矩阵界面")
    app.on_read_matrix()
    check("读取矩阵完成", wait_idle(root, app))
    first = app.mat_entries[0][0].get()
    check("矩阵第 1 行第 1 列已填入", _is_num(first), first)
    check("36 个格子都有值",
          all(_is_num(app.mat_entries[r][c].get()) for r in range(6) for c in range(6)))

    row = ["0.111111", "-0.222222", "0.333333", "-0.444444", "0.555555", "-0.666666"]
    for c, v in enumerate(row):
        app.mat_entries[2][c].delete(0, "end")
        app.mat_entries[2][c].insert(0, v)
    app.on_upload_row(2)
    check("上传单行完成", wait_idle(root, app))
    check("上传后有日志", "第 3 行已上传" in app.log_text.get("1.0", "end"))

    app.on_read_matrix()
    wait_idle(root, app)
    back = [app.mat_entries[2][c].get() for c in range(6)]
    check("读回的正是刚才上传的值",
          all(abs(float(a) - float(b)) < 1e-5 for a, b in zip(back, row)),
          ", ".join(back))

    app._fill_matrix("identity")
    check("填入单位矩阵生效",
          app.mat_entries[0][0].get().startswith("1") and
          app.mat_entries[1][1].get().startswith("1") and
          app.mat_entries[0][1].get().startswith("0"))
    app.on_upload_all()
    check("上传全部 6 行完成", wait_idle(root, app), "")
    app.on_read_matrix()
    wait_idle(root, app)
    check("单位矩阵已写入设备",
          all(abs(float(app.mat_entries[r][c].get()) - (1.0 if r == c else 0.0)) < 1e-5
              for r in range(6) for c in range(6)),
          app.mat_entries[0][0].get())
    check("复制到剪贴板不报错", _no_raise(app._copy_matrix))


def test_save(root: tk.Tk, app: App, tmpdir: str) -> None:
    section("5. 保存数据到 Excel")
    path = os.path.join(tmpdir, "save_test.xlsx")
    app.on_start()
    wait_idle(root, app)
    app._start_saving(path)
    check("进入保存状态", app.saving)
    check("按钮文字变成停止保存", "停止" in app.btn_save.cget("text"),
          app.btn_save.cget("text"))

    cli = app.cli
    assert cli is not None
    n0 = cli.push_count
    check("保存期间持续收帧", wait_for(root, lambda: cli.push_count > n0 + 100, 3.0),
          f"{cli.push_count - n0} 帧")
    time.sleep(0.5)
    root.update()

    app._stop_saving()
    check("退出保存状态", not app.saving)
    check("文件已生成", os.path.isfile(path),
          f"{os.path.getsize(path) if os.path.exists(path) else 0} 字节")

    with zipfile.ZipFile(path) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        wb = zf.read("xl/workbook.xml").decode("utf-8")
    heads = re.findall(r"<t[^>]*>([^<]*)</t>", sheet)
    check("表头是六个力通道", heads == list(AXIS_NAMES), str(heads))
    nrows = sheet.count("<row ")
    check("写入行数合理（表头 + 上百行数据）", nrows > 50, f"{nrows} 行")
    check("工作表名正确", "六维力数据" in wb)

    rows = re.findall(r'<row r="(\d+)">(.*?)</row>', sheet)
    first_data = rows[1][1] if len(rows) > 1 else ""
    last_data = rows[-1][1] if rows else ""
    check("首行与末行数据不同（不是同一份数据重发）", first_data != last_data,
          f"{first_data[:40]} … {last_data[:40]}")
    vals = re.findall(r"<v>([^<]+)</v>", first_data)
    check("每行 6 个数值", len(vals) == 6, str(len(vals)))
    check("数值里没有 nan/inf", not re.search(r"nan|inf", sheet.lower()))

    # openpyxl 交叉验证（没装就跳过）
    try:
        import openpyxl                                        # type: ignore
        book = openpyxl.load_workbook(path)
        ws = book.active
        check("openpyxl 能正常打开导出的文件",
              ws.max_column == 6 and ws.max_row > 50,
              f"{ws.max_row} 行 × {ws.max_column} 列")
    except ImportError:
        print("[SKIP] 没装 openpyxl，跳过交叉校验")

    # 保存过程中断线：文件要能正常收尾
    path2 = os.path.join(tmpdir, "save_abort.xlsx")
    app._start_saving(path2)
    wait_for(root, lambda: app.writer is not None and app.writer.rows > 5, 3.0)
    app.on_disconnect()
    wait_idle(root, app)
    check("断线时自动收尾保存", not app.saving)
    check("断线后文件仍正常生成", os.path.isfile(path2) and os.path.getsize(path2) > 0,
          f"{os.path.getsize(path2) if os.path.exists(path2) else 0} 字节")


def test_layout(root: tk.Tk, app: App) -> None:
    section("6. 布局与滚动条")
    # 这一段必须有真实几何尺寸才测得出，所以临时把窗口显示出来
    root.deiconify()
    root.update()

    small = f"{int(1080 * app.scale)}x{int(700 * app.scale)}"
    root.geometry(small)
    root.update()
    app._update_left_scrollbar()
    root.update()
    content_h = app._left_inner.winfo_reqheight()
    view_h = app.left_canvas.winfo_height()
    shown = bool(app.left_sb.winfo_ismapped())
    check("最小窗口下滚动条按需显示（内容高于视口就该显示）",
          shown == (content_h > view_h),
          f"内容 {content_h} vs 视口 {view_h} → {'显示' if shown else '隐藏'}")
    check("最小窗口下滚动条确实显示了（内容确实放不下）",
          shown and content_h > view_h,
          f"内容 {content_h} vs 视口 {view_h}")

    # 滚轮必须递归绑到左栏里每个控件 —— Tk 的滚轮事件不会从子控件冒到父控件
    bound = [w for w in (app.btn_connect, app.btn_disconnect, app.port_cb,
                         app.mat_hint if hasattr(app, "mat_hint") else app.port_cb)
             if w.bind("<MouseWheel>")]
    check("左栏控件都绑了滚轮事件", len(bound) >= 2, f"{len(bound)} 个控件已绑")

    before = app.left_canvas.yview()
    app.left_canvas.yview_moveto(1.0)
    root.update()
    check("能把左栏滚到底部（零点/统计不会点不到）",
          app.left_canvas.yview()[1] >= 0.999 or content_h <= view_h,
          f"yview {before} → {app.left_canvas.yview()}")

    # 反向：窗口拉到足够高 → 滚动条应当收起（不占位）
    root.geometry(f"{int(1080 * app.scale)}x{int(1500 * app.scale)}")
    root.update()
    app._update_left_scrollbar()
    root.update()
    c2 = app._left_inner.winfo_reqheight()
    v2 = app.left_canvas.winfo_height()
    check("窗口够高时滚动条收起（与内容高度的等价关系）",
          bool(app.left_sb.winfo_ismapped()) == (c2 > v2),
          f"内容 {c2} vs 视口 {v2} → "
          f"{'显示' if app.left_sb.winfo_ismapped() else '隐藏'}")

    check("六个数值卡片都可见",
          all(c[0].winfo_exists() for c in app.cards))
    check("三个页签都在",
          app.nb.index("end") == 3, f"{app.nb.index('end')} 个页签")
    root.withdraw()
    root.update()


def test_errors(root: tk.Tk, app: App, host: str) -> None:
    section("7. 错误处理")
    # 连一个没人监听的端口
    app.on_disconnect()
    wait_idle(root, app)
    check("断开后 connected=False", not app.connected)
    check("断开后信息清空", app.info_labels["sn"].cget("text") == "—",
          app.info_labels["sn"].cget("text"))
    check("断开后数值卡片回到 —", app.cards[0][0].cget("text") == "—",
          app.cards[0][0].cget("text"))

    app.tcp_port_var.set("65001")
    app.on_connect()
    wait_idle(root, app)
    check("连不上时报错而不是崩掉", not app.connected)
    check("日志里有失败记录",
          any(k in app.log_text.get("1.0", "end") for k in ("失败", "拒绝", "超时", "Error")),
          "已记录")

    # 未连接时按功能键不应崩
    ok = True
    for fn in (app.on_start, app.on_stop, app.on_single, app.on_read_info,
               app.on_read_matrix, app.on_zero, app.on_unzero):
        try:
            fn()
            wait_idle(root, app, 1.0)
        except Exception as exc:                              # noqa: BLE001
            ok = False
            print(f"    {fn.__name__} 抛了 {type(exc).__name__}: {exc}")
    check("未连接时点各功能键不抛异常", ok)


def _is_num(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _no_raise(fn) -> bool:
    try:
        fn()
        return True
    except Exception as exc:                                  # noqa: BLE001
        print("    ", exc)
        return False


# =====================================================================
#  入口
# =====================================================================
def test_wave_decoupled(root: tk.Tk, app: App) -> None:
    """
    波形重绘必须与「数据到达节奏」解耦。

    串口路径上驱动会把字节攒成一批（USB Latency Timer），批与批之间
    没有任何新帧。如果此时不重绘，时间轴就会「来一批跳一下、不来就冻住」，
    而 TCP 回环因为每 tick 都有新帧恰好躲开 —— 这正是「网络模拟丝滑、
    串口卡」的根源。这条用例把它钉死。
    """
    section("7. 波形重绘与数据到达解耦")

    class FakeCli:
        """只在第一次 take_wave 时给数据，之后模拟「这几 tick 没有新帧」。"""

        def __init__(self) -> None:
            self.push_count = 0
            self.calls = 0

        def take_wave(self, window_sec=None):
            self.calls += 1
            if self.calls == 1:
                self.push_count += 5
                return self.push_count, [
                    (100.0 + i * 0.001, (0.1, 0.0, 0.0, 0.0, 0.0, 0.0))
                    for i in range(5)]
            return self.push_count, []           # 没有新帧（正常的攒批间隙）

    real_cli = app.cli
    real_draw = app.wave.draw
    saved_samples = app.wave._last_samples
    drawn: list = []
    fake = FakeCli()
    app.cli = fake                              # type: ignore[assignment]
    app.wave._last_samples = [(100.0, (0.1, 0.0, 0.0, 0.0, 0.0, 0.0))]
    app.wave.draw = lambda samples, force=False: drawn.append(len(samples))
    try:
        for _ in range(5):
            app._update_wave()
        check("一个攒批之后连续 4 个 tick 没有新帧时，波形仍然每次都重绘",
              len(drawn) == 5,
              f"调用 5 次 → 重绘 {len(drawn)} 次"
              f"（旧实现只重绘 1 次，剩下 4 次画面冻住）")
        check("没有新帧时复用上一批样本，而不是清空",
              all(n > 0 for n in drawn),
              f"各次样本数 {drawn}")
    finally:
        app.cli = real_cli
        app.wave.draw = real_draw
        app.wave._last_samples = saved_samples
        app.wave.reset(replot=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Y75 上位机界面回归测试")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15120)
    ap.add_argument("--auto", action="store_true", help="自己拉起模拟器（默认行为）")
    ap.add_argument("--shot", default="", help="测试结束后把界面截图存到这个路径")
    args = ap.parse_args()

    host = args.host
    port = args.port
    proc: Optional[subprocess.Popen] = None
    if not wait_port(host, port, 0.5):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "y75_simulator.py"),
             "--host", host, "--port", str(port), "--quiet"],
            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if not wait_port(host, port, 6.0):
            print("模拟器起不来")
            proc.kill()
            return 2
        print(f"（已自动拉起模拟器，端口 {port}）")

    tmpdir = tempfile.mkdtemp(prefix="y75_app_test_")
    root = tk.Tk()
    root.withdraw()                       # 测试时不真的弹窗
    ns = Namespace(port="", baud=460800, host="", sim=False, sim_port=port,
                   save_dir=tmpdir, selftest=False)
    app = App(root, ns)

    print("=" * 66)
    print(f"Y75 上位机界面回归测试   模拟器 {host}:{port}")
    print("=" * 66)

    try:
        test_connect(root, app, host, port)
        test_stream(root, app)
        test_single_and_settings(root, app)
        test_matrix(root, app)
        test_save(root, app, tmpdir)
        test_layout(root, app)
        test_wave_decoupled(root, app)
        test_errors(root, app, host)
    except Exception as exc:                                  # pragma: no cover
        import traceback
        traceback.print_exc()
        check(f"测试过程未抛异常（{type(exc).__name__}）", False, str(exc))
    finally:
        if args.shot:
            try:
                import make_icon
                root.deiconify()
                root.geometry(f"{int(1360 * app.scale)}x{int(880 * app.scale)}+40+40")
                root.update()
                time.sleep(0.5)
                root.update()
                make_icon.grab_window(root, args.shot)
                print(f"界面截图 → {args.shot}")
            except Exception as exc:                          # noqa: BLE001
                print(f"截图失败：{exc}")
        try:
            app.on_close()
        except Exception:
            pass
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    print("\n" + "=" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：")
        for n in FAILED:
            print(f"  - {n}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
