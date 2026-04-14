# City-Specific Weather Source Registry Design

## Goal
Build a per-city registry that learns which weather/forecast/observation sources are most reliable for each market city over time.

## Core Idea
Each city gets its own evolving source profile.
We do **not** assume one global best source.
We score sources using actual market outcomes, timing, and consistency.

---

## Data Model

### 1. City Registry
One record per city.

```json
{
  "city_id": "miami_fl",
  "city": "Miami",
  "state": "FL",
  "country": "US",
  "timezone": "America/New_York",
  "nws_office": "MFL",
  "default_market_types": ["high_temp", "low_temp", "precip", "wind"],
  "resolution_notes": "Check exact Kalshi rules/source for each market class",
  "status": "active",
  "trusted_primary": ["src_nws_mfl", "src_local_met_x"],
  "trusted_secondary": ["src_local_station_7"],
  "watch_only": ["src_weather_forum_a"],
  "rejected": ["src_hype_account_1"],
  "updated_at": null,
  "notes": []
}
```

### 2. Source Record
One record per candidate source.

```json
{
  "source_id": "src_nws_mfl",
  "city_id": "miami_fl",
  "name": "NWS Miami-South Florida",
  "type": "official",
  "role": "forecast",
  "platform": "weather_gov",
  "url": "https://www.weather.gov/mfl/",
  "coverage_area": ["Miami", "South Florida"],
  "verified": true,
  "status": "primary",
  "trust_score": 92,
  "metrics": {
    "accuracy": 0.0,
    "timeliness": 0.0,
    "specificity": 0.0,
    "consistency": 0.0,
    "resolution_alignment": 0.0,
    "hype_penalty": 0.0,
    "sample_size": 0
  },
  "last_reviewed": null,
  "notes": []
}
```

### 3. Market Observation Log
One record per market instance per source observation.

```json
{
  "market_id": "KXHIGHMIA-26APR14-T85",
  "city_id": "miami_fl",
  "market_type": "high_temp",
  "market_question": "Will the high temp in Miami be above 85?",
  "resolution_source": "unknown",
  "source_id": "src_nws_mfl",
  "source_role": "forecast",
  "observed_at": "2026-04-14T12:00:00Z",
  "forecast_value": 86,
  "forecast_direction": "above",
  "confidence": 0.78,
  "lead_time_hours": 6.2,
  "resolved_value": null,
  "resolved_direction": null,
  "was_correct": null,
  "was_early": null,
  "notes": "AFD implied warmer boundary layer than market priced"
}
```

---

## Trust Bands
- **90–100** → primary
- **75–89** → strong secondary
- **50–74** → useful but cautious
- **0–49** → watch-only / reject

---

## Scoring Dimensions
Each source gets scored on:

1. **Accuracy**
   - Was the source directionally right?
   - Was the forecast value close?

2. **Timeliness**
   - Did the source provide useful signal before the market moved/resolved?

3. **Specificity**
   - Concrete call or vague commentary?

4. **Consistency**
   - Repeatedly useful, or only occasionally right?

5. **Resolution alignment**
   - How often does the source line up with the actual source the market resolves from?

6. **Hype penalty**
   - Is the source engagement-farming, alarmist, or overly dramatic?

7. **Locality**
   - Is this source actually strong for this city/microclimate?

---

## Source Roles
- `forecast` → predicts the outcome
- `observation` → reports what happened / is happening
- `resolution_adjacent` → close to what the market likely resolves from
- `social_only` → can boost confidence, never trusted alone

---

## Promotion / Demotion Rules

### Promote to primary if:
- enough sample size
- strong accuracy
- good timeliness
- low hype penalty
- strong resolution alignment

### Demote if:
- repeatedly late
- repeatedly wrong
- high drama / low specificity
- diverges from resolution source too often

### Reject if:
- low-quality rumor source
- engagement bait
- unverifiable source identity
- consistently misleading

---

## Trade Safety Rules
For weather trades:
- require **1 official or highly trusted primary source**
- optional **1 secondary corroboration source** for stronger confidence
- never trade on social/forum chatter alone
- if source disagreement is high, reduce confidence or skip

---

## Suggested File Layout

```text
weather/
├── city_registry.json
├── sources/
│   ├── miami_fl.json
│   ├── nyc_ny.json
│   └── los_angeles_ca.json
├── logs/
│   ├── observations.jsonl
│   └── resolutions.jsonl
└── reviews/
    ├── miami_fl.md
    └── nyc_ny.md
```

---

## Evaluation Workflow
1. Create city record
2. Add baseline official sources
3. Add candidate local sources
4. Log source observations against live markets
5. Record resolved outcomes
6. Update per-source metrics
7. Re-rank source trust for that city
8. Promote/demote/update registry

---

## MVP Recommendation
Start with 3–5 active weather market cities.
For each city:
- add NWS office
- add 2–3 local meteorologist candidates
- add 1–2 local station candidates
- log outcomes for a few weeks
- review trust score changes weekly

