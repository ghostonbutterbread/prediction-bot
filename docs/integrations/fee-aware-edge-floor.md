# Fee-aware edge-floor lane integration dossier

- **Status:** review-approved
- **Branch:** `feat/payout-aware-shadow`
- **Base / target:** `beta` at `2c313af` → `beta`
- **Implementation commits:** `6eb0644`, `0536810`, `d109ff8`
- **Independent review:** approved after fee-math/provenance corrections; the reviewer also independently exercised a ledger write and confirmed the audit payload persisted.

## Goal

Add a disabled paper-only comparator that is deliberately selective: it can retain an already-approved stable BUY only when the recorded decision-time edge clears a configured fee-aware net-edge floor. It must not create trades, recompute a model, change sizing, touch wallets, or activate runtime configuration.

## Evidence and non-goal

A **descriptive, non-promotion-grade** root-loss report at `/mnt/data-collection/prediction-bot/data/derived_reports/source_router_root_loss_analysis_20260723.json` recorded 2,797 independently market-ID-resolved historical rows with 71.11% directional win rate and -13.27% ROI; its own methodology warning says decision provenance was not yet leakage-sanitized. This is motivation only, not evidence for promotion. A separate in-sample price/action filter was positive but remains a disabled historical hypothesis, so this lane uses a general fee-aware edge constraint rather than hard-coding historical price buckets.

## Verification

- Focused test covers baseline BUY_YES and BUY_NO retention/rejection using only decision-time side probability, executable side ask, and fee inputs.
- Focused suite: `PYTHONPATH=. python3 -m unittest tests.test_paper_shadow_lanes tests.test_collector_lane_replay` (56 tests, 1 skipped).
- Pending: beta merge decision.

## Activation boundary

The YAML definition is `enabled: false`. Any forward cohort needs explicit runtime allowlisting and independently resolved, chronological comparison evidence before promotion.
