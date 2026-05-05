# Prediction Lab Blind Replay + Data Validator Spec

_Last updated: 2026-05-04_

## Purpose

Prediction Lab should be the bot's evidence engine: collect what the bot saw, replay what the bot would decide, then join outcomes afterward to measure whether logic/source/bucket changes helped.

The core guarantee:

> Replay decisions must be made blind to market results. Outcomes are joined only after replay for scoring.

This lets us answer:

- Did a source change improve decisions?
- Did bucket handling add good trades or bad trades?
- Which market areas are we ignoring?
- Are weird historical results caused by bot logic or bad/incomplete data?
- Can we prove the table was collected correctly before trusting conclusions?

## Operating Modes

### `score_only: true`

Research collection mode.

- Do not open normal Prediction Lab prediction rows.
- Record replay-grade decision artifacts in `market_snapshots.jsonl`.
- Replay target is `market_snapshots.jsonl`.
- Resolutions live in a separate resolution ledger/table.

This is the default for broad data collection because it avoids treating every scored market as an open prediction.

### `score_only: false`

Intentional prediction-row mode.

- Writes `predictions.jsonl` rows when `collector_record_predictions: true`.
- Use when we explicitly want open prediction tracking.
- Still keep outcomes out of replay inputs; use the separate resolution ledger for scoring.

## Data Tables / Ledgers

### Replay Input Ledger: `market_snapshots.jsonl`

Append-only observed-as-of rows. Each row should include:

- stable identity: `market_id`, `run_id`, `snapshot_key` or equivalent
- timestamps: `observed_at` / `timestamp`
- market metadata: group, series, event ticker, bucket/threshold metadata
- prices: yes/no market prices
- order-book snapshot with executable asks when available
- execution snapshot or reason why unavailable
- source/weather snapshot with fetched/as-of timestamps
- weather date/forecast date validation data
- decision artifact:
  - final action
  - final reason code
  - shared-core reasoning
  - validator/risk/Kelly reasons
  - source modes and warnings

Must **not** include final outcome/resolution fields.

### Resolution Ledger: `resolutions.jsonl`

Append-only outcome rows. Each row should include:

- `market_id`
- optional identity fields: experiment/version/snapshot/prediction id where applicable
- `resolved_at`
- `outcome`: `YES`, `NO`, or `VOID`
- settlement/source metadata when available
- scoring fields only after join, not inside replay input rows

### Replay Output Table

Replay output should be appendable/exportable for future comparisons:

- input row identity
- replay logic version / strategy version
- replayed action
- replayed reason code
- replay artifact / reasoning
- strict-vs-coverage quality classification
- joined outcome after unblind
- correctness / hypothetical P&L after unblind

This allows future logic versions to be re-compared against the same frozen input rows.

## Blind Replay Protocol

1. Load replay input rows.
2. Strip/ignore any accidental outcome/resolution fields.
3. Build recorded source/order-book/execution context only from as-of row data.
4. Run current or target logic version.
5. Store replay decision/reasoning.
6. Join resolution ledger by stable identity.
7. Score correctness, missed wins, bad buys added/removed, P&L, and bucket/source deltas.

Replay must be deterministic and must not fetch current/live source data unless the run is explicitly marked non-strict/coverage.

## Validator Requirements

Prediction Lab needs an active validator that can be run against the table before trusting analysis.

Minimum checks:

### Schema

- required fields present
- valid JSONL rows
- stable row identity exists
- no duplicate identities unless explicitly versioned

### Timestamp / as-of integrity

- observed timestamps parse
- source `fetched_at` / `as_of` timestamps parse
- no source timestamp after market resolution in strict rows
- no impossible future timestamps relative to collection time

### Outcome leakage

- replay inputs must not contain:
  - `resolution`
  - `outcome`
  - `actual_outcome`
  - `settled_outcome`
  - `market_result`
- if found, validator should fail or mark row non-strict.

### Weather/source integrity

- weather rows include weather/source snapshot blocks
- forecast/weather date aligns with market date when derivable
- date validation reason is recorded
- station/source mapping quality is recorded
- source agreement/confidence fields are present when used by logic

### Order-book / execution integrity

- strict rows require executable ask prices, not zero asks or bid-only books
- execution snapshot source is recorded
- BUY strict rows require passing `execution_feasibility` evidence
- fallback/synthetic price use is explicitly labeled and excluded from strict metrics unless policy allows

### Resolution joinability

- every resolved row can join to at least one replay input by stable identity or market id
- duplicate/conflicting resolutions are flagged
- VOID handling is explicit

### Quality classification

Rows should classify into categories such as:

- `replay_grade_original`
- `replay_grade_backfilled`
- `missing_weather_snapshot`
- `missing_order_book`
- `date_unverified`
- `source_missing`
- `outcome_leakage`
- `coverage_only`

Strict analysis uses only replay-grade rows. Coverage reports include all rows with exclusion reasons.
Legacy BUY rows that predate `execution_feasibility` are intentionally non-strict `coverage_only`, not promoted to replay-grade.

## Success Criteria

This work is ready when we can run:

1. collector in `score_only: true`
2. validator over fresh `market_snapshots.jsonl` + `resolutions.jsonl`
3. blind replay over the same snapshots
4. unblind scoring via resolution join
5. report:
   - strict row count
   - excluded row reasons
   - trades gained/lost by logic version
   - bucket-specific deltas
   - source-change deltas
   - missed market areas / coverage gaps

## Implementation Notes

- Preserve append-only raw data where possible.
- Prefer derived replay/report outputs over mutating original collection rows.
- Never mix backfilled rows into strict metrics without provenance labels.
- Keep paper trading state independent from Prediction Lab replay/collector state.
