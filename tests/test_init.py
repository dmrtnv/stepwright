"""`pipeline init`: links, units sized from /proc and config skeletons in a temp HOME, never overwriting anything.

Run: python3 -m unittest discover tests
"""
import contextlib
import errno
import io
import json
import os
import shutil
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

REPO = TESTS.parent
CAPS = ('MemoryHigh=', 'MemoryMax=', 'CPUQuota=')


def meminfo(gib=None, kib=None):
    """A /proc/meminfo whose MemTotal is a little under gib GiB, as on a real box (or exactly kib)."""
    total = kib if kib is not None else gib * 2**20 - 300_000

    return f'MemTotal:       {total} kB\nMemFree:        1000 kB\nMemAvailable:   2000 kB\n'


class SizesTest(unittest.TestCase):
    def sizes(self, gib, threads, **kib):
        return core.init_sizes(int(meminfo(gib, **kib).split()[1]), threads)

    def test_the_formula(self):
        rows = {(32, 16): (6, 8, 24, 21, 1200, 8704, 2),
                (24, 12): (6, 8, 16, 13, 800, 8704, 1),
                (16, 8): (4, 4, 8, 5, 400, 4608, 1),
                (64, 32): (6, 8, 56, 53, 2800, 8704, 6),
                (8, 4): (2, 4, 1, 1, 100, 4608, 1)}

        for (gib, threads), expected in rows.items():
            with self.subTest(gib=gib, threads=threads):
                sizes = self.sizes(gib, threads)
                self.assertEqual(sizes['ramGb'], gib)
                self.assertEqual(sizes['threads'], threads)
                self.assertEqual(tuple(sizes[key] for key in ('cpus', 'memoryGb', 'memoryMax', 'memoryHigh',
                                                              'cpuQuota', 'pl', 'slots')), expected)

    def test_memtotal_is_rounded_up(self):
        # 24_400_000 kB is 23.27 GiB: truncating it (free -g) would size a 24 GiB box as 23 and give it 4 GiB VMs.
        sizes = core.init_sizes(24_400_000, 12)
        self.assertEqual((sizes['ramGb'], sizes['memoryGb'], sizes['memoryMax']), (24, 8, 16))

    def test_a_tiny_box_is_floored_at_1(self):
        sizes = core.init_sizes(1_000_000, 1)
        self.assertEqual(tuple(sizes[key] for key in ('ramGb', 'cpus', 'memoryGb', 'memoryMax', 'memoryHigh',
                                                      'cpuQuota', 'slots')), (1, 1, 4, 1, 1, 100, 1))

    def test_no_cpu_count_counts_as_1(self):
        self.assertEqual(core.init_sizes(32 * 2**20, None)['threads'], 1)


class InitCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='init-'))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.kit = fixture_kit(self.base)
        self.meminfo = self.base / 'meminfo'
        self.pipeline_config = self.home / '.config/pipeline/config.json'
        self.host_config = self.home / '.config/sbx-host/config.json'
        self.units = self.home / '.config/systemd/user'
        self.box(32, 16)

        for patcher in (mock.patch.dict(os.environ, {'HOME': str(self.home)}),
                        mock.patch.object(core, 'HOME', self.home),
                        mock.patch.object(adapter, 'CONFIG', self.pipeline_config),
                        mock.patch.object(sbx_host, 'CONFIG', self.host_config),
                        mock.patch.object(backend, 'KIT', self.kit),
                        mock.patch.object(core, 'MEMINFO', self.meminfo)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def box(self, gib, threads):
        self.meminfo.write_text(meminfo(gib))
        patcher = mock.patch('os.cpu_count', return_value=threads)
        patcher.start()
        self.addCleanup(patcher.stop)

    def init(self, *arguments):
        out, err = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch('subprocess.run', side_effect=AssertionError('init runs no command')):
            code = core.main(['init', *arguments])

        self.err = err.getvalue()

        return code, out.getvalue().splitlines()

    def kit_links(self):
        return {self.home / '.claude' / item / entry.name: entry
                for item in backend.KIT_ITEMS for entry in sorted((self.kit / item).iterdir())
                if entry.name != '__pycache__'}

    def links(self):
        return {self.home / '.local/bin/pipeline': REPO / 'pipeline',
                self.home / '.local/bin/sbx-host': REPO / 'sbx-host/sbx-host', **self.kit_links()}

    def files(self):
        return [self.units / 'sbx.service', self.units / 'pipeline-tick.service', self.units / 'pipeline-tick.timer',
                self.pipeline_config, self.host_config]

    def items(self, lines):
        return [line for line in lines if line.split(' ', 1)[0] in ('wrote', 'linked', 'skipped', 'failed')]

    def snapshot(self):
        return {path: (os.readlink(path) if path.is_symlink() else
                       path.read_bytes() if path.is_file() else sorted(os.listdir(path)))
                for path in [*self.links(), *self.files()] if os.path.lexists(path)}

    def caps(self, unit):
        return [line for line in unit.read_text().splitlines() if line.startswith(CAPS)]


class FreshTest(InitCase):
    def test_writes_everything_with_the_formula(self):
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))

        for link, target in self.links().items():
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(os.readlink(link), str(target))
            self.assertEqual(os.path.realpath(link), os.path.realpath(target))

        self.assertFalse((self.home / '.claude/skills/__pycache__').exists())

        unit = (self.units / 'sbx.service').read_text().splitlines()
        template = (REPO / 'systemd/sbx.service').read_text().splitlines()
        self.assertEqual(len(unit), len(template))
        self.assertEqual([(ours, theirs) for ours, theirs in zip(unit, template) if ours != theirs],
                         [])  # 32 GiB and 16 threads: the shipped caps
        self.assertEqual(self.caps(self.units / 'sbx.service'), ['MemoryHigh=21G', 'MemoryMax=24G', 'CPUQuota=1200%'])

        for name in ('pipeline-tick.service', 'pipeline-tick.timer'):
            self.assertEqual((self.units / name).read_bytes(), (REPO / 'systemd' / name).read_bytes())

        for path in self.files():
            self.assertNotIn(str(self.home), path.read_text())

        example = json.loads((REPO / 'examples/config.example.json').read_text())
        written = json.loads(self.pipeline_config.read_text())
        self.assertEqual(written, dict(example, admission={'slots': 2}, vm={'cpus': 6, 'memoryGb': 8}))
        self.assertTrue(self.pipeline_config.read_text().endswith('}\n'))
        self.assertNotIn('minMemoryGb', self.pipeline_config.read_text())
        self.assertEqual(json.loads(self.host_config.read_text()), {'reserve': {'pl': 8704}})
        self.assertEqual(adapter.load_config()['vm'], {'cpus': 6, 'memoryGb': 8})
        self.assertEqual(sbx_host.load_config()['reserve']['pl'], 8704)

        items = self.items(lines)
        self.assertEqual(len(items), len(self.links()) + len(self.files()), '\n'.join(lines))
        self.assertTrue(all(line.split(' ', 1)[0] in ('wrote', 'linked') for line in items), '\n'.join(lines))
        self.assertFalse(any(line.startswith('Warning') for line in lines), '\n'.join(lines))

    def test_a_16_gib_box(self):
        self.box(16, 8)
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(self.caps(self.units / 'sbx.service'), ['MemoryHigh=5G', 'MemoryMax=8G', 'CPUQuota=400%'])
        config = adapter.load_config()
        self.assertEqual((config['admission']['slots'], config['vm']), (1, {'cpus': 4, 'memoryGb': 4}))
        pl = sbx_host.load_config()['reserve']['pl']
        self.assertEqual(pl, 4608)
        self.assertGreaterEqual(pl, config['vm']['memoryGb'] * 1024)  # doctor's sbx-host reserve rule

    def test_a_small_box_warns_and_still_writes_everything(self):
        self.box(8, 4)
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        warning = [line for line in lines if line.startswith('Warning')]
        self.assertEqual(len(warning), 1, '\n'.join(lines))
        self.assertIn('8 GiB of RAM and 4 CPU threads', warning[0])
        self.assertIn('at least 16 GiB and 8 threads', warning[0])
        self.assertIn('By default sbx-host starts no VM while less than 10 GiB of RAM is free '
                      f'(floorGib in {self.host_config})', warning[0])
        self.assertEqual(self.caps(self.units / 'sbx.service'), ['MemoryHigh=1G', 'MemoryMax=1G', 'CPUQuota=100%'])
        self.assertTrue(all(path.exists() for path in self.files()))

    def test_prints_the_sizing_and_the_next_commands_and_runs_none(self):
        _, lines = self.init()
        self.assertIn('32 GiB of RAM and 16 CPU threads', lines[0])
        self.assertFalse(any(line.startswith('Kept') for line in lines), '\n'.join(lines))
        next_block = lines[lines.index('Next (INSTALL step 3 on):'):]
        self.assertEqual(next_block[1:6], ['  systemctl --user daemon-reload',
                                           '  systemctl --user enable --now sbx.service',
                                           '  loginctl enable-linger "$USER"',
                                           f'  {REPO}/sbx-host/install-sbx.sh',
                                           '  sbx login'])
        self.assertIn('pipeline doctor', next_block[-1])
        self.assertIn(str(self.pipeline_config), '\n'.join(next_block))

    def test_extra_arguments_are_a_usage_error(self):
        code, lines = self.init('now')
        self.assertEqual((code, lines), (2, []))
        self.assertIn('usage: pipeline init', self.err)
        self.assertFalse(self.units.exists())


class RerunTest(InitCase):
    def test_a_second_run_skips_everything(self):
        self.init()
        before = self.snapshot()
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        items = self.items(lines)
        self.assertEqual(len(items), len(self.links()) + len(self.files()))
        self.assertTrue(all(line.startswith('skipped') and line.endswith('(exists)') for line in items),
                        '\n'.join(lines))
        self.assertEqual(self.snapshot(), before)

    def test_what_exists_is_kept(self):
        skills = self.home / '.claude/skills'
        own = skills / 'commit-message'
        own.mkdir(parents=True)
        (own / 'SKILL.md').write_text('mine')
        other = skills / 'plan-harden'
        other.symlink_to(self.base / 'elsewhere')  # dangling, and not the kit's
        bin_link = self.home / '.local/bin/pipeline'
        bin_link.parent.mkdir(parents=True)
        bin_link.symlink_to(self.base / 'gone')
        unit = self.units / 'sbx.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('tuned')
        before = self.snapshot()

        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual((own / 'SKILL.md').read_text(), 'mine')
        self.assertEqual(sorted(os.listdir(own)), ['SKILL.md'])

        for path in (own, other, bin_link, unit):
            self.assertEqual(self.snapshot()[path], before[path])
            self.assertIn(f'skipped  {path} (exists)', lines)

        self.assertTrue((skills / 'pr-text').is_symlink())
        kept = [line for line in lines if line.startswith('Kept')]
        self.assertEqual(kept, [f'Kept your {unit}: its values are the ones to check, not the ones above.'])
        self.assertTrue(lines[0].startswith('This box has 32 GiB of RAM and 16 CPU threads. For it:'), lines[0])

    def test_a_kept_config_sizes_the_reserve(self):
        self.pipeline_config.parent.mkdir(parents=True)
        self.pipeline_config.write_text(json.dumps({
            'app': {'id': 1, 'slug': 's', 'privateKey': '~/k.pem', 'reviewer': 'r'}, 'repos': {'a/b': '/tmp/b'},
            'vm': {'memoryGb': 10}}))
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(json.loads(self.host_config.read_text()), {'reserve': {'pl': 10752}})
        self.assertIn('Your config keeps vm.memoryGb 10; this box would get 8.', lines)
        self.assertIn(f'Kept your {self.pipeline_config}: its values are the ones to check, not the ones above.',
                      lines)

    def test_a_kept_config_without_vm_on_a_16_gib_box(self):
        self.box(16, 8)
        self.pipeline_config.parent.mkdir(parents=True)
        self.pipeline_config.write_text(json.dumps({
            'app': {'id': 1, 'slug': 's', 'privateKey': '~/k.pem', 'reviewer': 'r'}, 'repos': {'a/b': '/tmp/b'}}))
        _, lines = self.init()
        self.assertIn('Your config keeps vm.memoryGb 8; this box would get 4.', lines)
        self.assertEqual(json.loads(self.host_config.read_text()), {'reserve': {'pl': 8704}})

    def test_a_kept_sbx_host_config_is_named(self):
        self.host_config.parent.mkdir(parents=True)
        self.host_config.write_text('{"reserve": {"pl": 99999}, "floorGib": 2}')
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(self.host_config.read_text(), '{"reserve": {"pl": 99999}, "floorGib": 2}')
        kept = [line for line in lines if line.startswith('Kept')]
        self.assertEqual(kept, [f'Kept your {self.host_config}: its values are the ones to check, not the ones above.'])

    def test_an_unloadable_kept_config_falls_back_to_the_computed_vm(self):
        self.box(16, 8)
        self.pipeline_config.parent.mkdir(parents=True)
        self.pipeline_config.write_text('{')
        code, lines = self.init()
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(self.pipeline_config.read_text(), '{')
        self.assertEqual(json.loads(self.host_config.read_text()), {'reserve': {'pl': 4608}})
        self.assertFalse(any(line.startswith('Your config keeps') for line in lines))


class FailureTest(InitCase):
    def test_an_unusable_directory_fails_that_item_and_the_rest_go_on(self):
        (self.home / '.claude').mkdir()
        (self.home / '.claude/agents').write_text('a file, not a dir')
        code, lines = self.init()
        self.assertEqual(code, 1, '\n'.join(lines))
        agent = self.home / '.claude/agents/web-researcher.md'
        failed = [line for line in lines if line.startswith('failed')]
        self.assertEqual(len(failed), 1, '\n'.join(lines))
        self.assertTrue(failed[0].startswith(f'failed   {agent}: '), failed[0])
        self.assertTrue(all(path.exists() for path in self.files()))
        self.assertTrue((self.home / '.local/bin/pipeline').is_symlink())

    def test_a_write_error_leaves_no_file(self):
        target = self.units / 'pipeline-tick.timer'
        real_open = open

        class Full:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.handle.close()

            def write(self, _):
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        def fake_open(path, *arguments, **options):
            handle = real_open(path, *arguments, **options)
            return Full(handle) if Path(path) == target else handle

        with mock.patch.object(core, 'open', fake_open, create=True):
            code, lines = self.init()

        self.assertEqual(code, 1, '\n'.join(lines))
        self.assertFalse(os.path.lexists(target))
        self.assertIn(f'failed   {target}: {os.strerror(errno.ENOSPC)}', lines)
        self.assertTrue((self.units / 'pipeline-tick.service').exists())

    def test_a_failed_cleanup_still_reports_the_item_and_the_rest_go_on(self):
        target = self.units / 'pipeline-tick.service'
        real_open, real_unlink = open, Path.unlink

        def fake_open(path, *arguments, **options):
            handle = real_open(path, *arguments, **options)

            if Path(path) == target:
                handle.close()
                handle = real_open(path)  # read-only: write() raises

            return handle

        def fake_unlink(path, *arguments, **options):
            if path == target:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))

            return real_unlink(path, *arguments, **options)

        with mock.patch.object(core, 'open', fake_open, create=True), mock.patch.object(Path, 'unlink', fake_unlink):
            code, lines = self.init()

        self.assertEqual(code, 1, '\n'.join(lines))
        failed = [line for line in lines if line.startswith('failed')]
        self.assertEqual(len(failed), 1, '\n'.join(lines))
        self.assertTrue(failed[0].startswith(f'failed   {target}: '), failed[0])
        self.assertTrue((self.units / 'pipeline-tick.timer').exists())
        self.assertTrue(self.host_config.exists())

    def test_a_drifted_template_fails_loudly_and_writes_no_unit(self):
        runner = self.base / 'runner'
        shutil.copytree(REPO / 'systemd', runner / 'systemd')
        shutil.copytree(REPO / 'examples', runner / 'examples')
        unit = runner / 'systemd/sbx.service'
        unit.write_text(''.join(line for line in unit.read_text().splitlines(keepends=True)
                                if not line.startswith('CPUQuota=')))

        with mock.patch.object(core, 'PIPELINE_DIR', runner):
            code, lines = self.init()

        self.assertEqual(code, 1)
        self.assertIn('CPUQuota', self.err)
        self.assertFalse((self.units / 'sbx.service').exists())


if __name__ == '__main__':
    unittest.main()
