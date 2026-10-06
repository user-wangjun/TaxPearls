"""Constructed BIFF/CFB boundaries, not copies of customer/public originals."""
from hashlib import sha256
from io import BytesIO
import struct
import unittest

from src import material_formats, materials
from src.input_errors import InputError
from src.legacy_workbooks import read
from src.workbooks import open_workbook


def record(code, body=b''):
    return struct.pack('<HH', code, len(body)) + body


def label(row, col, value):
    raw = value.encode('cp1252')
    return record(0x204, struct.pack('<HHHH', row, col, 0, len(raw)) + raw)


def biff(*, extra=b'', global_extra=b'', version=0x500, sheet_type=0, value='literal', financial=False):
    global_records = (record(0x809, struct.pack('<HHHH', version, 5, 0, 0)) +
                      record(0x42, struct.pack('<H', 936 if financial else 1252)) + record(0xE0, b'\0' * 16) +
                      record(0x4D, b'\0' * 4200) + global_extra)
    name = b'Sheet1'
    bound_size = 4 + 7 + len(name)
    start = len(global_records) + bound_size + 4
    bound = record(0x85, struct.pack('<IBBB', start, 0, sheet_type, len(name)) + name)
    cells = label(0, 0, value) + record(0x203, struct.pack('<HHHd', 1, 1, 0, 12.5))
    if financial:
        cells = b''
        for row, values in enumerate([
                ['损益表'], ['单位名称：构造测试企业 2026年1月'],
                ['项目', '行次', '本年累计金额', '本月金额'],
                ['项目', '行次', '本年累计金额', '本月金额'],
                ['一、营业收入', 1, 900, 10], ['减：营业成本', 2, 800, 6]]):
            for col, item in enumerate(values):
                if isinstance(item, str):
                    text = item.encode('gbk')
                    cells += record(0x204, struct.pack('<HHHH', row, col, 0, len(text)) + text)
                else:
                    cells += record(0x203, struct.pack('<HHHd', row, col, 0, item))
    return (global_records + bound + record(0xA) +
            record(0x809, struct.pack('<HHHH', version, 0x10, 0, 0)) +
            record(0x200, struct.pack('<HHHHH', 0, 6 if financial else 3, 0, 4 if financial else 2, 0)) + cells +
            extra + record(0xA))


def compound(stream, *, extra_name=None):
    """Minimal valid CFB with regular (>=4096-byte) Book stream, no mini-FAT."""
    free, end, fat_sector = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    sectors = (len(stream) + 511) // 512
    header = bytearray(512)
    header[:8] = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    struct.pack_into('<HHHHH', header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0, 1, 0, 0, 4096, end, 0, end, 0)
    struct.pack_into('<109I', header, 76, sectors + 1, *([free] * 108))

    def entry(name, kind, child=free, right=free, start=end, size=0):
        raw = bytearray(128)
        text = (name + '\0').encode('utf-16-le')
        raw[:len(text)] = text
        struct.pack_into('<HBBIII', raw, 64, len(text), kind, 1, free, right, child)
        struct.pack_into('<IQ', raw, 116, start, size)
        return raw
    directory = (entry('Root Entry', 5, child=1) +
                 entry('Book', 2, right=2 if extra_name else free, start=1, size=len(stream)))
    if extra_name:
        directory += entry(extra_name, 2, start=1, size=len(stream))
    directory += bytes(512 - len(directory))
    fat = [end, *range(2, sectors + 1), end, fat_sector]
    fat += [free] * (128 - len(fat))
    return bytes(header + directory) + stream.ljust(sectors * 512, b'\0') + struct.pack('<128I', *fat)


class LegacyWorkbookTests(unittest.TestCase):
    def test_fixed_value_read_is_source_addressed_and_never_saved(self):
        raw = compound(biff(value='=not a formula'))
        original = sha256(raw).hexdigest()
        info = material_formats.validate('source.xls', raw)
        self.assertEqual(info['parser'], 'fixed-biff57-v1')
        book = open_workbook(raw)
        self.addCleanup(book.close)
        self.assertEqual(book['Sheet1']['B2'].value, 12.5)
        self.assertEqual(book['Sheet1']['A1'].data_type, 's')
        self.assertEqual(book['Sheet1']['A1'].value, '=not a formula')
        self.assertEqual(sha256(raw).hexdigest(), original)

    def test_formula_and_shared_array_records_reject_without_cache_fallback(self):
        for code in (0x6, 0x206, 0x406, 0x221, 0x4BC, 0x21):
            with self.subTest(code=code), self.assertRaisesRegex(InputError, '公式'):
                read(compound(biff(extra=record(code, b'\0' * 25))))

    def test_unknown_external_object_encrypted_and_macro_records_reject(self):
        for code in (0x23, 0x2F, 0x5D, 0x1AE, 0x1B8, 0xEB, 0xFFFF):
            with self.subTest(code=code), self.assertRaises(InputError):
                read(compound(biff(extra=record(code))))
        for version in (0x600, 0x400):
            with self.subTest(version=version), self.assertRaises(InputError):
                read(compound(biff(version=version)))
        for kind in (1, 2, 6):
            with self.subTest(kind=kind), self.assertRaises(InputError):
                read(compound(biff(sheet_type=kind)))

    def test_ole_parts_suffix_spoofing_and_truncation_reject(self):
        for raw in (b'PK\x03\x04fake', compound(biff(), extra_name='VBA'),
                    compound(biff())[:-512], compound(biff() + b'tail')):
            with self.subTest(size=len(raw)), self.assertRaises(InputError):
                material_formats.validate('bad.xls', raw)
        with self.assertRaises(InputError):
            material_formats.validate('renamed.xlsx', compound(biff()))

    def test_out_of_bounds_and_nonfinite_values_reject(self):
        for row, column, value in ((30000, 0, 1), (0, 100, 1), (2, 1, float('nan'))):
            raw = compound(biff(extra=record(0x203, struct.pack('<HHHd', row, column, 0, value))))
            with self.subTest(row=row, column=column), self.assertRaises(InputError):
                open_workbook(raw)

    def test_duplicate_cells_are_not_silently_overwritten(self):
        with self.assertRaisesRegex(InputError, '重复'):
            read(compound(biff(extra=label(0, 0, 'replacement'))))

    def test_dimension_col_limit_is_checked_before_decoder(self):
        raw = biff().replace(struct.pack('<HHHHH', 0, 3, 0, 2, 0),
                             struct.pack('<HHHHH', 0, 3, 0, 200, 0))
        with self.assertRaises(InputError):
            read(compound(raw))

    def test_understated_dimension_does_not_allow_large_sparse_allocation(self):
        raw = compound(biff(extra=record(0x203, struct.pack('<HHHd', 29999, 99, 0, 1))))
        with self.assertRaisesRegex(InputError, '展开范围'):
            read(raw)

    def test_changed_legacy_parser_or_dependency_requires_original_reanalysis(self):
        from copy import deepcopy
        from src import financial_import
        from tests.test_materials import KEYS
        doc = materials.preview([('constructed.xls', compound(biff(financial=True)))], KEYS,
                                allow_incomplete_company=True, capture_standard=True)[0]
        self.assertFalse(financial_import.reanalysis_required(doc))
        for name, section in (('legacy_workbooks.py', 'sources'), ('xlrd', 'dependencies'), ('olefile', 'dependencies')):
            changed = deepcopy(doc)
            changed['extraction']['local']['program'][section][name] = 'stale'
            self.assertTrue(financial_import.reanalysis_required(changed))

    def test_named_area_and_internal_link_without_formula_execution(self):
        formula = b'\x3B' + struct.pack('<h', -1) + bytes(8) + struct.pack('<HHHHBB', 0, 0, 0, 2, 0, 1)
        body = struct.pack('<HBBHHH4B', 0, 0, 4, len(formula), 0, 0, 0, 0, 0, 0) + b'Area' + formula
        links = record(0x16, struct.pack('<H', 1)) + record(0x17, b'\x06\x03Sheet1')
        book = read(compound(biff(global_extra=links + record(0x18, body))))
        self.addCleanup(book.release_resources)
        self.assertEqual(book.sheet_names(), ['Sheet1'])
        for invalid in (body.replace(b'\x3B', b'\x22', 1), b'\x08' + body[1:],
                        body[:-21] + formula[:1] + struct.pack('<h', 1) + formula[3:]):
            with self.subTest(body=invalid.hex()), self.assertRaises(InputError):
                read(compound(biff(global_extra=links + record(0x18, invalid))))
        with self.assertRaises(InputError):
            read(compound(biff(global_extra=links.replace(b'Sheet1', b'SheetX'))))

    def test_financial_alias_repeated_header_and_missing_unit_gate(self):
        from copy import deepcopy
        from tests.test_materials import KEYS
        doc = materials.preview([('constructed.xls', compound(biff(financial=True)))], KEYS,
                                allow_incomplete_company=True, capture_standard=True)[0]
        self.assertFalse(doc['error'])
        self.assertTrue(doc['import_mapping']['pending'])
        self.assertEqual(doc['import_mapping']['sheets'][0]['kind'], '利润表')
        self.assertEqual(doc['import_mapping']['sheets'][0]['unit_origin'], 'missing')
        selected = deepcopy(doc)
        materials._excel(compound(biff(financial=True)), selected, allow_incomplete_company=True,
                         capture_standard=True, keys=KEYS, import_options={'Sheet1': {'unit': '元'}})
        self.assertFalse(selected['import_mapping']['pending'])
        self.assertEqual(selected['import_mapping']['sheets'][0]['unit_origin'], 'user')
        income = next(row for row in selected['rows'] if row['name'] == '利润表.营业收入')
        from decimal import Decimal
        self.assertEqual(Decimal(income['value']), Decimal('10'))
        self.assertIn('Sheet1!D5', income['source'])
        self.assertEqual(selected['import_mapping']['sheets'][0]['columns'][0]['header_row'], 4)

    def test_whole_batch_rejection_does_not_return_good_prefix(self):
        from zipfile import ZipFile
        data = BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('good.xls', compound(biff()))
            archive.writestr('bad.xls', compound(biff(extra=record(6))))
        with self.assertRaises(InputError):
            materials.expand_uploads([('pack.zip', data.getvalue())])
