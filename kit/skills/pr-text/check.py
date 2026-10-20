"""Checks a pr.md (title, blank line, body) against the pr-text format: the repo squash-merges with the PR body.

Usage: check.py <pr.md>. Prints one `line N: ...` per problem and exits 1 when there is any.
The pipeline's respond gate imports problems() from this file.
"""
import re
import sys
from pathlib import Path

TITLE = re.compile(r'^(feat|fix|docs|refactor|test|chore|perf|build|ci|style|revert)!?: [A-Z0-9]')
WIDTH = 72
MARKDOWN = [
    (re.compile(r'^\s*#{1,6}(\s|$)'), 'a Markdown heading'),
    (re.compile(r'^\s*(```|~~~)'), 'a code fence'),
    (re.compile(r'^\s*\*\*'), 'bold text'),
    (re.compile(r'^\s*\* '), 'a "* " bullet (use "- ")'),
    (re.compile(r'^\s*- \[[ xX]\]'), 'a checkbox'),
]
NESTED_BULLET = re.compile(r'^  - ')
DEEP_INDENT = re.compile(r'^ {4,}')
MIGRATIONS = re.compile(r'migration', re.IGNORECASE)


def problems(text):
    lines = text.splitlines()
    found = []

    if not lines or not TITLE.match(lines[0]):
        found.append('line 1: the title is not "type: Description" (a Conventional Commits type, no scope, then a '
                     'capital or a digit)')

    if lines and '`' in lines[0]:
        found.append('line 1: a backtick; the title is plain text')

    if len(lines) > 1 and lines[1].strip():
        found.append('line 2: must be blank; the body starts on line 3')

    if not any(line.strip() for line in lines[2:]):
        found.append('line 3: the body is empty')

        return found

    continuation = False

    for number, line in enumerate(lines[2:], 3):
        if '`' in line:
            found.append(f'line {number}: a backtick; the body is plain text')

        if len(line.rstrip()) > WIDTH and ' ' in line.strip():
            found.append(f'line {number}: {len(line.rstrip())} columns; wrap at {WIDTH}')

        for pattern, what in MARKDOWN:
            if pattern.match(line):
                found.append(f'line {number}: {what}; the body is plain text')

        deep = bool(DEEP_INDENT.match(line))

        if deep and not continuation:
            found.append(f'line {number}: indented 4 or more spaces outside a nested bullet; never paste file '
                         'contents or command output')

        continuation = bool(NESTED_BULLET.match(line)) or (deep and continuation)

    paragraphs = []

    for number, line in enumerate(lines[2:], 3):
        if not line.strip():
            continue

        if paragraphs and paragraphs[-1][1] == number - 1:
            paragraphs[-1] = (paragraphs[-1][0], number, paragraphs[-1][2] + '\n' + line)
        else:
            paragraphs.append((number, number, line))

    start, _, last = paragraphs[-1]

    if not MIGRATIONS.search(last):
        found.append(f'line {start}: the last paragraph is not the migrations line ("No migrations." or what the '
                     'migration does)')

    return found


def main():
    if len(sys.argv) != 2:
        print('usage: check.py <pr.md>', file=sys.stderr)

        return 2

    found = problems(Path(sys.argv[1]).read_text())

    for problem in found:
        print(problem)

    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
