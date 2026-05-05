# Prediction Lab Backfill / Upgrader Spec

_Last updated: 2026-05-04_

## Purpose

Make older Prediction Lab data useful without pretending it is equivalent to correctly collected as-of data.

The backfill/upgrader should recover missing replay fields where possible, label provenance clearly, and produce upgraded datasets that can be used for logic analysis, coverage analysis, and carefully separated historical/backfilled scoring.

Core rule:

> Old rows are not invalid by default. They become useful according to their evidence tier.

## Goals

1. Preserve value from previously collected Prediction Lab rows.
2. Recover missing fields from existing artifacts/logs before calling external historical sources.
3. Backfill weather/source data where possible using deterministic archive/historical providers.
4. Never mix backfilled/post-facto data into strict-original metrics without quality/provenance flags.
5. Give Ryushe a repeatable validator/upgrader command instead of manual one-off inspection.

## Non-goals

- Do not rewrite raw source ledgers in place.
- Do not mark post-facto historical data as original as-of evidence.
- Do not invent missing order-book/executable ask data.
- Do not use resolved outcomes during replay decisioning.
- Do not mutate paper trading state.

## Input Ledgers

Potential inputs:

- `data/paper/prediction_lab/market_snapshots.jsonl`
- `data/paper/prediction_lab/predictions.jsonl`
- `data/paper/prediction_lab/resolutions.jsonl`
- collector logs under `data/paper/prediction_lab/logs/`
- older collector restart/manual logs if they contain serialized artifacts
- weather historical/archive caches under `data/archive_replay/`
- external historical weather providers already represented in:
  - `bot/weather/historical_provider.py`
  - `bot/weather/replay.py`
  - `scripts/weather_archive_replay_current_logic.py`
  - `scripts/prediction_lab_archive_replay.py`

## Output Ledgers

Backfill should write derived ledgers, not overwrite raw rows:

```text
data/paper/prediction_lab/backfill/
├── upgraded_market_snapshots.jsonl
├── backfill_report.json
├── backfill_failures.jsonl
└── provenance_manifest.json
```

The canonical analysis flow writes stable report names under:

```text
data/paper/prediction_lab/analysis/
├── market_snapshots_upgraded.jsonl
├── backfill_report.json
├── provenance_manifest.json
├── validation_report.json
└── latest_metadata.json
```

Optional per-run subdirectories are allowed if the tool also writes/updates a stable latest pointer.

## Evidence Tiers

### `replay_grade_original`

Original row already has:

- decision artifact
- recorded-as-of source/weather snapshot
- valid market/weather date validation
- recorded executable order-book/execution snapshot
- for BUY rows, passing `execution_feasibility` evidence
- no outcome leakage

Use for strict conclusions.

### `replay_grade_backfilled_from_artifact`

Fields were missing at top level but recoverable from the original decision artifact or nested row data.

Examples:

- weather snapshot nested under `source_context.data.weather_source_snapshot`
- source snapshot list contains the accepted signal
- order book exists under an older artifact key

This is high-confidence backfill because evidence was already in the original row.
For BUY rows, artifact backfill still requires passing `execution_feasibility`; legacy BUY rows with only source/order-book recovery stay `coverage_only`.

### `replay_grade_backfilled_from_log`

Fields were recovered from collector logs created at or near collection time.

Requirements:

- log timestamp is near row `observed_at`
- market id matches
- recovered data is copied with log source path/line metadata

Use separately from original rows but can be considered stronger than post-facto historical data.

### `historical_post_facto`

Fields were recovered from historical/archive providers after the fact.

Examples:

- observed weather from Open-Meteo/NOAA/IEM archive
- settlement-station actual high/low

This is useful for training, settlement validation, and answer-key construction, but it is not proof of what the bot knew at decision time.

### `coverage_only`

Row is useful for market coverage/reason-code analysis but lacks enough source/order-book evidence for strict scoring.
Legacy BUY rows without `execution_feasibility` belong here under the current standard.

### `unusable`

Row lacks stable identity, timestamp, market id, or decision artifact fallback.

## Backfill Pipeline

### Phase 1 — Inventory

Read input rows and classify missing pieces:

- missing decision artifact
- missing source snapshot
- missing weather snapshot
- missing weather/forecast date validation
- missing order-book/execution snapshot
- possible outcome leakage
- duplicate identity
- missing resolution join

Output `backfill_report.json` with counts by reason, market group, series, event ticker, and date.

### Phase 2 — Artifact/Nested Recovery

For each row:

- inspect `decision_artifact`
- inspect `source_context.data`
- inspect `source_snapshots`
- inspect older/fallback keys used by legacy collector rows
- normalize recovered fields into current replay schema
- add provenance:

```json
{
  "provenance": {
    "tier": "replay_grade_backfilled_from_artifact",
    "sources": [
      {
        "field": "weather_source_snapshot",
        "method": "nested_artifact_recovery",
        "path": "decision_artifact.source_context.data.weather_source_snapshot"
      }
    ]
  }
}
```

### Phase 3 — Log Recovery

If configured, scan collector logs for matching serialized rows/artifacts.

Matching keys:

- `market_id`
- `snapshot_key`
- `run_id`
- observed timestamp tolerance

Recovered fields must include source path, line number, and timestamp tolerance.

### Phase 4 — Historical Weather Backfill

For weather rows still missing weather/date evidence:

- map market to city/station/date
- use historical weather provider/cache where available
- add source type `historical_post_facto`
- include provider, fetched_at, archive date, station mapping, date validation

This should help answer “what actually happened?” and train source rules, but should not become strict-original.

### Phase 5 — Order-Book Handling

Order book is hardest to backfill.

Rules:

- If executable ask snapshot exists in original artifact/logs, recover it and label provenance.
- If only bid/mid/price exists, keep row coverage-only or fallback-priced, not strict.
- Do not synthesize executable asks from current market, resolution data, or post-facto prices.
- If external historical order-book archive exists later, add a separate provider with explicit timestamp/source labeling.

### Phase 6 — Validation

Run `validate_prediction_lab_tables(...)` on upgraded output.

Report:

- strict-original count
- artifact-backfilled count
- log-backfilled count
- historical-post-facto count
- coverage-only count
- unusable count
- remaining blockers by reason

## CLI Proposal

```bash
python3 scripts/prediction_lab_backfill.py \
  --input data/paper/prediction_lab/market_snapshots.jsonl \
  --resolutions data/paper/prediction_lab/resolutions.jsonl \
  --output-dir data/paper/prediction_lab/backfill/latest \
  --limit 10000 \
  --artifact-recovery \
  --log-recovery \
  --historical-weather
```

Useful modes:

```bash
# Inventory only; no writes besides report
python3 scripts/prediction_lab_backfill.py --input ... --inventory-only

# Safe first pass: recover only from row artifacts
python3 scripts/prediction_lab_backfill.py --input ... --artifact-recovery

# Weather historical post-facto backfill, explicitly labeled
python3 scripts/prediction_lab_backfill.py --input ... --historical-weather

# Canonical stable analysis ledger, bounded for smoke validation
# In canonical mode, --limit applies per raw input ledger.
python3 scripts/prediction_lab_backfill.py \
  --canonical-analysis \
  --analysis-dir data/paper/prediction_lab/analysis \
  --include-predictions \
  --limit 5000 \
  --validate-output
```

## Implementation Phases

### Phase 1 — Inventory + artifact recovery

- Add `bot/prediction_lab_backfill.py` pure helpers.
- Add `scripts/prediction_lab_backfill.py` CLI.
- Recover nested source/weather snapshots already present in artifacts.
- Produce upgraded output + report.
- Tests for tiering/provenance/no raw mutation.

### Phase 2 — canonical analysis ledger + validator integration

- Build `data/paper/prediction_lab/analysis/market_snapshots_upgraded.jsonl` from raw `market_snapshots.jsonl`, optionally including `predictions.jsonl`.
- Force artifact/nested recovery for the canonical analysis build.
- Reuse `validate_prediction_lab_tables` against the upgraded output.
- Write stable `backfill_report.json`, `provenance_manifest.json`, `validation_report.json`, and `latest_metadata.json`.
- Add backfill-specific report fields, including source path counts, written row counts, excluded row counts, tier counts, and validator status.
- Ensure outcome leakage remains forbidden in replay input.
- Preserve raw ledgers and paper trading state; canonical output is derived analysis data only.

### Phase 3 — log recovery

- Add log parser only for known structured/JSON lines.
- Do not scrape arbitrary prose as truth.
- Require timestamp and market id match.

### Phase 4 — historical weather post-facto backfill

- Use existing historical weather modules.
- Label as `historical_post_facto`.
- Never mark as strict-original.

### Phase 5 — report/replay integration

- Allow `scripts/prediction_lab_replay.py` to accept upgraded ledgers.
- Summary must separate strict-original, backfilled, and coverage-only metrics.

## Success Criteria

The first useful implementation is done when:

1. We can run inventory on old rows and know why each row is not strict.
2. We can upgrade rows using only data already present in artifacts.
3. The upgrader writes a separate ledger with provenance.
4. Validator passes with no errors on upgraded output.
5. Replay summary separates strict-original vs backfilled vs coverage-only.

## Safety Notes

- Keep raw ledgers append-only and unmodified.
- Treat backfill as a derived dataset.
- Prefer fewer high-confidence upgrades over broad speculative upgrades.
- If a field cannot be recovered with evidence, leave it missing and explain why.
