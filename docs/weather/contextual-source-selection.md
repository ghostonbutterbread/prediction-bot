# Opt-in contextual weather source selection

This bridge is disabled unless explicitly enabled under `strategy`. It does not
enable a runtime, publish history, place orders, or change wallet/risk policies.

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

`strategy_config_with_policy` passes the strategy block to
`EnhancedStrategyEngine`; enabled mode passes only this selection block to
`LiveFeedAggregator`. Legacy/disabled mode still constructs the aggregator with
no arguments, preserving existing callers and test doubles.

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
correctness evidence, not forward performance or calibration evidence. Local
adapter coverage remains the collection builder's documented two verified
numeric adapters, with the other 22 city sources explicitly blocked; this bridge
does not add or claim parsers for them.
