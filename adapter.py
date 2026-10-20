"""Repo adapter: which repos the pipeline serves (~/.claude/pipeline/config.json), where their features live, the
runner's clean clone of each, and each repo's recipe card (`.claude/pipeline.json` on its base branch).

Nothing here is repo-specific: a new repo is a registry line plus its committed pipeline.json.
"""
import contextlib
import fcntl
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

HOME = Path.home()
PIPELINE_DIR = Path(__file__).resolve().parent
CONFIG = PIPELINE_DIR / 'config.json'
LOGS = PIPELINE_DIR / 'logs'
CLONES = HOME / '.local/share/pipeline/repos'
GIT_TIMEOUT_SECONDS = 600
REPO_NAME = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
FEATURE_NAME = re.compile(r'^[a-z0-9][a-z0-9-]*$')
STEP_DIR = re.compile(r'^(\d{2})-([a-z0-9][a-z0-9-]*)$')

REQUIRED = ('base', 'setup', 'setupEgress', 'checkFast', 'protected', 'ci')
OPTIONAL = ('gateConfig', 'dependencyFields', 'migrations', 'github')
MIGRATION_KEYS = ('dir', 'journal', 'generate')
DEFAULT_ADMISSION = {'slots': 2, 'minMemoryGb': 10, 'weeklyMax': 80, 'fiveHourMax': 70}
# claude: 'host' follows the host's Claude Code, '2.1.286' pins one; build: 'auto' (the runner builds a missing one
# before a create) or 'manual' (only `pipeline template build`).
DEFAULT_TEMPLATE = {'claude': 'host', 'build': 'auto'}
CLAUDE_VERSION = re.compile(r'^\d+\.\d+\.\d+$')
# The CLI's command names (core sets it from its COMMANDS): a feature folder named like one is never a feature, since
# `pipeline <name>` (tick's run included) would run the command instead.
RESERVED = frozenset()


class AdapterError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- config


def load_config():
    """The pipeline's own config (no secrets): the GitHub App, the repo registry, the admission limits and the
    sandbox template policy."""
    try:
        config = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        raise AdapterError(f'no {CONFIG}: it names the GitHub App and the repos the pipeline serves') from None
    except ValueError as error:
        raise AdapterError(f'{CONFIG}: not JSON ({error})') from None

    app = config.get('app') or {}
    missing = [f'app.{key}' for key in ('id', 'slug', 'privateKey', 'reviewer') if not app.get(key)]

    if not isinstance(config.get('repos'), dict) or not config['repos']:
        missing.append('repos')

    if missing:
        raise AdapterError(f'{CONFIG} lacks {", ".join(missing)}')

    bad = [name for name in config['repos'] if not REPO_NAME.match(name)]

    if bad:
        raise AdapterError(f'{CONFIG}: repos are keyed "<owner>/<repo>", not {", ".join(bad)}')

    shorts = [name.split('/')[1] for name in config['repos']]

    if len(set(shorts)) != len(shorts):
        raise AdapterError(f'{CONFIG}: two repos share a name; sandbox and lock names need it unique')

    config['admission'] = dict(DEFAULT_ADMISSION, **(config.get('admission') or {}))
    template = config.get('template') or {}

    if not isinstance(template, dict) or set(template) - set(DEFAULT_TEMPLATE):
        raise AdapterError(f'{CONFIG}: template is {{"claude": "host" | "<x.y.z>", "build": "auto" | "manual"}}')

    template = config['template'] = dict(DEFAULT_TEMPLATE, **template)

    if template['claude'] != 'host' and not CLAUDE_VERSION.match(str(template['claude'])):
        raise AdapterError(f'{CONFIG}: template.claude is "host" or a version like "2.1.287", not {template["claude"]!r}')

    if template['build'] not in ('auto', 'manual'):
        raise AdapterError(f'{CONFIG}: template.build is "auto" or "manual", not {template["build"]!r}')

    config['app']['privateKey'] = str(Path(os.path.expanduser(app['privateKey'])))

    return config


def checkout(repo):
    """The registered checkout of a repo: where you work and where its feature folders live."""
    repos = load_config()['repos']

    if repo not in repos:
        raise AdapterError(f'{repo} is not registered in {CONFIG}')

    return Path(os.path.expanduser(repos[repo])).resolve()


def short(repo):
    return repo.split('/')[1]


def plans_root(repo):
    return checkout(repo) / '.claude/plans'


def backlog_root(repo):
    """Parked features: the `_` fails the feature-name rule, so nothing runs them (`pipeline start <name>` moves one out)."""
    return plans_root(repo) / '_backlog'


def feature_folders(repo, root=None):
    """Every folder of the repo's plans (or of `root`) that holds steps (NN-slug/), reserved names included."""
    root = root or plans_root(repo)

    if not root.is_dir():
        return []

    return sorted(path.name for path in root.iterdir()
                  if path.is_dir() and FEATURE_NAME.match(path.name)
                  and any(child.is_dir() and STEP_DIR.match(child.name) for child in path.iterdir()))


def repo_features(repo):
    return [name for name in feature_folders(repo) if name not in RESERVED]


def reserved_features(repo):
    """Feature folders named like a command: skipped everywhere; status names them so you rename them."""
    return [name for name in feature_folders(repo) if name in RESERVED]


def all_features():
    """(repo, feature) for every feature folder in every registered checkout."""
    return [(repo, feature) for repo in load_config()['repos'] for feature in repo_features(repo)]


def repo_for(feature, repo=None, cwd=None):
    """The repo whose checkout holds the feature folder: --repo if given, else the checkout you stand in, else the
    one registered checkout that has it. None or several matches is an error naming the fix."""
    repos = load_config()['repos']

    if repo:
        if repo not in repos:
            raise AdapterError(f'{repo} is not registered in {CONFIG}')

        return repo

    here = Path(cwd or os.getcwd()).resolve()

    for name in repos:
        root = checkout(name)

        if (here == root or root in here.parents) and (plans_root(name) / feature).is_dir():
            return name

    found = [name for name in repos if (plans_root(name) / feature).is_dir()]

    if not found:
        raise AdapterError(f'no feature {feature} in any registered checkout ({", ".join(repos)})')

    if len(found) > 1:
        raise AdapterError(f'feature {feature} exists in {", ".join(found)}: pass --repo <owner>/<repo>')

    return found[0]


# ---------------------------------------------------------------------------------------------- clean clone


@contextlib.contextmanager
def exclusive(name):
    """Waits for, then holds, a lock every pipeline process shares (fetching into or creating sandboxes from one
    clone must not overlap: sbx writes a remote into its config)."""
    LOGS.mkdir(parents=True, exist_ok=True)

    with (LOGS / f'{name}.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def clone_path(repo):
    return CLONES / repo


def remote_url(repo):
    return f'https://github.com/{repo}.git'


def git(directory, *arguments, env=None, check=True):
    result = subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, env=env, timeout=GIT_TIMEOUT_SECONDS)

    if check and result.returncode != 0:
        raise AdapterError(f'git {" ".join(arguments[:4])} in {directory}: {result.stderr.strip()[-500:]}')

    return result


# The clone receives refs fetched out of sandboxes, which the agent controls: git checks every object it takes in.
SAFE_FETCH = ('-c', 'transfer.fsckObjects=true', '-c', 'fetch.fsckObjects=true')


def clean_clone(repo, environment):
    """The runner's own clone of a repo, fetched from GitHub with the App token (`environment` carries it) and never
    checked out: sandboxes clone from it (sbx mounts it read-only at /run/sandbox/source) and it holds the refs the
    runner fetches out of them. Its local base branch follows origin, so a sandbox's `git fetch origin` sees the new
    base. Returns the path."""
    path = clone_path(repo)

    with exclusive(f'fetch-{short(repo)}'):
        if not (path / '.git').is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(['git', 'clone', '--quiet', '--no-checkout', remote_url(repo), str(path)],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL, env=environment,
                                    timeout=GIT_TIMEOUT_SECONDS)

            if result.returncode != 0:
                raise AdapterError(f'git clone {repo}: {result.stderr.strip()[-500:]}')

            for key, value in (('transfer.fsckObjects', 'true'), ('fetch.fsckObjects', 'true'), ('gc.auto', '0')):
                git(path, 'config', key, value)

        git(path, *SAFE_FETCH, 'fetch', '--quiet', '--prune', '--no-tags', 'origin', env=environment)
        git(path, 'remote', 'set-head', 'origin', '--auto', env=environment)
        base = repo_config(repo)['base']
        tip = git(path, 'rev-parse', '--verify', f'refs/remotes/origin/{base}').stdout.strip()
        git(path, 'update-ref', f'refs/heads/{base}', tip)
        git(path, 'symbolic-ref', 'HEAD', f'refs/heads/{base}')
        stray = [item.name for item in path.iterdir() if item.name != '.git']

        if stray:
            raise AdapterError(f'{path} holds files outside .git ({", ".join(stray[:5])}); a sandbox would see them '
                               'at /run/sandbox/source (env files included). Delete them, then re-run.')

    return path


# ---------------------------------------------------------------------------------------------- pipeline.json


def repo_config(repo, revision=None):
    """The repo's recipe card, read from its base branch in the clean clone (never an agent branch, never your
    checkout). `revision` defaults to origin's default branch."""
    path = clone_path(repo)
    revision = revision or 'refs/remotes/origin/HEAD'
    result = git(path, 'show', f'{revision}:.claude/pipeline.json', check=False)

    if result.returncode != 0:
        raise AdapterError(f'{repo}: no .claude/pipeline.json on {revision} (commit one; see README "New repo")')

    try:
        config = json.loads(result.stdout)
    except ValueError as error:
        raise AdapterError(f'{repo}: .claude/pipeline.json is not JSON ({error})') from None

    return validate(repo, config)


def validate(repo, config):
    where = f'{repo} .claude/pipeline.json'

    if not isinstance(config, dict):
        raise AdapterError(f'{where}: must be an object')

    unknown = sorted(set(config) - set(REQUIRED) - set(OPTIONAL))
    missing = [key for key in REQUIRED if key not in config]

    if unknown:
        raise AdapterError(f'{where}: unknown key {", ".join(unknown)}')

    if missing:
        raise AdapterError(f'{where}: missing key {", ".join(missing)}')

    def strings(key, value):
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise AdapterError(f'{where}: {key} must be a list of non-empty strings')

    if not isinstance(config['base'], str) or not config['base']:
        raise AdapterError(f'{where}: base must be a branch name')

    for key in ('setup', 'setupEgress', 'protected'):
        strings(key, config[key])

    for key in ('gateConfig', 'dependencyFields'):
        strings(key, config.setdefault(key, []))

    checks = config['checkFast']

    if not isinstance(checks, list) or not checks:
        raise AdapterError(f'{where}: checkFast must list at least one {{"name", "run"}}')

    for check in checks:
        if not isinstance(check, dict) or set(check) - {'name', 'run', 'flakeReruns'} or \
                not check.get('name') or not check.get('run'):
            raise AdapterError(f'{where}: checkFast entries are {{"name", "run", "flakeReruns"?}}: {check}')

    ci = config['ci']

    if not isinstance(ci, dict) or set(ci) != {'requiredChecks'}:
        raise AdapterError(f'{where}: ci must be {{"requiredChecks": [...]}}')

    strings('ci.requiredChecks', ci['requiredChecks'])
    migrations = config.setdefault('migrations', None)

    if migrations is not None:
        if not isinstance(migrations, dict) or set(migrations) - set(MIGRATION_KEYS) or not migrations.get('dir'):
            raise AdapterError(f'{where}: migrations is {{"dir", "journal"?, "generate"?}}')

        migrations['dir'] = migrations['dir'].rstrip('/') + '/'

    github = config.setdefault('github', {})

    if not isinstance(github, dict) or set(github) - {'protected'}:
        raise AdapterError(f'{where}: github is {{"protected": [branches]}}')

    strings('github.protected', github.setdefault('protected', []))

    return config


def matches(path, patterns):
    """gitignore-like: a pattern without a slash matches the file name at any depth; one with a slash is anchored at
    the repo root, and `**` crosses directories."""
    name = path.rsplit('/', 1)[-1]

    for pattern in patterns:
        if '/' not in pattern.rstrip('/'):
            if fnmatch.fnmatchcase(name, pattern):
                return True
            continue

        expression = re.escape(pattern.strip('/')).replace(r'\*\*', '.*').replace(r'\*', '[^/]*').replace(r'\?', '[^/]')

        if re.fullmatch(expression, path) or re.fullmatch(expression + '/.*', path):
            return True

    return False
