# Paper shadow lanes

This directory contains paper-only lane definitions used by `paper_shadow_lanes`.

Lanes are decision overlays: they write compact `paper_lane` decision rows for shared candidates, but they do not mutate paper wallet balances, accounting ledgers, shared candidate rows, or live orders.

A lane must not duplicate stable strategy logic. Lane inputs are always the shared candidate dataset and its embedded shared market/signal fields (`input_source: shared_candidate_dataset`, `input_market_source: shared_market`). Stable and beta paper decision rows are provenance references only: stable is the baseline row, beta is the comparison row, and `source_wallet` selects which already-computed paper row a lane may mirror before applying lane-specific overrides such as action, reason, confidence floor, allowlist, or size hint.

Lane definition shape:

```yaml
id: shadow_confidence_floor
type: confidence_floor
source_wallet: stable_paper
source_role: baseline
input_source: shared_candidate_dataset
input_market_source: shared_market
enabled: true
description: Shared-candidate-fed lane that starts from the stable baseline decision and only overrides the shared signal confidence floor outcome.
parameters:
  confidence_floor: 0.58
```

Enable them from config with an explicit allowlist:

```yaml
paper_shadow_lanes:
  enabled: true
  definitions_dir: lanes
  enabled_lanes:
    - control_stable
    - shadow_confidence_floor
```

## Collector-paired comparison cohorts

When a collector owns the shared market feed, run paper lanes as a strict consumer:

```yaml
paper:
  shared_market_runtime_enabled: true
  shared_market_consumer_only: true
```

In this mode paper never fetches or publishes a separate candidate universe. A
missing, stale, or publisher-mismatched collector snapshot blocks the scan and
writes no lane decisions. Every emitted lane row carries the exact collector
`shared_snapshot_id` at the top level, in provenance, and in the shared-candidate
reference; use that ID for comparison and resolution joins.

Merge precedence is:

1. built-in lane defaults
2. YAML definitions in this directory
3. inline config overrides under `paper_shadow_lanes`

## Current lanes

- `control_stable` — shared-candidate-fed control lane that mirrors the stable paper wallet decision as baseline provenance.
- `shadow_current_beta` — shared-candidate-fed shadow lane that mirrors the current beta paper wallet decision as comparison provenance.
- `shadow_confidence_floor` — starts from the stable baseline decision, but uses the shared signal confidence to require confidence greater than or equal to the configured floor before the lane would buy.
- `shadow_premium_city` — starts from the stable baseline decision, but only allows buys for configured premium cities. Disabled by default.
- `shadow_source_scoreboard` — starts from the stable baseline decision, but records source scoreboard recommendations and compact future-PnL reconstruction inputs under provenance only. Disabled by default and still non-mutating: no balance, accounting, or PnL state is changed. Resolved P&L is available only through derived read-only report artifacts built from lane decisions plus finalized market resolutions.
- `shadow_source_router` — makes an independent source-implied BUY_YES/BUY_NO/SKIP recommendation from the same shared candidate and source observation bundle, then records side-specific price, notional, source votes, and future-PnL inputs under provenance only. Disabled by default and non-mutating.
