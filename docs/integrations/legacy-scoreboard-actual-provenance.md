# Legacy scoreboard actual-temperature provenance — integration dossier

- **Status:** feature and exact combined beta tree independently accepted for an intentional local merge only; no push or activation
- **Owner:** task `t_5743e6af` (focused subagent)
- **Canonical path:** `docs/integrations/legacy-scoreboard-actual-provenance.md` (branch-local; remove at integration)
- **Branch / owning ref:** `fix/legacy-scoreboard-actual-provenance`
- **Base commit:** `9e7f83e4f4d507a56413ddf91d546fd1f78c8250` (clean local `beta`)
- **Intended integration target:** local `beta`, not `main` or `origin/beta`
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint:** `eea0754c8b5e6da6eacf7f456406323d0c967224`
- **Feature implementation commit(s):** `eea0754c8b5e6da6eacf7f456406323d0c967224`
- **Inspiration:** `bot/feeds/weather_pro.py` assigns `actual_temp_used` from selected forecast high/low; `bot/feeds/live_data.py` uses the same label for a forecast; `docs/architecture/weather_source_scoreboard_evidence_20260516.md` documents misleading perfect-match slices.

## Intent and implemented contract

At the shared `bot/weather/source_scoreboard.py` extraction boundary, a decision-time `actual_temp_used` in root, `weather`, `weather_source_snapshot.forecast`, or live signal data is not an observed temperature. Nor do `actual_temp`/`resolved_temp` inside the **forecast** snapshot alone establish resolution. Preserve source forecast extraction and coverage diagnostics, but leave actual missing unless separately recorded. The ledger in `bot/weather/source_reliability.py` consumes that observation unchanged: it keeps predicted direction/outcome, but no actual outcome, direction correctness, bias, or absolute error and marks reliability ineligible when temperature is missing. Explicit caller `actual_lookup` wins; standalone actual/observed/resolved and resolution temperatures remain available under existing date checks. An official YES/NO result is not converted to temperature. The separate source-side edge evaluator's official-outcome selection is unchanged; a supplied official outcome enables its separate diagnostic without a temperature, while an unbound row remains blocked.

No forecast producer, raw ledger, wallet, service, runtime, or configuration changes. Historical materialized scoreboards and ledgers are not rewritten by this branch. This correction does not attest the provenance of every generically named `actual_temp` elsewhere or upgrade any historic label to independent settlement evidence.

## Evidence and review

- **RED:** `PYTHONPATH=<worktree> python3 -m unittest tests.test_weather_source_scoreboard.WeatherSourceScoreboardTests.test_forecast_derived_actual_fields_remain_coverage_only -v` failed `AssertionError: 72.0 is not None` before production edit.
- **GREEN focused:** `PYTHONPATH=<worktree> python3 -m unittest tests.test_weather_source_scoreboard tests.test_source_reliability_replay_evidence -q` — 32 tests, OK after final source-only assertion.
- **Full isolated suite:** `PYTHONPATH=<worktree> python3 -m unittest discover -s tests -q` — 1343 tests, OK (7 skipped), after final source-only assertion.
- **Coverage:** forecast-only root/weather/forecast/live aliases; forecast-snapshot direct `actual_temp` and `resolved_temp`; lookup precedence; resolution/observed actuals; date failure; YES/NO no-imputation; source-only edge binding; CLI ledger coverage.
- **Independent feature review:** ACCEPTED clean tip `ff7f741177a372dd25a5a10d04f24676d2437f5f` for a new noncommitting preflight only; 32 focused, 1,343 full (7 skipped). Reviewer reproduced forecast-only unscored and explicit independently supplied actual still scored.
- **Independent combined-tree review:** ACCEPTED exact staged local-beta tree `746ecf1fb3d8f88d9a7de3c673edeb75d09e33eb` for an intentional local merge only; both feature parents staged, both dossiers excluded, combined 207 focused (1 skipped) and 1,348 full (7 skipped). No beta merge, push, or runtime activation at this dossier checkpoint.
- **Ancestry:** task branch based on local `beta` at base above. Remote `origin/beta` was fetched read-only and is a different commit (`295f69cf3199782d192a95950ed101779adea3db`); do not silently change the assigned local target.

## Blockers and deferred data limits

- **Legacy real-data re-score:** no frozen independent observed-temperature lookup and bounded raw cohort was supplied. Run the offline scoreboard/ledger on a separately sourced, market/date/kind-matched actual lookup and compare coverage, error, and rankings to prior outputs *before* any ranking/promotion use; do not treat synthetic fixtures as empirical validation. Parent controls cohort selection and approval.
- **Existing date-validation quirk (out of scope):** `_date_validation_problem` calls `_first_dict` with an empty `weather.date_validation` mapping before a populated root `date_validation`, so root-only `ok: false` can be masked. Existing nested live/snapshot date check still works and is tested. Separate focused test-first repair should resolve empty-dict precedence before trusting root-only date flags in a real cohort; do not conflate with this alias fix.
- **Legacy generic actual fields:** top-level/weather/live `actual_temp` and `resolved_temp` remain admissible for compatibility; their independent provenance is not mechanically proven. Do not promote derived reliability without a separately documented observation source or lookup.

## Interruption / resume handoff and decision gates

- **Owning feature branch/ref:** `fix/legacy-scoreboard-actual-provenance`
- **Latest immutable recovery checkpoint / implementation commits:** `eea0754c8b5e6da6eacf7f456406323d0c967224` (reachable from this branch; current tip includes this later dossier-only handoff commit).
- **Exact resume point:** from clean local beta HEAD `9e7f83e4f4d507a56413ddf91d546fd1f78c8250`, stage both accepted feature branches, remove both dossiers, verify index tree `746ecf1fb3d8f88d9a7de3c673edeb75d09e33eb`, rerun focused/full, then local beta merge. If identity or tests differ, stop for re-review.
- **Working-tree state at handoff:** clean after dossier-only handoff commit.
- **Integration gate:** independent exact-tree review authorizes the matching local beta merge only; exclude this branch-local dossier from beta. A changed staged code tree requires new review.
- **Activation / cohort gate:** no activation in this task; independent actuals required before promoting rankings.
- **Promotion gate:** fresh independently resolved forward overlap; synthetic success is not performance evidence.

## Decision record

- 2026-09-27 — Chose exclusion at actual-extraction seam rather than rewriting producer values, which remain useful for decision diagnostics; no own beta merge/push/runtime activation.
- 2026-09-27 — Implementation checkpoint `eea0754c8b5e6da6eacf7f456406323d0c967224` passed focused 32-test and full 1343-test isolated suites (7 skipped); handoff-only commit follows.
- 2026-09-27 — independent feature and exact combined-tree reviews accepted their distinct gates; root-only date-validation masking is tracked separately as `t_50cd1699` before real-cohort ranking use.
