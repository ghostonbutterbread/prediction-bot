# Research feed page acknowledgments

`research_feed_progress.py` is a cooperating-agent bridge between `fresh_research_feed.py` and the daily research runner. It never runs the collector or an experiment and does not mutate feed pages. Each page's receipt filename and content hash, pinned manifest, projected inputs and empty outcomes are rechecked before use.

For a page containing accepted inputs, run the trusted baseline and candidate on the exact page, save the real `auto_research.py compare` JSON outside the feed directory, then call:

```sh
python scripts/research_feed_progress.py ack --page "$PAGE/receipt.json" --ack-dir "$ACKS" --comparison "$COMPARE"
```

The comparison must report matching selected rows, the page's exact input hash and empty initial outcome hash, matching decision count, and both experiment IDs. A page with zero accepted rows is acknowledged without `--comparison` and reports `no_usable_inputs`; it is not a completed experiment. Acknowledgments are non-overwriting and an identical repeat is idempotent. A mismatched page or changed comparison fails rather than being silently marked consumed.

After every page of the pinned delta has a terminal acknowledgment, create a **new** checkpoint receipt outside the feed directory:

```sh
python scripts/research_feed_progress.py finish --last-page "$LAST_PAGE/receipt.json" \
  --previous "$PREVIOUS_PAGE/receipt.json" --previous-checkpoint "$PREVIOUS_CHECKPOINT" \
  --ack-dir "$ACKS" --output "$NEW_CHECKPOINT"
```

For the first bootstrap omit both `--previous` and `--previous-checkpoint`. The checkpoint records the final page path for the next `fresh_research_feed.py --previous`; preserve the prior checkpoint and all feed pages, comparisons and acknowledgments. A final page is not enough: `finish` traverses its resume chain, verifies all earlier page acknowledgments and their comparison receipts, and requires a contiguous path back to the previous checkpoint boundary. A truncated **initial** bootstrap is explicitly reported as `partial_bootstrap_newest_sample` and does not claim earlier coverage. `--max-input-bytes` bounds individual page input reads; it is not a bound on total pages or total workflow cost.

The model agent owns the sequence, claim/recovery and eventual checkpoint selection; this helper does **not** run or schedule the agent. Files and directories are published without replacing existing files, assuming a stable cooperating filesystem namespace. Acknowledgments attest the supplied compare bytes and consistency, not that an untrusted actor could not forge both; protect the coordinator state and comparator artifacts from candidate code. This is not an atomic transaction with downstream experiments, a source-authority proof, a complete independent opportunity roster, or evidence of profitability or paper readiness. Reconcile any interrupted or ambiguous operation before advancing the checkpoint.
