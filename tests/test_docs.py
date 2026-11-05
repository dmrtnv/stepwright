"""The docs hold together: links and anchors resolve, every INSTALL step has a command block and a Check, INSTALL's
commands paste cleanly, the step numbers the code cites exist, and README's cost numbers match the code.

Run: python3 -m unittest tests/test_docs.py
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import adapter  # noqa: E402
import core  # noqa: E402

LINKED_DOCS = ('README.md', 'INSTALL.md', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md')
NO_TABLE_DOCS = ('INSTALL.md', 'SECURITY.md', 'CONTRIBUTING.md')
STEP_CITERS = ('core.py', 'README.md', 'CHANGELOG.md')
INSTALL_SECTIONS_WITH_STEPS = ('Update', 'Manual setup (without init)', 'Uninstall')
FENCE = re.compile(r'^\s*```')
LINK = re.compile(r'\]\(([^)\s]+)\)')
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
NUMBERED = re.compile(r'^\d+(\.\d+)*\.?\s')
SUB_STEP = re.compile(r'^\d+\.\d+\s')
PROMPT = re.compile(r'^\s*\$ ')
CD = re.compile(r'(^|[;&|(])\s*cd\s')
QUOTED = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
COMMAND_V = re.compile(r'\$\(command -v ')
XARGS = re.compile(r'\bxargs\b(?! -d \'\\n\')')
STEP_REFERENCE = re.compile(r'INSTALL steps? (\d+)(?:-(\d+))?')
NAMED_REFERENCE = re.compile(r'INSTALL(?::| ) ?(What you need|Update)\b')


def read(name):
    return (ROOT / name).read_text()


def prose_lines(text):
    """The lines outside fenced code blocks."""
    inside = False

    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
        elif not inside:
            yield line


def bash_blocks(text):
    blocks, current = [], None

    for line in text.splitlines():
        if current is None and line.strip() == '```bash':
            current = []
        elif current is not None and FENCE.match(line):
            blocks.append('\n'.join(current))
            current = None
        elif current is not None:
            current.append(line)

    return blocks


def slug(heading):
    return re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-')


def headings(text):
    """(level, title) for each heading outside fenced code."""
    return [(len(match[1]), match[2]) for match in map(HEADING.match, prose_lines(text)) if match]


def sections(text):
    """(title, body) per heading, the body running to the next heading of any level."""
    found, title, body, inside = [], None, [], False

    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
        match = None if inside else HEADING.match(line)

        if match:
            if title is not None:
                found.append((title, '\n'.join(body)))
            title, body = match[2], []
        else:
            body.append(line)

    if title is not None:
        found.append((title, '\n'.join(body)))

    return found


def install_steps():
    """INSTALL's top-level step numbers: `## 5. Configure` gives 5."""
    return {int(title.split('.')[0]) for level, title in headings(read('INSTALL.md'))
            if level == 2 and re.match(r'^\d+\.\s', title)}


class Links(unittest.TestCase):
    def test_every_relative_link_and_anchor_resolves(self):
        problems = []

        for name in LINKED_DOCS:
            for line in prose_lines(read(name)):
                for target in LINK.findall(line):
                    if re.match(r'^[a-z]+:', target):
                        continue
                    path, _, anchor = target.partition('#')
                    linked = name if not path else path

                    if not (ROOT / linked).exists():
                        problems.append(f'{name}: {target} (no file {linked})')
                    elif anchor and anchor not in {slug(title) for _, title in headings(read(linked))}:
                        problems.append(f'{name}: {target} (no heading #{anchor} in {linked})')

        self.assertEqual(problems, [])


class InstallSteps(unittest.TestCase):
    def test_every_step_has_a_command_block_and_a_check(self):
        text = read('INSTALL.md')
        titles = [title for _, title in headings(text)]
        parents = {title.split('.')[0] for title in titles if SUB_STEP.match(title)}
        steps = [(title, body) for title, body in sections(text)
                 if title in INSTALL_SECTIONS_WITH_STEPS
                 or (NUMBERED.match(title) and not (re.match(r'^\d+\.\s', title) and title.split('.')[0] in parents))]
        self.assertGreaterEqual(len(steps), 9)
        missing = [title for title, body in steps if '```bash' not in body or '**Check:**' not in body]

        self.assertEqual(missing, [])

    def test_what_you_need_has_a_check(self):
        body = dict(sections(read('INSTALL.md')))['What you need']

        self.assertIn('**Check:**', body)

    def test_commands_paste_cleanly(self):
        problems = []

        for block in bash_blocks(read('INSTALL.md')):
            for line in block.splitlines():
                if PROMPT.match(line):
                    problems.append(f'prompt: {line}')
                if CD.search(line):
                    problems.append(f'cd: {line}')
                if any('~' in quoted for quoted in QUOTED.findall(line)):
                    problems.append(f'~ inside quotes: {line}')
                if COMMAND_V.search(line):
                    problems.append(f'$(command -v) prints an alias as text, use type -P: {line}')
                if XARGS.search(line):
                    problems.append(f"xargs without -d '\\n' splits names on spaces and quotes: {line}")

        self.assertEqual(problems, [])

    def test_no_tables(self):
        for name in NO_TABLE_DOCS:
            with self.subTest(name):
                self.assertEqual([line for line in read(name).splitlines() if line.startswith('|')], [])


class StepReferences(unittest.TestCase):
    def test_cited_step_numbers_exist(self):
        steps = install_steps()
        missing = []

        for name in STEP_CITERS:
            for first, last in STEP_REFERENCE.findall(read(name)):
                for number in range(int(first), int(last or first) + 1):
                    if number not in steps:
                        missing.append(f'{name}: INSTALL step {number}')

        self.assertEqual(missing, [])

    def test_cited_section_names_exist(self):
        titles = {title for _, title in headings(read('INSTALL.md'))}
        cited = set(NAMED_REFERENCE.findall(read('core.py')))

        self.assertEqual(cited, {'What you need', 'Update'})
        self.assertLessEqual(cited, titles)


class Cost(unittest.TestCase):
    def setUp(self):
        self.readme = read('README.md')

    def test_each_stage_cap_is_listed(self):
        for stage, caps in core.STAGES.items():
            with self.subTest(stage):
                self.assertIn(f"\n- {stage}: {caps['hours']} h, ${caps['budget']}\n", self.readme)

    def test_normal_path_total(self):
        def normal_path(key):
            return (core.STAGES['plan'][key] + core.STAGES['implement'][key]
                    + core.MAX_ROUNDS * (core.STAGES['review'][key] + core.STAGES['respond'][key]))

        self.assertIn(f"up to {core.MAX_ROUNDS} review and respond rounds: {normal_path('hours')} h and "
                      f"${normal_path('budget')} in caps", self.readme)

    def test_extra_sessions_and_admission(self):
        admission = adapter.DEFAULT_ADMISSION
        sentences = (f'A red gate resumes the stage, up to {core.GATE_RESUMES} times.',
                     f'A red CI run gets {core.CI_FIXES} fixer sessions, then more while the failure moves, up to '
                     f'{core.CI_FIXES_MAX} in all.',
                     f"weekly usage is at {admission['weeklyMax']} % or more, or your five-hour usage is at "
                     f"{admission['fiveHourMax']} % or more",
                     f'up to {core.NTFY_CHARACTERS:,} characters per message',
                     f"the tail of a failed CI job's log (up to {core.CI_LOG_CHARACTERS:,} characters), when a CI fixer "
                     'session runs')
        flat = ' '.join(self.readme.split())

        for sentence in sentences:
            with self.subTest(sentence):
                self.assertIn(sentence, flat)

    def test_models(self):
        self.assertEqual({caps['model'] for caps in core.STAGES.values()}, {'claude-opus-5-5'})
        self.assertIn('Opus 5.5 (`claude-opus-5-5`)', self.readme)
        self.assertEqual(core.FALLBACK_MODEL, 'claude-sonnet-5-5')
        self.assertIn('falls back to Sonnet 5.5', self.readme)


if __name__ == '__main__':
    unittest.main()
