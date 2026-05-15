# Market Router + Strategy Lane Spec

_Last updated: 2026-05-07_

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

- Do not add new sports or energy trading logic in this pass.
- Do not allow broad keyword matching to approve trades.
- Do not use route labels as proof of profitability; routes only decide whether the bot is allowed to reason about the market.
- Do not treat the 2026-05-06 hotfix as final lane strategy. It is a safety bridge that blocks weak penny weather buckets until the evidence-card work exists.

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

## Current branch state — 2026-05-06

The active `feature/weather-strategy-lanes` branch already has an initial lane-selection implementation in progress:

- `bot/strategy_lanes.py` selects `edge`, `hidden_gem`, and optional `confidence_slow_profit` lanes.
- `bot/shared_core/decision.py` records `decision.reasoning["strategy_lane"]` and can reject disabled lanes before Kelly/risk sizing.
- Default lane config is metadata-preserving: `edge` and `hidden_gem` stay enabled, while `confidence_slow_profit` is inert unless explicitly enabled.
- The 2026-05-06 hotfix on `main` / runtime added a paper safety bridge in `bot/strategies/enhanced.py` and `paper_loop.py`: cheap weather bucket hidden-gems require `distribution_probability`; cheap tail hidden-gems can be vetoed when validated live weather strongly rejects the candidate side; paper can fail closed on unusable news/feed state.

Important branch rule:

> Continue lanes from the current branch state, but fold the hotfix intent into the lane/evidence-card design instead of preserving it as an unrelated strategy-layer special case forever.

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
  `strategy_lanes.confidence_slow_profit.enabled: true`, explicit lane
  thresholds are configured, and the beta feature flag
  `strategy_policy.beta.features.confidence_slow_profit` is active. Config
  normalization treats explicit sub-lane enablement as allowlist inclusion for
  `confidence_slow_profit`; defaults still omit it from `enabled_lanes`.

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

## Phase 3 evidence-card lane expansion

Phase 3 should convert the hotfix into durable lane/evidence-card logic.

The immediate target is the `hidden_gem` lane for `weather.daily_temperature` markets. The goal is not to remove hidden gems; it is to keep 24x-style opportunities possible only when the evidence explains why the market is wrong.

### Phase 3A prerequisite: beta-gated rollout wiring

The 2026-05-06 `feature/policy-beta-config` scaffold is now merged into `main` and available to this branch through `config["strategy_policy_normalized"]`.

Before adding more lane behavior, Phase 3 must first wire lane/weather-risk behavior through `strategy_policy` so stable behavior can remain comparable and reversible:

- `strategy_policy.version: stable` or `beta.mode: off` must preserve old/stable decision behavior except for explicitly accepted safety scaffolding such as route identity metadata.
- `strategy_policy.version: beta` with `beta.mode: shadow` should compute and record beta lane/evidence-card decisions next to the stable decision, but must not change final paper/live/live-like action.
- `strategy_policy.version: beta` with `beta.mode: enforce` may allow beta lane/evidence-card logic to affect paper decisions after shadow data is reviewed.
- live should remain stable until explicit promotion.
- `strategy_lanes.enabled` and lane-specific config are not enough by themselves for behavior-changing rollout; behavior-changing lane/weather-risk gates must also check the normalized strategy policy mode and relevant feature flag.

Initial feature-flag mapping:

```yaml
strategy_policy:
  version: beta
  beta:
    mode: shadow   # off | shadow | enforce
    features:
      weather_hidden_gem_evidence_card: true
      bucket_distribution_scoring: true
      hidden_gem_lane_gates: true
      confidence_slow_profit: true
      lane_sizing_caps: true
```

Recommended implementation order:

1. Add policy helpers/adapters near the decision pipeline so callers can ask whether beta lane logic is `off`, `shadow`, or `enforce`.
2. Preserve a stable decision artifact and add a beta/shadow decision artifact for comparison.
3. Gate hidden-gem evidence-card rejection, bucket distribution rejection, tail bridge rejection, exceptional hidden-gem rejection, weather sizing caps/multipliers, and `confidence_slow_profit` admission behind beta policy checks before they become behavior-changing defaults. `confidence_slow_profit` uses its own feature flag so confidence-lane shadowing can be reviewed independently from hidden-gem lane gates.
4. Make Prediction Lab emit stable-vs-beta deltas before paper uses `enforce`.
5. Add tests proving stable/off preserves old behavior, shadow records beta differences without changing final action, and enforce can change paper decisions only when the relevant feature flag is enabled.

### Hidden-gem lane evidence card

For each hidden-gem candidate, record at least:

- `market_route` and weather shape (`tail_high`, `tail_low`, `bucket`)
- entry price and probability multiple
- model probability / candidate-side probability
- source mode: recorded, live, historical, synthetic, or missing
- official station mapping quality (`exact`, `inferred`, `missing`)
- source agreement and source freshness
- market volume/liquidity and whether it is known
- for buckets: `distribution_probability`, forecast mean, forecast spread/uncertainty, bucket center, bucket width, and distance to center
- for tails: threshold probability mass and distance to threshold
- reason code for approve / reject / resize

Phase 3B implementation note, 2026-05-06:

- shared-core now emits `decision.reasoning["hidden_gem_evidence_card"]` for weather hidden-gem candidates after weather assessment
- the card is observability-only in stable/off and beta/shadow; beta gate and sizing deltas are copied from the existing gated weather-risk metadata
- this slice does not add new rejection rules beyond existing beta/enforce weather-risk gates
- Prediction Lab/shared decision artifacts preserve the card through their existing `shared_core_decision.reasoning` payload

Phase 3C implementation note, 2026-05-06:

- hidden-gem evidence cards are now summarized by `weather_shape x hidden_gem_tier x reason_code`
- reports include final approvals/rejections, beta-rejection counts, no-card legacy rows, and incomplete-card rows
- `scripts/analyze.py` and Prediction Lab summary paths share the same reporting helper
- this slice is reporting-only and does not alter strategy selection, trade decisions, sizing, risk checks, or order behavior

Phase 3D implementation note, 2026-05-06:

- Prediction Lab replay summaries now include `weather_hidden_gem_comparison` with separate `strict` and `coverage` slices
- the pre-hotfix/hotfix/evidence-card comparator is `artifact_derived_conservative`: recorded final action is used as the pre-hotfix proxy, hotfix bridge rejection is inferred only from stable bridge reason codes, and evidence-card behavior is read from `hidden_gem_evidence_card`
- the report includes bad bucket buys removed, winners skipped by the hotfix bridge, approvals/rejections by `weather_shape x hidden_gem_tier x reason_code`, bucket rows with/without `distribution_probability`, and the `entry_price + 0.05` / `3x entry_price` threshold slices
- this slice is reporting-only and does not alter strategy selection, trade decisions, sizing, risk checks, runtime config, or order behavior

Phase 3E implementation note, 2026-05-06:

- bucket hidden-gem distribution scoring now rejects beta/enforce candidates unless `distribution_probability >= entry_price + 0.05` and `distribution_probability >= 3x entry_price`
- the new rejection reasons are `weather_bucket_hidden_gem_distribution_probability_below_entry_plus_buffer` and `weather_bucket_hidden_gem_distribution_probability_below_multiple`
- stable/off and beta/shadow preserve final actions while recording the beta gate and hidden-gem evidence-card deltas; beta/enforce can change paper-like decisions only when `bucket_distribution_scoring` is enabled
- this slice changes only beta-gated bucket hidden-gem behavior and leaves stable defaults/live promotion untouched

Phase 3F implementation note, 2026-05-07:

- bucket hidden-gem distribution scoring now also requires minimum source/station evidence quality before beta/enforce approval
- the first gate requires exact station mapping and `source_agreement_score >= 0.65`; failures use `weather_bucket_hidden_gem_source_station_quality_below_minimum`
- stable/off and beta/shadow preserve final actions while recording beta gate and hidden-gem evidence-card deltas; beta/enforce can change paper-like decisions only when `bucket_distribution_scoring` is enabled
- this slice keeps the behavior feature-gated, does not promote live behavior, and does not relax bucket sizing/caps

Phase 3G implementation note, 2026-05-07:

- tail hidden-gem scoring now records whether the candidate-side probability check used `distribution_probability` or the live-probability bridge
- when tail `distribution_probability` is present, beta/enforce with `weather_hidden_gem_evidence_card` can reject candidates below the configured tail threshold with `weather_tail_hidden_gem_distribution_probability_below_threshold`
- when tail `distribution_probability` is missing, the existing bridge behavior remains in place and continues to use `weather_tail_hidden_gem_live_probability_mismatch`
- stable/off and beta/shadow preserve final actions while recording beta deltas and evidence-card tail probability-scoring details

Phase 3H implementation note, 2026-05-07:

- strategy-lane config now accepts metadata-only `strategy_lanes.sizing` entries for known lanes with optional `size_multiplier`, `max_position_usd`, and `max_position_pct`
- shared-core records selected-lane sizing metadata after Kelly as `decision.reasoning["lane_sizing"]`, including whether the metadata would have adjusted the requested size
- this slice does not apply lane-specific caps or multipliers in stable, shadow, or enforce mode; actual sizing behavior remains owned by the existing Kelly, weather-risk, event, and risk-policy paths

Phase 3I implementation note, 2026-05-07:

- `strategy_policy.beta.features.lane_sizing_caps` is the dedicated feature flag for applying selected-lane sizing caps
- stable/off and beta/shadow preserve final requested/approved sizes and only record `lane_sizing` deltas, including the beta-adjusted size that would have reached risk
- beta/enforce applies explicit selected-lane `strategy_lanes.sizing` reductions before `risk_policy.check_trade`; caps fail closed by clamping to non-negative values and never increasing the post-weather/event requested size
- this slice does not promote live behavior or introduce runtime config changes; defaults remain unchanged

Phase 3J implementation note, 2026-05-07:

- `confidence_slow_profit` remains inert by default and still requires explicit `strategy_lanes.enabled`, `strategy_lanes.confidence_slow_profit.enabled`, lane thresholds, and beta/enforce `confidence_slow_profit` before it can change final admission thresholds
- explicit slow-profit config plus beta/shadow `confidence_slow_profit` records `strategy_lane.evidence.beta_lane_gate` would-select metadata without changing the final selected lane or final action
- `bot.strategy_lane_reporting`, `scripts/analyze.py`, and Prediction Lab replay summaries report selected lanes, would-select lanes, lane-selection deltas, and how often slow-profit would differ from the final stable action
- this slice is observability/reporting-focused except for preserving the already beta/enforce-gated slow-profit admission path

Phase 3K implementation note, 2026-05-07:

- strategy-lane reporting now also summarizes `lane_sizing` cap deltas from direct rows, paper/live `decision_trace`, and `decision_artifact.shared_core_decision.reasoning`
- the compact report includes configured lane-sizing rows, would-adjust rows, applied rows, preserved/shadow rows, selected sizing-lane counts, and safe requested/beta/applied size totals and averages
- `scripts/analyze.py` and Prediction Lab replay summaries receive the sizing-delta view through the existing shared `bot.strategy_lane_reporting` helper, including original-vs-replayed replay slices
- this slice is observability-only and does not change strategy selection, trade admission, sizing, risk checks, or order behavior

### Bucket hidden-gem direction

The 2026-05-06 paper/archive check showed cheap bucket rows were the clearest danger:

- current paper cheap open buckets were mostly negative, and the hotfix would have rejected 7 open trades while improving marked P&L by about `$1.45`
- resolved Prediction Lab cheap bucket rows checked during the hotfix review had no observed wins in the sampled resolved set, despite many old model probabilities implying 20x–40x opportunities

Therefore bucket hidden-gems should not pass from point forecasts alone.

Proposed first durable bucket approval rule:

```text
entry_price <= 0.05
AND distribution_probability is present
AND distribution_probability >= entry_price + 0.05
AND distribution_probability >= 3 * entry_price
AND source/station evidence passes minimum quality gates
```

This still allows true 24x-style opportunities. Example:

```text
entry_price = 0.01
distribution_probability = 0.24
multiple = 24x
edge = +0.23
=> eligible for hidden_gem evidence-card approval, subject to source/station/size caps
```

It rejects the old failure mode:

```text
entry_price = 0.03
point forecast near bucket
no distribution_probability
=> reject: bucket_missing_distribution_probability
```

Initial bucket sizing should remain capped even when approved. Treat bucket approval as a carefully budgeted lottery lane, not normal sizing.

### Tail hidden-gem direction

Tail markets can remain more permissive than buckets, but Phase 3 should stop relying only on point-forecast confidence.

Tail hidden-gem approval should use candidate-side probability mass when available. If distribution scoring is not yet available, tails may use the current hotfix-style veto as a bridge:

- reject cheap tails when strong live weather evidence says the candidate side is very unlikely
- do not reject solely from weak/stale/inferred evidence
- record whether the decision used full distribution scoring or bridge logic

### Phase 3 acceptance criteria

- Stable/off config preserves pre-beta final decisions, except for explicitly accepted route-safety scaffolding.
- Shadow config records beta lane/evidence-card decisions and stable-vs-beta deltas without changing the final paper/live-like action.
- Enforce config can change paper decisions only when `strategy_policy.version: beta`, `beta.mode: enforce`, and the relevant feature flag are enabled.
- `hidden_gem` decisions emit an evidence card in shared-core reasoning and Prediction Lab artifacts.
- Bucket hidden-gems without `distribution_probability` fail closed with a stable reason code in beta/enforce, and appear as beta rejections in shadow.
- Bucket hidden-gems with strong distribution probability can still pass and are clearly capped/sized.
- Prediction Lab can report approvals/rejections by `shape x hidden_gem_tier x reason_code`.
- Replay can compare stable/pre-hotfix, hotfix bridge, and evidence-card logic on the same recorded artifacts.
- Paper/live behavior remains route-gated and defaults stay conservative until enough clean-label rows accumulate.
