# Weather Narrow Bucket / Tail Risk Spec

Status: Finalized implementation spec for `v2` / `v3` / `v4`; updated with 2026-05-06 hidden-gem hotfix learnings
Date: 2026-04-27; updated 2026-05-06
Scope: documentation only; no code changes implied by this file

## Purpose

This spec finalizes the next weather-risk rollout after the corrected paper audit and the shared-brain review. It defines:

- the current implemented `v1` safety layer
- the uncertainty that remains, especially around `tail_high`
- the staged `v2` / `v3` / `v4` implementation path
- Prediction Lab data and clean-label requirements
- the archived baseline and replay comparison protocol

This document is intentionally conservative. The archived replay is useful, but it is too small to justify fine threshold tuning or strong claims about durable edge.

## Archived Evidence Base

Primary archived baseline:

- `data/paper/audits/baselines/weather_risk_20260427_shared_brain_v1/baseline_summary.json`
- `data/paper/audits/baselines/weather_risk_20260427_shared_brain_v1/README.md`

Current post-resolution-guard comparison snapshot:

- `data/paper/audits/weather_risk_replay_compare_after_resolution_guard_summary.json`

Shared-brain inputs that informed this spec:

- `data/paper/audits/baselines/weather_risk_20260427_shared_brain_v1/brainstorm_tail_policy_codex_a.md`
- `data/paper/audits/baselines/weather_risk_20260427_shared_brain_v1/brainstorm_tail_policy_codex_b.md`

Archived baseline headline numbers:

- `61` resolved/flattened paper trades
- actual corrected P&L: `-$90.31`
- replayed current-logic P&L: `+$203.25`
- improvement vs actual: `+$293.56`
- actual total size: `$5813.30`
- replay total size: `$1006.02`

Shape slices:

| Shape | Trades | Actual P&L | Replay P&L | Improvement | Replay behavior |
|---|---:|---:|---:|---:|---|
| `bucket` | 46 | `-$275.80` | `+$13.59` | `+$289.39` | mostly heavy resize |
| `tail_high` | 8 | `+$42.55` | `-$50.20` | `-$92.75` | 3 exceptional skips |
| `tail_low` | 7 | `+$142.94` | `+$239.86` | `+$96.92` | modest improvement |

Interpretation:

- `bucket` damage was real and large.
- current replay improvement comes mostly from crushing narrow-bucket size.
- `tail_high` got worse under current logic because the strict exceptional-evidence rule skipped trades that included at least one large winner.
- this does **not** prove that `tail_high` is a durable alpha source.

## Guardrail: Do Not Overfit 61 Trades

This spec must be read with one non-negotiable rule:

- do not overfit `61` trades

Implications:

- do not tune narrow thresholds from this sample alone
- do not treat one replayed large winner as proof of skill
- keep initial policy tiers coarse
- require Prediction Lab accumulation before promoting any experimental carveout to default behavior
- prefer instrumentation and reason codes over aggressive policy branching

The sample is especially thin in the cohorts that matter most:

- `tail_high`: `8` trades total
- `tail_low`: `7` trades total
- `tail_high exceptional`: `3` trades total

Any policy that appears to “fix” this baseline may still be fitting noise.

## Definitions

### Market shape

- `bucket`: exact or narrow range, such as `82-83°` or ticker strike `B82.5`
- `tail_low`: less-than / below threshold
- `tail_high`: greater-than / above threshold

### Hidden-gem disagreement tier

Current hidden-gem logic is based on cheap entry plus model/market disagreement:

- entry price `<= 0.05`
- probability multiple = `win_probability / entry_price`

Tier names:

- `none`: not a hidden gem
- `normal`: `>= 3x` and `< 10x`
- `suspicious`: `>= 10x` and `< 15x`
- `exceptional`: `>= 15x`

## Current State: Implemented `v1` Safety Layer

This section describes what is implemented today in code, not what was only proposed in earlier drafts.

Implemented in `bot/weather_market_risk.py` and shared-core wiring:

- market shape classification: `bucket`, `tail_low`, `tail_high`, `unknown`
- deterministic weather evidence derivation in shared core
- narrow-bucket size suppression
- hidden-gem disagreement tiering
- exceptional hidden-gem skip unless “perfect evidence” is present
- Prediction Lab weather-risk metadata emission when derivable

### `v1` narrow-bucket controls

Current default policy:

- base bucket `size_multiplier = 0.50`
- `max_position_pct = 0.02`
- `max_position_usd = 10`
- if volume is unknown: multiply by `0.25`
- if volume is known but `< 500`: multiply by `0.50`

Practical effect in the archived replay:

- bucket total size fell from `$4111.03` actual to `$62.43` replayed
- bucket P&L improved from `-$275.80` to `+$13.59`

This means current `v1` is a strong safety layer for buckets, but it is not yet a proven bucket-selection model. Most of the improvement comes from sharply reducing exposure.

### `v1` hidden-gem disagreement controls

Current default policy:

- `normal` hidden gems: allowed
- `suspicious` hidden gems: allowed with `size_multiplier = 0.35`
- `exceptional` hidden gems: allowed only if “perfect evidence” passes
- otherwise `exceptional` trades are skipped with reason code `weather_extreme_disagreement_without_perfect_evidence`

Current “perfect evidence” requirements:

- `weather_station_mapping == exact`
- known volume required
- `weather_confidence_score >= 0.90`
- `source_agreement_score >= 0.85`
- `distribution_probability >= 0.20`

If perfect evidence passes:

- `exceptional_size_multiplier = 0.10`

If perfect evidence fails:

- skip

Current default does **not** enable a tiny-probe fallback for exceptional trades.

### `v1` evidence helper state

Current shared-core evidence derivation can emit:

- `weather_station_mapping`
- `weather_station_resolution`
- `weather_confidence_score`
- `source_agreement_score`
- `distribution_probability` when supplied
- `market_volume`
- `volume_known`

Important limitation:

- in the archived replay baseline used for this spec, many intended evidence fields were still absent or unpopulated
- the historical compare set therefore reflects a partially instrumented state, not a mature weather-evidence stack

### `v1` Prediction Lab state

Prediction Lab can already attach `weather_risk` metadata to prediction rows and market snapshots when the market is weather-related or weather risk is derivable. This is the correct place to collect the data required for `v2` / `v3` / `v4`.

## Uncertainty and What Is Not Proven

### `tail_high` improvement may be luck

The strongest reason not to overreact is simple:

- `tail_high` only has `8` trades in the archived set
- the replay regression is dominated by `3` exceptional skips
- one skipped winner contributed `+$142.67` actual P&L by itself

That creates a plausible path where a small `tail_high` carveout improves replay results. It also creates an equally plausible path where the apparent edge is mostly variance.

### Custom `tail_high` carveout is hypothesis only

Explicit rule for this spec:

- any custom `tail_high` carveout is a **hypothesis only**
- it is **not** proven skill
- it must be labeled experimental in code, config, reporting, and replay analysis until a larger clean-label sample exists

No policy text in this spec should be interpreted as “we know `tail_high` is good.”  
The correct interpretation is “current `v1` may be over-skipping one tail cohort, and we want a controlled way to test that hypothesis.”

### Buckets remain the main proven danger

The archived evidence is much clearer on buckets than on tails:

- `46` bucket trades
- large negative actual P&L
- strong improvement after severe size suppression

So the burden of proof remains asymmetrical:

- `bucket` needs proof before expansion
- `tail_high` only gets an experimental carveout
- `tail_low` stays more permissive than buckets, but not because it is proven either

## Finalized `v2`: Tail Evidence Tiers While Keeping Buckets Constrained

### `v2` objective

`v2` is the next implementation step.

Goal:

- preserve the current `v1` bucket safety layer
- stop treating all exceptional tails as equally bad
- add explicit evidence tiers and reason codes
- gather data without pretending distributions and station-history calibration already exist

### `v2` shape policy

#### `bucket`

In `v2`, buckets remain constrained.

- keep current bucket caps and size penalties
- require exact station mapping for any future relaxation
- do not restore normal bucket sizing in `v2`
- do not approve buckets from point forecast confidence alone
- if evidence is weak or mapping is inferred/unknown, keep near-disable behavior

Operational stance:

- `v2` buckets are a protected cohort, not a rediscovered edge

#### `tail_low`

In `v2`, `tail_low` can remain tradable under current conservative flow.

- keep `none` and `normal` eligible
- keep `suspicious` eligible with hard resize
- do not add a `Tier 0` exceptional carveout
- do not reopen `>= 15x` `tail_low` by default from this evidence set

#### `tail_high`

In `v2`, `tail_high` gets the only experimental carveout.

- keep `none` and `normal` eligible
- keep `suspicious` eligible with resize
- replace blanket exceptional skipping with an evidence-tier path

### `v2` tail evidence tiers

#### Tier 0: experimental probe lane

This tier exists only because the archived baseline lacks full evidence fields. It is a bridge, not a permanent destination.

`tail_high` only:

- shape must be `tail_high`
- station mapping must be `exact`
- disagreement tier must be `exceptional` (`>= 15x`)
- `win_probability >= 0.35`
- missing `distribution_probability` is tolerated
- unknown volume is tolerated

Suggested initial probe sizing:

- `size_multiplier = 0.35`
- `max_position_pct = 0.01`
- `max_position_usd = 100`

Constraints:

- this is an experiment, not the default forever
- all Tier 0 trades must be reported separately in Prediction Lab and replays
- Tier 0 should be retired or promoted only after materially more clean-label data exists

`tail_low`:

- no Tier 0 carveout

#### Tier 1: supported evidence

Use once distribution and station evidence are populated reliably.

`tail_high`:

- exact station mapping
- `source_agreement_score >= 0.30`
- `distribution_probability >= 0.15`
- `probability_multiple >= 10`
- if volume remains unknown, keep Tier 0 sizing
- if volume is known, modestly relax size

`tail_low`:

- exact station mapping
- `source_agreement_score >= 0.35`
- `distribution_probability >= 0.20`
- volume known
- `10x <= probability_multiple < 15x`
- still heavily resized

#### Tier 2: strong evidence

This is the first tier that should behave like a real permission path rather than a probe.

`tail_high`:

- exact station mapping
- `source_agreement_score >= 0.55`
- `distribution_probability >= 0.22`
- volume known
- moderate disagreement only; do not let Tier 2 become an excuse for uncapped exceptional exposure

`tail_low`:

- exact station mapping
- `source_agreement_score >= 0.60`
- `distribution_probability >= 0.30`
- volume known
- still stricter than ordinary tails, especially for extreme disagreement

### `v2` required outputs

Every weather decision path must emit explicit reason codes for:

- bucket cap applied
- volume unknown penalty
- low-volume penalty
- suspicious hidden-gem resize
- exceptional hidden-gem skip
- Tier 0 / Tier 1 / Tier 2 tail approval
- station mapping failure

`v2` is successful if:

- bucket risk remains tightly capped
- `tail_high` can be tested without re-opening broad tail risk
- the system produces cleaner evidence for `v3`

## Finalized `v3`: Distribution Scoring

### `v3` objective

Make bucket approval depend on probability mass, not point estimates.

### `v3` scoring model

For weather markets, estimate a distribution around the relevant official observation:

```text
actual_temp ~ Distribution(mean, spread, station_error, time_to_close)
P(bucket)    = P(lower <= actual_temp <= upper)
P(tail_low)  = P(actual_temp < threshold)
P(tail_high) = P(actual_temp > threshold)
```

Minimum `v3` requirements:

- compute `distribution_probability` for all weather shapes
- record forecast mean
- record forecast spread / uncertainty
- record distance from forecast mean to bucket center or tail threshold
- use official station-aligned inputs, not generic city labels

### `v3` bucket rules

Buckets should only become selectively tradable in `v3` if all are true:

- exact station mapping, or an explicit experimental label if the mapping is not exact
- valid distribution probability
- minimum bucket probability passes
- source agreement is acceptable
- size remains capped
- station-history penalties do not invalidate the setup

2026-05-06 update from paper/hotfix review:

The current paper and Prediction Lab samples showed the old bucket model could create apparent 20x–40x hidden gems from point forecasts that still resolved badly. The durable rule should preserve true hidden-gem upside without letting point forecasts masquerade as bucket probability.

Initial coarse `v3` bucket approval gate:

```text
entry_price <= 0.05
AND distribution_probability is present
AND distribution_probability >= entry_price + 0.05
AND distribution_probability >= 3 * entry_price
AND source/station evidence passes minimum quality gates
```

Interpretation:

- a `$0.01` bucket with `distribution_probability = 0.24` remains eligible as a 24x-style hidden gem, subject to source/station and sizing caps
- a `$0.03` bucket with only a nearby point forecast and no distribution probability is rejected
- a `$0.05` bucket with `distribution_probability = 0.08` is not enough; the probability is above market but not high enough to justify hidden-gem treatment
- current Phase 3F source/station minimums are exact station mapping and `source_agreement_score >= 0.65`, enforced only through the beta-gated bucket distribution path

Important:

- do not set bucket thresholds by maximizing the `61`-trade replay or the 2026-05-06 paper sample
- start with coarse floors
- re-evaluate only after clean-label Prediction Lab samples accumulate
- keep bucket sizing capped even when the bucket passes; approved buckets are still a lottery lane, not normal exposure

### `v3` tail rules

Tails should also use distribution scoring, even if they remain easier than buckets:

- tail approvals should depend on threshold mass, not only point forecast
- extreme disagreement should be harder to justify when distribution mass is weak
- Tier 0 `tail_high` probes should be reduced or retired once real distribution scoring exists
- current Phase 3G beta behavior uses candidate-side `distribution_probability` for tails when populated and records `distribution` vs `bridge` scoring basis in the hidden-gem evidence card; missing distribution data keeps the live-probability bridge path instead of widening exposure

`v3` is successful if:

- bucket approvals become explainable by probability mass rather than blanket micro-sizing
- tail approvals become evidence-based rather than anecdotal

## Finalized `v4`: Station Historical Error

### `v4` objective

Add station-specific calibration so the system learns where bucket confidence should be discounted.

### `v4` station history data

Track per official station:

- station CLI / station ID
- market shape
- direction
- forecast mean at entry
- distribution probability at entry
- official realized max/min
- signed error in degrees
- absolute error in degrees
- trade result
- P&L

### `v4` station-history usage

Use station history to build a `station_history_score` that can:

- widen the effective uncertainty band for noisy stations
- cap bucket approvals when historical error is too high
- downsize tails at stations with unstable threshold performance
- distinguish exact-mapped but low-quality stations from exact-mapped and reliable stations

Rules:

- do not trust tiny per-station samples
- require minimum sample counts before station-specific adjustments become strong
- use shrinkage toward the global weather prior when station history is sparse

`v4` is successful if:

- station calibration explains persistent wins/misses better than raw forecast confidence
- bucket approvals stop treating all exact mappings as equally trustworthy

## Prediction Lab Clean-Label Requirements

Prediction Lab is the evidence engine for this rollout. The quality of conclusions is constrained by label quality.

Only count a row as a clean training/evaluation label if it has a definitive outcome:

- explicit `result` / `outcome` / `settlement_value` that normalizes to `YES`, `NO`, or `VOID`
- or status in `settled`, `resolved`, or `finalized` with definitive `close_price` of `1` or `0`

Do **not** treat these as clean labels:

- `closed` markets with terminal quotes but no definitive settlement
- `closed-unsettled` markets where `close_price` looks terminal but status is not definitive
- ambiguous rows missing a resolvable settlement outcome

Operational rule:

- ambiguous rows may be kept for diagnostics
- ambiguous rows must not be used to claim model or policy improvement

These requirements align with the current collector behavior and tests: Prediction Lab already rejects unresolved terminal-quote states as training labels and only accepts definitive settlement states.

## Prediction Lab Required Weather Fields

For every weather candidate, whether traded or skipped, Prediction Lab should record:

- `shape`
- `direction`
- `hidden_gem_tier`
- `station_cli`
- `city_code`
- `weather_station_mapping`
- `entry_price`
- `win_probability`
- `probability_multiple`
- `weather_confidence_score`
- `source_agreement_score`
- `distribution_probability`
- forecast mean
- forecast spread / uncertainty proxy
- distance to bucket center or tail threshold
- bucket width for `bucket`
- market volume / liquidity and whether it was known
- time to close
- source freshness
- source mode (`recorded`, `live`, `historical`, `synthetic`, `missing`)
- strategy lane / evidence-card lane id
- requested size
- final size
- skip / resize / tier reason code
- actual official station temp
- signed forecast error
- absolute forecast error
- outcome
- resolution type
- P&L
- P&L per dollar risked

Prediction Lab rows and market snapshots should both carry `weather_risk` metadata whenever derivable.

## Prediction Lab Required Metrics

Report at minimum:

- overall trade count, resolved count, accuracy, and net P&L
- P&L per dollar deployed
- skip / resize / unchanged counts
- winners skipped, losers skipped, and retained large winners
- by `shape`
- by `shape x hidden_gem_tier`
- by `shape x weather_station_mapping`
- by `shape x volume_known`
- by `shape x evidence tier`
- by `station_cli`
- by `resolution_type`

Interpretation rule:

- only clean-label rows should drive policy decisions
- diagnostic-only rows may explain edge cases, but they do not justify threshold changes

## Archived Baseline Comparison Protocol

Every new weather-risk pass must replay the same archived trade universe before any broader conclusion is made.

Baseline anchor:

- `data/paper/audits/baselines/weather_risk_20260427_shared_brain_v1/baseline_summary.json`

Current parity/check anchor:

- `data/paper/audits/weather_risk_replay_compare_after_resolution_guard_summary.json`

Static replay caveat:

- this replay does **not** model alternate-timeline redeployment of saved cash
- it only re-scores the trades the old bot actually took

Preserve the archived compare keys:

- `shape`
- `hidden_gem_tier`
- `current_logic_outcome`
- `old_pnl`
- `replayed_pnl`
- `old_size`
- `new_size`
- `weather_station_mapping`
- `volume_known`
- `distribution_probability`
- `source_agreement_score`

Each policy pass must compare:

- overall trade count
- overall replayed P&L
- overall total size
- per-shape trade count
- per-shape replayed P&L
- per-shape total size
- skip / resize / unchanged counts
- winners skipped
- losers skipped
- large-winner retention
- P&L saved versus actual
- P&L per dollar deployed

Protocol:

1. Replay the archived baseline universe unchanged.
2. Compare against the archived baseline summary.
3. Compare against the current post-resolution-guard summary.
4. Explain any metric drift that is not rounding.
5. Do not claim success from “less capital deployed” alone.
6. Do not promote a carveout unless it survives clean-label forward collection, not just archived replay.

Note:

- the current post-resolution-guard summary shows near-identical key metrics to the archived baseline, with small float-format differences; treat it as the active comparison anchor, not as a materially different policy result

## Final Rollout Stance

This spec resolves the policy direction as follows:

- `v1` stays in place as the current safety layer
- `v2` is the next implementation target
- `v2` keeps buckets constrained
- `v2` treats any `tail_high` exceptional carveout as an experiment only
- `v3` adds real distribution scoring
- `v4` adds station historical error

Most important conclusions:

- bucket damage is the clearest signal in the archived audit
- current replay improvement is mostly a safety win, not proof of bucket skill
- `tail_high` may be over-suppressed by the current strict exceptional skip
- that possible `tail_high` fix is a hypothesis only, not proven edge
- no part of this rollout should be tuned to “win” a `61`-trade replay
