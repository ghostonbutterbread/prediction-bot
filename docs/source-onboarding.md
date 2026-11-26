# Source onboarding (agent-proposed local weather sources)

Paper/observer data collection only. A source agent may add or patch **city-local weather forecast sources**;
they go live automatically after mechanical checks and an independent code review, then are watched twice a day
for two weeks. `scripts/source_onboarding.py` owns the gate; it never touches trading, accounts or storage limits.

## Flow

1. The source agent works in its own clone (`<work>/agent-repo`, remote `origin` = the collector's checkout),
   branches from `origin/beta`, adds a registry entry plus a page parser (and a test with a saved page), commits and
   runs `propose --branch <branch> --note '<what and why>'`. The proposal pins the branch commit. A parser-only fix
   (registry unchanged) names the onboarded source(s) it is for with `--source <id>`.
2. `onboard` (agentless cron) processes pending proposals one at a time, in a temporary worktree merged onto the
   collector checkout's current `beta`:
   - **scope**: only `docs/weather/city_registry_starter.json`, `bot/feeds/weather_local*.py` and
     `tests/test_weather_local*.py` may be added or modified, as regular files (renames count as a deletion, and
     deletions, symlinks and submodules are rejected); the `OTHER_LOCAL_*` mappings in
     `bot/feeds/weather_local_other.py` must stay literal dicts and may change only for added or onboarded sources;
   - **registry**: sources may be added (`src_local_*`, `role: forecast`, `type: local_met`, https URL, starting
     `watch_only`, `trust_score: 0`, `verified: false`) or patched if this pipeline onboarded them; cities, other
     sources and trust fields may not change;
   - **cap**: a city's recorded sources = every catalog entry + its local forecasts + its station observation must
     stay ≤ `max_sources_per_city` (10; today most cities record 8) for every city the change touches;
   - **tests**: the local-source test modules plus any test file the branch changes;
   - **live probe**: the proposal's own parser fetches each added/patched source and must return a plausible dated
     high for today or tomorrow in the city's timezone;
   - **independent review** (`claude -p`, read-only): `VERDICT: PASS` required; BLOCK rejects the proposal.
3. On PASS: re-check that the collector checkout is clean and still at the reviewed base, fast-forward it to the
   reviewed merge commit, push exactly that commit to `beta`, `systemctl --user restart` the collector and require
   the same main process to still be running after `restart_settle_seconds`. Otherwise the merge is reverted, the
   collector restarted on the revert, then the revert pushed. An incomplete rollback marks the proposal `failed`
   (never retried) and prints `[CRON_FAILURE]`. A busy, moved or diverged checkout, a git network failure or a
   missing reviewer verdict **defers** the queue to the next run; any other failed check (including a source site
   that is down during the probe) **rejects** the proposal with the reason, which the agent reads from `status`.
   After a rollback the branch's commits are reverted history: rebuild the change on a fresh branch. While a `failed`
   proposal exists, `onboard` does nothing (status shows `BLOCKED`) until the operator repairs the checkout/collector
   and runs `acknowledge --id <id> --note '...'`. A reviewer that answers without a verdict line is retried and the
   proposal rejected after three attempts; a deferral is reported once per new reason.
4. Added sources are recorded in `watches.json` for `watch_days` (14). `watch` (run by the source agent morning and
   night) samples the last 12 h of collected rows and reports per watched source: rows present, scoreable share,
   unavailable reasons, median gap to the other sources' highs, source clocks after the decision time, source/market
   date mismatches and bytes per row, plus storage growth (snapshot archive, capture store, free disk). The agent
   patches problems through the same gate; a patch keeps the original watch window.

A parser runs inside the collector process. `ProWeatherEngine` isolates each adapter (a failing parser makes only its
source unavailable), and the gate's review blocks I/O, unbounded work and wrong date/period mapping. The gate catches
mistakes, not a hostile agent: tests and the probe run proposal code as the operator before review, the agent already
has a shell, and edits to shared parser helpers used by existing sources are guarded only by the review (the watch
covers onboarded sources). Global (all-city)
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
