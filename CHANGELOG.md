# Changelog

What changed for you in each release, newest first. Each release says whether to rerun `sandbox/gate.py`
([INSTALL.md](INSTALL.md), Update). Detail lives in [README.md](README.md) and [INSTALL.md](INSTALL.md).

## Unreleased

**Rerun the gate:** yes (its preflight now validates the recipe, and its kit test copies the runner's kit).

### Added

- This changelog. INSTALL's Update section points at it.
- `pipeline doctor` checks the install without the network or `sbx`: tools on PATH, the kvm group, Docker, the
  runner's tag, the kit links, `sbx.service` and linger, the App key, the config, the sbx-host reserve and the tick
  units. It prints a PASS or FAIL line per check, a fix under each FAIL, and `N checks, K FAIL`; it exits 1 on any
  FAIL. INSTALL's Check lines use it. A feature folder can no longer be named `doctor`.
- `pipeline init` sets up an install: the `pipeline` and `sbx-host` links, a link per kit skill and agent, the three
  user units with `sbx.service`'s caps sized from `/proc/meminfo` and the CPU count, and the pipeline and sbx-host
  config skeletons with `vm`, `admission.slots` and `reserve.pl` sized to match. It never replaces an existing file
  or link and never enables or starts a unit; it prints the next commands. Under 24 GiB of RAM it gives each stage
  VM 4 GiB, and under 16 GiB or 8 threads it warns. A feature folder can no longer be named `init`.

### Changed

- INSTALL uses `pipeline init` for its setup steps, which are now 9 instead of 10; the manual commands moved to an
  appendix. Update now reruns `pipeline init` to link kit entries a new tag adds.
- `pipeline doctor`'s fixes name `pipeline init` for a missing unit or config and follow the new step numbers. Its
  `config` check fails while the config still holds the example's `app.id`, `app.slug`, `app.reviewer` or repo.
- Stage VMs get the stage skills and agents from the runner's kit dir, so a stage runs the checked-out tag's skills.
  Your CLAUDE.md, skills and agents are still copied; one with a kit name is replaced whole. `pipeline-feature` is
  no longer copied into VMs.

### Fixed

- `sandbox/gate.py` validates `.claude/pipeline.json` the way the runner does before it creates a sandbox: a recipe
  that is not JSON, can't be read or that the runner rejects fails preflight with the runner's message (exit 2),
  instead of a traceback or a late failure.
- A usage window a `rate_limit_event` leaves out is kept, with its own reading time, until it resets, so the 5-hour
  limit is still enforced. Status shows a carried window's age (`5h 75% (90 min old)`) and a window with no live
  reading (missing, unreadable or past its reset) as `?` instead of `0%` or a stale percentage.
- The "required checks queued" ping names `ci-runner status` only when a ci-runner unit exists.
- INSTALL's gate check now reads "`gate.py` ends with `N checks, 0 FAIL`" instead of asking for PASS lines (the
  gate also prints INFO lines).
- A `config.json` or sbx-host `config.json` that is not a JSON object, or whose `app`, `admission` or `reserve` is
  not an object, now gives a message naming the file instead of a traceback.
- README quotes the Claude Code legal page as it reads now: "Pro, Max, and Ultra plans".

## v0.2 - 2026-11-03

**Rerun the gate:** yes.

### Changed

- Config moved to `~/.config/pipeline/` and state to `~/.local/state/pipeline/`, so the runner clone stays read-only.
  There is no fallback to the old paths: move your files by hand.
- Admission reserves memory through sbx-host: a stage starts when its VM fits the budget and the host's `floorGib`,
  and keeps its reserve until the run ends; tick starts only as many stages as sbx-host has room for.
- Publishing rebases the step first when main changed a workflow file.
- The respond gate checks `pr.md` with the PR-text checker from the kit dir.
- The gate runs setup through the runner's own setup and adds a `setup egress sealed` check.

### Added

- Optional `vm` config key (`cpus`, `memoryGb`; default 6 and 8) sizes every stage VM and the gate's sandbox.
- GitHub outages heal: a 5xx, a network error or a body that is not JSON spends no fix or rerun and loses no ping;
  tick asks again every 5 min and pauses after 45 min.

### Fixed

- The gate passes on a repo with an empty `setupEgress`.
- The gate prints its config path instead of failing on an undefined name.

### Removed

- `admission.minMemoryGb`: now a config error. Set `floorGib` in the sbx-host config instead.

## v0.1 - 2026-10-20

First release: the `pipeline` runner, sbx-host, the stage kit, systemd unit templates, the step-by-step
[INSTALL.md](INSTALL.md) and an MIT licence.
