# Pre-risk picker paper observer integration dossier

- **Status:** feature corrected after blocking review (fresh independent review pending)
- **Owner:** t_c6d04b67 builder
- **Branch / owning ref:** `feat/pre-risk-picker-paper-observer`
- **Base commit:** `3fdf4efc665357d83c0a2124b7ae31e7ec169bdb`
- **Intended integration target:** `beta`
- **Last updated:** 2026-10-13
- **Latest immutable recovery checkpoint:** `6fd1d92db351ab1ea7827aeaa831170dfbba3980`
- **Feature implementation commits:** `4af3b2fac8430ef4ab3f68387cd1fbfef08d12a7`, `6fd1d92db351ab1ea7827aeaa831170dfbba3980`
- **Inspiration:** existing disabled `bot/forward_composed_paper.py`, collector-owned shared candidate loader and lane writer.

## Intent

Replace accepted-wallet-only observer denominator with genuinely captured pre-risk picker BUYs, including risk-rejected ones. Pair Source Router and an independent selected-side fee-aware guard on exact shared snapshot/candidate identity; feed matched pre-risk controls and combined intents to isolated synthetic wallets. Retain `control_stable` accepted-wallet audit rows unchanged. Never infer picker intent from trace-only or rewritten SKIP signals.

## Implemented contract

- The candidate loader projects a pre-risk marker only from an exact-market `decision_artifact.strategy_signal` BUY. A post-risk SKIP, trace-only direction, or nested spoof marker cannot manufacture intent.
- Two new disabled lane definitions write the picker action and an independent selected-side, fee-aware quote veto on each loaded candidate. Router stays independently evaluated on that candidate; `control_stable` remains the accepted-wallet audit. Existing definitions and defaults are unchanged.
- The observer pairs by candidate/snapshot/run/market/time, retains wallet-rejected picker BUYs in both pre-risk control and combined input universes, and reports absent picker rows without inferring action. Legacy accepted-only cohorts remain explicitly labeled as such. A verified ask below the guard's floor still enters the control wallet, not the combined wallet; wrong-market/future books enter neither.
- For pre-risk guard decisions only, the loader preserves the decision artifact's separate estimated fill price alongside the selected-side ask. The guard uses the more conservative price; the control wallet still uses the genuine ask. A manually invoked observer can opt into `--require-pre-risk-picker`, which skips a latest snapshot with no picker lane or a missing picker row for a control identity. Legacy accepted-only diagnostics remain available without that opt-in; neither path activates a scheduled observer.
- This remains derived, disabled, quote-proxy-only paper code; no order, exchange, collector raw archive, persistent wallet, or service changes.

## Evidence and review

- RED: `PYTHONPATH=<worktree> /home/ryushe/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_forward_composed_paper.ForwardCompositionTests.test_recorded_pre_risk_buys_survive_wallet_risk_rejection_through_loader_lanes_and_observer -v` failed on unknown picker lane; later RED checks caught a spoofed signal marker, silently missing picker row, and lost control quote under guard floor. All are GREEN.
- Focused: same interpreter/import root, `-m unittest tests.test_forward_composed_paper tests.test_paper_shadow_lanes tests.test_paper_evaluator_input -q` → 123 tests, OK (1 skipped).
- Full isolated: `-m unittest discover -s tests -q` → 1,394 tests, OK (7 skipped), 60.257 s; log in task scratch `pre-risk-picker-full-suite.log` (not a repository artifact). `git diff --check` passed.
- Original fixture: seven collector-shaped rows loaded, 28 lane rows; control opened 3, combined opened 2, with zero settlements. Corrected fixture adds an eighth BUY with ask 0.40, artifact estimate 0.55, and YES probability 0.51: 32 lane rows, control opens 4 at the genuine ask, combined opens 2 because the fee-aware guard vetoes that estimated entry. Trace-only/spoofed markers, wrong-market/future books, and a verified ask below guard floor remain explicit SKIPs or control-only openings. Removing a picker row reports `missing_picker_intent` in the diagnostic; opt-in required-pre-risk mode skips the latest snapshot rather than silently falling back.
- Source Router strict-history reader and source confidence builder were patched in the fixture to return deterministic prior evidence; no live strict-history consumer smoke or forward cohort is claimed.
- Independent review of tip `1f014f429c7eed25bffc5966bcc5d943a931f834` blocked code-only beta integration: normal candidate loading dropped `decision_artifact.execution_snapshot.estimated_fill_price`, allowing the guard to approve at an ask even when the higher recorded estimate would veto. RED loader→writer→observer regression reproduced a false BUY; GREEN after the narrow fix in `6fd1d92db351ab1ea7827aeaa831170dfbba3980`. A separate RED→GREEN check added the optional latest-snapshot picker coverage gate, including the manual CLI flag. Parent reran 123 focused tests (1 skipped), full isolated suite 1,394 tests (7 skipped, 79.776s), and `git diff --check`; all passed. The corrected commit needs **fresh independent review**; the prior no-go cannot approve it.
- Merge/ancestry: branch from clean beta SHA above; no merge or push.

## Blockers and deferred work

- Strict-history consumer smoke, activation/fresh cohort, persistent wallet lifecycle/resolution binding are intentionally out of scope. Run separate authorized source-history and fresh paper cohort gates after reviewed integration; this branch does not prove forward returns or runtime parity.
- Candidate rows that never contain a genuine `decision_artifact.strategy_signal` (including direct shared-candidate-only exports and collector-filtered rows) cannot yield a pre-risk BUY. They remain SKIPs/coverage gaps, never inferred from flattened actions or traces. The tested loader did not filter the risk-rejected recorded row before lane emission.
- Separate `fix/source-router-unpriced-correctness` and deployment cards are owned elsewhere; no cross-branch assumptions.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/pre-risk-picker-paper-observer`
- **Latest immutable recovery checkpoint:** `6fd1d92db351ab1ea7827aeaa831170dfbba3980`
- **Feature implementation commit(s):** `4af3b2fac8430ef4ab3f68387cd1fbfef08d12a7`, `6fd1d92db351ab1ea7827aeaa831170dfbba3980`
- **Exact resume point:** fresh independent read-only review of corrected `6fd1d92db351ab1ea7827aeaa831170dfbba3980` plus this dossier-only tip; if accepted, non-committing combined-beta preflight and exact-tree review. Do not activate.
- **Working-tree state:** clean after dossier-only handoff commit; tip contains that later metadata commit.

## Decision gates

- **Integration:** focused and isolated full suite, diff check, independent review by parent on combined tree.
- **Activation / cohort:** separate explicit authorization, fresh collector-owned cohort and strict-history consumer smoke.
- **Promotion:** independently resolved forward full-overlap evidence; not claimed here.

## Decision record

- 2026-10-13 — opened branch-local dossier before material code.
- 2026-10-13 — independent reviewer blocked beta integration over dropped pre-risk estimated entry; RED/GREEN correction and opt-in pre-risk cohort gate committed, awaiting fresh review. No runtime change.
