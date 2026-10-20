---
name: commit-message
description: Generate a Conventional Commits-compliant commit message for the currently staged changes. Use whenever the user asks to "commit", "write a commit message", or finalize staged git work — even without saying "conventional".
---

# Conventional Commit Generator

## Workflow

1. Run `git diff --cached --stat`. If nothing is staged: in commit mode, report "nothing staged" in one line and
   continue the task; in message mode, tell the user and stop.
2. Read `git diff --cached` (chunk if large).
3. Pick the mode:
   - **Commit mode** — the user already told you to commit (a task or plan that commits as it goes, or "commit it"). Run the commit yourself, then keep working. See **Commit mode** below.
   - **Message mode** — anything else. Output the message only, and do **not** run the commit. See **Output (message mode)** below.

## Format

```
<type>: <Description>

<body>

<footer>
```

**Types:** `feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`

**No scope**, even in a monorepo: `feat: …`, never `feat(web): …`.

**Description:** imperative, **capitalized first letter**, no period, ~72 chars, concrete.

**Breaking changes:** append `!` after type and add `BREAKING CHANGE: ...` footer.

**Body/footers:** optional. Add body only when the *why* isn't obvious. Blank line before body/footers.

## Rules

- One logical change per commit. If the diff mixes unrelated changes: in commit mode, split it into separate commits
  yourself (unstage, then stage each change by path or `git add -p`), then continue; in message mode, stop and tell the
  user to split with `git add -p` — don't produce a message.
- Be specific — no "Update stuff", "Misc changes".
- Always flag breaking changes (removed exports, changed signatures, altered API/DB shape).
- Never mention Claude co-authorship

## Commit mode

Commit with the message on stdin, with no trailers:

```
git commit -F - <<'EOF'
<message>
EOF
```

If a hook rejects it, fix the cause and commit again. Report the commit in one line (short hash + subject), then continue the task in the same turn. Never push.

## Output (message mode)

Output **only** a single fenced code block containing the commit message itself — no `git commit`, no language tag, no preamble, no explanation, no trailing text. The user pastes it into the VSCode commit UI.

Single-line:

````
```
feat: Add password reset endpoint
```
````

Multi-line:

````
```
fix: Return 409 instead of 500 on duplicate email

Catch the unique constraint violation and map it to a typed
ConflictError instead of letting it bubble up.
```
````

That's the entire response. Nothing before, nothing after.
