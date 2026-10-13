# Pre-risk picker paper observer integration dossier

- **Status:** feature (verified locally; independent review pending)
- **Owner:** t_c6d04b67 builder
- **Branch / owning ref:** `feat/pre-risk-picker-paper-observer`
- **Base commit:** `3fdf4efc665357d83c0a2124b7ae31e7ec169bdb`
- **Intended integration target:** `beta`
- **Last updated:** 2026-10-13
- **Latest immutable recovery checkpoint:** none yet
- **Feature implementation commits:** none yet
- **Inspiration:** existing disabled `bot/forward_composed_paper.py`, collector-owned shared candidate loader and lane writer.

## Intent

Replace accepted-wallet-only observer denominator with genuinely captured pre-risk picker BUYs, including risk-rejected ones. Pair Source Router and an independent selected-side fee-aware guard on exact shared snapshot/candidate identity; feed matched pre-risk controls and combined intents to isolated synthetic wallets. Retain `control_stable` accepted-wallet audit rows unchanged. Never infer picker intent from trace-only or rewritten SKIP signals.

## Implemented contract

- The candidate loader projects a pre-risk marker only from an exact-market `decision_artifact.strategy_signal` BUY. A post-risk SKIP, trace-only direction, or nested spoof marker cannot manufacture intent.
- Two new disabled lane definitions write the picker action and an independent selected-side, fee-aware quote veto on each loaded candidate. Router stays independently evaluated on that candidate; `control_stable` remains the accepted-wallet audit. Existing definitions and defaults are unchanged.
- The observer pairs by candidate/snapshot/run/market/time, retains wallet-rejected picker BUYs in both pre-risk control and combined input universes, and reports absent picker rows without inferring action. Legacy accepted-only cohorts remain explicitly labeled as such. A verified ask below the guard's floor still enters the control wallet, not the combined wallet; wrong-market/future books enter neither.
- This remains derived, disabled, quote-proxy-only paper code; no order, exchange, collector raw archive, persistent wallet, or service changes.

## Evidence and review

- RED: `PYTHONPATH=<worktree> /home/ryushe/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_forward_composed_paper.ForwardCompositionTests.test_recorded_pre_risk_buys_survive_wallet_risk_rejection_through_loader_lanes_and_observer -v` failed on unknown picker lane; later RED checks caught a spoofed signal marker, silently missing picker row, and lost control quote under guard floor. All are GREEN.
- Focused: same interpreter/import root, `-m unittest tests.test_forward_composed_paper tests.test_paper_shadow_lanes tests.test_paper_evaluator_input -q` → 123 tests, OK (1 skipped).
- Full isolated: `-m unittest discover -s tests -q` → 1,394 tests, OK (7 skipped), 60.257 s; log in task scratch `pre-risk-picker-full-suite.log` (not a repository artifact). `git diff --check` passed.
- Fixture: seven collector-shaped rows loaded; seven rows per enabled lane, 28 total. One accepted + one risk-rejected genuine picker BUY pass guard/Router; trace-only and spoofed marker SKIP; wrong-market and future book SKIP; a verified ask below guard floor opens the control but not combined wallet. Control opens 3, combined opens 2; removing the rejected candidate's picker row explicitly reports `missing_picker_intent` rather than opening it.
- Source Router strict-history reader and source confidence builder were patched in the fixture to return deterministic prior evidence; no live strict-history consumer smoke or forward cohort is claimed.
- Independent review: assigned to parent; not performed by builder.
- Merge/ancestry: branch from clean beta SHA above; no merge or push.

## Blockers and deferred work

- Strict-history consumer smoke, activation/fresh cohort, persistent wallet lifecycle/resolution binding are intentionally out of scope. Run separate authorized source-history and fresh paper cohort gates after reviewed integration; this branch does not prove forward returns or runtime parity.
- Candidate rows that never contain a genuine `decision_artifact.strategy_signal` (including direct shared-candidate-only exports and collector-filtered rows) cannot yield a pre-risk BUY. They remain SKIPs/coverage gaps, never inferred from flattened actions or traces. The tested loader did not filter the risk-rejected recorded row before lane emission.
- Separate `fix/source-router-unpriced-correctness` and deployment cards are owned elsewhere; no cross-branch assumptions.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/pre-risk-picker-paper-observer`
- **Latest immutable recovery checkpoint:** none yet
- **Feature implementation commit(s):** none yet
- **Exact resume point:** add failing producer-to-observer fixture, then minimal feature implementation.
- **Working-tree state:** dossier only before test/code.

## Decision gates

- **Integration:** focused and isolated full suite, diff check, independent review by parent on combined tree.
- **Activation / cohort:** separate explicit authorization, fresh collector-owned cohort and strict-history consumer smoke.
- **Promotion:** independently resolved forward full-overlap evidence; not claimed here.

## Decision record

- 2026-10-13 — opened branch-local dossier before material code.
