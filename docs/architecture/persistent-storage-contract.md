# Persistent storage and code-checkout contract

## Code and data have different lifetimes

A branch selects code, not a dataset or wallet. Keep the selected storage root and cohort identity fixed when testing a different checkout. A new cohort requires an explicit new subtree; never infer it from the Git branch name.

The default persistent root is `/mnt/data-collection/prediction-bot`. `load_config` selects the root at call time in this order:

1. `PREDICTION_BOT_COLLECTOR_ROOT` environment override;
2. `runtime.storage_root`;
3. the collector-volume default for the beta runtime overlays.

Roots must be absolute after expanding `~`. The `paper_beta_shadow_runtime` and `prediction_lab_beta_shadow_runtime` overlays opt into persistent storage. Non-beta legacy configurations retain their old relative-path behavior unless a root is explicitly selected. Loading configuration does not create directories or start workers.

```yaml
runtime:
  storage_root: /mnt/data-collection/prediction-bot
  base_dir: data/beta_shadow/<cohort-id>
shared_market:
  runtime_root: data/beta_shadow/<cohort-id>/shared_market_runtime
prediction_lab:
  replay_index:
    root_dir: data/beta_shadow/<cohort-id>/replay_index
resolution_feed:
  market_ref_paths:
    - data/beta_shadow/<cohort-id>/paper/prediction_lab/market_snapshots.jsonl
  output_dir: data/beta_shadow/<cohort-id>/resolution_feed
  central_output_dir: data/beta_shadow/<cohort-id>/resolutions
```

This fragment documents paths only; it does not enable collection, resolution, paper lanes, or trading.

## Path ownership

`load_config` anchors the known persistent data fields before constructing paper-wallet contracts:

- runtime/data/log roots and weather-observation logs;
- shared-market state;
- collector snapshot overrides and compact replay-index root;
- resolver input paths/globs, state/report output, and canonical resolution output;
- paper-wallet roots, lane-decision ledgers, and configured source-scoreboard paths;
- configured derived-maintenance state and Source Router promotion paths.

Relative values are storage-root-relative. Explicit absolute paths are preserved, including a `current` symlink: do not resolve it to a frozen generation during config loading. An environment root override does **not** relocate existing absolute input or output paths. Inspect such configurations for intentional external inputs versus an accidental split before running them.

Code-owned paths—configuration composition bases, lane definitions, Python imports—continue to belong to the code checkout. No arbitrary strings or provenance records are rewritten. Direct module calls that bypass `load_config` must pass resolved absolute data paths themselves.

## Evidence flow

```text
collector raw snapshot (immutable/append-only)
  → compact index and verified hydration
  → shared candidate and snapshot identity
  → outcome-free paper/lane decision

independent market settlement
  → separate canonical resolution artifact
  → exact identity-bound outcome, joined at read time
  → qualified source observations and independent sample collapse in memory
  → SourceRouter decision directly from the existing dataset
```

Source observations must precede their authoritative settlement to become predictive history. A later decision may use only evidence already available at its immutable cutoff. Observation-only, unavailable, future-dated, or contradictory source targets are not forecasts. VOID releases synthetic reservations without being counted as a directional win/loss or economic P&L.

## Direct dataset SourceRouter (canonical)

**Status:** accepted architecture; owner: Prediction Bot / Hermes. This section
supersedes the separate SourceWriter/persisted-history-generation plan and the
obsolete `docs/integrations/sourcewriter-indexed-artifacts.md` handoff (removed;
retained in Git history). Ryushe reaffirmed this boundary in Discord message
`1562336764885213315`: use the dataset directly, rather than rewrite sources.

The collector owns raw evidence and its compact index; the independent resolver
owns final resolution receipts. SourceRouter reads those existing inputs,
verifies their committed identities and clocks, and derives eligible source
history and scorecards in memory. No separate SourceWriter, copied source
dataset, generated history directory, or recurring publisher is required.
Ordinary collector/index/resolution writes and compact decision/audit receipts
remain legitimate; this is not a ban on collecting data or recording decisions.

The implemented entry point is
`bot.weather.source_router_direct_history.load_direct_strict_source_history`,
wired into `bot/paper_shadow_lanes.py`. Implementation
`2d45eec4f4ee346e94d76a6a2f52ec03cb14430a` and integration
`696900651abbd108f4285dee45ceee09cdf77d28` are historical references; use Git to
verify the selected checkout before claiming availability or activation.

Single-archive lane parameters are `collector_replay_index_path`,
`collector_replay_manifest_path`, `strict_resolutions_path`, and
`history_row_limit` (default 100). Alternatively supply `collector_replay_archives`,
an explicit newest-first list of `index_path`, `manifest_path`, and
`strict_resolutions_path` mappings. Do not mix the two modes or infer paths.
The consumer supplies the immutable candidate decision timestamp.

The limit counts **distinct events per source/city/kind/shape**, not the newest
100 raw polls globally. The reader verifies and scans the committed index,
hydrates referenced rows, applies canonical sanitization, exact independent
outcome binding, source/target proof and strict decision-time eligibility, and
collapses duplicate events. Incorrect but eligible forecasts remain in accuracy
denominators; pending, ambiguous, VOID and future evidence cannot train a later
decision. Missing or changed evidence fails closed rather than falling back to
an unrelated generated history. The cap bounds selected history, not total I/O;
do not claim a constant-cost read or already-proven runtime throughput.

Dataset availability removes a rewriting prerequisite, not evidence checks or
consumer wiring. Verify each actual consumer separately: the contextual-weather
bridge currently uses `history_metadata_path` and
`load_predictive_source_history`, not this strict-scorecard result. If that bridge
needs direct access, adapt it to the same dataset/shared qualification helpers;
do not introduce a writer/publisher as the solution. Its 30-event contextual
floor and the strict Router's normal 100-event reliability floor are distinct
and unchanged. Code merged, paths configured, and recent output proving use are
three separate states; this contract does not activate any of them.

Existing materializers, V2 generation readers, `auto_source_router_promotion`
and disabled generated-scorecard configs are **legacy compatibility only**.
They are not pending SourceWriter scope, a mandatory post-resolver stage, or a
reason to copy archives. Preserve existing data and explicit compatibility
inputs until separately retired; never silently enable legacy promotion.

## Collector replay-index committed prefixes (manifest version 2)

This is the collector-owned indexing contract used by direct dataset readers,
not a prerequisite to implementing a separate writer. The collector remains
the only live index writer through its existing optional
`prediction_lab_collect` hook. No worker is enabled by this change. The compact
row schema remains version 1; the manifest is version 2. These versions are
independent of legacy generated-history schema versions.

The version-2 manifest commits `committed_index_bytes` and `index_sha256` over
exactly that compact-index prefix, `indexed_source_bytes` over complete raw
JSONL rows, `source_rows_seen`, indexed/invalid counts, and raw archive device /
inode identity. Each accepted-row locator retains byte offset/length, physical
row number, full payload digest, market, candidate, snapshot, and observation
identity. Initial and subsequent passes defer unterminated records, including
parseable JSON lacking a newline. Each pass freezes its starting raw size;
concurrent appends wait for the next pass. The deterministic append chain
starts at SHA-256(empty), then folds SHA-256(previous-chain bytes + row-digest
bytes) for **every complete raw row**, including invalid rows. Full build and
append/update therefore produce identical compact-index bytes and chain over
the same final raw extent. `source_sha256` is the whole complete-prefix digest
only when computed from byte zero; after an append it is null, not a claimed
whole-file hash.

Writers serialize through the existing file-lock primitive. The first build
publishes an empty checkpoint, then extends it; updates append/fsync compact
rows before atomically replacing the manifest using `atomic_write_json`.
Interrupted publication leaves a prior readable prefix. Retry verifies that
prefix and truncates only an unpublished **derived index suffix**, never raw
evidence. An unchanged update retains exact manifest bytes. This is a
process-interruption guarantee, not a new power-loss or filesystem durability
guarantee; the existing lock/atomic helpers retain their existing platform
limitations.

Readers may retain an exact manifest receipt and continue using its committed
prefix after later appends. A reproducible decision or audit must bind the exact
receipt it consumed rather than follow the mutable collector manifest later.
Reading verifies the committed compact digest before filtering/hydration, then
validates strictly increasing row numbers and ordered non-overlapping byte
ranges across the complete compact prefix before any filtered/limited result or
update, then uses bounded offset/length raw reads and verifies payload plus full
routing identity. Missing/truncated/replaced raw archives, changed selected payloads,
wrong index paths, unsupported schemas, and invalid locators fail closed.
Device/inode identity is local to the archive filesystem; relocation is **not
supported yet**. There is no heuristic search or fallback to another archive.

Memory is bounded by the largest individual decoded raw record plus a fixed
compact hashing buffer, not by total raw archive size (filter sets are
caller-owned). Updates read the new raw suffix but verify/hash the compact
index prefix. Each load performs a bounded digest pass, a bounded structural
validation pass, and a filtered hydration pass over the compact index (the
last may stop early); updates validate their prior prefix in the same way and
hash the extended compact index. None of these passes rereads the full raw
archive. Verification is repeated **per load**, not cached per generation or
process. This is not a per-decision verification cache or a constant-cost query.

Legacy collector manifest version 1 remains read-compatible for its original
replay use, but cannot be silently extended/upgraded into a committed
checkpoint: update reports an explicit offline migration prerequisite. Existing
V2 copy-backed generations and V1 promotion rejection remain legacy
compatibility behavior, not direct-reader prerequisites. Migration or relocation
work is needed only for an actual unsupported input. Invalid-row counts alone
do not preserve exporter rejection reasons: the accepted-row-only index is not
a complete accounting of rejected raw records. Do not turn that coverage limit
into a requirement to rewrite the source archive.

## Composition diagnostics

`paper_shadow_lane_compose_replay.py` and `paper_shadow_lane_composition_sweep.py` now default to:

```text
<storage-root>/data/derived_reports/lane_compositions/<run>/
```

Their relative `data/...` lane/resolution inputs select the storage root; composition/configuration paths remain code-relative. Absolute inputs remain usable. Explicit checkout-local derived output directories are retained for backwards-compatible disposable diagnostics, but are not persistent runtime destinations. Outputs beneath raw collector directories remain rejected. Text-mode output supports absolute external derived paths.

The unified corpus retains legacy checkout discovery and also discovers collector-root composition outputs. Its mixed-ledger diagnostic status is unchanged. Other legacy diagnostic commands may still have checkout-local defaults: pass explicit absolute data paths and keep them separate from forward/persistent workers. The chunked replay's existing `data/summaries/lane_compositions/...` default remains a supported explicit legacy location; it is not a new persistent worker destination.

## Changing code safely

1. Identify the actual service command, code checkout, working directory, effective config, and selected data/cohort roots. A directory named `active` is not proof of authority.
2. Test in a clean isolated checkout with temporary roots, no credential-bearing environment, and its source first on `PYTHONPATH`.
3. Keep raw evidence unchanged. Verify raw/index/resolver references and candidate/snapshot identity before and after changing code.
4. For compatibility with older branches that do not know `runtime.storage_root`, supply an externally stored configuration with absolute persistent data paths.
5. Treat changing the code used by a running service as deployment, even if no restart occurs. Never switch the branch beneath an active collector as a persistence test.

A merge does not authorize a service restart, timer enablement, data migration, old-generation deletion, or trading promotion. Path stability is not a claim of power-loss durability or a tested filesystem backup/restore guarantee.
