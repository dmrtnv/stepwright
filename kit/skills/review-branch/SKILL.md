---
name: review-branch
description: Adversarial, read-only review of a branch, commit or range for bugs, regressions, plan conformance, tests and improvements, with a verdict per finding and a merge-confidence score.
disable-model-invocation: true
argument-hint: "[commit | range | branch] [plan] | <plan> <round> [scoped]"
---

# Review a branch

**Input** (from the arguments):
- nothing: the current branch against `origin/<default branch>`;
- a commit, a range (`a..b`, `base...head`) or a branch, optionally followed by the plan it implements;
- a plan (or brief) path plus a round number K, optionally `scoped`: the pipeline form.

**Output:** `review.md` at the root of the current work tree (`git rev-parse --show-toplevel`). In the pipeline form,
write `review-K.md` beside the plan instead, and set the structured result's `blocking` to the number of blocking
findings.

**Rules:** read-only on code: don't fix, commit or push. Project rules add to `checklist.md` in this skill's
directory: CLAUDE.md / AGENTS.md files, a repo-root `REVIEW.md`, and the "Workflow overlay" memory entry if the index
lists one.

**Stance:** assume the author is wrong until the code proves otherwise. The plan, its `## Shipped` section and the
commit bodies are claims to verify, not facts. Verify in the code and in the installed dependency source, never from
the plan text.

## Steps (full review)

1. **Scope.** Resolve the base (`origin/<default>` when unsure; `git fetch` first) and list the change:
   `git log --oneline <range>` and `git diff --stat <range>`. With a plan, read its premises, Decisions, Done when
   and Out of scope.
2. **Automated pass** (skip it in a `scoped` re-review). Run `/code-review medium <range>`: the level goes first,
   and the range is explicit because worktree branches have no upstream. In an interactive session it runs in the
   background: wait for its findings. Treat them as candidates to verify, not findings.
3. **Read** the diff commit by commit (small diffs are where bugs show), then as a whole.
4. **Lenses**, deeper where the consequences are bigger:
   - behavior regressions against the base;
   - plan conformance, including whether its premises were true;
   - `checklist.md` plus the project rules;
   - tests: vacuous, weakened, deleted, skipped, or not run;
   - performance, migration SQL, new dependencies.

   Changes to behavior or input handling also get hostile inputs and `/security-review`.
5. **Run things** before trusting any test-risk call. Run the touched packages' tests, one suite at a time, and the
   build when types or exports changed.
6. **UI changes:** exercise the changed flows in a browser. Use a scratch end-to-end spec, deleted afterwards, or the
   running app, with screenshots when layout is in question.

## Report

Each finding:
- severity: blocking / should-fix / nit (at most 5 nits, plus a count of the rest);
- `file:line`;
- the failure scenario (inputs or state → wrong result) or the cost;
- the fix, in one or two lines;
- a verdict: CONFIRMED with a repro (blockers need an executable one), or PLAUSIBLE with what would confirm it.

Then two more sections:
- **Improvements and refactors:** naming, structure, patterns not followed, smells, simplifications. Each says the
  concrete benefit.
- **Pre-existing:** issues the branch didn't introduce but touches.

Close with a **confidence score 0–100** that the branch is safe to merge as is. Derive it from the verdicts and from
the checks you could not run (name them), and give the two or three facts that limit it most. If nothing
significant turned up, say so plainly.

## Scoped re-review (`scoped`)

Read `review-(K-1).md` beside the plan and the plan's `## Review round (K-1)` response. Mark each prior finding ADDRESSED or
NOT ADDRESSED with evidence, and review only the fix commits, with no automated pass. Report only blocking and
should-fix findings: no nits, no improvements section.
