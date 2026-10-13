# Reusable lane combination integration dossier

- **Status:** fourth feature-tip review accepted; first combined beta preflight blocked on exposure-cap validation; corrected isolated feature awaiting re-review
- **Owner:** Hermes, task `t_50c237c6`
- **Branch / owning ref:** `feat/lane-combination-promotion`
- **Base commit:** `b653de4c40f3b61f8f2265681d5a8fdd6bdb661e`
- **Intended integration target:** `beta`
- **Last updated:** 2026-10-12 PDT
- **Latest immutable recovery checkpoint:** `634eae0f4867278aed8d328052b4f440676528c6` (a later dossier-only handoff may follow)
- **Feature implementation commits:** `2e63eef2e53dcf91e82e33a43fc21238e897b1b2`, `fef01b926a156b118e6c64fbb840eb2ad17431ac`, `1745334b9839bb7e6ceadcdf0f9b98e199c2589d`, `69551f28b58073e62aa9bf6e075d202cc714a190`, `3db0a2d7831cb805d5bccf26815153436d9261bb`, `634eae0f4867278aed8d328052b4f440676528c6`
- **Inspiration:** Ryu's Discord thread `1553496349343752358` asked for one easy saved lane combination, tested in replay, reused in forward paper, with Kelly/risk sizing after choosing what to buy. Existing `lane_compositions/*.yaml`, `scripts/paper_shadow_lane_compose_replay.py`, and concurrently owned `feat/forward-composed-paper` are the implementation seams.

## Intent and boundaries

Ingest an existing composition YAML directly into collector-lane replay, run its named lanes on the same collector candidates, preserve composed SKIP/BUY and wallet-intent provenance, and pass the same combination into the forward paper observer once its separate owner integrates. No duplicated lane functions, new collector, live execution, real wallet, raw artifact edits, or runtime activation. The other task `t_36c37b20` owns the forward observer and is staging beta; never modify its worktree or beta index.

## Implemented contract

`scripts/collector_lane_replay.py --composition-config lane_compositions/<idea>.yaml` selects existing component lanes on one candidate set and writes composed decisions/intents into derived outputs. The two *forward-supported* variants—stable + selected-side guard, with or without strict Router confirmation—use the **same `compose_forward_rows` selector** in replay and the disabled forward observer; the saved veto list changes BUY/SKIP selection. Other YAML ideas remain generic replay-only diagnostics and fail explicitly at forward enrollment. The forward wallet uses existing synthetic shared-core Kelly/risk and an in-memory account, not production account state. The saved Router example retains exact parsed-byte SHA identity and strict `router_history_unverified` and `guard_quote_unavailable` blockers. Recursively scrub known outcome/result/settlement aliases from replay inputs, and mark guard-composed quotes `quote_not_fill` so a later outcome cannot fabricate a fill or fill P&L. Supported replay output records shared identity/decision ID, reason and missing-intent coverage. Unknown keys/lanes/invalid settings fail before outputs. **Recorded stable actions—not full pre-risk picker re-execution—remain the candidate universe. No promotion, persistent-wallet parity, fill proof, or live activation is claimed.**

## Evidence and review

- Tests and commands: RED `TypeError` for absent `composition_config`, GREEN single candidate test; RED unknown lane was rejected only *after* output creation, GREEN validates before creation. RED missing saved sample config, GREEN YAML parsed with expected role/veto lanes. Focused `PYTHONPATH=<worktree> python3 -m unittest tests.test_collector_lane_replay -v` — 6 passed. Isolated full suite rerun on sample YAML/test — exit 0 (captured subprocess output omitted the count); earlier full receipt before sample was 1,382 passed, 7 skipped, 70.644 s. `git diff --check` passed.
- Independent reviews: `deleg_f7e2fd90` BLOCKED `2e63eef` (intent/typo/partial-output/outcome); `deleg_3e6476b1` BLOCKED `1745334` (further aliases, quote as fill P&L, replay/forward divergence); `deleg_5ee9c214` BLOCKED `c1b3436` (remaining `winner`/`payout`/`resolved`/`void` aliases and unvalidated gate fields). `deleg_08d477ef` ACCEPTED clean feature tip `6489761a69b4df50065da4acf58b4bd537e7d139` for code-only integration against its older base. The subsequent staged beta preflight at tree `eee5402aa5e9246400640cd8ad554b54d807fa97` was BLOCKED by `deleg_cf05b17b`: `max_rows_per_market: bogus` silently disables the cap, while `.nan` leaves partial replay output. Aborted the blocked preflight; no beta merge. RED/GREEN synthetic replay now rejects unparseable, nonfinite, nonintegral, negative, null, and boolean caps before output creation; also rejects non-boolean `per_side`. Numeric string `"1"` remains valid and is verified to cap one market.
- Current verification on isolated feature: focused `PYTHONPATH=<worktree> python3 -m unittest tests.test_collector_lane_replay tests.test_forward_composed_paper tests.test_paper_shadow_lane_compose_replay -q` — 56 OK. Full `python3 -m unittest discover -s tests -q` from the isolated worktree — 1,411 OK, 7 skipped, 68.774 s. `git diff --check` passed. No raw artifact/realtime cohort inspected.
- Ancestry/merge evidence: sibling forward observer merged/pushed beta `3fdf4efc665357d83c0a2124b7ae31e7ec169bdb`; this feature merged that beta as `9c049ed9a61bf7e688e92ca96dc63fcde0252cf7`. Beta then advanced independently to `a52b127d3dc26cef8439a550972d277f3472c6d0` (a distinct beta ref, **not** an ancestor of this feature branch) with a separate reviewed pre-risk picker. The staged tree before the exposure fix retained beta's default pre-risk picker, rejected saved-stable/pre-risk mismatch before outputs, and passed focused/full tests, but failed independent exposure validation review. No feature merge or push yet.

## Blockers and deferred work

- **Integration:** corrected feature tip and combined beta tree require fresh independent read-only acceptance before any feature merge or push. Tests and sample prove synthetic logic only; no real-cohort validation performed. The recursive scrub is not a proven complete positive allowlist of every unknown collector extension; retain diagnostic status. Recreate preflight from clean beta `a52b127...`, reapply reviewed conflict resolution (preserve beta pre-risk default, fail saved stable/pre-risk mixing), verify exact staged tree plus focused/full tests and independent read-only verdict.
- **Promotion:** a forward paper wallet equivalent to production account state, independently settled full-overlap cohort, and live execution approval are not established by a synthetic observer. Beta's newer disabled pre-risk observer is the no-config default after integration; the saved stable YAML deliberately fails rather than silently select that picker cohort. Stable-config supported patterns remain a separate synthetic path, not persistent account state. Strict Router-history coverage and real quote/fill eligibility must be checked on a named fresh cohort. Request separate activation authorization before any service/profile/live change.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/lane-combination-promotion`
- **Latest immutable recovery checkpoint:** `634eae0f4867278aed8d328052b4f440676528c6`; a later dossier-only tip does not alter implementation.
- **Exact resume point:** request fresh skeptical review of the corrected feature tip, then stage noncommitting beta merge again. Reproduce and test pre-risk/stable resolution plus the exposure cap against that exact staged tree; obtain new independent tree verdict. Integrate code only after acceptance. Never enable the observer or live path from this branch.
- **Working-tree state:** isolated branch; verify before handoff.

## Decision gates

- **Integration:** focused/full tests, independent read-only review on exact feature and combined beta tree; remove this dossier from target.
- **Activation / cohort:** separate owner authorization; verified strict history and fresh shared snapshot.
- **Promotion:** fresh forward comparison and paper-wallet parity; live needs explicit approval.

## Decision record

- 2026-10-12 PDT — two read-only reviews blocked successive checkpoints; the second identified alias leakage, false fill P&L, and a label-only YAML binding. Added RED/GREEN tests for each, shared the supported decision selector between replay/forward, made Router veto optional and effective when saved, and kept guard quote-only with strict selected-side checks. Full isolated suite green; no integration/activation.
- 2026-10-12 PDT — third reviewer found additional blind aliases and an accepted misspelled gate field. RED/GREEN regressions use the existing replay forbidden-name predicate and reject unknown fields before derived output; an existing saved `price` floor/cap now reads entry-price context rather than dropping all rows. Full isolated suite green; re-review pending.
- 2026-10-12 PDT — fourth feature-tip review accepted `6489761`; newer beta's pre-risk picker required a semantic noncommitting merge. Combined-tree reviewer BLOCKED `eee5402` for silently disabled or late-failing exposure caps. Aborted beta preflight; on isolated feature RED/GREEN-tested early cap and `per_side` validation, retained valid numeric-string cap behavior. New feature and combined-tree review pending.
