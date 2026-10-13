# Saved pre-risk paper wallet integration dossier

- **Status:** saved pre-risk selector implemented in isolation; persistent account not implemented or enrolled
- **Owner:** Hermes, prediction-bot Kanban `t_ab52708b`
- **Branch / owning ref:** `feat/saved-pre-risk-paper-wallet`
- **Base commit:** `1e227a51e8582a8637903858588ed6a638dd8f42`
- **Intended integration target:** local `beta`
- **Last updated:** 2026-10-12 PDT
- **Latest immutable recovery checkpoint:** `6eeca18c589cbec02f91854db1c222740e908a86` (correction; dossier-only tip follows)
- **Feature implementation commits:** `9017660a3e140754adb23bc0f9e4635bd4c1e9bb`, `6eeca18c589cbec02f91854db1c222740e908a86`
- **Inspiration / canonical references:** accepted code-only saved-combination tree `174fbf0c24ec7b31452e640c558dbd694a713b0f`; read-only seam audit `deleg_81fa2e8d`; existing `bot/forward_composed_paper.py`, `bot/paper_wallet_runner.py`, `bot/simulator.py`.

## Intent

Make one small saved file point to existing pre-risk picker, guard, and optionally Router, so the same replay-selected rule can select genuinely new forward candidates. Then send qualified intents through an isolated, persistent paper account using existing Simulator decision/Kelly/risk/lifecycle code. No copied lane functions, raw rewrites, real-wallet access, live order, service/config activation, or implicit enrollment.

## Implemented contract

The new `pre_risk_router_guard_kelly.yaml` explicitly selects existing pre-risk picker, pre-risk fee-aware guard, and Router lanes; removing its Router veto selects guard-only. Supported saved selection uses the same forward selector in collector replay and disabled observer, including a picker BUY rejected by the original stable wallet and a guard estimate distinct from the recorded selected-side ask. Stable-saved/pre-risk mismatch fails before output; pre-risk-saved/stable-only **forward rows** fail at the selector, while collector replay also enables the required picker lanes and allows extra stable diagnostic lanes. The no-config stable guard remains unaffected by the pre-risk-only recorded estimate. This is still a diagnostic/in-memory observer, not a persistent composed paper account.

## Evidence and review

- Baseline: local beta merge `1e227a5`, full suite 1,419 passed, 7 skipped. Scoped builder `deleg_3ed78128` reports RED/GREEN; parent independently ran 67 focused and 1,422 full tests OK (7 skipped) at initial checkpoint; after correction, 68 focused and 1,423 full tests OK (7 skipped), plus `git diff --check`.
- Independent review: read-only seam audit `deleg_81fa2e8d` identified pre-risk saved variant. Skeptical feature review `deleg_109bf643` BLOCKED old clean tip `17e6a7553bee44431c5f9b70a986a549de1962cc`: extra stable guard was incorrectly treated as cohort mismatch, and no-config stable guard consumed newly forwarded execution estimate. Both reproduced RED and locally corrected; fresh exact-tip review is pending.
- Replay/cohort: synthetic tests only; no fresh active cohort or actual wallet state inspected.
- Merge ancestry: branch starts at current local beta `1e227a5`, ahead of fetched `origin/beta` `a52b127`.

## Blockers and deferred work

- **Saved pre-risk contract:** initial review blockers corrected locally with tests; obtain fresh independent verdict on a clean corrected tip. Later fresh exact-cohort verification is an activation prerequisite, not code-merge proof. This does not enroll paper.
- **Persistent account:** use a new explicitly isolated paper root and Simulator, with processed-decision identity and persisted cohort/config binding; tests must cover restart/replay idempotency (BUY and SKIP), safe account-root preflight, and actual shared-core sizing/risk. Do not use the existing stable/beta wallet namespace.
- **Settlement:** market-only Simulator resolver is not an exact authoritative composed-decision join; hold pending until a later nonconflicting exact decision/candidate/run/market receipt is adapted into paper lifecycle. VOID releases capital without P&L. Test before any P&L claim.
- **Activation:** require independent feature-tip and exact combined beta tree review plus fresh, complete collector-owned cohort, verified strict Router history and forward consumer smoke (`t_8215b7e1`, `t_547c1d89`, `t_c6d04b67`); no activation authorized here.

## Interruption / resume handoff

- **Owning ref:** `feat/saved-pre-risk-paper-wallet`
- **Latest immutable recovery checkpoint:** `6eeca18c589cbec02f91854db1c222740e908a86` (dossier-only follow-up tip)
- **Feature implementation commits:** `9017660a3e140754adb23bc0f9e4635bd4c1e9bb`, `6eeca18c589cbec02f91854db1c222740e908a86`
- **Exact resume point:** review the saved-selector code checkpoint, then TDD the isolated persistent paper consumer (root, restart/idempotency, Simulator sizing) and exact-authority settlement; do not activate a real wallet or runtime.
- **Working-tree state at handoff:** clean after this dossier-only handoff commit; subsequent account changes start from this feature tip, not beta.

## Decision gates

- **Integration:** code-only isolated branch, focused/full tests, independent tip review, exact staged beta-tree review, no raw/runtime changes.
- **Activation / cohort:** separately reviewed explicit profile and fresh owner cohort; wallet-root and resolver safety verified before any account write.
- **Promotion:** later independent full-overlap forward evidence and explicit decision; never implied by replay, merge, or paper enrollment.

## Decision record

- 2026-10-12 — created branch from reviewed local beta after read-only seam audit; no active paper enrollment or live authorization.
- 2026-10-12 — saved pre-risk pattern added with paired synthetic replay/forward tests; parent focused 67 passed; no persistent account or activation.
- 2026-10-12 — independent review BLOCKED old tip for overbroad diagnostic-lane rejection and altered no-config guard estimate. RED/GREEN corrected both; parent focused 68 and full 1,423 OK/7 skipped. Re-review corrected commit, not the blocked tip.
