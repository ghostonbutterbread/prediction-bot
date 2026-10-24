# Weather source guards — t_d694dfa0

- **Owner / branch:** source-guards subtask of parent `t_d694dfa0`, `fix/weather-source-guards-t_d694dfa0`.
- **Base:** `fec68a30e3e66a5bfcb51dff40d2f22536329200` (reviewed pagination inherited unchanged).
- **Target:** `beta`, explicitly directed by the parent; not `main` and not deployment.
- **Implementation commits:** original `fd029e03d58a8cbdaf1832e3b52cbc0fa065c78e` / `c8ba6a121b6c96054253eeba18380140f2fdc0fd`, review corrections `f959fcb4b387b9aa5bee5b492d0301809e08191c`, fixture reconciliation `59e92cac9c43744b52cb36766961eecfd09a0301`, and current nested-normalization checkpoint **`e3c26337579aa71cb63a2ddb26f1d47ef2d743fe`** (68 focused tests passed; final full/independent verdict pending). Current tip may add only this dossier pointer after that tested checkpoint.
- **State:** fixture-aligned combined `3e5a748` passed its 1,859-test full gate, but the nested-emphasis review blocker below still applies. The new nested correction passes 68 focused tests; a fresh exact-candidate full gate and independent verdict remain mandatory. No release, merge into beta, push, service or production-data changes.

## Implemented contract

1. Resolve the five missing explicit CLI labels using individually verified physical station bindings, with city owners available to live and historical crosschecks. This does **not** change Weather Company settlement authority to NWS.
2. Reject known other-city station ownership before forecast transport, and matching-ID geometry at exact configured other-city coordinates before caching/querying. Unknown nearby station/grid coordinates remain valid; no invented radius/equality requirement.
3. Apply shared retained query/provider geography checks before admitting a live Open-Meteo forecast (also inside the direct adapter) or aggregated source. Keep rejected numeric values and captured forecast bytes. Historical strict and predictive readers reuse the same reconciliation owner, now checking every provider state alias/selected-period assertion and ambiguous repeated URL coordinate parameters. Identical URL repetitions remain valid.
4. Treat highest/lowest as daily high/low identity. Contrary original question/ticker/rule assertions fail closed; the real paired `maximum/minimum temperature` secondary-rule template stays nondiscriminating.
5. Reconcile all explicit market date fields, original IDs (including metadata-only event ticker) and every recognized date inside the question. A recognized contradictory date cannot be hidden by first-field precedence. The wrapper preserves metadata-only date evidence and refuses conflicts before transport.
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
- Earlier combined focused gate (before adding the catalog case): **40 tests passed**, including identity, date and geography suites.
- Initial frozen candidate `fd029e0`: focused **379 passed**; full **1851 run, 2 failures / 1 error**, all due to treating Markdown-wrapped `**high temp` as an unsupported city prefix. Corrected by using the existing text normalizer before the prefix check; no unsupported-city relaxation. Added the exact Markdown positive and an additional failing-first within-question contradictory-date control. Local corrected checks: **28 passed** (source guards/date/analysis) and **11 passed** (replay/source validation). The initial failed full receipt remains preserved.
- **Final corrected frozen gate:** `run_frozen_gates.py c8ba6a121b6c96054253eeba18380140f2fdc0fd source-guards-c8ba6a1 --mode full`: **1851 tests, zero failures/errors, seven runtime-config skip entries**; the inherited single real-archive smoke exclusion is `test_replay_decision_input.ReplayDecisionInputTests.test_real_archive_first_row_builds_in_default_legacy_compatibility_mode`. This is not an unfiltered archive gate. All 481 frozen source hashes remain unchanged; imported modules resolve under the frozen candidate, production reads are Python-audit denied, and Landlock write/seccomp network-denial probes pass. Exact tree `b822632279e07bb4210eebe2d654eeeee7e5097d`; receipts in `source-guards-c8ba6a1/verification.json` and `full-receipt.json`.
- Reused the original independent reproducer with scenario construction unchanged, isolated source path and corrected acceptance assertions only: `validate_boundaries_fixed.py` passed **19 strategy/provider, seven local, 16 actual-history, 18 kind controls**, with zero outbound attempts. `independent-harness-provenance.json` records the original reproducer hash; `current-run.json` locates byte-verified synthetic captures, immutable reader fixtures and results.
- Final checkpoint rerun of `validate_catalog.py --repo <worktree> --catalog <retained-catalog-root> --output <derived-output>`: **576/576** retained daily contracts resolve/route the correct city, kind, date and station; all retain Weather Company authority. **24 cities, 288 HIGH / 288 LOW**; zero real provider HTTP. All 539 listing-eligible plus 37 quote-excluded diagnostic records remain in the denominator, without changing production quote gates. Final receipt: `catalog-validation-final.json`.
- Actual strict/predictive history readers reject duplicate-coordinate ambiguity, provider-state/selected-state contradictions, foreign station owners and highest/lowest conflicts; identical coordinate repetitions, nearby provider geometry and matching states remain usable. Retained source/index/resolution fixtures are unchanged by reads.
- `git diff --check` passed at the focused checkpoint.

Durable evidence root: `/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/finish_e2e_t_d694dfa0/source_guards/` (`regressions-red.log`, `regressions-green.log`, `focused-green.log`, `catalog-validation.json`, reproducible `validate_catalog.py`, `stations/bindings.json`). Committed bounded fixtures: `tests/fixtures/weather_source_guard_contracts.json`, snapshot SHA-256 `fc554bc5f03072169f04bbefdeabe22802f6ece147a2d87dd6209f49b49f4062`.

## Independent review corrections

Review `deleg_880cdcb0` blocked source checkpoint
`1ddb4c9c8c0eaaf61694f39b5277d9bdf717ef86` and combined checkpoint
`ec6eca0dc4368c2b796d3b21e96e3816f9e48562`: supported emphasis could hide an
explicit unsupported post-metric city. It also reproduced original date
contradictions in both historical readers and a returned-snapshot city that
could self-authorize against a different requested city. The latter two were
inherited boundary gaps, not evidence of normal HTTP-adapter production misuse.
All three are within this task's city/date/source-correctness acceptance scope.

- Added five committed collection/actual-history regression cases. Before the
  correction they produced **37 failed assertions, zero errors**; the full RED
  log is `review-corrections-red.log` in the existing derived source-guards root.
- Strip balanced supported star/underscore emphasis before location extraction,
  preserving punctuation, station tokens and identifier-internal underscores.
  Matching-city, generic and station-only positives remain accepted.
- Pass the actual requested city to the shared live geography guard at both
  call sites. Returned snapshot labels remain independent assertions; rejected
  numeric values, city labels and receipt evidence are not rewritten.
- Apply the existing original-date resolver at the shared pending-observation
  qualification seam used by both direct historical readers. Only recognized
  contradictions add `conflicting_original_market_dates` to strict/predictive
  proof rejection; missing optional dates stay neutral. No clocks or raw records
  are changed, and no materializer/publication worker is enabled.
- Corrected focused gate: **95 tests, zero failures/errors/skips**, including
  collection/geography, both history readers, shared source ledger and predictive
  history controls (`review-corrections-green.log`, 18.816 seconds).
- Reference-impact check: both live `_guard_source_geography` call sites now pass
  the requested city; no legacy self-label-only call remains.

## Full-suite fixture reconciliation

The first combined correction gate, `ed4b1d6752e5f65ad33a85795c9876681c5bf05d`,
ran **1,859 tests with 25 failed assertions and zero errors** (seven runtime-config
skip entries and the inherited archive exclusion). Its RED receipts remain in
`weather_identity_repair_t_ca47d502/combined_t_d694dfa0_review_corrected/`.

- Twenty-four assertions came from calendar range fixtures changing the question
  month but leaving August market/event IDs, market date, forecast target and
  chronology. They were contradictory originals, not valid monthly positives.
  Calendar variants now move the entire synthetic identity, observation/source
  clocks, independent resolution timestamps and reader cutoff together. Parser
  expectations, consumer counts, input immutability and writer/direct parity
  assertions remain unchanged.
- The remaining fixture injected an Austin snapshot for `not a city` and expected
  the later numeric-plausibility reason. It now separately verifies a matching
  Austin request fails plausibility and an unknown request fails original-city
  proof; both retain the original 999-degree value as unscoreable. Local-provider
  I/O for the newly valid city is explicitly mocked.
- No production code or guard was changed in this fixture-only correction. The
  original contradictory-date, formatted-city and wrong-returned-city regression
  controls remain green.
- Corrected focused gate: **47 tests, zero failures/errors/skips**,
  `review-fixture-alignment-green-v2.log` (42.717 seconds). The first attempted
  focused run hit the accounting-only harness's incorrect relative-symlink audit;
  its log is preserved. The new source-review fixture runner checks only a
  symlink's mutated destination while retaining write/network/subprocess guards.
  The reviewed accounting/recovery helper was not modified. The full frozen
  kernel gate remains mandatory; this focused runner is not a substitute.

The corrected source must be propagated by merge into the isolated parent
accounting candidate, then receive a new combined full gate and fresh independent
review. The earlier 1,854-test combined pass does not approve this correction.
Production remains on execution-approval hold: the accounting repair request
expired without running. Do not retry that operation, alter the existing unit,
merge into the beta execution checkout, or start the collector until the user
explicitly responds. No production repair or live end-to-end success is claimed.

## Boundaries / review handoff

### Nested-emphasis review correction

Independent review `deleg_4f9fd97a` blocked source checkpoint `f959fcb` and
combined `ed4b1d6`: one-layer normalization still hid Sacramento in nested
`**_high temperature_**`, `_**high temperature**_`, or
`**high temperature _in Sacramento_**`. Both actual direct historical readers
also admitted those contradictory originals. The fixture-only successor
`59e92cac9c43744b52cb36766961eecfd09a0301` / combined
`3e5a748bcd5ba66f574bb506f61a13e343e72ec3` retained that same code, so its
**1,859-test full pass** is compatibility evidence, not release approval.
Its seven runtime-config skip entries and single real-archive exclusion remain.

- Added actual HIGH/LOW collection and both-reader positive/negative controls,
  plus composed emphasis tests across questions and both rule fields. They keep
  generic, matching-city, station-only and identifier-underscore positives.
- Test-first receipt `nested-emphasis-red.log`: **8 methods, 516 failed
  assertions, zero errors/skips**. Three newly added methods exposed the gap.
- The owning city parser now removes matched emphasis delimiter runs until the
  text stops changing. Each replacement strictly removes delimiters, so the loop
  terminates without a nesting-depth cap. Adjacent same-kind wrappers can merge
  into runs longer than three characters; matching the whole balanced run avoids
  leaving an artificial unmatched delimiter. No blanket punctuation stripping,
  source relabelling, transport bypass or input mutation is introduced.
- The first attempted GREEN retained four assertions for six-star location/station
  runs and a separate missing-test-module import error
  (`nested-emphasis-green.log`). Both
  causes were diagnosed; the actual geography suite is
  `test_weather_identity_geography_guard`, not the guessed module name.
- Corrected focused gate `nested-emphasis-green-v2.log`: **68 tests, zero
  failures/errors/skips**, 52.325 seconds, including the fixture-aligned range
  consumers, broad collection, original-date/source-city and new nested controls.

Only the city parser, regression tests and this branch-local dossier change in
this checkpoint. Merge it into the existing isolated owning parent and obtain
a new full frozen gate plus an independent verdict on that exact combined tree.
The original independent nested acceptance fixture remains unchanged for review.

No capture-accounting implementation edits. No production captures, raw ledgers, service/runtime/config, beta checkout, wallet or trading actions. Public HTTP was station evidence retrieval only. Tests use disposable synthetic stores and mocked HTTP; catalog-wide validation proves operands, not live availability. Parent must independently review this exact commit range, combine with accounting, run its end-to-end stopped-writer/production integration gate, and authorize activation separately. Bounded startup fallback redesign and operational restart are outside this branch. Existing inherited pagination dossier is untouched; parent removes it only at final integration.
