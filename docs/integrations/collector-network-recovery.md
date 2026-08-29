# Collector network recovery integration dossier

- **Status:** feature
- **Owner:** Hermes Agent
- **Branch:** `fix/collector-network-recovery`
- **Base commit:** `a30121d` (`beta`)
- **Intended integration target:** `beta`
- **Last updated:** 2026-08-29
- **Owning feature branch/ref:** `fix/collector-network-recovery`
- **Latest immutable recovery checkpoint:** `5c8b608d68c8435e95ddf4d440667b66c5a1e95f`
- **Feature implementation commit(s):** `5c8b608d68c8435e95ddf4d440667b66c5a1e95f` — `fix: surface collector direct-fetch outages`
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
  - Full: `PYTHONPATH=/home/ryushe/worktrees/prediction-bot-collector-network-recovery /mnt/data-collection/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1042 tests, `OK` (7 expected skips). The earlier reviewer failure was caused by using a worktree-local/nonexistent `.venv`; the mounted runtime venv contains the optional `rich` dependency used by this beta runtime.
  - `git diff --check` — passed.
- Independent review: requested after the initial focused receipt. Reviewer confirmed the behavior but deferred merge until the clean full-suite receipt above; no code finding remained.
- Replay/cohort/fixture evidence: deterministic mocked transport exhaustion and HTTP-200-empty fixtures only; no live network calls.
- Merge/ancestry evidence: branch starts at `a30121d` / `beta`; no merge or push performed.

## Blockers and deferred work

- **Integration evidence:** complete — focused and full-suite receipts are green, and the independent reviewer found no code-level blocker after the runtime-venv receipt was supplied.

## Interruption / resume handoff

- **Owning feature branch/ref:** `fix/collector-network-recovery`
- **Latest immutable recovery checkpoint:** `5c8b608d68c8435e95ddf4d440667b66c5a1e95f`
- **Feature implementation commit(s):** `5c8b608d68c8435e95ddf4d440667b66c5a1e95f`
- **Exact resume point:** integration-ready: merge the reviewed implementation into `beta`, then restart the managed observer collector and verify a fresh snapshot.
- **Working-tree state at handoff:** clean after committing this dossier update.

## Decision gates

- **Integration gate:** focused collector tests green, full-suite environment blocker resolved, and review of the minimal exception propagation accepted.
- **Activation / cohort gate:** explicit user direction only; this branch makes no service, config, runtime, or trading change.
- **Promotion gate:** no promotion scope; paper/observer safeguards remain unchanged.

## Decision record

- 2026-08-29 — created for the direct-fetch retry-exhaustion recovery repair; focused tests green; full-suite environment failures recorded.
- 2026-08-29 — implementation committed as `5c8b608d68c8435e95ddf4d440667b66c5a1e95f`; no merge, push, activation, or config change.
