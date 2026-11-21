# Automatic learning fresh-feed adapter: blocked

- **Status:** blocked; no adapter implemented or activated
- **Owner / branch:** delegated fresh-feed task, `feat/auto-learning-feed-t_2f4bb125`
- **Base:** `99216cdd33117200f2e049fcf82b0b2aa88d05fd`
- **Intended integration target:** parent agent's auto-learning feature (exact ref to be confirmed by parent; no merge authorized)
- **Last updated:** 2026-11-20
- **Implementation commits:** none
- **Recovery checkpoint:** this branch's dossier commit; base above is not an implementation

## Intent and boundaries

Create a bounded incremental fresh-feed adapter for the parent daily research worker, with canonical projected inputs, explicitly empty outcomes, durable idempotent checkpoints, bounded source reads, and selected-only integrity disclosures. Never write collector-owned data, run against live material without parent approval, schedule services, create a second collector/source writer, or place trades. Source time remains unverified and diagnostic-ineligible. Repeated market snapshots are not independent outcomes.

## Blocker: the committed-index contract cannot authenticate a bounded slice

`docs/architecture/persistent-storage-contract.md` lines 152–185 requires that readers verify the SHA-256 of the *entire committed compact index prefix*, then validate all locator ordering before filtering or hydration. The v2 manifest has one whole-prefix `index_sha256`; the source append chain is over raw row digests, not a membership proof for index chunks. It has no independent chunk digests, authenticated tree, index-generation identity, or authenticated links between manifest generations. `_verify_index_extent` in `bot/collector_replay_index.py` implements the required full digest and structural passes; a saved manifest and a watermark do not authenticate a newly selected suffix, nor prove the previous committed prefix has not been changed in place. Per-selected-row payload hashes and identity checks bind a locator to the raw bytes it names, but do not establish that locator is a member of the pinned committed index or that unselected entries and ordering are unchanged. A saved inode or tail anchor cannot detect an earlier in-place change. A manifest change can be a normal producer append, but the v2 manifest alone provides no bounded proof that the new committed index extends the earlier exact index.

The context identifies an approximately 716 MB current committed index; full-prefix validation conflicts with the requested bounded daily operation. We did not process the live manifest, index, or raw archive to remeasure it. A synthetic-only adapter capped below that extent would not be useful for the requested feed; silently relaxing this repository's reader contract or claiming fail-closed detection from selected-row hashes would be incorrect. The requested stop condition therefore applies. No watermark, frozen input, outcome file, catalog, CLI, or service was created.

## Evidence

- Inspected the version-2 manifest writer and existing reader in `bot/collector_replay_index.py`, and the collector contract in `docs/architecture/persistent-storage-contract.md`.
- Ran four existing task-owned temporary-fixture tests using `PYTHONPATH="$PWD" python3 -m unittest -v tests.test_collector_index_checkpoints.CollectorIndexCheckpointTests.test_corrupt_index_fails_before_filtered_hydration tests.test_collector_index_checkpoints.CollectorIndexCheckpointTests.test_frozen_manifest_excludes_later_committed_index_and_raw_append tests.test_collector_index_checkpoints.CollectorIndexCheckpointTests.test_interrupted_publication_preserves_checkpoint_and_retry_is_exact tests.test_collector_index_checkpoints.CollectorIndexCheckpointTests.test_index_truncation_and_failed_update_leave_manifest_unchanged`: **4 passed**. These demonstrate the existing contract, not a new adapter or proof of bounded incremental safety.
- `origin/main` was fetched; the designated base is not an ancestor of the fetched `origin/main`. The parent feature branch is the intended handoff, not a direct main integration.
- Independent review, actual adapter CLI, cap/crash adapter tests, and any live-data execution: not performed because there is no adapter.

## Resolution and gates

The parent must decide whether to authorize a compatible proof mechanism, e.g. collector-owned authenticated chunk commitments with an authenticated append continuity contract and a safe bootstrap for the existing prefix, or explicitly revise the index reader's integrity contract and accept the loss of detection/completeness guarantees. Either requires design and review; an untrusted locally generated chunk catalog alone cannot authenticate the large existing prefix without an initial full verification or independent producer commitments. Any producer change or large bootstrap scan is outside this delegated scope. Once an acceptable contract is established, resume with test-first synthetic end-to-end adapter implementation, including cap, partial-row, boundary, concurrent-append, replacement/tamper, checkpoint crash, and repeat-market tests. Only after review and explicit approval may a bounded live read be considered.

**Integration / activation / promotion:** blocked. No merge, deployment, scheduling, live material processing, source-time testing, or trading has been performed.
