# Fee-aware edge-floor lane integration dossier

- **Status:** feature
- **Branch:** `feat/payout-aware-shadow`
- **Base / target:** `beta` at `2c313af` → `beta`

## Goal

Add a disabled paper-only comparator that is deliberately selective: it can retain an already-approved stable BUY only when the recorded decision-time edge clears a configured fee-aware net-edge floor. It must not create trades, recompute a model, change sizing, touch wallets, or activate runtime configuration.

## Evidence and non-goal

A historical diagnostic showed 2,797 resolved router buys with a 71.11% win rate but -$3,711.73 PnL (-13.27% ROI); directional correctness alone is not the optimization target. A separate in-sample price/action filter was positive but remains a disabled historical hypothesis, so this lane uses a general fee-aware edge constraint rather than hard-coding historical price buckets.

## Verification

- Focused test covers a baseline BUY that is rejected and one that is retained using only decision-time price/edge/fee inputs.
- Pending: affected lane test suite, independent review, beta merge decision.

## Activation boundary

The YAML definition is `enabled: false`. Any forward cohort needs explicit runtime allowlisting and independently resolved, chronological comparison evidence before promotion.
