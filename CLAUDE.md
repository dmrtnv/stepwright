# stepwright

An unattended PR pipeline: the `pipeline` CLI runs each step of a feature (plan → implement → review → respond) in its
own Docker Sandboxes (`sbx`) microVM, gates the result and opens a PR through a GitHub App. `sbx-host/` decides whether
one more VM fits the box. See `README.md` and `sandbox/README.md`.

## Rules
- Python ≥ 3.12, standard library only. No new dependency.
- Linux with systemd only (`/proc`, cgroups, user units).
- Tests: `python3 -m unittest discover tests`. Every behavior change comes with a test; never weaken or skip one.
- `system.md` and the VM layout use VM paths (`/home/agent/...`): they are right as they are.
- Commit messages and PR text are public: no home paths, personal emails or memory notes. Name the stage kit "the kit
  dir", never its home-relative path.
- Nothing personal ships: `tests/test_public.py` fails on real home paths, real emails and the owner's private names;
  its `ALLOWED` list only shrinks.
- `.github/**` and `.claude/**` are protected: the pipeline never edits them.
