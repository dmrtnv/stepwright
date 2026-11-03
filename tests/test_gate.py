"""Gate tests 4 and 5 against the suite's fake sbx, with a curl that obeys each fake sandbox's own rules.

Run: python3 -m unittest discover tests
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / 'sandbox'))

import gate  # noqa: E402
from test_core import FAKE_SBX  # noqa: E402

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

    def test_fake_sbx_rejects_an_empty_resource(self):
        result = subprocess.run(['sbx', 'policy', 'allow', 'network', '--sandbox', gate.SANDBOX_A, ''],
                                capture_output=True, text=True, env=os.environ)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('RESOURCES', result.stderr)
        self.assertEqual(self.rules(gate.SANDBOX_A), [])


if __name__ == '__main__':
    unittest.main()
