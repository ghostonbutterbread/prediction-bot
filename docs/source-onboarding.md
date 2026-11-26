# Source onboarding (agent-proposed local weather sources)

Paper/observer data collection only. A source agent may add or patch **city-local weather forecast sources**;
they go live automatically after mechanical checks and an independent code review, then are watched twice a day
for two weeks. `scripts/source_onboarding.py` owns the gate; it never touches trading, accounts or storage limits.

## Flow

1. The source agent works in its own clone (`<work>/agent-repo`, remote `origin` = the collector's checkout),
   branches from `origin/beta`, adds a registry entry plus a page parser (and a test with a saved page), commits and
   runs `propose --branch <branch> --note '<what and why>'`. The proposal pins the branch commit.
2. `onboard` (agentless cron) processes pending proposals one at a time, in a temporary worktree merged onto the
   collector checkout's current `beta`:
   - **scope**: only `docs/weather/city_registry_starter.json`, `bot/feeds/weather_local*.py` and
     `tests/test_weather_local*.py` may change;
   - **registry**: sources may be added (`src_local_*`, `role: forecast`, `type: local_met`, https URL, starting
     `watch_only`, `trust_score: 0`, `verified: false`) or patched if this pipeline onboarded them; cities, other
     sources and trust fields may not change;
   - **cap**: a city's recorded sources = every catalog entry + its local forecasts + its station observation must
     stay ≤ `max_sources_per_city` (10; today most cities record 8) for every city the change touches;
   - **tests**: the local-source test modules plus any test file the branch changes;
   - **live probe**: the proposal's own parser fetches each added/patched source and must return a plausible dated
     high for today or tomorrow in the city's timezone;
   - **independent review** (`claude -p`, read-only): `VERDICT: PASS` required; BLOCK rejects the proposal.
3. On PASS: fast-forward the collector checkout, push `beta`, `systemctl --user restart` the collector and require the
   same main process to still be running after `restart_settle_seconds`. Otherwise the merge is reverted, pushed and
   the collector restarted again (an incomplete rollback prints `[CRON_FAILURE]`). A busy or diverged checkout,
   network failure or missing reviewer **defers** the proposal to the next run; any other failed check **rejects** it
   with the reason, which the agent reads from `status`.
4. Added sources are recorded in `watches.json` for `watch_days` (14). `watch` (run by the source agent morning and
   night) samples the last 12 h of collected rows and reports per watched source: rows present, scoreable share,
   unavailable reasons, median gap to the other sources' highs, source clocks after the decision time, source/market
   date mismatches and bytes per row, plus storage growth (snapshot archive, capture store, free disk). The agent
   patches problems through the same gate; a patch keeps the original watch window.

A parser runs inside the collector process. `ProWeatherEngine` isolates each adapter (a failing parser makes only its
source unavailable), and the gate's review blocks I/O, unbounded work and wrong date/period mapping. Global (all-city)
sources, catalog entries and sources needing API keys or accounts are out of scope for automatic onboarding.

## Operating

```sh
PY=<beta>/.venv/bin/python; CLI="$PY <trusted checkout>/scripts/source_onboarding.py"
$CLI init --work <work> --beta <collector checkout> --python "$PY" --unit <collector user unit> \
  --replay-index <collector_replay_index.jsonl> --market-snapshots <market_snapshots.jsonl> --capture-root <weather_captures>
$CLI status --work <work>           # cap and free slots per city, proposals, watches
$CLI propose --work <work> --branch source/<date>-<slug> --note '...'
$CLI onboard --work <work>          # cron: silent unless something happened
$CLI watch --work <work>            # morning/night integrity report
```

Raise the cap by editing `max_sources_per_city` in `<work>/config.json` once the watch's storage growth looks safe.
