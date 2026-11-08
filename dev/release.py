#!/usr/bin/python3
"""`dev/release.py <version>` cuts a release of the checkout it lives in: the "chore: Release <version>" PR (CHANGELOG.md
only), the checks gate, the squash merge, the SSH-signed tag on the merge commit, its push and the GitHub Release. A
rerun of the same command reads the state again (Release, tag on origin, PR) and resumes where a run stopped. It never
touches the work tree: the release commit is built on origin/main with a temp index.

Run: dev/release.py v0.9
"""
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'dev')]

import core  # noqa: E402
from release_github import failed, gate, gh  # noqa: E402
from release_tag import tag  # noqa: E402

UNRELEASED = re.compile(r'^## Unreleased[ \t]*$', re.MULTILINE)


def released_changelog(text, version, date):
    """The Unreleased entries become `## <version> - <date>`, under a new empty Unreleased."""
    match = UNRELEASED.search(text)

    if not match:
        raise core.PipelineError('CHANGELOG.md has no "## Unreleased" heading')

    return text[:match.end()] + f'\n\n## {version} - {date}' + text[match.end():]


def version_key(tag):
    parts = [int(part) for part in tag[1:].split('.')]
    return tuple(parts + [0] * (3 - len(parts)))


def section(directory, revision, version):
    shown = core.git_result(directory, 'show', f'{revision}:CHANGELOG.md')
    body = core.changelog_sections(shown.stdout).get(version) if shown.returncode == 0 else None

    if not body:
        raise core.PipelineError(f'CHANGELOG.md at {revision} has no {version} section')

    return body


def release(version, directory):
    if not core.RELEASE_TAG.fullmatch(version):
        raise core.PipelineError(f'{version} is not a release version: use vN.N or vN.N.N')

    view = gh(directory, 'release', 'view', version)

    if view.returncode == 0:
        print(f'{version} is already released')
        return 0

    if 'release not found' not in view.stderr:
        raise failed(view, 'gh release view')

    core.git(directory, 'fetch', '--tags', 'origin')

    if not core.git(directory, 'ls-remote', '--tags', 'origin', f'refs/tags/{version}'):
        tag(directory, version, merge_commit(directory, version))

    publish(directory, version)
    return 0


def pull_request(directory, branch):
    result = gh(directory, 'pr', 'view', branch, '--json', 'number,state,headRefOid,mergeCommit')

    if result.returncode == 0:
        return json.loads(result.stdout)

    if 'no pull requests found' in result.stderr:
        return None

    raise failed(result, 'gh pr view')


def merge_commit(directory, version):
    branch = f'chore/release-{version}'
    found = pull_request(directory, branch)

    if found is None:
        prepare(directory, version, branch)
        found = pull_request(directory, branch)

    if found is None:
        raise core.PipelineError(f'gh pr create reported no error, but no pull request exists for {branch}: rerun')

    if found['state'] == 'CLOSED':
        raise core.PipelineError(f'#{found["number"]} for {branch} is closed: reopen it, or release another version')

    if found['state'] == 'OPEN':
        check_head(directory, version, found['headRefOid'])
        gate(directory, found['number'])
        merged = gh(directory, 'pr', 'merge', str(found['number']), '--squash', '--admin', '--match-head-commit',
                    found['headRefOid'])

        if merged.returncode != 0:
            raise failed(merged, 'gh pr merge')

        found = pull_request(directory, branch) or found

    if found['state'] != 'MERGED':
        raise core.PipelineError(f'#{found["number"]} for {branch} is {found["state"]}, not merged yet: rerun '
                                 'once GitHub shows it merged')

    if not found.get('mergeCommit'):
        raise core.PipelineError(f'GitHub has not reported the merge commit of #{found["number"]} yet: rerun')

    oid = found['mergeCommit']['oid']
    core.git(directory, 'fetch', '--tags', 'origin')
    section(directory, oid, version)
    print(f'Merged #{found["number"]} as {oid[:12]}')
    return oid


def prepare(directory, version, branch):
    if core.git(directory, 'status', '--porcelain', '--untracked-files=no'):
        raise core.PipelineError('the checkout has uncommitted changes: commit or discard them, then rerun')

    if core.git(directory, 'rev-parse', 'HEAD') != core.git(directory, 'rev-parse', 'origin/main'):
        raise core.PipelineError('HEAD is not origin/main: switch to main and pull, then rerun')

    newest = (core.release_tags(directory) or [None])[0]

    if newest and version_key(version) <= version_key(newest):
        raise core.PipelineError(f'{version} is not newer than the latest release {newest}')

    shown = core.git_result(directory, 'show', 'origin/main:CHANGELOG.md')

    if shown.returncode != 0:
        raise failed(shown, 'git show origin/main:CHANGELOG.md')

    unreleased = core.changelog_sections(shown.stdout).get('Unreleased')

    if not unreleased:
        raise core.PipelineError('Unreleased in CHANGELOG.md on origin/main has no entries: nothing to release')

    gate_lines = core.GATE_LINE.findall(unreleased)

    if len(gate_lines) != 1:
        raise core.PipelineError(f'Unreleased in CHANGELOG.md needs exactly one **Rerun the gate:** line, it has '
                                 f'{len(gate_lines)}')

    date = datetime.date.today().isoformat()
    commit = release_commit(directory, version, released_changelog(shown.stdout, version, date))
    core.git(directory, 'push', '--force-with-lease', 'origin', f'{commit}:refs/heads/{branch}')
    body = (f'The Unreleased entries become {version} - {date}, with a new empty Unreleased above them.\n\n'
            f'**Rerun the gate:** {gate_lines[0]}\n\n'
            'After merge, dev/release.py tags the merge commit, pushes the tag and publishes the GitHub Release.\n')
    created = gh(directory, 'pr', 'create', '--base', 'main', '--head', branch, '--title', f'chore: Release {version}',
                 '--body', body)

    if created.returncode != 0:
        raise failed(created, 'gh pr create')

    print(f'Opened {created.stdout.strip()}')


def release_commit(directory, version, text):
    with tempfile.TemporaryDirectory(prefix='release-') as scratch:
        changelog = Path(scratch) / 'CHANGELOG.md'
        changelog.write_text(text)
        blob = core.git(directory, 'hash-object', '-w', str(changelog))
        index = os.environ | {'GIT_INDEX_FILE': str(Path(scratch) / 'index')}
        core.git(directory, 'read-tree', 'origin/main', env=index)
        core.git(directory, 'update-index', '--cacheinfo', f'100644,{blob},CHANGELOG.md', env=index)
        tree = core.git(directory, 'write-tree', env=index)

    commit = core.git(directory, 'commit-tree', tree, '-p', 'origin/main', '-m', f'chore: Release {version}')
    changed = core.git(directory, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'origin/main', commit)

    if changed != 'CHANGELOG.md':
        raise core.PipelineError(f'the release commit changes {changed or "nothing"}, not only CHANGELOG.md')

    return commit


def check_head(directory, version, head):
    changed = core.git(directory, 'diff', '--name-only', f'origin/main...{head}')

    if changed != 'CHANGELOG.md':
        raise core.PipelineError(f'the release PR changes {changed or "nothing"}, not only CHANGELOG.md: no merge')

    section(directory, head, version)


def publish(directory, version):
    notes = section(directory, f'refs/tags/{version}^{{commit}}', version)

    with tempfile.NamedTemporaryFile('w', prefix='release-notes-', suffix='.md') as handle:
        handle.write(notes + '\n')
        handle.flush()
        created = gh(directory, 'release', 'create', version, '--verify-tag', '--title', version, '--notes-file',
                     handle.name)

    if created.returncode != 0:
        raise failed(created, 'gh release create')

    print(f'Released {version}: {created.stdout.strip()}')


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv

    if len(arguments) != 1:
        print('usage: dev/release.py <version>', file=sys.stderr)
        return 2

    try:
        return release(arguments[0], ROOT)
    except core.PipelineError as error:
        print(f'release: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('release: stopped; rerun the same command to resume', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
