# Prediction Lab Long-Run Collection Spec

## Goal
Turn Prediction Lab into a safe long-running market data farming system for paper research.

Initial use case:
- run on selected market groups such as `weather` or `sports`
- repeatedly score markets over time
- record prediction opportunities and market snapshots
- dedupe open predictions by market
- resolve matured predictions later
- analyze long-run signal quality without requiring live capital deployment

This mode is for research and aggregation first, not execution.

## What already exists

### Existing capabilities
Prediction Lab already supports:
- group filtering via `prediction_lab.groups`
- weather and sports groups
- configurable `max_markets_per_run`
- prediction recording
- market snapshot recording
- open-prediction dedupe by `market_id`
- resolution pass for matured predictions
- summary reporting
- config fields for collection intervals and storage caps

### Existing entrypoints
- `scripts/prediction_lab_run.py` — one scoring pass
- `scripts/prediction_lab_resolve.py` — one resolution pass
- `scripts/prediction_lab_report.py` — one summary pass

## Current gap
The current UX is not yet a true long-running collector.

What is missing is a thin orchestration layer that:
- loops collection at a configured interval
- loops resolution at a configured interval
- watches storage usage
- supports pausing cleanly
- optionally hot-reloads config changes
- emits clear status and pause reasons

This is an orchestration gap, not a missing-core-logic gap.

## Desired operator behavior

### Example operator goals
- run weather collection continuously
- run sports collection continuously
- cap collection storage at 25 GB
- pause cleanly via config change
- stop manually if needed
- resume later without duplicate open predictions

## Desired design

### Mode separation
Prediction Lab should support these operator modes clearly:

1. `seed_and_watch`
   - do a bounded prediction seed pass
   - stop after `max_new_predictions_per_seed`
   - wait for future resolution

2. `collector`
   - repeatedly scan and record predictions/snapshots on an interval
   - suitable for long-run data farming

3. `resolve_only`
   - only resolve matured predictions

4. `collector_daemon` (new orchestration concept)
   - a wrapper process around collector mode
   - handles repeated collect + resolve loops + pause logic

## Live control requirements

### Pause control
We want three stop mechanisms:
1. config pause
2. storage cap pause
3. manual process stop

### Config pause
Prediction Lab should support a live-reloadable pause field, for example:

```yaml
prediction_lab:
  enabled: true
  paused: false
```

Behavior:
- if `paused: true`, collector daemon stops running collection loops
- it records `paused_reason = "manual_pause"`
- optionally still allows resolve-only passes if desired

### Storage-cap pause
Prediction Lab should stop collecting when storage cap is reached.

For current testing target:
- `collection_storage_cap_gb: 25`

Behavior:
- warn before cap at configured threshold
- if cap is exceeded and auto-pause is enabled, set pause reason to `storage_cap`
- stop new collection passes
- keep existing data intact

### Manual stop
If the process is manually stopped:
- state should remain durable
- future restart should continue from the same ledger/state

## Required config shape

Recommended config additions / clarifications:

```yaml
prediction_lab:
  enabled: true
  paused: false
  mode: collector
  groups: [weather]
  max_markets_per_run: 1000
  continue_collecting: true
  collector_interval_seconds: 900
  resolve_interval_seconds: 1800
  collector_record_market_snapshots: true
  collector_record_predictions: true
  collection_storage_cap_gb: 25
  collection_warning_threshold_pct: 90
  auto_pause_collection_on_storage_cap: true
  score_only: true
```

### Notes
- `paused` should be hot-reloadable
- `continue_collecting` means keep running repeated collector passes
- `mode=collector` selects collector semantics
- `score_only=true` means research mode, not execution mode

## Required state fields
Prediction Lab state should make operator status obvious.

Current state already includes:
- `mode`
- `last_run_id`
- `paused_reason`
- `open_prediction_count`
- `resolved_prediction_count`

Recommended additions:
- `paused`
- `last_collect_at`
- `last_resolve_at`
- `last_storage_check_at`
- `storage_usage_bytes`
- `storage_usage_gb`
- `warning_emitted`

## Current dedupe behavior
Prediction recording already dedupes open predictions by `market_id`.

Current behavior:
- if a prediction for a market is already open, a new open prediction is not appended
- market snapshots may still be recorded repeatedly when enabled

This is good and should be preserved.

## Long-run data model

### Canonical logs
Prediction Lab should keep two primary research ledgers:

1. `predictions.jsonl`
   - one row per open prediction opportunity that was admitted
   - later rewritten to mark rows resolved

2. `market_snapshots.jsonl`
   - repeated observations of the market over time
   - useful for repeated-opportunity and drift analysis

### Resolution ledger
3. `resolutions.jsonl`
   - append-only record of resolved prediction outcomes

## Questions this should answer
Long-run collection should help answer:
- did we skip good trades?
- did repeated opportunities improve?
- did we overtrade or undertrade?
- which categories produce signal without execution?
- how often did the same market reappear before resolution?
- which groups produce the strongest confidence-calibrated outcomes?

## Needed analysis improvements
The current summary is useful but not yet complete for the desired research workflow.

Recommended analysis additions:
- repeated-market appearance counts from `market_snapshots.jsonl`
- open-vs-resolved breakdown by market group
- series-level breakdown by category / family
- predicted-but-not-recorded counts when thresholds reject signals
- skipped-opportunity quality analysis
- calibration by confidence bucket and market group
- category-specific storage usage metrics

## Orchestration spec

### New wrapper script
Recommended new script:

`scripts/prediction_lab_collect.py`

Responsibilities:
- load config
- instantiate Prediction Lab
- loop forever while enabled and not paused
- run collection pass on `collector_interval_seconds`
- run resolution pass on `resolve_interval_seconds`
- periodically evaluate storage usage
- hot-reload config before each cycle
- stop or pause cleanly based on config/state

### Loop pseudocode
```text
while true:
  reload config
  if disabled: exit cleanly
  if paused: sleep and continue
  if storage cap exceeded and auto-pause enabled:
    set paused_reason=storage_cap
    sleep and continue

  if collect_due:
    run PredictionLab.run(exchange)

  if resolve_due:
    run PredictionLab.resolve_open_predictions(exchange)

  sleep short interval
```

## Hot reload requirement
This collector wrapper should hot-reload config between cycles.

At minimum, these should reload without restart:
- `prediction_lab.paused`
- `prediction_lab.enabled`
- `prediction_lab.groups`
- `prediction_lab.max_markets_per_run`
- `prediction_lab.collector_interval_seconds`
- `prediction_lab.resolve_interval_seconds`
- `prediction_lab.collection_storage_cap_gb`

## Safety / rate-limit requirements
- keep exchange fetch limits bounded per run
- respect configured `max_markets_per_run`
- no tight polling loops
- use configured collection interval
- prefer one collect pass per interval rather than nested retry storms

## Recommendation for initial test run
For your requested first test:

```yaml
prediction_lab:
  enabled: true
  paused: false
  mode: collector
  groups: [weather]
  max_markets_per_run: 1000
  continue_collecting: true
  collector_interval_seconds: 900
  resolve_interval_seconds: 1800
  collector_record_market_snapshots: true
  collector_record_predictions: true
  collection_storage_cap_gb: 25
  collection_warning_threshold_pct: 90
  auto_pause_collection_on_storage_cap: true
  score_only: true
```

Recommended first run target:
- weather only first
- then sports second after validating log shape and storage growth

## Build priority

### Phase 1
- add `paused` config support
- build `prediction_lab_collect.py` wrapper
- add storage-cap usage checks + auto-pause
- add state/status visibility

### Phase 2
- add better report outputs for repeated opportunities and skipped-opportunity analysis
- add optional notifications on pause/warning/resume

### Phase 3
- consider multi-group continuous runs with per-group summaries

## Conclusion
Prediction Lab already has the core data model for long-run collection.
What is missing is the operator layer:
- continuous collector loop
- hot-reloadable pause
- storage-cap enforcement
- clearer long-run analysis outputs

That is a focused, buildable next step.
