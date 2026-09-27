# Per-market weather settlement authority — integration dossier

- **Status:** feature (local gate passed; independent review/integration pending)
- **Owner:** weather-authority task agent
- **Branch / owning ref:** `fix/market-weather-settlement-authority`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-weather-authority`
- **Base commit:** `28fbc7f41f89a8680a7490e7f3e6934f66913761`
- **Intended integration target:** `beta` (fetched `origin/beta` at `295f69cf3199782d192a95950ed101779adea3db`; feature base is older)
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint:** `1f0ac294de7844d73c686c664c4405346e785546`
- **Feature implementation commit(s):** `1f0ac294de7844d73c686c664c4405346e785546`

## Intent and contract

Correct the false hardcoded NWS settlement authority for Kalshi daily weather markets without changing forecast selection, confidence, scoring or execution. Exact market metadata (`rules_primary`/`rules_secondary`) travels exchange → enhanced strategy → live weather feed before lab snapshot. Explicit rule text identifies NWS or The Weather Company; missing, unrecognized or conflicting provider text is `unknown` with a reason. `settlement_authority` records its source/status/evidence field/origin, while `forecast_driver` and 1/0 source roles/weights describe the independent existing NWS-first forecast policy. Snapshot source rows and sealed replay inputs retain both. Older records with only a `settlement_source` label do not acquire rule authority. No backdated rule capture, final TWC temperature, report-equivalent NWS low, independent resolution, 50/50 imputation, new gate or runtime activation is claimed.

## Evidence and review

- RED→GREEN receipts: TWC end-to-end first failed `None != the_weather_company`; NWS incidental/conflict classification failed `nws != unknown`; paper label failed TWC provider treated as target; unsupported equal weights failed; old lab signal and `settlement_primary` role failed; each targeted test passed after its fix.
- `PYTHONPATH=.:tests python -m unittest test_weather_settlement_authority test_weather_forecast_targeting test_replay_decision_input test_prediction_lab_collect test_paper_shadow_lanes test_beta_data_flow_e2e test_source_observation_ledger -q`: 246 tests, OK (1 skipped).
- `PYTHONPATH=.:tests python -m unittest discover -s tests -q`: 1334 tests, OK (7 skipped); final receipt log `/home/ryushe/.hermes/cache/scratch/weather-authority-full-suite-final.log` (ephemeral, not a repository artifact).
- `git diff --check`: clean. Existing `test_source_observation_ledger` confirms nighttime NWS `/forecast` low is `partial_day_low_proxy_not_full_day` in strict history. Synthetic tests of KXLOWTPHX-26SEP24-T73 and KXLOWTPHX-26AUG01-T85 assert rule classification, not historical decision-time rule availability.
- Independent read-only review: pending parent/reviewer. No non-committing beta merge or integration test requested on this branch; owner must preflight against current beta.

## Blockers, deferred work and decision gates

- **Integration gate:** independent review and clean beta non-committing preflight at current target with focused/full tests on the combined tree; no feature merge/push here. Reconcile this older feature base with target's unique commits (including other-owned changes) without touching another worktree.
- **Activation gate:** separately validate fresh collector decision-time `rules_primary` coverage, new output provenance and exact market identity in an observer cohort before relying on authority claims. Do not infer availability from today's public API for an older decision.
- **Promotion gate:** exact independently finalized settlements and forward holdout; synthetic source labels and paper forecasts are not outcomes, full-day weather reports, fills or P&L evidence.

## Handoff

- **Owning feature branch/ref:** `fix/market-weather-settlement-authority`
- **Latest immutable recovery checkpoint:** `1f0ac294de7844d73c686c664c4405346e785546`
- **Feature implementation commit(s):** `1f0ac294de7844d73c686c664c4405346e785546`
- **Exact resume point:** inspect code commit and this later dossier commit, obtain independent review, then beta preflight only on explicit integration assignment.
- **Working-tree state at handoff:** expected clean after separate commits.

2026-09-27 — scoped feature implementation and synthetic tests complete; no merge, push, service/config/wallet changes.
