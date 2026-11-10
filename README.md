# stepwright

An unattended PR pipeline for your own GitHub repos. You write short briefs; stepwright turns each into a planned,
implemented, reviewed branch and a draft PR, one step at a time, while you work. Every stage is Claude Code running in
a throwaway [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) microVM with no secrets and deny-by-default
network. Claude never pushes or merges: the runner pushes `agent/**` branches through your GitHub App, **you** review
and merge, then the next step starts.

## Before you run it

Read this before you install. It covers the terms, the cost, the risks, what leaves your machine and what the
GitHub App can do.

### Terms

stepwright runs the unmodified `claude` CLI with **your own** Claude login. It is tested with a subscription login
only. Unattended use of a subscription is a gray area. Read the sources and decide for yourself.

- Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (§3) forbid accessing the services
  "through automated or non-human means, whether through a bot, script, or otherwise", "except when you are
  accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it" (checked 2026-11-05).
- The [Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance) says (checked 2026-11-05):
  - OAuth "is designed to support ordinary use of Claude Code";
  - "Developers building products … should use API key authentication";
  - this does not "prevent an end user from signing in to the unmodified Claude Code binary with their own Claude
    subscription";
  - "Advertised usage limits for Pro, Max, and Ultra plans assume ordinary, individual usage of Claude Code and the
    Agent SDK".
- The same page does not permit routing requests "through Free, Pro, Max, or Ultra plan credentials on behalf of
  their users".
- Each person installs stepwright and signs in with their own Claude account. Don't share one install or one login.
- You are responsible for your account. stepwright is not made, endorsed or supported by Anthropic.

### Cost

Every stage is a Claude Code session on Opus 5.5 (`claude-opus-5-5`). It falls back to Sonnet 5.5 when Opus is
overloaded. Each session has a time cap and a dollar cap:
- plan: 3 h, $40
- implement: 5 h, $60
- review: 3 h, $20
- respond: 4 h, $30

The normal path is plan, implement and up to 2 review and respond rounds: 22 h and $200 in caps.

Some events add sessions, each with its stage's caps:
- A red gate resumes the stage, up to 2 times.
- A rebase conflict, or a base branch that moved, runs a fixer session.
- A red CI run gets 2 fixer sessions, then more while the failure moves, up to 5 in all.
- Each round of PR comments runs a respond session.

The dollar caps are estimates at API prices, and "Spend can pass the cap"
([Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference), checked 2026-11-04).

No stage starts while your weekly usage is at 80 % or more, or your five-hour usage is at 70 % or more
(`admission` in the config). When Claude reports no five-hour window, only the weekly limit applies. To read your
usage, the host makes one small Haiku call when the last reading is over 30 minutes old.

Docker Sandboxes local use "is free … including for commercial work"
([Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), checked 2026-11-04).

### What the sandbox protects, and what it doesn't

It protects ([Docker Sandboxes docs](https://docs.docker.com/ai/sandboxes/), checked 2026-11-04, and
[sandbox/README.md](sandbox/README.md)):
- Each stage runs in its own microVM, with a "separate kernel per sandbox".
- Network egress is denied by default. A VM can reach only Anthropic's hosts, plus your `setupEgress` hosts during
  setup.
- Your Claude login is an sbx secret on the host: "The raw credential values never enter the VM".
- The GitHub App's private key never enters a VM. The host signs with it.

It doesn't protect:
- The agent can change anything on its branch. Protected paths, the tamper and secret gates, CI and your review
  check that.
- Briefs, repo content and CI logs can carry prompt injection.
- Your CI runs the agent's code with whatever secrets those jobs can read. The branches live in your repo, not a
  fork.

### What leaves your machine

To Anthropic, from each stage VM:
- the repo clone and the step's feature folder;
- your `~/.claude/CLAUDE.md`, your `~/.claude/rules/`, your other skills and agents, and the repo's Claude memory;
- the tail of a failed CI job's log (up to 12,000 characters), when a CI fixer session runs.

To Anthropic, from the host:
- the usage probe;
- `sandbox/gate.py`'s few test calls, when you run it.

To GitHub: the pushed commits, the PR title and body, the evidence comment and the review request.

To ntfy.sh, only when `~/.config/ntfy/topic` exists:
- a title of repo, feature and step;
- error lines, plan questions and PR links;
- up to 1,000 characters per message.

Anyone who knows the topic can read them. Keep it secret.

### What the GitHub App can do

- It has Contents and Pull requests write, Checks and Actions read, and Metadata read. Nothing else.
- It can push wherever your rulesets let a non-admin push. `examples/rulesets.sh` limits that to `agent/**`.
- Publish refuses to push unless the base branch requires a PR with at least one code-owner approval. It checks
  before every push.
- Its private key has no expiry. To revoke it, delete the key under the App's **Private keys**, or delete the App.
- **Actions write** (optional) lets the App rerun CI that failed without a failed step. It also lets the App
  dispatch any workflow on its own branches and delete runs, logs and caches.
- Grant Actions write only once deploy environments allow deploys from reviewed branches alone, with the deploy
  secrets in those environments.

### Requirements you can't change

- Node on the host: the VMs get the same version.
- x86_64: the VM template installs Node's `linux-x64` build.
- One reviewer: `app.reviewer` takes a single GitHub login.

## Requirements

- A Linux box with systemd and hardware virtualization.
- Claude Code, signed in with your own Claude subscription.
- A GitHub App and a repo of your own.

The full list, with a Check: [INSTALL: What you need](INSTALL.md#what-you-need).

## Quickstart

1. Install stepwright with [INSTALL.md](INSTALL.md). It takes about an hour.
2. In your repo, start Claude Code and run `/pipeline-feature <name>`. It writes the feature's steps.
3. Start the feature and watch it:

   ```bash
   NAME=<name>
   pipeline "$NAME" --detach
   pipeline status --watch
   ```

4. Review and merge each draft PR. The next step starts on its own.

**Check:** `pipeline show "$NAME"` lists the feature's steps and the current stage.

## Usage

`pipeline help` groups every command, and `pipeline <command> -h` shows one.
- `/pipeline-feature <name>` in Claude Code: create a feature.
- `pipeline <name>`: start or continue it. Add `--detach` to run it in the background.
- `pipeline status`: what needs you, what runs, what waits. `--watch` redraws it.
- `pipeline show <name>`: one feature in full, with the next command.
- `pipeline log <name>`: what a stage is doing now. `-f` follows it.
- `pipeline answer <name>`: answer a plan's questions.
- `pipeline revise <name>`: send a plan back with a `## Feedback` section.
- `pipeline autonomy <name> auto`: let the plan's own recommendations decide.
- `pipeline respond <name>`: act on your PR comments.
- `pipeline pause <name>` and `pipeline start <name>`: hold a feature and lift the hold.
- `pipeline stop --all`: stop everything now. `pipeline stop --clear` lets it run again.
- `pipeline doctor`: check the install.
- `pipeline update`: move to the newest release.
- `pipeline gc`: clean up after merged steps.
- `pipeline archive`: move fully merged features into `_archive/`.

Re-running is always safe. Every command, in detail: [Everyday use](docs/how-it-works.md#everyday-use).

## Troubleshooting

`pipeline status -v` shows why each step waits and the next command. What each status needs from you:
[docs/troubleshooting.md](docs/troubleshooting.md).

## Update

- stepwright: `pipeline update` ([INSTALL: Update](INSTALL.md#update)).
- Claude Code: `config.json` `"template"` decides what sandboxes run and when it is built.
  - `"claude": "host"` follows the host: after an update the next sandbox create builds the new template
    (`"build": "auto"`) or waits for `pipeline template build` (`"manual"`).
  - `"claude": "2.1.286"` pins: host updates change nothing until you move the pin (`pipeline template build`
    builds it now).
  - Existing sandboxes keep their template (a session never resumes across CLI versions). Then `gate.py`.
- Disk: `pipeline template prune` (also run by `gc`) removes every template that is neither the target nor used
  by a sandbox.

## Uninstall

[INSTALL: Uninstall](INSTALL.md#uninstall).

## License

[MIT](LICENSE).
