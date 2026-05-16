# Weather Source Scoreboard — Overnight Evidence Notes

Generated: 2026-05-16
Mode: offline/report-only. No runtime/config activation, no pushes, no collector restarts.

## Reviewer follow-up

Read-only branch reviewer found one medium ops issue: `scripts/prediction_lab_monitor_cron.sh` could auto-select beta-shadow monitoring when stale beta state/pid files existed.

Fix applied in this pass:
- beta-shadow monitor selection now requires explicit `PREDICTION_LAB_MONITOR_PROFILE=beta_shadow`
- default profile remains stable/normal/paper
- stale beta state/pid files no longer move monitor alerts to beta-shadow implicitly

Validation:
- `PYTHONPATH=. pytest -q tests/test_prediction_lab_monitor.py` → `13 passed`
- broader source-reliability/shadow gate after patch → `118 passed, 9 subtests passed`

## Scoreboard report smoke

Bounded offline smoke path:
- input: `data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl`
- command limit: `--limit 50000`
- output: `/tmp/weather_source_scoreboard_beta_smoke`

Smoke results:
- rows: 50,000
- source observations: 95,117
- scored observations: 46,483
- slices: 152
- report artifacts emitted successfully

Full `data/paper/prediction_lab/market_snapshots.jsonl` run was attempted, but it was too memory-heavy for the current in-memory CLI shape. It was stopped before completion. A bounded 50k paper smoke completed but had no source observations in that older artifact.

## Label sanity pass

Representative rows were sampled from the generated best/worst slices.

Examples observed:

| section | market | source | city | kind | shape | threshold | forecast | actual_used | note |
|---|---|---|---|---|---|---:|---:|---:|---|
| best | `KXLOWTMIA-26MAY08-B75.5` | `nws` | Miami | low | range | 76 | 82.0 | 82.0 | NWS forecast equals `actual_temp_used` |
| best | `KXLOWTNYC-26MAY08-B50.5` | `nws` | New York | low | range | 51 | 52.0 | 52.0 | NWS forecast equals `actual_temp_used` |
| best | `KXHIGHTATL-26MAY08-B76.5` | `nws` | Atlanta | high | range | 77 | 73.0 | 73.0 | NWS forecast equals `actual_temp_used` |
| worst | `KXLOWTMIA-26MAY08-T76` | `open-meteo` | Miami | low | tail | 76 | 75.3 | 82.0 | Open-Meteo disagreed with NWS settlement temp |
| worst | `KXHIGHTSFO-26MAY08-B68.5` | `open-meteo` | San Francisco | high | range | 69 | 66.7 | 69.0 | Open-Meteo missed NWS settlement temp |

Important interpretation:
- The current scorer is internally consistent for these rows: market id/date/city/threshold/source forecasts line up.
- But the `actual_temp_used` field appears to be derived from the settlement source (`settlement_source: nws`) in the same recorded weather snapshot.
- That means NWS scoring as perfect (`mae=0`, `dir_acc=1`) is expected and should not be treated as independent proof that NWS is objectively best.
- For promotion decisions, we need either independent resolved actuals or explicit acceptance that the target label is “match Kalshi/NWS settlement source,” not “match external observed truth.”

Conservative conclusion:
- Scoreboard reports are useful for diagnostics and source-disagreement analysis now.
- Do not use current NWS-perfect ranking as promotion evidence without an independent label check.

## Replay-grade artifact hunt

Promising local artifacts checked:
- `data/archive_replay/weather_pull_1000_20260511_010538/prediction_lab/market_snapshots.jsonl`
- `data/summaries/weather_pull_1000_replay_20260511_010659/replay_rows.jsonl`
- `data/summaries/e2e_dual_policy_validation_20260512T172107Z/recent_market_snapshots_1000.jsonl`
- `data/beta_shadow/archive/pre_fresh_shadow_20260508_153123/prediction_lab/market_snapshots.jsonl`

Evidence-helper summary across those files:
- rows read: 4,569
- weather rows: 3,619
- source observation rows: 2,498
- selected replay-grade rows: 0
- source modes: 2,583 `recorded_as_of`, 1,939 `missing`, 47 `historical_post_facto`
- order book modes: 2 `recorded_book`, 4,567 `missing`
- execution snapshot modes: 4,569 `missing`

Two rows had recorded order books, but both were historical/post-facto and lacked usable execution snapshots:
- `HIGHNY0-21JUL19-T82`
- `KXHIGHMIA-24OCT25-B84.5`

Conservative conclusion:
- I did not find a local replay-grade dataset that combines recorded-as-of source observations, usable order book/execution snapshots, buy interest, and strict replay quality.
- The blocker remains data quality, not scoreboard/reporting code.

## What to collect next

For future promotion-grade source-reliability replay, each candidate row needs:

1. Candidate identity
   - `shared_candidate_id`
   - `market_id`
   - `observed_at` / `recorded_as_of`

2. Weather/source evidence recorded as of decision time
   - source name/id
   - source forecast high/low/current as used
   - source `as_of` / `fetched_at`
   - market weather date and date-validation result

3. Independent/resolution label
   - resolved high/low or settlement temp
   - label source (`kalshi_settlement`, `nws_observed`, `noaa_daily`, etc.)
   - recorded/known-after timestamp

4. Price/execution evidence recorded as of decision time
   - best yes/no bid/ask
   - execution snapshot or signal-price fallback accepted by replay
   - buy side and estimated fill price

5. Stable/control decision
   - final stable action/reason
   - approved size
   - confidence

Without #4, we can rank source accuracy but cannot honestly estimate P&L impact.
Without #3 as an independent/explicit label, we can accidentally prove “NWS matches NWS.”
