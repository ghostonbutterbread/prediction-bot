# Market Router + Strategy Lane Spec

_Last updated: 2026-05-05_

## Purpose

Prevent paper/live trades from leaking across market categories, then make strategy lanes category-aware without duplicating the shared EV/risk/execution core.

The immediate bug this spec addresses: a non-weather market such as `KXPRIMEENGCONSUMPTION-30-WIND` can be treated as weather because broad keyword classification treats `wind` as weather-like. During weather-focused paper/live runs, this must fail closed.

Core rule:

> No market route, no trade.

## Design goals

1. Fail closed before paper/live trade entry unless a market matches an explicitly enabled route.
2. Keep the current shared root decision path for EV, confidence, Kelly sizing, risk, retrade checks, and execution feasibility.
3. Move category-specific interpretation into route handlers.
4. Support current weather daily-temperature markets first.
5. Leave a clean extension path for sports, crypto, energy, and other future categories.
6. Make Prediction Lab, paper, and live use the same route identity so research cannot silently diverge from execution.

## Non-goals

- Do not implement the full confidence/edge/hidden-gem lane split in this pass.
- Do not add new sports or energy trading logic in this pass.
- Do not allow broad keyword matching to approve trades.
- Do not use route labels as proof of profitability; routes only decide whether the bot is allowed to reason about the market.

## Current implementation review

A delegated implementation review identified these relevant paths:

- `bot/market_classification.py`
  - `WEATHER_KEYWORDS` includes broad terms like `wind`.
  - `classify_market()` returns `market_group="weather"` when any weather keyword appears in category/question/series/event ticker/market id.
  - `_infer_weather_family()` can assign `family="wind"`.
- `bot/exchanges/kalshi.py`
  - `_market_allowed()` accepts any market classified into an allowed market group.
  - `_is_weather_series_ticker()` also treats series tickers containing weather tokens like `wind` as weather-like.
- `bot/prediction_lab.py`
  - `PredictionLab.run()` applies classification metadata and checks `_group_allowed(market_group)`.
  - `_group_allowed()` checks group-level allowance, not family/subcategory-level allowance.
- `bot/shared_core/decision.py`
  - Weather risk activates if classification says weather or source metadata says weather.
  - `assess_weather_market_risk()` can return `shape="unknown"` without rejecting the trade.
- Paper/live entry points:
  - `bot/simulator.py` around paper trade creation.
  - `bot/runner.py::_process_signal()` before `live_execution.build_trade_context(...)`.
  - `bot/live_execution.py` immediately before live order placement as defense-in-depth.

## Reliable current weather daily-temperature identifiers

For current weather trading, prefer strict structural identifiers over loose natural-language keywords.

A market is current-supported `weather.daily_temperature` only if it has strong evidence such as:

1. Series/category/ticker prefix starts with one of:
   - `KXHIGH`
   - `KXHIGHT`
   - `KXLOW`
   - `KXLOWT`
   - `KXMINTEMP`
2. Temperature-market semantics are present, for example:
   - question references high/low/minimum/maximum temperature
   - question includes degrees/temp/temperature
   - market shape is parseable as tail high, tail low, or bucket/range
3. Prediction Lab direct-series metadata for current rows commonly includes:
   - `group: weather`
   - `series: KXHIGH...` / `KXLOW...`
   - `market_group: weather`
   - `market_family: daily_temperature`
   - `source: direct_series`
   - `event_ticker: <series>-<date>`

Unreliable identifiers:

- `wind` alone
- `high` or `low` alone
- `market_group="weather"` without a supported family/subcategory
- any generic keyword match that does not prove the market belongs to a supported route

## Proposed route model

Add `bot/market_router.py` with side-effect-free routing.

Suggested data shape:

```python
@dataclass(frozen=True)
class MarketRoute:
    allowed: bool
    group: str                     # weather, sports, crypto, energy, unknown
    family: str | None             # daily_temperature, game_winner, etc.
    subcategory: str | None        # tail_high, tail_low, bucket, etc.
    handler_id: str | None         # weather.daily_temperature.v1
    reason_code: str
    evidence: dict[str, Any]
```

Suggested API:

```python
def route_market(market_or_signal: Any, config: Mapping[str, Any]) -> MarketRoute:
    ...

def route_allowed(route: MarketRoute) -> bool:
    return route.allowed
```

Configuration should allow explicit families, not just groups:

```yaml
scan:
  allowed_market_routes:
    - weather.daily_temperature
  allowed_market_groups:
    - weather   # backward-compatible broad scan hint only, not execution approval
```

Execution approval should use `allowed_market_routes`. `allowed_market_groups` can remain a scanning/fetch hint for backward compatibility, but must not by itself approve a trade.

## Current first supported route

### `weather.daily_temperature`

Allow if:

- route is configured as allowed
- series/category/market id has a daily-temperature prefix
- question/category metadata supports a temperature market
- route handler can classify shape as one of:
  - `tail_high`
  - `tail_low`
  - `bucket`

Reject if:

- route is unknown
- family is unknown
- only evidence is a loose keyword such as `wind`
- question/series disagree with the route
- active config does not include the route

Route evidence should include:

- `series_ticker`
- `event_ticker`
- `market_ticker` / `market_id`
- `question`
- `classification_reason`
- `prefix_match`
- `temperature_semantics_match`
- `shape`

## Handler architecture

The market router should choose a category handler before strategy scoring.

```text
market/signal
  -> MarketRouter
  -> CategoryHandler
  -> shared TradeContext
  -> build_trade_decision
  -> paper/live execution adapter
```

Handlers own category-specific parsing and data-source requirements.

Examples:

```text
weather.daily_temperature
  -> weather data handler
  -> high / low / bucket / tail parser
  -> weather directional gate later
  -> shared EV/risk/execution core

sports.game_winner
  -> sports data handler
  -> team/opponent/odds/injury parser later
  -> sports-specific side/confidence gate later
  -> shared EV/risk/execution core
```

The shared root should keep owning:

- edge math
- confidence thresholds
- Kelly / position sizing
- retrade/event exposure logic
- fee-aware checks
- execution feasibility
- paper/live lifecycle behavior

Category handlers should own:

- route eligibility
- required data source checks
- subcategory/shape parsing
- category-specific risk overlays
- category-specific directional interpretation

## Required enforcement points

Add the route check in all paper/live paths, with defense in depth:

1. **Exchange/fetch filtering**
   - Tighten `bot/exchanges/kalshi.py::_is_weather_series_ticker()`.
   - Tighten `bot/exchanges/kalshi.py::_market_allowed()` to require route/family where possible.
2. **Prediction Lab collection**
   - In `bot/prediction_lab.py`, require configured route/family in addition to group.
   - Store route metadata in prediction rows and market snapshots.
3. **Shared decision path**
   - In `bot/shared_core/decision.py`, reject missing/disallowed route metadata before sizing/risk approval.
4. **Paper execution path**
   - In `bot/simulator.py`, block disallowed route before creating a paper trade.
5. **Live runner path**
   - In `bot/runner.py::_process_signal()`, block disallowed route before `build_trade_context` / `build_trade_decision`.
6. **Live execution adapter**
   - In `bot/live_execution.py`, block disallowed route before any `place_order` call.

## Backward compatibility

- Existing broad `market_group` values can remain in rows for reporting.
- New rows should include route metadata, for example:

```json
"market_route": {
  "allowed": true,
  "group": "weather",
  "family": "daily_temperature",
  "subcategory": "tail_high",
  "handler_id": "weather.daily_temperature.v1",
  "reason_code": "allowed_weather_daily_temperature",
  "evidence": {"series_ticker": "KXHIGHTPHX", "shape": "tail_high"}
}
```

- Legacy rows without route metadata should be treated as analysis-only unless backfilled by deterministic route inference.
- Do not mark legacy route inference as original execution evidence.

## Test requirements

### Classification/router tests

Add or extend `tests/test_market_classification.py` / `tests/test_market_router.py`:

- `KXPRIMEENGCONSUMPTION-30-WIND` is rejected under weather-only config.
- Energy question `What will be the largest source of global primary energy consumption in 2030?` is not weather.
- `KXHIGHTPHX-26MAY06-T88` routes to `weather.daily_temperature` / `tail_high`.
- `KXLOWTDEN-26MAY06-B28.5` routes to `weather.daily_temperature` / `bucket`.
- Loose `wind` keyword does not classify as supported weather unless a future meteorological wind route is explicitly configured.

### Exchange filtering tests

Extend `tests/test_kalshi_direct.py`:

- Weather-only direct pull accepts daily-temperature series prefixes.
- Weather-only direct pull rejects energy `WIND` series.
- Unknown route is not accepted even if `allowed_market_groups` includes weather.

### Prediction Lab tests

Extend `tests/test_prediction_lab_collect.py`:

- Weather-only Prediction Lab records/permits daily temperature markets.
- Weather-only Prediction Lab rejects non-weather energy/wind market with stable reason code.
- Route metadata appears in market snapshots and prediction rows.

### Shared decision tests

Extend `tests/test_shared_core_decision.py`:

- Missing `market_route` fails closed for paper/live mode.
- Disallowed route fails before Kelly/risk approval.
- Allowed `weather.daily_temperature` route preserves current approved decision behavior.

### Paper/live tests

Extend:

- `tests/test_risk_and_simulator.py`
- `tests/test_runner_status_and_live_path.py`
- `tests/test_live_execution.py`

Required cases:

- Paper blocks disallowed route before trade creation.
- Live runner blocks disallowed route before execution adapter call.
- Live execution adapter blocks disallowed route before order placement.

## Implementation sequence

1. Add `MarketRoute` and `route_market()` in `bot/market_router.py`.
2. Add route config normalization in `bot/config.py`.
3. Tighten weather classification to distinguish:
   - broad `weather` interest
   - supported `weather.daily_temperature` execution route
4. Store route metadata in Prediction Lab artifacts/rows.
5. Add shared-core fail-closed route gate.
6. Add paper/live defense-in-depth gates.
7. Add tests proving the energy/wind market cannot trade.
8. Only after this lands, implement the strategy-lane split:
   - edge lane
   - confidence lane
   - hidden-gem lane

## Acceptance criteria

This pass is complete when:

- Weather-only config cannot trade `KXPRIMEENGCONSUMPTION-30-WIND`.
- Current daily temperature markets still route and score normally.
- Prediction Lab snapshots include route metadata.
- Paper/live/shared-core all fail closed on missing or disallowed route.
- Targeted tests pass for classification, Prediction Lab, paper, live, and shared-core routing.
- No new strategy-lane behavior is introduced before route safety is verified.

## Phase 2 strategy-lane split

After route safety, the shared core may select a strategy lane for decision
metadata and optional explicitly configured threshold behavior. The route
handler/category decision remains separate: `market_route` still decides whether
the market may enter shared core at all, and `strategy_lane` only interprets the
already-normalized trade economics.

Supported lane IDs:

- `edge` — default non-cheap-contract path using the existing configured
  `min_edge`, `min_confidence`, max-entry, EV/risk, sizing, and execution gates.
- `hidden_gem` — cheap-contract path selected when entry price is at or below
  the existing hidden-gem entry cap; the existing hidden-gem edge and probability
  multiple gates still live in shared core.
- `confidence_slow_profit` — optional high-confidence, lower-edge lane. It is
  inert unless `strategy_lanes.enabled: true`,
  `strategy_lanes.confidence_slow_profit.enabled: true`, and explicit lane
  thresholds are configured. Config normalization treats that explicit sub-lane
  enablement as allowlist inclusion for `confidence_slow_profit`; defaults still
  omit it from `enabled_lanes`.

Default config keeps lanes as metadata only:

```yaml
strategy_lanes:
  enabled: false
  enabled_lanes:
    - edge
    - hidden_gem
  confidence_slow_profit:
    enabled: false
    min_edge:
    min_confidence:
```

When defaults are used, approvals and rejections remain equivalent to the
pre-lane edge/confidence/hidden-gem behavior. New lane behavior requires an
explicit config change and is recorded in `decision.reasoning["strategy_lane"]`.
