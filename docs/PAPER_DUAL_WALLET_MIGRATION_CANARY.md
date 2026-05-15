# Paper Dual-Wallet Migration/Canary Runbook

This runbook is the Phase 5 read-only plan for existing paper state.

It does not stop or restart paper or Prediction Lab processes. It does not move,
delete, or rewrite `risk_state.json`, `sim_*.json`, lifecycle ledgers, or old
Prediction Lab ledgers. The goal is to describe the current compatibility
mapping, preview any later shared-candidate cutover, and make operator steps
explicit before any runtime change is attempted.

## Current compatibility mapping

Keep the current accounting roots in place:

- `data/paper` remains `stable_paper`
- `data/beta_shadow/paper` remains `beta_paper`

That mapping preserves the existing paper wallet/accounting state introduced in
Phases 1-4. The Phase 5 cutover target is narrower: shared-candidate datasets
should eventually live outside those wallet roots, while wallet accounting stays
where it is unless a later spec intentionally migrates accounting too.

## Read-only canary helper

Run from the repo root:

```bash
python3 scripts/paper_migration_canary.py --json
```

Or against a specific config:

```bash
python3 scripts/paper_migration_canary.py \
  --config config.paper_beta_shadow_weather.yaml \
  --json
```

The helper is read-only by default and reports:

- the current stable/beta wallet mapping
- whether `stable_paper` and `beta_paper` roots, `risk_state.json`, and preview
  session paths are isolated
- what current paper state exists under each wallet root
- whether any Prediction Lab candidate datasets were written under a wallet root
  with path and `size_bytes` metadata
- what would later be copied to `shared_candidates/`
- what canonical backfill command would later be used after copy

By default, the canary is bounded/stat-only: it detects candidate dataset files
and calls `stat()`, but it does not open JSONL files to count rows. Use the
explicit deep-scan mode only when row counts are worth the extra IO:

```bash
python3 scripts/paper_migration_canary.py --deep-scan --json
```

## What counts as accidental candidate data under wallet roots

These are the main cases the helper flags:

- `prediction_lab/market_snapshots.jsonl`
- `prediction_lab/predictions.jsonl`
- `prediction_lab/analysis/*.jsonl`

Those files are useful and should be preserved. They are only "accidental" in
the sense that the long-term shared-candidate design wants them outside
`stable_paper` / `beta_paper` accounting roots.

## Later cutover plan

Do not perform these steps during the current Phase 5 implementation pass. They
are the explicit operator procedure for a future supervised maintenance window.

1. Run `scripts/paper_migration_canary.py` and confirm the current mapping is still:
   `data/paper -> stable_paper` and `data/beta_shadow/paper -> beta_paper`.
2. Confirm the canary reports isolated roots and distinct `risk_state.json` /
   preview `sim_phase5_canary.json` paths.
3. Leave existing wallet accounting ledgers in place:
   `risk_state.json`, `sim_*.json`, `agent_runs.jsonl`, `agent_decisions.jsonl`,
   `lifecycle.jsonl`, `reconciliation.jsonl`.
4. If the canary reports Prediction Lab datasets under a wallet root, copy them
   out to the suggested shared-candidate destination. Use copy-only commands
   such as `cp -n` or `rsync --ignore-existing`; do not move files in place.
5. After the raw datasets are copied, optionally build canonical analysis at the
   suggested shared destination with `scripts/prediction_lab_backfill.py
   --canonical-analysis`.
6. Validate readers/reports against the copied shared-candidate path first.
   Until that validation passes, leave collectors and reports pointed at the old
   location.
7. In a later supervised maintenance window, repoint only the Prediction Lab
   writer path to the shared-candidate root, verify new rows land there, and
   keep the old wallet-root `prediction_lab/` files as read-only history.

## Safety rules

- Do not mutate active paper state as part of the canary.
- Do not move or delete old ledgers.
- Do not assume candidate rows under wallet roots are disposable.
- Do not repoint writers until read-only validation against copied data passes.
