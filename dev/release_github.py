"""The gh calls of dev/release.py: gh itself, and the checks gate a release PR passes before its admin merge."""
import json
import subprocess
import time

import core

POLL_SECONDS = 10
POLL_LIMIT_SECONDS = 600
GH_TIMEOUT_SECONDS = 120
PASSING = ('pass', 'skipping')


def gh(directory, *arguments):
    try:
        return subprocess.run(['gh', *arguments], cwd=directory, capture_output=True, text=True, errors='replace',
                              stdin=subprocess.DEVNULL, timeout=GH_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise core.PipelineError('gh is not installed: install the GitHub CLI and run gh auth login') from None
    except subprocess.TimeoutExpired:
        raise core.PipelineError(f'gh {" ".join(arguments[:2])} gave no answer in {GH_TIMEOUT_SECONDS} s') from None


def failed(result, command):
    return core.PipelineError(f'{command}: {core.error_line(result.stderr or result.stdout) or "no output"}')


def checks(directory, number):
    """gh's checks as a list, or None while none is reported; any other answer stops."""
    result = gh(directory, 'pr', 'checks', str(number), '--json', 'name,bucket')

    try:
        listed = json.loads(result.stdout)
    except ValueError:
        listed = None

    if isinstance(listed, list) and all(isinstance(check, dict) for check in listed):
        return result.returncode, listed

    if 'no checks reported' in result.stderr:
        return result.returncode, None

    raise failed(result, 'gh pr checks')


def named(listed):
    return ', '.join(f'{check.get("name")} {check.get("bucket")}' for check in listed)


def gate(directory, number):
    print('Waiting for the test check')
    deadline = time.monotonic() + POLL_LIMIT_SECONDS

    while True:
        _, listed = checks(directory, number)
        broken = [check for check in listed or [] if check.get('bucket') in ('fail', 'cancel')]

        if broken:
            raise core.PipelineError(f'checks failed on #{number} ({named(broken)}): no merge')

        if any(check.get('name') == 'test' for check in listed or []):
            break

        if time.monotonic() >= deadline:
            raise core.PipelineError(f'no test check on #{number} after {POLL_LIMIT_SECONDS // 60} minutes: no merge')

        time.sleep(POLL_SECONDS)

    if subprocess.run(['gh', 'pr', 'checks', str(number), '--watch', '--fail-fast'], cwd=directory,
                      stdin=subprocess.DEVNULL).returncode != 0:
        raise core.PipelineError(f'checks did not pass on #{number}: no merge, no tag')

    code, listed = checks(directory, number)
    listed = listed or []
    blocking = [check for check in listed if check.get('bucket') not in PASSING
                or (check.get('name') == 'test' and check.get('bucket') != 'pass')]

    if code != 0 or blocking or not any(check.get('name') == 'test' for check in listed):
        raise core.PipelineError(f'not every check passed on #{number} ({named(blocking) or "no test pass"}): no '
                                 'merge. Rerun a cancelled check, then rerun this command')
