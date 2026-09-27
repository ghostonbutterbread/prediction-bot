# Per-market weather settlement authority — integration dossier

- **Status:** feature corrected after independent review blocker; new exact-tip review and beta preflight pending.
- **Owner:** weather-authority task agent
- **Branch / owning ref:** `fix/market-weather-settlement-authority`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-weather-authority`
- **Base commit:** `28fbc7f41f89a8680a7490e7f3e6934f66913761`
- **Intended integration target:** local `beta` (currently `28fbc7f41f89a8680a7490e7f3e6934f66913761`; fetched `origin/beta` at `295f69cf3199782d192a95950ed101779adea3db`, behind local integration)
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint:** `584c9d603fcc499fa7b404dc6d5c31db4e4bfd4c`
- **Feature implementation commit(s):** `1f0ac294de7844d73c686c664c4405346e785546`, `267749d25d5ddd7b09663d460c5ab0969a209a21`, `584c9d603fcc499fa7b404dc6d5c31db4e4bfd4c`

## Intent and contract

Correct the false hardcoded NWS settlement authority for Kalshi daily weather markets without changing forecast selection, confidence, scoring or execution. Exact market metadata (`rules_primary`/`rules_secondary`) travels exchange → enhanced strategy → live weather feed before lab snapshot. Explicit rule text identifies NWS or The Weather Company; missing, unrecognized or conflicting provider text is `unknown` with a reason. `settlement_authority` records its source/status/evidence field/origin, while `forecast_driver` and 1/0 source roles/weights describe the independent existing NWS-first forecast policy. Snapshot source rows and sealed replay inputs retain both. Older records with only a `settlement_source` label do not acquire rule authority. No backdated rule capture, final TWC temperature, report-equivalent NWS low, independent resolution, 50/50 imputation, new gate or runtime activation is claimed.

## Evidence and review

- RED→GREEN receipts: TWC end-to-end first failed `None != the_weather_company`; NWS incidental/conflict classification failed `nws != unknown`; paper label failed TWC provider treated as target; unsupported equal weights failed; old lab signal and `settlement_primary` role failed; each targeted test passed after its fix.
- `PYTHONPATH=.:tests python -m unittest test_weather_settlement_authority test_weather_forecast_targeting test_replay_decision_input test_prediction_lab_collect test_paper_shadow_lanes test_beta_data_flow_e2e test_source_observation_ledger -q`: 246 tests, OK (1 skipped).
- `PYTHONPATH=.:tests python -m unittest discover -s tests -q`: 1334 tests, OK (7 skipped); final receipt log `/home/ryushe/.hermes/cache/scratch/weather-authority-full-suite-final.log` (ephemeral, not a repository artifact).
- `git diff --check`: clean. Existing `test_source_observation_ledger` confirms nighttime NWS `/forecast` low is `partial_day_low_proxy_not_full_day` in strict history. Synthetic tests of KXLOWTPHX-26SEP24-T73 and KXLOWTPHX-26AUG01-T85 assert rule classification, not historical decision-time rule availability.
- `deleg_761c42c4` **BLOCKED** clean tip `84062627fe4f6e6a89e2f9fbc9506cdfda8a07e1` for false authority on negated attribution (TWC or NWS) and contradictory legacy `weight: 1.0`/`cross_validation` role. Both targeted regressions failed before correction and passed afterward; affirmative exact-rule positives remain. Independently reviewed focus: 246 OK (1 skipped); reviewer's full rerun timed out, so do not claim independent full success.
- Corrected feature focused: `PYTHONPATH=$PWD:$PWD/tests python -m unittest test_weather_settlement_authority test_weather_forecast_targeting test_replay_decision_input test_prediction_lab_collect test_paper_shadow_lanes test_beta_data_flow_e2e test_source_observation_ledger -q`: **248 OK, 1 skipped**. Full isolated discover: **1,336 OK, 7 skipped**, receipt `/home/ryushe/.hermes/cache/scratch/weather-authority-review-correction-full-20260927.log`. New exact-tip independent verdict remains pending.
- `deleg_ed36c285` **BLOCKED** corrected tip `154756dfbea7c8e4fa63db558409ab07a1f97ba7`: passive negation (`will not be determined according to TWC`, `will not be reported by NWS`) still produced false authority through the sealed input. It also identified a misleading paper diagnostic: an explicitly observed NWS daily archive under a TWC-settled market was classified settlement-derived. The reviewer independently ran **248 focused OK, 1 skipped** and **1,336 full OK, 7 skipped**. Both new cases were reproduced RED, then corrected GREEN: passive negation stays unknown; the TWC/NWS archive classification is independent only for explicit observation/archive-like labels, not forecast/proxy or NWS-settled labels.
- After this correction the isolated focused suite passed **248 OK, 1 skipped** and full discover passed **1,336 OK, 7 skipped**; full receipt `/home/ryushe/.hermes/cache/scratch/weather-authority-passive-negation-full-20260927.log`. A new exact-tip independent verdict is required before beta preflight.
- Independent read-only review: pending again on the latest correction; no non-committing beta preflight or integration yet.

## Blockers, deferred work and decision gates

- **Integration gate:** fresh independent review and clean beta non-committing preflight at current local target with focused/full tests on the combined tree; exclude this dossier and leave other-owned worktrees untouched. If beta advances, reconcile its unique commits first.
- **Activation gate:** separately validate fresh collector decision-time `rules_primary` coverage, new output provenance and exact market identity in an observer cohort before relying on authority claims. Do not infer availability from today's public API for an older decision.
- **Promotion gate:** exact independently finalized settlements and forward holdout; synthetic source labels and paper forecasts are not outcomes, full-day weather reports, fills or P&L evidence.

## Handoff

- **Owning feature branch/ref:** `fix/market-weather-settlement-authority`
- **Latest immutable recovery checkpoint:** `584c9d603fcc499fa7b404dc6d5c31db4e4bfd4c`
- **Feature implementation commit(s):** `1f0ac294de7844d73c686c664c4405346e785546`, `267749d25d5ddd7b09663d460c5ab0969a209a21`, `584c9d603fcc499fa7b404dc6d5c31db4e4bfd4c`
- **Exact resume point:** inspect code commit and this later dossier commit, obtain independent review, then beta preflight only on explicit integration assignment.
- **Working-tree state at handoff:** expected clean after separate commits.

2026-09-27 — scoped feature implementation and synthetic tests complete; no merge, push, service/config/wallet changes.
