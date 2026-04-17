# No-Price Weather Training Design

## Purpose
Support historical weather archives that have resolved outcomes but no usable YES/NO prices, without mixing that data into trading P&L learning.

## Current State
- `bot/weather/training.py` only emits training samples when `yes_price` is present, so no-price history is dropped.
- The current trainer calibrates per-city/source trust by shrinking market price toward `0.5` and minimizing Brier loss against the resolved outcome.
- `bot/weather/replay.py` already separates quiz payloads from answer keys and can still score direction correctness when prices are missing, but P&L fields become unavailable.

## Recommendation
Split weather learning into two explicit modes that share a normalized example schema:

1. `structural` mode
   Learn event direction, source reliability, and calibration from resolved weather markets, even when price is absent.
2. `price_aware` mode
   Learn whether and how to trade once a structural probability is compared against a live market price.

Do not treat no-price history as a weaker version of price-aware replay. Treat it as a different training target.

## Shared Example Shape
Every training example should have a structural core plus an optional price block.

Structural fields:
- `market_id`
- `series_ticker`
- `city_id`
- `source_id`
- `market_type`
- `question`
- `event_date`
- `observed_at`
- `resolved_at`
- `outcome`
- `threshold_value` or normalized target bucket from subtitle/question
- `question_side` such as `above`, `below`, `range`, `binary_bucket`
- `sample_kind`
- `lead_time_hours` when known
- compact normalized weather evidence from observation logs when available

Optional price-aware fields:
- `yes_price`
- `no_price`
- `volume`
- `liquidity`
- `spread`
- `time_to_close_hours`
- `fees`
- `model_probability` at decision time
- `model_edge` versus market price
- chosen `action`
- `position_size` or `contracts`
- realized P&L fields

## Mode 1: Structural / Direction Learning
Use this mode for:
- historical archives with outcome but no price
- replay sets where the goal is prediction quality, not trading quality
- source trust updates

Target:
- estimate `p_yes_structural`
- evaluate whether a source or source-family helps predict the resolved outcome

Trainer behavior:
- group by `(city_id, source_id, market_type)`
- hold out by date blocks, not random row shuffle
- score only on resolved outcome and calibration
- never compute or report trading P&L in this mode

Recommended metrics to store:
- `sample_size`
- `unique_days`
- `coverage_by_market_type`
- `coverage_by_lead_time_bucket`
- `direction_accuracy`
- `brier`
- `log_loss` if probabilities are available
- `calibration_error`
- `skip_rate` or `unscored_rate`
- `holdout_brier`
- `holdout_accuracy`
- `outcome_base_rate`
- `missing_timestamp_rate`
- `missing_city_mapping_rate`

Recommended outputs:
- candidate `trust_score` updates
- source notes such as strong city coverage, poor calibration, or weak timeliness
- structural model checkpoints or probability summaries

## Mode 2: Price-Aware Trading Learning
Use this mode only when the example has a valid pre-resolution price snapshot aligned to the decision timestamp.

Target:
- decide `BUY_YES`, `BUY_NO`, or `SKIP`
- optionally size the trade

Inputs:
- structural probability from mode 1
- current market price context
- execution assumptions such as fees and fill model

Trainer behavior:
- consume only rows with usable price context
- compare `p_yes_structural` to `yes_price`
- optimize trading thresholds and skip discipline, not source trust directly
- keep structural weights frozen during a price-aware run unless a separate reviewed update is approved

Recommended metrics to store:
- `answers_scored`
- `buys`
- `skipped`
- `win_rate`
- `accuracy`
- `gross_pnl`
- `fee_adjusted_pnl`
- `average_edge`
- `realized_ev_gap`
- `position_size_total`
- `max_drawdown`
- `pnl_per_trade`
- `calibration_vs_price`
- `side_balance`
- `trade_rate_by_edge_bucket`
- `holdout_fee_adjusted_pnl`

Recommended outputs:
- threshold recommendations
- size policy recommendations
- skip-policy recommendations
- reviewer-facing evidence for whether structural predictions are translating into tradable edge

## Safe Use Of No-Price Archive Data
- Allow no-price rows in `structural` mode only.
- Reject no-price rows from any metric that implies trade quality, edge, or P&L.
- Never invent synthetic prices for training. Deriving `no_price` from `yes_price` is fine when one side exists; inventing both sides is not.
- Split train and holdout by event date or market day to reduce leakage across same-day contracts.
- Deduplicate by market identity and normalized bucket so one series does not dominate.
- Down-weight or exclude rows where the timestamp is clearly post-resolution or missing enough context to know what was knowable before close.
- Keep archive-derived trust updates bounded and reviewable, exactly like current dry-run candidate updates.

## How Live Training Complements No-Price Learning
Historical no-price data should teach the model what tends to happen.
Live priced data should teach the agent when that belief is worth trading.

Recommended live loop:
1. Train or refresh structural probabilities from the larger no-price archive.
2. During live or paper trading, log the structural probability, market prices, time to close, and selected action for each decision.
3. After resolution, score both:
   - structural quality: was the probability directionally correct and calibrated?
   - trading quality: did the price-aware policy create positive fee-adjusted value?
4. Use live priced outcomes to tune entry thresholds, skip rules, and sizing while leaving structural learning mostly anchored by the larger historical set.

This gives the system a clean division:
- archive data improves forecasting priors and source trust
- live priced data improves trade selection

## Suggested Trainer Refactor
Refactor the current trainer into three layers:

1. Canonical example builder
   Normalize `WeatherSampleRecord` into a `WeatherTrainingExample` with `structural_context` plus optional `price_context`.
2. Structural trainer
   Accepts any resolved example, including no-price rows, and emits source-quality and calibration reports.
3. Price-aware trainer
   Accepts only examples with price context plus structural model output and emits trading-policy reports.

The current `run_temperature_training` logic becomes the first price-aware trainer pass, not the universal trainer.

## Next Implementation Slice
Keep the next slice small and reviewable:

1. Add a mode-explicit training doc/schema in code terms:
   define a canonical example object with required structural fields and optional price fields.
2. Introduce a `run_structural_training(...)` path:
   reuse current grouping and holdout logic, but remove the `yes_price` requirement and score resolved direction/calibration only.
3. Rename or wrap the existing trainer as `run_price_aware_training(...)`:
   keep its current trust-from-price behavior isolated behind explicit price requirements.
4. Extend live logging:
   persist `model_probability`, `yes_price`, `no_price`, `time_to_close_hours`, and final action so price-aware learning has real training rows later.

This slice preserves existing behavior while making no-price historical data immediately usable.
