# Weather MVP Foundation

This package is intentionally small and isolated for the `tailored-weather-market` branch.

- `registry.py` loads the draft city/source registry, performs basic structure checks, and keeps score updates in memory until a caller explicitly saves the file.
- `observation_log.py` writes compact JSONL records with content-hash dedupe, identical-observation cooldowns, and simple size-based rotation into an `archive/` directory.
- `source_validation.py` loads small per-city pilot source files and scores manual threshold-direction checks against archive weather-market outcomes.

Planned later usage:

1. Load the starter registry once at process startup.
2. Read city/source profiles when evaluating a weather market.
3. Append only distinct, decision-relevant observations.
4. Persist registry score changes deliberately in a review/update step, not on every scan loop.
5. Backfill small `data/weather/sources/*.json` validation entries to estimate which local/global sources deserve more attention per city.
