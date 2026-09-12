# Source history: quote-independent projection; range scoring blocked

## Ownership and integration boundary

- Owner: explicitly authorized prediction-history builder subagent; parent owns Kanban and independent review.
- Branch: `fix/source-history-intervals-all-candidates`.
- Worktree: `/home/ryushe/worktrees/prediction-bot-source-history-intervals-all-candidates`.
- Base: freshly fetched `origin/beta` = local `beta` = `ada72a2b85a2e37058ffbd94cb40a64640530cd9`.
- Target: **beta only**, explicit current user direction supersedes stale AGENTS.md main-based lane guidance. No merge/push authority; no main promotion or runtime activation.
- Status: **partial implementation / evidence-blocked range task**, recoverable checkpoint required for parent review.
- Existing registered worktrees were preserved. No branch reused. `perf/source-router-history-throughput` has unique commits (including `3b90d7b`) and overlaps the direct-history module conceptually; it was not imported or changed. Distinct-history-buckets, SourceWriter-default and SourceWriter-indexed-artifact tips have no commits unique to beta in the inspected ranges. Parent must reconcile future throughput integration separately.

## Intent and implemented contract

User authorized (1) range/bucket source correctness with proven bounds/rounding and (2) source history independent of hypothetical trading gates. Only (2)'s demonstrated quote-dependency repair is implemented here.

Both production history consumers called the ordinary replay sanitizer, which rejects entirely absent quotes/asks. Neither inspected production history path gates on action, trade approval, edge, Kelly or risk. Existing real SKIPs already contribute when other evidence passes. Do not attribute the separate collector_source_router_replay research price/edge policy to production correctness.

- Added opt-in `source_history_only` projection to the existing sanitizer/exporter. It keeps exact identity, source proof, blind allowlists and provenance, tags the derived purpose, and permits absent prices without imputing them.
- Direct history and materialized SourceWriter explicitly opt in. Default replay behavior and strict executable replay remain price-dependent.
- Revised SourceWriter generation identity so byte-identical inputs cannot reuse a generation built with the earlier price gate.
- No source probability/rank conversion, quote-capture change, PnL change or trade-gate relaxation.
- Existing `_source_router_decision` loads history before its lane action/price/edge gates; this ordering was inspected and is unchanged.
- Exact outcome binding, VOID handling, strict settlement-before-future-decision admission and independent collapse are unchanged.

Changed product paths: `bot/replay_decision_input.py`, `bot/collector_replay_inputs.py`, `bot/auto_source_router_promotion.py`, `bot/weather/source_router_direct_history.py`. Tests: `tests/test_source_history_all_candidates.py`.

## Verified evidence

Persistent report root:
`/mnt/data-collection/prediction-bot/data/derived_reports/source_history_fix_20260912`

- `REPORT.md`, `proof.py`, `original_byte_receipts.json`, `before/report.json`, `after/report.json` document reproducible read-only raw hydration and derived-only processing.
- Full captured compact prefix: 219,999 rows. Outcome-blind B/T-stratified 120 original locators, all hashes match, 9,735,875 bytes hydrated; every original main action is SKIP.
- Identical before/after: 111 projected inputs, 888 pending source observations, 24 strict eligible pre-cutoff tail source contributions, all 24 from original SKIPs; zero eligible ranges. Materialized/direct semantic parity true in both versions. Strict settled chronology excludes 39 at cutoff `2026-09-03T00:00:00+00:00`; no later-settled warmup imported.
- **No observed real-data recovered samples** in this selection. The real baseline already admits its SKIPs; missing-price recovery is proven by the controlled regression, not invented corpus gains.
- Final baseline regression replay against beta: 1 expected failure with `rejection_invalid_decision_time_price=1`; fixed real SourceWriter and direct reader both return one matching source sample for that quote-less risk SKIP.
- Focused: `PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_source*.py'`: 65 tests, OK.
- Full: `PYTHONPATH=. python3 -m unittest discover -s tests`: 1,170 tests run, OK, 7 skipped, 40.583s. Logs are persistent `focused-tests.log`, `full-tests.log`, `baseline-red.log`.
- `git diff --check`: passed.

TDD caveat for reviewer: initial quote-less fixture mistakenly used zero (the old sanitizer allows zero), and initial integration fixture lacked a compact-index snapshot identity / had inconsistent target date. These were corrected. The saved final baseline red receipt isolates the real missing-price gate. The new projection call failed for its absent keyword before implementation; generation-identity assertion separately failed before its fix. Range and strict-mode safety tests are characterization/additional coverage, not claimed RED→GREEN range implementation.

## Range blocker and exact resume point

No interval scoring change was made. Questions preserve visible ranges, but none of the 120 hydrated rows contains keys matching strike/bound/round/rules/inclusive. This is not a proof of absence in all 17GB. The pending owner retains only scalar threshold/question_side/shape; `_source_implied_outcome` supports above/below. A recorded range question alone has not established the exact requested settlement rounding/tie contract.

Resume by obtaining a bounded immutable original market/rules receipt proving lower and upper bounds, inclusivity and rounding. Add raw→sanitized→pending→implied-side regression fixtures (decimal forecasts, endpoints, ties, negative temperatures, missing/conflicting proof), then implement the shared shape-aware scorer and rerun identical corpus before/after. Do not infer two bounds or rounding from the scalar threshold. Existing unproven ranges remain unusable, covered by a safety regression. Parent must independently decide whether the partial price fix is acceptable separately; **do not mark both requested fixes complete**.

## Activation, deferred work and recovery

No service, runtime config, wallet, raw archive, active index or beta working-tree changes. Stale publication and executable ask capture are separate existing issues and were not repaired. No merge, push or deployment. Parent independent review remains outstanding.

Recovery checkpoint: this dossier and implementation will be committed together, followed by a dossier-only handoff naming the immutable implementation SHA. Resume from this owned branch, not the dirty storage checkout. This document is temporary branch-local integration metadata and must not be promoted as a permanent beta/main status board.
