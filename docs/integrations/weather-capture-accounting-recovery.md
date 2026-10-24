# Weather capture accounting capacity and verified recovery

Task: `t_d694dfa0`. Dependent base: `fec68a3bf059fb71ef257e451c2799802f1c04abb`
(the independently reviewed Kalshi pagination feature). Target: beta; no main promotion.

## Diagnosed failure

The previous 8 KiB accounting-sidecar ceiling was insufficient as the fixed
SHA-256 shard namespace filled. `_publish()` created a new shard directory before
persisting its pending reservation. An accounting-size failure at that point
left an empty directory outside the saved directory snapshot. Startup then
correctly failed closed on `capture directory changed outside accounting lock`.
This is distinct from the earlier evidence-empty missing-checksum recovery.

The authorized runtime was deliberately stopped before inspection; beta has not
been changed or activated. Existing raw/capture evidence is not being reset,
relabelled, deleted, or rewritten.

## Narrow implementation

- Reserve 32 KiB for a 16 KiB sidecar plus its atomic replacement. This covers
  the bounded 256-shard directory inventory and pending reservations.
- Persist the pending reservation before `_write_object()` creates the target
  parent; an initial accounting failure must not modify directory evidence.
- The collector CLI returns terminal status 78 for `CaptureRetentionError`,
  including initialization. Transient unrelated failures retain existing handling.
- Deployment proposal: create a user-unit drop-in for the existing collector
  with `RestartPreventExitStatus=78`; reload and verify the exact effective unit.
  This prevents another blind integrity retry loop. The drop-in remains held for
  independent review and deliberate parent activation.
- Storage tests express their small usable budgets relative to accounting reserve.

## Evidence and tests

Derived receipts:
`/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/finish_e2e_t_d694dfa0/accounting_parent/`.

- Baseline `red.log`: all three new regression cases failed as expected (one
  failure, two errors). The outer 90-second command limit expired after the test
  report had printed; no successful baseline gate is claimed.
- `green.log`: 80 cases, zero failures, two harness errors because the read/write
  guard denied owned-worktree fixture data. No product failure inferred.
- Corrected `green-v2.log`: **80 tests, zero failures/errors/skips**, including
  all 256 actual blob shards, pre-reservation directory preservation, CLI terminal
  status, existing corruption/hash/capacity/thread/process cases and collector
  regressions. Network and subprocess creation denied by fixture audit guard;
  writes limited to scratch and the owned worktree's ignored fixture `data/`.
- `reconcile-fixtures.log`: **3 disposable-fixture tests passed**. Successful
  populated-store reconciliation preserves originals and all evidence bytes;
  new/unaccounted files and mutated blobs refuse before metadata publication.
- `full_inventory_v1/inventory.json`: all **2,873 files** (853 blobs and 2,020
  receipts) read and hashed under the existing exclusive lock with the service
  stopped. All content-addressed body hashes, 2,912 nested payload references
  and 1,456 receipt references verified. Evidence bytes **104,000,180** exactly
  match the valid-checksum original accounting. Only the empty `blobs/64` and
  its parent directory timestamp disagree. No claim that every external raw/index
  reference was audited: capture-internal inventory is a separate boundary.
- Inventory receipt SHA-256:
  `f9318ff7f091f0fc51b27b2c9cabd25ee52f75efbf8fb7f59b8491e1ea92d2df`.
- Original sidecar preserved outside the store, SHA-256:
  `266bceb865f9e72e2edb98a903193b04e7dc3035972f26bcb236758b5e0ffba1`.

## Proposed operational metadata repair — not yet applied

Review the derived `inventory_capture_store.py`,
`reconcile_capture_accounting.py`, fixture tests and inventory receipt together.
The helper pins the original sidecar and complete inventory, refuses any changed
file/hash/byte total or discrepancy other than the diagnosed empty shard, proves
quiescence and takes the existing exclusive lock. It preserves original sidecar
bytes, then calls canonical `_save_state()` with the **unchanged** byte count and
null pending state. Only the directory snapshot and checksum are refreshed.
It reads back canonical state and hashes every original evidence file again.
`usage()` is called only after releasing the outer lock to avoid self-deadlock.
There is no reset, checksum-only retrofit, integrity bypass, evidence removal,
or automatic service start. Any unexpected condition leaves the service stopped.

Independent code and operation review, integration/full gate, verified metadata
repair, effective unit drop-in and deliberate activation are all still required.
A fresh complete collection-cycle audit (catalog pages, identities, geography,
captures/integrity and replay publication) is the final acceptance gate.
