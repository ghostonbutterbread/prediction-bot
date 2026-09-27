# Paper-shadow market-only settlement chronology — integration dossier

- **Owner / task:** Hermes, prediction-bot Kanban `t_c9eb20d8`; parent flow audit `t_500d82fb`.
- **Branch / worktree:** `fix/paper-shadow-resolution-chronology`; `/home/ryushe/worktrees/prediction-bot-paper-untimed-resolution`.
- **Base / target:** local beta `fa84310b4d7dc5035294db72a2c87ee6fbe171ae` → `beta`. This branch is not a runtime lane or deployment.
- **Implementation:** `c6766b5d0a7e85c91b2259f5d07be0e5896db5af` (production guard and RED→GREEN/regression fixtures).
- **Status:** isolated correction passes tests; independent feature review pending. No merge, push, or activation.

## Defect and intended contract

On the clean beta base, a single market-ID-only YES outcome lacking `settlement_ts` matched a BUY_YES `.40`/`$10` paper decision and produced `blocker=None`, `$15` fixed-notional PnL; the Kelly diagnostic correctly kept it pending. An independent outcome can support a directional comparison, but a market-only, untimed match does not prove settlement after the decision or authorize an economic paper-PnL claim. Preserve the matched outcome and its lookup provenance for diagnostics. Require an authoritative post-decision settlement timestamp *for a market-only BUY economic receipt*; keep valid timed market-only matches, stronger exact-identity legacy diagnostics, VOID treatment, and quote-only no-fill blockers usable. Do not treat `resolved_at` or `outcome_known_at` as settlement time or add blanket mandatory metadata to every historical row.

## Evidence and gates

- Read-only in-memory reproduction on beta `fa84310`: `matched_by=market_id`, `settlement_ts=None`, `blocker=None`, `pnl_usd=15.0`, synthetic Kelly settlements `0`.
- RED: three market-only untimed cases (no clock, `resolved_at` only, `outcome_known_at` only) each falsely returned `blocker=None` while the timed and exact-candidate controls passed. GREEN: they retain the directional match/outcome but return `missing_settlement_timestamp` and no PnL, with no Kelly settlement. Timed market-only and exact-candidate legacy diagnostic remain calculable. Existing economic fixtures now carry a real post-decision settlement clock; the collector auto-resolution mock without one intentionally loses economic PnL while keeping the finalized outcome. Focused 147 tests pass (1 skipped).
- Final isolated full suite: **1,306 OK (7 skipped)** in `/home/ryushe/.hermes/cache/scratch/prediction-market-only-untimed-full-final-20260927.log`; original market-only source-scoreboard fixtures were updated to carry chronology, while the auto-resolve fixture without a settlement clock explicitly expects a blocked economic claim.
- Next: independent read-only feature review, clean beta-first noncommitting merge/checks/review before intentional local integration.
- **Deferred:** historical cohort prevalence is not measured here; no raw archive inspection or rewrite. Runtime/source-router activation, actual-wallet accounting, and market-specific weather settlement authority are separate gates.
- **Resume:** request independent feature review of the clean dossier tip. Do not merge or activate from a passing suite alone.
