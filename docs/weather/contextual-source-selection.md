# Contextual weather source selection and collector defaults

**Status:** implementation candidate; release and collector activation verification in progress.
**Owner:** Prediction Bot / Hermes; canonical specification is this file.
**Supersedes:** the opt-in-only deployment description in earlier revisions of this file.
**Implementation:** inherited collection/selection checkpoint `4b7ee3e4fadc4855faa4564dabb8ad64ecbf82a6`; collector-default continuation pending commit.

## Required default workflow

The observer collector must implement **collect and retain → independently score → compare prior contextual history → select → pass the selected numeric forecast**. Collection is not gated on the winning source, purchased positions, source trust label, or minimum history count. Every configured supported global/city forecast is attempted independently; unsupported or unavailable sources retain explicit reasons. Station/current observations remain context, not forward forecasts or settlement authority.

The collector daemon defaults contextual selection on only when its effective `prediction_lab` settings are `observer_mode: true` and `mode: collector`. Explicit `strategy.weather_source_selection` settings, including `enabled: false`, win. This does not change nonobserver paper/live defaults or activate wallets/orders. With insufficient or missing history the collector continues retaining source evidence; selection abstains instead of falling back to unrelated actionable odds.

The default history metadata locator is `<effective collector root>/data/derived_reports/auto_source_router_history/current/source_observations/run_metadata.json`. The effective root follows the existing environment > runtime.storage_root > collector-volume precedence. Default selection policy remains 30 prior independent events minimum, 100 maximum per compatible source/location/kind/shape slice. A history publication clock cannot replace resolver knowledge time.

### Release acceptance

- Preserve original forecast values, exact target date/location/period semantics, request/source/capture clocks, failures, losing and nonselected sources, raw payload hashes and verified compact references.
- Independently finalized outcomes score all eligible retained predictors, including nontrades; only `known_after < current decision time` can inform selection. Duplicate polls are not extra independent events; equal empirical support abstains.
- Demonstrate actual collector → sealed input → independent binding/materialization → later contextual source choice → changed numeric forecast/probability → unchanged price/risk gates using bounded offline disagreement fixtures.
- Execute producer-free separate-process retained-data readback, real storage-failure/clean-retry, and all 19 hotfix/weather-veto function regressions. Preserve commands/results and integrity receipts on persistent derived storage, not only auto-pruned scratch.
- Verify the configured resolver/history publication handoff separately from collection and raw archive growth; do not claim automatic learning from an inactive publisher or a missing metadata locator.
- Independently review the exact integrated/default tree and run the full offline suite, explicitly excluding production-archive probes. Before activation, record safe rollback, capacity/budget coverage including capture storage, and exactly one collector owner. After activation, read back a fresh retained multisource snapshot and the effective process/config/paths, with trading disabled.

## Explicit configuration override

The settings below illustrate an explicit opt-out. Loading them does not enable a runtime, publish history, place orders, or change wallet/risk policies.

```yaml
strategy:
  weather_source_selection:
    enabled: false
    history_metadata_path: /path/to/derived/source-history/run_metadata.json
    min_sample_count: 30
    max_history_events: 100
    # Optional freshness policy in seconds; absent means no extra age cutoff.
    # max_history_age_seconds: 86400
```

`history_metadata_path` must name the actual metadata emitted by
`materialize_source_observation_ledger`, with independently bound finalized
outcomes and original strict resolver rows. The bridge uses
`load_predictive_source_history`, including its settled-byte hash/count checks;
it does not synthesize accuracy, use purchased-position-only history, or read a
full raw collector archive. History is loaded per weather decision, so a newer
materialization is not hidden by a permanent process-local history cache.

## Concrete consumer path

`PredictionLab` resolves the existing effective collector root (environment
`PREDICTION_BOT_COLLECTOR_ROOT` > `runtime.storage_root` > canonical collector
volume) once, constructs one `WeatherCaptureStore` with that explicit absolute
root, and injects the same instance through `EnhancedStrategyEngine` ->
`LiveFeedAggregator` -> `ProWeatherEngine` and its global/local adapters.
Standalone strategy/aggregator callers may inject `capture_store=` explicitly;
without injection they retain the existing collector-root default. Relative
configured roots are rejected; no checkout-relative evidence root is inferred.
Disabled source selection still uses the configured store: persistence is not
an opt-in selection feature.

1. The existing ProWeatherEngine collects relevant global and city candidates
   independently and scores its legacy forecast to expose the captured source
   details. No sample threshold gates collection.
2. The bridge feeds those recorded source rows through the canonical
   `build_replay_decision_input_v1(..., source_history_only=True)` allowlist,
   lifting recorded clocks and forecast type via the same small projection used
   by PredictionLab. Captured city labels use the collector registry's city ID
   even when the exchange metadata lacks city enrichment. This is a **derived,
   pre-persistence, self-sealed current projection**, not a raw archive locator
   or proof that raw bytes have already been durably stored.
3. `select_contextual_weather_source` compares prior independent city/kind/shape
   events. Only `known_after < decision_time` is eligible. Default policy is at
   least 30 events and up to 100 per source. Current source target, location,
   fetch/source clocks and predictor proof still have to qualify. Partial-period
   predictors are not relabelled full-day observations.
4. A routeable choice is passed to the existing engine's `selected_source_id`
   seam, which scores the **same cached collection** with that source's numeric
   forecast. Stable ID `open_meteo` maps to engine provider `open-meteo`;
   `src_local_kvvu_weather` passes through unchanged. Competing source captures
   remain present. The probability receipt continues to describe the existing
   heuristic (`heuristic_weather_distance_v1`, calibration `not_established`),
   not the source's historical win rate. Existing ensemble probabilities remain
   labelled heuristic as well.
5. PredictionLab retains the selection receipt and source period/type/location/
   clocks through its source snapshot into the sealed replay projection.
   Existing quote, fee, entry-price, exposure, sizing and wallet gates remain
   unchanged.

Missing/unreadable/corrupt history, a generation changing during load,
insufficient history, invalid selection settings, or no usable current
predictors are observer-only. The captured weather signal remains in the trace
when available; neither NWS legacy odds nor an unrelated price/news ensemble may
buy instead. If the weather engine returns no signal at all, the strategy also
fails closed with `contextual_weather_observer_only` and
`no_current_weather_signal`, without inventing a weather observation.

If `max_history_age_seconds` is configured, freshness is measured from the most
recent prior `known_after` among the actual contextual comparison's selected
history references; unrelated cities and future/equal-time outcomes do not
refresh it. A stale comparison reports `stale_predictive_history`. No implicit
calendar-age default was added to the 30/100 event policy.

## Routing, unavailable evidence, and retention failures

Collection and the enabled observer boundary use the same market scope:
canonical `route_market` daily-temperature identity, or genuine question-only
temperature terms/units. Canonical identity is **not** permission to score an
unsupported question. In particular, an identified `KXHIGHLV` question using
`85 F` may remain unsupported by the numerical scorer and must not fall through
to price-only trading when contextual selection is enabled. Unrelated college
qualifications (`college degrees`) and financial high/low/below wording do not
become weather contracts; their matching-API controls remain unchanged.

If collection returns `collection_only: true`, `prediction_available: false`,
`confidence: 0`, both strategy APIs stop **before numerical validation or
ensemble scoring, whether selection is enabled or disabled**. The traced reason
is `weather_collection_only`; raw/rejected weather evidence retains every
source attempt, unavailable reason and compact capture/collection reference.
The contextual selector returns a nonrouteable receipt without projecting a
numerical input or opening history. It does not invent `predicted_prob`,
`direction`, calibration or a price-based weather opinion. PredictionLab's
serialized source snapshot preserves these flags, the explicit availability
reason and collection receipt, without adding absent odds. Successful forecasts
and their existing probability/value, fee, sizing and risk gates are unchanged.

`CaptureRetentionError` propagates through the strategy boundary in either mode
and API. Persistence/integrity failures are not ordinary provider-unavailable
results and cannot authorize a fallback. Original source bytes remain in the
configured store; Lab artifacts contain compact references, not raw-body copies.

Offline consumer coverage is in `tests/test_weather_capture_consumer.py`, using
actual Lab construction, mocked HTTP for independently failing NWS/Open-Meteo/
local transports, verified receipt/body rehydration, JSONL snapshot roundtrip,
and configured/effective root identity checks. Replay allowlist verification and
the corrected combined/full-suite release gate remain separate requirements.

## Bounded audit

`data.weather_source_selection` contains chosen stable ID/value, routeability and
blockers, effective policy, prior count/rank, per-source wins/losses/count/rate,
up to three representative observation IDs per source, the materialization
metadata generation SHA-256, and current derived-input/projection hashes and
clock. It intentionally excludes the full history and duplicate current source
pool. The collector's original source details remain the evidence owner; the
receipt hashes do not claim raw archive provenance.

Offline regression: `tests/test_contextual_weather_source_flow.py` uses 30
independent Las Vegas event fixtures, independently bound YES/NO resolutions,
real history materialization/loading, actual mocked-HTTP NWS and KVVU
collection, aggregator and both strategy paths, PredictionLab snapshot/replay,
and the actual composed wallet's unchanged entry-price rejection. It is fixture
correctness evidence, not forward performance or calibration evidence. This
bridge adds no adapters by itself. The combined implementation wires 20 local
adapter candidates through the configured registry without test overrides. Four
other local sources retain documented date/location/access limitations;
WLS-low remains unavailable and TEGNA hourly lows remain explicitly partial.
Parser success alone is not live coverage, independent provider skill, or
upstream-provider independence. Registry/parser and full consumer/replay tests
must remain part of the combined release gate.
