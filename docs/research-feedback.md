# Retrospective research feedback

`scripts/research_feedback.py` scores already-sealed `auto_research.py` control decisions against a separate, caller-pinned finalized-outcome JSONL. It compares the baseline and candidate versions on the same sealed input identities. The trusted scoring checkout runs **after** experiment code exits, so this analysis does not run candidate code or require exposing later outcome bytes to candidate execution. Both original experiment records must name the empty-outcome SHA-256; that is an important input check, not proof that arbitrary experimental code had no other way to learn outcomes.

The scorer reads the existing experiment database and verified decision-artifact bytes, requires matching input hashes and replay settings, and calls the existing exact-identity directional resolver. It reports resolved, unresolved, invalid side, BUY/SKIP and correct/incorrect row counts, plus changes between versions. Repeated snapshots or markets can represent the same physical event; these counts are **not** independent sample counts. The report explicitly remains `diagnostic_eligible=false` with source timing and outcome producer/clock authority unverified. Caller-pinned bytes prove identity, not authority. It contains no profit metric, does not model fills, and is not paper or promotion evidence. Previously inspected data are in-sample even if scored after sealing.

Example from a controlled analysis environment, with paths mapped to the same absolute names as the experiment database:

```sh
python scripts/research_feedback.py \
  --store /work/collector/data/derived_reports/research \
  --baseline BASELINE_EXPERIMENT_ID --candidate CANDIDATE_EXPERIMENT_ID \
  --outcomes /read-only/finalized_replay_outcomes.jsonl \
  --outcome-sha256 HASH_FROM_RETAINED_BINDING_RECEIPT \
  --max-outcome-bytes 65536 --max-decision-bytes 4000000 \
  --output /work/collector/data/derived_reports/feedback/new-report.json
```

Set `PREDICTION_BOT_COLLECTOR_ROOT=/work/collector`; create the feedback output parent first. Both bounds are whole-file read limits and should be chosen for the actual input, not assumed memory or elapsed-time limits. Run the trusted scorer with a read-only store/outcome mount, no network, and an isolated writable report mount. Keep the frozen outcome bytes and their real producer receipt. If the independent outcome ledger is unavailable, an explicitly empty outcome file produces zero resolved decisions and must not be described as learning from results. Missing, duplicate, invalid or unbound outcomes remain unresolved through the existing resolver.

The output filename must not already exist and must be under `data/derived_reports` outside the research store. A private temporary file is linked into place only after its full JSON bytes are written and synced; this assumes a stable cooperating filesystem namespace. The report preserves experiment IDs, Git commit strings, recorded input and initial-outcome hashes, sealed-artifact hashes and supplied outcome hash. The SQLite database and run artifacts themselves must be protected from experimental writes for these hashes to carry meaning; a hash is not an authentication mechanism. Directly edited candidate code is not trusted as the scorer. This tool does not discover or create outcomes, schedule itself, update accounts, change the collector, or promote candidates.
