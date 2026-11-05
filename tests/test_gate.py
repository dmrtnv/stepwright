"""Gate tests 4 and 5 against the suite's fake sbx, with a curl that obeys each fake sandbox's own rules.

Run: python3 -m unittest discover tests
"""
import contextlib
import io
import json
import os
import re
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
sys.path.insert(0, str(TESTS.parent / 'sandbox'))

import backend  # noqa: E402
import core  # noqa: E402
import gate  # noqa: E402
from test_core import FAKE_SBX, REAL_KIT, fixture_kit, relative_files  # noqa: E402

# 200 when the calling sandbox holds an allow rule for the URL's host, else what a blocked sbx proxy gives curl.
RULE_CURL = r'''#!/usr/bin/python3
import json, os, pathlib, sys, urllib.parse

url = next(arg for arg in sys.argv[1:] if arg.startswith(('http://', 'https://')))
host = urllib.parse.urlsplit(url).hostname
meta = json.loads((pathlib.Path(os.environ['FAKE_SBX_ROOT']) / 'meta.json').read_text())

if ['allow', host] in meta[os.environ['FAKE_SBX_NAME']]['rules']:
    sys.stdout.write('ok\n200')
    sys.exit(0)

sys.stdout.write('\n000')
print('curl: (56) blocked by sandbox policy', file=sys.stderr)
sys.exit(56)
'''

NO_EGRESS = {'setup': [], 'setupEgress': [], 'checkFast': []}


class GateEgressCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='gate-test-'))
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.base)])
        self.root = self.base / 'sbx'
        bin_dir = self.base / 'bin'
        bin_dir.mkdir()

        for name, text in (('sbx', FAKE_SBX), ('curl', RULE_CURL)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)

        meta = {}

        for name in (gate.SANDBOX_A, gate.SANDBOX_B):
            (self.root / name / 'home').mkdir(parents=True)
            (self.root / name / 'workspace').mkdir(parents=True)
            meta[name] = {'status': 'running', 'source': str(self.base), 'rules': []}

        (self.root / 'meta.json').write_text(json.dumps(meta))
        env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', FAKE_SBX_ROOT=str(self.root))

        for name in ('FAKE_POLICY_ALLOW_FAIL', 'FAKE_POLICY_RM_FAIL', 'FAKE_EGRESS'):
            env.pop(name, None)

        self.workspace = str(self.root / gate.SANDBOX_A / 'workspace')
        self.results = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(mock.patch.object(gate, 'SBX_ENV', env))
        stack.enter_context(mock.patch.object(gate, 'results', self.results))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def calls(self, offset=0):
        lines = (self.root / 'calls.jsonl').read_text().splitlines() if (self.root / 'calls.jsonl').exists() else []

        return [json.loads(line) for line in lines[offset:]]

    def rules(self, name):
        return json.loads((self.root / 'meta.json').read_text())[name]['rules']

    def failures(self):
        return [r for r in self.results if r['status'] == 'FAIL']

    def status(self, check):
        return [r['status'] for r in self.results if r['check'] == check]

    def policy_calls(self, calls, *verbs):
        return [call for call in calls if call[:1] == ['policy'] and call[1] in verbs]

    def test_rules_test_uses_its_own_rule_and_removes_it(self):
        gate.test_5_rules()

        self.assertEqual(self.failures(), [])
        self.assertEqual(self.status('rule removed, live without restart'), ['PASS'])
        calls = self.calls()
        self.assertIn(['policy', 'allow', 'network', '--sandbox', gate.SANDBOX_A, 'registry.npmjs.org'], calls)
        self.assertIn(['policy', 'rm', 'network', '--sandbox', gate.SANDBOX_A, '--resource', 'registry.npmjs.org',
                       '--force'], calls)
        self.assertEqual(self.rules(gate.SANDBOX_A), [])
        self.assertEqual(self.rules(gate.SANDBOX_B), [])
        probes = [call for call in calls if call[:1] == ['exec'] and 'curl' in ' '.join(call)]
        self.assertEqual(len(probes), 4, 'every probe went through the fake sbx')

    def test_no_egress_calls_no_policy_and_no_empty_resource(self):
        gate.test_5_rules()
        offset = len(self.calls())
        gate.test_4_setup_and_checks(NO_EGRESS, self.workspace, True)

        self.assertEqual(self.failures(), [])
        self.assertEqual(self.status('setup'), ['PASS'])
        self.assertEqual(self.status('setup egress sealed'), ['PASS'])
        self.assertEqual(self.status('setup egress removed'), ['PASS'])
        self.assertEqual(self.policy_calls(self.calls(offset), 'allow', 'deny', 'rm'), [])

        for call in self.policy_calls(self.calls(), 'allow', 'deny'):
            self.assertTrue(all(call[-1].split(',')), call)

        for call in self.policy_calls(self.calls(), 'rm'):
            self.assertTrue(all(call[call.index('--resource') + 1].split(',')), call)

    def test_setup_egress_is_allowed_then_removed(self):
        config = dict(NO_EGRESS, setup=['true'], setupEgress=['registry.npmjs.org', 'pypi.org'])
        gate.test_4_setup_and_checks(config, self.workspace, True)

        self.assertEqual(self.failures(), [])
        calls = self.calls()
        self.assertEqual(self.policy_calls(calls, 'allow'),
                         [['policy', 'allow', 'network', '--sandbox', gate.SANDBOX_A, 'registry.npmjs.org,pypi.org']])
        removed = [call[call.index('--resource') + 1] for call in self.policy_calls(calls, 'rm')]
        self.assertEqual(removed, ['registry.npmjs.org', 'pypi.org'])
        self.assertEqual(self.rules(gate.SANDBOX_A), [])
        self.assertIn('INFO', self.status('setup: true → exit 0'))

    def test_a_failed_setup_allow_fails_setup(self):
        os.environ['FAKE_POLICY_ALLOW_FAIL'] = '1'
        config = dict(NO_EGRESS, setup=['touch ran'], setupEgress=['pypi.org'])
        gate.test_4_setup_and_checks(config, self.workspace, True)

        self.assertEqual(self.status('setup'), ['FAIL'])
        self.assertFalse(os.path.exists(os.path.join(self.workspace, 'ran')), 'setup ran without its egress')
        self.assertEqual(self.status('policy log'), ['INFO'])

    def test_a_failed_setup_command_fails_setup(self):
        config = dict(NO_EGRESS, setup=['echo broken >&2; exit 3', 'touch ran'])
        gate.test_4_setup_and_checks(config, self.workspace, True)

        self.assertEqual(self.status('setup'), ['FAIL'])
        self.assertIn('broken', [r for r in self.results if r['check'] == 'setup'][0]['detail'])
        self.assertFalse(os.path.exists(os.path.join(self.workspace, 'ran')), 'setup stops at the first failure')

    def test_setup_label_is_short_and_keeps_the_exit_code(self):
        command = 'echo ' + 'x' * 200 + '\nexit 3'
        gate.test_4_setup_and_checks(dict(NO_EGRESS, setup=[command]), self.workspace, True)

        labels = [r['check'] for r in self.results if r['check'].startswith('setup: ')]
        self.assertEqual(len(labels), 1, labels)
        self.assertTrue(labels[0].endswith(' → exit 3'), labels[0])
        self.assertLessEqual(len(labels[0]), 100, labels[0])
        self.assertNotIn('\n', labels[0])

    def test_a_failed_egress_removal_fails_the_seal(self):
        os.environ['FAKE_POLICY_RM_FAIL'] = '1'
        config = dict(NO_EGRESS, setup=['true'], setupEgress=['pypi.org'])
        gate.test_4_setup_and_checks(config, self.workspace, True)

        self.assertEqual(self.status('setup'), ['PASS'])
        self.assertEqual(self.status('setup egress sealed'), ['FAIL'])

    def test_a_visible_host_path_fails_and_a_hidden_one_passes(self):
        present = self.base / 'present'
        present.write_text('secret')
        gate.expect_hidden(8, {'present': str(present), 'absent': str(self.base / 'absent')})

        self.assertEqual(self.status('present invisible'), ['FAIL'])
        self.assertEqual(self.status('absent invisible'), ['PASS'])

    def test_a_failed_exec_fails_every_hidden_probe(self):
        meta = json.loads((self.root / 'meta.json').read_text())
        del meta[gate.SANDBOX_A]
        (self.root / 'meta.json').write_text(json.dumps(meta))
        gate.expect_hidden(8, {'present': str(self.base), 'absent': str(self.base / 'absent')})

        self.assertEqual(self.status('present invisible'), ['FAIL'])
        self.assertEqual(self.status('absent invisible'), ['FAIL'])

    def test_a_path_under_the_vm_home_is_not_checked(self):
        gate.expect_hidden(8, {'vm home': f'{backend.HOME_TEMPLATE}/.ssh'})

        self.assertEqual(self.status('vm home invisible'), ['INFO'])
        self.assertEqual([call for call in self.calls() if call[:1] == ['exec']], [])

    def test_fake_sbx_rejects_an_empty_resource(self):
        result = subprocess.run(['sbx', 'policy', 'allow', 'network', '--sandbox', gate.SANDBOX_A, ''],
                                capture_output=True, text=True, env=os.environ)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('RESOURCES', result.stderr)
        self.assertEqual(self.rules(gate.SANDBOX_A), [])


class PreflightCase(unittest.TestCase):
    """Preflight with sbx, the template and kvm faked present, so only the checkout's recipe decides."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='gate-preflight-')
        self.addCleanup(subprocess.run, ['rm', '-rf', self.repo])
        os.makedirs(os.path.join(self.repo, '.git'))
        os.makedirs(os.path.join(self.repo, '.claude'))
        self.recipe = os.path.join(self.repo, '.claude', 'pipeline.json')
        self.argvs = []

        def fake_run(argv, *args, **kwargs):
            self.argvs.append(argv)
            out = {'template': 'pipeline 1\n', 'id': 'agent kvm\n'}.get(argv[1] if argv[0] == 'sbx' else argv[0], '')

            return subprocess.CompletedProcess(argv, 0, out, '')

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(gate.shutil, 'which', lambda name: f'/usr/bin/{name}'))
        stack.enter_context(mock.patch.object(gate, 'run', fake_run))
        stack.enter_context(mock.patch.object(gate, 'template_tag', lambda: 'pipeline:1'))

    def write(self, text):
        with open(self.recipe, 'w') as handle:
            handle.write(text)

    def test_a_recipe_that_is_not_json_fails_preflight(self):
        self.write('{"base": "main",')
        stderr = io.StringIO()

        with mock.patch.object(sys, 'argv', ['gate.py', '--repo', self.repo]), contextlib.redirect_stderr(stderr):
            code = gate.main()

        self.assertEqual(code, 2)
        self.assertIn('preflight failed:', stderr.getvalue())
        self.assertIn(f'{self.repo}: .claude/pipeline.json is not JSON', stderr.getvalue())
        self.assertNotIn('create', [argv[1] for argv in self.argvs if argv[0] == 'sbx'])

    def test_a_recipe_the_runner_rejects_fails_preflight(self):
        recipe = json.loads((TESTS.parent / 'examples' / 'pipeline.python.json').read_text())
        del recipe['ci']
        self.write(json.dumps(recipe))

        self.assertEqual(gate.preflight(self.repo, 'pipeline:1'),
                         ([f'{self.repo} .claude/pipeline.json: missing key ci'], None))

    def test_a_valid_recipe_passes_preflight_validated(self):
        preset = (TESTS.parent / 'examples' / 'pipeline.python.json').read_text()
        self.write(preset)
        problems, config = gate.preflight(self.repo, 'pipeline:1')

        self.assertEqual(problems, [])
        self.assertEqual(config['gateConfig'], [])
        self.assertEqual(config['checkFast'], json.loads(preset)['checkFast'])

    def test_an_unreadable_recipe_fails_preflight(self):
        os.makedirs(self.recipe)
        problems, config = gate.preflight(self.repo, 'pipeline:1')

        self.assertEqual(len(problems), 1)
        self.assertIn(self.recipe, problems[0])
        self.assertIsNone(config)

    def test_a_missing_recipe_still_names_the_presets(self):
        problems, config = gate.preflight(self.repo, 'pipeline:1')

        self.assertEqual(problems, [f'{self.repo} has no .claude/pipeline.json '
                                    f'(presets: {backend.PIPELINE_DIR}/examples/pipeline.*.json)'])
        self.assertIsNone(config)


class KitCopyCase(unittest.TestCase):
    """Gate test 6 copies the kit that stages get: the runner's kit dir plus the user's own skills and agents."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='gate-kit-test-'))
        self.addCleanup(shutil.rmtree, self.base)
        self.home = self.base / 'home'
        (self.home / '.claude/skills/plan-harden').mkdir(parents=True)
        (self.home / '.claude/skills/plan-harden/SKILL.md').write_text('# stale\n')
        (self.home / '.claude/skills/pipeline-feature').mkdir()
        (self.home / '.claude/skills/pipeline-feature/SKILL.md').write_text('# host only\n')
        (self.home / '.claude/agents').mkdir()
        (self.home / '.claude/agents/web-researcher.md').write_text('# stale\n')
        (self.home / '.claude/CLAUDE.md').write_text('# rules\n')
        (self.home / '.claude/rules/sub').mkdir(parents=True)
        (self.home / '.claude/rules/code.md').write_text('# code\n')
        (self.home / '.claude/rules/sub/b.md').write_text('# b\n')
        (self.home / '.claude/rules/__pycache__').mkdir()
        (self.home / '.claude/rules/__pycache__/a.pyc').write_text('bytecode')
        self.captured = self.base / 'captured'
        self.captured.mkdir()
        self.checkout = self.base / 'checkout'
        self.checkout.mkdir()
        self.projects = self.home / '.claude/projects'
        self.make_memory_code = 0
        self.settings = ''
        self.argvs = []
        self.results = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(gate, 'HOME', str(self.home)))
        stack.enter_context(mock.patch.object(gate, 'results', self.results))
        stack.enter_context(mock.patch.object(gate, 'run', self.fake_run))
        stack.enter_context(mock.patch.object(core, 'PROJECTS', self.projects))
        stack.enter_context(mock.patch.multiple(backend, HOME=self.home, KIT=fixture_kit(self.base)))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def fake_run(self, argv, timeout=120, check=False, **kwargs):
        self.argvs.append(argv)

        if argv[:3] == ['sbx', 'cp', '-L']:
            source = Path(argv[3])
            (shutil.copytree if source.is_dir() else shutil.copy)(source, self.captured / source.name)

        script = argv[-1] if argv[:2] == ['sbx', 'exec'] else ''

        if script == f'ls {gate.VM_MEMORY} | wc -l':
            memory = self.captured / 'memory'
            count = len(os.listdir(memory)) if memory.is_dir() else 0

            return subprocess.CompletedProcess(argv, 0, f'{count}\n', '')

        if script == f'rm -rf {gate.VM_MEMORY} && mkdir -p {gate.VM_MEMORY}':
            return subprocess.CompletedProcess(argv, self.make_memory_code, '', '')

        if script.startswith("cat > /home/agent/.claude/settings.json <<'EOF'\n"):
            self.settings = script.split('\n')[1]

        if script == 'cat /home/agent/.claude/settings.json':
            return subprocess.CompletedProcess(argv, 0, self.settings, '')

        return subprocess.CompletedProcess(argv, 0, '', '')

    def scripts(self):
        return [argv[-1] for argv in self.argvs if argv[:2] == ['sbx', 'exec']]

    def status(self, check):
        return [result['status'] for result in self.results if result['check'] == check]

    def failures(self):
        return [result for result in self.results if result['status'] == 'FAIL']

    def test_the_gate_copies_the_runners_kit(self):
        gate.copy_kit(gate.SANDBOX_A, self.checkout)
        skills = self.captured / 'skills'
        self.assertNotIn('pipeline-feature', {path.name for path in skills.iterdir()})
        self.assertEqual((skills / 'plan-harden/SKILL.md').read_text(),
                         (REAL_KIT / 'skills/plan-harden/SKILL.md').read_text())
        self.assertEqual((self.captured / 'agents/web-researcher.md').read_text(), '# stale\n',
                         'your own convention agent wins')
        self.assertEqual((self.captured / 'CLAUDE.md').read_text(), '# rules\n')
        copies = {result['check']: result['status'] for result in self.results if result['check'].startswith('sbx cp ')}
        self.assertEqual(copies, {'sbx cp CLAUDE.md': 'PASS', 'sbx cp skills': 'PASS', 'sbx cp agents': 'PASS',
                                  'sbx cp system.md': 'PASS', 'sbx cp rules': 'PASS'})
        rules = self.captured / 'rules'
        self.assertEqual(relative_files(rules), {'code.md', 'sub/b.md'})
        self.assertEqual((rules / 'code.md').read_text(), '# code\n')
        self.assertEqual((rules / 'sub/b.md').read_text(), '# b\n')

    def test_the_gate_copies_no_rules_without_a_rules_dir(self):
        shutil.rmtree(self.home / '.claude/rules')
        gate.copy_kit(gate.SANDBOX_A, self.checkout)
        checks = [result['check'] for result in self.results]
        self.assertEqual([check for check in checks if check.startswith('sbx cp rules')], [])
        self.assertFalse((self.captured / 'rules').exists())
        failed = [result['check'] for result in self.results
                  if result['check'].startswith('sbx cp ') and result['status'] == 'FAIL']
        self.assertEqual(failed, [])

    def test_no_claude_md_is_info_and_not_copied(self):
        (self.home / '.claude/CLAUDE.md').unlink()
        gate.copy_kit(gate.SANDBOX_A, self.checkout)

        self.assertEqual(self.status('CLAUDE.md'), ['INFO'])
        self.assertNotIn('sbx cp CLAUDE.md', [result['check'] for result in self.results])
        self.assertFalse((self.captured / 'CLAUDE.md').exists())
        self.assertEqual(self.failures(), [])

    def test_no_memory_dir_makes_an_empty_vm_memory_dir(self):
        gate.copy_kit(gate.SANDBOX_A, self.checkout)

        self.assertIn('rm -rf /home/agent/memory && mkdir -p /home/agent/memory', self.scripts())
        self.assertEqual(self.status('repo memory'), ['INFO'])
        self.assertEqual(self.failures(), [])

    def test_a_failed_empty_memory_dir_fails(self):
        self.make_memory_code = 1
        gate.copy_kit(gate.SANDBOX_A, self.checkout)

        self.assertEqual([result['check'] for result in self.failures()], ['make empty VM memory dir'])

    def test_a_checkout_with_a_dot_and_an_underscore_finds_its_memory(self):
        checkout = self.base / 'my.repo_x'
        checkout.mkdir()
        key = re.sub(r'[^A-Za-z0-9]', '-', str(self.base.resolve())) + '-my-repo-x'
        memory = self.projects / key / 'memory'
        memory.mkdir(parents=True)
        (memory / 'MEMORY.md').write_text('- one\n')
        (memory / 'one.md').write_text('one\n')
        gate.copy_kit(gate.SANDBOX_A, checkout)

        self.assertEqual(relative_files(self.captured / 'memory'), {'MEMORY.md', 'one.md'})
        self.assertEqual(self.status('sbx cp repo memory'), ['PASS'])
        self.assertEqual(self.failures(), [])


if __name__ == '__main__':
    unittest.main()
