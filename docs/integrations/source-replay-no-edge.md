# Source-only historical replay NO edge fix

- **Status:** independent read-only review accepted the narrow source-only fix; beta integration and historical cohort recomputation remain pending
- **Owner:** delegated source replay fix agent
- **Branch / owning ref:** `fix/source-replay-no-edge`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-source-replay-no-edge`
- **Base commit:** `b980ece98874d8039d3aebddea61d69989071135`
- **Intended integration target:** local `beta` (parent owns merge and descendant propagation)
- **Last updated:** 2026-10-04
- **Latest immutable recovery checkpoint:** `b79d805e45327396d9347077db264604f6a5cd5b` (implementation and initial dossier)
- **Feature implementation commit(s):** `b79d805e45327396d9347077db264604f6a5cd5b`
- **Inspiration:** independent review of `feat/combined-lanes-market-replay` at `962e39ad29689e589cbf591e5e411424e1cf5262`; the replay owner is the local beta ancestor.

## Intent and scope

Correct the source-only historical control replay's BUY_NO threshold. The control's `weather_source_snapshot.predicted_prob` is YES probability: for NO compare `1 - predicted_prob` with the recorded NO ask. Preserve its sealed `probability` as the original YES probability; do not change the candidate router's `prior_win_rate`, which already measures selected-side correctness. This branch changes only the replay owner, its focused regressions, and this handoff. It does not edit the combined feature branch, the shared beta worktree, runtime config, or raw ledgers.

## Implemented contract

`_threshold_action` receives the probability interpretation explicitly from each caller. A control NO at YES probability 0.2 and NO ask 0.3 becomes BUY_NO; a NO ask 0.76 stays SKIP. Control YES compares YES probability directly. Candidate NO uses prior selected-side win rate directly, buying when its prior correctness clears the ask and rejecting when it does not. Skip reasons, price basis, source selection, resolution, and probability fields remain unchanged. Reference-price fallback remains diagnostic rather than executable evidence.

## Evidence and review

- **RED:** `PYTHONPATH=<this worktree> python3 -m unittest tests.test_collector_source_router_replay.CollectorSourceRouterReplayTests.test_control_no_uses_selected_side_probability_for_edge -v` failed with `SKIP != BUY_NO` before changing production code.
- **GREEN:** same regression passed after the fix. `PYTHONPATH=<this worktree> python3 -m unittest tests.test_collector_source_router_replay -v` passed 22 tests, including YES, NO reject, and candidate NO win-rate semantics.
- **Full suite:** `PYTHONPATH=<this worktree> python3 -m unittest discover -s tests` passed 1,363 tests, 7 skipped, in 385.836s. `git diff --check` passed. No checkout-local `.venv/bin/python`; used `python3` with the isolated worktree first on `PYTHONPATH`.
- **Ancestry:** local beta was clean at base; fetched `origin/beta`, which remains at `295f69cf3199782d192a95950ed101779adea3db`, an ancestor of the local beta (128 commits behind). The combined feature's merge-base with this branch/base is `b980ece98874d8039d3aebddea61d69989071135`. No beta merge or push.
- **Independent review:** fresh reviewer found no blocking issue in `b79d805e45327396d9347077db264604f6a5cd5b` or the separate combined-feature guard-ID fix, reran 22 focused tests, and reproduced source SKIP→BUY_NO with candidate prior-win-rate unchanged. This accepts the narrow repair only; no combined-wallet evidence claim.
- **Replay/cohort fixture:** focused synthetic decision-time fixtures only; no measured recomputation of the reviewed 13 control and 12 guard skips or full historical cohort, which belongs to combined-branch propagation.

## Blockers and deferred work

- **Missing evidence:** integrated combined-feature replay/counts after propagation; beta staged-tree focused/full tests and fresh receipt are required before relying on the changed control.
- **Command / fixture:** parent to merge this commit into current beta, then propagate from corrected beta to affected descendant and rerun its focused/full tests plus the historical market replay with its recorded fixture.
- **Trigger / boundary:** after parent accepts this branch and its separate guard-ID repair. Until then do not merge, push, activate, or interpret old derived replay artifacts as corrected; recompute them from immutable inputs.
- **Next completion step:** parent preflights and integrates only the reviewed repair into beta (excluding this dossier), propagates to `feat/combined-lanes-market-replay`, and verifies results; do not cherry-pick duplicate logical fixes into descendants.

## Interruption / resume handoff

- **Owning feature branch/ref:** `fix/source-replay-no-edge`
- **Latest immutable recovery checkpoint:** `b79d805e45327396d9347077db264604f6a5cd5b`; current branch tip will also contain a later dossier-only handoff commit.
- **Feature implementation commit(s):** `b79d805e45327396d9347077db264604f6a5cd5b`
- **Exact resume point:** review the implementation commit against beta base, then integrate only with parent authorization. Parent propagates to combined replay after its independent guard-ID work, reruns the descendant checks, and recomputes historical derived artifacts.
- **Working-tree state at handoff:** clean after the dossier-only handoff commit.

## Decision gates

- **Integration gate:** independent review accepted; beta staged-tree tests still required. Remove this branch-local dossier from the beta merge index.
- **Activation / cohort gate:** no activation; historical replay remains diagnostic, not forward evidence.
- **Promotion gate:** requires independent forward-paper evidence per repository AGENTS.md, not this fix or historical replay alone.

## Decision record

- 2026-10-04 — created a dedicated fix worktree from clean local beta; reproduced RED, applied selected-side threshold fix, verified focused and full suites. Parent coordinates review, integration, and descendant propagation.
