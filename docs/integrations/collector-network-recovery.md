# Collector network recovery integration dossier

- **Status:** feature
- **Owner:** Hermes Agent
- **Branch:** `fix/collector-network-recovery`
- **Base commit:** `a30121d` (`beta`)
- **Intended integration target:** `beta`
- **Last updated:** 2026-08-29
- **Owning feature branch/ref:** `fix/collector-network-recovery`
- **Latest immutable recovery checkpoint:** `a30121d` (pre-implementation base; implementation commit pending)
- **Feature implementation commit(s):** none yet
- **Inspiration / canonical references:** collector DNS outage report; `bot/http_rate_limit.py`; `bot/exchanges/kalshi.py`; `bot/prediction_lab_collect.py`

## Intent

Repair the observer collector boundary so exhaustion of `http_get_with_retry` transport retries is not represented as an empty, successful market collection. Preserve real successful zero-market responses and all observer/paper-only behavior. Do not activate or reconfigure a service.

## Implemented contract

`KalshiExchange.get_markets_direct` and its direct weather-series helper raise `DirectMarketFetchUnavailable` when `http_get_with_retry` returns `None` after its own retries. The exception crosses the existing collector fatal-error boundary, which records the error and exits non-zero for systemd recovery instead of advancing `last_collect_at`. A HTTP 200 response with an empty `markets` list remains a normal `[]` result.

## Evidence and review

- Tests and commands:
  - RED: `PYTHONPATH=. .venv/bin/python -m unittest tests.test_kalshi_direct.KalshiDirectMarketTests.test_get_markets_direct_raises_when_retries_exhaust_without_response -v` — failed as expected: `AssertionError: RuntimeError not raised`.
  - GREEN: same focused regression — passed.
  - Focused: `PYTHONPATH=. .venv/bin/python -m unittest tests.test_kalshi_direct tests.test_prediction_lab_collect -v` — 69 tests passed.
  - Full: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests` — 1042 tests run, 18 pre-existing/environmental errors and 7 skips; failures include missing optional `rich` and absent ignored `data/summaries` fixture directory. The changed focused suites passed.
  - `git diff --check` pending final staged diff inspection.
- Independent review: not requested; minimal local ownership-boundary change.
- Replay/cohort/fixture evidence: deterministic mocked transport exhaustion and HTTP-200-empty fixtures only; no live network calls.
- Merge/ancestry evidence: branch starts at `a30121d` / `beta`; no merge or push performed.

## Blockers and deferred work

- **Missing test or evidence:** clean-environment full-suite receipt.
- **Command / fixture / environment needed:** `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests`, with optional `rich` installed and expected ignored `data/summaries` test fixture directory available.
- **Trigger to run it:** before integration or any activation decision.
- **Why it blocks integration, activation, or promotion:** a whole-suite clean receipt cannot be claimed from this fresh worktree; it does not block the focused collector repair evidence.
- **Next completion step / successor reference:** supply the repository's complete test environment, re-run discovery, and record outcome before an integration decision.

## Interruption / resume handoff

- **Owning feature branch/ref:** `fix/collector-network-recovery`
- **Latest immutable recovery checkpoint:** `a30121d` (implementation commit pending)
- **Feature implementation commit(s):** none yet
- **Exact resume point:** inspect/stage only `bot/exchanges/kalshi.py`, `tests/test_kalshi_direct.py`, `tests/test_prediction_lab_collect.py`, and this dossier; run `git diff --check`; commit locally; then amend this dossier in a follow-up documentation commit with the implementation SHA.
- **Working-tree state at handoff:** intentionally uncommitted implementation and dossier pending local commit.

## Decision gates

- **Integration gate:** focused collector tests green, full-suite environment blocker resolved, and review of the minimal exception propagation accepted.
- **Activation / cohort gate:** explicit user direction only; this branch makes no service, config, runtime, or trading change.
- **Promotion gate:** no promotion scope; paper/observer safeguards remain unchanged.

## Decision record

- 2026-08-29 — created for the direct-fetch retry-exhaustion recovery repair; focused tests green; full-suite environment failures recorded.
