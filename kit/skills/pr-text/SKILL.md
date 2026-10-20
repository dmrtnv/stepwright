---
name: pr-text
description: Write the PR title and plain-text description (the squash-merge commit body) for a branch or a plan's branch, in the repository's own squash-body style. Never pushes or opens the PR.
disable-model-invocation: true
argument-hint: "[plan] [range]"
---

# PR title and description

**Input** (from the arguments or the calling skill):
- a plan path, optionally: its Digest, `## Shipped` and `## Review round K` sections are sources;
- a range, optionally. The default is `origin/<default branch>...HEAD`; `git fetch` first, since a stale local
  `main` adds merged commits.

**Sources:**
- `git log --format='%s%n%n%b' <range>` (bodies, not only subjects);
- `git diff --stat <range>`;
- the plan;
- the last five squash bodies on the default branch for this repo's style:
  `git log --first-parent -5 --format='%s%n%n%b' origin/<default>`.

The repo's style wins over this skill's defaults.

## Format (plain text: no Markdown, no emoji, no attribution)

The body is the squash commit body: never paste file contents, command output, checklists or test counts.

- **Title:**
  - Conventional Commits with no scope: `type: Description`;
  - capitalized imperative, naming the user-visible outcome;
  - `!` after the type for breaking changes;
  - aim for 72 characters including the " (#NNN)" GitHub appends, but never at the cost of clarity.
- **Body**, wrapped at 72 columns:
  1. 1–3 sentences of motivation: the problem, and why now.
  2. A short list of changes as "- " lines. Put user-visible behavior first, grouped by area. Name a file or symbol
     only when a reviewer must look at it. Wrapped lines indent two spaces.
  3. Closing blocks, only when they apply:
     - breaking changes;
     - behavior changes callers or users will notice even when nothing breaks (API shapes, status codes, export
       formats, ordering). Repos often combine this with the migrations line ("Behavior changes, no
       migrations:");
     - new, renamed or removed env vars and config;
     - data-loss risk, quoting the SQL;
     - steps owed after merge (migrations to run, data SQL, flags to set);
     - rule exceptions: each repo rule broken and why; each rule newly baselined, with its site count;
     - follow-ups and who owns them, under "Follow-ups:" only: deferred and out-of-scope items go there, never in
       an ad-hoc "Not in this PR:" block.
  4. A migrations line, always: say what the migration does, or "No migrations" / "No schema or data migration".

## Output

Print the title and the body as two separate fenced code blocks, ready to paste. When a plan was given, also write
`pr.md` beside the plan: the title on line 1, a blank line, then the body.

Run `python3 ~/.claude/skills/pr-text/check.py <pr.md>` (or on a file holding the title, a blank line and the body)
and fix until it prints nothing. check.py locks this format: the pipeline's respond gate runs it, and every rule it
checks has a test in `test_check.py`.
