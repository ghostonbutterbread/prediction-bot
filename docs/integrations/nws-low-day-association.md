# NWS overnight-low target association — integration dossier

- **Owner / branch:** Hermes, Kanban `t_ebcc5d16`; `fix/nws-low-day-association`
- **Base / target:** `f4fa746e0484c083ee0ea5c019caae33058bb925` → `beta` (review gate required)
- **Status:** feature checkpoint, independent review pending; no merge/push/deploy/runtime activation

## Defect and intent

For a September 27 daily-low target, a synthetic NWS response with September 26 night ending September 27 at 58°F, September 27 day at 84°F, and September 27 night ending September 28 at 68°F returned 68°F as the September 27 low. The source target mapping pointed to the daytime high period, not the selected low period. This can corrupt decision-time low signals and later source-history proof.

The corrected *NWS proxy* selects a nighttime period ending on the target morning when available and binds a low decision's source-period metadata to that period. If no such nighttime period is available, only NWS low evidence is marked unavailable; a daytime high can still use the daytime period and other forecast providers remain eligible. The weather-engine cache now distinguishes high and low requests. The strict source-period proof accepts a prior-evening start only for a low period with a retained end on the target day and rejects a low period ending the following day. Older records lacking optional period bounds retain their existing treatment.

This does **not** establish that a 12-hour NWS nighttime low exactly equals a market's full 24-hour minimum; it removes the demonstrably wrong next-morning association. It does not fix the separate unconditional `settlement_source=nws` assumption. Check each market's published rules before claiming settlement-source provenance; official Kalshi rules for `KXLOWTPHX-26SEP24-T73` name The Weather Company. Do not change active configs or promote source history from this checkpoint.

## Evidence / gates

- RED before fix: two NWS target tests failed on unrecognized `measurement_kind`; the source-proof test failed with `unusable_source_period_target_mismatch` for the prior-night low.
- GREEN on worktree-first PYTHONPATH: `python -m unittest tests.test_weather_forecast_targeting tests.test_source_observation_ledger -q` — 31 tests OK.
- Full isolated suite: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1,193 tests OK (7 skipped), 63.904s; receipt `/home/ryushe/.hermes/cache/scratch/prediction-nws-low-full-suite-20260927.log`.
- `git diff --check` passed. Independent read-only review, beta integration compatibility, and market-rule-driven settlement-source audit remain open.

## Handoff

Review NWS period selection, kind-specific high coverage, source-metadata/strict-history parity, behavior when prior night is absent, and whether 12-hour proxy use should remain eligible for full-day source training. If reviewer finds a correctness blocker, add a focused regression, fix here, rerun focused/full isolated checks, and obtain a fresh verdict. The source-authority assumption and other audit findings remain on audit card `t_500d82fb`, not part of this branch. Keep all raw snapshots, wallets, orders, services, and ignored runtime config untouched.
