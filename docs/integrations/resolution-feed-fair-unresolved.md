# Bounded unresolved resolution queue — integration dossier

- **Status:** corrected code independently accepted; beta target advanced after the accepted old-base preflight, so new-base compatibility remains a merge gate
- **Owner:** Hermes, Kanban `t_eeedba96` (audit `t_500d82fb`)
- **Branch / owning ref:** `fix/resolution-feed-fair-unresolved`
- **Base commit:** `f4fa746e0484c083ee0ea5c019caae33058bb925`
- **Intended integration target:** `beta`
- **Last updated:** 2026-09-27
- **Latest immutable implementation checkpoint:** `29d672e7e4e673bbd842be1a165763a037aca7e8` (current tip also contains later dossier-only commits)
- **Feature implementation commits:** `858abadda0927ae951d41b6b090346f12884b562`, `29d672e7e4e673bbd842be1a165763a037aca7e8`
- **Inspiration:** bounded independent resolver repeatedly selects the first unresolved alphabetic market when that market remains active; later markets never reach resolution.

## Intent and implemented contract

Keep the collector-owned, deduplicated market-reference queue and central outcome ledger. Under a finite effective fetch budget (the smaller of `max_incremental_markets` and downstream `max_markets` when both are set), use the last market selected in the previous cohort-scoped pending batch as a sorted rotation cursor. A still-active market is revisited after other pending markets receive a turn. A first pass remains alphabetically deterministic; no new state store or configuration is needed. This changes only derived resolution polling order, not source evidence, outcomes, wallets, or trading.

## Evidence and review

- RED: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_resolution_feed.ResolutionFeedTests.test_bounded_unresolved_queue_rotates_past_still_active_markets -v` produced `T70,T70,T70,T70` rather than `T70,T71,T72,T70`.
- GREEN: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_resolution_feed -v` — 13 tests OK (6 missing ignored runtime configs skipped).
- GREEN after reviewer blocker fix: `tests.test_resolution_feed` — 14 tests OK (6 ignored-config skips); 27 integrated collector/resolver/identity tests OK; `git diff --check` passed.
- Independent review of initial checkpoint: **blocked** — with `max_incremental_markets=2` and `max_markets=1`, last-selected cursor skipped every second market. Reproduced RED with four markets/eight passes (`T70,T72` loop), then bounded pending selection by the effective fetch budget.
- Independent corrected-code re-review of `29d672e`: dual-cap and additional combinations covered all pending markets; 14 focused tests OK (6 skipped), 27 related integration tests OK, clean diff; **no behavioral blocker**. Reviewer could not approve integration because the then-read dossier still pointed to `858abad` and its recorded full suite was incomplete; those handoff gates are corrected here and in the preceding dossier-only `21c3e1d`.
- Broad isolated discovery: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` completed on corrected checkpoint `29d672e` — **1,191 tests OK (7 skipped), 69.958 seconds**. Log `/home/ryushe/.hermes/cache/scratch/prediction-resolver-full-suite-corrected-20260927.log`. An earlier verbose invocation hit an agent-selected 300-second guard without a verdict; isolated range-review module passed four tests (516 generated cases) in 65.504 seconds. The completed quiet full suite is the actual gate receipt, not an inference from the earlier run.
- Beta non-committing merge preflight against clean fetched `beta` at `f4fa746`: staged cleanly, `git diff --cached --check` passed, beta-first focused integration suite 41 tests OK (6 skipped). The beta-first full suite exceeded an agent-chosen 240-second guard while multiple unrelated full suites were concurrently running; `/home/ryushe/.hermes/cache/scratch/prediction-resolver-beta-preflight-20260927.log` has no final summary. The shell trap aborted the preflight; `beta` is clean and unchanged. **Incomplete gate, not a failing assertion or product latency requirement.** Re-run when concurrent suites finish rather than counting feature-branch full-suite results as beta compatibility.
- Beta-first full-suite retry after concurrent suites finished: non-committing merge of this branch into freshly fetched clean `beta` staged cleanly; `git diff --cached --check` passed; `PYTHONPATH=<beta-worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` completed **1,191 tests OK (7 skipped), 76.786 seconds**. Receipt `/home/ryushe/.hermes/cache/scratch/prediction-resolver-beta-preflight-retry-20260927.log`. Preflight was aborted by the shell trap; readback showed clean beta at `f4fa746` with no merge state or staged diff.
- Final independent read-only handoff/compatibility re-review `deleg_f579c71a`: **accepted** corrected feature against beta `f4fa746`; verified both 1,191-test receipts and clean target. No merge/push/runtime action. Before integration, a new fetch showed clean local and remote `beta` had independently advanced to `295f69cf3199782d192a95950ed101779adea3db` via the disabled loss-guard diagnostic. The previous beta-first receipt does not establish compatibility against this new tip; stage and test a fresh non-committing merge.
- Runtime/data action: none. Active resolver config and source archive unchanged.

## Blockers and deferred work

- **Remaining gate:** beta-first compatibility on the newly advanced clean `295f69c` target (focused/full suite and staged diff check) followed by review of any material interaction. The final feature-code review found no behavioral blocker against its then-current `f4fa746` target.
- **Trigger:** before integration into beta. Do not misclassify either earlier agent-chosen timeout as a product failure; the completed quiet suites are the gate receipts.
- **Activation boundary:** code merge is not service restart/activation; separately inspect the active collector runtime and resolver output after an explicitly approved rollout.
- **Other audit defects:** NWS low date selection, missing-side NO fill, paper PnL resolution authority, and contradictory Kelly receipts are separate owned fixes, not folded into this queue patch.

## Interruption / resume handoff

- **Branch/ref:** `fix/resolution-feed-fair-unresolved`.
- **Latest immutable implementation checkpoint:** `29d672e7e4e673bbd842be1a165763a037aca7e8`; current tip includes later dossier-only handoff commits.
- **Feature implementation commit(s):** `858abadda0927ae951d41b6b090346f12884b562`, `29d672e7e4e673bbd842be1a165763a037aca7e8`.
- **Exact resume point:** stage a non-committing merge into fresh clean `beta` at `295f69cf3199782d192a95950ed101779adea3db`, verify focused/full beta-first tests and staged diff, abort preflight; then integrate without the branch-local dossier if compatible. Verify beta after merge, record its commit, and retire the clean contained feature worktree. Do not deploy services as part of the merge.
- **Working-tree state at handoff:** clean after the dossier-only checkpoint.

## Decision gates

- **Integration:** focused/full tests, independent review, beta diff/merge compatibility.
- **Activation:** explicit runtime decision after code integration; no service changes in this task.
- **Promotion:** no live trading or strategy promotion.

## Decision record

- 2026-09-27 — created from current clean beta; RED and focused GREEN observed.
- 2026-09-27 — final read-only feature review accepted; beta concurrently advanced from `f4fa746` to `295f69c`, requiring a fresh beta-first compatibility gate before merge.
