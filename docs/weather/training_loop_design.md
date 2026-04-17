# Weather Replay Training Loop Design

## Goal
Use historical weather market data to improve the bot with a **bounded training run** that learns from replay results without blindly poisoning itself.

## Core Idea
Training should be:
- time-bounded
- data-bounded
- reviewable
- safe to stop early

If the trainer runs out of replay data, it ends early.
If it hits the time budget, it stops cleanly.

---

## Proposed Training Workflow

### 1. Select training set
Input can come from:
- local historical Kalshi weather data
- curated replay datasets
- future downloaded datasets from reputable sources

### 2. Build replay records
For each market:
- hide outcome from quiz payload
- preserve answer key separately
- attach city/source registry context

### 3. Run evaluator agent
The evaluator agent:
- consumes quiz payloads
- decides BUY_YES / BUY_NO / SKIP
- sets confidence and size
- returns compact answer records

Possible runtimes:
- Codex CLI (preferred current direction)
- other provider/runtime later if needed

### 4. Score results
After answers are generated:
- compare against answer key
- compute fee-aware P&L
- compute side bias / skip rate / confidence calibration

### 5. Produce candidate learnings
Examples:
- source A in city X is consistently useful
- source B is noisy and should be demoted
- market type Y is overtraded
- confidence threshold too loose/tight

### 6. Reviewer / babysitter gate
A separate reviewer agent should inspect candidate learnings before they modify trust or strategy.

### 7. Apply approved updates
Only approved updates should affect:
- source trust scores
- city source ordering
- possible future strategy parameters

---

## Time-Bounded Training Mode

### Required parameters
- `max_duration_minutes`
- `max_records`
- `max_batches`
- `max_cost` (optional future guard)

### Stop conditions
The training run ends when any of these happen:
1. no more replay data remains
2. max time reached
3. max records reached
4. reviewer blocks further updates
5. operator stops the run

This means the trainer can safely:
- work for 30 minutes
- or 2 hours
- or until it runs out of useful data

---

## Suggested Components

### `bot/weather/training.py`
Should eventually handle:
- dataset selection
- batching
- replay execution
- scoring
- candidate update generation

### `scripts/weather_train.py`
CLI entry point for bounded training runs.

Example future shape:
```bash
python3 scripts/weather_train.py \
  --input data/historical/kalshi.csv \
  --max-duration-minutes 45 \
  --max-records 500 \
  --runtime codex-cli
```

### `data/summaries/weather_training_runs/`
Compact summaries for each training run.

---

## Reviewer / Babysitter Model

The reviewer agent should block bad learning.

### Reviewer checks
- sample size large enough?
- result statistically meaningful enough?
- source quality issue or just variance?
- overfitting to one city/day?
- bad assumptions in fee/fill model?
- contradictory evidence across sources?

### Reviewer outputs
- approve
- reject
- needs more data
- reduce confidence of proposed update

---

## Safe Learning Rules

### Allowed to auto-update
- source trust score within bounded deltas
- source status from watch_only -> secondary only with enough evidence
- confidence notes / review queues

### Not allowed to auto-update yet
- major strategy rewrites
- large threshold shifts
- changing fee model assumptions automatically
- promoting noisy social accounts to trusted primary

---

## Why this matters
This gives us a path to:
- train faster on resolved markets
- inspect how the bot thinks
- improve source trust over time
- avoid learning from garbage

---

## MVP Next Step Recommendation
Before building full auto-training, implement:
1. training run spec
2. bounded batch executor
3. reviewer/babysitter output schema
4. compact training summary format

That keeps the system safe and understandable.
