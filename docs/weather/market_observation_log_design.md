# Per-Market Observation Log Design

## Goal
Log enough per-market evidence to improve source trust over time **without** repeating the old mistake of excessive logging / storage churn.

## Design Principle
This is **not** a full firehose.
We only store compact, decision-relevant observations.

---

## What to Log
One observation row should represent a meaningful market/source event, not every internal model step.

### Log when
- a source makes or updates a concrete call relevant to a market
- we record a market resolution outcome
- we update source trust based on completed evidence

### Do NOT log when
- internal noisy intermediate steps
- repeated duplicate fetches with no meaningful content change
- every failed HTTP request
- every low-level scoring pass
- repetitive debug output

---

## Recommended Storage
Use append-only **JSONL**, but keep rows compact.

Suggested files:
- `weather/logs/observations.jsonl`
- `weather/logs/resolutions.jsonl`
- `weather/logs/source_score_updates.jsonl`

---

## Observation Record
```json
{
  "ts": "2026-04-14T18:00:00Z",
  "market_id": "KXHIGHMIA-26APR14-T85",
  "city_id": "miami_fl",
  "market_type": "high_temp",
  "source_id": "src_nws_mfl",
  "source_role": "forecast",
  "kind": "forecast_update",
  "value": {
    "forecast_temp_f": 86,
    "direction": "above"
  },
  "confidence": 0.78,
  "lead_time_hours": 5.4,
  "content_hash": "sha256:...",
  "note": "AFD implied stronger warming than previous run"
}
```

### Field notes
- `kind` examples:
  - `forecast_update`
  - `observation_update`
  - `resolution_recorded`
  - `trust_score_update`
- `content_hash` helps dedup repeated identical observations
- `note` should stay short

---

## Resolution Record
```json
{
  "ts": "2026-04-14T23:59:00Z",
  "market_id": "KXHIGHMIA-26APR14-T85",
  "city_id": "miami_fl",
  "market_type": "high_temp",
  "resolved_value": 87,
  "resolved_direction": "above",
  "resolution_source": "official_market_source_unknown",
  "note": "Market resolved YES"
}
```

---

## Trust Update Record
```json
{
  "ts": "2026-04-15T01:00:00Z",
  "source_id": "src_nws_mfl",
  "city_id": "miami_fl",
  "sample_size": 12,
  "old_score": 90,
  "new_score": 92,
  "reason": "Strong accuracy + good timeliness on recent high-temp markets"
}
```

---

## Anti-Bloat Rules

### 1. Deduplicate by content hash
If a source repeats the same effective forecast/update, do not append another row.

### 2. Rate-limit observation writes
For the same `(market_id, source_id, kind)`:
- only log again if content changed
- or enough time elapsed
- or confidence changed materially

### 3. Keep notes short
No giant blobs.
No raw article dumps.
No full transcript storage in the observation log.

### 4. Store summaries, not full payloads
Keep full source content elsewhere only if truly needed.
Observation logs should contain normalized facts, not raw pages.

### 5. Rotate or archive logs
When log files exceed threshold, rotate to archive.

Suggested threshold:
- 10–25 MB per log before rotation

### 6. No per-scan spam
Do **not** write one row for every scan loop if nothing materially changed.

---

## Minimal MVP Logging Policy
For first implementation, only log:
1. the latest distinct forecast per source per market
2. the final market resolution
3. source trust score changes

That gives us enough to learn over time without flooding the disk.

---

## Recommended Future Helper Functions
- `normalize_observation()`
- `observation_hash()`
- `should_log_observation()`
- `append_jsonl_compact()`
- `rotate_log_if_needed()`

---

## Bottom Line
This system should optimize for:
- learning
- deduplication
- compact evidence
- low write volume

Not maximum verbosity.
