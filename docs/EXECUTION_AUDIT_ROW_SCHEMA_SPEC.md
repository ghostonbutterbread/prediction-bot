# Execution / Audit Row Schema Spec

## Purpose

Define the canonical write-time row contract for paper and live execution/audit records.

This spec exists so:
- paper and live emit the same core fields
- `bot/parity_audit.py` does not need to guess between aliases for required fields
- lifecycle/reporting work can build on one stable schema
- missing or contradictory fields are treated as contract violations, not normal variation

This is a **code-facing schema spec**, not just an analysis note.

---

## Scope

This contract applies to newly written execution/audit rows from:
- `bot/paper_adapters.py`
- `bot/live_execution.py`
- related persistence/reporting code that records order/execution outcomes

It is not a mandate to rewrite all historical rows immediately.
Historical rows should remain readable through normalization/backward-compat helpers.

---

## Design Rules

1. **One canonical field per meaning**
   - Required meanings should not depend on aliases like `id|trade_id|order_id` at write time.
2. **Lifecycle state must be explicit**
   - A row should clearly say whether it was rejected, placed, partially filled, filled, canceled, stale, or failed.
3. **Sizing progression must be monotonic**
   - Requested → approved → placed → filled should be consistent.
4. **Parity fields are first-class**
   - If revalidation happened, the row must say so directly.
5. **Historical compatibility lives in readers**
   - New writers should emit the canonical contract; readers may keep compatibility shims.
6. **API truth over local inference in live mode**
   - For live trading, when the exchange/API can provide order, fill, balance, position, or resting-order truth, writers and reconciliation should prefer that over local assumptions.
   - Paper may synthesize these states because it has no exchange truth source, but live should minimize drift by deferring to API-observed reality whenever possible.

---

## Schema Versioning

All newly written canonical rows should include:

```json
{
  "schema_name": "execution_audit_row",
  "schema_version": 1
}
```

### Versioning rule
- Increment `schema_version` only for contract changes that affect writer/reader expectations.
- Additive optional fields can remain within the same version if old readers safely ignore them.

---

## Canonical Fields

### 1. Identity fields

These fields identify the row and what it refers to.

| Field | Type | Required | Notes |
|---|---|---:|---|
| `schema_name` | string | yes | Must be `execution_audit_row` |
| `schema_version` | integer | yes | Start at `1` |
| `timestamp` | string | yes | ISO-8601 timestamp for when the row was written |
| `trade_id` | string | yes | Stable row/trade identifier; do not rely on `id`/`order_id` aliases in new rows |
| `market_id` | string | yes | Target market |
| `event_key` | string | yes | Event/family linkage key |
| `question` | string | no | Human-readable market question |
| `exchange` | string | yes | e.g. `kalshi` |
| `direction` | string | yes | `BUY_YES` or `BUY_NO` |

### 2. Lifecycle fields

These fields describe where the trade/order ended up.

| Field | Type | Required | Notes |
|---|---|---:|---|
| `status` | string | yes | Canonical row status enum |
| `lifecycle_state` | string | yes | More specific state machine label; may match `status` initially |
| `failure_stage` | string/null | no | Where the failure/rejection occurred |
| `resolved` | boolean | no | Whether the position/row has resolved |
| `resolved_at` | string/null | no | Resolution timestamp when applicable |

### 3. Sizing fields

These fields describe the size progression.

| Field | Type | Required | Notes |
|---|---|---:|---|
| `requested_size` | number | yes | Size originally requested by the candidate/order path |
| `approved_size` | number | yes | Size allowed by decision/risk logic |
| `placed_size` | number | yes | Size actually submitted to exchange/simulator |
| `filled_size` | number | yes | Size actually filled |
| `remaining_size` | number | yes | Unfilled remainder |
| `reserved_capital` | number | yes | Capital reserved by this row/state |

### 4. Pricing fields

| Field | Type | Required | Notes |
|---|---|---:|---|
| `market_price` | number/null | yes | Canonical decision-time market price used for the row |
| `entry_price` | number/null | yes | Intended execution/entry price of record |
| `fill_price` | number/null | no | Actual fill price if any |
| `estimated_fill_price` | number/null | no | Best estimate before execution/fill |
| `slippage_estimate` | number/null | no | Optional slippage estimate |
| `yes_price` | number/null | no | Optional explicit normalized yes price |
| `no_price` | number/null | no | Optional explicit normalized no price |

### 5. Decision / reasoning fields

| Field | Type | Required | Notes |
|---|---|---:|---|
| `decision_reason` | string/null | no | Human-readable reason |
| `decision_reason_code` | string/null | yes | Canonical primary reason code for the row outcome |
| `original_decision_reason_code` | string/null | no | Pre-revalidation decision code |
| `execution_decision_reason_code` | string/null | no | Post-revalidation decision code |

### 6. Parity / revalidation fields

| Field | Type | Required | Notes |
|---|---|---:|---|
| `parity_mode_enabled` | boolean | yes | Whether parity mode was enabled for this row |
| `execution_revalidated` | boolean | yes | Whether execution-time revalidation actually ran |
| `execution_revalidation_outcome` | string/null | no | Outcome enum defined below |
| `execution_snapshot_source` | string | yes | Source enum defined below |
| `original_signal_snapshot` | object/null | no | Original signal snapshot payload |
| `execution_snapshot` | object/null | no | Execution-time snapshot payload |

### 7. Account / accounting context fields

| Field | Type | Required | Notes |
|---|---|---:|---|
| `available_cash_before` | number/null | no | Pre-entry available cash |
| `available_cash_after_entry` | number/null | no | Post-entry available cash |
| `pnl` | number/null | no | Net P&L when known |
| `outcome` | string/null | no | `YES` or `NO` when resolved |

---

## Canonical Enums

### `direction`
- `BUY_YES`
- `BUY_NO`

### `status`
Writers should use one of:
- `candidate`
- `rejected`
- `approved`
- `placed`
- `partial`
- `filled`
- `canceled`
- `stale`
- `failed`
- `resolved`

Initial implementations may use a narrower subset, but new statuses should come from this set.

### `lifecycle_state`
Use a more operational state label where useful, for example:
- `candidate`
- `risk_rejected`
- `revalidation_rejected`
- `placement_failed`
- `placed_open`
- `partial_open`
- `filled_open`
- `canceled_unfilled`
- `canceled_partial`
- `stale_open_order`
- `resolved_position`

### Resolution semantics
For resolved rows and settlement events:
- `outcome` should mean the **market outcome truth** (`YES` or `NO`), not whether the bot won or lost.
- If code needs bot-relative result semantics (`won` / `lost`), keep that in a separate metadata field rather than overloading `outcome`.
- `status = resolved` should represent a position that was previously open and is now settled.
- `lifecycle_state = resolved_position` should be the canonical resolved-state label.

### `failure_stage`
Suggested enum:
- `signal_validation`
- `risk_check`
- `revalidation`
- `order_submission`
- `exchange_reconcile`
- `settlement`
- `unknown`

### `execution_revalidation_outcome`
Suggested enum:
- `approved`
- `rejected`
- `fallback`
- `skipped`
- `missing`

### `execution_snapshot_source`
Required enum:
- `book`
- `fallback`
- `missing`
- `unknown`

---

## Required Invariants

These are contract rules for newly written rows.

### General
- `trade_id` must be non-empty.
- `market_id` must be non-empty.
- `direction` must be one of the canonical direction enums.
- `requested_size`, `approved_size`, `placed_size`, `filled_size`, `remaining_size`, and `reserved_capital` must be finite numbers >= 0.

### Sizing monotonicity
Unless a future contract explicitly documents an exception:

```text
0 <= filled_size <= placed_size <= approved_size <= requested_size
remaining_size = max(placed_size - filled_size, 0)
```

### Status consistency
- `status = rejected` -> `filled_size == 0`
- `status = placed` -> `placed_size > 0`
- `status = partial` -> `filled_size > 0` and `remaining_size > 0`
- `status = filled` -> `filled_size > 0` and `remaining_size == 0`
- `status = canceled` -> `lifecycle_state` should distinguish fully unfilled vs partially filled cancellation
- `status = stale` -> `lifecycle_state` should normally be `stale_open_order`
- `status = resolved` -> `resolved = true`, `resolved_at` should be present, and `lifecycle_state` should normally be `resolved_position`
- In live mode, post-submission status should prefer API-confirmed state over local optimistic assumptions. For example, a submitted live order should not be written as `filled` unless the exchange truth supports that conclusion.

### Revalidation consistency
- `execution_revalidated = false` -> `execution_revalidation_outcome` should be `null` or `skipped`
- `execution_revalidated = true` -> `execution_revalidation_outcome` should be non-null
- `execution_revalidated = true` and `execution_snapshot_source = missing` should be treated as suspicious unless explicitly intended

### Snapshot consistency
- If `parity_mode_enabled = true` and `execution_revalidated = true`, `execution_snapshot` should normally be present.
- If `original_decision_reason_code` is present, it should refer to the pre-revalidation decision.
- If `execution_decision_reason_code` is present, it should refer to the execution-time decision.

### Reporting / viewer expectations
The normalized report layer should be able to surface, without re-guessing writer intent:
- lifecycle-state counts
- resolved outcome counts (`YES` / `NO`)
- execution decision deltas (`original_decision_reason_code` vs `execution_decision_reason_code`)
- execution price deltas (`original_signal_snapshot.market_price` vs `execution_snapshot.market_price` when both exist)
- invalid-contract row counts and top contract issues

---

## Minimum Snapshot Payload Shape

Writers do not need to dump huge nested objects, but these keys should be preferred when available.

### `original_signal_snapshot`
Suggested fields:
```json
{
  "market_price": 0.42,
  "yes_price": 0.42,
  "no_price": 0.58,
  "best_yes_ask": 0.43,
  "best_no_ask": 0.59,
  "best_yes_bid": 0.41,
  "best_no_bid": 0.57,
  "estimated_fill_price": 0.43,
  "model_probability": 0.51,
  "edge": 0.08,
  "confidence": 0.71
}
```

### `execution_snapshot`
Suggested fields:
```json
{
  "market_price": 0.47,
  "yes_price": 0.47,
  "no_price": 0.53,
  "best_yes_ask": 0.48,
  "best_no_ask": 0.54,
  "best_yes_bid": 0.46,
  "best_no_bid": 0.52,
  "estimated_fill_price": 0.48,
  "source": "book"
}
```

---

## Example Rows

### Revalidation rejection row

```json
{
  "schema_name": "execution_audit_row",
  "schema_version": 1,
  "timestamp": "2026-04-24T18:00:00Z",
  "trade_id": "paper-20260424-001",
  "market_id": "KXRAIN-NYC-2026-04-24",
  "event_key": "KXRAIN-NYC-2026-04",
  "question": "Will NYC record rain on Apr 24?",
  "exchange": "kalshi",
  "direction": "BUY_YES",
  "status": "rejected",
  "lifecycle_state": "revalidation_rejected",
  "failure_stage": "revalidation",
  "requested_size": 10.0,
  "approved_size": 10.0,
  "placed_size": 0.0,
  "filled_size": 0.0,
  "remaining_size": 0.0,
  "reserved_capital": 0.0,
  "market_price": 0.47,
  "entry_price": 0.47,
  "fill_price": null,
  "estimated_fill_price": 0.48,
  "decision_reason": "Hidden gem edge below threshold after revalidation",
  "decision_reason_code": "hidden_gem_edge_below_threshold",
  "original_decision_reason_code": "approved",
  "execution_decision_reason_code": "hidden_gem_edge_below_threshold",
  "parity_mode_enabled": true,
  "execution_revalidated": true,
  "execution_revalidation_outcome": "rejected",
  "execution_snapshot_source": "book",
  "original_signal_snapshot": {"market_price": 0.42},
  "execution_snapshot": {"market_price": 0.47, "source": "book"},
  "available_cash_before": 100.0,
  "available_cash_after_entry": 100.0,
  "resolved": false,
  "resolved_at": null,
  "pnl": null,
  "outcome": null
}
```

### Filled live row

```json
{
  "schema_name": "execution_audit_row",
  "schema_version": 1,
  "timestamp": "2026-04-24T18:05:00Z",
  "trade_id": "live-20260424-019",
  "market_id": "KXRAIN-NYC-2026-04-24",
  "event_key": "KXRAIN-NYC-2026-04",
  "exchange": "kalshi",
  "direction": "BUY_YES",
  "status": "filled",
  "lifecycle_state": "filled_open",
  "failure_stage": null,
  "requested_size": 10.0,
  "approved_size": 8.0,
  "placed_size": 8.0,
  "filled_size": 8.0,
  "remaining_size": 0.0,
  "reserved_capital": 8.0,
  "market_price": 0.45,
  "entry_price": 0.45,
  "fill_price": 0.45,
  "estimated_fill_price": 0.45,
  "decision_reason": "Approved",
  "decision_reason_code": "approved",
  "original_decision_reason_code": "approved",
  "execution_decision_reason_code": "approved",
  "parity_mode_enabled": false,
  "execution_revalidated": true,
  "execution_revalidation_outcome": "approved",
  "execution_snapshot_source": "book",
  "original_signal_snapshot": null,
  "execution_snapshot": {"market_price": 0.45, "source": "book"},
  "available_cash_before": 100.0,
  "available_cash_after_entry": 92.0,
  "resolved": false,
  "resolved_at": null,
  "pnl": null,
  "outcome": null
}
```

---

## Backward-Compatibility Guidance

Reader/normalizer code may keep supporting historical aliases such as:
- `id`, `order_id` -> `trade_id`
- `price`, `fill_price`, `market_price` -> canonical pricing fields where needed
- `position_size` -> fallback for missing sizing fields in old rows

But that alias handling should remain in readers like `bot/parity_audit.py`, not in new writers.

---

## Recommended Implementation Order

1. Update writers in `bot/paper_adapters.py` and `bot/live_execution.py` to emit the canonical fields for new rows.
2. Centralize shared coercion/validation helpers in `bot/trade_audit.py`.
3. Tighten `bot/parity_audit.py` so required canonical fields are treated as expected, not optional guesses.
4. Extend tests to cover:
   - invariant enforcement
   - row normalization from canonical rows
   - detection of contradictory lifecycle/sizing states
   - parity diff summaries by reason code / snapshot source / lifecycle state

---

## Acceptance Criteria

This spec is implemented well when:
- new paper/live rows use the same core field names
- report code stops depending on multiple aliases for required meanings
- contradictory rows are surfaced as invalid or suspicious
- lifecycle hardening can extend the schema without redefining the basics
