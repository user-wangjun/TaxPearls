"""Adversarial containers and real upload boundaries; never execute payloads."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import base64
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import struct
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZipInfo, ZIP_STORED
import zlib

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from PIL import Image

from src import material_formats as formats, material_format_guard as guard, materials
from src.ai_extraction import AIExtractor, Extraction
from src.settings import AISettings
from tests.test_materials import accounts, workbook, zip_bytes, COMPANY, KEYS, selections
from tests.test_invoices import invoice_xml
from tests.test_financial_import import export_book
from webapp import app as module, material_batches as batches, uploads
from webapp.storage import Store


def picture(kind='PNG', multipage=False):
    out = BytesIO()
    with Image.new('RGB', (64, 80), 'white') as first, Image.new('RGB', (64, 80), 'gray') as second:
        first.save(out, format=kind, save_all=True, append_images=[second]) if multipage else first.save(out, format=kind)
    return out.getvalue()


def pdf(extra=b'', catalog=b''):
    objects = [b'<< /Type /Catalog /Pages 2 0 R ' + catalog + b' >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>']
    if extra:
        objects.append(extra)
    out, positions = b'%PDF-1.7\n', [0]
    for index, body in enumerate(objects, 1):
        positions.append(len(out))
        out += str(index).encode() + b' 0 obj\n' + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(positions)}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in positions[1:])
    return out + f'trailer\n<< /Size {len(positions)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def printer_workbook(*, change='', raw=None):
    """Synthetic package metadata only; not a financial source or driver call."""
    ct = 'http://schemas.openxmlformats.org/package/2006/content-types'
    rel = 'http://schemas.openxmlformats.org/package/2006/relationships'
    office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    sheet = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(BytesIO(raw or accounts())) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    types = ET.fromstring(parts['[Content_Types].xml'])
    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings'
    if change != 'missing_type':
        ET.SubElement(types, f'{{{ct}}}Default', Extension='bin', ContentType='application/octet-stream' if change == 'wrong_type' else mime)
    parts['[Content_Types].xml'] = ET.tostring(types)
    page = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    setup = page.find(f'{{{sheet}}}pageSetup')
    if setup is None:
        setup = ET.SubElement(page, f'{{{sheet}}}pageSetup')
    setup.set(f'{{{office}}}id', 'wrong' if change == 'wrong_reference' else 'printer1')
    parts['xl/worksheets/sheet1.xml'] = ET.tostring(page)
    relationships = ET.Element(f'{{{rel}}}Relationships')
    if change != 'unreferenced':
        ET.SubElement(relationships, f'{{{rel}}}Relationship', Id='printer1',
            Type=office + '/printerSettings', Target='../printerSettings/printerSettings1.bin',
            TargetMode='External' if change == 'external' else 'Internal')
    if change == 'duplicate_id':
        ET.SubElement(relationships, f'{{{rel}}}Relationship', Id='printer1',
            Type=office + '/drawing', Target='../drawings/drawing1.xml')
    if change == 'relative_wrong':
        relationships[0].set('Target', 'xl/printerSettings/printerSettings1.bin')
    if change == 'absolute':
        relationships[0].set('Target', '/xl/printerSettings/printerSettings1.bin')
    parts['xl/worksheets/_rels/sheet1.xml.rels'] = ET.tostring(relationships)
    data = bytearray(220)
    data[:10] = 'Test\0'.encode('utf-16-le')
    struct.pack_into('<HHHH', data, 64, 0x401, 1, 220, 0)
    if change == 'private':
        opaque = b'opaque driver bytes not model text'
        struct.pack_into('<H', data, 70, len(opaque))
        data.extend(opaque)
    if change == 'oversized':
        struct.pack_into('<H', data, 70, 65535)
        data.extend(b'd' * 65535)
    if change == 'bad_size':
        struct.pack_into('<H', data, 70, 1)
    elif change == 'executable':
        data[:2] = b'MZ'
    parts['xl/printerSettings/printerSettings1.bin'] = bytes(data)
    if change == 'outgoing':
        parts['xl/printerSettings/_rels/printerSettings1.bin.rels'] = ET.tostring(relationships)
    if change == 'unknown_binary':
        parts['xl/other.bin'] = bytes(data)
    if change == 'macro':
        parts['xl/vbaProject.bin'] = b'macro'
    return zip_bytes(list(parts.items()))


def position_workbook(rows, *, raw=None, part='xl/worksheets/sheet1.xml', missing_relations=False, hidden=False):
    """Constructed XML only; never a modified user/public financial source."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(BytesIO(raw or workbook('来源表', [['构造数据']]))) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(parts[part])
    data = root.find(f'{{{ns}}}sheetData')
    data.clear()
    data.extend(ET.fromstring(f'<sheetData xmlns="{ns}">{rows}</sheetData>'))
    parts[part] = ET.tostring(root)
    if hidden:
        book = ET.fromstring(parts['xl/workbook.xml'])
        for sheet in book.findall(f'{{{ns}}}sheets/{{{ns}}}sheet'):
            sheet.set('state', 'hidden')
        parts['xl/workbook.xml'] = ET.tostring(book)
    if missing_relations:
        parts.pop('xl/_rels/workbook.xml.rels')
    return zip_bytes(list(parts.items()))


class FormatContainers(unittest.TestCase):
    def test_duplicate_source_positions_reject_fixed_and_formula_collisions(self):
        first = '<c r="C3"><v>10</v></c>'
        for second in ('<c r="C3"><v>99</v></c>', '<c r="c3"><v>99</v></c>',
                       '<c r="C03"><v>99</v></c>', '<c r="C3"><f>1+1</f><v>2</v></c>'):
            for order in (first + second, second + first):
                with self.subTest(order=order):
                    raw = position_workbook(f'<row r="3">{order}</row>')
                    before = sha256(raw).hexdigest()
                    with self.assertRaisesRegex(materials.InputError, '位置重复') as caught:
                        formats.validate('duplicate.xlsx', raw)
                    self.assertIn('来源表!C3', str(caught.exception))
                    self.assertNotIn('99', str(caught.exception))
                    self.assertNotIn('1+1', str(caught.exception))
                    self.assertEqual(sha256(raw).hexdigest(), before)

    def test_duplicate_positions_cover_separate_rows_implicit_cells_and_hidden_sheets(self):
        variants = (
            '<row r="3"><c r="C3"><v>10</v></c></row><row r="3"><c r="C3"><v>99</v></c></row>',
            '<row r="3"><c/><c/><c><v>10</v></c><c r="C3"><v>99</v></c></row>',
            '<row r="3"><c r="C3"><v>10</v></c><c r="B3"/><c><v>99</v></c></row>',
        )
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaisesRegex(materials.InputError, '来源表!C3'):
                formats.validate('hidden.xlsx', position_workbook(rows, hidden=True))
        with self.assertRaisesRegex(materials.InputError, r'xl/worksheets/sheet1.xml!C3'):
            formats.validate('ambiguous.xlsx', position_workbook(variants[0], missing_relations=True))

    def test_unique_implicit_positions_match_both_readers_without_rewriting_original(self):
        from src.workbooks import open_workbook
        raw = position_workbook('<row><c><v>10</v></c><c r="C1"><v>20</v></c>'
            '<c><v>30</v></c></row><row><c><v>40</v></c></row>'
            '<row r="5"><c r="CV5"><v>50</v></c></row>')
        before = sha256(raw).hexdigest()
        result = guard.check([('implicit.xlsx', raw)], expand=True)
        self.assertEqual(result[0][1], raw)
        for read_only in (False, True):
            book = open_workbook(raw, read_only=read_only)
            try:
                sheet = book['来源表']
                self.assertEqual([sheet[address].value for address in ('A1', 'C1', 'D1', 'A2', 'CV5')],
                                 [10, 20, 30, 40, 50])
            finally:
                book.close()
        self.assertEqual(sha256(raw).hexdigest(), before)

    def test_invalid_and_disagreeing_positions_are_not_normalized_into_evidence(self):
        for rows in ('<row r="0"><c/></row>', '<row r="30001"><c/></row>',
                     '<row r="1"><c r="CW1"/></row>', '<row r="1"><c r="A0"/></row>',
                     '<row r="1"><c r="$A$1"/></row>', '<row r="1"><c r="A2"/></row>',
                     '<row r="1"><c r=""/></row>', '<row r="1.0"><c/></row>',
                     '<row r="2"><c/></row><row r="1"><c/></row>',
                     '<row r="1"><c/></row><row r="1"><c r="C1"/></row>'):
            with self.subTest(rows=rows), self.assertRaises(materials.InputError):
                formats.validate('invalid.xlsx', position_workbook(rows))

    def test_material_preview_never_adopts_last_value_at_a_duplicate_source_address(self):
        rows = '<row r="3"><c r="C3"><v>10</v></c><c r="C3"><v>99</v></c></row>'
        raw = position_workbook(rows, raw=export_book(value=10), part='xl/worksheets/sheet2.xml')
        with self.assertRaisesRegex(materials.InputError, '利润表!C3'):
            materials.preview([('duplicate.xlsx', raw)], KEYS, allow_incomplete_company=True, capture_standard=True)

    def test_worksheet_structure_cannot_hide_positions_from_preflight(self):
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        original = position_workbook('<row r="1"><c r="A1"><v>10</v></c></row>')
        with ZipFile(BytesIO(original)) as archive:
            source = {name: archive.read(name) for name in archive.namelist()}
        for mode in ('wrong_root', 'relocated_wrong_root', 'relocated_non_xml_suffix', 'second_table', 'nested_row', 'stray_cell'):
            with self.subTest(mode=mode):
                parts = dict(source)
                root = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
                if mode in ('wrong_root', 'relocated_wrong_root', 'relocated_non_xml_suffix'):
                    root.tag = f'{{{ns}}}notWorksheet'
                elif mode == 'second_table':
                    ET.SubElement(root, f'{{{ns}}}sheetData')
                elif mode == 'nested_row':
                    extra = ET.SubElement(root, f'{{{ns}}}extension')
                    row = ET.SubElement(extra, f'{{{ns}}}row', r='1')
                    cell = ET.SubElement(row, f'{{{ns}}}c', r='A1')
                    ET.SubElement(cell, f'{{{ns}}}v').text = '99'
                else:
                    ET.SubElement(root, f'{{{ns}}}c', r='A1')
                parts['xl/worksheets/sheet1.xml'] = ET.tostring(root)
                if mode.startswith('relocated_'):
                    part = 'xl/custom.dat' if mode == 'relocated_non_xml_suffix' else 'xl/custom.xml'
                    parts[part] = parts.pop('xl/worksheets/sheet1.xml')
                    relations = ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
                    for rel in relations:
                        if rel.get('Type', '').endswith('/worksheet'):
                            rel.set('Target', '/' + part)
                    parts['xl/_rels/workbook.xml.rels'] = ET.tostring(relations)
                    parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace(
                        b'/xl/worksheets/sheet1.xml', ('/' + part).encode())
                with self.assertRaisesRegex(materials.InputError, '唯一单元格表' if mode == 'second_table' else '结构'):
                    formats.validate('ambiguous.xlsx', zip_bytes(list(parts.items())))

    def test_bounded_native_print_metadata_preserves_original_and_amounts(self):
        raw = printer_workbook()
        result = guard.check([('print.xlsx', raw)], expand=True)
        self.assertEqual(result[0][1], raw)
        self.assertEqual(result[0][2]['status'], 'passed')
        parsed = materials.preview([('print.xlsx', raw)], KEYS,
            allow_incomplete_company=True, capture_standard=True)[0]
        plain = materials.preview([('plain.xlsx', accounts())], KEYS,
            allow_incomplete_company=True, capture_standard=True)[0]
        self.assertEqual(parsed['error'], '')
        self.assertEqual(parsed['sha256'], sha256(raw).hexdigest())
        self.assertEqual(parsed['accounts'], plain['accounts'])
        self.assertEqual(parsed['rows'], plain['rows'])
        self.assertFalse(parsed['pages'])  # Print driver data never becomes model text.
        for change in ('private', 'absolute'):
            with self.subTest(change=change):
                formats.validate('print.xlsx', printer_workbook(change=change))

    def test_print_metadata_is_not_a_general_binary_or_relationship_exception(self):
        for change in ('missing_type', 'wrong_type', 'wrong_reference', 'external',
                       'unreferenced', 'bad_size', 'executable', 'outgoing', 'unknown_binary', 'macro',
                       'duplicate_id', 'relative_wrong', 'oversized'):
            with self.subTest(change=change), self.assertRaises(materials.InputError):
                formats.validate('print.xlsx', printer_workbook(change=change))

    def test_scripts_double_extensions_controls_and_renamed_containers_are_rejected(self):
        attacks = [('evil.ps1', b'Write-Host test'), ('evil.js.pdf', pdf()), ('evil.ｊｓ.pdf', pdf()),
                   ('evil\u202epdf.exe', b'MZ'), ('a.pdf:stream', pdf()),
                   ('../a.pdf', pdf()), ('a.pdf ', pdf()),
                   ('invoice.pdf', b'#!/bin/sh\necho test'), ('invoice.xlsx', b'console.log(1)'),
                   ('invoice.xml', b'<script>test</script>'), ('invoice.jpg', picture('PNG'))]
        for name, raw in attacks:
            with self.subTest(name=name), self.assertRaises(materials.InputError):
                formats.expand([(name, raw)])

    def test_real_supported_containers_have_content_proof(self):
        for name, raw in [('a.xlsx', accounts()), ('a.pdf', pdf()), ('a.xml', invoice_xml()),
                          ('a.png', picture()), ('a.jpeg', picture('JPEG')), ('a.tiff', picture('TIFF', True))]:
            with self.subTest(name=name):
                info = formats.expand([(name, raw)])[0][2]
                self.assertEqual(info['status'], 'passed')
                self.assertEqual(info['size'], len(raw))

    def test_zip_checks_every_member_and_rejects_ambiguous_paths(self):
        for entries in [[('a.xlsx', accounts()), ('evil.py', b'print(1)')],
                        [('nested.zip', zip_bytes([('a.pdf', pdf())]))],
                        [('../a.pdf', pdf())], [('A.pdf', pdf()), ('a.pdf', pdf())],
                        [('a.pdf', pdf()), ('a.pdf', pdf())], [('a.pdf', b'bad')]]:
            with self.subTest(entries=[n for n, _ in entries]), self.assertRaises(materials.InputError):
                formats.expand([('pack.zip', zip_bytes(entries))])
        result = formats.expand([('pack.zip', zip_bytes([('目录/a.xlsx', accounts()), ('b.pdf', pdf())]))])
        self.assertEqual([r[0] for r in result], ['pack.zip/目录/a.xlsx', 'pack.zip/b.pdf'])

    def test_zip_symlink_crc_and_expansion_bomb_are_rejected(self):
        out = BytesIO()
        info = ZipInfo('link.pdf')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with ZipFile(out, 'w') as archive:
            archive.writestr(info, b'target')
        with self.assertRaises(materials.InputError):
            formats.expand([('links.zip', out.getvalue())])
        out = BytesIO()
        raw = pdf()
        with ZipFile(out, 'w', ZIP_STORED) as archive:
            archive.writestr('a.pdf', raw)
        corrupted = out.getvalue().replace(b'%PDF-', b'%PDE-', 1)
        with self.assertRaises(materials.InputError):
            formats.expand([('crc.zip', corrupted)])
        with self.assertRaisesRegex(materials.InputError, '压缩比例'):
            formats.expand([('bomb.zip', zip_bytes([('bomb.pdf', b'0' * 500000)]))])

    def test_hidden_macro_external_relationship_and_executable_workbook_parts(self):
        for name, content in [('xl/vbaProject.bin', b'macro'), ('xl/embeddings/object.bin', b'object'),
                              ('xl/hidden.py', b'print(1)'),
                              ('xl/_rels/hidden.xml.rels', b'<Relationships><Relationship TargetMode="External" Target="https://example.test/" /></Relationships>')]:
            with self.subTest(part=name), self.assertRaises(materials.InputError):
                formats.validate('a.xlsx', zip_bytes(self.workbook_parts() + [(name, content)]))
        parts = self.workbook_parts()
        changed = [(n, b.replace(b'spreadsheetml.sheet.main+xml', b'spreadsheetml.sheet.macroEnabled.main+xml')) for n, b in parts]
        with self.assertRaises(materials.InputError):
            formats.validate('a.xlsx', zip_bytes(changed))

    @staticmethod
    def workbook_parts():
        with ZipFile(BytesIO(accounts())) as archive:
            return [(name, archive.read(name)) for name in archive.namelist()]

    def test_dde_and_external_functions_in_hidden_cells_are_rejected(self):
        for formula in ['=WEBSERVICE("https://example.test")', '=HYPERLINK("https://example.test")',
                        "=cmd|' /C echo test'!A0", '=[1]资产负债表!A40']:
            raw = workbook('隐藏数据', [['项目', '内容'], ['说明', formula]])
            with self.subTest(formula=formula):
                before = sha256(raw).hexdigest()
                with self.assertRaisesRegex(materials.InputError, '危险公式') as caught:
                    formats.validate('a.xlsx', raw)
                self.assertIn('隐藏数据!B2', str(caught.exception))
                self.assertNotIn(formula, str(caught.exception))
                self.assertNotIn('https://example.test', str(caught.exception))
                self.assertEqual(sha256(raw).hexdigest(), before)

    def test_rejected_formula_uses_package_location_when_sheet_identity_is_unproven(self):
        raw = workbook('隐藏数据', [['项目', '内容'], ['说明', '=[1]资产负债表!A40']])
        with ZipFile(BytesIO(raw)) as original:
            parts = [(item.filename, original.read(item)) for item in original.infolist()]
        for mode in ('missing_relations', 'missing_identifier', 'ambiguous_sheet'):
            modified = []
            for name, content in parts:
                if name == 'xl/_rels/workbook.xml.rels' and mode == 'missing_relations':
                    continue
                if name == 'xl/workbook.xml' and mode != 'missing_relations':
                    root = ET.fromstring(content)
                    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                    sheets = root.find('s:sheets', ns)
                    rejected_sheet = next(sheet for sheet in sheets if sheet.get('name') == '隐藏数据')
                    if mode == 'missing_identifier':
                        rejected_sheet.attrib.pop('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                    else:
                        duplicate = ET.fromstring(ET.tostring(rejected_sheet))
                        duplicate.set('name', '伪造别名')
                        sheets.append(duplicate)
                    content = ET.tostring(root)
                modified.append((name, content))
            with self.subTest(mode=mode), self.assertRaises(materials.InputError) as caught:
                formats.validate('a.xlsx', zip_bytes(modified))
            self.assertIn('xl/worksheets/sheet1.xml!B2', str(caught.exception))
            self.assertNotIn('伪造别名', str(caught.exception))

    def test_pdf_actions_attachments_and_encoded_names_are_rejected(self):
        for body in [b'<< /S /JavaScript /JS (test) >>', b'<< /S /J#61vaScript /J#53 (test) >>',
                     b'<< /S /Launch /F (test.exe) >>', b'<< /Type /EmbeddedFile >>',
                     b'<< /XFA (test) >>', b'<< /AA << /O 1 0 R >> >>']:
            with self.subTest(body=body), self.assertRaises(materials.InputError):
                formats.validate('a.pdf', pdf(body))
        with self.assertRaises(materials.InputError):
            formats.validate('a.pdf', pdf(catalog=b'/OpenAction 3 0 R'))
        formats.validate('ordinary.pdf', pdf(b'<< /Title (JavaScript revenue is ordinary text) >>'))
        with self.assertRaises(materials.InputError):
            formats.validate('a.pdf', pdf() + b'<script>test</script>')

    def test_utf16_entities_and_deep_xml_are_rejected(self):
        entity = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///not-read">]><x>&e;</x>'
        for encoding in ['utf-8', 'utf-16']:
            with self.subTest(encoding=encoding), self.assertRaisesRegex(materials.InputError, 'DTD'):
                formats.validate('a.xml', entity.encode(encoding))
        with self.assertRaisesRegex(materials.InputError, '嵌套'):
            formats.xml_root(('<x>' * 42 + '</x>' * 42).encode())

    def test_tail_payloads_cannot_be_appended_to_ordinary_containers(self):
        for name, raw in [('a.xlsx', accounts()), ('a.zip', zip_bytes([('a.pdf', pdf())])),
                          ('a.png', picture()), ('a.jpg', picture('JPEG'))]:
            with self.subTest(name=name), self.assertRaises(materials.InputError):
                formats.expand([(name, raw + b'<script>test</script>')])

    def test_csv_encoding_quotes_signed_amounts_and_formula_injection(self):
        text = '科目编码,科目名称,期初余额,本期借方,本期贷方,期末余额\r\n001,"收入,其他",0,-12.50,0,0\r\n'
        for encoding in ['utf-8-sig', 'utf-16', 'gb18030']:
            rows = formats.table_rows(text.encode(encoding), 'csv')
            self.assertEqual(rows[1][:2], ['001', '收入,其他'])
            self.assertEqual(rows[1][3], '-12.50')
        for cell in ['=1+1', '=cmd|test', '@SUM(A1)', '+HYPERLINK(test)', '-cmd|test']:
            with self.subTest(cell=cell), self.assertRaises(materials.InputError):
                formats.validate('a.csv', ('项目,金额\n收入,' + cell).encode())
        for raw in [b'a,b\n1,2,3', b'#!/bin/sh\necho test', b'a,b\n1,\x00']:
            with self.assertRaises(materials.InputError):
                formats.validate('a.csv', raw)

    def test_images_are_decoded_and_dimensions_and_animation_are_bounded(self):
        for raw in [b'not an image', picture()[:32]]:
            with self.assertRaises(materials.InputError):
                formats.validate('a.png', raw)
        bomb = bytearray(picture())
        bomb[16:24] = struct.pack('>II', 10000, 10000)
        bomb[29:33] = struct.pack('>I', zlib.crc32(bomb[12:29]) & 0xffffffff)
        with self.assertRaises(materials.InputError):
            formats.validate('bomb.png', bytes(bomb))
        out = BytesIO()
        with Image.new('RGB', (2, 2), 'white') as one, Image.new('RGB', (2, 2), 'black') as two:
            one.save(out, format='PNG', save_all=True, append_images=[two])
        with self.assertRaises(materials.InputError):
            formats.validate('animated.png', out.getvalue())

    def test_csv_tsv_import_preserves_original_and_standard_review_coordinates(self):
        for extension, separator in [('csv', ','), ('tsv', '\t')]:
            raw = (separator.join(['科目编码','科目名称','期初余额','本期借方','本期贷方','期末余额']) + '\n' +
                   separator.join(['6001','主营业务收入','0','0','100','0']) + '\n' +
                   separator.join(['6051','其他业务收入','0','0','0','0'])).encode()
            before = sha256(raw).hexdigest()
            doc = materials.preview([('科目余额表.' + extension, raw)], KEYS,
                                    allow_incomplete_company=True, capture_standard=True)[0]
            self.assertEqual(doc['error'], '')
            self.assertEqual(doc['sha256'], before)
            self.assertEqual(doc['kind'], extension)
            self.assertIn('科目余额表', doc['standard_tables'])
            dataset = materials.build_dataset([doc], selections([doc]), COMPANY, KEYS)
            self.assertEqual(dataset.get('营业收入'), 100)
            self.assertIn('.' + extension, dataset.source_of('营业收入'))

    def test_ambiguous_statement_headers_do_not_guess_business_table(self):
        raw = '项目,本期金额\n营业收入,100'.encode()
        unknown = materials.preview([('表格.csv', raw)], KEYS)[0]
        self.assertTrue(unknown['error'])
        known = materials.preview([('利润表.csv', raw)], KEYS)[0]
        self.assertFalse(known['error'])
        self.assertEqual(known['rows'][0]['name'], '利润表.营业收入')

    def test_tiff_pages_are_sent_separately_without_cross_page_images(self):
        raw = picture('TIFF', True)
        seen = []
        def transport(settings, messages, timeout):
            seen.extend(p['image_url']['url'] for p in messages[1]['content'] if p['type'] == 'image_url')
            return Extraction(company=COMPANY, rows=[])
        settings = replace(AISettings(enabled=True, api_key='synthetic'), vision=True)
        doc = materials.preview([('扫描.tiff', raw)], KEYS, AIExtractor(settings, {}, transport))[0]
        self.assertFalse(doc['error'])
        self.assertEqual(doc['page_count'], 2)
        self.assertEqual(doc['extraction']['attempts'][0]['image_pages'], [1, 2])
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(doc['sha256'], sha256(raw).hexdigest())

    def test_guard_real_worker_and_cache_do_not_accept_changed_bytes(self):
        raw = '项目,数值\n收入,0'.encode()
        result = guard.check([('worker-proof.csv', raw)], expand=True)
        self.assertEqual(result[0][1], raw)
        with patch.object(guard.subprocess, 'Popen', side_effect=AssertionError('unnecessary worker')):
            self.assertEqual(guard.check([('worker-proof.csv', raw)])[0][2], result[0][2])
        with self.assertRaises(materials.InputError):
            guard.check([('worker-proof.csv', b'print(1)')])

    def test_many_processing_workers_wait_for_bounded_format_slots(self):
        active = peak = 0
        lock = Lock()
        actual_check = guard._check
        def validate(files, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            try:
                return actual_check(files, **kwargs)
            finally:
                with lock:
                    active -= 1
        def run(index):
            return guard.check([(f'parallel-worker-{index}.csv', f'a,b\n{index},2'.encode())], wait=True, expand=True)
        with patch.object(guard, '_check', side_effect=validate), ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(run, range(16)))
        self.assertEqual(len(results), 16)
        self.assertEqual(peak, 4)
        self.assertEqual(len({r[0][0] for r in results}), 16)
        for index, result in enumerate(results):
            self.assertEqual(result[0][1], f'a,b\n{index},2'.encode())
            self.assertEqual(result[0][2]['status'], 'passed')

    def test_transparent_scan_is_rendered_on_white_paper(self):
        out = BytesIO()
        with Image.new('RGBA', (40, 40), (0, 0, 0, 0)) as image:
            image.save(out, format='PNG')
        seen = []
        def transport(settings, messages, timeout):
            uri = next(part['image_url']['url'] for part in messages[1]['content'] if part['type'] == 'image_url')
            with Image.open(BytesIO(base64.b64decode(uri.split(',', 1)[1]))) as rendered:
                seen.append(rendered.getpixel((20, 20)))
            return Extraction(company=COMPANY, rows=[])
        settings = AISettings(enabled=True, api_key='synthetic', vision=True)
        doc = materials.preview([('transparent.png', out.getvalue())], KEYS, AIExtractor(settings, {}, transport))[0]
        self.assertFalse(doc['error'])
        self.assertEqual(seen, [(255, 255, 255)])

    def test_cached_archives_cannot_bypass_combined_expansion_budget(self):
        for count, size in [(1, formats.MAX_TOTAL // 2 + 1), (12, 1)]:
            def cached(name, raw):
                return {'type':'zip', 'status':'passed', 'members':[
                    {'name':name+f'/{index}.csv', 'info':{'size':size}} for index in range(count)]}
            with patch.object(guard, 'metadata', side_effect=cached), \
                 patch.object(guard.subprocess, 'Popen', side_effect=AssertionError('cached validation')):
                with self.assertRaises(materials.InputError):
                    guard.check([('cached-one.zip', b'one'), ('cached-two.zip', b'two')])

    def test_guard_timeout_terminates_and_does_not_inherit_credentials(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired('validator', 20)
        process.poll.return_value = None
        close_job = Mock()
        with patch.dict(os.environ, {'TAXPEARLS_AI_API_KEY':'do-not-inherit', 'TAXPEARLS_DB':'private.db'}), \
             patch.object(guard.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(guard, '_windows_job', return_value=close_job), self.assertRaisesRegex(materials.InputError, '超时'):
            guard.check([('timeout-unique.csv', b'a,b\n1,2')], timeout=0.01)
        self.assertNotIn('TAXPEARLS_AI_API_KEY', launch.call_args.kwargs['env'])
        self.assertNotIn('TAXPEARLS_DB', launch.call_args.kwargs['env'])
        process.kill.assert_called_once()
        process.wait.assert_called_once()


class FormatUploadBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY':base64.b64encode(b'f'*32).decode(),
                                     'TAXPEARLS_MATERIAL_QUEUE_ENABLED':'0','TAXPEARLS_AI_ENABLED':'0'})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.tmp.name)/'formats.db')
        replacement = patch.object(module, 'store', self.store)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.actor = self.store.create_user('formats-admin','Formats-test-2026!','管理员','org_admin','formats')
        self.client = TestClient(module.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.client.post('/api/login', json={'username':self.actor['username'],'password':'Formats-test-2026!'})

    def counts(self):
        with self.store.connect() as db:
            return [db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                    ['material_batches','material_originals','material_revisions','material_jobs','audits']]

    def test_mixed_upload_and_queue_reject_before_originals_jobs_or_audits(self):
        for queued in ['0', '1']:
            with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_QUEUE_ENABLED':queued}):
                response = self.client.post('/api/enterprise/materials', files=[
                    ('files', ('good.xlsx', accounts())), ('files', ('script.pdf', b'#!/bin/sh\necho test'))])
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.counts(), [0]*5)
        with self.assertRaises(batches.MaterialStorageError):
            batches.create(self.store, self.actor, [('script.xlsx', b'console.log(1)')])
        self.assertEqual(self.counts(), [0]*5)

    def test_duplicate_cell_upload_queue_and_zip_reject_whole_batch_before_persistence(self):
        raw = position_workbook('<row r="3"><c r="C3"><v>10</v></c><c r="C3"><v>99</v></c></row>',
            raw=export_book(value=10), part='xl/worksheets/sheet2.xml')
        for queued in ('0', '1'):
            for name, content in (('duplicate.xlsx', raw), ('pack.zip', zip_bytes([('duplicate.xlsx', raw)]))):
                with self.subTest(queued=queued, name=name), patch.dict(os.environ, {'TAXPEARLS_MATERIAL_QUEUE_ENABLED':queued}):
                    response = self.client.post('/api/enterprise/materials', files=[
                        ('files', ('good.xlsx', accounts())), ('files', (name, content))])
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn('利润表!C3', response.text)
                    self.assertEqual(self.counts(), [0]*5)

    def test_failed_supplement_keeps_original_and_revision(self):
        response = self.client.post('/api/enterprise/materials', files={'files':('账.xlsx', accounts())})
        item = response.json()
        before = self.counts()
        rejected = self.client.post('/api/enterprise/materials/'+item['id']+'/supplement',
            data={'expected_revision':str(item['revision'])}, files={'files':('script.zip', zip_bytes([('evil.py',b'print(1)')]))})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.counts(), before)
        current = self.client.get('/api/enterprise/materials/'+item['id']).json()
        self.assertEqual(current['revision'], item['revision'])
        self.assertEqual(current['files'][0]['sha256'], item['files'][0]['sha256'])

    def test_csv_original_is_downloaded_byte_identically_and_corrections_are_derived(self):
        raw = '项目,本期金额\n营业收入,100'.encode()
        response = self.client.post('/api/enterprise/materials', files={'files':('利润表.csv', raw)})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        base = '/api/enterprise/materials/'+item['id']
        self.assertEqual(item['files'][0]['format_check']['type'], 'csv')
        self.assertEqual(item['documents'][0]['standard_fields'][0]['value'], '100')
        edited = self.client.post(base+'/analyze', json={'expected_revision':item['revision'],
            'selections':{item['documents'][0]['id']:{'standard_edits':{'利润表!B2':'200'}}}})
        self.assertEqual(edited.status_code, 200, edited.text)
        downloaded = self.client.get(base+'/originals/'+item['files'][0]['id'])
        self.assertEqual(downloaded.content, raw)
        self.assertEqual(downloaded.headers['x-content-type-options'], 'nosniff')

    def test_download_rechecks_revocation_after_format_validation(self):
        item = self.client.post('/api/enterprise/materials', files={'files':('账.xlsx', accounts())}).json()
        validate = batches.validate_uploads
        def revoke(files, **kwargs):
            proof = validate(files, **kwargs)
            with self.store.connect() as db:
                db.execute('UPDATE users SET active=0 WHERE id=?', (self.actor['id'],))
            return proof
        with patch.object(batches, 'validate_uploads', side_effect=revoke):
            response = self.client.get('/api/enterprise/materials/'+item['id']+'/originals/'+item['files'][0]['id'])
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn('content-disposition', response.headers)

    def test_download_rechecks_deletion_after_format_validation(self):
        item = self.client.post('/api/enterprise/materials', files={'files':('账.xlsx', accounts())}).json()
        validate = batches.validate_uploads
        def delete(files, **kwargs):
            proof = validate(files, **kwargs)
            batches.delete_original(self.store, self.actor, item['id'], item['files'][0]['id'], item['revision'])
            return proof
        with patch.object(batches, 'validate_uploads', side_effect=delete):
            response = self.client.get('/api/enterprise/materials/'+item['id']+'/originals/'+item['files'][0]['id'])
        self.assertEqual(response.status_code, 410, response.text)
        self.assertNotIn('content-disposition', response.headers)


class UploadAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_busy_admission_is_rejected_before_reading_and_released_on_disconnect(self):
        class Request:
            headers = Headers({'content-type':'multipart/form-data; boundary=test'})
            reads = 0
            async def stream(self):
                self.reads += 1
                raise RuntimeError('disconnected')
                yield b''
        request = Request()
        with patch.object(uploads, '_TOTAL', 4), patch.object(uploads, '_ACTIVE', {}):
            with self.assertRaises(HTTPException) as raised:
                async with uploads.multipart(request, material_scope='tenant'):
                    pass
            self.assertEqual(raised.exception.status_code, 429)
            self.assertEqual(request.reads, 0)
        with patch.object(uploads, '_TOTAL', 0), patch.object(uploads, '_ACTIVE', {}):
            with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                async with uploads.multipart(request, material_scope='tenant'):
                    pass
            self.assertEqual(uploads._TOTAL, 0)
            self.assertEqual(uploads._ACTIVE, {})

    async def test_org_admission_timeout_closes_unfinished_files(self):
        request = Mock(headers={'content-type':'multipart/form-data; boundary=test'})
        with patch.object(uploads, '_TOTAL', 2), patch.object(uploads, '_ACTIVE', {'tenant':2}):
            with self.assertRaises(HTTPException) as raised:
                async with uploads.multipart(request, material_scope='tenant'):
                    pass
            self.assertEqual(raised.exception.status_code, 429)
        file = BytesIO(b'partial')
        parser = Mock(_files_to_close_on_error=[file])
        async def slow():
            await asyncio.sleep(1)
        parser.parse = slow
        timeout = asyncio.timeout
        with patch.object(uploads, 'MemoryMultipart', return_value=parser), \
             patch.object(uploads.asyncio, 'timeout', side_effect=lambda seconds: timeout(0.001)), \
             patch.object(uploads, '_TOTAL', 0), patch.object(uploads, '_ACTIVE', {}):
            with self.assertRaises(HTTPException) as raised:
                async with uploads.multipart(request, material_scope='tenant'):
                    pass
            self.assertEqual(raised.exception.status_code, 408)
            self.assertTrue(file.closed)
            self.assertEqual(uploads._TOTAL, 0)
            self.assertEqual(uploads._ACTIVE, {})
