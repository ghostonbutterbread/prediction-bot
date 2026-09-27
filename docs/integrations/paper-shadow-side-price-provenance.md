# Paper-shadow side-price provenance — integration dossier

- **Status:** corrected after a blocking independent review; fresh read-only review and beta-first compatibility pending; no merge, push, deployment, or runtime activation
- **Owner / task:** Hermes, prediction-bot Kanban `t_d7273f28`; parent flow audit `t_500d82fb`
- **Branch / worktree:** `fix/paper-shadow-side-price-provenance`; `/home/ryushe/worktrees/prediction-bot-paper-shadow-side-price-provenance`
- **Base / intended target:** `f4fa746e0484c083ee0ea5c019caae33058bb925` → `beta`
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint / implementation commit:** `d23d23402c080746d5d6f2b98558bb79d54d4d20` (reachable on this branch; previous implementation `9329ab27f117ea78376fc2617bfcba772cb8d6bb`); the branch tip may also contain a later dossier-only handoff commit
- **Inspiration:** independent read-only provenance review `deleg_e1734166`; it reproduced YES-side `.44` entering a `BUY_NO` row/PnL without a NO ask and snapshot-complemented opposite asks.

## Intent and implemented contract

A shadow lane's chosen side must not inherit a generic reference or estimated fill from the strategy's opposite side. The paper simulator retains its pre-normalization side-labelled asks before `build_execution_snapshot` can complement a missing opposite ask. For this marked hypothetical snapshot, lane entry/ask inputs use those retained quotes (or a raw order-book artifact) rather than the normalized complement. For other inputs, the lane prefers same-side asks from the signal or raw book artifact and does not use an execution snapshot's potentially complemented opposite ask as proof.

`future_pnl_inputs` may retain directional reference/diagnostic prices, but source-router provenance only promotes a recorded selected-side ask as its hypothetical entry/fill. At independent resolution, source-shadow BUY PnL uses the selected side's ask; a marked historical hypothetical row without separately retained raw quote is price-blocked, even if its `best_*_ask` was copied from a complemented snapshot. The decision and raw evidence remain; no quote, fill, resolution, or PnL is fabricated. A best ask is a hypothetical pricing proxy, **not** proof that an order could fill at size; depth, fees, and slippage are not established. Other lanes' accounting rules are not changed here.

The first independent review (`deleg_ca844534`) **blocked** tip `14feb2afc9329352f982c3979ae5925ddbf425ef`: a normal, unmarked shared-candidate producer flattened a `source=fallback` execution/reference snapshot's computed NO `.56` into `prices.best_no_ask`; normalization carried it to a `BUY_NO` future PnL input and booked a hypothetical profit without a NO book quote. The corrected consumer now consults the candidate's structured order-book envelope when no artifact is attached and does not promote a non-book snapshot's flattened asks, entry, or estimated fill. The producer retains reference prices; real `source=book` selected-side asks and legacy inputs without an explicit non-book envelope remain usable. This does not change a market-only resolution's authority.

## Evidence and gates

- RED→GREEN regressions cover cross-side generic entry, direction-mismatched estimated-fill origins, YES-only snapshot complement (both marked and unmarked), pre-normalization ask preservation, source-router reference promotion, stale `.44` estimate versus recorded NO ask `.58`, marked legacy complemented row price-block, and a marked valid retained NO ask.
- Focused: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_paper_shadow_lanes tests.test_simulator_source_scoreboard_shadow -q` — 67 tests OK, 1 skipped.
- Full isolated suite: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1,198 tests OK, 7 skipped, 58.604s; receipt `/home/ryushe/.hermes/cache/scratch/prediction-paper-side-price-full-final-20260927.log`.
- Review-blocker RED: `tests.test_paper_shadow_lanes.PaperShadowLaneTests.test_shared_candidate_fallback_snapshot_does_not_become_no_ask` failed at `.56 is not None` through producer → normalization → paper consumer, then GREEN after the correction. A producer-shaped genuine book NO `.58` remains usable.
- Corrected isolated focused run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_paper_shadow_lanes tests.test_shared_market_feed tests.test_simulator_source_scoreboard_shadow -q` — 92 OK, 1 skipped; receipt `/home/ryushe/.hermes/cache/scratch/prediction-paper-side-fallback-focused-20260927.log`.
- Corrected isolated full run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1,200 OK, 7 skipped; receipt `/home/ryushe/.hermes/cache/scratch/prediction-paper-side-fallback-full-20260927.log`.
- `git diff --check` passed before final dossier edit; rerun after staging. Full-suite initial pass had one legacy entry-only identity fixture fail because it lacked a same-side ask; that fixture now supplies a quote to isolate its chronology assertions. Earlier focused fixtures expecting PnL from entry-only/estimate-only data likewise explicitly supply same-side asks.
- Independent review of corrected tip: pending; prior read-only review blocked the old tip. Beta-first compatibility: pending. No live fills, orders, wallet changes, raw-data writes, services, or ignored config changes.

## Boundaries, blockers, resume

- **Review gate:** independent reviewer must verify the corrected producer→normalizer→consumer boundary, genuine book/no-book distinction, market/time metadata where recorded, and check whether other active producers can flatten synthetic `best_*_ask`. Review marked legacy behavior and valid quote retention; no integration before a fresh verdict.
- **Integration blocker:** from clean fetched beta, stage a non-committing merge, run focused/full beta-first tests and `git diff --cached --check`, abort; then make an intentional reviewed merge that removes this temporary dossier from beta. Do not push or activate services by inference.
- **Separate flow-audit findings:** market-only untimed legacy PnL outcome, stale published Source Router manifest locator, and market-specific settlement-source authority are not repaired by this price branch. The Kelly conflicting-receipt fix was reviewed and merged into local beta separately, but not pushed or activated.
- **Exact resume point:** review implementation commit `d23d23402c080746d5d6f2b98558bb79d54d4d20` plus the current branch tip/dossier; request a fresh independent read-only review, fix any reproduced blocker with a RED regression, then perform beta-first compatibility. Worktree clean at implementation checkpoint.

## Decision gates

- **Beta integration:** focused/full isolated suites, independent review, then clean beta-first compatibility; no runtime activation implied.
- **Forward paper:** a new cohort and measured quote-quality/coverage after explicit activation; ask-based hypothetical PnL must not be reported as executed performance.
- **Promotion:** independent resolution, full shared-candidate overlap, actual quote/depth and cost caveats; outside this branch.
