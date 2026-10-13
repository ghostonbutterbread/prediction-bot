# Reusable lane combination integration dossier

- **Status:** third independent review BLOCKED; corrected feature code tested, fresh re-review pending
- **Owner:** Hermes, task `t_50c237c6`
- **Branch / owning ref:** `feat/lane-combination-promotion`
- **Base commit:** `b653de4c40f3b61f8f2265681d5a8fdd6bdb661e`
- **Intended integration target:** `beta`
- **Last updated:** 2026-10-12 PDT
- **Latest immutable recovery checkpoint:** `3db0a2d7831cb805d5bccf26815153436d9261bb` (a later dossier-only handoff may follow)
- **Feature implementation commits:** `2e63eef2e53dcf91e82e33a43fc21238e897b1b2`, `fef01b926a156b118e6c64fbb840eb2ad17431ac`, `1745334b9839bb7e6ceadcdf0f9b98e199c2589d`, `69551f28b58073e62aa9bf6e075d202cc714a190`, `3db0a2d7831cb805d5bccf26815153436d9261bb`
- **Inspiration:** Ryu's Discord thread `1553496349343752358` asked for one easy saved lane combination, tested in replay, reused in forward paper, with Kelly/risk sizing after choosing what to buy. Existing `lane_compositions/*.yaml`, `scripts/paper_shadow_lane_compose_replay.py`, and concurrently owned `feat/forward-composed-paper` are the implementation seams.

## Intent and boundaries

Ingest an existing composition YAML directly into collector-lane replay, run its named lanes on the same collector candidates, preserve composed SKIP/BUY and wallet-intent provenance, and pass the same combination into the forward paper observer once its separate owner integrates. No duplicated lane functions, new collector, live execution, real wallet, raw artifact edits, or runtime activation. The other task `t_36c37b20` owns the forward observer and is staging beta; never modify its worktree or beta index.

## Implemented contract

`scripts/collector_lane_replay.py --composition-config lane_compositions/<idea>.yaml` selects existing component lanes on one candidate set and writes composed decisions/intents into derived outputs. The two *forward-supported* variants—stable + selected-side guard, with or without strict Router confirmation—use the **same `compose_forward_rows` selector** in replay and the disabled forward observer; the saved veto list changes BUY/SKIP selection. Other YAML ideas remain generic replay-only diagnostics and fail explicitly at forward enrollment. The forward wallet uses existing synthetic shared-core Kelly/risk and an in-memory account, not production account state. The saved Router example retains exact parsed-byte SHA identity and strict `router_history_unverified` and `guard_quote_unavailable` blockers. Recursively scrub known outcome/result/settlement aliases from replay inputs, and mark guard-composed quotes `quote_not_fill` so a later outcome cannot fabricate a fill or fill P&L. Supported replay output records shared identity/decision ID, reason and missing-intent coverage. Unknown keys/lanes/invalid settings fail before outputs. **Recorded stable actions—not full pre-risk picker re-execution—remain the candidate universe. No promotion, persistent-wallet parity, fill proof, or live activation is claimed.**

## Evidence and review

- Tests and commands: RED `TypeError` for absent `composition_config`, GREEN single candidate test; RED unknown lane was rejected only *after* output creation, GREEN validates before creation. RED missing saved sample config, GREEN YAML parsed with expected role/veto lanes. Focused `PYTHONPATH=<worktree> python3 -m unittest tests.test_collector_lane_replay -v` — 6 passed. Isolated full suite rerun on sample YAML/test — exit 0 (captured subprocess output omitted the count); earlier full receipt before sample was 1,382 passed, 7 skipped, 70.644 s. `git diff --check` passed.
- Independent reviews: `deleg_f7e2fd90` BLOCKED `2e63eef` (intent/typo/partial-output/outcome); `deleg_3e6476b1` BLOCKED `1745334` (further aliases, quote as fill P&L, replay/forward divergence); `deleg_5ee9c214` BLOCKED `c1b3436` (remaining `winner`/`payout`/`resolved`/`void` aliases and unvalidated gate fields). Earlier quote, strict-selection, and file-hash repros passed on `c1b3436`. Corrected remaining blockers via shared canonical replay forbidden-field predicate, preflight from the actual gate context's field set, and repaired legacy saved `price` gates; guard-only success reason now accurately states Router was not required. Fresh read-only exact-tip verdict required.
- Current verification: focused `tests.test_collector_lane_replay tests.test_forward_composed_paper tests.test_paper_shadow_lane_compose_replay` — 53 passed. Isolated full `PYTHONPATH=<worktree> python3 -m unittest discover -s tests -q` — 1,408 tests, 7 skipped, 64.540 s, exit 0. `git diff --check` passed before final staged check.
- Ancestry/merge evidence: sibling forward observer reviewed, locally merged and pushed to beta `3fdf4efc665357d83c0a2124b7ae31e7ec169bdb`; this feature merged that beta as `9c049ed9a61bf7e688e92ca96dc63fcde0252cf7`. No feature merge or push yet.

## Blockers and deferred work

- **Integration:** corrected exact feature tip and combined beta tree require fresh independent read-only acceptance before any feature merge or push. Tests and sample prove synthetic logic only; no real-cohort validation performed. The recursive scrub is not a proven complete positive allowlist of every unknown collector extension; retain diagnostic status.
- **Promotion:** a forward paper wallet equivalent to production account state, independently settled full-overlap cohort, and live execution approval are not established by a synthetic observer. Pre-risk picker intents remain uncaptured by this observer; accepted stable rows are the exact universe. Strict Router-history coverage and real quote/fill eligibility must be checked on a named fresh cohort. Request separate activation authorization before any service/profile/live change.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/lane-combination-promotion`
- **Latest immutable recovery checkpoint:** `3db0a2d7831cb805d5bccf26815153436d9261bb`; any later tip is handoff-only unless verified otherwise.
- **Exact resume point:** ask fresh skeptical reviewer to reproduce forbidden aliases, unknown gate-field preflight and valid saved `price` alias, and verify prior quote-only and strict-selection fixes on this exact tip. If accepted, stage a noncommitting merge on clean beta, rerun focused/full suite on that exact tree, obtain exact-tree review and integrate code only. Never enable the observer or live path from this branch.
- **Working-tree state:** isolated branch; verify before handoff.

## Decision gates

- **Integration:** focused/full tests, independent read-only review on exact feature and combined beta tree; remove this dossier from target.
- **Activation / cohort:** separate owner authorization; verified strict history and fresh shared snapshot.
- **Promotion:** fresh forward comparison and paper-wallet parity; live needs explicit approval.

## Decision record

- 2026-10-12 PDT — two read-only reviews blocked successive checkpoints; the second identified alias leakage, false fill P&L, and a label-only YAML binding. Added RED/GREEN tests for each, shared the supported decision selector between replay/forward, made Router veto optional and effective when saved, and kept guard quote-only with strict selected-side checks. Full isolated suite green; no integration/activation.
- 2026-10-12 PDT — third reviewer found additional blind aliases and an accepted misspelled gate field. RED/GREEN regressions use the existing replay forbidden-name predicate and reject unknown fields before derived output; an existing saved `price` floor/cap now reads entry-price context rather than dropping all rows. Full isolated suite green; re-review pending.
