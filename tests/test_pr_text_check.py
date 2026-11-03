import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / 'kit/skills/pr-text'
sys.path.insert(0, str(KIT))

from check import problems  # noqa: E402

SCRIPT = KIT / 'check.py'
GOOD = 'fix: Do the thing\n\nWhy it matters.\n\n- One change\n\nNo migrations.\n'


def body(*lines):
    return 'fix: Do the thing\n\n' + '\n'.join(lines) + '\n\nNo migrations.\n'


class ProblemsTest(unittest.TestCase):
    def assertFlags(self, text, *fragments):
        found = '\n'.join(problems(text))

        for fragment in fragments:
            self.assertIn(fragment, found)

    def test_a_well_formed_body_passes(self):
        self.assertEqual(problems(GOOD), [])

    def test_the_title_needs_a_type_without_scope_and_a_capital(self):
        for title in ('Do the thing', 'fix(web): Do the thing', 'fix: do the thing', 'wip: Do the thing'):
            with self.subTest(title):
                self.assertFlags(GOOD.replace('fix: Do the thing', title), 'line 1: the title')

        for title in ('feat!: Drop the old route', 'chore: 2 lockfile dedupes'):
            with self.subTest(title):
                self.assertEqual(problems(GOOD.replace('fix: Do the thing', title)), [])

    def test_line_two_is_blank_and_the_body_is_not_empty(self):
        self.assertFlags('fix: Do the thing\nWhy it matters.\n\nNo migrations.\n', 'line 2: must be blank')
        self.assertFlags('fix: Do the thing\n\n', 'line 3: the body is empty')
        self.assertFlags('fix: Do the thing\n', 'line 3: the body is empty')

    def test_lines_wrap_at_72_unless_they_have_no_space(self):
        self.assertEqual(problems(body('x' * 72)), [])
        self.assertEqual(problems(body('https://example.com/' + 'a' * 80)), [])
        self.assertFlags(body('word ' * 15), 'line 3: 74 columns')

    def test_markdown_at_a_line_start_is_flagged(self):
        for line, what in (('## Summary', 'a Markdown heading'), ('~~~', 'a code fence'), ('**Bold** text', 'bold'),
                           ('* item', 'a "* " bullet'), ('- [ ] todo', 'a checkbox'), ('- [x] done', 'a checkbox')):
            with self.subTest(line):
                self.assertFlags(body(line), f'line 3: {what}')

    def test_an_issue_reference_is_not_a_heading(self):
        self.assertEqual(problems(body('Follows #348, which moved it.', '#348 moved it first.')), [])

    def test_a_backtick_anywhere_is_flagged(self):
        self.assertFlags(body('Calls the `helper` now'), 'line 3: a backtick')
        self.assertFlags(GOOD.replace('the thing', 'the `thing`'), 'line 1: a backtick')

    def test_a_deep_indent_is_only_a_nested_bullet_continuation(self):
        self.assertEqual(problems(body('- Routes:', '  - a nested bullet that wraps onto', '    the next line',
                                       '    and one more')), [])
        self.assertFlags(body('Output:', '    exit 0'), 'line 4: indented 4 or more spaces')
        self.assertFlags(body('- a bullet that wraps', '    too deep'), 'line 4: indented 4 or more spaces')

    def test_the_last_paragraph_is_the_migrations_line(self):
        self.assertEqual(problems('fix: Do the thing\n\nWhy.\n\nBehavior changes, no migrations: none.\n'), [])
        self.assertEqual(problems('fix: Do the thing\n\nWhy.\n\nNo schema or data migration.\n'), [])
        self.assertFlags('fix: Do the thing\n\nNo migrations.\n\nFollow-ups: none.\n', 'line 5: the last paragraph')


class CommandTest(unittest.TestCase):
    def run_script(self, text):
        with tempfile.NamedTemporaryFile('w', suffix='.md') as handle:
            handle.write(text)
            handle.flush()

            return subprocess.run([sys.executable, str(SCRIPT), handle.name], capture_output=True, text=True)

    def test_a_good_file_prints_nothing_and_exits_0(self):
        result = self.run_script(GOOD)
        self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_a_bad_file_prints_each_problem_and_exits_1(self):
        result = self.run_script(body('Calls the `helper` now'))
        self.assertEqual((result.returncode, result.stdout), (1, 'line 3: a backtick; the body is plain text\n'))


if __name__ == '__main__':
    unittest.main()
