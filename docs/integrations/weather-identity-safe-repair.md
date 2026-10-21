# Weather identity repair and retained-history reconciliation

## Ownership and intent

- User request: patch upstream weather identity handling and safely repair existing data (Discord thread `1562242778174328893`).
- Task: `t_ca47d502`.
- Feature: `fix/weather-identity-safe-repair`.
- Worktree: `/home/ryushe/worktrees/prediction-bot-weather-identity-safe-repair`.
- Base: `0c193e9372771c35a6eeb71407ec5cd9c7c39da6` (fetched beta).
- Intended integration: reviewed **beta code only**. Stable `main` is not this change's integration target and currently has no common ancestor with beta; no unrelated-history merge or stable propagation is authorized.
- Raw evidence and independent outcomes remain the canonical dataset. SourceRouter reads them directly and derives win-percentage history in memory. No SourceWriter, new history publication service, copied forecasts, or closeness ranking.

## Defect and required behavior

The aggregator's duplicate substring table selected another city for overlapping codes, appended that guessed city to the original question, and the provider then used the altered question as location evidence. Official `custom_strike.City` was dropped by exchange construction. Generic HIGH questions could also request LOW forecasts. Historical source validation could compare two mutually agreeing but wrongly derived city labels rather than the original market identity.

The implementation must preserve official exchange location/rules and original identifiers, use one registry-backed resolver, carry explicit supported city/date/measurement to the fetch boundary, leave the original question unchanged, and refuse unknown/conflicting identity without guessing. Exact series and full-contract parsing must distinguish SAN/SAT, SDF/SD, DC/EWR/TTN and family-prefix overlaps. Settlement authority and station evidence must not be inferred merely from city shorthand.

At historical consumption, independently derive intended context from original retained identity, preserve original bytes and hashes, correct only provable derived attribution in memory, and reject actual wrong-location source/market associations. Valid siblings in a mixed-source row remain independently usable. Missing timing, period, location or outcome proof cannot be invented; label correction must not promote partial-day data to strict full-day qualification.

## Evidence and current checkpoint

Persistent evidence root:
`/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/`.

- Prior causal RED evidence: `routing_root_cause_1562525967174340609/desired-behavior-red.log` and `root-cause-verification.json` under the same `derived_reports` parent.
- Implementation is split into non-overlapping collector/resolver and historical-reader surfaces. Independent exact-candidate review is owned by the parent, not nested implementer reviewers.
- Compact-index inventory and frozen original manifest receipts are being generated for the four explicitly selected retained indexed collector cohorts. This is not an assertion that all storage files are indexed or repairable.
- The oldest July cohort's sampled index entries lack per-row payload hashes and newer extent fields, but its original manifest retains a whole-source digest. That is a different verification capability, not proof that the source has no original integrity anchor. Do not synthesize per-row historical verification or rewrite its index. Newly computed audit-time digests must be labelled as such. The Aug 1 legacy index also contains more entries than its manifest declares and nonmonotonic locator transitions; report actual entry counts and do not apply version-2 committed-read claims to that format.
- Collector implementation returned with exact-identity and strategy→aggregator→provider/retention regressions; its aggregate adjacent run is not an all-green receipt (harness/root/IPC errors retained under `collector/`). The parent will use its separate frozen offline gate rather than claiming that unexecuted rerun passed.
- Parent preserved five already-configured provider locations (Portland, Nashville, Detroit, Tampa, Death Valley) in the one registry as watch-only identities, with no new source definitions, trust or settlement-station assertions. Existing PDX/NSH/DET/TPA aliases are explicitly legacy compatibility, not verified current exchange listings; Death Valley has no invented ticker alias. `parent-legacy-registry-red.log` records 22 failing subtests before the registry repair; `parent-legacy-registry-green.log` records all 3 tests passing afterward.
- Parent review added `test_weather_identity_geography_guard.py`: known contradictory query/provider coordinates must block qualification/label repair while agreeing provider evidence and unknown noncanonical station coordinates remain supported. Initial receipt: 4 tests, 2 failures, 2 passing controls (`parent-geography-red.log`). Historical owner must repair these before acceptance.
- Compact inventory completed for 2,447,542 index entries across the four explicit cohorts. These are index entries, not unique forecasts or independent events. `full-index-inventory.json` pins every consumed prefix and distinguishes original committed proof from audit-time hashes and legacy count/order differences. Bounded canonical hydration passed for 500 rows each from three per-row-hash-capable cohorts (1,500 total); the July whole-source-digest format was not silently upgraded.
- No complete real-data reconciliation, frozen full-suite pass, independent approval or beta integration is claimed at this checkpoint. Record immutable implementation commits and exact acceptance receipts before review/integration.

## Verification gates

1. Retain new RED/GREEN test receipts for actual exchange → resolver → aggregator → provider flow, explicit HIGH/LOW, unchanged questions, conflicts and unresolved inputs.
2. Exercise real direct dataset/index + independent outcomes with correct-source/wrong-derived-label salvage, true wrong-query rejection, mixed sources, immutable input bytes and unchanged source values/clocks.
3. Run focused and full suites from a fresh exact-candidate export, offline and write-confined to task-owned synthetic output, with exact executed/skip/excluded IDs and source import/hash verification.
4. Review the complete product and audit CLI diff independently; fix reproduced blockers and obtain re-review on the corrected exact candidate.
5. Run the reviewed reconciliation/audit rules against explicit frozen verified input prefixes. Report processed scope, hash failures/unverifiable legacy rows, source-level attribution counts and unprocessed extent separately. Derived reporting must not copy forecasts or mutate original archives, index files, capture/accounting sidecars, outcomes or state.
6. For beta integration, verify the clean target and fetched remote, exclude this branch-local dossier, compare the accepted code tree, run the merged-export gates and verify the exact remote target after any push.

## Activation boundary and unresolved operational prerequisite

The selected service is `prediction-lab-strict-source-cohort-20260823.service`; the pre-change observation records PID 1590, active since October 18, 2026, with working directory `/mnt/data-collection/prediction-bot`. No service/config/schedule/trading changes are authorized here.

The earlier combined release's capture-root checksum/accounting prerequisite is still protected: obtain an operator-controlled canonical preflight result and explicit disposition of the earlier checksum mismatch before rollout. Do not traverse/reset/delete/reseed/override/reconcile capture accounting to make startup succeed. A successful code merge, synthetic fixture or historical label correction does not clear that gate. Controlled collector-only fresh-capture verification is deferred until the operator clears it and authorizes activation.

## Next action

Complete and locally verify the two scoped implementations; commit a recoverable checkpoint with this dossier and exact evidence, then request independent review before running the accepted full-data attribution audit and integrating beta code. Keep any unsupported legacy data in place and explicitly unqualified rather than silently upgrading it.
