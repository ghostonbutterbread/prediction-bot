# Paper Weather Findings — 2026-05-04

Status: active note  
Owner: Ghost  
Canonical path: `docs/PAPER_WEATHER_FINDINGS_2026-05-04.md`  
Supersedes: none  
Last reviewed: 2026-05-04

## Why this note exists

This captures the current paper-trading findings from the fresh `$100` weather-focused paper run so future agents can quickly understand what is going on before changing strategy logic.

Primary source files reviewed:
- `data/paper/sim_20260504_184102.json`
- `data/paper/risk_state.json`
- `data/paper/audits/open_15_trades_quality_review_20260504T2236Z.md`
- `data/paper/audits/paper_weather_stage_review_20260504T2207Z.md`

## Current state

Paper trading is running and opened a fresh post-reset session:

- Session: `sim_20260504_184102`
- Starting balance: `$100.00`
- Open trades: `15`
- Reserved capital: `$26.56`
- Available cash: `$73.44`
- Resolved trades at review time: `0`
- Bot entered standby due to max positions/capital-headroom rules.

This run is the cleanest near-term readout of the newer paper behavior, but the trades are not resolved yet, so it is not proof of profitability.

## Important strategy finding

The open basket is mixed:

1. **Golden-ish trades** — forecast/source data supports the selected side and model probability is around or above 50%.
2. **Cheap hidden-gem / lottery trades** — market price is tiny, so the bot sees possible positive EV even when model probability is far below 50%.
3. **Direction-mismatch trades** — primary weather forecast points away from the purchased side, but cheap YES still passes hidden-gem logic.

The current basket is therefore useful for learning, but it is not yet a clean “slow consistent growth” portfolio.

## Best-looking open trades

These looked most aligned with a reliability-first strategy:

- `KXHIGHLAX-26MAY05-B66.5` — LA high 66–67 YES
  - model probability: `0.6574`
  - forecast high: `66`
  - source agreement: `0.87`
  - judgment: good / golden-ish

- `KXHIGHAUS-26MAY05-T88` — Austin high <88 YES
  - model probability: `0.6237`
  - forecast high: `86`
  - source agreement: `0.78`
  - judgment: good, though size is relatively large

- `KXHIGHMIA-26MAY05-B86.5` — Miami 86–87 NO
  - direction: `BUY_NO`
  - model probability: `0.7361`
  - forecast high: `80`
  - source agreement: `0.80`
  - judgment: good / golden-ish

- `KXHIGHCHI-26MAY05-T65` — Chicago high >65 YES
  - price: `0.03`
  - model probability: `0.3861`
  - forecast high: `79`
  - source agreement: `0.80`
  - judgment: weather evidence looks strong, but model probability is below 50%; track calibration carefully

- `KXHIGHDEN-26MAY05-T55` — Denver high >55 YES
  - price: `0.05`
  - model probability: `0.4360`
  - forecast high: `73`
  - source agreement: `0.73`
  - judgment: weather evidence looks strong, but model probability is below 50% and size is high

## Questionable pattern: cheap YES when NO is the natural side

A recurring issue is that the bot can buy cheap YES because it appears positive-EV, even when the primary forecast points away from YES.

Example pattern:

- Market: “Will Atlanta high be >85?”
- Forecast high: `79`
- Bot buys `YES` at `0.02`

Reliability interpretation:

- If the forecast says `79`, the natural side is **NO**, not YES.
- Buying YES here is a lottery bet that the forecast is badly wrong.
- Cheap price alone should not make this a normal trade.

This highlights a known weakness: the bot has historically had trouble buying NO and tends to find cheap YES trades instead.

## Proposed policy lanes

### 1. Golden lane
Allow normal sizing only when:

- the forecast/source data supports the selected side
- model probability is >= 50%, or weather-source probability is extremely strong with high source agreement
- market family is explicitly allowed
- station/source mapping is exact or explicitly trusted
- distribution/forecast support is present or the reason for missing distribution support is documented

### 2. Lottery lane
Allow cheap hidden-gem trades only with a tiny capped budget, not normal sizing.

Possible starting cap:
- 5–10% of daily exposure total for all lottery trades combined
- never per-trade unlimited hidden-gem sizing

### 3. Block lane
Block trades where the primary forecast points away from the purchased side unless there is a named override reason.

Examples:
- Forecast high below threshold + market asks above threshold → consider `BUY_NO`, not cheap `BUY_YES`.
- Forecast low above threshold + market asks below threshold → consider `BUY_NO`, not cheap `BUY_YES`.
- Bucket/range markets should require forecast inside/near the range for YES; otherwise consider NO or skip.

## Market allowlist recommendation

For reliability, add an explicit paper/live market allowlist gate.

Current desired behavior:
- The bot should only trade market families we intentionally support.
- While testing weather, non-weather markets should be blocked unless explicitly enabled.
- This prevents ambiguous behavior such as a random 2030 energy market slipping into a weather-focused paper run.

This is now tracked in the project todo as a market-gating task.

## Stage judgment

The bot is safer than the old overextended paper runs, especially around bucket exposure, but it is still in evidence-gathering/calibration mode.

Current conclusion:

- Do not scale based on this run yet.
- Let the 15 open positions resolve.
- Audit the fresh session separately from legacy Prediction Lab hypothetical rows.
- Prioritize side-selection and market-allowlist gates before promoting more autonomy.

## Next actions for future agents

1. After May 5 settlement, audit `data/paper/sim_20260504_184102.json` for actual realized P&L.
2. Split performance by:
   - golden vs lottery lane
   - BUY_YES vs BUY_NO
   - high-temp vs low-temp
   - bucket vs tail_high vs tail_low
3. Implement/verify explicit market allowlist gating.
4. Implement/verify forecast-direction side selection:
   - evidence supports YES → evaluate YES
   - evidence rejects YES → evaluate NO
   - evidence unclear/close → skip or tiny lottery only
5. Keep lottery hidden gems capped separately from normal trades.
