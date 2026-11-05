# Install

Read [Before you run it](README.md#before-you-run-it) first. It covers the terms, the cost and the risks.

About an hour, most of it waiting for downloads. Paste each block into a terminal. Each step ends with a **Check**:
don't go on until it passes. When a block starts with lines like `REPO=<owner>/<repo>`, fill in those lines first.

## What you need

- **Ubuntu 24.04 or newer** (x86_64) with systemd, and hardware virtualization (`/dev/kvm` exists). Tested on
  Ubuntu 26.04.
- **≥ 24 GiB RAM and ≥ 8 CPU threads.** Each stage runs in a VM with 8 GiB and 6 CPUs by default (`vm` in the config).
  With less RAM, `pipeline init` gives each stage VM 4 GiB instead of 8, and under 16 GiB it warns. By default sbx-host
  starts no VM while less than 10 GiB of RAM is free (`floorGib` in `~/.config/sbx-host/config.json`), so a 16 GiB box
  needs most of its RAM free when a stage starts.
- **Claude Code** (`claude`) signed in with your own Claude subscription. Max is recommended: stages are long.
- **A Docker account** (free) for Docker Sandboxes.
- **A GitHub account** and a repo you want the pipeline to work on.
- **The GitHub CLI** (`gh`), signed in with `gh auth login` as an admin of that repo. Step 6 uses it.
- Packages: `sudo apt install git jq python3 curl gh`, plus **Node** (any LTS; the VMs get the same version) and
  **Docker Engine** that your user can run (`docker run hello-world` works without sudo).

**Check:** `python3 --version` (≥ 3.12), `node --version`, `claude --version`, `gh auth status`,
`docker run --rm hello-world`, `ls /dev/kvm`.

## 1. Install Docker Sandboxes

1. Install the `docker-sbx` package by following
   [Docker's install guide](https://docs.docker.com/ai/sandboxes/install/).
2. **Don't run any `sbx` command yet.** The first one would start a daemon outside the limits you set in step 3.
3. Add yourself to the `kvm` group, then log out and back in:

   ```bash
   sudo usermod -aG kvm "$USER"
   ```

**Check:** `command -v sbx` prints a path; `id -nG | grep -w kvm` prints a line.

## 2. Get stepwright and run init

```bash
R=$HOME/.local/share/pipeline/runner
git clone https://github.com/dmrtnv/stepwright.git "$R"
git -C "$R" checkout "$(git -C "$R" describe --tags --abbrev=0 2>/dev/null || echo main)"   # the latest release, if any
"$R/pipeline" init
```

Keep this clone for running the pipeline only. Don't develop in it.

`pipeline init` sets up everything that lives in your home directory. It never replaces a file or link that already
exists: it prints `skipped … (exists)` for it instead. It never enables or starts anything. It writes:
- the links `~/.local/bin/pipeline` and `~/.local/bin/sbx-host` into this clone;
- a link in `~/.claude/skills` and `~/.claude/agents` for each skill and agent in the kit dir;
- the units `sbx.service`, `pipeline-tick.service` and `pipeline-tick.timer` in `~/.config/systemd/user/`, with
  `sbx.service`'s caps sized for this machine;
- `~/.config/pipeline/config.json`, from the example, with `vm` and `admission.slots` sized for this machine;
- `~/.config/sbx-host/config.json`, with the memory each stage VM reserves.

It first prints what it sized: RAM (MemTotal from `/proc/meminfo`, rounded up to a whole GiB) and CPU threads, then
the caps. Then it prints one line per item, and the next commands.

Stages run Claude Code skills (plan, implement, review, respond). They live in the kit dir of this clone, and
stage VMs get them from there, so a stage runs the skills of the checked-out tag. The links make them available
on the host too: for `/pipeline-feature` and your own sessions. If you already have a skill or agent with a kit
name, init skips it.

The kit has two kinds of entries:
- Contract skills: `plan-harden`, `plan-implement`, `review-branch`, `review-respond` and `pipeline-feature`. The
  runner depends on them: stage VMs always use the kit's four stage skills, and `pipeline-feature` stays on the
  host. Your own copy of one is a `pipeline doctor` FAIL until you replace it with the kit's link.
- Convention entries: the skills `commit-message`, `pr-text` and `wrap-up`, and the agent `web-researcher.md`. You
  can replace them with your own: a copy, or a link that resolves outside the kit dir. Stage VMs then use yours,
  and `pipeline doctor` shows it as PASS "your own, stage VMs use it".

Your own convention entry must keep what the stages rely on:
- `commit-message` supports commit mode: plan-implement and review-respond use it to commit.
- `wrap-up` never asks the user anything: stages run unattended.
- `pr-text` ships a `check.py`. The respond gate runs `python3 check.py <file>` on pr.md, with a 60 s timeout. It
  prints one problem per line and exits 1, or exits 0 when there are none. Without `check.py`, doctor FAILs.
- pr.md keeps the title on line 1, then a blank line and the body.
- The agent keeps the name `web-researcher`: plan-harden spawns it by name.

No override changes the trailer and personal-text commit gates, or the redaction of pr.md.

Stage VMs also get your `~/.claude/CLAUDE.md`, your `~/.claude/rules/` and your other skills and agents.
`pipeline-feature` stays on the host. Keep secrets out of them. [What leaves your
machine](README.md#what-leaves-your-machine) lists everything that goes to Anthropic.

**Check:** `pipeline help` lists the commands. If you get "command not found", add `~/.local/bin` to your `PATH`.
Then run `pipeline doctor`: the `tool`, `kvm group`, `docker`, `runner` and `kit` lines PASS. The lines for later
steps FAIL until you do those steps; each FAIL has a `fix:` line under it.

## 3. Start the sandbox daemon

### 3.1 Review the caps

Open `~/.config/systemd/user/sbx.service` and review `MemoryHigh`, `MemoryMax` and `CPUQuota`. The comments in the
unit explain them. If init kept a unit you already had, it says so: that unit's caps are the ones that apply.

```bash
grep -E '^(MemoryHigh|MemoryMax|CPUQuota)=' ~/.config/systemd/user/sbx.service
```

**Check:** the command prints the three caps.

### 3.2 Enable the daemon

Enable it, and let your units run while you are logged out:

```bash
systemctl --user daemon-reload
systemctl --user enable --now sbx.service
loginctl enable-linger "$USER"
```

**Check:** in `pipeline doctor`, the `unit sbx.service` and `linger` lines PASS.

### 3.3 Set up sbx and sign in to Docker

Run the one-time sbx setup, then sign in to Docker (it opens a browser):

```bash
~/.local/share/pipeline/runner/sbx-host/install-sbx.sh
sbx login
```

**Check:** `sbx-host status` prints the memory budget, and `sbx create --help` lists `--skills`. Stage VMs are created
with `--skills off`, and an older sbx fails every create: update the `docker-sbx` package.

## 4. Create your GitHub App

The pipeline pushes branches and opens PRs as this App, never as you.

1. Open <https://github.com/settings/apps/new>.
2. Fill in:
   1. **GitHub App name**: anything, for example `<you>-pipeline`.
   2. **Homepage URL**: your repo's URL.
   3. **Webhook**: untick **Active**.
   4. **Repository permissions**: Contents *Read and write*, Pull requests *Read and write*, Checks *Read-only*,
      Actions *Read-only*. Leave everything else at *No access*, Workflows included.
   5. **Where can this GitHub App be installed?**: **Only on this account**.
3. Click **Create GitHub App**. Note the **App ID**, and the **slug**: the last part of the App's URL.
4. Under **Private keys**, click **Generate a private key**. Your browser saves it in `~/Downloads`. Keep only this
   App's key there, then move it into place:

   ```bash
   mkdir -p ~/.config/pipeline
   mv ~/Downloads/*.private-key.pem ~/.config/pipeline/app.pem
   chmod 600 ~/.config/pipeline/app.pem
   ```

5. Click **Install App**, then **Only select repositories**, pick your repo and install.

**Check:** `ls -l ~/.config/pipeline/app.pem` starts with `-rw-------`, and `pipeline doctor | grep 'app key'` shows
PASS.

## 5. Configure

### 5.1 Fill in the config

This edits `~/.config/pipeline/config.json`, which init wrote. Fill in the five lines at the top first:
- `APP_ID` and `APP_SLUG`: from step 4.
- `REVIEWER`: your GitHub login. The pipeline requests your review on each PR.
- `REPO` and `CHECKOUT`: your repo and the path of your checkout of it.

```bash
APP_ID=<App ID from step 4>
APP_SLUG=<slug from step 4>
REVIEWER=<your GitHub login>
REPO=<owner>/<repo>
CHECKOUT=$HOME/code/<repo>
python3 -c '
import json, pathlib, sys
app_id, slug, reviewer, repo, checkout = sys.argv[1:]
path = pathlib.Path.home() / ".config/pipeline/config.json"
config = json.loads(path.read_text())
config["app"].update(id=int(app_id), slug=slug, reviewer=reviewer)
config["repos"].pop("your-github-login/your-repo", None)
config["repos"][repo] = checkout
path.write_text(json.dumps(config, indent=2) + "\n")
' "$APP_ID" "$APP_SLUG" "$REVIEWER" "$REPO" "$CHECKOUT"
```

`pipeline doctor` reports the config as a FAIL while it still holds the example's values.

init sized the stage VM (`vm`) and how many stages run at once (`admission.slots`) for this machine. The defaults for
the other limits (`admission`) and the sandbox template (`template`) are described in the README.

**Check:** `pipeline doctor | grep -E '^(PASS|FAIL)  (config|sbx-host reserve)'` shows two lines, and both read
PASS. Then run `pipeline prove-app --repo "$REPO"`: every permission line must PASS. Lines about rules, CI runs or
job logs may still FAIL; step 6 fixes them.

### 5.2 Turn on phone notifications (optional)

This sends notifications through [ntfy](https://ntfy.sh). It creates a random topic name once and never replaces
one you have.

```bash
mkdir -p ~/.config/ntfy
test -e ~/.config/ntfy/topic || python3 -c 'import secrets; print("pipeline-" + secrets.token_hex(12))' > ~/.config/ntfy/topic
chmod 600 ~/.config/ntfy/topic
```

**Check:** `cat ~/.config/ntfy/topic` prints the topic. Subscribe to that topic in the ntfy app.

## 6. Prepare your repo

Steps 6.1 to 6.3 write files in your checkout. Step 6.4 commits and pushes them, and step 6.5 protects the default
branch.

### 6.1 Add the recipe

`.claude/pipeline.json` tells the pipeline how to set up and check the repo. This copies a preset: `python` or
`node-pnpm`. It never replaces a recipe you already have.

```bash
CHECKOUT=$HOME/code/<repo>
PRESET=python            # or node-pnpm
mkdir -p "$CHECKOUT/.claude"
cp --update=none ~/.local/share/pipeline/runner/examples/pipeline.$PRESET.json "$CHECKOUT/.claude/pipeline.json"
```

Then adjust `setup` (install commands), `setupEgress` (hosts setup may reach), `checkFast` (the commands that must
pass) and `ci.requiredChecks` (your CI job names). The README lists every key. With drizzle-kit, add
`"migrations": {"dir": "<out>", "journal": "<out>/meta/_journal.json", "generate": "drizzle-generate.py"}`,
where `<out>` is your drizzle-kit `out` dir.

**Check:** `jq -r '.ci.requiredChecks[]' "$CHECKOUT/.claude/pipeline.json"` prints your CI job names.

### 6.2 Add CODEOWNERS

One line, `* @<your-login>`, so every PR needs your approval. It never replaces a CODEOWNERS you already have.

```bash
CHECKOUT=$HOME/code/<repo>
LOGIN=<your GitHub login>
mkdir -p "$CHECKOUT/.github"
test -e "$CHECKOUT/.github/CODEOWNERS" || printf '* @%s\n' "$LOGIN" > "$CHECKOUT/.github/CODEOWNERS"
```

**Check:** `cat "$CHECKOUT/.github/CODEOWNERS"` names you as an owner.

### 6.3 Ignore the plans folder

Your feature briefs and plans live in `.claude/plans/`.

```bash
CHECKOUT=$HOME/code/<repo>
touch "$CHECKOUT/.gitignore"
[ -z "$(tail -c1 "$CHECKOUT/.gitignore")" ] || echo >> "$CHECKOUT/.gitignore"
grep -qxF '.claude/plans/' "$CHECKOUT/.gitignore" || printf '%s\n' '.claude/plans/' >> "$CHECKOUT/.gitignore"
```

**Check:** `grep -x '.claude/plans/' "$CHECKOUT/.gitignore"` prints the line once.

### 6.4 Add CI, then commit and push

Add a CI workflow that runs on `pull_request`, with job names matching `ci.requiredChecks`. Then commit everything
to the default branch and push it:

```bash
CHECKOUT=$HOME/code/<repo>
git -C "$CHECKOUT" add .claude/pipeline.json .github .gitignore
git -C "$CHECKOUT" commit -m "chore: Prepare the repo for stepwright"
git -C "$CHECKOUT" push
```

**Check:** `git -C "$CHECKOUT" status --short --branch` shows no `ahead`, and the CI run on the default branch has
finished on GitHub.

### 6.5 Add the rulesets

Run this once, after one CI run has finished on the default branch. A second run fails, because the rulesets already
exist. Rulesets need a public repo, or GitHub Pro or Team for a private one.

```bash
CHECKOUT=$HOME/code/<repo>
REPO=<owner>/<repo>
jq -r '.ci.requiredChecks[]' "$CHECKOUT/.claude/pipeline.json" | xargs -d '\n' ~/.local/share/pipeline/runner/examples/rulesets.sh "$REPO"
```

**Check:** `pipeline prove-app --repo "$REPO"` and `pipeline prove-gate --repo "$REPO"` (prove-gate opens a
throwaway draft PR and closes it again): every line PASS.

## 7. Build the VM template and test a sandbox

```bash
CHECKOUT=$HOME/code/<repo>
pipeline template build
~/.local/share/pipeline/runner/sandbox/gate.py --repo "$CHECKOUT"
```

Run `gate.py` from a terminal: the first time, it asks you to `/login` to Claude once inside a sandbox. It makes a few
tiny Claude calls on your subscription.

**Check:** `gate.py` ends with `N checks, 0 FAIL`. It prints that line only after every check has run.

## 8. Turn on the timer

init installed the tick units in step 2. This adds the directories of `node` and `claude` to the unit's `PATH=`
once, then starts the timer:

```bash
UNIT=~/.config/systemd/user/pipeline-tick.service
DIRS=$(dirname "$(command -v node)"):$(dirname "$(command -v claude)")
grep -qF "PATH=$DIRS:" "$UNIT" || sed -i "s|^Environment=PATH=|&$DIRS:|" "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now pipeline-tick.timer
```

**Check:** `systemctl --user list-timers pipeline-tick.timer` shows the next run, `pipeline status` prints a
header without errors, and `pipeline doctor` ends with `N checks, 0 FAIL`.

## 9. Run your first feature

1. In your checkout, start Claude Code and run `/pipeline-feature <name>`. It interviews you and writes the steps
   under `.claude/plans/<name>/`.
2. Start it, or wait up to 5 minutes for the timer to start it:

   ```bash
   NAME=<name>
   pipeline "$NAME" --detach
   ```

3. Watch it with `pipeline status --watch`. The README explains what each state needs from you.
4. When the PR turns green, review it and merge it. The next step starts on its own.

**Check:** `pipeline show "$NAME"` shows the feature's steps and the current stage.

## Update

When `pipeline status` shows no running stage, fill in the tag and run:

```bash
TAG=<new tag>
R=$HOME/.local/share/pipeline/runner
git -C "$R" fetch --tags
git -C "$R" checkout "$TAG"
"$R/pipeline" init
```

`pipeline init` links any skill or agent the new tag adds to the kit and skips everything you already have. Then
rerun `sandbox/gate.py` if the [CHANGELOG](CHANGELOG.md) says so. To roll back, check out the previous tag. Config
(`~/.config/pipeline/`) and state (`~/.local/state/pipeline/`) live outside the clone, so a checkout never touches
them.

**Check:** `git -C "$R" describe --tags` prints the tag, and `pipeline doctor` ends with `N checks, 0 FAIL`.

## Manual setup (without init)

What `pipeline init` does in steps 2 to 5, by hand. Every command refuses to overwrite what you already have.

```bash
R=$HOME/.local/share/pipeline/runner
mkdir -p ~/.local/bin ~/.claude/skills ~/.claude/agents ~/.config/systemd/user ~/.config/pipeline ~/.config/sbx-host
ln -s "$R/pipeline" ~/.local/bin/pipeline
ln -s "$R/sbx-host/sbx-host" ~/.local/bin/sbx-host
for skill in "$R"/kit/skills/*; do ln -s "$skill" ~/.claude/skills/; done
for agent in "$R"/kit/agents/*; do ln -s "$agent" ~/.claude/agents/; done
cp --update=none "$R"/systemd/sbx.service "$R"/systemd/pipeline-tick.service "$R"/systemd/pipeline-tick.timer ~/.config/systemd/user/
cp --update=none "$R/examples/config.example.json" ~/.config/pipeline/config.json
```

Then set the caps in `~/.config/systemd/user/sbx.service` for your machine. RAM is MemTotal from `/proc/meminfo`
rounded up to a whole GiB, and threads is `nproc`:
- `MemoryMax` = RAM − 8 GiB, for example 24G on a 32 GiB machine.
- `MemoryHigh` = `MemoryMax` − 3 GiB.
- `CPUQuota` = (threads − 4) × 100 %, for example 1200% with 16 threads.

With fewer than 12 threads, set `vm.cpus` in the config to half the threads, for example `"vm": {"cpus": 4}` with 8
threads (the default is 6). Under 24 GiB of RAM, also set `vm.memoryGb` to 4, and `{"reserve": {"pl": 4608}}` in
`~/.config/sbx-host/config.json` (the VM's MiB plus 512). `admission.slots` is `MemoryHigh` in MiB divided by
`reserve.pl`, at least 1.

**Check:** in `pipeline doctor`, the `runner` and `kit` lines PASS.

## Uninstall

```bash
systemctl --user disable --now pipeline-tick.timer sbx.service
rm ~/.local/bin/pipeline ~/.local/bin/sbx-host ~/.config/systemd/user/{sbx.service,pipeline-tick.service,pipeline-tick.timer}
find ~/.claude/skills ~/.claude/agents -maxdepth 1 -lname "$HOME/.local/share/pipeline/runner/*" -delete
rm -rf ~/.local/share/pipeline ~/.config/pipeline ~/.config/sbx-host ~/.local/state/pipeline
```

Finally, delete the GitHub App in its settings, and remove the sandboxes and templates (`sbx ls`, `sbx rm`).

**Check:** `command -v pipeline` prints nothing.
