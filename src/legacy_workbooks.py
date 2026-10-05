"""Bounded, fixed-value BIFF5/7 reader. Never activate OLE or evaluate formulas.

This deliberately does not promise general XLS support. Reject formulas before
xlrd can silently substitute their caches, and reject unknown records/objects.
The returned workbook is an in-memory parser interface, never a converted file.
"""
from io import BytesIO, StringIO
import math
import struct

import olefile
from openpyxl import Workbook
import xlrd

from .input_errors import InputError

MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
VERSION = 'fixed-biff57-v1'
MAX_FILE = 10 * 1024 * 1024
MAX_CELLS = 500_000
# Calculation/view/print/style metadata are inert: no OS print driver, toolbar,
# hyperlink, connection or object handler is ever invoked. Unknown IDs fail closed.
RECORDS = {
    0x000A, 0x000C, 0x000D, 0x000E, 0x000F, 0x0010, 0x0011,
    0x0012, 0x0013, 0x0014, 0x0015, 0x0016, 0x0017, 0x0018,
    0x0019, 0x001D, 0x0022, 0x0026, 0x0027, 0x0028, 0x0029,
    0x002A, 0x002B, 0x0031, 0x003C, 0x003D, 0x0040, 0x0042,
    0x004D, 0x0055, 0x005C, 0x005F, 0x007D, 0x0080, 0x0081,
    0x0082, 0x0083, 0x0084, 0x0085, 0x008C, 0x008D, 0x0092,
    0x009C, 0x00A1, 0x00AB, 0x00BD, 0x00BE, 0x00BF, 0x00C0,
    0x00C1, 0x00D6, 0x00D7, 0x00DA, 0x00E0, 0x00E1, 0x00E2,
    0x01B7, 0x0200, 0x0201, 0x0203, 0x0204, 0x0205, 0x0208,
    0x020B, 0x0225, 0x023E, 0x027E, 0x0293, 0x041E, 0x0809,
    0x0892,
}
CELL_RECORDS = {0x00BD, 0x00BE, 0x00D6, 0x0201, 0x0203, 0x0204, 0x0205, 0x027E}


def _records(raw):
    position = 0
    count = 0
    while position < len(raw):
        if len(raw) - position < 4:
            raise InputError('XLS 记录头截断。')
        code, size = struct.unpack_from('<HH', raw, position)
        if size > 8224 or position + 4 + size > len(raw):
            raise InputError('XLS 记录长度无效。')
        count += 1
        if count > MAX_CELLS:
            raise InputError('XLS 记录数量超限。')
        yield position, code, raw[position + 4:position + 4 + size]
        position += 4 + size


def _names(names, externals, sheets):
    """Accept only absolute, single-sheet internal area references, not expressions.

    BIFF5 NAME layout and tArea3d are specified in OpenOffice's Excel format
    reference sections 5.33 and 3.9.16; EXTERNSHEET section 5.41.
    """
    for body in names:
        if len(body) < 14:
            raise InputError('XLS 命名范围记录截断。')
        flags, _, length, formula_size = struct.unpack_from('<HBBH', body)
        start = 14 + length
        if (flags not in {0, 0x20} or body[2] or any(body[10:14]) or
                len(body) != start + formula_size or formula_size != 21):
            raise InputError('XLS 含未支持的命名公式、宏或命令。')
        formula = body[start:]
        if formula[0] != 0x3B:
            raise InputError('XLS 命名范围不是受控的内部绝对区域。')
        link = struct.unpack_from('<h', formula, 1)[0]
        first, last, row1, row2, col1, col2 = struct.unpack_from('<HHHHBB', formula, 11)
        if (not -len(externals) <= link < 0 or first != last or last >= len(sheets) or
                sheets[first] != externals[-link - 1] or row1 & 0xC000 or row2 & 0xC000 or row1 > row2 or row2 >= 30000 or
                col1 > col2 or col2 >= 100):
            raise InputError('XLS 命名范围含外部、跨表或超限引用。')


def _stream(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FILE or not raw.startswith(MAGIC):
        raise InputError('XLS 内容与扩展名不符、为空或超过 10MB。')
    try:
        with olefile.OleFileIO(BytesIO(raw), raise_defects=olefile.DEFECT_INCORRECT) as ole:
            paths = ole.listdir(streams=True, storages=True)
            allowed = {'Book', 'Workbook', '\x05SummaryInformation', '\x05DocumentSummaryInformation'}
            if (any(len(path) != 1 or path[0] not in allowed or
                    ole.get_type(path) != olefile.STGTY_STREAM for path in paths) or
                    sum(path[0] in {'Book', 'Workbook'} for path in paths) != 1):
                raise InputError('XLS 含宏、嵌入对象或未支持的容器部件。')
            # Read all accepted streams so broken/shared allocation chains are checked,
            # but never parse property sets or dispatch their embedded metadata.
            streams = {}
            for path in paths:
                size = ole.get_size(path)
                if size > MAX_FILE or (path[0].startswith('\x05') and size > 65536):
                    raise InputError('XLS 容器流大小超限。')
                streams[path[0]] = ole.openstream(path).read()
                if len(streams[path[0]]) != size:
                    raise InputError('XLS 容器流截断。')
            if ole.parsing_issues:
                raise InputError('XLS 容器存在结构歧义。')
            return streams.get('Book', streams.get('Workbook'))
    except InputError:
        raise
    except (OSError, ValueError, struct.error, IndexError):
        raise InputError('XLS 容器损坏或结构无效。') from None


def read(raw):
    """Preflight the entire BIFF stream before decoding any cached cell results."""
    stream = _stream(raw)
    names, externals, bounds, sheet_starts = [], [], [], []
    external_count = None
    active, globals_done, previous = None, False, None
    codepage, cell_count, grid_size = None, 0, 0
    positions = set()
    extent = [0, 0]
    for offset, code, body in _records(stream):
        if code in {0x0006, 0x0206, 0x0406, 0x0221, 0x04BC, 0x0021}:
            raise InputError('旧版 XLS 公式尚不能完整溯源；请从可信来源导出固定值或提供原始 XLSX。')
        if code not in RECORDS:
            raise InputError(f'XLS 含未支持或高风险记录 0x{code:04X}，不忽略后继续取数。')
        if code == 0x0809:
            if active is not None or len(body) < 8:
                raise InputError('XLS 子流结构无效。')
            version, kind = struct.unpack_from('<HH', body)
            if offset == 0:
                if version != 0x0500 or kind != 5:
                    raise InputError('目前仅受控读取无公式的 BIFF5/7 XLS；其他版本须提供原始 XLSX。')
                active = 'globals'
            else:
                if not globals_done or kind != 0x10 or version not in {0x0500, 0x0600}:
                    raise InputError('XLS 含非工作表子流或不支持的版本。')
                sheet_starts.append(offset)
                active = 'sheet'
                positions = set()
                extent = [0, 0]
        elif active is None:
            raise InputError('XLS 含游离或尾随记录。')
        elif code == 0x000A:
            if body:
                raise InputError('XLS EOF 记录无效。')
            if active == 'globals':
                globals_done = True
            else:
                grid_size += extent[0] * extent[1]
                if grid_size > MAX_CELLS:
                    raise InputError('XLS 全部工作表展开范围超限。')
            active = None
        elif code == 0x0042:
            if active != 'globals' or codepage is not None or len(body) != 2:
                raise InputError('XLS 字符编码记录重复或无效。')
            codepage = struct.unpack('<H', body)[0]
        elif code == 0x0085:
            if active != 'globals' or len(body) < 8 or body[5] != 0 or body[4] not in {0, 1, 2}:
                raise InputError('XLS 含宏表、图表或无效工作表描述。')
            if len(body) != 7 + body[6] or not body[6] or len(bounds) >= 40:
                raise InputError('XLS 工作表名称或数量无效。')
            bounds.append((struct.unpack_from('<I', body)[0], body[7:]))
        elif code in {0x0017, 0x0018, 0x0016}:
            if active != 'globals':
                raise InputError('XLS 工作表内存在不支持的引用定义。')
            if code == 0x0017:
                if len(body) < 3 or body[1] != 3 or len(body) != body[0] + 2:
                    raise InputError('XLS 含外部引用、DDE 或插件链接。')
                externals.append(body[2:])
            elif code == 0x0018:
                names.append(body)
            else:
                if len(body) != 2 or external_count is not None:
                    raise InputError('XLS 引用计数无效或重复。')
                external_count = struct.unpack('<H', body)[0]
        elif code == 0x003C and previous not in {0x004D, 0x003C}:
            raise InputError('XLS 含未支持的延续记录。')
        elif code in CELL_RECORDS | {0x0200, 0x0208}:
            if active != 'sheet' or len(body) < 6:
                raise InputError('XLS 单元格或范围记录无效。')
            row, column = struct.unpack_from('<HH', body)
            first_column = column
            if code == 0x0200:
                if len(body) != 10:
                    raise InputError('XLS 工作表维度记录无效。')
                first_row, row, first_column, column = struct.unpack_from('<4H', body)
                if first_row > row or first_column > column:
                    raise InputError('XLS 工作表维度倒置。')
            elif code == 0x0208:
                row, column = struct.unpack_from('<HH', body, 2)
                row = struct.unpack_from('<H', body)[0] + 1
            elif code in {0x00BD, 0x00BE}:
                if len(body) < 8:
                    raise InputError('XLS 多单元格记录截断。')
                last = struct.unpack_from('<H', body, len(body) - 2)[0]
                if last < column or len(body) != 6 + (last - column + 1) * (6 if code == 0x00BD else 2):
                    raise InputError('XLS 多单元格长度无效。')
                cell_count += last - column
                column = last
            if row > 30000 or column > 100 or (code in CELL_RECORDS and (row >= 30000 or column >= 100)):
                raise InputError('每张 Excel 工作表最多 30000 行、100 列。')
            # DIMENSIONS may understate the real sparse-cell bounds. Bound the
            # rectangular allocation that xlrd can derive from every cell/ROW,
            # not only the declared dimensions or the count of populated cells.
            extent[0] = max(extent[0], row + (code in CELL_RECORDS))
            extent[1] = max(extent[1], column + (code in CELL_RECORDS))
            if grid_size + extent[0] * extent[1] > MAX_CELLS:
                raise InputError('XLS 全部工作表展开范围超限。')
            if code in CELL_RECORDS:
                for c in range(first_column, column + 1):
                    if (row, c) in positions:
                        raise InputError('XLS 单元格位置重复，不能静默覆盖。')
                    positions.add((row, c))
            if code == 0x0203 and (len(body) != 14 or not math.isfinite(struct.unpack_from('<d', body, 6)[0])):
                raise InputError('XLS 数值记录无效或含非有限数值。')
            cell_count += 1
            if cell_count > MAX_CELLS:
                raise InputError('XLS 单元格数量超限。')
        previous = code
    if active is not None or not globals_done or not bounds or len(bounds) != len(sheet_starts) or codepage is None:
        raise InputError('XLS 子流不完整或工作表数量不一致。')
    if len({b[1] for b in bounds}) != len(bounds) or sorted(b[0] for b in bounds) != sheet_starts:
        raise InputError('XLS 工作表位置重复或与子流不一致。')
    if (externals and external_count != len(externals)) or any(name not in {b[1] for b in bounds} for name in externals):
        raise InputError('XLS 内部引用数量或工作表名称不一致。')
    _names(names, externals, [b[1] for b in bounds])
    try:
        book = xlrd.open_workbook(file_contents=stream, formatting_info=True,
                                  logfile=StringIO(), ignore_workbook_corruption=False)
        if book.biff_version not in {50, 70} or book.nsheets != len(bounds):
            raise InputError('XLS 解码版本或工作表数量与容器检查不一致。')
        return book
    except InputError:
        raise
    except Exception:
        raise InputError('XLS 工作簿损坏或不支持，不忽略解析错误。') from None


def open_legacy_workbook(raw):
    book = read(raw)
    result = Workbook()
    result.remove(result.active)
    try:
        for source in book.sheets():
            if source.nrows > 30000 or source.ncols > 100:
                raise InputError('每张 Excel 工作表最多 30000 行、100 列。')
            target = result.create_sheet(source.name)
            target.sheet_state = ('visible', 'hidden', 'veryHidden')[source.visibility]
            for r in range(source.nrows):
                for c in range(source.ncols):
                    cell = source.cell(r, c)
                    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                        continue
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate_as_datetime(value, book.datemode)
                    if isinstance(value, float) and not math.isfinite(value):
                        raise InputError('XLS 含非有限数值。')
                    output = target.cell(r + 1, c + 1)
                    output.value = value
                    # Literal strings beginning '=' remain strings, not new formulas.
                    if cell.ctype == xlrd.XL_CELL_TEXT:
                        output.data_type = 's'
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        output.value = bool(value)
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        output.value = xlrd.error_text_from_code.get(value, '#VALUE!')
                        output.data_type = 'e'
                    if cell.xf_index is not None:
                        xf = book.xf_list[cell.xf_index]
                        output.number_format = book.format_map[xf.format_key].format_str
            for r1, r2, c1, c2 in source.merged_cells:
                if r1 < 0 or c1 < 0 or r2 > 30000 or c2 > 100:
                    raise InputError('XLS 合并范围超限。')
                target.merge_cells(start_row=r1 + 1, end_row=r2, start_column=c1 + 1, end_column=c2)
        return result
    finally:
        book.release_resources()
