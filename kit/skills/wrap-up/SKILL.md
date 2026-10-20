---
name: wrap-up
description: End-of-session sync. Update the docs, CLAUDE.md files, plan and memory that this session's changes, findings and decisions made stale, and propose locks for recurring mistakes.
disable-model-invocation: true
---

# Wrap up a session

Run at the end of a working session, or when another skill says so. Input: this conversation, plus
`git diff` / `git log` of the work it produced.

## Steps

1. **Collect** what this session changed and decided: commits, behavior changes, decisions with their reasons,
   findings (including refuted ones), traps hit, and anything deferred.
2. **Docs that are now false.** Grep the repo's docs, CLAUDE.md / AGENTS.md files and rule docs for statements this
   session made wrong, and fix them.
   - Code is the source of truth: docs keep the why, system shape, vocabulary and conventions, and link to code for
     detail. Never transcribe schemas, lists, signatures or counts.
   - If the project has a docs link or lint check, run it.
3. **The plan.** If the work follows a plan file, update it:
   - `## Shipped` or `## Review round K` as the calling skill says;
   - deviations and why;
   - deferred items → owner.
4. **Memory.** Save only durable, non-obvious learnings a future session couldn't get from the code or git history:
   a user preference, a trap, a decision's why.
   - Update an existing entry before creating a new one, and delete entries that proved wrong.
   - In an unattended pipeline stage, memory is read-only: return the learnings in the result instead.
5. **Recurring mistakes.** When the session found a class of error that could recur, propose a lint rule, test or
   type that makes it impossible, instead of relying on memory.
6. **Report** in a few lines: files updated, memory entries touched, and proposals not applied.
