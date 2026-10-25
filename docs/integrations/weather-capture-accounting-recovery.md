# Weather capture accounting capacity and verified recovery

Task: `t_d694dfa0`. Dependent base: `fec68a30e3e66a5bfcb51dff40d2f22536329200`
(the independently reviewed Kalshi pagination feature). Target: beta; no main promotion.

## Accepted integration decision — supersedes historical holds below

Exact source `1c86e778077d816f5a07fb54e3972dc2baa5fb88` and combined
`645826103dca7673fb28bd2c2ac1e9e30c7111eb` / tree
`1a451cfb36a45611cd47f22bf561b8b1a340e47d` received independent APPROVE
from `deleg_1afa4044`. Combined full gate: 1,866 unique executed IDs, 1,864
successful IDs, zero failures/errors, seven runtime-config skip entries and
one inherited real-archive exclusion. All 484 hashes match. Earlier blocked
regex checkpoints remain historical BLOCKs. The retained supplemental's 48
string-spy failures per candidate were individually reconciled as visible literal
CommonMark characters, not suppressed or described as a green diagnostic.

User message `1563768870861406211` now authorizes beta integration and restarting
the existing observer. Fresh locked inventory and all original helper/source
pins matched the reviewed discrepancy. The approved metadata-only reconciliation
was actually applied and independently read back: 2,873 evidence files /
104,000,180 bytes unchanged; original sidecar preserved; new sidecar SHA-256
`331386f56f17d741bb9774a9227e399833b97507588dd2d921278afe794ebde6`.
Receipt: `accounting_parent/production_reconcile_1563768870861406211/result.json`
under the existing finish-e2e report root. No raw blob/receipt was rewritten or
deleted. Service is still stopped at this checkpoint.

The effective service interpreter is the storage-root `.venv` (Python 3.14),
not the source-checkout `.venv` used for prior frozen tests. Runtime dependency
preflight found only the new parser pin mismatched; the installer dependency
step was applied to that existing interpreter using the reviewed root manifest.
All runtime requirements now verify, including markdown-it-py 4.2.0. No source
root, cohort, limits or trading setting changed. Next: integrate beta excluding
these three branch-local dossiers, prove all other blobs equal the approved
combined candidate, run the exact merged tree with the actual runtime interpreter,
apply/read back the reviewed existing-unit exit-78 override and start exactly one
existing observer, then verify fresh complete-cycle geography and retained/index
integrity against the new public catalog. Fresh live acceptance is not yet proven.
Remove this dossier from beta in the integration commit; retain this accepted
decision in feature history. Do not promote main.

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

Implementation checkpoint: `0b3ebee1e12eaefcbb4bc9e5417cf6333a636297`.
Independent review `deleg_64a0efbd` approved that exact code, the narrowly pinned
metadata-only recovery and the existing-unit exit-78 override. Its strict frozen
fixture gate passed **87 tests with zero failures/errors/skips**; the parent's
exact-candidate full gate ran **1,840 tests, zero failures/errors**, with seven
runtime-config skip entries and the same single real-archive exclusion.
Review receipts are under `independent_final_review_0b3ebee/`; full receipts are
under `weather_identity_repair_t_ca47d502/accounting_t_d694dfa0_feature/`.

The incorrect base identifier in the original dossier was corrected using Git
and the independent review; no source behavior changed in this handoff update.
Before application, verify the reviewed helper hashes and the imported source
bytes as well as clean Git status. Regular Git modes 100644 and 100755 are both
valid; symlinks or mismatching blobs must still refuse.

**Production hold:** the corrected execution request expired at the approval
gate and did not run. Readback confirms the original sidecar digest and stopped
service are unchanged. Do not retry or rephrase that operation until the user
responds. No metadata repair, unit override, beta merge/push or startup occurred.

The isolated parent accounting feature is also the owning candidate for combined
offline compatibility with source-guards checkpoint
`1ddb4c9c8c0eaaf61694f39b5277d9bdf717ef86`. This is not a beta/runtime activation.
Fresh independent source/combined review, combined full gate, verified metadata
repair, effective unit drop-in and deliberate activation are still required.

Independent review `deleg_880cdcb0` subsequently **blocked** combined checkpoint
`ec6eca0dc4368c2b796d3b21e96e3816f9e48562` despite its 1,854-test full pass:
Markdown could hide an explicit unsupported city. The source-owning feature
repairs that gap and the reproduced historical-date / requested-versus-returned
city gaps at checkpoint `f959fcb4b387b9aa5bee5b492d0301809e08191c`.
Five added tests first produced 37 failed assertions with zero errors; the
corrected 95-test focused gate passed. They were propagated to combined checkpoint
`ed4b1d6752e5f65ad33a85795c9876681c5bf05d`, whose full gate ran 1,859 tests
with 25 failed assertions and zero errors. Twenty-four failures exposed original
calendar fixtures changing the question month without changing August identity,
forecast and resolution evidence; one expected a numeric-plausibility reason for
an unproven requested city. The source-owning feature corrects only those fixtures,
keeps all original counterexample guards/assertions, and reruns the full combined
gate. Its 47-test fixture/negative-control rerun passed without failures/errors.
Fresh independent review must cover these fixture changes; no RED gate is waived.
Earlier approvals/full-gate counts do not approve the corrected tree.
The source dossier and durable review logs retain the complete attempt history.
The fixture-aligned combined checkpoint
`3e5a748bcd5ba66f574bb506f61a13e343e72ec3` subsequently passed **1,859 tests,
zero failures/errors**, with the same seven runtime-config skip entries and one
archive exclusion. However, independent `deleg_4f9fd97a` found nested balanced
emphasis still hid unsupported cities in the unchanged parser; this BLOCK also
applies to that fixture-only successor. The source-owning feature adds actual
HIGH/LOW, both-history-reader and emphasis-composition controls (8-method RED,
516 failed assertions, zero errors), then fixes delimiter-run normalization to
reach a fixed point at source checkpoint
`e3c26337579aa71cb63a2ddb26f1d47ef2d743fe`. Its corrected **68-test focused
gate passes**. Propagate that new source checkpoint and require a fresh exact
combined full gate and verdict; no prior pass waives this reviewer blocker.

Final exact review `deleg_95fedb83` **BLOCKED** combined
`283a04b26cd5477da8f13ad5db7fb09124f60070` despite its independently verified
1,862-test full pass. Balanced emphasis over separate metric/location spans,
`***high temperature** **in Sacramento***` and
`___high temperature_ _in Sacramento___`, leaves stranded delimiter text and
still fetches/scores the wrong city. Two independent methods produce eight
failed assertions through actual HIGH/LOW and both historical readers. Accounting,
pagination and stations remain unchanged. The source-owning feature records an
architectural pause rather than a fourth regex patch. A structural Markdown
parser is already installed; the operator is being asked to authorize replacing
the normalization boundary and declaring the dependency explicitly. No product
code or dependency change has been made under that proposal.
Production execution remains held; no operation retry is authorized by review
notifications. Once explicitly authorized, pin the then-reviewed clean imported
source and helper hashes rather than blindly replaying a stale HEAD-pinned call.
A fresh complete collection-cycle audit (catalog pages, identities, geography,
captures/integrity and replay publication) is the final acceptance gate.
