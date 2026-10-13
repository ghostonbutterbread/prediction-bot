# Persistent composed paper wallet integration dossier

- **Status:** feature in progress; disabled and unenrolled
- **Owner / task:** Hermes, prediction-bot Kanban `t_ab52708b`
- **Branch / owning ref:** `feat/persistent-composed-paper-wallet`
- **Base commit / intended target:** `3911f1808828261d8cfe712144e881c5b0882351` / local `beta`
- **Last updated:** 2026-10-12 PDT
- **Latest immutable recovery checkpoint / implementation commits:** `f3e5ec8eeabc54641d5e842042fb7ce311813c66` (entry consumer and tests); a later dossier-only tip records review status.
- **Canonical context:** code-only reviewed saved pre-risk selector at beta `3911f18`; read-only seam audit `deleg_81fa2e8d`; `bot/forward_composed_paper.py`, `bot/composed_lane_wallet.py`, `bot/simulator.py`, `bot/paper_adapters.py`, `bot/resolver.py`.

## Intent

Consume qualified new-market BUY/SKIP decisions selected by the *same saved combination* in an explicitly isolated, persistent, paper-only account. Reuse Simulator's existing decision, Kelly, risk, reservation, session, and settlement seams; never interpret the composition's diagnostic notional as spend. Replay remains outcome-blind; decision-side quotes and estimated fills are not exchange fills. No current stable/beta wallet, service, active config, raw archive, or live orders may be touched. Loading a file does not enroll any account.

## Implemented contract

Disabled-by-default callable `consume_persistent_composed_paper` accepts the saved pre-risk file, append-only lane rows, publisher state, cohort ID, explicit isolated paper root and protected stable/beta/live roots. It validates the complete latest-snapshot picker cohort and selects only that fresh publisher snapshot; older appended observations are not newly enrolled. It binds saved bytes and effective account/risk settings, journals BUYs and SKIPs, and uses `Simulator.submit_paper_signal` for shared-core Kelly/risk, simulated fills, reservation and persistence. An uncertain write remains pending and fences later BUYs until reconciled with a saved trade marker. Open positions have no economic settlement claim. The callable has no scheduler or activation path.

## Evidence and review

- Baseline: code-only beta merge `3911f18`, reviewed staged tree `725a6383886a9f0ca4eaf226cc2dd356a03bf20c`, full suite 1,423 OK/7 skipped.
- Tests: RED→GREEN latest-snapshot regression (old and current rows previously opened two trades, now only current opens); 13 focused passed; full `PYTHONPATH=. python3 -B -m unittest discover -s tests -q`: 1,436 OK, 7 skipped. Tests use temporary paper roots only.
- Review: pending independent feature-tip and exact combined-tree gates.
- Fresh cohort: not started; no archive or active account inspected.

## Blockers and deferred work

- **Entry and persistence:** implemented as an isolated disabled callable; independent skeptical review still required. The crash between journal and session is fenced, not autonomously recoverable without a saved marker. Fresh publisher-state and cohort proof is synthetic only; no runtime enrollment.
- **Settlement (read-only seam audit `deleg_5a897539`):** do not use `TradeResolver.get_market(market_id)` or a shim; it lacks decision/candidate/run identity, permits untimed receipts, and does not retain resolution-row provenance. Add a narrow exact-receipt method to the existing Simulator paper resolution adapter. Require one independently verified nonconflicting decision/candidate/run/market binding with source-row hash and authoritative settlement later than entry; release at the later of settlement and outcome-known time. Persist the binding, both clocks, and hash with the trade, and use existing accounting helpers for YES/NO and VOID. RED tests: two same-market open identities, one exact later receipt settles only one after availability; market-only, untimed, wrong-identity and conflicting receipts remain pending; valid exact VOID releases without P&L; replay after restart is a no-op. **Crash gap:** session and risk-state files are separately written; reconcile daily P&L/streak/cooldown from a durable trade authority before claiming crash-safe accounting. Treat Simulator prices as simulated fills, not observed exchange fills.
- **Activation:** reviewed code-only integration is not enrollment. Verify a fresh full-overlap picker cohort, strict Router history/admission, exact consumer smoke, explicit root/owner/retention, and independent review before any runtime paper account (`t_8215b7e1`, `t_547c1d89`, `t_c6d04b67`). Live separately authorized.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/persistent-composed-paper-wallet`
- **Latest immutable recovery checkpoint:** `f3e5ec8eeabc54641d5e842042fb7ce311813c66`
- **Feature implementation commits:** `f3e5ec8eeabc54641d5e842042fb7ce311813c66`
- **Exact resume point:** independent read-only feature-tip review of the disabled entry consumer, including append-only latest-only behavior, real producer shape, isolation, failure/restart idempotency and side quote/estimate. Fix blockers with RED→GREEN and re-review. Then TDD exact-authority settlement and VOID before combined-tree review. Keep API code-only and no activation.
- **Working-tree state:** entry code and tests committed, dossier-only handoff follows; independent review pending.

## Decision gates

- **Integration:** TDD focused/full tests, independent clean feature-tip review, exact staged beta tree review; no raw/runtime changes.
- **Activation:** fresh independently verified cohort and explicit operational review/authorization; root isolation, retention and single owner.
- **Promotion:** independently resolved forward evidence and explicit authorization. Never implied by code merge.

## Decision record

- 2026-10-12 — started isolated successor from reviewed local beta; no account/service/config activation.
- 2026-10-12 — read-only settlement audit `deleg_5a897539` found market-only/untimed generic resolver and separate non-atomic session/risk writes; direct exact-receipt adapter and crash-recovery proof required before settlement claims.
- 2026-10-12 — disabled entry consumer implemented via delegated RED→GREEN; parent reproduced and repaired an append-only historical-snapshot enrollment error with a new failing test. Full isolated suite 1,436 OK/7 skipped. No runtime changes.
