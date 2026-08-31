# SourceWriter collector-root repair

## Handoff

- **Feature branch:** `fix/sourcewriter-collector-root`
- **Base:** `ed76d37` (`docs: close collector recovery handoff`)
- **Integration path:** merge into `beta`, verify, then explicitly promote the resulting `beta` to `main` as requested.
- **Scope:** SourceWriter's derived Source Router promotion pipeline only; no service, timer, activation, retention, or historical-artifact mutation.

## Implemented contract

- Derived artifacts use the canonical collector root `/mnt/data-collection/prediction-bot`, not the code checkout/root disk.
- `PREDICTION_BOT_COLLECTOR_ROOT` is an explicit isolated-test override; production default stays the collector mount.
- SourceWriter defaults to the canonical collector `auto_source_router_history` output root, rejects an explicit checkout-root output, and its disabled handoff template follows the collector path.
- The exporter, outcome binder, and Source Router replay all validate their output directories against the same collector-derived root so the staged promotion chain cannot split writes across disks.

## Evidence

- Focused source promotion, replay input, outcome binding, and collector Source Router replay suites: `PYTHONPATH=. python3 -m unittest tests.test_auto_source_router_promotion tests.test_collector_replay_inputs tests.test_replay_outcome_binding tests.test_collector_source_router_replay -q` — 57 tests, `OK`.
- `git diff --check` — passed.
- New regressions prove default API/CLI output reaches the collector volume and an explicit root-disk output is rejected.
- Independent read-only review: **approved with no concrete blockers**. The reviewer confirmed every changed SourceWriter helper output boundary resolves through `bot.collector_paths.derived_reports_root()`, and that both API/CLI defaults and the disabled config target the collector volume.
- Full suite: `PYTHONPATH=. python3 -m unittest discover -s tests -q` — 977 tests ran with 7 skips; five pre-existing environment failures remain: missing optional `kalshi_python_sync` for `test_kalshi_direct` and `test_prediction_lab_collect`, plus three sweep tests requiring the absent ignored `data/summaries/` directory.
- Python compilation for every changed runtime module/script — passed.

## Deliberately deferred

- The existing promotion documentation records a retention-versus-immutability decision and disabled runtime schedule. This repair does not re-enable it, change its services/timers, prune any artifact, or modify existing generations.

## Next action

Obtain independent review, run the full suite, commit the focused repair, merge it to clean `beta`, verify `beta`, then perform the user-requested `beta` to `main` promotion.