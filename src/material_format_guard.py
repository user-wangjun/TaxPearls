"""Bound and terminate native container validation before business persistence."""
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from threading import Lock, BoundedSemaphore
import unicodedata

from .input_errors import InputError
from . import material_formats as formats

_CACHE = OrderedDict()
_LOCK = Lock()
_SLOTS = BoundedSemaphore(4)


def metadata(name, raw):
    with _LOCK:
        return deepcopy(_CACHE.get((name, sha256(raw).digest())))


def _remember(name, raw, info):
    with _LOCK:
        key = (name, sha256(raw).digest())
        _CACHE[key] = deepcopy(info)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 256:
            _CACHE.popitem(last=False)


def _windows_job(process):
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
                    ('Flags', wintypes.DWORD), ('Min', ctypes.c_size_t), ('Max', ctypes.c_size_t),
                    ('Active', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
                    ('Priority', wintypes.DWORD), ('Scheduling', wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', ctypes.c_size_t),
                    ('JobMemory', ctypes.c_size_t), ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.Basic.Flags = 0x2000 | 0x100  # kill-on-close + process memory limit
    limits.ProcessMemory = 768*1024*1024
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        if job:
            kernel.CloseHandle(job)
        raise InputError('文件检查进程无法建立资源限制，未接收材料。')
    return lambda: kernel.CloseHandle(job)


def check(files, *, expand=False, timeout=20, wait=False):
    if not isinstance(files, list) or not 1 <= len(files) <= 20:
        raise InputError('每次最多 20 个文件。')
    seen, total = set(), 0
    for name, raw in files:
        formats.filename(name)
        folded = unicodedata.normalize('NFKC', name).casefold()
        if folded in seen:
            raise InputError('同批文件名重复或存在歧义。')
        seen.add(folded)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= formats.MAX_FILE:
            raise InputError('单个材料须非空且不超过 10MB。')
        total += len(raw)
    if total > formats.MAX_TOTAL:
        raise InputError('材料合计不超过 50MB。')
    cached = [metadata(name, raw) for name, raw in files]
    if all(cached) and (not expand or not any(info['type'] == 'zip' for info in cached)):
        output = []
        for (name, raw), info in zip(files, cached):
            if info['type'] == 'zip':
                output.extend((member['name'], None, member['info']) for member in info['members'])
            else:
                output.append((name, raw if expand else None, info))
        if len(output) > 20:
            raise InputError('解压后最多 20 个文件。')
        if sum(info.get('size', 0) for _, _, info in output) > formats.MAX_TOTAL:
            raise InputError('解压后材料合计不超过 50MB。')
        return output
    admitted = _SLOTS.acquire(timeout=timeout) if wait else _SLOTS.acquire(blocking=False)
    if not admitted:
        raise InputError('文件检查名额已满，请稍后重试。')
    try:
        return _check(files, expand=expand, timeout=timeout)
    finally:
        _SLOTS.release()


def _check(files, *, expand, timeout):
    payload = json.dumps({'files': [[name, base64.b64encode(raw).decode()] for name,raw in files],
                          'expand': expand}, ensure_ascii=False).encode()
    environment = {k:v for k,v in os.environ.items() if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG'}}
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    try:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'src.material_format_worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parents[1]), env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except OSError:
        raise InputError('无法启动材料检查进程，未接收文件。') from None
    close_job = None
    try:
        if os.name=='nt':
            close_job = _windows_job(process)
        try:
            output, _ = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise InputError('材料格式检查超时，未接收文件；请拆分或导出简化材料。') from None
        if process.returncode != 0 or len(output)>72*1024*1024:
            raise InputError('材料格式检查超过资源预算或异常退出，未接收文件。')
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise InputError('材料格式检查未完成，未接收文件。') from None
        if result.get('ok') is not True:
            raise InputError(result.get('message', '材料格式不符合要求。'))
        checked = [(name, base64.b64decode(raw) if expand else None, info) for name,raw,info in result['files']]
        originals = dict(files)
        for name, raw, info in checked:
            original = raw if expand else originals.get(name)
            if original is not None:
                _remember(name, original, info)
        for name, raw in files:
            if name.lower().endswith('.zip'):
                _remember(name, raw, {'version': formats.VERSION, 'type': 'zip', 'status': 'passed',
                    'members_checked': True, 'members': [{'name': member, 'info': info}
                        for member, _, info in checked if member.startswith(name + '/')]})
        return checked
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if close_job:
            close_job()
