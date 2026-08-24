# Integration dossier template

`TEMPLATE.md` is the repository-owned starting point for a **temporary dossier
on an owning feature or experiment branch**. It is not a beta/main status board.

## Lifecycle

1. When material code work begins, create `docs/integrations/<topic>.md` in the
   owning branch from `TEMPLATE.md`.
2. Before every material code commit, update the dossier when intent, contract,
   evidence, blockers, target, or successor work changed.
3. During review, compare its statements with the candidate diff and receipts;
   stale statements or vague blockers fail review.
4. A blocker that defers verification must name the exact missing test/evidence,
   command or fixture where known, and the trigger for running it.
5. For a blocked or incomplete branch, retain its dossier in that branch so a
   later agent can read the full handoff.
6. For an accepted merge, record the final decision in the feature branch, then
   remove its dossier from beta/main in the merge cleanup. The closed record
   remains discoverable in Git history without cluttering the target checkout.
7. Delete the dossier only with a merged, rejected, or superseded branch.

A Git merge remains separate from runtime activation. The dossier must
separately state repository integration readiness, observer/paper cohort
gates, and promotion/live boundaries.
