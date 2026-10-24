"""Run repository checks with isolated databases and no external credentials."""
from pathlib import Path
import ast
import argparse
import faulthandler
import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def shard_suite(suite, index, total):
    """Balance whole modules so class/module fixtures never cross runners."""
    if total < 1 or not 0 <= index < total:
        raise ValueError('shard must be INDEX/TOTAL with 0 <= INDEX < TOTAL')
    modules = {}

    def collect(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                collect(test)
            else:
                modules.setdefault(test.id().rsplit('.', 2)[0], []).append(test)
    collect(suite)
    buckets = [[] for _ in range(total)]
    for name, tests in sorted(modules.items(), key=lambda item: (-len(item[1]), item[0])):
        bucket = min(range(total), key=lambda number: (len(buckets[number]), number))
        buckets[bucket].extend(tests)
    return unittest.TestSuite(buckets[index])


class TimedResult(unittest.TextTestResult):
    timeout = 0

    def startTest(self, test):
        super().startTest(test)
        if self.timeout:
            print('Running: ' + test.id(), flush=True)
            faulthandler.dump_traceback_later(self.timeout, exit=True)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('patterns', nargs='*')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--static-only', action='store_true')
    modes.add_argument('--tests-only', action='store_true')
    parser.add_argument('--shard', default='0/1', metavar='INDEX/TOTAL')
    parser.add_argument('--test-timeout', type=int, default=0, metavar='SECONDS')
    options = parser.parse_args()
    try:
        index, total = map(int, options.shard.split('/'))
        if total < 1 or not 0 <= index < total or options.test_timeout < 0:
            raise ValueError
    except ValueError:
        parser.error('invalid shard or test timeout')
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    def fingerprint():
        paths = [ROOT / "main.py", ROOT / "docs" / "企业政策适用目录.yaml", *[p for d in ("src", "webapp", "scripts", "tests", "rules", "templates") for p in (ROOT / d).rglob("*") if p.suffix in {".py", ".js", ".html", ".css", ".yaml"}]]
        return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    before = fingerprint()
    if not options.tests_only:
        for path in [ROOT / 'main.py', *[p for d in ('src', 'webapp', 'scripts', 'tests')
                                        for p in (ROOT / d).rglob('*.py')]]:
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
        subprocess.run([sys.executable, '-m', 'ruff', 'check', 'main.py', 'src', 'webapp', 'scripts', 'tests'], check=True)
        subprocess.run(['node', 'scripts/check_frontend.cjs'], check=True)
        subprocess.run(['node', 'scripts/check_ai_key_frontend.cjs'], check=True)
        subprocess.run(['node', 'scripts/check_enterprise_frontend.cjs'], check=True)
        subprocess.run(['node', 'scripts/check_core_frontend.cjs'], check=True)
    with tempfile.TemporaryDirectory(prefix='taxpearls-check-') as directory:
        os.environ.update(TAXPEARLS_DB=str(Path(directory) / 'bootstrap.db'),
                          TAXPEARLS_ENVIRONMENT='local', TAXPEARLS_INSTANCE_ID='local',
                          TAXPEARLS_AI_ENABLED='0', TAXPEARLS_NOTIFICATION_EMAIL_ENABLED='0',
                          TAXPEARLS_AI_API_KEY='', TAXPEARLS_AI_BACKUP_API_KEYS='', TAXPEARLS_RESEND_API_KEY='',
                          TAXPEARLS_BACKUP_KEY='', TAXPEARLS_MATERIAL_KEY='',
                          # patch.dict 类装饰器只包 test 方法不包 setUp：setUp 阶段 lifespan 的
                          # material_worker.start() 会以默认 enabled=True 抢先 BEGIN IMMEDIATE，
                          # 与用例首条写事务在 Windows 上并发初始化 WAL，以 SQLITE_READONLY 面目
                          # 偶发失败（storage.connect 注释记录的同一竞态）。队列用例自行显式开 '1'。
                          TAXPEARLS_MATERIAL_QUEUE_ENABLED='0',
                          TAXPEARLS_FIELD_KEY=base64.b64encode(os.urandom(32)).decode())
        # Multiple explicit patterns let related migrations share one isolated
        # process and the same source-stability/contract checks.
        suite = unittest.TestSuite(unittest.defaultTestLoader.discover('tests', pattern=pattern)
                                   for pattern in ([] if options.static_only else dict.fromkeys(options.patterns or ['test_*.py'])))
        success = True
        if not options.static_only:
            suite = shard_suite(suite, index, total)
            TimedResult.timeout = options.test_timeout
            print(f'Shard {index}/{total}: {suite.countTestCases()} tests', flush=True)
            result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
            success = result.wasSuccessful()
    after = fingerprint()
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    if changed:
        print("检查期间源码发生变化，请复验：" + ", ".join(changed))
    return 0 if success and not changed else 1


if __name__ == '__main__':
    raise SystemExit(main())
