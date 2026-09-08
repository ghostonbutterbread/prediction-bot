# Indexed SourceWriter artifacts: implementation specification

## Status and ownership
- Status: **incomplete tested prerequisite checkpoint; NOT merge-ready**. Collector committed-prefix foundation implemented; pointer-backed SourceWriter, both consumers, authoritative binding and full baseline/router parity remain pending.
- Owner: Hermes parent; implementation delegated to one builder.
- Canonical spec: this branch-local dossier. Durable accepted contract belongs in `docs/architecture/persistent-storage-contract.md`.
- Branch: `feat/sourcewriter-indexed-artifacts`; worktree `/home/ryushe/worktrees/prediction-bot-sourcewriter-indexed-artifacts`.
- Immutable baseline: `537c6268d06c2313b8af63d341beff9f35b66e61`; target: local `beta`, never main.
- Recoverable prerequisite implementation commit: `8c042a2a585960cb36d7255ddd3c121be9a27055`; owning ref `feat/sourcewriter-indexed-artifacts`. Later dossier-only commits are handoff metadata, not additional implementation. Complete SourceWriter implementation remains pending.
- Supersedes the full-copy resource workaround from `fix/sourcewriter-default-enabled`. That branch's dirty default-config changes remain separately owned and untouched; they are not silently imported. Default activation remains a successor operational gate.

## User objective and invariants
Refactor storage, not learning policy. Keep original collector evidence in its existing storage-root archive and reference it through verified indexes/pointers. Do not copy the complete raw archive into each generation or commit raw data into Git. Keep all reports beneath selected storage root. Consumers validate evidence shape and eligibility; invalid/incorrect forecasts are not erased. Incorrect but eligible forecasts must contribute to accuracy denominators. No raw deletion, historical modification, active config/service/scheduler changes, live orders, or unrelated refactors. Parent may merge reviewed code into beta after all gates pass; merging is not activation or permission to push.

## Observable parity contract
Compare baseline and candidate on EXACTLY the same frozen collector bytes, independently authoritative resolution bytes, and candidate decision timeline. Baseline execution must import baseline code, not candidate code via editable installs. Seal/hash fixtures before either run; neither may see future outcome information at decision time.

The new implementation must reproduce baseline semantic results: accepted/rejected and pending/unresolved/unusable/VOID/strict/collapsed counts and reasons; eligible source correctness rows (including incorrect predictions); independence-group winner selection; scorecard source/context keys, sample counts and accuracy; actual SourceRouter action, source choice, confidence and skip reasons at identical decision cutoffs. Compare rows by full canonical decision/source identity, not totals alone. If economic receipts are present, compare amounts/outcome/timestamps too. Expected representations that may differ are artifact paths, generation/version IDs, locator/index fields, representation-specific provenance hashes, and timings. Define a small explicit allowlist of such differences, not a recursive stripping of arbitrary fields. Raw-row identity hashes, forecast values, outcome authority, chronology, and decision identities are NOT ignorable. Baseline bugs exposed by this refactor are recorded separately; do not quietly change learning semantics to get parity.

## Required implementation boundaries
1. Reuse `bot/collector_replay_index.py`; collector remains sole live index writer through existing `prediction_lab_collect` hook. Do not create a parallel index registry or live writer. Offline fixtures may build their own index.
2. Define atomically published immutable index checkpoints/segments or equivalent bounded committed extents. Record format version, stable archive/cohort identity, complete-row extent, byte offset/length, row number/identity, row digest and index digest. Initial build and updates defer incomplete trailing rows. An append after the committed extent must not invalidate prior references or enter a sealed run. Interrupted publication must leave a valid prior checkpoint and allow retry; use existing atomic/file-lock primitives rather than inventing lifecycle tooling.
3. Read referenced raw rows with bounded I/O, verify digest and identity, and use the existing sanitization/strict-proof logic. A plain pathname or hardlink to a still-growing input is not an immutable snapshot. Missing, truncated, replaced, tampered or identity-conflicting referenced evidence must fail closed with useful diagnostics. Rotation/movement is supported only through an explicit verified relocation contract; no heuristic search fallback.
4. Keep independently authoritative resolution binding separate and pinned to a stable receipt/committed extent. Preserve exact decision binding, authoritative settlement timestamps, no future leakage, VOID treatment and global deterministic independence collapse. An index is an optimization, not permission to omit pending/unusable accounting or globally relevant observations.
5. Emit small derived correctness/scorecard/index metadata, not raw-sized archive copies or full replay-payload exports per generation. Use iterable/external-memory processing where the current exporter/binder/materializer/collapse accumulates unbounded payloads. Reuse owning pure functions; avoid duplicating policy logic or introducing a second router.
6. Version the pointer-backed generation contract explicitly and update BOTH actual history and scorecard consumers. Preserve compatibility with valid existing V2 copy-backed generations; V1 remains inadmissible. Bind validated reference receipts to the scorecard and ensure atomic `current` selection cannot mix generations. Verify evidence once per bounded load/checkpoint where appropriate rather than rehashing the entire archive for every decision.
7. Repeated identical inputs/checkpoints reuse a complete generation. Append/update processing must equal a full rebuild over the same final committed extent. Do not claim computationally incremental operation if only storage references changed; report exact memory/I/O behavior.

## Test-first acceptance gates
- RED/GREEN focused tests for append-stable references; initial/update partial tail; interrupted publication/retry; corrupted index digest; wrong locator identities; missing/truncated/tampered/replaced raw evidence; independent late/conflicting resolutions; VOID; strict history chronology; global deduplication across chunks; V2 compatibility and V1 rejection; atomic current handoff.
- Deterministic E2E: raw collector fixture -> collector index -> authoritative resolver fixture -> pointer-backed SourceWriter -> actual SourceRouter consumer, including YES, NO, wrong forecasts, pending, unusable, duplicate polls, target contradiction and equal-time exclusions.
- Two-codebase parity harness against frozen baseline537c626; machine-readable semantic diff must be empty and enumerated counts verified programmatically. Run from disposable export/checkouts without runtime Git dependence in committed tests. Include replay after a committed raw append and incremental/full equivalence.
- Bounded resource fixture with large irrelevant raw payloads demonstrates output lacks full archive copies and does not call full-archive read_bytes/read. Report measured input/output bytes, peak memory and elapsed time for baseline/candidate; do not fabricate production-scale extrapolations. No multi-GB live run without separate resource preflight.
- Full isolated test suite with explicit worktree PYTHONPATH and temporary storage; `git diff --check`; independent read-only review and narrow fix/re-review loop.

## Checkpoint evidence and exact resume point

This turn implements only the collector-owned committed-prefix prerequisite in
`bot/collector_replay_index.py`, with regressions in
`tests/test_collector_index_checkpoints.py`. It is working code exercised through
the existing collector hook, not a placeholder pointer-backed publisher. The
unchanged publisher still makes V2 full-copy generations: **do not run it on
live/large input or activate it on the basis of this checkpoint**.

- Code base on entry: spec commit `eb8e66f1a7c6c34de911ffc3d08b00099df8e640`
  atop immutable baseline `537c6268d06c2313b8af63d341beff9f35b66e61`.
- Selected TEMP storage root: `/mnt/data-collection/sourcewriter-indexed-tpv8yxcv`.
  All synthetic fixtures, exports, logs and receipts remain there; no live raw
  input was read or modified. Preflight reported 48G available on the volume.
- Durable contract: `docs/architecture/persistent-storage-contract.md`, section
  “Collector replay-index committed prefixes (manifest version 2)”.
- Full isolated suite rerun on committed `8c042a2`: **1128 run, 0 failures, 0 errors, 7 skips**;
  `committed-full-suite.json` and `committed-full-suite.log` beneath the TEMP root. Elapsed
  32.1009924239479 seconds; process peak RSS 862820 KiB. This is suite memory,
  not SourceWriter workload memory. Python socket guard also inherited by
  subprocesses via `sitecustomize.py` on the explicit PYTHONPATH.
  All seven skips are absent ignored/local runtime configs in this isolated
  worktree, not skipped indexed-artifact assertions.
- Focused index suite on committed `8c042a2`: **24 run, 0 failures/errors** in
  `committed-focused-suite.json`. The earlier `full-suite.json`/1127 run is
  superseded, not evidence for the final committed code. RED/GREEN receipts are paired by suffix:
  `initial-tail`, `frozen-extent`, `index-digest`, `interrupted-publication`,
  `initial-publication`, `identity`, `locator-extents`, `concurrent-append`,
  `idempotency-path`, `partial-only`. Additional invariant tests verify
  malformed rows, append/full rebuild equality and legacy index read behavior.
- `checkpoint-gates.json`: **index-only semantic diff [] across 64 complete
  hydrated row identities/full-row hashes**. SourceWriter semantic diff is
  null, SourceWriter parity and actual router E2E are `not_run`, merge gate
  false. These are intentionally separate fields, not an empty full-pipeline
  diff falsely presented as success.
- `baseline-index-resource.json` and `candidate-index-resource.json` compare
  index build + full indexed hydration ONLY on a single sealed 16785836-byte
  fixture. Baseline: 26403 derived bytes, peak RSS 33512 KiB, 0.19510921090841293
  seconds. Candidate: 26735 derived bytes, peak RSS 30832 KiB,
  0.3042108869412914 seconds. Both read 16785836 raw bytes during indexing and
  16785836 during hydration; max hydration request 262279 bytes. Both forbid
  `Path.read_bytes` and negative-size raw `read`. This does **not** measure or
  satisfy the SourceWriter resource gate. Probe cap: 1 GiB address space,
  30 CPU seconds. No production extrapolation.
- Review: independent read-only review dispatched for this prerequisite;
  full implementation review/integration remains the parent's responsibility.
- Small final JSON report copies only (no fixture/raw copies):
  `/mnt/data-collection/prediction-bot/data/derived_reports/sourcewriter_indexed_artifacts/checkpoint-8c042a2/`.
  Contains `checkpoint-gates.json`, `committed-full-suite.json`,
  `committed-focused-suite.json`, and baseline/candidate `*-index-resource.json`,
  each naming the implementation checkpoint SHA; the older `full-suite.json`
  is explicitly marked superseded/precommit rather than pinned to that SHA.
- No beta/main changes, merges, pushes, runtime config edits, services,
  schedulers, orders or activation. Only task-owned local files changed.

Exact commands (run from the owning worktree):

```bash
W=/home/ryushe/worktrees/prediction-bot-sourcewriter-indexed-artifacts
R=/mnt/data-collection/sourcewriter-indexed-tpv8yxcv
P=/mnt/data-collection/prediction-bot/.venv/bin/python
PYTHONPATH="$R/guard:$W" PREDICTION_BOT_COLLECTOR_ROOT="$R" TMPDIR="$R" \
  "$P" "$R/run_tests.py" 'test_*.py' committed-full-suite
PYTHONPATH="$R/guard:$W" PREDICTION_BOT_COLLECTOR_ROOT="$R" TMPDIR="$R" \
  "$P" "$R/run_tests.py" 'test_collector*index*.py' committed-focused-suite
git diff --check
```

Index-only parity/resource reproduction (use fresh output labels on repeat;
fixture preparation is exclusive-create and must not overwrite the sealed raw):

```bash
mkdir -p "$R/baseline-export"
git archive 537c6268d06c2313b8af63d341beff9f35b66e61 bot | tar -x -C "$R/baseline-export"
"$P" "$R/index_probe.py" --prepare --code-root "$W" --storage-root "$R" --label prepare
PYTHONPATH="$R/guard" "$P" "$R/index_probe.py" --code-root "$R/baseline-export" --storage-root "$R" --label baseline
PYTHONPATH="$R/guard" "$P" "$R/index_probe.py" --code-root "$W" --storage-root "$R" --label candidate
"$P" "$R/compare_index.py"
```

`run_tests.py`, `guard/sitecustomize.py`, `index_probe.py`, and `compare_index.py`
are retained TEMP-root receipt helpers, not committed final acceptance harnesses.
The committed index regressions run from a disposable source export without Git.
The required complete SourceWriter two-codebase harness remains to be written.

### Remaining work, in dependency order (merge remains blocked)

1. Add collector-owned locators/diagnostics for rejected raw rows so exporter
   acceptance/rejection **reasons**, pending and unusable accounting stay exact.
   Existing accepted-row iterator and invalid counts are insufficient. Add an
   explicit verified relocation and legacy-index checkpoint migration contract;
   current legacy updates fail closed instead of silently acquiring trust.
2. Pin an independent resolution receipt/extent. Preserve the owning binder's
   exact decision identity, conflict/late/VOID handling, and sanitization logic;
   implement bounded iterable/external-memory export, binding, materialization
   and global collapse without a second learning policy or raw payload exports.
3. Add explicitly versioned pointer-backed promotion and BOTH real consumers
   (`weather/source_history_manifest.py` and verified strict-scorecard loading
   in `auto_source_router_promotion.py`); keep V2 generation compatibility and
   V1 rejection. Pin receipt hashes, generation reuse and atomic `current`.
4. Implement the committed full baseline537c626/candidate parity harness on
   identical sealed collector + independent resolver + decision timelines.
   Exercise actual router YES/NO/wrong/pending/unusable/duplicate/contradictory
   and equal-time cases, all row identities/reasons, append/full equivalence.
   Require an empty full-pipeline semantic diff, not this index-only receipt.
5. Measure complete SourceWriter baseline/candidate resource behavior with
   large irrelevant payloads; then full isolated suite, independent review and
   narrow fixes/re-review. Parent alone may integrate after every gate passes;
   this builder must not merge/push/modify beta.

## Merge and activation
Before merge, commit implementation plus updated dossier containing exact commands/receipts, immutable baseline and implementation SHAs, review decision, parity verdict and remaining operational gates. Parent verifies artifacts and beta ancestry/status, preserves unrelated migration handoff, merges locally only if all correctness/resource gates pass, and reruns beta tests. Remove temporary dossier on integration; retain durable format contract and reproducible tests.

Default-enabled maintenance configuration and scheduler rollout are a FOLLOW-ON gate: exactly one resolver/SourceWriter owner, resource limits, explicit storage/retention strategy preserving evidence, verified current generation and consumer smoke, no accidental trading activation. Do not call the writer active merely because defaults were changed. If a gate cannot be met, leave the branch recoverable and report the concrete blocker; do not merge a plausible partial implementation.
