# Review checklist (generic)

Used by `review-branch` and by `plan-implement`'s pre-review. The project's rules add to it: CLAUDE.md / AGENTS.md
files, a repo-root `REVIEW.md`, and the "Workflow overlay" memory entry (the project checklist).

## What to look for

- **Design first.** Does the change belong here, fit the system, and solve today's problem rather than a speculated
  one? Is it more complex than it needs to be?
- **Functionality.** Does it do what the author intended, for users as well as callers? Think through edge cases,
  concurrency, empty and huge inputs, failure paths, retries and partial writes.
- **Regressions.** Every behavior the base had (routes, copy, errors, UI states, edge cases) still holds, unless a
  decision says otherwise. Compare with `git show <base>:<path>` before blaming the branch.
- **Tests.** They come with the change, and they would fail if the code broke. Look for vacuous assertions, weakened
  or deleted tests, new skips, and suites that didn't run (read the skip counts).
- **Naming.** Names say what the thing is in the project's vocabulary, spelled out, with no abbreviations the project
  doesn't use.
- **Structure and patterns.** Consistency with existing code and the area's rule docs. Look for duplication a helper
  already covers, single-use wrappers, dead code, and code smells (long parameter lists, flag arguments, feature
  envy, shotgun surgery).
- **Comments** explain why, not what. **Docs** that the change makes false get updated.
- **Safety.** Authorization and data isolation (children constrained by their parent), secrets, injection, raw
  errors reaching users, destructive actions without a confirm, hostile inputs (size, depth, encoding, numeric
  bounds).
- **Data.** Migrations are generated, not hand-edited once merged, and reversible. Data-loss SQL (drop, lossy type
  change, a rename emitted as drop + create) gets quoted.
- **Dependencies.** Any new one is necessary, maintained, not brand new, and has no install scripts it doesn't need.
- **Performance.** Look for N+1 queries, unbounded loops or loads, missing pagination, work on the hot path, and
  leaks (listeners, connections, timers).

## Precision rules

- Report a finding only with a concrete failure scenario (inputs or state → wrong result) or a concrete cost.
  Behavior claims need a file:line in the source, not an inference from a name.
- Verify before reporting: run it, write a repro, or trace it to the line. Unverified suspicions are labeled
  PLAUSIBLE with what would confirm them.
- Skip what linters and type checkers already catch.
- Label issues the branch didn't introduce as pre-existing, in their own section.
- Report at most five nits, and give the rest as a count. Pedantry is not a finding.
- "Nothing significant found" is a valid result. Don't invent issues to fill a report.
