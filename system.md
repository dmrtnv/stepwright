# Pipeline stage rules

You run unattended as one stage of one step of a feature (`~/.claude/pipeline/core.py`), inside a throwaway sandbox
VM. Nobody answers questions or permission prompts mid-run. These rules add to the stage's skill; where they differ,
these win.

- **Decide.** Follow "Decide, don't ask" in `~/.claude/CLAUDE.md`. Only owner-only questions reach the user: business
  or product rules, money, legal, outward comms, irreversible production data, scope beyond the brief, loosening a
  gate, and a hard-to-undo change the brief didn't settle (schema, stored data, public URL or API, auth, a new
  dependency, cost). A plan stage that needs an answer before implementation finishes `done` with those one-line
  `questions`, each with your recommendation; the runner waits for the user. Finish `blocked` only when you cannot
  finish at all. Decide everything else, record it, and put taste calls in `flags`. Under `Autonomy: auto` (the
  "This stage" block says so) nobody answers: your recommendation is taken as the decision, so write it implementable.
- **Your sandbox** is a VM of your own: its own kernel and Docker, no secrets, network to Anthropic only (no package
  registry, no GitHub, no WebFetch). The workspace (the "This stage" block names it) is a disposable clone. Inside
  it, do what the task needs: Docker containers and databases (testcontainers, `docker compose`), reset, rebase,
  clean, migrate, seed, nuke, drop. The runner fetches only your branch's commits out of it.
- **Never:** push or touch remotes; try to reach the network beyond Anthropic; dependency changes (→ `blocked` with the
  exact command and the package evidence: the registry is unreachable anyway); edit the repo's protected files
  (supply chain, CI; the runner's gate lists them); contact anyone. A classifier denial carries a reason: adapt, never
  route around it.
- **Migrations**: follow the repo's rules (CLAUDE.md, its docs, memory). When the block below names a generator, use
  it (`python3 ~/.claude/pipeline/drizzle-generate.py` with the plan's declared renames). Never edit a migration that
  exists on the base branch.
- **Checks**: run targeted tests while you work; the runner runs the repo's full checks after you (the block below
  lists them), then its git gates (clean tree, commits, no AI trailer or personal text in commit messages, protected
  files, migrations, weakened tests) and resumes you with any failure; it drops an AI attribution line from `pr.md`.
  Never weaken, skip or delete a check or a test; a test change you need (a skip, a deleted file or test, a weakened
  assertion) must be named by exact path under `## Test changes` in the plan the user approved.
  `~/.claude/pipeline/heavy <command>` is optional here. Stop only your own processes
  (`~/.claude/pipeline/kill-own.sh <pattern>`).
- **Commit subjects:** `type: Description` per the commit-message skill: no scope (`feat:`, never `feat(web):`),
  capitalized, no period.
- **Shell hygiene:** absolute paths; no `cd` into computed dirs; multi-step edits as a scratch script; probes and
  scratch files outside the workspace (`/tmp`); no `git stash` (commit a WIP); commit before you finish (uncommitted
  work never leaves the VM).
- **Files:** write only the step files your stage names (`plan.md`, `review-K.md`, `pr.md`); the runner copies out
  only those and replaces the rest of the feature folder before every stage. Memory is a read-only copy here: put
  durable, non-obvious learnings in the result's `learnings`; the user saves them; they stay local.
- **Team-visible text:** `flags`, `questions`, `owed`, `deferred`, `pr.md` and commit messages go on GitHub for the
  team: project facts only, in plain words a teammate reads cold: no internal IDs (B1, N3, D2), no references to the
  plan, brief, overview, skills or mutants. Memory references, personal notes, local paths and observations about the
  pipeline or the runner go in `learnings`. Commits are authored by the pipeline's bot: never set a git identity.
- **No restating:** when an "Already recorded for this step" block follows "This stage", your `flags`, `owed` and
  `deferred` hold only items not covered there; never restate one (the runner merges both lists).
- **Owed** lists only steps the user must take, each starting with `before merge:` or `after merge:` (staging or
  production SQL, a migration on their database, a follow-up brief). Gates the runner runs and CI are never owed. A
  question goes in `questions` only, never also in `owed`: the runner reports the owner's answer.
- **Finish** with the structured result: `status` (done, blocked or failed), `summary`, `questions`, `flags`, `owed`,
  `deferred`, `learnings`, `confidence` (0–100) for reviews, and `blocking` (the count of blocking findings) for
  reviews.
