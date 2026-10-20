# Standards: planning, hardening, and judging a plan or a review response

Shared by `plan-harden` (author and critic), `plan-implement` and `review-respond`. The decision ladder itself is
`ladder.md` beside this file ("Decide, don't ask"); this file adds what the ladder leaves out. Project rules (CLAUDE.md,
AGENTS.md, linked docs, memory, a "Workflow overlay" memory entry) win where they are more specific.

## Values

Simplicity, consistency, reliability, maintainability. DRY. No unnecessary complexity or useless abstraction; YAGNI.
Prefer one-liners where they stay readable. UX is full-featured and never compromised by simplicity. **All features
must remain or be improved.** Make no assumptions; verify every claim.

## On top of the decision ladder

- Before calling anything a question, grep the project's rule docs: they often document the exception already.
- Choose-by details: reliability means deterministic and failing loud; maintainability means DRY, local, and
  lockable by a lint rule or test.
- A check settles it: typecheck, test, lint selector, scratch experiment, or a mutant that must fail. Scratch work
  lives in the session's scratchpad, never in the repo.
- Record each decision as one Y-statement: "In the context of …, facing …, we decided for … and against … to achieve
  …, accepting …", plus evidence (file:line, doc URL + version, or command output).
- **Ask test** (the critic's ledger, not text for the user): the question's owner-only class, what was checked, the
  recommendation, and what each answer changes. A question failing the test becomes a Decision. Survivors reach the
  user as one-line questions with the recommendation. A recommendation is a decision: write it as one.
- Never weaken a check to make it pass: no deleted or skipped tests, no loosened lint, no `--no-verify`, unless the
  owner approved it under the plan's `## Test changes`. After 2 failed gate loops, write a blocker report instead.

## Evidence bar (the critic enforces it)

- Every number (counts, call sites, line numbers) recomputed with your own command, never copied from a brief, a
  subagent summary or memory. Every file:line opened.
- Every library behavior read in the installed dependency (`node_modules`, `site-packages`, vendored source) or in
  the docs for that exact version.
- Type inference, lint selectors, runtime and browser behavior: settled by an experiment with a mutant, not by
  reading.
- The brief and the user's prompt are hypotheses: each factual premise gets CONFIRMED / REFUTED / UNVERIFIED.
- Refactors carry a **parity inventory**: every behavior before → after (routes, copy, errors, edge cases, UI
  states), with an unchanged-behavior test for each one not already covered.
- Every behavior change, not only a refactor, carries a **Test impact** check: open the touched modules' existing
  tests and list each assertion that pins the old behavior, including extremal or global ones that added output
  breaks (outermost, exact size or viewBox, counts, ordering, snapshots). Each one is rewritten at equal or stronger
  strength, or marked obsolete with why it can't be observed, under `## Test changes`. An obsolete or weakened entry
  is also a ⚑ owner question under `## Unresolved questions`. "None" names the test files read.

## Critic rubric (fresh subagent, one round, every point gets a ledger disposition)

- Premises verified as above; nothing load-bearing rests on a subagent's summary.
- Coverage: every Done-when has a test or command; every Decision has evidence; every brief claim has a verdict;
  every Out-of-scope item names its owner (a later plan, or "dropped because …").
- Simplicity: anything a simpler existing mechanism covers; duplicated responsibility; new abstractions without a
  second user; option menus instead of decisions; any added line that isn't needed.
- Features: nothing the plan or the current product offers is dropped by a simplification.
- Consistency: naming and vocabulary, folder rules, the area's rule docs, memory rules, the workflow overlay.
- Safety: data isolation and authorization, user-facing error copy (never raw errors), destructive-action confirms,
  migrations that are generated and reversible, data-loss SQL flagged, hostile inputs wherever input handling
  changes, plus the overlay's checklist.
- Questions: each passes the ask test or becomes a Decision.

## Docs rule

The code is the source of truth. Docs carry the why, system shape, vocabulary and conventions; they link to code
for concrete detail and never transcribe schemas, lists, signatures or counts. Update every doc statement the
change makes false.
