"""Nothing personal ships: no real home path or email, none of the owner's private names, no stale host path, in any
file of the repo.

Private names are kept as sha256 prefixes so this file doesn't publish them: a word, or two adjacent ones joined by
`-`, whose hash is in PRIVATE_NAMES is a hit. Add one with:
  python3 -c "import hashlib; print(hashlib.sha256(b'<lowercase name>').hexdigest()[:16])"

Run: python3 -m unittest tests/test_public.py
"""
import hashlib
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = 'tests/test_public.py'
HOME_PATH = re.compile(r'/home/(?!(?:agent|runner|someone)\b)[\w.-]+')
EMAIL = re.compile(r'\b[\w.+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b')
# Fixtures and trailers: example domains, GitHub noreply, the Claude Code trailer, systemd unit instances.
EMAIL_DOMAIN = re.compile(r'(^|\.)(example\.(com|org)|users\.noreply\.github\.com|anthropic\.com)$|^\d+\.service$')
STALE_PATH = re.compile(r'\.local/lib/sbx-host')  # sbx-host lives in this repo now
PRIVATE_NAMES = frozenset({'62625a191dbe48b3', '166d41d13f7dbdfe', '9f4e4b90a3b0ffbc', '6a6a1aefb5dce285',
                           '735c9cc6e6e71ae3', '6b65d25f9d51298d'})
WORD = re.compile(r'[a-z0-9]+')


def digest(word):
    return hashlib.sha256(word.encode()).hexdigest()[:16]


def files():
    listed = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout

    return sorted(name for name in listed.split('\0') if name and (ROOT / name).is_file())


def private_names(text, names=PRIVATE_NAMES):
    words = WORD.findall(text.lower())
    candidates = set(words) | {f'{a}-{b}' for a, b in zip(words, words[1:])}

    return sorted(word for word in candidates if digest(word) in names)


def hits(text):
    found = HOME_PATH.findall(text) + STALE_PATH.findall(text) + private_names(text)

    return found + [match.group(0) for match in EMAIL.finditer(text) if not EMAIL_DOMAIN.search(match.group(1))]


class PublicTest(unittest.TestCase):
    def test_no_file_names_personal_text(self):
        leaks = {}

        for name in files():
            if name == SELF:  # its fixtures are fake personal text
                continue

            try:
                found = hits((ROOT / name).read_text())
            except UnicodeDecodeError:
                continue

            if found:
                leaks[name] = sorted(set(found))

        self.assertEqual(leaks, {})

    def test_the_patterns_catch_what_they_are_for(self):
        for text in ('/home/alice/x', 'mail bob@gmail.com', '~/.local/lib/sbx-host/base-image'):
            self.assertTrue(hits(text), text)

        for text in ('/home/agent/x', 'x@example.com', '1+a@users.noreply.github.com', 'ci-runner@1.service',
                     'noreply@anthropic.com', 'react@18.2.0', 'pl-shop-a-01'):
            self.assertFalse(hits(text), text)

    def test_a_private_name_is_caught_as_a_word_or_a_dashed_pair(self):
        names = {digest('acme'), digest('box-thing')}

        self.assertEqual(private_names('see pl-ACME-a-01', names), ['acme'])
        self.assertEqual(private_names('~/box-thing/x', names), ['box-thing'])
        self.assertEqual(private_names('acmes, boxthing', names), [])


if __name__ == '__main__':
    unittest.main()
