# Live Parity Implementation Brief

## Objective

Implement optional parity mode so paper can simulate live's final execution-time approval path, while preserving paper's current logic-first mode as the default.

## Architectural Rule

If a behavior affects decision inputs in both paper and live, it must be moved into shared code before parity mode depends on it.

Use this rule throughout implementation:
- **Shared core**: decision logic, normalized decision inputs, common reasoning fields
- **Adapters**: environment-specific state gathering, exchange/API calls, simulated fills, live order placement
- **Do not** duplicate execution-time price normalization in paper and live

## Current Known Repo State

- Shared decision function already exists: `bot/shared_core/decision.py::build_trade_decision()`
- Hidden-gem gating already lives in shared core
- Live already does execution-time revalidation in `bot/live_execution.py`
- Paper currently decides once from strategy snapshot / signal snapshot path
- No real `parity_mode` config implementation yet in `bot/config.py`

## Required Phase 1 Order

### Step 1: Shared execution snapshot helper
Create a helper module, suggested path:
- `bot/shared_core/execution_snapshot.py`

It should accept:
- raw signal
- current bid/ask snapshot
- direction

It should return normalized fields used by both paper and live:
- `market_price`
- `yes_price`
- `no_price`
- `best_yes_ask`
- `best_no_ask`
- `best_yes_bid`
- `best_no_bid`
- `estimated_fill_price`
- optional metadata describing source (`book`, `fallback`, `missing`)

### Step 2: Refactor live to use the helper first
Update:
- `bot/live_execution.py`

Goal:
- replace hand-rolled live signal price rewriting with the shared helper
- keep behavior equivalent
- establish canonical execution-price semantics before paper parity mode is added

### Step 3: Add config plumbing
Update:
- `bot/config.py`
- config docs/examples if needed

Add:
```yaml
parity_mode:
  enabled: false
  record_revalidation_snapshot: true
  require_book_prices: false
  fallback_to_signal_prices: true
```

Config semantics:
- disabled = inert
- `require_book_prices: true` overrides fallback
- missing config must preserve current behavior

### Step 4: Add paper parity revalidation path
Likely update:
- `bot/paper_adapters.py`
- `bot/simulator.py`

Goal:
- existing paper mode stays unchanged when parity mode is off
- when parity mode is on:
  - build original context
  - fetch/derive execution-time prices
  - normalize through shared helper
  - rebuild context from execution snapshot
  - rerun `build_trade_decision()`
  - execute only from final revalidated decision

### Step 5: Expand audit fields
Update:
- `bot/paper_adapters.py`
- `bot/simulator.py`
- `bot/trade_audit.py` if useful

Minimum fields:
- `parity_mode_enabled`
- `execution_revalidated`
- `execution_revalidation_outcome`
- `original_signal_snapshot`
- `execution_snapshot`
- `original_decision_reason_code`
- `execution_decision_reason_code`
- `execution_snapshot_source`

Backward compatibility:
- old rows without parity metadata must still load cleanly

### Step 6: Add tests
Update or add:
- `tests/test_live_execution.py`
- new paper parity tests
- shared fixture tests as needed

Required coverage:
1. live and paper parity paths match under identical prices
2. hidden-gem drift case rejects in parity mode but passes in logic-only mode
3. fallback behavior matches config
4. audit fields persist when enabled
5. parity mode off preserves current paper flow

## Non-Goals for This Phase

Do not expand scope into:
- partial-fill simulation in paper
- full live lifecycle parity
- slippage realism overhaul
- exchange microstructure simulation

Those are later phases.

## Build Standard

A Phase 1 implementation is successful only if:
1. live and paper use the same execution snapshot normalization path
2. parity mode is optional and off by default
3. paper parity mode can reject trades that stale snapshot paper would have accepted
4. audit output explains whether the change came from logic vs execution-time drift
5. tests prove parity behavior, not just structure
