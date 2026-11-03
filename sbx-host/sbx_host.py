"""Host concerns every sbx user on this box shares: the pipeline's stage VMs (pl-*) and ci-runner's job VMs (ci-*) run
in one capped daemon, sbx.service. This module owns that daemon's guard, which VMs exist and what each reserves of the
unit's memory, one admission rule for both users (fits; claim takes the reservation, release gives it back), and the
peak samples that size the reserves. CLI: sbx-host (beside this file). Config: ~/.config/sbx-host/config.json
(DEFAULT_CONFIG's keys).
"""
import contextlib
import fcntl
import json
import math
import os
import subprocess
import time
from pathlib import Path

HOME = Path.home()
UNIT = 'sbx.service'
CONFIG = HOME / '.config/sbx-host/config.json'
STATE = HOME / '.local/state/sbx-host'  # peaks.jsonl, pending/<name> (claim), lock
PIPELINE_SHARE = HOME / '.local/share/pipeline'  # stage VMs hold an fd in here (their clone): the kind's fallback
CGROUP_ROOT = Path('/sys/fs/cgroup')
PROC = Path('/proc')
SHIM = b'containerd-shim-nerdbox-v1'  # one per running VM; its cwd is the VM's bundle (config.json names the VM)
CI_LANE_UNIT = 'ci-runner@1.service'  # its slot always takes a job, so its VM's reserve is kept free while it waits
# A VM's host memory past its allotment. Memory the guest frees goes back to the host (libkrun free page reporting:
# a shim at HWM 6.0 GiB held 1.9 anon, 2026-10-26); the host peak is mostly the guest's page cache. CI VMs peaked 0.1-0.9
# GiB past their 7 GiB, stage VMs under their 8 (peaks()).
VM_OVERHEAD_MIB = 512
GIB = 2**30
MIB = 2**20
PEAK_DAYS = 30
# reserve: MiB a VM of each kind is counted at (pl: stage VMs, ci: CI job VMs; others: allotment + overhead).
# floorGib: host MemAvailable below which nothing new starts. budget: what the reserves are summed against, "high"
# (MemoryHigh: never throttled) or "max+swap-1G" (MemoryMax + MemorySwapMax − 1 GiB: never OOM-killed).
DEFAULT_CONFIG = {'reserve': {'pl': 8192 + VM_OVERHEAD_MIB, 'ci': 7168 + VM_OVERHEAD_MIB}, 'floorGib': 10,
                  'budget': 'high'}
BUDGETS = ('high', 'max+swap-1G')


class HostError(Exception):
    pass


def load_config():
    try:
        value = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        value = {}
    except (OSError, ValueError) as error:
        raise HostError(f'{CONFIG}: unreadable ({error})') from None

    if not isinstance(value, dict):
        raise HostError(f'{CONFIG}: not a JSON object')

    if not isinstance(value.get('reserve') or {}, dict):
        raise HostError(f"{CONFIG}: reserve is an object, not {value['reserve']!r}")

    config = dict(DEFAULT_CONFIG, **{key: value[key] for key in DEFAULT_CONFIG if key in value})
    config['reserve'] = dict(DEFAULT_CONFIG['reserve'], **(value.get('reserve') or {}))

    if config['budget'] not in BUDGETS:
        raise HostError(f"{CONFIG}: budget {config['budget']!r} is not one of {', '.join(BUDGETS)}")

    return config


# ---------------------------------------------------------------------------------------------- the daemon


def systemctl(*arguments):
    try:
        return subprocess.run(['systemctl', '--user', *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(arguments, 1, '', str(error))


def active():
    return systemctl('is-active', '--quiet', UNIT).returncode == 0


def guard():
    """Why no sbx call may run now, or None. Any sbx call autostarts a daemon when none answers on its socket, outside
    the unit's caps (verified): so none while the unit is down, and none with another XDG_STATE_HOME (the socket lives
    there, so sbx would start a second daemon)."""
    if not active():
        return f'{UNIT} is not active (systemctl --user start {UNIT})'

    if os.environ.get('XDG_STATE_HOME', str(HOME / '.local/state')) != str(HOME / '.local/state'):
        return 'XDG_STATE_HOME is not the default: sbx would start a second daemon'

    return None


def cgroup():
    """The unit's cgroup directory, or None."""
    group = systemctl('show', '-p', 'ControlGroup', '--value', UNIT).stdout.strip()

    return CGROUP_ROOT / group.lstrip('/') if group else None


def vms():
    """[{name, status, memory_mib}] from one `sbx ls --json` (never an exec: that would start a stopped VM)."""
    problem = guard()

    if problem:
        raise HostError(f'no sbx call: {problem}')

    try:
        result = subprocess.run(['sbx', 'ls', '--json'], capture_output=True, text=True, timeout=60,
                                stdin=subprocess.DEVNULL, env=dict(os.environ, SBX_NO_TELEMETRY='1'))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HostError(f'sbx ls: {error}') from None

    if result.returncode != 0:
        raise HostError(f'sbx ls exited {result.returncode}: {(result.stderr or result.stdout).strip()[-300:]}')

    try:
        return [{'name': item['name'], 'status': item.get('status'), 'memory_mib': item.get('memory_mib') or 0}
                for item in json.loads(result.stdout)]
    except (ValueError, KeyError, TypeError):
        raise HostError(f'unparsed `sbx ls --json`: {result.stdout[:300]}') from None


def kind(name):
    """pl (a pipeline stage VM), ci (a CI job VM) or other, by the names both users give them."""
    return 'pl' if name.startswith('pl-') else 'ci' if name.startswith('ci-') else 'other'


# ---------------------------------------------------------------------------------------------- memory


def read_cgroup(group, name):
    try:
        return (group / name).read_text()
    except (OSError, TypeError):
        return ''


def number(text):
    """A cgroup value in bytes: inf for `max`, None when unreadable."""
    text = text.strip()

    return math.inf if text == 'max' else int(text) if text.isdigit() else None


def field(text, key):
    return next((line.split()[1] for line in text.splitlines() if line.split()[:1] == [key]), None)


def shims():
    """[(pid directory, cgroup path)] of every VM shim on the host."""
    found = []

    for entry in PROC.iterdir() if PROC.is_dir() else ():
        try:
            if entry.name.isdigit() and SHIM in (entry / 'cmdline').read_bytes():
                found.append((entry, (entry / 'cgroup').read_text().strip().rpartition(':')[2]))
        except OSError:
            continue

    return found


def reading():
    """sbx.service's memory in bytes (inf: no cap, None: unreadable), OOM kills, swap-outs refused at the swap cap,
    memory pressure (PSI full avg60, %), and how many VM shims sit in the unit (fewer than all ⇒ a VM escaped the caps: a daemon started by hand)."""
    group = cgroup()
    pressure = next((line for line in read_cgroup(group, 'memory.pressure').splitlines() if line.startswith('full ')), '')
    avg60 = next((part.partition('=')[2] for part in pressure.split() if part.startswith('avg60=')), None)
    found = shims()
    stat_text, events = read_cgroup(group, 'memory.stat'), read_cgroup(group, 'memory.events')

    return {**{key: number(read_cgroup(group, f'memory.{key}'))
               for key in ('current', 'max', 'high', 'peak')},
            'swap': number(read_cgroup(group, 'memory.swap.current')),
            'swap_max': number(read_cgroup(group, 'memory.swap.max')),
            'anon': number(field(stat_text, 'anon') or ''), 'oom': number(field(events, 'oom_kill') or ''),
            'swap_fail': number(field(read_cgroup(group, 'memory.swap.events'), 'fail') or ''),
            'high_events': number(field(events, 'high') or ''),
            'avg60': float(avg60) if avg60 and avg60.replace('.', '', 1).isdigit() else None,
            'inside': sum(1 for _, path in found if group is not None and CGROUP_ROOT / path.lstrip('/') == group),
            'total': len(found)}


def mem_available_gib():
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass

    return 0.0


def budget_mib(config, memory):
    """What the reserves are summed against (config budget), or None when that cap is unset or unreadable."""
    if config['budget'] == 'high':
        value = memory['high']
    else:
        value = None if None in (memory['max'], memory['swap_max']) else memory['max'] + memory['swap_max'] - GIB

    return None if value is None or value == math.inf else value / MIB


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def process_start(pid):
    """A process's start time (clock ticks since boot), so a reused pid never keeps a dead holder's claim; None when
    unreadable."""
    try:
        return int((PROC / str(pid) / 'stat').read_text().rpartition(')')[2].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def holder(path):
    """A pending file's {pid, kind, start} while its process lives, else None."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    pid = value.get('pid') if isinstance(value, dict) else None

    if not isinstance(pid, int) or not pid_alive(pid) or value.get('start') != process_start(pid):
        return None

    return value


def pending():
    """[(name, kind)] claims (claim): a VM about to start, or one a run keeps between its stages (a stopped VM counts
    only through its claim); a dead holder's file counts for nothing."""
    found = []

    for path in sorted((STATE / 'pending').glob('*')):
        value = holder(path)

        if value:
            found.append((path.name, value.get('kind') or kind(path.name)))

    return found


def ci_lane():
    return systemctl('is-active', '--quiet', CI_LANE_UNIT).returncode == 0


def reservations(config=None, listed=None, exclude=None):
    """[(name, kind, MiB)]: every VM not stopped, the guaranteed CI lane while slot 1 has no VM (`ci lane`), and pending
    reservations not up yet. A VM is counted at its kind's reserve, others at allotment + overhead. `exclude` names a VM
    being admitted (it must not count twice)."""
    config = config or load_config()
    listed = vms() if listed is None else listed
    reserve = config['reserve']
    counted = []

    for vm in listed:
        if vm['status'] != 'stopped' and vm['name'] != exclude:
            vm_kind = kind(vm['name'])
            counted.append((vm['name'], vm_kind, reserve.get(vm_kind) or vm['memory_mib'] + VM_OVERHEAD_MIB))

    names = {name for name, _, _ in counted} | ({exclude} if exclude else set())

    for name, pending_kind in pending():
        if name not in names:
            counted.append((name, pending_kind, reserve.get(pending_kind, 0)))
            names.add(name)

    if not any(name.startswith('ci-1-') for name in names) and ci_lane():
        counted.append(('ci lane', 'ci', reserve['ci']))

    return counted


def headroom(vm_kind, name, config, listed, memory=None):
    """(reserved MiB without `name`, this kind's reserve, budget MiB or None, host free GiB). `listed` and `memory`
    reuse an `sbx ls` and a reading() already taken."""
    counted = reservations(config, listed, exclude=name)

    return (sum(mib for _, _, mib in counted), config['reserve'].get(vm_kind, 0),
            budget_mib(config, memory or reading()), mem_available_gib())


def fits(vm_kind, name=None, config=None, listed=None, memory=None):
    """(ok, why) for starting a VM of this kind now: every reservation plus its own stays within the budget, and the
    host keeps floorGib free. `why` says what failed or, when it fits, what it fits in."""
    config = config or load_config()
    reserved, need, budget, free = headroom(vm_kind, name, config, listed, memory)

    if free < config['floorGib']:
        return False, f"{floor_tenth(free)} GiB free < {config['floorGib']} GiB"

    if budget is not None and reserved + need > budget:
        return False, f'reserved {gib(reserved)} + {gib(need)} GiB > {gib(budget)} ({config["budget"]})'

    return True, f'reserved {gib(reserved)} + {gib(need)} GiB' + (f' of {gib(budget)}' if budget is not None else '')


def room(vm_kind, config=None, listed=None):
    """How many more VMs of this kind fit now, one beside the other (inf: no budget to fill)."""
    config = config or load_config()
    reserved, need, budget, free = headroom(vm_kind, None, config, listed)

    if free < config['floorGib']:
        return 0

    if budget is None or not need:
        return math.inf

    return max(0, int((budget - reserved) // need))


def claim(vm_kind, name, always=False, config=None):
    """(ok, why) of fits, taking the VM's reservation (pending/<name>) when it fits, or anyway with `always` (the CI
    lane). Kept until release(name) or the process ends. One claim per process: its claim on another VM goes first, and
    dead holders' files are deleted."""
    config = config or load_config()
    directory = STATE / 'pending'

    with lock():
        directory.mkdir(parents=True, exist_ok=True)

        for path in directory.glob('*'):
            value = holder(path)

            if value is None or (value['pid'] == os.getpid() and path.name != name):
                path.unlink(missing_ok=True)

        ok, why = fits(vm_kind, name, config)

        if ok or always:
            temporary = STATE / f'pending-{name}.tmp'
            temporary.write_text(json.dumps({'pid': os.getpid(), 'kind': vm_kind,
                                             'start': process_start(os.getpid())}))
            os.replace(temporary, directory / name)

    return ok, why


def release(name):
    """Gives back this process's claim on the VM (claim), if it holds one."""
    path = STATE / 'pending' / name

    with lock():
        value = holder(path)

        if value and value['pid'] == os.getpid():
            path.unlink(missing_ok=True)


def gib(mib):
    return f'{mib / 1024:.1f}'


def floor_tenth(value):
    """One decimal, rounded down: 9.99 GiB free is below a 10 GiB floor and must not read 10.0."""
    return f'{int(value * 10) / 10:.1f}'


@contextlib.contextmanager
def lock():
    STATE.mkdir(parents=True, exist_ok=True)

    with (STATE / 'lock').open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------------------------------------- peaks


def shim_vm(entry):
    """(name, kind) of a shim's VM: the hostname in its bundle (its cwd), else pl when it holds an fd under
    PIPELINE_SHARE (a stage's clone) and ci otherwise."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        name = json.loads((entry / 'cwd' / 'config.json').read_text()).get('hostname')

        if name:
            return name, kind(name)

    with contextlib.suppress(OSError):
        for fd in (entry / 'fd').iterdir():
            with contextlib.suppress(OSError):
                if os.readlink(fd).startswith(str(PIPELINE_SHARE)):
                    return None, 'pl'

    return None, 'ci'


def status_kib(entry, key):
    for line in (entry / 'status').read_text().splitlines():
        if line.startswith(f'{key}:'):
            return int(line.split()[1])

    return 0


def sample(now=None):
    """Appends each VM shim's host peak (VmHWM) and swap to peaks.jsonl and drops lines older than PEAK_DAYS. Called at
    the end of every stage and CI job, so each VM's last line is its lifetime peak. Returns the new lines."""
    now = time.time() if now is None else now
    lines = []

    for entry, _ in shims():
        try:
            name, vm_kind = shim_vm(entry)
            lines.append({'at': int(now), 'name': name, 'kind': vm_kind, 'hwm_mib': status_kib(entry, 'VmHWM') // 1024,
                          'swap_mib': status_kib(entry, 'VmSwap') // 1024})
        except (OSError, ValueError):
            continue

    path = STATE / 'peaks.jsonl'
    STATE.mkdir(parents=True, exist_ok=True)

    with lock():
        try:
            kept = [line for line in path.read_text().splitlines()
                    if (json.loads(line).get('at') or 0) >= now - PEAK_DAYS * 86400]
        except (OSError, ValueError):
            kept = []

        temporary = path.with_name('peaks.jsonl.tmp')
        temporary.write_text(''.join(line + '\n' for line in kept + [json.dumps(line) for line in lines]))
        os.replace(temporary, path)

    return lines


def peaks(days=PEAK_DAYS):
    """{kind: [each VM's highest VmHWM, MiB]} over the last `days`."""
    best = {}

    try:
        text = (STATE / 'peaks.jsonl').read_text()
    except OSError:
        return {}

    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue

        if (value.get('at') or 0) >= time.time() - days * 86400 and value.get('name'):
            key = (value['kind'], value['name'])
            best[key] = max(best.get(key, 0), value.get('hwm_mib') or 0)

    found = {}

    for (vm_kind, _), mib in best.items():
        found.setdefault(vm_kind, []).append(mib)

    return found


def p95(values):
    ordered = sorted(values)

    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None
