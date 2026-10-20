# Pipeline sandbox

Each pipeline step runs in a Docker Sandboxes (`sbx`) microVM: own kernel, own Docker, no secrets, egress
deny-by-default. This folder holds the template and the gate that proves it; the runner's sbx code is `../backend.py`.

| File | What |
|---|---|
| `Dockerfile` + `build-template.sh` | template = `docker/sandbox-templates:claude-code-docker` (pinned digest) + host Node + host Claude Code or the given version (`build-template.sh [<x.y.z>]`); `docker image save` → `sbx template load`. The runner calls it (`config.json` `"template"`, `pipeline template build`); `pipeline template prune` removes old ones |
| `gate.py` | tests 1–9 (9: the runner's real stage argv + `--resume`); imports `../backend.py` |
| `pipeline.node-pnpm.json` | a `.claude/pipeline.json` preset for a pnpm/turbo monorepo with drizzle migrations (validated by `tests/test_adapter.py`) |

## Run order
```
# root: docker-sbx from Docker's apt repo (docs.docker.com/ai/sandboxes/install), you in the kvm group (log out/in)
../sbx-host/install-sbx.sh                     # sbx policy init deny-all, SSH agent forwarding off, sbx.service
sbx login                                      # Docker account (browser)
pipeline template build                        # the target template (config.json "template"); auto mode rebuilds on host upgrades itself
./gate.py --repo <checkout>                    # from a terminal; asks for one in-sandbox /login if needed
./gate.py --repo <checkout> --full             # + a real /plan-harden run (≤ $5)
```

## Egress model
- Global policy `deny-all`. The claude kit adds its own per-sandbox allows; everything outside
  `anthropic.com`/`claude.ai`/`claude.com` is denied per sandbox right after `sbx create`.
- Setup phase (runner's commands): per-sandbox allow of `setupEgress` from `pipeline.json`, removed before any
  agent stage (`sbx policy rm network --sandbox <name> --resource <host>`; takes effect live).

## Known sbx facts (docs + issues, 2026-09-29, sbx 0.46.0)
- `sbx exec` passes the exit code; a missing sandbox also exits 1 ⇒ check `sbx ls` first. No detached exec.
- `sbx cp` needs absolute sandbox paths. `sbx rm` needs `--force` non-interactively.
- `--clone` exposes the source repo read-only at `/run/sandbox/source` ⇒ clone from the runner's clean clone only.
- Claude subscription: `/login` inside the sandbox, token held host-side (docs); in-sandbox OAuth flaky (sbx #626).
- sbx's daemon auto-stops a VM 30 s after its last exec session disconnects (`daemon.log` "auto-stopped runtime
  after last session disconnected", 0.46.0, pilot 2026-09-30).
- Killing `sbx exec` does not kill the VM process (until that auto-stop) ⇒ the runner `sbx stop`s its VMs after each stage and at exit, and a
  run that finds a VM running which it did not start (a dead run's: OOM, `kill -9`) stops it before resuming.
- Verified by gate 2026-09-30 (sbx 0.46.0): base must be `claude-code-docker` (label `start-docker=true`), else no
  dockerd; `sbx policy rm` needs `--force` without a TTY; rule changes apply live; a per-sandbox deny beats a kit allow.
- The proxy accepts every TCP connect and then drops denied ones ⇒ test reachability by protocol reply, not connect.
- Claude login is a global host-side sbx secret; the VM only holds `…proxy-managed` placeholders, and the proxy swaps
  any Anthropic key for yours. Kit writes `settings.json` with `bypassPermissions` ⇒ the runner replaces it.
- Docker Hub layers come from `production.cloudfront.docker.com`; the auto-start banner goes to stderr.

Verified by Phase 3 probes (2026-09-30, sbx 0.46.0; the runner relies on each):
- P1 `--clone`: the VM's workspace is its own clone at the source's host path, with the source's git config copied,
  so `origin` is the source's origin (GitHub for the runner's clean clone: unreachable), and every VM start resets
  `remote.origin.url` to it (a `set-url` does not survive a stop; pilot 2026-09-30). The runner adds
  `url./run/sandbox/source.insteadOf <that URL>` to the workspace's `.git/config`, which survives: origin is then the
  clean clone (mounted read-only and live), a host commit is one `git fetch origin` away, and repo tools that ask
  origin (`db:squash`'s `ls-remote`) see the clean clone. The VM's
  `~/.gitconfig` carries `safe.directory` and your git `user.name`/`user.email`; a repo-local `user.*` in the
  workspace overrides it for author and committer and survives a stop/start (pilot 2026-10-01): the runner sets the
  App's bot there.
- P2 `--clone` also adds the host remote `sandbox-<name>` (`git://127.0.0.1:<port>/…`, a git-daemon in the VM). Its own
  refspecs fetch every branch into `refs/remotes/` and `refs/sandboxes/`, so the runner fetches its URL with one
  explicit refspec. `sbx stop` removes that remote; the next start restores it on a new port ⇒ read the URL after
  the VM runs. Git has no fetch-side object size limit (`transfer.maxObjectSize` does not exist): the runner caps new
  objects with `rev-list --disk-usage` after the fetch.
- P3 `sbx stop` takes ~0.4 s, an exec auto-starts the VM in ~1.6 s, and `claude -p` works after it (init
  `claude-opus-5-5`/`auto`, `unifiedWindows` present). No process survives a stop; the disk and dockerd's images do.
- P4 `sbx exec` forwards neither SIGINT nor SIGTERM. `sbx exec <name> -- pkill -INT -x claude` ends a turn in < 1 s
  (result `error_during_execution`).
- P5 `sbx cp` out of a VM copies a symlink as a symlink and a fifo as a fifo; a missing path exits 1 ("not found in
  container") ⇒ copy-out goes to a fresh temp dir and reads regular files only (O_NOFOLLOW, size cap).
- P6 A host Haiku call (`--tools '' --no-session-persistence`, ~$0.015) emits `rate_limit_event.unifiedWindows`.
- P7 The App token (repo-scoped) reads rules, PRs (`mergeable`), reviews and comments, GraphQL, check runs and job
  logs (a redirect fetched without the token): `pipeline prove-app`. The writes (draft PR, comment, ready flip,
  review request) are exercised by `pipeline prove-gate`.
