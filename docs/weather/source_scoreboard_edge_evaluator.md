# Source Scoreboard Edge Evaluator

The weather source scoreboard remains the foundation. It records weather source observations known at decision time and scores source quality. The edge evaluator is a read-only layer on top of those scoreboard/source-outcome rows.

## Why this exists

A weather source can look accurate without creating Kalshi trading edge. For Kalshi weather contracts, the useful question is whether a source predicted the final Kalshi YES/NO outcome better than the observed market price, not whether it was meteorologically impressive in the abstract.

## Data flow

1. Scoreboard/source lane records what each source said at decision time.
2. The source-outcome ledger normalizes each source forecast into a source-implied YES/NO side.
3. After Kalshi finalizes the market, the evaluator joins the official outcome and observed source-side price.
4. Reports summarize win rate, realized binary edge, and flat $1 PnL by source/city/kind/shape.

## Guardrails

- This is not a new trading lane.
- No paper/live accounting is mutated.
- Sources stay scoreboard-only or disabled until Ryushe explicitly approves promotion.
- NWS-derived actual-temperature labels are useful for consistency checks but must not be mistaken for independent edge proof.
