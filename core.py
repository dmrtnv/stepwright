#!/usr/bin/python3
"""Feature pipeline runner: one feature folder = numbered steps, each a fresh `claude -p` sequence (plan → implement →
review → respond) on its own branch, in its own Docker Sandboxes VM, gated by the runner and published as a draft PR
by a GitHub App. A step starts only after the previous one merged. Runbook: ~/.claude/pipeline/README.md. Commands:
`pipeline help` (the COMMANDS table below).

Modules: core (this: state machine, stages, gates, publish, PR loops, admission), backend (sbx), adapter (repos and
their .claude/pipeline.json).
"""
import base64
import calendar
import collections
import contextlib
import difflib
import fcntl
import http.client
import io
import json
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
import tty
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import adapter
import backend
from backend import sbx_host

HOME = Path.home()
PIPELINE_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT = PIPELINE_DIR / 'system.md'
RESULT_SCHEMA = PIPELINE_DIR / 'result.schema.json'
NTFY_TOPIC = HOME / '.config/ntfy/topic'  # one line, mode 600, never in a dotfiles repo; a companion CI runner may read it too
LOGS = PIPELINE_DIR / 'logs'
STOP_FLAG = LOGS / 'STOP'
USAGE = LOGS / 'usage.json'
CONTROL = LOGS / 'control.json'  # yours, from the CLI: parallel, drain, order (per feature: .pipeline/feature.json)
TICK_UNIT = 'pipeline-tick.service'
TEMPLATE_FAILED = LOGS / 'template-failed.json'
SBX_UNIT = sbx_host.UNIT  # the capped daemon that owns every VM (README "What runs where")
CI_STATE = HOME / '.local/state/ci-runner'  # a companion ci-runner's slot-<n>.json: what each CI slot does now
PRESSURE_BADGE_PERCENT = 1.0  # PSI full avg60 of sbx.service (memory.high throttling counts) that badges the status
PROJECTS = HOME / '.claude/projects'
CLAUDE = 'claude'
PYTHON = '/usr/bin/python3'
GITHUB_API = 'https://api.github.com'
GITHUB_API_VERSION = '2022-11-28'
TOKEN_MARGIN_SECONDS = 120
SECRET_PATH = re.compile(r'(^|/)(\.env(\.[^/]*)?|[^/]+\.env)$')
SECRET_TEXT = re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----')
EVIDENCE_MARKER = '<!-- pipeline-evidence -->'
# PR text is team-visible: home paths (yours or the VM's) are shortened and personal emails hidden; noreply ones stay.
HOME_PATH = re.compile(r'/home/[\w.-]+')
EMAIL = re.compile(r'\b[\w.+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b')
NOREPLY = '@users.noreply.github.com'
PERSONAL_TEXT = re.compile(r'/home/[\w.-]+|\b[\w.+-]+@(?!users\.noreply\.github\.com\b)(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b'
                           r'|\bMEMORY\.md\b|~/\.claude\b')

STAGES = {
    'plan': {'model': 'claude-opus-5-5', 'effort': 'high', 'hours': 3, 'budget': 40},
    'implement': {'model': 'claude-opus-5-5', 'effort': 'medium', 'hours': 5, 'budget': 60},
    'review': {'model': 'claude-opus-5-5', 'effort': 'high', 'hours': 3, 'budget': 20},
    'respond': {'model': 'claude-opus-5-5', 'effort': 'medium', 'hours': 4, 'budget': 30},
}
FALLBACK_MODEL = 'claude-sonnet-5-5'
USAGE_PROBE_MODEL = 'claude-haiku-4-5-20251001'
STAGE_ORDER = ('plan', 'implement', 'rebase', 'review', 'respond', 'update', 'publish', 'done')
VM_STAGES = ('plan', 'implement', 'rebase', 'review', 'respond', 'update')
BRANCH_PREFIX = 'agent'
MAX_ROUNDS = 2
GATE_RESUMES = 2
CI_FIXES = 2
CI_FIXES_MAX = 5
CI_RERUNS = 2
LIMIT_MARGIN_SECONDS = 120
LIMIT_UNKNOWN_WAIT_SECONDS = 30 * 60
STOP_GRACE_SECONDS = 120
KILL_SWITCH_WAIT_SECONDS = 300
CHECK_TIMEOUT_SECONDS = 3600
USAGE_STALE_SECONDS = 30 * 60
CI_SILENT_SECONDS = 30 * 60
MERGEABLE_RETRY_SECONDS = 5
GITHUB_RETRY_SECONDS = (2, 8)
GIT_TIMEOUT_SECONDS = 300
SNAPSHOT_DAYS = 14
LOG_DAYS = 14
MAX_PROMPT_CHARACTERS = 60_000
MAX_LISTED_FILES = 50
CI_LOG_CHARACTERS = 12_000
TRANSIENT_RETRIES = 3
TRANSIENT_WAIT_SECONDS = 15 * 60
TEMPLATE_PINGS = (1, 3)
TAR_STALE_SECONDS = 3600
NTFY_CHARACTERS = 1000
AUTONOMY = ('ask', 'auto')
AUTO_ANSWERED = 'Answered by autonomy auto: each recommendation below is the decision.'
AUTO_DECIDED = '⚑ auto-decided: '
ANSWER_CAP = 1000  # characters of your answer to a plan's questions the PR report quotes
UNRESOLVED = re.compile(r'^##[ \t]+Unresolved questions[ \t]*$', re.MULTILINE | re.IGNORECASE)
AFTER = re.compile(r'^([a-z0-9][a-z0-9-]*)(?::(\d{2}))?$')
LOG_LINES = 40

FEATURE_NAME = adapter.FEATURE_NAME
STEP_DIR = adapter.STEP_DIR
LIMIT_TEXT = re.compile(r"hit your [^.\n]{0,40}limit|usage limit|overloaded", re.IGNORECASE)
TRAILER = re.compile(r'^co-authored-by:.*(claude|anthropic)|generated with \[?claude code', re.IGNORECASE | re.MULTILINE)
LEADING_FLAG = re.compile(r'^\s*⚑\s*')
DATA_LOSS = re.compile(r'\b(DROP TABLE|DROP COLUMN|ALTER COLUMN\b.*\bTYPE\b|DROP TYPE|TRUNCATE|DELETE FROM)\b', re.IGNORECASE)
TEST_FILE = re.compile(r'(^|/)(test|tests|e2e|__tests__)/|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.py$')
PATH_PATTERN = re.compile(r'\*|\{[^}]*,[^}]*\}|\.\w+\(x\)')
TEST_OR_DOC = re.compile(r'(^|/)(test|tests|e2e|docs)/|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.py$|\.md$')
TEST_CASE_LINE = re.compile(r'^[-+]\s*((it|test)(\.\w+)?\(|(async\s+)?def test_)')
ONLY_LINE = re.compile(r'\b(it|test|describe|suite|context)(\.\w+)*\.only\b')
ERROR_LINE = re.compile(r'error|fatal|failed|denied|refused', re.IGNORECASE)
SKIP_LINE = re.compile(r'\b(it|test|describe|suite|context)(\.\w+)*\.(skip|todo|fixme)\b|\bx(it|test|describe)\(|'
                       r'\b(unittest|pytest\.mark)\.skip|\bskipTest\(')

RESUME_INTERRUPTED = ('The previous run of this stage was interrupted (timeout, crash, usage limit, the kill switch or a '
                      'stopped runner). Continue where you stopped, then finish with the structured result.')
RESUME_BLOCKED = ('The user re-ran the pipeline after your `{status}` result. Their answers, if any, are under '
                  '`## Unresolved questions` in {document}. Re-check what stopped you and continue; finish with the '
                  'structured result.')
BASE_MOVED = ('{base} moved since your last run ({start} → {tip}); the runner moved your branch onto it. Re-check '
              'anything you read on the old base before relying on it.')
RESUME_GATES = ("The runner's gates failed after your stage. Fix the causes (never weaken, skip or delete a check or a "
                'test), commit, and finish with the structured result.\n\n{failures}')
RESUME_CI = ("CI failed on the pull request for {sha} ({checks}). Fix the cause (never weaken, skip or delete a check or "
             "a test), commit, and finish with the structured result. A failing test stopped at its first failed "
             'assertion, so its later steps never ran in CI: before you commit, trace each later step and assertion of '
             'every failing test against the source (response shapes, copy, state preconditions) and fix those too. '
             "The runner's gates run after you, then it pushes. Tail of the failed job log:\n\n{log}")
RESUME_LOST = ('Your earlier session of this stage is gone (its sandbox was recreated), so this is a fresh session on '
               'the same branch. Commits over {base}:\n{commits}\n\nPR text (pr.md):\n{pr_text}')
REVISE = ('The user wrote `## Feedback` in {plan}. Revise the plan accordingly (critic round included), then finish '
          'with the structured result.')
REBASE = ('origin/{base} moved and `git rebase origin/{base}` conflicts (aborted; the runner kept the pre-rebase branch '
          'as {backup}). Rebase onto origin/{base} and resolve the conflicts, following the repo\'s rules for this '
          '(CLAUDE.md or memory; for migrations, their "main moved on" recipe and the plan\'s declared renames). Finish '
          'with the structured result; the runner re-runs setup and the gates after you.')
PR_COMMENTS = ('{review} holds the user\'s own comments on the pull request (PR #{number}), not a pipeline review. '
               'Address every one (or answer why not in pr.md), update pr.md, and finish with the structured result.')


class PipelineError(Exception):
    pass


ERRORS = (PipelineError, adapter.AdapterError, backend.BackendError)


class Stop(Exception):
    """The step paused, waits for the user or for admission, or stopped on --step/--until; its state says why."""


# ---------------------------------------------------------------------------------------------- processes


@contextlib.contextmanager
def signals_ignored():
    """A second Ctrl+C, a closed terminal or a kill must not cut a cleanup short and orphan a stage."""
    handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    main_thread = threading.current_thread() is threading.main_thread()
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in handled} if main_thread else {}

    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def read_lines(stream, consume):
    try:
        with stream:
            for line in stream:
                consume(line)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------------------------- git (host)


def git_result(directory, *arguments, env=None):
    try:
        return subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, text=True,
                              errors='replace', stdin=subprocess.DEVNULL, env=env, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise PipelineError(f'git {" ".join(arguments)} in {directory} timed out after {GIT_TIMEOUT_SECONDS}s') from None


def git(directory, *arguments, env=None):
    result = git_result(directory, *arguments, env=env)

    if result.returncode != 0:
        raise PipelineError(f'git {" ".join(arguments)} in {directory}: {result.stderr.strip()}')

    return result.stdout.strip()


def git_succeeds(directory, *arguments):
    return git_result(directory, *arguments).returncode == 0


def tail(text, lines=40):
    return '\n'.join((text or '').rstrip().splitlines()[-lines:])


def cap(text, characters):
    return text if len(text) <= characters else text[:characters] + f'\n… ({len(text) - characters} characters cut)'


def error_line(text):
    """The line of a tool's output that names the cause: the last one that reads like an error, else the last one."""
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    matching = [line for line in lines if ERROR_LINE.search(line)]

    return (matching or lines or [''])[-1][:200]


def headline(reason):
    """A reason's first line; one that ends in ':' (its cause on a later line) gets that cause appended."""
    lines = (reason or '').splitlines() or ['']

    return lines[0] + (' ' + error_line('\n'.join(lines[1:])) if lines[0].endswith(':') else '')


def plural(count, noun, nouns=None):
    return f"{count} {noun if count == 1 else nouns or noun + 's'}"


def parse_json(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {}

    return value if isinstance(value, dict) else {}


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def parse_iso(text):
    try:
        return calendar.timegm(time.strptime(text[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------- GitHub App


def app_settings():
    return adapter.load_config()['app']


def check_key(app):
    """The App's private key: present and readable by you alone (never in a dotfiles repo, never in a sandbox)."""
    key = Path(app['privateKey'])

    try:
        mode = key.stat().st_mode
    except OSError:
        raise PipelineError(f'the GitHub App key {key} is missing: generate one in the App settings, save it there '
                            'with mode 600 (`pipeline prove-app` checks it)') from None

    if mode & 0o077:
        raise PipelineError(f'the GitHub App key {key} is mode {oct(mode & 0o777)}: chmod 600 it')

    return key


def base64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def app_jwt(app):
    """A 10-minute RS256 JWT for the App, signed by openssl so the key never enters this process."""
    key = check_key(app)
    now = int(time.time())
    header = base64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    payload = base64url(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': str(app['id'])}).encode())
    signing_input = f'{header}.{payload}'.encode()
    signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key)], input=signing_input,
                            capture_output=True, timeout=30)

    if signed.returncode != 0:
        raise PipelineError(f'openssl could not sign with {key}: {signed.stderr.decode(errors="replace").strip()}')

    return f'{header}.{payload}.{base64url(signed.stdout)}'


def github_read(open_request, method, label):
    """The body of one GitHub request, tried again GITHUB_RETRY_SECONDS later after a network failure (the box's DNS
    or WAN stalls): one before GitHub had the request (connect, send) on any method, one while reading its answer only
    on a GET (a POST may have landed). An HTTP status is the caller's: raised as is."""
    for wait in (*GITHUB_RETRY_SECONDS, None):
        try:
            with open_request() as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as error:
            failure = error.reason
        except (OSError, http.client.HTTPException) as error:
            if method != 'GET':
                raise PipelineError(f'GitHub {label}: {error}') from None

            failure = error

        if wait is None:
            raise PipelineError(f'GitHub {label}: {failure}')

        time.sleep(wait)


def github_api(method, path, token, body=None):
    """One GitHub REST (or /graphql) call; the JSON answer, or a PipelineError naming the status and GitHub's message."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(GITHUB_API + path, data=data, method=method, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': GITHUB_API_VERSION, 'User-Agent': 'agent-pipeline',
        **({'Content-Type': 'application/json'} if data is not None else {}),
    })

    try:
        text = github_read(lambda: urllib.request.urlopen(request, timeout=60), method, f'{method} {path}').decode()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors='replace')[:300]

        raise PipelineError(f'GitHub {method} {path}: {error.code} {detail}') from None

    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        raise PipelineError(f'GitHub {method} {path}: not JSON: {text[:200]}') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def github_raw(path, token):
    """A GitHub download that answers with a redirect (job logs): the signed URL is fetched without the token."""
    request = urllib.request.Request(GITHUB_API + path, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': GITHUB_API_VERSION, 'User-Agent': 'agent-pipeline'})

    opener = urllib.request.build_opener(NoRedirect)

    try:
        return github_read(lambda: opener.open(request, timeout=60), 'GET', f'GET {path}').decode(errors='replace')
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 307, 308) or not error.headers.get('Location'):
            raise PipelineError(f'GitHub GET {path}: {error.code}') from None

        location = error.headers['Location']

    signed = urllib.request.Request(location, headers={'User-Agent': 'agent-pipeline'})

    try:
        return github_read(lambda: urllib.request.urlopen(signed, timeout=120), 'GET',
                           f'GET {path} (redirected)').decode(errors='replace')
    except urllib.error.HTTPError as error:
        raise PipelineError(f'GitHub GET {path} (redirected): {error}') from None


def graphql(token, query, variables):
    answer = github_api('POST', '/graphql', token, {'query': query, 'variables': variables})

    if answer.get('errors'):
        raise PipelineError(f'GitHub GraphQL: {json.dumps(answer["errors"])[:300]}')

    return answer.get('data') or {}


TOKEN_CACHE = {}
BOT_IDENTITY = {}


def bot_identity(repo):
    """The App's bot user as a git identity: agent commits name it as author and committer, so a squash merge (authored
    by the bot, the PR author) adds no Co-authored-by trailer for the host identity sbx copies into the VM."""
    if 'identity' not in BOT_IDENTITY:
        login = f"{app_settings()['slug']}[bot]"
        user = github_api('GET', f'/users/{login}', installation_token(repo))
        BOT_IDENTITY['identity'] = (login, f"{user['id']}+{login}{NOREPLY}")

    return BOT_IDENTITY['identity']


def installation_token(repo):
    """A one-hour installation token scoped to this one repo, minted once per run and kept only in memory."""
    cached = TOKEN_CACHE.get(repo)

    if cached and time.time() < cached['expires'] - TOKEN_MARGIN_SECONDS:
        return cached['token']

    jwt = app_jwt(app_settings())
    installation = github_api('GET', f'/repos/{repo}/installation', jwt).get('id')

    if not installation:
        raise PipelineError(f'the GitHub App is not installed on {repo}')

    answer = github_api('POST', f'/app/installations/{installation}/access_tokens', jwt,
                        {'repositories': [adapter.short(repo)]})
    expires = parse_iso(answer['expires_at']) if answer.get('expires_at') else time.time() + 3300
    TOKEN_CACHE[repo] = {'token': answer['token'], 'expires': expires, 'installation': installation,
                         'permissions': answer.get('permissions', {})}

    return answer['token']


def token_environment(token):
    """Git reads the credential from its environment, never from argv or a file."""
    basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode()

    return dict(os.environ, GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='http.extraHeader',
                GIT_CONFIG_VALUE_0=f'Authorization: Basic {basic}', GIT_CONFIG_KEY_1='credential.helper',
                GIT_CONFIG_VALUE_1='', GIT_TERMINAL_PROMPT='0')


def push_url(repo):
    return adapter.remote_url(repo)


# ---------------------------------------------------------------------------------------------- features and steps


def feature_dir(repo, feature):
    return adapter.plans_root(repo) / feature


def state_dir(repo, feature):
    return feature_dir(repo, feature) / '.pipeline'


def state_file(repo, feature, step_id):
    return state_dir(repo, feature) / f'{step_id}.json'


class Step:
    """One numbered step folder: `NN-slug/` holding brief.md, plan.md, review-K.md, pr.md and done.md."""

    def __init__(self, repo, feature, directory):
        match = STEP_DIR.match(directory.name)
        self.repo = repo
        self.feature = feature
        self.directory = directory
        self.id, self.slug = match.group(1), match.group(2)
        self.brief = directory / 'brief.md'
        self.plan = directory / 'plan.md'
        self.pr_text = directory / 'pr.md'
        self.done_notice = directory / 'done.md'

    def review(self, round_number):
        return self.directory / f'review-{round_number}.md'

    @property
    def document(self):
        return self.plan if self.plan.exists() else self.brief

    @property
    def name(self):
        return f'{self.id}-{self.slug}'

    @property
    def branch(self):
        return f'{BRANCH_PREFIX}/{self.feature}-{self.name}'

    def vm_path(self, path):
        """The same file inside the step's sandbox (the feature folder is copied to backend.plans_dir)."""
        name = backend.sandbox_name(adapter.short(self.repo), self.feature, self.id)

        return f'{backend.plans_dir(name, self.feature)}/{self.name}/{Path(path).name}'


def steps(repo, feature):
    directory = feature_dir(repo, feature)

    if not FEATURE_NAME.match(feature) or not directory.is_dir():
        raise PipelineError(f'no feature folder {directory} (a name of lowercase letters, digits and dashes)')

    found = sorted((Step(repo, feature, path) for path in directory.iterdir()
                    if path.is_dir() and STEP_DIR.match(path.name)), key=lambda step: step.id)
    ids = collections.Counter(step.id for step in found)
    duplicates = sorted(step_id for step_id, count in ids.items() if count > 1)

    if duplicates:
        raise PipelineError(f'{directory}: step numbers used twice: {", ".join(duplicates)}')

    if not found:
        raise PipelineError(f'{directory} has no step folders (NN-slug/brief.md)')

    return found


def step_of(state):
    return next(step for step in steps(state['repo'], state['feature']) if step.id == state['id'])


# ---------------------------------------------------------------------------------------------- state


def new_state(repo, feature, step_id):
    return {
        'repo': repo, 'feature': feature, 'id': step_id, 'stage': 'plan', 'status': 'ready', 'reason': '',
        'branch': None, 'sandbox_ready': False, 'round': 1, 'approved': False, 'sessions': {}, 'pending': {},
        'results': {}, 'started': {}, 'finals': {}, 'respond_base': None, 'green_head': None, 'post_rebase': None,
        'pr': None, 'published_sha': None, 'published_at': None,
        'ci': {'sha': None, 'fixes': 0, 'reviewed': None, 'reruns': 0},
        'update': None, 'responded_through': None, 'not_before': None, 'evidence_comment': None, 'merged': None,
        'public_notice': None,
        'usage': {}, 'denials': [], 'copy_errors': {}, 'notes': [], 'history': [],
    }


def load_state(repo, feature, step_id):
    path = state_file(repo, feature, step_id)
    state = new_state(repo, feature, step_id)

    if path.exists():
        try:
            state.update(json.loads(path.read_text()))
        except (OSError, ValueError) as error:
            raise PipelineError(f'{path}: unreadable ({error}); fix or delete it') from None

    state['repo'] = repo

    return state


def save_state(state):
    path = state_file(state['repo'], state['feature'], state['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    os.replace(temporary, path)


def mark(state, status, reason):
    state['status'] = status
    state['reason'] = reason
    state['history'].append({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'stage': state['stage'], 'status': status,
                             'reason': reason[:300]})
    save_state(state)


def note(state, text):
    if text not in state['notes']:
        state['notes'].append(text)
        save_state(state)


def pause(state, reason, short):
    mark(state, 'paused', reason)
    notify(state, f'paused: {short}', priority='high')

    raise Stop()


def wait_ready(state, reason, ping, priority='low'):
    """The step waits (queued for admission, or for a usage limit to reset) and the run ends; tick resumes it. A
    reason that did not change is neither recorded again nor pinged again; it heals itself, so the ping is low."""
    if state['status'] == 'ready' and state['reason'] == reason:
        raise Stop()

    mark(state, 'ready', reason)

    if ping:
        notify(state, ping, priority=priority)

    raise Stop()


def retry_later(state, what, output, fix, short):
    """A per-step environment failure (sbx create, the VM's git, a base fetch, a setup command): the step waits and
    tick retries it, TRANSIENT_RETRIES times TRANSIENT_WAIT_SECONDS apart; then it pauses for you. Pings on the first retry and the
    pause. Queue entries in between do not reset the count; any other one (a stage ran) does."""
    line = error_line(output)
    reason = f'{what} failed: {line}\nfix: {fix}\n\n{tail(output, 10)}'
    tries = 0

    for entry in reversed(state['history']):
        if entry['reason'].startswith('queued:'):
            continue

        if not entry['reason'].startswith('retrying '):
            break

        tries += 1

    if tries >= TRANSIENT_RETRIES:
        pause(state, f'{reason}\n\n(after {plural(tries, "retry", "retries")})', short)

    state['not_before'] = time.time() + TRANSIENT_WAIT_SECONDS
    save_state(state)
    wait_ready(state, f'retrying ({tries + 1}/{TRANSIENT_RETRIES}) after: {reason}',
               f'{short}, retrying in {TRANSIENT_WAIT_SECONDS // 60} min: {line}' if tries == 0 else None)


# ---------------------------------------------------------------------------------------------- notifications


def ntfy_topic():
    try:
        return NTFY_TOPIC.read_text().strip() or None
    except OSError:
        return None


def notify(state, message, click=None, priority='default'):
    """A phone ping (ntfy) and a log line. `click` opens a URL (the PR) when the notification is tapped; `priority`
    (ntfy's: high for what needs you, default for news, low for what heals itself) decides how loud it is."""
    title = f"{adapter.short(state['repo'])} {state['feature']} {state['id']}"
    print(f'[{title}] {message}', flush=True)
    topic = ntfy_topic()

    if not topic:
        print(f'  ntfy: no topic at {NTFY_TOPIC}', flush=True)
        return

    # ntfy turns a message over 4,096 bytes into an attachment: 1,000 characters stay a message.
    request = urllib.request.Request(f'https://ntfy.sh/{topic}', data=message[:NTFY_CHARACTERS].encode(),
                                     headers={'Title': title, 'Priority': priority,
                                              **({'Click': click} if click else {})})

    try:
        urllib.request.urlopen(request, timeout=10).close()
    except OSError as error:
        print(f'  ntfy failed: {error}', flush=True)


# ---------------------------------------------------------------------------------------------- step context


REPO_CONFIG = {}
KIT_COPIED = set()
ACTIVE_SANDBOXES = set()


def step_context(state):
    """Where a step lives: its sandbox, the VM workspace, the host clone and the host ref that holds its branch."""
    repo = state['repo']
    name = backend.sandbox_name(adapter.short(repo), state['feature'], state['id'])
    clone = adapter.clone_path(repo)
    config = REPO_CONFIG.get(repo) or adapter.repo_config(repo)
    REPO_CONFIG[repo] = config

    return {'name': name, 'clone': clone, 'workspace': backend.workspace(name, clone), 'config': config,
            'base': config['base'], 'origin_base': f"refs/remotes/origin/{config['base']}",
            'ref': f"refs/pipeline/{state['feature']}/{state['id']}"}


def ref_sha(context):
    return git_result(context['clone'], 'rev-parse', '--verify', '--quiet', context['ref']).stdout.strip() or None


def merge_base(context):
    return git(context['clone'], 'merge-base', context['ref'], context['origin_base'])


def base_sha(context):
    return git(context['clone'], 'rev-parse', context['origin_base'])


def changed_files(context, base, *arguments):
    names = git(context['clone'], 'diff', '--name-only', '--no-renames', *arguments, base, context['ref']).splitlines()

    return [name for name in names if name]


def json_at(context, revision, path):
    result = git_result(context['clone'], 'show', f'{revision}:{path}')

    return parse_json(result.stdout) if result.returncode == 0 else {}


def memory_source(repo):
    """The repo's local memory (the workflow overlay): Claude keys projects by the checkout path, / → -."""
    return PROJECTS / re.sub(r'[^A-Za-z0-9]', '-', str(adapter.checkout(repo))) / 'memory'


def sync_ref(state, context):
    """Quiesces the VM (no stray process may move the branch afterwards), then fetches the branch to the host ref.
    First the VMs' host peaks are sampled: the stage's VM is at its highest now (sbx_host.sample)."""
    sample_peaks()
    backend.quiesce(context['name'])
    sha = backend.fetch_branch(context['name'], context['clone'], state['branch'], context['ref'],
                               context['origin_base'])

    return sha


def sample_peaks():
    try:
        sbx_host.sample()
    except (OSError, ValueError) as error:
        print(f'sampling VM peaks failed: {error}', file=sys.stderr)


def append_log(state, suffix, text):
    log = state_dir(state['repo'], state['feature']) / f"{state['id']}-{suffix}.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    with log.open('a') as handle:
        handle.write(f"== {time.strftime('%Y-%m-%dT%H:%M:%S')} {text.rstrip()}\n\n")


def template_policy():
    return adapter.load_config()['template']


def target_tag(policy=None):
    """The template new sandboxes are created from (config.json "template".claude: the host's Claude Code or a pin)."""
    return backend.template_tag((policy or template_policy())['claude'])


def template_name(tag):
    return tag.rpartition(':')[2]


def template_marker():
    """{tag, failures, at, error} of the template the runner could not get (built and failed, or missing in manual
    mode), or {}."""
    return parse_json(TEMPLATE_FAILED.read_text()) if TEMPLATE_FAILED.exists() else {}


def write_template_marker(marker):
    LOGS.mkdir(parents=True, exist_ok=True)
    temporary = TEMPLATE_FAILED.with_name(TEMPLATE_FAILED.name + '.tmp')
    temporary.write_text(json.dumps(marker) + '\n')
    os.replace(temporary, TEMPLATE_FAILED)


def wait_for_template(state, until, reason, ping):
    """A missing template is global and repairable: the step waits (never paused), tick retries; the reason is the
    same for every step, so each records it once."""
    state['not_before'] = until
    save_state(state)
    wait_ready(state, reason, ping)


def ensure_template(state):
    """The tag a create uses: the target, built first when it is missing and the policy is `auto` (one build at a time
    across runs, a failed one retried at most every TRANSIENT_WAIT_SECONDS for all steps, pinged on its 1st and 3rd
    failure). `manual` never builds: the step waits for `pipeline template build`, one ping per tag."""
    policy = template_policy()

    with adapter.exclusive('template'):
        try:
            tag = target_tag(policy)
            loaded = backend.template_loaded(tag)
        except backend.BackendError as error:
            retry_later(state, 'checking the sandbox template', str(error),
                        'claude --version; node --version; sbx template ls', 'template check failed')

        if loaded:
            return tag

        name, marker, now = template_name(tag), template_marker(), time.time()
        fix = 'pipeline template build'

        if policy['build'] == 'manual':
            first = marker.get('tag') != tag

            if first:
                write_template_marker({'tag': tag, 'failures': 0, 'at': now, 'error': ''})

            wait_for_template(state, now + TRANSIENT_WAIT_SECONDS, f'template {name} missing\nfix: {fix}',
                              f'template {name} missing: {fix}' if first else None)

        failures = marker.get('failures', 0) if marker.get('tag') == tag else 0

        if failures and marker['at'] + TRANSIENT_WAIT_SECONDS > now:
            wait_for_template(state, marker['at'] + TRANSIENT_WAIT_SECONDS,
                              f"waiting for template {name}: build failed ({failures}): {marker['error']}\nfix: {fix}",
                              None)

        print(f'building template {name} (config.json "template": {policy["claude"]}, auto)', flush=True)
        started = time.monotonic()
        built, result = backend.build_template(policy['claude'])

        if built:
            TEMPLATE_FAILED.unlink(missing_ok=True)
            note(state, f'built template {template_name(built)} ({time.monotonic() - started:.0f} s)')

            return built

        failures, failed_at = failures + 1, time.time()
        line = error_line(result.stderr + '\n' + result.stdout)
        write_template_marker({'tag': tag, 'failures': failures, 'at': failed_at, 'error': line})
        wait_for_template(state, failed_at + TRANSIENT_WAIT_SECONDS,
                          f'waiting for template {name}: build failed ({failures}): {line}\nfix: {fix}\n\n'
                          f'{tail(result.stderr or result.stdout, 10)}',
                          f'template {name} build failed ({failures}): {line}; fix: {fix}'
                          if failures in TEMPLATE_PINGS else None)


def ensure_sandbox(state):
    """The step's VM, running: created (clone of the host clone, egress locked) when missing, then per run the kit,
    and per sandbox the branch (fresh from origin/<base>, or from the host ref when the VM was lost) and setup."""
    context = step_context(state)
    name = context['name']
    current = backend.status(name)

    if current is None:
        if state['sandbox_ready']:
            note(state, f"sandbox {name} was gone: recreated from {context['ref']}")
            state['sandbox_ready'] = False
            save_state(state)

        tag = ensure_template(state)

        with adapter.exclusive(f"fetch-{adapter.short(state['repo'])}"):
            created = backend.create(name, tag, context['clone'], clone=True)

        if created.returncode != 0:
            backend.remove(name)  # a half-made VM must never be reused without its egress lock
            retry_later(state, f'sbx create {name}', created.stderr or created.stdout,
                        'sbx ls; sbx diagnose; pipeline template', 'sandbox create failed')

        backend.lock_egress(name)
        KIT_COPIED.discard(name)
    else:
        if current == 'running' and name not in ACTIVE_SANDBOXES:
            # This run stops its VMs after each stage and at exit, and the run lock excludes a second run: a running
            # VM it never touched is a dead run's (OOM, kill -9), whose claude would work beside the resumed one.
            note(state, f'sandbox {name} was left running by an earlier run: stopped it first')
            backend.quiesce(name)

        backend.ensure_running(name)

    ACTIVE_SANDBOXES.add(name)

    if name not in KIT_COPIED:
        backend.copy_kit(name, memory_source(state['repo']))
        configured = backend.configure_git(name, context['workspace'], *bot_identity(state['repo']))

        if configured.returncode != 0:
            retry_later(state, f'configuring git in {name} (origin, commit identity)',
                        configured.stderr or configured.stdout, f'sbx exec -it {name} bash; git config -l',
                        'sandbox git failed')

        KIT_COPIED.add(name)

    if not state['sandbox_ready']:
        make_branch(state, context)
        run_setup(state, context)
        state['sandbox_ready'] = True
        save_state(state)

    return context


def refresh_base(state, context):
    fetched = backend.refresh_base(context['name'], context['workspace'], context['base'])

    if fetched.returncode != 0:
        retry_later(state, f"fetching origin/{context['base']} into {context['name']}", fetched.stderr or fetched.stdout,
                    f"sbx exec -it {context['name']} bash; git fetch origin {context['base']}", 'base fetch failed')


def make_branch(state, context):
    step = step_of(state)
    name, workspace, branch = context['name'], context['workspace'], step.branch
    base = context['base']
    refresh_base(state, context)

    def vm(*command):
        return backend.exec(name, list(command), workdir=workspace, timeout=300)

    if vm('git', 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}').returncode == 0:
        checked = vm('git', 'checkout', '-q', branch)
    elif ref_sha(context):
        checked = vm('bash', '-c', f"git fetch --quiet origin +{context['ref']}:refs/heads/{branch} && "
                                   f'git checkout -q {branch}')
    else:
        remote = git_result(context['clone'], 'rev-parse', '--verify', '--quiet', f'refs/remotes/origin/{branch}')
        tip = remote.stdout.strip()

        if tip and not git_succeeds(context['clone'], 'merge-base', '--is-ancestor', tip, context['origin_base']):
            pause(state, f'The step\'s branch {branch} already exists on GitHub with commits of its own ({tip[:12]}). '
                         'Delete that branch (or rename the step folder), then re-run.', 'branch exists')

        if tip:
            note(state, f'leftover GitHub branch {branch} ({tip[:12]}) holds nothing of its own: branched fresh')

        checked = vm('git', 'checkout', '-q', '--no-track', '-b', branch, f'origin/{base}')

    if checked.returncode != 0:
        pause(state, f'making branch {branch} in {name} failed:\n{tail(checked.stderr, 10)}', 'branch failed')

    state['branch'] = branch
    save_state(state)
    backend.fetch_branch(name, context['clone'], branch, context['ref'], context['origin_base'])


def run_setup(state, context):
    config = context['config']
    failures, seal_failures = backend.setup(context['name'], context['workspace'], config['setup'],
                                            config['setupEgress'], lambda text: append_log(state, 'setup', text))
    log = f".pipeline/{state['id']}-setup.log"

    if seal_failures:
        pause(state, f'setup failed in the sandbox (log: {log}):\n\n' + '\n\n'.join(seal_failures + failures),
              'setup failed')

    if failures:
        retry_later(state, f'setup in the sandbox (log: {log})', '\n\n'.join(failures),
                    'read the setup log; check pipeline.json setup', 'setup failed')


# ---------------------------------------------------------------------------------------------- admission


SLOT_HANDLES = []


def stop_flag():
    return STOP_FLAG.exists()


def take_slot(parallel, ceiling):
    """A run holds one slot lock from its first admitted stage until it exits (the kernel frees it with the process,
    so a crash leaves nothing stale). It takes one only while fewer than `parallel` are held (any slot file counts: one
    above a lowered ceiling too); counting and taking happen under one lock, so two runs never both take the last."""
    if SLOT_HANDLES:
        return True

    LOGS.mkdir(parents=True, exist_ok=True)

    with adapter.exclusive('slots'):
        if slots_held() >= parallel:
            return False

        for index in range(ceiling):
            handle = (LOGS / f'slot-{index}.lock').open('a+')

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue

            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            SLOT_HANDLES.append(handle)

            return True

    return False


def release_slot():
    while SLOT_HANDLES:
        SLOT_HANDLES.pop().close()


def slots_held():
    return sum(1 for path in LOGS.glob('slot-*.lock') if locked_path(path))


def read_usage():
    usage = parse_json(USAGE.read_text()) if USAGE.exists() else {}

    return usage if usage.get('at') else None


def write_usage(windows):
    if not isinstance(windows, dict) or not windows:
        return

    LOGS.mkdir(parents=True, exist_ok=True)
    temporary = USAGE.with_name('usage.json.tmp')
    temporary.write_text(json.dumps({'at': time.time(), **windows}) + '\n')
    os.replace(temporary, USAGE)


def usage_probe():
    """A tiny Haiku call on the host (no tools, no session kept) whose rate_limit_event refreshes usage.json, so your
    manual use since the last stage is seen. Returns an error text or None."""
    try:
        result = subprocess.run([CLAUDE, '-p', 'Reply with OK only.', '--model', USAGE_PROBE_MODEL, '--tools', '',
                                 '--no-session-persistence', '--output-format', 'stream-json', '--verbose'],
                                capture_output=True, text=True, errors='replace', timeout=180, cwd=str(LOGS),
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'usage probe failed: {error}'

    windows, rejected = None, None

    for line in result.stdout.splitlines():
        event = parse_json(line)
        info = event.get('rate_limit_info') or {}

        if event.get('type') == 'rate_limit_event':
            windows = info.get('unifiedWindows') or windows
            rejected = info if info.get('status') == 'rejected' else rejected

    if windows:
        write_usage(windows)

    if rejected:
        return f"usage limit ({rejected.get('rateLimitType') or 'usage'}) reached"

    return None if windows else f'usage probe gave no usage (exit {result.returncode}: {tail(result.stderr, 2)})'


def usage_problem(limits, probe=True):
    """A usage window at or above its limit, or None. A stale reading is refreshed by a probe first; without `probe`
    (the status) the last reading decides, and none is no problem."""
    usage = read_usage()

    if probe and (not usage or time.time() - usage['at'] > USAGE_STALE_SECONDS):
        error = usage_probe()

        if error:
            return error

        usage = read_usage()

    if not usage:
        return None

    for window, key, label in (('seven_day', 'weeklyMax', 'weekly'), ('five_hour', 'fiveHourMax', '5-hour')):
        info = usage.get(window) or {}
        utilization = info.get('utilization')

        if not isinstance(utilization, (int, float)) or (info.get('resetsAt') or 0) < time.time():
            continue

        percent = utilization * 100

        if percent >= limits[key]:
            return f'{label} usage {percent:.0f}% ≥ {limits[key]}%'

    return None


def sbx_service_active():
    return sbx_host.active()


def gib(value):
    """Bytes as a GiB number text: ? when unreadable, max when uncapped."""
    return '?' if value is None else 'max' if value == float('inf') else f'{value / 2**30:.1f}'


def floor_tenth(value):
    """One decimal, rounded down: 9.99 GiB free is below a 10 GiB floor and must not read 10.0."""
    return f'{int(value * 10) / 10:.1f}'


def memory_reading(limits):
    """sbx.service's memory (sbx_host.reading), the VMs, every reservation and the budget they are summed against
    (sbx_host), and the host's free memory against admission.minMemoryGb. Only while the unit is active: `vms` and
    `reservations` are None when sbx can't tell."""
    memory = dict(sbx_host.reading(), free=sbx_host.mem_available_gib(), floor=limits['minMemoryGb'], vms=None,
                  reservations=None, budget=None, budget_name=None)

    try:
        config = sbx_host.load_config()
        memory.update(vms=sbx_host.vms(), budget=sbx_host.budget_mib(config, memory), budget_name=config['budget'])
        memory['reservations'] = sbx_host.reservations(config, memory['vms'])
    except sbx_host.HostError:
        pass

    return memory


def is_number(text):
    try:
        float(text)
    except ValueError:
        return False

    return True


def sbx_service_summary(active, memory):
    """`status -v`'s unit line: the VMs' memory against both caps, swap, OOM kills, pressure, shims inside the unit
    (fewer than all ⇒ a VM escaped the caps: a daemon started by hand)."""
    if not active:
        return f'{SBX_UNIT}: not active — stages queue'

    oom = '?' if memory['oom'] is None else memory['oom']
    avg60 = '?' if memory['avg60'] is None else f"{memory['avg60']:.2f}"

    return (f"{SBX_UNIT}: VMs use {gib(memory['current'])} GiB (anon {gib(memory['anon'])}, peak "
            f"{gib(memory['peak'])}) of {gib(memory['high'])} soft / {gib(memory['max'])} hard · swap "
            f"{gib(memory['swap'])} of {gib(memory['swap_max'])} · oom {oom} · pressure full avg60 {avg60}% · "
            f"shims {memory['inside']}/{memory['total']}")


def reserved_summary(memory):
    """`status -v`'s reservations line: the total against the budget, then each VM (and the CI lane) at its reserve."""
    counted = memory['reservations']

    if counted is None:
        return "reserved: ? (sbx can't tell)"

    total = sum(mib for _, _, mib in counted)
    budget = memory['budget']
    head = f'reserved: {total / 1024:.1f} GiB' + (f" of {budget / 1024:.1f} ({memory['budget_name']})"
                                                  if budget is not None else '')

    return ' · '.join([head] + [f'{name} {mib / 1024:.1f}' for name, _, mib in counted])


def admission_problem(slot=True, probe=True):
    """Why no stage may start now, or None. With `slot` a run slot is taken (and kept); tick asks without one. Without
    `probe` (the status) a stale usage reading is not refreshed."""
    limits = adapter.load_config()['admission']

    if stop_flag():
        return 'kill switch on (pipeline stop --clear)'

    if not sbx_service_active():
        return f'{SBX_UNIT} not active (systemctl --user start {SBX_UNIT})'

    parallel = live_parallel(limits)

    if slot and not take_slot(parallel, limits['slots']):
        return f'all {parallel} stage slots busy'

    free = sbx_host.mem_available_gib()

    if free < limits['minMemoryGb']:
        return f"{floor_tenth(free)} GiB free < {limits['minMemoryGb']} GiB"

    return usage_problem(limits, probe)


def admit(state):
    """Checked before every stage: your hold (before the slot this run may already hold, so a hold also stops the next
    step after a merge), the capped sbx daemon, a slot, free memory, and room in both usage windows (your manual work
    comes first). Never waits in-process: the step is queued, the run ends, tick retries."""
    held = hold_reason(state['repo'], state['feature'])

    if held:
        wait_ready(state, f'queued: {held}', None)

    problem = admission_problem()

    if problem:
        # All slots busy is routine (status shows the queue); anything else is worth a ping, sbx down is yours.
        routine = problem.startswith('all ') and problem.endswith(' stage slots busy')
        wait_ready(state, f'queued: {problem}', None if routine else f'queued: {problem}',
                   'high' if problem.startswith(SBX_UNIT) else 'low')

    usage = read_usage() or {}
    seven_day = (usage.get('seven_day') or {}).get('utilization')

    if isinstance(seven_day, (int, float)):
        state['usage'].setdefault('first', seven_day)


# ---------------------------------------------------------------------------------------------- your controls


FEATURE_CONTROL = {'hold': False, 'after': None, 'autonomy': 'ask'}
GLOBAL_CONTROL = {'parallel': None, 'drain': False, 'order': []}


def control_file(repo, feature):
    """Your settings for one feature, beside its step states (synced with them, deleted with the feature)."""
    return state_dir(repo, feature) / 'feature.json'


def read_control(path, defaults):
    """A control file's known keys over the defaults; a missing or broken file is the defaults (= no control)."""
    try:
        value = parse_json(path.read_text())
    except OSError:
        value = {}

    return dict(defaults, **{key: value[key] for key in defaults if key in value})


def write_control(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temporary, path)


def feature_control(repo, feature):
    """{hold, after, autonomy}: runs only read it; the CLI writes it."""
    return read_control(control_file(repo, feature), FEATURE_CONTROL)


def global_control():
    """{parallel, drain, order}: the live slot count, a drain of everything, and the queue order."""
    return read_control(CONTROL, GLOBAL_CONTROL)


def update_feature_control(repo, feature, **changes):
    with adapter.exclusive('control'):
        value = dict(feature_control(repo, feature), **changes)
        write_control(control_file(repo, feature), value)

    return value


def update_global_control(**changes):
    with adapter.exclusive('control'):
        value = dict(global_control(), **changes)
        write_control(CONTROL, value)

    return value


def live_parallel(limits, control=None):
    """How many stages may run at once: `pipeline parallel`, capped by config.json admission.slots (the ceiling)."""
    parallel = (global_control() if control is None else control)['parallel']

    return min(parallel, limits['slots']) if isinstance(parallel, int) and parallel >= 1 else limits['slots']


def feature_key(repo, feature):
    return f'{repo}/{feature}'


def queue_key(repo, feature, state, order):
    """Tick's start order: the features `pipeline order` named, in that order, then the oldest queued."""
    key = feature_key(repo, feature)

    return order.index(key) if key in order else len(order), queued_since(state)


def hold_of(repo, feature, control=None):
    """(kind, what) that keeps a feature from starting a stage, or None: ('you', None) for your `pipeline pause
    <feature>`, ('after', what is still awaited) for a `start --after` not merged yet, ('drain', None) for `pipeline
    pause`."""
    mine = feature_control(repo, feature)

    if mine['hold']:
        return 'you', None

    waiting_for = after_unmet(repo, mine['after']) if mine['after'] else None

    if waiting_for:
        return 'after', waiting_for

    if (global_control() if control is None else control)['drain']:
        return 'drain', None

    return None


def hold_reason(repo, feature, control=None):
    """Why a feature may start no stage (hold_of), with how to lift it; or None."""
    held = hold_of(repo, feature, control)

    if not held:
        return None

    kind, what = held

    return {'you': f'held (pipeline start {feature})', 'after': f'{what} (pipeline start {feature} drops it)',
            'drain': 'drained (pipeline start)'}[kind]


def hold_label(held):
    """`by you`, `after X` or `drained`: a hold_of in a few words."""
    kind, what = held

    return {'you': 'by you', 'after': what, 'drain': 'drained'}[kind]


def after_unmet(repo, after):
    """None once `<feature>[:NN]` is merged (all its steps, or step NN), else what is still awaited. A feature that is
    not there (parked, renamed, a typo) is never merged: the hold stays and says so."""
    name, _, step_id = after.partition(':')

    try:
        found = steps(repo, name)
    except PipelineError:
        return f'after {after}: no feature {name}'

    targets = [step for step in found if not step_id or step.id == step_id]

    if not targets:
        return f'after {after}: {name} has no step {step_id}'

    merged = all(load_state(repo, name, step.id)['status'] == 'closed' for step in targets)

    return None if merged else f'after {after}'


def polls_pr(state):
    """A step whose run only asks GitHub about its PR (a hold never stops that: a merge still closes the step)."""
    return state['stage'] == 'done' and bool(state['pr'])


def kick_tick():
    """Starts tick now instead of at the next 5-min timer, so a freed slot does not sit idle (a start while tick runs
    merges into its job)."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(['systemctl', '--user', 'start', '--no-block', TICK_UNIT], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)


# ---------------------------------------------------------------------------------------------- stage sessions


def stage_environment():
    """The VM has no secrets to hide; this pins models, keeps git non-interactive and lets long commands finish.
    Background tasks are off: with --json-schema, a turn that ends while a background agent or command runs is forced
    to its structured result at once (no wait), so agents and long commands run in the foreground, up to an hour."""
    return dict(backend.STAGE_ENV, HEAVY_MIN_FREE_GB='0', GIT_EDITOR='true', GIT_SEQUENCE_EDITOR='true',
                GIT_PAGER='cat', GIT_TERMINAL_PROMPT='0', CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS='3600000',
                BASH_MAX_TIMEOUT_MS='3600000', CLAUDE_CODE_DISABLE_BACKGROUND_TASKS='1')


def stage_settings(state, stage_key, context):
    return json.dumps({'autoMode': {
        'environment': ['$defaults', (
            f"This session is an unattended pipeline stage (feature {state['feature']}, step {state['id']}, "
            f"{stage_key}) inside a throwaway sandbox VM (a Docker Sandboxes microVM): its own kernel and Docker, no "
            f"secrets, network only to Anthropic. {context['workspace']} is a disposable clone; the runner fetches "
            'the branch out, gates it, and the user reviews the pull request before anything merges.')],
        'allow': ['$defaults', (
            'Inside this VM: Docker containers, databases, resets, rebases, cleans, migrations, seeds and drops the '
            'task needs, and deleting untracked files tools generate in the clone (e.g. turbo\'s AGENTS.md). '
            'Everything here is a disposable copy, never the user\'s.')],
    }})


def merged_results(state, field, skip=None):
    """A result field across the step's stages in run order, exact repeats dropped. `skip` leaves out a stage that is
    about to replace its own result."""
    items = []

    for key in state['sessions']:
        if key != skip and key in state['results']:
            items += [item for item in state['results'][key].get(field, []) if item not in items]

    return items


def recorded_lines(state, stage_key):
    """What earlier stages already recorded, so this one adds only what is new instead of restating it."""
    lines = [f"- {field}: {' '.join(str(item).split())}" for field in ('flags', 'owed', 'deferred')
             for item in merged_results(state, field, skip=stage_key)]

    return ['', '## Already recorded for this step', ''] + lines if lines else []


def system_prompt(state, stage_key, context):
    step = step_of(state)
    config = context['config']
    checks = '; '.join(f"{check['name']}: `{check['run']}`" for check in config['checkFast'])
    migrations = config.get('migrations')
    lines = [
        '',
        '## This stage',
        '',
        f"- Repo {state['repo']}, feature {state['feature']}, step {step.name}, stage {stage_key}. "
        f'Plan: {step.vm_path(step.document)}.',
        f'- Step dir: {Path(step.vm_path(step.plan)).parent} (brief.md, plan.md, review-K.md, pr.md). Write plan '
        'files only here; the runner copies out only your stage\'s own files.',
        f"- Feature dir: {backend.plans_dir(context['name'], state['feature'])} (00-overview.md, other steps): "
        'read-only.',
        f"- Workspace: {context['workspace']} (branch {state['branch']}, base origin/{context['base']}).",
        f'- The runner\'s checks after you: {checks}; then its own git gates and CI on the PR.',
    ]

    if feature_control(state['repo'], state['feature'])['autonomy'] == 'auto':
        lines.append('- Autonomy: auto — your questions will be answered with your recommendation; make each one '
                     'implementable as written.')

    if migrations:
        lines.append(f"- Migrations live in {migrations['dir']}; never edit one that exists on origin/{context['base']}."
                     + (f" Generate with `python3 ~/.claude/pipeline/{migrations['generate']}`."
                        if migrations.get('generate') else ''))

    return SYSTEM_PROMPT.read_text() + '\n'.join(lines + recorded_lines(state, stage_key) + [''])


def transcript_state(context, session):
    """Whether the session's transcript exists in the VM and can be resumed (it holds the first user message). A lost
    sandbox has none."""
    result = backend.exec(context['name'], ['python3', f"{backend.kit_dir(context['name'])}/pipeline/transcripts.py",
                                            session], timeout=60)
    found = parse_json(tail(result.stdout, 1)) if result.returncode == 0 else {}

    return {'exists': bool(found.get('exists')), 'resumable': bool(found.get('resumable'))}


def stop_process(context, process):
    """`sbx exec` forwards no signal: SIGINT to claude inside the VM ends the turn cleanly; SIGTERM if it doesn't;
    then the host side is killed and the VM quiesced by the caller."""
    with signals_ignored():
        for signal_name, grace in (('INT', STOP_GRACE_SECONDS), ('TERM', 30)):
            if process.poll() is not None:
                return

            backend.interrupt(context['name'], signal_name)

            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

        process.kill()


STAGE_FILES = {'plan': ('plan.md',), 'implement': ('plan.md',), 'review': ('review-{round}.md',),
               'respond': ('pr.md', 'plan.md')}


def stage_files(state, stage_key):
    kind, _, number = stage_key.partition('-')

    return [name.format(round=number) for name in STAGE_FILES[kind]]


def replace_step_file(path, text):
    """Writes a file into the step folder through a fresh file and a rename: whatever is at `path` (a symlink, say) is
    replaced, never written through."""
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix=f'.{path.name}.', delete=False) as handle:
        handle.write(text)

    os.chmod(handle.name, 0o644)
    os.replace(handle.name, path)


def read_step_file(path):
    """A step file on the host, read without following a symlink or blocking on a fifo; None when there is none."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PipelineError(f'{path} is not a regular file ({error.strerror}); delete it, then re-run') from None

    with os.fdopen(descriptor, errors='replace') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise PipelineError(f'{path} is not a regular file; delete it, then re-run')

        return handle.read()


def after_stage(state, stage_key, context):
    """Always, after a stage session ends (done, timeout, limit, crash or stop): quiesce the VM, fetch the branch to
    the host ref and copy this stage's own step files out. A lost sandbox has nothing to give."""
    step = step_of(state)

    with signals_ignored():
        try:
            sync_ref(state, context)
        except backend.SandboxMissing:
            return
        except backend.BackendError as error:
            note(state, f'fetch after {stage_key} failed: {str(error)[:300]}')

        for name in stage_files(state, stage_key):
            try:
                text = backend.copy_out(context['name'], step.vm_path(step.directory / name))
            except backend.BackendError as error:
                state['copy_errors'][name] = str(error)
                save_state(state)
                continue

            state['copy_errors'].pop(name, None)

            if text is not None:
                replace_step_file(step.directory / name, text)

        save_state(state)


def invoke_claude(state, stage_key, context, prompt, resume):
    config = STAGES[stage_key.split('-')[0]]
    session = state['sessions'][stage_key]
    step = step_of(state)
    name = context['name']
    backend.copy_feature(name, state['feature'], feature_dir(state['repo'], state['feature']))
    arguments = [
        CLAUDE, '-p', prompt, *(('--resume', session) if resume else ('--session-id', session)),
        '--name', f"{state['feature']}-{state['id']}-{stage_key}", '--model', config['model'],
        '--effort', config['effort'], '--fallback-model', FALLBACK_MODEL, '--max-budget-usd', str(config['budget']),
        '--permission-mode', 'auto', '--permission-prompts', 'none',
        '--add-dir', backend.plans_dir(name, state['feature']),
        '--append-system-prompt', system_prompt(state, stage_key, context),
        '--settings', stage_settings(state, stage_key, context),
        '--json-schema', RESULT_SCHEMA.read_text(),
        '--output-format', 'stream-json', '--verbose',
    ]
    log_path = state_dir(state['repo'], state['feature']) / f"{state['id']}-{stage_key}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    seen = {'mode': None, 'model': None, 'result': None, 'rate_limit': None, 'expected_model': config['model']}
    print(f"  {state['id']} {stage_key}: claude {'--resume' if resume else '--session-id'} {session} in {name} "
          f'(log {log_path})', flush=True)
    argv = backend.exec_argv(name, arguments, env=stage_environment(), workdir=context['workspace'])
    process = subprocess.Popen(argv, env=backend.sbx_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True)
    log = log_path.open('a')

    def consume(line):
        log.write(line)
        log.flush()
        event = parse_json(line)

        if event.get('type') == 'system' and event.get('subtype') == 'init':
            seen['mode'], seen['model'] = event.get('permissionMode'), str(event.get('model') or '').removesuffix('[1m]')

            if seen['mode'] != 'auto' or seen['model'] != config['model']:
                threading.Thread(target=backend.interrupt, args=(name,), daemon=True).start()
        elif event.get('type') == 'rate_limit_event':
            info = event.get('rate_limit_info') or {}
            write_usage(info.get('unifiedWindows'))

            if info.get('status') == 'rejected':
                seen['rate_limit'] = info
        elif event.get('type') == 'result':
            seen['result'] = event

    reader = threading.Thread(target=read_lines, args=(process.stdout, consume), daemon=True)
    reader.start()
    timed_out = False

    try:
        process.wait(timeout=config['hours'] * 3600)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process(context, process)
    except KeyboardInterrupt:
        stop_process(context, process)
        raise
    finally:
        with signals_ignored():
            if process.poll() is None:
                process.kill()

            process.wait()
            reader.join(60)
            log.close()
            after_stage(state, stage_key, context)

    denials = (seen['result'] or {}).get('permission_denials') or []

    if denials:
        state['denials'] = (state['denials'] + [f"{stage_key}: {json.dumps(denial)[:200]}" for denial in denials])[-30:]

    windows = read_usage() or {}

    if isinstance((windows.get('seven_day') or {}).get('utilization'), (int, float)):
        state['usage']['last'] = windows['seven_day']['utilization']
        state['usage'].setdefault('first', state['usage']['last'])

    save_state(state)

    return classify(seen, timed_out, process.returncode, log_path)


def classify(seen, timed_out, exit_code, log_path):
    mode, model, result = seen['mode'], seen.get('model'), seen['result']

    if mode is not None and mode != 'auto':
        return {'kind': 'mode', 'detail': f'the session ran in permission mode {mode!r}, not auto (edits would be denied)'}

    if model and seen.get('expected_model') and model != seen['expected_model']:
        return {'kind': 'mode', 'detail': f"the session ran on {model!r}, not {seen['expected_model']!r}"}

    if timed_out:
        return {'kind': 'timeout', 'detail': f'the stage timed out ({log_path})'}

    if result is None:
        return {'kind': 'crash', 'detail': f'claude exited {exit_code} without a result:\n{tail(log_path.read_text(), 10)}'}

    if mode is None:
        return {'kind': 'mode', 'detail': 'no init event, so the permission mode is unknown'}

    if result.get('subtype') == 'error_max_budget_usd':
        return {'kind': 'budget', 'detail': f"the stage spent its --max-budget-usd (total {result.get('total_cost_usd')})"}

    text = str(result.get('result') or '')

    if result.get('is_error'):
        rate_limit = seen.get('rate_limit') or {}

        if rate_limit or result.get('api_error_status') in (429, 529) or LIMIT_TEXT.search(text):
            return {'kind': 'limit', 'detail': text[:200], 'resets_at': rate_limit.get('resetsAt'),
                    'window': rate_limit.get('rateLimitType')}

        return {'kind': 'crash', 'detail': f"{result.get('subtype')}: {text[:300]}"}

    structured = result.get('structured_output')

    if not isinstance(structured, dict) or structured.get('status') not in ('done', 'blocked', 'failed'):
        return {'kind': 'crash', 'detail': f'the result has no valid structured output ({log_path})'}

    return {'kind': 'result', 'result': structured}


def vm_time(context):
    result = backend.exec(context['name'], ['date', '+%s'], timeout=60)

    return int(result.stdout.strip()) if result.stdout.strip().isdigit() else int(time.time())


def ensure_stage(state, stage_key, context, prompt):
    """Runs the stage unless it already finished `done`: a re-run after an interrupted gate never re-prompts it. The
    first start is stamped with the VM's clock, so a file gate can tell a file written by the stage from an older one."""
    if state['results'].get(stage_key, {}).get('status') != 'done':
        if stage_key not in state['started']:
            state['started'][stage_key] = vm_time(context)
            save_state(state)

        run_stage(state, stage_key, context, prompt)


def lost_context(state, context):
    step = step_of(state)
    commits = git_result(context['clone'], 'log', '--oneline', f"{context['origin_base']}..{context['ref']}").stdout

    try:
        pr_text = read_step_file(step.pr_text)
    except PipelineError:
        pr_text = None

    return RESUME_LOST.format(base=f"origin/{context['base']}", commits=commits.strip() or '(none)',
                              pr_text=cap(pr_text or '(none yet)', 8_000))


def run_stage(state, stage_key, context, fresh_prompt):
    """Runs or resumes one stage session until it returns `done`; anything else pauses or queues the step."""
    crashes = 0
    ran_before = stage_key in state['sessions']
    state['sessions'].setdefault(stage_key, str(uuid.uuid4()))

    while True:
        pending = state['pending'].get(stage_key)
        transcript = transcript_state(context, state['sessions'][stage_key])

        if transcript['resumable']:
            resume, prompt = True, pending or RESUME_INTERRUPTED
        else:
            if transcript['exists']:
                state['sessions'][stage_key] = str(uuid.uuid4())

            extra = [lost_context(state, context)] if ran_before and not transcript['exists'] else []
            resume, prompt = False, '\n\n'.join([fresh_prompt] + extra + ([pending] if pending else []))

        state['not_before'] = None
        mark(state, 'running', f'{stage_key} running')

        try:
            outcome = invoke_claude(state, stage_key, context, cap(prompt, MAX_PROMPT_CHARACTERS), resume)
        except KeyboardInterrupt:
            state['pending'].setdefault(stage_key, RESUME_INTERRUPTED)
            raise

        ran_before = True

        if outcome['kind'] == 'result':
            result = outcome['result']
            state['pending'].pop(stage_key, None)
            state['results'][stage_key] = result
            save_state(state)

            if result['status'] == 'done':
                return result

            step = step_of(state)
            state['pending'][stage_key] = RESUME_BLOCKED.format(status=result['status'],
                                                                document=step.vm_path(step.document))
            questions = ''.join(f'\n- {question}' for question in result.get('questions', []))
            pause(state, f"{stage_key} {result['status']}: {result['summary']}{questions}",
                  f"{stage_key} {result['status']}: {headline(result['summary'])[:200]}{questions}")

        state['pending'].setdefault(stage_key, RESUME_INTERRUPTED)

        if outcome['kind'] == 'limit':
            wait_for_limit(state, stage_key, outcome)

        if outcome['kind'] == 'crash' and crashes == 0:
            crashes += 1
            continue

        pause(state, f"{stage_key} {outcome['kind']}: {outcome['detail']}", f"{stage_key} {outcome['kind']}")


def wait_for_limit(state, stage_key, limit):
    """A usage limit never holds a slot: the step waits until the reset (plus a margin), the run ends and the VM
    stops; tick resumes the same session then."""
    resets_at = limit.get('resets_at')
    resume_at = (resets_at + LIMIT_MARGIN_SECONDS if isinstance(resets_at, (int, float))
                 else time.time() + LIMIT_UNKNOWN_WAIT_SECONDS)
    state['not_before'] = resume_at
    reset = time.strftime('%a %H:%M %Z', time.localtime(resume_at))
    window = limit.get('window') or 'usage'
    wait_ready(state, f"{stage_key} limit ({window}) until {reset}: {limit['detail']}. Tick resumes it then.",
               f'{stage_key} limit until {reset}')


# ---------------------------------------------------------------------------------------------- gates


def gate_tree(state, context):
    """In the VM: HEAD on the step's branch, and nothing uncommitted or untracked."""
    result = backend.sh(context['name'], 'git symbolic-ref --short -q HEAD; echo ::; '
                                         'git status --porcelain --untracked-files=all', workdir=context['workspace'])

    if result.returncode != 0 and '::' not in result.stdout:
        return [f'reading the workspace failed: {tail(result.stderr, 5)}']

    branch_part, _, status_part = result.stdout.partition('::\n')
    branch = branch_part.strip() or None
    failures = []

    if branch != state['branch']:
        failures.append(f"HEAD must be on {state['branch']} (it is on {branch or 'a detached HEAD'}): finish any rebase "
                        f"or merge, then switch back to {state['branch']} with your commits on it.")

    status = [line for line in status_part.splitlines() if line.strip()]

    if status:
        more = [f'… {len(status) - MAX_LISTED_FILES} more'] if len(status) > MAX_LISTED_FILES else []
        failures.append('The workspace has uncommitted or untracked files; commit them or delete probes:\n' +
                        '\n'.join(status[:MAX_LISTED_FILES] + more))

    return failures


def gate_checks(state, context):
    """In the VM: the repo's checkFast commands (a flaky one reruns whole), then the branch tip they ran on. The VM is
    quiesced and the branch fetched; the fetched SHA must be that tip, else something moved it after the gates."""
    failures = []

    for check in context['config']['checkFast']:
        reruns = int(check.get('flakeReruns') or 0)

        for attempt in range(reruns + 1):
            started = time.time()
            result = backend.sh(context['name'], check['run'], workdir=context['workspace'],
                                timeout=CHECK_TIMEOUT_SECONDS)
            output = result.stdout + result.stderr
            append_log(state, 'gates', f"{check['name']} `{check['run']}` → exit {result.returncode} "
                                       f'({int(time.time() - started)}s)\n{output}')

            if result.returncode == 0:
                if attempt:
                    note(state, f"{check['name']} failed, then passed on rerun {attempt} (flake?)")
                break
        else:
            failures.append(f"`{check['run']}` ({check['name']}) failed" +
                            (f' (also on {reruns} rerun(s))' if reruns else '') + f':\n{tail(output, 60)}')

    tip = backend.exec(context['name'], ['git', 'rev-parse', '--verify', '--quiet', f"refs/heads/{state['branch']}"],
                       workdir=context['workspace']).stdout.strip()
    fetched = sync_ref(state, context)
    context['head'] = fetched

    if tip != fetched:
        pause(state, f"{state['branch']} moved after the gates (tip {tip[:12]} when they ran, {fetched[:12]} fetched): "
                     're-run to gate what is there now.', 'branch moved')

    return failures


def gate_commits(state, context):
    count = int(git(context['clone'], 'rev-list', '--count', f"{context['origin_base']}..{context['ref']}"))

    return [] if count else [f"The branch has no commits over origin/{context['base']}."]


def gate_trailer(state, context):
    messages = git(context['clone'], 'log', '--format=%h %s%n%b', f"{context['origin_base']}..{context['ref']}")

    failures = []

    if TRAILER.search(messages):
        failures.append('A commit message carries an AI attribution trailer ("Co-Authored-By: Claude" or "Generated with '
                        'Claude Code"). Reword it (`git commit --fixup=reword:<sha>` + autosquash).')

    personal = sorted({match.group(0) for match in PERSONAL_TEXT.finditer(messages)})

    if personal:
        failures.append(f"A commit message carries personal text ({', '.join(personal[:5])}): commit messages are "
                        'team-visible, so keep them to project facts (no home paths, personal emails or memory notes). '
                        'Reword it (`git commit --fixup=reword:<sha>` + autosquash).')

    return failures


def gate_protected(state, context):
    config = context['config']
    base = merge_base(context)
    failures, gate_config = [], []

    for name in changed_files(context, base):
        if adapter.matches(name, config['protected']):
            failures.append(f'{name} changed: supply-chain and CI files are the user\'s. Revert it and report blocked '
                            'with the exact change.')
            continue

        if adapter.matches(name, config['gateConfig']):
            gate_config.append(name)

        if name.endswith('package.json'):
            before, after = json_at(context, base, name), json_at(context, context['ref'], name)
            fields = [field for field in config['dependencyFields'] if before.get(field) != after.get(field)]
            scripts_before, scripts_after = before.get('scripts') or {}, after.get('scripts') or {}
            scripts = sorted(key for key in {*scripts_before, *scripts_after}
                             if scripts_before.get(key) != scripts_after.get(key))

            if fields:
                failures.append(f'{name}: {", ".join(fields)} changed. Dependency changes are the user\'s: revert and '
                                'report blocked with the exact command and the package evidence.')

            if scripts:
                gate_config.append(f'{name} scripts {", ".join(scripts)}')

    if gate_config:
        note(state, f"⚑ gate-defining config changed (check it can't weaken the checks): {'; '.join(gate_config)}")

    return failures


def gate_migrations(state, context):
    migrations = context['config'].get('migrations')

    if not migrations:
        return []

    base = merge_base(context)
    journal = migrations.get('journal')
    changes = [line.split('\t', 1) for line in git(context['clone'], 'diff', '--name-status', '--no-renames', base,
                                                    context['ref'], '--', migrations['dir']).splitlines() if line]
    failures = []

    for status, name in changes:
        if journal and name == journal:
            before = json_at(context, base, name).get('entries', [])

            if json_at(context, context['ref'], name).get('entries', [])[:len(before)] != before:
                failures.append(f"{name}: entries that exist on origin/{context['base']} changed; never edit a merged "
                                'migration.')
        elif status != 'A':
            failures.append(f"{name} ({status}) exists on origin/{context['base']}: never edit, delete or rename a "
                            'merged migration.')
        elif name.endswith('.sql'):
            for line in git(context['clone'], 'show', f"{context['ref']}:{name}").splitlines():
                if DATA_LOSS.search(line):
                    note(state, f'⚑ data loss in {name}: {line.strip()[:200]}')

    return failures


def approved_plan(state):
    return state_dir(state['repo'], state['feature']) / f"{state['id']}-plan.approved.md"


def asked_plan(state):
    """plan.md as it stood when the plan last asked you: what your answer is told apart from."""
    return state_dir(state['repo'], state['feature']) / f"{state['id']}-plan.asked.md"


def save_approved_plan(state):
    step = step_of(state)
    text = read_step_file(step.plan)

    if text is not None:
        path = approved_plan(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_step_file(path, text)


def test_changes_of(text):
    match = re.search(r'^##\s+Test changes\s*$(.*?)(?=^##\s|\Z)', text, re.MULTILINE | re.DOTALL | re.IGNORECASE)

    return match.group(1) if match else ''


def test_changes_section(state):
    return test_changes_of(approved_plan(state).read_text() if approved_plan(state).exists() else '')


def path_patterns(section):
    """Path tokens that are a glob or a brace list, not an exact path. A backtick span is taken verbatim; outside one,
    `**bold**` markers are markdown, not a glob."""
    quoted = re.findall(r'`([^`]+)`', section)
    plain = re.sub(r'`[^`]+`', ' ', section).replace('**', ' ')
    tokens = [token for text in [*quoted, plain] for token in re.findall(r'[^\s`]+', text)]

    return [token for token in tokens if '/' in token and PATH_PATTERN.search(token)]


def gate_test_changes(state, context):
    """The tamper gate matches `## Test changes` by exact path, so a pattern there would allow nothing: refuse it while
    the plan can still be fixed."""
    if 'plan.md' in state['copy_errors']:
        return []

    patterns = path_patterns(test_changes_of(read_step_file(step_of(state).plan) or ''))

    if not patterns:
        return []

    return [f"`## Test changes` names a pattern ({', '.join(patterns)}): the tamper gate allows a deleted, moved or "
            'skipped test only by its exact path. Edit plan.md: write each file\'s full path in backticks (a moved test '
            'as old path → new path).']


def added_test_lines(context, base):
    """(path, added line) for every line the branch adds to a test file."""
    lines, path = [], None

    for line in git(context['clone'], 'diff', '-U0', '--no-renames', base, context['ref']).splitlines():
        if line.startswith('+++ '):
            path = line[6:] if line.startswith('+++ b/') else None
        elif line.startswith('+') and path and TEST_FILE.search(path):
            lines.append((path, line[1:].strip()))

    return lines


def gate_tamper(state, context):
    """Weakened tests: a new `.only` always fails; a deleted test file or a new skip/todo/fixme fails unless the
    approved plan names that exact path under `## Test changes`. Every such change is flagged for the user."""
    base = merge_base(context)
    allowed = test_changes_section(state)
    failures = []

    for path in changed_files(context, base, '--diff-filter=D'):
        if TEST_FILE.search(path):
            if path in allowed:
                note(state, f'⚑ test file deleted (plan allows it): {path}')
            else:
                failures.append(f'{path}: a test file was deleted. Restore it, or report blocked; only the approved '
                                'plan\'s `## Test changes` section can allow this, by its exact path.')

    for path, line in added_test_lines(context, base):
        if ONLY_LINE.search(line):
            failures.append(f'{path}: `.only` added ({line[:120]}): it silently skips every other test. Remove it.')
        elif SKIP_LINE.search(line):
            if path in allowed:
                note(state, f'⚑ test skipped (plan allows it): {path}: {line[:120]}')
            else:
                failures.append(f'{path}: a skipped/todo/fixme test was added ({line[:120]}). Make it pass; only the '
                                'approved plan\'s `## Test changes` section can allow this, by its exact path.')

    return failures


def gate_file(file_name, what, stage_key):
    def gate(state, context):
        step = step_of(state)
        path = step.directory / file_name

        if file_name in state['copy_errors']:
            return [f"{what} could not be taken: {state['copy_errors'][file_name]}. Write it as a regular file."]

        result = backend.exec(context['name'], ['stat', '-c', '%Y', step.vm_path(path)], timeout=60)

        if result.returncode != 0 or not path.exists():
            return [f'{what} was not written: {step.vm_path(path)}']

        if int(result.stdout.strip() or 0) < state['started'].get(stage_key, 0):
            return [f'{what} was not updated by this stage (it predates the stage): {step.vm_path(path)}']

        return []

    gate.__name__ = f'gate_file({file_name})'

    return gate


BRANCH_GATES = [gate_tree, gate_checks, gate_commits, gate_trailer, gate_protected, gate_migrations, gate_tamper]


def run_gates(state, context, gates):
    failures = []

    for gate in gates:
        print(f"  {state['id']} gate {gate.__name__}", flush=True)
        failures += gate(state, context)

    return failures


def gates_loop(state, stage_key, context, gates, fresh_prompt):
    """Runs the gates; on failure resumes the stage session with the failures, at most GATE_RESUMES times."""
    for attempt in range(GATE_RESUMES + 1):
        failures = run_gates(state, context, gates)

        if not failures:
            return

        report = cap('\n\n'.join(cap(failure, 12_000) for failure in failures), MAX_PROMPT_CHARACTERS - 2_000)

        if stage_key not in state['sessions'] or attempt == GATE_RESUMES:
            pause(state, f'{stage_key}: gates red' + (f' after {GATE_RESUMES} fix attempts' if attempt else '') +
                  f':\n\n{report}', f'{stage_key} gates red')

        state['pending'][stage_key] = RESUME_GATES.format(failures=report)
        run_stage(state, stage_key, context, fresh_prompt)


# ---------------------------------------------------------------------------------------------- steps


def settle_questions(state, flags):
    """The plan's questions are answered: they leave the report's "Questions" for these `flags` under "Decisions", and
    the approval is spent. One save, so a crash never leaves the approval to answer a later pause."""
    result = state['results']['plan']
    result['flags'] = result.get('flags', []) + flags
    result['questions'] = []
    state['approved'] = False
    save_state(state)


def unresolved_section(text):
    """The body of plan.md's `## Unresolved questions` section; None when it has none."""
    section = UNRESOLVED.search(text)

    if not section:
        return None

    end = re.search(r'^##[ \t]', text[section.end():], re.MULTILINE)

    return text[section.end():section.end() + end.start() if end else len(text)]


def owner_answer(state, step):
    """Your answer: the lines you added or changed under `## Unresolved questions` since the plan asked."""
    if not asked_plan(state).exists():
        return 'approved, answer not recorded'

    asked = {line.strip() for line in (unresolved_section(asked_plan(state).read_text()) or '').splitlines()}
    plan = approved_plan(state) if approved_plan(state).exists() else step.plan
    lines = [line.strip() for line in (unresolved_section(read_step_file(plan) or '') or '').splitlines()
             if line.strip() and line.strip() not in asked]
    answer = ' / '.join(lines)

    return (answer[:ANSWER_CAP] + '…' if len(answer) > ANSWER_CAP else answer) or 'the recommendation stands'


def step_plan(state, options):
    step = step_of(state)
    context = step_context(state)
    prompt = '/plan-harden ' + ' '.join(step.vm_path(path) for path in (step.brief, step.plan) if path.exists())

    if state['results'].get('plan', {}).get('status') != 'done':
        catch_up_plan(state, context, prompt)

    ensure_stage(state, 'plan', context, prompt)
    gates_loop(state, 'plan', context, [gate_tree, gate_file('plan.md', 'The plan', 'plan'), gate_test_changes], prompt)
    questions = state['results']['plan'].get('questions') or []
    listed = ''.join(f'\n- {question}' for question in questions)

    if questions and feature_control(state['repo'], state['feature'])['autonomy'] == 'auto':
        auto_decide(state, 'plan', questions)
        answer_in_plan(step)
        notify(state, f'plan auto-decided {len(questions)}:{listed}')
        settle_questions(state, [])
    elif questions and state['approved']:
        asked = ' / '.join(' '.join(str(question).split()) for question in questions)
        settle_questions(state, [f'Owner answered: {asked} — {owner_answer(state, step)}'])
    elif questions:
        path = asked_plan(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_step_file(path, read_step_file(step.plan) or '')
        mark(state, 'awaiting-approval', f'The plan asks you:{listed}\nAnswer under `## Unresolved questions` in '
                                         f"{step.plan}, then `pipeline approve {state['feature']}`; or write "
                                         f"`## Feedback` and `pipeline revise {state['feature']}`.")
        notify(state, f"plan asks {len(questions)}:{listed}\n→ pipeline answer {state['feature']}", priority='high')

        raise Stop()

    save_approved_plan(state)
    state['stage'] = 'implement'
    save_state(state)


def catch_up_plan(state, context, prompt):
    """A re-run plan (blocked, paused, interrupted) plans on today's origin/<base>, not the one its VM branched from:
    a step blocked on another PR re-checks with that PR merged. A resumed session is told the base moved."""
    start, tip = merge_base(context), base_sha(context)

    if start == tip and not state['post_rebase']:
        return

    if start != tip and 'plan' in state['sessions']:
        moved = BASE_MOVED.format(base=f"origin/{context['base']}", start=start[:12], tip=tip[:12])
        pending = state['pending'].get('plan')

        if moved not in (pending or ''):
            state['pending']['plan'] = '\n\n'.join(filter(None, [pending, moved]))
            save_state(state)

    sync_with_main(state, context, 'plan', prompt)


def auto_decide(state, stage_key, decisions):
    """Records each decision autonomy auto took for you as a ⚑ flag of the stage (the PR's "Decisions" lists it);
    idempotent, so a resumed run adds nothing twice."""
    result = state['results'][stage_key]
    flags = result.setdefault('flags', [])
    flags += [flag for flag in (AUTO_DECIDED + ' '.join(str(item).split()) for item in decisions) if flag not in flags]
    save_state(state)


def answer_in_plan(step):
    """Autonomy auto answers the plan's questions with its own recommendations: one line under `## Unresolved
    questions` says so (inside the section, so implement reads them as decided, not open). Idempotent."""
    text = read_step_file(step.plan) or ''

    if AUTO_ANSWERED in text:
        return

    section = UNRESOLVED.search(text)
    text = (text[:section.end()] + f'\n\n{AUTO_ANSWERED}' + text[section.end():] if section else
            text.rstrip() + f'\n\n## Unresolved questions\n\n{AUTO_ANSWERED}\n')
    replace_step_file(step.plan, text)


def step_implement(state, options):
    step = step_of(state)
    context = step_context(state)
    prompt = f'/plan-implement {step.vm_path(step.document)}'
    ensure_stage(state, 'implement', context, prompt)
    gates_loop(state, 'implement', context, BRANCH_GATES, prompt)
    state['green_head'] = context['head']
    state['stage'] = 'rebase'
    save_state(state)


def unique_ref(clone, name):
    candidate, suffix = name, 2

    while git_succeeds(clone, 'rev-parse', '--verify', '--quiet', candidate):
        candidate, suffix = f'{name}-{suffix}', suffix + 1

    return candidate


def refresh_clone(repo):
    adapter.clean_clone(repo, token_environment(installation_token(repo)))


def sync_with_main(state, context, fixer_key, fixer_prompt):
    """Rebases onto a moved origin/<base>: a pre-rebase host ref first, conflicts resolved by the fixer session, then
    setup again (idempotent, so no per-file triggers). Every step survives a pause and re-run."""
    refresh_clone(state['repo'])
    tip = base_sha(context)

    if merge_base(context) != tip:
        if not state['post_rebase']:
            backup = unique_ref(context['clone'], f"{context['ref']}-pre-rebase-{time.strftime('%m%d')}")
            git(context['clone'], 'update-ref', backup, context['ref'])
            state['post_rebase'] = {'backup': backup}
            save_state(state)

        backup = state['post_rebase']['backup']
        name, workspace = context['name'], context['workspace']
        refresh_base(state, context)
        rebased = backend.exec(name, ['git', 'rebase', f"origin/{context['base']}"], env=stage_environment(),
                               workdir=workspace, timeout=600)

        if rebased.returncode != 0:
            backend.exec(name, ['git', 'rebase', '--abort'], workdir=workspace, timeout=120)

            if fixer_key not in state['sessions']:
                pause(state, f"rebase onto origin/{context['base']} conflicts and no session can resolve it (backup "
                             f'{backup}):\n{tail(rebased.stdout + rebased.stderr, 20)}', 'rebase conflict')

            state['pending'][fixer_key] = REBASE.format(base=context['base'], backup=backup)
            state['results'].pop(fixer_key, None)
            run_stage(state, fixer_key, context, fixer_prompt)
        else:
            sync_ref(state, context)

        if merge_base(context) != tip:
            pause(state, f"{fixer_key} finished without rebasing onto origin/{context['base']} (backup {backup})",
                  'rebase unresolved')

        note(state, f"rebased onto origin/{context['base']} (backup {backup})")

    if state['post_rebase']:
        run_setup(state, context)
        state['post_rebase'] = None
        save_state(state)


def step_rebase(state, options):
    step = step_of(state)
    context = step_context(state)
    prompt = f'/plan-implement {step.vm_path(step.document)}'

    if 'implement' in state['sessions']:
        ensure_stage(state, 'implement', context, prompt)

    sync_with_main(state, context, 'implement', prompt)

    if ref_sha(context) != state['green_head']:
        gates_loop(state, 'implement', context, BRANCH_GATES, prompt)
        state['green_head'] = context['head']

    state['stage'] = 'review'
    save_state(state)


def step_review(state, options):
    step = step_of(state)
    context = step_context(state)
    round_number = state['round']
    stage_key = f'review-{round_number}'
    prompt = f'/review-branch {step.vm_path(step.document)} {round_number}' + (' scoped' if round_number > 1 else '')
    ensure_stage(state, stage_key, context, prompt)
    gates = [gate_tree, gate_file(f'review-{round_number}.md', 'The review', stage_key)]
    gates_loop(state, stage_key, context, gates, prompt)
    blocking = state['results'][stage_key].get('blocking') or 0

    if round_number >= MAX_ROUNDS and blocking and \
            feature_control(state['repo'], state['feature'])['autonomy'] == 'auto':
        auto_decide(state, stage_key, [f'review round {round_number} still reported {blocking} blocking finding(s); '
                                       'the respond stage fixed them without another review'])
        notify(state, f'review round {round_number}: {blocking} blocking finding(s) auto-approved to respond')
    elif round_number >= MAX_ROUNDS and blocking and state['approved']:
        # Your approve answers this one pause; flag, spent approval and stage go in the one save below.
        result = state['results'][stage_key]
        result['flags'] = result.get('flags', []) + [f'Owner approved: review round {round_number} still reported '
                                                     f'{blocking} blocking finding(s); the respond stage fixed them '
                                                     'without another review']
        state['approved'] = False
    elif round_number >= MAX_ROUNDS and blocking:
        mark(state, 'awaiting-approval', f'Review round {round_number} still reports {blocking} blocking finding(s) '
                                         f"({step.review(round_number)}). Decide: `pipeline approve {state['feature']}` "
                                         'lets the respond stage fix them without another review.')
        notify(state, f"review round {round_number}: {blocking} blocking finding(s) need your call\n→ pipeline approve "
                      f"{state['feature']}", priority='high')

        raise Stop()

    state['stage'] = 'respond'
    save_state(state)


def step_respond(state, options):
    step = step_of(state)
    context = step_context(state)
    round_number = state['round']
    stage_key = f'respond-{round_number}'

    if stage_key not in state['sessions']:
        state['respond_base'] = ref_sha(context)

    prompt = f'/review-respond {step.vm_path(step.document)} {round_number}'
    ensure_stage(state, stage_key, context, prompt)

    if str(round_number) not in state['finals']:
        source = [name for name in changed_files(context, state['respond_base']) if not TEST_OR_DOC.search(name)]
        state['finals'][str(round_number)] = not (source and round_number < MAX_ROUNDS)

        if source and round_number < MAX_ROUNDS:
            note(state, f'respond {round_number} changed {len(source)} source files: scoped re-review {round_number + 1}')

        save_state(state)

    final = state['finals'][str(round_number)]

    if final:
        sync_with_main(state, context, stage_key, prompt)

    gates_loop(state, stage_key, context, BRANCH_GATES + [gate_file('pr.md', 'The PR text', stage_key)], prompt)
    state['green_head'] = context['head']

    if final:
        finish(state, context, step)
    else:
        state['round'] = round_number + 1
        state['stage'] = 'review'
        save_state(state)


def fixer_of(state):
    """The session that fixes an open PR: the last respond round's, else implement's."""
    responds = sorted((key for key in state['sessions'] if key.startswith('respond-')),
                      key=lambda key: int(key.split('-')[1]))

    return responds[-1] if responds else 'implement'


def step_update(state, options):
    """An open PR that turned red in CI or conflicts with its base: the fixer session resumes (a fresh one with context
    if its sandbox was lost), the branch rebases when needed, the gates run, and it is pushed again. Red after the base
    moved (`base_moved`) only rebases: CI judges the rebased branch before any fix."""
    step = step_of(state)
    context = step_context(state)
    update = state['update'] or {}
    fixer = fixer_of(state)
    command = '/review-respond' if fixer.startswith('respond-') else '/plan-implement'
    prompt = f"{command} {step.vm_path(step.document)}" + (f" {fixer.split('-')[1]}" if fixer != 'implement' else '')

    if update.get('ci_started') and not update.get('ci_fixed') and \
            state['results'].get(fixer, {}).get('status') == 'blocked':
        # A re-run after a blocked CI fix: the job may have been rerun since, so ask CI again (its current result and
        # a fresh log) instead of resuming the fixer with the old log. Its blocked result and prompt go too, so its
        # questions don't reach the PR comment.
        state['results'].pop(fixer, None)
        state['pending'].pop(fixer, None)
        state['update'], state['stage'] = None, 'done'
        mark(state, 'done', f"PR #{state['pr']['number']} {state['pr']['url']}: checking CI again after a blocked fix")
        return

    if update.get('ci') and not update.get('ci_fixed'):
        state['pending'][fixer] = update['ci']
        state['results'].pop(fixer, None)
        update['ci_started'] = True
        state['update'] = update
        save_state(state)
        run_stage(state, fixer, context, prompt)
        update['ci_fixed'] = True
        state['update'] = update
        save_state(state)

    sync_with_main(state, context, fixer, prompt)
    gates_loop(state, fixer, context, BRANCH_GATES, prompt)
    state['green_head'] = context['head']
    finish(state, context, step)


def scan_for_secrets(state, context):
    """Nothing that looks like a credential leaves the machine: env files and token-shaped strings stop the push."""
    base = merge_base(context)
    env_files = [name for name in changed_files(context, base) if SECRET_PATH.search(name) and not name.endswith('.example')]

    if env_files:
        pause(state, f"env files in the diff: {', '.join(env_files)}. Remove them from the branch, then re-run.",
              'secret files in the diff')

    if SECRET_TEXT.search(git(context['clone'], 'diff', base, context['ref'])):
        pause(state, 'the diff contains a token-shaped string (a GitHub token or a private key). Remove it, then '
                     're-run.', 'secret in the diff')


def branch_rules(repo, branch):
    """The rules GitHub enforces on a branch, read with the App token; None when GitHub can't answer."""
    try:
        rules = github_api('GET', f'/repos/{repo}/rules/branches/{urllib.parse.quote(branch)}', installation_token(repo))
    except PipelineError:
        return None

    return rules if isinstance(rules, list) else None


def require_branch_rules(state, repo, base):
    """Publishing rests on GitHub enforcing review on the base; a Free-plan org silently drops rulesets, so check
    before every push."""
    rules = branch_rules(repo, base)

    if rules is None:
        pause(state, f'cannot read the rules on {base} (the App token got no answer from …/rules/branches/{base})',
              'rules unknown')

    for rule in rules:
        parameters = rule.get('parameters') or {}

        if (rule.get('type') == 'pull_request' and parameters.get('required_approving_review_count', 0) >= 1
                and parameters.get('require_code_owner_review')):
            return

    pause(state, f'{base} has no active rule requiring a code-owner-approved pull request, so nothing would stop an '
                 'unreviewed merge: fix the ruleset (README "New repo"), then re-run.', 'rulesets missing')


def push_branch(state, context, sha):
    """Pushes the gate-green commit, and only it, from the host clone to the step's agent/** branch; a branch that
    moved since the last publish (someone pushed to it) stops the run instead of being overwritten."""
    repo = state['repo']
    lease = f"--force-with-lease=refs/heads/{state['branch']}:{state['published_sha'] or ''}"
    result = git_result(context['clone'], 'push', '--quiet', lease, push_url(repo),
                        f"{sha}:refs/heads/{state['branch']}", env=token_environment(installation_token(repo)))

    if result.returncode != 0:
        pause(state, f"push of {state['branch']} refused:\n{tail(result.stderr, 10)}", 'push refused')


def public_text(text):
    """Text bound for GitHub: home paths shortened to ~, personal emails replaced (noreply ones stay)."""
    text = HOME_PATH.sub('~', text)

    return EMAIL.sub(lambda match: match.group(0) if match.group(0).endswith(NOREPLY) else '[email]', text)


def pull_request_text(state, step):
    """The PR title and body from the respond stage's pr.md; it goes public, so a token-shaped string stops it,
    personal text is redacted and an AI attribution line is dropped (the repo may squash-merge the body)."""
    try:
        text = read_step_file(step.pr_text) or ''
    except PipelineError as error:
        pause(state, str(error), 'PR text unsafe')

    if SECRET_TEXT.search(text):
        pause(state, f'{step.pr_text} contains a token-shaped string (a GitHub token or a private key). Remove it, then '
                     're-run.', 'secret in the PR text')

    kept = [line for line in text.splitlines() if not TRAILER.search(line)]
    lines = public_text('\n'.join(kept)).splitlines()
    title = lines[0].strip() if lines else f'{step.feature}: {step.name}'

    return title, '\n'.join(lines[1:]).strip()


def ensure_pull_request(state, base, title, body):
    """The step's PR: found by branch and re-synced with pr.md, or created as a draft (it turns ready, and your review
    is requested, once CI is green)."""
    repo = state['repo']
    token = installation_token(repo)
    owner = repo.split('/')[0]
    existing = github_api('GET', f"/repos/{repo}/pulls?state=open&head={owner}:{urllib.parse.quote(state['branch'])}",
                          token)

    if isinstance(existing, list) and existing:
        pull = existing[0]
        github_api('PATCH', f"/repos/{repo}/pulls/{pull['number']}", token, {'title': title, 'body': body})
    else:
        pull = github_api('POST', f'/repos/{repo}/pulls', token,
                          {'title': title, 'head': state['branch'], 'base': base, 'body': body, 'draft': True})

    return {'number': pull['number'], 'url': pull.get('html_url'), 'node_id': pull.get('node_id')}


def usage_points(state):
    first, last = state['usage'].get('first'), state['usage'].get('last')

    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return None

    return round((last - first) * 100, 1)


def ci_reruns(ci):
    """', k CI reruns' when the published commit's CI was rerun (status, show, the PR comment)."""
    return f", {plural(ci['reruns'], 'CI rerun')}" if ci.get('reruns') else ''


def ci_fixes(ci):
    """'n/2' while the fixes are within CI_FIXES, then 'n/5': the rounds past it that a moving failure earns."""
    fixes = ci.get('fixes', 0)

    return f"{fixes}/{CI_FIXES if fixes <= CI_FIXES else CI_FIXES_MAX}"


def evidence_text(state):
    """The PR comment: the public part of done.md that `finish` built, CI, and the denial count. Learnings, runner
    notes, usage and the denials themselves stay in done.md (yours, not the team's)."""
    ci = state['ci']
    ci_line = ('green' if ci.get('reviewed') == state['published_sha'] else 'pending') + \
              f" on {str(state['published_sha'])[:12]}, {plural(ci.get('fixes', 0), 'CI fix', 'CI fixes')}" + \
              ci_reruns(ci)
    lines = [EVIDENCE_MARKER, (state.get('public_notice') or '').strip(), '', '## CI', '', f'- {ci_line}']
    denials = len(state['denials'])

    if denials:
        lines += ['', f'Permission denials: {denials} (details in done.md)']

    return public_text('\n'.join(lines)) + '\n'


def upsert_evidence(state):
    """One runner comment on the PR, edited in place on every change."""
    repo, token = state['repo'], installation_token(state['repo'])
    body = cap(evidence_text(state), 60_000)
    comment = state['evidence_comment']

    if comment:
        try:
            github_api('PATCH', f'/repos/{repo}/issues/comments/{comment}', token, {'body': body})
            return
        except PipelineError:
            comment = None

    created = github_api('POST', f"/repos/{repo}/issues/{state['pr']['number']}/comments", token, {'body': body})
    state['evidence_comment'] = created.get('id')
    save_state(state)


def step_publish(state, options):
    step = step_of(state)
    context = step_context(state)
    sha = ref_sha(context)

    if sha != state['green_head']:
        pause(state, f"{state['branch']} is at {str(sha)[:12]} on the host, not at the gate-green "
                     f"{str(state['green_head'])[:12]}: re-run to gate and publish what is there.", 'branch moved')

    if state['pr'] and state['published_sha'] == sha:
        state['stage'] = 'done'
        mark(state, 'done', f"PR #{state['pr']['number']} {state['pr']['url']}")
        return

    scan_for_secrets(state, context)
    title, body = pull_request_text(state, step)
    require_branch_rules(state, state['repo'], context['base'])
    push_branch(state, context, sha)
    update = state['update'] or {}
    ci_fix = bool(update.get('ci'))
    seen = state['ci'].get('seen', []) if ci_fix else []

    if ci_fix and update.get('signature') and update['signature'] not in seen:
        seen = seen + [update['signature']]

    state['published_sha'], state['published_at'] = sha, time.time()
    state['ci'] = {'sha': sha, 'fixes': state['ci'].get('fixes', 0) if ci_fix else 0, 'reviewed': None,
                   'silent_pinged': False, 'reruns': 0, 'seen': seen}
    state['update'] = None
    save_state(state)
    state['pr'] = ensure_pull_request(state, context['base'], title, body)
    save_state(state)
    upsert_evidence(state)
    state['stage'] = 'done'
    mark(state, 'done', f"PR #{state['pr']['number']} {state['pr']['url']}: waiting for CI on {sha[:12]}")


STEPS = {'plan': step_plan, 'implement': step_implement, 'rebase': step_rebase, 'review': step_review,
         'respond': step_respond, 'update': step_update, 'publish': step_publish}


# ---------------------------------------------------------------------------------------------- done notice


def test_case_changes(context):
    base = merge_base(context)
    deleted = [name for name in changed_files(context, base, '--diff-filter=D') if TEST_FILE.search(name)]
    diff = git(context['clone'], 'diff', '-U0', '--no-renames', base, context['ref'], '--', '*.test.*', '*.spec.*',
               '**/test/**', '**/tests/**', 'test_*.py', '*/test_*.py', '*_test.py').splitlines()
    removed = sum(1 for line in diff if line.startswith('-') and TEST_CASE_LINE.match(line))
    added = sum(1 for line in diff if line.startswith('+') and TEST_CASE_LINE.match(line))

    return deleted, removed, added


def finish(state, context, step):
    reviews = [state['results'][key] for key in state['sessions'] if key.startswith('review-') and key in state['results']]
    commits = git(context['clone'], 'log', '--oneline', f"{context['origin_base']}..{context['ref']}").splitlines()
    deleted, removed, added = test_case_changes(context)
    memory_index = memory_source(state['repo']) / 'MEMORY.md'
    memory_lines = len(memory_index.read_text().splitlines()) if memory_index.exists() else 0

    def section(title, items):
        # One line per item: an agent's item can't open a section of its own.
        return ['', f'## {title}', ''] + [f"- {' '.join(str(item).split())}" for item in items]

    # The public part goes to the PR comment; done.md is it plus what is yours alone. Built, never parsed back out of
    # done.md, so a new section is private unless added here.
    public = [
        f"## Pipeline report: {state['feature']} {step.name}",
        '',
        f"Branch `{state['branch']}`: {plural(len(commits), 'commit')} over origin/{context['base']}, "
        f"{plural(state['round'], 'review round')}, last review confidence "
        f"{reviews[-1].get('confidence', '?') if reviews else '?'}.",
    ]

    for title, field in (('Decisions', 'flags'), ('Questions', 'questions'), ('Owed', 'owed'), ('Deferred', 'deferred')):
        items = [LEADING_FLAG.sub('', str(item)) for item in merged_results(state, field)]

        if items:
            public += section(title, items)

    public += ['', '## Tests', '', f"- {plural(added, 'test case')} added, {removed} removed; deleted test files: "
                                   f"{', '.join(deleted) or 'none'}"]
    first, last = state['usage'].get('first'), state['usage'].get('last')
    usage = (f'weekly {first * 100:.0f}% → {last * 100:.0f}% ({usage_points(state):+} pts)'
             if usage_points(state) is not None else 'unknown')
    private = (section('Learnings for memory', merged_results(state, 'learnings') or ['none'])
               + section('Runner notes', state['notes'] or ['none'])
               + ['', '## Memory', '', f'- MEMORY.md: {memory_lines} lines (cap 200)', '', '## Usage', '', f'- {usage}']
               + section('Permission denials', [f'`{denial}`' for denial in state['denials']] or ['none']))
    sections = public + private
    state['public_notice'] = '\n'.join(public)
    replace_step_file(step.done_notice, '\n'.join(sections) + '\n')
    state['stage'] = 'publish'
    mark(state, 'ready', f'done notice: {step.done_notice}; publishing next')
    print('\n'.join(sections), flush=True)


# ---------------------------------------------------------------------------------------------- PR loops


def close_merged(state, pull):
    context = step_context(state)
    removed = backend.remove(context['name'])

    if removed.returncode != 0:
        note(state, f"sbx rm {context['name']} failed ({(removed.stderr or removed.stdout).strip()[-200:]}): "
                    'tick retries')

    KIT_COPIED.discard(context['name'])
    ACTIVE_SANDBOXES.discard(context['name'])

    for ref in git(context['clone'], 'for-each-ref', '--format=%(refname)', context['ref'],
                   f"{context['ref']}-pre-rebase-*").splitlines():
        git(context['clone'], 'update-ref', '-d', ref)

    state['merged'] = {'number': pull.get('number'), 'url': pull.get('html_url')}
    mark(state, 'closed', f"merged as PR #{pull.get('number')}")
    notify(state, f"closed: PR #{pull.get('number')} merged", click=pull.get('html_url'))


def required_runs(repo, sha, required, token):
    """The newest check run of every required check on exactly this commit."""
    answer = github_api('GET', f'/repos/{repo}/commits/{sha}/check-runs?per_page=100', token)
    newest = {}

    for run in answer.get('check_runs') or []:
        if run.get('name') in required and (run.get('name') not in newest or run['id'] > newest[run['name']]['id']):
            newest[run['name']] = run

    return newest


CI_STEP_FAILED = '##[error]Process completed with exit code'
CI_LINE_NOISE = re.compile(r'^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z |\x1b\[[0-9;]*m', re.MULTILINE)
# A code frame's failing line, the one above its caret: Playwright `  > 164 |   expect(\u2026)` over `      |   ^`, vitest
# `      4|   expect(\u2026)` over `       |   ^`.
CI_FAILING_LINE = re.compile(r'^[ \t]*>?[ \t]*\d+[ \t]*\|(.*)\n[ \t]*\|[ \t]*\^', re.MULTILINE)


def ci_log(repo, run, token):
    """The end of the failed step's output: a job log ends in artifact uploads and teardown after it, which would
    fill the whole tail. Timestamps and colour codes go, so the tail holds more of the failure. Without a log, the
    check's output; None when GitHub has neither (a job whose log is gone)."""
    try:
        text = github_raw(f"/repos/{repo}/actions/jobs/{run['id']}/logs", token)
    except PipelineError:
        text = ''

    if not text.strip():
        output = run.get('output') or {}
        text = '\n'.join(filter(None, (output.get('title'), output.get('summary'), output.get('text'))))

    if not text.strip():
        return None

    failed = text.find(CI_STEP_FAILED)

    if failed != -1:
        end = text.find('\n', failed)
        text = text if end == -1 else text[:end]

    return CI_LINE_NOISE.sub('', text)[-CI_LOG_CHARACTERS:]


def failure_signature(log):
    """The failing source lines of a CI log, sorted and unique: the same failure gives the same signature when a fix
    shifts its line numbers or other tests pass around it. Empty when the log has no code frame (tsc, lint, an
    unhandled error), which never reads as a new failure."""
    return sorted({match.group(1).strip() for match in CI_FAILING_LINE.finditer(log or '')})


CI_PASSED = ('success', 'neutral', 'skipped')
CI_NOT_CODE = ('cancelled', 'timed_out', 'failure')
CI_WHY_NOISE = re.compile(r'The operation was canceled\.|Process completed with exit code \d+\.')
# Seen live (2026-10-07): "The job was not started because recent account payments have failed or your spending
# limit needs to be increased." Undocumented wording: if it changes, the runs are rerun (≤ CI_RERUNS) instead.
CI_BILLING = re.compile(r'spending limit|payments have failed', re.IGNORECASE)
# A lookup that failed in the job VM (it resolves through the runner host's DNS, which can stall): Node, curl/git, glibc
# and Go words. Not ECONNRESET or "socket hang up": a crashed local app server says those too.
CI_NETWORK = re.compile(r'getaddrinfo (?:ENOTFOUND|EAI_AGAIN) (?!localhost\b)\S+|Could not resolve host|'
                        r'Temporary failure in name resolution|'
                        r'lookup \S+ on \S+:53: (?:read udp \S+: i/o timeout|server misbehaving)')


def classify_run(jobs, run, runs_on_sha):
    """Whose fault a red workflow run is, from documented fields only, with the job that decided it (None when
    unknown). The red job that ended first decides: a fail-fast sibling or a `needs:` gate is red because of it.
    'code': a step of it failed (the fixer's), even while siblings run; a check outside Actions, or a run whose jobs
    GitHub did not give, too (the fix path as before). 'pending': the run (a rerun of it) still runs. 'superseded': a
    newer run of its workflow still runs on the commit. 'infra': it ended without a failed step (timeout, cancel, no
    runner, a lost one): a rerun, never a fix."""
    if not run:
        return 'code', None

    if not jobs:
        return ('code' if run.get('status') == 'completed' else 'pending'), None

    red = [job for job in jobs if job.get('status') == 'completed' and job.get('conclusion') not in CI_PASSED]
    first = min(red, key=lambda job: job.get('completed_at') or '', default=None)

    if first and any(step.get('conclusion') == 'failure' for step in first.get('steps') or []):
        return 'code', first

    if run.get('status') != 'completed':
        return 'pending', first

    if any(other.get('workflow_id') == run.get('workflow_id') and (other.get('id') or 0) > run['id']
           and other.get('status') != 'completed' for other in runs_on_sha):
        return 'superseded', first

    return ('infra', first) if first and first.get('conclusion') in CI_NOT_CODE else ('code', first)


def ci_why(annotations, job):
    """A red job's own words for a message (GitHub's annotations: "The job has exceeded the maximum execution time
    …"), else its conclusion."""
    messages = [' '.join(str(annotation.get('message') or '').split()) for annotation in annotations or []
                if annotation.get('annotation_level') == 'failure']
    why = '; '.join(dict.fromkeys(message for message in messages if message and not CI_WHY_NOISE.fullmatch(message)))

    return (why or str(job.get('conclusion') or 'red'))[:300]


def ci_verdicts(repo, sha, red, token):
    """Each red required check with its run's verdict ({check, kind, run, job, why}); a run's jobs are read once.
    None when GitHub cannot list the commit's runs: the poll waits rather than guess."""
    try:
        listed = github_api('GET', f'/repos/{repo}/actions/runs?head_sha={sha}&per_page=100',
                            token).get('workflow_runs') or []
    except PipelineError:
        return None

    suites = {run.get('check_suite_id'): run for run in listed}
    classified = {}
    verdicts = []

    for check in red:
        run = suites.get((check.get('check_suite') or {}).get('id'))

        if run and run['id'] not in classified:
            try:
                jobs = github_api('GET', f"/repos/{repo}/actions/runs/{run['id']}/attempts/"
                                         f"{run.get('run_attempt') or 1}/jobs?per_page=100", token).get('jobs')
            except PipelineError:
                jobs = None

            classified[run['id']] = classify_run(jobs, run, listed)

        kind, job = classified[run['id']] if run else ('code', None)
        verdicts.append({'check': check, 'kind': kind, 'run': run, 'job': job, 'why': None})

    return verdicts


def rerun_ci(state, number, infra, repo, token):
    """Infra red ⇒ GitHub reruns the runs' failed jobs, ≤ CI_RERUNS times per published commit, never a fix; then, or
    when GitHub refuses, a pause that says so. Counted only once every rerun is accepted. No rerun when one cannot help
    (Actions billing) or the App may not (Actions read only: you re-run it)."""
    names = ', '.join(dict.fromkeys(verdict['check']['name'] for verdict in infra))

    for verdict in infra:
        if verdict['why'] is None and verdict['job']:
            try:
                annotations = github_api('GET', f"/repos/{repo}/check-runs/{verdict['job']['id']}/annotations", token)
            except PipelineError:
                annotations = []

            verdict['why'] = ci_why(annotations if isinstance(annotations, list) else [], verdict['job'])

    why = '; '.join(dict.fromkeys(verdict['why'] or verdict['check'].get('conclusion') or 'red' for verdict in infra))[:300]
    feature = state['feature']
    actions = (TOKEN_CACHE.get(repo) or {}).get('permissions', {}).get('actions')

    if CI_BILLING.search(why):
        pause(state, f"PR #{number}: CI {names} did not run: GitHub Actions billing ({why}). No fix was tried: fix the "
                     f"org's Actions billing or spending limit, re-run it on GitHub, then `pipeline {feature}`.",
              f'PR #{number} CI billing')

    if actions != 'write':
        pause(state, f"PR #{number}: CI {names} ended without a code failure ({why}). No fix was tried, and auto-rerun "
                     f"is off (the App has Actions: {actions or 'none'}): re-run it on GitHub, then `pipeline {feature}`.",
              f'PR #{number} CI not code')

    if state['ci'].get('reruns', 0) >= CI_RERUNS:
        pause(state, f"PR #{number}: CI {names} ended without a code failure {CI_RERUNS + 1} times ({why}). Could be a "
                     f"slow or hung test, or the runner; no fix was tried: check the job's timeout-minutes or the "
                     f"runner, re-run it on GitHub, then `pipeline {feature}`.", f'PR #{number} CI not code')

    for run_id in dict.fromkeys(verdict['run']['id'] for verdict in infra):
        try:
            github_api('POST', f'/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs', token, {})
        except PipelineError as error:
            needs = 'the App needs Actions: write; ' if re.search(r': 403\b', str(error)) else ''
            pause(state, f"PR #{number}: CI {names} ended without a code failure ({why}) and GitHub refused the rerun "
                         f"({str(error)[:200]}): {needs}re-run it on GitHub, then `pipeline {feature}`.",
                  f'PR #{number} CI rerun refused')

    state['ci']['reruns'] = state['ci'].get('reruns', 0) + 1
    mark(state, 'done', f"PR #{number} {state['pr']['url']}: CI {names} not a code failure ({why}): rerun "
                        f"{state['ci']['reruns']}/{CI_RERUNS}")


def ci_runner_down():
    """No ci-runner@ unit is active (a companion self-hosted PR runner): a check queued for the self-hosted runner then
    never starts. False when systemctl can't say: a busy runner must never ping."""
    try:
        units = subprocess.run(['systemctl', '--user', 'list-units', 'ci-runner@*', '--state=active', '--no-legend',
                                '--plain'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False

    return units.returncode == 0 and not units.stdout.strip()


def ping_silent(state, number, missing, stuck=()):
    """One ping when a required check has not started CI_SILENT_SECONDS after the push: missing, or queued (`stuck`)
    while no ci-runner unit is active."""
    if (missing or stuck) and not state['ci'].get('silent_pinged') and \
            time.time() - (state['published_at'] or time.time()) > CI_SILENT_SECONDS:
        state['ci']['silent_pinged'] = True
        save_state(state)
        minutes = CI_SILENT_SECONDS // 60
        notify(state, f"PR #{number}: " + '; '.join(
            ([f"no required check ({', '.join(missing)}) started in {minutes} min"] if missing else []) +
            ([f"required checks ({', '.join(stuck)}) queued for {minutes} min; no ci-runner unit is active "
              f"(`ci-runner status`)"] if stuck else [])), priority='high')


def request_review(state, repo, number, token):
    """Asks for your review. The ready flip makes CODEOWNERS request you too, and a request racing it can get a 422
    (seen live): being requested is what counts. A request that never lands is noted, never a pause."""
    reviewer = app_settings()['reviewer']
    path = f'/repos/{repo}/pulls/{number}/requested_reviewers'

    try:
        github_api('POST', path, token, {'reviewers': [reviewer]})
    except PipelineError as error:
        with contextlib.suppress(PipelineError):
            if reviewer in [user.get('login') for user in github_api('GET', path, token).get('users') or []]:
                return

        note(state, f'review request for {reviewer} failed ({str(error)[:200]}): request it on the PR')


def poll_pr(state):
    """A done step waits for its PR. Merged ⇒ closed. Closed unmerged ⇒ paused. Conflicting ⇒ update (rebase). CI on
    the published commit: red on a moved base ⇒ update (rebase); red by the code ⇒ update with the failed job's log
    (CI_FIXES times, then while its failing lines are new to every pushed fix, ≤ CI_FIXES_MAX); red by a run still
    running or superseded ⇒ wait; red otherwise (timeout, runner, a log that shows a failed DNS lookup) ⇒ rerun
    (≤ CI_RERUNS times); green ⇒ ready for review."""
    repo, token = state['repo'], installation_token(state['repo'])
    context = step_context(state)
    number = state['pr']['number']
    pull = github_api('GET', f'/repos/{repo}/pulls/{number}', token)

    if (pull.get('merged') or pull.get('state') == 'closed') and not sbx_service_active():
        # Removing the sandbox now would autostart an uncapped daemon: wait quietly, tick polls again.
        return

    if pull.get('merged'):
        close_merged(state, pull)
        return

    if pull.get('state') == 'closed':
        removed = backend.remove(context['name'])

        if removed.returncode != 0:
            note(state, f"sbx rm {context['name']} failed ({(removed.stderr or removed.stdout).strip()[-200:]}): "
                        f"`sbx rm --force {context['name']}` before you re-run")

        state['sandbox_ready'] = False
        pause(state, f'PR #{number} was closed unmerged: reopen it and re-run, or delete the feature.',
              f'PR #{number} closed')

    head = (pull.get('head') or {}).get('sha')

    if head and head != state['published_sha']:
        # Someone else pushed (a rebase from GitHub's page, a fix by hand): CI on the runner's commit says nothing
        # about theirs, and a runner push would overwrite it.
        pause(state, f"PR #{number} now points at {head[:12]}, not the runner's {state['published_sha'][:12]}: "
                     f"someone else pushed to {state['branch']}. Review and merge it yourself, or push "
                     f"{state['published_sha'][:12]} back to the branch and re-run to hand it back.",
              f'PR #{number} pushed by someone else')

    if pull.get('mergeable') is None:
        time.sleep(MERGEABLE_RETRY_SECONDS)
        pull = github_api('GET', f'/repos/{repo}/pulls/{number}', token)

    if pull.get('mergeable_state') == 'dirty':
        state['update'] = {'conflict': True}
        state['stage'] = 'update'
        mark(state, 'ready', f'PR #{number} conflicts with {context["base"]}: rebasing')
        return

    sha = state['published_sha']
    required = context['config']['ci']['requiredChecks']
    runs = required_runs(repo, sha, required, token)
    red = [run for run in runs.values() if run.get('status') == 'completed' and run.get('conclusion') not in CI_PASSED]

    if red:
        verdicts = ci_verdicts(repo, sha, red, token)

        if verdicts is None:
            return

        actionable = [verdict['check']['name'] for verdict in verdicts if verdict['kind'] in ('code', 'infra')]

        if actionable and git_result(context['clone'], 'merge-base', '--is-ancestor', context['origin_base'],
                                     sha).returncode == 1:
            # CI tested a merge with a newer base than the published commit's: rebase first so CI judges what would
            # merge and a fix sees that base. Not a fix (publish resets the count), so before the cap.
            state['update'] = {'base_moved': True}
            state['stage'] = 'update'
            mark(state, 'ready', f"PR #{number}: CI red ({', '.join(actionable)}) and {context['base']} moved: "
                                 f"rebasing first")
            return

        log = None

        for verdict in [verdict for verdict in verdicts if verdict['kind'] == 'code']:
            if verdict['kind'] != 'code':
                continue

            log = ci_log(repo, verdict['check'], token)
            network = CI_NETWORK.search(log or '') if state['ci'].get('reruns', 0) < CI_RERUNS else None

            if network and verdict['run']:
                # A lookup failed, not the code: a rerun. This check only, so a sibling's real failure is still fixed
                # from its own log; once the reruns are spent, the fixer gets it.
                verdict.update(kind='infra', why=f'network: {network.group(0)}')
                log = None
                continue

            if log is not None or not verdict['run']:
                break

            # Its log is gone (a job GitHub cut short): a fix would be blind, a rerun writes a new one.
            for other in verdicts:
                if other['run'] is verdict['run']:
                    other.update(kind='infra', why='its log is gone')

        code = [verdict['check']['name'] for verdict in verdicts if verdict['kind'] == 'code']

        if code:
            signature = failure_signature(log)
            new = bool(signature) and signature not in state['ci'].get('seen', [])
            fixes = state['ci'].get('fixes', 0)

            if fixes >= CI_FIXES_MAX or (fixes >= CI_FIXES and not new):
                stuck = ': the failure did not move' if signature and not new else ''
                pause(state, f"CI is red on PR #{number} after {fixes} fixes ({', '.join(code)}){stuck}.",
                      f'PR #{number} CI red')

            state['ci']['fixes'] = fixes + 1
            state['update'] = {'ci': RESUME_CI.format(sha=sha[:12], checks=', '.join(code), log=log or ''),
                               'signature': signature}
            state['stage'] = 'update'
            moved = ', failure moved' if fixes + 1 > CI_FIXES else ''
            mark(state, 'ready', f"PR #{number}: CI red ({', '.join(code)}): fixing ({ci_fixes(state['ci'])}{moved})")
            return

        if any(verdict['kind'] in ('pending', 'superseded') for verdict in verdicts):
            # A rerun in flight, or a newer run of the workflow: wait quietly (no mark: tick polls every 5 min). A
            # superseded check has not really started; a rerun has.
            ping_silent(state, number, [name for name in required if name not in runs] +
                        [verdict['check']['name'] for verdict in verdicts if verdict['kind'] == 'superseded'])
            return

        rerun_ci(state, number, [verdict for verdict in verdicts if verdict['kind'] == 'infra'], repo, token)
        return

    green = len(runs) == len(required) and all(run.get('status') == 'completed' for run in runs.values())

    if green and state['ci'].get('reviewed') != sha:
        if pull.get('draft'):
            graphql(token, 'mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) '
                           '{ pullRequest { isDraft } } }', {'id': pull.get('node_id') or state['pr'].get('node_id')})

        request_review(state, repo, number, token)
        state['ci']['reviewed'] = sha
        save_state(state)
        upsert_evidence(state)
        reviews = [state['results'][key] for key in state['sessions'] if key.startswith('review-') and key in state['results']]
        confidence = reviews[-1].get('confidence', '?') if reviews else '?'
        points = usage_points(state)
        auto = [flag for flag in merged_results(state, 'flags') if str(flag).startswith(AUTO_DECIDED)]
        notify(state, f"✅ PR #{number} · CI green · confidence {confidence}"
                      + (f' · {points:+} weekly pts' if points is not None else '') + f" · {state['pr']['url']}"
                      + ''.join(f'\n{flag}' for flag in auto), click=state['pr']['url'])
        mark(state, 'done', f"PR #{number} {state['pr']['url']}: CI green, your review requested")
        return

    queued = [name for name in required if (runs.get(name) or {}).get('status') == 'queued']
    ping_silent(state, number, [name for name in required if name not in runs],
                queued if queued and ci_runner_down() else ())


# ---------------------------------------------------------------------------------------------- commands


def advance(state, options):
    if state['status'] == 'awaiting-approval':
        print(f"{state['id']}: awaiting your answer. {state['reason']}", flush=True)
        return

    if options.until and STAGE_ORDER.index(state['stage']) > STAGE_ORDER.index(options.until):
        print(f"{state['id']}: already past {options.until} (at {state['stage']})", flush=True)
        return

    try:
        while state['stage'] != 'done':
            stage = state['stage']

            if stage in VM_STAGES:
                admit(state)
                ensure_sandbox(state)

            STEPS[stage](state, options)

            if state['stage'] != 'done' and stage not in ('rebase', 'update') and (options.step or options.until == stage):
                mark(state, 'ready', f'stopped after {stage} (--step/--until); re-run to continue')
                return
    except Stop:
        return


def run_step(state, step, options):
    """True when the step is closed (merged and cleaned up) and the next one may start."""
    if state['status'] == 'closed':
        return True

    if state['stage'] == 'done' and state['status'] == 'paused' and state['pr']:
        # A pause on the open PR (CI, a foreign push, a GitHub error): the re-run asks GitHub again.
        mark(state, 'done', f"PR #{state['pr']['number']} {state['pr']['url']}: checking again after a pause")

    if state['stage'] != 'done':
        advance(state, options)

    if state['stage'] != 'done' or state['status'] != 'done' or not state['pr']:
        return False

    try:
        poll_pr(state)
    except Stop:
        return False

    if state['status'] == 'closed':
        return True

    if state['stage'] == 'update':
        advance(state, options)

    return False


def stop_sandboxes():
    with signals_ignored():
        for name in sorted(ACTIVE_SANDBOXES):
            with contextlib.suppress(*ERRORS, OSError):
                backend.stop(name)

        ACTIVE_SANDBOXES.clear()
        release_slot()


def command_run(repo, options):
    feature = options.feature

    if stop_flag():
        print('kill switch on: `pipeline stop --clear` first', file=sys.stderr)
        return 1

    try:
        feature_steps = steps(repo, feature)
        held = hold_reason(repo, feature)
        current, _ = current_step(repo, feature)

        if held and current and not polls_pr(current):
            print(f'{feature}: {held}', file=sys.stderr)
            return 1

        check_key(app_settings())
        refresh_clone(repo)
        REPO_CONFIG[repo] = adapter.repo_config(repo)
    except ERRORS as error:
        print(error, file=sys.stderr)
        return 1

    try:
        for step in feature_steps:
            try:
                state = load_state(repo, feature, step.id)
            except ERRORS as error:
                print(error, file=sys.stderr)
                return 1

            if (state['not_before'] or 0) > time.time():
                print(f"{step.name}: waiting until {time.strftime('%a %H:%M', time.localtime(state['not_before']))} "
                      f"({headline(state['reason'])}); running anyway", flush=True)

            try:
                if not run_step(state, step, options):
                    return 0
            except KeyboardInterrupt:
                held = hold_reason(repo, feature)

                if stop_flag():
                    mark(state, 'ready', 'stopped by the kill switch; `pipeline stop --clear` lets tick resume it')
                elif held:
                    mark(state, 'ready', f'queued: {held}')
                else:
                    mark(state, 'paused', 'interrupted (Ctrl+C, SIGTERM or a closed terminal); re-run to resume')

                return 130
            except ERRORS as error:
                mark(state, 'paused', str(error))
                notify(state, f'paused: error: {headline(str(error))[:200]}', priority='high')
                return 1
            except Exception as error:  # noqa: BLE001 - a runner bug must pause the step, not strand the feature
                mark(state, 'paused', f'runner error {type(error).__name__}: {error}\n{tail(traceback.format_exc(), 15)}')
                notify(state, 'paused: runner error', priority='high')
                return 1

        print(f'{feature}: every step is merged', flush=True)

        return 0
    finally:
        freed = bool(SLOT_HANDLES)
        stop_sandboxes()

        if freed:
            kick_tick()  # only a freed slot: a run that ended queued would start a tick storm


def command_detach(repo, options):
    """`pipeline <feature> --detach`: the run tick would start, now, as a background unit (a closed terminal no longer
    stops it). Refused where the run would do nothing useful."""
    feature = options.feature

    if options.until or options.step:
        print('--detach runs to the end: drop --until/--step, or run in the foreground', file=sys.stderr)
        return 2

    if stop_flag():
        print('kill switch on: `pipeline stop --clear` first', file=sys.stderr)
        return 1

    if locked(run_lock(repo, feature)):
        print(f'a run already holds {feature}: pipeline status', file=sys.stderr)
        return 1

    try:
        state, step = current_step(repo, feature)
    except (*ERRORS, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    if state is None:
        print(f'{feature}: every step is merged', file=sys.stderr)
        return 1

    if state['status'] == 'awaiting-approval':
        print(f'{feature} {step.name}: awaiting your answer: pipeline approve {feature}  |  pipeline revise {feature}',
              file=sys.stderr)
        return 1

    held = hold_reason(repo, feature)

    if held and not polls_pr(state):
        print(f'{feature}: {held}', file=sys.stderr)
        return 1

    try:
        spawn_feature_run(repo, feature)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        detail = getattr(error, 'stderr', None)
        print(f'starting the run failed: {error}' + (f': {detail.decode(errors="replace").strip()}' if detail else ''),
              file=sys.stderr)
        return 1

    print(f'{feature} {step.name}: run started in the background; log: {run_log(repo, feature)}; pipeline status')

    return 0


def run_lock(repo, feature):
    return f'run-{adapter.short(repo)}-{feature}'


def locked_path(path):
    try:
        with path.open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False

    return False


def locked(name):
    return locked_path(LOGS / f'{name}.lock')


def with_lock(name, function):
    """Holds a lock for the call, or fails at once when another process holds it. The file names the holder's pid,
    so `stop --all` can signal it."""
    LOGS.mkdir(parents=True, exist_ok=True)

    with (LOGS / f'{name}.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            pid = handle.read().strip()
            print(f'another pipeline process{f" (pid {pid})" if pid.isdigit() else ""} holds {name}; pipeline status '
                  'shows what runs', file=sys.stderr)
            return 1

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()

        return function()


def with_stop_signals(function):
    """SIGTERM and SIGHUP (a closed terminal, systemctl stop) take the Ctrl+C path: the stage stops, resumable."""
    previous = {sig: signal.signal(sig, signal.default_int_handler) for sig in (signal.SIGTERM, signal.SIGHUP)}

    try:
        return function()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def current_step(repo, feature):
    """(state, step) of the first step not closed, or (None, None)."""
    for step in steps(repo, feature):
        state = load_state(repo, feature, step.id)

        if state['status'] != 'closed':
            return state, step

    return None, None


def status_lines(repo, feature, show_all, sandboxes):
    lines = []
    feature_steps = steps(repo, feature)
    states = {step.id: load_state(repo, feature, step.id) for step in feature_steps}
    prefix = f'{adapter.short(repo)}/{feature}'

    if not show_all and all(state['status'] == 'closed' for state in states.values()):
        return []

    current = next((step.id for step in feature_steps if states[step.id]['status'] != 'closed'), None)
    held = hold_of(repo, feature)
    autonomy = feature_control(repo, feature)['autonomy']

    for step in feature_steps:
        state = states[step.id]
        reason = headline(state['reason'])[:240]
        fix = next((line for line in state['reason'].splitlines() if line.startswith('fix: ')), None)
        suffix = f' held: {hold_label(held)}' if held and step.id == current else ''

        if not state['history'] and state['status'] == 'ready':
            lines.append(f'{prefix} {step.name}: not started{suffix}')

            if step.id == current:
                lines.append(f'  autonomy {autonomy}')

                if held:
                    lines.append(f'  next: {next_command(state, "ready")}')

            continue

        status = state['status']

        if status == 'running' and not locked(run_lock(repo, feature)):
            status, reason = 'running?', f'no run holds the lock, so it stopped; tick or `pipeline {feature}` resumes it'

        lines.append(f"{prefix} {step.name}: {state['stage']} [{status}] {reason}".rstrip() + suffix)
        name = backend.sandbox_name(adapter.short(repo), feature, step.id)

        if sandboxes is None:
            extra = ['sandbox ?']
        elif name in sandboxes:
            extra = [f'sandbox {sandboxes[name]}']
        else:
            extra = ['sandbox gone'] if state['sandbox_ready'] and state['status'] != 'closed' else []

        if state['pr']:
            ci = state['ci']
            ci_state = 'green' if ci.get('reviewed') == state['published_sha'] else 'waiting'
            extra.append(f"PR #{state['pr']['number']} CI {ci_state} ({ci_fixes(ci)} fixes{ci_reruns(ci)})")

        if (state['not_before'] or 0) > time.time():
            extra.append(f"not before {time.strftime('%a %H:%M', time.localtime(state['not_before']))}")

        if step.id == current:
            extra.append(f'autonomy {autonomy}')

        if state['status'] != 'closed' and extra:
            lines.append('  ' + ' · '.join(extra))

        if fix and state['status'] in ('paused', 'ready'):
            lines.append(f'  {fix}')

        command = next_command(state, status)

        if command:
            lines.append(f'  next: {command}')

    return lines


def next_command(state, status):
    """What you (or nobody) do next for a step; `status` is its status with `running?` for a run that died."""
    repo, feature = state['repo'], state['feature']

    if state['status'] == 'awaiting-approval':
        return f'pipeline approve {feature}  |  pipeline revise {feature}'

    if state['status'] == 'done':
        return f'review and merge the PR; comments → pipeline respond {feature}'

    if status == 'running':
        return f'nothing (log: {run_log(repo, feature)})'

    if state['status'] in ('paused', 'ready', 'running'):
        held = hold_of(repo, feature)

        if held and held[0] == 'you':
            return f'pipeline start {feature}'

        if held and held[0] == 'after':
            waits_for = held[1].removeprefix('after ')
            return f'waits for {waits_for} to merge (pipeline start {feature} runs it now)'

        if held:
            return 'pipeline start'

        return f'pipeline {feature} --detach' + ('  (or wait for tick)' if state['status'] == 'ready' else '')

    return None


def usage_windows(usage, labels=('weekly', '5h')):
    """`weekly 41% · 5h 4%` from a usage reading."""
    return ' · '.join(f"{label} {((usage.get(window) or {}).get('utilization') or 0) * 100:.0f}%"
                      for window, label in zip(('seven_day', 'five_hour'), labels))


def command_status_verbose(show_all=False, probes=None):
    """`status -v`: the host's full readings, then every step's line, sandbox, PR/CI, fix and next command. A watch
    passes its `probes` cache, so the host is read at most every PROBE_SECONDS."""
    limits, control = adapter.load_config()['admission'], global_control()
    usage = read_usage()

    if usage:
        age = int((time.time() - usage['at']) / 60)
        print(f"claude usage: {usage_windows(usage)} ({age} min ago; stages queue at {limits['weeklyMax']}% / "
              f"{limits['fiveHourMax']}%)")
    else:
        print('claude usage: unknown (the next stage probes it)')

    parallel = live_parallel(limits, control)
    print(f"stages: {slots_held()} running of {parallel} (parallel {parallel}, ceiling {limits['slots']}: config "
          f"admission.slots) · kill switch {'ON (pipeline stop --clear)' if stop_flag() else 'off'} · "
          f"drain {'ON (pipeline start)' if control['drain'] else 'off'}")
    # Any sbx call autostarts a daemon outside the caps (verified), so none while the unit is down.
    host = host_readings({} if probes is None else probes, time.time(), verbose=True)
    active, memory = host['active'], host['memory']
    print(sbx_service_summary(active, memory))

    if active:
        print(reserved_summary(memory))

    print(f"template: {host['template_summary'] if active else f'? ({SBX_UNIT} not active)'}")

    if not ntfy_topic():
        print(f'ntfy: topic missing ({NTFY_TOPIC}): no phone pings')
    sandboxes = {vm['name']: vm['status'] for vm in memory['vms']} if active and memory['vms'] is not None else None

    names = adapter.all_features()

    if not names:
        print('no features yet')

    for repo, feature in names:
        for line in status_lines(repo, feature, show_all, sandboxes):
            print(line)

    for line in skipped_lines():
        print(line)

    return 0


# ---------------------------------------------------------------------------------------------- views

GROUPS = (('needs', 'NEEDS YOU'), ('running', 'RUNNING'), ('queued', 'QUEUED'), ('held', 'HELD'), ('waiting', 'WAITING'),
          ('closed', 'MERGED'))
QUEUE_GROUPS = (('running', 'RUNNING'), ('queued', 'QUEUED'), ('held', 'HELD'), ('waiting', 'WAITING'),
                ('needs', 'NEEDS YOU'), ('backlog', 'BACKLOG'))
LISTED_GROUPS = ('held', 'waiting', 'closed')  # one wrapped line of `name (why)` items each
GROUP_COLORS = {'needs': '31', 'running': '32', 'queued': '2', 'held': '33', 'waiting': '33', 'closed': '2'}
BADGE_COLOR = '1;31'
ICONS = {'needs': '✋', 'running': '▶'}
PROBE_SECONDS = 15  # sbx, cgroup and template readings in --watch, at most this often
ACTIVITY_BYTES = 64 * 1024
TOOL_TARGET_KEYS = ('file_path', 'command', 'pattern', 'path', 'description', 'url', 'prompt', 'query')
WATCH_ENTER, WATCH_LEAVE = '\x1b[?1049h\x1b[?25l', '\x1b[?25h\x1b[?1049l'  # alt screen + hidden cursor, and back


def use_color(stream):
    """https://no-color.org: any non-empty NO_COLOR turns colour off; so does a pipe or a dumb terminal."""
    return stream.isatty() and not os.environ.get('NO_COLOR') and os.environ.get('TERM') != 'dumb'


def paint(text, code, color):
    return f'\x1b[{code}m{text}\x1b[0m' if color and code else text


def text_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in 'WF' else 1 for char in text)


def clip(text, width):
    """The text cut to `width` terminal columns (wide characters count two); None keeps it whole."""
    if width is None or text_width(text) <= width:
        return text

    while text and text_width(text) > width - 1:
        text = text[:-1]

    return text + '…'


def history_seconds(entry):
    """A history entry's time (host-local, no zone) as epoch seconds, or None."""
    try:
        return time.mktime(time.strptime(entry['at'][:19], '%Y-%m-%dT%H:%M:%S'))
    except (KeyError, TypeError, ValueError):
        return None


def duration(seconds):
    minutes = max(0, int(seconds // 60))

    return f'{minutes}m' if minutes < 60 else f'{minutes // 60}h{minutes % 60:02}'


def last_tool_use(path):
    """(UTC stamp, tool, input) of the last tool call in the newest ACTIVITY_BYTES of a stage's stream-json log."""
    try:
        with path.open('rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - ACTIVITY_BYTES))
            lines = handle.read().decode(errors='replace').splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        event = parse_json(line)

        if event.get('type') != 'assistant':
            continue

        for item in reversed((event.get('message') or {}).get('content') or []):
            if isinstance(item, dict) and item.get('type') == 'tool_use':
                return event.get('timestamp') or '', item.get('name') or '?', item.get('input') or {}

    return None


def step_logs(state):
    """The step's `.pipeline/NN-*.log` files, newest first."""
    found = []

    for path in state_dir(state['repo'], state['feature']).glob(f"{state['id']}-*.log"):
        with contextlib.suppress(OSError):
            found.append((path.stat().st_mtime, path))

    return [path for _, path in sorted(found, reverse=True)]


def activity_line(path, workspace):
    """`HH:MM tool target` for a step log: a stage's last tool call, or the setup/gates log's last write."""
    kind = path.stem.split('-', 1)[1] if '-' in path.stem else path.stem

    if kind in ('gates', 'setup'):
        try:
            return f"{time.strftime('%H:%M', time.localtime(path.stat().st_mtime))} {kind}"
        except OSError:
            return None

    found = last_tool_use(path)

    if not found:
        return None

    stamp, tool, arguments = found
    at = parse_iso(stamp)

    return f"{time.strftime('%H:%M', time.localtime(at)) if at else '--:--'} {tool} {tool_target(arguments, workspace)}" \
        .rstrip()


def tool_target(arguments, workspace):
    """What a tool call acts on (a path relative to the workspace, a command, a pattern), on one line."""
    target = next((str(arguments[key]) for key in TOOL_TARGET_KEYS
                   if isinstance(arguments, dict) and arguments.get(key)), '')

    return ' '.join(HOME_PATH.sub('~', target.replace(workspace + '/', '')).split())[:200]


def activity(state):
    """What the current step did last (its newest log), or None. Just after a stage switch it may name the last one."""
    logs = step_logs(state)

    return activity_line(logs[0], str(adapter.clone_path(state['repo']))) if logs else None


def stage_since(state):
    """When the step last turned `running` (history), or None."""
    for entry in reversed(state['history']):
        if entry['status'] == 'running':
            return history_seconds(entry)

    return None


def ci_green(state):
    return bool(state['pr']) and state['ci'].get('reviewed') == state['published_sha']


def step_view(state, run_held, now):
    """(group, text, link) for a feature's current step in the compact status: what it does or waits for, in a few
    words, and what you type next when it is yours."""
    feature, stage, status = state['feature'], state['stage'], state['status']
    reason = headline(state['reason'])
    pr = state['pr'] or {}

    if status == 'awaiting-approval':
        if stage == 'plan':
            count = len((state['results'].get('plan') or {}).get('questions') or [])
            return 'needs', f'plan asks {count} → pipeline answer {feature}', None

        blocking = (state['results'].get(f"review-{state['round']}") or {}).get('blocking') or '?'
        return 'needs', f'review {state["round"]}: {blocking} blocking → pipeline approve {feature}', None

    if status == 'paused':
        where = f"PR #{pr['number']}" if stage == 'done' and pr else stage
        return 'needs', f'{where} paused: {reason} → pipeline show {feature}', pr.get('url') if stage == 'done' else None

    if stage == 'done' and status == 'done' and pr:
        if ci_green(state):
            return 'needs', f"PR #{pr['number']} green → review & merge", pr.get('url')

        return 'waiting', f"PR #{pr['number']} CI {'fix ' if state['ci'].get('fixes') else ''}running", pr.get('url')

    if run_held:
        if status == 'running':
            since = stage_since(state)
            parts = [reason.split()[0] if reason else stage, duration(now - since) if since else '', activity(state) or '']
            return 'running', '  '.join(part for part in parts if part), None

        return 'running', f'{stage}: {reason or "starting"}', None

    if status == 'running':
        return 'waiting', f'{stage}: run died, tick resumes it', None

    if (state['not_before'] or 0) > now:
        return 'waiting', waiting_text(reason), None

    if not state['history']:
        return 'queued', stage, None

    return 'queued', f"{stage} · {reason.removeprefix('queued: ')}"[:120], None


def waiting_text(reason):
    """A step that waits for a time (usage reset, retry, template build), in a few words."""
    limit = re.match(r'\S+ limit \(.*?\) until (.+?): ', reason)
    retry = re.match(r'retrying \((\d+/\d+)\) after: (.*)', reason)

    if limit:
        return f'limit until {limit.group(1)}'

    return (f'retrying {retry.group(1)}: {retry.group(2)}' if retry else reason)[:80]


def feature_view(repo, feature, now, control):
    """One feature's row for the compact status. Your hold shows unless the step needs you anyway."""
    view = {'repo': repo, 'feature': feature, 'position': '', 'link': None, 'queue': (0, ''), 'hold': None}

    try:
        feature_steps = steps(repo, feature)
        states = [load_state(repo, feature, step.id) for step in feature_steps]
    except (*ERRORS, OSError, ValueError) as error:
        return dict(view, group='needs', text=f'unreadable: {headline(str(error))[:160]}')

    index = next((index for index, state in enumerate(states) if state['status'] != 'closed'), None)

    if index is None:
        merged = (states[-1].get('merged') or {}).get('number')
        return dict(view, group='closed', text=f'PR #{merged}' if merged else 'merged')

    state = states[index]
    group, text, link = step_view(state, locked(run_lock(repo, feature)), now)

    control_of = feature_control(repo, feature)
    position = f'{index + 1:02}/{len(feature_steps):02}' + (' auto' if control_of['autonomy'] == 'auto' else '')

    waiting_for = after_unmet(repo, control_of['after']) if control_of['after'] and not control_of['hold'] else None
    hold = 'you' if control_of['hold'] else control_of['after'] if waiting_for else None

    if hold:
        if group == 'running':
            text += ' · then held'
        elif group == 'waiting' and state['pr']:
            text += ' · held'  # its PR still waits for CI or you; the hold stops only the next stage
        elif group in ('queued', 'waiting'):
            group, text = 'held', waiting_for or 'by you'

    return dict(view, group=group, text=text, link=link, position=position, hold=hold,
                queue=queue_key(repo, feature, state, control['order']))


def template_problem():
    """`template failed N×`, or `template missing` when only `pipeline template build` brings it (build: manual),
    else None: a missing target under build auto is built by the next sandbox create, so it is no anomaly."""
    policy = template_policy()

    try:
        tag = target_tag(policy)

        if backend.template_loaded(tag):
            return None
    except (*ERRORS, OSError):
        return 'template ?'

    marker = template_marker()

    if marker.get('tag') == tag and marker.get('failures'):
        return f"template failed {marker['failures']}×"

    return 'template missing' if policy['build'] == 'manual' else None


def host_readings(probes, now, verbose=False):
    """sbx.service, its memory and VMs, the CI slots and the template: re-read at most every PROBE_SECONDS for a watch
    (`probes` is its cache; `verbose` adds the template summary). No sbx call while the unit is down: any would
    autostart an uncapped daemon."""
    if now - probes.get('at', 0) >= PROBE_SECONDS or (verbose and 'template_summary' not in probes):
        active = sbx_service_active()
        memory = memory_reading(adapter.load_config()['admission']) if active else None
        probes.update(at=now, active=active, memory=memory, template=template_problem() if active else None,
                      ci=ci_slots(memory and memory['vms']))

        if verbose:
            probes['template_summary'] = template_summary() if active else None

    return probes


def ci_units():
    """{slot: active?} of the ci-runner@ units systemd knows; {} when it can't say."""
    try:
        result = subprocess.run(['systemctl', '--user', 'list-units', 'ci-runner@*', '--all', '--no-legend', '--plain'],
                                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {}

    units = {}

    for line in result.stdout.splitlines():
        parts = line.split()
        match = re.match(r'ci-runner@(\d+)\.service$', parts[0]) if parts else None

        if match:
            units[int(match.group(1))] = parts[2:3] == ['active']

    return units


def ci_slots(vms):
    """[(slot, what it does)] for the CI line: ci-runner's units, the state each slot's loop writes, and the
    ci-<slot>-* VMs (`vms`: None when sbx can't tell). [] when ci-runner has nothing here."""
    units = ci_units()
    with_vm = {int(match.group(1)) for match in (re.match(r'ci-(\d+)-\d+$', vm['name']) for vm in vms or ())
               if match}
    slots = []

    for slot in sorted(set(units) | with_vm):
        try:
            state = parse_json((CI_STATE / f'slot-{slot}.json').read_text()).get('state')
        except OSError:
            state = None

        if not units.get(slot):
            slots.append((slot, 'unit inactive' + (' (VM left)' if slot in with_vm else '')))
        else:
            slots.append((slot, state or ('VM up' if slot in with_vm else 'no VM')))

    return slots


def status_model(show_all=False, probes=None, now=None):
    """Everything the compact status shows, read fresh (a watch re-reads it every frame)."""
    now = time.time() if now is None else now
    limits, control = adapter.load_config()['admission'], global_control()
    host = host_readings({} if probes is None else probes, now)
    badges = []

    stopped = stop_flag()

    if stopped:
        badges.append('KILL SWITCH ON: pipeline stop --clear')

    if control['drain']:
        badges.append('DRAINED: pipeline start')

    memory = host['memory']

    if not host['active']:
        badges.append(f'sbx down: systemctl --user start {SBX_UNIT}')
    else:
        if memory['oom']:
            badges.append(f"oom {memory['oom']}: journalctl --user -u sbx")

        if memory['inside'] != memory['total']:
            badges.append(f"shims {memory['inside']}/{memory['total']}: a VM runs outside the caps")

        if (memory['avg60'] or 0) >= PRESSURE_BADGE_PERCENT:
            badges.append(f"memory pressure {memory['avg60']:.1f}%: pipeline parallel 1")

    if host['template']:
        badges.append(host['template'])

    rows = [feature_view(repo, feature, now, control) for repo, feature in adapter.all_features()]

    # Why queued rows don't start, once (each row says it too); the kill switch and sbx are badges already. The
    # status never probes usage: the last reading decides.
    if host['active'] and not stopped and any(row['group'] == 'queued' for row in rows):
        problem = admission_problem(slot=False, probe=False)

        if problem:
            badges.append(f'stages queue: {problem}')

    return {'now': now, 'usage': read_usage(), 'slots': (slots_held(), live_parallel(limits, control)),
            'memory': memory, 'ci': host['ci'], 'badges': badges, 'show_all': show_all, 'rows': rows,
            'order': control['order'],
            'repos': len({row['repo'] for row in rows}), 'skipped': skipped_lines(), 'backlog': []}


def header_line(model):
    usage = model['usage']

    if usage:
        stale = ' (stale)' if model['now'] - usage['at'] > USAGE_STALE_SECONDS else ''
        claude = f'claude {usage_windows(usage)}{stale}'
    else:
        claude = 'claude usage ?'

    running, total = model['slots']

    return f"pipeline {time.strftime('%H:%M', time.localtime(model['now']))}  {claude} · stages {running} running of {total}"


def memory_line(memory):
    """(line, the part to paint yellow or None): the VMs' memory against both caps, what the VMs reserve (by owner,
    against the budget), and the host's free memory against the stage floor. None unless the unit's cgroup reads."""
    if not memory or memory['current'] is None:
        return None, None

    if memory['high'] not in (None, float('inf')):
        caps = f" of {gib(memory['high'])} soft cap ({gib(memory['max'])} hard)"
    else:
        caps = f" ({gib(memory['max'])} hard cap)" if memory['max'] not in (None, float('inf')) else ''

    parts, over = [f"VMs use {gib(memory['current'])} GiB{caps}"], None
    counted = memory['reservations']

    if counted is not None:
        total = sum(mib for _, _, mib in counted)
        owners = {kind: sum(mib for _, owner, mib in counted if owner == kind) / 1024 for kind in ('pl', 'ci', 'other')}
        text = f'reserved {total / 1024:.1f} GiB'

        if total:
            text += f": stages {owners['pl']:.1f} + CI {owners['ci']:.1f}" + \
                (f" + other {owners['other']:.1f}" if owners['other'] else '')

        if memory['budget'] is not None and total > memory['budget']:
            over = f" (over {memory['budget'] / 1024:.1f} by {(total - memory['budget']) / 1024:.1f})"
            text += over

        parts.append(text)

    parts.append(f"host free {floor_tenth(memory['free'])} GiB (floor {memory['floor']})")

    return 'memory   ' + ' · '.join(parts), over


def ci_line(slots):
    return ('CI       ' + ' · '.join(f'ci-{slot} {text}' for slot, text in slots) + '   (ci-runner status)'
            if slots else None)


def render(model, color=False, width=None, groups=GROUPS):
    """The compact status as lines: a header with anomaly badges, then one group per kind of row."""
    head = header_line(model)
    badges = ' '.join(f'[{badge}]' for badge in model['badges'])
    first = clip(head + ('   ' + badges if badges else ''), width)
    lines = [first[:-len(badges)] + paint(badges, BADGE_COLOR, color) if badges and first.endswith(badges) else first]
    memory, over = memory_line(model['memory'])

    if memory:
        memory = clip(memory, width)
        lines.append(memory.replace(over, paint(over, '33', color)) if over and over in memory else memory)

    rows = [row for row in model['rows'] if row['group'] != 'closed' or model['show_all']]

    def name(row):
        return f"{adapter.short(row['repo'])}/{row['feature']}" if model['repos'] > 1 else row['feature']

    width_of_name = min(28, max((len(name(row)) for row in rows), default=0))

    for group, title in groups:
        members = [row for row in rows if row['group'] == group]

        if group == 'queued':
            members.sort(key=lambda row: row['queue'])

        if group == 'backlog' and model['backlog']:
            lines += [paint(clip(line, width), '2', color) for line in wrapped(title, model['backlog'], width)]

        if group == 'held' and ci_line(model['ci']):
            lines.append(clip(ci_line(model['ci']), width))

        if not members:
            continue

        code = GROUP_COLORS[group]

        if group == 'held':
            lines += [paint(line, code, color) for line in held_lines(members, name, model['order'], width)]
            continue

        if group in LISTED_GROUPS:
            items = [f"{name(row)} ({row['text']})" for row in members]
            lines += [paint(clip(line, width), code, color) for line in wrapped(title, items, width)]
            continue

        lines.append(paint(title, '1', color))

        for number, row in enumerate(members, 1):
            mark = str(number) if group == 'queued' else ICONS.get(group, '·')
            text = f" {mark} {name(row):<{width_of_name}} {row['position']}  {row['text']}"
            linked = f"{text}  {row['link']}" if row['link'] else text
            text = linked if width is None or text_width(linked) <= width else clip(text, width)
            lines.append(paint(hyperlink(text, row['link']) if color else text, code, color))

    if not model['rows']:
        lines.append('no features yet')
    elif not rows:
        lines.append('every feature is merged (--all lists them)')

    return lines + [clip(line, width) for line in model['skipped']]


HELD_INDENT = ' ' * 9  # under `HELD     `, `memory   `, `CI       `


def held_lines(members, name, order, width):
    """HELD as chains in merge order: `after X: a → b, c` per feature X still unmerged (each level waits for the one
    before it), roots in `pipeline order` then feature order; your holds as `by you: a, b (pipeline start …)`. A cycle
    (nothing in it has an unmerged root) falls back to `a (after b)` items."""
    rows = {row['feature']: row for row in members}
    waiting = {row['feature']: row['hold'] for row in members if row['hold'] not in (None, 'you')}
    children = collections.defaultdict(list)

    for feature, after in waiting.items():
        children[after.partition(':')[0]].append(feature)

    roots = [after for after in dict.fromkeys(waiting.values()) if after.partition(':')[0] not in waiting]
    position = {feature: index for index, feature in enumerate(rows)}

    def root_key(after):
        key = f"{members[0]['repo']}/{after.partition(':')[0]}"
        return order.index(key) if key in order else len(order), position.get(waiting_child(after), 0)

    def waiting_child(after):
        return next(feature for feature, value in waiting.items() if value == after)

    lines, placed = [], set()

    for after in sorted(roots, key=root_key):
        level = [feature for feature, value in waiting.items() if value == after]
        levels = []

        while level:
            levels.append(', '.join(name(rows[feature]) for feature in level))
            placed.update(level)
            level = [child for feature in level for child in children[feature] if child not in placed]

        label = rows[waiting_child(after)]['text']
        lines += wrapped_chain(f'{label}: ', levels, width)

    cycle = [feature for feature in waiting if feature not in placed]

    if cycle:
        lines += wrapped_chain('', [', '.join(f"{name(rows[feature])} ({rows[feature]['text']})" for feature in cycle)],
                               width)

    mine = [name(row) for row in members if row['hold'] == 'you']

    if mine:
        lines += wrapped_chain('by you: ', [', '.join(mine) + ' (pipeline start ' +
                                            (mine[0] if len(mine) == 1 else '<feature>') + ')'], width)

    return ['HELD     ' + line[len(HELD_INDENT):] if index == 0 else line for index, line in enumerate(lines)]


def wrapped_chain(head, levels, width):
    """`head` + levels joined by → over as many indented lines as the width needs (never cut)."""
    lines, line = [], HELD_INDENT + head

    for index, piece in enumerate(levels):
        piece = piece if index == 0 else ' → ' + piece

        if width and index and text_width(line + piece) > width:
            lines.append(line)
            line = HELD_INDENT + '  ' + piece.removeprefix(' ')
        else:
            line += piece

    return lines + [line]


def hyperlink(text, link):
    """The row's `PR #N` as a terminal hyperlink (OSC 8; terminals without it show the plain text), so the PR opens
    with a click even when the URL itself didn't fit."""
    match = re.search(r'PR #\d+', text) if link else None

    return text if not match else \
        f"{text[:match.start()]}\x1b]8;;{link}\x1b\\{match.group(0)}\x1b]8;;\x1b\\{text[match.end():]}"


def wrapped(title, items, width):
    """`TITLE  a · b · c` over as many lines as the width needs (never cut: each item is something waiting)."""
    lines, line = [], f'{title}  '

    for index, item in enumerate(items):
        piece = item if index == 0 else ' · ' + item

        if width and index and text_width(line + piece) > width:
            lines.append(line)
            line = ' ' * (len(title) + 2) + item
        else:
            line += piece

    return lines + [line]


def skipped_lines():
    return [f"! {adapter.plans_root(repo) / name}: named like a pipeline command, so never run; rename it"
            for repo in adapter.load_config()['repos'] for name in adapter.reserved_features(repo)]


def command_status(argv):
    options = {'verbose': False, 'all': False, 'watch': False, 'interval': 2.0}
    rest = list(argv)

    while rest:
        word = rest.pop(0)

        if word in ('-v', '--verbose'):
            options['verbose'] = True
        elif word == '--all':
            options['all'] = True
        elif word == '--watch':
            options['watch'] = True
        elif word == '-n' and rest:
            try:
                options['interval'] = max(0.5, float(rest.pop(0)))
            except ValueError:
                return usage_error('status')
        else:
            return usage_error('status')

    if options['watch']:
        return with_stop_signals(lambda: command_watch(options['all'], options['interval'], options['verbose']))

    if options['verbose']:
        return command_status_verbose(options['all'])

    stream = sys.stdout
    width = shutil.get_terminal_size().columns if stream.isatty() else None

    for line in render(status_model(options['all']), use_color(stream), width):
        print(line)

    return 0


def verbose_frame(show_all, probes):
    """`status -v`'s lines, for a watch (`probes`: its host readings cache)."""
    with contextlib.redirect_stdout(io.StringIO()) as buffer:
        command_status_verbose(show_all, probes)

    return buffer.getvalue().splitlines()


def command_watch(show_all, interval, verbose=False, stdin=None, stdout=None):
    """`status --watch`: the status redrawn in place every `interval` s on the alternate screen until q or Ctrl+C
    (SIGTERM/SIGHUP take the Ctrl+C path); the terminal is restored however it ends."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout

    if not (stdin.isatty() and stdout.isatty()):
        print('pipeline status --watch needs a terminal (pipe `pipeline status` instead)', file=sys.stderr)
        return 2

    descriptor = stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    probes = {}

    try:
        tty.setcbreak(descriptor)
        stdout.write(WATCH_ENTER)

        while True:
            columns, rows = shutil.get_terminal_size()
            footer = f'every {interval:g}s · q quits'

            if verbose:
                lines = [clip(line, columns) for line in verbose_frame(show_all, probes)]
            else:
                lines = render(status_model(show_all, probes), use_color(stdout), columns)

            lines = lines[:max(1, rows - 1)] + [paint(footer, '2', use_color(stdout))]
            stdout.write('\x1b[H' + '\n'.join(line + '\x1b[K' for line in lines) + '\x1b[J')
            stdout.flush()

            if wait_for_quit(descriptor, interval):
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        stdout.write(WATCH_LEAVE)
        stdout.flush()
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, saved)


def wait_for_quit(descriptor, seconds):
    """Waits out a frame; True once q is pressed (other keys are ignored)."""
    deadline = time.monotonic() + seconds

    while (left := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([descriptor], [], [], left)

        if ready and b'q' in os.read(descriptor, 64).lower():
            return True

    return False


def command_show(repo, feature):
    """One feature in full: every step, and for the current one the whole reason, files, PR/CI, logs and history."""
    feature_steps = steps(repo, feature)
    states = [load_state(repo, feature, step.id) for step in feature_steps]
    index = next((index for index, state in enumerate(states) if state['status'] != 'closed'), None)
    run_held = locked(run_lock(repo, feature))
    print(f'{adapter.short(repo)}/{feature} · {plural(len(feature_steps), "step")} · {feature_dir(repo, feature)}')
    control, held = feature_control(repo, feature), hold_of(repo, feature)
    print(f"autonomy {control['autonomy']} · " + (f'held: {hold_label(held)}' if held else 'not held') +
          (f" · after {control['after']}" if control['after'] and not (held and held[0] == 'after') else ''))

    width = max(len(step.name) for step in feature_steps)

    for position, (step, state) in enumerate(zip(feature_steps, states)):
        mark = '→' if position == index else ' '

        if not state['history'] and state['status'] == 'ready':
            print(f' {mark} {step.name:<{width}}  not started')
        else:
            print(f" {mark} {step.name:<{width}}  {state['stage']} [{state['status']}]  {headline(state['reason'])[:160]}")

    if index is None:
        print('every step is merged')
        return 0

    state, step = states[index], feature_steps[index]
    status = 'running?' if state['status'] == 'running' and not run_held else state['status']
    print(f"\n{step.name}: {state['stage']} [{status}]" + (' · a run holds it' if run_held else ''))

    if state['reason']:
        print('\n'.join('  ' + line for line in state['reason'].rstrip().splitlines()))

    command = next_command(state, status)

    if command:
        print(f'next: {command}')

    files = [path.name for path in (step.brief, step.plan, *sorted(step.directory.glob('review-*.md')), step.pr_text,
                                    step.done_notice) if path.exists()]
    print(f"files: {', '.join(files)} ({step.directory})")

    if state['pr']:
        ci = state['ci']
        print(f"PR: #{state['pr']['number']} {state['pr']['url']} · CI {'green' if ci_green(state) else 'waiting'} "
              f"({ci_fixes(ci)} fixes{ci_reruns(ci)})")

    if (state['not_before'] or 0) > time.time():
        print(f"not before: {time.strftime('%a %H:%M', time.localtime(state['not_before']))}")

    logs = step_logs(state)
    print(f'log: {run_log(repo, feature)}' + (f' · stage log {logs[0]}' if logs else ''))
    doing = activity(state) if state['status'] == 'running' and run_held else None

    if doing:
        print(f'doing: {doing}')

    if state['history']:
        print('history:')

        for entry in state['history'][-5:]:
            print(f"  {entry['at'].replace('T', ' ')[5:16]}  {entry['stage']} [{entry['status']}]  "
                  f"{headline(entry['reason'])[:140]}")

    return 0


def awaiting_step(repo, feature):
    for step in steps(repo, feature):
        state = load_state(repo, feature, step.id)

        if state['status'] == 'awaiting-approval':
            return state, step

    return None, None


def command_review_decision(decision, repo, feature):
    state, step = awaiting_step(repo, feature)

    if state is None:
        print(f'{feature}: no step is awaiting your answer', file=sys.stderr)
        return 1

    if decision == 'approve':
        state['approved'] = True

        if state['stage'] == 'plan':
            save_approved_plan(state)

        mark(state, 'ready', 'approved; the next run continues')
    else:
        state['pending']['plan'] = REVISE.format(plan=step.vm_path(step.plan))
        state['results'].pop('plan', None)
        state['started'].pop('plan', None)
        state['approved'] = False
        mark(state, 'ready', 'revision requested; the next run resumes the plan session')

    return 0


def command_decide(decision, repo, feature):
    """`approve` / `revise`: your call on the waiting step, then tick, which continues it now."""
    _, step = awaiting_step(repo, feature)
    code = command_review_decision(decision, repo, feature)

    if code == 0:
        kick_tick()
        print(f'{feature} {step.name}: ' + ('approved; tick continues it now' if decision == 'approve' else
                                            'sent back; tick resumes the plan'))

    return code


def pr_comments(repo, number, reviewer, since, token):
    """The reviewer's own review summaries, inline comments and conversation comments after `since`."""
    def own(item, stamp):
        return (item.get('user') or {}).get('login') == reviewer and (item.get('body') or '').strip() and \
            (item.get(stamp) or '') > (since or '')

    reviews = [item for item in github_api('GET', f'/repos/{repo}/pulls/{number}/reviews?per_page=100', token)
               if own(item, 'submitted_at')]
    inline = [item for item in github_api('GET', f'/repos/{repo}/pulls/{number}/comments?per_page=100', token)
              if own(item, 'created_at')]
    conversation = [item for item in github_api('GET', f'/repos/{repo}/issues/{number}/comments?per_page=100', token)
                    if own(item, 'created_at')]

    return reviews, inline, conversation


def command_respond(repo, feature):
    """Turns your PR comments into the next respond round: review-K.md in the step folder, then a run."""
    state, step = current_step(repo, feature)

    if state is None or (state['stage'], state['status']) != ('done', 'done') or not state['pr']:
        print(f'{feature}: no step is done with an open PR', file=sys.stderr)
        return 1

    held = hold_reason(repo, feature)

    if held:
        print(f'{feature}: {held}', file=sys.stderr)
        return 1

    token = installation_token(repo)
    number = state['pr']['number']
    pull = github_api('GET', f'/repos/{repo}/pulls/{number}', token)

    if pull.get('state') != 'open':
        print(f'PR #{number} is {pull.get("state")}, not open', file=sys.stderr)
        return 1

    reviewer = app_settings()['reviewer']
    reviews, inline, conversation = pr_comments(repo, number, reviewer, state['responded_through'], token)

    if not (reviews or inline or conversation):
        print(f'PR #{number}: no new comments from {reviewer}', file=sys.stderr)
        return 1

    round_number = state['round'] + 1
    lines = [f'# PR review round {round_number}: comments by @{reviewer} on PR #{number}', '']

    for title, items, render in (
            ('Review summaries', reviews, lambda item: f"- ({item.get('state', '').lower()}) {item['body'].strip()}"),
            ('Inline comments', inline, lambda item: f"- `{item.get('path')}:{item.get('line') or item.get('original_line')}` "
                                                     f"{item['body'].strip()} ({item.get('html_url')})"),
            ('Conversation', conversation, lambda item: f"- {item['body'].strip()} ({item.get('html_url')})")):
        if items:
            lines += [f'## {title}', ''] + [render(item) for item in items] + ['']

    replace_step_file(step.review(round_number), '\n'.join(lines))
    stamps = [item.get('submitted_at') or item.get('created_at') or '' for item in reviews + inline + conversation]
    state['responded_through'] = max(stamps)
    state['round'] = round_number
    state['finals'][str(round_number)] = True
    state['pending'][f'respond-{round_number}'] = PR_COMMENTS.format(review=step.vm_path(step.review(round_number)),
                                                                    number=number)
    state['stage'] = 'respond'
    mark(state, 'ready', f'responding to your PR comments (round {round_number})')
    spawn_feature_run(repo, feature)
    print(f'{feature}: round {round_number} from {len(reviews) + len(inline) + len(conversation)} comment(s); run started')

    return 0


def actionable(state):
    """Whether tick should start a run for this step: work to do (or a PR to poll), and no usage reset pending."""
    if (state['not_before'] or 0) > time.time():
        return False

    # A `running` step whose run died resumes too: tick starts only features no run holds.
    return state['status'] in ('ready', 'running') or (state['status'] == 'done' and bool(state['pr']))


def merged_while_paused(state):
    """A step paused on its open PR (a foreign push, CI red after the fixes, a GitHub error) leaves the merge to you;
    tick asks GitHub so that merge still closes the step and starts the next one, without a run per tick."""
    if not (state['stage'] == 'done' and state['status'] == 'paused' and state['pr']):
        return False

    try:
        token = installation_token(state['repo'])
        pull = github_api('GET', f"/repos/{state['repo']}/pulls/{state['pr']['number']}", token)
    except PipelineError:
        return False

    return bool(pull.get('merged'))


def queued_since(state):
    for entry in reversed(state['history']):
        if not entry['reason'].startswith('queued:'):
            return entry['at']

    return state['history'][0]['at'] if state['history'] else ''


def run_log(repo, feature):
    """Where a background run (tick's or --detach's) writes its output."""
    return LOGS / f'{adapter.short(repo)}-{feature}.out'


def spawn_feature_run(repo, feature):
    """Each run is a transient systemd user unit: stopping or restarting the tick timer never kills it, and
    `systemctl --user stop 'pipeline-run-*'` stops them all. Ordered after sbx.service, so a run at boot (the timer is
    persistent) waits for the capped daemon instead of autostarting one inside itself that would die with it."""
    LOGS.mkdir(parents=True, exist_ok=True)
    unit = f'pipeline-run-{adapter.short(repo)}-{feature}-{int(time.time())}'
    output = run_log(repo, feature)
    argv = ['systemd-run', '--user', '--collect', '--quiet', '--unit', unit, '-p', 'TimeoutStopSec=300',
            '-p', f'Wants={SBX_UNIT}', '-p', f'After={SBX_UNIT}',
            '-p', f'StandardOutput=append:{output}', '-p', f'StandardError=append:{output}',
            f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}", f'--working-directory={PIPELINE_DIR}',
            PYTHON, str(PIPELINE_DIR / 'pipeline'), feature, '--repo', repo]
    subprocess.run(argv, check=True, capture_output=True, timeout=60)


def command_tick():
    """Starts runs; a timer calls this every 5 minutes (and a run that frees a slot, at once), so features resume after
    answers, merges, CI and usage resets. A PR poll always starts (it takes no slot until its step needs a stage
    again); stage work starts only into free slots, in `pipeline order` then oldest-queued order, and none while the
    host would refuse it anyway. Held and drained features wait."""
    if stop_flag():
        print('tick: kill switch on, nothing started', flush=True)
        return 0

    limits, control = adapter.load_config()['admission'], global_control()
    pollers, stages, waiting, held, busy = [], [], [], [], 0

    for repo, feature in adapter.all_features():
        state, _ = current_step(repo, feature)

        if state is None:
            continue

        if locked(run_lock(repo, feature)):
            # A run that holds its feature but no slot yet (fetching, provisioning) is about to take one.
            busy += (state['stage'], state['status']) != ('done', 'done')
        elif polls_pr(state) and (actionable(state) or merged_while_paused(state)):
            pollers.append(feature_key(repo, feature))
            spawn_feature_run(repo, feature)
        elif actionable(state):
            why = hold_of(repo, feature, control)

            if why:
                held.append(f'{adapter.short(repo)}/{feature} ({hold_label(why)})')
            else:
                stages.append((queue_key(repo, feature, state, control['order']), repo, feature))
        elif state['status'] in ('paused', 'awaiting-approval'):
            waiting.append(f"{adapter.short(repo)}/{feature} {state['id']} [{state['status']}]")

    free = live_parallel(limits, control) - max(slots_held(), busy)
    problem = admission_problem(slot=False) if stages and free > 0 else None
    started = [feature for _, _, feature in sorted(stages)[:max(0, free)]] if not problem else []

    for _, repo, feature in sorted(stages)[:len(started)]:
        spawn_feature_run(repo, feature)

    print(f"tick: started {', '.join(started + [key.rpartition('/')[2] for key in pollers]) or 'nothing'}", flush=True)

    if len(stages) > len(started):
        print(f"tick: {len(stages) - len(started)} queued ({problem or f'{max(0, free)} free slots'})", flush=True)

    if held:
        print(f"tick: held: {', '.join(held)}", flush=True)

    if waiting:
        print(f"tick: waiting on you: {', '.join(waiting)} (pipeline status)", flush=True)

    for line in skipped_lines():
        print(f'tick: {line}', flush=True)

    leftovers = remove_closed_sandboxes()

    if leftovers:
        print(f"tick: removed leftover sandboxes of closed steps: {', '.join(leftovers)}", flush=True)

    return 0


def remove_closed_sandboxes():
    """A merged step's sandbox whose removal failed: tick retries it. Never while the capped unit is down (an sbx call
    would autostart an uncapped daemon), never a step that is not closed."""
    if not sbx_service_active():
        return []

    closed = set()

    for repo, feature in adapter.all_features():
        with contextlib.suppress(*ERRORS):
            closed |= {backend.sandbox_name(adapter.short(repo), feature, step.id) for step in steps(repo, feature)
                       if load_state(repo, feature, step.id)['status'] == 'closed'}

    if not closed:
        return []

    try:
        leftovers = sorted(closed & set(backend.statuses()))
    except ERRORS as error:
        print(f'tick: leftover sandboxes unread: {error}', flush=True)
        return []

    return [name for name in leftovers if backend.remove(name).returncode == 0]


def held_pid(path):
    """The pid in a lock file, when the lock is held and that process is a pipeline run (never a stale number)."""
    if not locked_path(path):
        return None

    try:
        pid = int(path.read_text().strip())
        command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except (OSError, ValueError):
        return None

    return pid if 'pipeline' in command else None


def stop_units():
    """Stops every transient run unit; systemd SIGTERMs each run, which stops its stage and marks it resumable."""
    subprocess.run(['systemctl', '--user', 'stop', 'pipeline-run-*'], capture_output=True, timeout=600)


def command_stop(argv):
    if argv == ['--clear']:
        STOP_FLAG.unlink(missing_ok=True)
        kick_tick()
        print('kill switch off: tick resumes the stopped steps now')
        return 0

    if argv != ['--all']:
        return usage_error('stop')

    LOGS.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.write_text(now_iso() + '\n')
    stop_units()
    signalled = []

    for path in LOGS.glob('run-*.lock'):
        pid = held_pid(path)

        if pid and pid != os.getpid():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
                signalled.append(path)

    deadline = time.monotonic() + KILL_SWITCH_WAIT_SECONDS

    while any(locked_path(path) for path in signalled) and time.monotonic() < deadline:
        time.sleep(0.5)

    if not sbx_service_active():
        # The README's cutover runs this while the unit is down: an sbx call would autostart the daemon that blocks it.
        print(f'kill switch on: runs stopped, no sandbox runs ({SBX_UNIT} not active); `pipeline stop --clear` resumes')
        return 0

    stopped = []

    for name, status in backend.statuses().items():
        if name.startswith('pl-') and status == 'running':
            backend.stop(name)
            stopped.append(name)

    print(f"kill switch on: runs stopped, sandboxes stopped ({', '.join(stopped) or 'none running'}); "
          '`pipeline stop --clear` resumes')

    return 0


def refuse_while_sbx_down(command):
    """True (after saying why) while the capped unit is down: any sbx call would autostart an uncapped daemon."""
    if sbx_service_active():
        return False

    print(f'pipeline {command}: {SBX_UNIT} not active, and any sbx call would autostart an uncapped daemon '
          f'(systemctl --user start {SBX_UNIT})', file=sys.stderr)

    return True


def command_gc():
    if refuse_while_sbx_down('gc'):
        return 1

    cutoff = time.time() - SNAPSHOT_DAYS * 86400
    keep, dropped, removed = set(), 0, 0

    for repo in adapter.load_config()['repos']:
        for tree in (adapter.checkout(repo),):
            if not (tree / '.git').exists():
                continue

            for ref in git(tree, 'for-each-ref', '--format=%(refname)', 'refs/snapshots/').splitlines():
                match = re.search(r'/(\d{13})-[^/]*$', ref)

                if match and int(match.group(1)) < cutoff * 1000:
                    git(tree, 'update-ref', '-d', ref)
                    dropped += 1

        clone = adapter.clone_path(repo)

        for feature in adapter.repo_features(repo):
            for step in steps(repo, feature):
                state = load_state(repo, feature, step.id)
                name = backend.sandbox_name(adapter.short(repo), feature, step.id)

                if state['status'] != 'closed':
                    keep.add(name)
                    continue

                if (clone / '.git').exists():
                    for ref in git(clone, 'for-each-ref', '--format=%(refname)', f'refs/pipeline/{feature}/{step.id}',
                                   f'refs/pipeline/{feature}/{step.id}-pre-rebase-*').splitlines():
                        git(clone, 'update-ref', '-d', ref)

                for path in state_dir(repo, feature).glob(f'{step.id}-*'):
                    if path.suffix in ('.log', '.json', '.md') and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1

    orphans = [name for name in backend.statuses() if name.startswith('pl-') and name not in keep]

    for name in orphans:
        backend.remove(name)

    print(f'dropped {dropped} snapshot refs older than {SNAPSHOT_DAYS} days; removed {removed} logs of merged steps '
          f"older than {LOG_DAYS} days; removed orphan sandboxes: {', '.join(orphans) or 'none'}")

    return command_template_prune()


# ---------------------------------------------------------------------------------------------- templates


def template_summary(policy=None):
    """One line: the target's Claude Code version, the policy and whether it is loaded (else what happens next)."""
    policy = policy or template_policy()
    kind = 'host' if policy['claude'] == 'host' else 'pinned'

    try:
        tag = target_tag(policy)
        loaded = backend.template_loaded(tag)
    except (*ERRORS, OSError) as error:
        return f"? ({kind}, {policy['build']}): {error_line(str(error))}"

    version = template_name(tag).split('-')[1]
    marker = template_marker()

    if loaded:
        state = 'loaded'
    elif marker.get('tag') == tag and marker.get('failures'):
        state = f"MISSING (build failed {marker['failures']}×: {marker['error']}; fix: pipeline template build)"
    elif policy['build'] == 'manual':
        state = 'MISSING (fix: pipeline template build)'
    else:
        state = 'MISSING (the next sandbox create builds it)'

    return f"claude {version} ({kind}, {policy['build']}) · {state}"


def gigabytes(size):
    return f'{size / 1e9:.1f} GB'


def command_template_show():
    policy = template_policy()
    host = [backend.run([command, '--version']).stdout.split()[:1] for command in ('claude', 'node')]
    print(f"host: claude {''.join(host[0]) or '?'} · node {''.join(host[1]).lstrip('v') or '?'}")
    print(f"policy: claude {policy['claude']} · build {policy['build']} (config.json \"template\")")
    print(f'target: {template_summary(policy)}')
    target = target_tag(policy)
    users = collections.defaultdict(list)

    for name in sorted(backend.statuses()):
        try:
            users[backend.sandbox_image(name)].append(name)
        except backend.BackendError:
            users['?'].append(name)

    for tag, image_id, size in backend.templates():
        marks = (['target'] if tag == target else []) + ([f"used by {', '.join(users[tag])}"] if users[tag] else [])
        print(f"  {template_name(tag)}  {image_id}  {gigabytes(size)}  {' · '.join(marks) or 'unused'}")

    if users['?']:
        print(f"  ? (sbx inspect failed): {', '.join(users['?'])}")

    marker = template_marker()

    if marker:
        what = f"build failed {marker['failures']}×: {marker['error']}" if marker['failures'] else 'missing'
        print(f"marker: {template_name(marker['tag'])} {what} "
              f"(since {time.strftime('%a %H:%M', time.localtime(marker['at']))}; {TEMPLATE_FAILED})")

    return 0


def command_template_build(version):
    """Builds now, in the foreground, under the runner's template lock; on success the steps waiting for that template
    lose their wait, so tick resumes them within 5 min."""
    policy = template_policy()
    claude = version or policy['claude']
    print(f'building template claude {claude} (takes about a minute) …', flush=True)

    with adapter.exclusive('template'):
        built, result = backend.build_template(claude)

        if not built:
            line = error_line(result.stderr + '\n' + result.stdout)
            print(f'build failed (exit {result.returncode}): {line}\n{tail(result.stderr or result.stdout, 30)}',
                  file=sys.stderr)
            return 1

        if template_marker().get('tag') == built:
            TEMPLATE_FAILED.unlink(missing_ok=True)

    name, resumed = template_name(built), []

    for repo, feature in adapter.all_features():
        for step in steps(repo, feature):
            state = load_state(repo, feature, step.id)

            if state['reason'].startswith((f'waiting for template {name}', f'template {name} missing')):
                state['not_before'] = None
                save_state(state)
                resumed.append(f'{adapter.short(repo)}/{feature} {step.id}')

    print(f"built {built}; tick resumes: {', '.join(resumed) or 'nothing waits for it'}")

    return 0


def command_template_prune():
    """Removes every pipeline template that is neither the target nor the image of an existing sandbox (any sandbox:
    gate ones too) from sbx and docker, and build-template.sh's leftover tars. One unreadable sandbox ⇒ nothing goes."""
    with adapter.exclusive('template'):
        keep = {target_tag()}

        for name in backend.statuses():
            try:
                keep.add(backend.sandbox_image(name) or '')
            except backend.BackendError as error:
                print(f'template prune: {error_line(str(error))}; removed nothing', file=sys.stderr)
                return 1

        sizes = {tag: size for tag, _, size in backend.templates()}
        removed = sorted((set(sizes) | set(backend.built_images())) - keep)

        for tag in removed:
            backend.remove_template(tag)

        tars = [path for path in backend.TEMPLATE_CACHE.glob('pipeline-sandbox_*.tar')
                if path.stat().st_mtime < time.time() - TAR_STALE_SECONDS]
        freed = sum(sizes.get(tag, 0) for tag in removed) + sum(path.stat().st_size for path in tars)

        for path in tars:
            path.unlink()

    kept = sorted(template_name(tag) for tag in keep if tag.startswith(backend.TEMPLATE_REPOSITORY + ':'))
    print(f"template prune: kept {', '.join(kept)}; removed "
          f"{', '.join(template_name(tag) for tag in removed) or 'no template'} and {plural(len(tars), 'tar')} "
          f'({gigabytes(freed)} freed in sbx and the cache, plus their docker images)')

    return 0


def command_template(argv):
    if refuse_while_sbx_down('template'):
        return 1

    if not argv:
        return command_template_show()

    if argv[0] == 'build' and len(argv) <= 2:
        version = argv[1] if len(argv) == 2 else None

        if version and not adapter.CLAUDE_VERSION.match(version):
            print(f'pipeline template build: a version like 2.1.287, not {version!r}', file=sys.stderr)
            return 2

        return command_template_build(version)

    if argv == ['prune']:
        return command_template_prune()

    return usage_error('template')


def expect(label, ok, detail=''):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f': {detail}' if detail else ''))

    return ok


def probe(label, call):
    try:
        return expect(label, True, str(call())[:160])
    except (PipelineError, KeyError, TypeError, IndexError) as error:
        return expect(label, False, str(error)[:200])


def command_prove_app(repo):
    """Read-only: the key, a repo-scoped token and its permissions (actions write optional: CI reruns), and every
    endpoint the PR loops read."""
    app = app_settings()

    try:
        check_key(app)
        token = installation_token(repo)
    except PipelineError as error:
        print(f'FAIL  {error}')
        return 1

    cached = TOKEN_CACHE[repo]
    permissions = cached['permissions']
    print(f"App {app['id']} ({app['slug']}), installation {cached['installation']}, token scoped to {repo}, "
          f"expires in {int((cached['expires'] - time.time()) / 60)} min (never printed)")
    print(f'permissions: {json.dumps(permissions, sort_keys=True)}')
    wanted = {'contents': 'write', 'pull_requests': 'write', 'checks': 'read', 'actions': 'read', 'metadata': 'read'}
    results = [expect(f'permission {key}: {level}', permissions.get(key) in ((level, 'write') if level == 'read'
                                                                              else (level,)), permissions.get(key))
               for key, level in wanted.items()]
    results.append(expect('no workflows permission', 'workflows' not in permissions))
    print('note: actions ' + ('write: CI reruns automatically (it also allows workflow dispatch and deleting runs)'
                              if permissions.get('actions') == 'write' else
                              'read: CI auto-rerun off; a red run with no failed step pauses for a re-run on GitHub'))
    reachable = github_api('GET', '/installation/repositories', token)
    names = [item.get('full_name') for item in reachable.get('repositories', [])]
    results.append(expect(f'token reaches only {repo}', names == [repo], ', '.join(names)))
    try:
        base = adapter.repo_config(repo)['base']
    except adapter.AdapterError as error:
        base = 'main'
        print(f'note: {error}; probing {base}')
    rules = branch_rules(repo, base)
    results.append(expect(f'rules on {base}', bool(rules), str([rule.get('type') for rule in rules or []])))
    pulls = github_api('GET', f'/repos/{repo}/pulls?state=all&per_page=1', token)

    if pulls:
        number = pulls[0]['number']
        results.append(probe(f'PR #{number} mergeable', lambda: github_api('GET', f'/repos/{repo}/pulls/{number}', token)
                             .get('mergeable_state')))
        results.append(probe(f'PR #{number} reviews, inline and conversation comments', lambda: [
            len(github_api('GET', f'/repos/{repo}/{path}?per_page=100', token))
            for path in (f'pulls/{number}/reviews', f'pulls/{number}/comments', f'issues/{number}/comments')]))
        results.append(probe(f'PR #{number} via GraphQL (ready flip reads the same node)', lambda: graphql(
            token, 'query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) '
                   '{ pullRequest(number: $number) { isDraft } } }',
            {'owner': repo.split('/')[0], 'name': adapter.short(repo), 'number': number})))
    else:
        print('SKIP  PR endpoints: the repo has no PR yet')

    head = github_api('GET', f'/repos/{repo}/commits/{base}', token)['sha']
    runs = github_api('GET', f'/repos/{repo}/commits/{head}/check-runs?per_page=20', token).get('check_runs') or []
    results.append(expect(f'check runs on {base}', True, ', '.join(sorted({run['name'] for run in runs})) or 'none'))
    jobs = [run for run in runs if (run.get('app') or {}).get('slug') == 'github-actions']

    if jobs:
        job_log = f"/repos/{repo}/actions/jobs/{jobs[0]['id']}/logs"
        results.append(probe(f"job log of {jobs[0]['name']} (redirect, no token on the download)",
                             lambda: f'{len(github_raw(job_log, token))} bytes'))
        results.append(probe(f'workflow runs on {base} and their jobs (CI rerun triage)', lambda: [
            len(github_api('GET', f"/repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs",
                           token).get('jobs') or [])
            for run in github_api('GET', f'/repos/{repo}/actions/runs?head_sha={head}&per_page=5',
                                  token).get('workflow_runs') or []]))

    print('writes (draft PR, ready flip, comment, review request) are exercised by `pipeline prove-gate`; a CI rerun '
          'only by a real infra failure')

    return 0 if all(results) else 1


def expect_refusal(label, call):
    try:
        call()
    except PipelineError as error:
        print(f'PASS  {label}: refused ({str(error)[:120]})')
        return True

    print(f'FAIL  {label}: allowed')
    return False


def command_prove_gate(repo):
    """Proves, with a throwaway agent/prove-* branch and draft PR, that GitHub refuses the App everything but its own
    branches and never merges without your review, and that the PR loop's writes work. Cleans up after itself."""
    refresh_clone(repo)
    config = adapter.repo_config(repo)
    base = config['base']
    rules = branch_rules(repo, base) or []
    reviewed = any(rule.get('type') == 'pull_request' and (rule.get('parameters') or {}).get('require_code_owner_review')
                   for rule in rules)

    if not reviewed:
        print(f'FAIL  {base} has no code-owner review rule; not attempting a merge (it might succeed)')
        return 1

    results = []

    for branch in config['github']['protected']:
        types = {rule.get('type') for rule in (branch_rules(repo, branch) or [])}
        results.append(expect(f'{branch} has an update restriction', 'update' in types, str(sorted(types))))

    token = installation_token(repo)
    stamp = int(time.time())
    branch = f'agent/prove-{stamp}'
    base_sha_value = github_api('GET', f'/repos/{repo}/git/ref/heads/{base}', token)['object']['sha']
    github_api('POST', f'/repos/{repo}/git/refs', token, {'ref': f'refs/heads/{branch}', 'sha': base_sha_value})
    print(f"PASS  created {branch} (agent/** is the App's to write)")
    results += [
        expect_refusal('creating a non-agent branch', lambda: github_api(
            'POST', f'/repos/{repo}/git/refs', token, {'ref': f'refs/heads/prove-{stamp}', 'sha': base_sha_value})),
        expect_refusal('writing a workflow file', lambda: github_api(
            'PUT', f'/repos/{repo}/contents/.github/workflows/prove-{stamp}.yml', token,
            {'message': 'prove', 'branch': branch, 'content': base64url(b'name: prove\n')})),
    ]
    github_api('PUT', f'/repos/{repo}/contents/.pipeline-prove-{stamp}.md', token,
               {'message': 'chore: Pipeline gate probe (throwaway)', 'branch': branch,
                'content': base64.b64encode(b'throwaway\n').decode()})
    pull = github_api('POST', f'/repos/{repo}/pulls', token,
                      {'title': f'chore: Pipeline gate probe {stamp} (throwaway)', 'head': branch, 'base': base,
                       'body': 'Opened by `pipeline prove-gate`; closed by it too.', 'draft': True})
    number = pull['number']
    print(f'PASS  opened draft PR #{number}')

    try:
        comment = github_api('POST', f'/repos/{repo}/issues/{number}/comments', token, {'body': 'prove: comment'})
        github_api('PATCH', f"/repos/{repo}/issues/comments/{comment['id']}", token, {'body': 'prove: edited'})
        results.append(expect('comment posted and edited in place', True))
        results.append(probe('draft → ready for review (GraphQL)', lambda: graphql(
            token, 'mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) '
                   '{ pullRequest { isDraft } } }', {'id': pull['node_id']})))
        results.append(probe('review requested', lambda: github_api(
            'POST', f'/repos/{repo}/pulls/{number}/requested_reviewers', token,
            {'reviewers': [app_settings()['reviewer']]}).get('number')))
        results.append(expect_refusal('merging the PR without your approval', lambda: github_api(
            'PUT', f'/repos/{repo}/pulls/{number}/merge', token, {'merge_method': 'squash'})))
    finally:
        github_api('PATCH', f'/repos/{repo}/pulls/{number}', token, {'state': 'closed'})
        github_api('DELETE', f'/repos/{repo}/git/refs/heads/{branch}', token)
        print(f'PASS  closed PR #{number} and deleted {branch}')

    return 0 if all(results) else 1


def command_queue():
    """The queue alone: what runs, what starts next (in order), what is held, what waits, and the backlog."""
    model = status_model()
    model['backlog'] = [name for repo in adapter.load_config()['repos']
                        for name in adapter.feature_folders(repo, adapter.backlog_root(repo))]
    stream = sys.stdout
    width = shutil.get_terminal_size().columns if stream.isatty() else None

    for line in render(model, use_color(stream), width, QUEUE_GROUPS):
        print(line)

    return 0


def feature_argument(command, argv, extra=()):
    """(feature, repo, flags) from `<feature> [--repo <o>/<r>] [flags]`; repo None = usage error printed."""
    rest = list(argv)
    flags = [word for word in rest if word in extra]
    rest = [word for word in rest if word not in extra]

    if not rest or not FEATURE_NAME.match(rest[0]):
        usage_error(command)
        return None, None, flags

    feature, rest = rest[0], rest[1:]
    repo = pick_repo(rest, feature)

    if rest:
        usage_error(command)
        return None, None, flags

    return feature, repo, flags


def unpark(feature, argv):
    """Moves a parked feature out of _backlog/ (into the checkout's plans) and returns its repo, or None."""
    rest = list(argv)
    repo = pick_repo(rest) if '--repo' in rest else None
    repos = [repo] if repo else list(adapter.load_config()['repos'])
    parked = [name for name in repos if (adapter.backlog_root(name) / feature).is_dir()]

    if not parked or feature_anywhere(feature):
        return None

    if len(parked) > 1:
        raise PipelineError(f'{feature} is parked in {", ".join(parked)}: pass --repo <owner>/<repo>')

    os.rename(adapter.backlog_root(parked[0]) / feature, adapter.plans_root(parked[0]) / feature)
    print(f'{feature}: moved out of {adapter.backlog_root(parked[0])}')

    return parked[0]


def command_start(argv):
    """No feature: ends the drain. A feature: unparks it, lifts its hold, and starts it (a paused step at once, as
    --detach; else tick, now, when a slot is free)."""
    if not argv:
        update_global_control(drain=False)
        kick_tick()
        print('drain ended: tick starts queued work now (pipeline queue)')
        return 0

    argv, after = list(argv), None

    if '--after' in argv:
        index = argv.index('--after')
        after = argv[index + 1] if index + 1 < len(argv) else ''
        del argv[index:index + 2]

        if not AFTER.match(after):
            return usage_error('start')

    if not argv or not FEATURE_NAME.match(argv[0]):
        return usage_error('start')

    unpark(argv[0], argv[1:])
    feature, repo, _ = feature_argument('start', argv)

    if repo is None:
        return 2

    if after and AFTER.match(after).group(1) == feature:
        print(f'{feature} cannot wait for itself', file=sys.stderr)
        return 2

    update_feature_control(repo, feature, hold=False, after=after)
    state, step = current_step(repo, feature)

    if after and after_unmet(repo, after):
        print(f'{feature}: starts {after_unmet(repo, after)} merges (tick releases it; pipeline start {feature} '
              'drops the wait)')
        return 0

    if state is None:
        print(f'{feature}: every step is merged')
        return 0

    if state['status'] == 'awaiting-approval':
        print(f'{feature} {step.name}: awaiting your answer: pipeline approve {feature}  |  pipeline revise {feature}')
        return 0

    if state['status'] == 'paused' and not polls_pr(state):
        return command_detach(repo, argparse_parser().parse_args([feature]))

    kick_tick()
    print(f'{feature} {step.name}: released; ' + ('its PR waits for you' if polls_pr(state) else
                                                  'tick starts it when a slot is free (pipeline queue)'))

    return 0


def interrupt_run(path):
    """SIGTERMs the run holding a run lock (its stage stops, resumable); False when none holds it."""
    pid = held_pid(path)

    if not pid or pid == os.getpid():
        return False

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
        return True

    return False


def command_pause(argv):
    """A feature: hold it (its running stage finishes, no new one starts; --now interrupts it, resumable). No
    feature: drain everything the same way."""
    now = '--now' in argv
    rest = [word for word in argv if word != '--now']

    if not rest:
        update_global_control(drain=True)
        stopped = [path for path in LOGS.glob('run-*.lock') if now and interrupt_run(path)]
        print('drained: no new stage starts' + (f'; interrupted {plural(len(stopped), "run")}' if now else
                                                '; running stages finish') + ' (pipeline start ends it)')
        return 0

    feature, repo, _ = feature_argument('pause', rest)

    if repo is None:
        return 2

    update_feature_control(repo, feature, hold=True)
    running = locked(run_lock(repo, feature))
    stopped = now and interrupt_run(LOGS / f'{run_lock(repo, feature)}.lock')
    print(f'{feature}: held' + ('; its run was interrupted (resumable)' if stopped else
                                '; its running stage finishes first' if running else '') + f' (pipeline start {feature})')

    return 0


def command_order(argv):
    """Puts these features first in tick's start order, in this order; the rest keep theirs."""
    argv, repo = list(argv), None

    if '--repo' in argv:
        index = argv.index('--repo')
        repo = argv[index + 1] if index + 1 < len(argv) else None
        del argv[index:index + 2]

        if not repo:
            return usage_error('order')

    if not argv or not all(FEATURE_NAME.match(word) for word in argv):
        return usage_error('order')

    keys = [feature_key(adapter.repo_for(feature, repo), feature) for feature in argv]

    with adapter.exclusive('control'):
        control = global_control()
        known = {feature_key(repo, feature) for repo, feature in adapter.all_features()}
        order = keys + [key for key in control['order'] if key not in keys and key in known]
        write_control(CONTROL, dict(control, order=order))

    return command_queue()


def command_parallel(argv):
    """Shows or sets how many stages run at once (1..config.json admission.slots). Lowering never stops a running
    stage: the runs above it finish theirs and no new one starts until fewer run."""
    limits = adapter.load_config()['admission']
    ceiling, held = limits['slots'], slots_held()

    if not argv:
        print(f'parallel {live_parallel(limits)} (ceiling {ceiling}: config.json admission.slots) · '
              f'{plural(held, "stage slot")} held')
        return 0

    if len(argv) != 1 or not argv[0].isdigit() or not 1 <= int(argv[0]) <= ceiling:
        print(f'pipeline parallel: a number 1..{ceiling} (config.json admission.slots is the ceiling)', file=sys.stderr)
        return 2

    parallel = int(argv[0])
    update_global_control(parallel=parallel)
    kick_tick()
    holding = f"{plural(held, 'run')} {'holds a stage slot' if held == 1 else 'hold stage slots'}"
    print(f'parallel {parallel}' + (f'; {holding}: no new stage starts until fewer than {parallel} do'
                                    if held >= parallel else ''))

    return 0


def command_answer(repo, feature):
    """Opens the waiting plan in $VISUAL/$EDITOR: a changed plan.md is approved (your answers under `## Unresolved
    questions`); an unchanged one changes nothing."""
    state, step = awaiting_step(repo, feature)

    if state is None:
        print(f'{feature}: no step is awaiting your answer', file=sys.stderr)
        return 1

    if state['stage'] != 'plan':
        print(f"{feature} {step.name}: review round {state['round']} waits for your call: pipeline approve {feature} "
              f'(findings: {step.review(state["round"])})', file=sys.stderr)
        return 1

    before = read_step_file(step.plan)
    editor = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    subprocess.run([*editor, str(step.plan)])

    if read_step_file(step.plan) == before:
        print(f'{step.plan.name} unchanged: nothing approved (pipeline answer {feature} again, or approve/revise)')
        return 0

    code = command_review_decision('approve', repo, feature)

    if code == 0:
        kick_tick()
        print(f'{feature} {step.name}: approved; tick continues it now')

    return code


def log_events(lines, workspace):
    """`HH:MM what` for each tool call, session start and result in stream-json lines (other lines are skipped)."""
    out = []

    for line in lines:
        event = parse_json(line)
        at = parse_iso(event.get('timestamp') or '')
        clock = time.strftime('%H:%M', time.localtime(at)) if at else '     '

        if event.get('type') == 'system' and event.get('subtype') == 'init':
            out.append(f"{clock} session start ({event.get('model')})")
        elif event.get('type') == 'assistant':
            for item in (event.get('message') or {}).get('content') or []:
                if isinstance(item, dict) and item.get('type') == 'tool_use':
                    out.append(f"{clock} {item.get('name')} {tool_target(item.get('input'), workspace)}".rstrip())
        elif event.get('type') == 'result':
            structured = event.get('structured_output') or {}
            summary = ' '.join(str(structured.get('summary') or event.get('result') or '').split())[:160]
            out.append(f"{clock} result {structured.get('status') or event.get('subtype')}: {summary}")

    return out


def plain_log_events(lines):
    """A gates/setup log's entry lines (`== <time> <what>`) as `HH:MM what`, like a stage's."""
    return [re.sub(r'^\d{4}-\d\d-\d\dT(\d\d:\d\d):\d\d ', r'\1 ', line[3:].rstrip())
            for line in lines if line.startswith('== ')]


def log_reader(path, workspace):
    """A function returning the log's new event lines since its last call (whole lines only)."""
    kind = path.stem.split('-', 1)[1] if '-' in path.stem else path.stem
    position, pending = 0, ''

    def read():
        nonlocal position, pending

        try:
            with path.open('rb') as handle:
                handle.seek(position)
                data = handle.read()
        except OSError:
            return []

        position += len(data)
        text = pending + data.decode(errors='replace')
        lines, _, pending = text.rpartition('\n')
        lines = lines.split('\n') if lines else []

        return plain_log_events(lines) if kind in ('gates', 'setup') else log_events(lines, workspace)

    return read


def command_log(repo, feature, follow):
    """The current step's newest log as `HH:MM tool target` lines; -f follows it (and the next stage's) until the step
    merges or Ctrl+C."""
    workspace = str(adapter.clone_path(repo))
    state, step = current_step(repo, feature)

    if state is None:
        print(f'{feature}: every step is merged')
        return 0

    logs = step_logs(state)

    if not logs and not follow:
        print(f'{feature} {step.name}: no stage log yet')
        return 0

    path = logs[0] if logs else None
    reader = log_reader(path, workspace) if path else None

    if reader:
        print(f'== {feature} {path.name}')

        for line in reader()[-LOG_LINES:]:
            print(line)

    if not follow:
        return 0

    following = step

    try:
        while True:
            time.sleep(1)
            state, step = current_step(repo, feature)

            if state is None or step.id != following.id:
                for line in reader() if reader else []:
                    print(line)

                print(f'{feature} {following.name}: merged', flush=True)
                return 0

            newest = (step_logs(state) or [None])[0]

            if newest and newest != path:
                if reader:
                    for line in reader():
                        print(line)

                path, reader = newest, log_reader(newest, workspace)
                print(f'== {feature} {path.name}', flush=True)

            for line in reader() if reader else []:
                print(line, flush=True)
    except KeyboardInterrupt:
        return 0


def command_autonomy(argv):
    """Shows or sets who answers a feature's questions: `ask` (you; the default) or `auto` (each plan question is
    decided by the plan's own recommendation and a last review round's blocking findings go to respond; every such
    decision is a ⚑ in the PR and the ping). Blocked or failed stages still stop for you either way."""
    mode = argv[-1] if argv and argv[-1] in AUTONOMY else None
    feature, repo, _ = feature_argument('autonomy', argv[:-1] if mode else argv)

    if repo is None:
        return 2

    if mode:
        update_feature_control(repo, feature, autonomy=mode)

    print(f"{feature}: autonomy {feature_control(repo, feature)['autonomy']}")
    state, step = current_step(repo, feature)

    if mode == 'auto' and state and state['status'] == 'awaiting-approval':
        print(f'{step.name} already waits for you: pipeline approve {feature} (auto answers from the next question on)')

    return 0


def pick_repo(argv, feature=None):
    """--repo from argv, else the repo the feature (or, without one, the cwd or the only registry entry) points to."""
    repo = None

    if '--repo' in argv:
        index = argv.index('--repo')
        repo = argv[index + 1] if index + 1 < len(argv) else None
        del argv[index:index + 2]

        if not repo:
            raise adapter.AdapterError('--repo needs <owner>/<repo>')

    if feature:
        return adapter.repo_for(feature, repo)

    repos = adapter.load_config()['repos']

    if repo:
        return adapter.repo_for('', repo)

    here = Path.cwd().resolve()

    for name in repos:
        root = adapter.checkout(name)

        if here == root or root in here.parents:
            return name

    if len(repos) == 1:
        return next(iter(repos))

    raise adapter.AdapterError(f'several repos are registered: pass --repo ({", ".join(repos)})')


# name → (usage after `pipeline `, what it does, help group). Help, `-h`, usage errors and typo suggestions read it;
# adapter.RESERVED keeps a feature folder from being named like one (tick's `pipeline <feature>` would run the command).
COMMANDS = {
    'status': ('status [-v] [--all] [--watch [-n SECS]]',
               'what runs, waits and needs you (-v: every step in full; --all: merged too; --watch: live, q quits)',
               'daily'),
    'show': ('show <feature> [--repo <o>/<r>]', 'one feature in full: steps, reason, files, PR/CI, logs, history', 'daily'),
    'log': ('log <feature> [--repo <o>/<r>] [-f]',
            "the current step's newest stage log as `HH:MM tool target` lines; -f follows it", 'daily'),
    'answer': ('answer <feature> [--repo <o>/<r>]',
               'open the waiting plan.md in $EDITOR; changed ⇒ approved, unchanged ⇒ nothing', 'daily'),
    'approve': ('approve <feature> [--repo <o>/<r>]',
                "accept plan.md's answers (or let respond fix review round 2's findings)", 'daily'),
    'revise': ('revise <feature> [--repo <o>/<r>]', 'send the plan back (`## Feedback` in plan.md)', 'daily'),
    'respond': ('respond <feature> [--repo <o>/<r>]', 'turn your PR comments into a respond round, now', 'daily'),
    'queue': ('queue', 'running, queued in start order, held, waiting, needs you, backlog', 'queue'),
    'start': ('start [<feature>] [--repo <o>/<r>] [--after <feature>[:NN]]',
              'unpark from _backlog, lift the hold, start it (paused: now) or once that merged; none: end the drain',
              'queue'),
    'pause': ('pause [<feature>] [--repo <o>/<r>] [--now]',
              'hold at the next stage (--now: interrupt, resumable); no feature: drain everything', 'queue'),
    'order': ('order <feature>... [--repo <o>/<r>]', 'start these first, in this order', 'queue'),
    'parallel': ('parallel [N]', 'show or set how many stages run at once: status `stages n running of p` (p = this, '
                              'at most config.json admission.slots, the ceiling)', 'queue'),
    'autonomy': ('autonomy <feature> [--repo <o>/<r>] [ask|auto]',
                 "who answers plan questions: ask = you (default); auto = the plan's recommendations, flagged ⚑",
                 'queue'),
    'tick': ('tick', 'start what has work (the timer does it every 5 min)', 'admin'),
    'gc': ('gc', "clean merged steps' VMs, refs and logs, old snapshot refs, unused templates", 'admin'),
    'stop': ('stop --all | --clear', 'kill switch: stop every run and VM (resumable) | lift it', 'admin'),
    'template': ('template [build [<x.y.z>] | prune]', 'sandbox templates (policy: config.json "template")', 'admin'),
    'prove-app': ('prove-app [--repo <o>/<r>]', 'read-only: what the GitHub App reaches', 'admin'),
    'prove-gate': ('prove-gate [--repo <o>/<r>]', 'throwaway draft PR: GitHub refuses the App all but agent/**',
                   'admin'),
    'help': ('help [<command>]', 'this help, or one command', 'admin'),
}
RUN_USAGE = ('<feature> [--repo <o>/<r>] [--until plan|implement|review|respond] [--step] [--detach]',
             'run the current step on, in the foreground (--detach: background, log logs/<repo>-<feature>.out)')
HELP_GROUPS = (('daily', 'DAILY', 'pipeline status --watch'), ('queue', 'QUEUE', 'pipeline order csv-export'),
               ('run', 'RUN', 'pipeline csv-export --detach'), ('admin', 'ADMIN', 'pipeline stop --all'))
adapter.RESERVED = frozenset(COMMANDS)


def usage_text(command):
    return f'usage: pipeline {COMMANDS[command][0]}'


def usage_error(command):
    print(usage_text(command), file=sys.stderr)

    return 2


def command_help(argv):
    if argv and argv[0] in ('-h', '--help'):
        argv = ['help']

    if argv and argv[0] in COMMANDS:
        usage, summary, _ = COMMANDS[argv[0]]
        print(f'{usage_text(argv[0])}\n  {summary}')
        return 0

    if argv:
        print(f"no command {argv[0]!r}" + suggestion(argv[0]), file=sys.stderr)
        return 2

    print('pipeline: unattended feature pipeline, one PR per step (runbook ~/.claude/pipeline/README.md)')

    for group, title, example in HELP_GROUPS:
        entries = [(usage, summary) for usage, summary, kind in COMMANDS.values() if kind == group]
        entries += [RUN_USAGE] if group == 'run' else []

        if not entries:
            continue

        print(f'\n{title}')

        for usage, summary in entries:
            print(f'  pipeline {usage}' + (f'\n      {summary}' if len(usage) > 38 else ' ' * (39 - len(usage)) + summary))

        print(f'  e.g. {example}')

    print('\n`pipeline <command> -h` shows one command.')

    return 0


def suggestion(word):
    close = difflib.get_close_matches(word, list(COMMANDS), n=3, cutoff=0.6)

    return f"; did you mean: {', '.join(close)}?" if close else '; pipeline help lists the commands'


def feature_anywhere(feature):
    return any((adapter.plans_root(repo) / feature).is_dir() for repo in adapter.load_config()['repos'])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ('-h', '--help', 'help'):
        return command_help(argv[1:] if argv and argv[0] == 'help' else [])

    command = argv[0]

    if command in COMMANDS and any(word in ('-h', '--help') for word in argv[1:]):
        return command_help([command])

    try:
        if command == 'status':
            return command_status(argv[1:])

        if command == 'show':
            rest = argv[1:]

            if not rest or not FEATURE_NAME.match(rest[0]):
                return usage_error(command)

            extra = rest[1:]
            repo = pick_repo(extra, rest[0])

            return usage_error(command) if extra else command_show(repo, rest[0])

        if command == 'stop':
            return command_stop(argv[1:])

        if command == 'queue':
            return command_queue() if len(argv) == 1 else usage_error(command)

        if command == 'log':
            feature, repo, flags = feature_argument(command, argv[1:], extra=('-f', '--follow'))

            return 2 if repo is None else with_stop_signals(lambda: command_log(repo, feature, bool(flags)))

        if command == 'answer':
            feature, repo, _ = feature_argument(command, argv[1:])

            return 2 if repo is None else with_lock(run_lock(repo, feature), lambda: command_answer(repo, feature))

        if command in ('start', 'pause', 'order', 'parallel', 'autonomy'):
            return {'start': command_start, 'pause': command_pause, 'order': command_order,
                    'parallel': command_parallel, 'autonomy': command_autonomy}[command](argv[1:])

        if command in ('tick', 'gc') and len(argv) > 1:
            return usage_error(command)

        if command == 'tick':
            return with_lock('tick', command_tick)

        if command == 'gc':
            return with_lock('gc', command_gc)

        if command == 'template':
            return command_template(argv[1:])

        if command in ('prove-app', 'prove-gate'):
            extra = argv[1:]
            repo = pick_repo(extra)

            if extra:
                return usage_error(command)

            return command_prove_app(repo) if command == 'prove-app' else \
                with_lock('prove-gate', lambda: command_prove_gate(repo))

        if command in ('approve', 'revise', 'respond'):
            rest = argv[1:]

            if not rest or not FEATURE_NAME.match(rest[0]):
                return usage_error(command)

            feature, extra = rest[0], rest[1:]
            repo = pick_repo(extra, feature)

            if extra:
                return usage_error(command)

            if command == 'respond':
                return with_lock(run_lock(repo, feature), lambda: command_respond(repo, feature))

            return with_lock(run_lock(repo, feature), lambda: command_decide(command, repo, feature))
    except ERRORS as error:
        print(error, file=sys.stderr)
        return 1

    try:
        if FEATURE_NAME.match(command) and not feature_anywhere(command):
            # Neither a command nor a feature: a typo, most likely (a real feature is never shadowed by a suggestion).
            print(f'no feature or command {command!r}' + suggestion(command), file=sys.stderr)
            return 2
    except ERRORS as error:
        print(error, file=sys.stderr)
        return 1

    parser = argparse_parser()
    options = parser.parse_args(argv)

    if not FEATURE_NAME.match(options.feature):
        parser.error('a feature is a folder name of lowercase letters, digits and dashes')

    try:
        repo = adapter.repo_for(options.feature, options.repo)
    except ERRORS as error:
        print(error, file=sys.stderr)
        return 1

    if options.detach:
        # Before the lock: the child takes it, and would exit at once while this process held it.
        return command_detach(repo, options)

    return with_lock(run_lock(repo, options.feature), lambda: with_stop_signals(lambda: command_run(repo, options)))


def argparse_parser():
    import argparse

    parser = argparse.ArgumentParser(prog='pipeline')
    parser.add_argument('feature')
    parser.add_argument('--repo')
    parser.add_argument('--until', choices=['plan', 'implement', 'review', 'respond'])
    parser.add_argument('--step', action='store_true')
    parser.add_argument('--detach', action='store_true', help='run in the background (a transient unit, like tick)')

    return parser


if __name__ == '__main__':
    sys.exit(main())
