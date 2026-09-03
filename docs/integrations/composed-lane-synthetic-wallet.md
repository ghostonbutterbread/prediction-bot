# Composed-lane synthetic-wallet integration dossier

- **Status:** feature
- **Owner:** Hermes
- **Branch:** `feat/composed-lane-synthetic-wallet`
- **Base commit:** `4ca50c30c39707fd13e4b657d6299667cd761580`
- **Intended integration target:** `main` (per repository guidance; no merge or activation is authorized by this task)
- **Last updated:** 2026-09-02
- **Inspiration / canonical references:** `docs/architecture/collector-first-replay-lane-contract.md`; `scripts/paper_shadow_lane_compose_replay.py`; user request in Discord thread `1544788048519110787`.

## Intent

Add the smallest paper-only derived replay path that can consume an existing composed shadow-lane decision stream (including Source Router when selected), preserve its selected action/price provenance, and evaluate it with the shared-core decision/Kelly/risk seam against an in-memory synthetic account. Existing fixed-notional composition reports remain unchanged controls.

No live order path, runtime lane activation, real wallet access/mutation, or raw-ledger modification is in scope.

## Implemented contract

The first isolated vertical slice now provides an outcome-free, in-memory evaluator over **new sealed composed intents**. It projects action, side-selected decision-time price, YES probability, confidence, and allowlisted provenance into `TradeContext`; it recomputes approval/size through shared core; it reserves synthetic capital; and it accepts only an exact `(decision_id, shared_candidate_id, run_id, market_id)` later authoritative receipt. Nested outcome-like fields are rejected recursively; conflicting receipts remain unresolved; and `VOID` releases capital while carrying no economic stake/P&L attribution.

It remains a synthetic fixture tracer, not archive-backed parity. It does not write files, invoke runtime code, touch a wallet, mutate raw ledgers, or activate a lane. Results are labelled non-mutating, paper-only, not paper-parity/promotion evidence.

## Evidence and review

- Tests and commands: `PYTHONPATH=. python3 -m unittest tests.test_composed_lane_wallet -v` — 4 passed after review-driven RED→GREEN fixes; broader focused composition suite remains to run after adapter work.
- Independent reviews: `deleg_b2d8dafe` established that the current composer is fixed-notional only; `deleg_01870599` identified shared-core/replay binding seams; `deleg_c55b10cd` found nested-outcome leakage, ambiguous resolution handling, VOID attribution, and stale dossier defects. The first three are fixed in this slice; actual-composition adaptation remains blocked below.
- Replay/cohort/fixture evidence: synthetic fixtures only; no historical cohort replay executed.
- Merge/ancestry evidence: branch created from local `main` at `4ca50c3`.

## Blockers and deferred work

- **Missing test or evidence:** current fixed-notional composition rows omit the sealed wallet intent fields required here (probability/confidence, complete question/exchange/route context, immutable input/config identity). A versioned opt-in adapter/projection must be designed and tested; it must support a generic stable/shadow route as well as Source Router enrichment, without making Source Router mandatory.
- **Command / fixture / environment needed:** adapter integration fixture from composition output → sealed intent → strict binding receipt; then a bounded, common-universe archive replay.
- **Trigger to run it:** after the adapter emits complete, hash-addressed decision-time fields and the exact binding ledger is available.
- **Why it blocks integration, activation, or promotion:** current rows cannot honestly produce a real-composition parity claim; neither synthetic fixture success nor historical replay is promotion evidence.
- **Exact resume point:** write the next RED test for an explicit `compose_row_to_sealed_intent` adapter that accepts both a non-Source-Router shadow composition and an intact Source Router composition, fails closed on missing decision-time fields, and never derives missing probability/context.

## Decision gates

- **Integration gate:** focused and full tests pass; independent review findings resolved; clean branch diff.
- **Activation / cohort gate:** explicitly out of scope; code merge does not enable a lane.
- **Promotion gate:** fresh full-overlap independently resolved paper evidence after a frozen holdout; no historical replay alone is sufficient.

## Decision record

- 2026-09-02 — created after user authorized paper-only composed-lane synthetic-wallet implementation.
