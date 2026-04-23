# Prediction Lab Long-Run Collection V1 Spec

## Goal
Build a narrow, safe first version of long-run Prediction Lab collection for paper research.

This v1 is intentionally limited.
It should be robust enough to run unattended for one market group, collect useful data, pause safely, and survive restart without duplicating open predictions or forgetting why it stopped.

## Initial scope
V1 supports:
- one active collector process per data directory
- one market group at a time, starting with `weather`
- repeated collection passes
- repeated resolution passes
- config-driven manual pause
- storage-cap pause
- restart-safe state
- append-only market snapshots
- open-prediction dedupe by `market_id`

V1 does NOT try to solve:
- multi-group orchestration in one process
- advanced re-entry logic for the same market
- complex resolution dispute handling
- rich reporting beyond collector-operability basics

## Operator model
The operator has exactly three stop mechanisms:
1. manual pause in config
2. storage-cap auto-pause
3. manual process stop

## Formal state model
Use explicit collector state, not a vague paused flag.

V1 separates:
- `mode` = operator-selected workflow intent
- `run_state` = current runtime status
- `pause_reason` = why the process is paused, if paused

### Allowed mode values
- `seed_and_watch`
- `collector`
- `resolve_only`

### Allowed run_state values
- `active_collect`
- `active_resolve`
- `paused`
- `idle_watch`
- `completed`
- `errored`

### Allowed pause_reason values
- `manual_pause`
- `storage_cap`
- `disabled`
- `none`

### State meanings
- `active_collect`: actively doing collection passes
- `active_resolve`: actively resolving open predictions
- `paused`: collection is not progressing because an explicit pause condition is active
- `idle_watch`: not collecting right now, but waiting for future resolution checks
- `completed`: no open predictions and no further collection expected for the current mode
- `errored`: process encountered a non-recoverable error and exited

### Transition rules
- `active_collect -> active_resolve` when resolve becomes due and open predictions exist
- `active_collect -> paused` when `paused=true`, `enabled=false`, or storage cap pause triggers
- `active_collect -> idle_watch` when mode is `seed_and_watch`, seeding is complete, and open predictions remain
- `active_collect -> completed` when no further collection is expected and no open predictions remain
- `active_resolve -> paused` when a pause condition is active after the resolve pass
- `active_resolve -> idle_watch` when open predictions remain but no collection is due or allowed
- `active_resolve -> completed` when no open predictions remain and no further collection is expected
- `paused -> active_collect` when pause condition is cleared and collection is due
- `paused -> active_resolve` when pause condition is cleared and resolve is due first
- any state -> `errored` on unrecoverable lock/state corruption or fatal startup failure

## Required persisted state fields
Prediction Lab state file should include at least:
- `mode`
- `run_state`
- `pause_reason`
- `last_run_id`
- `open_prediction_count`
- `resolved_prediction_count`
- `last_collect_at`
- `last_resolve_at`
- `last_storage_check_at`
- `storage_usage_bytes`
- `storage_usage_gb`
- `warning_emitted`
- `active_group`
- `last_error`
- `seed_complete`
- `experiment_id`
- `strategy_version`

## Config shape for v1
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

## V1 interpretation rules
- `groups` must contain exactly one active group in v1
- if more than one group is configured, collector exits with a clear error
- `paused: true` means do not run collection passes
- while paused, resolution passes may still run if open predictions exist

## One-owner lock
There must be one active collector owner lock per Prediction Lab data directory.

### V1 rule
If another collector process owns the lock:
- the new collector exits cleanly
- it does not attempt collection or resolution

This prevents duplicate runners from mutating the same prediction ledger.

## Storage-cap accounting
V1 must define storage cap scope exactly.

### Count toward collection storage cap
- `market_snapshots.jsonl`
- `predictions.jsonl`

### Do NOT count toward collection storage cap
- `resolutions.jsonl`
- `state.json`
- report files

Reason:
- snapshots and open-prediction capture are the growth vectors we want to limit
- resolution history and state should not block cleanup of the research process

### Cap check timing
- check storage usage before every collect cycle
- check again immediately after a collect cycle completes
- if cap is exceeded after a collect cycle, transition to `paused` with `pause_reason=storage_cap`

### Resume rule after cap
V1 resumes from storage-cap pause only when:
- storage usage is below the configured cap at the next check, or
- the operator raises the configured cap, or
- the operator manually archives/prunes collection files

No automatic hysteresis beyond that in v1.

## Duplicate prediction policy
V1 uses simple, strict prediction dedupe.

### Prediction uniqueness key
A prediction row is uniquely identified by:
- `market_id`
- `experiment_id`
- `strategy_version`

This is the V1 prediction identity.

### Rule
At most one `open` prediction may exist for a given prediction identity.

### Behavior
- if an `open` prediction for the same identity already exists, do not append another open prediction row
- still allow append-only snapshot rows for repeated market observations
- if a prediction is already `resolved` or `voided`, a new open prediction for the same identity is not created in v1 unless the operator starts a new `experiment_id`

This is intentionally conservative and matches the current design direction.

## Snapshot policy
Snapshots are append-only.

Each snapshot row includes:
- timestamp
- run_id
- market_id
- group
- series
- question
- yes_price
- no_price
- confidence
- edge
- direction
- decision_type
- whether a prediction row was recorded

This gives us repeated-opportunity history without duplicating open predictions.

## Resolution lifecycle
V1 uses a narrow lifecycle.

### Prediction statuses
- `open`
- `resolved`
- `voided`

### Resolution write semantics
V1 resolution behavior must be idempotent.

For each prediction identity:
- `predictions.jsonl` remains the canonical open-state ledger
- resolution outcome is appended once to `resolutions.jsonl`
- after resolution is confirmed, `predictions.jsonl` is atomically rewritten so that the matching prediction row is no longer `open`

### Duplicate resolution rule
If resolve runs twice for the same prediction identity:
- do not append a second resolution row
- do not double-count pnl
- leave the already-resolved state intact

### Resolution rules
- a market is resolvable only when exchange outcome is clearly final as `YES` or `NO`
- if outcome is unavailable or ambiguous, leave prediction `open`
- if the market is explicitly cancelled/voided and that signal is available, mark `voided`
- do not invent dispute handling in v1

## Restart and idempotency rules
Restart behavior must be deterministic.

### On startup
1. load config
2. acquire owner lock
3. load state
4. evaluate storage usage
5. reconcile `run_state` from persisted state + current config
6. resume according to persisted state + current config

### Restart semantics
- if `pause_reason=manual_pause`, remain paused until config changes
- if `pause_reason=storage_cap`, remain paused until storage is below cap or operator changes config/cap
- if there are open predictions, resolution passes may continue
- do not reseed duplicate open predictions after restart
- if `continue_collecting` is true and not paused, resume collection loop
- if `seed_complete=true` and mode is `seed_and_watch`, do not restart seeding automatically

### Idempotency rules
- collect pass must be safe to rerun after crash or restart
- resolve pass must be safe to rerun after crash or restart
- report generation must be safe to rerun at any time
- repeated startup must not create duplicate open predictions for the same prediction identity

## Hot reload semantics
V1 hot reload happens between loop cycles, not mid-cycle.

### Reloadable fields in v1
- `prediction_lab.enabled`
- `prediction_lab.paused`
- `prediction_lab.groups`
- `prediction_lab.max_markets_per_run`
- `prediction_lab.collector_interval_seconds`
- `prediction_lab.resolve_interval_seconds`
- `prediction_lab.collection_storage_cap_gb`
- `prediction_lab.collection_warning_threshold_pct`

### Non-goal
No mid-request or mid-market-pass config mutation.
Only reload between cycles.

## Collector wrapper behavior
Add a dedicated wrapper script:

`scripts/prediction_lab_collect.py`

### Loop responsibilities
- hot-reload config between cycles
- honor enabled/paused state
- enforce one-owner lock
- check collection storage usage
- auto-pause on storage cap if configured
- run collect pass when due
- run resolve pass when due
- persist state transitions
- sleep briefly between checks

## Loop pseudocode
```text
startup:
  load config
  acquire owner lock
  load state
  evaluate storage

while enabled:
  reload config
  evaluate storage

  if paused manually:
    state = paused_manual
    maybe run resolve if due and open predictions exist
    sleep
    continue

  if storage cap exceeded and auto-pause enabled:
    state = paused_storage_cap
    maybe run resolve if due and open predictions exist
    sleep
    continue

  if collect_due and continue_collecting:
    state = running_collect
    run collect pass

  if resolve_due and open predictions exist:
    state = running_resolve
    run resolve pass

  if not continue_collecting and open predictions exist:
    state = idle_watch

  if not continue_collecting and no open predictions:
    state = completed

  sleep
```

## Notifications
V1 should only notify on meaningful transitions:
- collector paused manually
- collector auto-paused on storage cap
- collector resumed from pause
- first warning threshold crossing

No noisy per-loop messaging.

## Analysis expectations for v1
V1 is mainly about collecting safely, not full research reporting.

However, the stored data should already support future questions like:
- did repeated opportunities improve?
- which group produced useful signals most often?
- how often did we observe a market before it resolved?

That deeper analysis can be a second-phase reporting enhancement.

## Initial recommended run
For the first real test:
- group: `weather`
- mode: `collector`
- continue_collecting: `true`
- max_markets_per_run: `1000`
- storage cap: `25 GB`
- score_only: `true`

## Build priority
1. owner lock
2. explicit collector state machine
3. storage usage accounting + storage-cap pause
4. hot-reloadable manual pause
5. collector wrapper loop
6. targeted tests for restart, pause, cap, and dedupe

## Success criteria
V1 is successful if:
- only one collector process can run per lab data directory
- restart does not duplicate open predictions
- manual pause survives restart
- storage-cap pause survives restart
- collection resumes cleanly when pause/cap conditions are cleared
- repeated market observations accumulate in snapshots without duplicating open predictions

## Conclusion
This v1 is intentionally narrow.
It should be boring, durable, and safe.
That is the right foundation before building richer multi-group orchestration or deeper research reporting.
