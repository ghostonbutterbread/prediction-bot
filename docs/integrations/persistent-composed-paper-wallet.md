# Persistent composed paper wallet integration dossier

- **Status:** feature in progress; disabled and unenrolled
- **Owner / task:** Hermes, prediction-bot Kanban `t_ab52708b`
- **Branch / owning ref:** `feat/persistent-composed-paper-wallet`
- **Base commit / intended target:** `3911f1808828261d8cfe712144e881c5b0882351` / local `beta`
- **Last updated:** 2026-10-13 PDT
- **Latest independent ACCEPT:** entry implementation `b8fa1c7c6b2d4893890e3df21f9dc1c1766e22f4`. **Latest experimental recovery checkpoint:** `423932474e0ec00ffd27196596421ba4bd7a9543` (settlement code and tests; not independently reviewed or integrated); this dossier-only handoff follows it.
- **Canonical context:** code-only reviewed saved pre-risk selector at beta `3911f18`; read-only seam audit `deleg_81fa2e8d`; `bot/forward_composed_paper.py`, `bot/composed_lane_wallet.py`, `bot/simulator.py`, `bot/paper_adapters.py`, `bot/resolver.py`.

## Intent

Consume qualified new-market BUY/SKIP decisions selected by the *same saved combination* in an explicitly isolated, persistent, paper-only account. Reuse Simulator's existing decision, Kelly, risk, reservation, session, and settlement seams; never interpret the composition's diagnostic notional as spend. Replay remains outcome-blind; decision-side quotes and estimated fills are not exchange fills. No current stable/beta wallet, service, active config, raw archive, or live orders may be touched. Loading a file does not enroll any account.

## Implemented contract

Disabled-by-default callable `consume_persistent_composed_paper` accepts the saved pre-risk file, append-only lane rows, publisher state, cohort ID, explicit isolated paper root and protected stable/beta/live roots. It validates the complete latest-snapshot picker cohort, requires every BUY picker to have a matching control identity, and selects only that fresh publisher snapshot; older appended observations and materially stale observations mislabeled with the latest snapshot ID are not newly enrolled. It binds saved bytes and effective account/risk settings, journals BUYs and SKIPs, and uses `Simulator.submit_paper_signal` for shared-core Kelly/risk, simulated fills, reservation and persistence. An uncertain write remains pending and fences later BUYs until reconciled with a saved trade marker. The callable has no scheduler or activation path. An unreviewed exact-resolution adapter can read an explicit strict JSONL source and exact binding file, verify source and row hashes/identity/finality/clocks, calculate simulated-fill accounting, and fence an interrupted session/risk write rather than automatically recover it; the entry consumer accepts *only* marked exact settlements on restart. No settlement producer or runtime caller is enrolled.

## Evidence and review

- Baseline: code-only beta merge `3911f18`, reviewed staged tree `725a6383886a9f0ca4eaf226cc2dd356a03bf20c`, full suite 1,423 OK/7 skipped.
- Tests: RED→GREEN latest-only, orphan BUY and mislabeled stale-observation regressions; ordinary observation-to-publication lag still opens. 16 focused passed; full `PYTHONPATH=. python3 -B -m unittest discover -s tests -q`: 1,439 OK, 7 skipped. Tests use temporary paper roots only.
- Review: `deleg_dc185644` BLOCKED prior exact tip `d214e800c5ac952a51abff5ac89ba7e897ce5fba` (orphan picker BUY opened without control; old observed-at row sharing latest snapshot ID opened). Corrected at `b8fa1c7`; `deleg_f640c98b` ACCEPTED exact corrected tip `994b7eb1486d6372ead86035ffa7f981daafefa1` for **entry code only**, independently reproducing the prior failures and validating realistic publisher clocks, diagnostic extras, restart, 16 focused and 1,439 full OK/7 skipped. This is not settlement/enrollment acceptance.
- Settlement experiment: `deleg_69ac514f` left partial files when its provider returned HTTP 402 (remaining budget could not meet requested context). Parent inspected the partial work, RED→GREEN-tested post-settlement re-entry, nonfinite settled P&L rejection, and bounded source streaming; 32 focused tests passed. Full isolated `PYTHONPATH=. python3 -B -m unittest discover -s tests -q`: **1,455 OK, 7 skipped**; `git diff --check` clean. No independent settlement review occurred; entry ACCEPT does **not** cover these changed files.
- Fresh cohort: not started; no archive or active account inspected.

## Blockers and deferred work

- **Entry and persistence:** independently accepted isolated disabled callable, **entry only**. The crash between journal and session is fenced, not autonomously recoverable without a saved marker. Fresh publisher-state and cohort proof is synthetic only; no runtime enrollment.
- **Settlement experiment not release-ready:** the code verifies that supplied source bytes and exact binding agree; their hash does not itself prove the source came from an independently authoritative collector, and no trusted forward exact-binding producer or fresh-cohort source-selection receipt exists. Prove that source authority and decision→binding provenance before treating the method as settlement evidence. The separate session/risk writes are guarded by a durable unsafe marker and fail closed on an interrupted write, **not** automatically reconciled; demonstrate a supported recovery procedure and test each crash boundary before persistent-wallet acceptance. Existing tests use synthetic strict source rows and simulated fills, not observed exchange fills. Obtain a fresh skeptical independent review of the entire settlement diff and a combined-tree review before any beta merge. The reviewer provider currently returned HTTP 402; request an authorized alternative or restored budget rather than treating a parent-only test as review.
- **Activation:** reviewed code-only integration is not enrollment. Verify a fresh full-overlap picker cohort, strict Router history/admission, exact consumer smoke, explicit root/owner/retention, and independent review before any runtime paper account (`t_8215b7e1`, `t_547c1d89`, `t_c6d04b67`). Live separately authorized.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/persistent-composed-paper-wallet`
- **Latest independently accepted checkpoint:** `b8fa1c7c6b2d4893890e3df21f9dc1c1766e22f4` (entry code). **Latest unreviewed implementation checkpoint:** `423932474e0ec00ffd27196596421ba4bd7a9543` (experimental settlement). Current tip includes this later dossier-only handoff; reviewers must inspect the full diff from beta, not reuse the earlier entry verdict.
- **Feature implementation commits:** `f3e5ec8eeabc54641d5e842042fb7ce311813c66`, `b8fa1c7c6b2d4893890e3df21f9dc1c1766e22f4`, `423932474e0ec00ffd27196596421ba4bd7a9543`.
- **Exact resume point:** establish a verifiable forward strict-source and exact-binding producer, test unsafe-marker recovery across crash boundaries, then independently review settlement and the exact combined beta tree. Keep code-only and no activation until accepted.
- **Working-tree state:** experimental settlement code and tests are committed at `4239324`; this dossier-only handoff follows. No beta merge, push, or runtime account change.

## Decision gates

- **Integration:** TDD focused/full tests, independent clean feature-tip review, exact staged beta tree review; no raw/runtime changes.
- **Activation:** fresh independently verified cohort and explicit operational review/authorization; root isolation, retention and single owner.
- **Promotion:** independently resolved forward evidence and explicit authorization. Never implied by code merge.

## Decision record

- 2026-10-12 — started isolated successor from reviewed local beta; no account/service/config activation.
- 2026-10-12 — read-only settlement audit `deleg_5a897539` found market-only/untimed generic resolver and separate non-atomic session/risk writes; direct exact-receipt adapter and crash-recovery proof required before settlement claims.
- 2026-10-12 — disabled entry consumer implemented via delegated RED→GREEN; parent reproduced and repaired an append-only historical-snapshot enrollment error with a new failing test. Full isolated suite 1,436 OK/7 skipped. No runtime changes.
- 2026-10-12 — `deleg_dc185644` independently BLOCKED entry tip `d214e80` for orphan picker BUY and stale observation sharing the latest snapshot ID. Parent reproduced both RED, fixed at `b8fa1c7` without changing extra diagnostic lanes; 16 focused, full 1,439 OK/7 skipped. Re-review required.
- 2026-10-12 — `deleg_f640c98b` ACCEPTED exact corrected entry tip `994b7eb` for code-only checkpoint after independent RED repro, realistic fresh publisher and diagnostic control, restart check, and full 1,439 OK/7 skipped. Settlement/enrollment still blocked.
- 2026-10-13 — settlement implementation delegation failed with HTTP 402 after changing only task-owned files; parent repaired bounded source reading and post-settlement entry continuity, full isolated suite 1,455 OK/7 skipped. This is an **unreviewed synthetic-only checkpoint**; source authority, forward binding producer and crash-recovery demonstration remain blockers.
