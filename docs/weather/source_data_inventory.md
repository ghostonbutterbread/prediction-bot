# Weather Source Data Inventory

Last checked: 2026-05-22

This map separates raw source collection, source truth scoring, Kalshi
resolution labels, and source-router replay inputs. These artifacts answer
different questions and should not be treated as interchangeable.

## Raw Source Snapshots

Path: `data/beta_shadow/paper/source_scoreboard/recent_market_snapshots.jsonl`

- Current checked rows: 500
- Snapshot date: 2026-05-17
- Rows with `source_details`: 482
- Source observations: 964
- Sources present: `open_meteo` 482, `nws` 482
- Has market price fields: yes, 500 rows have YES/NO price fields
- Has final outcomes: no
- Use: current/source-router candidate pool and known-at-time source forecasts

Path: `data/beta_shadow/source_scoreboard_inputs/recent_market_snapshots.jsonl`

- Current checked rows: 500
- Snapshot date: 2026-05-18
- Rows with `source_details`: 481
- Source observations: 962
- Sources present: `open_meteo` 481, `nws` 481
- Has market price fields: yes, 500 rows have YES/NO price fields
- Has final outcomes: no
- Use: source truth/evaluation input pool

## Shadow Lane Decisions

Path: `data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl`

- Current checked rows: 146,367
- Dates: 2026-05-17, 2026-05-18, 2026-05-19
- Has stable/shadow decision rows and action/price/accounting provenance
- Does not contain per-source `source_details`
- Use: stable/control comparison decisions and shadow-lane audit trail

Forward-running update: `shadow_source_router` now lives beside
`shadow_source_scoreboard` in the source-scoreboard shadow runtime config. Fresh
rows are still written to this same decision ledger, but router rows preserve
their own source-implied `BUY_YES`/`BUY_NO`/`SKIP`, side-specific book price,
hypothetical notional, compact source observations, and future-PnL join fields
under `provenance.source_router` / `provenance.future_pnl_inputs`.

Path: `lanes/shadow_source_router.yaml`

- Disabled by default outside explicit shadow configs
- Input source: shared candidate dataset / shared market
- Mutates accounting/live orders: no
- Use: forward collection of replay/PnL-ready source-router decisions

Local-station note: the router lane does not require sources to be only `nws`
or `open_meteo`; any source rows present in `source_details` are compacted and
preserved. The live collector still needs a separate source-gathering slice to
promote station-adjacent sources such as cataloged `metar_asos` from
`candidate_disabled` into the forward source pool.

## Kalshi Final Outcomes

Path: `data/summaries/source_scoreboard_kalshi_resolutions_20260521T051152Z.jsonl`

- Current checked rows: 854
- Has finalized `kalshi_result` / `resolution` payloads
- Has `resolved_at` / outcome known-time information
- Use: final market truth labels for source scoring and replay joins

## Source Truth Scoring Artifacts

Path: `data/summaries/source_scoreboard_truth_eval_20260521T051152Z/source_truth_rows.jsonl`

- Current checked rows: 976
- Markets: 322
- Sources: `open_meteo` 488, `nws` 488
- Contains source-implied outcome and official outcome
- Current result snapshot:
  - `open_meteo`: 382/488 correct, 78.28%
  - `nws`: 370/488 correct, 75.82%
  - market consensus: 219/258 scored markets correct, 84.88%
- Has partial YES/NO price fields: 506 rows
- Missing as-of history fields: no `outcome_known_at`; 470 rows missing `observed_at`
- Use: posthoc source truth scoring, not directly safe as router history until known-time fields are normalized

Path: `data/summaries/source_scoreboard_truth_eval_20260521T051152Z/source_truth_summary.json`

- Summary of source-vs-finalized-market outcome quality
- Mode: source truth scoring only, no wallet sizing, no Kelly, no PnL

## Edge Evaluator Artifact

Path: `data/summaries/source_scoreboard_truth_eval_20260521T051152Z/source_edge_evaluation_rows.jsonl`

- Current checked rows: 976
- Current eligible rows: 0
- Blockers at the time of that run:
  - missing official outcome
  - missing source-implied side on many rows
  - missing source-side price
- Use: intended PnL/edge input shape, but the existing run is not usable for router PnL yet

## Current Router History Artifact

Path: `data/summaries/source_router_history_weather_pull_20260511_20260522T044417Z/source_outcome_ledger.jsonl`

- Current checked rows: 47
- Source present: `noaa_daily_summaries_station`
- Eligible source-history rows: 0
- Problem: rows are missing usable source forecast/implied side/history known-time fields
- Use: this is the artifact currently causing source-router replay to route 0 rows

Path: `data/summaries/source_router_history_weather_pull_20260511_20260522T044417Z/scoreboard/source_scoreboard_by_slice.jsonl`

- Current checked rows: 8
- Scored observations: 0
- This is only coverage/missing-data output, not a usable source reliability table

## Working Interpretation

We do have raw per-source observations in the recent source-scoreboard snapshots.
The earlier source-truth replay worked because it did a posthoc truth scoring
join: source forecasts plus finalized Kalshi outcomes. That proved the raw data
can identify which sources and market shapes were more correct.

The source-router replay needs a stricter artifact:

```text
source forecast known at candidate time
→ source-implied YES/NO side
→ side-specific observed price/fill proxy
→ finalized official outcome
→ outcome_known_at before the candidate being routed
```

The current router history input does not provide that. The next build step is
to promote the existing source-truth rows into a replay-grade as-of source
history ledger, preserving `outcome_known_at` so the router cannot accidentally
use future-resolved data.
