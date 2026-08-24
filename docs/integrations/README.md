# Integration dossiers

This directory is the repository's durable branch-handoff record. It prevents a
branch name, a local worktree, or chat history from becoming the only account of
why work exists and whether it is safe to merge.

## Required lifecycle

For every material code change, the owning branch updates its dossier before the
commit that changes its integration facts. Create it from `TEMPLATE.md` when the
branch first gains a material implementation or experiment contract.

A dossier records:

- intent and source/inspiration documents;
- branch, base commit, intended target, and status;
- implemented behavior and contract boundaries;
- tests, review, and evidence receipts;
- blockers, deferred work, and explicit non-claims;
- separate integration and activation/promotion gates; and
- the successor branch or document when work is superseded or rejected.

Update the dossier whenever code changes alter the contract, evidence, blockers,
target, or next step. At a branch decision, mark it `integrated`, `blocked`,
`superseded`, or `rejected`; do not leave an ambiguous active dossier.

## Canonical status

`../INTEGRATION_STATUS.md` is the discoverability index. It links active,
blocked, and recently decided dossiers, but does not duplicate their evidence.
The dossier itself is canonical.

## Scope

Use this for material feature, experiment, refactor, replay, lifecycle, or
contract work. Trivial spelling-only documentation changes do not need a new
dossier unless they change a currently recorded decision or boundary.

A Git merge remains separate from runtime activation. A dossier must distinguish
repository integration readiness from observer/paper cohort eligibility and any
live/promotion readiness.
