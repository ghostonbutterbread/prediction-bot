# Beta Shadow Resolution Feed

The source-router beta-shadow runtime uses one central, shadow-only resolution
cache at `data/beta_shadow/resolutions/latest_resolutions.jsonl`.

Runtime-supported inputs are explicit `resolution_feed.decision_ledger_paths`
plus `resolution_feed.decision_ledger_globs`. The current paper-shadow runtime
configs list the current source-router low-sample and source-scoreboard lane
ledgers explicitly, then use this future-lane convention:

```yaml
resolution_feed:
  decision_ledger_globs:
    - data/beta_shadow/paper/*/paper_shadow_lane_decisions.jsonl
```

New shadow lane ledgers under
`data/beta_shadow/paper/<lane>/paper_shadow_lane_decisions.jsonl` are picked up
by that convention without editing the active config. Keep important current
ledgers in `decision_ledger_paths` as explicit anchors, especially while
operators are validating state by hand.

Backward compatibility is preserved: `resolution_feed.decision_ledger_path` and
`paper_shadow_lanes.decision_ledger_path` still resolve to a single-ledger feed
when the plural list/globs are absent.

Verify state without starting or stopping services:

```bash
PYTHONPATH=. python3 - <<'PY'
from bot.config import load_config
from bot.resolution_feed import normalize_resolution_feed_config

cfg = load_config("data/runtime_configs/paper_source_router_shared_shadow_collect_only_20260614.yaml")
print(normalize_resolution_feed_config(cfg)["decision_ledger_paths"])
print(cfg["resolution_feed"].get("decision_ledger_globs"))
PY
```

After a feed refresh, check
`data/beta_shadow/resolution_feed/source_router_low_sample/state.json` for
`decision_ledger_paths` and `central_resolution_path`.
