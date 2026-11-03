"""sbx_host against a fake `sbx` on PATH and fake cgroup, proc and state directories."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'sbx-host'))

import sbx_host  # noqa: E402

GIB, MIB = 2**30, 2**20
FAKE_SBX = '#!/bin/sh\n[ "$1 $2" = "ls --json" ] && exec cat "$FAKE_SBX_LS"\nexit 1\n'


class HostCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.base)])
        self.group = self.base / 'cgroup/user.slice/sbx.service'
        self.group.mkdir(parents=True)
        self.cap(high=16.5, high_max=19.5, swap_max=4)
        (self.base / 'bin').mkdir()
        (self.base / 'bin/sbx').write_text(FAKE_SBX)
        (self.base / 'bin/sbx').chmod(0o755)
        self.units = {'sbx.service': True, 'ci-runner@1.service': True}
        self.free = 20.0
        self.vms([])

        def systemctl(*arguments):
            if arguments[:2] == ('is-active', '--quiet'):
                return subprocess.CompletedProcess(arguments, 0 if self.units.get(arguments[2]) else 3, '', '')

            return subprocess.CompletedProcess(arguments, 0, '/user.slice/sbx.service\n', '')

        for patch in (mock.patch.multiple(sbx_host, HOME=self.base, CONFIG=self.base / 'config.json',
                                          STATE=self.base / 'state', CGROUP_ROOT=self.base / 'cgroup',
                                          PROC=self.base / 'proc', PIPELINE_SHARE=self.base / 'share/pipeline',
                                          systemctl=systemctl, mem_available_gib=lambda: self.free),
                      mock.patch.dict(os.environ, {'PATH': f"{self.base / 'bin'}:{os.environ['PATH']}",
                                                   'FAKE_SBX_LS': str(self.base / 'ls.json')})):
            patch.start()
            self.addCleanup(patch.stop)

        os.environ.pop('XDG_STATE_HOME', None)

    def cap(self, high, high_max, swap_max):
        for name, value in (('memory.high', high), ('memory.max', high_max), ('memory.swap.max', swap_max)):
            (self.group / name).write_text('max\n' if value is None else f'{int(value * GIB)}\n')

    def vms(self, items):
        (self.base / 'ls.json').write_text(json.dumps([{'name': name, 'status': status, 'memory_mib': mib}
                                                       for name, status, mib in items]))

    def config(self, **values):
        (self.base / 'config.json').write_text(json.dumps(values))

    def shim(self, pid, hwm_kib, name=None, fd_target=None):
        entry = self.base / 'proc' / str(pid)
        (entry / 'fd').mkdir(parents=True)
        (entry / 'cmdline').write_bytes(b'/usr/libexec/containerd-shim-nerdbox-v1\0-id\0x')
        (entry / 'cgroup').write_text('0::/user.slice/sbx.service\n')
        (entry / 'status').write_text(f'Name:\tshim\nVmHWM:\t{hwm_kib} kB\nVmSwap:\t2048 kB\n')
        bundle = self.base / 'bundles' / str(pid)
        bundle.mkdir(parents=True)

        if name:
            (bundle / 'config.json').write_text(json.dumps({'hostname': name}))

        (entry / 'cwd').symlink_to(bundle)

        if fd_target:
            (entry / 'fd/3').symlink_to(fd_target)


class FitsTest(HostCase):
    def test_one_stage_and_the_ci_lane_fit_under_high(self):
        self.vms([('ci-1-1', 'running', 7168)])
        self.assertEqual(sbx_host.fits('pl'), (True, 'reserved 7.5 + 8.5 GiB of 16.5'))

    def test_two_stages_and_the_ci_lane_are_refused_under_high_and_fit_under_max_plus_swap_at_a_lower_reserve(self):
        self.vms([('pl-shop-a-01', 'running', 8192), ('ci-1-1', 'running', 7168)])
        ok, why = sbx_host.fits('pl')
        self.assertFalse(ok)
        self.assertEqual(why, 'reserved 16.0 + 8.5 GiB > 16.5 (high)')
        self.config(budget='max+swap-1G')
        self.assertEqual(sbx_host.fits('pl'), (False, 'reserved 16.0 + 8.5 GiB > 22.5 (max+swap-1G)'))
        self.config(budget='max+swap-1G', reserve={'pl': 7680})
        self.assertEqual(sbx_host.fits('pl'), (True, 'reserved 15.0 + 7.5 GiB of 22.5'))

    def test_the_ci_lane_is_reserved_even_with_no_vm(self):
        self.config(reserve={'pl': 5632})
        self.vms([('pl-shop-a-01', 'running', 8192)])
        self.assertEqual([name for name, _, _ in sbx_host.reservations()], ['pl-shop-a-01', 'ci lane'])
        self.assertFalse(sbx_host.fits('pl')[0], 'a second stage would take the lane')
        self.units['ci-runner@1.service'] = False
        self.assertTrue(sbx_host.fits('pl')[0], 'no CI runner: no lane')
        self.units['ci-runner@1.service'] = True
        self.vms([('ci-1-5', 'running', 7168)])
        self.assertEqual([name for name, _, _ in sbx_host.reservations()], ['ci-1-5'], 'the lane is its VM')

    def test_stopped_vms_reserve_nothing_and_the_admitted_vm_counts_once(self):
        self.units['ci-runner@1.service'] = False
        self.vms([('pl-shop-a-01', 'stopped', 8192), ('pl-shop-b-01', 'running', 8192)])
        self.assertEqual(sbx_host.reservations(), [('pl-shop-b-01', 'pl', 8704)])
        self.assertEqual(sbx_host.reservations(exclude='pl-shop-b-01'), [])

    def test_a_pending_file_counts_and_a_dead_holders_does_not(self):
        self.units['ci-runner@1.service'] = False
        pending = self.base / 'state/pending'
        pending.mkdir(parents=True)
        (pending / 'pl-shop-a-01').write_text(json.dumps({'pid': os.getpid(), 'kind': 'pl'}))
        dead = subprocess.Popen(['true'])
        dead.wait()
        (pending / 'pl-shop-b-01').write_text(json.dumps({'pid': dead.pid, 'kind': 'pl'}))
        self.assertEqual(sbx_host.reservations(), [('pl-shop-a-01', 'pl', 8704)])

    def test_no_memory_high_leaves_only_the_floor(self):
        self.cap(high=None, high_max=19.5, swap_max=4)
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192)])
        self.assertEqual(sbx_host.fits('pl'), (True, 'reserved 24.5 + 8.5 GiB'))
        self.free = 9.99
        self.assertEqual(sbx_host.fits('pl'), (False, '9.9 GiB free < 10 GiB'))

    def test_the_reserve_and_floor_come_from_the_config(self):
        self.config(reserve={'pl': 5632}, floorGib=4)
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192),
                  ('dev-box', 'running', 1024)])
        self.free = 5
        self.assertEqual(sum(mib for _, _, mib in sbx_host.reservations()), 5632 * 2 + 1536 + 7680)
        self.assertFalse(sbx_host.fits('pl')[0])
        self.config(budget='low')
        self.assertRaises(sbx_host.HostError, sbx_host.load_config)

    def test_the_guard_refuses_a_second_daemon_or_a_stopped_unit(self):
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'elsewhere')}):
            self.assertIn('XDG_STATE_HOME', sbx_host.guard())
            self.assertRaises(sbx_host.HostError, sbx_host.vms)

        self.units['sbx.service'] = False
        self.assertEqual(sbx_host.guard(), 'sbx.service is not active (systemctl --user start sbx.service)')
        self.assertRaises(sbx_host.HostError, sbx_host.fits, 'pl')


class ClaimTest(HostCase):
    def setUp(self):
        super().setUp()
        self.config(budget='max+swap-1G', reserve={'pl': 8192, 'ci': 8512})
        self.cap(high=16.5, high_max=19.5, swap_max=8)
        self.pending = self.base / 'state/pending'

    def other_process(self):
        holder = subprocess.Popen(['sleep', '60'])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)

        return holder.pid

    def test_a_claim_is_written_only_when_it_fits_unless_always(self):
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192)])
        self.assertEqual(sbx_host.claim('pl', 'pl-shop-c-01'),
                         (False, 'reserved 24.3 + 8.0 GiB > 26.5 (max+swap-1G)'))
        self.assertFalse((self.pending / 'pl-shop-c-01').exists())
        self.assertEqual(sbx_host.claim('ci', 'ci-1-9'), (True, 'reserved 16.0 + 8.3 GiB of 26.5'),
                         'two stages and the CI lane fit')
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192),
                  ('pl-shop-c-01', 'running', 8192)])
        self.assertEqual(sbx_host.claim('ci', 'ci-1-9', always=True)[0], False)
        self.assertEqual(json.loads((self.pending / 'ci-1-9').read_text())['kind'], 'ci', 'the CI lane claims anyway')

    def test_a_second_claim_counts_the_first_and_release_gives_it_back(self):
        self.units['ci-runner@1.service'] = False
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192)])
        self.pending.mkdir(parents=True)
        (self.pending / 'ci-2-5').write_text(json.dumps({'pid': self.other_process(), 'kind': 'ci'}))
        self.assertEqual(sbx_host.claim('pl', 'pl-shop-c-01'), (False, 'reserved 24.3 + 8.0 GiB > 26.5 (max+swap-1G)'))
        (self.pending / 'ci-2-5').unlink()
        self.assertEqual(sbx_host.claim('pl', 'pl-shop-c-01'), (True, 'reserved 16.0 + 8.0 GiB of 26.5'))
        self.assertEqual(sbx_host.fits('ci', 'ci-2-6')[0], False, 'the claim counts for others')
        sbx_host.release('pl-shop-c-01')
        self.assertEqual(sbx_host.pending(), [])

    def test_one_claim_per_process_and_dead_holders_files_go(self):
        self.units['ci-runner@1.service'] = False
        self.pending.mkdir(parents=True)
        dead = subprocess.Popen(['true'])
        dead.wait()
        (self.pending / 'pl-shop-x-01').write_text(json.dumps({'pid': dead.pid, 'kind': 'pl'}))
        sbx_host.claim('pl', 'pl-shop-a-01')
        sbx_host.claim('pl', 'pl-shop-a-02')
        self.assertEqual(sorted(path.name for path in self.pending.iterdir()), ['pl-shop-a-02'],
                         'the next step moves the claim; a dead holder\'s file is deleted')
        self.assertEqual(sbx_host.fits('pl', 'pl-shop-a-02'), (True, 'reserved 0.0 + 8.0 GiB of 26.5'),
                         'its own claim never counts against it')

    def test_a_reused_pid_holds_nothing(self):
        proc = self.base / 'proc' / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / 'stat').write_text(f'{os.getpid()} (python3) S' + ' 0' * 18 + ' 500 0\n')
        self.assertEqual(sbx_host.process_start(os.getpid()), 500)
        self.pending.mkdir(parents=True)
        (self.pending / 'pl-shop-x-01').write_text(json.dumps({'pid': os.getpid(), 'kind': 'pl', 'start': 499}))
        self.assertEqual(sbx_host.pending(), [])

    def test_room_counts_what_still_fits(self):
        self.assertEqual(sbx_host.room('pl'), 2, '26.5 − the CI lane 8.3 holds two stages')
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'stopped', 8192)])
        self.assertEqual(sbx_host.room('pl'), 1)
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192)])
        self.assertEqual(sbx_host.room('pl'), 0)
        self.vms([])
        self.free = 9.99
        self.assertEqual(sbx_host.room('pl'), 0, 'below the floor')
        self.free = 20.0
        self.cap(high=16.5, high_max=None, swap_max=8)
        self.assertEqual(sbx_host.room('pl'), float('inf'), 'no cap: no budget to fill')


class ReadingTest(HostCase):
    def test_the_reading_has_every_cap_and_counts_shims_outside(self):
        (self.group / 'memory.current').write_text(f'{13 * GIB}\n')
        (self.group / 'memory.events').write_text('low 0\nhigh 7\nmax 0\noom 0\noom_kill 2\n')
        (self.group / 'memory.swap.events').write_text('high 0\nmax 360\nfail 372\n')
        (self.group / 'memory.pressure').write_text('some avg10=0 avg60=1.00 avg300=0 total=1\n'
                                                    'full avg10=0 avg60=0.25 avg300=0 total=1\n')
        self.shim(11, 1024)
        self.shim(12, 1024)
        (self.base / 'proc/12/cgroup').write_text('0::/user.slice/tmux.scope\n')
        memory = sbx_host.reading()
        self.assertEqual((memory['current'], memory['high'], memory['oom'], memory['high_events'], memory['avg60'],
                          memory['swap_fail']), (13 * GIB, int(16.5 * GIB), 2, 7, 0.25, 372))
        self.assertEqual((memory['inside'], memory['total']), (1, 2))
        self.assertIsNone(memory['peak'], 'unreadable is None, not 0')


class SampleTest(HostCase):
    def test_sample_names_each_vm_and_trims_old_lines(self):
        self.shim(21, 6 * 1024 * 1024, name='pl-shop-a-01')
        self.shim(22, 4 * 1024 * 1024, fd_target=self.base / 'share/pipeline/clones/shop')
        self.shim(23, 5 * 1024 * 1024)
        state = self.base / 'state'
        state.mkdir()
        old = {'at': int(time.time()) - 31 * 86400, 'name': 'ci-1-1', 'kind': 'ci', 'hwm_mib': 1}
        (state / 'peaks.jsonl').write_text(json.dumps(old) + '\n')
        sbx_host.sample()
        lines = [json.loads(line) for line in (state / 'peaks.jsonl').read_text().splitlines()]
        self.assertEqual(sorted((line['name'] or '', line['kind'], line['hwm_mib'], line['swap_mib']) for line in lines),
                         [('', 'ci', 5120, 2), ('', 'pl', 4096, 2), ('pl-shop-a-01', 'pl', 6144, 2)])

    def test_peaks_keep_each_vms_highest(self):
        state = self.base / 'state'
        state.mkdir()
        now = int(time.time())
        (state / 'peaks.jsonl').write_text(''.join(json.dumps(line) + '\n' for line in (
            {'at': now, 'name': 'ci-1-1', 'kind': 'ci', 'hwm_mib': 3000},
            {'at': now, 'name': 'ci-1-1', 'kind': 'ci', 'hwm_mib': 5000},
            {'at': now, 'name': 'ci-2-1', 'kind': 'ci', 'hwm_mib': 4000})))
        self.assertEqual(sorted(sbx_host.peaks()['ci']), [4000, 5000])
        self.assertEqual(sbx_host.p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]), 10)
        self.assertEqual(sbx_host.p95(list(range(1, 41))), 38)


class CliTest(HostCase):
    def setUp(self):
        super().setUp()
        cli = Path(__file__).resolve().parent.parent / 'sbx-host/sbx-host'
        self.cli = {'__name__': 'cli', '__file__': str(cli)}
        exec(compile(cli.read_text(), str(cli), 'exec'), self.cli)

    def hold(self, *argv, code=0):
        """main(['hold', ...]) running a command that records the claims it sees and exits `code`: (exit, stderr,
        the claims the command saw or None when it never ran)."""
        seen = self.base / 'seen'
        seen.unlink(missing_ok=True)
        command = [sys.executable, '-c', 'import os, sys; open(sys.argv[1], "w").write(" ".join(sorted(os.listdir('
                   'sys.argv[2])))); sys.exit(int(sys.argv[3]))', str(seen), str(self.base / 'state/pending'), str(code)]

        with mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            result = self.cli['main'](['hold', *argv, '--', *command])

        return result, stderr.getvalue(), seen.read_text() if seen.exists() else None

    def test_fits_exits_by_the_answer(self):
        spec = self.cli
        self.vms([('pl-shop-a-01', 'running', 8192), ('ci-1-1', 'running', 7168)])

        with mock.patch('sys.stdout'):
            self.assertEqual(spec['main'](['fits', 'ci']), 1)
            self.assertEqual(spec['main'](['fits', 'gpu']), 2)
            self.vms([])
            self.assertEqual(spec['main'](['fits', 'ci']), 0)
            self.vms([('pl-shop-a-01', 'running', 8192)])
            self.assertEqual(spec['main'](['fits', 'ci']), 1, 'the CI lane and a new CI VM')
            self.assertEqual(spec['main'](['fits', 'ci', 'ci-1-9']), 0, "slot 1's VM is the lane")

    def test_hold_runs_the_command_holding_the_claim(self):
        self.assertEqual(self.hold('ci', 'ci-2-9', code=3), (3, '', 'ci-2-9'), "the command's own code")
        self.assertEqual(sbx_host.pending(), [], 'given back after')
        self.vms([('pl-shop-a-01', 'running', 8192), ('pl-shop-b-01', 'running', 8192)])
        self.assertEqual(self.hold('ci', 'ci-2-9'), (75, 'sbx-host: refused: reserved 24.5 + 7.5 GiB > 16.5 (high)\n',
                                                     None))
        self.assertEqual(self.hold('--always', 'ci', 'ci-1-9'), (0, 'sbx-host: over: reserved 17.0 + 7.5 GiB > 16.5 (high)\n',
                                                                 'ci-1-9'))
        self.assertEqual(sbx_host.pending(), [])

    def test_hold_never_runs_the_command_when_no_sbx_call_may(self):
        self.units['sbx.service'] = False
        self.assertEqual(self.hold('--always', 'ci', 'ci-1-9'),
                         (1, 'sbx-host: no sbx call: sbx.service is not active (systemctl --user start sbx.service)\n',
                          None))
        with mock.patch('sys.stderr'):
            self.assertEqual(self.cli['main'](['hold', 'ci', 'ci-1-9']), 2, 'no command: the usage')

    def test_status_says_when_a_reserve_is_below_its_peaks(self):
        state = self.base / 'state'
        state.mkdir()
        now = int(time.time())
        (state / 'peaks.jsonl').write_text(''.join(json.dumps({'at': now, 'name': f'ci-1-{index}', 'kind': 'ci',
                                                               'hwm_mib': 7900}) + '\n' for index in range(3)))
        self.config(reserve={'ci': 8512})

        with mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.cli['main'](['status'])

        self.assertIn('retune ci: reserve 8.3 · p95+512 8.2\n', stdout.getvalue())
        self.config(reserve={'ci': 8192})

        with mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.cli['main'](['status'])

        self.assertIn('retune ci: reserve 8.0 · p95+512 8.2 (raise the reserve)\n', stdout.getvalue())
        self.assertNotIn('retune pl', stdout.getvalue(), 'no pl peaks yet')


if __name__ == '__main__':
    unittest.main()
