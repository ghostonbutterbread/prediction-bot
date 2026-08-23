# Source Router Forward Evidence and Wallet Plan

**Status:** proposed implementation plan; paper/observer only
**Owner:** Prediction Lab collector and derived replay artifacts
**Canonical companion:** `collector-first-replay-lane-contract.md` (shared replay and wallet contract)
**Supersedes:** none
**Implementation commit:** pending — reviewed plan only
**Date:** 2026-08-23

## Purpose

Source Router is a source-selection behavior: it supplies evidence for *which
forecast source* should inform a market prediction. It is not itself authority
to trade, nor is it an automatic position-size policy.

This plan closes the evidence loop needed to evaluate Source Router honestly:

```text
immutable observer snapshot
→ exact source-target proof and source observation time
→ separate authoritative market settlement receipt
→ derived strict source-correctness observation
→ only later decisions can use that observation as router history
→ sealed lane decision
→ shared replay wallet/risk accounting
→ report routeability, selection quality, and economic outcome separately
```

The goal is to build a growing forward cohort of strict Source Router-eligible
observations and a canonical paper-only way to ask, for every frozen lane:

- Given a declared starting balance (for example, $50), what decisions fit the
  wallet at their decision times?
- Which decisions are selected, skipped, or rejected by risk policy, and why?
- Does a fixed-notional signal comparison differ from a capital-constrained,
  sequential-wallet comparison?
- Which predeclared market shapes, price bands, sources, and event-exposure
  rules produce acceptable forward evidence?

This is research and paper evaluation only. It does not enable a lane, change
collector trading behavior, place an order, or mutate a real/paper wallet.

## Evidence contract for new collector snapshots

Raw snapshots remain append-only and unchanged. Each **new** captured weather
source forecast must retain, directly in its immutable snapshot payload or in a
content-hashed collector sidecar that references that exact raw row:

```text
snapshot provenance
  raw_row_sha256
  raw_payload_sha256
  archive path + line/index provenance
  shared_snapshot_id + shared_candidate_id

source evidence
  source_id
  source_observed_at / source_as_of
  exact city identity
  target local date including timezone basis
  measurement kind (high/low/etc.)
  contract shape (threshold/range/etc.)
  forecast value or side-compatible forecast expression
  source payload locator/hash

market evidence
  market_id, question, event/root-date identity
  decision observed_at (UTC)
  side-specific executable quote fields when available
```

A later independent resolution receipt must contain:

```text
complete sealed decision identity
market_id
official outcome or VOID
explicit authoritative settlement_ts / outcome_known_at
resolution_id
canonical resolution-row hash and source-file provenance
```

The derived source-observation ledger must mark an observation strictly eligible
only when all of these are true:

1. source target proof has explicit, recorded, non-conflicting city, local date
   (including timezone basis), measurement kind, contract shape, and
   side-compatible forecast components; `unknown` or parser-only inferred
   components are non-eligible;
2. `source_as_of` is a valid offset-aware source-observation timestamp and a
   raw-row/source-payload hash-plus-locator provenance chain is present;
3. an exact authoritative outcome receipt exists;
4. `settlement_ts` is valid; and
5. the row is only exposed to a later candidate where
   `settlement_ts < candidate.observed_at`.

Repeated polls remain immutable raw evidence, but router history will collapse
them conservatively to one independent contribution per
`source × event/root-date × city × kind × shape × side`. The representative is
the earliest valid `source_as_of`, then earliest collector observation, then
canonical raw-row hash. The derived collapse manifest retains every member,
selected representative, and reason. No raw-row volume will be presented as
independent sample volume.

## Forward artifact chain

The collector remains the only shared-market publisher. The following are
separate derived jobs, never collector mutations:

1. **Sanitize replay inputs** from bounded collector snapshots. Inputs exclude
   outcomes, settlement, P&L, and future resolver metadata.
2. **Bind finalized outcomes** by complete sealed decision identity in the
derived receipt. Authority lookup may use a unique `market_id` only when the
strict resolution proves that requested and returned market IDs match and there
is exactly one authority row; ambiguous or `resolved_at`-only matches fail
closed. This is a provenance-preserving authority lookup, not a market-only
decision settlement shortcut.
3. **Materialize source observations** into pending, settled, unusable, and
`void_resolution` JSONL artifacts with hashes and rejection counts.
4. **Build a history manifest** referencing the exact input, outcome, and
   settled-source artifacts plus their hashes.
5. **Run chronological router replay** with only observations whose authoritative
   settlement precedes each candidate.

Every generated report records cohort start/end, artifact hashes, selected and
rejected counts, and explicit blocker reasons. A row with incomplete proof is a
coverage gap, not a negative source score and not a reason to weaken strictness.

## Canonical frozen-lane wallet evaluation

All lanes must use the same frozen candidate universe and emit sealed decision
artifacts before outcomes are visible. Lanes may vary only through their
versioned declared policy components. A lane cannot obtain an advantage by
using a different collector snapshot, a later outcome, or another lane's
action/probability.

Each sealed blind lane intent records: lane ID/config hash; canonical decision
key; selected-side probability; decision-time executable price classification;
edge/confidence; and reason trace. It does not embed stateful risk approval.
The wallet separately emits an account-state-specific execution receipt with
requested Kelly amount, shared risk/retrade result, reservation, and settlement.

Two reports always run over the same sealed intent stream:

1. **Fixed-notional diagnostic** — declared stake per eligible BUY. It measures
   normalized selection economics and has no ending-wallet claim.
2. **Sequential synthetic wallet** — starts with declared paper balance (the
   default comparison is $50), processes UTC chronological decisions, reserves
   approved capital until authoritative settlement, and applies the existing
   shared-core Kelly, fees, risk, duplicate-market, event-exposure,
   overlap/retrade, and drawdown controls.

The sequential wallet emits per-decision receipts including balance/equity,
available cash, reserved capital, open positions, requested/approved stake,
rejection codes, settlement, P&L, and balance after settlement. `VOID` is a
first-class `void_resolution` receipt: it retains exact resolution provenance,
releases reserved capital, and never contributes to source correctness/history,
P&L, or win/loss attribution. Missing executable price, side probability, exact
receipt, or settlement timestamp is an explicit unresolved/skip state, never an
imputed fill or payout.

Until a lane is re-executed via this contract, recorded-decision Kelly outputs
remain **sizing-and-capacity diagnostics**, not paper-parity or promotion
results.

## Evaluation questions and predeclared views

The reports will separate rather than optimize across these dimensions:

- **routeability:** exact eligible history count and candidates reaching 5, 25,
  50, and 100 independent observations;
- **source quality:** settled correctness/calibration by source and target
  slice, with counts and uncertainty;
- **decision quality:** BUY/SKIP/rejection counts, decision-time prices, and
  exact-resolution coverage;
- **wallet economics:** fixed-notional P&L versus sequential-wallet balance,
  drawdown, capital occupancy, and unresolved exposure;
- **risk/cap behavior:** duplicate, event/root-date, overlap, retrade, price,
  balance, and drawdown rejection receipts;
- **market-shape studies:** predeclared shape/price/time-to-settlement cohorts
  evaluated on the same frozen decisions, with no outcome-selected cap changes.

No single historical P&L aggregate will be treated as a source-router result
unless it comes from one identified policy generation and exact replay input
contract. Historical results are diagnostic; only a fresh independently resolved
full-overlap paper cohort can support a promotion decision.

## Implementation slices

### Slice 1 — strict forward source evidence

Add the smallest derived `strict_source_observation_v2` adapter for already
captured new-format source fields. It explicitly validates each required proof
component, valid source timestamp, provenance chain, `VOID`, and deterministic
poll collapse; it does not change collector runtime behavior. Add a coverage
report that counts field presence, dispositions, and collapse reasons. Do not
reinterpret old rows that lack proof.

**Acceptance:** a fixture with complete explicit proof and provenance becomes
one strict settled observation after a valid exact receipt; it becomes visible
only to a later candidate after `settlement_ts`. Missing/conflicting city,
date/timezone, kind, shape, side, timestamp, or provenance produces a named
unusable disposition. `VOID` remains auditable but never becomes source history;
repeat polls emit one selected contribution plus a collapse record.

### Slice 2 — canonical replay artifact runner

Make the existing export → outcome-binding → source-observation materialization
flow a documented, deterministic forward-cohort command/runbook. It writes to a
new derived namespace, refuses overwrite, and emits a hash-verified manifest.

**Acceptance:** one bounded fixture completes the full artifact chain with no
outcome field in replay input and no raw-file mutation.

### Slice 3 — shared-core wallet parity harness

Add a stateful synthetic account adapter around existing shared core decision,
Kelly, and risk interfaces. Re-execute a frozen control lane and one candidate
from identical inputs. Do not route legacy recorded actions through it.

**Acceptance:** a $50 fixture proves capital reservation through later
settlement, duplicate/event/retrade caps, drawdown accounting, and exact
receipt-only settlement.

### Slice 4 — forward observation and A/B reports

Run only after new strict observations resolve. Report threshold maturity
(5/25/50/100) and pair fixed-notional with sequential-wallet outcomes on the
same selected stream. Keep control and candidate rows aligned by shared
candidate/snapshot identity.

**Acceptance:** reports state coverage/blockers before P&L and explicitly mark
any cohort without enough strict evidence as not promotion-eligible.

## Explicit deferrals

- live trading, runtime lane enablement, promotion, or collector duplication;
- changes to current Source Router scoring mathematics or lowering strict sample
  rules just to create more trades;
- retrospective invention of missing historical target/settlement evidence;
- outcome-tuned caps, price bands, source filters, or wallet parameters;
- changing existing active collector configuration as part of code merge.

## Promotion gate

Source Router can be proposed for paper-only behavioral promotion only after a
predeclared forward cohort has: exact strict source history; full shared
candidate overlap with its control; decision-time executable price evidence;
exact authoritative settlements; declared fees/exposure policy; and sufficient
resolved independent events. A positive wallet result alone is not sufficient.
