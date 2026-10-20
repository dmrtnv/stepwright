---
name: review-respond
description: Respond to a code review. Verify every finding first (verdict table with evidence), decide its open questions, apply one fix wave with repro-first tests, then sync docs and memory and, with a plan, the PR text.
disable-model-invocation: true
argument-hint: "[review file] | <plan> <round>"
---

# Respond to a review

**Input** (from the arguments):
- nothing: `review.md` at the root of the current work tree;
- a review file path;
- a plan (or brief) path plus round K: the review is `review-K.md` beside the plan (pipeline form).

**Rules:** `~/.claude/skills/plan-harden/standards.md` and `~/.claude/skills/review-branch/checklist.md`. Project
rules win: CLAUDE.md / AGENTS.md files, linked docs, memory, and the "Workflow overlay" memory entry if the index
lists one.

**Stance:** a review is a set of claims. Agree only with evidence, and thank no one: fix or refute.

## 1. Verify before fixing

Build a verdict table first: finding → CONFIRMED / PARTIAL / REFUTED, with evidence (command output, file:line,
installed library source).
- Check `git show <base>:<path>` before blaming the branch: the issue may predate it.
- Re-verify the mechanism, not only the conclusion. A confirmed finding can still be wrong in its details, including
  its suggested fix.
- Run the relevant suites before trusting any test-risk claim.
- Verdicts are binding: a rejection needs counter-evidence, and nothing is silently dropped.
- Decide every open question in the review by the ladder in `~/.claude/skills/plan-harden/ladder.md`. Only owner-only questions reach
  the user, each with a recommendation.

## 2. One fix wave

- Fix confirmed blocking and should-fix findings. Take a nit only when it simplifies.
- Each fix:
  1. starts from the finding's repro as a failing test;
  2. then the fix;
  3. then a mutant proving the new test non-vacuous (`cp` backup and restore);
  4. then one commit via the commit-message skill in commit mode.
- Improvements and refactors: apply those with zero user-facing change that clearly pay off. In the pipeline, apply
  them only in round 1; later rounds are final and nobody re-reviews them.
- Pre-existing findings become follow-ups, unless the fix is trivial and in touched code.
- A recurring finding class gets a lint rule or test proposal (or a brief) instead of a one-off patch. Apply it only
  in round 1.
- Schema fixes follow the project's migration rules.

## 3. Docs and learnings

Follow `~/.claude/skills/wrap-up/SKILL.md`: fix docs, CLAUDE.md files and rule docs this session made false, and
record durable learnings.

## 4. With a plan (pipeline form, or when a plan was named)

- Append `## Review round K` to the plan:
  - the verdict table;
  - fixes with their commits;
  - ⚑ judgment calls;
  - deferred items → which plan owns them.
- Rewrite `pr.md` beside the plan **every round**, even with zero findings: read `~/.claude/skills/pr-text/SKILL.md`
  first, every round, and follow it with this plan as input. The title goes on line 1. Then run
  `python3 ~/.claude/skills/pr-text/check.py <pr.md>` and fix until it prints nothing; the runner's gate runs it
  too.

Without a plan, end with the verdict table summary, the commits and anything owed.
