"""Sandbox backend: one Docker Sandboxes (`sbx`) microVM per pipeline step. The VM is disposable; the host holds the
truth (the runner's clean clone and the refs it fetches out of each VM).

Verified sbx facts (sandbox/README.md "Known sbx facts") this relies on: `sbx exec` passes the exit code and auto-starts
a stopped sandbox; it forwards no signal, so a claude turn is ended from inside the VM; `sbx stop` ends every VM process
and removes the host remote `sandbox-<name>` until the next start (new port); a missing sandbox makes exec exit 1;
`sbx cp` keeps symlinks and fifos as they are; `sbx policy rm` needs --force without a terminal.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent / 'sbx-host'))  # after PYTHONPATH, so a scratch copy can stand in

import sbx_host  # noqa: E402

HOME = Path.home()
PIPELINE_DIR = Path(__file__).resolve().parent
AGENT_DOMAINS = ('anthropic.com', 'claude.ai', 'claude.com')  # kit rules outside these are denied per sandbox
LEAK_PROBE = 'https://example.com'
STAGE_ENV = {
    'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-5-5',
    'ANTHROPIC_DEFAULT_SONNET_MODEL': 'claude-sonnet-5-5',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'claude-haiku-4-5-20251001',
    'CLAUDE_CODE_SUBAGENT_MODEL': 'claude-sonnet-5-5',
    'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': '1',
}
KIT_ITEMS = ('CLAUDE.md', 'skills', 'agents')
KIT_SCRIPTS = ('heavy', 'kill-own.sh', 'drizzle-generate.py', 'transcripts.py')
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
MAX_COPY_OUT_BYTES = 2_000_000
MAX_FETCH_BYTES = 200_000_000
BUILD_TEMPLATE = PIPELINE_DIR / 'sandbox' / 'build-template.sh'
BUILD_TIMEOUT_SECONDS = 1800
# sbx never adds docker.io/library/ itself, and `sbx template load` stores the image under that full name.
TEMPLATE_REPOSITORY = 'docker.io/library/pipeline-sandbox'
TEMPLATE_CACHE = Path(os.environ.get('XDG_CACHE_HOME') or HOME / '.cache') / 'pipeline'  # build-template.sh's tars

# Where things live inside a sandbox. The test fake keeps each sandbox in a host directory and points these there, so
# no path is ever rewritten between host and VM.
HOME_TEMPLATE = '/home/agent'
WORKSPACE_TEMPLATE = '{source}'  # --clone puts the VM's own clone at the source's host path
SOURCE_MOUNT = '/run/sandbox/source'  # --clone mounts the source read-only here, live


class BackendError(Exception):
    pass


class SandboxMissing(BackendError):
    pass


def home(name):
    return HOME_TEMPLATE.format(name=name)


def workspace(name, source):
    return WORKSPACE_TEMPLATE.format(name=name, source=source)


def kit_dir(name):
    return f'{home(name)}/.claude'


def memory_dir(name):
    return f'{home(name)}/memory'


def plans_dir(name, feature):
    return f'{home(name)}/plans/{feature}'


def stage_settings(name):
    """Replaces the kit's settings.json (it defaults to bypassPermissions); stages still pass --permission-mode auto.
    No hooks: the VM is the boundary. Attribution off, in the object form older CLIs also read."""
    return json.dumps({'availableModels': ['opus', 'sonnet', 'haiku'], 'autoMemoryDirectory': memory_dir(name),
                       'attribution': {'commit': '', 'pr': '', 'sessionUrl': False}})


def sandbox_name(repo_short, feature, step_id):
    name = f'pl-{repo_short}-{feature}-{step_id}'.lower()

    if not NAME.match(name):
        raise BackendError(f'sandbox name {name!r} is invalid or longer than 63 characters: shorten the feature name')

    return name


# ---------------------------------------------------------------------------------------------- sbx calls


def sbx_env():
    """No telemetry; a 40 GB clone disk (a monorepo's node_modules and Docker images outgrow the default)."""
    return dict(os.environ, SBX_NO_TELEMETRY='1', DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE='40g')


def run(argv, timeout=120, stdin=None, check=False):
    try:
        result = subprocess.run([str(argument) for argument in argv], capture_output=True, text=True, errors='replace',
                                timeout=timeout, env=sbx_env(), input=stdin,
                                stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        output = error.stdout.decode(errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        result = subprocess.CompletedProcess(argv, 124, output, f'timeout after {timeout}s')

    if check and result.returncode != 0:
        raise BackendError(f'{" ".join(str(a) for a in argv[:4])}… exited {result.returncode}: '
                           f'{(result.stderr or result.stdout).strip()[-500:]}')

    return result


def sbx(*arguments, timeout=120, check=False, stdin=None):
    return run(['sbx', *arguments], timeout=timeout, check=check, stdin=stdin)


def exec_argv(name, command, env=None, workdir=None, interactive=False):
    argv = ['sbx', 'exec'] + (['-i'] if interactive else [])

    for key, value in (env or {}).items():
        argv += ['-e', f'{key}={value}']

    if workdir:
        argv += ['-w', str(workdir)]

    return argv + [name, '--', *(str(argument) for argument in command)]


def exec(name, command, env=None, workdir=None, timeout=120, stdin=None):
    return run(exec_argv(name, command, env, workdir, interactive=stdin is not None), timeout=timeout, stdin=stdin)


def sh(name, script, env=None, workdir=None, timeout=120):
    return exec(name, ['bash', '-c', script], env=env, workdir=workdir, timeout=timeout)


def statuses():
    """{sandbox name: status} from one `sbx ls --json`."""
    result = sbx('ls', '--json', check=True)

    try:
        return {item['name']: item.get('status') for item in json.loads(result.stdout)}
    except (ValueError, KeyError, TypeError):
        raise BackendError(f'unparsed `sbx ls --json`: {result.stdout[:300]}') from None


def status(name):
    return statuses().get(name)


def ensure_running(name):
    """A stopped sandbox starts on its first exec (verified: claude and the gateway work after it)."""
    current = status(name)

    if current is None:
        raise SandboxMissing(f'sandbox {name} does not exist')

    if current != 'running':
        started = exec(name, ['true'], timeout=300)

        if started.returncode != 0:
            raise BackendError(f'sandbox {name} did not start: {(started.stderr or started.stdout).strip()[-300:]}')


def template_tag(claude='host'):
    """The template a sandbox is created from: the host's Node plus `claude` ('host': the host's Claude Code, or a
    pinned version like '2.1.286')."""
    node = run(['node', '--version'], check=True).stdout.strip().lstrip('v')

    if claude == 'host':
        claude = run(['claude', '--version'], check=True).stdout.split()[0]

    return f'{TEMPLATE_REPOSITORY}:claude-{claude}-node-{node}'


def build_template(claude='host'):
    """Builds and loads a template with build-template.sh ('host': the host's Claude Code, else that version). Returns
    (tag, result): the tag the script built (its last stdout line; the host may have updated meanwhile), or None."""
    argv = [BUILD_TEMPLATE] + ([] if claude == 'host' else [claude])

    try:
        result = run(argv, timeout=BUILD_TIMEOUT_SECONDS)
    except OSError as error:
        return None, subprocess.CompletedProcess(argv, 127, '', f'{BUILD_TEMPLATE}: {error}')

    built = (result.stdout.strip().splitlines() or [''])[-1].strip()
    ok = result.returncode == 0 and built.startswith(TEMPLATE_REPOSITORY.rpartition('/')[2] + ':')

    return (f'docker.io/library/{built}' if ok else None), result


def templates():
    """[(tag, id, bytes)] of the pipeline templates sbx holds, from `sbx template ls --json`."""
    result = sbx('template', 'ls', '--json', check=True)

    try:
        images = json.loads(result.stdout) or []

        return [(f"{image['repository']}:{image['tag']}", image.get('id') or '', image.get('size') or 0)
                for image in images if image['repository'] == TEMPLATE_REPOSITORY]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise BackendError(f'unparsed `sbx template ls --json`: {result.stdout[:300]}') from None


def template_loaded(tag):
    return tag in [listed for listed, _, _ in templates()]


def built_images():
    """The pipeline images docker holds (build-template.sh builds there, then loads into sbx), as full tags."""
    try:
        result = run(['docker', 'image', 'ls', TEMPLATE_REPOSITORY.rpartition('/')[2], '--format',
                      '{{.Repository}}:{{.Tag}}'], check=True)
    except OSError:
        return []

    return [f'docker.io/library/{line.strip()}' for line in result.stdout.splitlines() if ':' in line]


def sandbox_image(name):
    """The template a sandbox was created from (a sandbox keeps needing it, stopped too)."""
    result = sbx('inspect', '--json', name, check=True)

    try:
        return json.loads(result.stdout)['image']
    except (ValueError, KeyError, TypeError):
        raise BackendError(f'unparsed `sbx inspect --json {name}`: {result.stdout[:300]}') from None


def remove_template(tag):
    """Removes a template from sbx, then its source image from docker; either one already gone is fine."""
    removed = sbx('template', 'rm', '--force', tag, timeout=300)

    if removed.returncode != 0 and 'not found' not in removed.stderr + removed.stdout:
        raise BackendError(f'sbx template rm {tag}: {(removed.stderr or removed.stdout).strip()[-300:]}')

    try:
        image = run(['docker', 'image', 'rm', tag.removeprefix('docker.io/library/')], timeout=300)
    except OSError:
        return

    if image.returncode != 0 and 'No such image' not in image.stderr + image.stdout:
        raise BackendError(f'docker image rm {tag}: {(image.stderr or image.stdout).strip()[-300:]}')


def create(name, tag, source=None, clone=False, cpus='6', memory='8g'):
    argv = ['create', '--name', name, '-t', tag, '--pull', 'never', '--cpus', cpus, '-m', memory]

    if clone:
        argv.append('--clone')

    return sbx(*argv, 'claude', *([str(source)] if source else []), timeout=600)


def policy(*arguments):
    """sbx policy call; rm needs --force without a terminal (else: 'stdin is not a terminal')."""
    return sbx('policy', *arguments, *(['--force'] if arguments[0] == 'rm' else []))


def kit_rules(name):
    """Network allow rules the claude kit added to this sandbox: (hosts, raw) or (None, raw) when unreadable."""
    result = sbx('policy', 'ls', name, '--source', 'kit', '--type', 'network', '--json')

    try:
        rules = json.loads(result.stdout)['rules']
    except (ValueError, KeyError, TypeError):
        return None, result.stdout + result.stderr

    hosts = {host for rule in rules if rule.get('decision') == 'allow' for host in rule.get('resources') or []}

    return sorted(hosts), result.stdout


def agent_domain(host):
    bare = host.split(':')[0].lstrip('*.')

    return any(bare == domain or bare.endswith('.' + domain) for domain in AGENT_DOMAINS)


def lock_egress(name):
    """Denies, per sandbox, every host the claude kit allows outside AGENT_DOMAINS. Returns (kit hosts, denied)."""
    hosts, raw = kit_rules(name)

    if hosts is None:
        raise BackendError(f'{name}: unparsed `sbx policy ls --json`: {raw.strip()[-300:]}')

    extra = [host for host in hosts if not agent_domain(host)]

    for host in extra:
        result = policy('deny', 'network', '--sandbox', name, host)

        if result.returncode != 0:
            raise BackendError(f'{name}: sbx policy deny {host}: {(result.stderr or result.stdout).strip()[-300:]}')

    return hosts, extra


def reachable(name, url):
    """Any origin response counts as reachable; a curl error or a proxy-looking refusal does not. (The sandbox proxy
    accepts every TCP connect and drops denied ones, so only a protocol reply proves reach.)"""
    result = sh(name, f"curl -sS -m 15 -w '\\n%{{http_code}}' {url} | tail -c 600", timeout=40)
    body, _, code = result.stdout.strip().rpartition('\n')
    refused = code in ('000', '') or (code in ('403', '407', '451', '502', '503')
                                      and re.search(r'block|denied|policy|sandbox|not allowed', body, re.I))
    ok = result.returncode == 0 and not refused

    return ok, f'rc={result.returncode} http={code or "-"} {" ".join(body.split())[:80]!r}'


# ---------------------------------------------------------------------------------------------- files


def copy_in(name, host_path, vm_dir):
    """Copies a host file or directory into vm_dir (a directory lands inside an existing one, so the target goes
    first). Follows host symlinks (skills may be links)."""
    target = f'{vm_dir.rstrip("/")}/{Path(host_path).name}'
    prepared = sh(name, f'rm -rf {quote(target)} && mkdir -p {quote(vm_dir)}')

    if prepared.returncode != 0:
        raise BackendError(f'{name}: preparing {target}: {prepared.stderr.strip()[-300:]}')

    sbx('cp', '-L', str(host_path), f'{name}:{vm_dir.rstrip("/")}/', timeout=600, check=True)


def write_file(name, vm_path, text):
    with tempfile.TemporaryDirectory(prefix='pipeline-in-') as directory:
        path = Path(directory) / Path(vm_path).name
        path.write_text(text)
        copy_in(name, path, str(Path(vm_path).parent))


def copy_out(name, vm_path, limit=MAX_COPY_OUT_BYTES):
    """A file the agent wrote, or None when it has none. It lands in a fresh directory and is read only when it is a
    regular file within the size cap: a symlink, fifo or device the agent left there is refused, never followed."""
    with tempfile.TemporaryDirectory(prefix='pipeline-out-') as directory:
        result = sbx('cp', f'{name}:{vm_path}', f'{directory}/', timeout=300)

        if result.returncode != 0:
            if 'not found' in (result.stderr + result.stdout):
                return None

            raise BackendError(f'sbx cp {name}:{vm_path}: {(result.stderr or result.stdout).strip()[-300:]}')

        path = Path(directory) / Path(vm_path).name

        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise BackendError(f'{vm_path} in {name} is not a regular file ({error.strerror})') from None

        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())

            if not stat.S_ISREG(info.st_mode):
                raise BackendError(f'{vm_path} in {name} is not a regular file')

            if info.st_size > limit:
                raise BackendError(f'{vm_path} in {name} is {info.st_size} bytes (cap {limit})')

            return handle.read().decode(errors='replace')


def copy_kit(name, memory_source=None):
    """The kit, fresh on every run: CLAUDE.md, skills and agents; the repo's memory (read-only by convention, the
    stage's autoMemoryDirectory); the helpers stages call; and the stage settings.json replacing the kit's."""
    claude = kit_dir(name)

    for item in KIT_ITEMS:
        if (HOME / '.claude' / item).exists():
            copy_in(name, HOME / '.claude' / item, claude)

    for script in KIT_SCRIPTS:
        copy_in(name, PIPELINE_DIR / script, f'{claude}/pipeline')

    if memory_source and Path(memory_source).is_dir():
        with tempfile.TemporaryDirectory(prefix='pipeline-memory-') as directory:
            staged = Path(directory) / Path(memory_dir(name)).name
            shutil.copytree(memory_source, staged, symlinks=False)
            copy_in(name, staged, home(name))
    else:
        sh(name, f'rm -rf {quote(memory_dir(name))} && mkdir -p {quote(memory_dir(name))}')

    write_file(name, f'{claude}/settings.json', stage_settings(name))


def copy_feature(name, feature, feature_dir):
    """The whole feature folder, minus the runner's .pipeline state, at plans_dir(name, feature)."""
    target = plans_dir(name, feature)
    prepared = sh(name, f'rm -rf {quote(target)} && mkdir -p {quote(target)}')

    if prepared.returncode != 0:
        raise BackendError(f'{name}: preparing {target}: {prepared.stderr.strip()[-300:]}')

    touches = []

    for entry in sorted(Path(feature_dir).iterdir()):
        if entry.name == '.pipeline':
            continue

        sbx('cp', '-L', str(entry), f'{name}:{target}/', timeout=300, check=True)
        files = [entry] if entry.is_file() else sorted(path for path in entry.rglob('*') if path.is_file())
        touches += [f'touch -m -d @{int(path.stat().st_mtime)} {quote(target + "/" + str(path.relative_to(feature_dir)))}'
                    for path in files]

    # Copies keep the host's mtimes, so a stage's file gate can tell a file it wrote from one it was given.
    if touches:
        touched = sh(name, ' && '.join(touches))

        if touched.returncode != 0:
            raise BackendError(f'{name}: setting mtimes under {target}: {touched.stderr.strip()[-300:]}')


def quote(text):
    return "'" + str(text).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- lifecycle


def interrupt(name, signal_name='INT'):
    """`sbx exec` forwards no signal: SIGINT to claude inside the VM ends its turn cleanly (verified)."""
    return exec(name, ['pkill', f'-{signal_name}', '-x', 'claude'], timeout=60)


def quiesce(name):
    """Ends every process in the VM (a stray one could move the branch after the gates). Its disk stays; the next
    exec starts it again. A missing sandbox is already quiet."""
    if status(name) is not None:
        sbx('stop', name, timeout=300)


stop = quiesce


def remove(name):
    return sbx('rm', '--force', name, timeout=300)


def setup(name, workdir, commands, egress, log):
    """Runs the repo's setup commands with only its setup hosts reachable, then removes them and proves the agent
    phase is sealed again (example.com and a setup host unreachable). Returns (failures, seal_failures), both [] when
    green: a failed allow or command can pass on a retry; a failed rm or a canary leak leaves the agent phase open."""
    if egress:
        allowed = policy('allow', 'network', '--sandbox', name, ','.join(egress))

        if allowed.returncode != 0:
            return [f'sbx policy allow {",".join(egress)}: {(allowed.stderr or allowed.stdout).strip()[-300:]}'], []

    failures = []
    seal_failures = []

    try:
        for command in commands:
            result = sh(name, command, workdir=workdir, timeout=3600)
            log(f'setup: {command} → exit {result.returncode}\n{result.stdout}{result.stderr}')

            if result.returncode != 0:
                failures.append(f'setup `{command}` exited {result.returncode}:\n'
                                + '\n'.join((result.stdout + result.stderr).strip().splitlines()[-30:]))
                break
    finally:
        for host in egress:
            removed = policy('rm', 'network', '--sandbox', name, '--resource', host)

            if removed.returncode != 0:
                seal_failures.append(f'sbx policy rm {host}: {(removed.stderr or removed.stdout).strip()[-300:]}')

    for url in [LEAK_PROBE] + [f'https://{host}/' for host in egress[:1]]:
        ok, detail = reachable(name, url)
        log(f'egress canary {url}: {"REACHABLE" if ok else "blocked"} {detail}')

        if ok:
            seal_failures.append(f'egress canary: {url} is reachable from {name} after setup ({detail}); the agent '
                                 'phase must reach Anthropic only. Check `sbx policy ls` and the global deny-all policy.')

    return failures, seal_failures


# ---------------------------------------------------------------------------------------------- git


def remote_url(source, name):
    return subprocess.run(['git', '-C', str(source), 'remote', 'get-url', f'sandbox-{name}'], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60)


def fetch_branch(name, source, branch, ref, base):
    """Fetches the VM's branch, and only it, into `ref` of the host clone `source`; every object is fsck-checked and
    the new objects are size-capped (git has no fetch-side object size limit). Returns the fetched SHA."""
    ensure_running(name)
    url = remote_url(source, name)

    if url.returncode != 0:
        # sbx rewrites the remote on every VM start and stop (its 30 s auto-stop included): a stop between our
        # ensure_running and the read leaves none. A clean restart writes it again.
        quiesce(name)
        ensure_running(name)
        url = remote_url(source, name)

    if url.returncode != 0:
        raise BackendError(f'{source} has no remote sandbox-{name} although the sandbox runs: {url.stderr.strip()}')

    fetched = subprocess.run(['git', '-C', str(source), '-c', 'transfer.fsckObjects=true', '-c', 'fetch.fsckObjects=true',
                              'fetch', '--quiet', '--no-tags', '--no-write-fetch-head', url.stdout.strip(),
                              f'+refs/heads/{branch}:{ref}'],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900)

    if fetched.returncode != 0:
        raise BackendError(f'fetching {branch} from {name}: {fetched.stderr.strip()[-500:]}')

    size = subprocess.run(['git', '-C', str(source), 'rev-list', '--objects', '--disk-usage', ref, f'^{base}'],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)

    if size.returncode == 0 and size.stdout.strip().isdigit() and int(size.stdout) > MAX_FETCH_BYTES:
        subprocess.run(['git', '-C', str(source), 'update-ref', '-d', ref], capture_output=True, timeout=60)
        raise BackendError(f'{branch} in {name} adds {int(size.stdout) // 1_000_000} MB of objects '
                           f'(cap {MAX_FETCH_BYTES // 1_000_000} MB); not taken')

    return subprocess.run(['git', '-C', str(source), 'rev-parse', ref], capture_output=True, text=True,
                          timeout=60).stdout.strip()


def configure_git(name, workdir, user, email):
    """--clone copies the source's config, so the VM's origin is GitHub, which the VM can't reach, and every VM start
    resets origin's URL to it: rewrite that URL to the source mount instead (an insteadOf survives the reset). The
    runner keeps the mount's base branch on origin's tip, so repo tools that ask origin see it too. The VM's
    ~/.gitconfig carries your git identity: a repo-local user.name/user.email (the App's bot) overrides it for every
    agent commit and survives a restart too (pilot 2026-10-01)."""
    script = ('url=$(git config --get remote.origin.url) && git config "url.$0.insteadOf" "$url" && '
              'git config user.name "$1" && git config user.email "$2"')

    return exec(name, ['bash', '-c', script, SOURCE_MOUNT, user, email], workdir=workdir, timeout=60)


def refresh_base(name, workdir, base):
    """The VM fetches the base from the host clone it was made from (origin = the live source mount)."""
    return exec(name, ['git', 'fetch', '--quiet', 'origin', base], workdir=workdir, timeout=600)
