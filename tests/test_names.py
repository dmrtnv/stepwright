"""Every name a module reads is defined: a typo or a leftover name otherwise raises NameError only when that line runs,
which for sandbox/gate.py means on a real box with sbx.

Run: python3 -m unittest tests/test_names.py
"""
import builtins
import subprocess
import symtable
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPLICIT = set(dir(builtins)) | {'__file__', '__name__', '__doc__'}


def undefined(path):
    top = symtable.symtable(path.read_text(), str(path), 'exec')
    defined = {symbol.get_name() for symbol in top.get_symbols() if symbol.is_assigned() or symbol.is_imported()}
    found = []

    def walk(table):
        for symbol in table.get_symbols():
            name = symbol.get_name()

            if symbol.is_referenced() and symbol.is_global() and name not in defined | IMPLICIT:
                found.append(f'{path.relative_to(ROOT)}: {table.get_name()} reads {name}')

        for child in table.get_children():
            walk(child)

    walk(top)
    return found


class NamesTest(unittest.TestCase):
    def test_every_module_defines_the_globals_it_reads(self):
        files = subprocess.run(['git', 'ls-files', '*.py'], cwd=ROOT, capture_output=True, text=True,
                               check=True).stdout.split()
        self.assertTrue(files)
        self.assertEqual([hit for name in files for hit in undefined(ROOT / name)], [])


if __name__ == '__main__':
    unittest.main()
