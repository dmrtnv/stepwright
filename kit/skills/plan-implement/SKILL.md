---
name: plan-implement
description: Implement an approved plan step by step with red tests first, one green commit per step, mutation-checked tests, an optional pre-review subagent, full gates and a Shipped section.
disable-model-invocation: true
argument-hint: "[plan]"
---

# Implement a plan

**Input:** the plan path from the arguments, or else the approved plan in this conversation. Work on the plan's
branch (a `Branch:` line, or in the pipeline the workspace's checked-out branch). On the default branch, create the
branch first.

**Rules:** engineering questions are decided by the ladder in `~/.claude/skills/plan-harden/ladder.md` plus
`~/.claude/skills/plan-harden/standards.md`, and recorded. Project rules win: CLAUDE.md / AGENTS.md files, their
linked docs, memory, and the memory entry "Workflow overlay" if the index lists one. Those give the gate commands,
migration recipe, e2e setup and database rules.

## Per step (one logical commit each)

1. Write or change the step's tests first and run them: they must fail for the right reason. Record the output.
2. Implement it, following the plan's Decisions. A deviation is decided by the ladder and recorded. If the plan
   contradicts reality in a way only the user can settle, stop as blocked with one-line questions.
3. Run the step's targeted tests plus lint and types for the touched packages. Run one suite at a time, and before
   a test run, wait until no other run is active (a pipeline sandbox runs nothing else).
   Stop only processes you started, by exact pattern, never a broad `pkill -f`.
4. Prove each new test non-vacuous: `cp` its subject to the scratchpad, break it, confirm the test fails, restore
   from the copy (never `git checkout --`, which also drops uncommitted work). Rebuild build output when the test
   reads it.
5. Commit with the commit-message skill in commit mode. Every commit is green and carries no attribution trailer.

Never delete, skip or weaken a test or assertion unless the approved plan's `## Test changes` names it with the
owner's approval. Never `git stash`: commit a WIP and fold it in later with
`GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash`.

## Data, schema and dependencies

- Schema changes and migrations follow the project's migration docs and overlay: generated, never hand-edited once
  on the default branch, with declared renames. Data-loss SQL gets quoted and ⚑.
- Touch only databases the project rules say are yours. Fixes to other databases are not migrations: prove the SQL
  on your own seeded copy and list it under Owed.
- A new dependency stops as blocked (or is asked in an interactive session) with the exact install command and the
  evidence: release age, repository, maintainers, downloads, install scripts, and why nothing in the repo fits. A
  frozen install is fine.

## Before finishing

1. **Pre-review.** In the pipeline (stage rules from `~/.claude/pipeline/system.md`), skip it: the review stage
   follows. Otherwise spawn one fresh subagent with the plan, `git diff origin/<default>...HEAD`,
   `~/.claude/skills/review-branch/checklist.md` and the overlay. Ask it to check the diff against the plan's Done
   when and the checklist, reporting findings with file:line. Fix what holds, with commits.
2. **Gates.** Run the project's full gates once, one after another, and report pass, fail and skip counts. A
   skipped test is not a pass.
3. **Claim check.** Before saying done, write each claim with the evidence that proves it: a command and its
   output, or a file:line. An agent's report or a diff alone is not evidence that something works.
4. **Shipped.** Append `## Shipped` to the plan:
   - commits (hash + subject);
   - deviations and why;
   - deferred items → which plan owns them;
   - owed: steps only the user can run, such as migrations on their database after merge, data SQL, or e2e runs
     that couldn't run here.
5. **Wrap-up.** Follow `~/.claude/skills/wrap-up/SKILL.md` for this session.
