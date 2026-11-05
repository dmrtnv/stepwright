# Contributing

## Rules

- Python 3.12 or newer, standard library only. No new dependency.
- Linux with systemd only.
- [INSTALL.md](INSTALL.md) is the only install path. A change to setup, config keys, units or the kit updates it in
  the same PR.
- Kit edits go in `kit/`. Stage VMs and your links read the skills from there.
- Nothing personal ships. `tests/test_public.py` fails on home paths, real emails and private names.

## Tests

```bash
python3 -m unittest discover tests
```

The full suite takes about 13 minutes. Every behavior change comes with a test. Never weaken or skip one.

Rerun `sandbox/gate.py` after a `claude` or sbx upgrade, or a change to `STAGES` in `core.py`.

## Commits

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat: Add …`, `fix: …`,
  `docs: …`.
- Commit with your GitHub noreply email, so your own address stays private. Fill in the line at the top:

  ```bash
  EMAIL=<id>+<login>@users.noreply.github.com
  git config user.email "$EMAIL"
  ```

  GitHub shows your noreply address under **Settings**, then **Emails**.

## Changelog

Each user-visible change adds an entry under **Unreleased** in [CHANGELOG.md](CHANGELOG.md). Keep its
**Rerun the gate** line up to date: yes or no, with the reason.

## Public API

A change to any of these is a breaking change for users. Each has one home:
- commands and flags: `pipeline help` and `pipeline <command> -h`;
- config keys: `examples/config.example.json` and [INSTALL step 5](INSTALL.md#5-configure);
- `pipeline.json` keys: [README.md](README.md#setup), and `REQUIRED` and `OPTIONAL` in `adapter.py`;
- convention entries, what they must keep, and the `check.py` command line:
  [INSTALL step 2](INSTALL.md#2-get-stepwright-and-run-init);
- state that survives an update: `~/.config/pipeline/`, `~/.local/state/pipeline/` and each feature's `.pipeline/`.
