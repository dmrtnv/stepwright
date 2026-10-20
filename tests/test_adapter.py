"""Repo adapter tests: the registry, feature lookup, the clean clone and pipeline.json validation.

Run: python3 -m unittest discover tests
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapter  # noqa: E402

VALID = {
    'base': 'main', 'setup': ['pnpm install'], 'setupEgress': ['registry.npmjs.org'],
    'checkFast': [{'name': 'check', 'run': 'pnpm check'}], 'protected': ['.github/**'],
    'ci': {'requiredChecks': ['check']},
}


def git(directory, *arguments):
    return subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


def write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='adapter-test-'))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.config = self.base / 'config.json'
        patches = mock.patch.multiple(adapter, CONFIG=self.config, LOGS=self.base / 'logs', CLONES=self.base / 'clones')
        patches.start()
        self.addCleanup(patches.stop)
        environment = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': '/dev/null'})
        environment.start()
        self.addCleanup(environment.stop)

    def register(self, repos, **extra):
        write(self.config, json.dumps(dict({'app': {'id': 1, 'slug': 's', 'privateKey': '~/k.pem', 'reviewer': 'me'},
                                            'repos': {name: str(path) for name, path in repos.items()}}, **extra)))

    def checkout(self, name, features=()):
        path = self.base / name
        path.mkdir(parents=True)

        for feature in features:
            write(path / f'.claude/plans/{feature}/01-x/brief.md', '# x\n')

        return path


class ConfigTest(AdapterCase):
    def test_the_example_config_loads_with_defaults(self):
        shutil.copy(Path(adapter.__file__).parent / 'examples/config.example.json', self.config)
        config = adapter.load_config()
        self.assertEqual(config['app']['privateKey'], str(Path.home() / '.config/pipeline/app.pem'))
        self.assertEqual((config['admission'], config['template']), (adapter.DEFAULT_ADMISSION, adapter.DEFAULT_TEMPLATE))

    def test_a_missing_config_or_missing_keys_are_named(self):
        with self.assertRaisesRegex(adapter.AdapterError, 'no .*config.json'):
            adapter.load_config()

        write(self.config, json.dumps({'app': {'id': 1}, 'repos': {}}))

        with self.assertRaisesRegex(adapter.AdapterError, 'lacks app.slug, app.privateKey, app.reviewer, repos'):
            adapter.load_config()

    def test_repos_are_owner_slash_repo_with_unique_names(self):
        self.register({'shop': self.base})

        with self.assertRaisesRegex(adapter.AdapterError, 'keyed "<owner>/<repo>"'):
            adapter.load_config()

        self.register({'a/shop': self.base, 'b/shop': self.base})

        with self.assertRaisesRegex(adapter.AdapterError, 'share a name'):
            adapter.load_config()

    def test_admission_defaults_and_the_key_path_expand(self):
        self.register({'a/r': self.base}, admission={'slots': 3})
        config = adapter.load_config()
        self.assertEqual(config['admission'], {'slots': 3, 'minMemoryGb': 10, 'weeklyMax': 80, 'fiveHourMax': 70})
        self.assertEqual(config['app']['privateKey'], str(Path.home() / 'k.pem'))

    def test_the_template_policy_defaults_to_host_auto_and_rejects_anything_else(self):
        self.register({'a/r': self.base})
        self.assertEqual(adapter.load_config()['template'], {'claude': 'host', 'build': 'auto'})
        self.register({'a/r': self.base}, template={'claude': '2.1.286', 'build': 'manual'})
        self.assertEqual(adapter.load_config()['template'], {'claude': '2.1.286', 'build': 'manual'})

        for template, message in (({'claude': 'latest'}, 'template.claude is "host" or a version'),
                                  ({'claude': '2.1'}, 'template.claude'),
                                  ({'build': 'sometimes'}, 'template.build is "auto" or "manual"'),
                                  ({'pin': '2.1.286'}, 'template is {'),
                                  (['host'], 'template is {')):
            self.register({'a/r': self.base}, template=template)

            with self.assertRaisesRegex(adapter.AdapterError, re.escape(message)):
                adapter.load_config()


class FeatureLookupTest(AdapterCase):
    def test_the_one_checkout_holding_the_feature_is_found(self):
        self.register({'a/one': self.checkout('one', ['csv']), 'a/two': self.checkout('two', ['other'])})
        self.assertEqual(adapter.repo_for('csv', cwd=self.base), 'a/one')

        with self.assertRaisesRegex(adapter.AdapterError, 'no feature missing in any registered checkout'):
            adapter.repo_for('missing', cwd=self.base)

    def test_an_ambiguous_feature_needs_repo_unless_you_stand_in_one_checkout(self):
        one, two = self.checkout('one', ['csv']), self.checkout('two', ['csv'])
        self.register({'a/one': one, 'a/two': two})

        with self.assertRaisesRegex(adapter.AdapterError, 'exists in a/one, a/two: pass --repo'):
            adapter.repo_for('csv', cwd=self.base)

        (two / 'src').mkdir()
        self.assertEqual(adapter.repo_for('csv', cwd=two / 'src'), 'a/two')
        self.assertEqual(adapter.repo_for('csv', repo='a/one', cwd=two), 'a/one')

        with self.assertRaisesRegex(adapter.AdapterError, 'not registered'):
            adapter.repo_for('csv', repo='a/three')

    def test_all_features_lists_step_folders_only(self):
        one = self.checkout('one', ['csv', 'pdf'])
        write(one / '.claude/plans/notes/readme.md', 'x\n')
        self.register({'a/one': one})
        self.assertEqual(adapter.all_features(), [('a/one', 'csv'), ('a/one', 'pdf')])


class CleanCloneTest(AdapterCase):
    def setUp(self):
        super().setUp()
        self.origin = self.base / 'origin.git'
        work = self.base / 'work'
        git(self.base, 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        git(self.base, 'clone', '-q', str(self.origin), str(work))

        for key, value in (('user.email', 't@t'), ('user.name', 't'), ('commit.gpgsign', 'false')):
            git(work, 'config', key, value)

        write(work / '.claude/pipeline.json', json.dumps(VALID))
        git(work, 'add', '-A')
        git(work, 'commit', '-qm', 'init')
        git(work, 'push', '-q', 'origin', 'main')
        write(work / '.claude/pipeline.json', json.dumps(dict(VALID, base='agent-wants-this')))
        git(work, 'checkout', '-q', '-b', 'agent/x')
        git(work, 'commit', '-qam', 'agent edits its recipe')
        git(work, 'push', '-q', 'origin', 'agent/x')
        self.work = work
        self.register({'a/r': work})
        patch = mock.patch.object(adapter, 'remote_url', lambda repo: str(self.origin))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_clone_follows_origin_and_is_never_checked_out(self):
        path = adapter.clean_clone('a/r', dict(os.environ))
        self.assertEqual(sorted(item.name for item in path.iterdir()), ['.git'])
        self.assertEqual(git(path, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'main'))
        self.assertEqual(git(path, 'config', 'transfer.fsckObjects'), 'true')
        git(self.work, 'checkout', '-q', 'main')
        write(self.work / 'more.txt', 'x\n')
        git(self.work, 'add', '-A')
        git(self.work, 'commit', '-qm', 'more')
        git(self.work, 'push', '-q', 'origin', 'main')
        adapter.clean_clone('a/r', dict(os.environ))
        self.assertEqual(git(path, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'main'),
                         'a sandbox fetching origin from the clone sees the new base')

    def test_the_recipe_is_read_from_the_base_branch_never_an_agent_branch_or_your_checkout(self):
        adapter.clean_clone('a/r', dict(os.environ))
        self.assertEqual(adapter.repo_config('a/r')['base'], 'main')

    def test_stray_files_in_the_clone_are_refused(self):
        path = adapter.clean_clone('a/r', dict(os.environ))
        write(path / 'apps/api/.env', 'SECRET=1\n')

        with self.assertRaisesRegex(adapter.AdapterError, r'holds files outside \.git \(apps\).*env files'):
            adapter.clean_clone('a/r', dict(os.environ))

    def test_a_missing_recipe_is_named(self):
        git(self.work, 'checkout', '-q', 'main')
        git(self.work, 'rm', '-q', '.claude/pipeline.json')
        git(self.work, 'commit', '-qm', 'drop')
        git(self.work, 'push', '-q', 'origin', 'main')

        with self.assertRaisesRegex(adapter.AdapterError, 'no .claude/pipeline.json on'):
            adapter.clean_clone('a/r', dict(os.environ))


class ValidateTest(unittest.TestCase):
    def check(self, config, message):
        with self.assertRaisesRegex(adapter.AdapterError, message):
            adapter.validate('a/r', config)

    def test_every_validation_error_is_named(self):
        self.check(dict(VALID, appendOnly=True), 'unknown key appendOnly')
        self.check({key: value for key, value in VALID.items() if key != 'ci'}, 'missing key ci')
        self.check(dict(VALID, base=''), 'base must be a branch name')
        self.check(dict(VALID, setup='pnpm install'), 'setup must be a list')
        self.check(dict(VALID, checkFast=[]), 'checkFast must list at least one')
        self.check(dict(VALID, checkFast=[{'name': 'x'}]), 'checkFast entries are')
        self.check(dict(VALID, checkFast=[{'name': 'x', 'run': 'y', 'retries': 2}]), 'checkFast entries are')
        self.check(dict(VALID, ci={'requiredChecks': ['x'], 'e2e': True}), 'ci must be')
        self.check(dict(VALID, migrations={'journal': 'x'}), 'migrations is')
        self.check(dict(VALID, migrations={'dir': 'x', 'appendOnly': True}), 'migrations is')
        self.check(dict(VALID, github={'protected': 'staging'}), 'github.protected must be a list')
        self.check(dict(VALID, gateConfig=[1]), 'gateConfig must be a list')

    def test_optional_keys_get_defaults(self):
        config = adapter.validate('a/r', dict(VALID, migrations={'dir': 'packages/db/drizzle'}))
        self.assertEqual((config['gateConfig'], config['dependencyFields'], config['github']), ([], [], {'protected': []}))
        self.assertEqual(config['migrations']['dir'], 'packages/db/drizzle/')

    def test_the_node_pnpm_preset_is_valid(self):
        draft = json.loads((Path(adapter.__file__).parent / 'sandbox/pipeline.node-pnpm.json').read_text())
        config = adapter.validate('acme/shop', draft)
        self.assertIn('one-migration', [check['name'] for check in config['checkFast']])

    def test_the_python_preset_is_valid(self):
        draft = json.loads((Path(adapter.__file__).parent / 'sandbox/pipeline.python.json').read_text())
        self.assertEqual([check['name'] for check in adapter.validate('acme/shop', draft)['checkFast']], ['test'])


class GlobTest(unittest.TestCase):
    def test_globs_match_like_gitignore(self):
        cases = [('pnpm-lock.yaml', ['pnpm-lock.yaml'], True), ('apps/web/pnpm-lock.yaml', ['pnpm-lock.yaml'], True),
                 ('.github/workflows/check.yml', ['.github/**'], True), ('docs/.github/x', ['.github/**'], False),
                 ('apps/api/tsconfig.build.json', ['tsconfig*.json'], True),
                 ('scripts/check-docs.ts', ['scripts/check-*'], True), ('scripts/sub/check-x.ts', ['scripts/check-*'], False),
                 ('apps/api/vitest.config.ts', ['vitest.config.*'], True), ('src/thing.ts', ['turbo.json'], False)]

        for path, patterns, expected in cases:
            self.assertEqual(adapter.matches(path, patterns), expected, (path, patterns))


if __name__ == '__main__':
    unittest.main()
