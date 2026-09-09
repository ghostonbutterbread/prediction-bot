# Prediction Bot Data Reorganization Handoff

**Status:** proposed; read-only discovery and compatibility plan first

**Owner:** a dedicated data-migration agent working from an isolated feature branch/worktree based on `beta`

**Primary goal:** make Prediction Bot storage logical and durable without breaking the active collector, resolver, replay tooling, or Source Router handoff.

## Non-negotiable safety rules

- Preserve raw collector snapshots as append-only immutable evidence. Do not rewrite, compact, backfill, or move them in place.
- Keep the active observer collector and resolver unchanged until an approved compatibility cutover proves equivalent behavior.
- No deletion without explicit approval, a provenance/retention decision, and a metadata-preserving verification receipt.
- Do not enable or restart the paper Source Router or auto-promotion service as part of this task.
- Keep all work paper/observer-only. Never enable trading, wallets, or live orders.
- Make one inactive-cohort migration at a time; do not perform a whole-tree rewrite.

## Current operational paths that must not break

### Active observer collector

```text
/mnt/data-collection/prediction-bot/data/beta_shadow/
  forward_20260829T063405Z_beta_strict_source/
    paper/prediction_lab/market_snapshots.jsonl
    replay_index/collector_replay_index.jsonl
    resolutions/
    resolution_feed/
```

The active service is `prediction-lab-strict-source-cohort-20260823.service` and uses the fresh strict-source cohort configuration at:

```text
/home/ryushe/.config/prediction-bot/cohorts/beta-strict-source-20260829.yaml
```

This raw snapshot stream and compact replay index are high-priority evidence and active-supporting inputs.

### Source Router handoff

```text
/mnt/data-collection/prediction-bot/data/derived_reports/
  auto_source_router_history/
    current -> generations/7eee62c87847892d4e1a786f8216639b945b3113c2f1bdca70a5d3c1a3af11f5
    current/source_router_scoreboard/strict_finalized_source_scoreboard.jsonl
```

The branch-local disabled paper configuration points to that exact scoreboard:

```text
/home/ryushe/projects/prediction-bot-beta-integration/config.paper_source_router_auto_population.yaml
```

The paper shadow and Source Router lanes are intentionally disabled. Preserve the `derived_reports/auto_source_router_history/current` compatibility path until a reviewed successor is deployed.

### Previous root-drive material now preserved on collector storage

```text
/mnt/data-collection/prediction-bot/data/legacy-root-migrations/2026-08-30/
```

This is an archive, not an active runtime input. It contains old SourceWriter generations, partitioned migration previews, and historical copies. Do not let runtime configuration start consuming it implicitly.

## Current data classification

| Class | Current location | Intended handling |
|---|---|---|
| Active collector evidence | `data/beta_shadow/forward_20260829T063405Z_beta_strict_source/` | Preserve exactly; migrate only through a compatibility cutover |
| Collector replay index | Active cohort `replay_index/` | Preserve with its raw snapshot source; keep manifest source references correct |
| Resolutions and decision outputs | Cohort `resolutions/`, `resolution_feed/` | Derived cohort outputs; separate from raw evidence |
| Source Router history | `data/derived_reports/auto_source_router_history/` | Preserve stable `current`; generations belong under a derived-only namespace |
| External imports | `data/external/` | Keep provenance isolated from collector evidence |
| Legacy historical imports | `data/historical/` | Preserve as imported evidence with dataset-level identity |
| Runtime logs/state | Mixed today under cohort trees | Move only after explicit runtime configuration verification |
| Old experiments, smoke runs, retries, migration copies | `summaries/`, `archive*`, `legacy-root-migrations/` | Archive, document retention, and never treat as active data by default |

## Target storage contract

```text
data/
  evidence/
    collector/cohorts/<cohort-id>/
      raw/market_snapshots.jsonl
      indexes/
      receipts/
    external/<provider-or-import-id>/
    historical/<dataset-id>/

  derived/
    cohorts/<cohort-id>/
      decisions/
      resolutions/
      source-router/
      reports/
    source-router-history/
      generations/<immutable-generation-id>/
      current -> generations/<id>

  runtime/
    active/<cohort-id>/
      logs/
      shared-state/

  archive/
    legacy-root-migrations/2026-08-30/
    experiments/<experiment-id>/
    operational-audits/
```

## Required plan before any data move

1. Inventory every active read/write path from service units, cohort configuration, CLI defaults, and repository code.
2. Produce a path-reference matrix: old path, reader/writer, data class, proposed target, compatibility strategy, verification command, and rollback action.
3. Identify an inactive cohort suitable for a first migration rehearsal.
4. Define a compatibility approach per path: config cutover, symlink, or read-only alias. Do not use a symlink where it would hide a mistaken writer.
5. Add or update tests for:
   - collector raw path and replay-index manifest agreement;
   - resolver input references;
   - Source Router `current` scoreboard resolution;
   - failure-closed behavior for missing or mismatched evidence;
   - prevention of derived output next to raw snapshots.
6. Obtain approval for the exact first-cohort move plan before mutating data.

## SourceWriter-specific requirement

The previous root-drive incident came from 45 full SourceWriter generations retaining complete materialized inputs. The agent must design a bounded retention and compact-reference strategy before promotion can be enabled again:

- retain the stable current generation plus explicitly selected audit milestones;
- use the compact replay-index/locator model and on-demand hydration rather than copying full collector snapshots into every generation;
- preserve manifests and hashes for retained generations;
- make retention deletion explicit, reviewed, and receipt-backed;
- keep promotion disabled until this behavior is tested and accepted.

## Migration verification checklist

Before each cutover:

1. Run metadata-preserving dry-run comparison (`rsync --dry-run --itemize-changes` or equivalent).
2. Verify raw snapshot size/identity and replay-index manifest source paths.
3. Verify resolver market-reference paths against the intended raw collector source.
4. Verify the Source Router scoreboard path resolves through `current` exactly as before.
5. Exercise a non-mutating replay/read-only smoke path from the new location.
6. Record a receipt: source, destination, checksum/manifest strategy, command, result, and rollback path.
7. Only after the receipt and explicit approval, retire the old compatibility path.

## First deliverable expected from the agent

A read-only migration proposal—not a blind filesystem reorganization—with:

- complete path-reference matrix;
- proposed first inactive cohort migration;
- compatibility/rollback plan;
- retention proposal for SourceWriter generations;
- exact config/code changes required to make the layout durable;
- focused test plan and acceptance criteria;
- explicit list of deletions or archival moves requiring separate approval.

## Known capacity and risk

- DataCollector is approximately 85% full, with roughly 60 GB free.
- The legacy root-migration archive alone is about 99 GB.
- The old SourceWriter archive contains about 45 historical generations and orphaned staging material.
- The active Source Router handoff is only the mounted `current` generation; it is not the legacy archive.
