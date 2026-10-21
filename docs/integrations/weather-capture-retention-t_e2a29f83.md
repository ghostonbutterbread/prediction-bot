# Weather capture storage safety — isolated implementation handoff

Owner: t_e2a29f83. Branch: `fix/weather-capture-retention-t_e2a29f83`, fetched base and intended integration target `origin/beta` at `7d72c9d74e7dace5f498d85deda33906f961beb1`. CLOSED / APPROVED for code integration only: independent review t_4088555e approved implementation checkpoint `5235561b651ea80fd0ab6cf91cebb3fadbd5fa9c` / tree `d9ae8e95440309443a3c44efbcd5a99ca814575a`; independent combined review t_cc0b2274 approved exact integration tree `c40e7bbfa0afb61e7648e90099c16faa63835b6c` on 2026-10-21 (focused 367/full 1774, zero failures/errors, seven absent-runtime-config skips and one exact production-archive exclusion). This later checkpoint changes only this dossier; exclude it on beta and preserve the exact approved product tree. Neither approval authorizes activation or production-root action. Operator-controlled canonical accounting preflight remains mandatory.

## Contract

`prediction_lab.weather_capture_storage` accepts integer `max_bytes` (>0) and `min_free_bytes` (>=0). Enabled `mode: collector` plus `observer_mode: true` defaults to 2147483648 and 10737418240 respectively; explicit values override individually. All other modes retain uncapped behavior unless explicitly configured. Values are checked before any collection, including receipts without a body. `CaptureRetentionError` terminates the collector's current pass as an actionable storage failure; it cannot be turned into an unavailable forecast.

One lock and compact derived accounting record in `data/weather_captures` cover blobs and both capture/collection receipts across cohorts/cities. Admission conservatively budgets the exact new object's size twice (immutable publication plus temporary copy) and 16 KiB for lock/accounting/atomic replacement overhead; available filesystem bytes must exceed reserve plus that peak. Deduplicated blobs cost no new bytes; each receipt is distinct. There is no retention expiry, historical pruning, source publisher or archive scan. Snapshot/runtime state expose `weather_capture_storage` with effective max, reserve, accounted object bytes and free bytes; `storage_usage`'s existing snapshot/prediction cap remains separate.

First capped use initializes accounting only for an empty capture root. An existing uncapped evidence corpus without sidecar must be inventoried offline and reconciled by a separately reviewed migration; do not delete or blindly seed a zero baseline. A stopped write's single pending locator and exact digest permit bounded recovery of the one object and uniquely owned temp; interruption recovery does not traverse source archives. Directory metadata and a sidecar checksum detect common out-of-band changes; the store requires exclusive managed writers. Arbitrary external in-place file mutation is not proven absent by this compact account: inspect offline and do not reset the sidecar or override the guard to proceed. A legacy uncapped writer encountering an active accounting sidecar fails closed.

## Verification and activation boundary

RED reproduced missing store-limit constructor and configured-Lab attributes. GREEN synthetic tests cover boundaries, dedup/receipt accounting, concurrent stores/processes, reserve, crash-like failed publication, retry, invalid config, preexisting unaccounted evidence, and named failures; final tests select temporary roots and mocked HTTP paths are not live collection. Run focused suites and the repository's broad permitted offline gate before review. No source/history rewrites, service/config changes, beta merge/push, runtime activation or live trading. Controlled collector-only activation is separately owned by the root task after independent review and real empty-root/preflight verification.

The first broad offline run (1759 tests; one failure) exposed an old collector
fixture whose temporary `data_dir` omitted `runtime.storage_root`; its default
collector-root accounting read failed on a stale checksum. No production data
was inspected or removed. The fixture now selects its temporary root, and the
test runner also temporarily substitutes the default collector root and rejects
network connections. The final isolated broad gate passed 1761 tests,
seven expected skips, one real-archive test excluded; the final focused gate
passed 91 tests. Command from this worktree:
`PYTHONPATH=.:tests <beta-venv>/bin/python ../offline_suite.py`
and with `test_weather_capture_store test_prediction_lab_collect
test_weather_capture_consumer test_weather_capture_failure_provenance` arguments
for the focused gate. See workspace `offline_suite.py`,
`offline_suite_final2.log`, `focused_final2.log`. An operator must separately verify the canonical production
capture root's accounting status before any activation; never reset/delete a
sidecar or conclude an existing corpus is empty from these synthetic tests.
