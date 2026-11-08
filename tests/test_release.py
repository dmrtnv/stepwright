"""`dev/release.py <version>`: the release PR, the checks gate, the merge, the signed tag on the merge commit and the
GitHub Release, resumed by a rerun. Origin is a temp bare repo, the release runs in a temp clone, `gh` is
tests/fake_gh.py answering from a state file, and git signs and verifies with tests/fake_ssh.py as ssh-keygen.

Run: python3 -m unittest tests/test_release.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

import core  # noqa: E402
import fake_ssh  # noqa: E402
from fake_ssh import ATTACKER_KEY, TRUSTED_KEY  # noqa: E402

SPEC = importlib.util.spec_from_file_location('release', ROOT / 'dev' / 'release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
github = sys.modules['release_github']

REAL_GIT = shutil.which('git')
BAD_SIGNER = f'''#!{sys.executable}
import sys
from pathlib import Path
Path(sys.argv[-1] + '.sig').write_text('not a signature\\n')
'''
GATE = '**Rerun the gate:** no (only the docs changed).'
NOTES = f'{GATE}\n\n### Fixed\n\n- A fix.'
V081 = '## v0.8.1 - 2026-11-07\n\n**Rerun the gate:** no (status text).\n\n### Fixed\n\n- Status.\n\n'
V08 = '## v0.8 - 2026-11-05\n\n**Rerun the gate:** yes (the kit).\n\n### Added\n\n- Skills.\n'
CHANGELOG = f'# Changelog\n\nIntro.\n\n## Unreleased\n\n{NOTES}\n\n' + V081 + V08
RELEASED = f'# Changelog\n\nIntro.\n\n## Unreleased\n\n## v0.9 - 2026-11-08\n\n{NOTES}\n\n' + V081 + V08
BRANCH = 'chore/release-v0.9'
NOT_SIGNED = re.escape('the tag v0.9 is not signed by a key in v0.8.1:.github/allowed_signers')


def git(directory, *arguments):
    return subprocess.run([REAL_GIT, '-C', str(directory), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


def checks(*buckets, exit_code=0):
    return {'stdout': json.dumps([{'name': name, 'bucket': bucket} for name, bucket in buckets]), 'exit': exit_code}


NO_CHECKS = {'stderr': f"no checks reported on the '{BRANCH}' branch", 'exit': 1}


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ReleaseCase(unittest.TestCase):
    first_signers = True

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='release-'))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.origin = self.base / 'origin.git'
        self.seed = self.base / 'seed'
        self.clone = self.base / 'clone'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.state_path = self.base / 'gh.json'
        bad_signer = self.bin / 'bad-signer'
        bad_signer.write_text(BAD_SIGNER)
        bad_signer.chmod(0o755)
        fake_ssh.install(self.bin)
        gitconfig = self.base / 'gitconfig'
        gitconfig.write_text(f'[gpg]\n\tformat = ssh\n[user]\n\tsigningkey = key::{TRUSTED_KEY}\n')
        gh = self.bin / 'gh'
        gh.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(TESTS / "fake_gh.py"))} '
                      f'{shlex.quote(str(self.state_path))} "$@"\n')
        gh.chmod(0o755)

        environment = {'PATH': f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}', 'HOME': str(self.base / 'home'),
                       'GIT_CONFIG_GLOBAL': str(gitconfig), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'Test',
                       'GIT_AUTHOR_EMAIL': 'test@example.com', 'GIT_COMMITTER_NAME': 'Test',
                       'GIT_COMMITTER_EMAIL': 'test@example.com'}
        self.clock = Clock()
        for patcher in (mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(github, 'time', self.clock)):
            patcher.start()
            self.addCleanup(patcher.stop)

        git(self.base, 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        git(self.base, 'clone', '-q', str(self.origin), str(self.seed))
        self.commit(CHANGELOG.replace(f'## Unreleased\n\n{NOTES}\n\n', '## Unreleased\n\n'), tags=('v0.8', 'v0.8.1'),
                    signers=self.first_signers)
        self.commit(CHANGELOG)
        git(self.base, 'clone', '-q', str(self.origin), str(self.clone))
        self.state(origin=str(self.origin), calls=[], releases=[], prs={}, next_number=24, watch_exit=0,
                   checks=[NO_CHECKS, checks(('test', 'pending'), exit_code=8), checks(('test', 'pass'))])

    def commit(self, changelog, tags=(), extra=None, signers=True):
        (self.seed / 'CHANGELOG.md').write_text(changelog)
        signers_file = self.seed / '.github/allowed_signers'
        if signers:
            signers_file.parent.mkdir(exist_ok=True)
            signers_file.write_text(f'stepwright-release namespaces="git" {TRUSTED_KEY}\n')
        else:
            signers_file.unlink(missing_ok=True)
        if extra:
            (self.seed / extra).write_text('Extra.\n')
        git(self.seed, 'add', '-A')
        git(self.seed, 'commit', '-q', '-m', 'work')
        for tag in tags:
            git(self.seed, 'tag', tag)
        git(self.seed, 'push', '-q', 'origin', 'main', *tags)
        return git(self.seed, 'rev-parse', 'HEAD')

    def state(self, **changes):
        current = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.state_path.write_text(json.dumps(current | changes))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def gh_calls(self):
        return [call[:2] for call in self.read_state()['calls']]

    def run_release(self, version='v0.9'):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = release.release(version, self.clone)
        return code, out.getvalue()

    def refused(self, pattern, version='v0.9'):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(core.PipelineError, pattern):
            release.release(version, self.clone)

    def on_origin(self, reference):
        return subprocess.run([REAL_GIT, '--git-dir', str(self.origin), 'rev-parse', '--verify', '--quiet', reference],
                              capture_output=True, text=True).stdout.strip()

    def open_pr(self, changelog=RELEASED, extra=None, state='OPEN', merge_commit=None):
        main = git(self.seed, 'rev-parse', 'HEAD')
        git(self.seed, 'switch', '-q', '-c', BRANCH)
        head = self.commit_on_branch(changelog, extra)
        git(self.seed, 'switch', '-q', 'main')
        git(self.seed, 'reset', '-q', '--hard', main)
        self.state(prs={BRANCH: {'number': 30, 'state': state, 'branch': BRANCH, 'title': 'chore: Release v0.9',
                                 'headRefOid': head, 'mergeCommit': merge_commit}})
        return head

    def commit_on_branch(self, changelog, extra):
        (self.seed / 'CHANGELOG.md').write_text(changelog)
        if extra:
            (self.seed / extra).write_text('Extra.\n')
        git(self.seed, 'add', '-A')
        git(self.seed, 'commit', '-q', '-m', 'chore: Release v0.9')
        git(self.seed, 'push', '-q', 'origin', BRANCH)
        return git(self.seed, 'rev-parse', 'HEAD')

    def merged_pr(self, changelog=RELEASED):
        oid = self.commit(changelog)
        self.commit(changelog, extra='later.txt')
        self.state(prs={BRANCH: {'number': 30, 'state': 'MERGED', 'branch': BRANCH, 'title': 'chore: Release v0.9',
                                 'headRefOid': oid, 'mergeCommit': {'oid': oid}}})
        return oid


class ReleasedChangelog(unittest.TestCase):
    def test_the_unreleased_entries_become_the_version_under_an_empty_unreleased(self):
        result = release.released_changelog(CHANGELOG, 'v0.9', '2026-11-08')

        self.assertEqual(result, RELEASED)
        sections = core.changelog_sections(result)
        self.assertEqual(sections['Unreleased'], '')
        self.assertEqual(sections['v0.9'], NOTES)
        self.assertEqual(list(sections), ['Unreleased', 'v0.9', 'v0.8.1', 'v0.8'])

    def test_no_unreleased_heading_raises(self):
        with self.assertRaisesRegex(core.PipelineError, 'Unreleased'):
            release.released_changelog('# Changelog\n\n' + V08, 'v0.9', '2026-11-08')


class FullRun(ReleaseCase):
    def test_a_release_opens_gates_merges_tags_and_publishes(self):
        user_head = git(self.clone, 'rev-parse', 'HEAD')
        (self.clone / 'notes.txt').write_text('Untracked work.\n')

        code, out = self.run_release()

        self.assertEqual(code, 0)
        state = self.read_state()
        calls = state['calls']
        self.assertEqual([call[:2] for call in calls], [
            ['release', 'view'], ['pr', 'view'], ['pr', 'create'], ['pr', 'view'], ['pr', 'checks'], ['pr', 'checks'],
            ['pr', 'checks'], ['pr', 'checks'], ['pr', 'merge'], ['pr', 'view'], ['release', 'create']])
        create = calls[2]
        self.assertEqual(create[create.index('--title') + 1], 'chore: Release v0.9')
        self.assertIn(GATE, create[create.index('--body') + 1])
        self.assertEqual(calls[6], ['pr', 'checks', '24', '--watch', '--fail-fast'])
        pull = state['prs'][BRANCH]
        self.assertEqual(calls[8], ['pr', 'merge', '24', '--squash', '--admin', '--match-head-commit',
                                    pull['headRefOid']])
        self.assertEqual(git(self.clone, 'rev-parse', f'{pull["headRefOid"]}^'), user_head)
        self.assertEqual(git(self.clone, 'diff-tree', '--no-commit-id', '--name-only', '-r', pull['headRefOid']),
                         'CHANGELOG.md')

        oid = pull['mergeCommit']['oid']
        self.assertNotEqual(oid, self.on_origin('refs/heads/main'))
        self.assertEqual(self.on_origin('refs/tags/v0.9^{commit}'), oid)
        self.assertIn('-----BEGIN SSH SIGNATURE-----', git(self.clone, 'cat-file', 'tag', 'v0.9'))
        self.assertEqual(calls[10][:6], ['release', 'create', 'v0.9', '--verify-tag', '--title', 'v0.9'])
        self.assertEqual(state['notes'].strip(), NOTES)
        self.assertIn('Released v0.9: https://github.com/owner/repo/releases/tag/v0.9', out)

        self.assertEqual(git(self.clone, 'rev-parse', 'HEAD'), user_head)
        self.assertEqual(git(self.clone, 'symbolic-ref', '--short', 'HEAD'), 'main')
        self.assertEqual(git(self.clone, 'status', '--porcelain'), '?? notes.txt')

    def test_the_notes_are_the_exact_version_section(self):
        self.commit(RELEASED.replace('## Unreleased\n\n', '## Unreleased\n\n**Rerun the gate:** yes (the next).\n\n'),
                    tags=('v0.9',))
        git(self.clone, 'pull', '-q', 'origin', 'main')

        self.assertEqual(self.run_release('v0.9.1')[0], 0)

        self.assertEqual(self.read_state()['notes'].strip(), '**Rerun the gate:** yes (the next).')


class Refusals(ReleaseCase):
    def test_a_bad_version_makes_no_call(self):
        for version in ('v0.9-rc1', '0.9'):
            with self.subTest(version), mock.patch.object(core, 'git_result', wraps=core.git_result) as spy:
                self.refused('is not a release version', version)
                spy.assert_not_called()
                self.assertEqual(self.read_state()['calls'], [])

    def assert_prepare_refuses(self, pattern, version='v0.9'):
        self.refused(pattern, version)
        self.assertEqual(self.gh_calls(), [['release', 'view'], ['pr', 'view']])
        self.assertEqual(self.on_origin(f'refs/heads/chore/release-{version}'), '')

    def test_a_modified_tracked_file(self):
        (self.clone / 'CHANGELOG.md').write_text('Changed.\n')
        self.assert_prepare_refuses('uncommitted changes')

    def test_head_not_origin_main(self):
        git(self.clone, 'switch', '-q', '--detach', 'HEAD~1')
        self.assert_prepare_refuses('HEAD is not origin/main')

    def test_a_version_not_newer(self):
        for version in ('v0.7', 'v0.8.0'):
            with self.subTest(version):
                self.state(calls=[])
                self.assert_prepare_refuses('is not newer than the latest release v0.8.1', version)

    def test_a_version_equal_once_padded(self):
        git(self.clone, 'tag', '-d', 'v0.8.1')
        git(self.seed, 'push', '-q', 'origin', ':refs/tags/v0.8.1')
        self.assert_prepare_refuses('v0.8.0 is not newer than the latest release v0.8', 'v0.8.0')

    def test_an_empty_unreleased(self):
        self.commit(CHANGELOG.replace(f'## Unreleased\n\n{NOTES}\n\n', '## Unreleased\n\n'))
        git(self.clone, 'pull', '-q', 'origin', 'main')
        self.assert_prepare_refuses('has no entries')

    def test_unreleased_without_a_gate_line(self):
        self.commit(CHANGELOG.replace(GATE + '\n\n', ''))
        git(self.clone, 'pull', '-q', 'origin', 'main')
        self.assert_prepare_refuses('exactly one \\*\\*Rerun the gate:\\*\\* line, it has 0')

    def test_a_closed_pr(self):
        self.open_pr(state='CLOSED')
        self.refused('#30 for chore/release-v0.9 is closed: reopen it')
        self.assertEqual(self.gh_calls(), [['release', 'view'], ['pr', 'view']])

    def test_a_pr_view_failure(self):
        self.state(pr_view_error='HTTP 502: Bad Gateway (https://api.github.com/graphql)')
        self.refused('gh pr view: HTTP 502: Bad Gateway')
        self.assertEqual(self.gh_calls(), [['release', 'view'], ['pr', 'view']])

    def test_a_release_view_failure(self):
        self.state(release_view_error='error connecting to api.github.com')
        with mock.patch.object(core, 'git_result', wraps=core.git_result) as spy:
            self.refused('gh release view: error connecting to api.github.com')
            spy.assert_not_called()
        self.assertEqual(self.gh_calls(), [['release', 'view']])


class Gate(ReleaseCase):
    def assert_no_merge(self, pattern):
        self.refused(pattern)
        self.assertNotIn(['pr', 'merge'], self.gh_calls())
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')
        self.assertEqual(git(self.clone, 'tag', '--list', 'v0.9'), '')

    def test_the_watch_failing(self):
        self.state(watch_exit=1)
        self.assert_no_merge('checks did not pass on #24: no merge, no tag')

    def test_a_final_read_with_a_failing_test(self):
        self.state(checks=[checks(('test', 'pending')), checks(('test', 'fail'))])
        self.assert_no_merge(r'not every check passed on #24 \(test fail\)')

    def test_a_final_read_with_another_check_pending(self):
        self.state(checks=[checks(('test', 'pending')), checks(('test', 'pass'), ('lint', 'pending'))])
        self.assert_no_merge(r'\(lint pending\)')

    def test_a_final_read_with_a_cancelled_check(self):
        self.state(checks=[checks(('test', 'pending')), checks(('test', 'pass'), ('old', 'cancel'))])
        self.assert_no_merge(r'\(old cancel\)')

    def test_a_final_read_that_exits_nonzero(self):
        self.state(checks=[checks(('test', 'pending')), checks(('test', 'pass'), ('lint', 'pass'), exit_code=1)])
        self.assert_no_merge(r'not every check passed on #24 \(no test pass\)')

    def test_a_final_read_with_test_skipping(self):
        self.state(checks=[checks(('test', 'pending')), checks(('test', 'skipping'))])
        self.assert_no_merge(r'not every check passed on #24 \(test skipping\)')

    def test_a_final_read_without_test(self):
        self.state(checks=[checks(('test', 'pending')), checks(('lint', 'pass'))])
        self.assert_no_merge(r'not every check passed on #24 \(no test pass\)')

    def test_no_test_check_by_the_deadline(self):
        self.state(checks=[checks(('lint', 'pending'))])
        self.assert_no_merge('no test check on #24 after 10 minutes')
        self.assertEqual(self.clock.now, 600)

    def test_a_poll_with_a_failing_check_stops_at_once(self):
        self.state(checks=[checks(('lint', 'fail'), exit_code=1)])
        self.assert_no_merge(r'checks failed on #24 \(lint fail\)')
        self.assertEqual(self.gh_calls().count(['pr', 'checks']), 1)

    def test_a_poll_with_another_gh_failure_stops(self):
        self.state(checks=[{'stderr': 'HTTP 401: Bad credentials', 'exit': 1}])
        self.assert_no_merge('gh pr checks: HTTP 401: Bad credentials')
        self.assertEqual(self.gh_calls().count(['pr', 'checks']), 1)


class Resume(ReleaseCase):
    def test_an_open_pr_is_gated_and_merged_without_a_new_one(self):
        head = self.open_pr()

        self.assertEqual(self.run_release()[0], 0)

        self.assertNotIn(['pr', 'create'], self.gh_calls())
        self.assertIn(['pr', 'merge', '30', '--squash', '--admin', '--match-head-commit', head],
                      self.read_state()['calls'])
        self.assertTrue(self.on_origin('refs/tags/v0.9'))

    def test_an_open_pr_changing_another_file_is_refused(self):
        self.open_pr(extra='other.txt')
        self.refused('the release PR changes CHANGELOG.md\nother.txt, not only CHANGELOG.md: no merge')
        self.assertNotIn(['pr', 'merge'], self.gh_calls())
        self.assertNotIn(['pr', 'checks'], self.gh_calls())

    def test_an_open_pr_without_the_version_section_is_refused(self):
        self.open_pr(changelog=CHANGELOG.replace('Intro.', 'Intro, edited.'))
        self.refused('has no v0.9 section')
        self.assertNotIn(['pr', 'merge'], self.gh_calls())

    def test_a_failed_pr_create_reruns_with_the_lease(self):
        self.state(pr_create_error='GraphQL: was submitted too quickly (createPullRequest)')
        self.refused('gh pr create: GraphQL: was submitted too quickly')
        first = self.on_origin(f'refs/heads/{BRANCH}')
        self.assertTrue(first)

        self.state(pr_create_error='')
        os.environ['GIT_COMMITTER_DATE'] = '2026-11-08T12:00:00'
        self.assertEqual(self.run_release()[0], 0)

        head = self.read_state()['prs'][BRANCH]['headRefOid']
        self.assertNotEqual(head, first)
        self.assertTrue(self.on_origin('refs/tags/v0.9'))

    def test_a_merged_pr_without_the_version_section_is_not_tagged(self):
        oid = self.merged_pr(changelog=CHANGELOG.replace('Intro.', 'Intro, edited.'))
        self.refused(f'CHANGELOG.md at {oid} has no v0.9 section')
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')
        self.assertEqual(git(self.clone, 'tag', '--list', 'v0.9'), '')
        self.assertNotIn(['release', 'create'], self.gh_calls())

    def test_a_merge_commit_not_reported_yet(self):
        self.state(merge_commit_null=True)
        self.refused('GitHub has not reported the merge commit of #24 yet: rerun')
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')
        self.assertEqual(git(self.clone, 'tag', '--list', 'v0.9'), '')

    def test_a_signed_local_tag_on_the_merge_commit_is_pushed(self):
        oid = self.merged_pr()
        git(self.clone, 'fetch', '-q', 'origin')
        git(self.clone, 'tag', '-s', 'v0.9', '-m', 'v0.9', oid)

        self.assertEqual(self.run_release()[0], 0)

        self.assertEqual(self.on_origin('refs/tags/v0.9^{commit}'), oid)
        self.assertEqual(self.gh_calls(), [['release', 'view'], ['pr', 'view'], ['release', 'create']])

    def test_a_local_tag_on_another_commit_is_refused(self):
        self.merged_pr()
        git(self.clone, 'tag', '-s', 'v0.9', '-m', 'v0.9', 'HEAD')
        self.refused('the local tag v0.9 is not on the merge commit')
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')

    def test_an_unsigned_local_tag_is_refused(self):
        oid = self.merged_pr()
        git(self.clone, 'fetch', '-q', 'origin')

        for arguments, reason in ((['-a', '-m', 'v0.9'], 'no signature found'), ([], 'cannot verify a non-tag object')):
            with self.subTest(arguments):
                git(self.clone, 'tag', '-f', *arguments, 'v0.9', oid)
                self.refused(f'{NOT_SIGNED} \\(error: .*{reason}')
                self.assertEqual(self.on_origin('refs/tags/v0.9'), '')

    def test_a_fresh_tag_the_signer_did_not_sign_is_refused(self):
        git(self.clone, 'config', 'gpg.ssh.program', str(self.bin / 'bad-signer'))
        self.merged_pr()
        self.refused(NOT_SIGNED)
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')

    def test_a_tag_signed_by_a_key_the_previous_release_does_not_list_is_refused(self):
        git(self.clone, 'config', 'user.signingkey', f'key::{ATTACKER_KEY}')
        self.merged_pr()
        self.refused(f'{NOT_SIGNED} \\(exit 1\\)\\. Runners at v0\\.8\\.1 would refuse it: sign with a key in its '
                     '\\.github/allowed_signers')
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')
        self.assertNotIn(['release', 'create'], self.gh_calls())

    def test_no_earlier_release_is_refused(self):
        for tag in ('v0.8', 'v0.8.1'):
            git(self.seed, 'tag', '-d', tag)
            git(self.clone, 'tag', '-d', tag)
            git(self.seed, 'push', '-q', 'origin', f':refs/tags/{tag}')
        oid = self.merged_pr()
        self.refused(re.escape(f'no release before v0.9 in the history of {oid[:12]}: runners would have no signers '
                               'file to check it against'))
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')
        self.assertNotIn(['release', 'create'], self.gh_calls())

    def test_a_tag_on_origin_is_only_published(self):
        self.commit(RELEASED, tags=('v0.9',))

        self.assertEqual(self.run_release()[0], 0)

        self.assertEqual(self.gh_calls(), [['release', 'view'], ['release', 'create']])
        self.assertEqual(self.read_state()['notes'].strip(), NOTES)

    def test_a_tag_on_origin_without_its_section_is_refused(self):
        self.commit(CHANGELOG.replace('Intro.', 'Intro, edited.'), tags=('v0.9',))
        self.refused('CHANGELOG.md at refs/tags/v0.9\\^\\{commit\\} has no v0.9 section')
        self.assertEqual(self.gh_calls(), [['release', 'view']])

    def test_a_failed_release_create_keeps_the_tag_and_a_rerun_publishes(self):
        self.state(release_create_error='HTTP 422: Validation Failed')
        self.refused('gh release create: HTTP 422: Validation Failed')
        self.assertTrue(self.on_origin('refs/tags/v0.9'))

        self.state(release_create_error='', calls=[])
        self.assertEqual(self.run_release()[0], 0)
        self.assertEqual(self.gh_calls(), [['release', 'view'], ['release', 'create']])
        self.assertEqual(self.read_state()['releases'], ['v0.9'])

    def test_an_existing_release_is_already_released(self):
        self.state(releases=['v0.9'])

        with mock.patch.object(release, 'ROOT', self.clone), \
                mock.patch.object(core, 'git_result', wraps=core.git_result) as spy, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(release.main(['v0.9']), 0)

        spy.assert_not_called()
        self.assertEqual(self.gh_calls(), [['release', 'view']])
        self.assertEqual(out.getvalue(), 'v0.9 is already released\n')


class NoSignersFileBefore(ReleaseCase):
    first_signers = False

    def test_an_earlier_release_without_a_signers_file_is_refused(self):
        self.merged_pr()

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(core.PipelineError) as raised:
            release.release('v0.9', self.clone)

        self.assertEqual(str(raised.exception), 'v0.8.1 has no .github/allowed_signers, so runners there cannot check '
                                                'v0.9: the first release this script can cut is the one after the '
                                                'release that adds the file')
        self.assertNotIn('sign with a key', str(raised.exception))
        self.assertEqual(self.on_origin('refs/tags/v0.9'), '')


class Command(unittest.TestCase):
    def run_script(self, *arguments):
        with tempfile.TemporaryDirectory(prefix='release-bin-') as empty:
            return subprocess.run([sys.executable, str(ROOT / 'dev' / 'release.py'), *arguments], capture_output=True,
                                  text=True, env={'PATH': empty}, cwd=empty)

    def test_a_refusal_exits_1_on_stderr(self):
        result = self.run_script('v0.9-rc1')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'release: v0.9-rc1 is not a release version: use vN.N or vN.N.N\n')

    def test_usage(self):
        for arguments in ((), ('v0.9', 'v1.0')):
            with self.subTest(arguments):
                result = self.run_script(*arguments)
                self.assertEqual((result.returncode, result.stderr), (2, 'usage: dev/release.py <version>\n'))


if __name__ == '__main__':
    unittest.main()
