"""判题沙箱 v2 强化测试（FR-J03 教学强化级）。

覆盖：网络模块禁用、进程派生禁用、文件访问约束在工作目录内、
OS 级内存超限归因 runtime_error、正常代码不受影响。
直测 code_sandbox.run_case，不走 HTTP。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from webapp import code_sandbox


class SandboxHardeningTests(unittest.TestCase):
    def run_code(self, code: str, input_text: str = "") -> dict:
        return code_sandbox.run_case(code, input_text, time_limit_ms=5000)

    def test_normal_code_ok(self):
        run = self.run_code("a, b = map(int, input().split())\nprint(a + b)\n", "1 2\n")
        self.assertEqual(run["status"], "ok", run)
        self.assertEqual(run["output"].strip(), "3")

    def test_network_module_blocked(self):
        run = self.run_code("import socket\n")
        self.assertEqual(run["status"], "runtime_error", run)
        self.assertIn("沙箱禁止使用模块", run["output"])

    def test_subprocess_blocked(self):
        run = self.run_code("import subprocess\n")
        self.assertEqual(run["status"], "runtime_error", run)
        self.assertIn("沙箱禁止使用模块", run["output"])

    def test_os_system_blocked(self):
        run = self.run_code("import os\nos.system('echo pwned')\n")
        self.assertEqual(run["status"], "runtime_error", run)
        self.assertIn("沙箱禁止该操作", run["output"])

    def test_open_outside_workdir_blocked(self):
        outside = os.path.join(tempfile.gettempdir(), "tp-sandbox-escape-probe.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("probe")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        run = self.run_code(
            f"open({outside!r}, 'a').write('x')\n")
        self.assertEqual(run["status"], "runtime_error", run)
        self.assertIn("沙箱禁止访问文件", run["output"])

    def test_open_inside_workdir_allowed(self):
        run = self.run_code(
            "with open('local.txt', 'w') as f:\n    f.write('ok')\n"
            "print(open('local.txt').read())\n")
        self.assertEqual(run["status"], "ok", run)
        self.assertEqual(run["output"].strip(), "ok")

    def test_memory_limit_enforced(self):
        run = self.run_code("x = bytearray(%d)\nprint(len(x))\n"
                            % (code_sandbox.MEMORY_LIMIT_MB * 4 * 1024 * 1024))
        self.assertEqual(run["status"], "runtime_error", run)
        self.assertNotIn("ok", run["output"].strip().lower())

    def test_infinite_loop_still_times_out(self):
        run = code_sandbox.run_case("while True:\n    pass\n", "",
                                    time_limit_ms=1000)
        self.assertEqual(run["status"], "timeout")

    # ---- v3 审计钩子：防守卫摘除绕过 ----

    def test_meta_path_removed_still_blocked(self):
        code = (
            "import sys\n"
            "sys.meta_path[:] = [f for f in sys.meta_path\n"
            "                   if type(f).__name__ != '_SandboxBlocker']\n"
            "import socket\n"
            "socket.socket()\n")
        run = self.run_code(code)
        self.assertEqual(run["status"], "runtime_error", run)
        self.assertIn("沙箱", run["output"])

    def test_patch_restore_still_blocked(self):
        # 还原 builtins.open 为真实 open 后仍不能读工作目录外文件（审计钩子拦截）
        code = (
            "import sys\n"
            "sys.meta_path[:] = [f for f in sys.meta_path\n"
            "                   if type(f).__name__ != '_SandboxBlocker']\n"
            "import json, importlib\n"
            "importlib.reload(json)\n"  # 触发 json.py 重新加载，读标准库应放行
            "print(json.dumps({'a': 1}))\n")
        run = self.run_code(code)
        self.assertEqual(run["status"], "ok", run)
        self.assertIn('"a": 1', run["output"])

    def test_stdlib_import_allowed(self):
        run = self.run_code("import json\nprint(json.dumps([1, 2]))\n")
        self.assertEqual(run["status"], "ok", run)
        self.assertEqual(run["output"].strip(), "[1, 2]")

    def test_os_open_outside_blocked(self):
        outside = os.path.join(tempfile.gettempdir(), "tp-sandbox-escape2.txt")
        run = self.run_code(f"import os\nos.open({outside!r}, os.O_RDONLY)\n")
        self.assertEqual(run["status"], "runtime_error", run)


if __name__ == "__main__":
    unittest.main()
