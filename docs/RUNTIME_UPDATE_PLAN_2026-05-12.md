# Reviewed Runtime Update Plan — 2026-05-12

## Short answer

The monthly partitioning is **not automatic yet**. The committed helper is safe and ready to use manually, but no cron/systemd/OpenClaw job currently runs it at month-end. That is intentional until runtime update, reader compatibility, and writer safety are validated, because active runtime data is symlinked into the development repo.

The correct first automation is a **dry-run month-end audit/report**, not active file rotation. Once that is stable, we can add copy-first shard writing to a non-runtime directory. Only later should active readers switch to monthly shards.

Status update: the dry-run OpenClaw cron has been installed as `1a731ace-3796-4732-bab2-b6d376e47855`, scheduled for `30 0 1 * *` in `America/Los_Angeles`, delivering to Bot Status. It runs report-only with no `--write`.

## Current state

- Development repo at the time of this plan: `/home/ryushe/projects/prediction-bot`
  - Canonical runtime/data repo path is now: `/mnt/data-collection/prediction-bot` (the old `~/projects/prediction-bot` path remains as a compatibility symlink).
  - Branch: `feature/weather-strategy-lanes`
  - Current commit: `16703d9 Add monthly JSONL partition helper`
  - Dirty/untracked work exists and must be preserved. Runtime update must use committed refs only, not dirty worktree state.
  - Current dirty dev files observed:
    - Modified: `bot/market_router.py`, `bot/prediction_lab_backfill.py`, `scripts/analyze.py`, `scripts/morning_bot_status_report.py`, `tests/test_analyze_strategy_policy_status.py`, `tests/test_market_router.py`, `tests/test_prediction_lab_backfill.py`
    - Untracked: `bot/weather/training_dataset.py`, `scripts/shadow_replay_sweep.py`, `scripts/weather_training_dataset.py`, `tests/test_weather_training_dataset.py`, this plan doc until committed
- Active runtime repo: `/home/ryushe/active-projects/prediction-bot`
  - Branch: `runtime/prediction-bot`
  - Current commit: `9c40591 Merge remote-tracking branch 'origin/main' into runtime/prediction-bot`
  - Dirty runtime file to preserve before any merge/reset/stash: `scripts/prediction_lab_monitor_cron.sh`
  - Reviewer note: this dirty runtime file has local runtime-specific changes choosing active repo/config, so export it as a patch or commit it to a runtime-local branch before touching the runtime branch.
- Shared/symlinked runtime data:
  - `/home/ryushe/active-projects/prediction-bot/data -> /home/ryushe/projects/prediction-bot/data`
  - Strong rule: writes from either repo path mutate the same runtime data.

## New code awaiting runtime update

The active runtime is behind the dev branch by the broader stack ending in:

1. `e2cc89b Add unified agent decision ledger slices`
2. `673a4fb Add legacy decision backfill sidecars`
3. `96ab846 Add agent decision reporting summaries`
4. `810638a Add live readonly decision audit hooks`
5. `16703d9 Add monthly JSONL partition helper`

There are also earlier weather strategy / beta-shadow commits between runtime and dev. Updating runtime by merging the dev branch would pull those too, so this should not be treated as a tiny hotfix deploy.

## Data partitioning behavior

Committed helper:

- Script: `scripts/partition_jsonl_by_month.py`
- Test: `tests/test_partition_jsonl_by_month.py`
- Dry-run by default.
- Write mode requires `--write`.
- Refuses existing outputs unless `--force`.
- Can write to a separate `--output-root` for copy-first migration.
- Warning: when partitioning multiple inputs with the same stem, e.g. multiple `market_snapshots.jsonl` files, use separate output roots per source to avoid shard/manifest collisions.
- Date field priority:
  1. `observed_at`
  2. `timestamp`
  3. `decided_at`
  4. `created_at`
  5. `recorded_at`
  6. `started_at`
  7. `finished_at`
- Bad JSON and blank lines are skipped and counted in the manifest.
- Write mode streams rows to temp shard files and atomically promotes them, avoiding multi-GB in-memory buffering.

Validation already done:

- `python3 -W error::ResourceWarning -m unittest tests.test_partition_jsonl_by_month`
- `python3 -m py_compile scripts/partition_jsonl_by_month.py tests/test_partition_jsonl_by_month.py`
- Full dry-run scans of the big current Prediction Lab files.
- Copy-first preview written outside active runtime data:
  - `/home/ryushe/projects/prediction-bot-data-migration-preview`

Preview results:

- Paper Prediction Lab `market_snapshots.jsonl`:
  - `2026-04`: 60,873 rows
  - `2026-05`: 231,987 rows
  - 1 bad JSON row skipped
- Beta-shadow Prediction Lab `market_snapshots.jsonl`:
  - `2026-05`: 107,610 rows

## Month-end automation recommendation

Do **not** immediately schedule a destructive month-end rotation that rewrites active files.

Recommended rollout:

### Phase A — Dry-run month-end audit job

Create an OpenClaw cron/systemd job that runs around the first day of each month, but only in dry-run/report mode:

```bash
cd /mnt/data-collection/prediction-bot
python3 scripts/partition_jsonl_by_month.py \
  data/paper/prediction_lab/market_snapshots.jsonl \
  data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl
```

Deliver the manifest summary to Bot Status. This confirms the datasets are partitionable without touching runtime files.

### Phase B — Copy-first monthly shard writer

After dry-run output is stable, schedule a copy-first writer to a non-runtime location. Use separate output roots for files with the same stem:

```bash
cd /mnt/data-collection/prediction-bot
python3 scripts/partition_jsonl_by_month.py \
  data/paper/prediction_lab/market_snapshots.jsonl \
  --write \
  --output-root /home/ryushe/projects/prediction-bot-data-migration-preview/paper_prediction_lab/market_snapshots

python3 scripts/partition_jsonl_by_month.py \
  data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl \
  --write \
  --output-root /home/ryushe/projects/prediction-bot-data-migration-preview/beta_shadow_prediction_lab/market_snapshots
```

No active file replacement yet.

### Phase B preflight — required before any write-mode partitioning

Before any `--write` run, even copy-first:

1. Confirm no active writers are appending to the target file:
   - process check for paper loop / Prediction Lab collector
   - cron/systemd timer check
   - file mtime/size stability check across a short interval
2. Capture source metadata:
   - `stat`
   - `sha256sum` if practical
   - line count if practical
3. Create a backup/snapshot of the source or source path metadata:
   - prefer `cp --reflink=auto` when staying on the same filesystem
   - otherwise archive/copy manifest + source path details before write-mode work
4. Write to a non-runtime output root first.
5. Compare manifest counts to source counts before considering reader changes.

### Phase C — Reader compatibility

Only after replay/analysis readers support either:

- `monthly/<stem>/<stem>-YYYY-MM.jsonl`, or
- a manifest/list of shard paths,

then switch replay jobs to monthly shards.

### Phase D — Optional active rotation

Only after backups and reader compatibility are proven:

1. Stop writer processes cleanly.
2. Snapshot/hash source files.
3. Write monthly shards.
4. Move original monolith to an archive path, not delete.
5. Replace readers with monthly inputs.
6. Restart collector/paper only after smoke checks.

## Runtime update options

### Option 1 — Recommended: reviewed staging merge into runtime branch

Status update: rollback/preservation prep was started. Runtime dirty cron patch was exported to `/home/ryushe/projects/prediction-bot-runtime-update-20260512T2046/prediction_lab_monitor_cron.runtime.patch`, metadata to `/home/ryushe/projects/prediction-bot-runtime-update-20260512T2046/metadata.txt`, and rollback branch `runtime/rollback-2026-05-12` was created at active runtime HEAD.

An isolated staging worktree was created at `/home/ryushe/projects/prediction-bot-runtime-staging-2026-05-12` on branch `runtime/update-2026-05-ledger-audit-monthly`.

Staging update status: conflict resolution was completed in the isolated worktree and committed as `d240cea Stage ledger audit monthly runtime update`. Active runtime was not moved, restarted, or pointed at this commit.

Focused validation passed on the staging commit:

- `python3 -m py_compile scripts/partition_jsonl_by_month.py tests/test_partition_jsonl_by_month.py`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_partition_jsonl_by_month` → 5 tests passed
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_agent_decision_backfill.py tests/test_agent_decision_reporting.py tests/test_live_decision_audit.py tests/test_runner_status_and_live_path.py` → 30 tests passed
- `git diff --cached --check` passed before commit after fixing trailing blank lines in the new beta-shadow YAML configs

Promotion blocker found after commit: an extra broader safe gate failed on the clean staging commit:

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_prediction_lab_collect.py tests/test_prediction_lab_replay.py tests/test_prediction_lab_monitor.py tests/test_prediction_lab_backfill.py tests/test_analyze_strategy_policy_status.py tests/test_market_router.py`
- Result: 19 failures, 88 passed, 5 subtests passed.
- Main failure clusters: replay now skips expected BUY rows as `unknown_market_route`; analyze strategy-policy status/reporting expectations are not satisfied by the committed runtime-staging contents.
- Attempted experiment: checking out the feature versions of `bot/decision_pipeline.py`, `bot/prediction_lab_replay.py`, `scripts/analyze.py`, then `bot/strategies/enhanced.py` reduced failures to 12 replay failures but did not make the broader gate pass. The experiment was discarded with `git reset --hard d240cea`, leaving staging clean.
- Do not promote `d240cea` to active runtime until this broader Prediction Lab/replay/analyze mismatch is resolved or explicitly accepted as out-of-scope.

1. In active runtime worktree, preserve dirty file:
   - inspect `scripts/prediction_lab_monitor_cron.sh`
   - export patch: `git diff -- scripts/prediction_lab_monitor_cron.sh > /tmp/prediction_lab_monitor_cron.runtime.patch`
   - preferably commit it to a runtime-local preservation branch before any merge/reset
2. Create safety refs before touching runtime:
   - tag or branch current runtime HEAD, e.g. `runtime/pre-update-2026-05-12`
   - save the dirty cron patch externally
3. Create a staging branch from runtime:
   - `runtime/update-2026-05-ledger-audit-monthly`
4. Merge/cherry-pick the dev stack intentionally from committed refs only.
5. Resolve conflicts in staging, not directly on `runtime/prediction-bot`.
6. Run focused tests:
   - `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_partition_jsonl_by_month`
   - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_agent_decision_backfill.py tests/test_agent_decision_reporting.py tests/test_live_decision_audit.py tests/test_runner_status_and_live_path.py`
   - Prediction Lab collect/replay/monitor/backfill tests as applicable, including existing backfill/reporting tests before deploy
   - `python3 -m py_compile scripts/partition_jsonl_by_month.py tests/test_partition_jsonl_by_month.py`
7. Run a no-runtime-mutation smoke:
   - isolated Prediction Lab collector with temp `runtime.base_dir`
   - dry-run partition scans
8. Produce a diff summary and deploy checklist.
9. Only then fast-forward/reset `runtime/prediction-bot` to the reviewed staging commit.

Pros: safest audit trail, handles the broad stack honestly.

Cons: slower than cherry-picking only 5 commits.

### Option 2 — Minimal cherry-pick of ledger/audit/monthly commits

Cherry-pick only:

- `e2cc89b`
- `673a4fb`
- `96ab846`
- `810638a`
- `16703d9`

This may fail if those commits depend on earlier weather/beta commits. If conflicts appear, stop and fall back to Option 1.

Pros: smaller runtime change if dependencies are clean.

Cons: higher risk of hidden dependency drift.

### Option 3 — Full feature branch deployment

Deploy all commits from `feature/weather-strategy-lanes` into runtime.

Pros: simplest git history.

Cons: largest behavior change; not recommended until we want the full weather strategy stack live/paper active.

## Rollback path

Before promoting any runtime update:

1. Record current runtime HEAD:
   - expected current: `9c40591`
2. Create a rollback branch/tag:
   - `git branch runtime/rollback-2026-05-12 9c40591`
3. Save dirty runtime cron patch outside repo:
   - `/tmp/prediction_lab_monitor_cron.runtime.patch`
4. If deployment fails before restart:
   - reset runtime branch/worktree back to rollback ref
   - reapply cron patch if needed
5. If deployment fails after restart:
   - stop affected runtime processes
   - restore rollback ref
   - reapply cron patch
   - restart only after config/status checks

## Recommended next action

Use **Option 1**.

Before any restart/deploy:

1. Create staging branch from active runtime.
2. Preserve active dirty cron file with an external patch and/or runtime-local commit.
3. Merge dev branch into staging from committed refs only.
4. Run tests and isolated smoke.
5. Add a dry-run-only month-end OpenClaw cron after the staging branch passes.
6. Defer write-mode automation until monthly-reader compatibility exists.

## Explicit safety rules

- Do not touch active runtime state files in-place.
- Do not run `--write --force` under active/symlinked `data/` without explicit approval.
- Do not restart paper/Prediction Lab/live until the staging update passes tests.
- Do not enable live order/accounting mutation from audit hooks.
- Preserve unrelated dirty/untracked files in both dev and runtime worktrees.
- Runtime update must be based on committed refs; dirty dev changes must not leak into runtime.
