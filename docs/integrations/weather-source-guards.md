# Weather source guards — t_d694dfa0

- **Owner / branch:** source-guards subtask of parent `t_d694dfa0`, `fix/weather-source-guards-t_d694dfa0`.
- **Base:** `fec68a30e3e66a5bfcb51dff40d2f22536329200` (reviewed pagination inherited unchanged).
- **Target:** `beta`, explicitly directed by the parent; not `main` and not deployment.
- **Checkpoint:** implementation commit to be recorded in the final dossier-only handoff.
- **State:** local implementation; parent owns fresh independent review and integration. No nested reviewer, merge, push, service or production-data changes.

## Implemented contract

1. Resolve the five missing explicit CLI labels using individually verified physical station bindings, with city owners available to live and historical crosschecks. This does **not** change Weather Company settlement authority to NWS.
2. Reject known other-city station ownership before forecast transport, and matching-ID geometry at exact configured other-city coordinates before caching/querying. Unknown nearby station/grid coordinates remain valid; no invented radius/equality requirement.
3. Apply shared retained query/provider geography checks before admitting a live Open-Meteo forecast (also inside the direct adapter) or aggregated source. Keep rejected numeric values and captured forecast bytes. Historical strict and predictive readers reuse the same reconciliation owner, now checking every provider state alias/selected-period assertion and ambiguous repeated URL coordinate parameters. Identical URL repetitions remain valid.
4. Treat highest/lowest as daily high/low identity. Contrary original question/ticker/rule assertions fail closed; the real paired `maximum/minimum temperature` secondary-rule template stays nondiscriminating.
5. Reconcile all explicit market date fields, original IDs (including metadata-only event ticker) and dated question. A recognized contradictory date cannot be hidden by first-field precedence. The wrapper preserves metadata-only date evidence and refuses conflicts before transport.
6. Distinguish unsupported explicitly named question locations from absent location text. Preserve conditionally returned top-level city/date/kind metadata through all three exchange constructors. Do not redefine quote eligibility or guessed aliases.

## Station evidence

Fetched read-only on 2026-10-24 from public primary NWS sources. The durable `stations/bindings.json` records source URL, timestamp, raw SHA-256, safe-fetch verdict and retained artifact locator for each CLI report and corresponding station metadata response. The tests retain bounded geometry/name fields and their station response hashes; no mutable runtime lookup is required to run them.

| CLI report | Physical station / NWS name | City owner |
|---|---|---|
| CLISAN | KSAN / San Diego International Airport | san_diego_ca |
| CLIDCA | KDCA / Washington/Reagan National Airport, DC | washington_dc |
| CLISDF | KSDF / Louisville Muhammad Ali International Airport | louisville_ky |
| CLIEWR | KEWR / Newark, Newark International Airport | newark_nj |
| CLITTN | KTTN / Trenton-Mercer Airport | trenton_nj |

Reproduce primary sources with `https://forecast.weather.gov/product.php?site=NWS&issuedby=<CLI>&product=CLI&format=TXT&version=1&glossary=0` and `https://api.weather.gov/stations/<station_id>` (explicit pairs above, not a general prefix rule). Reports establish reporting city/location; station responses establish physical names/geometry. These sources establish forecast geography, not the market's settlement vendor.

## Verification and reproduction

Interpreter: `/home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python`; execute from this isolated worktree with `PYTHONPATH=.:tests`, `PYTHONDONTWRITEBYTECODE=1`, and scratch `TMPDIR`. Installed direct pins match requirements; no dependency edits/install.

- `python -m unittest test_weather_source_guards -v`: **14 tests passed**. Initial test-first receipt: 13 tests, 33 failed subassertions and 3 erroring counterexamples before implementation. The later retained-catalog case adds ten unchanged official HIGH/LOW identities through actual strategy/adapters, byte-verified synthetic captures and physical-station metadata fixtures.
- Earlier combined focused gate (before adding the catalog case): **40 tests passed**, including identity, date and geography suites. Final full frozen-source gate pending below.
- `validate_catalog.py --repo <worktree> --catalog <retained-catalog-root> --output <derived-output>`: **576/576** retained daily contracts resolve/route the correct city, kind, date and station; all retain Weather Company authority. **24 cities, 288 HIGH / 288 LOW**; zero real provider HTTP. All 539 listing-eligible plus 37 quote-excluded diagnostic records remain in the denominator, without changing production quote gates.
- Actual strict/predictive history readers reject duplicate-coordinate ambiguity, provider-state/selected-state contradictions, foreign station owners and highest/lowest conflicts; identical coordinate repetitions, nearby provider geometry and matching states remain usable. Retained source/index/resolution fixtures are unchanged by reads.
- `git diff --check` passed at the focused checkpoint.

Durable evidence root: `/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/finish_e2e_t_d694dfa0/source_guards/` (`regressions-red.log`, `regressions-green.log`, `focused-green.log`, `catalog-validation.json`, reproducible `validate_catalog.py`, `stations/bindings.json`). Committed bounded fixtures: `tests/fixtures/weather_source_guard_contracts.json`, snapshot SHA-256 `fc554bc5f03072169f04bbefdeabe22802f6ece147a2d87dd6209f49b49f4062`.

## Boundaries / review handoff

No capture-accounting implementation edits. No production captures, raw ledgers, service/runtime/config, beta checkout, wallet or trading actions. Public HTTP was station evidence retrieval only. Tests use disposable synthetic stores and mocked HTTP; catalog-wide validation proves operands, not live availability. Parent must independently review this exact commit range, combine with accounting, run its end-to-end stopped-writer/production integration gate, and authorize activation separately. Bounded startup fallback redesign and operational restart are outside this branch. Existing inherited pagination dossier is untouched; parent removes it only at final integration.
