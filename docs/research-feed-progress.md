# Research feed page acknowledgments

`research_feed_progress.py` is a cooperating-agent bridge between `fresh_research_feed.py` and the daily research runner. It never runs the collector or an experiment and does not mutate feed pages. Each page's receipt filename and content hash, pinned manifest, projected inputs and empty outcomes are rechecked before use.

For a page containing accepted inputs, run baseline and candidate on the exact page in **two separate, non-overlapping stores**, save the trusted checkout's real `auto_research.py compare --store "$BASELINE" --candidate-store "$CANDIDATE" ID1 ID2` JSON outside the feed and stores, then call:

```sh
python scripts/research_feed_progress.py ack --page "$PAGE/receipt.json" --ack-dir "$ACKS" \
  --comparison "$COMPARE" --baseline-store "$BASELINE" --candidate-store "$CANDIDATE"
```

Both experiment IDs must be distinct. The helper invokes **its own checkout's** trusted `scripts/auto_research.py` with those explicit stores and IDs; it does not run code from either store. The freshly computed full comparison must match the supplied JSON's typed identity, selected rows, decision count, input hash and empty outcome hash. It repeats this check at finish, including actual sealed artifact hashes. Stores must be quiescent; pending SQLite WAL or rollback journals are rejected for reconciliation. Cross-store comparison opens SQLite immutable/read-only and rejects store-internal symlinks. A page with zero accepted rows is acknowledged without comparison or stores as `no_usable_inputs`.

After every page of the pinned delta has a terminal acknowledgment, create a checkpoint outside the feed, acknowledgment directory and stores:

```sh
python scripts/research_feed_progress.py finish --last-page "$LAST_PAGE/receipt.json" \
  --previous "$PREVIOUS_PAGE/receipt.json" --previous-checkpoint "$PREVIOUS_CHECKPOINT" \
  --ack-dir "$ACKS" --output "$NEW_CHECKPOINT"
```

For the first bootstrap omit both `--previous` and `--previous-checkpoint`. Preserve the prior checkpoint and all feed pages, stores, comparisons and acknowledgments. `finish` traverses the contiguous resume chain and revalidates each acknowledgment and comparison before publication. For a subsequent delta it trusts the supplied prior checkpoint's schema and last-page hash; it does not revalidate that prior checkpoint's own acknowledgments, so the checkpoint must be protected trusted coordinator state. A truncated initial bootstrap is `partial_bootstrap_newest_sample`, not earlier coverage. `--max-input-bytes` bounds each input read, not total pages or comparator artifact reads; operators must bound the overall workflow.

Publication exclusively links a file, removes its temporary name, and fsyncs the parent directory. An identical retry revalidates all dependencies and fsyncs the directory again; existing different or malformed output is preserved and rejected. A directory-fsync error means the destination may be visible but durability is unconfirmed: retry the identical operation after fixing the failure, or reconcile manually without overwriting. Retry is not successful unless the sync succeeds.

The caller must choose a trusted checkout and protect coordinator state and the comparator from candidate code, designate a dedicated output namespace separate from **all** experiment stores and feed roots, and ensure quiescent stores and a stable cooperating filesystem namespace throughout each operation. Overlap checks cover the current feed and compared stores, not undisclosed unrelated stores or feeds. Hashes do not authenticate a producer who can rewrite both store metadata and artifacts. This is not a scheduler, an atomic transaction with experiments, publisher-authority or continuity proof, a complete opportunity roster, or evidence of profit or paper readiness.
