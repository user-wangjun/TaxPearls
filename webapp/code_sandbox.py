"""教学演示级代码运行沙箱（FR-J03/J04 v1，Windows 本地）。

边界如实声明（醒目）：
- 采用受限子进程方案：python -I 隔离模式（忽略环境变量与用户 site-packages）、
  每次运行独立临时目录、硬超时强杀、输出截断。
- **不是容器级安全边界**：不做文件系统重定向、不隔离网络、不限制内存峰值。
  学生代码可读写自身临时目录外的可访问文件——生产判题须容器沙箱专项
  （FR-J03 验收：逃逸/资源超限/恶意输入防护与记录，单独安全验收）。
- 输出比对：去行尾空白与末尾空行后逐行比对（浮点容差、多解判定属判题
  引擎增强，v1 未实现，如实标注）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

SANDBOX_NOTICE = ("教学演示级沙箱：python -I 隔离模式 + 独立临时目录 + 硬超时 + 输出截断；"
                  "非容器级安全边界，生产判题须容器沙箱专项（FR-J03）。")
MAX_OUTPUT_BYTES = 64 * 1024  # 单用例输出截断（防内存炸弹回读）

STATUS_TIMEOUT = "timeout"
STATUS_RUNTIME_ERROR = "runtime_error"
STATUS_WRONG = "wrong_answer"


def _normalize(text: str) -> list[str]:
    """比对归一：去行尾空白、去末尾空行。"""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def run_case(code: str, input_text: str, *, time_limit_ms: int) -> dict:
    """在受限子进程中运行一份代码面对单组输入，返回运行结果。

    返回：{status: ok|timeout|runtime_error, runtime_ms, output(截断)}。
    本函数不比对期望输出（比对属判题逻辑），只负责受控执行。"""
    workdir = tempfile.mkdtemp(prefix="tp-sandbox-")
    try:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code],
                input=input_text, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=max(1, int(time_limit_ms / 1000) + 1),
                cwd=workdir,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
            )
            runtime_ms = int((time.monotonic() - start) * 1000)
            output = (proc.stdout or "")[:MAX_OUTPUT_BYTES]
            if proc.returncode != 0:
                return {"status": STATUS_RUNTIME_ERROR, "runtime_ms": runtime_ms,
                        "output": output}
            return {"status": "ok", "runtime_ms": runtime_ms, "output": output}
        except subprocess.TimeoutExpired:
            runtime_ms = int((time.monotonic() - start) * 1000)
            return {"status": STATUS_TIMEOUT, "runtime_ms": runtime_ms, "output": ""}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def judge_output(actual: str, expected_output: str) -> bool:
    """v1 比对：归一化后逐行一致即通过（浮点容差/多解判定未实现，如实标注）。"""
    return _normalize(actual) == _normalize(expected_output)
