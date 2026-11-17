"""`pipeline upkeep`: deferred sweep, archive and gc in that order, one result file, one ping, and a status badge.

Run: python3 -m unittest discover tests
"""
import io
import json
import subprocess
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FEATURE, REPO, write  # noqa: E402
from test_deferred_command import deferred  # noqa: E402
from test_deferred_sweep import SweepCase, answer, answering, brief  # noqa: E402

import core  # noqa: E402

DAY = 86400


class UpkeepCase(SweepCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        patch = unittest.mock.patch.object(core.urllib.request, 'urlopen',
                                           lambda request, timeout: self.sent.append(request) or io.BytesIO(b''))
        patch.start()
        self.addCleanup(patch.stop)
        write(core.NTFY_TOPIC, 'topic-x\n')
        prune = unittest.mock.patch.object(core, 'command_template_prune', lambda: 0)  # it asks claude its version
        prune.start()
        self.addCleanup(prune.stop)

    def upkeep(self, code=0):
        self.assertEqual(self.main('upkeep'), code, self.output + self.errors)

        return self.output.splitlines()

    def result(self):
        return json.loads(core.UPKEEP_RESULT.read_text()) if core.UPKEEP_RESULT.exists() else None

    def stages(self):
        return self.result()['stages']

    def pings(self):
        return [(request.get_header('Title'), request.get_header('Priority'), request.data.decode())
                for request in self.sent]

    def gone(self):
        """A merged feature whose one item the judge calls moot, and the search confirms it."""
        self.push({'src/old.py': 'x\n'})
        self.remove('src/old.py')
        self.seed(FEATURE, '01', [deferred('gone', 'src/old.py')])
        self.runs(answering(answer('01.1', 'moot')))

    def hold_for(self, name, seconds):
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/{name}.lock'), 'sleep', str(seconds)],
                                  start_new_session=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.5)

    def live(self, feature=FEATURE):
        return (self.root() / feature).is_dir()


class OrderTest(UpkeepCase):
    def test_the_stages_run_in_order_and_archive_settles(self):
        calls = []

        def sweep_all(report=None):
            calls.append('sweep')
            return [], 0.0

        def archive(**options):
            calls.append(('archive', options))
            return 0

        with unittest.mock.patch.multiple(core, sweep_all=sweep_all, command_archive=archive,
                                          command_gc=lambda: calls.append('gc') or 0):
            output = self.upkeep()

        self.assertEqual(calls, ['sweep', ('archive', {'settle': True}), 'gc'])
        self.assertEqual(output[-1], 'upkeep: sweep ok · archive ok · gc ok')
        self.assertEqual(self.stages(), {'sweep': 'ok', 'archive': 'ok', 'gc': 'ok'})
        self.assertLessEqual(abs(self.result()['at'] - time.time()), 5)
        self.assertEqual(self.sent, [])

    def test_a_failure_does_not_stop_the_next_stage(self):
        ran = []

        def sweep_all(report=None):
            raise core.PipelineError('the config broke')

        with unittest.mock.patch.multiple(core, sweep_all=sweep_all, command_archive=lambda settle: 1,
                                          command_gc=lambda: ran.append('gc') or 0):
            output = self.upkeep(code=1)

        self.assertEqual(ran, ['gc'])
        self.assertIn('sweep: failed, the config broke', self.errors)
        self.assertEqual(output[-1], 'upkeep: sweep failed · archive failed · gc ok')
        self.assertEqual(self.stages(), {'sweep': 'failed', 'archive': 'failed', 'gc': 'ok'})
        self.assertEqual(self.pings(), [('pipeline upkeep', 'high', 'sweep failed: pipeline deferred sweep says why\n'
                                                                    'archive failed: pipeline archive says why')])

    def test_extra_words_are_a_usage_error(self):
        self.assertEqual(self.main('upkeep', 'extra'), 2)
        self.assertEqual(self.errors, 'usage: pipeline upkeep\n')

    def test_help_lists_upkeep_under_admin(self):
        self.assertEqual(self.main('help'), 0)
        admin = self.output.split('\nADMIN\n')[1]
        self.assertIn('pipeline upkeep', admin)
        self.assertIn('upkeep', core.adapter.RESERVED)

    def test_a_second_run_refuses_while_one_holds_upkeep(self):
        self.hold_for('upkeep', 60)
        self.upkeep(code=1)
        self.assertIn('holds upkeep', self.errors)
        self.assertIsNone(self.result())


class SkipTest(UpkeepCase):
    def assertSkipped(self, stage, line=None, stream='output'):
        output = self.upkeep()
        self.assertEqual(self.stages()[stage], 'skipped')
        self.assertEqual(sum(value == 'skipped' for value in self.stages().values()), 1, self.stages())
        self.assertEqual(self.sent, [])

        if line:
            self.assertIn(line, output if stream == 'output' else self.errors)

        return output

    def test_a_held_sweep_lock_skips_the_sweep_and_the_rest_runs(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.hold_for('sweep', 60)
        self.assertSkipped('sweep', 'holds sweep', 'errors')
        self.assertEqual(self.calls(), [])

    def test_the_kill_switch_skips_the_judging_call_only(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        write(self.fixture.base / 'logs/STOP', '')
        self.assertSkipped('sweep', f'{FEATURE}: skipped, the kill switch is on')
        self.assertEqual(self.calls(), [])

    def test_a_usage_refusal_skips_the_sweep(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.usage(0.95)
        self.assertSkipped('sweep', f'{FEATURE}: skipped, weekly usage 95% ≥ 80%')
        self.assertEqual(self.calls(), [])

    def test_github_not_answering_skips_the_sweep(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])

        def outage(method, path, token, body=None):
            raise core.GitHubUnavailable(f'GitHub {method} {path}: 502')

        with unittest.mock.patch.object(core, 'github_api', outage):
            self.assertSkipped('sweep', f'{FEATURE}: skipped, GitHub not answering')

    def test_a_held_run_lock_skips_the_sweep(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.hold_for(f'run-shop-{FEATURE}', 60)
        self.assertSkipped('sweep', f'{FEATURE}: skipped, a run holds it')
        self.assertEqual(self.calls(), [])

    def test_sbx_down_skips_gc_without_calling_it(self):
        with unittest.mock.patch.multiple(core, sbx_service_active=lambda: False,
                                          command_gc=lambda: self.fail('gc ran while sbx is down')):
            self.assertSkipped('gc', f'gc: skipped, {core.SBX_UNIT} not active')

    def test_a_held_gc_lock_skips_gc(self):
        self.hold_for('gc', 60)
        self.assertSkipped('gc', 'holds gc', 'errors')

    def test_features_waiting_for_the_owner_alone_skip_the_sweep(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        write(core.sweep_file(REPO, FEATURE), json.dumps({'01.1': {'verdict': 'none', 'note': '', 'reason': 'x',
                                                                     'at': '2026-01-01T00:00:00Z'}}))
        self.assertSkipped('sweep', f'{FEATURE}: skipped, its 1 open item wait for /deferred-sweep {FEATURE}')

    def test_one_feature_waiting_does_not_hide_one_judged(self):
        self.seed('alpha', '01', [deferred('a', 'x.py')])
        write(core.sweep_file(REPO, 'alpha'), json.dumps({'01.1': {'verdict': 'none', 'note': '', 'reason': 'x',
                                                                     'at': '2026-01-01T00:00:00Z'}}))
        self.gone()
        self.upkeep()
        self.assertEqual(self.stages()['sweep'], 'ok')
        self.assertEqual(len(self.calls()), 1)


class ArchiveTest(UpkeepCase):
    def test_archive_waits_for_a_tick_that_ends(self):
        self.seed(FEATURE, '01', [])
        self.hold_for('tick', 1.5)

        with unittest.mock.patch.object(core, 'UPKEEP_TICK_WAIT_SECONDS', 5):
            self.upkeep()

        self.assertEqual(self.stages()['archive'], 'ok')
        self.assertFalse(self.live())

    def test_archive_skips_a_tick_that_runs_on_and_gc_still_runs(self):
        self.seed(FEATURE, '01', [])
        self.hold_for('tick', 60)
        ran = []

        with unittest.mock.patch.multiple(core, UPKEEP_TICK_WAIT_SECONDS=1, command_gc=lambda: ran.append(1) or 0):
            output = self.upkeep()

        self.assertTrue(any(line.startswith('archive: skipped, tick still runs after ') for line in output))
        self.assertEqual(self.stages(), {'sweep': 'ok', 'archive': 'skipped', 'gc': 'ok'})
        self.assertTrue(self.live())
        self.assertEqual(ran, [1])

    def test_upkeep_leaves_a_fresh_verdict_a_day_and_archive_by_hand_moves_it(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.verdicts(FEATURE, {'01.1': dict(self.verdict('done'), at=core.now_iso())})
        self.assertIn(f'{FEATURE}: skipped, because its newest deferred verdict is under a day old. Upkeep archives '
                      'it tomorrow; pipeline archive moves it now.', self.upkeep())
        self.assertTrue(self.live())
        self.assertEqual(self.main('archive'), 0, self.errors)
        self.assertFalse(self.live())

    def test_upkeep_archives_a_verdict_over_a_day_old(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py'), deferred('b', 'y.py')])
        old = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 25 * 3600))
        self.verdicts(FEATURE, {'01.1': dict(self.verdict('done'), at=old),
                                '01.2': dict(self.verdict('done'), at=old)})
        self.upkeep()
        self.assertFalse(self.live())

    def test_a_verdict_time_that_does_not_parse_counts_as_old(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.verdicts(FEATURE, {'01.1': dict(self.verdict('done'), at='not a time')})
        self.upkeep()
        self.assertFalse(self.live())


class PingTest(UpkeepCase):
    def test_a_closed_item_is_one_default_ping(self):
        self.gone()
        self.upkeep()
        self.assertEqual(self.pings(), [('pipeline upkeep', 'default',
                                         f'shop {FEATURE} 01.1: moot, path src/old.py is gone from origin/main')])
        self.assertTrue(self.live(), 'the verdict settles a day before archive moves it')

    def test_a_filed_item_is_a_default_ping(self):
        self.seed(FEATURE, '01', [deferred('the cache leaks', 'x.py')])
        self.runs(answering(answer('01.1', 'still_true', brief=brief())))
        self.upkeep()
        (title, priority, message), = self.pings()
        self.assertEqual((title, priority), ('pipeline upkeep', 'default'))
        self.assertIn(f"filed {self.root('backlog') / 'follow-ups/01-the-cache-leaks/brief.md'}", message)

    def test_a_left_open_item_pings_high_once(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.runs(answering(answer('01.1', 'needs_owner')))
        self.upkeep()
        self.assertEqual(self.pings(), [('pipeline upkeep', 'high',
                                         f'shop {FEATURE} 01.1: left open, the judge says it needs the owner → '
                                         f'/deferred-sweep {FEATURE}')])
        self.upkeep()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.calls()), 1)

    def test_a_failed_gc_pings_high(self):
        with unittest.mock.patch.object(core, 'command_gc', lambda: 1):
            self.upkeep(code=1)

        self.assertEqual(self.pings(), [('pipeline upkeep', 'high', 'gc failed: pipeline gc says why')])
        self.assertEqual(self.stages()['gc'], 'failed')

    def test_nothing_to_do_sends_no_ping(self):
        self.upkeep()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.stages(), {'sweep': 'ok', 'archive': 'ok', 'gc': 'ok'})

    def test_failures_come_first_and_the_cut_drops_only_closed_lines(self):
        closed = [(REPO, FEATURE, f'01.{index}', 'closed', f'{FEATURE} 01.{index}: done, commit {index:07d} x' * 2)
                  for index in range(1, 40)]
        left = (REPO, FEATURE, '02.1', 'left_open', f'{FEATURE} 02.1: left open, why')

        def sweep_all(report):
            for entry in [*closed, left]:
                report(entry)

            return [*closed, left], 0.0

        with unittest.mock.patch.multiple(core, sweep_all=sweep_all, command_gc=lambda: 1):
            self.upkeep(code=1)

        (_, priority, message), = self.pings()
        self.assertEqual(priority, 'high')
        self.assertEqual(len(message), core.NTFY_CHARACTERS)
        lines = message.split('\n')
        self.assertEqual(lines[:2], ['gc failed: pipeline gc says why',
                                     f'shop {FEATURE} 02.1: left open, why → /deferred-sweep {FEATURE}'])
        self.assertTrue(all(line.startswith(f'shop {FEATURE} 01.') for line in lines[2:]), lines)
        self.assertLess(len(lines) - 2, len(closed))


class ResultTest(UpkeepCase):
    def test_an_interrupt_records_nothing_and_returns_130(self):
        def sweep_all(report):
            raise KeyboardInterrupt

        with unittest.mock.patch.object(core, 'sweep_all', sweep_all):
            self.upkeep(code=130)

        self.assertIn('upkeep: interrupted; no result recorded', self.errors)
        self.assertIsNone(self.result())
        self.assertEqual(self.pings(), [('pipeline upkeep', 'high', 'upkeep interrupted during sweep')])

    def test_an_interrupt_after_a_closed_item_still_pings_it(self):
        line = f'{FEATURE} 01.1: moot, path src/old.py is gone from origin/main'

        def sweep_all(report):
            report((REPO, FEATURE, '01.1', 'closed', line))
            raise KeyboardInterrupt

        with unittest.mock.patch.object(core, 'sweep_all', sweep_all):
            self.upkeep(code=130)

        self.assertEqual(self.pings(), [('pipeline upkeep', 'high', f'upkeep interrupted during sweep\nshop {line}')])
        self.assertIsNone(self.result())

    def test_a_signal_during_the_judging_call_stops_the_run(self):
        self.seed(FEATURE, '01', [deferred('a', 'x.py')])
        self.runs([{'sleep': 30}])
        self.when_called(lambda: core.os.kill(core.os.getpid(), core.signal.SIGTERM))
        started = time.time()
        self.upkeep(code=130)
        self.assertLess(time.time() - started, 15)
        self.assertGone(self.calls()[0]['pid'])
        self.assertIsNone(self.result())

    def test_a_result_that_cannot_be_written_still_pings_and_exits_1(self):
        write(self.fixture.base / 'a-file', '')

        with unittest.mock.patch.object(core, 'UPKEEP_RESULT', self.fixture.base / 'a-file/upkeep.json'):
            self.upkeep(code=1)

        self.assertIn('upkeep.json: cannot write', self.errors)
        (_, priority, message), = self.pings()
        self.assertEqual(priority, 'high')
        self.assertTrue(message.startswith('upkeep result not saved: '), message)


class BadgeTest(UpkeepCase):
    def badges(self):
        with unittest.mock.patch.object(core, 'template_problem', lambda: None):  # the fake claude has no version
            return [badge for badge in core.status_model()['badges'] if badge.startswith('upkeep')]

    def record(self, age, **stages):
        write(core.UPKEEP_RESULT, json.dumps({'at': time.time() - age,
                                              'stages': {'sweep': 'ok', 'archive': 'ok', 'gc': 'ok', **stages}}))

    def test_no_result_no_badge(self):
        self.assertEqual(self.badges(), [])

    def test_a_fresh_ok_has_no_badge(self):
        self.record(3600)
        self.assertEqual(self.badges(), [])

    def test_a_fresh_failure_names_its_stages(self):
        self.record(3600, gc='failed')
        self.assertEqual(self.badges(), ['upkeep failed (gc): pipeline upkeep'])
        self.record(3600, sweep='failed', gc='failed')
        self.assertEqual(self.badges(), ['upkeep failed (sweep, gc): pipeline upkeep'])

    def test_a_result_over_3_days_old_is_stale_and_wins_over_a_failure(self):
        self.record(4 * DAY)
        self.assertEqual(self.badges(), ['upkeep 4d old: pipeline doctor'])
        self.record(4 * DAY, gc='failed')
        self.assertEqual(self.badges(), ['upkeep 4d old: pipeline doctor'])
        self.record(3 * DAY - 60)
        self.assertEqual(self.badges(), [])

    def test_an_unreadable_result_is_a_question_mark(self):
        for content in ('{broken', '[]', '{"at": "x", "stages": {}}', '{"at": 1}'):
            with self.subTest(content=content):
                write(core.UPKEEP_RESULT, content)
                self.assertEqual(self.badges(), ['upkeep ?'])

    def test_a_finished_run_clears_a_failed_badge(self):
        self.record(3600, gc='failed')
        self.upkeep()
        self.assertEqual(self.badges(), [])


if __name__ == '__main__':
    unittest.main()
