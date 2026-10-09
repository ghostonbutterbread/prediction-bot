# Resolution feed append discovery integration dossier

- Status: review-ready; independent review corrections implemented
- Owner: collector_incremental_fix; fresh release gate owns independent review/integration
- Branch / owning feature ref: fix/resolution-feed-append-discovery
- Base commit: 4ca50c30c39707fd13e4b657d6299667cd761580 (Ghost local main)
- Intended integration targets: Ghost local main, then deployed beta
- Beta base: 14982f8 (descends from main base; retains bounded polling rotation fixes)
- Last updated: 2026-10-08
- Latest immutable recovery checkpoint: 45ac095; initial implementation: 7221f66; current tip also contains review corrections

## Intent and implemented contract

Fix resolution_feed incremental_unresolved repeatedly parsing the entire raw market archive before limiting fetches. Live archive is 75,559,437,457 bytes. No live archive is used for verification. Main owns discovery; beta separately owns newer polling fairness fixes that must survive propagation.

Discovery persists IDs and per-source complete-line offsets in one atomic JSON document, then regenerates the compact reference artifact. Unchanged refreshes never open the raw archive. New suffix reads share a 32 MiB default budget per refresh (`resolution_feed.max_discovery_bytes`). Rotation/truncation resets source discovery while retaining historical IDs for unresolved retries. Partial lines wait for completion; a record exceeding the configured budget raises an actionable error.

Bootstrap may use the collector's schema-v2 compact index only with matching source/device/inode and zero skipped rows. It reads only the committed index prefix, sharing a separate 512 MiB default budget (`resolution_feed.max_index_bootstrap_bytes`), enough for the observed 449,314,688-byte index. Index discovery persists progress if capped, keeps existing compact queue IDs eligible, and never claims raw-prefix coverage until the seed is complete. A collector's configured `prediction_lab.replay_index.root_dir` selects its manifest; otherwise the normal sibling replay_index directory is checked. Compact rows discover IDs only; authoritative network responses continue to own settlement.

After bootstrap, the compact cursor follows newly committed index suffixes on every refresh. Only after consuming each committed compact prefix may raw coverage advance to its indexed-source extent. This keeps normal discovery proportional to compact metadata rather than raw append bandwidth; any unindexed raw tail remains subject to its separate budget. If the index later skips rows or becomes unavailable, discovery resumes from its saved raw coverage. State.json includes discovery byte counts and remaining raw/index backlog. Persisted resolver cadence is unchanged; discovery only runs when the existing refresh gate allows it. Full mode retains its historical full-scan behavior.

## Evidence and review

- Initial red: discovery test module failed to import the absent incremental helper before implementation.
- Green: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p test_resolution_feed_discovery.py`: 17 passed. Fixtures cover unchanged/restart no raw open, appended suffix only, bounded reads, partial lines, rotation/truncation, separate sources, compact committed bootstrap, migration progress, skipped-index-row fallback, atomic publication failure recovery, actual runtime retries/resolved filtering, custom index root configuration, ongoing compact index appends without raw reads, fallback when later index coverage omits rows, raw-tail truncation beyond old index prefix, interruption followed by index deletion/rotation/truncation, and no raw read during capped ongoing index discovery.
- `PYTHONPATH=. python3 -m unittest discover -s tests -p test_resolution_feed.py`: 12 run, 6 skipped, passed on owning main.
- `git diff --check`, compilation: passed.
- Initial full suite with system Python 3.12: 989 tests, 5 environment errors (missing kalshi_python_sync requiring Python >=3.13; absent ignored data/summaries directory), 8 skipped. This is not a release receipt.
- Python 3.13 provisioned through setup.sh: 1058 tests initially had 15 unrelated import errors because requirements.txt omits rich, used by bot/dashboard.py. Installed rich only in local test environment; no dependency/product change made. Full suite then passed: 1061 tests, 8 skipped, 13.972s (before final same-inode truncation safeguard; final focused 17 tests passed afterward). Fresh reviewer reruns final full tree independently.
- Independent review: fresh resolution_feed_release_gate reproduced three scoped defects and requested corrections. Implemented all with red/green regressions: include raw-tail offset when checking truncation, fall back safely after missing/rotated/truncated compact seed, defer raw scanning whenever compact discovery is pending. Same-inode truncation also avoids reusing its stale compact prefix.
- Main/beta ownership: `git merge-base main beta` equals base main SHA. Remote fetch on Ghost succeeded before implementation.

## Boundaries and remaining gates

- Changed-path boundary: bot/resolution_feed.py, tests/test_resolution_feed_discovery.py, this temporary dossier.
- User emergency scope is corrected defect owner plus deployed collector beta. Historical experimental descendants are deferred rather than merged with unrelated experiments; audit/record their containment if needed. No unrelated runtime source or service is activated.
- Required integration gate: fresh independent review; full suite in proper environment; inspect reconciled feature; merge corrected main into beta preserving existing cursor rotation/downstream cap; focused/full beta checks; retire dossier on integrated trees; clean deployed beta checkout read-back.
- Local main and origin/main diverge in pre-existing history; do not force-push main or discard divergent history. Beta publication must follow current repository direction and remote read-back.
- Activation owner: root only. Root may restart the existing observer service once the reviewed beta commit is installed. This agent must not restart collector or touch Hermes database.
- Deferred activation evidence: root verifies actual beta source/import and bounded first refresh with existing compact index; no live 75 GB scan is permissible.

## Resume handoff

All implementation and focused receipts are in the branch checkpoints. Local .venv is Python 3.13.12, provisioned from requirements.txt via setup.sh with rich added only for testing and ignored data/summaries created for existing tests. Fresh gate must independently rerun final full tree, preserve beta fairness on propagation, record accepted review on feature branch, then retire this dossier from integrated targets.

## Decision record

- 2026-10-08: created emergency defect patch from lowest owning local main; targeted beta activation only authorized by root/user.
- 2026-10-08: retained compact cursor for ongoing commits to avoid raw append bandwidth exceeding the routine raw budget; synthetic steady-state regression passed. Recovery checkpoint 7221f66 contains initial implementation; later tip owns this adjustment.
- 2026-10-08: applied independent release-gate corrections with three new regression cases (including three missing/rotation/truncation subcases). Runtime config/service remains untouched.
