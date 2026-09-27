# Kalshi shared candidate rules integration dossier

- **Status:** feature and exact combined beta tree independently accepted for an intentional local merge only; no push or activation
- **Owner:** task `t_5662441a` (isolated feature implementation)
- **Branch / owning ref:** `fix/kalshi-shared-candidate-rules`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-rule-candidate-parity`
- **Base commit:** `9e7f83e4f4d507a56413ddf91d546fd1f78c8250` (clean local `beta`)
- **Intended integration target:** local `beta` (not integrated)
- **Last updated:** 2026-09-27
- **Latest immutable recovery checkpoint:** `7b58991416824c516c75b4388cd4552303485b45` (reachable implementation commit)
- **Feature implementation commit:** `7b58991416824c516c75b4388cd4552303485b45`
- **Canonical references:** `bot/exchanges/kalshi.py`, `bot/shared_market_feed.py`, `tests/test_kalshi_quotes.py`, `tests/test_weather_settlement_authority.py`; `AGENTS.md`

## Intent and implemented contract

The SDK `get_market` detail path now copies the same returned decision-time contract metadata as the raw/list adapters, including verbatim `rules_primary` and `rules_secondary`. A shared candidate's market summary retains only those two optional rule fields from the observed market metadata; direct and embedded shared-candidate reconstruction copies them back into `Market.metadata`. The weather settlement classifier can then use recorded rules without looking up a later market. Missing rules stay missing and authority remains `unknown_rules`; forecast scoring remains usable. An explicitly returned empty primary rule remains an empty string.

This does **not** add status/result/outcome, settlement labels, or inferred source weights to the shared market summary or reconstructed metadata. It does not change forecast weights, authority heuristics, raw history, live runtime, wallet, configuration, or services. Artifact-side conflicting rules do not replace a recorded shared-candidate rule. Legacy rows without recorded rules do not gain them from a future SDK fetch.

## Evidence and review

- RED: synthetic SDK detail/raw parity test failed on `KeyError: 'rules_primary'`; shared summary test failed on the same missing field; after summary fix, reconstruction test failed on missing metadata rule. Minimal code changes then made those tests green.
- Focused: `PYTHONPATH="$PWD:$PWD/tests" python3 -m unittest tests.test_kalshi_quotes tests.test_shared_market_feed tests.test_weather_settlement_authority -q` — 52 passed.
- Full isolated: `PYTHONPATH="$PWD:$PWD/tests" python3 -m unittest discover -s tests -q` — 1,344 run, OK (7 skipped); source root and sibling tests directory resolved from this worktree. Test stdout included existing fixture logging; no test failures.
- Synthetic coverage: SDK response object vs raw adapter (both rule fields, absent fields, no fallback); September TWC and August NWS (secondary-only) market summaries and direct/embedded reconstruction; artifact with contradictory rule/result excluded; no SDK detail refetch for shared-only reconstruction; rule-free candidate gives unknown settlement authority but a scoreable NWS-driven forecast with existing forecast-driver weights.
- `git diff --check` — clean.
- Independent feature review ACCEPTED clean tip `57170ac049718292a64b64c2bc9893882c9e409d` for a new noncommitting preflight only (52 focused; 1,344 full, 7 skipped). Installed SDK was 3.2.0, not the pinned 3.9.0; wheel inspection confirmed rule fields but did not execute the pinned version.
- Independent combined-tree review ACCEPTED exact staged local-beta tree `746ecf1fb3d8f88d9a7de3c673edeb75d09e33eb` for an intentional local merge only. Both feature parents were staged, both dossiers excluded; combined 207 focused (1 skipped) and 1,348 full (7 skipped) passed. No beta merge, push, or runtime activation at this dossier checkpoint.

## Limits and next gate

No live API rule-text capture or historical decision-time rule archive was sampled. Synthetic fixtures establish transport parity, not that every SDK market model or historical row exposes rules. Do not backfill missing rules using present-day market detail; those rows remain unknown. Parent may recreate and verify the reviewed exact combined beta tree, rerun focused and full suites, then intentionally merge locally only. No forward settlement/cohort activation evidence is claimed.

## Interruption / resume handoff

- **Owning feature ref:** `fix/kalshi-shared-candidate-rules`
- **Recovery checkpoint / implementation commit:** `7b58991416824c516c75b4388cd4552303485b45`; current feature tip will contain a later dossier-only handoff commit.
- **Exact resume point:** from clean local beta HEAD `9e7f83e4f4d507a56413ddf91d546fd1f78c8250`, stage both accepted feature branches, remove both dossiers, verify index tree `746ecf1fb3d8f88d9a7de3c673edeb75d09e33eb`, rerun focused/full, then local beta merge. If identity or tests differ, stop for re-review.
- **Working-tree state at handoff:** to be clean after implementation and dossier-only commits.

## Decision gates

- **Integration:** independent exact-tree review authorizes the matching local beta merge only; exclude this branch-local dossier from beta. A changed staged code tree requires new review.
- **Activation / cohort:** none authorized; synthetic fixture only.
- **Promotion:** out of scope; no historical authority or settlement outcome is inferred.

## Decision record

- 2026-09-27 — bounded test-first SDK/detail and shared-candidate preservation implemented locally; tests green.
- 2026-09-27 — independent feature and exact combined-tree reviews accepted their distinct gates; historical provenance and pinned-SDK execution remain deferred.
