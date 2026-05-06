# Prediction Lab Shared Pipeline Spec

_Last updated: 2026-04-29_

## Purpose

Prediction Lab should stop being a separate decision implementation.

It should become a research wrapper around the same core market-decision pipeline used by paper and live. The mode should determine **what happens after the decision**, not redefine how the decision is made.

This spec defines the intended shared architecture for:

- Collector mode
- Replay mode
- Paper Lab / opportunity mode
- Paper portfolio mode
- Live mode

The goal is to maximize shared logic, avoid duplicated gates, and make every mode explainable from the same artifacts.

---

## Core Thesis

The bot should have one canonical pipeline for answering:

> Given this market, source context, account state, and execution snapshot, would the bot buy, skip, or reject — and why?

That pipeline should be reusable by all modes.

Mode-specific behavior should be adapter-level:

- where market data comes from
- whether source data is live, recorded, historical, or synthetic
- what account state is used
- whether an approved decision is executed, simulated, or only recorded
- how resolution/PnL is later attached

---

## Terminology

### Collector mode

A data-capture mode.

It scans markets and records everything needed for future analysis, including source inputs and decision artifacts. It does not trade.

### Replay mode

A forensic backtest mode.

It re-runs one or more logic versions against previously collected/as-of data to compare outcomes, missed wins, bad buys, reason-code changes, and source validation effects.

### Paper Lab / opportunity mode

A scale simulation mode.

Each market/opportunity is evaluated independently with a fixed per-opportunity bankroll, e.g. `$100`, so Kelly sizing and gates can be tested without global capital exhaustion hiding useful signals.

### Paper portfolio mode

A realistic forward simulation mode.

It uses one shared simulated account balance, exposure caps, event caps, open positions, settlement, and portfolio constraints.

### Live mode

Real execution mode.

It must share the same decision logic but use live exchange/account truth and real execution lifecycle handling.

---

## Current Problem

Prediction Lab has been useful, but it can drift from paper/live because it has historically done its own simplified scoring and hypothetical recording.

Known gaps discovered during archive/weather replay work:

- Prediction Lab has called `strategy.analyze_market(market, None)` without the same order-book/execution snapshot semantics used by paper/live.
- Replay can accidentally use current/live source data for old markets unless source snapshots are explicitly recorded or historical adapters are used.
- Prediction rows may capture raw strategy direction without fully representing shared-core approval/rejection gates.
- Flat hypothetical sizing can hide whether Kelly/shared sizing would actually approve a trade.
- Weather/source validation must be recorded as first-class data, not reconstructed later.

These are not reasons to abandon Prediction Lab. They are reasons to make it a wrapper around the shared decision pipeline.

---

## Reviewer Reality Check — Current Repo Constraints

The desired architecture is correct, but the first implementation slice must respect current repo shape.

Important constraints from review:

1. `EnhancedStrategyEngine.analyze_market(...)` currently does too much inside one call:
   - fetches live/source data
   - validates signals
   - filters/rejects signals
   - ensembles accepted signals
   - may return `None` before shared-core decision logic sees anything

   Therefore the spec cannot assume `raw_signal` and `validated_signal` are already separately observable. A strategy trace API must come first.

2. Prediction Lab currently synthesizes generic skip rows when strategy returns `None`.

   That loses whether the skip came from no signal, validator rejection, edge below threshold, confidence below threshold, weather veto, stale source, missing order book, etc. Early collector artifacts must explicitly add strategy-level skip reasons before replay can become high fidelity.

3. Shared core already has interfaces and dataclasses.

   The build should reuse existing `AccountState`, `TradeContext`, `TradeDecision`, execution snapshot helpers, state adapters, and risk/Kelly wiring rather than inventing parallel abstractions.

4. Live mode has safety gates around the shared decision, not only after it.

   Live-specific reconciliation, runtime invariants, duplicate-intent checks, identity checks, and execution revalidation must remain adapter/safety-gate owned. The shared decision runner must not weaken or bypass them.

5. Prediction Lab v1 already has operational constraints:
   - one configured group at a time
   - collector ledger paths
   - `score_only`
   - `record_all_scored`
   - owner locking
   - storage caps
   - observer semantics

   For replay-grade production collection, `score_only: true` means the replay target is the append-only market snapshot ledger. `collector_record_predictions` only has effect when `score_only: false`, which should be an intentional choice to open Prediction Lab prediction rows.

   Early integration must be additive and backward-compatible with these constraints.

6. Replay source injection is not a solved boundary yet.

   The current archive replay used weather-engine injection/monkey-patching to avoid live-current weather. A real `SourceSnapshotProvider` boundary should replace this later, but it should not be assumed in Phase 1.

---

## Shared Pipeline

All modes should use this conceptual sequence:

```text
MarketProvider
  -> MarketNormalizer
  -> OrderBookProvider / ExecutionSnapshotProvider
  -> SourceContextProvider
  -> StrategyEngine
  -> SignalValidator
  -> TradeContextBuilder
  -> SharedCoreDecisionEngine
  -> Sizing/RiskPolicy
  -> ModeAdapter
  -> Audit/DecisionArtifactWriter
```

### Canonical decision path

1. Fetch/receive a market candidate.
2. Normalize market fields.
3. Fetch or load order book / tradable price snapshot.
4. Fetch, load, or replay source context.
5. Generate strategy signals.
6. Validate source freshness, agreement, station/date alignment, staleness, and quality.
7. Build a `TradeContext`.
8. Call shared-core `build_trade_decision(...)`.
9. Produce a `DecisionArtifact`.
10. Let the mode adapter decide whether to record, simulate, execute, or skip.

### Passive Execution Feasibility Snapshots

Collector artifacts must distinguish "the book used by decision logic" from "the trade still looked executable after decision logic finished."

For shared-pipeline Prediction Lab collection:

- `pre_logic_order_book_snapshot` records the passive book read used before strategy and shared-core logic.
- `decision_latency_ms` records elapsed decision time before the optional post-logic read.
- BUY candidates get a second passive `post_logic_order_book_snapshot`.
- BUY candidates also get `execution_feasibility`, which compares same-market/open status, same-side ask presence, ask unchanged or within configured slippage, quantity sufficiency when size is available, and total elapsed time against the configured threshold.

This is still observer-only metadata. It must not call an execution adapter, reserve cash, place orders, or mutate paper portfolio state.

Replay and validation should treat passing `execution_feasibility` as stronger execution evidence for newly collected BUY rows. Legacy rows with only one recorded book can still be used for coverage and logic inspection, but should not be promoted to strict execution-feasible replay rows.

---

## Shared Modules to Introduce or Consolidate

Implementation rule:

> Prefer wrapping existing shared-core modules before introducing new names.

The names below describe roles. If the repo already has an equivalent shape, reuse it instead of creating a duplicate abstraction.

### `MarketDecisionRunner`

Single orchestration entry point for one market.

Proposed API:

```python
@dataclass
class MarketDecisionInput:
    market: Market
    account_state: AccountState
    order_book: dict | None
    source_context: dict
    mode: str
    config_snapshot: dict
    as_of: datetime | None = None

@dataclass
class MarketDecisionArtifact:
    market_id: str
    mode: str
    observed_at: str
    as_of: str | None
    raw_signal: dict | None
    validated_signal: dict | None
    source_context: dict
    order_book: dict | None
    execution_snapshot: dict | None
    trade_context: dict
    shared_core_decision: dict
    final_action: str
    final_reason_code: str | None
    warnings: list[str]
    config_hash: str
    logic_version: str
```

`MarketDecisionRunner.run(input) -> MarketDecisionArtifact`

This module owns no persistence and no execution. It only returns the artifact.

In the first build slice, this may be named `DecisionPipelineEvaluator` if that better reflects a wrapper around existing shared-core `build_trade_decision(...)` and paper/live context builders. It should not bypass existing shared-core input types. It should adapt into them.

### Strategy trace mode

Before high-fidelity collector/replay can exist, strategy needs an inspectable trace path.

Proposed minimal trace output:

```python
@dataclass
class StrategyTrace:
    raw_signals: dict[str, dict]
    validation_results: dict[str, dict]
    accepted_signals: dict[str, dict]
    rejected_signals: dict[str, dict]
    ensemble_signal: dict | None
    skip_reason_code: str | None
    warnings: list[str]
```

This can be added without changing live behavior by either adding an optional `analyze_market_with_trace(...)`, or returning trace metadata behind a config/debug flag.

Prediction Lab collector should use this trace path first. Without it, replay can only explain shared-core rejects, not strategy-level rejects.

### `DecisionArtifactWriter`

Writes canonical artifacts to JSONL.

Used by:

- Collector mode
- Replay mode
- Paper mode
- Live audit mode

### `SourceSnapshotProvider`

Interface for source data.

Implementations:

- `LiveSourceSnapshotProvider`
- `RecordedSourceSnapshotProvider`
- `HistoricalSourceSnapshotProvider`
- `NoSourceSnapshotProvider` for markets with no relevant external source

### `AccountStateProvider`

Implementations:

- `LiveAccountStateProvider`
- `PaperPortfolioAccountStateProvider`
- `FixedOpportunityAccountStateProvider`
- `ReplayRecordedAccountStateProvider`

### `ExecutionAdapter`

Implementations:

- `NoopExecutionAdapter` for collector/replay
- `PaperExecutionAdapter` for paper positions
- `LiveExecutionAdapter` for real exchange orders

---

## Mode Semantics

### 1. Collector mode

Purpose:

> Record reality as close to the bot's decision moment as possible.

Collector should use:

- live market scan
- live order book snapshots
- live source snapshots
- canonical shared pipeline
- no execution

Collector should record:

- market fields
- exchange/order book snapshot
- every source used, including raw-ish compact payloads where safe
- source timestamps
- staleness/freshness result
- weather station/date/source validation result
- raw signal
- validated signal
- shared-core decision
- sizing output
- skip/approval reason code
- config snapshot/hash
- code/logic version

Collector should **not**:

- place trades
- mutate account state
- rely on future source data
- omit skipped decisions

Collector output is the primary dataset for replay.

---

### 2. Replay mode

Purpose:

> Re-run logic against recorded/as-of evidence to understand cause and effect.

Replay should use:

- recorded market snapshots
- recorded order book snapshots
- recorded source snapshots when available
- historical adapters only when recorded source snapshots do not exist and the run explicitly allows them
- fixed logic/config version supplied by the replay command
- same `MarketDecisionRunner`

Replay should answer:

- Did new logic reduce bad buys?
- Did new logic recover missed wins?
- Did stricter gates over-filter?
- Which reason code changed?
- Which source validation changed?
- Was the decision relying on post-facto data or as-of data?

Replay must label source mode:

- `recorded_as_of`
- `historical_post_facto`
- `live_current_forbidden`
- `synthetic`
- `missing`

Replay should also label execution snapshot mode:

- `book`
- `signal_price_fallback`
- `missing`
- `recorded_book`
- `synthetic`

This matters because paper parity and live decision context can differ depending on whether prices came from a tradable book or a fallback signal price.

Replay should fail or warn loudly if it would use current live data for historical markets.

Replay does not simulate portfolio capital unless explicitly requested. Its default unit is an observed decision snapshot.

---

### 3. Paper Lab / opportunity mode

Purpose:

> Stress-test current logic at scale without account scarcity hiding decision quality.

Paper Lab should use:

- live market scan or archived scan
- canonical shared pipeline
- fixed per-opportunity bankroll, e.g. `$100`
- Kelly sizing within that per-opportunity bankroll
- no shared capital exhaustion

This answers:

> If this opportunity appeared in isolation, would our sizing/gating be good?

It should record:

- decision artifact
- Kelly fraction
- position size from fixed bankroll
- would-buy/would-skip
- eventual hypothetical PnL

This is useful for broad market learning and candidate discovery.

---

### 4. Paper portfolio mode

Purpose:

> Simulate the bot operating forward with realistic capital constraints.

Paper portfolio should use:

- canonical shared pipeline
- one shared simulated account
- open positions
- reserved capital
- event/family exposure caps
- settlement/reconciliation

This answers:

> If we let the bot run as an account, what would happen?

This mode is closer to live than Paper Lab / opportunity mode, but slower for large learning experiments because capital constraints intentionally suppress many opportunities.

---

### 5. Live mode

Purpose:

> Place real orders safely with exchange/account truth.

Live should use:

- canonical shared pipeline
- live exchange order book/account truth
- live lifecycle/reconciliation adapters
- strict caps
- real execution audit artifacts

Live does not own separate strategy logic.

---

## Source Snapshot Contract

Every source signal should record enough data to make replay explainable.

Minimum fields:

```json
{
  "source_id": "src_station_knyc",
  "source_type": "weather_station",
  "provider": "noaa_daily_summaries",
  "mode": "recorded_as_of",
  "fetched_at": "2026-04-29T21:00:00Z",
  "source_timestamp": "2026-04-29T20:55:00Z",
  "target_date": "2026-04-29",
  "staleness_seconds": 300,
  "ttl_seconds": 900,
  "validation": {
    "date_match": true,
    "station_match": true,
    "source_quality": "settlement_station_official_daily",
    "reason_code": "dates_match"
  },
  "compact_value": {
    "high_temp_f": 91,
    "low_temp_f": 73,
    "threshold": 90,
    "question_side": "above"
  }
}
```

For non-weather sources, use the same envelope:

- source identity
- as-of timestamp
- fetched timestamp
- validation/freshness result
- compact normalized value
- warnings

---

## Replay Safety Rules

1. Replay must never silently call current live APIs for historical markets.
2. If replay uses post-facto data, mark it as `historical_post_facto`.
3. If replay uses collector-recorded data, mark it as `recorded_as_of`.
4. If required source data is missing, record `source_missing` and decide according to shared-core behavior.
5. All replay outputs must include logic/config version.
6. Replay comparisons must report both original and replayed decisions.

---

## Decision Artifact Contract

Each mode should record a canonical artifact with these sections:

```json
{
  "artifact_version": 1,
  "mode": "collector|replay|paper_lab|paper_portfolio|live",
  "run_id": "...",
  "market": {},
  "order_book": {},
  "source_snapshots": [],
  "strategy": {
    "raw_signal": {},
    "validated_signal": {},
    "warnings": []
  },
  "trade_context": {},
  "shared_core_decision": {
    "approved": false,
    "action": "SKIP",
    "reason_code": "hidden_gem_probability_multiple_below_min",
    "edge": 0.04,
    "confidence": 0.52,
    "kelly_fraction": 0.0,
    "position_size_usd": 0.0
  },
  "mode_result": {
    "executed": false,
    "simulated": false,
    "recorded_only": true
  },
  "audit": {
    "config_hash": "...",
    "logic_version": "...",
    "code_ref": "..."
  }
}
```

---

## Build Phases

### Phase 0 — Freeze semantics with tests/specs

- Add this spec.
- Add reviewer feedback.
- Identify current duplicate decision paths.
- Define artifact schema tests without full migration.
- Add explicit notes about current Prediction Lab v1 constraints and strategy trace requirements.

### Phase 1 — Strategy trace + shared decision evaluator MVP

- Add strategy trace mode or equivalent trace metadata.
- Introduce `DecisionPipelineEvaluator` / `MarketDecisionRunner` as a thin wrapper around existing shared-core types.
- Wrap current strategy + shared-core decision path without changing live execution.
- Use existing shared-core interfaces and execution snapshot helpers where available.
- Record execution snapshot source: book, fallback, missing, recorded, synthetic.

Exit criteria:

- Unit tests prove one market can be evaluated through the runner.
- Runner returns strategy trace, order book/execution snapshot, trade context, shared-core decision, and final reason where currently observable.
- If strategy returned `None` before trace support, artifact labels it `strategy_returned_none_untraced`.

### Phase 2 — Prediction Lab collector uses runner

- Replace direct `strategy.analyze_market(...)` decision behavior in Prediction Lab with `MarketDecisionRunner`.
- Collector records decision artifacts for buys and skips.
- Keep old output fields backward-compatible.
- Do not replace existing `predictions.jsonl` / `market_snapshots.jsonl` shape yet; write canonical artifacts additively beside or inside backward-compatible fields.

Exit criteria:

- Existing Prediction Lab tests pass.
- New tests show hidden-gem shared-core gate appears in Prediction Lab artifacts.
- Collector records source snapshots and order book snapshot.

### Phase 3 — Replay mode uses recorded artifacts

- Replay from recorded collector artifacts.
- Compare old decision vs replayed decision.
- Add source-mode labels.
- Forbid accidental current live source calls in historical replay.

Phase 3 should not be treated as complete until source injection exists. Before that, replay can use historical adapters, but must label any non-recorded source as post-facto or synthetic.

For the current hidden-gem lane work, Phase 3 must specifically support comparing:

1. pre-hotfix / current branch behavior
2. 2026-05-06 hotfix bridge behavior
3. proposed evidence-card behavior

on the same recorded candidate set.

Replay reports should include, at minimum:

- bad bucket buys removed
- winners skipped by the hotfix bridge
- hidden-gem approvals/rejections by `shape x tier x reason_code`
- bucket rows with and without `distribution_probability`
- threshold slices such as:
  - `distribution_probability >= entry_price + 0.05`
  - `distribution_probability >= 3x entry_price`
  - combined gate pass/fail
- strict-vs-coverage separation so legacy/incomplete rows do not masquerade as strategy truth

Exit criteria:

- Replay can run old vs new logic on the same artifact set.
- Report shows missed wins, bad buys, changed reason codes, and source-data mode.
- For weather hidden-gems, report also shows whether 24x-style bucket opportunities remain possible under evidence-card gates, rather than being blanket-disabled.

### Phase 4 — Paper Lab opportunity mode

- Add fixed per-opportunity account state provider.
- Run Kelly sizing against `$100` per market or configurable amount.
- No shared capital exhaustion.

Exit criteria:

- One candidate uses isolated bankroll.
- Kelly size is visible.
- Result does not mutate a portfolio account.

Implementation checkpoint 2026-04-30:

- Phase 3 replay criteria are covered by `bot/prediction_lab_replay.py`, `scripts/prediction_lab_replay.py`, and `tests/test_prediction_lab_replay.py`.
- Phase 4 is implemented through `FixedOpportunityAccountStateProvider`, explicit `paper_lab` / `opportunity` metadata, configurable `prediction_lab.opportunity_bankroll_usd`, and Prediction Lab row/artifact fields showing isolated bankroll, Kelly sizing, and `mutates_portfolio_account: false`.
- Verification gate: `PYTHONPATH=. pytest -q tests/test_prediction_lab_collect.py tests/test_prediction_lab_replay.py tests/test_decision_pipeline.py`.

Implementation checkpoint 2026-05-04:

- Replay CLI exists and supports `--live-source-policy`, `--require-recorded-source`, `--row-quality-policy`, `--summary-output`, and `--grid-output`.
- Replay result summaries include strict-vs-coverage separation, excluded reasons, source modes, order-book modes, changed decisions, missed wins, bad buys removed/added, and grid output.
- Current data proves why this separation matters: early/legacy rows are incomplete and can produce misleading P&L/replay conclusions if mixed with strict rows.
- A sampled replay of `500` current prediction rows with `row_quality_policy=annotate` produced `0` strict rows because source/order-book/weather snapshots were missing for that sample. That is a data-quality signal, not a strategy conclusion.
- Newer collector snapshots do contain shared-pipeline decision artifacts, but the next required checkpoint is to verify populated `weather_source_snapshot`/recorded source and order-book/execution snapshot fields on live collector rows.
- Until strict rows are available, Prediction Lab replay should be used for coverage diagnostics and reason-code plumbing, not final P&L claims.

Implementation checkpoint 2026-05-06 — hidden-gem/bucket evidence direction:

- A hotfix was shipped to `main` / active runtime to stop the worst cheap weather bucket failures while the lane work continues. It should be treated as bridge behavior, not final Prediction Lab strategy.
- The hotfix/paper audit found that old cheap bucket rows could look like 20x–40x opportunities while still resolving poorly. This reinforces that Phase 1 artifacts must capture the evidence behind a probability, not only the final model probability.
- Phase 1 trace/artifact work should now explicitly include hidden-gem evidence-card fields when available:
  - weather shape
  - hidden-gem tier / probability multiple
  - `distribution_probability`
  - forecast mean and spread/uncertainty
  - distance to bucket center or tail threshold
  - station mapping quality
  - source agreement/freshness
  - source mode (`recorded`, `live`, `historical`, `synthetic`, `missing`)
  - reason code for approve/reject/resize
- Phase 1 is not complete for weather hidden-gem analysis until collector rows can explain *why* a bucket or tail hidden gem passed/failed. Strategy trace alone is insufficient if it lacks source/evidence quality fields.

### Phase 5 — Paper portfolio/live alignment

- Paper portfolio and live use the runner for the same pre-execution decision artifact.
- Existing live lifecycle/reconciliation remains adapter-owned.

Exit criteria:

- Paper/live share decision artifact shape.
- Execution remains mode-specific.
- Live safety gates are not weakened.

---

## Non-Goals

This spec does not require:

- immediate live rewrite
- immediate replacement of all existing Prediction Lab files
- trusting post-facto weather data as live edge
- removing existing paper/live safety logic
- merging opportunity-mode and portfolio-mode semantics

---

## Key Risks

### Risk 1 — Replay contamination

Replay may accidentally use current source data.

Mitigation:

- source-mode labels
- fail/warn on live-current source in historical replay
- record source snapshots during collector mode

### Risk 2 — Artifact bloat

Recording too much raw source data can become expensive.

Mitigation:

- store compact normalized source values by default
- optionally store raw payload behind debug/config flag
- enforce storage caps

### Risk 3 — Shared runner becomes too large

A single runner could become a god object.

Mitigation:

- runner orchestrates only
- providers/adapters own IO and environment-specific behavior
- shared core owns decision semantics

### Risk 4 — Paper Lab confused with portfolio simulation

Opportunity mode can overstate capacity because each candidate gets isolated bankroll.

Mitigation:

- name it clearly: `paper_lab` / `opportunity_mode`
- report separately from `paper_portfolio`
- never use opportunity-mode results as portfolio-capacity proof

---

## Recommended Immediate Implementation Slice

Build only traceability + a thin shared-core evaluator first.

Concrete first slice:

1. Add strategy trace support:
   - raw source signals
   - validation outcomes
   - rejection reason codes
   - accepted signals
   - final ensemble signal
2. Add `bot/decision_pipeline.py` with a thin `DecisionPipelineEvaluator` that reuses existing shared-core types.
3. Add minimal snapshot wrappers:
   - order book / execution snapshot source
   - source snapshot envelope builder
   - fixed-opportunity account state builder
4. Add Prediction Lab config:
   - `prediction_lab.use_shared_pipeline: true`
   - default off until verified, then promote to default
5. In Prediction Lab, when enabled:
   - call runner
   - write artifact
   - maintain old prediction/snapshot fields for compatibility
6. Add tests:
   - hidden-gem gate is applied through shared-core
   - missing order book is represented in artifact
   - source snapshot appears in collector artifact
   - strategy-level skip reasons are recorded when trace is available
   - untraced strategy `None` is explicitly labeled
   - replay cannot use live-current source without explicit opt-in once source injection exists

This keeps the build controlled and makes the architecture real without destabilizing live.

---

## Bottom Line

Prediction Lab should not be rebuilt as another bot.

It should be a high-fidelity observer, replay engine, and opportunity simulator around the same decision pipeline used by paper and live.

That gives us:

- collector mode for data aggregation
- replay mode for cause/effect backtesting
- paper lab mode for isolated high-scale opportunity testing
- paper portfolio mode for realistic account simulation
- live mode for real execution

All sharing the same core decision logic.
