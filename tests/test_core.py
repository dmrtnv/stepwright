"""Runner tests: a fake `sbx` keeps each sandbox in a fixture directory (its home and its own clone of the runner's
clean clone) and runs `exec` on the host; fake `claude`, `pnpm` and `curl` on PATH; real git with a bare origin standing
in for GitHub; a fake GitHub API.

Run: python3 -m unittest discover tests
"""
import base64
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapter  # noqa: E402
import backend  # noqa: E402
import core  # noqa: E402

SPAWN = core.spawn_feature_run  # the real one: RunnerCase patches core's
NOTIFY = core.notify  # the real one too

FAKE_SBX = r'''#!/usr/bin/python3
"""Fake sbx: each sandbox is <root>/<name>/{home,workspace}; exec runs on the host with HOME=<home>, in its own
session, marked FAKE_SBX_NAME=<name> so stop can end whatever it left (as a VM stop would)."""
import fcntl, json, os, pathlib, shutil, signal, subprocess, sys, time

root = pathlib.Path(os.environ['FAKE_SBX_ROOT'])
root.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]

with open(root / 'calls.jsonl', 'a') as handle:
    handle.write(json.dumps(args) + '\n')

lock = open(root / 'meta.lock', 'a+')
GITHUB_URL = 'https://github.invalid/acme/shop.git'


def load():
    path = root / 'meta.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save(meta):
    (root / 'meta.json').write_text(json.dumps(meta))


def git(*arguments):
    subprocess.run(['git', *arguments], check=True, capture_output=True)


def add_remote(name, info):
    subprocess.run(['git', '-C', info['source'], 'remote', 'remove', f'sandbox-{name}'], capture_output=True)
    git('-C', info['source'], 'remote', 'add', f'sandbox-{name}', str(root / name / 'workspace'))


def kill(name):
    marker = f'FAKE_SBX_NAME={name}'.encode()

    for _ in range(3):
        found = False

        for entry in os.listdir('/proc'):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue

            try:
                environ = pathlib.Path(f'/proc/{entry}/environ').read_bytes().split(b'\0')
            except OSError:
                continue

            if marker in environ:
                found = True

                try:
                    os.kill(int(entry), signal.SIGKILL)
                except OSError:
                    pass

        if not found:
            return

        time.sleep(0.2)


def stop(meta, name):
    info = meta[name]
    kill(name)
    subprocess.run(['git', '-C', info['source'], 'remote', 'remove', f'sandbox-{name}'], capture_output=True)
    info['status'] = 'stopped'


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


command = args[0]

if command == 'create':
    name = args[args.index('--name') + 1]

    source = args[-1]
    image = args[args.index('-t') + 1] if '-t' in args else None

    if os.environ.get('FAKE_SBX_CREATE_FAIL'):
        fail(f"✓ Git repository detected: {source}\nerror: create failed: no kvm\nSee 'sbx diagnose'.")

    if image and image not in json.loads((root / 'templates.json').read_text()):
        fail(f"✓ Git repository detected: {source}\nerror: image \"{image}\" is not cached locally and "
             'pull_policy=never; pre-pull the image or use pull_policy=missing')

    home, workspace = root / name / 'home', root / name / 'workspace'
    home.mkdir(parents=True)
    git('clone', '--quiet', source, str(workspace))

    for key, value in (('user.email', 'agent@t'), ('user.name', 'agent'), ('commit.gpgsign', 'false')):
        git('-C', str(workspace), 'config', key, value)

    # A real --clone copies the source's config, so origin is the source's GitHub URL, unreachable from the VM.
    git('-C', str(workspace), 'remote', 'set-url', 'origin', GITHUB_URL)

    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = load()
        size = args[args.index('-m') + 1] if '-m' in args else '8g'
        memory_mib = int(size[:-1]) * (1024 if size.endswith('g') else 1)
        meta[name] = {'status': 'running', 'source': source, 'rules': [], 'clone': '--clone' in args, 'image': image,
                      'memory_mib': memory_mib}
        add_remote(name, meta[name])
        save(meta)

    print(f'Created sandbox {name}')
elif command == 'ls':
    meta = load()
    print(json.dumps([{'name': name, 'status': info['status'], 'workspaces': [info.get('source')],
                       'memory_mib': info.get('memory_mib', 8192)} for name, info in meta.items()]))
elif command == 'exec':
    rest, env, workdir, interactive = args[1:], {}, None, False

    while rest[0].startswith('-'):
        flag = rest.pop(0)

        if flag == '-e':
            key, _, value = rest.pop(0).partition('=')
            env[key] = value
        elif flag == '-w':
            workdir = rest.pop(0)
        elif flag == '-i':
            interactive = True

    name = rest.pop(0)
    assert rest.pop(0) == '--'

    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = load()

        if name not in meta:
            fail(f'sandbox {name} not found')

        if meta[name]['status'] != 'running':
            meta[name]['status'] = 'running'
            add_remote(name, meta[name])
            # Every start rewrites origin to the source's URL, as a real one does.
            git('-C', str(root / name / 'workspace'), 'remote', 'set-url', 'origin', GITHUB_URL)
            save(meta)
            print(f'Sandbox {name} started successfully', file=sys.stderr)

    if rest[0] == 'pkill':
        # Only this sandbox's processes: a real pkill in a VM never reaches the host.
        signal_name = rest[1].lstrip('-')
        marker = f'FAKE_SBX_NAME={name}'.encode()
        hit = 0

        for entry in os.listdir('/proc'):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue

            try:
                environ = pathlib.Path(f'/proc/{entry}/environ').read_bytes().split(b'\0')
                argv = pathlib.Path(f'/proc/{entry}/cmdline').read_bytes().split(b'\0')
            except OSError:
                continue

            if marker in environ and any(part.endswith(b'/claude') or part == b'claude' for part in argv[:2]):
                os.kill(int(entry), getattr(signal, f'SIG{signal_name}'))
                hit += 1

        sys.exit(0 if hit else 1)

    child_env = dict(os.environ, HOME=str(root / name / 'home'), FAKE_SBX_NAME=name, **env)
    process = subprocess.Popen(rest, cwd=workdir or str(root / name / 'workspace'), env=child_env,
                               stdin=None if interactive else subprocess.DEVNULL, start_new_session=True)
    sys.exit(process.wait())
elif command == 'stop':
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = load()

        if args[1] in meta:
            stop(meta, args[1])
            save(meta)
elif command == 'rm':
    name = [arg for arg in args[1:] if not arg.startswith('-')][0]

    if os.environ.get('FAKE_SBX_RM_FAIL'):
        fail(f'error: remove {name}: device or resource busy')

    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = load()

        if name in meta:
            stop(meta, name)
            del meta[name]
            save(meta)
            shutil.rmtree(root / name, ignore_errors=True)
elif command == 'cp':
    rest = [arg for arg in args[1:] if arg != '-L']
    follow = '-L' in args
    source, target = rest
    meta = load()

    def resolve(spec):
        if ':' in spec and not spec.startswith('/'):
            name, _, path = spec.partition(':')

            if name not in meta:
                fail(f'sandbox {name} not found')

            if not path.startswith('/'):
                fail('sandbox paths must be absolute')

            return path, True

        return spec, False

    source_path, source_vm = resolve(source)
    target_path, _ = resolve(target)

    if not os.path.lexists(source_path.rstrip('/')):
        fail(f'error: path "{source_path}" not found in {"container" if source_vm else "host"}')

    source_path = source_path.rstrip('/')
    destination = pathlib.Path(target_path.rstrip('/'))

    if target_path.endswith('/') or destination.is_dir():
        destination = destination / pathlib.Path(source_path).name

    destination.parent.mkdir(parents=True, exist_ok=True)
    item = pathlib.Path(source_path)

    if item.is_symlink() and not follow:
        os.symlink(os.readlink(item), destination)
    elif item.is_fifo():
        os.mkfifo(destination)
    elif item.is_dir():
        shutil.copytree(item, destination, symlinks=not follow)
    else:
        shutil.copy2(item, destination)
elif command == 'policy':
    verb = args[1]

    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = load()

        if verb == 'ls':
            print(json.dumps({'rules': [{'decision': 'allow',
                                         'resources': ['api.anthropic.com:443', '*.claude.ai', 'statsig.example.org']}]}))
        elif verb in ('allow', 'deny'):
            if verb == 'allow' and os.environ.get('FAKE_POLICY_ALLOW_FAIL'):
                fail('error: allow failed')

            name = args[args.index('--sandbox') + 1]
            meta[name]['rules'].append([verb, args[-1]])
            save(meta)
        elif verb == 'rm':
            if os.environ.get('FAKE_POLICY_RM_FAIL'):
                fail('error: rm failed')

            name = args[args.index('--sandbox') + 1]
            host = args[args.index('--resource') + 1]
            meta[name]['rules'] = [rule for rule in meta[name]['rules'] if rule != ['allow', host]]
            save(meta)
elif command == 'template':
    path = root / 'templates.json'
    listed = json.loads(path.read_text())

    if args[1] == 'ls' and '--json' in args:
        print(json.dumps([{'id': '549730947ed8', 'repository': 'docker.io/docker/sandbox-templates',
                           'tag': 'claude-code-docker', 'size': 900_000_000}] +
                         [{'id': f'{index:012x}', 'repository': tag.rpartition(':')[0],
                           'tag': tag.rpartition(':')[2], 'size': 1_100_000_000}
                          for index, tag in enumerate(listed)]))
    elif args[1] == 'ls':
        print('REPOSITORY TAG\n' + '\n'.join(tag.replace(':', ' ', 1) for tag in listed))
    elif args[1] == 'rm':
        if args[-1] not in listed:
            fail(f'Error: delete template: image not found: request failed: 404 Not Found: no image "{args[-1]}"')

        path.write_text(json.dumps([tag for tag in listed if tag != args[-1]]))
elif command == 'inspect':
    name = args[-1]
    meta = load()

    if os.environ.get('FAKE_SBX_INSPECT_FAIL') == name or name not in meta:
        fail(f'sandbox {name} not found')

    print(json.dumps({'name': name, 'state': meta[name]['status'], 'image': meta[name].get('image')}))
else:
    fail(f'fake sbx: unknown command {command}')
'''

FAKE_CLAUDE = r'''#!/usr/bin/python3
import json, os, pathlib, subprocess, sys, time

arguments = sys.argv[1:]

if arguments == ['--version']:
    print('2.1.285 (Claude Code)')
    sys.exit(0)


def value(flag):
    return arguments[arguments.index(flag) + 1] if flag in arguments else None


name, session = value('--name'), value('--session-id') or value('--resume')
calls = pathlib.Path(os.environ['FAKE_CLAUDE_CALLS'])
previous = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
keys = ('ANTHROPIC_DEFAULT_OPUS_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE',
        'HEAVY_MIN_FREE_GB', 'GIT_EDITOR', 'GIT_TERMINAL_PROMPT', 'CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS', 'HOME',
        'FAKE_SBX_NAME', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS')

with calls.open('a') as handle:
    handle.write(json.dumps({'name': name, 'argv': arguments, 'prompt': value('-p'), 'cwd': os.getcwd(),
                             'resume': '--resume' in arguments, 'session': session,
                             'env': {key: os.environ.get(key) for key in keys}}) + '\n')

behaviors = json.loads(pathlib.Path(os.environ['FAKE_CLAUDE_SCRIPT']).read_text()).get(name, [])
index = sum(1 for call in previous if call['name'] == name)
behavior = behaviors[index] if index < len(behaviors) else {}
transcript = pathlib.Path(os.environ['HOME'], '.claude/projects/project', f'{session}.jsonl')
transcript.parent.mkdir(parents=True, exist_ok=True)

with transcript.open('a') as handle:
    handle.write('{"type":"custom-title"}\n' + ('' if behavior.get('header_only') else '{"type":"user"}\n'))

print('{"type":"system","subtype":"hook_started"}', flush=True)
print(json.dumps({'type': 'system', 'subtype': 'init', 'permissionMode': behavior.get('mode', 'auto'),
                  'model': behavior.get('model', value('--model'))}), flush=True)
print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
    'five_hour': {'utilization': 0.1, 'resetsAt': int(time.time()) + 3600},
    'seven_day': {'utilization': behavior.get('weekly', 0.2), 'resetsAt': int(time.time()) + 86400}}}}), flush=True)

for command in behavior.get('run', []):
    if subprocess.run(command, shell=True).returncode != 0:
        print(f'fake claude: command failed: {command}', flush=True)
        sys.exit(3)

time.sleep(behavior.get('sleep', 0))

if behavior.get('no_result'):
    sys.exit(1)

if behavior.get('budget'):
    print(json.dumps({'type': 'result', 'subtype': 'error_max_budget_usd', 'is_error': True, 'result': None,
                      'total_cost_usd': 61.5}), flush=True)
    sys.exit(1)

if 'rate_limit_in' in behavior:
    print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'rejected', 'resetsAt': int(time.time()) + behavior['rate_limit_in'], 'rateLimitType': 'seven_day'}}),
        flush=True)

if 'error' in behavior:
    print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                      'result': behavior['error']}), flush=True)
    sys.exit(1)

result = {'status': 'done', 'summary': 'ok', 'questions': [], 'flags': [], 'owed': [], 'deferred': []}
result.update(behavior.get('result', {}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok',
                  'structured_output': result, 'permission_denials': behavior.get('denials', [])}), flush=True)
'''

FAKE_NODE = r'''#!/bin/bash
echo v24.21.0
'''

FAKE_BUILD = r'''#!/usr/bin/python3
"""Fake build-template.sh: logs its argv, then "loads" the tag into the fake sbx's templates (or fails)."""
import json, os, pathlib, subprocess, sys

root = pathlib.Path(os.environ['FAKE_SBX_ROOT'])

with open(root / 'builds.jsonl', 'a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\n')

if os.environ.get('FAKE_BUILD_FAIL'):
    print('#7 [3/3] RUN curl -fsSL https://claude.ai/install.sh | bash\n'
          'ERROR: failed to solve: process did not complete successfully: exit code: 22\n'
          'View build details: docker-desktop://dashboard/build', file=sys.stderr)
    sys.exit(1)

version = sys.argv[1] if len(sys.argv) > 1 else subprocess.run(['claude', '--version'], capture_output=True,
                                                               text=True).stdout.split()[0]
tag = os.environ.get('FAKE_BUILD_TAG') or f'pipeline-sandbox:claude-{version}-node-24.21.0'
path = root / 'templates.json'
path.write_text(json.dumps(json.loads(path.read_text()) + [f'docker.io/library/{tag}']))
print('#8 exporting layers done', file=sys.stderr)
print(tag)
'''

FAKE_DOCKER = r'''#!/usr/bin/python3
"""Fake docker: only `image ls` (from docker-images.json) and `image rm`, logged."""
import json, os, pathlib, sys

root = pathlib.Path(os.environ['FAKE_SBX_ROOT'])
args = sys.argv[1:]

with open(root / 'docker.jsonl', 'a') as handle:
    handle.write(json.dumps(args) + '\n')

path = root / 'docker-images.json'
images = json.loads(path.read_text()) if path.exists() else []

if args[:2] == ['image', 'ls']:
    print('\n'.join(images))
elif args[:2] == ['image', 'rm']:
    if args[2] not in images:
        print(f'Error response from daemon: No such image: {args[2]}', file=sys.stderr)
        sys.exit(1)

    path.write_text(json.dumps([image for image in images if image != args[2]]))
'''

FAKE_PNPM = r'''#!/usr/bin/python3
import json, os, pathlib, subprocess, sys

arguments = sys.argv[1:]
key = ' '.join(arguments)

with open(os.environ['FAKE_PNPM_CALLS'], 'a') as handle:
    handle.write(json.dumps({'argv': arguments, 'cwd': os.getcwd()}) + '\n')

control_path = pathlib.Path(os.environ['FAKE_PNPM_CONTROL'])
control = json.loads(control_path.read_text()) if control_path.exists() else {}

for prefix, outcomes in control.items():
    if key.startswith(prefix) and outcomes:
        outcome = outcomes.pop(0)
        control_path.write_text(json.dumps(control))

        if outcome.get('spawn'):
            sleeper = subprocess.Popen(['sleep', '300'], start_new_session=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            pathlib.Path(os.environ['FAKE_SPAWN_PID']).write_text(str(sleeper.pid))

        if outcome.get('commit'):
            subprocess.run(outcome['commit'], shell=True, check=True)

        print(outcome.get('output', ''))
        sys.exit(outcome.get('code', 0))

print(f'pnpm {key}: ok')
'''

FAKE_CURL = r'''#!/bin/bash
if [ "${FAKE_EGRESS:-}" = open ]; then
  printf 'hello\n200'
  exit 0
fi
printf '\n000'
echo 'curl: (56) blocked by sandbox policy' >&2
exit 56
'''

PIPELINE_JSON = {
    'base': 'main',
    'setup': ['pnpm install --frozen-lockfile'],
    'setupEgress': ['registry.npmjs.org'],
    'checkFast': [{'name': 'check', 'run': 'pnpm check'}, {'name': 'test', 'run': 'pnpm test', 'flakeReruns': 1}],
    'protected': ['pnpm-workspace.yaml', '.npmrc', 'pnpm-lock.yaml', '.pnpmfile.cjs', '.github/**'],
    'gateConfig': ['turbo.json', 'vitest.config.*', 'tsconfig*.json', 'scripts/check-*'],
    'dependencyFields': ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies', 'pnpm',
                         'packageManager', 'engines'],
    'migrations': {'dir': 'packages/db/drizzle', 'journal': 'packages/db/drizzle/meta/_journal.json',
                   'generate': 'drizzle-generate.py'},
    'ci': {'requiredChecks': ['check', 'test']},
    'github': {'protected': ['staging']},
}

REPO = 'acme/shop'
FEATURE = 'thing'
STEP = '01-thing'
BRANCH = 'agent/thing-01-thing'
SANDBOX = 'pl-shop-thing-01'
REF = 'refs/pipeline/thing/01'
TAG = 'docker.io/library/pipeline-sandbox:claude-2.1.285-node-24.21.0'  # fake claude --version, fake node


def tag_of(version):
    return f'docker.io/library/pipeline-sandbox:claude-{version}-node-24.21.0'


PLAN = {'run': ["printf '# Plan\\n\\nDigest.\\n' > {step}/plan.md"]}
PLAN_ASKS = {'run': PLAN['run'], 'result': {'questions': ['Q1? Rec: A']}}
IMPLEMENT = {'run': ["echo code > thing.txt && git add -A && git commit -qm 'fix: Do the thing'"]}
REVIEW = {'run': ['echo findings > {step}/review-1.md']}
RESPOND = {'run': ["printf 'fix: Do the thing\\n\\nWhy it matters.\\n' > {step}/pr.md"],
           'result': {'flags': ['⚑ kept the old label'], 'owed': ['after merge: pnpm db:migrate on shop'],
                      'learnings': ['a trap worth remembering']}}
BLOCKED = {'result': {'status': 'blocked', 'summary': 'needs a product call', 'questions': ['Q1? Rec: A']}}
RESOLVE = {'run': ["git reset -q --hard origin/main && echo resolved > thing.txt && git add -A && "
                   "git commit -qm 'fix: Do the thing'"]}
MOVE_MAIN = ("git -C {other} pull -q && echo CONTENT > {other}/FILE && git -C {other} add -A && "
             "git -C {other} commit -qm 'chore: main moved' && git -C {other} push -q origin main")
BOT = 'test-pipeline[bot] 99+test-pipeline[bot]@users.noreply.github.com'
REVIEW_RULES = [{'type': 'pull_request', 'parameters': {'required_approving_review_count': 1,
                                                       'require_code_owner_review': True}}]


def move_main(content, file):
    return MOVE_MAIN.replace('CONTENT', content).replace('FILE', file)


def write(path, content, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    if mode:
        path.chmod(mode)


def git(directory, *arguments):
    return subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


def git_result(directory, *arguments):
    return subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, text=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False

    return Path(f'/proc/{pid}/status').exists() and 'State:\tZ' not in Path(f'/proc/{pid}/status').read_text()


def step(name, status='completed', conclusion='success'):
    return {'name': name, 'status': status, 'conclusion': conclusion}


def job(job_id, name, conclusion, completed_at, *steps):
    return {'id': job_id, 'name': name, 'status': 'completed', 'conclusion': conclusion, 'completed_at': completed_at,
            'steps': [step('Set up job'), *steps] if steps != (None,) else []}


NOISE = {'annotation_level': 'notice', 'message': '"The ubuntu-latest label will migrate to Ubuntu 26 beginning '
                                                  'October 19, 2026. For more information, see …"'}
# Real payloads (repo renamed, trimmed to the fields the runner reads; steps that passed dropped). PR #245's
# `test` hit timeout-minutes 20: cancelled, its step left in_progress, logs 404 (run 37615714066 attempt 1).
TIMEOUT_RUN = {'id': 37615714066, 'workflow_id': 337897504, 'status': 'completed', 'conclusion': 'cancelled',
               'run_attempt': 1, 'check_suite_id': 101897021083}
TIMEOUT_JOBS = [job(112773421896, 'test', 'cancelled', '2026-10-07T12:05:53Z', step('Run pnpm test', 'in_progress', None),
                    step('Post Run pnpm/action-setup@ea17c68', 'pending', None)),
                job(112773422206, 'check', 'success', '2026-10-07T11:44:16Z'),
                job(112773422262, 'e2e', 'success', '2026-10-07T11:51:09Z')]
TIMEOUT_ANNOTATIONS = [{'annotation_level': 'failure',
                        'message': 'The job has exceeded the maximum execution time of 20m0s'}, NOISE]
# Superseded by a newer push to PR #238 (cancel-in-progress; the newer run was on another commit): run 37597201319.
SUPERSEDED_RUN = {'id': 37597201319, 'workflow_id': 337897504, 'status': 'completed', 'conclusion': 'cancelled',
                  'run_attempt': 1, 'check_suite_id': 101846174788}
SUPERSEDED_JOBS = [job(112712622693, 'test', 'cancelled', '2026-10-07T09:08:34Z', step('Run pnpm test', 'completed', 'cancelled')),
                   job(112712623008, 'check', 'success', '2026-10-07T09:02:16Z'),
                   job(112712623086, 'e2e', 'cancelled', '2026-10-07T09:08:35Z',
                       step('Run pnpm test:e2e', 'completed', 'cancelled'))]
SUPERSEDED_ANNOTATIONS = [{'annotation_level': 'failure', 'message': 'Canceling since a higher priority waiting request '
                                                                     'for check-refs/pull/238/merge exists'},
                          {'annotation_level': 'failure', 'message': 'The operation was canceled.'}, NOISE]
# A failing test: run 37328350172 attempt 1.
CODE_RUN = {'id': 37328350172, 'workflow_id': 337897504, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1,
            'check_suite_id': 101105769830}
CODE_JOBS = [job(111824748686, 'test', 'failure', '2026-10-05T15:09:53Z', step('Run pnpm test', 'completed', 'failure')),
             job(111824749113, 'check', 'success', '2026-10-05T14:57:43Z'),
             job(111824749174, 'e2e', 'success', '2026-10-05T15:04:42Z')]
CODE_ANNOTATIONS = [{'annotation_level': 'failure', 'message': 'Process completed with exit code 1.'},
                    {'annotation_level': 'failure', 'message': 'AssertionError: expected [ …(31) ] to deeply equal'}]
# Its re-run: no runner took `test` (cancelled, no steps, the run concluded failure): attempt 2.
NOT_ACQUIRED_RUN = dict(CODE_RUN, run_attempt=2)
NOT_ACQUIRED_JOBS = [job(111977558495, 'test', 'cancelled', '2026-10-05T21:22:22Z', None),
                     job(111977560382, 'e2e', 'success', '2026-10-05T15:04:42Z'),
                     job(111977560660, 'check', 'success', '2026-10-05T14:57:43Z')]
# Actions billing blocked every job (run 37643381384): failure, no steps.
BILLING_JOBS = [job(112867740806, 'check', 'failure', '2026-10-07T15:22:40Z', None)]
BILLING_ANNOTATIONS = [{'annotation_level': 'failure', 'message': 'The job was not started because recent account payments '
                        "have failed or your spending limit needs to be increased. Please check the 'Billing & plans' "
                        'section in your settings'}, NOISE]
# Runner lost mid-step: failure, but no step failed (the killed step stays in_progress). Seen live 2026-10-07 on the
# self-hosted CI runner (its VM removed mid-step): annotation "The self-hosted runner lost communication with the
# server…", the job failed ~9.5 min after the kill.
RUNNER_LOST_JOBS = [job(1, 'test', 'failure', '2026-10-07T12:05:53Z', step('Run pnpm test', 'in_progress', None)),
                    job(2, 'check', 'success', '2026-10-07T11:44:16Z')]
# Synthetic. fail-fast: the failing leg ends first, its sibling is cancelled after it.
FAIL_FAST_JOBS = [job(1, 'test (1)', 'cancelled', '2026-10-07T12:00:09Z', step('Run pnpm test', 'completed', 'cancelled')),
                  job(2, 'test (2)', 'failure', '2026-10-07T12:00:02Z', step('Run pnpm test', 'completed', 'failure'))]
# Synthetic. A `needs:` gate: `test` times out first, the gate fails on its result later.
GATE_JOBS = [job(1, 'test', 'cancelled', '2026-10-07T12:05:53Z', step('Run pnpm test', 'in_progress', None)),
             job(2, 'ci-ok', 'failure', '2026-10-07T12:06:10Z', step('Run test "$RESULT" = success', 'completed', 'failure'))]


def playwright_log(*failing, line=164, passing=3):
    """An e2e job log as Playwright prints it (PR #324, run 38657891700): `passing` green test ids, then each failing
    source line in its code frame from `line` on, a caret under it, once per attempt."""
    passed = ''.join(f'  ✓  {index} [chromium] › e2e/other-{index}.spec.ts:{20 + index}:3 › test {index} (2.1s)\n'
                     for index in range(1, passing + 1))
    frames = ''.join(f'    Error: expect(received).toBe(expected) // Object.is equality\n\n'
                     f'      {number - 1} |       const produced = await read();\n'
                     f'    > {number} |       {source}\n'
                     f'          |       ^\n'
                     f'      {number + 1} |       expect(produced.lineItems).toHaveLength(2);\n'
                     f'        at /home/agent/shop/apps/web/e2e/mixed.spec.ts:{number}:31\n\n'
                     for _ in range(3) for number, source in enumerate(failing, line))

    return f'Running 229 tests using 1 worker\n{passed}\n  1) [chromium] › e2e/mixed.spec.ts:64:3 › funnel\n\n{frames}'


class FakeGitHub:
    """The GitHub API as the App sees it: one installation, rules, PRs, check runs, comments and GraphQL. Records every
    call as (method, path, token, body)."""

    def __init__(self):
        self.calls = []
        self.rules = {'main': REVIEW_RULES, 'staging': [{'type': 'update'}]}
        self.pulls = {}
        self.next_number = 5
        self.check_runs = {}
        self.comments = {}
        self.reviews, self.inline, self.conversation = {}, {}, {}
        self.logs = {}
        self.origin = None
        self.requested = {}
        self.refuse_review_request = False
        self.workflow_runs = {}
        self.jobs = {}
        self.annotations = {}
        self.missing_logs = set()
        self.refuse_rerun = None
        self.actions_permission = 'write'
        self.runs_error = False

    def pull(self, number):
        return self.pulls[int(number)]

    def __call__(self, method, path, token, body=None):
        import re

        self.calls.append((method, path, token, body))
        route = path.split('?')[0]
        match = re.match(r'^/repos/acme/shop/(.*)$', route)
        rest = match.group(1) if match else None

        if route == '/repos/acme/shop/installation':
            return {'id': 7}

        if route == '/users/test-pipeline[bot]':
            return {'id': 99, 'login': 'test-pipeline[bot]', 'type': 'Bot'}

        if route.endswith('/access_tokens'):
            return {'token': 'ghs_test', 'expires_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 3600)),
                    'permissions': {'contents': 'write', 'pull_requests': 'write', 'checks': 'read',
                                    'actions': self.actions_permission, 'metadata': 'read'}}

        if route == '/graphql':
            for pull in self.pulls.values():
                if pull['node_id'] == body['variables'].get('id'):
                    pull['draft'] = False

            return {'data': {'markPullRequestReadyForReview': {'pullRequest': {'isDraft': False}}}}

        if rest and rest.startswith('rules/branches/'):
            rules = self.rules.get(rest.split('/', 2)[2])

            if rules is None:
                raise core.PipelineError(f'GitHub {method} {path}: 404')

            return rules

        if rest == 'pulls' and method == 'GET':
            head = path.split('head=acme:')[1] if 'head=acme:' in path else None

            return [dict(pull) for pull in self.pulls.values() if pull['state'] == 'open'
                    and (head is None or pull['head'] == head.replace('%2F', '/'))]

        if rest == 'pulls' and method == 'POST':
            number = self.next_number
            self.next_number += 1
            self.pulls[number] = {'number': number, 'html_url': f'https://example/pull/{number}',
                                  'node_id': f'PR_{number}', 'state': 'open', 'draft': body.get('draft', False),
                                  'merged': False, 'mergeable': True, 'mergeable_state': 'clean', 'head': body['head'],
                                  'title': body['title'], 'body': body['body']}
            return dict(self.pulls[number])

        match = re.match(r'^pulls/(\d+)$', rest or '')

        if match:
            pull = self.pull(match.group(1))

            if method == 'PATCH':
                pull.update(body)

            # The real shape: head is {ref, sha}, sha the branch's tip on GitHub (the fixture origin).
            tip = subprocess.run(['git', '-C', str(self.origin), 'rev-parse', f"refs/heads/{pull['head']}"],
                                 capture_output=True, text=True).stdout.strip() if self.origin else None

            return dict(pull, head={'ref': pull['head'], 'sha': tip or None})

        match = re.match(r'^pulls/(\d+)/requested_reviewers$', rest or '')

        if match and method == 'GET':
            return {'users': [{'login': login} for login in self.requested.get(int(match.group(1)), [])]}

        if match:
            if self.refuse_review_request:
                # Seen live: the ready flip makes CODEOWNERS request the owner, and a request racing it gets a 422.
                if self.refuse_review_request != 'lost':
                    self.requested.setdefault(int(match.group(1)), []).extend(body['reviewers'])

                raise core.PipelineError(f'GitHub {method} {path}: 422 {{"message":"Validation Failed","errors":'
                                         '["Could not add requested reviewers to pull request."]}')

            return {'number': int(match.group(1))}

        match = re.match(r'^commits/(\w+)/check-runs$', rest or '')

        if match:
            return {'check_runs': self.check_runs.get(match.group(1), [])}

        if rest == 'actions/runs':
            if self.runs_error:
                raise core.PipelineError(f'GitHub {method} {path}: 502 Bad Gateway')

            sha = path.split('head_sha=')[1].split('&')[0]

            return {'workflow_runs': [dict(run) for run in self.workflow_runs.get(sha, [])]}

        match = re.match(r'^actions/runs/(\d+)/attempts/(\d+)/jobs$', rest or '')

        if match:
            jobs = self.jobs.get((int(match.group(1)), int(match.group(2))))

            return {'jobs': jobs} if jobs is not None else {}

        match = re.match(r'^check-runs/(\d+)/annotations$', rest or '')

        if match:
            return self.annotations.get(int(match.group(1)), [])

        match = re.match(r'^actions/runs/(\d+)/rerun-failed-jobs$', rest or '')

        if match and method == 'POST':
            if self.refuse_rerun:
                raise core.PipelineError(f'GitHub {method} {path}: {self.refuse_rerun}')

            for runs in self.workflow_runs.values():
                for run in runs:
                    if run['id'] == int(match.group(1)):
                        run.update(status='queued', conclusion=None, run_attempt=run['run_attempt'] + 1)

            return {}

        match = re.match(r'^issues/(\d+)/comments$', rest or '')

        if match and method == 'POST':
            comment_id = 1000 + len(self.comments)
            self.comments[comment_id] = body['body']
            return {'id': comment_id}

        if match:
            return self.conversation.get(int(match.group(1)), [])

        match = re.match(r'^issues/comments/(\d+)$', rest or '')

        if match:
            self.comments[int(match.group(1))] = body['body']
            return {}

        match = re.match(r'^pulls/(\d+)/(reviews|comments)$', rest or '')

        if match:
            return (self.reviews if match.group(2) == 'reviews' else self.inline).get(int(match.group(1)), [])

        return {}

    def raw(self, path, token):
        self.calls.append(('RAW', path, token, None))

        if path in self.missing_logs:
            raise core.PipelineError(f'GitHub GET {path}: 404')

        return self.logs.get(path, 'setup\nFAIL src/thing.test.ts > thing > works\nError: expected 1 to be 2\n')

    def methods(self):
        return [(method, path.split('?')[0]) for method, path, _, _ in self.calls]


class Fixture:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix='core-test-'))
        self.origin = self.base / 'origin.git'
        self.main = self.base / 'shop'
        self.other = self.base / 'other'
        self.bin = self.base / 'bin'
        self.home = self.base / 'home'
        self.sbx = self.base / 'sbx'
        self.plans = self.main / '.claude/plans'
        self.feature = self.plans / FEATURE
        self.step = self.feature / STEP
        self.clone = self.base / 'clones' / REPO
        git(self.base, 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        git(self.base, 'clone', '-q', str(self.origin), str(self.main))
        self.configure(self.main)
        write(self.main / '.gitignore', '.claude/plans/\n.env\nnode_modules/\n')
        write(self.main / 'package.json', '{"name": "shop", "scripts": {"test": "turbo run test"}}\n')
        write(self.main / 'pnpm-lock.yaml', "lockfileVersion: '9.0'\n")
        write(self.main / '.github/workflows/check.yml', 'name: check\n')
        write(self.main / 'packages/db/drizzle/0000_init.sql', 'CREATE TABLE "t" ("id" integer);\n')
        write(self.main / 'packages/db/drizzle/meta/_journal.json',
              json.dumps({'entries': [{'idx': 0, 'when': 1, 'tag': '0000_init'}]}) + '\n')
        write(self.main / 'src/thing.test.ts', "it('works', () => {})\n")
        write(self.main / '.claude/pipeline.json', json.dumps(PIPELINE_JSON, indent=2) + '\n')
        git(self.main, 'add', '-A')
        git(self.main, 'commit', '-qm', 'init')
        git(self.main, 'push', '-q', 'origin', 'main')
        git(self.base, 'clone', '-q', str(self.origin), str(self.other))
        self.configure(self.other)
        write(self.feature / '00-overview.md', '# Thing\n')
        write(self.step / 'brief.md', '# 01 Thing (brief)\n')
        write(self.bin / 'sbx', FAKE_SBX, 0o755)
        write(self.bin / 'claude', FAKE_CLAUDE, 0o755)
        write(self.bin / 'pnpm', FAKE_PNPM, 0o755)
        write(self.bin / 'curl', FAKE_CURL, 0o755)
        write(self.bin / 'node', FAKE_NODE, 0o755)
        write(self.bin / 'docker', FAKE_DOCKER, 0o755)
        write(self.bin / 'build-template', FAKE_BUILD, 0o755)
        write(self.sbx / 'templates.json', json.dumps([TAG]))
        write(self.home / '.claude/CLAUDE.md', '# rules\n')
        write(self.home / '.claude/skills/plan-harden/SKILL.md', '# skill\n')
        write(self.home / '.claude/agents/web-researcher.md', '# agent\n')
        self.memory = self.base / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(self.main.resolve())) / 'memory'
        write(self.memory / 'MEMORY.md', '- one\n- two\n')
        self.key = self.base / 'app.pem'
        subprocess.run(['openssl', 'genrsa', '-out', str(self.key), '2048'], capture_output=True, check=True)
        self.key.chmod(0o600)
        self.config = self.base / 'config.json'
        write(self.config, json.dumps({
            'app': {'id': 42, 'slug': 'test-pipeline', 'privateKey': str(self.key), 'reviewer': 'reviewer1'},
            'repos': {REPO: str(self.main)},
            'admission': {'slots': 2, 'minMemoryGb': 0, 'weeklyMax': 80, 'fiveHourMax': 70}}))

    @staticmethod
    def configure(repository):
        for key, value in (('user.email', 't@t'), ('user.name', 't'), ('commit.gpgsign', 'false')):
            git(repository, 'config', key, value)

    def cleanup(self):
        subprocess.run(['chmod', '-R', 'u+w', str(self.base)], capture_output=True)
        shutil.rmtree(self.base, ignore_errors=True)


class RunnerCase(unittest.TestCase):
    """The fixture and helpers every runner test shares."""

    def setUp(self):
        self.fixture = fixture = Fixture()
        self.addCleanup(fixture.cleanup)
        self.notifications = []
        self.spawned = []
        self.kicks = []
        self.clicks = []
        self.github = FakeGitHub()
        self.github.origin = fixture.origin
        logs = fixture.base / 'logs'
        patches = [
            mock.patch.multiple(adapter, CONFIG=fixture.config, LOGS=logs, CLONES=fixture.base / 'clones',
                                remote_url=lambda repo: str(fixture.origin)),
            mock.patch.multiple(backend, HOME=fixture.home, HOME_TEMPLATE=str(fixture.sbx / '{name}/home'),
                                WORKSPACE_TEMPLATE=str(fixture.sbx / '{name}/workspace'),
                                SOURCE_MOUNT=str(fixture.clone), BUILD_TEMPLATE=fixture.bin / 'build-template',
                                TEMPLATE_CACHE=fixture.base / 'cache'),
            mock.patch.multiple(core, LOGS=logs, STOP_FLAG=logs / 'STOP', USAGE=logs / 'usage.json',
                                CONTROL=logs / 'control.json', kick_tick=lambda: self.kicks.append(1),
                                TEMPLATE_FAILED=logs / 'template-failed.json',
                                PROJECTS=fixture.base / 'projects', NTFY_TOPIC=fixture.base / 'ntfy-topic',
                                STOP_GRACE_SECONDS=5, MERGEABLE_RETRY_SECONDS=0, github_api=self.github,
                                github_raw=self.github.raw, push_url=lambda repo: str(fixture.origin),
                                notify=lambda state, message, click=None, priority=None: (
                                    self.notifications.append(message), self.clicks.append(click)),
                                spawn_feature_run=lambda repo, feature: self.spawned.append((repo, feature)),
                                stop_units=lambda: None, sbx_service_active=lambda: True),
            mock.patch.multiple(core.sbx_host, active=lambda: True, cgroup=lambda: None, PROC=fixture.base / 'proc',
                                STATE=fixture.base / 'sbx-host', CONFIG=fixture.base / 'sbx-host.json',
                                ci_lane=lambda: False),
            mock.patch.multiple(core, ci_units=lambda: {}, CI_STATE=fixture.base / 'ci-runner'),
        ]

        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        for cache in (core.REPO_CONFIG, core.KIT_COPIED, core.ACTIVE_SANDBOXES, core.TOKEN_CACHE, core.BOT_IDENTITY):
            cache.clear()

        core.release_slot()
        self.addCleanup(core.release_slot)
        environment = {
            'PATH': f"{fixture.bin}{os.pathsep}{os.environ['PATH']}",
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'FAKE_SBX_ROOT': str(fixture.sbx),
            'FAKE_CLAUDE_CALLS': str(fixture.base / 'claude-calls.jsonl'),
            'FAKE_CLAUDE_SCRIPT': str(fixture.base / 'claude-script.json'),
            'FAKE_PNPM_CALLS': str(fixture.base / 'pnpm-calls.jsonl'),
            'FAKE_PNPM_CONTROL': str(fixture.base / 'pnpm-control.json'),
            'FAKE_SPAWN_PID': str(fixture.base / 'spawned.pid'),
        }
        environment_patch = mock.patch.dict(os.environ, environment)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.usage(0.2)
        self.script({})
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        subprocess.run([str(self.fixture.bin / 'sbx'), 'rm', '--force', SANDBOX], env=dict(
            os.environ, FAKE_SBX_ROOT=str(self.fixture.sbx)), capture_output=True)

    # ------------------------------------------------------------------------------------------ helpers

    def usage(self, weekly, five_hour=0.1, age=0, resets_in=86400):
        write(self.fixture.base / 'logs/usage.json', json.dumps({
            'at': time.time() - age,
            'five_hour': {'utilization': five_hour, 'resetsAt': time.time() + 3600},
            'seven_day': {'utilization': weekly, 'resetsAt': time.time() + resets_in}}))

    def vm_step(self):
        return self.fixture.sbx / SANDBOX / 'home/plans' / FEATURE / STEP

    def workspace(self):
        return self.fixture.sbx / SANDBOX / 'workspace'

    def script(self, behaviors):
        def render(command):
            return (command.replace('{step}', str(self.vm_step()))
                    .replace('{feature}', str(self.vm_step().parent)).replace('{other}', str(self.fixture.other)))

        rendered = {name: [dict(behavior, run=[render(command) for command in behavior.get('run', [])])
                           for behavior in steps] for name, steps in behaviors.items()}
        write(self.fixture.base / 'claude-script.json', json.dumps(rendered))

    def pnpm(self, control):
        write(self.fixture.base / 'pnpm-control.json', json.dumps(control))

    def claude_calls(self, name=None):
        path = self.fixture.base / 'claude-calls.jsonl'
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        return [call for call in calls if name is None or call['name'] == name]

    def pnpm_calls(self):
        path = self.fixture.base / 'pnpm-calls.jsonl'

        return [' '.join(json.loads(line)['argv']) for line in path.read_text().splitlines()] if path.exists() else []

    def sbx_calls(self):
        path = self.fixture.sbx / 'calls.jsonl'

        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def templates(self, *tags):
        path = self.fixture.sbx / 'templates.json'

        if tags:
            write(path, json.dumps(list(tags)))

        return json.loads(path.read_text())

    def builds(self):
        path = self.fixture.sbx / 'builds.jsonl'

        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def docker_calls(self):
        path = self.fixture.sbx / 'docker.jsonl'

        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def created_from(self, name=SANDBOX):
        return [call[call.index('-t') + 1] for call in self.sbx_calls() if call[0] == 'create' and name in call]

    def configure(self, **template):
        config = json.loads(self.fixture.config.read_text())
        config['template'] = template
        write(self.fixture.config, json.dumps(config))

    def sandbox_status(self, name=SANDBOX):
        path = self.fixture.sbx / 'meta.json'
        meta = json.loads(path.read_text()) if path.exists() else {}

        return (meta.get(name) or {}).get('status')

    def state(self, step_id='01'):
        return core.load_state(REPO, FEATURE, step_id)

    def save(self, state):
        core.save_state(state)

    def main(self, *argv):
        with redirect_stdout(io.StringIO()) as output, mock.patch('sys.stderr', io.StringIO()) as errors:
            code = core.main(list(argv))

        self.output = output.getvalue()
        self.errors = errors.getvalue()

        return code

    def happy_path(self, **behaviors):
        self.script(dict({'thing-01-plan': [PLAN], 'thing-01-implement': [IMPLEMENT], 'thing-01-review-1': [REVIEW],
                          'thing-01-respond-1': [RESPOND]}, **behaviors))

    def ref(self):
        return git(self.fixture.clone, 'rev-parse', REF)

    def origin_branch(self):
        return git(self.fixture.origin, 'branch', '--list', BRANCH)

    def assertOnTopOfRemoteMain(self):
        remote_main = git(self.fixture.other, 'rev-parse', 'HEAD')
        ancestor = subprocess.run(['git', '-C', str(self.fixture.clone), 'merge-base', '--is-ancestor', remote_main, REF])
        self.assertEqual(ancestor.returncode, 0, 'the branch is not on top of the remote main')

    def ci(self, sha, *conclusions):
        """Required check runs on a commit: `check` and `test` with these conclusions (None = still running), the jobs
        of one workflow run. A red job's step ends the same way, so `failure` is a code failure."""
        offset = len(self.github.check_runs) * 10
        jobs = [{'id': 100 + index + offset, 'name': name, 'status': 'completed' if conclusion else 'in_progress',
                 'conclusion': conclusion, 'completed_at': f'2026-10-07T12:00:0{index}Z' if conclusion else None,
                 'steps': [step('Set up job'), step('Run pnpm test', *(('completed', conclusion) if conclusion
                                                                      else ('in_progress', None)))]}
                for index, (name, conclusion) in enumerate(zip(('check', 'test'), conclusions))]
        self.actions(sha, {'id': 900 + offset, 'workflow_id': 1, 'status': 'completed' if all(conclusions) else
                           'in_progress', 'run_attempt': 1, 'check_suite_id': 9000 + offset}, jobs)

        for run in self.github.check_runs[sha]:
            run['output'] = {'title': f"{run['name']} output"}

    def actions(self, sha, run, jobs, annotations=None, keep=False):
        """A workflow run on a commit as GitHub reports it: the run, its attempt's jobs, and their check runs (a job is
        its check run, same id). keep=True adds to the commit's runs and check runs (a rerun, another workflow)."""
        self.github.workflow_runs[sha] = [other for other in self.github.workflow_runs.get(sha, [])
                                          if keep and other['id'] != run['id']] + [dict(run)]
        self.github.jobs[(run['id'], run['run_attempt'])] = jobs
        self.github.annotations.update(annotations or {})
        self.github.check_runs[sha] = (self.github.check_runs.get(sha, []) if keep else []) + [
            {'id': job['id'], 'name': job['name'], 'status': job['status'], 'conclusion': job['conclusion'],
             'app': {'slug': 'github-actions'}, 'check_suite': {'id': run['check_suite_id']},
             'output': {'title': None, 'summary': None}} for job in jobs]

    def move_main(self):
        git(self.fixture.other, 'pull', '-q')
        write(self.fixture.other / 'fresh.txt', 'fresh\n')
        git(self.fixture.other, 'add', '-A')
        git(self.fixture.other, 'commit', '-qm', 'chore: main moved')
        git(self.fixture.other, 'push', '-q', 'origin', 'main')

    def published(self, **behaviors):
        """Runs the happy path to a published draft PR."""
        self.happy_path(**behaviors)
        self.assertEqual(self.main(FEATURE), 0)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])

        return state


class RunnerTest(RunnerCase):
    """The runner end to end on the fake sandbox."""

    # ------------------------------------------------------------------------------------------ happy path

    def vm_argv(self):
        create = next(call for call in self.sbx_calls() if call[0] == 'create')

        return create[create.index('--cpus') + 1], create[create.index('-m') + 1]

    def test_stage_vms_get_6_cpus_and_8_gb_by_default(self):
        self.happy_path()
        self.assertEqual(self.main(FEATURE, '--until', 'plan'), 0)
        self.assertEqual(self.vm_argv(), ('6', '8g'))

    def test_stage_vms_follow_the_vm_config_key(self):
        config = json.loads(self.fixture.config.read_text())
        config['vm'] = {'cpus': 4, 'memoryGb': 12}
        write(self.fixture.config, json.dumps(config))
        self.happy_path()
        self.assertEqual(self.main(FEATURE, '--until', 'plan'), 0)
        self.assertEqual(self.vm_argv(), ('4', '12g'))

    def test_a_run_writes_nothing_under_the_code_dir(self):
        def snapshot():
            return {path: path.stat().st_mtime_ns for path in core.PIPELINE_DIR.rglob('*') if path.is_file()
                    and not {'.git', '__pycache__'} & set(path.relative_to(core.PIPELINE_DIR).parts)}

        before = snapshot()
        self.published()
        self.assertEqual(snapshot(), before)

    def test_a_plan_without_questions_runs_unattended_to_a_draft_pr(self):
        state = self.published()
        self.assertEqual(state['branch'], BRANCH)
        calls = self.claude_calls()
        self.assertEqual([call['name'] for call in calls],
                         ['thing-01-plan', 'thing-01-implement', 'thing-01-review-1', 'thing-01-respond-1'])
        plan = self.vm_step() / 'plan.md'
        self.assertEqual(calls[0]['prompt'], f"/plan-harden {self.vm_step() / 'brief.md'}")
        self.assertEqual(calls[1]['prompt'], f'/plan-implement {plan}')
        self.assertEqual(calls[2]['prompt'], f'/review-branch {plan} 1')
        self.assertEqual(calls[3]['prompt'], f'/review-respond {plan} 1')

        for call in calls:
            self.assertEqual(Path(call['cwd']), self.workspace(), call['name'])
            self.assertEqual(call['env']['FAKE_SBX_NAME'], SANDBOX, 'every stage runs inside the step sandbox')

        implement = calls[1]
        argv = ' '.join(implement['argv'])

        for expected in ('--permission-mode auto', '--permission-prompts none', '--session-id', '--json-schema',
                         '--output-format stream-json', '--model claude-opus-5-5 --effort medium',
                         '--fallback-model claude-sonnet-5-5', '--max-budget-usd 60',
                         f'--add-dir {self.vm_step().parent}', 'throwaway sandbox VM', '## This stage',
                         f'Step dir: {self.vm_step()}', 'check: `pnpm check`'):
            self.assertIn(expected, argv)

        for call, expected in zip(calls, ('high', 'medium', 'high', 'medium')):
            self.assertIn(f'--model claude-opus-5-5 --effort {expected}', ' '.join(call['argv']), call['name'])

        self.assertEqual({key: implement['env'][key] for key in ('ANTHROPIC_DEFAULT_OPUS_MODEL',
                                                                  'CLAUDE_CODE_SUBAGENT_MODEL_FORCE',
                                                                  'HEAVY_MIN_FREE_GB', 'GIT_EDITOR',
                                                                  'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS')},
                         {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-5-5', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': '1',
                          'HEAVY_MIN_FREE_GB': '0', 'GIT_EDITOR': 'true', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'})
        pnpm = self.pnpm_calls()
        self.assertEqual(pnpm.count('install --frozen-lockfile'), 1, 'setup runs once per sandbox')
        self.assertIn('check', pnpm)
        self.assertIn('test', pnpm)
        notice = (self.fixture.step / 'done.md').read_text()

        for expected in ('kept the old label', 'after merge: pnpm db:migrate on shop', 'a trap worth remembering',
                         'MEMORY.md: 2 lines'):
            self.assertIn(expected, notice)

        sha = git(self.fixture.origin, 'rev-parse', BRANCH)
        self.assertEqual(sha, self.ref())
        self.assertEqual(state['published_sha'], sha)
        self.assertEqual((self.fixture.step / 'plan.md').read_text(), '# Plan\n\nDigest.\n', 'plan.md copied out')
        pull = self.github.pull(5)
        self.assertEqual((pull['title'], pull['body'], pull['draft']), ('fix: Do the thing', 'Why it matters.', True))
        self.assertIn(core.EVIDENCE_MARKER, next(iter(self.github.comments.values())))
        self.assertEqual(self.sandbox_status(), 'stopped', 'the VM stops when the run ends')
        calls_before = len(self.github.calls)
        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(len(self.claude_calls()), 4)
        self.assertNotIn(('POST', '/repos/acme/shop/pulls'), self.github.methods()[calls_before:],
                         'a done step must not publish again')

    def test_the_kit_memory_and_stage_settings_are_in_the_sandbox(self):
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        home = self.fixture.sbx / SANDBOX / 'home'
        self.assertEqual((home / '.claude/CLAUDE.md').read_text(), '# rules\n')
        self.assertTrue((home / '.claude/skills/plan-harden/SKILL.md').exists())
        self.assertTrue((home / '.claude/agents/web-researcher.md').exists())
        self.assertEqual((home / 'memory/MEMORY.md').read_text(), '- one\n- two\n')

        for script in backend.KIT_SCRIPTS:
            self.assertTrue((home / '.claude/pipeline' / script).exists(), script)

        settings = json.loads((home / '.claude/settings.json').read_text())
        self.assertEqual(settings, {'availableModels': ['opus', 'sonnet', 'haiku'], 'autoMemoryDirectory': str(home / 'memory'),
                                    'attribution': {'commit': '', 'pr': '', 'sessionUrl': False}})
        self.assertFalse((home / f'plans/{FEATURE}/.pipeline').exists(), 'runner state never enters the VM')
        self.assertTrue((home / f'plans/{FEATURE}/00-overview.md').exists())
        denied = [call for call in self.sbx_calls() if call[:2] == ['policy', 'deny']]
        self.assertEqual(denied, [['policy', 'deny', 'network', '--sandbox', SANDBOX, 'statsig.example.org']])
        setup_rules = [call[:2] for call in self.sbx_calls() if call[:1] == ['policy'] and 'registry.npmjs.org' in call]
        self.assertEqual(setup_rules, [['policy', 'allow'], ['policy', 'rm']], 'setup egress is removed after setup')

    def test_a_plan_with_questions_waits_for_the_answer(self):
        self.happy_path(**{'thing-01-plan': [PLAN_ASKS]})
        self.assertEqual(self.main(FEATURE), 0)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('plan', 'awaiting-approval'))
        self.assertIn('Q1? Rec: A', state['reason'])
        self.assertIn(f'plan asks 1:\n- Q1? Rec: A\n→ pipeline answer {FEATURE}', self.notifications)
        self.assertEqual(self.sandbox_status(), 'stopped')
        self.assertEqual(self.main(FEATURE), 0)
        self.assertIn('awaiting your answer', self.output)
        self.assertEqual(self.main('approve', FEATURE), 0)
        self.assertTrue(core.approved_plan(self.state()).exists())
        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-plan')), 1)
        self.assertFalse(self.state()['approved'])

    PLAN_WITH_QUESTIONS = {'run': ["printf '# Plan\\n\\n## Unresolved questions\\n\\n1. Q1? Rec: A\\n\\n## Steps\\n\\nx\\n' "
                                   "> {step}/plan.md"],
                           'result': {'questions': ['Q1? Rec: A'], 'owed': ['after merge: tell support']}}

    def answered(self, edit=None, **behaviors):
        """Runs the plan to its question, lets `edit` change plan.md, approves and runs the step to its draft PR; the
        PR comment."""
        self.happy_path(**dict({'thing-01-plan': [self.PLAN_WITH_QUESTIONS]}, **behaviors))
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'awaiting-approval')
        plan = self.fixture.step / 'plan.md'

        if edit:
            plan.write_text(edit(plan.read_text()))

        self.assertEqual(self.main('approve', FEATURE), 0)
        self.assertEqual(self.main(FEATURE), 0)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])

        return next(iter(self.github.comments.values()))

    def test_an_answered_plan_question_is_a_decision_with_your_answer(self):
        comment = self.answered(lambda text: text.replace('Rec: A\n', 'Rec: A\nA: B\n'))
        notice = (self.fixture.step / 'done.md').read_text()

        for text in (comment, notice):
            self.assertIn('## Decisions\n\n- Owner answered: Q1? Rec: A — A: B', text)
            self.assertNotIn('## Questions', text)
            self.assertIn('## Owed\n\n- after merge: tell support', text, 'a real owed item stays')

        state = self.state()
        self.assertEqual(state['results']['plan']['questions'], [])
        self.assertFalse(state['approved'])

    def test_an_edited_question_line_counts_as_the_answer(self):
        comment = self.answered(lambda text: text.replace('1. Q1? Rec: A', '1. Q1? Go with B instead'))
        self.assertIn('- Owner answered: Q1? Rec: A — 1. Q1? Go with B instead', comment)

    def test_approve_without_an_answer_keeps_the_recommendation(self):
        comment = self.answered()
        self.assertIn('- Owner answered: Q1? Rec: A — the recommendation stands', comment)
        self.assertNotIn('## Questions', comment)

    def test_an_approval_without_a_snapshot_says_so(self):
        comment = self.answered(lambda text: (core.asked_plan(self.state()).unlink(), text + 'A: B\n')[1])
        self.assertIn('- Owner answered: Q1? Rec: A — approved, answer not recorded', comment)
        self.assertNotIn('## Questions', comment)

    def test_open_questions_from_later_stages_stay_open(self):
        comment = self.answered(**{'thing-01-implement': [dict(IMPLEMENT, result={'questions': ['Q2? Rec: B']})]})
        self.assertIn('## Questions\n\n- Q2? Rec: B\n', comment)
        self.assertNotIn('- Q1? Rec: A\n', comment)

    def test_the_system_prompt_files_a_question_once(self):
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        argv = self.claude_calls('thing-01-plan')[0]['argv']
        prompt = argv[argv.index('--append-system-prompt') + 1]
        self.assertNotIn('an owner answer', prompt)
        self.assertIn('never also in `owed`', prompt)

    def test_step_and_until_stop_where_asked(self):
        self.happy_path()
        self.assertEqual(self.main(FEATURE, '--step'), 0)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('implement', 'ready'))
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        self.assertEqual(self.state()['stage'], 'rebase')
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        self.assertIn('already past implement', self.output)
        self.assertEqual(self.main(FEATURE, '--until', 'review'), 0)
        self.assertEqual(self.state()['stage'], 'respond')
        self.assertEqual([call['name'] for call in self.claude_calls()],
                         ['thing-01-plan', 'thing-01-implement', 'thing-01-review-1'])

    # ------------------------------------------------------------------------------------------ gates

    def test_red_gate_resumes_the_same_session_with_the_failure(self):
        self.happy_path(**{'thing-01-implement': [IMPLEMENT, {}]})
        self.pnpm({'check': [{'code': 1, 'output': 'lint: no-unused-vars in thing.ts'}]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        first, fix = self.claude_calls('thing-01-implement')
        self.assertFalse(first['resume'])
        self.assertTrue(fix['resume'])
        self.assertEqual(fix['session'], first['session'])
        self.assertIn("runner's gates failed", fix['prompt'])
        self.assertIn('no-unused-vars', fix['prompt'])
        self.assertEqual(self.state()['stage'], 'rebase')
        log = (core.state_dir(REPO, FEATURE) / '01-gates.log').read_text()
        self.assertRegex(log, r'== \S+ check `pnpm check` → exit 1')
        self.assertIn('no-unused-vars', log)

    def test_gates_red_after_two_fixes_pause(self):
        self.happy_path(**{'thing-01-implement': [IMPLEMENT, {}, {}]})
        self.pnpm({'check': [{'code': 1, 'output': 'broken'}] * 3})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('implement', 'paused'))
        self.assertIn('after 2 fix attempts', state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-implement')), 3)
        self.assertIn('paused: implement gates red', self.notifications)

    def test_a_flaky_check_reruns_whole_and_is_noted(self):
        self.happy_path()
        self.pnpm({'test': [{'code': 1, 'output': 'timeout in thing.test.ts'}]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        self.assertEqual(self.pnpm_calls().count('test'), 2)
        self.assertIn('test failed, then passed on rerun 1 (flake?)', self.state()['notes'])
        self.assertEqual(len(self.claude_calls('thing-01-implement')), 1)

    def test_a_check_red_on_every_rerun_fails(self):
        self.happy_path(**{'thing-01-implement': [IMPLEMENT, {}]})
        self.pnpm({'test': [{'code': 1, 'output': 'real failure'}] * 2})
        self.main(FEATURE, '--until', 'implement')
        self.assertIn('`pnpm test` (test) failed (also on 1 rerun(s))', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_trailer_protected_files_and_merged_migration_edits_fail(self):
        bad = {'run': ['echo x >> packages/db/drizzle/0000_init.sql && echo y >> pnpm-lock.yaml && '
                       'python3 -c "import json; p=\'package.json\'; d=json.load(open(p)); '
                       'd[\'dependencies\']={\'left-pad\': \'1\'}; json.dump(d, open(p, \'w\'))" && git add -A && '
                       "git commit -qm 'fix: x' -m 'Co-Authored-By: Claude <noreply@anthropic.com>'"]}
        self.happy_path(**{'thing-01-implement': [bad, {}, {}]})
        self.main(FEATURE)
        prompt = self.claude_calls('thing-01-implement')[1]['prompt']

        for expected in ('attribution trailer', 'pnpm-lock.yaml changed', 'package.json: dependencies changed',
                         '0000_init.sql (M) exists on origin/main'):
            self.assertIn(expected, prompt)

    def test_a_commit_message_with_personal_text_fails(self):
        for message in ('see /home/someone/notes.txt', 'asked someone@example.com', 'noted in MEMORY.md',
                        'per ~/.claude/rules'):
            with self.subTest(message=message):
                self.assertTrue(core.PERSONAL_TEXT.search(message))

        for message in ('fix: Bump react@18.2.0', 'Co-authored-by: a <1+a@users.noreply.github.com>',
                        'docs: .claude/plans layout'):
            with self.subTest(message=message):
                self.assertFalse(core.PERSONAL_TEXT.search(message))

        bad = {'run': ["echo x > thing.txt && git add -A && git commit -qm 'fix: x' -m 'Seen in /home/someone/scratch.'"]}
        self.happy_path(**{'thing-01-implement': [bad, {}, {}]})
        self.main(FEATURE)
        self.assertIn('personal text', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_public_text_shortens_home_paths_and_hides_personal_emails(self):
        self.assertEqual(core.public_text('in /home/someone/a/b and /home/agent, ask x.y+z@example.org or '
                                          '1+b[bot]@users.noreply.github.com; react@18.2.0 @types/node'),
                         'in ~/a/b and ~, ask [email] or 1+b[bot]@users.noreply.github.com; react@18.2.0 @types/node')

    def test_a_renamed_workflow_counts_as_changed(self):
        moved = {'run': ["git mv .github/workflows/check.yml check.yml && git commit -qm 'chore: Move check'"]}
        self.happy_path(**{'thing-01-implement': [moved, {}, {}]})
        self.main(FEATURE)
        self.assertIn('.github/workflows/check.yml changed', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_gate_config_and_scripts_changes_are_flagged(self):
        scripts = {'run': ['python3 -c "import json; p=\'package.json\'; d=json.load(open(p)); '
                           'd[\'scripts\'][\'test\']=\'true\'; json.dump(d, open(p, \'w\'))" && echo {} > turbo.json && '
                           "git add -A && git commit -qm 'chore: Scripts'"]}
        self.happy_path(**{'thing-01-implement': [scripts]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        notes = ' '.join(self.state()['notes'])
        self.assertIn('⚑ gate-defining config changed', notes)
        self.assertIn('package.json scripts test', notes)
        self.assertIn('turbo.json', notes)

    def test_a_detached_head_fails_the_branch_gate(self):
        detach = {'run': IMPLEMENT['run'] + ['git checkout -q --detach']}
        back = {'run': [f'git checkout -q {BRANCH}']}
        self.happy_path(**{'thing-01-implement': [detach, back]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        self.assertIn(f'HEAD must be on {BRANCH} (it is on a detached HEAD)',
                      self.claude_calls('thing-01-implement')[1]['prompt'])
        self.assertEqual(self.state()['stage'], 'rebase')

    def test_uncommitted_work_fails_the_clean_gate(self):
        dirty = {'run': IMPLEMENT['run'] + ['echo probe > probe.txt']}
        self.happy_path(**{'thing-01-implement': [dirty, {'run': ['rm probe.txt']}]})
        self.main(FEATURE, '--until', 'implement')
        self.assertIn('?? probe.txt', self.claude_calls('thing-01-implement')[1]['prompt'])
        self.assertEqual(self.state()['stage'], 'rebase')

    def test_a_new_migration_is_flagged_for_data_loss(self):
        journal = ('python3 -c "import json; p=\'packages/db/drizzle/meta/_journal.json\'; d=json.load(open(p)); '
                   'd[\'entries\'].append({\'idx\': 1, \'when\': 2, \'tag\': \'0001_x\'}); json.dump(d, open(p, \'w\'))"')
        migration = {'run': ["printf 'ALTER TABLE \"t\" DROP COLUMN \"c\";\\n' > packages/db/drizzle/0001_x.sql && "
                             f"{journal} && git add -A && git commit -qm 'feat: Drop c'"]}
        self.happy_path(**{'thing-01-implement': [migration]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        self.assertEqual(self.state()['stage'], 'rebase', self.state()['reason'])
        self.assertIn('⚑ data loss in packages/db/drizzle/0001_x.sql: ALTER TABLE "t" DROP COLUMN "c";',
                      self.state()['notes'])

    def test_a_rewritten_journal_entry_fails(self):
        rewrite = ('python3 -c "import json; p=\'packages/db/drizzle/meta/_journal.json\'; d=json.load(open(p)); '
                   'd[\'entries\'][0][\'when\']=9; json.dump(d, open(p, \'w\'))" && git add -A && git commit -qm \'fix: j\'')
        self.happy_path(**{'thing-01-implement': [{'run': [rewrite]}, {}, {}]})
        self.main(FEATURE)
        self.assertIn('entries that exist on origin/main changed', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_a_stale_review_file_does_not_pass(self):
        write(self.fixture.step / 'review-1.md', 'old review\n')
        os.utime(self.fixture.step / 'review-1.md', (time.time() - 3600,) * 2)
        self.happy_path(**{'thing-01-review-1': [{}, REVIEW]})
        self.main(FEATURE)
        second = self.claude_calls('thing-01-review-1')[1]
        self.assertIn('was not updated by this stage', second['prompt'])
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        self.assertEqual((self.fixture.step / 'review-1.md').read_text(), 'findings\n')

    def test_only_the_stages_own_files_are_copied_out(self):
        greedy = {'run': PLAN['run'] + ['echo planted > {step}/pr.md', 'echo planted > {feature}/00-overview.md',
                                        'echo planted > {step}/review-1.md']}
        self.happy_path(**{'thing-01-plan': [greedy]})
        self.main(FEATURE, '--until', 'plan')
        self.assertTrue((self.fixture.step / 'plan.md').exists())
        self.assertFalse((self.fixture.step / 'pr.md').exists())
        self.assertFalse((self.fixture.step / 'review-1.md').exists())
        self.assertEqual((self.fixture.feature / '00-overview.md').read_text(), '# Thing\n')

    def test_leftover_detached_processes_end_with_the_vm(self):
        self.happy_path()
        self.pnpm({'check': [{'spawn': True}]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        pid = int((self.fixture.base / 'spawned.pid').read_text())
        deadline = time.monotonic() + 5

        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)

        self.assertFalse(alive(pid), 'the detached sleeper outlived its gate')

    def test_a_branch_moved_after_the_gates_is_refused(self):
        self.happy_path()
        quiesce = backend.quiesce
        raced = []

        def racing_quiesce(name):
            # a stray VM process commits after the checks ran, before the VM stops
            if 'test' in self.pnpm_calls() and not raced:
                raced.append(1)
                git(self.workspace(), 'commit', '-q', '--allow-empty', '-m', 'chore: stray')

            quiesce(name)

        with mock.patch.object(backend, 'quiesce', racing_quiesce):
            self.main(FEATURE, '--until', 'implement')

        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertIn('moved after the gates', state['reason'])
        self.assertIsNone(state['green_head'])

    # ------------------------------------------------------------------------------------------ tamper gate

    def test_a_new_only_always_fails(self):
        only = {'run': ["echo \"it.only('x', () => {})\" >> src/thing.test.ts && git add -A && git commit -qm 'test: x'"]}
        self.happy_path(**{'thing-01-implement': [only, {}, {}]})
        self.main(FEATURE)
        self.assertIn('`.only` added', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_a_skip_or_deleted_test_fails_unless_the_approved_plan_names_it(self):
        skip = {'run': ["echo \"it.skip('flaky', () => {})\" >> src/thing.test.ts && git add -A && "
                        "git commit -qm 'test: Skip flaky'"]}
        self.happy_path(**{'thing-01-implement': [skip, {}, {}]})
        self.main(FEATURE)
        self.assertIn('skipped/todo/fixme test was added', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_the_approved_plans_test_changes_section_allows_the_exact_path_and_flags_it(self):
        plan = {'run': ["printf '# Plan\\n\\n## Test changes\\n- skip `src/thing.test.ts` (flaky upstream)\\n' "
                        '> {step}/plan.md']}
        skip = {'run': ["echo \"it.skip('flaky', () => {})\" >> src/thing.test.ts && git rm -q --cached src/thing.test.ts"
                        " && git commit -qm 'test: Drop flaky' && git add -A && git commit -qm 'test: back'"]}
        self.happy_path(**{'thing-01-plan': [plan], 'thing-01-implement': [skip]})
        self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)
        self.assertEqual(self.state()['stage'], 'rebase', self.state()['reason'])
        self.assertTrue(any(note.startswith('⚑ test skipped (plan allows it): src/thing.test.ts')
                            for note in self.state()['notes']), self.state()['notes'])

    def test_the_agent_editing_the_plan_later_cannot_allow_a_skip(self):
        skip = {'run': ["printf '# Plan\\n\\n## Test changes\\n- src/thing.test.ts\\n' > {step}/plan.md && "
                        "echo \"it.skip('x', () => {})\" >> src/thing.test.ts && git add -A && git commit -qm 'test: x'"]}
        self.happy_path(**{'thing-01-implement': [skip, {}, {}]})
        self.main(FEATURE)
        self.assertIn('skipped/todo/fixme test was added', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_a_deleted_test_file_fails(self):
        delete = {'run': ["git rm -q src/thing.test.ts && git commit -qm 'test: Remove'"]}
        self.happy_path(**{'thing-01-implement': [delete, {}, {}]})
        self.main(FEATURE)
        self.assertIn('src/thing.test.ts: a test file was deleted', self.claude_calls('thing-01-implement')[1]['prompt'])

    def test_a_pattern_in_the_plans_test_changes_sends_the_plan_back(self):
        glob = {'run': ["printf '# Plan\\n\\n## Test changes\\n- moved: `src/old/**/*.test.ts`\\n' > {step}/plan.md"]}
        exact = {'run': ["printf '# Plan\\n\\n## Test changes\\n- moved: `src/old/a.test.ts` to `src/new/a.test.ts`\\n' "
                         '> {step}/plan.md']}
        self.happy_path(**{'thing-01-plan': [glob, exact]})
        self.main(FEATURE, '--until', 'plan')
        second = self.claude_calls('thing-01-plan')[1]
        self.assertTrue(second['resume'])
        self.assertIn('names a pattern (src/old/**/*.test.ts)', second['prompt'])
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])

    # ------------------------------------------------------------------------------------------ pauses and resumes

    def test_blocked_pauses_and_the_rerun_resumes_with_the_answers(self):
        self.happy_path(**{'thing-01-plan': [BLOCKED, PLAN]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertIn('Q1? Rec: A', state['reason'])
        self.assertIn('paused: plan blocked: needs a product call\n- Q1? Rec: A', self.notifications)
        self.main(FEATURE, '--until', 'plan')
        second = self.claude_calls('thing-01-plan')[1]
        self.assertTrue(second['resume'])
        self.assertIn('Unresolved questions', second['prompt'])
        self.assertIn(f'in {self.vm_step()}/', second['prompt'], 'the VM copy, not the host path')
        self.assertEqual(self.state()['stage'], 'implement')

    def test_a_blocked_plan_rerun_after_main_moved_plans_on_the_new_main(self):
        self.happy_path(**{'thing-01-plan': [BLOCKED, PLAN]})
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused')
        self.move_main()
        self.main(FEATURE, '--until', 'plan')
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])
        self.assertTrue((self.workspace() / 'fresh.txt').exists(), 'the plan ran on the old main')
        self.assertOnTopOfRemoteMain()
        second = self.claude_calls('thing-01-plan')[1]
        self.assertTrue(second['resume'])
        self.assertIn('Unresolved questions', second['prompt'])
        self.assertIn('origin/main moved', second['prompt'])
        self.assertEqual(self.pnpm_calls().count('install --frozen-lockfile'), 2, 'setup runs again on the new main')

    def test_a_deleted_transcript_starts_fresh_with_the_pending_prompt_and_context(self):
        self.happy_path(**{'thing-01-plan': [BLOCKED, PLAN]})
        self.main(FEATURE)
        shutil.rmtree(self.fixture.sbx / SANDBOX / 'home/.claude/projects')
        self.main(FEATURE, '--until', 'plan')
        second = self.claude_calls('thing-01-plan')[1]
        self.assertFalse(second['resume'])
        self.assertTrue(second['prompt'].startswith('/plan-harden '))
        self.assertIn('Your earlier session of this stage is gone', second['prompt'])
        self.assertIn('Unresolved questions', second['prompt'])

    def test_a_lost_sandbox_is_recreated_from_the_host_ref(self):
        crash = {'run': IMPLEMENT['run'], 'no_result': True}
        self.happy_path(**{'thing-01-implement': [crash, {'no_result': True}, {}]})
        self.main(FEATURE, '--until', 'implement')
        self.assertIn('implement crash', self.state()['reason'])
        tip = self.ref()
        self.kill_leftovers()
        self.main(FEATURE, '--until', 'implement')
        self.assertEqual(git(self.workspace(), 'rev-parse', BRANCH), tip, 'the branch comes back from the host ref')
        self.assertIn(f'sandbox {SANDBOX} was gone: recreated from {REF}', self.state()['notes'])
        fresh = self.claude_calls('thing-01-implement')[-1]
        self.assertFalse(fresh['resume'])
        self.assertTrue(fresh['prompt'].startswith('/plan-implement '))
        self.assertIn('fix: Do the thing', fresh['prompt'], 'a fresh session gets the branch commits as context')
        self.assertIn(core.RESUME_INTERRUPTED, fresh['prompt'])
        self.assertEqual(self.state()['stage'], 'rebase', self.state()['reason'])

    def test_a_vm_left_running_by_a_dead_run_is_stopped_before_the_stage_resumes(self):
        crash = {'run': IMPLEMENT['run'], 'no_result': True}
        self.happy_path(**{'thing-01-implement': [crash, {'no_result': True}, {}]})
        self.main(FEATURE, '--until', 'implement')
        self.assertIn('implement crash', self.state()['reason'])
        orphan = subprocess.Popen([str(self.fixture.bin / 'sbx'), 'exec', SANDBOX, '--', 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(orphan.kill)
        time.sleep(0.5)
        self.assertEqual(self.sandbox_status(), 'running')
        before = len(self.sbx_calls())
        self.main(FEATURE, '--until', 'implement')
        orphan.wait(timeout=10)  # the dead run's process is gone
        calls = self.sbx_calls()[before:]
        claude = next(index for index, call in enumerate(calls) if call[0] == 'exec' and 'claude' in call)
        self.assertIn(['stop', SANDBOX], calls[:claude], 'stopped before the stage resumes')
        self.assertTrue(self.claude_calls('thing-01-implement')[-1]['resume'], 'the same session resumes')
        self.assertIn(f'sandbox {SANDBOX} was left running by an earlier run: stopped it first', self.state()['notes'])

    def test_a_sandbox_remote_lost_to_a_vm_stop_is_healed_by_a_restart(self):
        real = backend.fetch_branch
        dropped = []

        def drop_once(name, source, *rest):
            if not dropped:
                # sbx's own 30 s auto-stop (or another run's stop) lands between ensure_running and the remote read.
                original = backend.ensure_running

                def stop_after(sandbox):
                    original(sandbox)
                    subprocess.run(['git', '-C', str(source), 'remote', 'remove', f'sandbox-{sandbox}'], check=True)
                    backend.ensure_running = original

                backend.ensure_running = stop_after
                self.addCleanup(setattr, backend, 'ensure_running', original)
                dropped.append(name)

            return real(name, source, *rest)

        with mock.patch.object(backend, 'fetch_branch', drop_once):
            self.published()

        self.assertEqual(dropped, [SANDBOX])

    def test_the_vm_fetches_from_the_host_clone_not_github(self):
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        subprocess.run([str(self.fixture.bin / 'sbx'), 'exec', SANDBOX, '--', 'true'], check=True, capture_output=True)
        self.assertEqual(git(self.workspace(), 'remote', 'get-url', 'origin'), str(self.fixture.clone),
                         'still after a VM start, which resets origin')
        self.assertIn('refs/heads/main', git(self.workspace(), 'ls-remote', 'origin', 'refs/heads/main'))

    def test_a_failed_base_fetch_retries_then_pauses_and_never_branches_from_a_stale_base(self):
        self.happy_path()
        failed = subprocess.CompletedProcess([], 128, '', 'fatal: unable to access origin')

        with mock.patch.object(backend, 'refresh_base', return_value=failed):
            self.main(FEATURE, '--until', 'plan')
            state = self.state()
            self.assertEqual(state['status'], 'ready')
            self.assertTrue(state['reason'].startswith(f'retrying (1/3) after: fetching origin/main into {SANDBOX} '
                                                       'failed: fatal: unable to access origin'), state['reason'])
            self.assertGreater(state['not_before'], time.time() + 14 * 60)
            self.assertNotEqual(git_result(self.workspace(), 'rev-parse', '--verify', BRANCH).returncode, 0,
                                'no branch from a stale base')
            self.assertEqual(self.claude_calls(), [])

            for _ in range(3):
                self.main(FEATURE, '--until', 'plan')

        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertIn('fetching origin/main into', state['reason'])
        self.assertIn('unable to access origin', state['reason'].splitlines()[0])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.notifications, ['base fetch failed, retrying in 15 min: fatal: unable to access origin',
                                              'paused: base fetch failed'])

    def test_a_vm_this_run_started_is_not_stopped(self):
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        subprocess.run([str(self.fixture.bin / 'sbx'), 'exec', SANDBOX, '--', 'true'], check=True, capture_output=True)
        core.ACTIVE_SANDBOXES.add(SANDBOX)
        before = len(self.sbx_calls())
        core.ensure_sandbox(self.state())
        self.assertNotIn(['stop', SANDBOX], self.sbx_calls()[before:])
        self.assertEqual(self.sandbox_status(), 'running')

    def test_a_timed_out_stage_still_lands_its_commits_on_the_host(self):
        slow = {'run': IMPLEMENT['run'], 'sleep': 30}
        self.happy_path(**{'thing-01-implement': [slow]})
        stages = dict(core.STAGES, implement=dict(core.STAGES['implement'], hours=2 / 3600))

        with mock.patch.object(core, 'STAGES', stages):
            self.main(FEATURE, '--until', 'implement')

        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertIn('implement timeout', state['reason'])
        self.assertIn('fix: Do the thing', git(self.fixture.clone, 'log', '--format=%s', REF))
        self.assertEqual(self.sandbox_status(), 'stopped')

    def test_a_usage_limit_waits_for_the_reset_and_tick_resumes_the_same_session(self):
        self.happy_path(**{'thing-01-plan': [{'error': "You've hit your weekly limit", 'rate_limit_in': 3600}, PLAN]})
        self.assertEqual(self.main(FEATURE), 0)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('plan', 'ready'))
        self.assertIn('plan limit (seven_day) until', state['reason'])
        self.assertAlmostEqual(state['not_before'], time.time() + 3600 + core.LIMIT_MARGIN_SECONDS, delta=30)
        self.assertEqual(self.sandbox_status(), 'stopped')
        self.assertTrue(any(message.startswith('plan limit until') for message in self.notifications))
        self.main('tick')
        self.assertEqual(self.spawned, [], 'tick waits for the reset')
        state['not_before'] = time.time() - 1
        self.save(state)
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        self.main(FEATURE, '--until', 'plan')
        first, second = self.claude_calls('thing-01-plan')
        self.assertTrue(second['resume'])
        self.assertEqual(second['session'], first['session'])
        self.assertEqual(second['prompt'], core.RESUME_INTERRUPTED)
        self.assertEqual(self.state()['stage'], 'implement')
        self.assertIsNone(self.state()['not_before'])

    def test_a_limit_with_an_unknown_reset_waits_half_an_hour(self):
        self.script({'thing-01-plan': [{'error': "You've hit your session limit"}]})
        self.main(FEATURE)
        self.assertAlmostEqual(self.state()['not_before'], time.time() + core.LIMIT_UNKNOWN_WAIT_SECONDS, delta=30)

    def test_a_rejected_rate_limit_event_is_a_limit_whatever_the_wording(self):
        seen = {'mode': 'auto', 'result': {'is_error': True, 'result': 'Request refused'},
                'rate_limit': {'status': 'rejected', 'resetsAt': 5, 'rateLimitType': 'five_hour'}}
        outcome = core.classify(seen, False, 1, self.fixture.base / 'stage.log')
        self.assertEqual((outcome['kind'], outcome['resets_at'], outcome['window']), ('limit', 5, 'five_hour'))

    def test_a_crash_is_retried_once_by_resuming(self):
        self.happy_path(**{'thing-01-plan': [{'no_result': True}, PLAN]})
        self.main(FEATURE, '--until', 'plan')
        first, second = self.claude_calls('thing-01-plan')
        self.assertTrue(second['resume'])
        self.assertEqual(second['session'], first['session'])
        self.assertEqual(second['prompt'], core.RESUME_INTERRUPTED)
        self.assertEqual(self.state()['stage'], 'implement')

    def test_a_spent_budget_pauses_without_a_retry(self):
        self.happy_path(**{'thing-01-implement': [{'budget': True}, IMPLEMENT]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('implement', 'paused'))
        self.assertIn('spent its --max-budget-usd (total 61.5)', state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-implement')), 1)
        self.assertIn('paused: implement budget', self.notifications)

    def test_a_header_only_transcript_gets_a_new_session(self):
        self.happy_path(**{'thing-01-plan': [{'header_only': True, 'no_result': True}, PLAN]})
        self.main(FEATURE, '--until', 'plan')
        first, second = self.claude_calls('thing-01-plan')
        self.assertFalse(second['resume'])
        self.assertNotEqual(second['session'], first['session'])
        self.assertTrue(second['prompt'].startswith('/plan-harden '))
        self.assertEqual(self.state()['stage'], 'implement')

    def test_wrong_permission_mode_pauses(self):
        self.script({'thing-01-plan': [{'mode': 'default'}]})
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertIn("permission mode 'default'", self.state()['reason'])

    def test_a_session_on_another_model_pauses(self):
        self.script({'thing-01-plan': [{'model': 'claude-fable-5-1'}]})
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertIn("ran on 'claude-fable-5-1', not 'claude-opus-5-5'", self.state()['reason'])

    def test_revise_resumes_the_plan_session_with_the_feedback_prompt(self):
        self.happy_path(**{'thing-01-plan': [PLAN_ASKS, {'run': ["echo '# Plan v2' > {step}/plan.md"]}]})
        self.main(FEATURE)
        self.assertEqual(self.main('revise', FEATURE), 0)
        self.main(FEATURE, '--until', 'plan')
        second = self.claude_calls('thing-01-plan')[1]
        self.assertTrue(second['resume'])
        self.assertIn('## Feedback', second['prompt'])
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])

    def test_blocking_findings_in_the_last_review_round_wait_for_your_call(self):
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Handle the edge'"] + RESPOND['run']}
        self.happy_path(**{'thing-01-respond-1': [fix],
                           'thing-01-review-2': [{'run': ['echo ok > {step}/review-2.md'], 'result': {'blocking': 2}}],
                           'thing-01-respond-2': [RESPOND]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('review', 'awaiting-approval'))
        self.assertIn('2 blocking finding(s)', state['reason'])
        self.assertIn(f'review round 2: 2 blocking finding(s) need your call\n→ pipeline approve {FEATURE}',
                      self.notifications)
        self.assertEqual(self.main('approve', FEATURE), 0)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['stage'], 'done', state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-review-2')), 1)
        flag = ('Owner approved: review round 2 still reported 2 blocking finding(s); the respond stage fixed them '
                'without another review')
        self.assertIn(flag, state['results']['review-2']['flags'])
        self.assertFalse(state['approved'])
        comment = next(iter(self.github.comments.values()))
        notice = (self.fixture.step / 'done.md').read_text()

        for text in (comment, notice):
            decisions = text.split('## Decisions\n\n', 1)[1].split('\n## ', 1)[0]
            self.assertIn(f'- {flag}', decisions.splitlines())

        self.assertFalse([note for note in self.notifications if 'Owner approved' in note])
        self.assertIn(f'- flags: {flag}', ' '.join(self.claude_calls('thing-01-respond-2')[0]['argv']))

    def test_interrupted_gates_do_not_reprompt_a_finished_stage(self):
        self.happy_path()
        checks = []

        original = core.gate_checks

        def gate_checks(state, context):
            checks.append(1)

            if len(checks) == 1:
                raise KeyboardInterrupt

            return original(state, context)

        gates = [gate_checks if gate is core.gate_checks else gate for gate in core.BRANCH_GATES]

        with mock.patch.object(core, 'BRANCH_GATES', gates):
            self.assertEqual(self.main(FEATURE, '--until', 'implement'), 130)
            self.assertEqual(self.state()['status'], 'paused')
            self.assertEqual(self.main(FEATURE, '--until', 'implement'), 0)

        self.assertEqual(len(self.claude_calls('thing-01-implement')), 1)
        self.assertEqual(self.state()['stage'], 'rebase', self.state()['reason'])

    def test_sigterm_stops_the_stage_inside_the_vm_and_marks_it_resumable(self):
        self.happy_path(**{'thing-01-implement': [{'sleep': 30}]})
        timer = threading.Timer(4, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        self.assertEqual(self.main(FEATURE), 130)
        self.assertLess(time.monotonic() - started, 25)
        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['pending']['implement'], core.RESUME_INTERRUPTED)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertIn(['exec', SANDBOX, '--', 'pkill', '-INT', '-x', 'claude'], self.sbx_calls())
        self.assertEqual(self.sandbox_status(), 'stopped')

    def test_a_runner_error_pauses_the_step(self):
        self.happy_path()

        with mock.patch.object(core, 'make_branch', side_effect=ValueError('boom')):
            self.assertEqual(self.main(FEATURE), 1, 'a runner error is a failed run')

        self.assertIn('runner error ValueError: boom', self.state()['reason'])
        self.assertIn('paused: runner error', self.notifications)

    # ------------------------------------------------------------------------------------------ provisioning

    def test_an_egress_leak_after_setup_pauses_before_any_stage(self):
        self.happy_path()

        with mock.patch.dict(os.environ, {'FAKE_EGRESS': 'open'}):
            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertIn('egress canary: https://example.com is reachable', state['reason'])
        self.assertEqual(self.claude_calls(), [])

    def test_a_failed_egress_removal_pauses_at_once(self):
        self.happy_path()

        with mock.patch.dict(os.environ, {'FAKE_POLICY_RM_FAIL': '1'}):
            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn('sbx policy rm registry.npmjs.org: error: rm failed', state['reason'])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.notifications, ['paused: setup failed'])

    def test_a_failed_egress_removal_keeps_the_setup_error_in_the_pause(self):
        self.happy_path()
        self.pnpm({'install --frozen-lockfile': [{'code': 1, 'output': 'ERR_PNPM_OUTDATED_LOCKFILE'}]})

        with mock.patch.dict(os.environ, {'FAKE_POLICY_RM_FAIL': '1'}):
            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn('rm failed', state['reason'])
        self.assertIn('ERR_PNPM_OUTDATED_LOCKFILE', state['reason'])

    def test_a_failed_egress_allow_retries(self):
        self.happy_path()

        with mock.patch.dict(os.environ, {'FAKE_POLICY_ALLOW_FAIL': '1'}):
            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'ready', state['reason'])
        self.assertTrue(state['reason'].startswith('retrying (1/3) after: setup in the sandbox'), state['reason'])
        self.assertIn('allow failed', state['reason'])
        self.assertEqual(self.claude_calls(), [])

    def test_a_failed_setup_retries_three_times_then_pauses(self):
        self.happy_path()
        self.pnpm({'install --frozen-lockfile': [{'code': 1, 'output': 'ERR_PNPM_OUTDATED_LOCKFILE'}] * 4})

        for attempt in (1, 2, 3):
            self.main(FEATURE, '--until', 'plan')
            state = self.state()
            self.assertEqual(state['status'], 'ready', state['reason'])
            self.assertTrue(state['reason'].startswith(f'retrying ({attempt}/3) after: setup in the sandbox (log: '
                                                       f".pipeline/{state['id']}-setup.log) failed: "), state['reason'])
            self.assertIn('ERR_PNPM_OUTDATED_LOCKFILE', state['reason'].splitlines()[0])
            self.assertGreater(state['not_before'], time.time() + 14 * 60)
            self.assertFalse(core.actionable(state), 'tick waits the 15 minutes')
            self.assertTrue(core.step_view(state, False, time.time())[1].startswith(
                f'retrying {attempt}/3: setup in the sandbox'))

        self.main(FEATURE, '--until', 'plan')
        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn('ERR_PNPM_OUTDATED_LOCKFILE', state['reason'].splitlines()[0])
        self.assertIn('(after 3 retries)', state['reason'])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.notifications, ['setup failed, retrying in 15 min: ERR_PNPM_OUTDATED_LOCKFILE',
                                              'paused: setup failed'])

    def test_a_failed_setup_that_clears_on_the_retry_runs_the_stage(self):
        self.happy_path()
        self.pnpm({'install --frozen-lockfile': [{'code': 1, 'output': 'ERR_PNPM_OUTDATED_LOCKFILE'}]})
        self.main(FEATURE, '--until', 'plan')
        self.assertEqual(self.state()['status'], 'ready', self.state()['reason'])
        self.assertEqual(self.claude_calls(), [])
        self.main(FEATURE, '--until', 'plan')
        self.assertEqual(self.pnpm_calls().count('install --frozen-lockfile'), 2)
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])

    def test_a_failed_sandbox_create_retries_three_times_then_pauses(self):
        with mock.patch.dict(os.environ, {'FAKE_SBX_CREATE_FAIL': '1'}):
            for attempt in (1, 2, 3):
                self.main(FEATURE)
                state = self.state()
                self.assertEqual(state['status'], 'ready', state['reason'])
                self.assertTrue(state['reason'].startswith(f'retrying ({attempt}/3) after: sbx create {SANDBOX} '
                                                           'failed: error: create failed: no kvm'), state['reason'])
                self.assertGreater(state['not_before'], time.time() + 14 * 60)
                self.assertFalse(core.actionable(state), 'tick waits the 15 minutes')

            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'paused')
        self.assertIn('no kvm', state['reason'].splitlines()[0])
        self.assertIn('fix: sbx ls; sbx diagnose; pipeline template', state['reason'])
        self.assertEqual(self.notifications, ['sandbox create failed, retrying in 15 min: error: create failed: no kvm',
                                              'paused: sandbox create failed'])
        self.assertIsNone(self.sandbox_status())

    def test_a_queued_run_between_retries_does_not_reset_the_count(self):
        with mock.patch.dict(os.environ, {'FAKE_SBX_CREATE_FAIL': '1'}):
            self.main(FEATURE)
            self.usage(0.9)
            self.main(FEATURE)
            self.assertTrue(self.state()['reason'].startswith('queued: weekly usage 90%'), self.state()['reason'])
            self.usage(0.2)
            self.main(FEATURE)

        self.assertTrue(self.state()['reason'].startswith('retrying (2/3) after: sbx create'), self.state()['reason'])

    def test_an_existing_branch_on_github_is_never_silently_reused(self):
        git(self.fixture.other, 'checkout', '-q', '-b', BRANCH)
        git(self.fixture.other, 'commit', '-q', '--allow-empty', '-m', 'work of its own')
        git(self.fixture.other, 'push', '-q', 'origin', BRANCH)
        self.happy_path()
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('plan', 'paused'))
        self.assertIn(f'branch {BRANCH} already exists on GitHub', state['reason'])
        self.assertEqual(self.claude_calls(), [])

    def test_a_leftover_github_branch_holding_nothing_of_its_own_is_replaced_from_fresh_main(self):
        git(self.fixture.other, 'push', '-q', 'origin', f'main:{BRANCH}')
        git(self.fixture.other, 'pull', '-q')
        write(self.fixture.other / 'fresh.txt', 'fresh\n')
        git(self.fixture.other, 'add', '-A')
        git(self.fixture.other, 'commit', '-qm', 'chore: fresh main')
        git(self.fixture.other, 'push', '-q', 'origin', 'main')
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        state = self.state()
        self.assertEqual(state['stage'], 'implement', state['reason'])
        self.assertTrue((self.workspace() / 'fresh.txt').exists(), 'the branch was reused instead of branched fresh')
        self.assertTrue(any(note.startswith(f'leftover GitHub branch {BRANCH}') for note in state['notes']))

    def test_a_feature_without_steps_or_with_duplicate_numbers_is_refused(self):
        write(self.fixture.plans / 'empty/00-overview.md', '# Empty\n')
        self.assertEqual(self.main('empty'), 1)
        write(self.fixture.feature / '01-twin/brief.md', '# twin\n')
        self.assertEqual(self.main(FEATURE), 1)

        with self.assertRaises(SystemExit) as refused:
            self.main('Bad_Name')

        self.assertEqual(refused.exception.code, 2)

    def test_a_missing_or_open_app_key_fails_before_anything_runs(self):
        self.fixture.key.chmod(0o644)
        self.assertEqual(self.main(FEATURE), 1)
        self.assertIn('chmod 600', self.errors)
        self.fixture.key.unlink()
        self.assertEqual(self.main(FEATURE), 1)
        self.assertIn('is missing', self.errors)
        self.assertEqual(self.sbx_calls(), [])
        self.assertFalse(core.state_file(REPO, FEATURE, '01').exists())

    # ------------------------------------------------------------------------------------------ rebase and rounds

    def test_main_moving_rebases_with_a_pre_rebase_host_ref(self):
        self.happy_path(**{'thing-01-implement': [{'run': IMPLEMENT['run'] + [move_main('more', 'other.txt')]}]})
        self.main(FEATURE)
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        self.assertOnTopOfRemoteMain()
        backup = f"{REF}-pre-rebase-{time.strftime('%m%d')}"
        self.assertTrue(git(self.fixture.clone, 'rev-parse', '--verify', backup))
        self.assertIn(f'rebased onto origin/main (backup {backup})', self.state()['notes'])
        self.assertEqual(self.pnpm_calls().count('install --frozen-lockfile'), 2, 'setup runs again after a rebase')

    def test_rebase_conflict_resumes_implement_to_resolve_it(self):
        self.happy_path(**{'thing-01-implement': [{'run': IMPLEMENT['run'] + [move_main('main', 'thing.txt')]}, RESOLVE]})
        self.main(FEATURE)
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        prompt = self.claude_calls('thing-01-implement')[1]['prompt']
        self.assertIn('conflicts', prompt)
        self.assertIn(f'{REF}-pre-rebase-', prompt)

    def test_a_blocked_rebase_resolver_gets_the_answers_on_rerun(self):
        self.happy_path(**{'thing-01-implement': [{'run': IMPLEMENT['run'] + [move_main('main', 'thing.txt')]},
                                                  BLOCKED, RESOLVE]})
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused')
        self.main(FEATURE)
        third = self.claude_calls('thing-01-implement')[2]
        self.assertIn('Unresolved questions', third['prompt'])
        self.assertNotIn('conflicts', third['prompt'])
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        backups = git(self.fixture.clone, 'for-each-ref', '--format=%(refname)', f'{REF}-pre-rebase-*')
        self.assertEqual(len(backups.splitlines()), 1)

    def test_main_moving_during_respond_is_rebased_before_the_final_gates(self):
        respond = {'run': RESPOND['run'] + [move_main('late', 'late.txt')], 'result': RESPOND['result']}
        self.happy_path(**{'thing-01-respond-1': [respond]})
        self.main(FEATURE)
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        self.assertOnTopOfRemoteMain()
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), self.ref())

    def test_respond_changing_source_triggers_a_scoped_rereview(self):
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Handle the edge'"] + RESPOND['run']}
        self.happy_path(**{'thing-01-respond-1': [fix], 'thing-01-review-2': [{'run': ['echo ok > {step}/review-2.md']}],
                           'thing-01-respond-2': [RESPOND]})
        self.main(FEATURE)
        names = [call['name'] for call in self.claude_calls()]
        self.assertEqual(names, ['thing-01-plan', 'thing-01-implement', 'thing-01-review-1', 'thing-01-respond-1',
                                 'thing-01-review-2', 'thing-01-respond-2'])
        self.assertTrue(self.claude_calls('thing-01-review-2')[0]['prompt'].endswith(' 2 scoped'))
        self.assertEqual(self.state()['stage'], 'done')

    # ------------------------------------------------------------------------------------------ commands

    def test_approve_requires_a_step_awaiting_your_answer(self):
        self.assertEqual(self.main('approve', FEATURE), 1)

    def test_approve_revise_and_respond_fail_while_a_run_holds_the_feature(self):
        self.happy_path(**{'thing-01-plan': [PLAN_ASKS]})
        self.main(FEATURE)
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/run-shop-{FEATURE}.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)

        for command in ('approve', 'revise', 'respond'):
            self.assertEqual(self.main(command, FEATURE), 1, command)
            self.assertIn('another pipeline process (pid ', self.errors)

        self.assertEqual(self.state()['status'], 'awaiting-approval')

    def test_status_shows_usage_slots_sandbox_and_the_next_command(self):
        write(self.fixture.plans / 'later/01-x/brief.md', '# x\n')
        self.happy_path(**{'thing-01-plan': [PLAN_ASKS]})
        self.main(FEATURE)
        self.main('status', '-v')
        self.assertIn('claude usage: weekly 20% · 5h 10%', self.output)
        self.assertIn('stages: 0 running of 2 (parallel 2, ceiling 2: config admission.slots) · kill switch off · drain off',
                      self.output)
        self.assertIn(f'shop/{FEATURE} {STEP}: plan [awaiting-approval]', self.output)
        self.assertIn('sandbox stopped', self.output)
        self.assertIn(f'next: pipeline approve {FEATURE}', self.output)
        self.assertIn('shop/later 01-x: not started', self.output)
        state = self.state()
        state['status'] = 'closed'
        self.save(state)
        self.main('status', '-v')
        self.assertNotIn(f'shop/{FEATURE}', self.output)
        self.main('status', '-v', '--all')
        self.assertIn(f'shop/{FEATURE} {STEP}: plan [closed]', self.output)

    def test_tick_starts_features_with_work_and_skips_running_or_waiting_ones(self):
        write(self.fixture.plans / 'waiting/01-x/brief.md', '# x\n')
        write(self.fixture.plans / 'busy/01-y/brief.md', '# y\n')
        waiting = core.new_state(REPO, 'waiting', '01')
        core.mark(waiting, 'awaiting-approval', 'answer me')
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / 'logs/run-shop-busy.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.assertEqual(self.main('tick'), 0)
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        self.assertIn(f'tick: started {FEATURE}', self.output)

    def test_tick_starts_the_oldest_queued_first(self):
        write(self.fixture.plans / 'newer/01-x/brief.md', '# x\n')
        newer = core.new_state(REPO, 'newer', '01')
        newer['history'] = [{'at': '2026-09-30T10:00:00', 'stage': 'plan', 'status': 'ready', 'reason': 'x'}]
        self.save(newer)
        older = self.state()
        older['history'] = [{'at': '2026-09-29T10:00:00', 'stage': 'plan', 'status': 'ready', 'reason': 'x'},
                            {'at': '2026-09-30T11:00:00', 'stage': 'plan', 'status': 'ready', 'reason': 'queued: y'}]
        self.save(older)
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE), (REPO, 'newer')])

    def test_a_running_step_whose_run_died_is_marked_and_resumed_by_tick(self):
        state = self.state()
        core.mark(state, 'running', 'implement running')
        self.main('status', '-v')
        self.assertIn(f'shop/{FEATURE} {STEP}: plan [running?] no run holds the lock', self.output)
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/run-shop-{FEATURE}.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.spawned.clear()
        self.main('status', '-v')
        self.assertIn(f'shop/{FEATURE} {STEP}: plan [running] implement running', self.output)
        self.main('tick')
        self.assertEqual(self.spawned, [])

    def test_the_run_spawn_is_a_transient_systemd_unit(self):
        calls = []

        with mock.patch.object(core.subprocess, 'run', side_effect=lambda argv, **kwargs: calls.append(argv)):
            SPAWN(REPO, FEATURE)

        argv = calls[0]
        self.assertEqual(argv[:5], ['systemd-run', '--user', '--collect', '--quiet', '--unit'])
        self.assertRegex(argv[5], rf'^pipeline-run-shop-{FEATURE}-\d+$')
        self.assertIn('TimeoutStopSec=300', argv)
        self.assertIn('Wants=sbx.service', argv, 'a run at boot starts the capped daemon')
        self.assertIn('After=sbx.service', argv, 'and waits until it answers')
        self.assertEqual(argv[-5:], [core.PYTHON, str(core.PIPELINE_DIR / 'pipeline'), FEATURE, '--repo', REPO])

    def test_permission_denials_are_counted_on_the_pr_and_listed_in_done_md(self):
        self.happy_path(**{'thing-01-implement': [dict(IMPLEMENT, denials=[{'tool_name': 'Bash', 'tool_input': {
            'command': 'curl evil'}}])]})
        self.main(FEATURE)
        comment = next(iter(self.github.comments.values()))
        self.assertIn('Permission denials: 1 (details in done.md)', comment)
        self.assertNotIn('curl evil', comment)
        notice = (self.fixture.step / 'done.md').read_text()
        self.assertIn('implement: {"tool_name": "Bash"', notice)
        self.assertIn('## Usage', notice)

    def test_the_pr_comment_carries_project_facts_only_and_done_md_keeps_everything(self):
        respond = dict(RESPOND, result=dict(RESPOND['result'], flags=[
            '⚑ kept the old label', 'two lines\n## Learnings for memory\n- injected',
            'see /home/agent/code/x.ts, ask someone@example.com']))
        self.published(**{'thing-01-respond-1': [respond]})
        comment = next(iter(self.github.comments.values()))

        for expected in (core.EVIDENCE_MARKER, f'## Pipeline report: {FEATURE} {STEP}', '1 commit over', '1 review round,',
                         '## Decisions', '- kept the old label', '## Owed', 'after merge: pnpm db:migrate on shop',
                         '## Tests', '0 test cases added, 0 removed', '## CI', '0 CI fixes',
                         '- two lines ## Learnings for memory - injected', 'see ~/code/x.ts, ask [email]'):
            self.assertIn(expected, comment)

        for absent in ('⚑', '## Questions', '## Deferred', '- none', 'Permission denials', '(s)', '1 commits'):
            self.assertNotIn(absent, comment)

        for private in ('a trap worth remembering', 'Learnings for memory\n', 'Runner notes', 'MEMORY.md', '## Usage',
                        'weekly', '/home/', '@gmail'):
            self.assertNotIn(private, comment)

        notice = (self.fixture.step / 'done.md').read_text()

        for expected in ('- kept the old label', 'a trap worth remembering', '## Runner notes', 'MEMORY.md: 2 lines',
                         '## Usage', '## Permission denials'):
            self.assertIn(expected, notice)

        self.assertNotIn('## Decisions\n\n- ⚑', notice)

    def test_an_attribution_line_in_pr_md_never_reaches_the_pr(self):
        respond = dict(RESPOND, run=[
            "printf 'fix: Do the thing\\n\\nWhy it matters.\\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\\n"
            "Keep this, Claude Code wrote nothing here.\\n\\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
            "\\n\\n' > {step}/pr.md"])
        self.published(**{'thing-01-respond-1': [respond]})
        pull = self.github.pull(5)
        self.assertEqual((pull['title'], pull['body']),
                         ('fix: Do the thing', 'Why it matters.\nKeep this, Claude Code wrote nothing here.'))

    def test_later_stages_see_the_items_already_recorded_and_a_rerun_never_sees_its_own(self):
        owes = {'owed': ['before merge: reseed staging'], 'deferred': ['a wider rename']}
        self.happy_path(**{'thing-01-implement': [dict(IMPLEMENT, result=owes), {'result': owes}]})
        self.pnpm({'check': [{'code': 1, 'output': 'broken'}]})
        self.main(FEATURE)

        def prompts(name):
            return [' '.join(call['argv']) for call in self.claude_calls(name)]

        self.assertEqual(len(prompts('thing-01-implement')), 2, 'the red gate reruns implement')

        for prompt in prompts('thing-01-plan') + prompts('thing-01-implement'):
            self.assertNotIn('## Already recorded', prompt)

        review = prompts('thing-01-review-1')[0]

        for expected in ('## Already recorded for this step', '- owed: before merge: reseed staging',
                         '- deferred: a wider rename'):
            self.assertIn(expected, review)

        self.assertNotIn('- flags:', review)

    def test_agent_commits_are_authored_and_committed_by_the_app_bot(self):
        self.published()
        identities = git(self.fixture.origin, 'log', '--format=%an %ae%n%cn %ce', f'main..{BRANCH}').splitlines()
        self.assertTrue(identities)
        self.assertEqual(set(identities), {BOT})
        workspace_config = git(self.workspace(), 'config', '--local', '--list')
        self.assertIn('.insteadof=', workspace_config, 'origin still points at the host clone')

    def test_gc_drops_old_snapshots_refs_logs_and_orphan_sandboxes_of_merged_steps(self):
        old = int((time.time() - 20 * 86400) * 1000)
        new = int(time.time() * 1000)
        commit = git(self.fixture.main, 'rev-parse', 'HEAD')
        git(self.fixture.main, 'update-ref', f'refs/snapshots/shop/{old}-reset', commit)
        git(self.fixture.main, 'update-ref', f'refs/snapshots/shop/{new}-reset', commit)
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        subprocess.run(['sbx', 'create', '--name', 'pl-shop-gone-01', '--clone', 'claude', str(self.fixture.clone)],
                       check=True, capture_output=True)
        state = self.state()
        state['status'] = 'closed'
        self.save(state)
        old_log = core.state_dir(REPO, FEATURE) / '01-plan.log'
        os.utime(old_log, (time.time() - 20 * 86400,) * 2)
        write(core.state_dir(REPO, FEATURE) / '01-review-1.log', 'new\n')
        self.assertEqual(self.main('gc'), 0)
        refs = git(self.fixture.main, 'for-each-ref', '--format=%(refname)', 'refs/snapshots/')
        self.assertEqual(refs, f'refs/snapshots/shop/{new}-reset')
        self.assertFalse(old_log.exists())
        self.assertTrue((core.state_dir(REPO, FEATURE) / '01-review-1.log').exists())
        self.assertTrue(core.state_file(REPO, FEATURE, '01').exists())
        self.assertEqual(git(self.fixture.clone, 'for-each-ref', 'refs/pipeline/'), '')
        self.assertIsNone(self.sandbox_status('pl-shop-gone-01'))
        self.assertIsNone(self.sandbox_status(), 'a closed step keeps no sandbox')

    def test_gc_and_template_refuse_while_the_capped_sbx_daemon_is_down(self):
        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.assertEqual(self.main('gc'), 1)
            self.assertEqual(self.main('template'), 1)
            self.assertEqual(self.main('template', 'prune'), 1)

        self.assertIn('sbx.service not active', self.errors)
        self.assertEqual(self.sbx_calls(), [], 'no sbx call autostarts an uncapped daemon')



class TemplateTest(RunnerCase):
    """The sandbox template: built when the host's Claude Code moved (auto), or waited for (manual, failed builds),
    pinned, and pruned."""

    def marker(self):
        return json.loads((self.fixture.base / 'logs/template-failed.json').read_text())

    def age_marker(self):
        path = self.fixture.base / 'logs/template-failed.json'
        marker = json.loads(path.read_text())
        marker['at'] -= core.TRANSIENT_WAIT_SECONDS + 60
        path.write_text(json.dumps(marker))

    def test_a_host_upgrade_builds_the_template_once_then_creates_from_it(self):
        self.templates(tag_of('2.1.284'))
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        self.assertEqual(self.builds(), [[]], 'one build, of the host version')
        self.assertEqual(self.created_from(), [TAG])
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])
        self.assertIn('built template claude-2.1.285-node-24.21.0', ' '.join(self.state()['notes']))
        self.assertEqual(self.notifications, [], 'a successful build pings nobody')

    def test_a_run_waits_for_another_runs_build_instead_of_building_again(self):
        self.templates(tag_of('2.1.284'))
        self.happy_path()
        holding, waiting = threading.Event(), threading.Event()
        real = adapter.exclusive

        @contextlib.contextmanager
        def exclusive(name):
            if name == 'template' and threading.current_thread() is threading.main_thread():
                waiting.set()

            with real(name):
                yield

        def other_run():
            with real('template'):
                holding.set()
                waiting.wait(60)
                self.templates(tag_of('2.1.284'), TAG)  # the other run's build lands

        thread = threading.Thread(target=other_run)
        thread.start()
        self.assertTrue(holding.wait(10))

        with mock.patch.object(adapter, 'exclusive', exclusive):
            self.main(FEATURE, '--until', 'plan')

        thread.join(10)
        self.assertEqual(self.builds(), [], 'the check runs under the lock')
        self.assertEqual(self.created_from(), [TAG])

    def test_a_failed_build_holds_every_step_in_one_global_wait_and_heals(self):
        write(self.fixture.plans / 'other/01-x/brief.md', '# x\n')
        self.templates(tag_of('2.1.284'))
        self.happy_path()
        other = 'pl-shop-other-01'
        name = 'claude-2.1.285-node-24.21.0'
        self.addCleanup(subprocess.run, [str(self.fixture.bin / 'sbx'), 'rm', '--force', other],
                        env=dict(os.environ, FAKE_SBX_ROOT=str(self.fixture.sbx)), capture_output=True)

        with mock.patch.dict(os.environ, {'FAKE_BUILD_FAIL': '1'}):
            self.main(FEATURE, '--until', 'plan')
            self.main('other', '--until', 'plan')
            states = [self.state(), core.load_state(REPO, 'other', '01')]

            for state in states:
                self.assertEqual(state['status'], 'ready', state['reason'])
                self.assertTrue(state['reason'].startswith(
                    f'waiting for template {name}: build failed (1): ERROR: failed to solve'), state['reason'])
                self.assertFalse(core.actionable(state))

            self.assertEqual(len(self.builds()), 1, 'one build for both steps')
            self.assertEqual(self.notifications, [f'template {name} build failed (1): ERROR: failed to solve: process '
                                                  'did not complete successfully: exit code: 22; fix: pipeline '
                                                  'template build'])
            self.main(FEATURE, '--until', 'plan')
            self.assertEqual(len(self.builds()), 1, 'a fresh failure is not rebuilt')
            self.age_marker()
            self.main(FEATURE, '--until', 'plan')
            self.assertEqual((len(self.builds()), len(self.notifications)), (2, 1), 'the 2nd failure is quiet')
            self.age_marker()
            self.main('other', '--until', 'plan')
            self.assertEqual((len(self.builds()), len(self.notifications)), (3, 2), 'the 3rd failure pings again')
            self.assertIn('build failed (3)', self.notifications[-1])

        self.assertEqual([entry['status'] for entry in self.state()['history']
                          + core.load_state(REPO, 'other', '01')['history'] if entry['status'] == 'paused'], [])
        self.age_marker()
        self.main(FEATURE, '--until', 'plan')
        self.main('other', '--until', 'plan')
        self.assertEqual(len(self.builds()), 4)
        self.assertFalse((self.fixture.base / 'logs/template-failed.json').exists())
        self.assertEqual(self.created_from(), [TAG])
        self.assertEqual(self.created_from(other), [TAG])
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])
        self.assertTrue(self.claude_calls('other-01-plan'), 'the other step runs too')

    def test_a_pinned_version_is_used_whatever_the_host_runs(self):
        self.configure(claude='2.1.284', build='auto')
        self.templates(tag_of('2.1.284'))
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.created_from(), [tag_of('2.1.284')])
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])

    def test_manual_never_builds_and_waits_with_one_ping(self):
        self.configure(claude='2.1.283', build='manual')
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        self.main(FEATURE, '--until', 'plan')
        state = self.state()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['reason'], 'template claude-2.1.283-node-24.21.0 missing\nfix: pipeline template build')
        self.assertGreater(state['not_before'], time.time())
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.notifications, ['template claude-2.1.283-node-24.21.0 missing: pipeline template build'])
        self.assertEqual(len(state['history']), 1, 'the repeated wait is not recorded again')

    def test_template_build_builds_the_given_version_and_lets_waiting_steps_resume(self):
        write(self.fixture.plans / 'other/01-x/brief.md', '# x\n')
        name = 'claude-2.1.286-node-24.21.0'
        waiting = self.state()
        waiting['not_before'] = time.time() + 900
        core.mark(waiting, 'ready', f'template {name} missing\nfix: pipeline template build')
        limited = core.load_state(REPO, 'other', '01')
        limited['not_before'] = time.time() + 900
        core.mark(limited, 'ready', 'plan limit (weekly) until Fri 10:00: x. Tick resumes it then.')
        core.write_template_marker({'tag': tag_of('2.1.286'), 'failures': 0, 'at': time.time(), 'error': ''})
        self.assertEqual(self.main('template', 'build', '2.1.286'), 0, self.errors)
        self.assertEqual(self.builds(), [['2.1.286']])
        self.assertIn(tag_of('2.1.286'), self.templates())
        self.assertIsNone(self.state()['not_before'])
        self.assertIsNotNone(core.load_state(REPO, 'other', '01')['not_before'], 'other waits stay')
        self.assertFalse((self.fixture.base / 'logs/template-failed.json').exists())
        self.assertIn(f'tick resumes: shop/{FEATURE} 01', self.output)
        self.assertEqual(self.main('template', 'build', 'latest'), 2)

        with mock.patch.dict(os.environ, {'FAKE_BUILD_FAIL': '1'}):
            self.assertEqual(self.main('template', 'build'), 1)

        self.assertIn('build failed (exit 1): ERROR: failed to solve', self.errors)

    def test_prune_keeps_the_target_and_every_sandboxs_image(self):
        old, older, scratch = tag_of('2.1.284'), tag_of('2.1.283'), tag_of('2.1.282')
        self.templates(old, TAG, older, scratch)
        write(self.fixture.sbx / 'docker-images.json', json.dumps(
            [tag.removeprefix('docker.io/library/') for tag in (old, TAG, scratch, tag_of('2.1.280'))]))

        for name, tag in ((SANDBOX, TAG), ('scratch', scratch)):
            subprocess.run(['sbx', 'create', '--name', name, '-t', tag, 'claude', str(self.fixture.main)], check=True,
                           capture_output=True)

        self.addCleanup(subprocess.run, ['sbx', 'rm', '--force', 'scratch'], capture_output=True)
        stale, fresh = (self.fixture.base / 'cache/pipeline-sandbox_claude-2.1.284-node-24.21.0.tar',
                        self.fixture.base / 'cache/pipeline-sandbox_claude-2.1.286-node-24.21.0.tar')
        write(stale, 'x' * 1000)
        write(fresh, 'y')
        os.utime(stale, (time.time() - 7200,) * 2)

        with mock.patch.dict(os.environ, {'FAKE_SBX_INSPECT_FAIL': 'scratch'}):
            self.assertEqual(self.main('template', 'prune'), 1)

        self.assertIn('removed nothing', self.errors)
        self.assertEqual(self.templates(), [old, TAG, older, scratch])
        self.assertTrue(stale.exists())
        self.assertEqual(self.main('template', 'prune'), 0, self.errors)
        self.assertEqual(self.templates(), [TAG, scratch], 'a non-pl- sandbox keeps its template too')
        self.assertEqual(sorted(call[2] for call in self.docker_calls() if call[:2] == ['image', 'rm']),
                         [tag.removeprefix('docker.io/library/') for tag in (tag_of('2.1.280'), older, old)])
        self.assertEqual(json.loads((self.fixture.sbx / 'docker-images.json').read_text()),
                         [tag.removeprefix('docker.io/library/') for tag in (TAG, scratch)])
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists(), 'a tar a build may still be writing stays')
        self.assertIn('removed claude-2.1.280-node-24.21.0, claude-2.1.283-node-24.21.0, claude-2.1.284-node-24.21.0 '
                      'and 1 tar', self.output)

    def test_template_shows_host_policy_target_and_users(self):
        self.templates(tag_of('2.1.284'), TAG)
        subprocess.run(['sbx', 'create', '--name', SANDBOX, '-t', TAG, 'claude', str(self.fixture.main)], check=True,
                       capture_output=True)
        self.assertEqual(self.main('template'), 0, self.errors)
        self.assertIn('host: claude 2.1.285 · node 24.21.0', self.output)
        self.assertIn('policy: claude host · build auto', self.output)
        self.assertIn('target: claude 2.1.285 (host, auto) · loaded', self.output)
        self.assertIn('  claude-2.1.284-node-24.21.0  000000000000  1.1 GB  unused', self.output)
        self.assertIn(f'  claude-2.1.285-node-24.21.0  000000000001  1.1 GB  target · used by {SANDBOX}', self.output)


class HeadlessTest(RunnerCase):
    """What you see and do when a step stops: status, tick, ntfy and `--detach`."""

    def test_status_shows_the_cause_the_fix_the_detach_and_the_template(self):
        state = self.state()
        core.mark(state, 'paused', f'sbx create {SANDBOX} failed:\n✓ Git repository detected: /x\nerror: image '
                                   f'"{tag_of("2.1.287")}" is not cached locally and pull_policy=never')
        self.main('status', '-v')
        self.assertIn('template: claude 2.1.285 (host, auto) · loaded', self.output)
        self.assertIn(f'shop/{FEATURE} {STEP}: plan [paused] sbx create {SANDBOX} failed: error: image '
                      f'"{tag_of("2.1.287")}" is not cached locally', self.output)
        self.assertIn(f'  next: pipeline {FEATURE} --detach\n', self.output)
        self.assertNotIn('  sandbox', self.output, 'no `sandbox none` for a step that never had one')
        state['not_before'] = time.time() + 900
        state['sandbox_ready'] = True
        core.mark(state, 'ready', 'retrying (1/3) after: base fetch failed: fatal: x\nfix: sbx exec -it x bash\n\ntail')
        self.templates(tag_of('2.1.284'))
        self.main('status', '-v')
        self.assertIn('template: claude 2.1.285 (host, auto) · MISSING (the next sandbox create builds it)', self.output)
        self.assertIn('  sandbox gone · not before', self.output)
        self.assertIn('  fix: sbx exec -it x bash\n', self.output)
        self.assertIn(f'  next: pipeline {FEATURE} --detach  (or wait for tick)', self.output)
        core.mark(state, 'running', 'plan running')
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/run-shop-{FEATURE}.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.main('status', '-v')
        self.assertIn(f"  next: nothing (log: {self.fixture.base / 'logs'}/shop-{FEATURE}.out)", self.output)

    def test_detach_spawns_the_run_unless_it_would_do_nothing(self):
        held = []

        def spawn(repo, feature):
            held.append(core.locked(core.run_lock(repo, feature)))
            self.spawned.append((repo, feature))

        with mock.patch.object(core, 'spawn_feature_run', spawn):
            self.assertEqual(self.main(FEATURE, '--detach'), 0, self.errors)
            self.assertEqual(self.spawned, [(REPO, FEATURE)])
            self.assertEqual(held, [False], 'the child must be able to take the run lock')
            self.assertIn(f'run started in the background; log: {self.fixture.base}/logs/shop-{FEATURE}.out',
                          self.output)
            self.assertEqual(self.main(FEATURE, '--detach', '--until', 'plan'), 2)
            self.assertIn('drop --until/--step', self.errors)
            state = self.state()
            core.mark(state, 'awaiting-approval', 'The plan asks you')
            self.assertEqual(self.main(FEATURE, '--detach'), 1)
            self.assertIn(f'pipeline approve {FEATURE}', self.errors)
            core.mark(state, 'paused', 'x')
            (self.fixture.base / 'logs').mkdir(exist_ok=True)
            holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/run-shop-{FEATURE}.lock'), 'sleep',
                                       '60'], start_new_session=True)
            self.addCleanup(holder.kill)
            time.sleep(0.5)
            self.assertEqual(self.main(FEATURE, '--detach'), 1)
            self.assertIn('a run already holds', self.errors)
            holder.kill()
            holder.wait()
            write(self.fixture.base / 'logs/STOP', 'x\n')
            self.assertEqual(self.main(FEATURE, '--detach'), 1)
            self.assertIn('kill switch on', self.errors)

        self.assertEqual(self.spawned, [(REPO, FEATURE)])

    def test_tick_names_the_steps_waiting_on_you(self):
        write(self.fixture.plans / 'waiting/01-x/brief.md', '# x\n')
        core.mark(core.new_state(REPO, 'waiting', '01'), 'awaiting-approval', 'answer me')
        core.mark(self.state(), 'paused', 'x')
        self.main('tick')
        self.assertIn(f'tick: started nothing\ntick: waiting on you: shop/{FEATURE} 01 [paused], shop/waiting 01 '
                      '[awaiting-approval]', self.output)

    def test_a_runner_error_pings_its_first_line(self):
        self.happy_path()

        with mock.patch.object(core, 'make_branch', side_effect=core.PipelineError('git push failed:\nfatal: denied')):
            self.main(FEATURE)

        self.assertEqual(self.notifications, ['paused: error: git push failed: fatal: denied'])


class PublishTest(RunnerCase):
    """Publishing through the App: push the gate-green commit from the host clone, then the draft PR."""

    def test_a_green_step_is_pushed_from_the_host_and_opens_a_draft_pr(self):
        state = self.published()
        sha = self.ref()
        self.assertEqual((state['published_sha'], state['green_head']), (sha, sha))
        self.assertEqual(state['pr'], {'number': 5, 'url': 'https://example/pull/5', 'node_id': 'PR_5'})
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), sha)
        methods = [call for call in self.github.methods() if not call[1].endswith(('/installation', '/access_tokens'))]
        self.assertEqual(methods, [('GET', '/users/test-pipeline[bot]'), ('GET', '/repos/acme/shop/rules/branches/main'),
                                   ('GET', '/repos/acme/shop/pulls'), ('POST', '/repos/acme/shop/pulls'),
                                   ('POST', '/repos/acme/shop/issues/5/comments'),
                                   ('GET', '/repos/acme/shop/pulls/5'),
                                   ('GET', f'/repos/acme/shop/commits/{sha}/check-runs')])
        created = [body for method, path, _, body in self.github.calls if method == 'POST' and path.endswith('/pulls')][0]
        self.assertEqual(created, {'title': 'fix: Do the thing', 'head': BRANCH, 'base': 'main', 'body': 'Why it matters.',
                                   'draft': True})
        self.assertEqual({token for _, path, token, _ in self.github.calls
                          if not path.endswith(('/installation', '/access_tokens'))}, {'ghs_test'})
        token_request = [body for _, path, _, body in self.github.calls if path.endswith('/access_tokens')][0]
        self.assertEqual(token_request, {'repositories': ['shop']}, 'the token is scoped to the one repo')
        self.assertFalse(any(path.endswith('/requested_reviewers') for _, path in self.github.methods()),
                         'your review is requested only once CI is green')

    def test_a_symlinked_pr_text_is_never_read(self):
        secret = self.fixture.base / 'private.txt'
        write(secret, 'ssh private key\n')
        respond = {'run': [f"ln -s {secret} {{step}}/pr.md"]}
        self.happy_path(**{'thing-01-respond-1': [respond, respond, respond]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('respond', 'paused'), state['reason'])
        self.assertIn('not a regular file', state['reason'])
        self.assertFalse((self.fixture.step / 'pr.md').exists())
        self.assertFalse(any(method == 'POST' for method, _ in self.github.methods() if _ != '/app/installations/7/access_tokens'))
        self.assertEqual(self.origin_branch(), '')

    def test_a_token_in_the_pr_text_stops_the_publish(self):
        respond = {'run': ["printf 'fix: Do the thing\\n\\nghp_0123456789abcdefghijklmnopqrstuv\\n' > {step}/pr.md"]}
        self.happy_path(**{'thing-01-respond-1': [respond]})
        self.main(FEATURE)
        self.assertIn('token-shaped string', self.state()['reason'])
        self.assertEqual(self.origin_branch(), '')

    def test_a_planted_done_notice_symlink_is_replaced_not_written_through(self):
        victim = self.fixture.base / 'victim.txt'
        write(victim, 'yours\n')
        os.symlink(victim, self.fixture.step / 'done.md')
        self.published()
        self.assertEqual(victim.read_text(), 'yours\n')
        done = self.fixture.step / 'done.md'
        self.assertFalse(done.is_symlink())
        self.assertIn('## Decisions', done.read_text())

    def test_the_pr_title_and_body_are_redacted(self):
        respond = dict(RESPOND, run=["printf 'fix: Move /home/someone/x\\n\\nAsk someone@example.com.\\n' > {step}/pr.md"])
        self.published(**{'thing-01-respond-1': [respond]})
        pull = self.github.pull(5)
        self.assertEqual((pull['title'], pull['body']), ('fix: Move ~/x', 'Ask [email].'))

    def test_a_pr_that_already_exists_is_reused_and_its_text_resynced(self):
        self.github.pulls[9] = {'number': 9, 'html_url': 'https://example/pull/9', 'node_id': 'PR_9', 'state': 'open',
                                'draft': True, 'merged': False, 'mergeable': True, 'mergeable_state': 'clean',
                                'head': BRANCH, 'title': 'old', 'body': 'old'}
        state = self.published()
        self.assertEqual(state['pr']['number'], 9)
        self.assertEqual((self.github.pull(9)['title'], self.github.pull(9)['body']), ('fix: Do the thing', 'Why it matters.'))
        self.assertNotIn(('POST', '/repos/acme/shop/pulls'), self.github.methods())

    def test_an_env_file_in_the_diff_stops_the_publish(self):
        leak = {'run': ["mkdir -p deploy && echo SECRET=1 > deploy/prod.env && git add -f deploy/prod.env && "
                        "echo code > thing.txt && git add -A && git commit -qm 'fix: Do the thing'"]}
        self.happy_path(**{'thing-01-implement': [leak]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('publish', 'paused'), state['reason'])
        self.assertIn('env files in the diff: deploy/prod.env', state['reason'])
        self.assertEqual(self.origin_branch(), '')

    def test_a_token_shaped_string_in_the_diff_stops_the_publish(self):
        leak = {'run': ["echo 'token = \"ghp_0123456789abcdefghijklmnopqrstuv\"' > thing.txt && git add -A && "
                        "git commit -qm 'fix: Do the thing'"]}
        self.happy_path(**{'thing-01-implement': [leak]})
        self.main(FEATURE)
        self.assertIn('token-shaped string', self.state()['reason'])
        self.assertEqual(self.origin_branch(), '')

    def test_missing_review_rules_stop_the_publish_before_any_push(self):
        self.github.rules['main'] = [{'type': 'deletion'}]
        self.happy_path()
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('publish', 'paused'))
        self.assertIn('no active rule requiring a code-owner-approved pull request', state['reason'])
        self.assertEqual(self.origin_branch(), '')

    def test_a_branch_that_moved_on_github_is_not_overwritten(self):
        self.published()
        git(self.fixture.other, 'fetch', '-q', 'origin')
        git(self.fixture.other, 'checkout', '-q', '-b', BRANCH, f'origin/{BRANCH}')
        write(self.fixture.other / 'by-hand.txt', 'hand\n')
        git(self.fixture.other, 'add', '-A')
        git(self.fixture.other, 'commit', '-qm', 'fix: By hand')
        git(self.fixture.other, 'push', '-q', 'origin', BRANCH)
        moved = git(self.fixture.other, 'rev-parse', 'HEAD')
        git(self.workspace(), 'commit', '-q', '--allow-empty', '-m', 'fix: Again')
        git(self.fixture.clone, 'fetch', '-q', str(self.workspace()), f'+{BRANCH}:{REF}')
        state = self.state()
        state['stage'], state['status'], state['green_head'] = 'publish', 'ready', self.ref()
        self.save(state)
        self.main(FEATURE)
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), moved)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertIn(f'push of {BRANCH} refused', self.state()['reason'])

    def test_a_host_ref_that_is_not_the_gate_green_commit_is_never_published(self):
        self.happy_path()
        self.main(FEATURE, '--until', 'respond')
        state = self.state()
        state['stage'], state['status'] = 'publish', 'ready'
        self.save(state)
        git(self.fixture.clone, 'update-ref', REF, f'{REF}~1')
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertIn('not at the gate-green', self.state()['reason'])
        self.assertEqual(self.origin_branch(), '')

    def test_the_app_jwt_is_signed_by_the_key(self):
        app = adapter.load_config()['app']
        token = core.app_jwt(app)
        header, payload, signature = token.split('.')
        decode = lambda part: json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))  # noqa: E731
        self.assertEqual(decode(header), {'alg': 'RS256', 'typ': 'JWT'})
        self.assertEqual(decode(payload)['iss'], '42')
        self.assertLess(decode(payload)['exp'] - decode(payload)['iat'], 601)
        public = self.fixture.base / 'app.pub'
        subprocess.run(['openssl', 'rsa', '-in', str(self.fixture.key), '-pubout', '-out', str(public)],
                       capture_output=True, check=True)
        signature_file = self.fixture.base / 'sig.bin'
        signature_file.write_bytes(base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4)))
        verified = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public), '-signature', str(signature_file)],
                                  input=f'{header}.{payload}'.encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)


class PullRequestLoopTest(RunnerCase):
    """After the publish: merge, close, conflicts, CI and your PR comments."""

    def test_a_merged_pr_closes_the_step_and_starts_the_next(self):
        write(self.fixture.feature / '02-other/brief.md', '# 02 Other\n')
        self.published(**{'thing-02-plan': [{'run': ["printf '# Plan 2\\n' > $HOME/plans/thing/02-other/plan.md"]}]})
        git(self.fixture.clone, 'update-ref', f"{REF}-pre-rebase-0101", REF)
        self.github.pull(5)['merged'] = True
        self.assertEqual(self.main(FEATURE, '--until', 'plan'), 0)
        state = self.state()
        self.assertEqual(state['status'], 'closed', state['reason'])
        self.assertEqual(state['merged'], {'number': 5, 'url': 'https://example/pull/5'})
        self.assertIsNone(self.sandbox_status())
        self.assertEqual(git(self.fixture.clone, 'for-each-ref', 'refs/pipeline/thing/01*'), '')
        self.assertIn('closed: PR #5 merged', self.notifications)
        self.assertEqual(self.clicks[self.notifications.index('closed: PR #5 merged')], 'https://example/pull/5')
        self.assertEqual(self.state('02')['stage'], 'implement', self.state('02')['reason'])
        self.assertEqual(self.state('02')['branch'], 'agent/thing-02-other')

    def test_tick_resumes_a_step_paused_on_its_pr_only_once_you_merge_it(self):
        state = self.published()
        core.mark(state, 'paused', 'PR #5 now points at abc: someone else pushed')
        self.spawned.clear()
        self.main('tick')
        self.assertEqual(self.spawned, [], 'paused and unmerged: still yours')
        self.github.pull(5)['merged'] = True
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(self.state()['status'], 'closed', self.state()['reason'])

    def test_a_merged_pr_waits_to_close_while_the_capped_sbx_daemon_is_down(self):
        self.published()
        self.github.pull(5)['merged'] = True
        calls = len(self.sbx_calls())

        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.assertEqual(self.main(FEATURE), 0)

        self.assertEqual(self.state()['status'], 'done', self.state()['reason'])
        self.assertEqual(self.sbx_calls()[calls:], [], 'removing the sandbox would autostart an uncapped daemon')
        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(self.state()['status'], 'closed', self.state()['reason'])
        self.assertIsNone(self.sandbox_status())

    def test_a_sandbox_left_by_a_failed_removal_on_merge_goes_on_the_next_tick(self):
        self.published()
        self.github.pull(5)['merged'] = True

        with mock.patch.dict(os.environ, {'FAKE_SBX_RM_FAIL': '1'}):
            self.assertEqual(self.main(FEATURE), 0)

        state = self.state()
        self.assertEqual(state['status'], 'closed', state['reason'])
        self.assertEqual(self.sandbox_status(), 'stopped')
        self.assertTrue(any(f'sbx rm {SANDBOX} failed' in note and 'tick retries' in note for note in state['notes']),
                        state['notes'])
        calls = len(self.sbx_calls())

        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.main('tick')

        self.assertEqual(self.sbx_calls()[calls:], [], 'removing it now would autostart an uncapped daemon')
        self.assertEqual(self.sandbox_status(), 'stopped')
        self.main('tick')
        self.assertIsNone(self.sandbox_status())
        self.assertIn(f'tick: removed leftover sandboxes of closed steps: {SANDBOX}', self.output)

    def test_tick_leaves_the_sandboxes_of_open_steps(self):
        self.published()
        self.main('tick')
        self.assertEqual(self.sandbox_status(), 'stopped')
        self.assertNotIn('leftover', self.output)

    def test_an_open_pr_keeps_the_step_waiting(self):
        self.published()
        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(self.state()['status'], 'done')
        self.assertEqual(self.sandbox_status(), 'stopped')

    def test_a_pr_closed_unmerged_pauses_and_removes_the_sandbox(self):
        self.published()
        self.github.pull(5)['state'] = 'closed'
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'paused'))
        self.assertIn('closed unmerged: reopen it and re-run, or delete the feature', state['reason'])
        self.assertIsNone(self.sandbox_status())

    def test_a_failed_removal_on_an_unmerged_close_is_noted(self):
        self.published()
        self.github.pull(5)['state'] = 'closed'

        with mock.patch.dict(os.environ, {'FAKE_SBX_RM_FAIL': '1'}):
            self.main(FEATURE)

        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'paused'))
        self.assertTrue(any(f'sbx rm --force {SANDBOX}' in note for note in state['notes']), state['notes'])

    def test_ci_green_marks_the_pr_ready_and_requests_your_review_once(self):
        state = self.published()
        self.ci(state['published_sha'], 'success', 'success')
        self.main(FEATURE)
        self.assertFalse(self.github.pull(5)['draft'])
        requests = [body for method, path, _, body in self.github.calls if path.endswith('/requested_reviewers')]
        self.assertEqual(requests, [{'reviewers': ['reviewer1']}])
        self.assertTrue(any(message.startswith('✅ PR #5 · CI green · confidence') for message in self.notifications))
        self.assertEqual(self.clicks[-1], 'https://example/pull/5', 'tapping the ping opens the PR')
        self.assertIn('- green on', self.github.comments[1000])
        self.main(FEATURE)
        self.assertEqual(len([path for _, path in self.github.methods() if path.endswith('/requested_reviewers')]), 1)
        self.assertEqual(len([message for message in self.notifications if message.startswith('✅')]), 1)

    def test_a_refused_review_request_you_already_have_does_not_pause(self):
        state = self.published()
        self.github.refuse_review_request = True
        self.ci(state['published_sha'], 'success', 'success')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(state['ci']['reviewed'], state['published_sha'])
        self.assertTrue(any(message.startswith('✅ PR #5') for message in self.notifications))
        self.assertEqual(state['notes'], [], 'you are requested (by CODEOWNERS): nothing to note')

    def test_a_review_request_that_fails_outright_is_noted_not_paused(self):
        state = self.published()
        self.github.refuse_review_request = 'lost'
        self.ci(state['published_sha'], 'success', 'success')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertTrue(any('review request for reviewer1 failed' in note for note in state['notes']), state['notes'])

    def test_ci_still_running_waits(self):
        state = self.published()
        self.ci(state['published_sha'], 'success', None)
        self.main(FEATURE)
        self.assertTrue(self.github.pull(5)['draft'])
        self.assertEqual(self.state()['stage'], 'done')

    def test_ci_red_resumes_the_fixer_with_the_job_log_then_publishes_again(self):
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix]})
        self.main(FEATURE)
        first = self.state()['published_sha']
        self.ci(first, 'success', 'failure')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        resumed = self.claude_calls('thing-01-respond-1')[1]
        self.assertTrue(resumed['resume'])
        self.assertIn('CI failed on the pull request', resumed['prompt'])
        self.assertIn('Error: expected 1 to be 2', resumed['prompt'])
        self.assertIn('trace each later step and assertion of every failing test against the source', resumed['prompt'])
        self.assertIn(('RAW', '/repos/acme/shop/actions/jobs/101/logs'), [call[:2] for call in self.github.calls])
        self.assertNotEqual(state['published_sha'], first)
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), state['published_sha'])
        self.assertEqual(state['ci'], {'sha': state['published_sha'], 'fixes': 1, 'reviewed': None,
                                       'silent_pinged': False, 'reruns': 0, 'seen': []})
        self.ci(state['published_sha'], 'success', 'success')
        self.main(FEATURE)
        self.assertEqual(self.state()['ci']['reviewed'], state['published_sha'])

    def test_the_fixer_gets_the_failing_step_not_the_job_teardown(self):
        stamp = '2026-09-30T14:16:08.8128093Z '
        log = (f'{stamp}##[group]Run pnpm test:e2e\n{stamp}\x1b[31m  ✘ 73 route-smoke.spec.ts › staff admin\x1b[39m\n'
               f'{stamp}    Error: expect(page).toHaveTitle(expected) failed\n'
               f'{stamp}##[error]Process completed with exit code 1.\n'
               + ''.join(f'{stamp}Post job cleanup: container log line {index}\n' for index in range(2000)))
        self.github.logs['/repos/acme/shop/actions/jobs/101/logs'] = log
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix]})
        self.main(FEATURE)
        self.ci(self.state()['published_sha'], 'success', 'failure')
        self.main(FEATURE)
        prompt = self.claude_calls('thing-01-respond-1')[1]['prompt']
        self.assertIn('✘ 73 route-smoke.spec.ts › staff admin', prompt)
        self.assertIn('Error: expect(page).toHaveTitle(expected) failed', prompt)
        self.assertNotIn('Post job cleanup', prompt)
        self.assertNotIn('\x1b[', prompt)
        self.assertNotIn(stamp, prompt)

    def test_a_rerun_after_a_blocked_ci_fix_checks_ci_again(self):
        stuck = {'result': {'status': 'blocked', 'summary': 'the log shows no cause', 'questions': []}}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, stuck]})
        self.main(FEATURE)
        sha = self.state()['published_sha']
        self.ci(sha, 'success', 'failure')
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused', self.state()['reason'])
        self.ci(sha, 'success', 'success')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(state['ci']['reviewed'], sha, 'the rerun job is green: ready for review')
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 2, 'the fixer is not resumed with the old log')

    def test_a_rerun_after_a_blocked_ci_fix_that_is_still_red_resumes_the_fixer_with_the_new_log(self):
        stuck = {'result': {'status': 'blocked', 'summary': 'the log shows no cause', 'questions': []}}
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, stuck, fix]})
        self.main(FEATURE)
        sha = self.state()['published_sha']
        self.ci(sha, 'success', 'failure')
        self.main(FEATURE)
        self.github.logs['/repos/acme/shop/actions/jobs/111/logs'] = 'FAIL e2e/route-smoke.spec.ts\n'
        self.ci(sha, 'success', 'failure')
        self.main(FEATURE)
        calls = self.claude_calls('thing-01-respond-1')
        self.assertEqual(len(calls), 3, self.state()['reason'])
        self.assertIn('FAIL e2e/route-smoke.spec.ts', calls[2]['prompt'])
        self.assertEqual(self.state()['ci']['fixes'], 2)

    def test_ci_red_after_main_moved_is_rebased_and_pushed_before_any_fix(self):
        before = self.published()['published_sha']
        self.move_main()
        self.ci(before, 'success', 'failure')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1, 'no fixer: CI judges the rebased branch')
        self.assertNotIn(('RAW', '/repos/acme/shop/actions/jobs/101/logs'), [call[:2] for call in self.github.calls])
        self.assertOnTopOfRemoteMain()
        self.assertNotEqual(state['published_sha'], before)
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), state['published_sha'])
        self.assertEqual(state['ci']['fixes'], 0)
        self.assertTrue(any('rebased onto origin/main' in note for note in state['notes']), state['notes'])

    def test_a_rerun_after_a_blocked_ci_fix_rebases_onto_a_moved_main(self):
        stuck = {'result': {'status': 'blocked', 'summary': 'the log shows no cause', 'questions': ['Q1? Rec: A']}}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, stuck]})
        self.main(FEATURE)
        before = self.state()['published_sha']
        self.ci(before, 'success', 'failure')
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused', self.state()['reason'])
        self.move_main()
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 2, 'the fixer is not resumed on the old base')
        self.assertOnTopOfRemoteMain()
        self.assertNotEqual(state['published_sha'], before)
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), state['published_sha'])
        self.assertNotIn('respond-1', state['results'])
        self.assertNotIn('respond-1', state['pending'])
        self.assertNotIn('Q1?', self.github.comments[1000])
        self.assertNotIn('Q1?', (self.fixture.step / 'done.md').read_text())

    def test_a_ci_rebase_failed_in_setup_resumes_without_the_fixer(self):
        before = self.published()['published_sha']
        self.move_main()
        self.ci(before, 'success', 'failure')
        self.pnpm({'install --frozen-lockfile': [{'code': 1, 'output': 'ERR_PNPM_OUTDATED_LOCKFILE'}]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['status'], 'ready', state['reason'])
        self.assertTrue(state['reason'].startswith('retrying (1/3) after: setup in the sandbox'), state['reason'])
        self.assertIn('ERR_PNPM_OUTDATED_LOCKFILE', state['reason'])
        self.assertTrue(state['post_rebase'], 'waits in the setup after the rebase')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1)
        self.assertOnTopOfRemoteMain()
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), state['published_sha'])
        self.assertNotEqual(state['published_sha'], before)
        self.assertEqual(state['ci']['fixes'], 0)

    def test_a_stale_green_on_the_old_commit_is_ignored(self):
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix]})
        self.main(FEATURE)
        first = self.state()['published_sha']
        self.ci(first, 'success', 'failure')
        self.main(FEATURE)
        self.github.check_runs[first] = [dict(run, conclusion='success') for run in self.github.check_runs[first]]
        self.main(FEATURE)
        self.assertTrue(self.github.pull(5)['draft'], 'green on the old commit says nothing about the new one')
        self.assertIsNone(self.state()['ci']['reviewed'])

    def test_ci_red_three_times_pauses(self):
        fix = {'run': ["echo again >> thing.txt && git add -A && git commit -qm 'fix: Try again'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix, fix]})
        self.main(FEATURE)

        for _ in range(3):
            self.ci(self.state()['published_sha'], 'failure', 'success')
            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn('CI is red on PR #5 after 2 fixes', state['reason'])
        self.assertNotIn('did not move', state['reason'], 'a log without a code frame says nothing about moving')
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 3)

    def red(self, log):
        """CI red on the published commit by `check`, this text its job log; then a run."""
        sha = self.state()['published_sha']
        self.ci(sha, 'failure', 'success')
        failed = next(run for run in self.github.check_runs[sha] if run['conclusion'] == 'failure')
        self.github.logs[f"/repos/acme/shop/actions/jobs/{failed['id']}/logs"] = log
        self.main(FEATURE)

    def test_a_failure_that_moves_on_every_fix_gets_up_to_five_fixes(self):
        fix = {'run': ["echo again >> thing.txt && git add -A && git commit -qm 'fix: Try again'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND] + [fix] * 5})
        self.main(FEATURE)

        for index in range(6):
            self.red(playwright_log(f'expect(step{index}).toBe(true);'))

        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertEqual(state['reason'], 'CI is red on PR #5 after 5 fixes (check).')
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 6)
        reasons = [entry['reason'] for entry in state['history']]
        self.assertIn('PR #5: CI red (check): fixing (2/2)', reasons)
        self.assertIn('PR #5: CI red (check): fixing (3/5, failure moved)', reasons)
        self.assertIn('PR #5: CI red (check): fixing (5/5, failure moved)', reasons)
        self.assertEqual(state['ci']['seen'], [[f'expect(step{index}).toBe(true);'] for index in range(5)])
        self.main('show', FEATURE)
        self.assertIn('(5/5 fixes)', self.output)

    def test_a_failure_back_after_two_fixes_pauses_though_its_line_number_and_neighbours_changed(self):
        fix = {'run': ["echo again >> thing.txt && git add -A && git commit -qm 'fix: Try again'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix, fix]})
        self.main(FEATURE)
        stuck = "expect(produced.status).toBe('in_production');"
        self.red(playwright_log(stuck, line=168, passing=3))
        self.red(playwright_log(stuck, line=168, passing=3))
        self.red(playwright_log(stuck, line=173, passing=7))
        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertEqual(state['reason'], 'CI is red on PR #5 after 2 fixes (check): the failure did not move.')
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 3)

    def test_a_flaky_sibling_buys_one_more_fix_not_more(self):
        fix = {'run': ["echo again >> thing.txt && git add -A && git commit -qm 'fix: Try again'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix, fix, fix]})
        self.main(FEATURE)
        real, flaky = 'expect(order.status).toBe(1);', 'await expect(page.getByLabel(/existing rtu/i)).toBeAttached();'
        self.red(playwright_log('expect(first).toBe(1);'))
        self.red(playwright_log(real))
        self.red(playwright_log(real, flaky))
        self.assertEqual(self.state()['status'], 'done', self.state()['reason'])
        self.red(playwright_log(real))
        state = self.state()
        self.assertEqual(state['reason'], 'CI is red on PR #5 after 3 fixes (check): the failure did not move.')
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 4)

    def test_a_step_paused_before_failures_were_tracked_gets_a_third_fix_for_its_failure(self):
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        state = self.published(**{'thing-01-respond-1': [RESPOND, fix]})
        state['ci'] = {key: value for key, value in state['ci'].items() if key != 'seen'} | {'fixes': 2}
        state['status'], state['reason'] = 'paused', 'CI is red on PR #5 after 2 fixes (check).'
        self.save(state)
        failing = "expect(produced.status).toBe('in_production');"
        self.red(playwright_log(failing))
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertIn('PR #5: CI red (check): fixing (3/5, failure moved)',
                      [entry['reason'] for entry in state['history']])
        self.assertEqual((state['ci']['fixes'], state['ci']['seen']), (3, [[failing]]))
        self.main('status', '-v')
        self.assertIn('PR #5 CI waiting (3/5 fixes)', self.output)

    def test_a_rerun_after_a_blocked_third_fix_resumes_the_fixer_for_the_same_failure(self):
        fix = {'run': ["echo again >> thing.txt && git add -A && git commit -qm 'fix: Try again'"]}
        stuck = {'result': {'status': 'blocked', 'summary': 'the log shows no cause', 'questions': []}}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, fix, fix, stuck, fix]})
        self.main(FEATURE)
        self.red(playwright_log('expect(first).toBe(1);'))
        self.red(playwright_log('expect(second).toBe(1);'))
        self.red(playwright_log('expect(third).toBe(1);'))
        self.assertEqual(self.state()['status'], 'paused', self.state()['reason'])
        self.assertEqual(self.state()['ci']['seen'], [['expect(first).toBe(1);'], ['expect(second).toBe(1);']],
                         'the blocked fix pushed nothing')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        calls = self.claude_calls('thing-01-respond-1')
        self.assertEqual(len(calls), 5)
        self.assertIn('expect(third).toBe(1);', calls[4]['prompt'])
        self.assertEqual(state['ci']['fixes'], 4)

    def test_no_ci_half_an_hour_after_the_push_pings_once(self):
        state = self.published()
        state['published_at'] = time.time() - core.CI_SILENT_SECONDS - 5
        self.save(state)
        self.main(FEATURE)
        self.main(FEATURE)
        self.assertEqual(len([message for message in self.notifications if 'no required check' in message]), 1)

    def test_a_required_check_that_never_starts_pings_once(self):
        state = self.published()
        self.ci(state['published_sha'], 'success')
        state['published_at'] = time.time() - core.CI_SILENT_SECONDS - 5
        self.save(state)
        self.main(FEATURE)
        self.main(FEATURE)
        pings = [message for message in self.notifications if 'no required check' in message]
        self.assertEqual(pings, ['PR #5: no required check (test) started in 30 min'])
        self.assertTrue(self.github.pull(5)['draft'])

    def queued(self, sha):
        """check and test waiting for a runner: a workflow run whose jobs no runner took yet."""
        jobs = [{'id': 100 + index, 'name': name, 'status': 'queued', 'conclusion': None, 'completed_at': None,
                 'steps': []} for index, name in enumerate(('check', 'test'))]
        self.actions(sha, {'id': 900, 'workflow_id': 1, 'status': 'queued', 'run_attempt': 1, 'check_suite_id': 9000},
                     jobs)

    def runner_units(self, active):
        """systemctl's answer for the ci-runner@ units; every other command runs."""
        real = subprocess.run

        def run(argv, *arguments, **options):
            if argv[:3] == ['systemctl', '--user', 'list-units']:
                return subprocess.CompletedProcess(argv, 0, 'ci-runner@1.service loaded active running CI\n'
                                                   if active else '', '')

            return real(argv, *arguments, **options)

        return mock.patch.object(core.subprocess, 'run', side_effect=run)

    def test_checks_queued_with_no_ci_runner_unit_active_ping_once(self):
        state = self.published()
        self.queued(state['published_sha'])
        state['published_at'] = time.time() - core.CI_SILENT_SECONDS - 5
        self.save(state)

        with self.runner_units(active=False):
            self.main(FEATURE)
            self.main(FEATURE)

        self.assertEqual([message for message in self.notifications if 'PR #5' in message],
                         ['PR #5: required checks (check, test) queued for 30 min; no ci-runner unit is active '
                          '(`ci-runner status`)'])

    def test_checks_queued_behind_a_busy_ci_runner_never_ping(self):
        state = self.published()
        self.queued(state['published_sha'])
        state['published_at'] = time.time() - core.CI_SILENT_SECONDS - 5
        self.save(state)

        with self.runner_units(active=True):
            self.main(FEATURE)

        self.assertEqual([message for message in self.notifications if 'PR #5' in message], [])
        self.assertEqual(self.state()['status'], 'done', self.state()['reason'])

    def test_a_pr_branch_pushed_by_someone_else_pauses_instead_of_trusting_the_old_ci(self):
        state = self.published()
        self.ci(state['published_sha'], 'success', 'success')
        git(self.fixture.other, 'fetch', '-q', 'origin')
        git(self.fixture.other, 'checkout', '-q', '-b', BRANCH, f'origin/{BRANCH}')
        write(self.fixture.other / 'by-hand.txt', 'hand\n')
        git(self.fixture.other, 'add', '-A')
        git(self.fixture.other, 'commit', '-qm', 'fix: By hand')
        git(self.fixture.other, 'push', '-q', 'origin', BRANCH)
        moved = git(self.fixture.other, 'rev-parse', 'HEAD')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn(f'PR #5 now points at {moved[:12]}', state['reason'])
        self.assertTrue(self.github.pull(5)['draft'], 'green on the old commit says nothing about the pushed one')
        self.assertFalse([path for _, path in self.github.methods() if path.endswith('/requested_reviewers')])
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), moved, 'never overwritten')
        git(self.fixture.other, 'push', '-q', '--force', 'origin', f"{state['published_sha']}:refs/heads/{BRANCH}")
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), 'handed back: a re-run polls again')
        self.assertEqual(state['ci']['reviewed'], state['published_sha'])

    def test_a_conflicting_pr_is_rebased_and_pushed_with_a_lease(self):
        self.published()
        before = self.state()['published_sha']
        git(self.fixture.other, 'pull', '-q')
        write(self.fixture.other / 'thing.txt', 'main\n')
        git(self.fixture.other, 'add', '-A')
        git(self.fixture.other, 'commit', '-qm', 'chore: main moved')
        git(self.fixture.other, 'push', '-q', 'origin', 'main')
        self.github.pull(5)['mergeable_state'] = 'dirty'
        self.script({'thing-01-respond-1': [RESPOND, RESOLVE]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertIn('conflicts', self.claude_calls('thing-01-respond-1')[1]['prompt'])
        self.assertOnTopOfRemoteMain()
        self.assertNotEqual(state['published_sha'], before)
        self.assertEqual(git(self.fixture.origin, 'rev-parse', BRANCH), state['published_sha'])
        self.assertEqual(state['ci']['fixes'], 0)

    def test_a_null_mergeable_is_asked_again(self):
        self.published()
        pull = self.github.pull(5)
        pull['mergeable'] = None
        self.main(FEATURE)
        gets = [path for method, path in self.github.methods() if method == 'GET' and path == '/repos/acme/shop/pulls/5']
        self.assertEqual(len(gets), 3, 'one poll after the publish, then one plus a retry for the null')

    def comment(self, kind, body, login='reviewer1', stamp='2026-09-30T12:00:00Z', **extra):
        target = {'reviews': self.github.reviews, 'inline': self.github.inline,
                  'conversation': self.github.conversation}[kind]
        key = 'submitted_at' if kind == 'reviews' else 'created_at'
        target.setdefault(5, []).append(dict({'user': {'login': login}, 'body': body, key: stamp,
                                              'html_url': 'https://example/c'}, **extra))

    def test_respond_turns_your_pr_comments_into_the_next_round(self):
        self.published()
        self.comment('reviews', 'Rename the helper.', state='CHANGES_REQUESTED')
        self.comment('inline', 'Off by one here.', path='src/thing.ts', line=3)
        self.comment('conversation', 'Also update the docs.')
        self.comment('conversation', 'Bot noise', login='someone-else')
        self.assertEqual(self.main('respond', FEATURE), 0)
        review = (self.fixture.step / 'review-2.md').read_text()
        self.assertIn('Rename the helper.', review)
        self.assertIn('`src/thing.ts:3` Off by one here.', review)
        self.assertIn('Also update the docs.', review)
        self.assertNotIn('Bot noise', review)
        state = self.state()
        self.assertEqual((state['stage'], state['status'], state['round']), ('respond', 'ready', 2))
        self.assertEqual(state['finals'], {'1': True, '2': True})
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        self.assertEqual(self.main('respond', FEATURE), 1, 'nothing new until the round ran')
        self.script({'thing-01-respond-2': [{'run': [
            "echo renamed >> thing.txt && git add -A && git commit -qm 'fix: Rename the helper'",
            "printf 'fix: Do the thing better\\n\\nNew body.\\n' > {step}/pr.md"]}]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        prompt = self.claude_calls('thing-01-respond-2')[0]['prompt']
        self.assertTrue(prompt.startswith(f"/review-respond {self.vm_step() / 'plan.md'} 2"))
        self.assertIn("holds the user's own comments on the pull request (PR #5)", prompt)
        self.assertEqual((self.github.pull(5)['title'], self.github.pull(5)['body']),
                         ('fix: Do the thing better', 'New body.'), 'the PR text is re-synced from pr.md')
        self.assertEqual(len(self.github.comments), 1, 'the evidence comment is edited in place')
        self.assertEqual(self.main('respond', FEATURE), 1)
        self.assertIn('no new comments from reviewer1', self.errors)
        self.comment('conversation', 'One more thing.', stamp='2026-09-30T13:00:00Z')
        self.assertEqual(self.main('respond', FEATURE), 0)
        self.assertEqual(self.state()['round'], 3)
        self.assertNotIn('Rename the helper.', (self.fixture.step / 'review-3.md').read_text())

    def test_respond_needs_a_done_step_with_an_open_pr(self):
        self.assertEqual(self.main('respond', FEATURE), 1)
        self.published()
        self.github.pull(5)['state'] = 'closed'
        self.assertEqual(self.main('respond', FEATURE), 1)


class StatePathTest(unittest.TestCase):
    """Config and state live outside the code dir (no fixture: the module defaults)."""

    def test_config_and_state_live_in_the_users_config_and_state_dirs(self):
        self.assertEqual(adapter.CONFIG, Path.home() / '.config/pipeline/config.json')
        self.assertEqual(adapter.LOGS, Path.home() / '.local/state/pipeline')
        self.assertIs(core.LOGS, adapter.LOGS)

        for path in (core.STOP_FLAG, core.USAGE, core.CONTROL, core.TEMPLATE_FAILED):
            self.assertEqual(path.parent, adapter.LOGS, path)


class ClassifyRunTest(unittest.TestCase):
    """Whether a red workflow run is the code's fault, from documented fields: the red job that ended first decides."""

    def kind(self, jobs, run=TIMEOUT_RUN, *others):
        return core.classify_run(jobs, run, [run, *others])[0]

    def test_a_check_outside_actions_or_without_jobs_is_code(self):
        self.assertEqual(core.classify_run(TIMEOUT_JOBS, None, []), ('code', None))
        self.assertEqual(self.kind([]), 'code')
        self.assertEqual(self.kind(None), 'code')

    def test_a_failed_step_is_code(self):
        kind, decider = core.classify_run(CODE_JOBS, CODE_RUN, [CODE_RUN])
        self.assertEqual((kind, decider['id']), ('code', 111824748686))

    def test_a_failed_step_is_fixed_while_its_run_still_runs(self):
        self.assertEqual(self.kind(CODE_JOBS, dict(CODE_RUN, status='in_progress')), 'code')

    def test_a_run_still_running_waits(self):
        self.assertEqual(self.kind(TIMEOUT_JOBS, dict(TIMEOUT_RUN, status='queued')), 'pending')
        self.assertEqual(self.kind([], dict(TIMEOUT_RUN, status='queued')), 'pending', 'a rerun before its jobs exist')
        self.assertEqual(self.kind(TIMEOUT_JOBS[1:], dict(TIMEOUT_RUN, status='in_progress')), 'pending')

    def test_a_run_with_no_red_job_left_is_code(self):
        self.assertEqual(self.kind(TIMEOUT_JOBS[1:]), 'code')
        self.assertEqual(self.kind([dict(TIMEOUT_JOBS[0], conclusion='action_required')]), 'code')

    def test_a_run_superseded_by_a_newer_one_still_running_on_the_commit_waits(self):
        newer = dict(SUPERSEDED_RUN, id=SUPERSEDED_RUN['id'] + 5, status='queued', check_suite_id=1)
        self.assertEqual(self.kind(SUPERSEDED_JOBS, SUPERSEDED_RUN, newer), 'superseded')
        self.assertEqual(self.kind(SUPERSEDED_JOBS, SUPERSEDED_RUN, dict(newer, status='completed')), 'infra',
                         'a newer run that finished already gave its own check runs')
        self.assertEqual(self.kind(SUPERSEDED_JOBS, SUPERSEDED_RUN, dict(newer, workflow_id=1)), 'infra')
        self.assertEqual(self.kind(SUPERSEDED_JOBS, SUPERSEDED_RUN, dict(newer, id=SUPERSEDED_RUN['id'] - 5)), 'infra')

    def test_a_job_that_ended_without_a_failed_step_is_infra(self):
        self.assertEqual(self.kind(TIMEOUT_JOBS), 'infra')
        self.assertEqual(self.kind(SUPERSEDED_JOBS, SUPERSEDED_RUN), 'infra')
        self.assertEqual(self.kind(NOT_ACQUIRED_JOBS, NOT_ACQUIRED_RUN), 'infra')
        self.assertEqual(self.kind(RUNNER_LOST_JOBS), 'infra')
        self.assertEqual(self.kind([dict(TIMEOUT_JOBS[0], conclusion='timed_out'), *TIMEOUT_JOBS[1:]]), 'infra')

    def test_the_red_job_that_ended_first_decides(self):
        kind, decider = core.classify_run(FAIL_FAST_JOBS, TIMEOUT_RUN, [TIMEOUT_RUN])
        self.assertEqual((kind, decider['name']), ('code', 'test (2)'), 'fail-fast cancelled its sibling after')
        kind, decider = core.classify_run(GATE_JOBS, TIMEOUT_RUN, [TIMEOUT_RUN])
        self.assertEqual((kind, decider['name']), ('infra', 'test'), 'the gate failed because test timed out')

    def test_why_is_the_failure_annotations_without_the_noise(self):
        self.assertEqual(core.ci_why(TIMEOUT_ANNOTATIONS, TIMEOUT_JOBS[0]),
                         'The job has exceeded the maximum execution time of 20m0s')
        self.assertEqual(core.ci_why(SUPERSEDED_ANNOTATIONS, SUPERSEDED_JOBS[0]),
                         'Canceling since a higher priority waiting request for check-refs/pull/238/merge exists')
        self.assertEqual(core.ci_why(CODE_ANNOTATIONS[:1], RUNNER_LOST_JOBS[0]), 'failure')
        self.assertEqual(core.ci_why([], NOT_ACQUIRED_JOBS[0]), 'cancelled')
        self.assertEqual(core.ci_why([{'annotation_level': 'failure', 'message': 'a'}, NOISE,
                                      {'annotation_level': 'failure', 'message': 'b'}], GATE_JOBS[0]), 'a; b')
        self.assertEqual(len(core.ci_why([{'annotation_level': 'failure', 'message': 'x' * 500}], GATE_JOBS[0])), 300)


class PathPatternTest(unittest.TestCase):
    """A path in the plan's `## Test changes` that the tamper gate's exact-path check could never match."""

    def test_a_glob_or_brace_list_is_a_pattern(self):
        for text in ['`src/old/**/*.test.ts`', 'src/{a,b}/x.test.ts', '`apps/web/x/*.test.ts(x)`', 'apps/x.test.ts(x)']:
            self.assertTrue(core.path_patterns(text), text)

    def test_an_exact_path_url_route_file_bold_or_line_number_is_not(self):
        for text in ['`apps/web/x.test.ts`', '`/x?productCode=KD`', '`routes/[id].test.tsx`', '**apps/x.test.ts**',
                     'apps/x.test.ts(12)', '- **Moved:** `a/b.test.ts` → `c/b.test.ts`', 'x.test.ts', '*']:
            self.assertEqual(core.path_patterns(text), [], text)

    def test_a_missing_or_uncopied_plan_has_nothing_to_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / 'plan.md'

            with mock.patch.object(core, 'step_of', return_value=mock.Mock(plan=plan)):
                self.assertEqual(core.gate_test_changes({'copy_errors': {}}, {}), [])
                plan.write_text('## Test changes\n- `src/**/*.test.ts`\n')
                self.assertIn('names a pattern', core.gate_test_changes({'copy_errors': {}}, {})[0])
                self.assertEqual(core.gate_test_changes({'copy_errors': {'plan.md': 'a symlink'}}, {}), [])


class TestPatternTest(unittest.TestCase):
    """The tamper gate and the done notice read JS/TS and Python tests alike."""

    def test_python_test_files_count_as_tests(self):
        for path in ['test_core.py', 'tests/test_core.py', 'pkg/test_x.py', 'pkg/x_test.py']:
            self.assertTrue(core.TEST_FILE.search(path), path)
            self.assertTrue(core.TEST_OR_DOC.search(path), path)

        for path in ['core.py', 'pkg/contest.py', 'pkg/test_x.pyc', 'latest_x.py']:
            self.assertFalse(core.TEST_FILE.search(path), path)

    def test_python_skips_and_cases_are_seen(self):
        for line in ['@unittest.skip("flaky")', "@unittest.skipIf(True, 'x')", '@pytest.mark.skip', 'self.skipTest("x")']:
            self.assertTrue(core.SKIP_LINE.search(line), line)

        for line in ['+    def test_x(self):', '-    async def test_y(self):', "+  it('x', () => {})"]:
            self.assertTrue(core.TEST_CASE_LINE.match(line), line)

        self.assertFalse(core.TEST_CASE_LINE.match('+    def helper(self):'))


class FailureSignatureTest(unittest.TestCase):
    """The failing source lines of a CI log: what tells a failure that moved from one that did not."""

    def test_a_playwright_frame_gives_its_failing_line_once_across_attempts(self):
        log = playwright_log("expect(produced.status).toBe('in_production');")
        self.assertEqual(core.failure_signature(log), ["expect(produced.status).toBe('in_production');"])

    def test_a_vitest_frame_gives_its_failing_line(self):
        log = (' FAIL  test/thing.test.ts > thing > works\nAssertionError: expected 1 to be 2\n'
               ' ❯ test/thing.test.ts:4:13\n      2| \n      3| it(\'works\', () => {\n'
               '      4|   expect(1).toBe(2);\n       |             ^\n      5| });\n')
        self.assertEqual(core.failure_signature(log), ['expect(1).toBe(2);'])

    def test_lines_are_sorted_and_passing_test_ids_ignored(self):
        log = playwright_log('expect(b).toBe(1);', 'expect(a).toBe(1);', passing=40)
        self.assertEqual(core.failure_signature(log), ['expect(a).toBe(1);', 'expect(b).toBe(1);'])
        self.assertEqual(core.failure_signature(playwright_log('expect(a).toBe(1);', line=9, passing=0)),
                         ['expect(a).toBe(1);'], 'its line number and neighbours do not count')

    def test_a_log_without_a_frame_has_no_signature(self):
        self.assertEqual(core.failure_signature('src/thing.ts(3,1): error TS2322: Type string is not number.\n'), [])
        self.assertEqual(core.failure_signature('      4|   expect(1).toBe(2);\n      5| });\n'), [])
        self.assertEqual(core.failure_signature(''), [])
        self.assertEqual(core.failure_signature(None), [])


DNS_FAILURE = urllib.error.URLError(socket.gaierror(-3, 'Temporary failure in name resolution'))


class GitHubApiTest(unittest.TestCase):
    """A network failure is tried twice more; a GitHub status never is."""

    def setUp(self):
        patch = mock.patch.object(core, 'GITHUB_RETRY_SECONDS', (0, 0))
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, *answers, method='GET'):
        with mock.patch.object(core.urllib.request, 'urlopen', side_effect=answers) as urlopen:
            try:
                return core.github_api(method, '/x', 'ghs_test', {} if method == 'POST' else None)
            finally:
                self.calls = urlopen.call_count

    def test_a_failed_lookup_is_tried_again(self):
        self.assertEqual(self.call(DNS_FAILURE, io.BytesIO(b'{"ok": true}')), {'ok': True})
        self.assertEqual(self.calls, 2)

    def test_a_get_whose_answer_breaks_off_is_tried_again(self):
        self.assertEqual(self.call(http.client.IncompleteRead(b''), io.BytesIO(b'{"ok": true}')), {'ok': True})

    def test_a_post_whose_answer_times_out_is_not_sent_again(self):
        with self.assertRaisesRegex(core.PipelineError, 'GitHub POST /x: timed out'):
            self.call(TimeoutError('timed out'), io.BytesIO(b'{}'), method='POST')

        self.assertEqual(self.calls, 1)

    def test_an_http_status_is_not_tried_again(self):
        bad = urllib.error.HTTPError('https://api.github.com/x', 502, 'Bad Gateway', {}, io.BytesIO(b'oops'))

        with self.assertRaisesRegex(core.PipelineError, 'GitHub GET /x: 502 oops'):
            self.call(bad, io.BytesIO(b'{}'))

        self.assertEqual(self.calls, 1)

    def test_three_failed_lookups_are_a_pipeline_error(self):
        with self.assertRaisesRegex(core.PipelineError, 'GitHub GET /x: .*Temporary failure in name resolution'):
            self.call(DNS_FAILURE, DNS_FAILURE, DNS_FAILURE)

        self.assertEqual(self.calls, 3)


class GitHubRawTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(core, 'GITHUB_RETRY_SECONDS', (0, 0))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_network_error_on_the_first_request_is_a_pipeline_error(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError('timed out')

        with mock.patch.object(core.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(core.PipelineError, 'GitHub GET /x: .*timed out'):
                core.github_raw('/x', 'ghs_test')

    def test_a_failed_lookup_on_either_request_is_tried_again(self):
        opener = mock.Mock()
        redirect = urllib.error.HTTPError('https://api.github.com/x', 302, 'Found', {'Location': 'https://logs/x'}, None)
        opener.open.side_effect = [DNS_FAILURE, redirect]

        with mock.patch.object(core.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(core.urllib.request, 'urlopen', side_effect=[DNS_FAILURE, io.BytesIO(b'the log')]):
            self.assertEqual(core.github_raw('/x', 'ghs_test'), 'the log')

        self.assertEqual(opener.open.call_count, 2)


# The failed step of shop job 113602765777 (2026-10-09), when the runner host's DNS stalled, then the job's teardown.
DNS_LOG = '\n'.join(f'2026-10-09T00:10:{second:02}.1234567Z {line}' for second, line in enumerate([
    'Running 214 tests using 4 workers',
    '  1) [chromium] › e2e/adapter-design-files.spec.ts:18:3 › adapter design files › lists a drawing',
    '    Error: apiRequestContext.put: getaddrinfo ENOTFOUND ***.***.r2.cloudflarestorage.com',
    '    Call log:',
    '      - → PUT https://***.***.r2.cloudflarestorage.com/shop-e2e/fixtures/drawing.pdf',
    '      at uploadFile (/home/runner/work/shop/shop/apps/web/e2e/fixtures/catalog-file-fixtures.ts:35:3)',
    '  1 failed',
    '  213 passed (4.1m)',
    '\x1b[31m ELIFECYCLE \x1b[39m Command failed with exit code 1.',
    '##[error]Process completed with exit code 1.',
    'Post job cleanup.',
    'Stop and remove container: 1b2c3d_postgres',
    'Cleaning up orphan processes',
]))


class CiInfraTest(RunnerCase):
    """Red CI that is not the code's fault (a timeout, a missing or lost runner, a cancel): rerun it, never a fix."""

    def timed_out(self, sha, attempt=1):
        """PR #245's `test` timeout, as attempt `attempt` of its run (a rerun gives the jobs new ids)."""
        jobs = [dict(job, id=job['id'] + (attempt - 1) * 100_000) for job in TIMEOUT_JOBS]
        self.actions(sha, dict(TIMEOUT_RUN, run_attempt=attempt), jobs, {jobs[0]['id']: TIMEOUT_ANNOTATIONS},
                     keep=attempt > 1)
        self.github.missing_logs.add(f"/repos/acme/shop/actions/jobs/{jobs[0]['id']}/logs")

    def reruns(self):
        return [path for method, path, _, _ in self.github.calls if method == 'POST' and path.endswith('/rerun-failed-jobs')]

    def fixed(self, *more):
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        self.happy_path(**{'thing-01-respond-1': [RESPOND, *(more or [fix])]})
        self.main(FEATURE)

        return self.state()['published_sha']

    def test_a_timed_out_job_is_rerun_not_fixed_and_its_green_rerun_is_ready_for_review(self):
        sha = self.published()['published_sha']
        self.timed_out(sha)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(state['reason'], 'PR #5 https://example/pull/5: CI test not a code failure (The job has exceeded '
                                          'the maximum execution time of 20m0s): rerun 1/2')
        self.assertEqual(self.reruns(), ['/repos/acme/shop/actions/runs/37615714066/rerun-failed-jobs'])
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 1))
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1, 'no fix round')
        self.assertNotIn('RAW', [call[0] for call in self.github.calls], 'a rerun reads no log')
        history = len(state['history'])
        self.main(FEATURE)
        self.assertEqual(len(self.reruns()), 1, 'a rerun in flight is waited for')
        self.assertEqual(len(self.state()['history']), history, 'waiting records nothing')
        green = [dict(job, id=job['id'] + 100_000, conclusion='success', steps=[step('Set up job')]) for job in TIMEOUT_JOBS]
        self.actions(sha, dict(TIMEOUT_RUN, run_attempt=2), green, keep=True)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['ci']['reviewed'], sha, state['reason'])
        self.assertFalse(self.github.pull(5)['draft'])
        self.assertIn('0 CI fixes, 1 CI rerun', self.github.comments[1000])
        self.main('show', FEATURE)
        self.assertIn('(0/2 fixes, 1 CI rerun)', self.output)
        self.main('status', '-v')
        self.assertIn('PR #5 CI green (0/2 fixes, 1 CI rerun)', self.output)

    def test_a_job_that_keeps_ending_without_a_code_failure_pauses_after_two_reruns(self):
        sha = self.published()['published_sha']

        for attempt in (1, 2, 3):
            self.timed_out(sha, attempt)
            self.main(FEATURE)

        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertEqual(state['reason'], 'PR #5: CI test ended without a code failure 3 times (The job has exceeded the '
                                          'maximum execution time of 20m0s). Could be a slow or hung test, or the runner; '
                                          "no fix was tried: check the job's timeout-minutes or the runner, re-run it on "
                                          f'GitHub, then `pipeline {FEATURE}`.')
        self.assertEqual(self.notifications[-1], 'paused: PR #5 CI not code')
        self.assertEqual(len(self.reruns()), 2)
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 2))
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1)

    def test_a_refused_rerun_pauses_at_once_without_a_fix(self):
        sha = self.published()['published_sha']
        self.github.refuse_rerun = '403 {"message":"Resource not accessible by integration"}'
        self.timed_out(sha)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn('PR #5: CI test ended without a code failure (The job has exceeded the maximum execution time of '
                      '20m0s) and GitHub refused the rerun (GitHub POST /repos/acme/shop/actions/runs/37615714066/'
                      'rerun-failed-jobs: 403', state['reason'])
        self.assertIn(f'the App needs Actions: write; re-run it on GitHub, then `pipeline {FEATURE}`.', state['reason'])
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 0))
        self.github.refuse_rerun = '500 oops'
        self.main(FEATURE)
        self.assertNotIn('Actions: write', self.state()['reason'])

    def test_without_actions_write_it_pauses_for_a_rerun_by_hand_then_picks_it_up(self):
        self.github.actions_permission = 'read'
        sha = self.published()['published_sha']
        self.timed_out(sha)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertEqual(state['reason'], 'PR #5: CI test ended without a code failure (The job has exceeded the maximum '
                                          'execution time of 20m0s). No fix was tried, and auto-rerun is off (the App has '
                                          f'Actions: read): re-run it on GitHub, then `pipeline {FEATURE}`.')
        self.assertEqual(self.notifications[-1], 'paused: PR #5 CI not code')
        self.assertEqual(self.reruns(), [])
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 0))
        green = [dict(job, id=job['id'] + 100_000, conclusion='success', steps=[step('Set up job')]) for job in TIMEOUT_JOBS]
        self.actions(sha, dict(TIMEOUT_RUN, run_attempt=2), green, keep=True)
        self.main(FEATURE)
        self.assertEqual(self.state()['ci']['reviewed'], sha, self.state()['reason'])

    def test_actions_billing_pauses_at_once_without_a_rerun(self):
        sha = self.published()['published_sha']
        run = dict(TIMEOUT_RUN, conclusion='failure')
        self.actions(sha, run, BILLING_JOBS + [dict(job(112867740807, 'test', 'failure', '2026-10-07T15:22:41Z', None))],
                     {112867740806: BILLING_ANNOTATIONS})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['status'], 'paused', state['reason'])
        self.assertIn('PR #5: CI check, test did not run: GitHub Actions billing (The job was not started because recent '
                      'account payments have failed', state['reason'])
        self.assertIn(f"fix the org's Actions billing or spending limit, re-run it on GitHub, then `pipeline {FEATURE}`.",
                      state['reason'])
        self.assertEqual(self.notifications[-1], 'paused: PR #5 CI billing')
        self.assertEqual(self.reruns(), [])
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 0))

    def test_a_code_failure_without_a_job_log_is_fixed_from_the_check_output(self):
        sha = self.fixed()
        self.ci(sha, 'success', 'failure')
        self.github.missing_logs.add('/repos/acme/shop/actions/jobs/101/logs')
        self.main(FEATURE)
        self.assertIn('test output', self.claude_calls('thing-01-respond-1')[1]['prompt'])
        self.assertEqual(self.reruns(), [])
        self.assertEqual(self.state()['ci']['fixes'], 1)

    def test_a_run_superseded_on_the_same_commit_waits_for_the_newer_one(self):
        state = self.published()
        sha = state['published_sha']
        newer = dict(SUPERSEDED_RUN, id=SUPERSEDED_RUN['id'] + 1, status='queued', check_suite_id=1)
        self.actions(sha, SUPERSEDED_RUN, SUPERSEDED_JOBS, {112712622693: SUPERSEDED_ANNOTATIONS})
        self.github.workflow_runs[sha].append(newer)
        state['published_at'] = time.time() - core.CI_SILENT_SECONDS - 5
        self.save(state)
        self.main(FEATURE)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(self.reruns(), [])
        self.assertEqual((state['stage'], state['status'], state['ci']['fixes']), ('done', 'done', 0), state['reason'])
        self.assertIn('waiting for CI', state['reason'])
        self.assertEqual([message for message in self.notifications if 'no required check' in message],
                         ['PR #5: no required check (test) started in 30 min'])
        green = [dict(job, id=job['id'] + 1000, conclusion='success', steps=[step('Set up job')]) for job in SUPERSEDED_JOBS]
        self.actions(sha, dict(newer, status='completed'), green, keep=True)
        self.main(FEATURE)
        self.assertEqual(self.state()['ci']['reviewed'], sha)

    def test_a_code_failure_is_fixed_while_its_sibling_still_runs(self):
        sha = self.fixed()
        self.ci(sha, None, 'failure')
        self.main(FEATURE)
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 2, self.state()['reason'])
        self.assertEqual(self.state()['ci']['fixes'], 1)
        self.assertEqual(self.reruns(), [])

    def test_code_and_infra_red_in_two_runs_fix_the_code_and_name_only_it(self):
        sha = self.fixed()
        self.ci(sha, 'failure', 'success')
        self.actions(sha, dict(TIMEOUT_RUN, workflow_id=2), TIMEOUT_JOBS[:1], {112773421896: TIMEOUT_ANNOTATIONS},
                     keep=True)
        self.main(FEATURE)
        prompt = self.claude_calls('thing-01-respond-1')[1]['prompt']
        self.assertIn(f'for {sha[:12]} (check).', prompt)
        self.assertEqual(self.reruns(), [])
        self.assertEqual(self.state()['ci']['fixes'], 1)

    def test_a_code_failure_without_any_log_is_rerun(self):
        sha = self.published()['published_sha']
        self.ci(sha, 'success', 'failure')
        self.github.check_runs[sha][1]['output'] = {}
        self.github.missing_logs.add('/repos/acme/shop/actions/jobs/101/logs')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(self.reruns(), ['/repos/acme/shop/actions/runs/900/rerun-failed-jobs'], state['reason'])
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 1))
        self.assertIn('rerun 1/2', state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1)

    def test_a_failed_step_whose_log_shows_a_dns_failure_is_rerun_not_fixed(self):
        sha = self.published()['published_sha']
        self.ci(sha, 'success', 'failure')
        self.github.logs['/repos/acme/shop/actions/jobs/101/logs'] = DNS_LOG
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(self.reruns(), ['/repos/acme/shop/actions/runs/900/rerun-failed-jobs'])
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (0, 1))
        self.assertIn('CI test not a code failure (network: getaddrinfo ENOTFOUND ***.***.r2.cloudflarestorage.com): '
                      'rerun 1/2', state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1, 'no fix round')

    def test_after_two_reruns_a_dns_failure_goes_to_the_fixer(self):
        sha = self.fixed()
        state = self.state()
        state['ci']['reruns'] = core.CI_RERUNS
        self.save(state)
        self.ci(sha, 'success', 'failure')
        self.github.logs['/repos/acme/shop/actions/jobs/101/logs'] = DNS_LOG
        self.main(FEATURE)
        self.assertIn('getaddrinfo ENOTFOUND', self.claude_calls('thing-01-respond-1')[1]['prompt'])
        self.assertEqual(self.reruns(), [])
        self.assertEqual(self.state()['ci']['fixes'], 1)

    def test_a_dns_flake_in_one_check_does_not_hide_a_real_failure_in_another(self):
        sha = self.fixed()
        self.ci(sha, 'failure', 'failure')
        self.github.logs['/repos/acme/shop/actions/jobs/100/logs'] = DNS_LOG
        self.main(FEATURE)
        prompt = self.claude_calls('thing-01-respond-1')[1]['prompt']
        self.assertIn(f'for {sha[:12]} (test).', prompt)
        self.assertIn('expected 1 to be 2', prompt)
        self.assertNotIn('ENOTFOUND', prompt)
        self.assertEqual(self.reruns(), [])
        self.assertEqual(self.state()['ci']['fixes'], 1)

    def test_a_cancelled_job_after_a_blocked_ci_fix_is_rerun_without_a_second_fix(self):
        stuck = {'result': {'status': 'blocked', 'summary': 'the log shows no cause', 'questions': []}}
        sha = self.fixed(stuck)
        self.ci(sha, 'success', 'failure')
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused', self.state()['reason'])
        self.ci(sha, 'success', 'cancelled')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 2)
        self.assertEqual((state['ci']['fixes'], state['ci']['reruns']), (1, 1))
        self.assertEqual(len(self.reruns()), 1)

    def test_a_new_commit_gets_its_own_reruns(self):
        sha = self.fixed()
        self.timed_out(sha)
        self.main(FEATURE)
        self.assertEqual(self.state()['ci']['reruns'], 1)
        self.ci(sha, 'success', 'failure')
        self.main(FEATURE)
        state = self.state()
        self.assertNotEqual(state['published_sha'], sha, state['reason'])
        self.assertEqual(state['ci'], {'sha': state['published_sha'], 'fixes': 1, 'reviewed': None,
                                       'silent_pinged': False, 'reruns': 0, 'seen': []})

    def test_infra_red_after_main_moved_is_rebased_not_rerun(self):
        before = self.published()['published_sha']
        self.move_main()
        self.timed_out(before)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertNotEqual(state['published_sha'], before)
        self.assertEqual(self.reruns(), [])
        self.assertEqual((state['ci']['fixes'], state['ci'].get('reruns', 0)), (0, 0))

    def test_red_ci_waits_when_github_cannot_list_the_runs(self):
        sha = self.fixed()
        self.ci(sha, 'success', 'failure')
        self.github.runs_error = True
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status'], state['ci']['fixes']), ('done', 'done', 0), state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 1)
        self.assertEqual(self.reruns(), [])


class AdmissionTest(RunnerCase):
    """Admission before every stage, the kill switch, and tick."""

    def test_a_third_run_is_queued_not_waiting(self):
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holders = [subprocess.Popen(['flock', str(self.fixture.base / f'logs/slot-{index}.lock'), 'sleep', '60'],
                                    start_new_session=True) for index in (0, 1)]

        for holder in holders:
            self.addCleanup(holder.kill)

        time.sleep(0.5)
        self.happy_path()
        started = time.monotonic()
        self.assertEqual(self.main(FEATURE), 0)
        self.assertLess(time.monotonic() - started, 10)
        state = self.state()
        self.assertEqual((state['stage'], state['status'], state['reason']), ('plan', 'ready', 'queued: all 2 stage slots busy'))
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.sbx_calls(), [], 'a queued step costs no sandbox')
        self.assertNotIn('queued: all 2 stage slots busy', self.notifications, 'routine: status shows it')
        self.main('status', '-v')
        self.assertIn('stages: 2 running of 2', self.output)

    def test_a_stage_queues_while_the_capped_sbx_daemon_is_down(self):
        self.happy_path()

        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.assertEqual(self.main(FEATURE), 0)
            state = self.state()
            self.assertEqual((state['stage'], state['status'], state['reason']),
                             ('plan', 'ready', 'queued: sbx.service not active (systemctl --user start sbx.service)'))
            self.assertEqual(self.claude_calls(), [])
            self.main('status', '-v')

        self.assertEqual(self.sbx_calls(), [], 'no sbx call (run or status) autostarts an uncapped daemon')
        self.assertIn('template: ? (sbx.service not active)', self.output)
        self.assertIn('stages: 0 running of 2', self.output, 'no slot held')
        self.assertIn('sbx.service: not active — stages queue', self.output)

    def test_status_shows_the_vm_memory_against_the_caps_and_the_shims_inside(self):
        cgroup = self.fixture.base / 'cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/sbx.service'
        write(cgroup / 'memory.current', str(15 * 2**30 + 2**29) + '\n')
        write(cgroup / 'memory.max', str(20 * 2**30) + '\n')
        write(cgroup / 'memory.peak', str(18 * 2**30) + '\n')
        write(cgroup / 'memory.stat', f'anon {12 * 2**30}\nfile {3 * 2**30}\nanon_thp 0\n')
        write(cgroup / 'memory.events', 'low 0\nhigh 42\nmax 0\noom 1\noom_kill 1\noom_group_kill 0\n')
        write(cgroup / 'memory.pressure', 'some avg10=1.00 avg60=2.50 avg300=0.00 total=1\n'
                                          'full avg10=0.50 avg60=1.25 avg300=0.00 total=1\n')
        relative = '/' + str(cgroup.relative_to(self.fixture.base / 'cgroup'))
        proc = self.fixture.base / 'proc'

        for pid, cmdline, group in (('11', b'/usr/bin/containerd-shim-nerdbox-v1\0-id\0a', relative),
                                    ('12', b'/usr/bin/containerd-shim-nerdbox-v1\0-id\0b', relative),
                                    ('13', b'/usr/bin/containerd-shim-nerdbox-v1\0-id\0c', '/user.slice/tmux.scope'),
                                    ('14', b'/usr/bin/sbx\0daemon\0start', relative)):
            write(proc / pid / 'cmdline', cmdline.decode())
            write(proc / pid / 'cgroup', f'0::{group}\n')

        with mock.patch.multiple(core.sbx_host, CGROUP_ROOT=self.fixture.base / 'cgroup', cgroup=lambda: cgroup):
            self.main('status', '-v')

        self.assertIn('sbx.service: VMs use 15.5 GiB (anon 12.0, peak 18.0) of ? soft / 20.0 hard · swap ? of ? · '
                      'oom 1 · pressure full avg60 1.25% · shims 2/3', self.output)

    def test_the_header_shows_reserved_against_the_soft_cap(self):
        cgroup = self.fixture.base / 'cgroup/sbx.service'
        write(cgroup / 'memory.current', str(10 * 2**30) + '\n')
        write(cgroup / 'memory.max', str(19 * 2**30 + 2**29) + '\n')
        write(cgroup / 'memory.high', str(16 * 2**30 + 2**29) + '\n')
        write(self.fixture.sbx / 'meta.json', json.dumps({
            'ci-1-1': {'status': 'running', 'memory_mib': 8192},
            'ci-2-1': {'status': 'running', 'memory_mib': 7168},
            'pl-shop-thing-01': {'status': 'stopped', 'memory_mib': 8192},
        }))

        with mock.patch.multiple(core.sbx_host, CGROUP_ROOT=self.fixture.base / 'cgroup', cgroup=lambda: cgroup), \
                mock.patch.object(core.sbx_host, 'mem_available_gib', return_value=12.34):
            self.main('status')
            memory = self.output.splitlines()[1]
            self.assertEqual(memory, 'memory   VMs use 10.0 GiB of 16.5 soft cap (19.5 hard) · reserved 15.0 GiB: '
                                     'stages 0.0 + CI 15.0 · host free 12.3 GiB (floor 0)', 'stopped VMs hold nothing')
            self.assertNotIn('[', self.output.splitlines()[0])
            write(self.fixture.sbx / 'meta.json', json.dumps({
                'ci-1-1': {'status': 'running', 'memory_mib': 8192},
                'pl-shop-thing-01': {'status': 'running', 'memory_mib': 8192},
                'pl-shop-thing-02': {'status': 'running', 'memory_mib': 8192},
            }))
            self.main('status')
            memory = self.output.splitlines()[1]
            self.assertIn('reserved 24.5 GiB: stages 17.0 + CI 7.5 (over 16.5 by 8.0) · host free', memory)
            self.assertNotIn('[', self.output.splitlines()[0], 'reserved over the budget is no badge')
            write(cgroup / 'memory.high', 'max\n')
            self.main('status')
            memory = self.output.splitlines()[1]
            self.assertIn('VMs use 10.0 GiB (19.5 hard cap) · reserved 24.5 GiB: stages 17.0 + CI 7.5 · host', memory,
                          'no MemoryHigh: no budget to be over')

            write(self.fixture.sbx / 'meta.json', json.dumps({'pl-shop-thing-01': {'status': 'stopped'}}))
            self.main('status')
            self.assertIn('reserved 0.0 GiB · host free', self.output.splitlines()[1])

    def test_low_memory_queues(self):
        self.happy_path()

        with mock.patch.object(core.sbx_host, 'mem_available_gib', return_value=3.5), \
                mock.patch.dict(adapter.DEFAULT_ADMISSION, {}):
            config = json.loads(self.fixture.config.read_text())
            config['admission']['minMemoryGb'] = 10
            self.fixture.config.write_text(json.dumps(config))
            self.main(FEATURE)

        self.assertEqual(self.state()['reason'], 'queued: 3.5 GiB free < 10 GiB')

    def test_high_weekly_usage_queues_and_the_run_ends(self):
        self.usage(0.82)
        self.happy_path()
        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(self.state()['reason'], 'queued: weekly usage 82% ≥ 80%')
        self.assertEqual(self.claude_calls(), [])

    def test_usage_is_checked_before_every_stage(self):
        self.happy_path(**{'thing-01-plan': [dict(PLAN, weekly=0.85)]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['reason']), ('implement', 'queued: weekly usage 85% ≥ 80%'))
        self.assertEqual(self.sandbox_status(), 'stopped')

    def test_stale_usage_is_refreshed_by_a_probe(self):
        self.usage(0.1, age=core.USAGE_STALE_SECONDS + 60)
        probes = []

        def probe():
            probes.append(1)
            core.write_usage({'seven_day': {'utilization': 0.9, 'resetsAt': time.time() + 3600},
                              'five_hour': {'utilization': 0.1, 'resetsAt': time.time() + 3600}})

        with mock.patch.object(core, 'usage_probe', probe):
            self.main(FEATURE)

        self.assertEqual(probes, [1])
        self.assertEqual(self.state()['reason'], 'queued: weekly usage 90% ≥ 80%')

    def test_a_reading_past_its_reset_is_ignored(self):
        self.usage(0.95, resets_in=-60)
        self.happy_path()
        self.main(FEATURE, '--until', 'plan')
        self.assertEqual(self.state()['stage'], 'implement', self.state()['reason'])

    def test_the_usage_probe_reads_the_haiku_stream(self):
        stream = '\n'.join(json.dumps(event) for event in (
            {'type': 'system', 'subtype': 'init', 'model': core.USAGE_PROBE_MODEL},
            {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
                'seven_day': {'utilization': 0.42, 'resetsAt': 2e9}, 'five_hour': {'utilization': 0.1, 'resetsAt': 2e9}}}},
            {'type': 'result', 'subtype': 'success'}))

        with mock.patch.object(core.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stream, '')) as run:
            self.assertIsNone(core.usage_probe())

        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-haiku-4-5-20251001')
        self.assertIn('--no-session-persistence', argv)
        self.assertEqual(core.read_usage()['seven_day']['utilization'], 0.42)

    def test_the_usage_probe_creates_the_state_dir_it_runs_in(self):
        stream = '\n'.join(json.dumps(event) for event in (
            {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
                'seven_day': {'utilization': 0.42, 'resetsAt': 2e9}, 'five_hour': {'utilization': 0.1, 'resetsAt': 2e9}}}},
            {'type': 'result', 'subtype': 'success'}))
        state = self.fixture.base / 'fresh-state'

        def run(argv, **kwargs):
            self.assertTrue(Path(kwargs['cwd']).is_dir())

            return subprocess.CompletedProcess(argv, 0, stream, '')

        with mock.patch.object(core, 'LOGS', state), mock.patch.object(core.subprocess, 'run', side_effect=run):
            self.assertIsNone(core.usage_probe())

    def test_a_repeated_queue_reason_does_not_grow_the_history(self):
        self.usage(0.82)
        self.happy_path()
        self.main(FEATURE)
        self.main(FEATURE)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(sum(1 for entry in state['history'] if entry['reason'].startswith('queued:')), 1)
        self.assertEqual(self.notifications.count('queued: weekly usage 82% ≥ 80%'), 1)

    def test_the_kill_switch_blocks_tick_and_runs(self):
        self.happy_path()
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        core.STOP_FLAG.write_text('now\n')
        self.assertEqual(self.main('tick'), 0)
        self.assertIn('kill switch on', self.output)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.main(FEATURE), 1)
        self.assertEqual(self.claude_calls(), [])

    def test_stop_all_then_clear_resumes_the_same_session_through_tick(self):
        self.happy_path(**{'thing-01-implement': [{'sleep': 30}, IMPLEMENT]})

        def kill_switch():
            core.STOP_FLAG.write_text('now\n')
            os.kill(os.getpid(), signal.SIGTERM)

        timer = threading.Timer(5, kill_switch)
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(self.main(FEATURE), 130)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('implement', 'ready'))
        self.assertIn('stopped by the kill switch', state['reason'])
        self.assertEqual(self.sandbox_status(), 'stopped')
        self.main('tick')
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.main('stop', '--clear'), 0)
        self.assertFalse(core.STOP_FLAG.exists())
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        self.main(FEATURE, '--until', 'implement')
        first, second = self.claude_calls('thing-01-implement')
        self.assertTrue(second['resume'])
        self.assertEqual(second['session'], first['session'])
        self.assertEqual(self.state()['stage'], 'rebase', self.state()['reason'])

    def test_stop_all_signals_only_held_pipeline_locks_and_stops_sandboxes(self):
        logs = self.fixture.base / 'logs'
        logs.mkdir(exist_ok=True)
        holder = subprocess.Popen([sys.executable, '-c', (
            'import fcntl, os, sys, time\n'
            f"handle = open({str(logs / 'run-shop-other.lock')!r}, 'a+')\n"
            'fcntl.flock(handle, fcntl.LOCK_EX)\n'
            'handle.write(str(os.getpid())); handle.flush()\n'
            'time.sleep(60)  # pipeline run stand-in\n')])
        self.addCleanup(holder.kill)
        bystander = subprocess.Popen(['sleep', '60'])
        self.addCleanup(bystander.kill)
        (logs / 'run-shop-stale.lock').write_text(str(bystander.pid))
        time.sleep(0.8)
        subprocess.run(['sbx', 'create', '--name', 'pl-shop-other-01', '--clone', 'claude', str(self.fixture.origin)],
                       check=True, capture_output=True)
        subprocess.run(['sbx', 'create', '--name', 'unrelated', '--clone', 'claude', str(self.fixture.origin)],
                       check=True, capture_output=True)
        units = []

        with mock.patch.object(core, 'stop_units', lambda: units.append(1)):
            self.assertEqual(self.main('stop', '--all'), 0)

        self.assertTrue(core.STOP_FLAG.exists())
        self.assertEqual(units, [1])
        self.assertEqual(holder.wait(10), -signal.SIGTERM)
        self.assertIsNone(bystander.poll(), 'a pid in an unheld lock file is never signalled')
        self.assertEqual(self.sandbox_status('pl-shop-other-01'), 'stopped')
        self.assertEqual(self.sandbox_status('unrelated'), 'running')
        subprocess.run(['sbx', 'rm', '--force', 'pl-shop-other-01'], capture_output=True)
        subprocess.run(['sbx', 'rm', '--force', 'unrelated'], capture_output=True)

    def test_stop_all_makes_no_sbx_call_while_the_capped_daemon_is_down(self):
        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.assertEqual(self.main('stop', '--all'), 0)

        self.assertTrue(core.STOP_FLAG.exists())
        self.assertIn('no sandbox runs (sbx.service not active)', self.output)
        self.assertEqual(self.sbx_calls(), [], 'the cutover must not autostart the daemon that blocks the unit')


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class FakeKeyboard:
    """A terminal's input side: a pipe the test writes keys into."""

    def __init__(self, test):
        self.read_end, self.write_end = os.pipe()
        test.addCleanup(os.close, self.read_end)
        test.addCleanup(os.close, self.write_end)

    def isatty(self):
        return True

    def fileno(self):
        return self.read_end

    def press(self, keys):
        os.write(self.write_end, keys)


class ViewTest(RunnerCase):
    """The CLI's face: help, the compact status, --watch and show."""

    def hold_run_lock(self, feature=FEATURE):
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/run-shop-{feature}.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)

        return holder

    def feature(self, name, *step_names):
        for step_name in step_names or ('01-x',):
            write(self.fixture.plans / name / step_name / 'brief.md', '# x\n')

    def test_help_groups_the_commands_and_each_command_answers_dash_h(self):
        for argv in ((), ('-h',), ('help',)):
            self.assertEqual(self.main(*argv), 0)
            self.assertIn('DAILY\n', self.output)
            self.assertIn('  e.g. pipeline status --watch', self.output)
            self.assertIn('pipeline <feature> [--repo <o>/<r>]', self.output)

        for command in core.COMMANDS:
            self.assertEqual(self.main(command, '--help'), 0, command)
            self.assertTrue(self.output.startswith(f'usage: pipeline {command}'), self.output)

        self.assertNotIn('slots', self.output, '`status --help` prints help, never the status')
        self.assertEqual(self.main('stop', '--bogus'), 2)
        self.assertEqual(self.errors, 'usage: pipeline stop --all | --clear\n')

    def test_a_typo_gets_a_suggestion_but_never_hides_a_feature(self):
        self.assertEqual(self.main('stauts'), 2)
        self.assertEqual(self.errors, "no feature or command 'stauts'; did you mean: status, start?\n")
        self.assertEqual(self.main('help', 'stauts'), 2, 'the same exit as help')
        self.assertEqual(self.main('zzz'), 2)
        self.assertIn('pipeline help lists the commands', self.errors)
        self.feature('stat')
        self.assertEqual(self.main('stat', '--detach'), 0, self.errors)
        self.assertEqual(self.spawned, [(REPO, 'stat')])

    def test_a_queued_run_exits_0(self):
        self.happy_path(**{'thing-01-plan': [dict(PLAN, weekly=0.85)]})
        self.assertEqual(self.main(FEATURE), 0, 'queued is a handled outcome')
        self.assertEqual(self.state()['reason'], 'queued: weekly usage 85% ≥ 80%')

    def test_approve_and_revise_say_so_and_kick_tick(self):
        write(self.fixture.step / 'plan.md', '# plan\n')
        core.mark(self.state(), 'awaiting-approval', 'The plan asks you')
        self.assertEqual(self.main('approve', FEATURE), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE} {STEP}: approved; tick continues it now\n')
        self.assertEqual(self.kicks, [1])
        core.mark(self.state(), 'awaiting-approval', 'The plan asks you')
        self.assertEqual(self.main('revise', FEATURE), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE} {STEP}: sent back; tick resumes the plan\n')
        self.assertEqual(self.kicks, [1, 1])
        self.assertEqual(self.main('approve', FEATURE), 1)
        self.assertEqual(self.kicks, [1, 1], 'nothing to approve: no kick')

    def test_extra_arguments_are_refused(self):
        for argv in (('approve', FEATURE, 'x'), ('revise', FEATURE, 'x'), ('respond', FEATURE, 'x'), ('tick', 'x'),
                     ('gc', 'now'), ('prove-app', 'x'), ('prove-gate', 'x')):
            self.assertEqual(self.main(*argv), 2, argv)
            self.assertIn('usage: pipeline', self.errors)

        self.assertEqual(self.spawned, [])

    def test_order_takes_repo(self):
        self.feature('other')
        self.assertEqual(self.main('order', 'other', '--repo', REPO), 0, self.errors)
        self.assertEqual(core.global_control()['order'], [f'{REPO}/other'])
        self.assertEqual(self.main('order', 'other', '--repo'), 2)

    def test_a_corrupt_state_file_is_one_error_line(self):
        state = self.state()
        core.mark(state, 'ready', 'queued: x')
        path = core.state_file(REPO, FEATURE, '01')
        path.write_text('{"broken')

        for argv in (('tick',), ('status', '-v'), ('show', FEATURE), ('gc',)):
            self.assertEqual(self.main(*argv), 1, argv)
            self.assertRegex(self.errors, rf'^{re.escape(str(path))}: unreadable \(.+\); fix or delete it\n$', argv)

        self.assertEqual(self.main('status'), 0, 'the compact status lists it as unreadable')
        self.assertIn('unreadable', self.output)

    def test_log_follow_ends_when_the_step_merges(self):
        write(core.state_dir(REPO, FEATURE) / '01-gates.log', '== 2026-10-02T20:00:00 check → exit 0\n')
        core.mark(self.state(), 'running', 'implement running')

        def merge_soon():
            time.sleep(1.5)
            core.mark(self.state(), 'closed', 'merged as PR #5')

        threading.Thread(target=merge_soon, daemon=True).start()
        self.assertEqual(self.main('log', FEATURE, '-f'), 0)
        self.assertTrue(self.output.endswith(f'{FEATURE} {STEP}: merged\n'), self.output)

    def test_stop_clear_kicks_tick_and_a_held_lock_names_its_pid(self):
        write(self.fixture.base / 'logs/STOP', 'x\n')
        self.assertEqual(self.main('stop', '--clear'), 0)
        self.assertEqual(self.kicks, [1])
        holder = subprocess.Popen(['flock', str(self.fixture.base / 'logs/tick.lock'), 'sh', '-c',
                                   f"echo 4242 > {self.fixture.base / 'logs/tick.lock'}; sleep 60"],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.assertEqual(self.main('tick'), 1)
        self.assertEqual(self.errors, 'another pipeline process (pid 4242) holds tick; pipeline status shows what runs\n')

    def test_a_folder_named_like_a_command_is_never_a_feature(self):
        self.feature('show')
        self.assertEqual(adapter.all_features(), [(REPO, FEATURE)])
        warning = f"! {self.fixture.plans / 'show'}: named like a pipeline command, so never run; rename it"
        self.main('status')
        self.assertIn(warning, self.output)
        self.main('status', '-v')
        self.assertIn(warning, self.output)
        self.main('tick')
        self.assertIn(f'tick: {warning}', self.output)
        self.assertEqual(self.spawned, [(REPO, FEATURE)])

    def test_the_compact_status_groups_features_by_what_they_need(self):
        self.feature('asks', '01-a', '02-b')
        self.feature('green')
        self.feature('busy')
        self.feature('limited')
        self.feature('older')
        self.feature('merged')
        asks = core.new_state(REPO, 'asks', '02')
        asks['results']['plan'] = {'questions': ['Q1?', 'Q2?']}
        core.mark(asks, 'awaiting-approval', 'The plan asks you')
        closed = core.new_state(REPO, 'asks', '01')
        core.mark(closed, 'closed', 'merged as PR #3')
        green = core.new_state(REPO, 'green', '01')
        green.update(stage='done', pr={'number': 9, 'url': 'https://example/pull/9'}, published_sha='abc',
                     ci={'reviewed': 'abc', 'fixes': 0})
        core.mark(green, 'done', 'PR #9: CI green')
        busy = core.new_state(REPO, 'busy', '01')
        busy['stage'] = 'implement'
        core.mark(busy, 'running', 'implement running')
        workspace = str(adapter.clone_path(REPO))
        write(core.state_dir(REPO, 'busy') / '01-implement.log', '\n'.join(json.dumps(event) for event in (
            {'type': 'assistant', 'timestamp': '2026-10-02T18:51:34.442Z', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': f'{workspace}/apps/web/src/card.tsx'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}})) + '\n')
        self.hold_run_lock('busy')
        limited = core.new_state(REPO, 'limited', '01')
        limited['not_before'] = time.time() + 3600
        core.mark(limited, 'ready', 'implement limit (seven_day) until Fri 14:00: x. Tick resumes it then.')
        older = core.new_state(REPO, 'older', '01')
        older['reason'] = 'queued: all 2 stage slots busy'
        older['history'] = [{'at': '2026-09-29T10:00:00', 'stage': 'plan', 'status': 'ready', 'reason': older['reason']}]
        self.save(older)
        merged = core.new_state(REPO, 'merged', '01')
        core.mark(merged, 'closed', 'merged as PR #4')
        merged['merged'] = {'number': 4}
        self.save(merged)
        core.mark(self.state(), 'ready', 'queued: weekly usage 82% ≥ 80%')
        self.assertEqual(self.main('status'), 0)
        lines = self.output.splitlines()
        self.assertRegex(lines[0], r'^pipeline \d\d:\d\d  claude weekly 20% · 5h 10% · stages 0 running of 2$')
        at = time.strftime('%H:%M', time.localtime(core.parse_iso('2026-10-02T18:51:34Z')))
        self.assertEqual(lines[1:], [
            'NEEDS YOU',
            ' ✋ asks    02/02  plan asks 2 → pipeline answer asks',
            ' ✋ green   01/01  PR #9 green → review & merge  https://example/pull/9',
            'RUNNING',
            f' ▶ busy    01/01  implement  0m  {at} Edit apps/web/src/card.tsx',
            'QUEUED',
            ' 1 older   01/01  plan · all 2 stage slots busy',
            f' 2 {FEATURE}   01/01  plan · weekly usage 82% ≥ 80%',
            'WAITING  limited (limit until Fri 14:00)',
        ])
        self.main('status', '--all')
        self.assertIn('MERGED  merged (PR #4)', self.output)

    def test_the_status_header_badges_only_what_is_abnormal(self):
        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.main('status')

        self.assertIn('[sbx down: systemctl --user start sbx.service]', self.output.splitlines()[0])
        self.assertEqual(self.sbx_calls(), [], 'status never autostarts an uncapped daemon')
        write(self.fixture.base / 'logs/STOP', 'x\n')
        self.templates(tag_of('2.1.284'))
        self.main('status')
        self.assertTrue(self.output.splitlines()[0].endswith('[KILL SWITCH ON: pipeline stop --clear]'),
                        'build auto: the next create builds it')
        self.configure(build='manual')
        self.main('status')
        self.assertIn('[KILL SWITCH ON: pipeline stop --clear] [template missing]', self.output.splitlines()[0])
        self.configure()
        core.write_template_marker({'tag': TAG, 'failures': 2, 'at': time.time(), 'error': 'x'})
        self.templates(tag_of('2.1.284'))
        self.main('status')
        self.assertIn('[template failed 2×]', self.output.splitlines()[0])
        core.STOP_FLAG.unlink()
        self.templates(TAG)
        self.main('status')
        self.assertNotIn('[', self.output.splitlines()[0])

    def test_a_dead_run_waits_for_tick_and_colour_follows_the_terminal(self):
        core.mark(self.state(), 'running', 'plan running')
        model = core.status_model()
        self.assertEqual([line for line in core.render(model) if 'run died' in line],
                         [f'WAITING  {FEATURE} (plan: run died, tick resumes it)'])
        link = core.render({**model, 'rows': [dict(model['rows'][0], group='needs', text='PR #9 green',
                                                   link='https://example/pull/9')]}, color=True, width=40)[2]
        self.assertIn('\x1b]8;;https://example/pull/9\x1b\\PR #9\x1b]8;;\x1b\\ green', link)
        self.assertNotIn('  https://', link, 'the URL is left out when it does not fit; the link carries it')
        coloured = core.render(model, color=True, width=30)
        self.assertIn('\x1b[33m', coloured[-1])
        self.assertTrue(all(core.text_width(re.sub(r'\x1b\[[0-9;]*m', '', line)) <= 30 for line in coloured))
        terminal = FakeTerminal()

        with mock.patch.dict(os.environ, {'NO_COLOR': '', 'TERM': 'xterm'}):
            self.assertTrue(core.use_color(terminal))

        with mock.patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertFalse(core.use_color(terminal))

        with mock.patch.dict(os.environ, {'NO_COLOR': '', 'TERM': 'dumb'}):
            self.assertFalse(core.use_color(terminal))

        self.assertFalse(core.use_color(io.StringIO()))
        self.main('status')
        self.assertNotIn('\x1b[', self.output, 'no colour into a pipe')

    def test_clip_counts_wide_characters(self):
        self.assertEqual(core.clip(' ✋ abcdef', 6), ' ✋ a…')
        self.assertEqual(core.clip('short', 10), 'short')
        self.assertEqual(core.clip('x' * 50, None), 'x' * 50)

    def watch(self, keyboard, terminal, **patches):
        """command_watch on a fake terminal; self.restored collects the terminal modes written back."""
        self.restored = []

        with mock.patch.multiple(core.termios, tcgetattr=lambda fd: ['saved'],
                                 tcsetattr=lambda fd, when, attributes: self.restored.append(attributes)), \
                mock.patch.object(core.tty, 'setcbreak', lambda fd: None), \
                mock.patch.object(core.shutil, 'get_terminal_size', return_value=os.terminal_size((60, 20))), \
                mock.patch.multiple(core, **patches) if patches else contextlib.nullcontext():
            return core.command_watch(False, 0.1, stdin=keyboard, stdout=terminal)

    def test_watch_redraws_in_place_until_q_and_restores_the_terminal(self):
        keyboard, terminal = FakeKeyboard(self), FakeTerminal()

        def quit_after_two_frames():
            deadline = time.monotonic() + 30

            while terminal.getvalue().count('\x1b[H') < 2 and time.monotonic() < deadline:
                time.sleep(0.05)

            keyboard.press(b'xq')

        threading.Thread(target=quit_after_two_frames, daemon=True).start()
        self.assertEqual(self.watch(keyboard, terminal), 0)
        output = terminal.getvalue()
        self.assertEqual(self.restored, [['saved']])
        self.assertTrue(output.startswith(core.WATCH_ENTER + '\x1b[H'))
        self.assertTrue(output.endswith(core.WATCH_LEAVE))
        self.assertGreaterEqual(output.count('\x1b[H'), 2, 'redrawn more than once')
        self.assertIn('every 0.1s · q quits', output)

    def test_watch_restores_the_terminal_when_a_frame_fails_or_on_ctrl_c(self):
        def interrupted(*arguments):
            raise KeyboardInterrupt  # Ctrl+C, and SIGTERM/SIGHUP through with_stop_signals

        def broken(*arguments):
            raise RuntimeError('boom')

        terminal = FakeTerminal()
        self.assertEqual(self.watch(FakeKeyboard(self), terminal, status_model=interrupted), 0)
        self.assertTrue(terminal.getvalue().endswith(core.WATCH_LEAVE))
        self.assertEqual(self.restored, [['saved']])
        terminal = FakeTerminal()

        with self.assertRaises(RuntimeError):
            self.watch(FakeKeyboard(self), terminal, status_model=broken)

        self.assertTrue(terminal.getvalue().endswith(core.WATCH_LEAVE))
        self.assertEqual(self.restored, [['saved']])

    def test_watch_needs_a_terminal(self):
        self.assertEqual(self.main('status', '--watch'), 2)
        self.assertIn('needs a terminal', self.errors)

    def test_watch_verbose_reuses_probes(self):
        keyboard, terminal = FakeKeyboard(self), FakeTerminal()
        readings = []
        reading = core.sbx_host.reading

        def counted():
            readings.append(1)
            return reading()

        def quit_after_three_frames():
            deadline = time.monotonic() + 30

            while terminal.getvalue().count('\x1b[H') < 3 and time.monotonic() < deadline:
                time.sleep(0.05)

            keyboard.press(b'q')

        threading.Thread(target=quit_after_three_frames, daemon=True).start()

        with mock.patch.object(core.sbx_host, 'reading', counted), \
                mock.patch.object(core.shutil, 'get_terminal_size', return_value=os.terminal_size((200, 20))), \
                mock.patch.multiple(core.termios, tcgetattr=lambda fd: ['saved'], tcsetattr=lambda *arguments: None), \
                mock.patch.object(core.tty, 'setcbreak', lambda fd: None):
            self.assertEqual(core.command_watch(False, 0.1, True, stdin=keyboard, stdout=terminal), 0)

        self.assertGreaterEqual(terminal.getvalue().count('\x1b[H'), 3)
        self.assertIn('stages: 0 running of 2', terminal.getvalue())
        self.assertEqual(readings, [1], 'the host is read once per PROBE_SECONDS, not every frame')

    def test_ci_vms_show_in_their_own_line(self):
        self.main('status')
        self.assertNotIn('CI ', self.output, 'no ci-runner unit and no CI VM: no line')
        write(self.fixture.sbx / 'meta.json', json.dumps({
            'ci-1-1': {'status': 'running', 'memory_mib': 7168}, 'ci-2-1': {'status': 'running', 'memory_mib': 7168}}))
        write(self.fixture.base / 'ci-runner/slot-1.json', json.dumps({'state': 'busy'}))

        with mock.patch.object(core, 'ci_units', return_value={1: True, 2: True, 3: False}):
            self.main('status')

        self.assertIn('CI       ci-1 busy · ci-2 VM up · ci-3 unit inactive   (ci-runner status)', self.output)
        self.main('status')
        self.assertIn('CI       ci-1 unit inactive (VM left) · ci-2 unit inactive (VM left)', self.output)

    def test_pressure_badge_names_the_fix(self):
        cgroup = self.fixture.base / 'cgroup/sbx.service'
        write(cgroup / 'memory.current', str(10 * 2**30) + '\n')
        write(cgroup / 'memory.pressure', 'some avg10=0 avg60=3.00 avg300=0 total=1\nfull avg10=0 avg60=0.99 avg300=0 '
                                          'total=1\n')

        with mock.patch.multiple(core.sbx_host, CGROUP_ROOT=self.fixture.base / 'cgroup', cgroup=lambda: cgroup):
            self.main('status')
            self.assertNotIn('[', self.output.splitlines()[0], 'below 1%: no badge')
            write(cgroup / 'memory.pressure', 'full avg10=0 avg60=2.50 avg300=0 total=1\n')
            self.main('status')

        self.assertTrue(self.output.splitlines()[0].endswith('[memory pressure 2.5%: pipeline parallel 1]'),
                        self.output)

    def test_stages_queue_badge_only_with_queued_rows_and_no_duplicate(self):
        with mock.patch.object(core.sbx_host, 'mem_available_gib', return_value=9.99), \
                mock.patch.dict(adapter.DEFAULT_ADMISSION, {}):
            config = json.loads(self.fixture.config.read_text())
            config['admission']['minMemoryGb'] = 10
            self.fixture.config.write_text(json.dumps(config))
            core.mark(self.state(), 'paused', 'x')
            self.main('status')
            self.assertNotIn('stages queue', self.output, 'nothing queued')
            core.mark(self.state(), 'ready', 'queued: 9.9 GiB free < 10 GiB')
            self.main('status')
            self.assertTrue(self.output.splitlines()[0].endswith('[stages queue: 9.9 GiB free < 10 GiB]'), self.output)
            write(self.fixture.base / 'logs/STOP', 'x\n')
            self.main('status')

        self.assertTrue(self.output.splitlines()[0].endswith('[KILL SWITCH ON: pipeline stop --clear]'), self.output)

    def test_status_never_probes_usage(self):
        self.usage(0.95, age=core.USAGE_STALE_SECONDS + 60)
        core.mark(self.state(), 'ready', 'queued: weekly usage 95% ≥ 80%')

        with mock.patch.object(core, 'usage_probe', side_effect=AssertionError('probed')):
            self.assertEqual(self.main('status', '-v'), 0)
            self.assertEqual(self.main('status'), 0)

        self.assertIn('(stale)', self.output.splitlines()[0])
        self.assertTrue(self.output.splitlines()[0].endswith('[stages queue: weekly usage 95% ≥ 80%]'), self.output)

    def test_show_gives_one_feature_in_full(self):
        state = self.state()
        core.mark(state, 'paused', 'sbx create failed: x\nfix: sbx ls\n\nlong tail')
        write(self.fixture.step / 'plan.md', '# plan\n')
        self.assertEqual(self.main('show', FEATURE), 0, self.errors)
        self.assertIn(f' → {STEP}  plan [paused]  sbx create failed: x', self.output)
        self.assertIn('  fix: sbx ls\n', self.output)
        self.assertIn('  long tail\n', self.output)
        self.assertIn(f'next: pipeline {FEATURE} --detach', self.output)
        self.assertIn('files: brief.md, plan.md (', self.output)
        self.assertIn('history:\n', self.output)
        self.assertEqual(self.main('show'), 2)
        self.assertEqual(self.main('show', 'nope'), 1)


class QueueTest(RunnerCase):
    """Your queue controls: pause/start (per feature and drain), order, parallel, and tick's free-slot starts."""

    def feature(self, name, *step_names):
        for step_name in step_names or ('01-x',):
            write(self.fixture.plans / name / step_name / 'brief.md', '# x\n')

    def queued(self, name, at):
        state = core.new_state(REPO, name, '01')
        state['reason'] = 'queued: x'
        state['history'] = [{'at': at, 'stage': 'plan', 'status': 'ready', 'reason': 'queued: x'}]
        self.save(state)

    def hold(self, path):
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(path), 'sleep', '60'], start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)

        return holder

    def slots(self, *indexes):
        for index in indexes:
            self.hold(self.fixture.base / f'logs/slot-{index}.lock')

    def three_queued(self):
        for name, at in (('a', '2026-09-29T10:00:00'), ('b', '2026-09-29T11:00:00'), ('c', '2026-09-29T12:00:00')):
            self.feature(name)
            self.queued(name, at)

        self.queued(FEATURE, '2026-09-29T13:00:00')

    def test_tick_starts_only_as_many_as_slots_are_free_in_order(self):
        self.three_queued()
        self.slots(0)
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, 'a')], 'two slots, one held: one start')
        self.assertIn('tick: 3 queued (1 free slots)', self.output)
        self.spawned.clear()
        self.assertEqual(self.main('order', 'c', FEATURE), 0, self.errors)
        self.assertEqual(core.global_control()['order'], [f'{REPO}/c', f'{REPO}/{FEATURE}'])
        self.assertRegex(self.output, rf'QUEUED\n 1 c +01/01  plan · x\n 2 {FEATURE} +01/01  plan · x\n 3 a ')
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, 'c')])

    def test_a_run_still_fetching_counts_as_busy_and_pollers_always_start(self):
        self.three_queued()
        self.hold(self.fixture.base / 'logs/run-shop-a.lock')
        self.hold(self.fixture.base / 'logs/run-shop-b.lock')
        polling = core.new_state(REPO, FEATURE, '01')
        polling.update(stage='done', pr={'number': 5, 'url': 'u'}, published_sha='x')
        core.mark(polling, 'done', 'PR #5: waiting for CI')
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)], 'a and b hold their run locks (no slot yet): c waits')

    def test_nothing_starts_while_the_host_would_refuse_or_parallel_is_lowered(self):
        self.three_queued()

        with mock.patch.object(core, 'sbx_service_active', return_value=False):
            self.main('tick')

        self.assertEqual(self.spawned, [])
        self.assertIn('tick: 4 queued (sbx.service not active', self.output)
        self.slots(0)
        self.assertEqual(self.main('parallel', '1'), 0)
        self.assertEqual(self.kicks, [1])
        self.assertIn('parallel 1; 1 run holds a stage slot: no new stage starts until fewer than 1 do', self.output)
        self.main('tick')
        self.assertEqual(self.spawned, [])
        self.main('status')
        self.assertIn('stages 1 running of 1', self.output.splitlines()[0])
        self.assertEqual(self.main('parallel', '3'), 2, 'above the config ceiling')
        self.assertEqual(self.main('parallel', '0'), 2)
        self.main('parallel')
        self.assertEqual(self.output, 'parallel 1 (ceiling 2: config.json admission.slots) · 1 stage slot held\n')

    def test_slots_count_every_slot_file_under_the_lowered_parallel(self):
        config = json.loads(self.fixture.config.read_text())
        config['admission']['slots'] = 3
        self.fixture.config.write_text(json.dumps(config))
        core.update_global_control(parallel=2)
        self.slots(0, 2)
        self.assertFalse(core.take_slot(2, 3), 'slot-1 is free, but two are held already')
        self.assertEqual(core.SLOT_HANDLES, [])
        self.happy_path()
        self.main(FEATURE)
        self.assertEqual(self.state()['reason'], 'queued: all 2 stage slots busy')
        self.main('status', '-v')
        self.assertIn('stages: 2 running of 2', self.output)

    def test_a_held_feature_starts_no_stage_until_start_and_explicit_runs_refuse(self):
        self.happy_path()
        self.assertEqual(self.main('pause', FEATURE), 0)
        self.assertEqual(self.output, f'{FEATURE}: held (pipeline start {FEATURE})\n')
        self.assertEqual(core.feature_control(REPO, FEATURE)['hold'], True)
        self.assertTrue((self.fixture.feature / '.pipeline/feature.json').exists())
        self.assertEqual(self.main(FEATURE), 1)
        self.assertEqual(self.errors, f'{FEATURE}: held (pipeline start {FEATURE})\n')
        self.assertEqual(self.main(FEATURE, '--detach'), 1)
        self.main('tick')
        self.assertEqual(self.spawned, [])
        self.assertIn(f'tick: held: shop/{FEATURE} (by you)', self.output)
        self.main('status')
        self.assertIn(f'HELD     by you: {FEATURE} (pipeline start {FEATURE})', self.output)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.main('start', FEATURE), 0)
        self.assertEqual(self.kicks, [1])
        self.assertFalse(core.feature_control(REPO, FEATURE)['hold'])
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)])

    def test_a_hold_stops_the_next_stage_and_the_next_step_after_a_merge(self):
        self.happy_path(**{'thing-01-plan': [dict(PLAN, run=PLAN['run'] + [
            f"python3 -c \"import json, pathlib; p = pathlib.Path('{self.fixture.feature}/.pipeline/feature.json'); "
            "p.write_text(json.dumps({'hold': True}))\""])]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status'], state['reason']),
                         ('implement', 'ready', f'queued: held (pipeline start {FEATURE})'))
        self.assertEqual([call['name'] for call in self.claude_calls()], ['thing-01-plan'])
        self.assertEqual(self.kicks, [1], 'the run held a slot: tick is kicked')
        self.assertEqual(self.main('start', FEATURE), 0)
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        write(self.fixture.plans / FEATURE / '02-next/brief.md', '# next\n')
        self.main('pause', FEATURE)
        self.github.pull(state['pr']['number'])['merged'] = True
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'closed', 'a PR poll still runs while held')
        second = self.state('02')
        self.assertEqual((second['stage'], second['status'], second['reason']),
                         ('plan', 'ready', f'queued: held (pipeline start {FEATURE})'))

    def test_pause_now_interrupts_the_run_and_it_stays_held(self):
        self.happy_path(**{'thing-01-implement': [{'sleep': 30}, IMPLEMENT]})

        def pause_now():
            deadline = time.monotonic() + 60

            while not self.claude_calls('thing-01-implement') and time.monotonic() < deadline:
                time.sleep(0.2)

            time.sleep(1)
            core.update_feature_control(REPO, FEATURE, hold=True)
            os.kill(os.getpid(), signal.SIGTERM)  # what `pause --now` sends the run (the run is this process here)

        threading.Thread(target=pause_now, daemon=True).start()

        self.assertEqual(self.main(FEATURE), 130)

        state = self.state()
        self.assertEqual((state['stage'], state['status'], state['reason']),
                         ('implement', 'ready', f'queued: held (pipeline start {FEATURE})'))
        self.assertEqual(self.sandbox_status(), 'stopped')

    def test_pause_now_signals_only_that_features_run(self):
        logs = self.fixture.base / 'logs'
        logs.mkdir(exist_ok=True)
        holders = {}

        for name in (FEATURE, f'{FEATURE}-v2'):
            holders[name] = subprocess.Popen([sys.executable, '-c', (
                'import fcntl, os, time\n'
                f"handle = open({str(logs / f'run-shop-{name}.lock')!r}, 'a+')\n"
                'fcntl.flock(handle, fcntl.LOCK_EX)\n'
                'handle.write(str(os.getpid())); handle.flush()\n'
                'time.sleep(60)  # pipeline run stand-in\n')])
            self.addCleanup(holders[name].kill)

        time.sleep(0.8)
        self.assertEqual(self.main('pause', FEATURE, '--now'), 0)
        self.assertIn('its run was interrupted', self.output)
        self.assertEqual(holders[FEATURE].wait(10), -signal.SIGTERM)
        self.assertIsNone(holders[f'{FEATURE}-v2'].poll())

    def test_drain_holds_everything_and_start_ends_it(self):
        self.three_queued()
        self.assertEqual(self.main('pause'), 0)
        self.assertIn('drained: no new stage starts; running stages finish', self.output)
        self.main('tick')
        self.assertEqual(self.spawned, [])
        self.main('status')
        self.assertIn('[DRAINED: pipeline start]', self.output.splitlines()[0])
        self.happy_path()
        self.assertEqual(self.main(FEATURE), 1)
        self.assertIn('drained (pipeline start)', self.errors)
        self.assertEqual(self.main('start'), 0)
        self.assertFalse(core.global_control()['drain'])
        self.main('tick')
        self.assertEqual([feature for _, feature in self.spawned], ['a', 'b'])

    def test_start_unparks_a_backlog_feature_and_restarts_a_paused_one(self):
        write(self.fixture.plans / '_backlog/later/01-x/brief.md', '# x\n')
        self.main('queue')
        self.assertIn('BACKLOG  later', self.output)
        self.assertEqual(self.main('start', 'later'), 0, self.errors)
        self.assertTrue((self.fixture.plans / 'later/01-x/brief.md').exists())
        self.assertFalse((self.fixture.plans / '_backlog/later').exists())
        self.assertIn('later 01-x: released', self.output)
        core.mark(self.state(), 'paused', 'x')
        self.assertEqual(self.main('start', FEATURE), 0, self.errors)
        self.assertEqual(self.spawned, [(REPO, FEATURE)], 'a paused step starts at once, like --detach')
        self.assertEqual(self.main('start', 'nope'), 1)

    def test_respond_refuses_a_held_feature(self):
        state = self.published()
        self.main('pause', FEATURE)
        self.github.reviews[state['pr']['number']] = [{'user': {'login': 'reviewer1'}, 'body': 'fix x',
                                                        'submitted_at': '2099-01-01T00:00:00Z', 'state': 'COMMENTED'}]
        self.assertEqual(self.main('respond', FEATURE), 1)
        self.assertIn('held (pipeline start', self.errors)


class AutonomyTest(RunnerCase):
    """`autonomy auto`: the plan's recommendations answer its questions, a last review round's blocking findings go to
    respond, and every such decision is a ⚑ in the PR and the ping; blocked and failed still stop."""

    PLAN_WITH_QUESTIONS = {'run': ["printf '# Plan\\n\\n## Unresolved questions\\n\\n1. Q1? Rec: A\\n\\n## Steps\\n\\nx\\n' "
                                   "> {step}/plan.md"], 'result': {'questions': ['Q1? Rec: A']}}

    def test_the_verb_shows_and_sets_it(self):
        self.assertEqual(self.main('autonomy', FEATURE), 0)
        self.assertEqual(self.output, f'{FEATURE}: autonomy ask\n')
        self.assertEqual(self.main('autonomy', FEATURE, 'auto'), 0)
        self.assertEqual(core.feature_control(REPO, FEATURE)['autonomy'], 'auto')
        self.assertEqual(self.main('autonomy', FEATURE, 'bogus'), 2)
        self.main('status')
        self.assertIn('01/01 auto', self.output)

    def test_auto_takes_the_plans_recommendations_and_flags_each(self):
        self.main('autonomy', FEATURE, 'auto')
        self.published(**{'thing-01-plan': [self.PLAN_WITH_QUESTIONS]})
        self.assertEqual(len(self.claude_calls('thing-01-plan')), 1)
        prompt = self.claude_calls('thing-01-plan')[0]['argv']
        self.assertIn('- Autonomy: auto — your questions will be answered with your recommendation',
                      prompt[prompt.index('--append-system-prompt') + 1])
        plan = (self.fixture.step / 'plan.md').read_text()
        self.assertIn(f'## Unresolved questions\n\n{core.AUTO_ANSWERED}\n\n1. Q1? Rec: A', plan)
        self.assertIn(core.AUTO_ANSWERED, core.approved_plan(self.state()).read_text())
        self.assertIn('plan auto-decided 1:\n- Q1? Rec: A', self.notifications)
        notice = (self.fixture.step / 'done.md').read_text()
        self.assertIn('## Decisions\n\n- auto-decided: Q1? Rec: A', notice)
        self.assertNotIn('## Questions', notice)
        comment = next(iter(self.github.comments.values()))
        self.assertIn('- auto-decided: Q1? Rec: A', comment)
        self.assertNotIn('## Questions', comment)
        state = self.state()
        self.assertEqual(state['results']['plan']['questions'], [])
        core.auto_decide(state, 'plan', ['Q1? Rec: A'])
        core.answer_in_plan(core.step_of(state))
        self.assertEqual(state['results']['plan']['flags'].count('⚑ auto-decided: Q1? Rec: A'), 1)
        self.assertEqual((self.fixture.step / 'plan.md').read_text().count(core.AUTO_ANSWERED), 1)
        self.ci(state['published_sha'], 'success', 'success')
        self.main(FEATURE)
        self.assertIn('\n⚑ auto-decided: Q1? Rec: A', self.notifications[-1])

    def test_a_plan_without_the_section_gets_one(self):
        self.main('autonomy', FEATURE, 'auto')
        self.happy_path(**{'thing-01-plan': [PLAN_ASKS]})
        self.main(FEATURE, '--until', 'plan')
        self.assertTrue((self.fixture.step / 'plan.md').read_text().endswith(
            f'## Unresolved questions\n\n{core.AUTO_ANSWERED}\n'))
        self.assertEqual(self.state()['stage'], 'implement')

    def test_auto_sends_a_last_rounds_blocking_findings_to_respond(self):
        self.main('autonomy', FEATURE, 'auto')
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Handle the edge'"] + RESPOND['run']}
        self.happy_path(**{'thing-01-respond-1': [fix],
                           'thing-01-review-2': [{'run': ['echo ok > {step}/review-2.md'], 'result': {'blocking': 2}}],
                           'thing-01-respond-2': [RESPOND]})
        self.main(FEATURE)
        state = self.state()
        self.assertEqual(state['stage'], 'done', state['reason'])
        self.assertIn('⚑ auto-decided: review round 2 still reported 2 blocking finding(s); the respond stage fixed them '
                      'without another review', state['results']['review-2']['flags'])
        self.assertIn('review round 2: 2 blocking finding(s) auto-approved to respond', self.notifications)

    def test_blocked_still_stops_for_you(self):
        self.main('autonomy', FEATURE, 'auto')
        self.happy_path(**{'thing-01-plan': [BLOCKED]})
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused')


class ExtrasTest(RunnerCase):
    """`start --after`, `answer` and `log`."""

    def test_notify_sends_title_priority_and_click(self):
        state = self.state()
        sent = []

        with mock.patch.object(core.urllib.request, 'urlopen', lambda request, timeout: sent.append(request) or
                               io.BytesIO(b'')), redirect_stdout(io.StringIO()) as output:
            NOTIFY(state, 'no topic yet')
            self.assertEqual(sent, [])
            self.assertIn(f'ntfy: no topic at {core.NTFY_TOPIC}', output.getvalue())
            write(core.NTFY_TOPIC, 'topic-x\n')
            NOTIFY(state, 'paused: x', click='https://example/pull/9', priority='high')

        request, = sent
        self.assertEqual(request.full_url, 'https://ntfy.sh/topic-x')
        self.assertEqual((request.get_header('Title'), request.get_header('Priority'), request.get_header('Click')),
                         (f'shop {FEATURE} 01', 'high', 'https://example/pull/9'))
        self.assertEqual(request.data, b'paused: x')

    def test_slot_busy_queue_is_not_pinged(self):
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        core.update_global_control(parallel=1)
        holder = subprocess.Popen(['flock', str(self.fixture.base / 'logs/slot-0.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.happy_path()
        self.main(FEATURE)
        self.assertEqual(self.state()['reason'], 'queued: all 1 stage slots busy')
        self.assertEqual(self.notifications, [])
        self.usage(0.95)
        os.killpg(holder.pid, signal.SIGKILL)  # flock's sleep holds the lock too
        holder.wait()
        self.main(FEATURE)
        self.assertEqual(self.notifications, ['queued: weekly usage 95% ≥ 80%'], 'other queue reasons still ping')

    def feature(self, name, *step_names):
        for step_name in step_names or ('01-x',):
            write(self.fixture.plans / name / step_name / 'brief.md', '# x\n')

    def test_after_holds_a_feature_until_the_other_merges(self):
        self.feature('next')
        self.assertEqual(self.main('start', 'next', '--after', FEATURE), 0, self.errors)
        self.assertEqual(self.output, f'next: starts after {FEATURE} merges (tick releases it; pipeline start next drops '
                                      'the wait)\n')
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, FEATURE)])
        self.assertIn(f'tick: held: shop/next (after {FEATURE})', self.output)
        self.main('status')
        self.assertIn(f'HELD     after {FEATURE}: next', self.output)
        self.assertEqual(self.main('next'), 1)
        self.assertIn(f'next: after {FEATURE} (pipeline start next drops it)', self.errors)
        core.mark(self.state(), 'closed', 'merged as PR #5')
        self.spawned.clear()
        self.main('tick')
        self.assertEqual(self.spawned, [(REPO, 'next')], 'merged: tick releases it')

    def held_lines(self):
        lines = self.output.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith('HELD'))
        return [lines[start]] + [line for line in lines[start + 1:] if line.startswith(' ' * 9)]

    def test_held_lists_chains_in_merge_order(self):
        for name, after in (('c', 'b'), ('a', FEATURE), ('b', 'a'), ('x', 'y'), ('y', None)):
            self.feature(name)

            if after:
                core.update_feature_control(REPO, name, after=after)

        self.main('status')
        self.assertEqual(self.held_lines(), [f'HELD     after {FEATURE}: a → b → c', '         after y: x'])
        core.update_global_control(order=[f'{REPO}/y'])
        self.main('status')
        self.assertEqual(self.held_lines(), ['HELD     after y: x', f'         after {FEATURE}: a → b → c'])
        core.update_feature_control(REPO, 'y', hold=True)
        self.main('queue')
        self.assertEqual(self.held_lines(), ['HELD     after y: x', f'         after {FEATURE}: a → b → c',
                                             '         by you: y (pipeline start y)'])

    def test_held_siblings_and_cycles(self):
        for name, after in (('a', FEATURE), ('b', 'a'), ('c', 'a'), ('p', 'q'), ('q', 'p')):
            self.feature(name)
            core.update_feature_control(REPO, name, after=after)

        self.main('status')
        self.assertEqual(self.held_lines(), [f'HELD     after {FEATURE}: a → b, c', '         p (after q), q (after p)'])

    def test_next_for_a_held_step_names_start_not_detach(self):
        core.mark(self.state(), 'ready', f'queued: held (pipeline start {FEATURE})')
        core.update_feature_control(REPO, FEATURE, hold=True)
        self.main('status', '-v')
        self.assertIn(f'shop/{FEATURE} {STEP}: plan [ready] queued: held (pipeline start {FEATURE}) held: by you',
                      self.output)
        self.assertIn(f'  next: pipeline start {FEATURE}\n', self.output)
        self.assertIn('autonomy ask', self.output)
        self.feature('other')
        core.update_feature_control(REPO, FEATURE, hold=False, after='other')
        self.main('show', FEATURE)
        self.assertIn('autonomy ask · held: after other', self.output)
        self.assertIn(f'next: waits for other to merge (pipeline start {FEATURE} runs it now)', self.output)
        core.update_feature_control(REPO, FEATURE, after=None)
        core.update_global_control(drain=True)
        self.main('status', '-v')
        self.assertIn('  next: pipeline start\n', self.output)
        core.update_global_control(drain=False)
        self.main('status', '-v')
        self.assertIn(f'  next: pipeline {FEATURE} --detach  (or wait for tick)', self.output)

    def test_held_feature_keeps_its_pr_row(self):
        state = self.state()
        state.update(stage='done', pr={'number': 5, 'url': 'https://example/pull/5'}, published_sha='abc', ci={})
        core.mark(state, 'done', 'PR #5: CI running')
        core.update_feature_control(REPO, FEATURE, hold=True)
        self.main('status')
        self.assertIn(f'WAITING  {FEATURE} (PR #5 CI running · held)', self.output)
        self.assertNotIn('HELD', self.output)

    def test_after_a_single_step_and_bad_waits(self):
        self.feature('next')
        self.main('start', 'next', '--after', f'{FEATURE}:02')
        self.assertEqual(core.hold_reason(REPO, 'next'),
                         f'after {FEATURE}:02: {FEATURE} has no step 02 (pipeline start next drops it)')
        self.main('start', 'next', '--after', 'ghost')
        self.assertEqual(core.hold_reason(REPO, 'next'), 'after ghost: no feature ghost (pipeline start next drops it)')
        self.main('start', 'next', '--after', f'{FEATURE}:01')
        core.mark(self.state(), 'closed', 'merged as PR #5')
        self.assertIsNone(core.hold_reason(REPO, 'next'))
        self.assertEqual(self.main('start', 'next', '--after', 'next'), 2)
        self.assertEqual(self.main('start', 'next', '--after', 'Bad'), 2)
        self.assertEqual(self.main('start', 'next', '--after'), 2)
        self.main('start', 'next')
        self.assertIsNone(core.feature_control(REPO, 'next')['after'], 'a plain start drops the wait')

    def test_answer_approves_a_changed_plan_only(self):
        write(self.fixture.step / 'plan.md', '# Plan\n\n## Unresolved questions\n\n1. Q1? Rec: A\n')
        state = self.state()
        core.mark(state, 'awaiting-approval', 'The plan asks you')
        editor = self.fixture.bin / 'edit'
        write(editor, '#!/bin/sh\necho "Answer: A" >> "$1"\n', 0o755)

        with mock.patch.dict(os.environ, {'VISUAL': '', 'EDITOR': 'true'}):
            self.assertEqual(self.main('answer', FEATURE), 0)

        self.assertIn('plan.md unchanged: nothing approved', self.output)
        self.assertEqual(self.state()['status'], 'awaiting-approval')

        with mock.patch.dict(os.environ, {'VISUAL': str(editor)}):
            self.assertEqual(self.main('answer', FEATURE), 0, self.errors)

        state = self.state()
        self.assertEqual((state['status'], state['approved']), ('ready', True))
        self.assertIn('Answer: A', core.approved_plan(state).read_text())
        self.assertEqual(self.kicks, [1])
        self.assertEqual(self.main('answer', FEATURE), 1)
        self.assertIn('no step is awaiting your answer', self.errors)

    def test_answer_via_editor_records_the_answer(self):
        self.happy_path(**{'thing-01-plan': [{
            'run': ["printf '# Plan\\n\\n## Unresolved questions\\n\\n1. Q1? Rec: A\\n' > {step}/plan.md"],
            'result': {'questions': ['Q1? Rec: A']}}]})
        self.main(FEATURE)
        editor = self.fixture.bin / 'edit'
        write(editor, '#!/bin/sh\necho "Answer: B" >> "$1"\n', 0o755)

        with mock.patch.dict(os.environ, {'VISUAL': str(editor)}):
            self.assertEqual(self.main('answer', FEATURE), 0, self.errors)

        self.assertEqual(self.main(FEATURE), 0)
        self.assertEqual(self.state()['stage'], 'done', self.state()['reason'])
        comment = next(iter(self.github.comments.values()))
        self.assertIn('## Decisions\n\n- Owner answered: Q1? Rec: A — Answer: B', comment)
        self.assertNotIn('## Questions', comment)

    def test_log_prints_the_current_steps_tool_calls_and_follows_new_lines(self):
        workspace = str(adapter.clone_path(REPO))
        log = core.state_dir(REPO, FEATURE) / '01-implement.log'
        events = [{'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5'},
                  {'type': 'assistant', 'timestamp': '2026-10-02T18:51:34Z', 'message': {'content': [
                      {'type': 'text', 'text': 'x'},
                      {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pnpm test\n  --run'}}]}},
                  {'type': 'assistant', 'timestamp': '2026-10-02T18:52:00Z', 'message': {'content': [
                      {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': f'{workspace}/src/a.ts'}}]}},
                  {'type': 'result', 'structured_output': {'status': 'done', 'summary': 'Built it.'}}]
        write(log, ''.join(json.dumps(event) + '\n' for event in events))
        core.mark(self.state(), 'running', 'implement running')
        self.assertEqual(self.main('log', FEATURE), 0, self.errors)
        clock = time.strftime('%H:%M', time.localtime(core.parse_iso('2026-10-02T18:51:34Z')))
        later = time.strftime('%H:%M', time.localtime(core.parse_iso('2026-10-02T18:52:00Z')))
        self.assertEqual(self.output, f'== {FEATURE} 01-implement.log\n      session start (claude-opus-5-5)\n'
                                      f'{clock} Bash pnpm test --run\n{later} Edit src/a.ts\n      result done: Built it.\n')
        reader = core.log_reader(log, workspace)
        self.assertEqual(len(reader()), 4)
        line = json.dumps(events[1])

        with log.open('a') as handle:
            handle.write(line[:20])

        self.assertEqual(reader(), [], 'half a line waits for the rest')

        with log.open('a') as handle:
            handle.write(line[20:] + '\n')

        self.assertEqual(reader(), [f'{clock} Bash pnpm test --run'])
        write(core.state_dir(REPO, FEATURE) / '01-gates.log', '== 2026-10-02T20:00:00 check `pnpm check` → exit 0\nout\n')
        self.main('log', FEATURE)
        self.assertEqual(self.output, f'== {FEATURE} 01-gates.log\n20:00 check `pnpm check` → exit 0\n')
        self.assertEqual(self.main('log', 'thing', '-x'), 2)
