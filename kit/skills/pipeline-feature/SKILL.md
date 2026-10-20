---
name: pipeline-feature
description: Create a feature folder for the unattended pipeline (the `pipeline` CLI, stepwright's `core.py`): interview the user, split the work into numbered steps of one PR each, warn about overlap with in-flight features, write 00-overview.md and every step's brief.md in the shape the plan stage expects, and print the run command. Use when the user wants to start pipeline work, plan a multi-PR feature, "create a feature", add steps to one, or asks how to run the pipeline.
argument-hint: "<feature-name> [what it is about]"
---

# Create a pipeline feature

**Output:** `<repo>/.claude/plans/<feature>/` holding `00-overview.md` and one `NN-slug/brief.md` per step. Never
start the pipeline yourself: print the command and stop. The runbook is the `README.md` beside `core.py` (`readlink -f "$(command -v pipeline)"`).

**Rules:** project rules win (CLAUDE.md / AGENTS.md files, their linked docs, memory, and the "Workflow overlay"
memory entry if the index lists one). Plans stay out of the project's git (the folder is ignored on purpose; back it up yourself). A feature not to start yet goes to `.claude/plans/_backlog/<feature>/` and gets a row in
`.claude/plans/README.md` (the queue).

## Steps

1. **Name.** Lowercase letters, digits and dashes (`unique-names`, `perfect-fit-queue`), never a `pipeline` command
   name (`pipeline help` lists them: `status`, `show`, `log`, `start`, `queue`, …; such a folder is skipped). An
   existing folder is only extended (new steps get the next numbers); it is never rewritten while a step is running.
2. **Interview**, short and decisive: the goal and the user-visible outcome; what is out of scope; the decisions
   only the owner can take (product rules, money, schema or stored-data one-way doors, public URLs or APIs, new
   dependencies). Everything else the plan stage decides, so don't ask it.
3. **Explore the repo** enough to split the work into steps that each merge on their own and leave `main` working:
   data and backend first, the user-facing entry point last (a step can ship a feature dark). Aim for ≤ ~400
   changed lines per step, excluding tests; say when a step adds a migration. A one-PR task is a feature with one
   step. Steps run strictly in order, one after the previous one has merged.
4. **Check in-flight features.** Features run in parallel, so read the `00-overview.md` of every other folder in
   `.claude/plans/` that `pipeline status` shows as unfinished, and compare the areas they touch (packages, files,
   routes, tables) with this feature's steps; also read the queue in `.claude/plans/README.md` for parked
   `_backlog/` features. Two features adding migrations always overlap. On an overlap, tell
   the user what collides and recommend one: run in parallel (a later merge rebases, conflicts are resolved by the
   pipeline) or sequence (`pipeline start <feature> --after <other>[:NN]`: it starts on its own once the other's
   last PR, or its step NN, merges). Record the choice in the overview's locked decisions.
5. **Write `00-overview.md`:** goal and why now; locked decisions (never reopened by a step); the step table
   `| # | folder | goal | done when |`; what is out and who owns it; references (files, docs, prior art).
6. **Write each `NN-slug/brief.md`** (template below). Transfer context and decisions, not a script: the plan stage
   explores the code, resolves engineering questions and writes the detailed plan itself.
   Briefs and overviews never dictate pr.md content: no "paste", no "verbatim in pr.md", no "the PR attaches" or
   "the PR names". A fact the reviewer needs is an Owed line or a flag. pr.md follows the pr-text skill, and the
   runner checks it.
7. **Link to Linear**, only when the main checkout's `.claude/plans/README.md` has a `## Linear` section and the
   `mcp__linear-write__*` tools are loaded; otherwise skip it and say why in the finish. The `/linear` skill
   (`~/.claude/skills/linear/SKILL.md`) has the routing, templates and read/write split.
   - **Home:** an existing outcome project (`mcp__linear__list_projects`), an issue in a standing project, or a new
     outcome project (Feature template, Backlog, draft shown first). Ask once, only if it's unclear. Several features
     can share a home.
   - **Decisions in:** the home's Decisions that the overview lacks go into its locked decisions (a new folder only).
   - **Write** (1–2 prompted `save_project` / `save_issue` calls, `patch` on an existing description): append the slug
     to the home's `Plans:` line (add the line if missing), and the interview's locked decisions to its Decisions, in
     plain words with no paths or code.
   - Never rename a folder, and never edit the overview of a feature with a step running (extending one: only the
     Linear write).
8. **Autonomy.** Ask once: `ask` (default: the pipeline waits for the user's answers to a plan's questions) or
   `auto` (each plan question is decided by the plan's own recommendation, a last review round's blocking findings
   go to respond, every such decision is a ⚑ `auto-decided` in the result's `flags`, which the runner adds to the
   PR comment). Recommend `auto` only when the interview
   left no owner question and no one-way door open (schema, stored data, public URL/API, auth, new dependency,
   cost); otherwise `ask`. Blocked or failed stages stop for the user either way.
9. **Finish** by printing the Linear home's URL (or why the link was skipped) and, to run from the repo root:
   `pipeline autonomy <feature> auto` when chosen; the start (`pipeline <feature> --detach`, or tick within 5 min;
   sequenced: `pipeline start <feature> --after <other>`; parked: it stays in `_backlog/` until
   `pipeline start <feature>`); `pipeline status` (`--watch` live,
   `pipeline show <feature>` in full); and the moments the user is needed: a plan's questions (`pipeline answer
   <feature>`: edit `plan.md`, a saved change approves), reviewing and merging each PR (the next step starts on its
   own), and PR comments (`pipeline respond <feature>`). Hold: `pipeline pause <feature>`; kill switch:
   `pipeline stop --all`.

## Brief template

```markdown
# NN <slug>

**Goal:** one sentence, the user-visible outcome.
**Why:** the problem, and why now.

## Scope
In: … Out: … (name the step that owns each excluded part).

## Locked decisions
- … (from the interview and the overview; the plan stage does not reopen them)

## Pointers
- files, precedents, reference docs, memory entries

## Traps
- verified facts that bite (or "none known")

## Done when
- outcomes and the commands that prove them

## Open questions
- owner-only questions with a recommendation, or "None" (the plan stage decides the rest)
```
