# Reusable lane combination integration dossier

- **Status:** code-only saved replay/forward observer bridge tested; independent re-review pending
- **Owner:** Hermes, task `t_50c237c6`
- **Branch / owning ref:** `feat/lane-combination-promotion`
- **Base commit:** `b653de4c40f3b61f8f2265681d5a8fdd6bdb661e`
- **Intended integration target:** `beta`
- **Last updated:** 2026-10-12 PDT
- **Latest immutable recovery checkpoint:** `fef01b926a156b118e6c64fbb840eb2ad17431ac` (followed by beta merge `9c049ed9a61bf7e688e92ca96dc63fcde0252cf7`; final code checkpoint pending)
- **Feature implementation commits:** `2e63eef2e53dcf91e82e33a43fc21238e897b1b2`, `fef01b926a156b118e6c64fbb840eb2ad17431ac`
- **Inspiration:** Ryu's Discord thread `1553496349343752358` asked for one easy saved lane combination, tested in replay, reused in forward paper, with Kelly/risk sizing after choosing what to buy. Existing `lane_compositions/*.yaml`, `scripts/paper_shadow_lane_compose_replay.py`, and concurrently owned `feat/forward-composed-paper` are the implementation seams.

## Intent and boundaries

Ingest an existing composition YAML directly into collector-lane replay, run its named lanes on the same collector candidates, preserve composed SKIP/BUY and wallet-intent provenance, and pass the same combination into the forward paper observer once its separate owner integrates. No duplicated lane functions, new collector, live execution, real wallet, raw artifact edits, or runtime activation. The other task `t_36c37b20` owns the forward observer and is staging beta; never modify its worktree or beta index.

## Implemented contract

`scripts/collector_lane_replay.py --composition-config lane_compositions/<idea>.yaml` now selects all referenced component/veto lanes for the same collector snapshots, runs the existing decision-row composer, and writes `composition_rows.jsonl` plus outcome-free `wallet_intents.jsonl` into the derived replay directory. Its summary records the config path/raw SHA-256 from the exact parsed bytes, candidate and SKIP diagnostics, and missing wallet-intent coverage; it is explicitly diagnostic. Unknown/misspelled keys, invalid gate/exposure operations, and unknown lanes fail before outputs. The replay input drops recorded outcome/result/resolution fields from signal and shared candidate. `lane_compositions/stable_router_guard_kelly.yaml` references existing stable, Router, and selected-side guard lanes; the forward manual observer `--composition-config` accepts exactly this supported role/veto shape, records the same file hash/name, and passes qualified BUY intents through existing synthetic shared-core Kelly/risk and in-memory wallet. Other replay combinations fail explicitly at forward enrollment rather than silently running the hardcoded flow. Existing `--lane` and disabled-by-default observer behavior remain unchanged. **This is not the actual persistent paper wallet, blind full-picker replay, fill proof, or live promotion.**

## Evidence and review

- Tests and commands: RED `TypeError` for absent `composition_config`, GREEN single candidate test; RED unknown lane was rejected only *after* output creation, GREEN validates before creation. RED missing saved sample config, GREEN YAML parsed with expected role/veto lanes. Focused `PYTHONPATH=<worktree> python3 -m unittest tests.test_collector_lane_replay -v` — 6 passed. Isolated full suite rerun on sample YAML/test — exit 0 (captured subprocess output omitted the count); earlier full receipt before sample was 1,382 passed, 7 skipped, 70.644 s. `git diff --check` passed.
- Independent review: `deleg_f7e2fd90` BLOCKED initial replay checkpoint `2e63eef` on absent wallet intents, silent config typo/partial output, and nested outcome leakage. Corrected with focused RED/GREEN tests; forward producer field projection was independently reviewed and integrated on beta. Fresh review of corrected exact tip pending.
- Current verification: focused `tests.test_collector_lane_replay tests.test_forward_composed_paper tests.test_paper_shadow_lane_compose_replay` — 48 passed. Isolated full `PYTHONPATH=<worktree> python3 -m unittest discover -s tests -q` — 1,403 passed, 7 skipped, 74.555 s. `git diff --check` pending final staged check.
- Ancestry/merge evidence: sibling forward observer reviewed, locally merged and pushed to beta `3fdf4efc665357d83c0a2124b7ae31e7ec169bdb`; this feature merged that beta as `9c049ed9a61bf7e688e92ca96dc63fcde0252cf7`. No feature merge or push yet.

## Blockers and deferred work

- **Integration:** corrected feature diff and combined beta tree require fresh independent read-only acceptance before any feature merge or push. Tests and sample prove synthetic logic only; no real-cohort validation performed.
- **Promotion:** a forward paper wallet equivalent to production account state, independently settled full-overlap cohort, and live execution approval are not established by a synthetic observer. Pre-risk picker intents remain uncaptured by this observer; accepted stable rows are the exact universe. Strict Router-history coverage and real quote/fill eligibility must be checked on a named fresh cohort. Request separate activation authorization before any service/profile/live change.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/lane-combination-promotion`
- **Latest immutable recovery checkpoint:** final implementation commit pending; previous checkpoint `fef01b926a156b118e6c64fbb840eb2ad17431ac`, merge `9c049ed9a61bf7e688e92ca96dc63fcde0252cf7`.
- **Exact resume point:** commit this tested correction and ask a fresh skeptical reviewer to inspect both replay/forward paths, the config validation/captured hash, the blind-input test, and the paper-only boundary. If accepted, stage a noncommitting merge on clean beta, rerun focused/full suite on that exact tree, obtain exact-tree review and integrate code only. Never enable the observer or live path from this branch.
- **Working-tree state:** isolated branch; verify before handoff.

## Decision gates

- **Integration:** focused/full tests, independent read-only review on exact feature and combined beta tree; remove this dossier from target.
- **Activation / cohort:** separate owner authorization; verified strict history and fresh shared snapshot.
- **Promotion:** fresh forward comparison and paper-wallet parity; live needs explicit approval.

## Decision record

- 2026-10-12 PDT — initial replay slice and example committed; reviewer blocked 2e63eef on three concrete gaps. After reviewed forward observer merged to beta, corrected replay validation, nested outcome scrubbing and missing wallet-intent coverage; bound the saved file to the disabled forward synthetic Kelly/risk observer only for its implemented role/veto shape. Full isolated suite green; fresh code review pending.
