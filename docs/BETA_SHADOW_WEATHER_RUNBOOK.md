# Weather Beta-Shadow Runbook

This setup is for weather strategy lane shadow collection only. It does not
touch live config, `.env`, or existing process state by itself.

For the read-only Phase 5 migration/canary plan that preserves existing paper
state and previews later shared-candidate cutover, see
`docs/PAPER_DUAL_WALLET_MIGRATION_CANARY.md`.

## Configs

- Paper: `config.paper_beta_shadow_weather.yaml`
- Prediction Lab collector: `config.prediction_lab_beta_shadow_weather.yaml`
- Shadow root: `data/beta_shadow/paper`
- Prediction Lab shadow root: `data/beta_shadow/paper/prediction_lab`

The shadow configs are intentionally small composition declarations. Do not
manage separate full config forks for beta-shadow weather. The loader
materializes each shadow config as:

1. stable base: `config.yaml`
2. all-observability profile: `beta_shadow_observability_all`
3. runtime profile: `paper_beta_shadow_runtime` or
   `prediction_lab_beta_shadow_runtime`
4. explicit keys in the shadow YAML, when present, as the final override

The shared `beta_shadow_observability_all` overlay enables:

```yaml
strategy_policy:
  version: beta
  beta:
    mode: shadow
strategy_lanes:
  enabled: true
  enabled_lanes: [edge, hidden_gem, confidence_slow_profit]
```

with `weather_hidden_gem_evidence_card`, `bucket_distribution_scoring`,
`hidden_gem_lane_gates`, `confidence_slow_profit`, and `lane_sizing_caps` set to
`true`. It also keeps the weather-only route/group mapping and disables
news/social/AI inputs for the weather shadow mapping profile.

The runtime overlays own output isolation, shared-market runtime settings, and
paper-vs-observer runtime flags. They also explicitly disable generic alerts and
Telegram delivery so inherited stable alert settings cannot notify from shadow
runs. Lane sizing caps are configured for shadow comparison only; they do not
alter final paper/live actions unless beta mode is explicitly promoted to
`enforce`.

Isolated source-scoreboard paper shadow collection now has its own opt-in
runtime profile:

- Config: `data/runtime_configs/paper_source_scoreboard_shadow_20260516.yaml`
- Scoreboard input: `/tmp/weather_source_scoreboard_beta_smoke/source_scoreboard_by_slice.jsonl`
- Lane decisions: `data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl`

This profile is still recommendation-only and non-mutating. It keeps the
established paper shadow lanes enabled, adds `shadow_source_scoreboard`, and
records the scoreboard recommendation under provenance only. It does not change
stable/control paper actions, wallet balances, accounting, or live behavior.
It is not enabled in `config.yaml` or the current limited-shadow runtime
profiles.

Paper beta-shadow also records `shadow_intents.jsonl` for stable-skip candidates
when beta lane metadata differs, so old `SKIP -> beta candidate` cases are not
invisible during later PnL/replay review.

## Stop Normal Runtimes

Do not start shadow alongside the normal paper loop or normal Prediction Lab
collector unless you intentionally want two independent collectors competing
for API quota.

Inspect current ownership:

```bash
pgrep -af 'paper_loop.py|prediction_lab_collect.py'
```

Stop the current paper/lab process from its owning terminal or supervisor. Use
the same service wrapper you used to start it. Avoid killing processes from a
separate shell unless you have confirmed ownership and state.

## Start Shadow Paper

Run from the repo root:

```bash
PAPER_MODE=true \
SIMULATE_ONLY=true \
PAPER_CONFIG=config.paper_beta_shadow_weather.yaml \
PAPER_LOG_FILE=data/beta_shadow/paper/paper_loop.log \
python3 paper_loop.py
```

Equivalent explicit CLI form:

```bash
PAPER_MODE=true \
SIMULATE_ONLY=true \
PAPER_LOG_FILE=data/beta_shadow/paper/paper_loop.log \
python3 paper_loop.py --config config.paper_beta_shadow_weather.yaml
```

Paper remains `trading.mode: paper`; no live config is involved. The command
sets `PAPER_MODE=true` explicitly so an inherited live-shell environment cannot
move the paper loop into a live runtime directory.

## Start Shadow Prediction Lab

Run from the repo root:

```bash
TRADING_MODE=paper \
python3 scripts/prediction_lab_collect.py --config config.prediction_lab_beta_shadow_weather.yaml --observer
```

The collector config is observer mode with `trading.enabled: false` and
`trading_enabled: false`. The observer CLI patch also forces `trading.mode:
paper` and refreshes runtime paths, so an inherited `TRADING_MODE=live` cannot
move shadow collector output under the live directory.

## Replay And Analysis

Normal replay/analyze commands should continue to use normal `data/paper` by
default. Do not point replay at both `data/paper` and `data/beta_shadow/paper`
in the same run.

Shadow Prediction Lab replay example:

```bash
python3 scripts/prediction_lab_replay.py \
  data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl \
  --config config.prediction_lab_beta_shadow_weather.yaml
```

Shadow paper analysis example:

```bash
ANALYZE_DATA_DIR=data/beta_shadow \
ANALYZE_DATA_DIR_ONLY=true \
ANALYZE_CONFIG=config.paper_beta_shadow_weather.yaml \
python3 scripts/analyze.py --report
```

`ANALYZE_DATA_DIR_ONLY=true` is required for isolated shadow analysis; without
it, analyze also includes the normal `data/` root for backward compatibility.
`ANALYZE_CONFIG=config.paper_beta_shadow_weather.yaml` keeps strategy-policy
fallbacks and storage reporting pointed at the shadow runtime config. If
`ANALYZE_DATA_DIR_ONLY=true` is used without `ANALYZE_CONFIG`, analyze disables
storage audit/pruning rather than touching normal config paths.

The analyze script still writes summaries under the normal summaries directory
when run without `--report`, so treat shadow analysis output as review material
and do not compare it as a normal paper report without labeling it.

To review the isolated source-scoreboard lane decisions after a run:

```bash
python3 scripts/paper_shadow_lane_report.py \
  --lane-decision-path data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl
```

To materialize a derived, read-only lane-resolution artifact for resolved P&L/replay review:

```bash
python3 scripts/paper_shadow_lane_report.py \
  --lane-decision-path data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl \
  --resolution-path data/paper/prediction_lab/resolutions.jsonl \
  --section resolved_pnl \
  --resolved-output-jsonl data/summaries/source_scoreboard_lane_resolutions.jsonl
```

The `--resolved-output-jsonl` file is a derived analysis artifact only. Do not point it at
paper wallet session files, risk state, lifecycle/reconciliation ledgers, or live order/trade
paths. Resolution rows are regenerated from lane decisions plus finalized market outcomes;
they must not be consumed as wallet trades or accounting state.

Resolved PnL review is also read-only. It joins lane decision rows to a
resolution JSONL and builds replayable resolution rows internally from recorded
action/side, entry or estimated fill price, notional/approved size, outcome, and
replay sizing metadata. This remains separate from wallet accounting; it does
not mutate balances, paper sessions, trades, risk state, or live orders.

If a lane/candidate/decision file has old market IDs but the local
`resolutions.jsonl` does not cover them yet, first build a derived Kalshi
resolution backfill:

```bash
python3 scripts/backfill.py \
  --kind scoreboard-resolutions \
  --lane shadow_source_scoreboard \
  data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl \
  --output data/summaries/source_scoreboard_kalshi_resolutions.jsonl \
  --report-output data/summaries/source_scoreboard_kalshi_resolutions.report.json
```

Then feed that derived resolution file into the normal read-only P&L report:

```bash
python3 scripts/paper_shadow_lane_report.py \
  --section resolved_pnl \
  --lane-decision-path data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl \
  --resolution-path data/summaries/source_scoreboard_kalshi_resolutions.jsonl
```

The backfill tool fetches finalized public Kalshi market outcomes and writes
derived artifacts only under report/summaries directories. It must not be used
to overwrite paper wallet sessions, risk state, lifecycle/reconciliation
ledgers, or live trade/order paths. The legacy
`scripts/scoreboard_resolution_backfill.py` entrypoint still works, but
`scripts/backfill.py --kind ...` is the preferred front door for new backfills
so Prediction Lab, agent-decision, and lane-resolution backfills share one
discoverable command shape.

```bash
python3 scripts/paper_shadow_lane_report.py \
  --section resolved_pnl \
  --lane-decision-path data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl \
  --resolution-path data/paper/prediction_lab/resolutions.jsonl
```

If the frozen scoreboard artifact is regenerated elsewhere, update
`paper_shadow_lanes.source_scoreboard_path` in
`data/runtime_configs/paper_source_scoreboard_shadow_20260516.yaml` before using
that profile.
