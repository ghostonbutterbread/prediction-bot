# Cross-month Source Router history integration dossier

- **Status:** feature; independent review pending
- **Owner:** Hermes (`t_86a3f619`)
- **Branch:** `feat/source-router-cross-month-history`
- **Base commit:** `b7e066fe82593fa2842969ac65d1b62ccc3468f7`
- **Intended integration target:** local `beta` only
- **Last updated:** 2026-09-28 PDT
- **Owning feature branch/ref:** `feat/source-router-cross-month-history`
- **Latest immutable recovery checkpoint:** `7f310c33b64d5b024c94e75faf20f51fce221ff2`
- **Feature implementation commit(s):** `7f310c33b64d5b024c94e75faf20f51fce221ff2`
- **Inspiration:** Discord message `1554374234086576268`; collector-first replay and existing monthly replay-window selection. The latter discovers JSONL months but does not verify committed replay indexes, so the new reader takes explicit verified archive triples instead of importing discovery as an integrity proof.

## Intent

Allow the paper Source Router's direct, read-only history path to consider several indexed collector archives from different months, filling sparse per-source/city/kind/shape event buckets from older datasets. Use the *same* paper-lane evaluator in the existing indexed, derived-only offline lane replay. Preserve single-archive behavior, strict source and independent settlement eligibility, candidate-time cutoff, and raw immutability. No collector, wallet, service, or runtime config activation.

## Implemented contract

- `load_direct_strict_source_history(archive_sources=[{index_path,manifest_path,strict_resolutions_path}, ...])` verifies and walks each committed index in reverse archive order; retains distinct settled events in bounded per-bucket memory across archives, prefers newer event dates regardless of archive-list order, and keeps the earliest eligible observation of an admitted event. The old scalar parameters still work.
- Each supplied archive carries its own strict resolution receipt. Conflicting viable settlement authority for a retained overlapping event fails closed. The result includes per-archive manifest/resolution hashes and bucket coverage; no full copied generation is written.
- `collector_replay_archives` enables the same path in the paper shadow Source Router lane. The existing `scripts/collector_lane_replay.py` uses that lane evaluator over selected indexed collector candidates; it now preserves the recorded, sanitized weather source snapshot in candidate evidence for routing.
- Explicit archive triples are a requirement, not automatic index creation for every monthly JSONL. Monthly shards without a committed version-2 index and independent strict receipt cannot enter this path. Each candidate call currently scans all configured committed indexes; broad real-corpus throughput has not been established.

## Evidence and review

- RED→GREEN: multi-archive parameter failure; older-month bucket fill and overlap dedup; paper lane configuration and cutoff; archive-list-order independence; conflicting overlapping settlements; indexed offline runner supplying source observations.
- Focused: `TMPDIR=/home/ryushe/.hermes/cache/scratch PYTHONPATH=$PWD:$PWD/tests python3 -B -m unittest discover -s tests -p 'test_source_router_*history*.py'` → 23 OK.
- Full: same environment with `-p 'test_*.py'` implicit via `python3 -B -m unittest discover -s tests` → 1,355 OK, seven skipped. Receipt: `/home/ryushe/.hermes/cache/scratch/source-router-cross-month-full.log`.
- `git diff --check` passed precommit. Independent read-only architecture audit is in flight; final implementation review and exact beta-tree integration gate remain pending.
- Synthetic tests only; no multi-GB/raw live archive was read, hashed, or modified for this implementation.

## Blockers and deferred work

- **Independent review:** examine multi-archive ordering, authority conflict handling, receipts, paper/replay parity and bounded resource behavior. Trigger before integration; rerun focused/full on any fix.
- **Empirical replay:** freeze a small authorized cohort with two or more independently indexed archives and dated strict resolutions; run a bounded older-month consumer smoke, reconcile coverage and actions. Trigger before claiming real historical routing or activation. Do not assume monthly JSONL shards are indexed or qualified.
- **Throughput:** currently revalidates/scans the whole compact index and hydrates all indexed rows per decision. Benchmark/profile under an explicit resource budget before a broad replay or enabling a continuous lane; synthetic correctness is not throughput evidence.
- **Live parity:** offline runner is a recorded-decision/fixed-notional diagnostic, not execution-grade price/fill, Kelly/risk, or live-wallet equivalence. Forward paper is a separate gate.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/source-router-cross-month-history`
- **Latest immutable recovery checkpoint:** `7f310c33b64d5b024c94e75faf20f51fce221ff2`
- **Feature implementation commit(s):** `7f310c33b64d5b024c94e75faf20f51fce221ff2`
- **Exact resume point:** reconcile independent audit, add tests for any confirmed gaps, obtain fresh independent feature review, then noncommitting beta preflight and exact staged-tree verdict if eligible. No push/deployment.
- **Working-tree state at handoff:** clean after this dossier-only commit; review the current tip and separately named implementation commit.

## Decision gates

- **Integration gate:** independent feature review, focused/full tests, clean isolated branch, exact staged beta tree if materially reconciled.
- **Activation / cohort gate:** separate operator decision with resource-bound real-index smoke and observed runtime source/config; no automatic activation.
- **Promotion gate:** fresh independently resolved forward paper after in-sample replay; no live trading authorization.

## Decision record

- 2026-09-28 PDT — beta-based isolated implementation and synthetic tests; no integration or runtime action.
