# Strict source evidence integration dossier

- **Status:** integrated
- **Owner:** Prediction Lab collector and derived replay artifacts
- **Branch:** `feature/strict-source-evidence`
- **Base commit:** `c864b6e`
- **Integrated target:** `beta` at merge commit `3ed30b9`
- **Last updated:** 2026-08-23
- **Inspiration / canonical references:** [source-router forward evidence and wallet plan](../architecture/source-router-forward-evidence-wallet-plan.md); [collector-first replay lane contract](../architecture/collector-first-replay-lane-contract.md)

## Intent

Make strict Source Router history depend only on sealed decision inputs, explicit
source-target proof, provenance-bearing source observations, and independently
authoritative settlement evidence. This is paper/observer evaluation code; it
does not enable a trading lane or mutate raw history.

## Implemented contract

- Source evidence requires explicit source identity, city/date/timezone, kind,
  shape, side compatibility, valid source time, and raw-record provenance.
- Source history requires exact authoritative settlement and is visible only
  when `settlement_ts < future_decision_time`.
- Repeated polls collapse deterministically; `VOID` is retained for audit but
  never becomes history, PnL, or win/loss evidence.
- Collector/replay compatibility is reported rather than silently inferring
  missing strict fields.

## Evidence and review

- Focused strict-source tests: 100 passed.
- Declared collector/pipeline gate: 118 passed in independent review.
- Full beta suite: 1,016 tests passed.
- Independent read-only review: ready for paper-only beta integration; no
  critical findings.
- Merge and `git diff --check` were clean.

## Blockers and deferred work

A fresh named observer cohort must produce a bounded compatibility receipt
before strict history can be used for cohort eligibility or promotion claims.
This is an activation/evidence gate, not an integration blocker.

## Decision gates

- **Integration gate:** satisfied by `3ed30b9`.
- **Activation / cohort gate:** fresh collector compatibility receipt with
  required-field and rejection counts.
- **Promotion gate:** fresh, independently resolved, full-overlap paper cohort.

## Decision record

- 2026-08-23 — implemented and independently reviewed on `feature/strict-source-evidence`.
- 2026-08-23 — merged into local `beta` as `3ed30b9`; no runtime activation.
