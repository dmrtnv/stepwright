# Install

About an hour, most of it waiting for downloads. Each step ends with a **Check**: don't go on until it passes.

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
- Packages: `sudo apt install git jq python3 curl`, plus **Node** (any LTS; the VMs get the same version) and
  **Docker Engine** that your user can run (`docker run hello-world` works without sudo).

**Check:** `python3 --version` (≥ 3.12), `node --version`, `claude --version`, `docker run --rm hello-world`,
`ls /dev/kvm`.

Read [Terms of use](README.md#terms-of-use) before you start.

## 1. Install Docker Sandboxes

1. Install the `docker-sbx` package by following
   [Docker's install guide](https://docs.docker.com/ai/sandboxes/install/).
2. Add yourself to the `kvm` group: `sudo usermod -aG kvm "$USER"`. Then log out and back in.
3. **Don't run any `sbx` command yet.** The first one would start a daemon outside the limits you set in step 3.

**Check:** `command -v sbx` prints a path; `id -nG | grep -w kvm` prints a line.

## 2. Get stepwright and run init

```bash
git clone https://github.com/dmrtnv/stepwright.git ~/.local/share/pipeline/runner
cd ~/.local/share/pipeline/runner
git checkout "$(git describe --tags --abbrev=0 2>/dev/null || echo main)"   # the latest release, if any
./pipeline init
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
name, init skips it, and `pipeline doctor` reports it as a FAIL until you replace it with the kit's link. Stage VMs
always use the kit's either way.
Stage VMs also get your `~/.claude/CLAUDE.md`, your `~/.claude/rules/` and your other skills and agents. A skill
or agent with a kit name is replaced by the kit's, and `pipeline-feature` stays on the host. Keep secrets out of them.

**Check:** `pipeline help` lists the commands. If you get "command not found", add `~/.local/bin` to your `PATH`.
Then run `pipeline doctor`: the `tool`, `kvm group`, `docker`, `runner` and `kit` lines PASS. The lines for later
steps FAIL until you do those steps; each FAIL has a `fix:` line under it.

## 3. Start the sandbox daemon

1. Review the caps in `~/.config/systemd/user/sbx.service` (`MemoryHigh`, `MemoryMax` and `CPUQuota`). The comments
   in the unit explain them. If init kept a unit you already had, it says so: that unit's caps are the ones that
   apply.
2. Enable it, and let your units run while you are logged out:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now sbx.service
   loginctl enable-linger "$USER"
   ```

3. Run the one-time sbx setup, then sign in to Docker (it opens a browser):

   ```bash
   ~/.local/share/pipeline/runner/sbx-host/install-sbx.sh
   sbx login
   ```

**Check:** in `pipeline doctor`, the `unit sbx.service` and `linger` lines PASS; `sbx-host status` prints the memory
budget.
`sbx create --help` lists `--skills`. Stage VMs are created with `--skills off`, and an older sbx fails every create:
update the `docker-sbx` package.

## 4. Create your GitHub App

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

**Check:** in `pipeline doctor`, the `app key` line PASSes.

## 5. Configure

Edit `~/.config/pipeline/config.json`, which init wrote:
- `app.id` and `app.slug`: from step 4.
- `app.reviewer`: your GitHub login. The pipeline requests your review on each PR.
- `repos`: `{"<owner>/<repo>": "<path of your checkout>"}`, for example `{"alice/shop": "~/code/shop"}`.

`pipeline doctor` reports the config as a FAIL while it still holds the example's values.

init sized the stage VM (`vm`) and how many stages run at once (`admission.slots`) for this machine. The defaults for
the other limits (`admission`) and the sandbox template (`template`) are described in the README.

Optional: phone notifications through [ntfy](https://ntfy.sh). Put a random topic name on one line in
`~/.config/ntfy/topic`, `chmod 600` it, and subscribe to that topic in the ntfy app.

**Check:** in `pipeline doctor`, the `config` and `sbx-host reserve` lines PASS. Then run
`pipeline prove-app --repo <owner>/<repo>`: every permission line must PASS. Lines about rules, CI runs
or job logs may still FAIL; step 6 fixes them.

## 6. Prepare your repo

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

## 7. Build the VM template and test a sandbox

```bash
pipeline template build
~/.local/share/pipeline/runner/sandbox/gate.py --repo <path of your checkout>
```

Run `gate.py` from a terminal: the first time, it asks you to `/login` to Claude once inside a sandbox. It makes a few
tiny Claude calls on your subscription.

**Check:** `gate.py` ends with `N checks, 0 FAIL`. It prints that line only after every check has run.

## 8. Turn on the timer

init installed the tick units in step 2. If `node` or `claude` live outside `/usr/local/bin` and `/usr/bin`
(`command -v node claude`), first add their directories to `PATH=` in `~/.config/systemd/user/pipeline-tick.service`.
Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now pipeline-tick.timer
```

**Check:** `systemctl --user list-timers pipeline-tick.timer` shows the next run, `pipeline status` prints a
header without errors, and `pipeline doctor` ends with `N checks, 0 FAIL`.

## 9. Run your first feature

1. In your checkout, start Claude Code and run `/pipeline-feature <name>`. It interviews you and writes the steps
   under `.claude/plans/<name>/`.
2. Run `pipeline <name> --detach`, or wait up to 5 minutes for the timer to start it.
3. Watch it with `pipeline status --watch`. The README explains what each state needs from you.
4. When the PR turns green, review it and merge it. The next step starts on its own.

## Update

When `pipeline status` shows no running stage:

```bash
cd ~/.local/share/pipeline/runner && git fetch --tags && git checkout <new tag>
./pipeline init
```

`pipeline init` links any skill or agent the new tag adds to the kit and skips everything you already have. Then rerun `sandbox/gate.py` if the [CHANGELOG](CHANGELOG.md) says so. To roll back, check out the previous tag. Config
(`~/.config/pipeline/`) and state (`~/.local/state/pipeline/`) live outside the clone, so a checkout never touches them.

## Manual setup (without init)

What `pipeline init` does in steps 2 to 5, by hand. Every command refuses to overwrite what you already have.

```bash
cd ~/.local/share/pipeline/runner
mkdir -p ~/.local/bin ~/.claude/skills ~/.claude/agents ~/.config/systemd/user ~/.config/pipeline ~/.config/sbx-host
ln -s "$PWD/pipeline" ~/.local/bin/pipeline
ln -s "$PWD/sbx-host/sbx-host" ~/.local/bin/sbx-host
for skill in kit/skills/*; do ln -s "$PWD/$skill" ~/.claude/skills/; done
for agent in kit/agents/*; do ln -s "$PWD/$agent" ~/.claude/agents/; done
cp -n systemd/sbx.service systemd/pipeline-tick.service systemd/pipeline-tick.timer ~/.config/systemd/user/
cp -n examples/config.example.json ~/.config/pipeline/config.json
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

## Uninstall

```bash
systemctl --user disable --now pipeline-tick.timer sbx.service
rm ~/.local/bin/pipeline ~/.local/bin/sbx-host ~/.config/systemd/user/{sbx.service,pipeline-tick.service,pipeline-tick.timer}
find ~/.claude/skills ~/.claude/agents -maxdepth 1 -lname "$HOME/.local/share/pipeline/runner/*" -delete
rm -rf ~/.local/share/pipeline ~/.config/pipeline ~/.config/sbx-host ~/.local/state/pipeline
```

Finally, delete the GitHub App in its settings, and remove the sandboxes and templates (`sbx ls`, `sbx rm`).
