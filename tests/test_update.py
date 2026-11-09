"""`pipeline update [<tag>]`: fetch the tags, refuse while a stage runs, check out the newest release (or the named
one), rerun init and doctor as the new code, and print the Rerun the gate line of each release it adds or undoes. The
runner is a temp clone of a temp origin; its `pipeline` is a fake that records each call; a fake systemctl answers
from a state file.

Run: python3 -m unittest discover tests
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import adapter  # noqa: E402
import core  # noqa: E402
import fake_ssh  # noqa: E402
from fake_ssh import ATTACKER_KEY, TRUSTED_KEY  # noqa: E402

REAL_GIT = shutil.which('git')
# Behavior comes from behavior.json at call time, so a test changes it without a commit.
FAKE_PIPELINE = '''import fcntl, json, sys, time
from pathlib import Path
base = Path({base!r})
behavior = json.loads((base / 'behavior.json').read_text())
call = sys.argv[1:]
if call == ['doctor']:
    with open({tick!r}, 'a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            call.append('tick free')
        except BlockingIOError:
            call.append('tick held')
with open(base / 'calls', 'a') as handle:
    handle.write(json.dumps(call) + '\\n')
if call == ['init']:
    time.sleep(behavior['init_sleep'])
    print('\\n'.join(behavior['init_stdout']))
    print(behavior['init_stderr'], file=sys.stderr, end='')
    sys.exit(behavior['init_exit'])
sys.exit(behavior['doctor_exit'])
'''
FAKE_SYSTEMCTL = '''#!{python}
import json, sys
from pathlib import Path
state = json.loads(Path({state!r}).read_text())
assert sys.argv[1:] == ['--user', 'list-units', 'pipeline-run-*', '--state=active,activating', '--no-legend',
                        '--plain'], sys.argv
print(state['stdout'], end='')
print(state['stderr'], file=sys.stderr, end='')
sys.exit(state['exit'])
'''
BEHAVIOR = {'init_sleep': 0, 'init_stdout': ['linked   /x/new-skill -> /kit/new-skill', 'skipped  /x/old (exists)',
                                             'Kept your /x/config.json: its values are the ones to check'],
            'init_stderr': '', 'init_exit': 0, 'doctor_exit': 3}
V010 = '## v0.1.0 - 2026-01-01\n\nThe first release.\n'
MID = '## Unreleased\n\n**Rerun the gate:** yes (mid).\n\n' + V010
V020 = '## Unreleased\n\n## v0.2.0 - 2026-02-01\n\n**Rerun the gate:** yes (the kit changed).\n\n' + V010
V030 = '## Unreleased\n\n## v0.3.0 - 2026-03-01\n\nNo gate line.\n\n' + V020.removeprefix('## Unreleased\n\n')
# git looks for "\n[GNUPG:] GOODSIG ", so the status lines start after an empty line.
FAKE_GPG = '''#!/bin/sh
cat > /dev/null
printf '\\n[GNUPG:] GOODSIG 1234 T\\n[GNUPG:] VALIDSIG ABCD\\n[GNUPG:] TRUST_ULTIMATE\\n'
'''
NOT_SIGNED = 'is not signed by a key in HEAD:.github/allowed_signers'
HINT = 'If a release changed the signing key, update one release at a time'
AFTER = '## Unreleased\n\n**Rerun the gate:** yes (after v0.2.0).\n\n' + V020.removeprefix('## Unreleased\n\n')


def verified(target, was):
    return f'Verified {target}: signed by a key in .github/allowed_signers at {was}.'


def git(directory, *arguments):
    return subprocess.run([REAL_GIT, '-C', str(directory), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


class UpdateCase(unittest.TestCase):
    prefix = 'update-'

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix=self.prefix))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.origin = self.base / 'origin'
        self.runner = self.base / 'runner'
        self.logs = self.base / 'logs'
        self.logs.mkdir()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.config = self.base / 'config.json'
        self.behavior(**BEHAVIOR)
        self.systemctl()

        environment = {'PATH': f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}', 'HOME': str(self.base / 'home'),
                       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'Test',
                       'GIT_AUTHOR_EMAIL': 'test@example.com', 'GIT_COMMITTER_NAME': 'Test',
                       'GIT_COMMITTER_EMAIL': 'test@example.com'}
        for patcher in (mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(core, 'PIPELINE_DIR', self.runner),
                        mock.patch.object(core, 'LOGS', self.logs),
                        mock.patch.object(core, 'HOME', self.base / 'home'),
                        mock.patch.object(adapter, 'CONFIG', self.config),
                        mock.patch.object(core, 'PYTHON', sys.executable)):
            patcher.start()
            self.addCleanup(patcher.stop)

        systemctl = self.bin / 'systemctl'
        systemctl.write_text(FAKE_SYSTEMCTL.format(python=sys.executable, state=str(self.base / 'systemctl.json')))
        systemctl.chmod(0o755)
        fake_ssh.install(self.bin)

        self.origin.mkdir()
        git(self.origin, 'init', '-q', '-b', 'main')
        git(self.origin, 'config', 'gpg.format', 'ssh')
        git(self.origin, 'config', 'user.signingkey', f'key::{TRUSTED_KEY}')
        (self.origin / 'pipeline').write_text(FAKE_PIPELINE.format(base=str(self.base),
                                                                   tick=str(self.logs / 'tick.lock')))
        self.commit(V010, 'v0.1.0')
        self.mid = self.commit(MID)
        self.commit(V020, 'v0.2.0')
        subprocess.run([REAL_GIT, 'clone', '-q', str(self.origin), str(self.runner)], check=True)
        git(self.runner, 'checkout', '-q', '--detach', 'v0.1.0')

    def commit(self, changelog, tag=None, signers=(TRUSTED_KEY,), key=TRUSTED_KEY, sign=True):
        (self.origin / 'CHANGELOG.md').write_text('# Changelog\n\n' + changelog)
        signers_file = self.origin / '.github/allowed_signers'

        if signers:
            signers_file.parent.mkdir(exist_ok=True)
            signers_file.write_text(''.join(f'stepwright-release namespaces="git" {line}\n' for line in signers))
        else:
            signers_file.unlink(missing_ok=True)

        git(self.origin, 'add', '-A')
        git(self.origin, 'commit', '-q', '-m', tag or 'work')

        if tag and sign:
            git(self.origin, 'tag', '-s', '-u', f'key::{key}', '-m', tag, tag)
        elif tag:
            git(self.origin, 'tag', '-a', '-m', tag, tag)

        return git(self.origin, 'rev-parse', 'HEAD')

    def behavior(self, **changes):
        path = self.base / 'behavior.json'
        current = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps(current | changes))

    def systemctl(self, stdout='', stderr='', exit_code=0):
        (self.base / 'systemctl.json').write_text(json.dumps({'stdout': stdout, 'stderr': stderr, 'exit': exit_code}))

    def write_config(self):
        self.shop = self.base / 'shop checkout'
        self.shop.mkdir()
        self.config.write_text(json.dumps({'app': {'id': 1, 'slug': 'x', 'privateKey': '/tmp/key.pem', 'reviewer': 'r'},
                                           'repos': {'acme/shop': str(self.shop)}}))

    def update(self, *arguments):
        out, err = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = core.main(['update', *arguments])

        self.out, self.err = out.getvalue(), err.getvalue()

        return code

    def calls(self):
        path = self.base / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def head(self):
        return git(self.runner, 'rev-parse', 'HEAD')

    def tag_commit(self, tag):
        return git(self.runner, 'rev-parse', f'{tag}^{{commit}}')

    def hold(self, name):
        holder = subprocess.Popen(['flock', str(self.logs / name), 'sleep', '60'], start_new_session=True)

        def release():
            os.killpg(holder.pid, signal.SIGKILL)  # flock's sleep holds the lock too
            holder.wait()

        self.addCleanup(release)
        time.sleep(0.5)

    def assertRefused(self, arguments, *texts):
        before = self.head()
        code = self.update(*arguments)
        self.assertEqual(code, 1, self.out + self.err)
        for text in texts:
            self.assertIn(text, self.err)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.calls(), [])
        self.assertNotIn('Verified', self.out)


class MoveTest(UpdateCase):
    def test_forward_to_the_newest(self):
        self.write_config()
        code = self.update()
        self.assertEqual(code, 3, self.out + self.err)
        self.assertEqual(self.head(), self.tag_commit('v0.2.0'))
        self.assertEqual(self.calls(), [['init'], ['doctor', 'tick free']])
        lines = self.out.splitlines()
        self.assertEqual(lines[0], verified('v0.2.0', 'v0.1.0'))
        self.assertEqual(lines[1], 'Was at v0.1.0, now at v0.2.0.')
        self.assertEqual(lines[2], 'linked   /x/new-skill -> /kit/new-skill')
        self.assertNotIn('skipped', self.out)
        self.assertNotIn('Kept your', self.out)
        self.assertIn('  v0.2.0 (added): yes (the kit changed).', lines)
        self.assertNotIn('Unreleased', self.out)
        self.assertNotIn('v0.1.0 (', self.out)
        self.assertIn('Rerun the gate for each repo:', lines)
        self.assertIn(f'  {shlex.quote(f"{self.runner}/sandbox/gate.py")} --repo {shlex.quote(str(self.shop))}', lines)
        self.assertEqual(self.err, '')

    def test_an_explicit_older_tag_undoes_the_newer_release(self):
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.0')
        code = self.update('v0.1.0')
        self.assertEqual(code, 3, self.out + self.err)
        self.assertEqual(self.head(), self.tag_commit('v0.1.0'))
        self.assertEqual(self.out.splitlines()[:2], [verified('v0.1.0', 'v0.2.0'), 'Was at v0.2.0, now at v0.1.0.'])
        self.assertIn('  v0.2.0 (undone): yes (the kit changed).', self.out.splitlines())
        self.assertNotIn('(added)', self.out)

    def test_leaving_an_untagged_head_undoes_its_unreleased_changes(self):
        after = self.commit(AFTER)
        git(self.runner, 'fetch', '-q', 'origin')
        git(self.runner, 'checkout', '-q', '--detach', after)
        short = git(self.runner, 'rev-parse', '--short', 'HEAD')
        self.update('v0.1.0')
        lines = self.out.splitlines()
        self.assertEqual(lines[0], verified('v0.1.0', short))
        self.assertEqual(lines[1], f'Was at {short}, now at v0.1.0.')
        self.assertIn(f'  Unreleased at {short} (undone): yes (after v0.2.0).', lines)
        self.assertIn('  v0.2.0 (undone): yes (the kit changed).', lines)

    def test_leaving_a_tagged_head_prints_no_unreleased_line(self):
        self.commit(AFTER, 'v0.2.1')
        git(self.runner, 'fetch', '-q', '--tags', 'origin')
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.1')
        self.update('v0.1.0')
        self.assertIn('  v0.2.1 (undone): no Rerun the gate line', self.out.splitlines())
        self.assertNotIn('Unreleased', self.out)

    def test_an_untagged_head_behind_the_target_prints_no_unreleased_line(self):
        git(self.runner, 'checkout', '-q', '--detach', self.mid)
        self.update()
        self.assertEqual(self.head(), self.tag_commit('v0.2.0'))
        self.assertIn('  v0.2.0 (added): yes (the kit changed).', self.out.splitlines())
        self.assertNotIn('Unreleased', self.out)

    def test_leaving_a_branch_names_it(self):
        git(self.runner, 'checkout', '-q', '-B', 'main', 'v0.1.0')
        self.update()
        self.assertEqual(self.head(), self.tag_commit('v0.2.0'))
        self.assertEqual(self.out.splitlines()[:2], [verified('v0.2.0', 'main'), 'Was at main, now at v0.2.0.'])

    def test_two_release_tags_on_head_name_the_newer(self):
        git(self.runner, 'tag', 'v0.1.1', 'v0.1.0')
        self.update()
        self.assertEqual(self.out.splitlines()[:2], [verified('v0.2.0', 'v0.1.1'), 'Was at v0.1.1, now at v0.2.0.'])

    def test_a_release_without_a_gate_line_says_so(self):
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.0')
        self.commit('## Unreleased\n\n## v0.3.0 - 2026-03-01\n\nNo gate line.\n\n' + V020.removeprefix('## Unreleased\n\n'),
                    'v0.3.0')
        self.update()
        self.assertIn('  v0.3.0 (added): no Rerun the gate line', self.out.splitlines())
        self.assertNotIn('Rerun the gate for each repo', self.out)

    def test_init_with_nothing_new(self):
        self.behavior(init_stdout=['skipped  /x/old (exists)', 'Kept your /x/config.json: its values'])
        self.update()
        self.assertEqual(self.out.splitlines()[2], 'init: nothing new')

    def test_a_config_that_does_not_load_shows_a_placeholder(self):
        self.update()
        self.assertIn(f'  {shlex.quote(f"{self.runner}/sandbox/gate.py")} --repo <your checkout>', self.out.splitlines())

    def test_a_release_candidate_on_origin_is_never_the_default(self):
        git(self.origin, 'commit', '-q', '--allow-empty', '-m', 'rc')
        git(self.origin, 'tag', 'v0.3-rc1')
        self.update()
        self.assertEqual(self.head(), self.tag_commit('v0.2.0'))


class SpacedRunnerTest(UpdateCase):
    prefix = 'update runner-'

    def test_the_gate_command_quotes_the_runner_path(self):
        self.write_config()
        self.update()
        gate = shlex.quote(f'{self.runner}/sandbox/gate.py')
        self.assertTrue(gate.startswith("'"), gate)
        self.assertIn(f'  {gate} --repo {shlex.quote(str(self.shop))}', self.out.splitlines())

    def test_the_dirty_refusal_command_quotes_the_runner_path(self):
        (self.runner / 'CHANGELOG.md').write_text('mine')
        self.assertRefused((), f'local changes in {self.runner}; update needs a clean clone')
        command = self.err.rsplit('(', 1)[1].split(')', 1)[0]
        self.assertEqual(shlex.split(command), ['git', '-C', str(self.runner), 'status'])
        self.assertIn(shlex.quote(str(self.runner)), command)

    def test_the_go_back_command_quotes_the_runner_path_and_the_branch(self):
        git(self.runner, 'checkout', '-q', '-B', "it's", 'v0.1.0')
        self.behavior(init_exit=1)
        self.assertEqual(self.update(), 1)
        command = self.err.split('go back: ', 1)[1].strip()
        self.assertEqual(shlex.split(command), ['git', '-C', str(self.runner), 'checkout', "it's"])
        self.assertIn(shlex.quote(str(self.runner)), command)


class NoMoveTest(UpdateCase):
    def test_ahead_of_the_newest_stays(self):
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.0')
        git(self.runner, 'commit', '-q', '--allow-empty', '-m', 'fix')
        before = self.head()
        code = self.update()
        self.assertEqual(code, 0, self.out + self.err)
        self.assertEqual(self.head(), before)
        self.assertIn('ahead of v0.2.0, the newest release: nothing newer.', self.out)
        self.assertIn('`pipeline update v0.2.0` goes back to it.', self.out)
        self.assertEqual(self.calls(), [])

    def test_ahead_names_only_release_tags(self):
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.0')
        git(self.runner, 'commit', '-q', '--allow-empty', '-m', 'one')
        git(self.runner, 'tag', 'nightly-1')
        git(self.runner, 'tag', 'v0.3-rc1')
        git(self.runner, 'commit', '-q', '--allow-empty', '-m', 'two')
        short = git(self.runner, 'rev-parse', '--short', 'HEAD')
        code = self.update()
        self.assertEqual(code, 0, self.out + self.err)
        self.assertEqual(self.out, f'At v0.2.0-2-g{short}, ahead of v0.2.0, the newest release: nothing newer. '
                                   '`pipeline update v0.2.0` goes back to it.\n')
        self.assertEqual(self.calls(), [])

    def test_already_at_the_target(self):
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.0')
        code = self.update()
        self.assertEqual((code, self.out), (0, 'Already at v0.2.0.\n'))
        self.assertEqual(self.calls(), [])


class RefusalTest(UpdateCase):
    def test_an_unknown_tag(self):
        self.assertRefused(('v9.9',), 'no release tag v9.9; the newest: v0.2.0, v0.1.0')

    def test_a_release_candidate_named_explicitly(self):
        git(self.origin, 'tag', 'v0.3-rc1')
        self.assertRefused(('v0.3-rc1',), 'no release tag v0.3-rc1; the newest: v0.2.0, v0.1.0')

    def test_an_option_is_an_unknown_tag(self):
        self.assertRefused(('--force',), 'no release tag --force')

    def test_two_arguments_are_a_usage_error(self):
        self.assertEqual(self.update('a', 'b'), 2)
        self.assertIn('usage: pipeline update [<tag>]', self.err)

    def test_no_release_tags(self):
        for tag in ('v0.1.0', 'v0.2.0'):
            git(self.origin, 'tag', '-d', tag)
            git(self.runner, 'tag', '-d', tag)
        self.assertRefused((), 'no release tag')

    def test_a_dirty_clone(self):
        (self.runner / 'CHANGELOG.md').write_text('mine')
        self.assertRefused((), f'local changes in {self.runner}; update needs a clean clone')

    def test_an_untracked_file_is_not_dirty(self):
        (self.runner / 'notes.txt').write_text('mine')
        self.assertEqual(self.update(), 3, self.out + self.err)

    def test_a_held_run_lock(self):
        self.hold('run-shop-x.lock')
        self.assertRefused((), 'a stage is running (run-shop-x.lock)', '`pipeline pause`')

    def test_a_held_tick_lock(self):
        self.hold('tick.lock')
        self.assertRefused((), 'holds tick')

    def test_an_activating_run_unit(self):
        self.systemctl(stdout='pipeline-run-shop-x.service loaded activating start pipeline shop x\n')
        self.assertRefused((), 'a stage is running (pipeline-run-shop-x.service)')

    def test_systemctl_failing(self):
        self.systemctl(stderr='Failed to connect to bus: No medium found\n', exit_code=1)
        self.assertRefused((), 'Failed to connect to bus: No medium found', 'update needs your user manager')

    def test_a_clobbered_tag(self):
        git(self.origin, 'tag', '-f', 'v0.2.0', 'v0.1.0')
        self.assertRefused((), 'git fetch failed; the checkout is unchanged', 'would clobber existing tag')

    def test_a_failed_checkout(self):
        (self.origin / 'new.txt').write_text('released')
        self.commit(V030, 'v0.3.0')
        (self.runner / 'new.txt').write_text('mine')
        self.assertRefused((), 'git checkout v0.3.0 failed; the checkout is unchanged')


class VerifyTest(UpdateCase):
    def from_v020(self):
        git(self.runner, 'checkout', '-q', '--detach', 'v0.2.0')

    def test_a_tag_signed_by_an_unlisted_key(self):
        self.from_v020()
        self.commit(V030, 'v0.3.0', key=ATTACKER_KEY)
        self.assertRefused((), f'the tag v0.3.0 {NOT_SIGNED} (exit 1); the checkout is unchanged')
        self.assertNotIn(HINT, self.err)

    def test_an_unsigned_annotated_tag(self):
        self.from_v020()
        self.commit(V030, 'v0.3.0', sign=False)
        self.assertRefused((), f'the tag v0.3.0 {NOT_SIGNED} (error: no signature found)')

    def test_a_lightweight_tag(self):
        self.from_v020()
        self.commit(V030)
        git(self.origin, 'tag', 'v0.3.0')
        self.assertRefused((), f'the tag v0.3.0 {NOT_SIGNED}', 'cannot verify a non-tag object')

    def test_the_targets_own_signers_file_is_not_trusted(self):
        self.from_v020()
        self.commit(V030, 'v0.3.0', signers=(TRUSTED_KEY, ATTACKER_KEY), key=ATTACKER_KEY)
        self.assertRefused((), f'the tag v0.3.0 {NOT_SIGNED} (exit 1)')

    def test_an_old_signed_tag_under_a_newer_name(self):
        git(self.origin, 'update-ref', 'refs/tags/v0.3.0', git(self.origin, 'rev-parse', 'refs/tags/v0.2.0'))
        self.assertRefused(())
        self.assertEqual(self.err, 'the tag v0.3.0 is signed as v0.2.0, not v0.3.0; the checkout is unchanged\n'
                                   f'{HINT}: `pipeline update v0.2.0`\n')

    def signed_by_another_format(self, header, footer):
        commit = git(self.origin, 'rev-parse', 'v0.2.0^{commit}')
        body = (f'object {commit}\ntype commit\ntag v0.3.0\ntagger Test <test@example.com> 1767225600 +0000\n\nv0.3.0\n'
                f'{header}\n\nZmFrZQ==\n{footer}\n')
        made = subprocess.run([REAL_GIT, '-C', str(self.origin), 'mktag'], input=body, capture_output=True, text=True,
                              check=True).stdout.strip()
        git(self.origin, 'update-ref', 'refs/tags/v0.3.0', made)

    def test_a_pgp_or_x509_signature_the_users_gpg_accepts(self):
        gpg = self.bin / 'fake-gpg'
        gpg.write_text(FAKE_GPG)
        gpg.chmod(0o755)
        cases = {'pgp': ('gpg.program', '-----BEGIN PGP SIGNATURE-----', '-----END PGP SIGNATURE-----'),
                 'x509': ('gpg.x509.program', '-----BEGIN SIGNED MESSAGE-----', '-----END SIGNED MESSAGE-----')}

        for name, (key, header, footer) in cases.items():
            with self.subTest(name):
                self.signed_by_another_format(header, footer)
                git(self.runner, 'config', key, str(gpg))
                self.assertRefused((), f'the tag v0.3.0 {NOT_SIGNED}')
                git(self.runner, 'config', '--unset', key)
                git(self.runner, 'tag', '-d', 'v0.3.0')

    def test_the_users_ssh_program_does_not_decide(self):
        git(self.runner, 'config', 'gpg.ssh.program', '/bin/false')
        self.assertEqual(self.update(), 3, self.out + self.err)
        self.assertEqual(self.head(), self.tag_commit('v0.2.0'))

    def test_a_failed_move_across_two_releases_names_the_oldest(self):
        self.commit(V030, 'v0.3.0', key=ATTACKER_KEY)
        self.assertRefused((), f'the tag v0.3.0 {NOT_SIGNED} (exit 1)')
        self.assertTrue(self.err.endswith(f'\n{HINT}: `pipeline update v0.2.0`\n'), self.err)

    def test_a_failed_rollback_has_no_hint(self):
        self.commit(V030, 'v0.3.0')
        git(self.origin, 'tag', '-s', '-u', f'key::{ATTACKER_KEY}', '-m', 'v0.0.9', 'v0.0.9', 'v0.1.0^{commit}')
        git(self.runner, 'fetch', '-q', '--tags', 'origin')
        git(self.runner, 'checkout', '-q', '--detach', 'v0.3.0')
        self.assertRefused(('v0.0.9',), f'the tag v0.0.9 {NOT_SIGNED} (exit 1)')
        self.assertNotIn(HINT, self.err)

    def test_a_head_without_a_signers_file(self):
        self.commit(V030, 'v0.3.0')
        git(self.runner, 'rm', '-q', '.github/allowed_signers')
        git(self.runner, 'commit', '-q', '-m', 'no signers')
        (self.runner / '.github').mkdir()
        (self.runner / '.github/allowed_signers').write_text(f'stepwright-release namespaces="git" {TRUSTED_KEY}\n')
        self.assertRefused((), 'the installed checkout has no .github/allowed_signers, so update cannot verify v0.3.0; '
                               'the checkout is unchanged. Move by hand with the commands in INSTALL: Update.')
        self.assertNotIn('is not signed', self.err)
        self.assertNotIn(HINT, self.err)

    def test_already_at_a_release_without_a_signers_file(self):
        self.commit(V030, 'v0.3.0', signers=())
        git(self.runner, 'fetch', '-q', '--tags', 'origin')
        git(self.runner, 'checkout', '-q', '--detach', 'v0.3.0')
        code = self.update()
        self.assertEqual((code, self.out, self.err), (0, 'Already at v0.3.0.\n', ''))
        self.assertEqual(self.calls(), [])


class InitFailureTest(UpdateCase):
    def test_init_fails(self):
        self.behavior(init_exit=1, init_stderr='Traceback: boom\n')
        code = self.update()
        self.assertEqual(code, 1)
        self.assertEqual(self.head(), self.tag_commit('v0.2.0'))
        self.assertEqual(self.calls(), [['init']])
        self.assertIn('Traceback: boom', self.err)
        self.assertIn('init failed (exit 1)', self.err)
        self.assertIn(verified('v0.2.0', 'v0.1.0'), self.out)
        self.assertIn(f'git -C {self.runner} checkout v0.1.0', self.err)

    def test_init_fails_on_a_branch(self):
        git(self.runner, 'checkout', '-q', '-B', 'main', 'v0.1.0')
        self.behavior(init_exit=1)
        code = self.update()
        self.assertEqual(code, 1)
        self.assertIn(f'git -C {self.runner} checkout main', self.err)

    def test_init_times_out(self):
        self.behavior(init_sleep=5)

        with mock.patch.object(core, 'INIT_TIMEOUT_SECONDS', 1):
            code = self.update()

        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), [['init']])
        self.assertIn('init failed (no answer in 1 s)', self.err)
        self.assertIn(f'git -C {self.runner} checkout v0.1.0', self.err)
        self.assertNotIn('Traceback', self.err)


if __name__ == '__main__':
    unittest.main()
