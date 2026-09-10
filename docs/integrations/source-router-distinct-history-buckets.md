# Source Router distinct-event history budgets

Status: implemented checkpoint; independent review and full-suite run pending; NOT merged, pushed, or activated.
Branch/worktree: `fix/source-router-distinct-history-buckets` / `/home/ryushe/worktrees/prediction-bot-source-router-distinct-history-buckets`.
Implementation checkpoint: `3eadc0a0262b1b99934abfafb8a8fed32dc110c3`; current tip includes a later dossier-only handoff commit. Review both the implementation and later handoff changes.
Base/target: beta `696900651abbd108f4285dee45ceee09cdf77d28` / beta, never main.
Owner: parent Hermes, Discord thread 1547354226093461644. Kanban mutation remains blocked by the known child-context misclassification; do not bypass its guard.

## Intent and boundary
Ryu requests history budgets by source × city × market kind × contract shape AFTER strict qualification, chronology and market/event deduplication. Repeated polls/retrades must not consume new units. Raw evidence stays unchanged.
The direct-history module is beta-owned and absent from main. Repository AGENTS still describes an older main-based topology; no integration is performed under that ambiguity.

## Implemented contract
- Canonical lane config `history_events_per_bucket` (default 100); `history_row_limit` is a compatibility alias with corrected event-unit semantics. Both must be positive integers; conflicting values fail closed.
- Python API retains `accepted_limit`; its documented meaning is now the per-bucket event budget, not a raw-row limit.
- Each bucket independently selects newest eligible event units in committed archive order. The reader scans the committed archive and retains the earliest eligible source-as-of / captured-observation representative of each selected event, without choosing by outcome.
- Canonical weather event identity is strict city + target date + measurement kind. This prevents optional event-ticker metadata from splitting one physical event. Event ticker/id and exact contract ID remain labeled fallbacks when the canonical fields are absent.
- Shape buckets remain separate. Opposite threshold contracts and repeated polls of one weather event within one bucket count once. Existing proof/implied-side support is unchanged; this does NOT add range-contract outcome inference.
- Memory scales with retained event budget × discovered bucket count, not poll count. Coverage reports populated buckets, selected units, shortfall, representative-market count, event/contract-only units and source date bounds. It does not claim exhaustive available-unit counts or invent history for absent buckets.
- Actual lane decision provenance includes selection-unit meaning, canonical limit, per-bucket coverage, counts and evidence hashes.
- Legacy materialized-history readers/formats are not rewritten. No raw copies, wallet/accounting changes, services, schedules or live configuration changes.

## Evidence
- Observed RED→GREEN: repeated polls previously yielded sample_count 1 for budget 2; canonical named config previously ignored; coverage absent; metadata-present/absent copies split one event; conflicting aliases were silently accepted.
- Focused suite: 266 tests run, OK with 1 skipped. Source path asserted inside this worktree; temporary collector root, Python socket connections disabled, 1.5 GiB address-space cap. `git diff --check` passed.
- Exact runner and logs: `/mnt/data-collection/prediction-bot/data/derived_reports/source_router_lane_experiment/20260909/router_bucket_fix/` (`run_checks.py`, `focused.log`). Run with `/mnt/data-collection/prediction-bot/.venv/bin/python run_checks.py`; add `--full` for the full suite.
- Existing direct-history fixture dates corrected to match their three distinct market/event dates rather than falsely sharing one weather target date. Writer parity still passes on that valid distinct-event cohort.
- Cleanup is separately completed: parent readback verified all 45 retirement targets absent and all 324 preserved bundle hashes matching. Receipt: `source_router_retention/cleanup_20260909T230326Z_ae373a42/REPORT.md` under derived_reports.

## Source-name aggregation review repair (verified checkpoint; independent re-review pending)
- Confirmed defect: direct selection retained two event units for `nws`, but the legacy scorer grouped `NWS` and `National Weather Service` separately; the four-dimensional reliability lookup overwrote one score and returned sample_count 1.
- Direct-history-only repair: after event/representative selection, copy selected rows with the lexicographically smallest selected display name per bucket before collapse/scoring. No change to event identity, selection, raw files, legacy scorer/table behavior, configuration, or activation.
- Regression: `test_source_router_history_buckets.DistinctHistoryBucketTests.test_display_name_changes_preserve_combined_lookup_evidence` exercises committed temporary history through actual `SourceReliabilityTable.lookup`. Swapping the two display names preserves days 2/3, sample_count 2, combined accuracy 0.5, and both provenance observations; an older alphabetically earlier label cannot enter the budget or select the canonical name. Same-name control also passes.
- Exact RED/GREEN command (worktree cwd): `PYTHONPATH=.:tests PYTHONDONTWRITEBYTECODE=1 /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest test_source_router_history_buckets.DistinctHistoryBucketTests.test_display_name_changes_preserve_combined_lookup_evidence -v`. Before production edit: `Ran 1 test in 2.436s`, `FAILED (failures=2)`, both mixed-name cases `AssertionError: 1 != 2`; after edit: `Ran 1 test in 0.903s`, `OK`. Initial test fixture used threshold lookup rather than the fixture's actual tail shape; corrected before this defect-specific RED receipt.
- Focused suite: same interpreter/environment, `unittest.defaultTestLoader.loadTestsFromNames(['test_source_router_history_buckets', 'test_source_router_direct_history', 'test_auto_source_router_promotion', 'test_source_reliability_replay_evidence'])` with `TextTestRunner(verbosity=1)`, temporary collector-root environment and patched socket connect rejecting network. Actual output: `Ran 41 tests in 13.138s`, `OK`. Existing writer-parity test passes. `git diff --check` passed.
- Parent full-suite rerun after repair: 1145 tests in 36.742 seconds, OK with 7 skipped; exit code 0. Exact guarded runner above with `--full`; receipt `full_after_name_fix.log` in the external router_bucket_fix report directory. Independent re-review dispatched as `deleg_c523fead`. No merge, push, or deployment.

## Open gates / exact resume
1. Read independent re-review result (`deleg_c523fead`) for the source-name repair. Original review disposition is recorded externally in `router_bucket_fix/review_disposition.md`; event-metadata and alias-conflict defects already have passing regressions.
2. Add explicit city/kind/shape separation and representative/cutoff coverage where the review finds gaps; verify actual lane provenance assertions.
3. Before any runtime activation, benchmark the full committed-history scan on retained actual evidence. New semantics deliberately remove the old global early exit; per-decision full-history hydration may be too expensive. Do not call this production-latency-ready without a measured budget and a verified indexed/batched read strategy if needed. No actual archive benchmark has run for this checkpoint.
4. Full multi-month lane P&L remains a separate experiment with existing replay clock/price provenance blockers. No live-readiness or P&L improvement claim follows from these unit tests.
5. Keep the fix on its feature branch until review/evidence gates and integration target are reconciled. Do not enable SourceWriter, resurrect copied generations, or activate a lane to test this change.
