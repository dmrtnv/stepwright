# Changelog

What changed for you in each release, newest first. Each release says whether to rerun `sandbox/gate.py`
([INSTALL.md](INSTALL.md), Update). Detail lives in [README.md](README.md) and [INSTALL.md](INSTALL.md).

## Unreleased

**Rerun the gate:** yes (its preflight now validates the recipe).

### Added

- This changelog. INSTALL's Update section points at it.

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
