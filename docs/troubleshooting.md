# Troubleshooting

What each status and message means, and what you do. How the runner works: [How stepwright works](how-it-works.md).

- `paused` means it needs you (a decision, or a fix to the repo, branch, brief or GitHub).
- `ready` waits for something that heals (a slot, usage, a retry, the template) and tick resumes it.
- `pipeline status -v` shows the template, then per step the reason's first line (with the cause appended when that
  line ends in `:`), the `fix:` hint when there is one, and the next command (`status -v`).
- The full reason: `pipeline show <name>`.
- `pipeline tick` lists the steps waiting on you.
- ntfy pings carry the error line, a plan's or a blocked stage's questions (→ the command), and open the PR when
  tapped (CI green, merged).
  - Priority high: it needs you (paused, questions, a review's call);
  - default: news (CI green, merged);
  - low: it heals itself (a retry, a usage or memory queue).
  - All stage slots busy is never pinged (status shows the queue).

## Status and messages

Each item is the status or message, then what you do.

- `awaiting-approval`, "The plan asks you": `pipeline answer <name>` (or answer under `## Unresolved questions` in
  `plan.md`, `pipeline approve <name>`); your answer goes under the PR's Decisions.
- `awaiting-approval`, "Review round 2 still reports … blocking": read `review-2.md`; `approve` lets respond fix
  them without a third review; the PR's Decisions say "Owner approved: …".
- `paused`, "… blocked: …": fix the cause, write answers under `## Unresolved questions`,
  `pipeline <name> --detach`; a skip or a deleted test file approved after the plan also goes into
  `.pipeline/NN-plan.approved.md`.
- `paused`, "gates red after 2 fix attempts": read the failures (`NN-gates.log`), fix the cause, re-run (2 more
  tries).
- `ready`, "queued: …": nothing: tick retries when a slot, memory or usage frees up.
- `ready`, "queued: held (pipeline start <name>)" / "after <other> …" / "drained"; `held: by you` /
  `held: after <other>` in `-v`: yours: `pipeline start <name>` (or `pipeline start` for a drain); an `--after`
  releases itself when the other merges (`next:` says which).
- `held: …` / `after …` refusing a run: the feature is held: `pipeline start <name>` first.
- `ready`, "queued: sbx.service not active": `systemctl --user start sbx.service` (if another daemon holds
  `sandboxd.lock`: the cutover in [Setup notes](how-it-works.md#setup-notes)); tick resumes.
  - Never run `sbx …` by hand while it is down: that autostarts the daemon which blocks the unit.
  - After an unattended docker-sbx upgrade (`/var/log/apt/history.log`) the unit restarts itself
    (`Restart=always`); rerun `sandbox/gate.py`.
- "CI … not a code failure (network: …): rerun n/2": nothing: a failed DNS lookup in the job (CI VMs resolve
  through the host, see [Host DNS](how-it-works.md#host-dns)); GitHub reruns it.
- `paused`, "respond-N blocked: … e2e … `getaddrinfo ENOTFOUND` / PUT timeout to R2": the reruns were spent, or a
  timeout left no DNS words in the log: check [Host DNS](how-it-works.md#host-dns), rerun the failed jobs on
  GitHub, then `pipeline <name> --detach`.
- `ready`, "… limit until <time>": nothing: tick resumes after the reset.
- `ready`, "stopped by the kill switch": `pipeline stop --clear`.
- `ready`, "retrying (n/3) after: …": nothing: tick retries 15 min later; after the 3rd retry it pauses with the
  cause.
- `done`, "GitHub not answering, tick asks again: …" (`pipeline show`); `paused`,
  "GitHub has not answered for 45 min": nothing while `done`; once paused it does not resume by itself: check
  githubstatus.com, then `pipeline <name> --detach`.
- `ready`, "waiting for template …: build failed (n): …": nothing (tick retries the build every 15 min), or fix the
  cause (`fix:` line) and `pipeline template build`.
- `ready`, "template … missing" (`build: manual`): `pipeline template build` (tick resumes the steps within 5 min).
- "budget" / "timeout": read the stage log; usually the step is too big: split the brief.
- `paused`, "setup in the sandbox … failed …" (after 3 retries): read `NN-setup.log`; check `pipeline.json` setup;
  then `pipeline <name> --detach`.
- `paused`, "setup failed … egress canary" / "sbx policy rm": `sbx policy ls` and the global deny-all policy; then
  `pipeline <name> --detach`.
- `paused`, "sbx create … failed" (after 3 retries): `sbx ls`, `sbx diagnose`, `pipeline template`; then
  `pipeline <name> --detach`.
- "sandbox gone" in status: the VM was removed; the next run recreates it from the host ref.
- "moved after the gates" / "not at the gate-green": re-run: the gates run on what is there now.
- "PR text unsafe" / "secret in …": fix the file or the branch, re-run.
- "CI is red … after N fixes" (": the failure did not move"): read the CI log; fix on the branch yourself, or
  re-run: it fixes again only for a failure no pushed fix was made for, below 5 fixes (no code frame: below 2).
- "CI … ended without a code failure 3 times (…)": a slow or hung test or the runner: check the job's
  `timeout-minutes` (or the runner), re-run it on GitHub, then `pipeline <name>`.
- "CI … auto-rerun is off (the App has Actions: read)": open the PR's run, "Re-run failed jobs", then
  `pipeline <name>`.
- "CI … did not run: GitHub Actions billing": fix the org's Actions billing or spending limit, re-run on GitHub,
  then `pipeline <name>`.
- "CI … GitHub refused the rerun (…)": re-run the job on GitHub, then `pipeline <name>`.
- "respond-K blocked: … e2e … not caused by this branch": a DNS failure is rerun by itself (≤2×); this one wasn't,
  or the reruns were spent: rerun the failed job on GitHub, then `pipeline <name>` (asks CI again).
- "PR #n now points at …: someone else pushed": review and merge it yourself (tick sees the merge and starts the
  next step), or push the named commit back and re-run.
- "PR #n was closed unmerged": reopen it and re-run, or delete the feature folder (a note "sbx rm … failed": remove
  the sandbox first).
- `[running?]`: the run died; tick or `pipeline <name> --detach` resumes it (a VM it left running is stopped first;
  sbx itself stops a VM 30 s after its last session).
- `paused`, "interrupted (Ctrl+C, SIGTERM or a closed terminal)": `pipeline <name> --detach` (a background run no
  closed terminal stops).
- "the GitHub App key … missing / mode": `pipeline doctor` (its `app key` line) or `pipeline prove-app`
  diagnoses it.
