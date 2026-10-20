#!/usr/bin/env python3
"""Run `drizzle-kit generate` under a pty and answer its create-or-rename prompts from declared answers.

Without a TTY, drizzle-kit refuses to prompt ("Interactive prompts require a TTY terminal" from 0.31.10; 0.31.9
printed the prompt) and exits 0 without writing a migration; keys piped all at once are lost after the first prompt.
This drives each prompt as it appears and fails closed on anything undeclared, so the migration stays 100%
drizzle-generated. drizzle-kit also exits 0 on its own errors (a schema that fails to load, a BigInt default), so
exit 0 without one of its result lines is a failure too.

Usage (from packages/db):
  python3 ~/.claude/pipeline/drizzle-generate.py column:users.full_name=name table:orders=create [--name <name>]
  python3 ~/.claude/pipeline/drizzle-generate.py --custom --name <what>     an empty migration for data SQL
Answer keys: <kind>:<table>.<name> for columns (and other per-table entities), <kind>:<name> for tables, enums,
views, sequences, schemas, roles. Values: the old name it was renamed from, or `create`. Only drizzle-kit's
`--custom` and `--name` pass through.
Exit codes: 0 ok, 3 unanswered prompt, 4 declared answer has no matching option, 5 timeout, 6 drizzle-kit exited 0
without a result, else drizzle-kit's code.
"""
import os
import pty
import re
import select
import shutil
import signal
import sys
import time

ANSI = re.compile(rb'\x1b\[[0-9;?]*[A-Za-z]')
PROMPT = re.compile(r'Is (\S+) (\w+)(?: in (\S+) table)? created or renamed from another \w+\?')
OPTION = re.compile(r'^\s*(?:❯\s*)?([+~])\s+(.*?)\s+(create|rename) (\w+)\s*$')
RESULT = re.compile(r'Your SQL migration file|No schema changes, nothing to migrate')
DEADLINE_SECONDS = 180
DOWN_ARROW = b'\x1b[B'
PASS_THROUGH_FLAGS = ('--custom', '--name')


def split_arguments(arguments):
    answers, flags = [], []
    remaining = list(arguments)

    while remaining:
        argument = remaining.pop(0)

        if not argument.startswith('--'):
            answers.append(argument)
        elif argument.split('=', 1)[0] not in PASS_THROUGH_FLAGS:
            sys.exit(f'drizzle-generate: unsupported flag {argument!r}; only {", ".join(PASS_THROUGH_FLAGS)} pass through')
        elif argument == '--name':
            if not remaining:
                sys.exit('drizzle-generate: --name needs a value')

            flags += [argument, remaining.pop(0)]
        else:
            flags.append(argument)

    return answers, flags


def parse_answers(arguments):
    answers = {}

    for argument in arguments:
        key, separator, value = argument.partition('=')

        if not separator or ':' not in key or not value:
            sys.exit(f'drizzle-generate: bad answer {argument!r}; expected <kind>:<table>.<name>=<old name|create>')

        answers[key] = value

    return answers


def command():
    local = os.path.join(os.getcwd(), 'node_modules', '.bin', 'drizzle-kit')

    if os.path.exists(local):
        return [local, 'generate']

    return [shutil.which('pnpm') or 'pnpm', 'exec', 'drizzle-kit', 'generate']


def options_after(text, start):
    options = []

    for line in text[start:].splitlines()[1:]:
        match = OPTION.match(line)

        if match:
            options.append((match.group(1), match.group(2), match.group(3)))
        elif options and line.strip():
            break

    return options


def choose(options, answer, name):
    for index, (marker, label, action) in enumerate(options):
        if answer == 'create' and action == 'create':
            return index

        if answer != 'create' and action == 'rename' and re.match(rf'^{re.escape(answer)}\s+›\s+{re.escape(name)}$', label):
            return index

    return None


def main():
    answer_arguments, flags = split_arguments(sys.argv[1:])
    answers = parse_answers(answer_arguments)
    used = set()
    child_pid, descriptor = pty.fork()

    if child_pid == 0:
        arguments = command() + flags
        os.execvp(arguments[0], arguments)

    buffer = b''
    handled = 0
    deadline = time.time() + DEADLINE_SECONDS
    exit_code = None

    def stop(code, message):
        os.kill(child_pid, signal.SIGKILL)
        os.waitpid(child_pid, 0)
        sys.stdout.write(ANSI.sub(b'', buffer).decode('utf8', 'replace')[-1500:] + '\n')
        sys.stderr.write(f'drizzle-generate: {message}\n')
        sys.exit(code)

    while True:
        if time.time() > deadline:
            stop(5, f'timed out after {DEADLINE_SECONDS}s')

        ready, _, _ = select.select([descriptor], [], [], 0.3)

        if ready:
            try:
                chunk = os.read(descriptor, 65536)
            except OSError:
                chunk = b''

            if not chunk:
                break

            buffer += chunk

        text = ANSI.sub(b'', buffer).decode('utf8', 'replace')
        prompts = list(PROMPT.finditer(text))

        if len(prompts) <= handled:
            continue

        prompt = prompts[handled]
        time.sleep(0.3)
        text = ANSI.sub(b'', buffer).decode('utf8', 'replace')
        name, kind, table = prompt.group(1), prompt.group(2), prompt.group(3)
        key = f'{kind}:{table}.{name}' if table else f'{kind}:{name}'
        options = options_after(text, prompt.start())

        if key not in answers:
            stop(3, f'unanswered prompt {key!r}; options: {[" ".join(option) for option in options]}. Declare it, e.g. {key}=create or {key}=<old name>')

        choice = choose(options, answers[key], name)

        if choice is None:
            stop(4, f'answer {key}={answers[key]} matches no option: {[" ".join(option) for option in options]}')

        os.write(descriptor, DOWN_ARROW * choice + b'\r')
        used.add(key)
        handled += 1

    _, status = os.waitpid(child_pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    output = ANSI.sub(b'', buffer).decode('utf8', 'replace')
    sys.stdout.write(output[-3000:] + '\n')
    unused = sorted(set(answers) - used)

    if unused:
        sys.stderr.write(f'drizzle-generate: declared but never asked: {unused}\n')

    if exit_code == 0 and not RESULT.search(output):
        sys.stderr.write('drizzle-generate: drizzle-kit exited 0 without a result (it swallows its own errors); see the output above\n')
        sys.exit(6)

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
