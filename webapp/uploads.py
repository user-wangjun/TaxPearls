"""Authenticated callers parse bounded multipart bodies without disk spooling."""
from contextlib import asynccontextmanager
import asyncio
from threading import Lock

from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from src.workbooks import MAX_FILE

_LOCK = Lock()
_ACTIVE = {}
_TOTAL = 0


class MemoryMultipart(MultiPartParser):
    def __init__(self, *args, file_limit=MAX_FILE, **kwargs):
        self.file_limit = file_limit
        self.spool_max_size = file_limit + 1
        super().__init__(*args, **kwargs)

    def on_part_begin(self):
        self.part_bytes = 0
        super().on_part_begin()

    def on_part_data(self, data, start, end):
        self.part_bytes += end - start
        if self._current_part.file is not None and self.part_bytes > self.file_limit:
            raise MultiPartException(f"单个文件超过 {self.file_limit / (1024 * 1024):g}MB 大小限制。")
        super().on_part_data(data, start, end)


async def bounded_stream(request, limit):
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise MultiPartException("请求体过大。")
        yield chunk


@asynccontextmanager
async def multipart(request, *, file_limit=MAX_FILE, total_limit=None, max_files=1, max_fields=1, material_scope=None):
    global _TOTAL
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(422, "请以文件表单上传材料。")
    form = None
    parser = None
    admitted = False
    if material_scope is not None:
        with _LOCK:
            if _TOTAL >= 4 or _ACTIVE.get(material_scope, 0) >= 2:
                raise HTTPException(429, '上传名额已满，请稍后重试。')
            _TOTAL += 1
            _ACTIVE[material_scope] = _ACTIVE.get(material_scope, 0) + 1
            admitted = True
    try:
        parser = MemoryMultipart(request.headers, bounded_stream(request, total_limit or file_limit + 65536),
                                 file_limit=file_limit, max_files=max_files, max_fields=max_fields,
                                 max_part_size=65536)
        async with asyncio.timeout(60):
            form = await parser.parse()
        yield form
    except TimeoutError:
        raise HTTPException(408, '上传超过 60 秒，请检查网络或拆分材料。') from None
    except MultiPartException as exc:
        raise HTTPException(422, str(exc)) from None
    finally:
        try:
            if form is not None:
                await form.close()
            elif parser is not None:
                for file in parser._files_to_close_on_error:
                    file.close()
        finally:
            if admitted:
                with _LOCK:
                    _TOTAL -= 1
                    _ACTIVE[material_scope] -= 1
                    if not _ACTIVE[material_scope]:
                        del _ACTIVE[material_scope]


def single_file(form):
    files = [(key, value) for key, value in form.multi_items() if isinstance(value, UploadFile)]
    if len(files) != 1 or files[0][0] != "file":
        raise HTTPException(422, "请提供一个 file 文件字段。")
    return files[0][1]
