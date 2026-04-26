# Live Canary Readiness Notes

_Last updated: 2026-04-26_

## Current Recommendation

The bot is close enough for a **tiny, supervised live canary**, but not yet ready for unattended live trading.

Suggested framing for first live run:

- Treat it as a flight test, not a launch.
- Fund with a small account balance, e.g. **$100**.
- Start with hard live risk caps that make a bug survivable.
- Keep paper trading running in parallel for comparison.
- Review the first live orders manually before increasing limits.

## Suggested Initial Live Limits

These are intentionally conservative and should be enforced in config before live mode is enabled:

- Account bankroll: **$100**
- Max position size: **$1–$5**
- Max open exposure: **$10–$25**
- Max daily loss: **$10–$25**
- Max concurrent open positions: small fixed cap, e.g. **3–5**
- Prefer **supervised mode** / manual monitoring during the first session.
- Prefer markets with stronger confidence until live order lifecycle behavior is proven.

## What Looks Good

- Shared paper/live decision logic exists through `build_trade_decision()`.
- Paper/live entry policy now uses the same important gate:
  - hidden-gem criteria can allow cheap asymmetric trades;
  - non-hidden-gems require `win_probability > 0.50`.
- Hidden-gem rules are explicit and shared:
  - entry price cap: `<= $0.05`
  - minimum edge: `>= 0.05`
  - minimum probability multiple: `>= 3x market price`
- Recent targeted readiness test batch passed:
  - shared decision logic
  - parity fixtures
  - live execution path
  - runner/live status path
  - risk + simulator behavior
- Paper bot recovered after reboot and continued with existing open positions.
- Current paper run is in standby when capital headroom is exhausted, so it is not blindly piling into more exposure.

## Known Limitations Before Wider Live Trading

### 1. Live order lifecycle hardening

Live is real, but the operational lifecycle is still thinner than paper.

Needs more hardening around:

- partial fills
- rejected orders
- canceled orders
- stale resting orders
- duplicate submissions / idempotency
- transient API errors
- retry behavior for non-terminal exchange failures
- explicit handling for exchange/API inconsistency

### 2. Reconciliation and restart safety

Production readiness depends on safe recovery after bot restarts or host reboots.

Known gaps:

- shared reconciliation contract is not fully defined across paper/live;
- restart tests exist, but need broader live-lifecycle coverage;
- live should prefer exchange truth over local assumptions whenever possible;
- reconciliation mismatch alarms / kill switches need hardening.

### 3. Audit trail parity

Paper has strong audit visibility. Live has useful logging, but should be brought closer to paper before scaling.

Needed:

- canonical write-side execution/audit row schema across paper and live;
- account-state snapshots before/after order placement;
- persisted execution snapshots for live;
- normalized reason codes for approvals, rejections, fills, cancels, and settlements;
- stronger parity diff reports that highlight schema gaps and behavior deltas.

### 4. Settlement/accounting parity

Paper settlement/accounting is stronger than live right now.

Needed before unattended live:

- unified lifecycle states: open, closed-unsettled, settled, canceled, partially-filled, failed;
- shared accounting helpers for realized and unrealized P&L;
- tests for YES/NO outcomes, fees, unsettled markets, and edge settlement states.

### 5. Weather-model calibration risk

The current trading logic is promising, but weather markets — especially cheap bracket contracts — may still be overconfident.

Watch closely:

- very cheap brackets at `1¢–5¢`;
- high model probability multiples caused by tiny prices;
- city/source-specific model errors;
- cases where historical/replay evidence disagrees with live paper behavior.

For first live canary, consider restricting or down-sizing cheap weather hidden gems until more resolved paper outcomes are reviewed.

### 6. Operational guardrails

The bot should not be able to harm the host or silently drift into unsafe behavior.

Recommended before first live run:

- systemd memory limit for live service;
- systemd CPU limit or sane CPU quota;
- explicit live max daily loss;
- explicit live max exposure;
- explicit max position size;
- clear kill-switch behavior for repeated exceptions, failed reconciliations, or negative accounting invariants;
- lightweight resource/health logging.

## Monday Canary Checklist

Before enabling live with real funds:

- [ ] Commit or intentionally snapshot the current working tree so live behavior maps to a known code state.
- [ ] Confirm config is in live mode only for the intended live service/process.
- [ ] Confirm paper remains running separately for comparison.
- [ ] Set bankroll / exposure / daily-loss / position-size limits.
- [ ] Confirm live credentials and private key path point to the intended Kalshi account.
- [ ] Confirm order placement uses current bid/ask revalidation.
- [ ] Confirm Telegram/status reporting identifies live vs paper clearly.
- [ ] Confirm restart behavior with no open live order first.
- [ ] Place or allow only the first tiny live order under supervision.
- [ ] Review exchange account directly after the first live order.
- [ ] Review local audit row and exchange truth for the first live order.

## Readiness Summary

Current confidence estimate:

- Tiny supervised live canary: **reasonable with strict caps**
- Unattended live trading: **not yet recommended**
- Scaling position sizes: **wait until live lifecycle, reconciliation, settlement, and audit gaps are hardened**

The right next step is a controlled `$100` canary with tiny trade limits, while agents continue hardening the gaps above.
