# Bounded unresolved resolution queue — integration dossier

- **Status:** feature; awaiting full-suite verification and independent review
- **Owner:** Hermes, Kanban `t_eeedba96` (audit `t_500d82fb`)
- **Branch / owning ref:** `fix/resolution-feed-fair-unresolved`
- **Base commit:** `f4fa746e0484c083ee0ea5c019caae33058bb925`
- **Intended integration target:** `beta`
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint:** none yet
- **Feature implementation commits:** none yet
- **Inspiration:** bounded independent resolver repeatedly selects the first unresolved alphabetic market when that market remains active; later markets never reach resolution.

## Intent and implemented contract

Keep the collector-owned, deduplicated market-reference queue and central outcome ledger. Under a finite `max_incremental_markets`, use the last market selected in the previous cohort-scoped pending batch as a sorted rotation cursor. A still-active market is revisited after other pending markets receive a turn. A first pass remains alphabetically deterministic; no new state store or configuration is needed. This changes only derived resolution polling order, not source evidence, outcomes, wallets, or trading.

## Evidence and review

- RED: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_resolution_feed.ResolutionFeedTests.test_bounded_unresolved_queue_rotates_past_still_active_markets -v` produced `T70,T70,T70,T70` rather than `T70,T71,T72,T70`.
- GREEN: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_resolution_feed -v` — 13 tests OK (6 missing ignored runtime configs skipped).
- Integrated focused check: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_beta_data_flow_e2e tests.test_scoreboard_resolution_backfill tests.test_beta_handoff_identity_audit -q` — 27 tests OK.
- Broad isolated discovery attempt: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests` hit an agent-selected 300-second resource guard after simulator tests; log `/home/ryushe/.hermes/cache/scratch/prediction-resolver-fair-full-suite-20260927.log` contains no final summary or failure. **Incomplete, not a pass or a product latency requirement.** The base beta suite previously completed 1,189 tests OK (7 skipped) in a different run. Diagnose the slow/hung module before integration rather than silently counting the base receipt.
- Independent review: dispatched, pending.
- Runtime/data action: none. Active resolver config and source archive unchanged.

## Blockers and deferred work

- **Missing evidence:** full isolated suite and independent read-only review of cursor fairness, persistence, and cohort isolation.
- **Commands:** `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests`; `git diff --check`.
- **Trigger:** before integration into beta.
- **Activation boundary:** code merge is not service restart/activation; separately inspect the active collector runtime and resolver output after an explicitly approved rollout.
- **Other audit defects:** NWS low date selection, missing-side NO fill, paper PnL resolution authority, and contradictory Kelly receipts are separate owned fixes, not folded into this queue patch.

## Interruption / resume handoff

- **Branch/ref:** `fix/resolution-feed-fair-unresolved`.
- **Latest immutable recovery checkpoint:** none yet; commit after local gate.
- **Exact resume point:** run full suite, obtain independent review, resolve findings, commit dossier and implementation; review beta integration separately.
- **Working-tree state at handoff:** implementation and regression test intentionally uncommitted until verification.

## Decision gates

- **Integration:** focused/full tests, independent review, beta diff/merge compatibility.
- **Activation:** explicit runtime decision after code integration; no service changes in this task.
- **Promotion:** no live trading or strategy promotion.

## Decision record

- 2026-09-27 — created from current clean beta; RED and focused GREEN observed.
