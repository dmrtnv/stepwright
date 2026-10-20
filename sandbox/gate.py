#!/usr/bin/env python3
"""Phase 2 gate: prove Docker Sandboxes (sbx) can host pipeline stages before the runner is ported to it.

Run after sbx-host/install-sbx.sh, `sbx login` and `pipeline template build`, from a terminal (test 2 may ask you to /login once):
  sandbox/gate.py --repo <checkout> [--skip-checks] [--full] [--keep]

Tests (plan v4, Phase 2): 1 exit codes · 2 claude -p stream-json, model/mode/usage, login survives rm ·
3 coexists with host Docker · 4 repo setup + checkFast under agent egress · 5 per-sandbox rules live + isolated ·
6 kit copied and loaded (--full: a real /plan-harden run) · 7 kill + stop leaves nothing running · 8 exfil probes ·
9 the runner's real stage argv (session id, system prompt, settings, schema, --add-dir) and a --resume of it.
Any FAIL ⇒ the agent-user fallback. Costs: five tiny Opus calls; --full adds one /plan-harden run (≤ $5).
The sbx primitives are the runner's own (../backend.py).
Report: ~/.cache/pipeline/gate/report-<time>.json
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HOME = os.path.expanduser('~')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import backend  # noqa: E402
import core  # noqa: E402

WORK_ROOT = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.join(HOME, '.cache')), 'pipeline', 'gate')
SANDBOX_A, SANDBOX_B, SANDBOX_C = 'pl-gate-a', 'pl-gate-b', 'pl-gate-c'
MISSING = 'pl-gate-missing'
FORBIDDEN_URLS = ('https://example.com', 'https://github.com', 'https://registry.npmjs.org/', 'https://openrouter.ai',
                  'https://api.openai.com', 'https://pypi.org/simple/')
HOST_PORTS = (5432, 6543)
STAGE_ENV = backend.STAGE_ENV
VM_MEMORY = backend.memory_dir(SANDBOX_A)
STAGE_SETTINGS = backend.stage_settings(SANDBOX_A)
PLACEHOLDER = re.compile(r'proxy-?managed', re.I)  # sbx puts placeholders in the VM; its proxy injects the real token
STAGE_SKILLS = ('plan-harden', 'plan-implement', 'review-branch', 'review-respond')
SBX_ENV = backend.sbx_env()

results = []


def record(test, check, status, detail=''):
    results.append({'test': test, 'check': check, 'status': status, 'detail': detail})
    print(f'  [{status}] {test} {check}' + (f' — {detail}' if detail else ''), flush=True)


def expect(test, check, ok, detail=''):
    record(test, check, 'PASS' if ok else 'FAIL', detail)

    return ok


def run(argv, timeout=120, check=False, **kwargs):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=SBX_ENV,
                                stdin=subprocess.DEVNULL, **kwargs)
    except subprocess.TimeoutExpired as error:
        result = subprocess.CompletedProcess(argv, 124, error.stdout or '', f'timeout after {timeout}s')

    if check and result.returncode != 0:
        raise SystemExit(f'{" ".join(argv[:4])}… exited {result.returncode}: {result.stderr.strip()[-500:]}')

    return result


def sbx_exec(name, command, timeout=120, env=None, workdir=None):
    argv = ['sbx', 'exec']

    for key, value in (env or {}).items():
        argv += ['-e', f'{key}={value}']

    if workdir:
        argv += ['-w', workdir]

    return run(argv + [name, '--', *command], timeout=timeout)


def sh(name, script, timeout=120, env=None):
    return sbx_exec(name, ['bash', '-c', script], timeout=timeout, env=env)


def tail(text, lines=15):
    return '\n'.join((text or '').strip().splitlines()[-lines:])


reachable = backend.reachable
template_tag = core.target_tag  # config.json "template".claude: the host's Claude Code or the pin
create = backend.create
kit_rules = backend.kit_rules


def policy(test, *arguments):
    """sbx policy call; a failure is recorded against the test."""
    result = backend.policy(*arguments)

    if result.returncode != 0:
        record(test, f'sbx policy {" ".join(arguments)}', 'FAIL', tail(result.stderr or result.stdout, 3))

    return result.returncode == 0


def remove(*names):
    for name in names:
        run(['sbx', 'rm', '--force', name], timeout=180)


def lock_egress(test, name):
    hosts, raw = kit_rules(name)

    if hosts is None:
        record(test, f'{name} kit rules readable', 'FAIL', 'unparsed `sbx policy ls --json`: ' + tail(raw, 5))
        return

    extra = [host for host in hosts if not backend.agent_domain(host)]

    for host in extra:
        policy(test, 'deny', 'network', '--sandbox', name, host)

    record(test, f'{name} kit allow rules', 'INFO', f'kit allows {hosts}; denied {extra or "none"}')


def claude_probe(name, prompt='Reply with OK only.', effort='low', budget='0.50', timeout=600):
    command = [
        'claude', '-p', prompt, '--model', 'claude-opus-5-5', '--effort', effort,
        '--fallback-model', 'claude-sonnet-5-5', '--max-budget-usd', budget,
        '--permission-mode', 'auto', '--permission-prompts', 'none', '--settings', STAGE_SETTINGS,
        '--output-format', 'stream-json', '--verbose',
    ]
    result = sbx_exec(name, command, timeout=timeout, env=STAGE_ENV)
    events = []

    for line in result.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass

    init = next((e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init'), {})
    windows = next((e['rate_limit_info'].get('unifiedWindows') for e in events
                    if e.get('type') == 'rate_limit_event' and (e.get('rate_limit_info') or {}).get('unifiedWindows')), None)
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})

    return {'rc': result.returncode, 'init': init, 'windows': windows, 'result': final, 'stderr': result.stderr,
            'ok': result.returncode == 0 and final.get('subtype') == 'success' and not final.get('is_error')}


def check_probe(test, probe, label):
    init, final = probe['init'], probe['result']
    expect(test, f'{label}: result success', probe['ok'],
           f'rc={probe["rc"]} {final.get("subtype")} {str(final.get("result"))[:120]!r} {tail(probe["stderr"], 3)}')
    expect(test, f'{label}: init model claude-opus-5-5', init.get('model') == 'claude-opus-5-5', str(init.get('model')))
    expect(test, f'{label}: init permissionMode auto', init.get('permissionMode') == 'auto', str(init.get('permissionMode')))
    seven = ((probe['windows'] or {}).get('seven_day') or {}).get('utilization')
    expect(test, f'{label}: rate_limit_event unifiedWindows', seven is not None, f'windows={probe["windows"]}')
    record(test, f'{label}: auth source', 'INFO', f'apiKeySource={init.get("apiKeySource")} version={init.get("claude_code_version")}')


# ---------------------------------------------------------------------------------------------- tests


def test_1_exit_codes():
    expect(1, 'exit 7 passes through', sh(SANDBOX_A, 'exit 7').returncode == 7)
    expect(1, 'true → 0', sbx_exec(SANDBOX_A, ['true']).returncode == 0)
    result = sh(SANDBOX_A, 'echo out; echo err >&2')
    expect(1, 'stdout/stderr separate', result.stdout.strip() == 'out' and 'err' in result.stderr, repr(result.stdout))
    missing = sbx_exec(MISSING, ['true'])
    record(1, 'missing sandbox', 'INFO', f'rc={missing.returncode} (runner must check `sbx ls` first)')


def test_2_claude(interactive):
    probe = claude_probe(SANDBOX_A)

    if not probe['ok'] and interactive:
        print(f'\n  Claude in {SANDBOX_A} failed ({tail(probe["stderr"], 2)!r}); log in once:\n'
              f'    sbx exec -it {SANDBOX_A} -- claude     then /login, then /exit\n  Press Enter when done…', flush=True)
        input()
        probe = claude_probe(SANDBOX_A)

    check_probe(2, probe, 'sandbox A')
    skills, agents = probe['init'].get('skills') or [], probe['init'].get('agents') or []
    names = {s if isinstance(s, str) else s.get('name') for s in skills}
    memory_paths = json.dumps(probe['init'].get('memory_paths'))
    expect(6, 'memory overlay loaded', VM_MEMORY in memory_paths, memory_paths)
    expect(6, 'stage skills loaded', all(skill in names for skill in STAGE_SKILLS), f'missing {[s for s in STAGE_SKILLS if s not in names]}')
    expect(6, 'web-researcher agent loaded', 'web-researcher' in agents, str(agents))

    return probe['ok']


def test_2_login_survives_rm(tag):
    remove(SANDBOX_A)
    created = create(SANDBOX_C, tag, cpus='2', memory='2g')

    if not expect(2, 'create fresh sandbox C', created.returncode == 0, tail(created.stderr, 3)):
        return

    lock_egress(2, SANDBOX_C)
    check_probe(2, claude_probe(SANDBOX_C), 'after rm, no /login')


def test_3_coexistence(listeners_before):
    host = run(['docker', 'info', '--format', '{{.ServerVersion}}'])
    expect(3, 'host rootless Docker still answers', host.returncode == 0, host.stdout.strip() or tail(host.stderr, 2))
    inner = sh(SANDBOX_A, "docker info --format '{{.ServerVersion}}' && docker ps -aq | wc -l")
    lines = inner.stdout.split()
    expect(3, 'sandbox has its own Docker', inner.returncode == 0 and lines[-1:] == ['0'], f'server/containers {lines}')
    new = {address: owner for address, owner in listening().items() if address not in listeners_before}
    # sbx 0.48 binds a listener to lo (SO_BINDTODEVICE); ss prints it as 127.0.0.1%lo:<port>.
    exposed = {address: owner for address, owner in new.items()
               if not address.replace('%lo:', ':').startswith(('127.0.0.1:', '[::1]:')) or owner != 'sbx'}
    expect(3, 'new host listeners are sbx on loopback only', not exposed, f'new {new}; exposed {exposed or "none"}')
    record(3, 'MemAvailable', 'INFO', f'{mem_available_gb():.1f} GB with sandboxes up')


def test_5_rules(setup_hosts):
    ok_before, detail_before = reachable(SANDBOX_A, 'https://registry.npmjs.org/')
    expect(5, 'npm blocked before allow', not ok_before, detail_before)
    policy(5, 'allow', 'network', '--sandbox', SANDBOX_A, ','.join(setup_hosts))
    ok_a, detail_a = reachable(SANDBOX_A, 'https://registry.npmjs.org/')
    expect(5, 'allow is live without restart', ok_a, detail_a)
    ok_b, detail_b = reachable(SANDBOX_B, 'https://registry.npmjs.org/')
    expect(5, 'other sandbox still blocked', not ok_b, detail_b)


def test_4_setup_and_checks(config, setup_hosts, skip_checks):
    for command in config['setup']:
        result = sh(SANDBOX_A, command, timeout=1800)

        if not expect(4, f'setup: {command[:60]}', result.returncode == 0, tail(result.stderr or result.stdout, 8)):
            log = run(['sbx', 'policy', 'log', SANDBOX_A, '--limit', '30'])
            record(4, 'policy log', 'INFO', tail(log.stdout, 30))
            break

    for host in setup_hosts:
        policy(4, 'rm', 'network', '--sandbox', SANDBOX_A, '--resource', host)

    ok, detail = reachable(SANDBOX_A, 'https://registry.npmjs.org/')
    expect(4, 'setup egress removed', not ok, detail)

    if skip_checks:
        record(4, 'checkFast', 'SKIP', '--skip-checks')
        return

    for step in config['checkFast']:
        started = time.time()
        result = sh(SANDBOX_A, step['run'], timeout=2400)

        for _ in range(step.get('flakeReruns', 0)):
            if result.returncode == 0:
                break
            result = sh(SANDBOX_A, step['run'], timeout=2400)

        expect(4, f'checkFast {step["name"]} ({int(time.time() - started)}s)', result.returncode == 0,
               tail(result.stdout + result.stderr, 12) if result.returncode else '')


def copy_kit(name, memory_dir):
    sh(name, 'mkdir -p /home/agent/.claude')
    items = [os.path.join(HOME, '.claude', item) for item in ('CLAUDE.md', 'skills', 'agents')]
    items += [path for path in (str(backend.PIPELINE_DIR / 'system.md'),) if os.path.exists(path)]

    for item in items:
        result = run(['sbx', 'cp', '-L', item, f'{name}:/home/agent/.claude/'], timeout=300)
        expect(6, f'sbx cp {os.path.basename(item)}', result.returncode == 0, tail(result.stderr, 3))

    if memory_dir and os.path.isdir(memory_dir):
        # a directory lands inside an existing destination directory, so this makes VM_MEMORY
        result = run(['sbx', 'cp', '-L', memory_dir, f'{name}:{os.path.dirname(VM_MEMORY)}/'], timeout=300)
        count = sh(name, f'ls {VM_MEMORY} | wc -l').stdout.strip()
        expect(6, 'sbx cp repo memory', result.returncode == 0 and count == str(len(os.listdir(memory_dir))),
               f'{count} files in {VM_MEMORY} {tail(result.stderr, 3)}')
    else:
        record(6, 'repo memory', 'FAIL', f'{memory_dir} absent')

    kit_settings = sh(name, 'cat /home/agent/.claude/settings.json').stdout
    record(6, 'kit settings.json (replaced)', 'INFO', ' '.join(kit_settings.split()))
    sh(name, f"cat > /home/agent/.claude/settings.json <<'EOF'\n{STAGE_SETTINGS}\nEOF")
    installed = sh(name, 'cat /home/agent/.claude/settings.json').stdout.strip()
    expect(6, 'stage settings.json installed (no hooks, no bypass default)', installed == STAGE_SETTINGS, installed)


def test_6_full():
    brief = ('# 01 probe\n\n**Goal:** add a one-line comment to README.md saying the pipeline gate ran.\n\n'
             '## Scope\nIn: README.md. Out: everything else.\n\n## Open questions\nNone\n')
    sh(SANDBOX_A, f"mkdir -p /tmp/plans/01-probe && cat > /tmp/plans/01-probe/brief.md <<'EOF'\n{brief}EOF")
    probe = claude_probe(SANDBOX_A, '/plan-harden /tmp/plans/01-probe/brief.md', effort='high', budget='5', timeout=3600)
    check_probe(6, probe, '/plan-harden')
    written = sh(SANDBOX_A, 'test -s /tmp/plans/01-probe/plan.md && echo yes').stdout.strip()
    expect(6, '/plan-harden wrote plan.md (foreground agents returned)', written == 'yes')
    usage = list((probe['result'].get('modelUsage') or {}).keys())
    record(6, 'models used', 'INFO', f'{usage} (subagents should be claude-sonnet-5-5)')
    expect(6, 'no Fable used', not any('fable' in model for model in usage), str(usage))


def test_7_kill_and_stop():
    marker = "pgrep -f 'sleep 60[1]'"  # the bracket keeps pgrep from matching this bash -c itself
    process = subprocess.Popen(['sbx', 'exec', SANDBOX_A, '--', 'sleep', '601'], env=SBX_ENV,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    expect(7, 'sleep running in VM', sh(SANDBOX_A, marker).returncode == 0)
    process.terminate()

    try:
        process.wait(15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    time.sleep(3)
    survived = sh(SANDBOX_A, marker).returncode == 0
    record(7, 'killing `sbx exec` alone', 'INFO', 'VM process survives (runner must `sbx stop`)' if survived else 'VM process ended')
    stopped = run(['sbx', 'stop', SANDBOX_A], timeout=180)
    expect(7, 'sbx stop', stopped.returncode == 0, tail(stopped.stderr, 3))
    leftovers = run(['pgrep', '-af', f'sbx exec {SANDBOX_A}']).stdout.strip()
    expect(7, 'no host `sbx exec` left', not leftovers, leftovers)
    status = run(['sbx', 'ls', '--json']).stdout
    record(7, 'sbx ls after stop', 'INFO', tail(status, 5))
    expect(7, 'nothing running after restart', sh(SANDBOX_A, marker).returncode == 1)


def test_8_exfil(repo):
    for url in FORBIDDEN_URLS:
        ok, detail = reachable(SANDBOX_A, url)
        expect(8, f'blocked {url}', not ok, detail)

    # checkFast may leave the VM's own services up (a migration check may start its Postgres on the VM's
    # localhost:5432): stop them, so an answer can only come from the host.
    sh(SANDBOX_A, 'docker ps -q | xargs -r docker stop >/dev/null', timeout=180)
    # The sandbox proxy accepts every TCP connect (even to closed ports) and then drops it, so a bare connect proves
    # nothing: send a Postgres SSLRequest and require no answer ('N'/'S' = a real Postgres answered).
    probe = ("import socket,sys\n"
             "host,port=sys.argv[1],int(sys.argv[2])\n"
             "s=socket.create_connection((host,port),timeout=6)\n"
             "s.sendall(bytes([0,0,0,8,4,210,22,47]))\n"
             "print(repr(s.recv(8)))\n")
    targets = ['host.docker.internal', 'localhost'] + run(['hostname', '-I']).stdout.split()[:2]

    for host in targets:
        for port in HOST_PORTS:
            result = sbx_exec(SANDBOX_A, ['python3', '-c', probe, host, str(port)], timeout=30)
            answer = result.stdout.strip()
            expect(8, f'no Postgres answer from {host}:{port}', answer in ('', "b''"), answer or tail(result.stderr, 1))

    for remote in ('origin', 'https://github.com/pl-gate-invalid/nonexistent.git'):
        push = sh(SANDBOX_A, f'git push {remote} HEAD:refs/heads/agent/pl-gate-probe', timeout=90)
        expect(8, f'git push {remote} fails', push.returncode != 0, tail(push.stderr, 2))

    # The image sets SSH_AUTH_SOCK=/run/ssh-agent.sock; with forwarding off the socket must not exist.
    agent = sh(SANDBOX_A, 'test -S "${SSH_AUTH_SOCK:-/nonexistent}" && echo socket; ssh-add -l')
    expect(8, 'no ssh agent (host agent holds keys)', 'socket' not in agent.stdout and agent.returncode == 2,
           tail(agent.stdout + agent.stderr, 2))
    envs = sh(SANDBOX_A, "find /run/sandbox/source -name '.env*' ! -name '*.example' -not -path '*/node_modules/*' 2>/dev/null")
    expect(8, 'no .env under /run/sandbox/source', not envs.stdout.strip(), envs.stdout.strip()[:200])
    write = sh(SANDBOX_A, 'touch /run/sandbox/source/pl-gate-probe')
    expect(8, '/run/sandbox/source read-only', write.returncode != 0)
    host_checkout = sh(SANDBOX_A, f'ls {repo}/apps/api/.env {HOME}/.ssh {HOME}/.claude/.credentials.json 2>&1')
    expect(8, 'host checkout, ~/.ssh, host credentials invisible', host_checkout.returncode != 0, tail(host_checkout.stdout, 3))
    # Tokens in the VM must all be sbx placeholders (sk-ant-oat01-proxy-managed, gho_sbxproxymanaged000…).
    found = sh(SANDBOX_A, "{ env; cat /home/agent/.claude/.credentials.json /home/agent/.claude.json 2>/dev/null; } | "
                          "grep -oE '(sk-ant-[a-z0-9]+-[A-Za-z0-9_-]+|gh[opsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)' | sort -u")
    tokens = found.stdout.split()
    real = [token[:12] + '…' for token in tokens if not PLACEHOLDER.search(token)]
    expect(8, 'only placeholder tokens in VM', not real, f'placeholders {len(tokens) - len(real)}, real {real or "none"}')
    foreign = sh(SANDBOX_A, "curl -sS -m 20 https://api.anthropic.com/v1/messages -H 'x-api-key: sk-ant-api03-plgate-foreign' "
                            "-H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' "
                            """-d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"x"}]}' """
                            "-w '\\nHTTP %{http_code}'", timeout=60)
    reached = 'invalid x-api-key' in foreign.stdout or 'authentication_error' in foreign.stdout
    expect(8, 'foreign Anthropic key does not reach the API', not reached, tail(foreign.stdout, 1)[-40:])
    record(8, 'Anthropic credential injection', 'INFO', 'the proxy swaps any key for yours: any VM process can spend your '
                                                        'plan, none can use a foreign account' if not reached else '')


def stream(stdout):
    events = []

    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass

    init = next((e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init'), {})
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})

    return init, final


def test_9_stage_argv():
    """The runner's real stage call: core's flags, system prompt, per-stage settings, schema and --add-dir, then a
    --resume of the same session (what a gate fix or a limit resume does). Effort low: it proves the argv, not the work."""
    workspace = sh(SANDBOX_A, 'pwd').stdout.strip()
    plans = backend.plans_dir(SANDBOX_A, 'gate')
    sh(SANDBOX_A, f'mkdir -p {plans}/01-probe')
    state = {'feature': 'gate', 'id': '01'}
    session = str(uuid.uuid4())
    config = core.STAGES['plan']

    def call(prompt, resume):
        argv = [core.CLAUDE, '-p', prompt, *(('--resume', session) if resume else ('--session-id', session)),
                '--name', 'gate-01-plan', '--model', config['model'], '--effort', 'low',
                '--fallback-model', core.FALLBACK_MODEL, '--max-budget-usd', '1',
                '--permission-mode', 'auto', '--permission-prompts', 'none', '--add-dir', plans,
                '--append-system-prompt', core.SYSTEM_PROMPT.read_text() + '\n## This stage\n\n- Gate probe: no tools.\n',
                '--settings', core.stage_settings(state, 'plan', {'workspace': workspace}),
                '--json-schema', core.RESULT_SCHEMA.read_text(), '--output-format', 'stream-json', '--verbose']
        result = sbx_exec(SANDBOX_A, argv, timeout=600, env=core.stage_environment(), workdir=workspace)
        init, final = stream(result.stdout)
        structured = final.get('structured_output') or {}

        return result, init, final, structured

    result, init, final, structured = call('Do not use any tool. Finish at once with the structured result: status '
                                           'done, summary exactly "stage", and empty lists for the rest.', False)
    expect(9, 'stage argv: structured result', structured.get('status') == 'done' and structured.get('summary') == 'stage',
           f'rc={result.returncode} {final.get("subtype")} {str(structured or final.get("result"))[:160]!r} '
           f'{tail(result.stderr, 3)}')
    expect(9, 'stage argv: init model and mode', (str(init.get('model')).removesuffix('[1m]'), init.get('permissionMode'))
           == (config['model'], 'auto'), f'{init.get("model")} {init.get("permissionMode")}')
    expect(9, 'stage argv: session id honored', final.get('session_id') == session, str(final.get('session_id')))
    result, init, final, structured = call('Finish again the same way, summary exactly "resumed".', True)
    expect(9, 'stage argv: --resume of the same session', structured.get('summary') == 'resumed'
           and final.get('session_id') == session, f'rc={result.returncode} {str(structured or final.get("result"))[:160]!r}')


# ---------------------------------------------------------------------------------------------- main


def listening():
    """Host TCP listeners → owning process name."""
    owners = {}

    for line in run(['ss', '-ltnpH']).stdout.splitlines():
        fields = line.split()

        if len(fields) > 3:
            match = re.search(r'\(\("([^"]+)"', line)
            owners[fields[3]] = match.group(1) if match else '?'

    return owners


def mem_available_gb():
    with open('/proc/meminfo') as handle:
        for line in handle:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024 / 1024

    return 0.0


def preflight(repo, tag):
    problems = []

    if not shutil.which('sbx'):
        problems.append('sbx not installed: install docker-sbx (https://docs.docker.com/ai/sandboxes/install/)')
    elif tag.rpartition(':')[::2] not in [tuple(line.split()[:2]) for line in run(['sbx', 'template', 'ls']).stdout.splitlines()]:
        problems.append(f'template {tag} not loaded: run `pipeline template build`')

    if 'kvm' not in run(['id', '-nG']).stdout.split():
        problems.append('not in the kvm group: sudo usermod -aG kvm "$USER", then log out and back in')

    if not os.path.isdir(os.path.join(repo, '.git')):
        problems.append(f'{repo} is not a git checkout')

    if not os.path.exists(os.path.join(repo, '.claude', 'pipeline.json')):
        problems.append(f'{repo} has no .claude/pipeline.json (presets: sandbox/pipeline.*.json)')

    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--repo', required=True, help='a checkout whose .claude/pipeline.json the gate runs')
    parser.add_argument('--skip-checks', action='store_true', help='skip checkFast (setup still runs)')
    parser.add_argument('--full', action='store_true', help='also run a real /plan-harden (test 6)')
    parser.add_argument('--keep', action='store_true', help='keep sandboxes and the work dir for inspection')
    args = parser.parse_args()
    repo = os.path.abspath(os.path.expanduser(args.repo))
    tag = template_tag()
    problems = preflight(repo, tag)

    if problems:
        print('preflight failed:\n  ' + '\n  '.join(problems), file=sys.stderr)
        return 2

    with open(os.path.join(repo, '.claude', 'pipeline.json')) as handle:
        config = json.load(handle)

    setup_hosts = config['setupEgress']
    os.makedirs(WORK_ROOT, exist_ok=True)
    work = tempfile.mkdtemp(prefix='run-', dir=WORK_ROOT)
    clone = os.path.join(work, os.path.basename(repo))
    run(['git', 'clone', '--quiet', '--no-local', repo, clone], timeout=600, check=True)
    stray = run(['bash', '-c', f"find {clone} -name '.env*' ! -name '*.example' -not -path '*/.git/*'"]).stdout.strip()

    if stray:
        print(f'clean clone holds env files, refusing: {stray}', file=sys.stderr)
        return 2

    memory_dir = os.path.join(HOME, '.claude', 'projects', repo.replace('/', '-'), 'memory')
    listeners_before = listening()
    print(f'gate: template {tag}, repo {repo} (clean clone {clone}), config {config_path}', flush=True)

    try:
        created = create(SANDBOX_A, tag, clone, clone=True)

        if not expect(0, 'create sandbox A (--clone, 6 CPU, 8 GB)', created.returncode == 0, tail(created.stderr, 5)):
            return 1

        created = create(SANDBOX_B, tag, cpus='2', memory='2g')
        expect(0, 'create sandbox B (mountless)', created.returncode == 0, tail(created.stderr, 5))
        lock_egress(0, SANDBOX_A)
        lock_egress(0, SANDBOX_B)
        record(0, 'workspace', 'INFO', sh(SANDBOX_A, 'pwd; git log --oneline -1').stdout.strip().replace('\n', ' | '))
        pointed = backend.configure_git(SANDBOX_A, sh(SANDBOX_A, 'pwd').stdout.strip(), 'pipeline-gate',
                                        'pipeline-gate@users.noreply.github.com')
        listed = sh(SANDBOX_A, 'git ls-remote origin HEAD')
        expect(0, 'origin re-pointed at the source mount, reachable (as the runner does)',
               pointed.returncode == 0 and listed.returncode == 0, tail(listed.stderr or pointed.stderr, 3))
        test_1_exit_codes()
        copy_kit(SANDBOX_A, memory_dir)
        claude_ok = test_2_claude(sys.stdin.isatty())
        test_3_coexistence(listeners_before)
        test_5_rules(setup_hosts)
        test_4_setup_and_checks(config, setup_hosts, args.skip_checks)

        if args.full and claude_ok:
            test_6_full()

        test_8_exfil(repo)

        if claude_ok:
            test_9_stage_argv()

        test_7_kill_and_stop()

        if claude_ok:
            test_2_login_survives_rm(tag)
    finally:
        if not args.keep:
            remove(SANDBOX_A, SANDBOX_B, SANDBOX_C)
            shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if r['status'] == 'FAIL']
    report = os.path.join(WORK_ROOT, time.strftime('report-%Y%m%d-%H%M%S.json'))

    with open(report, 'w') as handle:
        json.dump({'template': tag, 'repo': repo, 'results': results}, handle, indent=2)

    print(f'\n{len(results)} checks, {len(failed)} FAIL — report {report}')

    for item in failed:
        print(f'  FAIL {item["test"]} {item["check"]}: {item["detail"][:200]}')

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
