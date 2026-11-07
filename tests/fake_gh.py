"""A fake `gh` for tests/test_release.py: `fake_gh.py <state.json> <gh arguments>`. It records each call in the state's
`calls`, answers `release view|create` and `pr view|create|checks|merge` from the state, and merges for real in the
bare origin the state names. Not a test module: discovery skips it.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

STATE = Path(sys.argv[1])
CALL = sys.argv[2:]


def git(*arguments):
    return subprocess.run(['git', *arguments], capture_output=True, text=True, check=True).stdout.strip()


def answer(state, stdout='', stderr='', exit_code=0):
    STATE.write_text(json.dumps(state))
    print(stdout, end='')
    print(stderr, file=sys.stderr, end='')
    sys.exit(exit_code)


def option(name):
    return CALL[CALL.index(name) + 1]


def merge(state, found, title):
    origin = state['origin']

    with tempfile.TemporaryDirectory(prefix='fake-gh-') as scratch:
        git('clone', '-q', origin, scratch)
        git('-C', scratch, 'merge', '-q', '--squash', f'origin/{found["branch"]}')
        git('-C', scratch, 'commit', '-q', '-m', f'{title} (#{found["number"]})')
        oid = git('-C', scratch, 'rev-parse', 'HEAD')
        (Path(scratch) / 'later.txt').write_text('A commit after the merge.\n')
        git('-C', scratch, 'add', 'later.txt')
        git('-C', scratch, 'commit', '-q', '-m', 'later')
        git('-C', scratch, 'push', '-q', 'origin', 'main')

    return oid


def main():
    state = json.loads(STATE.read_text())
    state['calls'].append(CALL)
    prs = state['prs']

    match CALL[:2]:
        case ['release', 'view']:
            if CALL[2] in state['releases']:
                answer(state, f'title:\t{CALL[2]}\n')
            answer(state, stderr=state.get('release_view_error', 'release not found') + '\n', exit_code=1)
        case ['release', 'create']:
            if state.get('release_create_error'):
                answer(state, stderr=state['release_create_error'] + '\n', exit_code=1)
            if '--verify-tag' in CALL and subprocess.run(['git', '--git-dir', state['origin'], 'rev-parse', '--verify',
                                                          '--quiet', f'refs/tags/{CALL[2]}'],
                                                         capture_output=True).returncode != 0:
                answer(state, stderr=f'tag {CALL[2]} doesn\'t exist in the repo\n', exit_code=1)
            state['releases'].append(CALL[2])
            state['notes'] = Path(option('--notes-file')).read_text()
            answer(state, f'https://github.com/owner/repo/releases/tag/{CALL[2]}\n')
        case ['pr', 'view']:
            if state.get('pr_view_error'):
                answer(state, stderr=state['pr_view_error'] + '\n', exit_code=1)
            if CALL[2] in prs:
                found = prs[CALL[2]]
                answer(state, json.dumps({key: found[key] for key in ('number', 'state', 'headRefOid',
                                                                      'mergeCommit')}) + '\n')
            answer(state, stderr=f'no pull requests found for branch "{CALL[2]}"\n', exit_code=1)
        case ['pr', 'create']:
            if state.get('pr_create_error'):
                answer(state, stderr=state['pr_create_error'] + '\n', exit_code=1)
            branch = option('--head')
            number = state['next_number']
            state['next_number'] += 1
            prs[branch] = {'number': number, 'state': 'OPEN', 'branch': branch, 'title': option('--title'),
                           'headRefOid': git('--git-dir', state['origin'], 'rev-parse', f'refs/heads/{branch}'),
                           'mergeCommit': None}
            answer(state, f'https://github.com/owner/repo/pull/{number}\n')
        case ['pr', 'checks'] if '--watch' in CALL:
            answer(state, 'test\tpass\t1m\n', exit_code=state['watch_exit'])
        case ['pr', 'checks']:
            reply = state['checks'][0] if len(state['checks']) == 1 else state['checks'].pop(0)
            answer(state, reply.get('stdout', ''), reply.get('stderr', ''), reply.get('exit', 0))
        case ['pr', 'merge']:
            found = next(found for found in prs.values() if str(found['number']) == CALL[2])
            if option('--match-head-commit') != found['headRefOid']:
                answer(state, stderr='Pull request head has moved\n', exit_code=1)
            oid = merge(state, found, found['title'])
            found['state'] = 'MERGED'
            found['mergeCommit'] = None if state.get('merge_commit_null') else {'oid': oid}
            answer(state)

    answer(state, stderr=f'fake gh: unknown call {CALL}\n', exit_code=2)


main()
