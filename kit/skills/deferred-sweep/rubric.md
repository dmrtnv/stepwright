# Deferred item verdicts

How each open deferred item of a finished feature is judged. Run no git: the runner gathers the evidence on its own
clone (`pipeline deferred --json --evidence`, each item's `evidence_check`).

- **done:** the work happened on the base. The note cites `<short sha> <subject>`. A handoff (owner `"NN"`) is
  done when step NN's merged change did it.
- **moot:** the evidence no longer applies (the file or the literal is gone, and the work no longer makes sense).
  The note cites the evidence.
- **won't do:** the item is one of the kinds a stage should never record: a decision or taste call; a conditional
  idea with no measured signal; a nit or a few-line fix in touched code; work the overview's step table already
  gives a later step; a step only the user can take; a review stage's finding. Name the kind. It needs the owner's
  confirmation.
- **still true:** the evidence still shows the gap, or a handoff step NN did not do. It is filed.
