# NWS overnight-low target association — integration dossier

- **Owner / branch:** Hermes, Kanban `t_ebcc5d16`; `fix/nws-low-day-association`
- **Base / target:** `f4fa746e0484c083ee0ea5c019caae33058bb925` → `beta` (review gate required)
- **Status:** initial `83d1c21` review blocked integration; corrective feature checkpoint awaits independent re-review and beta-first compatibility; no merge/push/deploy/runtime activation

## Defect and intent

For a September 27 daily-low target, a synthetic NWS response with September 26 night ending September 27 at 58°F, September 27 day at 84°F, and September 27 night ending September 28 at 68°F returned 68°F as the September 27 low. The source target mapping pointed to the daytime high period, not the selected low period. This can corrupt decision-time low signals and later source-history proof.

The corrected *NWS proxy* selects a nighttime period ending on the target morning when available and binds a low decision's source-period metadata to that period. If no such nighttime period is available, only NWS low evidence is marked unavailable; a daytime high can still use the daytime period and other forecast providers remain eligible. The weather-engine cache distinguishes high and low requests. A valid requested high/low can now score even if the unrelated period's temperature is missing; source confidence and aggregation tolerate that missing metric rather than inventing one. The strict source-period proof accepts a prior-evening start only for a low period with a retained end on the target day and rejects a low period ending the following day. Older records lacking optional period bounds retain their existing treatment.

The overnight low is explicitly tagged `source_local_nws_night_low_proxy`. A matching morning establishes the *date association*, not full-day coverage: paper decisions and raw/proxy audit evidence remain available, but the strict source proof marks this proxy `partial_day_low_proxy_not_full_day` and excludes it from chronological source history. Older bounded NWS low records using the former `exact_source_local_nws_period` tag are likewise excluded. Conflicting retained period instants (including aliases on the same day) and a source timestamp at/after its retained period end cannot prove strict history. This does **not** establish that a 12-hour NWS nighttime low equals a market's full 24-hour minimum. It does not fix the separate unconditional `settlement_source=nws` assumption. Check each market's published rules before claiming settlement-source provenance; official Kalshi rules for `KXLOWTPHX-26SEP24-T73` name The Weather Company. Do not change active configs or promote source history from this checkpoint.

## Evidence / gates

- RED before fix: two NWS target tests failed on unrecognized `measurement_kind`; the source-proof test failed with `unusable_source_period_target_mismatch` for the prior-night low.
- Initial independent read-only review reproduced a 58°F 12-hour proxy entering strict full-day source history, high-only coverage suppressed by a missing low, inconsistent same-day period aliases, and an after-period `source_as_of`; verdict **block integration as-is**. Complete transcript: `/home/ryushe/.hermes/cache/delegation/live/deleg_3c0a1ce4/task-0.log`.
- Corrective RED cases: requested-kind adapter coverage failed; settled low proxy remained eligible for strict source history; same-day alias conflict was accepted. Added a post-period timestamp regression and engine high/low partial-period scoring coverage.
- Corrective GREEN on worktree-first PYTHONPATH: `python -m unittest tests.test_weather_forecast_targeting tests.test_source_observation_ledger -q` — 35 tests OK.
- Corrective full isolated suite: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1,197 tests OK (7 skipped), 61.873s; receipt `/home/ryushe/.hermes/cache/scratch/prediction-nws-low-review-correction-full-20260927.log`.
- `git diff --check` passed. Corrective independent re-review, beta integration compatibility, and market-rule-driven settlement-source audit remain open.

## Handoff

Re-review the corrected proxy classification through collector normalization and strict source history, high/low partial coverage, alias/source-as-of proof, and cache isolation. If a correctness blocker remains, add a focused regression, fix here, and rerun focused/full isolated checks before beta integration. The source-authority assumption and other audit findings remain on audit card `t_500d82fb`, not part of this branch. Keep all raw snapshots, wallets, orders, services, and ignored runtime config untouched.
