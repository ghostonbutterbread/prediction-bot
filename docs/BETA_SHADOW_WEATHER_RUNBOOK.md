# Weather Beta-Shadow Runbook

This setup is for weather strategy lane shadow collection only. It does not
touch live config, `.env`, or existing process state by itself.

## Configs

- Paper: `config.paper_beta_shadow_weather.yaml`
- Prediction Lab collector: `config.prediction_lab_beta_shadow_weather.yaml`
- Shadow root: `data/beta_shadow/paper`
- Prediction Lab shadow root: `data/beta_shadow/paper/prediction_lab`

Both configs enable:

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
`hidden_gem_lane_gates`, and `lane_sizing_caps` set to `true`. Lane sizing caps
are configured for shadow comparison only; they do not alter final paper/live
actions unless beta mode is explicitly promoted to `enforce`.

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
