# Strategy Policy Beta Rollout

## Purpose

`strategy_policy` is a shared config surface for Prediction Lab, paper, and live strategy policy experiments. The initial scaffold only normalizes config and exposes helper booleans; it does not change scoring, lane gates, order sizing, order placement, or any other trading decision.

## Config Shape

```yaml
strategy_policy:
  version: stable # stable|beta, default stable
  beta_mode: off  # off|shadow|enforce, optional shorthand
  beta:
    mode: off     # off|shadow|enforce
    features:
      weather_hidden_gem_evidence_card: false
      bucket_distribution_scoring: false
      hidden_gem_lane_gates: false
      confidence_slow_profit: false
      lane_sizing_caps: false
```

The loader exposes the parsed value at `config["strategy_policy_normalized"]`. Invalid `version` or beta mode values fail closed to `stable` and `off`.

`beta.mode` takes precedence over the top-level `beta_mode` shorthand. Use top-level `beta_mode` only when the nested `beta.mode` key is omitted.

Unknown feature names are ignored. Only keys listed in the default feature set are normalized, so `feature_enabled("typo")` is always false.

## Helper Semantics

- `is_configured_beta`: true when `version: beta` parsed successfully, even if `beta_mode` is `off`.
- `is_active`: true only when `version: beta` and the effective mode is `shadow` or `enforce`.
- `is_shadow`: true only for active beta shadow mode.
- `is_enforce`: true only for active beta enforce mode.
- `is_beta`: compatibility alias for `is_configured_beta`; prefer the explicit helper in new code.
- `configured_features`: normalized feature flags requested in config.
- `features`: active feature flags. These are all false unless `is_active` is true.
- `feature_enabled(name)`: true only for a known feature whose active flag is true.

## Rollout

Stable main remains the default everywhere. Prediction Lab may opt into `version: beta` with `mode: shadow` to collect parallel evidence without changing decisions. Paper may move to `mode: enforce` after shadow data is reviewed. Live stays `stable` until the beta policy is explicitly promoted.

## Phase 3 Observability Notes

- Phase 3A added weather-lane beta gating under `strategy_policy`; stable behavior remains unchanged unless beta policy is active.
- Phase 3B added `decision.reasoning["hidden_gem_evidence_card"]` for weather hidden-gem candidates, and Prediction Lab preserves the card inside shared pipeline decision artifacts.
- Phase 3C adds reporting-only aggregation for those cards. Reports summarize card counts by `weather_shape x hidden_gem_tier x reason_code`, include final approvals/rejections plus beta-rejection counts where artifacts carry them, and count legacy no-card or incomplete-card rows without failing. This slice does not alter strategy selection, trade decisions, sizing, risk checks, or order behavior.
- Phase 3D adds replay/report comparison for hidden-gem evidence cards with strict-vs-coverage slices and conservative artifact-derived stable/pre-hotfix, hotfix bridge, and evidence-card comparators.
- Phase 3E adds beta-gated bucket hidden-gem distribution thresholds under `bucket_distribution_scoring`: enforce requires `distribution_probability >= entry_price + 0.05` and `distribution_probability >= 3x entry_price`; stable/off and beta/shadow record deltas without changing final actions.
- Phase 3F extends the same beta-gated bucket path with source/station evidence quality: enforce requires exact station mapping and `source_agreement_score >= 0.65`; stable/off and beta/shadow record deltas without changing final actions.
- Phase 3G adds beta-gated tail hidden-gem candidate-side distribution scoring under `weather_hidden_gem_evidence_card`: tails use `distribution_probability` when present, otherwise retain the live-probability bridge, and evidence cards/reporting record which path was used.
- Phase 3H adds metadata-only lane sizing/cap records under `strategy_lanes.sizing`; shared-core reports selected-lane potential size adjustments without applying them or changing stable/shadow/enforce sizing behavior.
- Phase 3I adds beta/enforce application for selected-lane sizing caps under `lane_sizing_caps`: stable/off and beta/shadow preserve final requested/approved sizes while reporting deltas; enforce only applies explicit `strategy_lanes.sizing` reductions before `risk_policy.check_trade`.
- Phase 3J finalizes confidence-slow-profit observability: explicit slow-profit lane config plus beta/shadow `confidence_slow_profit` records would-select lane metadata without changing final admission, while final admission remains gated behind beta/enforce plus the dedicated `confidence_slow_profit` feature flag. Analyze and Prediction Lab replay summaries report selected lanes, would-select lanes, and slow-profit deltas from final stable actions.
- Phase 3K adds lane-sizing delta reporting to analyze/replay summaries. This is observability-only: it extracts lane-sizing metadata from known artifact shapes and reports configured/would-adjust/applied/preserved/shadow counts plus size totals without changing decisions.
- Phase 3L hardens validation hermeticity by moving `paper_loop.py` env/logging setup out of import time. Normal runtime execution still loads `.env`, sets `PAPER_MODE`, refreshes settings, and configures logging; importing the module no longer mutates env controls, root logging, log files, or `sys.path`.
- Phase 3M adds analyze-only rollout readiness reporting for the beta strategy-lane system. The checklist is deterministic and conservative: it reports policy mode/features, hidden-gem evidence-card presence/cleanliness, beta lane-gate delta coverage, and lane-sizing delta coverage. Missing shadow evidence becomes a blocker or warning in the report, but the checklist is observational only and must not be treated as a decision, sizing, or order-placement input.

## Beta Strategy-Lane Readiness Checklist

`scripts/analyze.py` now emits `strategy_lane_rollout_readiness` next to the existing hidden-gem and strategy-lane summaries.

The checklist targets pre-enforce evidence collection, so a clean pass requires:
- `strategy_policy` is `beta/shadow`
- active beta features include `weather_hidden_gem_evidence_card`, `hidden_gem_lane_gates`, `confidence_slow_profit`, and `lane_sizing_caps`
- hidden-gem evidence cards are present and have no insufficient-data rows
- strategy-lane rows contain beta lane-gate metadata, so final-vs-would-select deltas are measurable
- lane-sizing rows contain shadow sizing metadata, so final-vs-would-size deltas are measurable

The readiness line is intentionally fail-closed. It can say `blocked` or `needs_review` even when trading behavior is working as configured, because it only answers whether the collected artifacts are clean enough to review before considering enforce. It does not change stable/off, beta/shadow, or beta/enforce final decisions.
