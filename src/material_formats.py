"""Allowlisted material containers, validated before persistence or model input."""
import csv
from io import BytesIO, StringIO
from pathlib import PurePosixPath
import re
import struct
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

from PIL import Image, UnidentifiedImageError

from . import config
from .input_errors import InputError

VERSION = 'material-formats-v1'
EXTENSIONS = ('xlsx', 'xls', 'csv', 'tsv', 'xml', 'pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'zip')
IMAGE_FORMATS = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'tif': 'TIFF', 'tiff': 'TIFF'}
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024
MAX_PDF_PAGES = 300
PDF_SEGMENT_PAGES = 50
MAX_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 100_000_000
SCRIPT_EXTENSIONS = {'exe', 'dll', 'com', 'scr', 'msi', 'bat', 'cmd', 'ps1', 'sh', 'py', 'js', 'vbs', 'html', 'htm', 'svg', 'php', 'jar'}


def filename(name, *, member=False):
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise InputError('文件名为空或超过 255 字符。')
    if any(unicodedata.category(c).startswith('C') for c in name):
        raise InputError('文件名含控制字符或不可见字符。')
    if name != name.strip() or ':' in name or '%' in name or '\\' in name or name.endswith(('.', ' ')):
        raise InputError('文件名含不安全字符或歧义。')
    parts = name.split('/')
    if (not member and len(parts) != 1) or any(p in {'', '.', '..'} for p in parts):
        raise InputError('文件名不能包含不安全目录路径。')
    path = PurePosixPath(name)
    if path.is_absolute():
        raise InputError('文件名不能使用绝对路径。')
    extension = path.suffix.lower().lstrip('.')
    if extension not in EXTENSIONS:
        hints = {'xlsm': '不支持带宏工作簿，请导出不含宏的 XLSX。',
                 'ofd': 'OFD 暂未接入解析，请使用可信阅读器导出 PDF 或原始 XML。',
                 'ods': 'ODS 暂未接入解析，请导出 XLSX 或 CSV/TSV。'}
        raise InputError(hints.get(extension, '不支持此格式；允许 ' + '、'.join('.'+x for x in EXTENSIONS) + '。'))
    if any(s.lower().lstrip('.') in SCRIPT_EXTENSIONS for s in PurePosixPath(unicodedata.normalize('NFKC', name)).suffixes[:-1]):
        raise InputError('文件名含脚本或可执行文件扩展名，不能以双扩展名伪装材料。')
    return extension


def xml_root(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeError:
            raise InputError('XML 必须采用 UTF-8 或带 BOM 的 UTF-16 编码。') from None
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        raise InputError('XML 不允许 DTD 或实体声明。')
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise InputError('XML 结构无效。') from None
    stack, count = [(root, 1)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 100000 or depth > 40:
            raise InputError('XML 节点过多或嵌套过深。')
        stack.extend((child, depth + 1) for child in node)
    return root


def archive_entries(archive, *, workbook=False):
    entries = archive.infolist()
    if len(entries) > (1000 if workbook else 100):
        raise InputError('压缩容器条目过多。')
    names, total = set(), 0
    for item in entries:
        name = item.filename.rstrip('/') if item.is_dir() else item.filename
        normalized = unicodedata.normalize('NFKC', name).casefold()
        if not name or normalized in names:
            raise InputError('压缩容器存在重复或歧义路径。')
        names.add(normalized)
        path = PurePosixPath(name)
        if ('\\' in name or ':' in name or path.is_absolute() or
                any(p in {'', '.', '..'} for p in name.split('/')) or
                any(unicodedata.category(c).startswith('C') for c in name)):
            raise InputError('ZIP 含不安全路径。')
        mode = item.external_attr >> 16
        if item.flag_bits & 1 or mode & 0o170000 not in {0, 0o100000, 0o040000}:
            raise InputError('ZIP 不支持加密文件或符号链接/特殊文件。')
        if item.is_dir():
            continue
        if item.compress_type not in {0, 8}:
            raise InputError('ZIP 仅支持普通存储或 Deflate 压缩。')
        total += item.file_size
        if item.file_size > (MAX_TOTAL if workbook else MAX_FILE) or item.file_size > max(1, item.compress_size)*250 or total > MAX_TOTAL:
            raise InputError('ZIP 解压大小或压缩比例超限。')
    return [i for i in entries if not i.is_dir()]


def _printer_settings_parts(archive, names):
    """Recognize bounded, inert DEVMODEW metadata; never invoke a print driver.

    This is not a general binary exception or malware certification. Require
    the native content type and a worksheet pageSetup relationship, and leave
    private driver bytes opaque. Cell parsers/model input do not consume them.
    """
    parts = {name for name in names if re.fullmatch(r'xl/printerSettings/printerSettings[1-9]\d*\.bin', name)}
    if not parts:
        return set()
    ct = 'http://schemas.openxmlformats.org/package/2006/content-types'
    rel = 'http://schemas.openxmlformats.org/package/2006/relationships'
    office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    sheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings'
    types = xml_root(archive.read('[Content_Types].xml'))
    defaults = [n.get('ContentType') for n in types.findall(f'{{{ct}}}Default') if n.get('Extension') == 'bin']
    for part in parts:
        overrides = [n.get('ContentType') for n in types.findall(f'{{{ct}}}Override') if n.get('PartName') == '/' + part]
        if (overrides or defaults) != [mime]:
            raise InputError('XLSX 打印设置部件类型缺失、重复或不符。')
        if f'xl/printerSettings/_rels/{PurePosixPath(part).name}.rels' in names:
            raise InputError('XLSX 打印设置不能含其他部件关系。')
        info = archive.getinfo(part)
        if not 220 <= info.file_size <= 65536:
            raise InputError('XLSX 打印设置大小超限或结构不符。')
        data = archive.read(part)
        version, _, size, extra = struct.unpack_from('<HHHH', data, 64)
        if data.startswith((b'MZ', b'\x7fELF', b'#!')) or version != 0x401 or size != 220 or len(data) != size + extra:
            raise InputError('XLSX 打印设置不是支持的 DEVMODEW 元数据。')
        try:
            data[:64].decode('utf-16-le')
        except UnicodeError:
            raise InputError('XLSX 打印设置设备名称编码无效。') from None
    referenced = set()
    for name in names:
        if not name.endswith('.rels'):
            continue
        root = xml_root(archive.read(name))
        relationships = root.findall(f'{{{rel}}}Relationship')
        if any(n.get('Type') == office + '/printerSettings' for n in relationships):
            ids = [n.get('Id') for n in relationships]
            if len(set(ids)) != len(ids):
                raise InputError('XLSX 打印设置所在关系表的标识重复。')
        for node in relationships:
            if node.get('Type') != office + '/printerSettings':
                continue
            match = re.fullmatch(r'xl/worksheets/_rels/(sheet[1-9]\d*\.xml)\.rels', name)
            target = node.get('Target', '')
            part = 'xl/' + target[3:] if target.startswith('../printerSettings/') else target[1:] if target.startswith('/xl/') else ''
            worksheet = 'xl/worksheets/' + match[1] if match else ''
            if (not match or worksheet not in names or part not in parts or part in referenced or
                    node.get('TargetMode', 'Internal') != 'Internal' or not node.get('Id')):
                raise InputError('XLSX 打印设置关系无效或重复。')
            page = xml_root(archive.read(worksheet)).find(f'{{{sheet_ns}}}pageSetup')
            if page is None or page.get(f'{{{office}}}id') != node.get('Id'):
                raise InputError('XLSX 打印设置缺少工作表页面设置引用。')
            referenced.add(part)
    if referenced != parts:
        raise InputError('XLSX 打印设置存在未引用部件。')
    return parts


def _formula_location(archive, part, root, formula):
    """Locate a rejected expression without echoing its contents or targets."""
    address = next((cell.get('r', '') for cell in root.iter()
                    if cell.tag.rsplit('}', 1)[-1] == 'c' and formula in list(cell)), '')
    return _cell_location(archive, part, address)


def _cell_location(archive, part, address):
    """Resolve only a unique worksheet relationship; never guess its identity."""
    if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', address):
        return part[:255]
    book = xml_root(archive.read('xl/workbook.xml'))
    rel_path = 'xl/_rels/workbook.xml.rels'
    if rel_path not in archive.namelist():
        return f'{part[:255]}!{address}'
    relations = xml_root(archive.read(rel_path))
    office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    locations = []
    for sheet in book.iter():
        if sheet.tag.rsplit('}', 1)[-1] != 'sheet':
            continue
        identifier = sheet.get(f'{{{office}}}id')
        if not identifier:
            continue
        matches = [rel for rel in relations if rel.get('Id') == identifier]
        if (len(matches) != 1 or matches[0].get('TargetMode', '').casefold() == 'external' or
                matches[0].get('Type') != office + '/worksheet'):
            continue
        target = matches[0].get('Target', '')
        path = PurePosixPath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if '..' not in path.parts and str(path) == part:
            name = ''.join(c for c in sheet.get('name', '') if not unicodedata.category(c).startswith('C'))
            locations.append(f'{name[:128] or part[:255]}!{address}')
    if len(locations) == 1:
        return locations[0]
    return f'{part[:255]}!{address}'


def _worksheet_positions(archive, part, root):
    """Reject source positions that readers could overwrite or interpret differently.

    Missing row/cell references retain the reader's sequential coordinates. No
    XML is rewritten, and both fixed values and formulas share the same grid.
    """
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    if root.tag != f'{{{ns}}}worksheet':
        raise InputError(f'XLSX 工作表根结构不符（部件：{part[:255]}），未采用金额。')
    tables = root.findall(f'{{{ns}}}sheetData')
    if len(tables) != 1:
        raise InputError(f'XLSX 工作表须有唯一单元格表（部件：{part[:255]}）。')
    rows = set(tables[0])
    cells = {cell for row in rows for cell in row}
    if set(root.iter(f'{{{ns}}}row')) != rows or set(root.iter(f'{{{ns}}}c')) != cells:
        raise InputError(f'XLSX 行或单元格不在唯一表格结构内（部件：{part[:255]}），未采用金额。')
    seen, row_index = set(), 0
    for row in tables[0]:
        if row.tag != f'{{{ns}}}row':
            raise InputError(f'XLSX 单元格表含未支持结构（部件：{part[:255]}）。')
        previous_row = row_index
        reference = row.get('r')
        if reference is not None and not re.fullmatch(r'[0-9]{1,7}', reference):
            raise InputError(f'XLSX 行位置无效（部件：{part[:255]}）。')
        row_index = int(reference) if reference is not None else row_index + 1
        if not 1 <= row_index <= 30000:
            raise InputError('每张 Excel 工作表最多 30000 行、100 列；行位置无效或超限。')
        column = 0
        for cell in row:
            if cell.tag != f'{{{ns}}}c':
                raise InputError(f'XLSX 行内含未支持结构（部件：{part[:255]}）。')
            reference = cell.get('r')
            if reference is None:
                column += 1
                cell_row = row_index
            else:
                match = re.fullmatch(r'([A-Za-z]{1,3})([0-9]{1,7})', reference)
                if not match:
                    raise InputError(f'XLSX 单元格位置无效（部件：{part[:255]}）。')
                column = 0
                for letter in match[1].upper():
                    column = column * 26 + ord(letter) - ord('A') + 1
                cell_row = int(match[2])
            if not 1 <= column <= 100 or not 1 <= cell_row <= 30000:
                raise InputError('每张 Excel 工作表最多 30000 行、100 列；单元格位置无效或超限。')
            letters, number = '', column
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(ord('A') + remainder) + letters
            address = f'{letters}{cell_row}'
            if (cell_row, column) in seen:
                location = _cell_location(archive, part, address)
                raise InputError(f'XLSX 单元格位置重复（位置：{location}），未采用金额；请核对来源导出，原件不改写。')
            if cell_row != row_index:
                location = _cell_location(archive, part, address)
                raise InputError(f'XLSX 单元格与所在行位置不一致（位置：{location}），未采用金额。')
            seen.add((cell_row, column))
        if row_index <= previous_row:
            raise InputError(f'XLSX 行位置重复或顺序不明（部件：{part[:255]}；行 {row_index}），未采用金额。')


def xlsx(raw):
    if not raw.startswith(b'PK\x03\x04'):
        raise InputError('XLSX 内容不是有效的工作簿容器，不能只修改扩展名。')
    zip_end(raw)
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive_entries(archive, workbook=True)
            names = {i.filename for i in entries}
            if not {'[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml'}.issubset(names):
                raise InputError('XLSX 缺少必要工作簿结构。')
            printer_parts = _printer_settings_parts(archive, names)
            worksheet_parts = {name for name in names if name.startswith('xl/worksheets/') and name.endswith('.xml')}
            office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            if 'xl/_rels/workbook.xml.rels' in names:
                for rel in xml_root(archive.read('xl/_rels/workbook.xml.rels')):
                    if rel.get('Type') != office + '/worksheet':
                        continue
                    target = rel.get('Target', '')
                    path = PurePosixPath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                    if not target or '..' in path.parts or str(path) not in names:
                        raise InputError('XLSX 工作表关系缺失或路径无效，未采用金额。')
                    worksheet_parts.add(str(path))
            for item in entries:
                lower = item.filename.casefold()
                if (any(part in lower for part in ('vbaproject', 'macrosheet', 'dialogsheets', 'activex/', 'embeddings/', 'externallinks/')) or
                        lower.rsplit('.', 1)[-1] in SCRIPT_EXTENSIONS or
                        (lower.endswith('.bin') and item.filename not in printer_parts)):
                    raise InputError('XLSX 含宏、嵌入对象、外部链接或可执行部件。')
                content = archive.read(item)
                if lower.endswith(('.xml', '.rels')) or item.filename in worksheet_parts:
                    root = xml_root(content)
                    if item.filename in worksheet_parts or root.tag == '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}worksheet':
                        _worksheet_positions(archive, item.filename, root)
                    for node in root.iter():
                        if node.tag.rsplit('}', 1)[-1] == 'f':
                            formula = node.text or ''
                            if ('|' in formula or re.search(r'\b(?:HYPERLINK|WEBSERVICE|RTD|CALL|REGISTER|EXEC|EVALUATE)\s*\(', formula, re.I)
                                    or re.search(r'\[[^\]]+\][^!]*!', formula)):
                                location = _formula_location(archive, item.filename, root, node)
                                raise InputError(f'XLSX 含外部调用或危险公式（位置：{location}），'
                                                 '请从来源系统导出不含外链的固定值副本；原件不改写。')
                    if lower.endswith('.rels') and any(
                            node.attrib.get('TargetMode', '').casefold() == 'external' for node in root.iter()):
                        raise InputError('XLSX 含外部关系，须导出不含外链的工作簿。')
                    if lower == '[content_types].xml':
                        if any('macroenabled' in node.attrib.get('ContentType', '').casefold() for node in root.iter()):
                            raise InputError('XLSX 实际为带宏工作簿。')
                        workbook_type = [n.attrib.get('ContentType') for n in root.iter() if n.attrib.get('PartName') == '/xl/workbook.xml']
                        if workbook_type != ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml']:
                            raise InputError('工作簿类型与 XLSX 扩展名不符。')
    except (BadZipFile, RuntimeError, NotImplementedError, UnicodeError):
        raise InputError('XLSX 容器损坏或编码无效。') from None


def table_rows(raw, extension):
    text = None
    for encoding in ('utf-8-sig', 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8', 'gb18030'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            pass
    if text is None or '\x00' in text or any(ord(c)<32 and c not in '\r\n\t' for c in text):
        raise InputError('CSV/TSV 文本编码无效或含二进制内容。')
    rows = []
    width = None
    populated = 0
    try:
        reader = csv.reader(StringIO(text, newline=''), delimiter='\t' if extension == 'tsv' else ',', strict=True)
        for row in reader:
            if len(row) > 100 or len(rows) >= 30000 or any(len(c) > 32000 for c in row):
                raise InputError('CSV/TSV 超过行列或单元格限制。')
            if not row or all(not c.strip() for c in row):
                rows.append(row)
                continue
            if width is None:
                width = len(row)
            for cell in row:
                value = cell.lstrip()
                if value.startswith(('=', '@')) or (value.startswith(('+', '-')) and not re.fullmatch(r'[+-]\d[\d,]*(?:\.\d+)?%?', value)):
                    raise InputError('CSV/TSV 含公式或可执行表达式，请导出纯数值和文本。')
            rows.append(row)
            populated += 1
    except csv.Error:
        raise InputError('CSV/TSV 结构无效。') from None
    if populated < 2:
        raise InputError('CSV/TSV 须有表头及数据行，至少两列；不接受普通脚本文本。')
    tokens = [[re.sub(r'\s+', '', c) for c in row] for row in rows[:10]]
    header = next((i for i, row in enumerate(tokens) if len(row) >= 2 and any(v in {
        '项目', '资产', '科目编码', '科目代码', '科目编号'} for v in row)), None)
    titles = {config.SHEET_INCOME, config.SHEET_BALANCE, config.SHEET_CASHFLOW,
              config.SHEET_ACCOUNTS, config.SHEET_DECLARATION, '科目余额', '科目发生额及余额表'}
    preamble = header is not None and any(c in titles for row in tokens[:header] for c in row)
    if preamble:
        # Only explicitly titled financial exports may have short pre-header records.
        width = len(rows[header])
    if width is None or width < 2:
        raise InputError('CSV/TSV 须有表头及数据行，至少两列；不接受普通脚本文本。')
    for index, row in enumerate(rows):
        if any(c.strip() for c in row) and len(row) != width:
            if not preamble or index >= header or len(row) > width:
                raise InputError('CSV/TSV 每行列数须一致；只允许明确财务标题前置行较短。')
    # Keep empty source records in place; dropping them would falsify cell row provenance.
    return [row + [''] * (width - len(row)) if any(c.strip() for c in row) else [''] * width for row in rows]


def image_pages(raw, extension):
    if extension == 'png' and not raw.endswith(b'\x00\x00\x00\x00IEND\xaeB\x60\x82'):
        raise InputError('PNG 结束结构无效或夹带尾部内容。')
    if extension in {'jpg', 'jpeg'} and not raw.endswith(b'\xff\xd9'):
        raise InputError('JPEG 结束结构无效或夹带尾部内容。')
    try:
        with Image.open(BytesIO(raw), formats=[IMAGE_FORMATS[extension]]) as source:
            if source.format != IMAGE_FORMATS[extension]:
                raise InputError('图片实际格式与扩展名不符。')
            count = getattr(source, 'n_frames', 1)
            if not 1 <= count <= 50 or (extension not in {'tif', 'tiff'} and count != 1):
                raise InputError('仅 TIFF 可上传多页，最多 50 页；不接受动画图片。')
            pages, total = [], 0
            for index in range(count):
                source.seek(index)
                width, height = source.size
                total += width*height
                if width<1 or height<1 or width*height>MAX_PIXELS or total>MAX_TOTAL_PIXELS or max(width,height)>20000:
                    raise InputError('图片尺寸或总像素超过解析预算。')
                source.load()
                pages.append({'page': index+1, 'width': width, 'height': height})
            return pages
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InputError('图片无效、损坏或尺寸超限。') from None


def zip_end(raw):
    offset = raw.rfind(b'PK\x05\x06', max(0, len(raw)-65557))
    if offset < 0 or offset+22 > len(raw) or offset+22+struct.unpack_from('<H', raw, offset+20)[0] != len(raw):
        raise InputError('压缩容器结束结构无效或夹带尾部内容。')


def pdf(raw):
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdftypes import resolve1, PDFStream
    from pdfminer.psparser import PSLiteral
    if not raw.startswith(b'%PDF-') or not raw.rstrip().endswith(b'%%EOF'):
        raise InputError('PDF 文件头或结束标记无效。')
    dangerous = {'JavaScript', 'JS', 'Launch', 'EmbeddedFile', 'EmbeddedFiles', 'RichMedia', 'XFA', 'OpenAction', 'AA', 'SubmitForm', 'ImportData', 'GoToR', 'GoToE'}
    try:
        doc = PDFDocument(PDFParser(BytesIO(raw)))
        if doc.encryption:
            raise InputError('不接受加密 PDF，请导出可读取原件。')
        ids = {i for xref in doc.xrefs for i in xref.get_objids()}
        if len(ids)>20000:
            raise InputError('PDF 对象数量超过预算。')
        from pdfminer.pdfpage import PDFPage
        if sum(1 for _ in PDFPage.create_pages(doc)) > MAX_PDF_PAGES:
            raise InputError(f'PDF 最多 {MAX_PDF_PAGES} 页；长文件须明确选择读取片段。')
        budget = [200000]
        decoded_bytes = 0
        def inspect(value, depth=0):
            budget[0] -= 1
            if depth>40 or budget[0]<0:
                raise InputError('PDF 对象结构过深或过多。')
            if isinstance(value, PDFStream):
                value = value.attrs
            if isinstance(value, PSLiteral) and value.name in dangerous:
                raise InputError('PDF 含脚本、自动动作或嵌入附件，不能作为普通材料上传。')
            if isinstance(value, dict):
                if set(value) & dangerous:
                    raise InputError('PDF 含脚本、自动动作或嵌入附件，不能作为普通材料上传。')
                for child in value.values():
                    inspect(child, depth+1)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    inspect(child, depth+1)
        for identifier in ids:
            value = resolve1(doc.getobj(identifier))
            inspect(value)
            if isinstance(value, PDFStream):
                decoded_bytes += len(value.get_data())
                if decoded_bytes > MAX_TOTAL:
                    raise InputError('PDF 解码内容超过 50MB。')
    except InputError:
        raise
    except Exception:
        raise InputError('PDF 结构损坏或不支持。') from None


def validate(name, raw, *, member=False):
    extension = filename(name, member=member)
    if not isinstance(raw, bytes) or not 0<len(raw)<=MAX_FILE:
        raise InputError('单个材料须非空且不超过 10MB。')
    info = {'version': VERSION, 'type': extension, 'status': 'passed', 'size': len(raw)}
    if extension == 'xlsx':
        xlsx(raw)
    elif extension == 'xls':
        from .legacy_workbooks import VERSION as legacy_version, read
        book = read(raw)
        try:
            info.update(parser=legacy_version, sheets=book.nsheets, formulas='rejected')
        finally:
            book.release_resources()
    elif extension in {'csv', 'tsv'}:
        rows = table_rows(raw, extension)
        info.update(rows=len(rows), columns=len(rows[0]))
    elif extension == 'xml':
        root = xml_root(raw)
        if root.tag.rsplit('}',1)[-1].lower() != 'xbrl' or not any(
                isinstance(node.tag,str) and node.tag.startswith('{http://xbrl.mof.gov.cn/taxonomy/') for node in root.iter()):
            raise InputError('XML 不是财政部数电发票 XBRL 实例，不接收任意 XML 或脚本标记。')
    elif extension == 'pdf':
        pdf(raw)
    elif extension in IMAGE_FORMATS:
        info['pages'] = image_pages(raw, extension)
    return info


def expand(files):
    if not isinstance(files, list) or not 1<=len(files)<=20 or sum(len(raw) for _,raw in files)>MAX_TOTAL:
        raise InputError('每次最多 20 个文件、合计 50MB。')
    output, names, total = [], set(), 0
    for name, raw in files:
        extension = filename(name)
        if not isinstance(raw, bytes) or not 0<len(raw)<=MAX_FILE:
            raise InputError('单个材料须非空且不超过 10MB。')
        folded = unicodedata.normalize('NFKC', name).casefold()
        if folded in names:
            raise InputError('同批文件名重复或存在歧义。')
        names.add(folded)
        if extension != 'zip':
            info = validate(name, raw)
            output.append((name, raw, info))
            total += len(raw)
        else:
            if not raw.startswith(b'PK\x03\x04'):
                raise InputError('ZIP 内容与扩展名不符。')
            zip_end(raw)
            try:
                with ZipFile(BytesIO(raw)) as archive:
                    entries = archive_entries(archive)
                    if not entries or len(entries)+len(output)>20:
                        raise InputError('ZIP 无有效材料或解压后超过 20 个文件。')
                    for item in entries:
                        if filename(item.filename, member=True)=='zip':
                            raise InputError('不支持嵌套 ZIP。')
                        data = archive.read(item)
                        info = validate(item.filename, data, member=True)
                        total += len(data)
                        output.append((name+'/'+item.filename, data, info))
            except (BadZipFile, RuntimeError, NotImplementedError):
                raise InputError('ZIP 无法读取或已损坏。') from None
        if total>MAX_TOTAL or len(output)>20:
            raise InputError('解压后最多 20 个文件、合计 50MB。')
    return output
