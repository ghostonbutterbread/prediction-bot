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
  → exact identity-bound derived outcome
  → qualified source observations and independent sample collapse
  → immutable history generation
  → stable current handoff for later Source Router decisions
```

Source observations must precede their authoritative settlement to become predictive history. A later decision may use only evidence already available at its immutable cutoff. Observation-only, unavailable, future-dated, or contradictory source targets are not forecasts. VOID releases synthetic reservations without being counted as a directional win/loss or economic P&L.

## Collector replay-index committed prefixes (manifest version 2)

This is the indexed-artifact foundation, **not yet a pointer-backed SourceWriter
promotion contract**. The collector remains the only live index writer through
its existing optional `prediction_lab_collect` hook. No worker is enabled by
this change. The compact row schema remains version 1; the manifest is version
2. Do not confuse these versions with SourceWriter generation versions.

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
prefix after later appends. A future generation must seal/hash that receipt in
its own immutable contract, rather than follow the mutable collector manifest.
Reading verifies the committed compact digest before filtering/hydration, then
uses bounded offset/length raw reads and verifies payload plus full routing
identity. Missing/truncated/replaced raw archives, changed selected payloads,
wrong index paths, unsupported schemas, and invalid locators fail closed.
Device/inode identity is local to the archive filesystem; relocation is **not
supported yet**. There is no heuristic search or fallback to another archive.

Memory is bounded by the largest individual decoded raw record plus a fixed
compact hashing buffer, not by total raw archive size (filter sets are
caller-owned). Updates read the new raw suffix but verify/hash the compact
index prefix; readers verify/scan the compact prefix once **per load**, not
once per generation or process. This is not computationally incremental
SourceWriter or a per-decision verification cache.

Legacy collector manifest version 1 remains read-compatible for its original
replay use, but cannot be silently extended/upgraded into a committed
checkpoint: update reports an explicit offline migration prerequisite. Existing
V2 copy-backed SourceWriter generations and V1 promotion rejection remain
unchanged. Migration, verified relocation, rejected-row locators, bounded
resolution binding/materialization/collapse, and both pointer-backed consumers
are remaining integration gates. In particular, invalid-row counts alone do
not preserve exporter rejection reasons; SourceWriter must not treat the
accepted-row-only index iterator as a complete source archive.

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
