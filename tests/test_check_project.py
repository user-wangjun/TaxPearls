"""CI partitioning must retain coverage and turn hangs into useful failures."""
import io
from pathlib import Path
import subprocess
import sys
import unittest

from scripts import check_project


class CheckProjectTests(unittest.TestCase):
    def samples(self):
        suites = []
        for name, count in [('large', 9), ('small', 2), ('medium', 5), ('tiny', 1)]:
            case = type(name, (unittest.TestCase,), {
                '__module__': name,
                **{f'test_{i}': lambda self: None for i in range(count)},
            })
            suites.append(unittest.defaultTestLoader.loadTestsFromTestCase(case))
        return unittest.TestSuite(suites)

    def test_shards_cover_each_test_once_and_keep_modules_together(self):
        selections = [list(check_project.shard_suite(self.samples(), i, 4)) for i in range(4)]
        ids = [test.id() for group in selections for test in group]
        self.assertEqual(len(ids), 17)
        self.assertEqual(len(set(ids)), 17)
        self.assertEqual(sorted(map(len, selections)), [1, 2, 5, 9])
        for name in ['large', 'small', 'medium', 'tiny']:
            self.assertEqual(sum(any(t.id().startswith(name + '.') for t in group)
                                 for group in selections), 1)
        again = [t.id() for t in check_project.shard_suite(self.samples(), 0, 4)]
        self.assertEqual(again, [t.id() for t in selections[0]])

    def test_invalid_shard_is_rejected(self):
        for index, total in [(-1, 4), (4, 4), (0, 0)]:
            with self.subTest(index=index, total=total), self.assertRaises(ValueError):
                check_project.shard_suite(self.samples(), index, total)

    def test_hung_test_exits_with_stack_instead_of_waiting_for_job_limit(self):
        root = Path(__file__).resolve().parents[1]
        program = 'import time, unittest\nfrom scripts.check_project import TimedResult\nclass Hung(unittest.TestCase):\n    def test_blocked(self):\n        time.sleep(30)\nTimedResult.timeout = 1\nunittest.TextTestRunner(resultclass=TimedResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(Hung))\n'
        result = subprocess.run([sys.executable, '-c', program], cwd=root,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('test_blocked', result.stderr)
        self.assertIn('Timeout', result.stderr)

    def test_completed_test_cancels_deadline(self):
        check_project.TimedResult.timeout = 1
        try:
            result = unittest.TextTestRunner(stream=io.StringIO(),
                resultclass=check_project.TimedResult).run(self.samples())
            self.assertTrue(result.wasSuccessful())
            self.assertEqual(result.testsRun, 17)
        finally:
            check_project.TimedResult.timeout = 0
