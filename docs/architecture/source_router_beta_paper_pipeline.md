# Source Router Beta Paper Pipeline

**Status:** minimal replay-readiness specification — paper-only beta extension of `main`
**Branch:** `feature/source-router-ev-shadow`
**Last updated:** 2026-08-11

## The one problem being fixed

We already collected historical source forecasts. We also recovered strict final outcomes and authoritative market settlement timestamps. But the router treats outcome history as becoming available when the later backfill job ran (`resolved_at`), not when the market actually settled (`settlement_ts`).

Result: during a historical replay, every earlier candidate sees effectively zero prior resolved source history and the router skips rather than selecting a source.

```text
historical source forecasts + strict outcomes exist
but
later backfill timestamp hides them from earlier replay decisions
therefore
source-router historical warm start routes zero or too few candidates
```

## Minimal goal

Make the existing source-router replay use existing historical source evidence correctly and safely, then run it.

```text
existing July archive + existing strict settlement data
→ only use a source result after its actual settlement time
→ run current router replay
→ inspect route count, selected sources, skips, and exact-resolution P&L coverage
```

This is **not** a claim that the router is profitable. It is the shortest honest way to make the already-collected history usable and learn whether the existing router has enough signal to begin a paper experiment.

## The only required code work

1. **Settlement-time fix**
   - Prefer `settlement_ts` over later `resolved_at` when deciding whether an outcome was known before a replay decision.
   - This is implemented with a regression test.

2. **One small history manifest**
   - A JSON record pointing at the already-existing source ledger, strict resolution ledger, raw archive, and replay index.
   - It records hashes so the replay knows exactly what historical evidence it used.
   - It does not copy, merge, or rewrite archives.

3. **Wire that manifest into the existing replay command**
   - `weather_source_router_replay.py --history-manifest ...`
   - The script loads the verified historical ledger as router history and records the manifest path/hash in its report.

4. **One narrow end-to-end test and one real blind offline replay**
   - Fixture: an older market settles, then a later candidate sees it as valid history.
   - Replay: expose each decision only to the data available at its decision time; hide its resolution until after the router has acted.
   - Replay rows strictly oldest-to-newest. Each decision sees only its own decision-time market/source data and outcomes that had already settled before that decision; its own resolution remains hidden until after the action is fixed.
   - Support two explicit synthetic evaluation modes, neither of which claims to reconstruct a historical production wallet:
     1. **Fixed-stake diagnostic:** a declared stake (for example, $10) per eligible BUY, useful for normalized signal quality.
     2. **Sequential synthetic wallet:** a declared starting balance (for example, $100) evolves oldest-to-newest. Before each decision, apply the declared paper risk/sizing rules to the then-current balance; after the fixed action, apply only the exact final resolution, update balance, and continue.
   - Record starting balance, stake/sizing mode, risk-policy version and inputs, decisions skipped for insufficient balance/risk, fills/prices, per-trade balance before/after, drawdown, and final balance in replay metadata. This yields genuine **model P&L under declared rules**, not historical account P&L.

## What we are explicitly not building now

These are worthwhile only if the replay demonstrates a concrete need; they are deferred:

```text
- new source scoring/ranking mathematics
- a 100-observation hierarchical model
- city/type/family regrouping changes
- source-router becoming stable/default trading logic
- a new lane-component/config framework
- an all-market exploration lane
- new runtime services, timers, cohorts, or live/paper activation
- changes to stable execution, pricing, sizing, or risk logic
```

The existing source-router behavior stays intact. We are repairing its historical input and measuring it before redesigning it.

## Safety conditions

- Offline/paper/replay only.
- Existing archives and prior decisions remain unchanged.
- Exact final resolution identity is still required for any hypothetical P&L.
- A missing settlement timestamp still excludes history; it never gets guessed.
- No live order, wallet, service restart, runtime enablement, or configuration activation.

## Done means

```text
✓ a historical settled result becomes usable before a later candidate
✓ the same result is unavailable before it actually settles
✓ existing source-router replay accepts the verified historical ledger
✓ a real offline replay report states route count, selected sources, skips,
  and exact P&L/resolution coverage
✓ all affected tests pass
```

Then we look at the actual replay results together and decide whether any larger source-selection or lane architecture change is justified.
