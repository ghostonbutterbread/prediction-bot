# Frozen-wallet integration dossier

- **Status:** blocked
- **Owner:** Prediction Lab replay diagnostics
- **Branch:** `feature/collector-lane-replay-kelly`
- **Base commit:** `0a84869`
- **Intended integration target:** `beta`, only after strict-contract alignment
- **Last updated:** 2026-08-23
- **Inspiration / canonical references:** [source-router forward evidence and wallet plan](../architecture/source-router-forward-evidence-wallet-plan.md); [strict source evidence dossier](strict-source-evidence.md)

## Intent

Provide deterministic, recorded-decision Kelly/capacity diagnostics: sequential
ordering, duplicate-decision handling, retrade context, and fail-closed
conflicting settlement receipts. It is not a shared-core wallet-parity or
promotion result.

## Implemented contract

The branch makes chronological ordering deterministic, rejects duplicate decision
IDs, records market re-entry context, and rejects conflicting resolution rows.
It remains a recorded-decision capacity diagnostic with
`paper_parity_claim: false` and `promotion_eligible: false`.

## Evidence and review

- Focused wallet tests: 9 passed.
- Independent review ran the wallet/collector/replay set: 99 passed.
- A merge-tree check against strict-source/up-to-date beta had no textual
  conflicts.

## Blockers and deferred work

**Do not merge unchanged.** The wallet accepts flat or insufficiently proven
resolution rows and does not require the strict chain's complete sealed decision
identity, canonical input/resolution hashes, or verified authoritative
provenance. A clean textual merge would not satisfy the contract.

Next smallest completion slice: consume strict-source artifacts through a
validated adapter that rejects flat/unproven receipts, requires the strict
identity/provenance contract, and proves shared-core risk/parity behavior against
the same frozen intent stream.

## Decision gates

- **Integration gate:** strict-artifact-backed input contract and focused
  strict+wallet integration tests; independent review must confirm exact
  settlement/provenance alignment.
- **Activation / cohort gate:** not eligible while recorded-decision diagnostic
  semantics remain.
- **Promotion gate:** shared-core parity harness plus fresh independently
  resolved full-overlap evidence.

## Decision record

- 2026-08-23 — reviewed after strict-source beta integration.
- 2026-08-23 — withheld from beta despite a clean merge tree: semantic
  settlement/provenance mismatch.
