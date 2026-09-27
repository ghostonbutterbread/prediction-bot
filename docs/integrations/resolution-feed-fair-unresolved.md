# Bounded unresolved resolution queue — integration dossier

- **Status:** feature checkpoint; corrected full suite passed, fresh independent re-review pending
- **Owner:** Hermes, Kanban `t_eeedba96` (audit `t_500d82fb`)
- **Branch / owning ref:** `fix/resolution-feed-fair-unresolved`
- **Base commit:** `f4fa746e0484c083ee0ea5c019caae33058bb925`
- **Intended integration target:** `beta`
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint:** `858abadda0927ae951d41b6b090346f12884b562`
- **Feature implementation commits:** `858abadda0927ae951d41b6b090346f12884b562`
- **Inspiration:** bounded independent resolver repeatedly selects the first unresolved alphabetic market when that market remains active; later markets never reach resolution.

## Intent and implemented contract

Keep the collector-owned, deduplicated market-reference queue and central outcome ledger. Under a finite effective fetch budget (the smaller of `max_incremental_markets` and downstream `max_markets` when both are set), use the last market selected in the previous cohort-scoped pending batch as a sorted rotation cursor. A still-active market is revisited after other pending markets receive a turn. A first pass remains alphabetically deterministic; no new state store or configuration is needed. This changes only derived resolution polling order, not source evidence, outcomes, wallets, or trading.

## Evidence and review

- RED: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_resolution_feed.ResolutionFeedTests.test_bounded_unresolved_queue_rotates_past_still_active_markets -v` produced `T70,T70,T70,T70` rather than `T70,T71,T72,T70`.
- GREEN: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_resolution_feed -v` — 13 tests OK (6 missing ignored runtime configs skipped).
- GREEN after reviewer blocker fix: `tests.test_resolution_feed` — 14 tests OK (6 ignored-config skips); 27 integrated collector/resolver/identity tests OK; `git diff --check` passed.
- Independent review of initial checkpoint: **blocked** — with `max_incremental_markets=2` and `max_markets=1`, last-selected cursor skipped every second market. Reproduced RED with four markets/eight passes (`T70,T72` loop), then bounded pending selection by the effective fetch budget; fresh independent re-review pending.
- Broad isolated discovery: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` completed on corrected checkpoint `29d672e` — **1,191 tests OK (7 skipped), 69.958 seconds**. Log `/home/ryushe/.hermes/cache/scratch/prediction-resolver-full-suite-corrected-20260927.log`. An earlier verbose invocation hit an agent-selected 300-second guard without a verdict; isolated range-review module passed four tests (516 generated cases) in 65.504 seconds. The completed quiet full suite is the actual gate receipt, not an inference from the earlier run.
- Runtime/data action: none. Active resolver config and source archive unchanged.

## Blockers and deferred work

- **Missing evidence:** full isolated suite and independent read-only review of cursor fairness, persistence, and cohort isolation.
- **Commands:** `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests`; `git diff --check`.
- **Trigger:** before integration into beta.
- **Activation boundary:** code merge is not service restart/activation; separately inspect the active collector runtime and resolver output after an explicitly approved rollout.
- **Other audit defects:** NWS low date selection, missing-side NO fill, paper PnL resolution authority, and contradictory Kelly receipts are separate owned fixes, not folded into this queue patch.

## Interruption / resume handoff

- **Branch/ref:** `fix/resolution-feed-fair-unresolved`.
- **Latest immutable recovery checkpoint:** `858abadda0927ae951d41b6b090346f12884b562` (implementation commit; current tip will include a later dossier-only handoff commit).
- **Feature implementation commit(s):** `858abadda0927ae951d41b6b090346f12884b562`.
- **Exact resume point:** reconcile independent review; diagnose or bound the stalled broad suite, rerun required gates, then review beta integration. This branch is not merge-ready.
- **Working-tree state at handoff:** clean after the dossier-only checkpoint.

## Decision gates

- **Integration:** focused/full tests, independent review, beta diff/merge compatibility.
- **Activation:** explicit runtime decision after code integration; no service changes in this task.
- **Promotion:** no live trading or strategy promotion.

## Decision record

- 2026-09-27 — created from current clean beta; RED and focused GREEN observed.
