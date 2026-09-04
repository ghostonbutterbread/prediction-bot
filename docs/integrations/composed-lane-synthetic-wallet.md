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

The composition runner now emits a separate `wallet_intents.jsonl` derived artifact when—and only when—the selected lane components provide complete, outcome-free decision-time data and exact matching `shared_snapshot_id`, candidate, market, and run identity. The sealed intent preserves the declared action/price/sizing lane ownership, so `shadow_source_router` remains optional: it can supply the action and price, operate as a veto, or be absent entirely. The selected intent can then pass through the synthetic wallet evaluator; existing fixed-notional composition outputs remain unchanged controls.

It remains a synthetic fixture tracer, not archive-backed parity. It does not write files outside the selected derived output directory, invoke runtime code, touch a wallet, mutate raw ledgers, or activate a lane. Results are labelled non-mutating, paper-only, not paper-parity/promotion evidence.

## Evidence and review

- Tests and commands: `PYTHONPATH="$PWD" python3 -m unittest tests.test_composed_lane_wallet tests.test_paper_shadow_lane_compose_replay -v` — 22 passed; isolated full suite `PYTHONPATH="$PWD" python3 -m unittest discover -s tests` — 1,055 passed, 7 skipped.
- Independent reviews: `deleg_b2d8dafe` established that the current composer is fixed-notional only; `deleg_01870599` identified shared-core/replay binding seams; `deleg_c55b10cd` found nested-outcome leakage, ambiguous resolution handling, VOID attribution, and stale dossier defects. The first three are fixed in this slice. The adapter design review (`deleg_184d2367`) required fail-closed recorded exchange/route identity; the adapter refuses missing recorded exchange rather than defaulting it, and includes every selected component in identity/outcome checks. Final review `deleg_ddddebe8` found a missing configured-price owner could be misrepresented; it is fixed and covered by a fail-closed regression test.
- Replay/cohort/fixture evidence: synthetic fixtures only; no historical cohort replay executed.
- Merge/ancestry evidence: branch created from local `main` at `4ca50c3`; first checkpoint is `843a942`.

## Blockers and deferred work

- **Missing test or evidence:** real historical composition rows may omit one or more required sealed decision-time fields; such rows now remain fixed-notional-only and are excluded from `wallet_intents.jsonl`. A bounded common-universe archive fixture is still required before any historical live-shaped claim.
- **Command / fixture / environment needed:** adapter integration fixture from real lane rows → sealed intent → strict binding receipt; then a bounded, common-universe archive replay.
- **Trigger to run it:** after a selected real composition yields a complete sealed-intent artifact and an exact authoritative binding ledger.
- **Why it blocks integration, activation, or promotion:** synthetic fixture success and historical replay do not prove forward parity or promotion readiness.
- **Exact resume point:** run a bounded derived composition; inventory emitted versus omitted wallet intents by reason; bind only exact later authoritative receipts and compare fixed-notional versus synthetic-wallet results.

## Decision gates

- **Integration gate:** focused and full tests pass; independent review findings resolved; clean branch diff.
- **Activation / cohort gate:** explicitly out of scope; code merge does not enable a lane.
- **Promotion gate:** fresh full-overlap independently resolved paper evidence after a frozen holdout; no historical replay alone is sufficient.

## Decision record

- 2026-09-02 — created after user authorized paper-only composed-lane synthetic-wallet implementation.
