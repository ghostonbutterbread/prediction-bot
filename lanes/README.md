# Paper shadow lanes

This directory contains paper-only lane definitions used by `paper_shadow_lanes`.

Lanes are decision overlays: they write compact `paper_lane` decision rows for shared candidates, but they do not mutate paper wallet balances, accounting ledgers, shared candidate rows, or live orders.

A lane must not duplicate stable strategy logic. It should point at a source wallet decision, usually `source_wallet: stable_paper`, reuse that already-computed baseline decision row, and then override only the lane-specific values such as action, reason, confidence floor, allowlist, or size hint.

Enable them from config with an explicit allowlist:

```yaml
paper_shadow_lanes:
  enabled: true
  definitions_dir: lanes
  enabled_lanes:
    - control_stable
    - shadow_confidence_floor
```

Merge precedence is:

1. built-in lane defaults
2. YAML definitions in this directory
3. inline config overrides under `paper_shadow_lanes`

## Current lanes

- `control_stable` — mirrors the stable paper wallet decision.
- `shadow_current_beta` — mirrors the current beta paper wallet decision.
- `shadow_confidence_floor` — starts from the stable paper decision, but requires confidence to be greater than or equal to the configured floor before the lane would buy.
- `shadow_premium_city` — starts from the stable paper decision, but only allows buys for configured premium cities. Disabled by default.
