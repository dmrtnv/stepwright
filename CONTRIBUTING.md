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
**Rerun the gate** line up to date: yes or no, with the reason. Keep it on one line, followed by a blank
line. `Changelog` in `tests/test_docs.py` fails when a release after v0.1, or a non-empty Unreleased, has no such
line, has two, or puts text on the next line.

## Releasing

Maintainers cut a release with [`dev/release.py`](dev/release.py). You need:
- a clean checkout whose HEAD is `origin/main`;
- `gh` logged in as a repo admin;
- git set to sign with SSH (`gpg.format ssh` and `user.signingkey`).

1. From the checkout, run it with the new version:

   ```bash
   VERSION=v0.9
   dev/release.py "$VERSION"
   ```

   It opens the "chore: Release" PR, which turns the Unreleased entries into the version's section. It waits for
   the `test` check, squash-merges the PR, signs the tag on the merge commit and pushes it. Last, it publishes a
   GitHub Release whose notes are the version's CHANGELOG section.

   **Check:** `gh release view "$VERSION"` shows the Release.

If a run stops, fix what it names and run the same command again: it resumes where it stopped. "Already released"
means nothing is left to do. Watch the repo for releases on GitHub to hear of each one.

## Public API

A change to any of these is a breaking change for users. Each has one home:
- commands and flags: `pipeline help` and `pipeline <command> -h`;
- config keys: `examples/config.example.json`, [INSTALL step 5](INSTALL.md#5-configure) and
  [What runs where](docs/how-it-works.md#what-runs-where);
- `pipeline.json` keys: [`pipeline.json` keys](docs/how-it-works.md#pipelinejson-keys),
  and `REQUIRED` and `OPTIONAL` in `adapter.py`;
- convention entries, what they must keep, and the `check.py` command line:
  [INSTALL step 2](INSTALL.md#2-get-stepwright-and-run-init);
- state that survives an update: `~/.config/pipeline/`, `~/.local/state/pipeline/` and each feature's `.pipeline/`.
