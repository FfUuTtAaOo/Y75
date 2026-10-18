# -*- coding: utf-8 -*-
"""
y75_xlsx.py —— 极简 .xlsx 写出器（纯标准库，零第三方依赖）

为什么不直接用 openpyxl：
    整套上位机要保持「纯标准库 + 单文件打包」。xlsx 本质就是一个 zip 包，
    最小可用只需 5 个部件：

        [Content_Types].xml
        _rels/.rels
        xl/workbook.xml
        xl/_rels/workbook.xml.rels
        xl/worksheets/sheet1.xml

写表策略（流式，内存恒定）：
    数据边采边以 <row> 片段追加到临时文件，采集时长不受内存限制；
    调用 close() 时才补上 XML 尾巴，把临时文件压进 zip 成为 .xlsx。

数值约定（与「不编造数值」一致）：
    * None / NaN / ±Inf 一律**留空单元格**，不写 0；
    * 一行六个值全无效 → 整行不写，计入 skipped_rows；
    * 部分无效 → 该行照样写，无效的那格留空。

用法：
    from y75_xlsx import XlsxTableWriter
    w = XlsxTableWriter("out.xlsx", headers=("FX", "FY", "FZ", "TX", "TY", "TZ"))
    w.add_row([1.0, 2.0, float("nan"), 4.0, 5.0, 6.0])
    rows = w.close()          # 返回实际写入的数据行数
    w.abort()                 # 放弃：删掉临时文件、不产生 xlsx

自检：
    python y75_xlsx.py --selftest
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import zipfile
from typing import Iterable, Optional, Sequence

#: Excel 单个工作表的数据行上限（含表头共 1,048,576 行）
MAX_DATA_ROWS = 1048575

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '</Relationships>'
)

#: 冻结首行（表头），方便滚动查看
_SHEET_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
    '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
    '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
    '</sheetView></sheetViews>'
    '<sheetData>'
)
_SHEET_TAIL = '</sheetData></worksheet>'


class XlsxLimitError(RuntimeError):
    """超过 Excel 单表行数上限。"""


def _esc(text: str) -> str:
    """XML 文本转义（工作表名、表头）。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _safe_sheet_name(name: str) -> str:
    """Excel 工作表名不能含 : \\ / ? * [ ] 且不超过 31 字符。"""
    for ch in ':\\/?*[]':
        name = name.replace(ch, "_")
    return (name or "Sheet1")[:31]


def _col_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _num_text(value: float) -> Optional[str]:
    """
    浮点数 → xlsx 单元格里的数字文本。

    返回 None 表示「这个值不合法，单元格留空」。
    用 %.10g 兼顾大小量级：既不会把 1e-7 压成 0.000000，
    也不会写出 0.30000000000000004 这种由 float32 转换来的长尾。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f"{f:.10g}"


class XlsxTableWriter:
    """
    流式写出一个单表 .xlsx。

    :param path:       目标 .xlsx 路径（父目录需已存在）
    :param headers:    表头文本（决定列数）
    :param sheet_name: 工作表名
    """

    def __init__(self, path: str, headers: Sequence[str],
                 sheet_name: str = "Sheet1"):
        self.path = path
        self.headers = tuple(str(h) for h in headers)
        self.sheet_name = _safe_sheet_name(sheet_name)
        self._ncol = len(self.headers)
        if self._ncol < 1:
            raise ValueError("至少需要一列表头")

        self.rows = 0            # 已写入的数据行数（不含表头）
        self.skipped_rows = 0    # 六个值全无效、整行被丢掉的行数
        self.blank_cells = 0     # 留空的单元格总数（含部分无效的行）

        fd, self._tmp_path = tempfile.mkstemp(prefix="y75_xlsx_", suffix=".xml")
        self._fh = os.fdopen(fd, "w", encoding="utf-8", newline="")
        self._write_header()

    # ------------------------------------------------------------------
    def _write_header(self) -> None:
        self._fh.write(_SHEET_HEAD)
        cells = "".join(
            f'<c r="{_col_letter(i)}1" t="inlineStr">'
            f'<is><t xml:space="preserve">{_esc(h)}</t></is></c>'
            for i, h in enumerate(self.headers))
        self._fh.write(f'<row r="1">{cells}</row>')

    # ------------------------------------------------------------------
    def add_row(self, values: Iterable[object]) -> bool:
        """
        追加一行。返回 True 表示这一行真的写进去了。
        超过 Excel 行数上限时抛 XlsxLimitError。
        """
        if self._fh is None:
            raise RuntimeError("写出器已关闭")
        if self.rows >= MAX_DATA_ROWS:
            raise XlsxLimitError(
                f"已达 Excel 单表上限 {MAX_DATA_ROWS} 行，请停止后另存一个文件")

        vals = list(values)
        row_no = self.rows + 2          # 第 1 行是表头
        cells = []
        for i in range(self._ncol):
            raw = vals[i] if i < len(vals) else None
            txt = _num_text(raw) if raw is not None else None
            if txt is None:
                continue
            cells.append(f'<c r="{_col_letter(i)}{row_no}"><v>{txt}</v></c>')

        if not cells:
            self.skipped_rows += 1
            return False

        self.blank_cells += self._ncol - len(cells)
        self._fh.write(f'<row r="{row_no}">' + "".join(cells) + '</row>')
        self.rows += 1
        return True

    # ------------------------------------------------------------------
    def close(self) -> int:
        """收尾并生成 .xlsx，返回写入的数据行数。可重复调用。"""
        if self._fh is None:
            return self.rows
        try:
            self._fh.write(_SHEET_TAIL)
            self._fh.close()
        finally:
            self._fh = None
        try:
            self._package()
        finally:
            self._cleanup()
        return self.rows

    def abort(self) -> None:
        """放弃写出：删掉临时文件，不产生 .xlsx。"""
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        self._cleanup()

    def _package(self) -> None:
        workbook = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
            '2006/main" xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships">'
            f'<sheets><sheet name="{_esc(self.sheet_name)}" sheetId="1" '
            'r:id="rId1"/></sheets>'
            '</workbook>'
        )
        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
            zf.writestr("_rels/.rels", _ROOT_RELS)
            zf.writestr("xl/workbook.xml", workbook)
            zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
            zf.write(self._tmp_path, "xl/worksheets/sheet1.xml")

    def _cleanup(self) -> None:
        try:
            os.remove(self._tmp_path)
        except OSError:
            pass


# =====================================================================
#  自检：写一个表再读回来核对结构
# =====================================================================
def _selftest() -> int:                                  # pragma: no cover
    import re
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="y75_xlsx_test_")
    path = os.path.join(tmpdir, "t.xlsx")
    bad = 0

    def check(name, cond, detail=""):
        nonlocal bad
        print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
        if not cond:
            bad += 1

    w = XlsxTableWriter(path, headers=("FX", "FY", "FZ", "TX", "TY", "TZ"),
                        sheet_name="六维力数据")
    w.add_row([1.0, -2.5, 3.25, 4.5, 1e-7, 1e30])
    w.add_row([None, 2.0, float("nan"), float("inf"), 5.0, 6.0])   # 部分无效
    w.add_row([None] * 6)                                          # 全无效 → 丢
    rows = w.close()

    check("close 返回数据行数", rows == 2, f"rows={rows}")
    check("全无效行被跳过", w.skipped_rows == 1, f"skipped={w.skipped_rows}")
    check("留空单元格计数", w.blank_cells == 3, f"blank={w.blank_cells}")
    check("临时文件已清理", not os.path.exists(w._tmp_path))
    check("xlsx 已生成", os.path.isfile(path),
          f"{os.path.getsize(path) if os.path.exists(path) else 0} 字节")

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        need = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml"}
        check("zip 内含全部必需部件", need <= names, str(sorted(names)))
        check("zip 无损", zf.testzip() is None)
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        wb = zf.read("xl/workbook.xml").decode("utf-8")

    check("工作表名正确", "六维力数据" in wb)
    heads = re.findall(r"<t[^>]*>([^<]*)</t>", sheet)
    check("表头六列", heads == ["FX", "FY", "FZ", "TX", "TY", "TZ"], str(heads))
    check("行数正确（表头 1 + 数据 2）",
          sheet.count("<row ") == 3, f"{sheet.count('<row ')} 行")
    check("全无效行未落盘", 'r="4"' not in sheet)
    check("科学计数法保留", "<v>1e-07</v>" in sheet and "<v>1e+30</v>" in sheet,
          "1e-07 / 1e+30")
    check("无效值不写成 0（留空）",
          "<v>0</v>" not in sheet and "<v>nan</v>" not in sheet,
          "NaN/Inf/None 均为空单元格")
    check("部分无效行仍保留有效格",
          '<c r="B3"><v>2</v></c>' in sheet and '<c r="E3"><v>5</v></c>' in sheet,
          "第 3 行 FY/TY/TZ 保留")
    check("冻结首行已写入", 'state="frozen"' in sheet)

    # 用 openpyxl（如果有）交叉验证能被真正解析
    try:
        import openpyxl                                        # type: ignore
        book = openpyxl.load_workbook(path)
        ws = book.active
        check("openpyxl 能打开且行列正确",
              ws.max_column == 6 and len(list(ws.rows)) == 3,
              f"{ws.max_row} 行 × {ws.max_column} 列")
        check("openpyxl 读回的值正确",
              ws.cell(2, 1).value == 1 and ws.cell(2, 6).value == 1e30)
    except ImportError:
        print("[SKIP] 没装 openpyxl，跳过交叉校验")

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n===== xlsx 自检：{'全部通过' if bad == 0 else f'{bad} 项失败'} =====")
    return 0 if bad == 0 else 1


if __name__ == "__main__":                               # pragma: no cover
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
