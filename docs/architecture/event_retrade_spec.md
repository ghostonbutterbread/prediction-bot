# Event-Aware Retrade Spec

## Status
Draft v1 for implementation across paper and live paths.

## Goal
Allow the bot to take multiple positions on the same event when incremental value remains attractive, while enforcing event-level concentration controls, stronger retrade thresholds, and fee-aware viability checks.

## Why
Current behavior recomputes each trade using current price and bankroll, but it does not fully reason about event-level portfolio concentration when adding another position on the same event. This can inflate apparent performance and create correlated overexposure.

## Product Decisions
- Allow retrades on the same event.
- Cap unresolved event exposure at **10% of current balance**.
- Cap unresolved positions per event at **3**.
- Require a **stronger threshold** for second and later entries on the same event.
- Make retrades **fee-aware** and **slippage-aware** so an additional entry only executes if post-fee expected value remains positive enough after cuts.
- Apply to **paper and live** through the shared-core decision path.
- Keep exact duplicate `market_id` blocking in place.
- Add stricter same-event overlap controls to avoid near-duplicate bracket stacking.

## Definitions

### Canonical exposure definition
For v1, event exposure means **worst-case loss still at risk**, implemented as unresolved `reserved_capital` summed across all positions sharing the same `event_key`.

This avoids ambiguity around notional, unrealized P&L, or model conviction.

### Current balance definition
For v1, `current_balance` means the mode-specific equity snapshot already used by shared-core sizing.
- paper: session `balance`
- live: exchange balance returned by the live adapter

Event cap is computed as:

```text
max_event_exposure_usd = current_balance * max_event_exposure_pct
```

### Event
Primary identity is `event_key`.

Examples:
- `KXLOWTDEN-26APR21`
- `KXHIGHTATL-26APR21`

### Position
A single market contract within an event.

Example:
- `KXLOWTDEN-26APR21-B49.5`

### Event exposure
Sum of unresolved `reserved_capital` across all positions sharing the same `event_key`.

### Retrade
Any new trade when there is already at least one unresolved position for the same `event_key`.

## Config Additions
Add to config with safe defaults:

```yaml
risk:
  max_event_exposure_pct: 0.10
  max_event_positions: 3
  retrade_edge_premium: 0.01
  retrade_confidence_premium: 0.00
  retrade_size_decay: 0.65
  strict_event_overlap: true
  min_retrade_net_edge: 0.005
  require_price_improvement_for_same_market_family: false
  price_improvement_ticks: 0.03
```

### Meaning
- `max_event_exposure_pct`: max unresolved event exposure as share of current balance.
- `max_event_positions`: max unresolved positions per event.
- `retrade_edge_premium`: extra edge required for retrades relative to normal threshold.
- `retrade_confidence_premium`: optional extra confidence requirement.
- `retrade_size_decay`: multiplicative haircut per existing unresolved same-event position.
- `strict_event_overlap`: block same-event trades judged too overlapping.
- `min_retrade_net_edge`: minimum net EV margin after estimated fees/slippage for retrades.
- `require_price_improvement_for_same_market_family`: optional guard for tighter rebuys.
- `price_improvement_ticks`: optional minimum price improvement requirement.

## Required Shared Data
The decision path must have access to:
- `event_key`
- open same-event positions
- current event exposure
- open event position count
- candidate bracket / overlap metadata
- estimated round-trip trading economics

## Shared-Core Decision Rules

### 1. Build event snapshot before approval
For the candidate signal:
- derive `event_key`
- load unresolved positions sharing that `event_key`
- compute:
  - `event_position_count`
  - `event_exposure_before`
  - `event_headroom`
  - held market ids
  - same-event overlap hints

### 2. Hard rejection rules
Reject if any of the following are true:
- exact `market_id` was already traded and is still unresolved
- `event_position_count >= max_event_positions`
- `event_exposure_before >= max_event_exposure_pct * current_balance`
- proposed size would exceed event headroom and clipped size falls below minimum position size
- strict overlap policy determines the new bracket is effectively redundant

### 3. Stronger retrade threshold
If this is a retrade, require:
- `edge >= min_edge + retrade_edge_premium`
- `confidence >= min_confidence + retrade_confidence_premium`

Initial v1 formula is explicit and deterministic:

```text
required_retrade_edge = min_edge + retrade_edge_premium
required_retrade_confidence = min_confidence + retrade_confidence_premium
```

This ensures later entries must be stronger than initial entries.

### 4. Event-aware sizing
Calculate normal Kelly size first.
Then adjust for retrades:

```text
size_after_decay = base_kelly_size * retrade_size_decay ^ existing_same_event_positions
size_after_headroom = min(size_after_decay, event_headroom)
```

If overlap policy returns a penalty, apply it before headroom clipping.

### 5. Fee-aware retrade viability
For retrades, estimate whether the trade still clears minimum economic quality after:
- expected entry price
- estimated slippage / spread impact
- fee model
- size reduction from decay and headroom clipping

Reject retrade if the estimated net edge or minimum expected net profit falls below threshold after those adjustments.

Important: a trade that looked good before decay/slippage should not be retraded if the remaining economics are no longer attractive.

### 6. Reasoning metadata
Every approved or rejected retrade should include reasoning fields:
- `retrade: true|false`
- `event_key`
- `event_position_count_before`
- `event_exposure_before`
- `event_headroom`
- `retrade_edge_threshold`
- `size_decay_applied`
- `overlap_penalty`
- `fee_aware_net_edge`
- `retrade_reason`

## Overlap Policy

### Strict overlap mode
Initial v1 policy:
- exact same `market_id` → reject
- same event and same normalized suffix family → reject
- same event but adjacent / distinct bracket with meaningful different payoff → allow

Implementation must be deterministic, not heuristic-only.

Initial v1 rule set:
- exact duplicate `market_id` is blocked
- exact duplicate normalized bracket suffix inside the same event is blocked
- other same-event markets are allowed for v1 unless future category-specific geometry says otherwise

This keeps v1 conservative and explainable while avoiding fuzzy blocker behavior.

## Paper Path
Paper execution should:
- preserve existing fill slippage behavior
- persist retrade metadata into session trade rows
- persist `event_key` explicitly
- ensure analytics can report event concentration and retrade activity

## Live Path
Live execution should use the same approval logic before order placement.
Live state must expose event-aware open positions and pending duplicate exposure so scanning can avoid redundant buys.

Important live requirements:
- pending open positions and resting orders for the same event should count toward duplicate-event controls when possible
- same-event concurrent candidates should not both pass independently and overrun the cap
- v1 should serialize or re-check event exposure immediately before order placement

## Analytics / Reporting Changes
Reporting should add:
- resolved event count
- event win rate
- average positions per resolved event
- open event concentration
- retrade count
- top concentrated events

Reports should present event-level performance as primary and position-level performance as secondary.

## Implementation Plan

### Phase 1: shared-core and risk foundation
- add config loading in `bot/risk.py`
- add event snapshot helpers
- add retrade rule evaluation helper
- thread event metadata into shared decision reasoning

### Phase 2: paper implementation
- make paper context include event snapshot inputs
- persist retrade metadata on execution
- update analyzer/reporting for event-aware output

### Phase 3: live implementation
- expose event-aware live open-position and pending-order state
- apply identical shared-core retrade controls before place_order
- log retrade metadata in live trade history / lifecycle events

### Phase 4: tests
- unit-test event caps
- unit-test stronger retrade threshold
- unit-test fee-aware rejection after decay/slippage
- unit-test duplicate same-event rejection
- unit-test parity between paper and live decision path

## Non-Goals for v1
- full portfolio payoff curve optimization across many brackets
- dynamic hedging between YES and NO on same event
- advanced event-family geometry for every market category

Those can come later once event-aware retrading is stable.

## Open Questions
- how aggressive should overlap detection be outside weather ranges?
- should resting live orders count as event positions immediately or as reserved pending exposure?
- should third same-event trade require an even larger premium than the second?

## Recommended v1 Answers
- yes, count resting orders as pending same-event exposure in live
- yes, keep overlap policy conservative and deterministic
- no extra per-step premium yet beyond decay plus event cap plus stronger base retrade threshold
- treat opposite-side same-event entries as blocked for v1 unless explicitly designed as hedges later
