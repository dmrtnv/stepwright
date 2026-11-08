# Changelog

What changed for you in each release, newest first. Each release says whether to rerun `sandbox/gate.py`
([INSTALL.md](INSTALL.md#update)). Detail lives in [README.md](README.md), [INSTALL.md](INSTALL.md) and
[docs/how-it-works.md](docs/how-it-works.md). Versions follow SemVer from v1.0.0.

## Unreleased

**Rerun the gate:** no (no kit or `STAGES` change).

### Changed

- After `pipeline update` checks out a verified tag, it says so first:
  `Verified v0.11: signed by a key in .github/allowed_signers at v0.10.`, then `Was at v0.10, now at v0.11.`

### Fixed

- Commands that `pipeline update`, `pipeline init` and `pipeline doctor` print quote the runner path, so they paste
  cleanly when it holds a space. The go-back command after a failed init also quotes the branch.

## v0.10 - 2026-11-08

**Rerun the gate:** no (no kit or `STAGES` change).

### Changed

- `pipeline update` verifies the release tag before it checks it out. The tag must be signed by a key in
  `.github/allowed_signers` as committed in the checkout you have installed; otherwise the checkout is unchanged.
  When the installed checkout has no such file, it points to the manual commands in
  [INSTALL: Update](INSTALL.md#update). The move from v0.9 to this release is not verified yet: v0.9's update has no
  check.
- `pipeline doctor` checks for `ssh-keygen` (`sudo apt install openssh-client`).
- `dev/release.py` verifies the tag against the previous release's `.github/allowed_signers` before it pushes it.

### Fixed

- `pipeline update` on a branch names the branch in "Was at" and in its go-back command, so going back re-attaches
  the branch instead of detaching at the commit.
- `pipeline update` and `pipeline doctor` describe a runner ahead of a release by release tags only
  (`v0.9-2-g<sha>`), so a nearer tag such as `nightly-1` no longer names it.
- `dev/release.py` stops with one line on Ctrl+C ("release: stopped; rerun the same command to resume", exit 130)
  instead of a traceback. When GitHub still shows the release PR open after the merge, it says to rerun once
  GitHub shows it merged.

## v0.9 - 2026-11-08

**Rerun the gate:** no (no kit or `STAGES` change).

### Added

- `pipeline update [<tag>]` updates the runner in one command. It fetches the tags, refuses while a stage runs or
  starts, checks out the newest release tag (or the one you name), runs `pipeline init` and `pipeline doctor`, and
  prints the Rerun the gate line of each release it adds or undoes. A bare update never moves the runner back.
  Moving to this release still takes the manual commands in [INSTALL: Update](INSTALL.md#update).

### Changed

- `pipeline doctor`'s runner check counts only release tags (`vN.N`, `vN.N.N`), so a `v0.9-rc1` tag is never "the
  newest". A checkout ahead of the newest release passes. Its fix is `pipeline update`.
- `pipeline init` prints the setup steps only when it writes `sbx.service`, a fresh install. Otherwise it ends with
  "Next: `pipeline doctor` checks this install."

## v0.8.1 - 2026-11-07

**Rerun the gate:** no (only the `pipeline status` text changed, not the kit or `STAGES`).

### Fixed

- `pipeline status` no longer shows `5h ?` when Claude reports no five-hour window (your plan has none). It shows
  only the windows Claude reports. An unreadable or past-reset window still shows `?`.

## v0.8 - 2026-11-05

**Rerun the gate:** yes (its kit and exfil tests changed, and stage VMs now get your own convention skills).

### Added

- Your own `commit-message`, `pr-text` and `wrap-up` skills and `web-researcher` agent replace the kit's in stage
  VMs: a copy, or a link that resolves outside the kit dir (INSTALL step 2). `pipeline doctor` shows each as PASS
  "your own, stage VMs use it". A `pr-text` of your own without `check.py` is a FAIL. The four stage skills and
  `pipeline-feature` stay the kit's.

  Upgrading: a copy that doctor showed as "your own copy, not the kit's" is now what stage VMs use. To keep the
  kit's, delete it or replace it with the kit's link.
- README "Before you run it": the terms with dated quotes, the cost caps per session, what the sandbox protects and
  what it doesn't, what leaves your machine, what the GitHub App can do, and the requirements you can't change.
- SECURITY.md: how to report a vulnerability privately. CONTRIBUTING.md: the rules, tests, commits and public API.
- GitHub: a bug report form, a PR template and private vulnerability reporting. Blank issues are off; security
  reports go through the private form.

### Changed

- INSTALL is a copy-paste guide: each step or sub-step has a command block and a Check. Step 5 fills in the
  config with one command, step 6 writes the recipe, CODEOWNERS and `.gitignore` with commands that never replace
  what you have, and step 8 adds `node` and `claude` to the timer's `PATH` itself. "What you need" adds the GitHub
  CLI (`gh`), which `examples/rulesets.sh` uses.
- README "Terms of use" became "Before you run it". The Actions write note moved there from Setup.
- The respond gate runs the pr-text skill's `check.py` as a command (`python3 check.py <file>`) with a 60 s
  timeout, instead of importing it. A crash or a timeout pauses the step with the path, the cause and the fix.
- The `.claude/pipeline.json` presets moved from `sandbox/` to `examples/`.
- The pnpm preset fits any pnpm repo: it installs, then runs `pnpm lint` and `pnpm test`. It no longer copies one
  project's env files, pulls Docker images or runs that project's scripts. Drizzle users add the `migrations` line
  INSTALL shows.
- The Python preset skips the install when there is no `requirements.txt` and the tests when there is no `tests/`
  dir. An empty `tests/` dir still fails.
- The README is short. How it works, the config and `pipeline.json` keys, every command in detail and the recovery
  notes moved to [docs/how-it-works.md](docs/how-it-works.md); what to do when a step stops moved to
  [docs/troubleshooting.md](docs/troubleshooting.md). Both are lists now.

### Fixed

- The gate runs without `~/.claude/CLAUDE.md` (an INFO line) and without repo memory (an INFO line and an empty
  memory dir in the VM, as stages get).
- The gate finds a checkout's memory when its path holds a `.` or `_`, as the runner does.
- Gate test 8 checks the host checkout, `~/.ssh` and the host credentials one by one, and each needs the VM's own
  answer. Before, one missing path passed all three.

## v0.7 - 2026-11-04

**Rerun the gate:** yes (its kit test copies your ~/.claude/rules/).

### Added

- Stage VMs and the gate's sandbox get your `~/.claude/rules/`, with links followed and `__pycache__` left out. If
  you delete your rules dir, the next run removes it from a reused VM too.

### Fixed

- Stage VMs are created with `sbx create --skills off`. sbx's default mounted its shared skills store read-only over
  the VM's skills dir, so every claude stage failed with "Device or resource busy". Your sbx must list `--skills` in
  `sbx create --help`; if it does not, update the `docker-sbx` package. If a stage still fails that way, remove its
  VM with `sbx rm --force <name>` and the runner creates a new one.
- The kit's `plan-harden` standards state the values again (simplicity, DRY, YAGNI, a fully featured UX, verify
  every claim), so a stage has them without your `~/.claude/CLAUDE.md`. Your CLAUDE.md adds to them.
- INSTALL's manual setup sets `vm.cpus` on a box with fewer than 12 threads at any RAM size, as `pipeline init` does.
- This changelog's headings match the tags.

## v0.6 - 2026-11-04

**Rerun the gate:** no.

### Added

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

## v0.5 - 2026-11-04

**Rerun the gate:** no.

### Added

- `pipeline doctor` checks the install without the network or `sbx`: tools on PATH, the kvm group, Docker, the
  runner's tag, the kit links, `sbx.service` and linger, the App key, the config, the sbx-host reserve and the tick
  units. It prints a PASS or FAIL line per check, a fix under each FAIL, and `N checks, K FAIL`; it exits 1 on any
  FAIL. INSTALL's Check lines use it. A feature folder can no longer be named `doctor`.

### Fixed

- A `config.json` or sbx-host `config.json` that is not a JSON object, or whose `app`, `admission` or `reserve` is
  not an object, now gives a message naming the file instead of a traceback.

## v0.4 - 2026-11-03

**Rerun the gate:** yes (its kit test copies the runner's kit).

### Changed

- Stage VMs get the stage skills and agents from the runner's kit dir, so a stage runs the checked-out tag's skills.
  Your CLAUDE.md, skills and agents are still copied; one with a kit name is replaced whole. `pipeline-feature` is
  no longer copied into VMs.
- `plan-harden` runs on every plan, including one that looks trivial. `plan-implement`'s pre-review step no longer
  names a host path for the stage rules. `pipeline-feature` writes the folder from an approved plan-mode plan
  without planning again, and checks its paths and line numbers against the default branch.

## v0.3 - 2026-11-03

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

- The gate runs setup through the runner's own setup and adds a `setup egress sealed` check.

### Fixed

- The gate passes on a repo with an empty `setupEgress`.

## v0.1 - 2026-11-03

**Run the gate:** yes.

First release: the `pipeline` runner, sbx-host, the stage kit, systemd unit templates, the step-by-step
[INSTALL.md](INSTALL.md) and an MIT licence. If you ran the untagged 2026-10-20 snapshot, these changed since:

### Changed

- Config moved to `~/.config/pipeline/` and state to `~/.local/state/pipeline/`, so the runner clone stays read-only.
  There is no fallback to the old paths: move your files by hand.
- Admission reserves memory through sbx-host: a stage starts when its VM fits the budget and the host's `floorGib`,
  and keeps its reserve until the run ends; tick starts only as many stages as sbx-host has room for.
- Publishing rebases the step first when main changed a workflow file.
- The respond gate checks `pr.md` with the PR-text checker from the kit dir.

### Added

- Optional `vm` config key (`cpus`, `memoryGb`; default 6 and 8) sizes every stage VM and the gate's sandbox.
- GitHub outages heal: a 5xx, a network error or a body that is not JSON spends no fix or rerun and loses no ping;
  tick asks again every 5 min and pauses after 45 min.

### Fixed

- The gate prints its config path instead of failing on an undefined name.

### Removed

- `admission.minMemoryGb`: now a config error. Set `floorGib` in the sbx-host config instead.
