# Synthetic Kelly conflicting-resolution receipts — integration dossier

- **Status:** initial independent review blocked beta integration on decision-ID alias and secondary-clock validity gaps; isolated test-first correction passed focused and final full suites, fresh independent review and beta-first compatibility pending; no merge/push/runtime activation
- **Owner:** Hermes, Kanban `t_5fc5d6e8` (parent audit `t_500d82fb`)
- **Branch / owning ref:** `fix/kelly-conflicting-resolution-receipts`
- **Base commit:** `02c5580b61fa4c30a54c4f80884cac329d21af0c`
- **Intended integration target:** `beta`
- **Last updated:** 2026-09-27
- **Latest immutable implementation checkpoint:** `aa255df87316ac0786be97b1f5263211ce666a69` (current tip will include a later dossier-only commit)
- **Feature implementation commits:** `05dd7ff93531c829d0f3ee61bc7d75ac6eb72ff7` (initial), `aa255df87316ac0786be97b1f5263211ce666a69` (review correction)
- **Inspiration:** the same decision's equally timed YES and NO settlement receipts produced $110 or $90 in the sequential synthetic wallet depending on input order.

## Intent and implemented contract

Keep this derived-only collector-lane diagnostic explicit: it replays *recorded* decisions and synthetic capacity, not an executable paper wallet or trading strategy. Accept at most one unambiguous exact settlement for each decision. Equivalent duplicate authority (same market, outcome, settlement instant; nonempty candidate/run identities do not disagree) settles once; prefer a more fully identity-bound receipt, then choose between equivalent retrieval receipts deterministically. A legacy flat duplicate with omitted candidate/run identifiers does not suppress a consistent exact nested receipt. Contradictory accepted receipts, including differing settlement instants, leave capital reserved and the position pending with `resolution_blocker=conflicting_resolution_receipts` and a counted `ambiguous_resolution_decisions` summary. Never choose by input order or earliest conflicting timestamp.

Reject explicit non-final market status and non-YES/NO/VOID outcome rather than booking a synthetic loss. Preserve legacy flat receipts without an optional status when the existing identity and exact settlement fields suffice; nested matched receipts still require candidate/run identity. When outer and nested identity, outcome, or same-named settlement clock disagree, do not select one by precedence. `outcome_known_at` is not a recorded `settlement_ts`; without a settlement instant this sequential wallet cannot release capital. This does not change immutable inputs, resolver output, active risk/wallet state, or other paper-shadow P&L consumers.

Review correction: conflicting outer `decision_id`/`lane_decision_id` aliases invalidate that receipt instead of choosing either identifier. Reconcile every retained settlement and outcome-known clock against the corresponding decision time **before** comparing authority candidates, so a pre-decision receipt cannot settle or inflate ambiguity when a valid later receipt exists. Preserve independently recorded settlement time but delay synthetic capital release until the later of settlement and outcome-known clocks (`available_at`). Equivalent duplicate receipts conservatively retain the latest such availability time regardless of input order. A repeated nonunique decision ID does not acquire an authoritative receipt by an arbitrary last-wins time lookup.

## Evidence and review

- RED against base behavior: opposing YES/NO receipts at one decision/time settled a $10 stake as +$10 or -$10 depending on order; `PENDING` outcome booked -$10; explicit `market_status=active` booked +$10; contradictory nested market/outcome/settlement aliases could book P&L; outcome-known time alone was used as settlement.
- GREEN worktree-first `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest tests.test_collector_lane_replay_kelly tests.test_beta_handoff_identity_audit -q`: 31 tests OK. Equivalent duplicate receipts settle once with order-independent provenance; VOID remains a capital refund with no P&L. A second RED/GREEN cycle prevented a flat receipt missing candidate/run enrichment from suppressing a matching exact nested receipt. `git diff --check` passed.
- Initial isolated full discovery completed **1,227 tests OK (7 skipped)** on the first correction, receipt `/home/ryushe/.hermes/cache/scratch/prediction-kelly-conflicting-receipts-full-20260927.log`. This predates the legacy-duplicate adjustment and is **not** the final code gate; rerun full discovery after the adjustment.
- Final-code isolated discovery `PYTHONPATH=<worktree> /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` completed **1,228 tests OK (7 skipped), 63.053s**, receipt `/home/ryushe/.hermes/cache/scratch/prediction-kelly-conflicting-receipts-full-final-20260927.log`.
- Independent read-only review `deleg_deafe3da` **blocked beta integration** of the first checkpoint `cf25d06c21659d581b1ea6a9b0b91396a827891b`: flat receipt with `decision_id=d` and `lane_decision_id=other` booked unsupported +$10; a pre-decision secondary `outcome_known_at` of 11:00 alongside settlement 13:00 booked +$10 for a 12:00 decision; a pre-decision receipt alongside a later valid one made ambiguity count 1 and kept capital pending instead of accepting the later authority. Reviewer verified baseline conflicting receipts, equivalent duplicates, VOID, and legacy flat receipt; focused 31 OK.
- Follow-up RED/GREEN: contradictory decision aliases and pre-decision outcome-known time each booked +$10; a 13:00 settlement with 14:00 outcome-known time released capital before a 13:30 decision (110 instead of 90 available cash). Repaired at the receipt-mapping/chronology boundary. Invalid pre-decision receipt plus valid later receipt now settles once with ambiguity count zero in either order; equivalent duplicate receipts with differing later known-at times use the later availability deterministically. Focused worktree-first `tests.test_collector_lane_replay_kelly tests.test_beta_handoff_identity_audit -q` **35 tests OK**, `git diff --check` passed.
- First follow-up full discovery run hit a 360s command timeout before a suite verdict; its log `/home/ryushe/.hermes/cache/scratch/prediction-kelly-review-clock-full-20260927.log` is **incomplete**, not a test failure. Tracked background retry `proc_aba5974ef5e6` exited **0**; final worktree-first full discovery passed **1,232 tests OK (7 skipped), 123.566s**, receipt `/home/ryushe/.hermes/cache/scratch/prediction-kelly-review-clock-full-retry-20260927.log`.
- Fresh independent read-only review and beta-first compatibility against then-current beta are required before integration.
- No raw evidence, service, live balance/order, config, or runtime changes.

## Blockers and deferred work

- **Integration gate:** fresh independent verdict on conflict/duplicate/alias/clock/legacy compatibility; beta-first non-committing merge with focused/full tests and staged diff check. Retain this dossier only on the feature branch; exclude it from beta during integration.
- **Out-of-scope validity issues:** other paper-shadow source-side quote provenance and market-only P&L authority are separate audit items; no full strategy/wallet parity claim. Repeated decision rows and execution-grade fill provenance require separate evidence before performance claims.

## Interruption / resume handoff

- **Branch:** `fix/kelly-conflicting-resolution-receipts` from `02c5580b61fa4c30a54c4f80884cac329d21af0c` → `beta`.
- **Checkpoint:** latest implementation `aa255df87316ac0786be97b1f5263211ce666a69`; this branch's current tip includes a later dossier-only handoff commit.
- **Exact resume:** obtain fresh independent read-only review of corrected code and verified full-suite receipt on the clean branch tip; beta-first compatibility follows only after acceptance. Do not merge before review. No deployment from this code task.
- **Working-tree state at handoff:** clean after the dossier-only handoff checkpoint; no raw or runtime modifications.

## Decision gates

- **Integration:** focused/full suites, independent review, clean beta-first merge test, no unowned diff.
- **Activation:** separate explicit runtime decision; this is a derived-only diagnostic correction.
- **Promotion:** none; synthetic capacity is not realized P&L.

## Decision record

- 2026-09-27 — reproduced order-dependent synthetic settlement; test-first correction passed focused/full isolated suites; review and beta compatibility pending.
- 2026-09-27 — independent review blocked unsupported P&L from decision alias/secondary-clock contradictions. Corrective focused 35 OK and full 1,232 OK (7 skipped); fresh review pending. No integration decision.
