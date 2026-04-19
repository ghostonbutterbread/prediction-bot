# Live Trading Spec v1

Status: draft
Scope: first controlled live rollout after paper trading baseline is committed and running

## Goals
- preserve current paper-trading flow as the stable baseline
- add explicit live-mode controls without breaking paper mode
- keep Kelly sizing, but bound it with wallet and position caps
- prioritize weather-market risk containment over realized-P&L-only controls
- add operator visibility through periodic status logging and alerts

## Non-goals for v1
- automatic cancellation of open orders on pause
- flattening existing positions on pause
- full unrealized drawdown engine
- autonomous capital scaling
- remote admin command surface

## Operating assumptions
- first live rollout starts with a small funded wallet, for example $10
- Kelly sizing remains the primary sizing engine
- weather markets can stay unresolved across day boundaries, so realized daily loss is not the primary safety control for this strategy at first
- paper mode should remain runnable while live-mode features are being built and tested

## Key decisions

### 1. Keep Kelly sizing, do not force fractional Kelly in v1
For this rollout, use full Kelly if configured by the operator.

Reasoning:
- initial capital is intentionally tiny
- Ryushe wants to evaluate live execution behavior before scaling capital
- the stronger safety layer for weather v1 is not fractional Kelly, but hard caps around usable capital and exposure

Implementation note:
- keep Kelly configurable so we can later reduce it if needed
- do not hardcode quarter-Kelly or half-Kelly as mandatory in live mode

### 2. Add a max tradable balance cap
This is the primary live safety control.

Example:
- wallet balance: $50
- max tradable balance: $10
- sizing logic behaves as if only $10 is available for trading

Benefits:
- keeps blast radius small
- lets the account hold more funds than the strategy may currently use
- works cleanly with Kelly sizing

### 3. Add a hard max position size cap
Kelly output must be clipped by a hard per-position cap.

Example:
- Kelly wants to size to $7.80
- max position size is $5.00
- final size becomes $5.00

This protects against:
- sizing bugs
- bad probability inputs
- exchange rounding surprises
- paper/live behavior mismatches

### 4. Use pause/resume through one boolean flag
Use one config value:

```yaml
trading_enabled: true
```

Behavior:
- true: bot may open new positions
- false: bot does not open new positions

Future:
- later add `cancel_open_orders_on_pause`
- later add external command/control surface if wanted

### 5. Keep realized loss limit support, but do not rely on it as the primary weather safeguard
Weather markets often resolve after the day they are entered.

So for v1:
- keep realized daily loss limit support in code/config
- treat it as secondary protection
- prioritize caps on tradable balance, per-position size, and total exposure

## Required config additions

Suggested config shape:

```yaml
trading:
  mode: paper  # paper | live
  trading_enabled: true

risk:
  kelly_fraction_live: 1.0
  max_tradable_balance_usd_live: 10.0
  max_position_size_usd_live: 5.0
  max_exposure_pct_live: 1.0
  max_open_positions_live: 10
  max_daily_realized_loss_usd_live: 5.0
```

Notes:
- `mode` should be explicit instead of inferred only through environment behavior
- `kelly_fraction_live` can remain configurable and default to 1.0 if that is the chosen rollout stance
- `max_tradable_balance_usd_live` is the most important new field
- `max_position_size_usd_live` is the second most important new field
- `max_exposure_pct_live` should still exist as a portfolio backstop
- `max_daily_realized_loss_usd_live` should exist even if not the main control for weather

## Sizing logic
For every candidate trade in live mode:
1. check `trading_enabled`
2. compute effective tradable bankroll as:
   - min(actual available cash, `max_tradable_balance_usd_live`)
3. compute Kelly size using the effective tradable bankroll
4. clip final size by:
   - `max_position_size_usd_live`
   - remaining exposure headroom
   - available cash
5. if final size is below minimum executable size, skip the trade and log the reason

Pseudo-logic:

```python
effective_bankroll = min(available_cash, max_tradable_balance_usd_live)
kelly_size = kelly(probability, price, effective_bankroll)
final_size = min(
    kelly_size,
    max_position_size_usd_live,
    remaining_exposure_capacity,
    available_cash,
)
```

## Risk priorities for weather v1
Order of importance:
1. trading enabled flag
2. max tradable balance cap
3. hard max position size
4. total exposure cap
5. max open positions
6. realized daily loss limit

This reflects the fact that unresolved weather trades make same-day realized loss less informative than open-risk containment.

## Logging and status reporting

### Required lifecycle events
- bot online
- bot offline
- mode changed
- trading paused
- trading resumed
- risk block triggered
- trade placed
- trade skipped
- error occurred

### Hourly status summary
Every hour, aggregate activity and emit one summary update.

Suggested content:
- mode
- trading enabled/paused state
- effective tradable cap
- current available cash
- reserved/open capital
- open positions count
- trades opened in the last hour
- notable skips or risk blocks
- errors in the last hour

Example:

```text
Bot status update
mode=paper
trading_enabled=true
tradable_cap=$10.00
available_cash=$8.40
reserved_capital=$1.60
open_positions=2
new_trades_last_hour=3
risk_blocks_last_hour=1 (position cap)
errors_last_hour=0
```

### Alert frequency
Make status cadence configurable, for example:

```yaml
alerts:
  enabled: true
  status_update_interval_minutes: 60
  send_startup: true
  send_shutdown: true
  send_trade_events: true
  send_error_events: true
```

Future tuning can reduce noise without changing core logic.

## Suggested implementation phases

### Phase 1: stabilize current paper state
- verify current paper-trading changes are in a good commit state
- avoid mixing live-trading work into unreviewed paper edits
- keep a clean branch point for live work

### Phase 2: add structured status logging while still in paper mode
- startup/shutdown messages
- hourly summaries
- trade and skip reason aggregation
- error aggregation

This gives immediate visibility and tests the reporting layer before live orders exist.

### Phase 3: add explicit live-mode config surface
- add `trading.mode`
- add `trading_enabled`
- add new live risk fields
- ensure old env behavior does not silently override the new config in confusing ways

### Phase 4: wire live sizing safeguards
- effective tradable bankroll cap
- hard per-position cap
- portfolio exposure cap integration
- pause gate before order placement

### Phase 5: controlled live rollout
- fund small amount
- set low tradable cap
- run for about a week
- compare live behavior versus paper expectations
- only then consider increasing balance/caps

## Deferred todo
- cancel open orders on pause
- cancel open orders on daily stop
- unrealized drawdown / mark-to-market stop logic
- admin command for pause/resume without manual config edit
- richer channel formatting for bot status topic
- per-market-type risk presets

## Current repo observations
At time of drafting, the repo already appears to have:
- paper/live risk presets in config and `bot/risk.py`
- a session-level drawdown halt
- persistent paper simulator state
- hourly summary logging in `paper_loop.py`

That means v1 is not starting from zero. The main missing pieces seem to be:
- a clean explicit live config surface
- wallet-level tradable balance cap
- hard live position cap in dollars
- clearer operator-facing status/alert delivery
- a clean branch/commit boundary from current paper work into live work
