# Auto-learning loop

The loop lets a research agent try strategy changes as Git commits and get them scored against the
current champion on fresh collector data, with real outcomes bound after the decisions are sealed. It is
paper research only: it never trades, touches accounts or services, changes the collector, or promotes
anything. Promotion (`set-champion`) and paper cohorts are operator decisions.

```text
research agent (Hermes cron, model)            agentless cycle (Hermes cron --no-agent)
  read report.md + scoreboard.json               1. lock; check trusted checkout and free space
  commit 1–3 ideas in <work>/agent-repo          2. bind newly settled outcomes to earlier sealed pairs, score
  auto_learning_cycle.py register ...            3. snapshot the newest collector rows (outcome-free, audited)
                                                 4. replay champion + each active candidate at its commit
                                                    (separate stores; Docker, network none)
                                                 5. trusted comparison; queue pair for outcomes
                                                 6. conclude candidates; write scoreboard + report
```

## Pieces

- `scripts/auto_learning_cycle.py` — the cycle and its small CLI (`init`, `run`, `register`, `adopt-pair`,
  `set-champion`, `status`). It reuses the reviewed tools: `fresh_research_feed.py` (verified index
  snapshot), `auto_research.py` (replay and trusted compare), `prepare_research_outcomes.py` (post-seal
  binding) and `research_feedback.py` (scoring).
- Work root (must be under `<collector_root>/data/derived_reports`):
  - `config.json` — paths, executor, limits. `champion.json` + `champion-history.jsonl`.
  - `agent-repo/` — the research agent's clone. Branch `champion` tracks the champion commit.
  - `candidates/<id>/` — `candidate.json` (commit, hypothesis, changed paths) and `events.jsonl`.
  - `cycles/<cycle-id>/` — per-cycle journal (`cycle.json`), replay stores, comparisons, stderr.
  - `pending/<pair-id>/` — sealed pair awaiting outcomes; bindings and feedback per cycle.
  - `feeds/` — immutable snapshot pages; `state/feed.json` — last snapshot boundary.
  - `scoreboard.json`, `report.md`, `reports/<cycle-id>.md`.

## Rules the cycle enforces

- **Blind:** candidates only see the frozen snapshot and an empty outcome file. Outcomes are bound later
  by trusted code from the trusted checkout, never by candidate code.
- **Same inputs:** every version in a cycle replays the same snapshot; trusted `compare` must report
  `selected_rows_matched` and the full row count, or the pair is rejected.
- **Source clocks:** a snapshot row with any clock-named field (`as_of`, `*_at`, `*_as_of`, `*_timestamp`)
  later than its `observed_at`, or unreadable, is excluded from replay and counted in the report. The replay
  itself does not check source availability (shown by the source-clock probe), so this guard is the
  protection. Forecast-target dates and contract schedule terms (`closes_at`, `close_time`,
  `expiration_time`, …) are not knowledge clocks and are not checked.
- **Canonical hashes:** the strategy-input projection adds recorded market fields and order books to
  `market`, so rows reprojected with this code get new `canonical_input_sha256` values. Never mix
  freshly projected rows with older persisted replay inputs or bindings.
- **No overlap:** a new snapshot starts only after `min_new_rows` new indexed rows, and must not reuse
  rows from the previous snapshot. The snapshot is consumed before replays, so a failed cycle loses that
  snapshot rather than replaying it twice; a version gets at most one pair per snapshot. Snapshots are the
  newest `page_rows` rows, not a complete history.
- **Executors:** `docker` (network none, read-only code, separate writable store) is the production
  executor. `local` runs candidate code as the cycle user with no isolation; use it only for tests.
- **Champion is fixed** until the operator runs `set-champion`, which also retires active candidates
  (`superseded`) because their scores were measured against the old champion. A `better` verdict is a flag
  for review.
- **Live ledger:** the resolver rewrites the resolution ledger in place, so outcome binding only uses a read
  that saw an unchanged file (size, mtime, inode); otherwise outcomes are deferred to the next cycle.
- **Failures are loud:** any cycle failure prints `[CRON_FAILURE]` as the first line and exits nonzero;
  the journal records the error. A failing candidate is recorded and skipped; two consecutive failures
  conclude it as `broken`.

## Scoring

Per candidate the scoreboard sums, over its settled pairs, the champion's and candidate's resolved BUYs,
directionally correct/incorrect counts and a hypothetical quote P&L ($1 per BUY at the recorded ask,
binary payout, zero fees — a price proxy, not fills). A candidate concludes when it reaches
`conclude_min_resolved_changes` resolved changed decisions (`better` needs higher quote P&L and no worse
net correct), when two snapshots change nothing (`no_effect`), after `candidate_max_age_days`, or after
repeated failures. Repeated snapshots see the same markets at different times, so decisions are not
independent events; treat verdicts as research signals.

When there are no candidates, the cycle replays the champion against itself (`replicate`) so the
pipeline and outcome binding keep running and determinism is checked (it must change 0 decisions).

## Operating

```sh
PY=/home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python
CYCLE="$PY <trusted>/scripts/auto_learning_cycle.py"
$CYCLE init --work <work> --collector-root /mnt/data-collection/prediction-bot --trusted <clean checkout> \
  --python "$PY" --executor docker --image <image id> --index <replay_index.jsonl> \
  --manifest <replay_index.manifest.json> --protected-root <cohort root> --resolutions <latest_resolutions.jsonl>
$CYCLE run --work <work> --verbose          # one cycle (cron runs this)
$CYCLE status --work <work>
$CYCLE register --work <work> --commit <sha> --hypothesis '...'   # research agent
$CYCLE set-champion --work <work> --commit <sha> --note '...'     # operator only
$CYCLE cron down --work <work>     # pause every scheduled job of this loop (up / status likewise)
```

`cron up|down|status` drives the Hermes cron job ids listed in `<work>/config.json` under `cron_jobs`
(the agentless cycle and the research agent), skips jobs already in the wanted state, re-reads the
scheduler and fails unless every listed job reached it. Other cron jobs are never touched. A cycle that is
already running finishes; `down` only stops future runs.

The trusted checkout must stay clean; update it by checking out a newly reviewed commit. Disk use grows
with cycles (replay stores, snapshot pages); `min_free_bytes` stops a cycle before work starts. Retention
is a manual decision — nothing deletes evidence.

## Research agent contract

The agent works only in `<work>/agent-repo`: branch from `champion`, change any versioned code, commit,
and `register` with a one-paragraph hypothesis. It must not edit the work root, run replays, touch
services or accounts, or call `set-champion`. Registration fails when no slot is free.
