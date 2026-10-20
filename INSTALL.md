# Install

About an hour, most of it waiting for downloads. Each step ends with a **Check**: don't go on until it passes.

## What you need

- **Ubuntu 24.04 or newer** (x86_64) with systemd, and hardware virtualization (`/dev/kvm` exists).
- **≥ 24 GiB RAM and ≥ 8 CPU threads.** Each stage runs in a VM with 8 GiB and 6 CPUs.
- **Claude Code** (`claude`) signed in with your own Claude subscription. Max is recommended: stages are long.
- **A Docker account** (free) for Docker Sandboxes.
- **A GitHub account** and a repo you want the pipeline to work on.
- Packages: `sudo apt install git jq python3 curl`, plus **Node** (any LTS; the VMs get the same version) and
  **Docker Engine** that your user can run (`docker run hello-world` works without sudo).

**Check:** `python3 --version` (≥ 3.12), `node --version`, `claude --version`, `docker run --rm hello-world`,
`ls /dev/kvm`.

Read [Terms of use](README.md#terms-of-use) before you start.

## 1. Install Docker Sandboxes

1. Install the `docker-sbx` package by following
   [Docker's install guide](https://docs.docker.com/ai/sandboxes/install/).
2. Add yourself to the `kvm` group: `sudo usermod -aG kvm "$USER"`. Then log out and back in.
3. **Don't run any `sbx` command yet.** The first one would start a daemon outside the limits you set in step 4.

**Check:** `command -v sbx` prints a path; `id -nG | grep -w kvm` prints a line.

## 2. Get stepwright

```bash
git clone https://github.com/dmrtnv/stepwright.git ~/.local/share/pipeline/runner
cd ~/.local/share/pipeline/runner
git checkout "$(git describe --tags --abbrev=0 2>/dev/null || echo main)"   # the latest release, if any
mkdir -p ~/.local/bin
ln -s "$PWD/pipeline" ~/.local/bin/pipeline
ln -s "$PWD/sbx-host/sbx-host" ~/.local/bin/sbx-host
```

Keep this clone for running the pipeline only. Don't develop in it.

**Check:** `pipeline help` lists the commands. If you get "command not found", add `~/.local/bin` to your `PATH`.

## 3. Install the stage kit

Stages run Claude Code skills (plan, implement, review, respond). Link them into your Claude config:

```bash
cd ~/.local/share/pipeline/runner
mkdir -p ~/.claude/skills ~/.claude/agents
for skill in kit/skills/*; do ln -s "$PWD/$skill" ~/.claude/skills/; done
ln -s "$PWD/kit/agents/web-researcher.md" ~/.claude/agents/
```

`ln` refuses to overwrite: if you already have a skill with the same name, decide which one to keep.
Everything in `~/.claude/CLAUDE.md`, `~/.claude/skills` and `~/.claude/agents` is copied into every stage VM, so
keep secrets out of them.

**Check:** `ls ~/.claude/skills` shows `plan-harden`, `plan-implement`, `review-branch`, `review-respond`, `pr-text`,
`wrap-up`, `commit-message` and `pipeline-feature`.

## 4. Start the sandbox daemon

1. Copy the unit:

   ```bash
   cd ~/.local/share/pipeline/runner
   mkdir -p ~/.config/systemd/user
   cp systemd/sbx.service ~/.config/systemd/user/
   ```

2. Edit `MemoryHigh`, `MemoryMax` and `CPUQuota` in it to fit your machine. The comments show how; `free -g` and
   `nproc` give the numbers.
3. Enable it, and let your units run while you are logged out:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now sbx.service
   loginctl enable-linger "$USER"
   ```

4. Run the one-time sbx setup, then sign in to Docker (it opens a browser):

   ```bash
   sbx-host/install-sbx.sh
   sbx login
   ```

**Check:** `systemctl --user is-active sbx.service` prints `active`; `sbx-host status` prints the memory budget.

## 5. Create your GitHub App

The pipeline pushes branches and opens PRs as this App, never as you.

1. Open <https://github.com/settings/apps/new>.
2. Fill in:
   - **Name**: anything, for example `<you>-pipeline`.
   - **Homepage URL**: your repo's URL.
   - **Webhook**: untick **Active**.
   - **Repository permissions**: Contents *Read and write*, Pull requests *Read and write*, Checks *Read-only*,
     Actions *Read-only*. Leave everything else at *No access*, Workflows included.
   - **Where can this GitHub App be installed?**: Only on this account.
3. Click **Create GitHub App**. Note the **App ID**, and the **slug**: the last part of the app's URL.
4. Under **Private keys**, click **Generate a private key**, then move the downloaded file into place:

   ```bash
   mkdir -p ~/.config/pipeline
   mv ~/Downloads/<app-name>.*.private-key.pem ~/.config/pipeline/app.pem
   chmod 600 ~/.config/pipeline/app.pem
   ```

5. Click **Install App**, then **Only select repositories**, pick your repo and install.

## 6. Configure

```bash
cd ~/.local/share/pipeline/runner
cp examples/config.example.json config.json
```

Edit `config.json`:
- `app.id` and `app.slug`: from step 5.
- `app.reviewer`: your GitHub login. The pipeline requests your review on each PR.
- `repos`: `{"<owner>/<repo>": "<path of your checkout>"}`, for example `{"alice/shop": "~/code/shop"}`.

The defaults for the limits (`admission`) and the sandbox template (`template`) are described in the README.

Optional: phone notifications through [ntfy](https://ntfy.sh). Put a random topic name on one line in
`~/.config/ntfy/topic`, `chmod 600` it, and subscribe to that topic in the ntfy app.

**Check:** `pipeline prove-app --repo <owner>/<repo>`. Every permission line must PASS. Lines about rules, CI runs
or job logs may still FAIL; step 7 fixes them.

## 7. Prepare your repo

Do this in your checkout, then commit and push it to the default branch.

1. **`.claude/pipeline.json`** tells the pipeline how to set up and check the repo. Start from a preset:
   - `sandbox/pipeline.python.json` for Python;
   - `sandbox/pipeline.node-pnpm.json` for a pnpm monorepo.

   Adjust `setup` (install commands), `setupEgress` (hosts setup may reach), `checkFast` (the commands that must
   pass) and `ci.requiredChecks` (your CI job names). The README lists every key.
2. **`.github/CODEOWNERS`** with one line, `* @<your-login>`, so every PR needs your approval.
3. **A CI workflow** that runs on `pull_request`, with job names matching `ci.requiredChecks`.
4. **`.gitignore`**: add `.claude/plans/`, where your feature briefs and plans live.
5. **Rulesets**, after one CI run has finished on the default branch:
   `~/.local/share/pipeline/runner/examples/rulesets.sh <owner>/<repo> <check> [<check>…]`.
   They need a public repo, or GitHub Pro or Team for a private one.

**Check:** `pipeline prove-app --repo <owner>/<repo>` and `pipeline prove-gate --repo <owner>/<repo>` (prove-gate
opens a throwaway draft PR and closes it again): every line PASS.

## 8. Build the VM template and test a sandbox

```bash
pipeline template build
~/.local/share/pipeline/runner/sandbox/gate.py --repo <path of your checkout>
```

Run `gate.py` from a terminal: the first time, it asks you to `/login` to Claude once inside a sandbox. It makes a few
tiny Claude calls on your subscription.

**Check:** every `gate.py` line prints PASS.

## 9. Turn on the timer

```bash
cd ~/.local/share/pipeline/runner
cp systemd/pipeline-tick.service systemd/pipeline-tick.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pipeline-tick.timer
```

If `node` or `claude` live outside `/usr/local/bin` and `/usr/bin` (`command -v node claude`), add their
directories to `PATH=` in `pipeline-tick.service`.

**Check:** `systemctl --user list-timers pipeline-tick.timer` shows the next run, and `pipeline status` prints a
header without errors.

## 10. Run your first feature

1. In your checkout, start Claude Code and run `/pipeline-feature <name>`. It interviews you and writes the steps
   under `.claude/plans/<name>/`.
2. Run `pipeline <name> --detach`, or wait up to 5 minutes for the timer to start it.
3. Watch it with `pipeline status --watch`. The README explains what each state needs from you.
4. When the PR turns green, review it and merge it. The next step starts on its own.

## Update

When `pipeline status` shows no running stage:

```bash
cd ~/.local/share/pipeline/runner && git fetch --tags && git checkout <new tag>
```

Then rerun `sandbox/gate.py` if the release notes say so. To roll back, check out the previous tag.

## Uninstall

```bash
systemctl --user disable --now pipeline-tick.timer sbx.service
rm ~/.local/bin/pipeline ~/.local/bin/sbx-host ~/.config/systemd/user/{sbx.service,pipeline-tick.service,pipeline-tick.timer}
find ~/.claude/skills ~/.claude/agents -maxdepth 1 -lname "$HOME/.local/share/pipeline/runner/*" -delete
rm -rf ~/.local/share/pipeline ~/.config/pipeline
```

Finally, delete the GitHub App in its settings, and remove the sandboxes and templates (`sbx ls`, `sbx rm`).
