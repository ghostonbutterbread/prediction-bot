# Auto populate Source Router history (beta)

## Handoff

- **Feature branch:** `feat/auto-source-router-promotion`
- **Base:** `d41bb473` (`docs: remove merged fee-aware lane dossier`)
- **Intended integration target:** `beta`
- **Status:** branch-local, derived-only implementation; not merged, pushed, scheduled, or activated.
- **Implementation checkpoint:** `6ab4655f7bc53257203cb39f1505e62d71bb9abf` (`fix: publish strict paper source router scorecard`)

## Contract

`auto_populate_source_router_history` and
`scripts/auto_populate_source_router_history.py` form the single named,
post-resolver pipeline.

```text
immutable beta collector snapshots
  -> sanitized sealed replay inputs (existing exporter)
  -> exact bindings to independently finalized strict resolutions (existing binder)
  -> strict source-observation ledger (existing strict materializer)
  -> settled Source Router history + independent collapsed scoreboard
```

Inputs are explicit `--collector-snapshots` and `--strict-resolutions` paths;
the intended cohort is
`/mnt/data-collection/prediction-bot/data/beta_shadow/forward_20260829T063405Z_beta_strict_source`.
The scheduled resolver remains an independent prerequisite. The pipeline does
not invoke a resolver or fetch a result.

Each output is a hash-addressed generation under an explicit root below
`data/derived_reports`. A byte-identical repeat returns the completed generation
without rewriting it. Inputs are read once then copied into the generation, so
the hashes, replay export, binding, and provenance all name the same immutable
bytes. Work is built in a private staging directory and atomically renamed only
after both manifests are complete; a failed staging attempt is never consumable
as a generation and the same inputs can be retried. The manifest records input
hashes, chronology, artifact paths, status, and:
`pending`, `unresolved`, `invalid`, `eligible`, and `collapsed` counts.

### Runtime handoff

The generated `source_history_manifest.json` follows the existing verified
`source_history_manifest` contract: it hash-binds the strict ledger, exact
materialized strict-resolution source, materialized raw archive, replay index,
and replay-export manifest. The intended consumer is
`bot.weather.collector_source_router_replay.run_collector_source_router_replay`
with `history_manifest_path` (and the exact matching `history_ledger_path`),
not the report-only replay helper. The generated
`source_observations/settled_source_correctness.jsonl` is that strict history
ledger. The separate
`source_router_scoreboard/strict_independent_source_history.jsonl` supplies
one outcome-blind independent observation per source/event slice for audit and
sample accounting. The generation manifest names both paths.

## Boundaries

- Reads immutable collector snapshots; does not modify raw archives, replay
  inputs, resolutions, collector state, orders, paper wallets, or trading.
- Uses only the strict canonical exporter, exact outcome binder,
  `source_observation_ledger`, and strict history-collapse components.
  It deliberately does **not** use the loose legacy `source_performance`
  materializer.
- The binder fails closed for missing, malformed, or ambiguous strict
  resolutions. The source ledger excludes malformed, ambiguous, unproven, and
  incomplete strict-source evidence from history.
- Outcome fields remain in separate finalized/bound and settled derived
  artifacts; they are never written to raw snapshots or sanitized replay inputs.
- History availability is `settlement_ts`; the runtime must include a row only
  when `settlement_ts < later_decision_time`.
- `history_ready` means only that a derived ledger has eligible rows. It is not
  a live result, lane promotion, activation, or trading claim.

## Test receipts

1. **Original RED (before feature implementation):**
   `python3 -m unittest tests.test_auto_source_router_promotion.AutoSourceRouterPromotionTests.test_no_resolutions_writes_no_router_history`
   failed as expected with `ModuleNotFoundError: No module named
   'bot.auto_source_router_promotion'`.
2. **Original focused green:**
   `python3 -m unittest tests.test_auto_source_router_promotion -v` — 6 tests,
   `OK` (no resolutions; exact strict settlement with provenance/chronology;
   existing router loader consumption; idempotent repeat; malformed/ambiguous/
   unproven exclusion; CLI count report).
3. **Adjacent green:**
   `python3 -m unittest tests.test_collector_replay_inputs tests.test_replay_decision_input tests.test_source_observation_ledger tests.test_source_history_manifest tests.test_weather_source_router -v`
   — 81 tests, `OK`.
4. **Repository suite attempted:** `python3 -m unittest discover -s tests` —
   1,020 tests run, 7 skipped, 4 environment/pre-existing errors: missing
   optional `kalshi_python_sync` for `test_kalshi_direct`, plus three
   `test_paper_shadow_lane_composition_sweep` tests requiring the absent ignored
   `data/summaries/` directory. The feature's focused/adjacent tests passed.
5. `git diff --check` (including staged feature paths) — passed.

## Rejection repair

Independent review rejected `9d375922` because it emitted only a custom
promotion manifest, hashed caller paths before downstream rereads, and created
the final generation before successful completion. This follow-up resolves
those blockers by emitting the contract-owned verified source-history manifest,
materializing the consumed source bytes before hashing/export, and atomically
publishing a completed staging tree.

**RED receipt:**
`PYTHONPATH=. python3 -m unittest tests.test_auto_source_router_promotion.AutoSourceRouterPromotionTests.test_generation_is_accepted_by_verified_collector_consumer_with_exact_manifest_ledger tests.test_auto_source_router_promotion.AutoSourceRouterPromotionTests.test_promotion_binds_provenance_to_materialized_input_bytes_when_source_changes tests.test_auto_source_router_promotion.AutoSourceRouterPromotionTests.test_failed_partial_generation_can_be_retried_without_publishing_incomplete_history -v`
failed on the pre-fix revision: two missing `history_manifest_path` errors and
one incomplete-final-generation retry rejection.

**Exact green receipt:** `PYTHONPATH=. python3 -m unittest tests.test_auto_source_router_promotion tests.test_collector_replay_inputs tests.test_replay_outcome_binding tests.test_source_observation_ledger tests.test_source_history_manifest tests.test_collector_source_router_replay -v && git diff --check` — 69 tests, `OK`; whitespace check passed.

## Second review repair: strict paper Source Router handoff

The published `scoreboard_path` is now
`source_router_scoreboard/strict_finalized_source_router_scorecard.jsonl`, a
strict aggregate built only from the collapsed `eligible_strict_source_proof`
finalized observations. It has the exact source-router consumer fields:
`source_id`, `source_name`, `city_id`, `market_kind`, `contract_shape`,
`sample_count`, `threshold_sample_count`, `threshold_correct_count`, and
`threshold_direction_accuracy`. Its provenance binds the collapsed strict
history SHA and preserves settlement availability values. The independent
strict-history JSONL remains published separately as an audit artifact.

This is deliberately consumed through the actual paper lane path:
`bot.paper_shadow_lanes._source_router_decision` ->
`load_scoreboard_rows` -> `build_source_confidence_row`; it does not use the
loose legacy materializer. `config.paper_source_router_auto_population.yaml`
is a committed handoff template with both `paper_shadow_lanes.enabled` and
`shadow_source_router.enabled` false and no configured scorecard path.

Before publishing, JSON helper metadata paths are rebased from the random
private staging directory to the deterministic generation directory. This
happens before the verified source-history manifest hashes the replay metadata,
so the existing byte materialization, hash verification, atomic rename, and
retry contract remain intact.

**RED receipt:**
`PYTHONPATH=. python3 -m unittest tests.test_auto_source_router_promotion.AutoSourceRouterPromotionTests.test_published_strict_scorecard_drives_actual_paper_source_router tests.test_auto_source_router_promotion.AutoSourceRouterPromotionTests.test_published_helper_metadata_never_retains_random_staging_paths -v`
failed before this repair: the collapsed-history row lacked `sample_count` and
published ledger helper metadata retained `/.staging/` paths.

**Green receipts:**

- `PYTHONPATH=. python3 -m unittest tests.test_auto_source_router_promotion -v` — 11 tests, `OK`.
- `PYTHONPATH=. python3 -m unittest tests.test_source_history_manifest tests.test_collector_source_router_replay tests.test_weather_source_confidence tests.test_simulator_source_scoreboard_shadow -v` — 53 tests, `OK`.

## Activation boundary and residual integration requirement

Do **not** schedule this script or change a runtime config/service/timer in this
branch. After independent review and a beta merge, an owner must explicitly:

1. verify a beta cohort collector snapshot path and separately finalized strict
   resolution feed are available;
2. choose/approve a derived output root and invoke the script after the resolver;
3. verify the generation manifest, strict ledger, scorecard, and metadata
   hashes/counts; and
4. copy that generation's `runtime_consumption.scoreboard_path` into an ignored
   fresh-cohort paper-lane config, explicitly set both lane enablement gates,
   and validate chronology in a fresh beta cohort.

That runtime/scheduler wiring and the explicit paper-lane enablement remain the
intentional blocker; neither is implemented here.
