# Legacy scoreboard date-validation precedence — integration dossier

- **Status:** feature and exact staged beta tree independently accepted for an intentional local merge only; no push or activation
- **Owner:** `t_50cd1699`
- **Branch / worktree:** `fix/legacy-scoreboard-date-validation` at `/home/ryushe/worktrees/prediction-bot-scoreboard-date-validation`
- **Base / intended target:** clean local `beta` `09bc0eefa659b04af956bc839a404a269c48f939` (not `main` or `origin/beta`)
- **Latest immutable recovery checkpoint / implementation commit:** `1fb7218414a8e286e2513e8b2aab9a77ed1d8168` (code and two RED→GREEN tests; a later dossier-only handoff commit follows)
- **Last updated:** 2026-09-27

## Intent and implemented contract

An explicit root-only `date_validation: {"ok": false}` was ignored when an earlier absent snapshot produced `{}`: `_first_dict` selected that empty mapping before reaching the root flag. A synthetic row with separately recorded `actual_temp=68`, NWS forecast 72, and root reason `root_flag` incorrectly produced `observations_scored=1`. The source-outcome ledger inherits this scoring extraction.

The small fix in `bot/weather/source_scoreboard.py::_date_validation_problem` skips empty mappings when selecting the date comparison context and checks all explicitly recorded `ok is False` flags in snapshot/live/weather/root priority before accepting an actual. It retains the existing first populated context for market/weather date comparison; missing optional validation does not disqualify legacy rows. No forecast producer, actual lookup, authoritative Kalshi YES/NO resolution, raw ledger, wallet, runtime, config, or service is changed. The branch does not rewrite historical derived scoreboards.

## Test-first evidence

- RED 1: `tests.test_weather_source_scoreboard.WeatherSourceScoreboardTests.test_root_only_failed_date_validation_prevents_scoring` failed `AssertionError: 68.0 is not None`; a minimal skip-empty change made it green.
- RED 2: `tests.test_weather_source_scoreboard.WeatherSourceScoreboardTests.test_root_failure_is_not_masked_by_populated_nested_validation` failed the same assertion with populated nested `ok:true` and root `ok:false`; checking all explicit false validations made it green. Existing nested false control stayed green.
- Focused: `PYTHONPATH="$PWD:$PWD/tests" python3 -B -m unittest tests.test_weather_source_scoreboard tests.test_source_reliability_replay_evidence tests.test_weather_source_confidence -q` — 58 tests, OK.
- Full isolated: `PYTHONPATH="$PWD:$PWD/tests" python3 -B -m unittest discover -s tests -q` — 1,350 tests, OK (7 skipped); log `/home/ryushe/.hermes/cache/scratch/prediction-date-validation-full-20260927.log`.
- Independent feature review ACCEPTED exact clean tip `08a368f965f8fee35bfb50075cc126f4e3c8fd07` for a new noncommitting beta preflight only; independently reran 58 focused tests and inspected 1,350 full tests (7 skipped).
- Independent combined-tree review ACCEPTED exact staged beta tree `717f679fe1164b6f2e6e00f1b04715b0215b3161` for an intentional local merge only. Beta HEAD was `09bc0eefa659b04af956bc839a404a269c48f939`, MERGE_HEAD was `08a368f965f8fee35bfb50075cc126f4e3c8fd07`, dossier excluded; reviewer reran 31 focused and 1,350 full tests (7 skipped). No merge, push, deployment, or service change at this dossier checkpoint.

## Boundaries and next gate

This protects explicitly invalid date evidence but does not prove independently sourced actual temperatures in old rows, real-cohort rescoring, historical rule capture, or TWC measurements. The feature and exact-tree reviews checked valid and invalid date paths; parent may recreate the exact staged tree, rerun focused/full suites, and merge locally only. No activation or ranking promotion is authorized.

## Interruption / resume

- **Implementation commit:** `1fb7218414a8e286e2513e8b2aab9a77ed1d8168`, reachable from the owning feature ref.
- **Exact resume point:** from clean local beta HEAD `09bc0eefa659b04af956bc839a404a269c48f939`, restage this feature (including this later dossier-only verdict), remove this dossier from the index, prove staged tree `717f679fe1164b6f2e6e00f1b04715b0215b3161`, rerun focused/full tests, then commit the intentional local beta merge. Any identity or test change requires new review.
- **Working tree:** clean after the dossier-only commit.
