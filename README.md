# Pipeline runbook

You write short briefs; the pipeline turns each into a reviewed branch and a draft PR, alone, in a throwaway sandbox VM,
while you work. Claude never pushes or merges: the runner pushes `agent/**` through a GitHub App, **you** review and
merge, then the next step starts. Sandbox facts: `sandbox/README.md`. What every sbx VM on the box reserves (stage VMs
`pl-*`, and a companion CI runner's `ci-*` VMs if you run one): `sbx-host status`.

```
you: /pipeline-feature <name>     → <repo>/.claude/plans/<name>/ with 00-overview.md + NN-slug/brief.md per PR
you: pipeline <name> [--detach]   (or wait: the tick timer starts it within 5 min, in queue order)
you: pipeline status [--watch]    (what needs you, runs, queues; `pipeline help` lists every command)

each step (one PR, one sandbox pl-<repo>-<name>-NN):
  admit       not held, sbx.service up, a free stage slot (`pipeline parallel`, ≤3), ≥10 GiB free, weekly <90 % and
              5-hour <85 % usage — else queued
  provision   template built if missing, sandbox cloned from the runner's clean clone, kit copied, branch from
              origin/<base>, repo setup (an sbx/VM-git/fetch/setup failure retries 3× 15 min apart, then pauses)
  plan        Claude writes plan.md            → waits if it has questions for you (autonomy auto: takes its
                                                 recommendations, each a ⚑ in the PR)
  implement   Claude codes + commits           → gates (checkFast in the VM, git gates on the host); red → fix ≤2×
  rebase      onto origin/<base> if it moved
  review      Claude reviews → review-1.md
  respond     Claude fixes findings, writes pr.md (source changed → one scoped review round more)
  publish     runner pushes the gate-green commit, opens a draft PR, posts the evidence comment
  CI          red on a moved base → rebase + push first; a step failed → the fixer session gets its log (2×, then
              ≤5× while the failure moves); no step failed (timeout, runner, cancel) or its log shows a failed DNS
              lookup → GitHub reruns the failed jobs (≤2×; only with Actions write, else pause for your re-run), no
              fix; Actions billing → pause; green → ready for review + your review requested
you: review; comments → `pipeline respond <name>`; merge → next step starts (tick)
```

## What runs where

| Piece | Where |
|---|---|
| runner (`pipeline` CLI) | `core.py` (state machine, stages, gates, publish, PR loops, admission), `backend.py` (sbx), `adapter.py` (repos, `pipeline.json`); `pipeline` → `~/.local/bin/pipeline` |
| config (no secrets) | `config.json` beside `core.py`: App id/slug/key path/reviewer, repo registry `{"<owner>/<repo>": "<checkout>"}`, admission limits, sandbox template policy `"template": {"claude": "host" \| "<x.y.z>", "build": "auto" \| "manual"}` |
| sandbox templates | `pipeline template` (target, loaded ones, which sandbox uses which); `logs/template-failed.json` marks a failed or missing one |
| App key | `~/.config/pipeline/app.pem` (mode 600, never in a dotfiles repo, never in a VM) |
| per-repo recipe | `<repo>/.claude/pipeline.json` on its base branch (setup, setup egress, checkFast, protected paths, migrations, required CI checks) |
| clean clones | `~/.local/share/pipeline/repos/<owner>/<repo>` (never checked out; holds `refs/pipeline/<feature>/<NN>`) |
| sandboxes | `sbx ls`: `pl-<repo>-<feature>-<NN>`, stopped between stages, removed on merge |
| stage rules | `system.md` (appended to every stage's system prompt), `result.schema.json` |
| kit copied into each VM | `~/.claude/{CLAUDE.md,skills,agents}`, the repo's memory (`~/.claude/projects/<checkout>/memory`), `heavy`, `kill-own.sh`, `drizzle-generate.py`, `transcripts.py`, a stage `settings.json` (no hooks, attribution off) |
| sbx daemon (owns every VM) | `systemctl --user status sbx.service`: a user unit that caps the VMs (MemoryHigh, MemoryMax, swap, CPUQuota, a low CPUWeight) so manual work wins every contention; stopping, restarting or a crash of the daemon ends every VM (a new daemon never re-adopts them), so restart it only when no stage runs |
| tick | `systemctl --user list-timers pipeline-tick.timer` (every 5 min; runs are transient units `pipeline-run-*`) |

## One-time setup

1. Sandboxes: install `docker-sbx` ([Docker's guide](https://docs.docker.com/ai/sandboxes/install/)) and join the
   `kvm` group (log out and in), then `sbx-host/install-sbx.sh`, `sbx login`, `pipeline template build`, then
   `sandbox/gate.py --repo <checkout>` (every line PASS). With the default policy (`"claude": "host", "build": "auto"`) the runner builds the template for a new
   host `claude` itself before the next sandbox create; see Upgrading.
2. Your own GitHub App: contents + pull requests write; checks, statuses, actions read; no
   workflows. Optional: actions write lets it rerun CI that failed without a failed step, but it also allows
   dispatching any workflow on its own branches and deleting runs, logs and caches: grant it only once deploy
   environments allow deploys from reviewed branches alone, with the deploy secrets in those environments. Private key → `~/.config/pipeline/app.pem`, `chmod 600`. Then `pipeline prove-app --repo <o>/<r>`.
3. Optional: `~/.config/ntfy/topic`, one line `<random>`, mode 600 (ntfy.sh phone pings; never in a dotfiles repo).
   `pipeline status -v` says `ntfy: topic missing` without it.
4. Systemd user units: `sbx.service` (the capped daemon: enabled, started only if no other daemon holds
   `sandboxd.lock`), `pipeline-tick.timer` (every 5 min) and its service, plus `~/.local/bin/pipeline` linked to
   `pipeline`; `loginctl enable-linger` so they run while you are logged out. A daemon started by hand (`sbx daemon start`, or one any `sbx` command autostarted)
   sits outside the caps and dies with its terminal or unit; while it holds the lock the unit refuses to start
   (retries every 15 s) and stages queue. Cut over once, when no stage runs:
   `pipeline stop --all`, `sbx daemon stop`, `systemctl --user start sbx.service`, `sandbox/gate.py`,
   `pipeline stop --clear`. Check: `pipeline status -v` shows `shims n/n` (every VM in the unit's cgroup).

**New repo**: install the App on it; rulesets: base branch requires a PR with 1 code-owner
approval and the CI checks, non-admins may push only `agent/**`; `CODEOWNERS`; CI running on `pull_request` (drafts
included); commit `.claude/pipeline.json` (template: `sandbox/pipeline.node-pnpm.json`); add
`"<owner>/<repo>": "<checkout>"` to `config.json`; `pipeline prove-app --repo …`, `pipeline prove-gate --repo …`.

`pipeline.json` keys: `base`, `setup` (commands, run in the VM with only `setupEgress` reachable, then egress is
sealed and checked), `checkFast` (`{name, run, flakeReruns?}`: the gate commands; a flaky one reruns whole),
`protected` (gitignore-style globs the agent may not change), `ci.requiredChecks`; optional `gateConfig` (flagged ⚑),
`dependencyFields` (package.json fields ⇒ fail), `migrations {dir, journal, generate}` (append-only + data-loss ⚑),
`github.protected` (branches prove-gate checks for an update restriction). Unknown keys are an error.

## Everyday use

`pipeline help` groups every command; `pipeline <command> -h` shows one; a mistyped one gets a suggestion.

| You want to | Command |
|---|---|
| Create a feature | `/pipeline-feature <name>` in a Claude session (interview → overview + briefs) |
| Start or continue | `pipeline <name>` (from inside the repo; `--repo <o>/<r>` if the name exists twice) |
| …in the background | `pipeline <name> --detach` (the run tick would start, now; a closed terminal doesn't stop it; log `logs/<repo>-<name>.out`) |
| See what needs you | `pipeline status`: header (claude usage, `stages n running of p`, badges only when abnormal, each with its fix), a `memory` line (the VMs' use against sbx.service's soft cap MemoryHigh and hard cap MemoryMax; `reserved` = what every VM not stopped may grow to, by owner, stages + CI (`sbx-host status`), `(over X by Y)` in yellow above the budget; host free against the stage floor), then NEEDS YOU · RUNNING (stage, time, last tool call) · QUEUED (start order) · `CI` (each companion CI runner slot, when there is one) · HELD (`after X: a → b, c` chains in merge order, then `by you: …`; a held feature whose PR waits stays in WAITING with `· held`) · WAITING; `--watch [-n s]` redraws in place (q quits); `--all` adds merged; `-v` the full per-step view (every reservation, sandbox, PR/CI, fix, hold, autonomy, next command) |
| One feature in full | `pipeline show <name>` (autonomy and hold, every step, the full reason, the next command, files, PR/CI, logs, last 5 history entries) |
| What a stage is doing | `pipeline log <name>` (`HH:MM tool target` lines of the newest stage log; `-f` follows) |
| Answer a plan's questions | `pipeline answer <name>`: `plan.md` opens in `$VISUAL`/`$EDITOR`; answer under `## Unresolved questions`, save: a changed file approves; or edit it yourself and `pipeline approve <name>`. The PR's Decisions list "Owner answered: <questions> — <the lines you added or changed there>" (none: "the recommendation stands") |
| Send a plan back | write `## Feedback` in `plan.md`, `pipeline revise <name>` |
| Let it decide | `pipeline autonomy <name> auto` (default `ask`): plan questions are answered by the plan's own recommendations and a last review round's blocking findings go to respond; each is a `⚑ auto-decided` in the PR's Decisions (never also under Questions) and the ping. Blocked/failed stages still stop |
| Act on your PR comments | `pipeline respond <name>` (your review, inline and conversation comments since the last round) |
| See and steer the queue | `pipeline queue` (running, queued in start order, held, waiting, needs you, backlog); `pipeline order <a> <b>` (start these first) |
| Hold a feature | `pipeline pause <name>` (its running stage finishes, no new one starts; `--now` interrupts it, resumable); `pipeline start <name>` lifts it |
| Hold everything | `pipeline pause` (drain: running stages finish, nothing new starts; `--now` interrupts them); `pipeline start` ends it |
| Start a parked feature | `pipeline start <name>` (moves it out of `_backlog/`); `--after <other>[:NN]` starts it once that feature (or its step NN) merged |
| Fewer or more at once | `pipeline parallel [N]` (shows / sets 1..`admission.slots`; lowering lets running stages finish) |
| Stop everything now | `pipeline stop --all` (runs stopped resumable, VMs stopped); `pipeline stop --clear` → tick resumes |
| Start what has work | `pipeline tick` (the timer does it every 5 min; a run that frees a slot starts it at once) |
| Clean up | `pipeline gc` (orphan sandboxes, refs and logs of merged steps, snapshot refs > 14 days, then `template prune`) |
| Sandbox templates | `pipeline template` (show), `pipeline template build [<x.y.z>]` (now, foreground), `pipeline template prune` (all but the target and the ones a sandbox uses, from sbx and docker, plus leftover tars) |

**Re-running is always safe.** State lives in `.pipeline/NN.json`; a re-run resumes the same Claude session, and a
finished stage never reruns, and a step paused on its open PR asks GitHub again. approve/revise/respond refuse while
a run holds the feature. Ctrl+C, SIGTERM and
`stop --all` stop the stage inside the VM, fetch its commits to the host and mark it resumable.

## How a step runs

- **Your controls** (`pipeline pause/start/order/parallel/autonomy`, written by the CLI only; missing file = the
  defaults): per feature `<name>/.pipeline/feature.json` `{hold, after, autonomy}` (synced with the state files),
  global `logs/control.json` `{parallel, drain, order}`. A hold or an unmerged `--after` stops the next stage (also
  the next step after a merge; a PR poll still runs), and an explicit `pipeline <name>`, `--detach` or `respond`
  refuses. Tick starts PR polls always, stage work only into free slots (`parallel` − max(slots held, runs holding a
  feature lock)), in `order` then oldest-queued order, and none while the host would refuse it (kill switch, sbx,
  memory, usage).
- **Admission** before every stage: not held or drained, `sbx.service` active (the capped daemon; never an uncapped one), a slot
  (`logs/slot-<n>.lock`, at most `pipeline parallel` held (ceiling `admission.slots` = 3), held until the run
  exits), `MemAvailable` ≥10 GiB (`admission.minMemoryGb`), weekly <90 % and 5-hour <85 %
  (from `logs/usage.json`, written from every stage's `rate_limit_event`; older than 30 min ⇒ a one-token Haiku probe
  refreshes it, so your manual use counts). Failing ⇒ step `ready`, "queued: <why>"
  (pinged once per reason, low priority; never for busy stage slots), the run ends, tick retries oldest first. Never waits in-process.
- **Template**: the target is `pipeline-sandbox:claude-<v>-node-<host node>`, `<v>` the host's `claude --version`
  (`"claude": "host"`) or the pin. Missing before a create: `auto` builds it (`build-template.sh`, ~1 min, one build
  at a time across runs; it notes it, no ping); a failed build makes every step needing it wait `ready` (never
  paused): one build try per 15 min, pings on the 1st and 3rd failure, heals on its own. `manual` never builds: the
  steps wait with one ping until `pipeline template build`. A sandbox keeps the template it was made from (its
  session's CLI version never changes under it).
- **Sandbox**: `sbx create --clone` of the clean clone (6 CPU, 8 GiB), the claude kit's extra egress denied, kit
  copied, the VM's `origin` rewritten to the clean clone (`url.…insteadOf`: sbx resets `origin` to GitHub on every
  start), a repo-local git identity of the App's bot (`<slug>[bot]`, `<id>+<slug>[bot]@users.noreply.github.com`; sbx
  copies yours into the VM's `~/.gitconfig`, and a squash credits every commit author other than the PR author as a
  co-author), branch `agent/<feature>-NN-<slug>` from `origin/<base>` (never from a stale one), setup with only `setupEgress`, then an egress
  canary (example.com and a setup host must be unreachable). A lost VM is recreated from the host ref; a lost session
  restarts fresh with the branch's commits and `pr.md` as context. A failed `sbx create`, VM git config, base fetch
  (also before a rebase), setup command or `sbx policy allow` is an environment failure: `ready` + `not_before`
  15 min, retried 3× (queue waits between don't count), pinged on the 1st retry, then `paused`. A failed
  `sbx policy rm` or a reachable canary pauses at once (the agent phase would not be sealed).
- **Stages**: `claude -p` inside the VM, Opus 5.5 pinned (plan and review `high`, implement and respond `medium`),
  subagents Sonnet 5.5, fallback Sonnet on overload, `--max-budget-usd` and hours per stage, auto mode, no permission
  prompts, the result schema. The init event must show `claude-opus-5-5` and `auto`, else the stage stops. Before
  each stage the feature folder is copied in (mtimes kept); after it (also on timeout, limit, crash or stop) the VM
  is stopped, the branch fetched to `refs/pipeline/<feature>/<NN>` in the clean clone (fsck, 200 MB cap) and only the
  stage's own files copied out (`plan.md`; `review-K.md`; `pr.md` + `plan.md`) as regular files.
- **Gates**: in the VM, branch + clean tree + `checkFast` (log: `.pipeline/NN-gates.log`), then the VM stops and
  the fetched SHA must be the tip the checks ran on. On the host, on the fetched ref: commits over base, no AI
  trailer or personal text (home path, non-noreply email, `MEMORY.md`, `~/.claude`) in commit messages, protected/dependency/gate-config, migrations append-only + data loss ⚑, and the **tamper gate** (a new
  `.only` fails; a deleted test file or a new skip/todo/fixme fails unless the approved plan's `## Test changes`
  names its exact path; either way it is flagged; the plan gate refuses a pattern there). Red ⇒ the same session resumes with the failures, at most twice.
- **Limits**: a usage limit sets `not_before` = reset + 2 min, marks the step `ready`, pings once and ends the run;
  tick resumes the same session after the reset.
- **Rebase** when the base moved (before a re-run plan, so a step blocked on another PR re-checks with it merged and
  its resumed session is told; before review and before the final gates; and for an open PR that turns
  conflicting or turns red after its base moved): a pre-rebase host ref
  `refs/pipeline/<feature>/<NN>-pre-rebase-<MMDD>` first; conflicts go to the fixer session; setup reruns after any
  rebase.

## Publishing and the PR loop

Before any push: a secret scan of the diff (env files, token-shaped strings), `pr.md` read safely (a symlink or a
token-shaped string stops it; home paths become `~`, personal emails `[email]`, an AI attribution line is dropped), the base branch must still require a code-owner-approved PR (read with the App
token), and the host ref must be the gate-green commit. The runner pushes exactly that commit from the clean clone
with `--force-with-lease` (a branch someone else moved is never overwritten), opens a **draft** PR with `pr.md` (or
re-syncs title and body of the open one) and upserts one evidence comment (`<!-- pipeline-evidence -->`: summary,
decisions (incl. your answers to the plan's questions and review-round approvals), questions still open, owed, deferred (each only when it has items), tests, CI state, the permission-denial count when
above 0; redacted like `pr.md`). Each stage sees the items earlier stages recorded and adds only new ones. Learnings,
runner notes, MEMORY.md size, usage and the denials themselves stay in the local `done.md`: PR text is the team's,
project facts only. Tokens: one per repo per hour, scoped to that repo, kept in memory,
passed to git via its environment only.

Tick then polls each done step's PR: merged ⇒ sandbox removed, host refs deleted, step closed, next step starts (a
failed `sbx rm` is noted; every tick removes a closed step's leftover sandbox while `sbx.service` is active); closed
unmerged ⇒ `paused` (reopen and re-run, or delete the feature); the PR's head is not the runner's commit (someone
pushed) ⇒ `paused`, nothing trusted or overwritten; conflicting ⇒ rebase + push; required checks on the published commit
red while origin/<base> moved past it ⇒ rebase + push first (CI tested a merge with the newer base; not a fix, so not
counted). Otherwise each red workflow run is judged by its red job that ended first (a fail-fast sibling or a `needs:`
gate is red because of it): a step failed ⇒ code: the fixer (last respond session, else implement) gets the end of the
failed step's log (cut at `##[error]Process completed…`, timestamps and colours stripped), 2 times, then more only while
the failure moves, ≤5 in all, then pause (the failure is its failing source lines, the code-frame lines above a `^`; it
moved when no pushed fix was made for the same set, so a shifted line number or other tests passing around it is not a
move, and a log without a frame, as from tsc or lint, never moves); the run (or a rerun of it) still runs, or a newer
run of its workflow runs on the commit ⇒ wait; it ended without a failed step (timeout, runner not acquired or lost, a
cancel; also a failed step whose log is gone, or whose log shows a failed DNS lookup: `CI_NETWORK`, that check only) ⇒
GitHub reruns the run's failed jobs (App with Actions write; with read it pauses for your re-run on GitHub), ≤2 per
published commit (status, show and the PR comment count them), no fix, then pause (a DNS failure goes to the fixer
instead); Actions billing ⇒ pause at once (code goes before waiting, waiting before a rerun; a check outside Actions is
code). A re-run after the fixer reported `blocked` asks CI again first (rerun the job on GitHub, then re-run); all green
⇒ ready for review, your review requested (a 422 while CODEOWNERS already requested you is fine; another failure is a
runner note), 📱 "✅ PR #n · CI green · confidence X · +Y weekly pts" (once per commit). No required check 30 min after a
push, or one still queued while no `ci-runner@` unit is active (a companion self-hosted PR runner; a busy runner never
pings) ⇒ one ping.

## When it stops

`paused` means it needs you (a decision, or a fix to the repo, branch, brief or GitHub); `ready` waits for something
that heals (a slot, usage, a retry, the template) and tick resumes it. `pipeline status -v` shows the template, then per
step the reason's first line (with the cause appended when that line ends in `:`), the `fix:` hint when there is one,
and the next command (`status -v`); the full reason: `pipeline show <name>`. `pipeline tick` lists the steps waiting
on you. ntfy pings carry the error line, a plan's or a blocked stage's questions (→ the command), and open the PR
when tapped (CI green, merged). Priority high: it needs you (paused, questions, a review's call); default: news (CI
green, merged); low: it heals itself (a retry, a usage or memory queue). All stage slots busy is never pinged (status
shows the queue).

| Status / message | What you do |
|---|---|
| `awaiting-approval`, "The plan asks you" | `pipeline answer <name>` (or answer under `## Unresolved questions` in `plan.md`, `pipeline approve <name>`); your answer goes under the PR's Decisions |
| `awaiting-approval`, "Review round 2 still reports … blocking" | read `review-2.md`; `approve` lets respond fix them without a third review; the PR's Decisions say "Owner approved: …" |
| `paused`, "… blocked: …" | fix the cause, write answers under `## Unresolved questions`, `pipeline <name> --detach`; a skip or a deleted test file approved after the plan also goes into `.pipeline/NN-plan.approved.md` |
| `paused`, "gates red after 2 fix attempts" | read the failures (`NN-gates.log`), fix the cause, re-run (2 more tries) |
| `ready`, "queued: …" | nothing: tick retries when a slot, memory or usage frees up |
| `ready`, "queued: held (pipeline start <name>)" / "after <other> …" / "drained"; `held: by you` / `held: after <other>` in `-v` | yours: `pipeline start <name>` (or `pipeline start` for a drain); an `--after` releases itself when the other merges (`next:` says which) |
| `held: …` / `after …` refusing a run | the feature is held: `pipeline start <name>` first |
| `ready`, "queued: sbx.service not active" | `systemctl --user start sbx.service` (if another daemon holds `sandboxd.lock`: the cutover in One-time setup); tick resumes. Never run `sbx …` by hand while it is down: that autostarts the daemon which blocks the unit. After an unattended docker-sbx upgrade (`/var/log/apt/history.log`) the unit restarts itself (`Restart=always`); rerun `sandbox/gate.py` |
| "CI … not a code failure (network: …): rerun n/2" | nothing: a failed DNS lookup in the job (CI VMs resolve through the host, see Host DNS); GitHub reruns it |
| `paused`, "respond-N blocked: … e2e … `getaddrinfo ENOTFOUND` / PUT timeout to R2" | the reruns were spent, or a timeout left no DNS words in the log: check Host DNS, rerun the failed jobs on GitHub, then `pipeline <name> --detach` |
| `ready`, "… limit until <time>" | nothing: tick resumes after the reset |
| `ready`, "stopped by the kill switch" | `pipeline stop --clear` |
| `ready`, "retrying (n/3) after: …" | nothing: tick retries 15 min later; after the 3rd retry it pauses with the cause |
| `ready`, "waiting for template …: build failed (n): …" | nothing (tick retries the build every 15 min), or fix the cause (`fix:` line) and `pipeline template build` |
| `ready`, "template … missing" (`build: manual`) | `pipeline template build` (tick resumes the steps within 5 min) |
| "budget" / "timeout" | read the stage log; usually the step is too big: split the brief |
| `paused`, "setup in the sandbox … failed …" (after 3 retries) | read `NN-setup.log`; check `pipeline.json` setup; then `pipeline <name> --detach` |
| `paused`, "setup failed … egress canary" / "sbx policy rm" | `sbx policy ls` and the global deny-all policy; then `pipeline <name> --detach` |
| `paused`, "sbx create … failed" (after 3 retries) | `sbx ls`, `sbx diagnose`, `pipeline template`; then `pipeline <name> --detach` |
| "sandbox gone" in status | the VM was removed; the next run recreates it from the host ref |
| "moved after the gates" / "not at the gate-green" | re-run: the gates run on what is there now |
| "PR text unsafe" / "secret in …" | fix the file or the branch, re-run |
| "CI is red … after N fixes" (": the failure did not move") | read the CI log; fix on the branch yourself, or re-run: it fixes again only for a failure no pushed fix was made for, below 5 fixes (no code frame: below 2) |
| "CI … ended without a code failure 3 times (…)" | a slow or hung test or the runner: check the job's `timeout-minutes` (or the runner), re-run it on GitHub, then `pipeline <name>` |
| "CI … auto-rerun is off (the App has Actions: read)" | open the PR's run, "Re-run failed jobs", then `pipeline <name>` |
| "CI … did not run: GitHub Actions billing" | fix the org's Actions billing or spending limit, re-run on GitHub, then `pipeline <name>` |
| "CI … GitHub refused the rerun (…)" | re-run the job on GitHub, then `pipeline <name>` |
| "respond-K blocked: … e2e … not caused by this branch" | a DNS failure is rerun by itself (≤2×); this one wasn't, or the reruns were spent: rerun the failed job on GitHub, then `pipeline <name>` (asks CI again) |
| "PR #n now points at …: someone else pushed" | review and merge it yourself (tick sees the merge and starts the next step), or push the named commit back and re-run |
| "PR #n was closed unmerged" | reopen it and re-run, or delete the feature folder (a note "sbx rm … failed": remove the sandbox first) |
| `[running?]` | the run died; tick or `pipeline <name> --detach` resumes it (a VM it left running is stopped first; sbx itself stops a VM 30 s after its last session) |
| `paused`, "interrupted (Ctrl+C, SIGTERM or a closed terminal)" | `pipeline <name> --detach` (a background run no closed terminal stops) |
| "the GitHub App key … missing / mode" | `pipeline prove-app` diagnoses it |

## Where things are

| What | Where |
|---|---|
| briefs, plans, reviews, PR text, done notice | `<repo>/.claude/plans/<name>/NN-<slug>/` (gitignored: back it up yourself) |
| parked features, queue | `<repo>/.claude/plans/_backlog/<name>/`, `<repo>/.claude/plans/README.md` (below), `pipeline queue` |
| your controls | `<name>/.pipeline/feature.json` (hold, after, autonomy), `logs/control.json` (parallel, drain, order) |
| step state (read, don't edit) | `<name>/.pipeline/NN.json` |
| stage logs (stream-json) | `<name>/.pipeline/NN-<stage>.log`; the final answer: `tail -n 1 <log> \| jq .structured_output` |
| gate and setup output | `<name>/.pipeline/NN-gates.log`, `NN-setup.log` |
| approved plan (tamper gate baseline) | `<name>/.pipeline/NN-plan.approved.md` |
| the step's branch on the host | `git -C ~/.local/share/pipeline/repos/<o>/<r> log refs/pipeline/<name>/<NN>` |
| inside the VM | `sbx exec -it pl-<repo>-<name>-<NN> bash` (workspace = the clean clone's path, plans under `/home/agent/plans`; a run that finds the VM running stops it first, ending this shell) |
| runs started by tick | `logs/<repo>-<name>.out` beside `core.py`, `journalctl --user -u 'pipeline-run-*'` |
| usage, slots, kill switch | `logs/usage.json`, `logs/slot-*.lock`, `logs/STOP` |

## Backlog

Features not to start yet sit in `<repo>/.claude/plans/_backlog/<name>/`: the `_` prefix fails the feature-name rule,
so tick and `status` ignore them (`pipeline queue` lists them). Start one with `pipeline start <name>` (moves it out;
`--after <other>[:NN]` waits for that merge). A feature folder named like a command (`status`, `start`, …) is skipped
too; `status` and tick name it so you rename it.
`<repo>/.claude/plans/README.md` holds the queue `| feature | steps | migration | areas | after |` and the run
order; `/pipeline-feature` reads it for the overlap check. Sequencing lives in the runner (`--after`), the table only
records it. Rule: never two features that add migrations at once
(each PR may hold one migration, and parallel ones collide on the journal). Plan-mode files go to
`.claude/plans/_plan-mode/` (`plansDirectory`).

## New machine

Carry over the config, the App key, each checkout and its `.claude/plans/` (state files included): a step whose PR
exists resumes, since tick re-polls GitHub. Not carried: stage logs and unpublished WIP (`refs/pipeline/…` in the clean
clone; GitHub only sees a step at publish). So one box runs the pipeline at a time; move boxes between steps, not
mid-step.

## Host DNS

Every lookup on the box, sbx VMs' and a self-hosted CI runner's included (sbx is Go and asks `127.0.0.53`), goes
through the host's resolver. A stalling upstream shows as `getaddrinfo ENOTFOUND` in CI logs: the runner reruns such a
job (`CI_NETWORK`), and its GitHub calls retry a failed connect or a broken GET answer twice (`GITHUB_RETRY_SECONDS`).
If it happens often, give systemd-resolved public fallback resolvers and serve stale answers (`StaleRetentionSec`).

## Recovery

- **A step's work**: the host ref `refs/pipeline/<name>/<NN>` in the clean clone is the truth (and its
  `-pre-rebase-<MMDD>` siblings until the step closes). A VM can always be removed (`sbx rm --force pl-…`); the next
  run recreates it from the ref.

## Tests

```bash
python3 -m unittest discover tests            # runner (fake sbx/claude/pnpm/curl/GitHub), CLI views, adapter, sbx-host, leak check
sandbox/gate.py --repo <checkout>             # live: sbx boundary + the real stage argv (5 tiny Opus calls)
```

Rerun the gate after a `claude` or sbx upgrade or a `STAGES` change.

## Upgrading

- **Claude Code**: `config.json` `"template"` decides what sandboxes run and when it is built. `"claude": "host"`
  follows the host: after an update the next sandbox create builds the new template (`"build": "auto"`) or waits for
  `pipeline template build` (`"manual"`). `"claude": "2.1.286"` pins: host updates change nothing until you move the
  pin (`pipeline template build` builds it now). Existing sandboxes keep
  their template (a session never resumes across CLI versions). Then `gate.py`. Disk: `pipeline template prune`
  (also run by `gc`) removes every template that is neither the target nor used by a sandbox.
