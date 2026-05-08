# Prediction Lab Shadow Delta Spec

_Last updated: 2026-05-08_

## Purpose

Shadow mode is a comparison spotlight, not a second trading stream.

Prediction Lab should record one opportunity row per observed market snapshot and attach compact stable-vs-beta shadow comparison metadata to that row. Shadow must not duplicate paper trades, prediction rows, or replay inputs in a way that makes analysis think more trades happened than actually happened.

The same semantics apply outside Prediction Lab. Paper and live may persist a
separate hypothetical shadow-intent audit ledger derived from row-level
`shadow_delta`, but the shared core owns the interpretation of that intent. The
paper/live handlers are deliberately thin: append the counterfactual row, execute
the final real decision when allowed, and keep normal audit trails intact.

## Goals

1. Preserve the normal Prediction Lab row/table as the source of truth for replay and outcome scoring.
2. Add beta/shadow comparison metadata additively inside the same Prediction Lab row.
3. Make raw data easy to compare, deduplicate, and later review by an agent.
4. Keep promotion simple: when beta logic is accepted, move the same `strategy_policy` flags into the main config instead of migrating data formats.
5. Keep paper/live final actions unchanged in `stable/off` and `beta/shadow`.
6. Let both paper and live benefit from beta/shadow evidence through shared
   counterfactual audit rows, without creating a second execution path.

## Non-goals

- Do not run a second paper bot just to represent shadow behavior.
- Do not duplicate buys/sells as separate shadow trades.
- Do not make replay treat shadow deltas as executed or hypothetical prediction rows by default.
- Do not require a centralized registry of every config flag.
- Do not let paper/live handlers reinterpret beta/shadow routing or sizing as
  executable instructions.

## Ownership boundary

- Shared core owns stable-vs-beta comparison semantics and conversion from
  row-level `shadow_delta` into a hypothetical shadow intent.
- Prediction Lab owns opportunity collection and attaches `shadow_delta` to
  `predictions.jsonl` / `market_snapshots.jsonl`.
- Paper handlers may persist hypothetical shadow-intent rows to a separate
  ledger and may audit them, but must not put them in paper session `trades`,
  mutate paper balance, reserve capital, update exposure, record risk trades, or
  affect P&L.
- Live handlers may persist hypothetical shadow-intent rows to a separate ledger
  and may audit them, but must not call exchange placement/cancel APIs, append
  live `trade_history`, mutate open orders/positions, reserve capital, update
  risk state, or affect P&L.
- `beta/shadow` is logging/audit-only. `beta/enforce` is the mode that may change
  final execution logic.

## Data model

Prediction Lab rows MAY include a top-level `shadow_delta` object.

Minimum shape:

```json
{
  "schema_version": 1,
  "mode": "beta_shadow_delta",
  "status": "complete",
  "comparison_complete": true,
  "action_comparison_available": true,
  "policy": {
    "version": "beta",
    "mode": "shadow",
    "enabled_features": ["hidden_gem_lane_gates", "lane_sizing_caps"]
  },
  "stable": {
    "action": "SKIP",
    "reason_code": "win_probability_below_non_hidden_gem_floor",
    "direction": "SKIP",
    "decision_type": "skip",
    "requested_position_size": null,
    "selected_lane": "edge"
  },
  "shadow": {
    "action": "BUY_YES",
    "reason_code": "approved",
    "direction": "BUY_YES",
    "decision_type": "buy_yes",
    "requested_position_size": 1.0,
    "selected_lane": "hidden_gem"
  },
  "changed": true,
  "action_changed": true,
  "side_changed": true,
  "buy_decision_changed": true,
  "reason_changed": true,
  "size_changed": true,
  "lane_changed": true,
  "dedupe_key": "KXHIGHNY-26MAY08-T71|2026-05-07T18:00Z|beta-shadow"
}
```

Fields are intentionally compact and denormalized for table use. Full evidence remains in `decision_artifact`.

## Hypothetical shadow-intent ledger

Paper/live shadow intent rows MUST use a separate ledger and schema from real
execution audit rows. The initial shared-core helper emits:

```json
{
  "schema_name": "shadow_intent_audit_row",
  "schema_version": 1,
  "ledger_type": "counterfactual_shadow_intent",
  "runtime_mode": "paper",
  "mode": "beta_shadow",
  "policy": {
    "version": "beta",
    "mode": "shadow",
    "enabled_features": ["hidden_gem_lane_gates"]
  },
  "hypothetical": true,
  "counterfactual": true,
  "real_trade": false,
  "counts_as_trade": false,
  "counts_as_exposure": false,
  "counts_as_pnl": false,
  "execution_allowed": false,
  "final_action_mutated": false,
  "final_action_effect": "none",
  "mutates_balances": false,
  "mutates_exposure": false,
  "mutates_risk_state": false,
  "mutates_pnl": false,
  "mutates_trade_history": false,
  "mutates_open_orders": false,
  "mutates_open_positions": false,
  "stable_final": {
    "action": "SKIP",
    "reason_code": "win_probability_below_non_hidden_gem_floor",
    "requested_position_size": null,
    "selected_lane": "edge"
  },
  "shadow_intent": {
    "intent_kind": "trade",
    "action": "BUY_YES",
    "direction": "BUY_YES",
    "reason_code": "approved",
    "hypothetical_requested_position_size": 1.0,
    "selected_lane": "hidden_gem"
  },
  "execution": {
    "status": "not_executed",
    "order_id": null,
    "requested_size": 0.0,
    "approved_size": 0.0,
    "placed_size": 0.0,
    "filled_size": 0.0,
    "remaining_size": 0.0,
    "reserved_capital_delta": 0.0,
    "pnl": null
  }
}
```

The row may describe a hypothetical trade, skip, or unknown partial comparison.
It is not an `execution_audit_row`, must not be written to `trades.jsonl` or a
paper session `trades` array, and must not be normalized as a real paper/live
trade. A handler that persists it should use a dedicated file such as
`shadow_intents.jsonl`.

### No-mutation invariants

- `execution_allowed` is always false for `beta/shadow`.
- `final_action_mutated` is always false for `beta/shadow`; final action remains
  the stable/shared-core final action.
- All real execution sizes are zero. The hypothetical size lives only at
  `shadow_intent.hypothetical_requested_position_size`.
- `mutates_balances`, `mutates_exposure`, `mutates_risk_state`, `mutates_pnl`,
  `mutates_trade_history`, `mutates_open_orders`, and
  `mutates_open_positions` are always false.
- The row is excluded from real trade counts, exposure counts, and P&L counts.

## Interpretation

- `stable` means the final action that normal Prediction Lab / stable policy would use.
- `shadow` means what beta logic would have done, extracted from existing beta gate / lane sizing / evidence metadata when available.
- If shadow metadata is not available, `shadow_delta` may be omitted.
- `status` describes how complete the extracted comparison is:
  - `complete`: stable and shadow action/side/reason/size/lane fields can be directly compared.
  - `partial_beta_evidence`: beta evidence says a different lane would be selected, but the artifact does not contain enough downstream beta evidence to know the final beta action.
- `comparison_complete` is true only when all explicit comparison booleans can be computed from recorded artifact data.
- `action_comparison_available` is true only when `action_changed`, `side_changed`, and `buy_decision_changed` can be computed. When false, those fields are `null`, and the row must not be summarized as action-unchanged.
- `changed` is true if any of the explicit change booleans are true.
- Replay should continue to score only the normal row action unless explicitly running a shadow-delta analysis.
- For paper/live, a derived shadow intent is an audit artifact only. It can help
  answer "what would beta have intended?" but must not answer "what should this
  handler execute now?"

## Phase plan

### Phase 1 — Row-level shadow delta metadata

- Add deterministic extraction of compact `shadow_delta` from `decision_artifact`.
- Attach `shadow_delta` to `predictions.jsonl` and `market_snapshots.jsonl` rows when the configured policy is `beta/shadow`.
- Do not change final action, prediction recording, replay scoring, or paper/live execution.
- Add tests for: no shadow data in stable/off, row-level deltas in beta/shadow, and no duplicate shadow prediction rows.

### Phase 2 — Table/report summaries

- Add `shadow_delta` summary counts to analyze/replay reports.
- Include changed action/side/reason/size/lane counts.
- Count only row-level `shadow_delta` metadata. Do not synthesize shadow trades or replay rows.
- Dedupe comparison rows by `shadow_delta.dedupe_key` when present. If the key is missing, use a conservative fallback of `market_id|run_id|beta-shadow` for Prediction Lab rows and otherwise treat the row as unkeyed.
- When both `predictions.jsonl` and `market_snapshots.jsonl` contain the same `dedupe_key`, count it once for opportunity-level summaries. Prefer the row with `recorded_prediction: true`; otherwise prefer a row with a populated `decision_artifact`.
- Prediction row counts remain based on normal Prediction Lab prediction rows. Shadow deltas add comparison counts only and must not increase total prediction, trade, or replay-row counts.
- `partial_beta_evidence` rows count toward shadow coverage and lane-change coverage, but not toward unchanged-action counts. Their action/side/buy-decision comparison fields are unavailable, not false.

### Phase 3 — Agent review input

- Build a compact review export that feeds only rows with meaningful shadow deltas to the replay/review agent.
- Keep normal replay input as source-of-truth and shadow deltas as comparison metadata only.

### Phase 3a — Shared hypothetical intent helper

- Add shared-core conversion from row-level `shadow_delta` to
  `shadow_intent_audit_row`.
- Keep the helper gated on `policy.version == beta` and `policy.mode == shadow`.
- Do not wire runtime paper/live handlers unless the write is to a separate
  ledger and is logging-only.
- Add tests that stable/off and beta/enforce emit no hypothetical shadow intent,
  and that beta/shadow rows do not count as real trades or mutations.

### Phase 3b — Paper/live append-only shadow-intent ledger

- Wire paper and live runtime handlers to derive a row-level `shadow_delta` from
  the existing pre-execution decision artifact and append the converted
  `shadow_intent_audit_row` to `shadow_intents.jsonl`.
- Keep the write append-only, best-effort, and isolated under the runtime mode
  directory alongside existing audit files, for example
  `data/paper/shadow_intents.jsonl` or `data/live/shadow_intents.jsonl`.
  A shadow-intent write failure must be swallowed/logged and must never abort
  the real paper/live execution path.
- Do not write shadow-intent rows to `trades.jsonl`, paper session `trades`,
  live `trade_history`, risk block logs, risk state, exposure state, or P&L
  accounting.
- Do not pass shadow-intent rows to paper execution adapters, live execution
  adapters, exchange placement/cancel APIs, reconciliation, or settlement.
- Preserve current Phase 1-2 behavior: Prediction Lab row-level
  `shadow_delta` remains comparison metadata only, and summary/replay counts
  remain based on real rows.
- Preserve current Phase 3a behavior: `stable/off` and `beta/enforce` sources
  still emit no shadow-intent rows because the shared helper only accepts
  `policy.version == beta` and `policy.mode == shadow`.
- Add tests for separate file writes, no paper trade/exposure/P&L mutation, no
  emission outside `beta/shadow`, and no live order placement from a shadow
  intent.

## Promotion rule

Promotion means changing config from `beta/shadow` to `beta/enforce` or moving accepted flags into main config. Promotion must not require moving data between roots or changing row schemas.
