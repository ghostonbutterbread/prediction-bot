# Strategy Lane Shadow Validation Spec

_Last updated: 2026-05-08_

## Purpose

Make beta-shadow strategy lanes produce reviewable old-vs-new evidence without
changing real paper/live execution.

The previous lane implementation proved that lane metadata could be attached to
paper trades, but review found two validation gaps:

1. beta-shadow configs enabled feature flags without enabling concrete lane
   behavior/caps, so gates and sizing caps were mostly metadata-only;
2. paper shadow intent rows were only written after stable paper admitted a
   trade, so stable `SKIP -> beta BUY` candidates were invisible.

## Required beta-shadow behavior

### Config

Weather beta-shadow configs must explicitly enable strategy lanes and sizing
caps under `strategy_lanes` while keeping `strategy_policy.beta.mode: shadow`.

This means beta-shadow may compute lane alternatives, would-select lanes, and
would-size caps, but must not mutate final paper balances, exposure, orders,
trades, or PnL.

Configured lanes:

- `edge` — normal/consistent trading lane.
- `hidden_gem` — cheap/lottery-style lane that must carry evidence-card and
  weather-risk metadata.
- `confidence_slow_profit` — optional high-confidence, lower-edge lane used to
  expose stable `SKIP -> beta candidate` opportunities for review.

Configured sizing caps are deliberately conservative and shadow-only until an
explicit enforce promotion.

### Paper runtime audit

Paper beta-shadow must write shadow-intent audit rows for both:

- stable BUY candidates whose beta shadow side differs by rejection, sizing, or
  lane; and
- stable SKIP candidates where beta lane metadata says the beta lane would have
  differed.

Stable-skip shadow-intent capture must happen before returning to the scan loop
and must never append to normal paper trades, reserve capital, risk exposure, or
PnL.

### Evidence limitations

Stable `SKIP -> beta BUY` rows may start as partial beta evidence when the stable
path never reached sizing/risk/execution. They are still required because they
tell the collector which opportunities need replay-grade market/order-book/source
snapshots for apples-to-apples PnL.

Full PnL remains blocked until frozen rows contain:

- stable and beta decision artifacts for the same opportunity,
- recorded order-book/execution-feasibility snapshots,
- recorded weather/source snapshots, and
- eventual resolution.

## Acceptance checks

- `config.paper_beta_shadow_weather.yaml` and
  `config.prediction_lab_beta_shadow_weather.yaml` normalize to beta/shadow and
  have `strategy_lanes.enabled: true` with explicit sizing caps.
- Paper stable-skip signals can append a `shadow_intents.jsonl` row without
  mutating paper trade state.
- Existing stable/off configs remain unchanged by default.
- Targeted tests for strategy-lane config, shadow intents, replay/reporting, and
  paper runtime paths pass.

## Status after config/profile composition

The beta-shadow runtime profiles now compose from stable `config.yaml` instead
of maintaining full copied config files. This satisfies the config-side
acceptance goal more safely: shadow profiles inherit stable defaults, add the
shared beta-shadow observability overlay, then add only runtime-specific output
isolation and observer/paper flags.

The original strategy-lane work should still be treated as **pre-enforce**:

- stable/off behavior remains the default and must remain unchanged;
- beta/shadow may compute and record would-have decisions only;
- beta/enforce remains a future paper-only promotion decision after review;
- live remains stable until explicit promotion.

Nothing in this spec is sufficient by itself to promote enforce. Full PnL and
promotion review remain blocked until the frozen/replay rows include aligned
stable and beta artifacts, order-book/execution-feasibility snapshots,
weather/source snapshots, and resolutions for the same opportunities.
