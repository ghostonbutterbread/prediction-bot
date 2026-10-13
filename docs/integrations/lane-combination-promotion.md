# Reusable lane combination integration dossier

- **Status:** replay ingestion implemented and tested; forward integration pending sibling merge
- **Owner:** Hermes, task `t_50c237c6`
- **Branch / owning ref:** `feat/lane-combination-promotion`
- **Base commit:** `b653de4c40f3b61f8f2265681d5a8fdd6bdb661e`
- **Intended integration target:** `beta`
- **Last updated:** 2026-10-12 PDT
- **Latest immutable recovery checkpoint:** none yet
- **Feature implementation commits:** none yet
- **Inspiration:** Ryu's Discord thread `1553496349343752358` asked for one easy saved lane combination, tested in replay, reused in forward paper, with Kelly/risk sizing after choosing what to buy. Existing `lane_compositions/*.yaml`, `scripts/paper_shadow_lane_compose_replay.py`, and concurrently owned `feat/forward-composed-paper` are the implementation seams.

## Intent and boundaries

Ingest an existing composition YAML directly into collector-lane replay, run its named lanes on the same collector candidates, preserve composed SKIP/BUY and wallet-intent provenance, and pass the same combination into the forward paper observer once its separate owner integrates. No duplicated lane functions, new collector, live execution, real wallet, raw artifact edits, or runtime activation. The other task `t_36c37b20` owns the forward observer and is staging beta; never modify its worktree or beta index.

## Implemented contract

`scripts/collector_lane_replay.py --composition-config lane_compositions/<idea>.yaml` now selects all referenced component/veto lanes for the same collector snapshots, runs the existing decision-row composer, and writes `composition_rows.jsonl` plus outcome-free `wallet_intents.jsonl` into the derived replay directory. Its summary records the config path/raw SHA-256, candidate and SKIP diagnostics, and the diagnostic-only methodology. Unknown lane references fail before creating outputs. Existing `--lane` behavior remains unchanged. This is a diagnostic replay, not a Kelly wallet run or paper/live promotion claim; the forward observer remains disabled and independently gated.

## Evidence and review

- Tests and commands: RED `TypeError` for absent `composition_config`, GREEN single candidate test; RED unknown lane was rejected only *after* output creation, GREEN validates before creation. Focused `PYTHONPATH=<worktree> python3 -m unittest tests.test_collector_lane_replay -v` — 5 passed. Isolated full `PYTHONPATH=<worktree> python3 -m unittest discover -s tests -q` — 1,382 passed, 7 skipped, 70.644 s. `git diff --check` passed.
- Independent review: pending.
- Ancestry/merge evidence: current beta base above; no merge yet.

## Blockers and deferred work

- **Combined integration:** forward observer is concurrently being reviewed/staged on beta by `t_36c37b20`. Wait for its clean merge before changing its code in this isolated worktree; then merge current beta, test same config on both replay and observer, and get independent review. Do not race or overwrite its staged state.
- **Promotion:** a forward paper wallet equivalent to production account state, independently settled full-overlap cohort, and live execution approval are not established by a synthetic observer. Run actual forward compatibility/sizing/risk/settlement tests and request separate activation authorization before any service/profile/live change.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/lane-combination-promotion`
- **Latest immutable recovery checkpoint:** none yet
- **Exact resume point:** after `t_36c37b20` merges reviewed forward observer to beta, merge beta into this feature (not into an active staged integration tree), add test-first reader for this same config to the disabled observer and verify Kelly/risk sizing plus SKIP trace on exact shared candidate fixture. Run focused/full tests and independent review before any code-only beta merge.
- **Working-tree state:** isolated branch; verify before handoff.

## Decision gates

- **Integration:** focused/full tests, independent read-only review on exact feature and combined beta tree; remove this dossier from target.
- **Activation / cohort:** separate owner authorization; verified strict history and fresh shared snapshot.
- **Promotion:** fresh forward comparison and paper-wallet parity; live needs explicit approval.

## Decision record

- 2026-10-12 PDT — created task/worktree from clean beta while sibling stages forward observer; scoped to config/replay seam until integration. Replay ingestion RED/GREEN and full suite complete; forward code remains separately owned and staged.
