---
name: plan-harden
description: Hardens an implementation plan before any code is written. It verifies every premise and claim against code and primary docs, builds a user-facing feature parity inventory, runs a pre-mortem, simplifies the implementation without dropping features, and turns open questions into evidenced decisions checked by a fresh critic. Use before presenting any non-trivial implementation plan (before ExitPlanMode), when a plan is sent back, or when asked to harden, critique, verify or stress-test a plan or brief.
argument-hint: "[plan | brief [plan]]"
---

# Harden a plan

**Input** (from the arguments, if any):
- nothing: the plan in this conversation; in plan mode, the harness plan file;
- a plan path: harden it in place, keeping what survives verification;
- a brief (`brief.md`), optionally with an existing plan: write `plan.md` beside the brief, consolidating the
  existing plan.

**Rules:** read `~/.claude/skills/plan-harden/standards.md` first. Project rules win over this skill's defaults:
CLAUDE.md / AGENTS.md files, the docs they link, and memory. If the memory index lists a "Workflow overlay" entry,
read it now and pass its path to every subagent.

**Stance:** the code changed since the plan was written, and the plan's claims are hypotheses. Behavior (everything
a user sees or does) changes only by explicit decision. Implementation is simplified aggressively.

## Steps

1. **Read** the plan or brief. For a brief, also read the feature's `00-overview.md` one folder up (locked decisions
   are not relitigated; it orders the steps), the earlier steps' `plan.md` (their `## Shipped` sections say what
   exists), the memory entries it touches, and the rule docs of the area. Earlier steps are merged before a later
   one is planned; a prose dependency on other work ("After <branch or feature>") is checked against
   `origin/<default branch>`, and an unmerged prerequisite means stop as blocked.
2. **Neutral questions.** Derive the questions the work depends on, phrased without the plan's conclusions ("How does
   X handle Y today?", not "X is broken because Y").
3. **Brief-blind research.** Group the questions by area and give each group to one subagent that gets only its
   questions, at most three subagents in all: Explore for the codebase, `web-researcher` for outside facts (docs for
   the pinned version, best practices, prior art). Spawn each with `model: "sonnet"`. Then tabulate each premise as
   CONFIRMED / REFUTED / UNVERIFIED with evidence.
4. **Parity inventory.** List every user-facing feature and flow from the plan, the current code, and the reference
   system when the project is a rebuild (the overlay or CLAUDE.md names it). Mark each kept, improved, removed (with
   the plan's reason) or missing. A missing one is restored, or escalated to removed with a justification.
5. **Decide** every engineering question by the ladder and `standards.md`. Run an experiment where reading can't
   settle it. One-way doors (schema, stored data, public API or URL, auth, dependency, cost) compare two designs and
   get a ⚑.
6. **Pre-mortem.** Assume the change shipped and failed: write the five likeliest causes, each with a mitigation or
   a test in the plan.
7. **Simplify the implementation**, never the behavior:
   - search first: reuse or extend an existing helper, component or pattern before writing a new one;
   - DRY and YAGNI: no speculative abstractions, no single-use wrappers, a second user before a new abstraction;
   - the shortest readable solution; probe every added line for necessity;
   - annotate each simplification "zero user-facing change", or move it to Decisions as a behavior change;
   - never drop a feature or option the plan already offered.
8. **Write the plan** (format below). Transfer context and decisions, not file-by-file scripts.
9. **Verify it yourself.** Recompute every number and open every file:line and library claim with your own
   commands before the critic sees it.
10. **Critic.** Spawn a fresh Plan subagent with the plan path, the brief, `standards.md` and the overlay: "find what
    is wrong, unverified, missing, over-engineered or dropped; evidence + fix per finding". Record every point in a
    ledger (accepted, or rejected with counter-evidence) and fix. One round: verify a changed decision yourself
    rather than running the critic again.

## Plan format

- **Digest** (≤ 2 screens, what the user approves):
  - goal; scope in and out;
  - user-visible behavior changes and the parity inventory;
  - ⚑ decisions with rejected alternatives;
  - files touched and key signatures; slices, each with its check;
  - pre-mortem, risks and UNVERIFIED items; critic ledger summary;
  - one-line owner-only questions.
- **Decisions:** Y-statements with evidence.
- **Out of scope:** who owns each item (a later plan, or "dropped because …").
- **Steps:** one commit each. Schema changes list their declared renames.
- **Tests:** red on the base first; unchanged-behavior tests for refactors.
- **Test changes** (`## Test changes`, top level): exact paths, never a pattern (a moved test as old path → new path),
  each rewritten or obsolete with the reason; or None naming the test files read.
- **Traps**, then **Done when** (commands).
- **Unresolved questions:** owner-only one-liners with a recommendation, or "None".

## Pipeline contract

When the input is a step brief in a feature folder (`.claude/plans/<feature>/NN-slug/`, or `~/plans/<feature>/NN-slug/` in a
pipeline sandbox):
- write only that folder's `plan.md`, never `00-overview.md` or another step's files; the runner owns the branch
  and the sandbox, so the plan carries no `Branch:` line;
- keep the `## Unresolved questions` heading, where the user answers; only owner-only questions go there, and the
  same one-liners go in the result's `questions` so the runner waits for the answers before implementing;
- carry the brief's `## Open questions` over: each one the brief left open is either answered by the brief or the
  overview's locked decisions (say where), decided with evidence, or listed under `## Unresolved questions` with its
  recommendation; none is dropped silently;
- under `Autonomy: auto` (the stage block) each recommendation becomes the decision unasked: write it implementable
  as worded;
- on a `## Feedback` section, revise the plan to it, critic round included.

Finish with the digest and the unresolved questions (or "None"). In plan mode, then call ExitPlanMode.
