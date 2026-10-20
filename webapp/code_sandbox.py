"""教学强化级代码运行沙箱（FR-J03/J04 v3，Windows 本地）。

在 v2（python -I + 独立临时目录 + 硬超时 + 输出截断 + Job Object 内存/进程数 +
import 守卫 + 文件路径检查）基础上强化：

- **审计钩子（sys.addaudithook，v3 新增）**：CPython 3.12+ 的 "import"、
  "open"、"subprocess.Popen"、"os.system/exec/fork/spawn"、"socket.__new__/
  connect/bind"、"ctypes.dlopen/call_function"、"os.remove/rename" 等事件在
  解释器最底层触发，用户代码摘除 sys.meta_path、还原被补丁的 open/os 函数
  也绕不过。这是进程内方案的天花板。
- **受限账号执行（v3 新增，可选）**：设置 TAXPEARLS_SANDBOX_USER /
  TAXPEARLS_SANDBOX_PASSWORD（可选 TAXPEARLS_SANDBOX_DOMAIN，默认本机"."）
  后，判题子进程以该专用低权限账号运行（CreateProcessWithLogonW），文件与
  网络约束升为 OS 权限；启动失败按判题失败处理（fail-closed）。账号创建见
  scripts/setup_sandbox_user.py（需管理员运行一次；未配置时回退普通路径，
  功能可用但边界同 v2）。
- **输出比对（v3.1 新增）**：exact（默认归一逐字符）| float（数值 token 按相对
  容差比对，浮点容差已实现）| any_of（多解判定：多候选任一命中即通过），
  见 compare_output；匹配模式与容差按用例配置（code_test_case.match_mode）。

边界如实声明（醒目）：**进程内守卫 + OS 资源限制的教学强化级，不是容器级
安全边界**。守卫目标是把"无意越界"拦下、把"有意越界"留痕；生产判题仍须
容器沙箱专项（FR-J03 验收：逃逸/资源超限/恶意输入防护与记录，单独安全
验收），推荐部署 go-judge 或容器池后由 run_case 切换实现。
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SANDBOX_NOTICE = ("教学强化级沙箱：python -I 隔离模式 + 独立临时目录 + 硬超时 + 输出截断；"
                  "网络/危险模块禁用、审计钩子（import/open/socket/子进程/ctypes 底层拦截，"
                  "防守卫摘除绕过）、OS 级内存与 CPU 限制、文件访问约束在题目工作目录内、"
                  "Job Object 进程树回收。仍非容器级安全边界，生产判题须容器沙箱专项（FR-J03）。")

MAX_OUTPUT_BYTES = 64 * 1024  # 单用例输出截断（防内存炸弹回读）
MEMORY_LIMIT_MB = 256         # 单次运行内存上限
MAX_PROCESSES = 4             # Windows Job Object 活动进程数上限

_SANDBOX_USER = os.environ.get("TAXPEARLS_SANDBOX_USER", "").strip()
_SANDBOX_PASSWORD = os.environ.get("TAXPEARLS_SANDBOX_PASSWORD", "")
_SANDBOX_DOMAIN = os.environ.get("TAXPEARLS_SANDBOX_DOMAIN", ".").strip() or "."
RESTRICTED_ACCOUNT_ENABLED = bool(_SANDBOX_USER and _SANDBOX_PASSWORD)

STATUS_TIMEOUT = "timeout"
STATUS_RUNTIME_ERROR = "runtime_error"
STATUS_WRONG = "wrong_answer"

# 进程内守卫：注入到学生代码之前执行（-c 单串，-I 模式无 site 干扰）。
_GUARD_SOURCE = r'''
import builtins as _b
import importlib.abc as _abc
import io as _io
import os as _os
import sys as _sys

_BLOCKED_ROOTS = {
    # 网络
    "socket", "ssl", "http", "urllib", "urllib2", "urllib3", "ftplib",
    "smtplib", "telnetlib", "xmlrpc", "asyncio", "selectors", "asynchat",
    "socketserver",
    # 进程/原生逃逸面
    "subprocess", "ctypes", "_ctypes", "multiprocessing", "_multiprocessing",
    "_winapi", "pty", "signal", "mmap", "curses", "winreg", "distutils",
    # 文件面收窄
    "shutil", "send2trash", "pathlib",
}


class _SandboxBlocker(_abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _BLOCKED_ROOTS:
            raise ImportError("沙箱禁止使用模块: %s" % fullname)


_sys.meta_path.insert(0, _SandboxBlocker())
for _name in list(_sys.modules):
    if _name.split(".")[0] in _BLOCKED_ROOTS:
        del _sys.modules[_name]

_WORKDIR = _os.path.realpath(_os.getcwd())
_PREFIX = _os.path.realpath(_sys.prefix)
_BASE_PREFIX = _os.path.realpath(_sys.base_prefix or _sys.prefix)
_EXEDIR = _os.path.realpath(_os.path.dirname(_sys.executable))


def _inside_workdir(path):
    try:
        real = _os.path.realpath(_os.fspath(path))
    except (TypeError, ValueError):
        return False
    return real == _WORKDIR or real.startswith(_WORKDIR + _os.sep)


def _open_allowed(path):
    if _inside_workdir(path):
        return True
    try:
        real = _os.path.realpath(_os.fspath(path))
    except (TypeError, ValueError):
        return False
    # 允许解释器自身读取标准库（用户 import 合法模块需要；venv 的
    # 标准库在 base_prefix 的基础解释器目录里，也要放行）
    for _root in (_PREFIX, _BASE_PREFIX, _EXEDIR):
        if real == _root or real.startswith(_root + _os.sep):
            return True
    return False


def _guard_open(file, *args, **kwargs):
    if not _open_allowed(file):
        raise PermissionError("沙箱禁止访问文件: %r" % (file,))
    return _REAL_OPEN(file, *args, **kwargs)


_REAL_OPEN = _b.open
_b.open = _guard_open
_io.open = _guard_open


def _guard_os_open(path, *args, **kwargs):
    if not _open_allowed(path):
        raise PermissionError("沙箱禁止访问文件: %r" % (path,))
    return _REAL_OS_OPEN(path, *args, **kwargs)


_REAL_OS_OPEN = _os.open
_os.open = _guard_os_open


def _deny(*args, **kwargs):
    raise PermissionError("沙箱禁止该操作（进程派生/系统调用）。")


for _fname in ("system", "popen", "execv", "execve", "execvp", "execvpe",
               "spawnv", "spawnve", "spawnvp", "spawnvpe", "fork", "forkpty"):
    if hasattr(_os, _fname):
        setattr(_os, _fname, _deny)


def _guard_remove(path, *args, **kwargs):
    if not _inside_workdir(path):
        raise PermissionError("沙箱仅允许删除题目工作目录内文件: %r" % (path,))
    return _os_remove(path, *args, **kwargs)


def _guard_rename(src, dst, *args, **kwargs):
    if not (_inside_workdir(src) and _inside_workdir(dst)):
        raise PermissionError("沙箱仅允许在题目工作目录内改名: %r -> %r" % (src, dst))
    return _os_rename(src, dst, *args, **kwargs)


_os_remove, _os_rename = _os.remove, _os.rename
_os.remove = _guard_remove
_os.unlink = _guard_remove
_os.rename = _guard_rename
_os.rmdir = _guard_remove
_os.removedirs = _guard_remove

# ---- 审计钩子（v3）：解释器最底层事件，meta_path/函数补丁被摘除也仍然生效 ----

_AUDIT_DENY_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.fork", "os.spawn",
    "os.posix_spawn", "os.posix_spawnp", "socket.__new__", "socket.connect",
    "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function",
    "winreg.PyHKEY", "pty.spawn",
}


def _sandbox_audit(event, args):
    if event == "import":
        # CPython 3.12+ 触发；被禁模块在 import 层再拦一次
        try:
            root = args[0].split(".")[0] if args else ""
        except Exception:
            root = ""
        if root in _BLOCKED_ROOTS:
            raise ImportError("沙箱禁止使用模块: %s" % args[0])
        return
    if event == "open":
        path = args[0] if args else None
        if path is not None and not _open_allowed(path):
            raise PermissionError("沙箱禁止访问文件: %r" % (path,))
        return
    if event in ("os.remove", "os.rmdir"):
        path = args[0] if args else None
        if path is not None and not _inside_workdir(path):
            raise PermissionError("沙箱仅允许删除题目工作目录内文件: %r" % (path,))
        return
    if event == "os.rename":
        if len(args) >= 2 and not (_inside_workdir(args[0]) and _inside_workdir(args[1])):
            raise PermissionError("沙箱仅允许在题目工作目录内改名")
        return
    if event in _AUDIT_DENY_EVENTS:
        raise PermissionError("沙箱禁止该操作（审计事件: %s）" % event)


_sys.addaudithook(_sandbox_audit)
'''

# Windows Job Object 常量与结构（ctypes，无第三方依赖）
_JOB_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObject:
    """Windows Job Object：内存上限 + 进程数上限 + 句柄关闭即整树回收。"""

    def __init__(self) -> None:
        self._handle = None
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return  # Job 不可用时降级，不阻断判题
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                _JOB_LIMIT_PROCESS_MEMORY | _JOB_LIMIT_ACTIVE_PROCESS
                | _JOB_LIMIT_KILL_ON_JOB_CLOSE)
            info.ProcessMemoryLimit = MEMORY_LIMIT_MB * 1024 * 1024
            info.BasicLimitInformation.ActiveProcessLimit = MAX_PROCESSES
            if not k32.SetInformationJobObject(
                    handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(handle)
                return
            self._handle = handle

    def assign(self, process_handle) -> None:
        if self._handle:
            ctypes.windll.kernel32.AssignProcessToJobObject(self._handle, process_handle)

    def close(self) -> None:
        if self._handle:
            # TerminateJobObject 显式收树；KILL_ON_JOB_CLOSE 兜底
            ctypes.windll.kernel32.TerminateJobObject(self._handle, 1)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


def _posix_preexec(time_limit_s: int):  # pragma: no cover - Windows 不执行
    def apply() -> None:
        import resource
        limit = MEMORY_LIMIT_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CPU, (time_limit_s + 2, time_limit_s + 2))
    return apply


def _normalize(text: str) -> list[str]:
    """比对归一：去行尾空白、去末尾空行。"""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


# ---- 受限账号执行（Windows，可选，env 门控）----

_STARTF_USESTDHANDLES = 0x00000100
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_SUSPENDED = 0x00000004
_WAIT_TIMEOUT_CODE = 0x00000102


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32), ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32),
    ]


def _grant_workdir(workdir: str) -> None:
    """把题目工作目录授权给受限账号（icacls，含子项继承）。"""
    subprocess.run(
        ["icacls", workdir, "/grant", f"{_SANDBOX_USER}:(OI)(CI)F"],
        capture_output=True, timeout=10, check=False)


def _run_restricted(cmd: list[str], workdir: str, input_path: str,
                    output_path: str, time_limit_s: int) -> int:
    """以受限账号运行判题命令，返回 returncode；超时抛 TimeoutError。

    stdin/stdout/stderr 重定向到工作目录内的文件；进程挂起创建 → 指派
    Job Object → 恢复执行；启动失败抛 OSError（fail-closed）。"""
    import msvcrt
    advapi32 = ctypes.windll.advapi32
    k32 = ctypes.windll.kernel32
    in_fd = os.open(input_path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_INHERIT)
    out_fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_BINARY", 0) | os.O_INHERIT)
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = _STARTF_USESTDHANDLES
    si.hStdInput = msvcrt.get_osfhandle(in_fd)
    si.hStdOutput = msvcrt.get_osfhandle(out_fd)
    si.hStdError = msvcrt.get_osfhandle(out_fd)
    pi = _PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
    job = _JobObject()
    try:
        ok = advapi32.CreateProcessWithLogonW(
            _SANDBOX_USER, _SANDBOX_DOMAIN, _SANDBOX_PASSWORD,
            1,  # LOGON_WITH_PROFILE
            None, cmdline,
            _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT,
            None, workdir, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise OSError("受限账号启动失败（错误码 %d）" % ctypes.GetLastError())
        job.assign(pi.hProcess)
        k32.ResumeThread(pi.hThread)
        wait = k32.WaitForSingleObject(pi.hProcess, time_limit_s * 1000)
        if wait == _WAIT_TIMEOUT_CODE:
            k32.TerminateProcess(pi.hProcess, 1)
            k32.WaitForSingleObject(pi.hProcess, 2000)
            raise TimeoutError()
        code = ctypes.c_uint32(1)
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return code.value
    finally:
        job.close()
        if pi.hProcess:
            k32.CloseHandle(pi.hProcess)
        if pi.hThread:
            k32.CloseHandle(pi.hThread)
        os.close(in_fd)
        os.close(out_fd)


def run_case(code: str, input_text: str, *, time_limit_ms: int) -> dict:
    """在受限子进程中运行一份代码面对单组输入，返回运行结果。

    返回：{status: ok|timeout|runtime_error, runtime_ms, output(截断)}。
    本函数不比对期望输出（比对属判题逻辑），只负责受控执行。"""
    workdir = tempfile.mkdtemp(prefix="tp-sandbox-")
    try:
        start = time.monotonic()
        time_limit_s = max(1, int(time_limit_ms / 1000) + 1)
        if RESTRICTED_ACCOUNT_ENABLED:
            return _run_case_restricted(code, input_text, workdir, start, time_limit_s)
        return _run_case_direct(code, input_text, workdir, start, time_limit_s)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_case_direct(code: str, input_text: str, workdir: str,
                     start: float, time_limit_s: int) -> dict:
    """直跑路径：当前账号 + Job Object + 审计钩子守卫。"""
    job = _JobObject()
    try:
        preexec = _posix_preexec(time_limit_s) if os.name == "posix" else None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-X", "utf8", "-c",
                 _GUARD_SOURCE + "\n" + code],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                cwd=workdir,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
                preexec_fn=preexec,
            )
        except OSError as exc:
            return {"status": STATUS_RUNTIME_ERROR,
                    "runtime_ms": int((time.monotonic() - start) * 1000),
                    "output": f"沙箱启动失败: {exc}"[:MAX_OUTPUT_BYTES]}
        if sys.platform == "win32":
            # _handle 是 Windows 独有属性；POSIX 走 rlimit，无 Job Object
            job.assign(int(proc._handle))  # 紧随创建指派 Job（窗口极小，教学级可接受）
        try:
            output, _ = proc.communicate(input=input_text, timeout=time_limit_s)
            runtime_ms = int((time.monotonic() - start) * 1000)
            output = (output or "")[:MAX_OUTPUT_BYTES]
            if proc.returncode != 0:
                return {"status": STATUS_RUNTIME_ERROR, "runtime_ms": runtime_ms,
                        "output": output}
            return {"status": "ok", "runtime_ms": runtime_ms, "output": output}
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=2)
            except (subprocess.TimeoutExpired, ValueError):
                pass
            runtime_ms = int((time.monotonic() - start) * 1000)
            return {"status": STATUS_TIMEOUT, "runtime_ms": runtime_ms, "output": ""}
    finally:
        job.close()


def _run_case_restricted(code: str, input_text: str, workdir: str,
                         start: float, time_limit_s: int) -> dict:
    """受限账号路径：CreateProcessWithLogonW + icacls 授权 + Job Object。"""
    cmd = [sys.executable, "-I", "-X", "utf8", "-c", _GUARD_SOURCE + "\n" + code]
    input_path = os.path.join(workdir, "stdin.txt")
    output_path = os.path.join(workdir, "stdout.txt")
    with open(input_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(input_text)
    try:
        _grant_workdir(workdir)
        returncode = _run_restricted(cmd, workdir, input_path, output_path,
                                     time_limit_s)
    except TimeoutError:
        return {"status": STATUS_TIMEOUT,
                "runtime_ms": int((time.monotonic() - start) * 1000), "output": ""}
    except OSError as exc:
        return {"status": STATUS_RUNTIME_ERROR,
                "runtime_ms": int((time.monotonic() - start) * 1000),
                "output": f"沙箱受限账号执行失败: {exc}"[:MAX_OUTPUT_BYTES]}
    runtime_ms = int((time.monotonic() - start) * 1000)
    try:
        with open(output_path, "r", encoding="utf-8", errors="replace") as fh:
            output = fh.read(MAX_OUTPUT_BYTES)
    except OSError:
        output = ""
    if returncode != 0:
        return {"status": STATUS_RUNTIME_ERROR, "runtime_ms": runtime_ms,
                "output": output}
    return {"status": "ok", "runtime_ms": runtime_ms, "output": output}


def judge_output(actual: str, expected_output: str) -> bool:
    """缺省 exact 比对（旧签名兼容）：归一化后逐行一致即通过。"""
    return compare_output(actual, expected_output, mode="exact")


# ---- 输出比对（v3.1）：exact | float（浮点容差）| any_of（多解判定）----

MATCH_MODES = ("exact", "float", "any_of")
DEFAULT_FLOAT_TOLERANCE = 1e-6
_NUMBER_RE = re.compile(
    r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")  # 12 / 3.14 / 1e-6 / .5 等


def _line_matches(actual_line: str, expected_line: str, *, mode: str,
                  tolerance: float) -> bool:
    """单行比对：exact 逐 token 一致；float 数值 token 按容差、其余逐字符。

    行首尾空白已在 _normalize 去除；行内按空白切 token，token 数须一致；
    两个 token 都是数字（含符号/科学计数法）时按相对容差判等
    （|a-e| <= tolerance*max(1,|e|)，容差锚定在期望值上），否则逐字符一致。"""
    if mode == "exact" or actual_line == expected_line:
        return actual_line == expected_line
    a_tokens = actual_line.split()
    e_tokens = expected_line.split()
    if len(a_tokens) != len(e_tokens):
        return False
    for a, e in zip(a_tokens, e_tokens):
        if a == e:
            continue
        if _NUMBER_RE.fullmatch(a) and _NUMBER_RE.fullmatch(e):
            av, ev = float(a), float(e)
            if abs(av - ev) <= tolerance * max(1.0, abs(ev)):
                continue
        return False
    return True


def compare_output(actual: str, expected_output: str, *, mode: str = "exact",
                   tolerance: float | None = None,
                   alternatives: list[str] | None = None) -> bool:
    """按用例匹配模式比对输出（v3.1）。

    - exact：归一化后逐行逐字符一致（默认）；
    - float：逐行比对，数值 token 误差 <= tolerance*max(1,|期望|) 即相等
      （缺省容差 1e-6；非数值部分仍须逐字符一致）；
    - any_of：expected_output 与 alternatives 中任一候选匹配即通过
      （候选同样按 mode/tolerance 比对，可与 float 组合实现"多解+容差"）。
    mode 非法或容差越界（0,0.5] 之外一律不通过（fail-closed）。"""
    if mode not in MATCH_MODES:
        return False
    tol = DEFAULT_FLOAT_TOLERANCE if tolerance is None else float(tolerance)
    if not (0 < tol <= 0.5):
        return False
    candidates = [expected_output]
    if mode == "any_of":
        candidates.extend(alternatives or [])
    actual_lines = _normalize(actual)
    for candidate in candidates:
        expected_lines = _normalize(candidate)
        if len(actual_lines) != len(expected_lines):
            continue
        if all(_line_matches(a, e, mode=mode, tolerance=tol)
               for a, e in zip(actual_lines, expected_lines)):
            return True
    return False
