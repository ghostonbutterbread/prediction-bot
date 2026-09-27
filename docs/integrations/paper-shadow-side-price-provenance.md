# Paper-shadow side-price provenance — integration dossier

- **Status:** feature checkpoint, awaiting independent read-only review and beta-first compatibility; no merge, push, deployment, or runtime activation
- **Owner / task:** Hermes, prediction-bot Kanban `t_d7273f28`; parent flow audit `t_500d82fb`
- **Branch / worktree:** `fix/paper-shadow-side-price-provenance`; `/home/ryushe/worktrees/prediction-bot-paper-shadow-side-price-provenance`
- **Base / intended target:** `f4fa746e0484c083ee0ea5c019caae33058bb925` → `beta`
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint / implementation commit:** `9329ab27f117ea78376fc2617bfcba772cb8d6bb` (reachable on this branch); the branch tip may also contain a later dossier-only handoff commit
- **Inspiration:** independent read-only provenance review `deleg_e1734166`; it reproduced YES-side `.44` entering a `BUY_NO` row/PnL without a NO ask and snapshot-complemented opposite asks.

## Intent and implemented contract

A shadow lane's chosen side must not inherit a generic reference or estimated fill from the strategy's opposite side. The paper simulator retains its pre-normalization side-labelled asks before `build_execution_snapshot` can complement a missing opposite ask. For this marked hypothetical snapshot, lane entry/ask inputs use those retained quotes (or a raw order-book artifact) rather than the normalized complement. For other inputs, the lane prefers same-side asks from the signal or raw book artifact and does not use an execution snapshot's potentially complemented opposite ask as proof.

`future_pnl_inputs` may retain directional reference/diagnostic prices, but source-router provenance only promotes a recorded selected-side ask as its hypothetical entry/fill. At independent resolution, source-shadow BUY PnL uses the selected side's ask; a marked historical hypothetical row without separately retained raw quote is price-blocked, even if its `best_*_ask` was copied from a complemented snapshot. The decision and raw evidence remain; no quote, fill, resolution, or PnL is fabricated. A best ask is a hypothetical pricing proxy, **not** proof that an order could fill at size; depth, fees, and slippage are not established. Other lanes' accounting rules are not changed here.

## Evidence and gates

- RED→GREEN regressions cover cross-side generic entry, direction-mismatched estimated-fill origins, YES-only snapshot complement (both marked and unmarked), pre-normalization ask preservation, source-router reference promotion, stale `.44` estimate versus recorded NO ask `.58`, marked legacy complemented row price-block, and a marked valid retained NO ask.
- Focused: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_paper_shadow_lanes tests.test_simulator_source_scoreboard_shadow -q` — 67 tests OK, 1 skipped.
- Full isolated suite: `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1,198 tests OK, 7 skipped, 58.604s; receipt `/home/ryushe/.hermes/cache/scratch/prediction-paper-side-price-full-final-20260927.log`.
- `git diff --check` passed before final dossier edit; rerun after staging. Full-suite initial pass had one legacy entry-only identity fixture fail because it lacked a same-side ask; that fixture now supplies a quote to isolate its chronology assertions. Earlier focused fixtures expecting PnL from entry-only/estimate-only data likewise explicitly supply same-side asks.
- Independent review: pending. Beta-first compatibility: pending. No live fills, orders, wallet changes, raw-data writes, services, or ignored config changes.

## Boundaries, blockers, resume

- **Review blocker:** independent reviewer must verify provenance end-to-end and check whether signal or raw-artifact `best_*_ask` can itself be synthetic in any active producer. Review the marked legacy behavior and the precise fallback distinction; do not call this integrated before a fresh verdict.
- **Integration blocker:** from clean fetched beta, stage a non-committing merge, run focused/full beta-first tests and `git diff --cached --check`, abort; then make an intentional reviewed merge that removes this temporary dossier from beta. Do not push or activate services by inference.
- **Separate flow-audit findings:** market-only untimed legacy PnL outcome, Kelly conflicting-receipt order dependence, stale published Source Router manifest locator, and market-specific settlement-source authority are not repaired by this price branch.
- **Exact resume point:** review implementation commit `9329ab27f117ea78376fc2617bfcba772cb8d6bb` and the current branch tip/dossier; request an independent read-only review, fix any reproduced blocker with a RED regression, then perform beta-first compatibility. Worktree clean at implementation checkpoint.

## Decision gates

- **Beta integration:** focused/full isolated suites, independent review, then clean beta-first compatibility; no runtime activation implied.
- **Forward paper:** a new cohort and measured quote-quality/coverage after explicit activation; ask-based hypothetical PnL must not be reported as executed performance.
- **Promotion:** independent resolution, full shared-candidate overlap, actual quote/depth and cost caveats; outside this branch.
