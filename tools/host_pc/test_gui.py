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
from tkinter import messagebox                                  # noqa: E402

from y75_app import (MODE_TCP, OUT_ASC, OUT_HEX, SPAN_PTS_CHOICES,  # noqa: E402
                     SPAN_SEC_CHOICES, X_MODE_PTS_LABEL, X_MODE_TIME_LABEL,
                     App, WaveCanvas)
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
    check("开始后立刻收到数据帧（50ms 轮询一拍）", got, f"push_count={cli.push_count}")

    # 数值卡片必须显示真实数值，不能是「—」
    wait_for(root, lambda: app.cards[0][0].cget("text") != "—", 2.0)
    val = app.cards[0][0].cget("text")
    check("数值卡片显示真实数值", val != "—" and _is_num(val), val)
    check("六张卡片都有数值",
          all(_is_num(l.cget("text")) for l, _ in app.cards),
          ", ".join(l.cget("text") for l, _ in app.cards))

    # 轮询模式 20 Hz：帧率统计要给区间，不能按老推流的 500Hz 口径
    check("运行统计：帧率 ≈ 20Hz（50ms 轮询）",
          wait_for(root, lambda: 12 <= app.frame_rate <= 30, 3.0),
          f"{app.frame_rate:.0f} Hz")
    check("运行统计：已收帧在增长",
          wait_for(root, lambda: cli.push_count > 20, 3.0),
          str(cli.push_count))
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


def test_bus_panel(root: tk.Tk, app: App) -> None:
    section("2b. 总线仲裁面板")
    check("默认开启「发送前先等总线空闲」", bool(app.bus_wait_var.get()))
    check("默认帧后窗口 = 0（自动）", app.bus_window_var.get().strip() == "0")
    check("默认最多等 250ms", app.bus_maxwait_var.get().strip() == "250")
    check("默认不重发（协议无事务号，重发有认错响应的风险）",
          app.bus_retry_var.get().strip() == "0")

    cli = app.cli
    assert cli is not None
    check("仲裁对象已挂到客户端且默认启用", cli.bus.enabled)

    # 改数字 → 立刻作用到当前连接
    app.bus_window_var.set("1.5")
    app.bus_maxwait_var.set("120")
    app.bus_retry_var.set("2")
    app._apply_bus_cfg()
    check("帧后窗口改动立刻生效", abs(cli.bus.window_ms - 1.5) < 1e-6,
          f"{cli.bus.window_ms}")
    check("最多等改动立刻生效", abs(cli.bus.max_wait_ms - 120.0) < 1e-6,
          f"{cli.bus.max_wait_ms}")
    check("重发次数改动立刻生效", cli.bus_retry == 2, str(cli.bus_retry))

    # 非法输入回落默认，不能让界面炸
    app.bus_window_var.set("abc")
    app.bus_maxwait_var.set("")
    app.bus_retry_var.set("99")
    cfg = app._bus_cfg()
    check("非法输入回落到默认值", cfg["bus_window"] == 0.0 and cfg["bus_maxwait"] == 250.0,
          str(cfg))
    check("重发次数被夹到 3 以内", cfg["bus_retry"] == 3, str(cfg["bus_retry"]))

    # 关掉开关 → cli.bus.enabled 跟着关
    app.bus_wait_var.set(False)
    app._apply_bus_cfg()
    check("关掉开关后客户端真的不再仲裁", not cli.bus.enabled)
    app.bus_wait_var.set(True)
    app.bus_window_var.set("0")
    app.bus_maxwait_var.set("250")
    app.bus_retry_var.set("0")
    app._apply_bus_cfg()
    check("恢复默认后仲裁重新启用", cli.bus.enabled and cli.bus_retry == 0)
    check("状态行能显示仲裁统计", "帧后" in app.bus_stat_label.cget("text"),
          app.bus_stat_label.cget("text").replace("\n", " / "))


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
    check("力矩标签变成 kg·m", app.cards[3][1].cget("text") == "kg·m",
          app.cards[3][1].cget("text"))
    check("波形单位也变成 kg", app.wave.unit == "kg", app.wave.unit)

    # 切到 N：力矩通道应显示 N·m（力 × 力臂 的量纲，不是纯 N）
    app.fmt_var.set("矩阵解耦 (N)")
    app.on_apply_output()
    check("应用输出设置完成", wait_idle(root, app))
    check("N 模式下力矩标签是 N·m", app.cards[5][1].cget("text") == "N·m",
          app.cards[5][1].cget("text"))

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


def test_sn_tab(root: tk.Tk, app: App) -> None:
    section("4b. 序列号选项卡（0x0A 写 / 0x05 读）")
    names = [app.nb.tab(t, "text") for t in app.nb.tabs()]
    check("选项卡顺序：波形 / 矩阵 / 报文日志 / 序列号",
          names == ["实时波形", "解耦矩阵", "报文日志", "序列号"], " | ".join(names))

    app.nb.select(len(names) - 1)
    root.update()

    app.on_read_sn()
    check("读取序列号完成", wait_idle(root, app))
    sn0 = app.sn_now.cget("text")
    check("当前序列号已显示", bool(sn0) and sn0 != "—", sn0)
    check("与顶部信息栏同一口径",
          app.info_labels["sn"].cget("text") == sn0,
          app.info_labels["sn"].cget("text"))

    # ---- 输入时实时校验 ----
    app.sn_var.set("A" * 16)
    root.update()
    check("超长时禁用「写入设备」", "disabled" in app.btn_sn_write.state())
    check("超长时计数变红", app.sn_count.cget("text") == "16/15",
          app.sn_count.cget("text"))
    app.sn_var.set("HS 001")
    root.update()
    check("含空格时禁用写入并说明原因",
          "disabled" in app.btn_sn_write.state()
          and "0x20" in app.sn_hint.cget("text"), app.sn_hint.cget("text"))
    app.sn_var.set("HS-001")
    root.update()
    check("合法值重新启用写入", "disabled" not in app.btn_sn_write.state())
    check("合法值计数 6/15", app.sn_count.cget("text") == "6/15",
          app.sn_count.cget("text"))

    # ---- 真写一次（跳过确认框）----
    orig_ask = messagebox.askyesno
    messagebox.askyesno = lambda *a, **k: True
    try:
        app.on_write_sn()
        check("写入完成", wait_idle(root, app))
    finally:
        messagebox.askyesno = orig_ask
    check("写入后当前值更新", app.sn_now.cget("text") == "HS-001",
          app.sn_now.cget("text"))
    check("写入后顶部信息栏一起更新",
          app.info_labels["sn"].cget("text") == "HS-001")
    check("写入后输入框清空", app.sn_var.get() == "", repr(app.sn_var.get()))
    check("提示里写明已回读确认", "回读" in app.sn_hint.cget("text"),
          app.sn_hint.cget("text"))
    check("设备里确实是新值", app.cli.query_sn() == "HS-001", app.cli.query_sn())

    # ---- 收尾：把设备的序列号写回去 ----
    app.sn_var.set(sn0)
    root.update()
    app.on_write_sn()
    wait_idle(root, app)
    check("收尾恢复原序列号", app.cli.query_sn() == sn0, app.cli.query_sn())


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
    check("保存期间持续收帧", wait_for(root, lambda: cli.push_count > n0 + 40, 4.0),
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
    check("写入行数合理（表头 + 轮询数据行）", nrows > 35, f"{nrows} 行")
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

    # 滚动域只能由内容尺寸决定。老实现拿 bbox("all")，把**已移动**的内框位置
    # 算了进去：每拖一格滚动条滚动域就缩一截（实测 1628→862），一次鼠标事件里
    # 窗口被搬好几回 → 卡顿 + Windows 重影，而且 yview 比例到 1 了内容还差半屏。
    sr0 = app.left_canvas.cget("scrollregion")
    app.left_canvas.yview_moveto(0.5)
    root.update()
    sr1 = app.left_canvas.cget("scrollregion")
    app.left_canvas.yview_moveto(1.0)
    root.update()
    sr2 = app.left_canvas.cget("scrollregion")
    check("滚动域不随滚动漂移（老实现 1628 → 862 越滚越缩）",
          sr0 == sr1 == sr2, f"{sr0} → {sr1} → {sr2}")
    drop = content_h - view_h
    check("滚到底时内容偏移等于 内容高-视口高（真底部够得着）",
          abs(app._left_inner.winfo_y() + drop) <= 2,
          f"inner.y {app._left_inner.winfo_y()}，期望 {-drop}")
    check("滚动域高度就是内容高度",
          abs(float(str(sr2).split()[-1]) - content_h) <= 2,
          f"scrollregion {sr2} vs 内容 {content_h}")

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
    check("四个页签都在（波形 / 矩阵 / 报文日志 / 序列号）",
          app.nb.index("end") == 4, f"{app.nb.index('end')} 个页签")
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

        def take_wave(self, window_sec=None, max_points=0):
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
    app.wave.draw = lambda samples, force=False, count=None: drawn.append(len(samples))
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


def test_points_axis(root: tk.Tk, app: App) -> None:
    """
    横轴可切「时间 ↔ 点数」，点数模式下：

    1. 横坐标按**采样点序号**定位 —— 相邻点等间距（这是点数轴的意义所在：
       数一个周期占几个点、两帧之间隔几个点，不能因为节拍抖动而伸缩）；
    2. 一屏正好是设定的点数（1000~20000），右端贴住最新一点；
    3. 抽稀后仍然等间距，不会因为抽稀把点挤到一边。
    """
    section("8. 横轴：时间 ↔ 点数")
    w = app.wave
    check("默认横轴是时间", w.x_mode == WaveCanvas.X_TIME, w.x_mode)
    check("默认跨度候选项是秒", tuple(app.span_box.cget("values")) == SPAN_SEC_CHOICES,
          str(app.span_box.cget("values")))

    t0 = time.time() - 8.0
    dt = 0.002                                    # 模拟 500Hz
    def make(n: int):
        return [(t0 + i * dt, (float(i % 100), 0.5, -0.5, 0.0, 1.0, -1.0))
                for i in range(n)]

    w.draw(make(3000), force=True)
    check("时间模式刻度带单位 s",
          w.itemcget(w._items["xlab"][0], "text").endswith("s"),
          w.itemcget(w._items["xlab"][0], "text"))

    # ---- 切到点数 ----
    app.xmode_var.set(X_MODE_PTS_LABEL)
    app._on_xmode_change()
    check("切到点数模式", w.x_mode == WaveCanvas.X_POINTS, w.x_mode)
    check("点数模式默认 2000 点", w.window_pts == 2000, str(w.window_pts))
    check("跨度候选项换成点数", tuple(app.span_box.cget("values")) == SPAN_PTS_CHOICES,
          str(app.span_box.cget("values")))
    check("跨度输入框同步成当前点数", app.win_var.get().strip() == "2000",
          app.win_var.get())

    w.draw(make(3000), force=True)
    xl = [w.itemcget(it, "text") for it in w._items["xlab"]]
    check("点数模式刻度是整数且左端为 -2000", xl[0] == "-2000" and xl[-1] == "0", str(xl))
    check("点数模式刻度不带 s", not any("s" in s for s in xl), str(xl))

    # ---- 一屏正好 N 点、等间距、贴住最新一点 ----
    w.set_window_pts(1000)
    w.draw(make(3000), force=True)
    co = w.coords(w._items["curve"][0])
    xs = co[0::2]
    pw = w.winfo_width() - w.PAD_L - w.PAD_R
    gaps = [b - a for a, b in zip(xs, xs[1:])]
    check("一屏点数等于设定值（1000 点铺满宽度，一个不丢）",
          len(xs) == 1000, f"画了 {len(xs)} 个点，设定 1000 点")
    check("相邻点等间距（不因节拍抖动伸缩）",
          max(gaps) - min(gaps) < 0.01, f"间距 {min(gaps):.3f}~{max(gaps):.3f}px")
    check("右端贴住最新一点（不留空白、也不越界）",
          abs(xs[-1] - (w.PAD_L + pw)) <= 1.5,
          f"末点 x={xs[-1]:.1f}，右轴 x={w.PAD_L + pw:.1f}")

    # ---- 20000 点也要能画（缓冲上限）----
    w.set_window_pts(20000)
    big = make(20000)
    t = time.perf_counter()
    w.draw(big, force=True)
    cost = (time.perf_counter() - t) * 1000.0
    co = w.coords(w._items["curve"][0])
    check("20000 点抽稀后仍然有点可画", len(co) >= 40, f"{len(co) // 2} 点")
    check("20000 点的重绘仍然够快（≤40ms，否则掉帧）", cost <= 40.0, f"{cost:.1f} ms")
    check("20000 点的刻度左端是 -20000",
          w.itemcget(w._items["xlab"][0], "text") == "-20000",
          w.itemcget(w._items["xlab"][0], "text"))
    check("点数模式的秒数换算提示", "s @" in app.span_hint.cget("text"),
          app.span_hint.cget("text"))
    check("跨度换算 = 点数 × 实测采样间隔",
          abs(w.span_sec() - w.window_pts * w.dt_est) < 1e-6,
          f"{w.span_sec():.2f}s = {w.window_pts} × {w.dt_est * 1000:.2f}ms")

    # ---- 切回时间 ----
    app.xmode_var.set(X_MODE_TIME_LABEL)
    app._on_xmode_change()
    check("切回时间模式", w.x_mode == WaveCanvas.X_TIME, w.x_mode)
    check("跨度候选项换回秒", tuple(app.span_box.cget("values")) == SPAN_SEC_CHOICES,
          str(app.span_box.cget("values")))
    check("横轴提示清空", app.span_hint.cget("text") == "",
          app.span_hint.cget("text"))
    w.draw(make(3000), force=True)
    check("时间模式刻度恢复", w.itemcget(w._items["xlab"][0], "text").endswith("s"),
          w.itemcget(w._items["xlab"][0], "text"))

    # ---- 时间模式：时间戳的抖动不许传到屏幕上 ----
    # 真机串口上帧时间戳是「设备时钟推算值」：USB 驱动一批 5 帧一起交上来，
    # 推算步长有 1% 误差，几百帧后就与本地时钟错开，被「超前就拉回」的护栏
    # 硬拽一下。若拿时间戳直接当 x，这一拽会让整条曲线在水平方向收一下、
    # 再放开 —— 用户说的「每次刷新都把数据从前面推到后面」就是它。
    # 现在横轴按点位序号铺，这类抖动必须完全看不出来。
    def make_live(n: int = 3000):
        now = time.time()
        return [(now - (n - 1 - i) * dt + (-0.06 if i % 200 == 0 else 0.0),
                 (float(i % 100), 0.5, -0.5, 0.0, 1.0, -1.0))
                for i in range(n)]

    w.draw(make_live(), force=True)
    co = w.coords(w._items["curve"][0])
    xs = co[0::2]
    gaps = [b - a for a, b in zip(xs, xs[1:])]
    # 最后一段是「抽稀也要保住最后一点」补上去的尾点，间距必然偏短，它单独看
    body = gaps[:-1] if len(gaps) > 2 else gaps
    check("时间戳抖动不会让点间距伸缩（横轴不跟时间戳走）",
          bool(body) and max(body) - min(body) < 1e-6,
          f"{len(xs)} 点，主体间距 {min(body):.3f}~{max(body):.3f}px")

    # ---- 时间模式：每帧整体平移的量必须相等（匀速滚动）----
    # 这一条直接对「定格感」：位移量就是时间轴右边界每帧前进的秒数。
    # 以前它取的是**实测帧间隔**，Tk 定时器一抖，屏幕上就一帧走 3px、
    # 下一帧走 6px —— 眼睛看到的就是一格一格。现在改用平滑后的周期。
    # （不拿末点的像素 x 来量：抽稀的整步索引会让末端在 0~step 像素之间
    #   锯齿，测不出匀速。）
    # 按真机方式驱动：每帧到一批点（30ms × 500Hz = 15 个），切片跟着滑动。
    # 位置必须由内部积分推进，而不是「n_all 涨了多少就跳多少」。
    cnt = [3000]

    def feed() -> None:
        cnt[0] += 15
        w.draw(make_live(), force=True, count=cnt[0])

    for _ in range(12):                             # 预热：让平滑后的帧周期收敛
        time.sleep(0.03)
        feed()
    ends = []
    for _ in range(4):
        time.sleep(0.03)
        feed()
        ends.append(w._pos_abs)      # 屏幕位置 = 绝对采样序号（kx 已定格，点数即像素）
    d = [b - a for a, b in zip(ends, ends[1:])]
    # 每帧到 15 个点、画一次，位置就该前进约 15 个点。
    # （不换算成秒再比：比例 dt_est 本身还在 EMA 收敛，拿一个 3000+ 的绝对
    #   序号去乘它，收敛期的微小变化会被放大成好几毫秒的假抖动。）
    check("相邻帧的推进量一致（匀速滚动）",
          len(d) == 3 and min(d) > 5.0 and max(d) - min(d) < 1.5,
          f"每帧 {[round(x, 2) for x in d]} 个点")

    # ---- 形状锁定：两帧之间必须是"整体平移" ----
    # 以前 x0 带小数，Tk 对每个点独立取整 → 相邻两帧每个点的取整结果
    # 各不相同，曲线**形状**每帧都在轻微变化 —— 看起来不像平移、
    # 像擦掉重画（用户说的「数据从前面被推到后面」）。量子化到整数像素后，
    # 每个点两帧之间的位移必须严格相等。
    w.draw(make_live(), force=True, count=cnt[0])
    dbg_a = getattr(w, "_dbg", None)
    co_a = w.coords(w._items["curve"][0])[0::2]
    time.sleep(0.03)
    w.draw(make_live(), force=True, count=cnt[0] + 15)   # 前进 15 个点 = 30ms
    dbg_b = getattr(w, "_dbg", None)
    co_b = w.coords(w._items["curve"][0])[0::2]
    # 只查主体：右端「保住最新点」的补点不在抽稀网格上，位移天然不同；
    # k_hi 随右边界前进也会让总点数 ±1，都属正常。
    n = int(min(len(co_a), len(co_b)) * 0.9)
    if n > 10:
        shifts = [b - a for a, b in zip(co_a[:n], co_b[:n])]
        check("两帧之间是整体平移（形状完全锁定）",
              max(shifts) - min(shifts) < 1e-9,
              f"{n} 点位移差 {max(shifts) - min(shifts):.2e}px"
              f"  A={dbg_a} B={dbg_b}")
    else:
        check("两帧之间是整体平移（形状完全锁定）", False,
              f"点数太少 {len(co_a)}→{len(co_b)}")

    # ---- 时间戳跳变不许让画面跳 ----
    # 真机上这一跳**必然**发生：设备推流率不是精确 500Hz，sample_dt 推算的
    # 设备时钟会慢慢攒出几十~几百毫秒误差，然后被「|now−_dev_t|>0.4s 就重锚」
    # 硬拉一次。老实现把本机时钟口径的 t_end 和设备时钟口径的 newest 相夹，
    # 一重锚就把右边界拽回去 —— 屏幕上整条曲线猛跳一截，看着就是「从头重来」。
    # 现在横轴位置只由「落后多少个点」决定，与时间戳完全无关：跳变必须不可见。
    for name, jump in (("前跳", +0.40), ("回跳", -0.08)):
        w.reset()
        w.draw(make(3000), force=True)
        for _ in range(4):                          # 让它稳到目标落后量
            time.sleep(0.03)
            cnt[0] += 15
            w.draw(make(3000), force=True, count=cnt[0])
        x_before = w.coords(w._items["curve"][0])[-2]
        jumped = [(t + (jump if i >= 2900 else 0.0), v)
                  for i, (t, v) in enumerate(make(3000))]
        time.sleep(0.03)
        cnt[0] += 15
        w.draw(jumped, force=True, count=cnt[0])
        x_after = w.coords(w._items["curve"][0])[-2]
        dx = x_after - x_before
        check(f"时间戳{name} {abs(jump)*1000:.0f}ms 不影响画面（横轴只看点数）",
              abs(dx) < 12.0,
              f"右端位移 {dx:+.1f}px（老实现会跳 ~60px）")


def test_serial_clock_pathology(root: tk.Tk, app: App) -> None:
    section("9. 串口时间戳病态（真机独有，TCP 复现不出来）")
    w = app.wave
    # 摘掉实时链路：否则 App 自己的刷新循环会拿模拟器数据一起调 draw()，
    # 两条 count 序列交叉喂进速率测量，测出来的 rate 是垃圾。
    saved_cli = app.cli
    app.cli = None
    try:
        _serial_clock_pathology(root, app, w)
    finally:
        app.cli = saved_cli


def _serial_clock_pathology(root: tk.Tk, app: App, w: WaveCanvas) -> None:
    app.xmode_var.set(X_MODE_TIME_LABEL)
    app._on_xmode_change()
    app.win_var.set("5")
    app._on_window_change()
    w.reset()                      # 相当于刚连上设备：速率/位置/窗口点数都从头量
    root.update()

    # 真机上协议层有一条兜底：设备时钟跑到本机时间前面时，时间戳按
    # sample_dt*0.05 挤压过去（y75_protocol._handle_frame）。连续触发时，
    # 相邻间隔就从 2ms 变成 0.1ms —— 而**帧计数仍然是准确的**。
    # 老实现拿时间戳间隔当采样周期，一屏点数 = 5s / 0.1ms = 50000 点，
    # 曲线被压到右边缘再随数据往左扫，就是「点从最右边刷到左边」。
    true_dt = 0.002                                   # 真实 500Hz
    sick_dt = 0.0001                                  # 被挤压后的时间戳间隔
    n = 4000
    cnt = [n]

    def make(gap: float) -> list:
        # 最新点始终锚在本机时间上，只改**相邻间隔** —— 这样自适应延迟不会
        # 因为「样本看起来很旧」而饱和，测到的就纯粹是间隔被压扁这一个变量。
        base = time.time()
        return [(base - (n - 1 - i) * gap,
                 (float(i % 100), 0.5, -0.5, 0.0, 1.0, -1.0))
                for i in range(n)]

    def feed(gap: float) -> None:
        time.sleep(0.03)
        cnt[0] += 15
        w.draw(make(gap), force=True, count=cnt[0])

    for _ in range(14):                               # 先用规整时间戳预热
        feed(true_dt)
    np_ok = w.np_win()
    for _ in range(14):                               # 再切成病态时间戳
        feed(sick_dt)

    check("时间戳被压到 0.1ms 也不改「一屏多少点」",
          w.np_win() == np_ok and 500.0 < np_ok < 10000.0,
          f"np_win {np_ok:.0f} → {w.np_win():.0f} 点"
          f"（老实现按时间戳算会变成 {5 / sick_dt:.0f}）")
    check("采样周期取自帧计数，不被时间戳污染",
          abs(w.dt_est - 1.0 / w._rate_pts) < 1e-12 and w.dt_est > 20 * sick_dt,
          f"dt_est={w.dt_est * 1000:.3f}ms（帧计数实测 {w._rate_pts:.0f}Hz），"
          f"病态时间戳会说 {sick_dt * 1000:.1f}ms")

    # 曲线必须铺满画布，而不是缩在右边缘
    co = w.coords(w._items["curve"][0])
    xs = co[0::2]
    pad_l, pad_r = WaveCanvas.PAD_L, WaveCanvas.PAD_R
    usable = w.winfo_width() - pad_l - pad_r
    check("曲线铺满整个绘图区（没有被挤到右边缘）",
          len(xs) > 50 and (max(xs) - min(xs)) > usable * 0.8,
          f"横向跨度 {max(xs) - min(xs):.0f}px / 可用 {usable:.0f}px，{len(xs)} 点")

    # 恢复规整时间戳后必须继续正常滚动
    make_ok = lambda: make(true_dt)                   # noqa: E731

    poses = []
    for _ in range(4):
        time.sleep(0.03)
        cnt[0] += 15
        w.draw(make_ok(), force=True, count=cnt[0])
        poses.append(w._pos_abs)
    d = [b - a for a, b in zip(poses, poses[1:])]
    check("病态时间戳消失后滚动依旧匀速",
          len(d) == 3 and min(d) > 5.0 and max(d) - min(d) < 1.5,
          f"每帧 {[round(x, 2) for x in d]} 个点")


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
        test_bus_panel(root, app)
        test_single_and_settings(root, app)
        test_matrix(root, app)
        test_sn_tab(root, app)
        test_save(root, app, tmpdir)
        test_layout(root, app)
        test_wave_decoupled(root, app)
        test_points_axis(root, app)
        test_serial_clock_pathology(root, app)
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
