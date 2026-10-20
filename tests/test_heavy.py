"""heavy tests: the default lock lives in the state dir, HEAVY_LOCK overrides it.

Run: python3 -m unittest discover tests
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HEAVY = Path(__file__).resolve().parent.parent / 'heavy'


class HeavyTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='heavy-test-'))
        self.addCleanup(shutil.rmtree, self.home, True)

    def heavy(self, **extra):
        environment = dict(PATH=os.environ['PATH'], HOME=str(self.home), HEAVY_MIN_FREE_GB='0',
                           HEAVY_WAIT_SECONDS='5', **extra)

        return subprocess.run([str(HEAVY), 'true'], env=environment, capture_output=True, text=True, timeout=30)

    def test_the_default_lock_is_in_the_state_dir(self):
        result = self.heavy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.local/state/pipeline/heavy.lock').is_file())
        self.assertFalse((self.home / '.claude').exists())

    def test_heavy_lock_overrides_the_default(self):
        result = self.heavy(HEAVY_LOCK=str(self.home / 'x.lock'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'x.lock').is_file())
        self.assertFalse((self.home / '.local').exists())


if __name__ == '__main__':
    unittest.main()
