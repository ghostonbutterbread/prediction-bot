# Cross-month Source Router history integration dossier

- **Status:** independently accepted for local beta merge of exact staged code tree; no activation
- **Owner:** Hermes (`t_86a3f619`)
- **Branch:** `feat/source-router-cross-month-history`
- **Base commit:** `b7e066fe82593fa2842969ac65d1b62ccc3468f7`
- **Intended integration target:** local `beta` only
- **Last updated:** 2026-09-29 PDT
- **Owning feature branch/ref:** `feat/source-router-cross-month-history`
- **Latest immutable recovery checkpoint:** `895c30dcbafbd53884ffea7e38ae43a9cd010ec5`
- **Feature implementation commit(s):** `7f310c33b64d5b024c94e75faf20f51fce221ff2`, `eb8ff5ed2a64943194443e82ab44ef45c17c9251`, `895c30dcbafbd53884ffea7e38ae43a9cd010ec5`
- **Inspiration:** Discord message `1554374234086576268`; collector-first replay and existing monthly replay-window selection. The latter discovers JSONL months but does not verify committed replay indexes, so the new reader takes explicit verified archive triples instead of importing discovery as an integrity proof.

## Intent

Allow the paper Source Router's direct, read-only history path to consider several indexed collector archives from different months, filling sparse per-source/city/kind/shape event buckets from older datasets. Use the *same* paper-lane evaluator in the existing indexed, derived-only offline lane replay. Preserve single-archive behavior, strict source and independent settlement eligibility, candidate-time cutoff, and raw immutability. No collector, wallet, service, or runtime config activation.

## Implemented contract

- `load_direct_strict_source_history(archive_sources=[{index_path,manifest_path,strict_resolutions_path}, ...])` verifies and walks each committed index in reverse archive order; retains distinct settled events in bounded per-bucket memory across archives, prefers newer event dates regardless of archive-list order, and keeps the earliest eligible observation of an admitted event. The old scalar parameters still work.
- Each supplied archive carries its own strict resolution receipt. For every finally retained market, all supplied receipt files are reread and reconciled against their pinned hashes: conflicting authority or an ambiguous duplicate fails closed, even if the conflicting receipt precedes the archive containing that market's row. Missing receipts do not veto otherwise valid authority. The result includes per-archive manifest/resolution hashes and bucket coverage; no full copied generation is written.
- `collector_replay_archives` enables the same path in the paper shadow Source Router lane. The existing `scripts/collector_lane_replay.py` uses that lane evaluator over selected indexed collector candidates; it now preserves the recorded, sanitized weather source snapshot in candidate evidence for routing.
- Indexed offline decision rows retain allowlisted `direct_history` archive hashes and bucket coverage for reproducibility, without diagnostic counters that could describe future ineligible outcomes. Explicit archive triples are a requirement, not automatic index creation for every monthly JSONL. Monthly shards without a committed version-2 index and independent strict receipt cannot enter this path. Each candidate call currently scans all configured committed indexes and rereads each resolution receipt; broad real-corpus throughput has not been established.
- The reverse indexed reader now checks the digest of the exact manifest bytes it parses against the direct reader's recorded manifest digest. A normal concurrent collector index update between receipt capture and hydration fails closed rather than recording the old manifest hash for newly committed rows; an update after the reader opens the old manifest may use that valid committed prefix.

## Evidence and review

- RED→GREEN: multi-archive parameter failure; older-month bucket fill and overlap dedup; paper lane configuration and cutoff; archive-list-order independence; conflicting overlapping settlements; indexed offline runner supplying source observations. Independent review of `fbf2a193` BLOCKED on an earlier-archive conflict invisible to later selection, ambiguous later receipts, and missing persisted decision receipts. Added reproducing RED tests for all three, fixed them in `eb8ff5e`, and reran GREEN.
- Initial focused: `TMPDIR=/home/ryushe/.hermes/cache/scratch PYTHONPATH=$PWD:$PWD/tests python3 -B -m unittest discover -s tests -p 'test_source_router_*history*.py'` → 23 OK. Revised cross-month: seven OK; revised direct-history: six OK.
- Revised full: `TMPDIR=/home/ryushe/.hermes/cache/scratch PYTHONPATH=$PWD:$PWD/tests python3 -B -m unittest discover -s tests` → 1,357 OK, seven skipped. Receipt: `/home/ryushe/.hermes/cache/scratch/source-router-cross-month-review-fix-full.log`.
- Second independent review of clean tip `d052cc3` BLOCKED on a manifest TOCTOU: an updater could commit a second index row after the direct reader recorded an old manifest hash, before the indexed reader opened the new manifest. Added a synthetic normal-updater timing regression, watched it fail, pinned the exact reader manifest, and reran it GREEN.
- Latest focused: 26 Source Router history tests and six reverse-index tests OK. Latest full: 1,358 OK, seven skipped. Receipt: `/home/ryushe/.hermes/cache/scratch/source-router-cross-month-manifest-pin-full.log`.
- Independent feature review ACCEPTED clean tip `e25fc2af0eabd8dba125f7636476afda47e5c429` for noncommitting local-beta preflight only. On beta base `b7e066fe82593fa2842969ac65d1b62ccc3468f7`, staged code tree `cac66e3fa8ea158dbe2a586b957f7773b65ef81a` excluded this branch-only dossier. Beta-source-first focused suites passed (26 history plus six index tests); an initial full invocation timed out without a verdict, then a verbose isolated retry completed 1,358 OK, seven skipped. Receipt: `/home/ryushe/.hermes/cache/scratch/source-router-cross-month-beta-preflight-verbose.log`.
- Independent exact-staged-tree review ACCEPTED `cac66e3fa8ea158dbe2a586b957f7773b65ef81a` for intentional **local beta merge only**. `git diff --cached --check` passed and nonstaged diff was empty. Preflight was aborted so the dossier-only verdict can be committed on this feature branch; recreate the same staged tree and rerun checks before the merge commit. This is not a push or activation approval.
- Synthetic tests only; no multi-GB/raw live archive was read, hashed, or modified for this implementation.

## Blockers and deferred work

- **Repository integration:** both review gates accepted; reproduce the exact reviewed staged code tree after this dossier-only update, rerun beta-source-first focused/full checks, and merge locally only if the tree identity remains `cac66e3fa8ea158dbe2a586b957f7773b65ef81a`.
- **Empirical replay:** freeze a small authorized cohort with two or more independently indexed archives and dated strict resolutions; run a bounded older-month consumer smoke, reconcile coverage and actions. Trigger before claiming real historical routing or activation. Do not assume monthly JSONL shards are indexed or qualified.
- **Throughput:** currently revalidates/scans the whole compact index, hydrates all indexed rows, and reads each strict-resolution file twice per decision. Selected-history memory is bounded by buckets and budget, but one archive's resolution index is resident in memory. Benchmark/profile under an explicit resource budget before a broad replay or enabling a continuous lane; synthetic correctness is not throughput evidence.
- **Live parity:** offline runner is a recorded-decision/fixed-notional diagnostic, not execution-grade price/fill, Kelly/risk, or live-wallet equivalence. Forward paper is a separate gate.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/source-router-cross-month-history`
- **Latest immutable recovery checkpoint:** `895c30dcbafbd53884ffea7e38ae43a9cd010ec5`
- **Feature implementation commit(s):** `7f310c33b64d5b024c94e75faf20f51fce221ff2`, `eb8ff5ed2a64943194443e82ab44ef45c17c9251`, `895c30dcbafbd53884ffea7e38ae43a9cd010ec5`
- **Exact resume point:** recreate the accepted `cac66e3fa8ea158dbe2a586b957f7773b65ef81a` code tree on clean local beta from this dossier-only tip, exclude this dossier, rerun focused/full suites, commit intentional local beta merge if unchanged, verify postmerge. No push/deployment.
- **Working-tree state at handoff:** clean after this dossier-only commit; review the current tip and separately named implementation commits.

## Decision gates

- **Integration gate:** independent feature review, focused/full tests, clean isolated branch, exact staged beta tree if materially reconciled.
- **Activation / cohort gate:** separate operator decision with resource-bound real-index smoke and observed runtime source/config; no automatic activation.
- **Promotion gate:** fresh independently resolved forward paper after in-sample replay; no live trading authorization.

## Decision record

- 2026-09-28 PDT — beta-based isolated implementation and synthetic tests; no integration or runtime action.
- 2026-09-28 PDT — independent read-only review BLOCKED prior tip; reproducing RED tests, authority/provenance fixes, revised 1,357-test full suite; fresh independent review pending.
- 2026-09-29 PDT — second independent review BLOCKED on manifest TOCTOU; synthetic updater race RED→GREEN, 1,358-test full suite, fresh review pending.
- 2026-09-29 PDT — independent feature and exact staged-tree reviews ACCEPTED code for local beta merge; real-cohort, throughput, activation and forward-paper gates remain separate. No push or activation.
