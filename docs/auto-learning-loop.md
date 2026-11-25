# Auto-learning loop

The loop lets a research agent try strategy changes and get them scored against the current champion on
fresh collector data, with real outcomes bound after the decisions are sealed. A change is a **version**:
a code commit plus a control-lane parameters file ("lane file"). Threshold-style ideas are parameters on
the champion's code (no branch); new logic is a commit. When a candidate passes the promotion proof the
loop makes it the champion automatically and keeps the previous champion running as a rollback guard. It
is paper research only: it never trades, touches accounts or services, or changes the collector; paper
cohorts and live trading remain separate operator decisions.

```text
research agent (Hermes cron, model)            agentless cycle (Hermes cron --no-agent)
  read report.md + scoreboard.json               1. lock; check trusted checkout and free space
  register 1–4 ideas: parameters on the          2. bind newly settled outcomes to earlier sealed pairs, score
  champion code, or commits in <work>/agent-repo 3. snapshot the newest collector rows (outcome-free, audited)
  auto_learning_cycle.py register ...            4. replay champion + each active candidate (commit + params)
                                                    (separate stores; Docker, network none)
                                                 5. trusted comparison; queue pair for outcomes
                                                 6. promote a proven candidate (old champion -> rollback guard),
                                                    conclude others; write scoreboard + report
```

## Pieces

- `scripts/auto_learning_cycle.py` — the cycle and its small CLI (`init`, `run`, `register`, `adopt-pair`,
  `set-champion`, `status`). It reuses the reviewed tools: `fresh_research_feed.py` (verified index
  snapshot), `auto_research.py` (replay and trusted compare), `prepare_research_outcomes.py` (post-seal
  binding) and `research_feedback.py` (scoring).
- Work root (must be under `<collector_root>/data/derived_reports`):
  - `config.json` — paths, executor, limits, promotion settings. `champion.json` (commit + parameters) and
    `champion-history.jsonl` (every change, with the proof that justified an automatic one).
  - `agent-repo/` — the research agent's clone. Branch `champion` tracks the champion commit.
  - `candidates/<id>/` — `candidate.json` (kind `parameters`/`code`/`rollback_guard`, commit, parameters,
    hypothesis, changed paths) and `events.jsonl`.
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
- **Champion changes** only through a passing proof (automatic, `auto_promote: true`) or the operator's
  `set-champion`. Active candidates keep running across a change, but their proof restarts: it only counts
  pairs measured against the current champion. With `auto_promote: false` a passing proof concludes the
  candidate as `better` with `needs_operator_review`.
- **Live ledger:** the resolver rewrites the resolution ledger in place, so outcome binding only uses a read
  that saw an unchanged file (size, mtime, inode); otherwise outcomes are deferred to the next cycle.
- **Failures are loud:** any cycle failure prints `[CRON_FAILURE]` as the first line and exits nonzero;
  the journal records the error. A failing candidate is recorded and skipped; two consecutive failures
  conclude it as `broken`.

## Scoring and the promotion proof

Per candidate the scoreboard sums, over its settled pairs, the champion's and candidate's resolved BUYs,
directionally correct/incorrect counts and a hypothetical quote P&L ($1 per BUY at the recorded ask, else
the recorded market price; binary payout, zero fees — a price proxy, not fills).

The **proof** asks one question: on settled decisions where the two versions decided differently, does
the candidate make more money? Each such decision contributes `candidate P&L − champion P&L` (a SKIP is
0). Decisions are summed per independence unit — the event ticker, e.g. one city-day, because every
bracket of that day settles on the same temperature and repeated snapshots see the same market many
times. A bootstrap over units estimates P(total delta > 0). The proof passes when:

- at least `promotion_min_units` (20) units have a settled disagreement;
- P(delta > 0) ≥ `promotion_min_confidence` (0.80) and the total delta is positive;
- the candidate's resolved buys are ≥ `promotion_min_buy_ratio` (0.25) of the champion's, so "trade
  nothing" cannot win by default.

Version code writes its own sealed decisions, so trusted `research_feedback.py --inputs` re-derives each
paired row's unit from the frozen input row and requires every BUY's sealed price, price basis and $1
stake to equal what trusted code selects from that row; otherwise that side's P&L is withheld and the
row cannot help the proof. A version that changes how prices are selected therefore cannot be promoted.
Only verified feedback and pairs measured against the current champion count. One proven candidate (highest confidence, then
delta) is promoted per cycle; the old champion is registered as a `rollback_guard` candidate and is
restored by the same proof if it later beats the new champion (a restoring guard does not spawn another
guard). A candidate also concludes as `not_better` once it has enough units and P(better) ≤ 1 − the
confidence, `no_effect` when two snapshots change nothing, `insufficient_evidence` after
`candidate_max_age_days`, or `broken` after repeated failures. The proof inherits the price proxy's
limits (quotes, not fills).

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
$CYCLE register --work <work> --parameters '{"YES": {"min_probability": 0.7}}' --hypothesis '...'  # lane file
$CYCLE register --work <work> --commit <sha> [--parameters '...'] --hypothesis '...'          # new logic
$CYCLE set-champion --work <work> --commit <sha> [--parameters '...'] --note '...'           # operator override
$CYCLE cron down --work <work>     # pause every scheduled job of this loop (up / status likewise)
```

From the main CLI, `python main.py learning on|off|status [--work DIR]` does the same with a readable summary
(switch state, schedules, champion, active candidates, last cycle). `DIR` defaults to
`<collector root>/data/derived_reports/auto-learning`, which may be a symlink to the active work root. The
collector root comes from the `PREDICTION_BOT_COLLECTOR_ROOT` environment variable (`.env` is not loaded for
this command), else `/mnt/data-collection/prediction-bot`.

`cron up|down|status` drives the Hermes cron job ids listed in `<work>/config.json` under `cron_jobs`
(the agentless cycle and the research agent), skips jobs already in the wanted state, re-reads the
scheduler and fails unless every listed job reached it. Other cron jobs are never touched. A cycle that is
already running finishes; `down` only stops future runs.

The trusted checkout must stay clean; update it by checking out a newly reviewed commit. Disk use grows
with cycles (replay stores, snapshot pages); `min_free_bytes` stops a cycle before work starts. Retention
is a manual decision — nothing deletes evidence.

## Control-lane parameters (lane files)

Validated by `auto_research.py check-parameters`; unknown keys are rejected; `{}` is today's policy.

- `min_edge` — replaces the 0.05 edge floor (side probability − price).
- `allowed_actions` — subset of `BUY_YES`, `BUY_NO`.
- `YES` / `NO` — per-side bounds `min_/max_probability` (side probability), `min_/max_price`,
  `min_/max_edge`.
- `include_/exclude_cities`, `include_/exclude_market_kinds`, `include_/exclude_contract_shapes`.

A parameter-induced skip carries a `parameter_*` reason. Parameters apply to the control lane of
`scripts/auto_research.py` (the research replay's source-probability lane); a later forward-paper stage
needs a matching paper lane before it can consume the same file.

## Research agent contract

The agent prefers parameters on the champion's code for threshold and filter ideas, and commits code in
`<work>/agent-repo` (branched from `champion`) only for new logic; then it `register`s with a
one-paragraph hypothesis. It must not edit the work root, run replays, touch services or accounts, or
call `set-champion`. Registration fails when no slot is free or the version is already active.
