# Live Trading Spec v1

Status: draft
Scope: first controlled live rollout after paper trading baseline is committed and running

## Goals
- preserve current paper-trading flow as the stable baseline
- add explicit live-mode controls without breaking paper mode
- keep Kelly sizing, but bound it with wallet and position caps
- prioritize weather-market risk containment over realized-P&L-only controls
- add operator visibility through periodic status logging and alerts
- make core trading controls and operator visibility shared between paper and live wherever possible

## Non-goals for v1
- automatic cancellation of open orders on pause
- flattening existing positions on pause
- full unrealized drawdown engine
- autonomous capital scaling
- remote admin command surface

## Production-readiness gap list
Before calling live trading production-ready for unattended real-money operation, we still want:
- strict paper/live runtime state isolation so simulated history cannot contaminate live drawdown, P&L, restart state, or audit trails
- fuller live order lifecycle handling
  - open-order status refresh hardening
  - partial-fill progression handling
  - clearer cancel/requery/replace behavior when needed
- stronger live settlement and reconciliation hardening against exchange edge cases
- stronger operator-facing observability and alerts for important lifecycle events
- small supervised real-money validation runs that confirm exchange truth matches internal logs/account state
- continued extraction of live-specific API-backed behavior into dedicated modules while keeping shared decision/risk/status logic centralized
- first-class operator verbosity controls so normal usage stays concise while supervised/debug modes expose full context

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

Important pause semantics:
- pause does **not** cancel existing resting orders
- pause does **not** ignore already-live positions
- while paused, reconciliation, settlement checks, and resolution logging should continue
- pause means "stop opening new trades", not "stop observing current exposure"

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

## Shared-core requirement
The long-term direction for v1 is not separate paper logic and live logic that merely look similar.

Instead:
- trade gating should be decided in one shared decision path
- risk controls should be computed in one shared risk path
- status snapshots and status formatting should be produced from one shared status path
- paper and live should differ mainly at the adapter boundaries that provide state and execute orders

Desired split:
- shared core:
  - signal normalization
  - entry-price normalization
  - Kelly sizing input calculation
  - `trading_enabled` gate
  - effective tradable bankroll cap
  - hard position cap
  - exposure cap / open-position cap checks
  - stable skip/block reason codes
  - status snapshot structure
  - status message formatting
- paper adapter:
  - paper account state
  - simulated fill/reserve/release behavior
  - paper session metadata
- live adapter:
  - exchange-backed account state
  - order placement / order status / cancellation behavior
  - live open-order and partial-fill state

Rule for implementation going forward:
- if a new control or status field is intended to exist in both paper and live, implement it in shared logic first unless there is a concrete reason it must be adapter-specific

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

Minimum important lifecycle alerts for supervised single-trade mode:
- trade placed
- single-trade completed (no further entries)
- resolved market / settlement event

These events provide an operator-readable state trail even when the user is not actively watching logs in real time.

## Operator alerts + verbosity model
Separate two concerns:
- `alerts`: which event categories should be emitted
- `verbosity`: how much detail emitted alerts should contain

### Configuration
Suggested config shape:

```yaml
alerts:
  enabled: true
  trade_events: true
  single_trade_events: true
  resolution_events: true
  reconciliation_events: false
  scan_summaries: false

verbosity:
  level: normal   # normal | verbose | double_verbose
```

### CLI overrides
- default: normal
- `-v` = verbose
- `-vv` = double verbose

CLI flags should override the configured verbosity level for the current run only.

### Telegram delivery design
- lifecycle/log events remain the source of truth
- a Telegram formatter layer shapes those events for user-facing delivery
- this lets us refine message formatting over time without changing core bot logic
- formatter output should be designed so another supervising agent can understand what happened without reading raw log files
- delivery should support routing to a dedicated Telegram group topic, not only DMs
- Telegram delivery is expected to be enabled by default for operator visibility during active runs

Suggested routing fields:
```yaml
alerts:
  telegram_enabled: true
  telegram_channel: telegram
  telegram_target: "-1003763915138"
  telegram_thread_id: "8"
```

Current intended destination:
- Ghost's Home
- topic: `bot-status`
- topic id: `8`

### Normal-mode notifications
Normal alert content should stay concise but informative.

Important: periodic lifecycle/status logs, including hourly summaries, should flow through the same Telegram notification path. A separate parallel periodic-status system is not required if the existing lifecycle/hourly event stream is already being mirrored to Telegram cleanly.

Required design behavior:
- hourly summaries should be emitted as normal lifecycle/status events
- those events should be formatted by the same formatter layer
- those formatted messages should be delivered by the same Telegram notifier path
- alert-category filtering should still apply, using a dedicated `status_events` category

Required normal payloads:
- **trade placed**
  - side / direction
  - market question or compact label
  - amount committed
  - confidence
  - entry price
  - updated balance snapshot
  - reserved / in-market amount when relevant
- **single-trade completed**
  - confirm no further new entries will be taken
  - confirm monitoring/resolution will continue
- **resolved market**
  - market label
  - outcome
  - realized P&L
  - updated balance snapshot

### Verbosity levels
- `normal`: key user-facing status only
- `verbose`: adds more decision/risk/reconciliation context
- `double_verbose`: adds deeper scan/dependency/lifecycle detail for supervision and debugging

### Verbose content expectations
`verbose` may include:
- why this trade was selected
- compact risk-clipping context
- reconciliation summary
- pause/resume/mode-change events
- available cash / reserved capital / tradable cap snapshot

`double_verbose` may include:
- top candidate comparisons
- blocker reasons for skipped trades
- deeper lifecycle details
- richer reconciliation/account-state details
- per-scan summaries when enabled

### Design rule
The formatter layer should be the place where message shape evolves.
Do not hardcode Telegram-specific prose deep in the trading logic.
The trading system should emit stable lifecycle/status events, and the formatter should decide how to present them for each verbosity level.

## Runtime data isolation
Paper and live must not share mutable runtime state.

Required structure:
```text
data/
  paper/
    risk_state.json
    lifecycle.jsonl
    notifications.jsonl
    hourly_summary.jsonl
    scans_*.jsonl
    ...
  live/
    risk_state.json
    lifecycle.jsonl
    notifications.jsonl
    hourly_summary.jsonl
    scans_*.jsonl
    ...
```

This isolation applies to at least:
- risk state
- trade history / execution logs
- lifecycle logs
- notification logs
- scan summaries
- restart/reconciliation artifacts

Reason:
- paper simulations must never create fake live drawdowns or fake live P&L
- live state must never inherit simulated history
- audit trails must remain mode-correct and trustworthy

## Config posture
The committed default config should be safe and neutral.

Recommended default posture:
- `config.yaml` = paper-first baseline
- optional supervised live config in a separate file, for example `config.live_supervised.yaml`
- operator intentionally opts into that file via explicit config-path selection, for example `--config config.live_supervised.yaml`

## Runtime supervising-agent todo
Future work: define a runtime supervising agent that watches active runs and improves operator leverage without becoming noisy or dangerous.

Questions to answer:
- what it should observe during paper vs live runs
- what it should summarize into operator-facing updates
- what it may recommend vs what it may change automatically
- how it should help tune thresholds/verbosity/alerts over time
- what hard safety boundaries it must never cross during live trading

## Minimum trade economics
Exchange-valid is not enough. A trade should also be economically meaningful.

Recommended controls:
```yaml
trade_economics:
  min_position_size_usd: 1.0
  min_expected_net_profit_usd: 0.10
```

Meaning:
- reject trades below exchange/practical size floor
- reject trades whose expected net profit after fees is too small to matter
- this prevents technically valid but economically pointless low-dollar trades

### Supervised single-trade live test mode
A useful pre-production mode is a supervised one-trade run:
- scan markets
- choose the best approved candidate
- place at most one trade
- then stop opening any further trades
- continue reconciliation / settlement / logging for that exposure afterward

This mode should be available as a first-class CLI flag for both paper and live so the same operator habit works in both environments.

This is useful for validating:
- order placement
- post-trade balance refresh
- reconciliation on later checks
- settlement/result logging
- cron-driven operational flow

Intent:
- this is not full live automation
- this is a controlled bridge between paper confidence and unattended live trading
- paper single-trade mode validates selection and single-entry behavior
- live single-trade mode validates API placement, balance refresh, reconciliation, and later settlement logging

## Suggested implementation phases

### Phase 1: stabilize current paper state
- verify current paper-trading changes are in a good commit state
- avoid mixing live-trading work into unreviewed paper edits
- keep a clean branch point for live work

### Phase 2: finish shared status + shared decision direction in paper
- keep `bot/status.py` as the shared status/notification formatting layer
- keep shared decision and risk evaluation in the shared path
- ensure new fields added for operator visibility are defined once and reusable by both modes
- continue using paper as the first proving ground, but do not let it become a paper-only architecture

### Phase 3: add explicit live-mode config surface
- add `trading.mode`
- add `trading_enabled`
- add new live risk fields
- ensure old env behavior does not silently override the new config in confusing ways
- make mode/config source of truth explicit instead of relying mainly on `PAPER_MODE`

### Phase 4: wire the actual live runner into the shared path
- route live trade evaluation through the same shared decision + risk path used by simulator/paper adapters
- route live status output through shared snapshot + formatter logic
- add a real `main.py status` command for the main runner
- ensure the current live/runner path does not bypass tradable-balance or position-cap protections

### Phase 5: complete operator lifecycle visibility
- startup/shutdown messages
- hourly summaries
- trade and skip reason aggregation
- risk block events
- error aggregation
- pause/resume visibility

This gives the same operator-facing surface in both paper and live.

### Phase 6: controlled live rollout
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
- shared status formatting in `bot/status.py`
- shared decision/adapters groundwork for the simulator path
- hourly summary logging in `paper_loop.py`

That means v1 is not starting from zero. The main missing pieces seem to be:
- a clean explicit live config surface
- live runner integration with the same shared decision/risk path already protecting simulator flow
- `main.py status` implementation for the main runner
- fuller shared lifecycle/status event coverage across paper and live
- a clean commit boundary once the working slice is actually solid

## Known architecture gap to close
Previous behavior was asymmetric:
- simulator/shared-core paper flow got the new `trading_enabled`, tradable-balance, and max-position protections
- the existing main/live runner path still had legacy direct execution behavior and could bypass those protections

Current status after the latest implementation slice:
- the main runner now routes live trade approval through the same shared decision + risk path before order placement
- the main runner now exposes a shared-formatted `main.py status` view
- the main runner now tracks shared blocker reasons in scan results/logs
- the analyzer path now works when invoked directly from cron or scripts without needing manual `PYTHONPATH=.` bootstrapping
- noisy `httpx` request logging is now suppressed at the main runner entrypoint so operator logs stay readable
- the daily paper-trading cron has been patched to degrade gracefully when direct posting is unavailable instead of failing hard
- runner lifecycle visibility now includes:
  - hourly summary logging in `hourly_summary.jsonl`
  - startup / stop-requested / shutdown events in `lifecycle.jsonl`
  - per-block risk event logging in `risk_blocks.jsonl`
- runtime control/config resolution is cleaner:
  - `trading.mode` is now the preferred source of truth for paper vs live
  - canonical operator pause/resume control is now `trading.enabled`
  - legacy `trading.trading_enabled` and top-level `trading_enabled` still fall back for backward compatibility
  - env fallback still exists when explicit config is absent
- the repo now has a standard local-environment bootstrap path:
  - `./setup.sh` creates or reuses `.venv`
  - dependencies install into the repo-local virtual environment
  - README now documents `.venv` as the expected execution environment for future agents/operators

Still not done yet:
- pause/resume visibility now exists through config-driven runtime events:
  - runner watches the configured `config.yaml` path during the live loop
  - changes to `trading.enabled` emit `trading_paused` / `trading_resumed`
  - changes to `trading.mode` emit `mode_changed`
  - pause behavior explicitly leaves existing resting orders untouched and only stops new trade approvals
- live reload now targets the canonical runtime control section instead of relying on env-only changes
- live account/order state has started moving into a dedicated live adapter boundary:
  - `bot/live_adapters.py` now holds live state snapshots and reconciliation logic
  - `bot/live_execution.py` now holds live execution-specific account-context building and order placement flow
  - runner now delegates reconciliation/state shaping/execution instead of owning all live normalization directly
  - this is not finished yet, but the live module boundary now exists and is under test
- live position reconciliation now exists as a first startup pass:
  - `connect_all()` reconciles exchange-reported open positions into runner memory
  - reconciled positions are mirrored into `trade_history` with `reconciled: true`
  - risk/account state is resynced from reconciled open exposure
  - lifecycle logs now emit `reconciliation_completed` or `reconciliation_failed`
- live reconciliation now also covers open orders:
  - exchange-reported resting orders are loaded into runner `open_orders`
  - reserved capital now includes both open positions and remaining resting-order size
  - reconciliation summary reports open-order count and partial-fill count
- remaining reconciliation gaps:
  - no order-cancel / replace workflow tied to reconciliation findings yet, by design resting orders are currently left alone on pause
  - deeper partial-fill handling has started through adapter-level resting-order state plus remaining-size accounting, but it is not a full execution lifecycle yet
  - reconciled entry price quality still depends on exchange position/order data fidelity
  - no persistence layer beyond the fresh exchange snapshot on connect
- live settlement/result reconciliation has started:
  - adapter-level settlement checks now emit resolution events from exchange market truth
  - runner removes resolved positions and records P&L into trade history/risk state
  - this still needs broader real-exchange hardening to match paper maturity
- live post-trade accounting is moving toward exchange truth instead of paper-style simulation:
  - after live execution, runner refreshes account state from the exchange-backed balance
  - reserved capital is derived from reconciled positions plus resting-order remainder, not handcrafted simulated wallet math
  - this keeps live behavior lighter and more API-native than paper mode
- paused-state semantics are now an explicit production requirement:
  - paused must still allow reconciliation and market resolution handling
  - paused must still keep logs/status current for already-open exposure
  - paused only disables new entries
- architectural direction is now explicit:
  - shared decision/risk/status logic stays centralized in runner/shared core
  - live-specific API-backed behavior is being extracted into dedicated modules that build on that shared logic
  - paper and live are intended to look structurally similar at the boundaries, not duplicate the core decision engine

Definition of done for this gap:
- paper and live both derive trade approvals from the same shared decision/risk logic
- paper and live both populate the same core status snapshot fields
- paper and live both expose operator status through the same formatter
- adapter-specific differences stay confined to environment state, order execution, and settlement mechanics
