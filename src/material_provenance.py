"""Allowlisted extraction program fingerprints, never environment or secrets."""
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

PDF_VERSION = 'pdf-material-v1'


def pdf_reanalysis_required(document):
    """Old PDF candidates cannot approve a new parser without an original rescan."""
    if document.get('kind') != 'pdf':
        return False
    if document.get('pdf_parser_version') != PDF_VERSION:
        return True
    extraction = document.get('extraction')
    record = extraction.get('local') if isinstance(extraction, dict) else None
    expected_status = 'awaiting_selection' if document.get('pdf_selection', {}).get('pending') else 'succeeded'
    if not isinstance(record, dict) or record.get('status') != expected_status:
        return True
    saved_program = record.get('program')
    if not isinstance(saved_program, dict):
        return True
    sources, dependencies = saved_program.get('sources'), saved_program.get('dependencies')
    if not isinstance(sources, dict) or not isinstance(dependencies, dict):
        return True
    current = program('local')
    return any(not current['sources'].get(name) or sources.get(name) != current['sources'][name]
               for name in ('materials.py', 'loader.py', 'financial_import.py', 'config.py', 'periods.py',
                            'material_provenance.py', 'material_security.py', 'material_formats.py',
                            'material_format_guard.py', 'material_format_worker.py')) or any(
               not current['dependencies'].get(name) or dependencies.get(name) != current['dependencies'][name]
               for name in ('pdfplumber', 'pdfminer.six'))


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def program(kind):
    # Hash actual deployed sources, not a branch name or an uncommitted HEAD.
    # Missing source/package information remains unknown, never inferred.
    files = ('ai_extraction.py', 'ai_transport.py', 'settings.py', 'periods.py') if kind == 'ai' else (
        'materials.py', 'loader.py', 'material_review.py', 'financial_import.py', 'workbooks.py',
        'config.py', 'periods.py', 'models.py', 'related_graph.py', 'legacy_workbooks.py')
    packages = ('pydantic', 'pypdfium2', 'Pillow') if kind == 'ai' else (
        'openpyxl', 'pdfplumber', 'pdfminer.six', 'Pillow', 'xlrd', 'olefile')
    sources, dependencies = {}, {}
    for name in (*files, 'material_provenance.py', 'material_security.py', 'material_formats.py',
                        'material_format_guard.py', 'material_format_worker.py'):
        try:
            sources[name] = sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        except OSError:
            sources[name] = None
    for name in packages:
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    return {'contract': 'extraction-provenance-v1', 'sources': sources, 'dependencies': dependencies}
