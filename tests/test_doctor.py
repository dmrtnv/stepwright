"""`pipeline doctor`: one PASS/FAIL line per install check, a fix under each FAIL, a summary and its exit code. Fake
systemctl, loginctl, docker and sbx answer from a state file on a bare PATH; git is the real one; HOME, the configs, the
kit and the runner checkout are temp copies.

Run: python3 -m unittest discover tests
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import adapter  # noqa: E402
import backend  # noqa: E402
import core  # noqa: E402
from backend import sbx_host  # noqa: E402
from test_core import fixture_kit  # noqa: E402

REAL_GIT = shutil.which('git')
# Each fake answers from state.json beside its bin dir; sbx only records that it was called.
FAKES = {
    'systemctl': r'''
args = sys.argv[1:]
assert args[0] == '--user', args
if args[1] == 'is-enabled':
    if isinstance(value := state['enabled'].get(args[2], ''), dict):
        print(value['stderr'], file=sys.stderr)
        sys.exit(1)
    print(value)
    sys.exit(0 if state['enabled'].get(args[2]) in ('enabled', 'static') else 1)
if args[1] == 'is-active':
    sys.exit(0 if state['active'] else 3)
sys.exit(1)
''',
    'loginctl': r'''
assert sys.argv[1:] == ['show-user', pwd.getpwuid(os.getuid()).pw_name, '--property=Linger', '--value'], sys.argv
if state['linger'] is None:
    print('Failed to get user: User is not logged in or lingering', file=sys.stderr)
    sys.exit(1)
print(state['linger'])
''',
    'docker': r'''
assert sys.argv[1:] == ['info', '--format', '{{.ServerVersion}}'], sys.argv
time.sleep(state['docker'].get('sleep', 0))
print(state['docker'].get('stdout', ''))
print(state['docker'].get('stderr', ''), file=sys.stderr)
sys.exit(state['docker'].get('exit', 0))
''',
    'sbx': r'''
with open(root / 'sbx-calls', 'a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\n')
sys.exit(1)
''',
    'claude': '', 'node': '', 'jq': '',
}
HEADER = '#!{python}\nimport json, os, pathlib, pwd, sys, time\nroot = pathlib.Path(__file__).resolve().parent.parent\n' \
         "state = json.loads((root / 'state.json').read_text())\n"
GOOD_STATE = {'enabled': {'sbx.service': 'enabled', 'pipeline-tick.service': 'static', 'pipeline-tick.timer': 'enabled'},
              'active': True, 'linger': 'yes', 'docker': {'stdout': '29.8.1'}}


def git(directory, *arguments):
    return subprocess.run([REAL_GIT, '-C', str(directory), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='doctor-'))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / 'home'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.runner = self.base / 'runner'
        self.kit = fixture_kit(self.base)
        self.pipeline_config = self.home / '.config/pipeline/config.json'
        self.host_config = self.home / '.config/sbx-host/config.json'
        self.key = self.home / '.config/pipeline/app.pem'

        environment = {'PATH': str(self.bin), 'HOME': str(self.home), 'GIT_CONFIG_GLOBAL': '/dev/null',
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
                       'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com'}
        for patcher in (mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(core, 'HOME', self.home),
                        mock.patch.object(core, 'PIPELINE_DIR', self.runner),
                        mock.patch.object(adapter, 'CONFIG', self.pipeline_config),
                        mock.patch.object(sbx_host, 'CONFIG', self.host_config),
                        mock.patch.object(backend, 'KIT', self.kit),
                        mock.patch('os.getgroups', return_value=[100, 108]),
                        mock.patch('os.getgid', return_value=100),
                        mock.patch('grp.getgrnam', return_value=mock.Mock(gr_gid=108))):
            patcher.start()
            self.addCleanup(patcher.stop)

        for name, body in FAKES.items():
            self.fake(name, body)
        (self.bin / 'git').write_text(f'#!{sys.executable}\nimport os, sys\n'
                                      f'os.execv({REAL_GIT!r}, [{REAL_GIT!r}] + sys.argv[1:])\n')
        (self.bin / 'git').chmod(0o755)
        self.state(GOOD_STATE)

        self.runner.mkdir()
        git(self.runner, 'init', '-q', '-b', 'main')
        git(self.runner, 'commit', '-q', '--allow-empty', '-m', 'one')
        git(self.runner, 'tag', 'v0.1.0')

        for item in backend.KIT_ITEMS:
            (self.home / '.claude' / item).mkdir(parents=True)

            for entry in (self.kit / item).iterdir():
                if entry.name != '__pycache__':
                    (self.home / '.claude' / item / entry.name).symlink_to(entry)

        self.key.parent.mkdir(parents=True)
        self.key.write_text('key')
        self.key.chmod(0o600)
        self.config({})

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(HEADER.format(python=sys.executable) + body)
        path.chmod(0o755)

    def state(self, state):
        (self.base / 'state.json').write_text(json.dumps(state))

    def change(self, **changes):
        state = json.loads((self.base / 'state.json').read_text())
        state.update(changes)
        self.state(state)

    def config(self, extra, key=None):
        self.pipeline_config.write_text(json.dumps(dict({
            'app': {'id': 1, 'slug': 'acme-pipeline', 'privateKey': str(key or self.key), 'reviewer': 'alice'},
            'repos': {'acme/shop': '/tmp/shop'}}, **extra)))

    def doctor(self, *arguments):
        out, err = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = core.main(['doctor', *arguments])

        self.err = err.getvalue()

        return code, out.getvalue().splitlines()

    def check(self, label):
        """(the label's line, the line after it) of a doctor run."""
        _, lines = self.doctor()
        found = [index for index, line in enumerate(lines)
                 if line.split('  ', 1)[-1].split(':', 1)[0] == label and line[:4] in ('PASS', 'FAIL', 'SKIP')]
        self.assertEqual(len(found), 1, '\n'.join(lines))

        return lines[found[0]], (lines[found[0] + 1:] or [''])[0]

    def assertPass(self, label, detail=None):
        line, after = self.check(label)
        self.assertEqual(line, f'PASS  {label}' + (f': {detail}' if detail else ''))
        self.assertFalse(after.strip().startswith('fix:'), after)

    def assertFail(self, label, detail, fix):
        line, after = self.check(label)
        self.assertEqual(line, f'FAIL  {label}: {detail}')
        self.assertEqual(after, f'      fix: {fix}')


class SummaryTest(DoctorCase):
    def test_all_pass_exits_0_and_counts_every_line(self):
        code, lines = self.doctor()
        checks = [line for line in lines if line[:4] in ('PASS', 'FAIL')]
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(lines[-1], f'{len(checks)} checks, 0 FAIL')
        self.assertTrue(all(line.startswith('PASS') for line in checks), '\n'.join(lines))
        labels = [line.split('  ', 1)[1].split(':', 1)[0] for line in checks]
        kit = [f'kit {item}/{entry.name}' for item in backend.KIT_ITEMS for entry in sorted((self.kit / item).iterdir())
               if entry.name != '__pycache__']
        self.assertEqual(labels, ['tool claude', 'tool node', 'tool git', 'tool jq', 'tool sbx', 'kvm group', 'docker',
                                  'runner', *kit, 'unit sbx.service', 'linger', 'app key', 'config',
                                  'sbx-host reserve', 'unit pipeline-tick.service', 'unit pipeline-tick.timer'])

    def test_one_fail_exits_1(self):
        (self.bin / 'jq').unlink()
        code, lines = self.doctor()
        self.assertEqual(code, 1)
        checks = [line for line in lines if line[:4] in ('PASS', 'FAIL')]
        self.assertEqual([line for line in checks if line.startswith('FAIL')], ['FAIL  tool jq: not on PATH'])
        self.assertEqual(lines[-1], f'{len(checks)} checks, 1 FAIL')

    def test_a_skip_is_not_counted(self):
        self.pipeline_config.unlink()
        code, lines = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn('SKIP  sbx-host reserve: needs config.json to load', lines)
        checks = [line for line in lines if line[:4] in ('PASS', 'FAIL')]
        self.assertEqual(lines[-1], f'{len(checks)} checks, {sum(line.startswith("FAIL") for line in checks)} FAIL')
        self.assertNotIn('sbx-host reserve', ' '.join(checks))

    def test_extra_arguments_are_a_usage_error(self):
        code, _ = self.doctor('extra')
        self.assertEqual(code, 2)
        self.assertIn('usage: pipeline doctor', self.err)

    def test_no_sbx_call_whether_the_unit_is_up_or_down(self):
        for active in (True, False):
            self.change(active=active)
            self.doctor()
            self.assertFalse((self.base / 'sbx-calls').exists(), f'active={active}')

    def test_the_real_home_is_never_read_or_written(self):
        real = Path.home()

        def listing():
            return {str(path): path.lstat().st_mtime_ns for directory in (real / '.claude', real / '.config/pipeline')
                    if directory.is_dir() for path in directory.iterdir()}

        before = listing()
        self.doctor()
        self.assertEqual(listing(), before)
        line, _ = self.check('kit skills/plan-harden')
        self.assertIn(str(self.kit / 'skills/plan-harden'), line)


class ToolsTest(DoctorCase):
    def test_each_tool_on_path(self):
        for name in ('claude', 'node', 'git', 'jq', 'sbx'):
            self.assertPass(f'tool {name}', str(self.bin / name))

    def test_a_missing_tool_fails_alone_with_its_install_fix(self):
        (self.bin / 'jq').unlink()
        self.assertFail('tool jq', 'not on PATH', 'sudo apt install jq (INSTALL: What you need)')
        self.assertPass('tool node', str(self.bin / 'node'))

    def test_the_sbx_fix_names_step_1(self):
        (self.bin / 'sbx').unlink()
        self.assertFail('tool sbx', 'not on PATH', 'install the docker-sbx package (INSTALL step 1)')


class KvmTest(DoctorCase):
    def test_in_the_group(self):
        self.assertPass('kvm group', 'you are in it')

    def test_primary_group_counts(self):
        with mock.patch('os.getgroups', return_value=[100]), mock.patch('os.getgid', return_value=108):
            self.assertPass('kvm group', 'you are in it')

    def test_not_in_the_group(self):
        with mock.patch('os.getgroups', return_value=[100]):
            self.assertFail('kvm group', 'you are not in it',
                            'sudo usermod -aG kvm "$USER", then log out and back in (INSTALL step 1)')

    def test_no_kvm_group(self):
        with mock.patch('grp.getgrnam', side_effect=KeyError('kvm')):
            self.assertFail('kvm group', 'this box has no kvm group',
                            'check that /dev/kvm exists (hardware virtualization on), then install docker-sbx '
                            '(INSTALL step 1)')


class DockerTest(DoctorCase):
    FIX = 'start Docker and let your user run it without sudo (INSTALL: What you need)'

    def test_daemon_answers(self):
        self.assertPass('docker', 'server 29.8.1')

    def test_daemon_refuses(self):
        self.change(docker={'exit': 1, 'stderr': 'Client: ...\npermission denied while trying to connect'})
        self.assertFail('docker', 'permission denied while trying to connect', self.FIX)

    def test_no_answer(self):
        self.change(docker={'sleep': 5})

        with mock.patch.object(core, 'DOCTOR_TIMEOUT_SECONDS', 1):
            self.assertFail('docker', 'no answer in 1 s', self.FIX)

    def test_no_binary(self):
        (self.bin / 'docker').unlink()
        line, after = self.check('docker')
        self.assertTrue(line.startswith('FAIL  docker: '), line)
        self.assertEqual(after, f'      fix: {self.FIX}')


class RunnerTest(DoctorCase):
    def test_at_the_newest_tag(self):
        self.assertPass('runner', 'v0.1.0, the newest local tag (`git fetch --tags` looks for newer)')

    def test_an_older_tag_checked_out(self):
        git(self.runner, 'commit', '-q', '--allow-empty', '-m', 'two')
        git(self.runner, 'tag', 'v0.2.0')
        git(self.runner, 'checkout', '-q', 'v0.1.0')
        self.assertFail('runner', 'at v0.1.0, the newest local tag is v0.2.0',
                        f'when `pipeline status` shows no running stage: git -C {self.runner} checkout v0.2.0 '
                        '(INSTALL Update)')

    def test_several_tags_on_the_head_commit(self):
        git(self.runner, 'commit', '-q', '--allow-empty', '-m', 'two')
        for tag in ('v0.2', 'v1.0', 'v0.10'):  # describe names v0.10 here: only a commit comparison passes
            git(self.runner, 'tag', tag)
        self.assertPass('runner', 'v1.0, the newest local tag (`git fetch --tags` looks for newer)')

    def test_no_tags(self):
        git(self.runner, 'tag', '-d', 'v0.1.0')
        self.assertPass('runner', 'no release tags (INSTALL step 2 checks out main then)')

    def test_not_a_checkout(self):
        shutil.rmtree(self.runner / '.git')
        self.assertFail('runner', f'{self.runner} is not a git checkout', 'clone it as INSTALL step 2 says')


class KitTest(DoctorCase):
    def test_every_entry_links_into_the_kit(self):
        _, lines = self.doctor()
        self.assertNotIn('__pycache__', '\n'.join(lines))

        for item in backend.KIT_ITEMS:
            for entry in (self.kit / item).iterdir():
                if entry.name != '__pycache__':
                    self.assertIn(f'PASS  kit {item}/{entry.name}: links to {entry}', lines)

    def test_missing(self):
        (self.home / '.claude/skills/wrap-up').unlink()
        self.assertFail('kit skills/wrap-up', 'not linked',
                        f'ln -s {self.kit}/skills/wrap-up {self.home}/.claude/skills/')

    def test_linked_elsewhere(self):
        link = self.home / '.claude/agents/web-researcher.md'
        link.unlink()
        link.symlink_to('/tmp/elsewhere/web-researcher.md')
        self.assertFail('kit agents/web-researcher.md', 'links to /tmp/elsewhere/web-researcher.md',
                        f'ln -sfn {self.kit}/agents/web-researcher.md {link}')

    def test_your_own_copy(self):
        link = self.home / '.claude/skills/pr-text'
        link.unlink()
        link.mkdir()
        self.assertFail('kit skills/pr-text', 'your own copy, not the kit\'s',
                        f'move {link} aside, then ln -s {self.kit}/skills/pr-text {self.home}/.claude/skills/')


class UnitsTest(DoctorCase):
    NO_FILE = {'stderr': 'Failed to get unit file state for x: No such file or directory'}
    NO_BUS = {'stderr': 'Failed to connect to bus: No medium found'}
    BUS_FIX = ('run doctor as yourself in a login session, not through sudo or su: systemctl --user cannot reach '
               'your user manager here')

    def enabled(self, **states):
        self.change(enabled=dict(GOOD_STATE['enabled'], **states))

    def test_sbx_service_enabled_and_active(self):
        self.assertPass('unit sbx.service', 'enabled, active')

    def test_sbx_service_disabled(self):
        self.enabled(**{'sbx.service': 'disabled'})
        self.assertFail('unit sbx.service', 'disabled, active', 'systemctl --user enable --now sbx.service')

    def test_sbx_service_enabled_but_inactive(self):
        self.change(active=False)
        self.assertFail('unit sbx.service', 'enabled, inactive', 'systemctl --user enable --now sbx.service')

    def test_sbx_service_not_found(self):
        for state in ('not-found', '', self.NO_FILE):
            self.enabled(**{'sbx.service': state})
            self.change(active=False)
            self.assertFail('unit sbx.service', 'not installed, inactive',
                            f'mkdir -p ~/.config/systemd/user && cp {self.runner}/systemd/sbx.service '
                            '~/.config/systemd/user/, set MemoryHigh, MemoryMax and CPUQuota in it to fit this box, '
                            'then systemctl --user daemon-reload && systemctl --user enable --now sbx.service '
                            '(INSTALL step 4)')

    def test_no_user_manager_is_not_read_as_not_installed(self):
        self.enabled(**{'sbx.service': self.NO_BUS, 'pipeline-tick.service': self.NO_BUS,
                        'pipeline-tick.timer': self.NO_BUS})
        self.change(active=False)
        error = self.NO_BUS['stderr']
        self.assertFail('unit sbx.service', f'{error}, inactive', self.BUS_FIX)
        self.assertFail('unit pipeline-tick.service', error, self.BUS_FIX)
        self.assertFail('unit pipeline-tick.timer', error, self.BUS_FIX)

    def test_tick_service_states(self):
        for state in ('static', 'enabled', 'disabled', 'indirect', 'linked', 'alias'):
            self.enabled(**{'pipeline-tick.service': state})
            self.assertPass('unit pipeline-tick.service', state)

    def test_tick_service_masked(self):
        self.enabled(**{'pipeline-tick.service': 'masked'})
        self.assertFail('unit pipeline-tick.service', 'masked', 'systemctl --user unmask pipeline-tick.service')

    def test_tick_service_missing(self):
        for state in ('not-found', '', self.NO_FILE):
            self.enabled(**{'pipeline-tick.service': state})
            self.assertFail('unit pipeline-tick.service', 'not installed',
                            f'cp {self.runner}/systemd/pipeline-tick.service ~/.config/systemd/user/, then '
                            'systemctl --user daemon-reload (INSTALL step 9)')

    def test_timer_enabled(self):
        self.assertPass('unit pipeline-tick.timer', 'enabled')

    def test_timer_disabled(self):
        self.enabled(**{'pipeline-tick.timer': 'disabled'})
        self.assertFail('unit pipeline-tick.timer', 'disabled', 'systemctl --user enable --now pipeline-tick.timer')

    def test_timer_not_found(self):
        self.enabled(**{'pipeline-tick.timer': 'not-found'})
        self.assertFail('unit pipeline-tick.timer', 'not installed',
                        f'cp {self.runner}/systemd/pipeline-tick.timer ~/.config/systemd/user/, then systemctl --user '
                        'daemon-reload && systemctl --user enable --now pipeline-tick.timer (INSTALL step 9)')


class LingerTest(DoctorCase):
    FIX = 'loginctl enable-linger "$USER" (INSTALL step 4)'

    def test_on(self):
        self.assertPass('linger', 'on')

    def test_off(self):
        self.change(linger='no')
        self.assertFail('linger', 'off', self.FIX)

    def test_loginctl_fails(self):
        self.change(linger=None)
        self.assertFail('linger', 'Failed to get user: User is not logged in or lingering', self.FIX)


class AppKeyTest(DoctorCase):
    def test_mode_600(self):
        self.assertPass('app key', f'{self.key}, mode 0o600')

    def test_mode_400(self):
        self.key.chmod(0o400)
        self.assertPass('app key', f'{self.key}, mode 0o400')

    def test_readable_by_others(self):
        self.key.chmod(0o644)
        self.assertFail('app key', f'the GitHub App key {self.key} is mode 0o644: chmod 600 it', f'chmod 600 {self.key}')

    def test_missing(self):
        self.key.unlink()
        line, after = self.check('app key')
        self.assertTrue(line.startswith(f'FAIL  app key: the GitHub App key {self.key} is missing'), line)
        self.assertEqual(after, f'      fix: generate a private key in the App settings and save it as {self.key} '
                                '(INSTALL step 5)')

    def test_the_configured_key_is_checked(self):
        other = self.base / 'keys/other.pem'
        other.parent.mkdir()
        other.write_text('key')
        other.chmod(0o644)
        self.config({}, key=other)
        self.assertFail('app key', f'the GitHub App key {other} is mode 0o644: chmod 600 it', f'chmod 600 {other}')

    def test_without_a_config_app_pem_is_checked(self):
        self.pipeline_config.unlink()
        self.key.chmod(0o640)
        self.assertFail('app key', f'the GitHub App key {self.key} is mode 0o640: chmod 600 it', f'chmod 600 {self.key}')


class ConfigTest(DoctorCase):
    def test_loads(self):
        self.assertPass('config', str(self.pipeline_config))

    def test_missing(self):
        self.pipeline_config.unlink()
        self.assertFail('config', f'no {self.pipeline_config}: it names the GitHub App and the repos the pipeline '
                                  'serves',
                        f'cp {self.runner}/examples/config.example.json {self.pipeline_config}, then edit it '
                        '(INSTALL step 6)')

    def test_not_json(self):
        self.pipeline_config.write_text('{')
        line, after = self.check('config')
        self.assertTrue(line.startswith(f'FAIL  config: {self.pipeline_config}: not JSON'), line)
        self.assertEqual(after, f'      fix: edit {self.pipeline_config} (INSTALL step 6)')

    def test_a_missing_key(self):
        self.pipeline_config.write_text(json.dumps({'app': {'id': 1}, 'repos': {}}))
        self.assertFail('config', f'{self.pipeline_config} lacks app.slug, app.privateKey, app.reviewer, repos',
                        f'edit {self.pipeline_config} (INSTALL step 6)')


    def test_not_an_object(self):
        self.pipeline_config.write_text('[]')
        self.assertFail('config', f'{self.pipeline_config}: not a JSON object',
                        f'edit {self.pipeline_config} (INSTALL step 6)')


class ReserveTest(DoctorCase):
    def host(self, value):
        self.host_config.parent.mkdir(parents=True, exist_ok=True)
        self.host_config.write_text(value if isinstance(value, str) else json.dumps(value))

    def test_defaults(self):
        self.assertPass('sbx-host reserve', 'pl 8704 MiB ≥ 8192 MiB (vm.memoryGb 8 × 1024)')

    def test_a_tuned_box(self):
        self.host({'reserve': {'pl': 8512}})
        self.assertPass('sbx-host reserve', 'pl 8512 MiB ≥ 8192 MiB (vm.memoryGb 8 × 1024)')

    def test_a_bigger_vm(self):
        self.config({'vm': {'memoryGb': 10}})
        self.assertFail('sbx-host reserve', 'pl 8704 MiB < 10240 MiB (vm.memoryGb 10 × 1024)',
                        f'set "reserve": {{"pl": 10752}} in {self.host_config} (at least 10240)')

    def test_not_a_number(self):
        for value in ('8704', True, None):
            self.host({'reserve': {'pl': value}})
            self.assertFail('sbx-host reserve', f'pl is {value!r}, not a number of MiB',
                            f'set "reserve": {{"pl": 8704}} in {self.host_config} (at least 8192)')

    def test_unreadable(self):
        self.host('{')
        line, after = self.check('sbx-host reserve')
        self.assertTrue(line.startswith(f'FAIL  sbx-host reserve: {self.host_config}: unreadable'), line)
        self.assertEqual(after, f'      fix: edit {self.host_config}')

    def test_not_an_object(self):
        for value, detail in (([], 'not a JSON object'), ({'reserve': 'x'}, "reserve is an object, not 'x'"),
                              ({'reserve': [8192]}, 'reserve is an object, not [8192]')):
            self.host(value)
            self.assertFail('sbx-host reserve', f'{self.host_config}: {detail}', f'edit {self.host_config}')

    def test_skipped_without_a_pipeline_config(self):
        self.pipeline_config.write_text('{')
        line, after = self.check('sbx-host reserve')
        self.assertEqual(line, 'SKIP  sbx-host reserve: needs config.json to load')


if __name__ == '__main__':
    unittest.main()
