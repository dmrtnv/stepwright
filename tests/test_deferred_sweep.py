"""`pipeline deferred sweep`: one tool-free Claude call per finished feature judges its open deferred items, and the
runner closes, files or leaves open each one on its own evidence.

Run: python3 -m unittest discover tests
"""
import json
import os
import signal
import sys
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FEATURE, REPO, write  # noqa: E402
from test_deferred_command import DeferredCommandCase, deferred  # noqa: E402

import core  # noqa: E402

FAKE_JUDGE = r'''#!/usr/bin/python3
import json, os, pathlib, sys, time

base = pathlib.Path(os.environ['FAKE_JUDGE_DIR'])
calls = base / 'judge-calls.jsonl'
previous = len(calls.read_text().splitlines()) if calls.exists() else 0
stdin = sys.stdin.read()

with calls.open('a') as handle:
    handle.write(json.dumps({'argv': sys.argv[1:], 'stdin': stdin, 'cwd': os.getcwd(), 'pid': os.getpid()}) + '\n')

runs = json.loads((base / 'judge-script.json').read_text())

for step in (runs[min(previous, len(runs) - 1)] if runs else []):
    if 'sleep' in step:
        time.sleep(step['sleep'])
    elif 'touch' in step:
        pathlib.Path(step['touch']).write_text('x')
    elif 'exit' in step:
        sys.exit(step['exit'])
    else:
        print(json.dumps(step), flush=True)
'''

INIT = {'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': [],
        'model': 'claude-opus-5-5'}


def rate_limit(weekly=0.3):
    return {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
        'five_hour': {'utilization': 0.1, 'resetsAt': int(time.time()) + 3600},
        'seven_day': {'utilization': weekly, 'resetsAt': int(time.time()) + 86400}}}}


def result(verdicts, cost=0.25):
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '',
            'structured_output': {'verdicts': verdicts}, 'total_cost_usd': cost}


def answering(*verdicts, cost=0.25):
    return [INIT, rate_limit(), result(list(verdicts), cost)]


def answer(item_id, verdict, note='why', **fields):
    return {'id': item_id, 'verdict': verdict, 'note': note, **fields}


class JudgeCase(DeferredCommandCase):
    """A fake `claude` that records its argv, stdin and cwd and prints the events of the run the script names."""

    def setUp(self):
        super().setUp()
        write(self.fixture.bin / 'claude', FAKE_JUDGE, 0o755)
        environment = unittest.mock.patch.dict(os.environ, {'FAKE_JUDGE_DIR': str(self.fixture.base)})
        environment.start()
        self.addCleanup(environment.stop)
        self.runs()

    def runs(self, *runs):
        write(self.fixture.base / 'judge-script.json', json.dumps(list(runs)))

    def calls(self):
        path = self.fixture.base / 'judge-calls.jsonl'

        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assertGone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def when_called(self, action):
        """Runs `action` in a thread once the fake has started."""
        def wait():
            deadline = time.time() + 30

            while not self.calls() and time.time() < deadline:
                time.sleep(0.05)

            time.sleep(0.2)
            action()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)


class JudgeTest(JudgeCase):
    def setUp(self):
        super().setUp()
        self.log = self.fixture.base / 'sweep.log'

    def judge(self, prompt='the prompt'):
        return core.judge(prompt, self.log)

    def failure(self):
        with self.assertRaises(core.JudgeFailure) as caught:
            self.judge()

        return caught.exception

    def test_the_call_has_the_exact_argv_and_reads_the_prompt_on_stdin(self):
        self.runs(answering(answer('01.1', 'needs_owner')))
        prompt = 'π ' * 70_000
        self.assertEqual(self.judge(prompt), ([answer('01.1', 'needs_owner')], 0.25))
        call, = self.calls()
        self.assertEqual(call['argv'], [
            '-p', '--model', 'claude-opus-5-5', '--fallback-model', 'claude-sonnet-5-5', '--safe-mode', '--tools', '',
            '--strict-mcp-config', '--disallowedTools', 'mcp__*', '--json-schema', json.dumps(core.SWEEP_SCHEMA),
            '--no-session-persistence', '--max-budget-usd', '5', '--output-format', 'stream-json', '--verbose'])
        self.assertEqual(call['stdin'], prompt)
        self.assertEqual(call['cwd'], str(core.LOGS))

    def test_the_stream_goes_to_the_log_and_the_usage_reading_is_kept(self):
        self.runs(answering(answer('01.1', 'done'), cost=1.5)[:1] + [rate_limit(0.42)] +
                  answering(answer('01.1', 'done'), cost=1.5)[2:])
        self.assertEqual(self.judge()[1], 1.5)
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([line['type'] for line in lines], ['system', 'rate_limit_event', 'result'])
        self.assertEqual(core.read_usage()['seven_day']['utilization'], 0.42)

    def test_an_init_with_more_than_the_answer_tool_kills_the_call(self):
        marker = self.fixture.base / 'went-on'
        cases = {
            'Bash': dict(INIT, tools=['StructuredOutput', 'Bash']),
            'mcp__x__y': dict(INIT, tools=['StructuredOutput', 'mcp__x__y']),
            'MCP servers': dict(INIT, mcp_servers=[{'name': 'x', 'status': 'connected'}]),
            'null': dict(INIT, tools=None),
        }

        for reason, init in cases.items():
            with self.subTest(reason=reason):
                self.runs([init, {'sleep': 3}, {'touch': str(marker)}, result([answer('01.1', 'done')])])
                started = time.time()
                failure = self.failure()
                self.assertLess(time.time() - started, 2.5)
                self.assertIn(reason, str(failure))
                self.assertFalse(failure.noted)
                self.assertFalse(marker.exists())
                self.assertGone(self.calls()[-1]['pid'])

    def test_a_model_event_before_the_init_kills_the_call(self):
        marker = self.fixture.base / 'went-on'

        for kind in ('assistant', 'user', 'result'):
            with self.subTest(kind=kind):
                event = result([answer('01.1', 'done')]) if kind == 'result' else {'type': kind}
                self.runs([event, {'sleep': 3}, {'touch': str(marker)}, INIT, result([])])
                failure = self.failure()
                self.assertEqual(str(failure), f'the judging call sent a {kind} event before a good init event')
                self.assertFalse(failure.noted)
                self.assertFalse(marker.exists())

    def test_a_timeout_kills_the_call_and_is_noted(self):
        self.runs([INIT, {'sleep': 30}])
        started = time.time()

        with unittest.mock.patch.object(core, 'SWEEP_TIMEOUT_SECONDS', 1):
            failure = self.failure()

        self.assertLess(time.time() - started, 15)
        self.assertTrue(failure.noted)
        self.assertEqual(str(failure), 'the judging call timed out after 1s')
        self.assertGone(self.calls()[-1]['pid'])

    def test_an_interrupt_kills_the_call(self):
        self.runs([INIT, {'sleep': 30}])
        self.when_called(lambda: os.kill(os.getpid(), signal.SIGINT))
        started = time.time()

        with self.assertRaises(KeyboardInterrupt):
            self.judge()

        self.assertLess(time.time() - started, 15)
        self.assertGone(self.calls()[-1]['pid'])

    def test_each_failure_says_whether_it_would_repeat(self):
        error = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'result': 'API Error: 500'}
        budget = {'type': 'result', 'subtype': 'error_max_budget_usd', 'is_error': True, 'total_cost_usd': 5.2}
        cases = [
            ([INIT, error], 'error_during_execution: API Error: 500', False, 0.0),
            ([INIT, {'exit': 1}], 'claude exited 1 without a result', False, 0.0),
            ([{'exit': 0}], 'claude exited 0 without a result', False, 0.0),
            ([INIT, budget], 'the judging call spent its $5 budget', True, 5.2),
            ([INIT, dict(result([]), structured_output={'verdict': []})], 'the answer has no verdicts list', True,
             0.25),
            ([INIT, dict(result([]), structured_output=None)], 'the answer has no verdicts list', True, 0.25),
        ]

        for events, reason, noted, cost in cases:
            with self.subTest(reason=reason, events=len(events)):
                self.runs(events)
                failure = self.failure()
                self.assertEqual((str(failure), failure.noted, failure.cost), (reason, noted, cost))

    def test_a_missing_claude_is_a_failure_that_is_tried_again(self):
        (self.fixture.bin / 'claude').unlink()

        with unittest.mock.patch.object(core, 'CLAUDE', str(self.fixture.base / 'no-claude')):
            failure = self.failure()

        self.assertIn('cannot run', str(failure))
        self.assertFalse(failure.noted)


if __name__ == '__main__':
    unittest.main()
