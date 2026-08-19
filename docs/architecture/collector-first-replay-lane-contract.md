# Collector-First Replay Lane Contract

**Status:** canonical design contract — implementation slice pending; no runtime activation
**Branch:** `feature/source-router-ev-shadow`
**Date:** 2026-08-18
**Supersedes:** no implementation; the blind-replay validator and fixed-stake
diagnostics remain subordinate compatibility/reporting references
**Related:** `prediction_lab_shared_pipeline_spec.md` owns forward shared-market
data ownership; `source_router_beta_paper_pipeline.md` remains the bounded
source-router research workflow

## Decision we are making

The collector is the canonical, immutable record of what was knowable at each market decision time. It is the primary substrate for research, historical replay, and lane A/B testing.

Paper trading is a **forward validation** layer, not the primary way to discover or iterate on trading logic. A promising replay lane later runs alongside its control on fresh shared snapshots and independently settled outcomes.

```text
collector snapshots → blind deterministic replay lanes → choose candidate
                   → fresh shared-snapshot paper comparison → promotion evidence
```

## Current implementation status

The repository already has the pieces this contract is meant to connect:

- the observer collector publishes immutable snapshots and shared candidate
  identities;
- collector-derived replay inputs are sanitized, content-hashed, and bound to
  exact later outcomes separately;
- paper owns real account state, open positions, reservation, risk, Kelly, and
  same-event re-entry behavior;
- shared-market paper consumers can emit non-mutating source-router lane
  receipts from collector snapshots.

What does **not** exist yet is the derived-only chronological adapter that feeds
sanitized collector inputs through the paper-account decision seam and settles
its synthetic positions only at authoritative historical settlement time. The
current source-router wallet and incremental lane reports remain explicitly
labelled diagnostics; they are not evidence of paper-parity account replay.

For a forward cohort, the collector is the only market-data publisher. Paper is
a shared-market consumer with its own isolated simulated account; it must not
poll a competing market universe or mutate collector evidence. The operational
configuration details live in the shared-pipeline spec and beta-shadow runbook.

## Problem this fixes

The archive already preserves much of the replay substrate: timestamps, shared candidate identity, market prices, raw source inputs, and a later strict resolution join. It does not consistently preserve or reconstruct enough information for every lane to re-run the same full decision path and synthetic wallet sizing.

That creates an avoidable split:

```text
paper runner: strategy model probability → Kelly/risk → decision
collector replay: source/router direction → resolution, but incomplete sizing replay
```

The fix is **not** to make the collector execute or mutate wallets. The fix is to give replay a small, versioned decision-input contract that lets the same decision logic be executed deterministically from a recorded snapshot.

## Invariants

1. **Immutable evidence.** Raw collector snapshots and historical resolutions are never changed.
2. **Single shared snapshot.** Control and every candidate lane receive the same exact snapshot/candidate identity.
4. **Blind decision.** Replay constructs a fresh allowlisted input object per lane. It recursively rejects outcome/settlement fields and gives the lane no resolution handle before a sealed decision artifact exists.
5. **Deterministic re-execution.** A lane gets its probability, side, action, and size by running its declared logic against snapshot inputs—not by copying an old lane's result.
6. **Canonical identity and settlement.** A decision key is `(shared_snapshot_id, shared_candidate_id, market_id, observed_at_utc, raw_row_sha256)`. A strict resolver must match this identity; `market_id` is allowed only when the resolution artifact proves it is unique. Ambiguous matches fail closed.
7. **Re-observations are distinct.** Repeated snapshots of a market are never collapsed. UTC `observed_at` plus the canonical raw-row hash is the stable tie-breaker; missing/invalid timestamps fail closed.
8. **Versioned provenance.** Each derived decision records hashes of canonical input bytes, lane definition/config, strategy logic, Kelly config, risk config, execution-price policy, and input schema.
9. **No deployment effect.** Replay never accesses wallets/balances, sends orders, restarts services, or enables a runtime lane.

## Replay input contract

The collector retains its raw payload. A derived, content-addressed `replay_decision_input_v1` record is produced per replayable snapshot under a separate replay-input/report namespace. It is bound to the selected raw row by canonical serialized-row and index-entry hashes, and an existing run is never overwritten. It must contain only decision-time information:

```json
{
  "schema_name": "replay_decision_input",
  "schema_version": 1,
  "shared_snapshot_id": "...",
  "shared_candidate_id": "...",
  "market_id": "...",
  "observed_at": "...",
  "snapshot_provenance": {
    "raw_row_sha256": "...",
    "raw_payload_sha256": "...",
    "collector_index_entry_sha256": "..."
  },
  "market": {
    "question": "...",
    "market_metadata": {"market_route_fields": "recorded decision-time values"},
    "yes_price": 0.0,
    "no_price": 0.0,
    "best_yes_ask": 0.0,
    "best_no_ask": 0.0,
    "execution_snapshot": {"executable-side order-book fields": "..."}
  },
  "source_inputs": {"recorded_as_of": "...", "full_decision_time_source_context": "..."},
  "derived_features": {"schema_version": "...", "values": {"...": "..."}},
  "decision_context": {
    "strategy_input_schema_version": "...",
    "policy_config_sha256": "...",
    "strategy_logic_sha256": "...",
    "kelly_config_sha256": "...",
    "risk_config_sha256": "...",
    "execution_price_policy_sha256": "..."
  }
}
```

The contract does **not** include an outcome, settlement timestamp, resolution label, later backfill metadata, or a previously computed action/model probability from a different lane. Raw inputs, versioned derived features, lane-owned logic/config, and lane output artifacts are separate objects. A record missing any field required by the selected strategy is explicitly non-replayable for this contract; legacy diagnostic replay may still report it under a prominent legacy mode label.

## Replay lanes

A lane is a declarative named policy choice. It must be isolated, explicitly versioned, and permitted only to alter declared policy inputs (for example a source-router selection rule, an entry threshold, or a sizing policy).

For every input record, replay evaluates:

```text
control (stable policy)
candidate A (one explicit hypothesis)
candidate B (optional, one separate hypothesis)
```

Every resulting sealed decision artifact includes:

```text
canonical decision key and canonical-input hash
lane id and lane-policy hash
model probability for the selected side
entry price and action
edge/confidence
Kelly requested notional
risk-approved notional or explicit rejection
reason codes and decision trace
```

## Decision and settlement phases

The new evaluator uses two APIs, not one mixed decision/P&L builder:

```text
decide(allowlisted_snapshot, prior_settled_history, lane_policy) → sealed decision artifact
settle(sealed_decision, strict_resolution) → settlement receipt
```

The strict-resolution handle is unavailable to `decide`. `prior_settled_history` is bounded by authoritative settlement time and the snapshot's UTC observation time. The legacy source-router and collector-lane reports remain diagnostics until migrated; they must be labelled as such and cannot claim contract compliance merely by carrying a result/action from a previous runner.

## Two evaluation views from the same decisions

### Fixed-stake diagnostic

Apply a declared fixed stake (e.g. $10) to each eligible BUY after its decision is locked. This compares signal/direction quality without compounding or capacity effects.

### Sequential synthetic wallet

Process emitted decisions by normalized UTC observation time, then canonical raw-row hash; missing/invalid time fails closed. There are two explicitly labelled wallet semantics:

1. **Immediate-settlement diagnostic:** settle each decision immediately after it is sealed. This is deterministic but does not model capital being tied up while markets overlap.
2. **Pending-position economic simulation:** reserve capital at entry, keep each position open until its authoritative settlement time, release capital and P&L at settlement, and apply Kelly/risk to available cash, exposure, open positions, and drawdown at every event boundary.

The implementation target for viability claims is the second mode. Both modes use the shared-core `build_trade_decision` / `KellySizerLike` / `RiskPolicyLike` seam via a stateful synthetic `AccountState` adapter—not `PredictionLabReplay`'s stateless fixed-opportunity risk policy. They reproduce configured fees, minima, rounding, caps, and risk receipts exactly.

The wallet result is **model P&L under declared replay policy**, not a reconstruction of an old production wallet. Its report must record all policy/config hashes and balance/exposure before/after receipts.

## Implementation sequence (minimal vertical slices)

1. **Inventory and contract test**
   - Map current collector/replay fields and identify what can be faithfully populated from existing archive snapshots.
   - Add a fixture showing a raw snapshot becomes a valid `replay_decision_input_v1` without outcomes, with a canonical decision key and hash verification.

2. **Allowlisted blind-input boundary**
   - Construct fresh lane input objects from explicit recorded fields only; recursively reject unknown outcome/settlement fields.
   - Split the evaluator into sealed `decide` and separate strict `settle` phases. Do not migrate legacy action/size passthrough reports by relabelling them.

3. **Control-lane re-execution**
   - Run stable decision logic from the contract using the `DecisionPipelineEvaluator` strategy path and shared-core decision seam.
   - Assert a deterministic decision artifact includes its own model probability, requested Kelly size, risk decision, canonical snapshot time (never replay wall-clock time), and provenance.

4. **Pending-position synthetic wallet uses actual sizing path**
   - Feed chronological control/candidate decisions through the actual paper Kelly and `RiskPolicyLike` interfaces using stateful synthetic `AccountState` and pending-position adapter state.
   - Reserve capital at entry and release it only at authoritative settlement; assert sizes vary with balance/probability/price and risk receipts record requested/adjusted/rejected decisions.
   - Keep immediate settlement only as an explicitly labelled diagnostic. Do not fall back to $10 except in the independent fixed-stake diagnostic lane.

5. **Two-lane replay and exact settlement**
   - Run control plus one candidate over shared fixtures.
   - Ensure candidate changes cannot contaminate control inputs; strict settlement uses the canonical decision key and fails closed for ambiguity.

6. **Real archived replay report**
   - Run a bounded historical window using hash-verified index/archive evidence.
   - Report coverage and blockers before interpreting P&L. If an archive lacks a required decision-time input, fail that row closed and report it; never synthesize the missing value. Label existing collector/source-router fixed-size reports `legacy_diagnostic` until migrated.

7. **Forward-paper handoff (separate, later)**
   - Only a replay-supported candidate gets a named fresh control-vs-candidate paper profile.
   - No activation is part of this implementation document.

## Acceptance criteria

```text
✓ raw archive remains unchanged
✓ same shared input identity reaches every lane; re-observations remain distinct
✓ allowlisted replay decision cannot access its own future outcome or unknown settlement fields
✓ control and candidate each compute their own model probability and Kelly request
✓ sequential wallet uses actual configured paper sizing/risk interfaces with pending-position accounting
✓ fixed stake remains a distinct normalized diagnostic and legacy reports are labelled
✓ strict resolution settlement uses canonical identity and fails closed for ambiguity
✓ every report has config/policy/input provenance and coverage/blocker counts
✓ focused tests and a bounded real archive run pass
```

## Explicit non-goals

```text
- live trading or any runtime enablement
- a new collector per lane
- copying prior paper decisions into candidate lanes
- retrospective mutation of raw snapshots or resolutions
- assuming missing decision-time data
- declaring a historical replay profitable enough for promotion
```
