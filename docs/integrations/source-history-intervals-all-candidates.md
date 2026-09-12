# Source history: quote-independent history and range forecast classification

## Ownership / boundary

- Branch: `fix/source-history-intervals-all-candidates`.
- Owned worktree: `/home/ryushe/worktrees/prediction-bot-source-history-intervals-all-candidates`.
- Original beta base: `ada72a2b85a2e37058ffbd94cb40a64640530cd9`.
- Accepted quote-independent implementation: `6c7b361f0204c19640f8a1b20343e7cdae143e69`; resumed checkpoint `4323e1963c57aef9e945e736c90e92c32f95affb`.
- Target **beta only**, explicit user instruction supersedes stale main-based local guidance. Parent owns Kanban, independent review and integration. No merge/push/deploy authority.
- Initial status clean; fetched origin beta without changing working tree. No sibling branch or worktree edits.
- Status: independent-review parser blockers repaired with RED→GREEN regressions, isolated focused/full receipts and a completed hash-verified historical differential preserving all valid range gains. **Fresh independent rereview remains required before beta integration**.

## Intent and implemented contract

Source correctness is independent of actions, quotes, edge, price guards, risk and Kelly. The accepted patch remains intact: both direct history and SourceWriter opt into blind quote-independent replay projection; strict executable/default projection remains unchanged. Absent prices are not imputed. Confidence rank is not converted into probability.

The new range owner is `bot/weather/thresholds.py`, reused by canonical pending/implied-outcome materialization. It retains two recorded bounds, origin/text evidence, endpoint policy and a named convention, not merely a scalar threshold. The existing per-source forecast extractor still selects the captured high or low and its target proof. Pending records retain interval evidence; exact independently bound outcomes produce YES/NO source correctness. Direct and materialized consumers share this owner.

### Forecast classification is not observation quantization

`recorded_interval_unrounded_forecast_v1` uses the existing repository closed, unrounded point-forecast convention (`weather_pro`, `historical_provider`, offline characterization of `audit_paper_resolution`). Question/subtitle wording anchored to temperature units can establish a range; no invented explicit inclusivity fields are required. Zero, negative and decimal bounds are supported. Ticker/scalar reconstruction is forbidden.

Each retained interval explicitly says `forecast_transform: none`, `observation_quantization: not_established`, and `exchange_settlement_parity_verified: false`. In particular **91.5 predicts NO for [92,93]**. This is not proof that an official station observation of 91.5 would settle NO. Legacy rows are source-forecast diagnostics under this declared methodology, not verified exchange quantization parity.

Returned `floor_strike`, `cap_strike`, `strike_type`, rules and subtitles survive raw/SDK producer conversion and the replay allowlist. Producer paths covered by the code change: daily raw listings, direct raw listings, raw converter (including direct listing/fallback), and SDK event objects. Existing source-context metadata transport remains unchanged. No new API call or quote-capture repair.

Supported explicit direct comparison sentences override default endpoint inclusion; matching rules/printed bounds must agree. Unknown/conditional/negated rules, conflicting bounds/endpoint rules, reversed/incomplete/nonfinite strikes, unsupported units and explicit unproven quantization fail closed with named interval statuses. Only supported direct-comparison grammar is implemented, not arbitrary legal-text interpretation. No quantizer is invented; future captured quantization rules still need a separately proven implementation.

SourceWriter generation hash and published manifest now declare `all_candidates_range_forecast_v6`, avoiding reuse of generations produced by the reviewed defective parser (including v5). Publication/service activation remains separate.

## Files

Additional changes relative to resumed checkpoint:
- `bot/weather/thresholds.py`: interval extraction and unrounded membership; signed range side recognition.
- `bot/weather/source_observation_ledger.py`: pending interval transport, range/bucket implied side and named unusable reason.
- `bot/replay_decision_input.py`: minimal decision-time contract metadata allowlist.
- `bot/exchanges/kalshi.py`: returned contract metadata retention across raw and SDK paths.
- `bot/auto_source_router_promotion.py`: generation semantics version and visible manifest tag.
- `tests/test_source_history_ranges.py`: parser boundaries, explicit-rule precedence/rejection, producer/sanitizer transport, raw-to-writer/direct high/low SKIP/quoted/absent-price/duplicate/VOID/cutoff fixtures.

## Prior implementation receipts (superseded by repair below)

Report root: `/mnt/data-collection/prediction-bot/data/derived_reports/source_history_fix_20260912`.

Read `independent-review.md`, `range-evidence-review.md`, companion `range-existing-semantics-probe.json` (17 prior characterization assertions), existing proof script, locator/report metadata, and implementation seams. No archive-wide scan or archive absence claim.

TDD:
- New parser/metadata tests added first; `range-red.log` records missing helper import on the old implementation, not a passing range implementation.
- Additional conditional/negated rules tests reproduced four semantic failures before the direct-sentence grammar restriction (tool output).
- Signed `-3 to -2°F` side regression reproduced `binary_bucket` before recognizing the signed unit-anchored range (tool output).
- Intermediate range pipeline fixture used an incorrect raw VOID status/count key; corrected to the existing finalized raw receipt plus `kalshi_result=void`. This was a test-harness issue, not a product VOID regression.

Final isolated commands, cwd owned worktree and `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests`:
- `python3 -m unittest discover -s tests -p 'test_source*.py'`: **72 tests OK**, 1.509s, `range-focused-final.log`.
- `python3 -m unittest discover -s tests`: **1,177 tests OK, 7 skipped**, 32.820s, `range-full-final.log`.
- `git diff --check`: passed.

New pipeline fixture verifies both quoted and quote-less SKIPs, exact YES receipts, separate captured high=0.5 / low=-0.5 forecasts for [0,1], three polls collapsing to two independent high/low contributions, two raw inputs unchanged, derived/direct scorecard equality for this selected fixture, strict settlement `<` (equal excluded, one second later admitted), and three VOID receipts with no history. It checks nested output sentinel removal and absent quotes staying absent. Existing target-proof/exact identity regression suite remains intact. This does not assert universal direct/materialized independence-policy equivalence.

## Independent-review blocker repair (2026-09-12)

Read `independent-range-review.md`, exact `independent-range-probe.py` and its JSON receipts before edits. Re-executed the unchanged reviewer probe: `repair-review-probe-before.json` confirms all five variants admitted one strict sample. Owning defect was introduced by `295b133` on this feature branch, not its beta ancestor; repaired here once. No descendant propagation or other branch edits are authorized; parent owns later beta integration.

Minimal owner changes:
- `thresholds.py`: apply recognized lower/upper comparisons from questions and all retained subtitles as well as rules; compare their bounds and endpoint semantics. Recognize the paired-comparison question as range-shaped for strict source proof.
- Reconcile high/maximum versus low/minimum temperature wording across retained evidence and recorded `market_kind`; contradictory evidence is unscored, never switches forecasts.
- Reject attached unsupported degree-unit names and Kelvin/Rankine rather than accepting a bare-degree prefix. Preserve legitimate bare degrees, Fahrenheit and dated questions.
- Reject residual disjunction/conditional/negated operators outside understood predicates; `or equal to` remains part of an understood comparison.
- Bump only the source-history projection identity/manifest to v3 so identical raw bytes cannot reuse a defective v2 generation.
- New `tests/test_source_history_range_review.py`: exact five reviewer variants plus supported/conflicting question comparisons, all retained origins, supported units/dated shapes and reversed measurement mismatch. Real raw→export→exact resolution→SourceWriter strict scoreboard and direct-history checks retain raw/resolution bytes. Existing chronology/VOID/dedup/quote-less SKIP tests remain unchanged.

Actual TDD receipts in the report root:
- `repair-review-red.log`: 3 new tests, **19 failing subtests**, including all five exact reviewer cases at parser and SourceWriter boundaries, before implementation edits.
- `repair-review-green.log`: range-focused 10 tests OK (0.644s).
- `repair-review-probe-after.json`: unchanged reviewer script now yields NO at zero for explicit open endpoints (one valid strict sample); conflicting bounds, wrong measurement, °K and compound question yield zero strict samples.
- Final isolated `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests python3 -m unittest discover -s tests -p 'test_source*.py'`: **75 tests OK**, 2.290s, `repair-focused-verified.log` (includes direct/materialized semantic scorecard equality).
- Same environment, `python3 -m unittest discover -s tests`: **1,180 tests OK, 7 skipped**, 32.757s, `repair-full-verified.log`.
- `git diff --check` passed. No executable pricing/action/edge/Kelly/risk gate changed. Quote-independent patch `6c7b361` remains intact.

## Historical evidence: completed repair differential

Actual clean implementation tested: `6cfd1defd9847a504246058d4945e31577e7979c`; baseline archive: `ada72a2b85a2e37058ffbd94cb40a64640530cd9`. New output directory: `repair-historical-range-run/` beneath the report root.

Read both actual scripts (`historical-range-repro.py`, `historical-range-verify.py`) and `proof.py` before execution. Derived `repair-historical-range-repro.py` changes only FIXED and the default new run name; `repair-historical-range-verify.py` changes only the default run name and redirects its root-level gains write into the new run. Original scripts, protected raw inputs and old receipts were not overwritten; no prepare/archive rescan was invoked.

Executed from the owned worktree (both exit 0):
```bash
PYTHONDONTWRITEBYTECODE=1 HISTORICAL_RANGE_RUN_NAME=repair-historical-range-run python3 /mnt/data-collection/prediction-bot/data/derived_reports/source_history_fix_20260912/repair-historical-range-repro.py
PYTHONDONTWRITEBYTECODE=1 HISTORICAL_RANGE_RUN_NAME=repair-historical-range-run python3 /mnt/data-collection/prediction-bot/data/derived_reports/source_history_fix_20260912/repair-historical-range-verify.py
```
Logs: `repair-historical-range-repro.log`, `repair-historical-range-verify.log`. Repro intentionally refuses an existing run and requires the exact clean implementation commit; future reruns must preserve these artifacts and use a new derived run/explicit verified checkpoint, not overwrite the old run.

Stdlib verifier result (`repair-historical-range-run/independent-verification.json`):
- 120 original SKIP rows / 120 byte hashes verified; 9,735,875 selected bytes; 6 protected input hashes unchanged.
- Both arms: 111 inputs, 888 pending observations.
- Baseline: 126 settled, 762 unusable, 24 pre-cutoff independent tail contributions; 39 strict settlements excluded by cutoff.
- Repaired: 252 settled, 636 unusable, **47 pre-cutoff independent = 24 tail + 23 range**; 75 strict settlements excluded by cutoff.
- All 23 range gains preserved (14 high / 9 low; 11 NWS / 12 Open-Meteo; 12 markets / 12 physical events). All 47 eligible repaired observations lack a positive book ask. This remains quote-independent source correctness, not executable trading evidence.
- Exact identity/provenance, settled-before-cutoff, implied interval membership, independent keys and selected-corpus direct/materialized semantics verified. Tail semantics unchanged.
- `repair-comparison.json` additionally compares prior completed range run: mode counts match exactly and both before/after eligible + independent row arrays are identical. No historical count changed after the parser repair. It retains script and new test/probe receipt hashes; `provenance.json` retains exact source commits, protected hashes and subprocess commands.

## Recovery / next action

### Final boundary repair (supersedes the prior recovery instructions below)

Read `final-independent-review.md` and the exact additional probe before editing the clean `8bb9f0c4d71eb408f1be24ae34139aa93fa205ff` tip. The same feature-owned parser still admitted `0-1° R`, `0-1°F and 4°F`, and `0-1°F / 4°F`; no ancestor or sibling worktree was changed.

- Replace token-specific unsupported-unit checks with supported F/Fahrenheit grammar and an explicit bare-degree continuation boundary. Attached and spaced unknown degree-unit tokens cannot fall back to bare Fahrenheit. Preserve bare-degree question/date and range/comparison continuations.
- Require each parsed origin to contain one interval predicate. Reject unconsumed temperature literals regardless of connector spelling, multiple independently parsed ranges (including identical ones), and mixed range/comparison predicates. Keep existing conditional/negated-prose rejection and strict full-sentence rules grammar. No arbitrary metadata scan or new contract fields.
- `tests/test_source_history_range_review.py` now parameterizes unsupported degree units across attached/spaced/spelled-degree forms and trailing temperature predicates across conjunctions, punctuation and words. The exact three reviewer variants are included. Each case exercises parser, real SourceWriter and indexed direct history; writer assertions use a nested subtest so a failing writer does not prevent the direct RED reproduction. Positive pipeline cases retain Fahrenheit variants, bare degrees, dated question prose, signed decimal/zero endpoints and explicit comparisons. Existing high/low, VOID, strict cutoff, dedup and quote/executable-price tests are unchanged.
- Bump source-history generation semantics to v4; regression independently recomputes each actual v4 generation hash and proves it differs from v3 for identical raw/resolution bytes. Existing external references to old generations are not automatically rebuilt or deployed.

Actual isolated receipts in the report root, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests`, cwd owned worktree:
- `boundary-red.log`: initial boundary additions before owner edits, 3 tests / **69 failing subtests**, including all three supplied cases through writer and direct history.
- `boundary-green-verified.log`: `python3 -m unittest test_source_history_range_review test_source_history_ranges`, **11 tests OK**, 3.588s.
- `boundary-focused.log`: `python3 -m unittest discover -s tests -p 'test_source*.py'`, **76 tests OK**, 5.089s.
- `boundary-full.log`: `python3 -m unittest discover -s tests`, **1,181 tests OK, 7 skipped**, 35.677s.
- `boundary-additional-probe.json`: unchanged final reviewer script, all three cases unscored with zero writer/direct strict samples. `boundary-original-probe.json`: unchanged original five-case script retains the repaired outcomes.
- `git diff --check` passes. Intermediate failures exposed a missing newline in the test's independent canonical hash and the prior missing-bounds status for a lone temperature literal; both corrected before the above verified receipts. No production price/history gates were changed.

Clean implementation/recovery checkpoint: **`f89dd4d0fd2481c43427b5e0b040edbdccf995ff`**, containing the two source files, expanded regression file and dossier. The current branch tip additionally contains a later dossier-only historical-receipt commit; verify that diff separately.

Completed the clean-checkpoint historical rerun:
- Read `repair-historical-range-repro.py`, `repair-historical-range-verify.py` and `proof.py` first. New `boundary-historical-range-repro.py` changes only FIXED and the default output run name; new `boundary-historical-range-verify.py` changes only the default run name. Both ran with the isolated environment above and `HISTORICAL_RANGE_RUN_NAME=boundary-historical-range-run`, exit 0. Logs have the same stems; outputs are under `boundary-historical-range-run/`.
- Original selected cohort remains **120 SKIPs / 120 verified original byte hashes / 9,735,875 bytes**. All six protected hashes unchanged. Both arms retain 111 accepted inputs / 888 pending observations. Baseline remains 126 settled / 762 unusable / 24 pre-cutoff tail contributions. Repaired remains 252 settled / 636 unusable / **47 independent contributions = 24 tail + 23 range**, with 75 strict settlements excluded by cutoff.
- Stdlib verifier independently recomputes exact identities, raw/source/input hashes, settlement chronology, range membership and independence. All range gains retain 14 high / 9 low, 11 NWS / 12 Open-Meteo, 12 markets / 12 physical events. All 47 repaired eligible rows lack a positive order-book ask. Both arms assert direct/materialized scorecard parity; tail semantics remain unchanged.
- `boundary-comparison.py` (exit 0) and `boundary-comparison.json` prove exact eligible and independent array equality against **all three** completed historical, repair and final-independent runs, before and after (12 comparisons). It also asserts exact new review cases failed through both consumers in the initial RED log, now yield zero samples, and records boundary receipt/script hashes.
- No `proof.prepare()`, original archive rescan, prior receipt overwrite, raw/index mutation, service/config changes, merge, push or deployment. This is a derived source-correctness diagnostic, not executable P&L, settlement quantization parity or promotion evidence.

**Next action:** parent obtains fresh independent read-only review of `8bb9f0c4..f89dd4d0`, reconciles the full beta-base-to-tip contract and new historical receipts, and separately checks current-beta compatibility. Earlier reviews do not approve this repair. Prior implementation `6cfd1defd9847a504246058d4945e31577e7979c` and review tip `8bb9f0c4d71eb408f1be24ae34139aa93fa205ff` remain reachable; no integration authority was exercised.

## Complete numeric clause repair (current handoff; supersedes above)

Started from clean `f7163b3b8a75610aabd7f9c74098382c747807b4`; first read `final-boundary-independent-review.md` and its exact RED harness. This is the feature-owned B1 repair, not an ancestor/runtime change. Parent retains Kanban, fresh independent review and beta compatibility/integration authority. No fetch/current-beta compatibility claim is needed for this explicitly pinned, non-integrating repair.

- Replace subtraction of matching substrings with a bounded numeric-clause parser: lex the complete numeric/comparison envelope, then **fullmatch** one printed interval or exactly two joined comparisons. Numeric endpoints may inherit Fahrenheit from either endpoint. An extra point, range or comparator extends the envelope and fails the whole grammar, regardless of connector or repeated unit. The lexer recognizes unsupported leading-dot decimals too, so they cannot disappear from residual validation.
- Only complete ISO calendar dates and English full/abbreviated-month/day/optional-year forms are removed as outside temperature context. Never drop everything after `on`, `in`, punctuation or a conjunction. Named location/question prose outside the numeric envelope is preserved; unknown numeric context fails closed. This is bounded interval grammar, not general date/location NLP or compound interval evaluation.
- Keep existing measurement/unit/quantization/conflict checks, explicit endpoint semantics, retained evidence and strict direct-sentence rules. Keep lone point-without-range status unchanged. All existing valid/invalid fixtures remain in the suites.
- Parameterized new cases cover both interval/comparison bases, comparator/point/range unit-inheritance suffixes, signed and leading-dot decimals, multiple connectors, legitimate inheritance at either bound, dates before/after the interval, location prose and a predicate after a date. Every case executes the parser and the real writer/indexed-direct strict history consumers; independent subtests expose both consumers on RED.
- v5 manifest/generation identity prevents selecting a defective v4 materialization with identical inputs. Tests independently compute the actual v5 directory hash and prove inequality with v3 and v4. This does not invalidate unrelated external pointers or publish any live generation.

Actual isolated receipts (report root above; cwd owned worktree, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests`):
- Unchanged `final-boundary-independent-grammar-red.py`: `clause-reviewer-red.log`, **2 tests / 9 failing subtests** before source edits. Same unchanged script after repair: `clause-reviewer-green-final.log`, **2 tests OK**, all three supplied cases blocked through both consumers.
- New family harness before source edits: `clause-family-red.log`, **4 tests / 189 failing subtests**. Additional leading-dot residual regression exposed **30 failing subtests** (`clause-leading-decimal-red.log`) and was repaired by lexical rather than connector changes.
- `python3 -m unittest discover -s tests -p 'test_source*.py'`: `clause-focused-final.log`, **76 tests OK**, 9.311s.
- `python3 -m unittest discover -s tests`: `clause-full-final.log`, **1,181 tests OK, 7 skipped**, 40.901s.
- `git diff --check` passes. Existing quote-independent SKIPs, provenance, VOID, strict chronology, deterministic collapse and consumer parity fixtures remain passing; no pricing/action/risk path changed.

Initial clean checkpoint `4067a8de3c1e1ff9a8340a405b635883f0e15bc0` ran `clause-historical-repro.py` and its stdlib verifier successfully, but **the acceptance comparison failed**: 24 tail / zero range gains. This exposed missing abbreviated calendar-month recognition (`90-91° on Aug 28, 2026`), not invalid historical contracts. The initial scripts/logs/run are preserved as failed acceptance evidence, not claimed as a passing differential. Added abbreviated/month-dot calendar cases (including trailing predicates after dates) through all three boundaries: `clause-calendar-red.log` records **4 tests / 51 failing subtests** before the calendar fix. `_MONTH` now supports normal full/abbreviated English month tokens without dropping arbitrary context suffixes.

Post-calendar-fix isolated receipts supersede earlier GREEN receipts:
- `clause-calendar-focused.log`: **76 tests OK**, 11.335s.
- `clause-calendar-full.log`: **1,181 tests OK, 7 skipped**, 41.941s.
- `clause-calendar-reviewer-green.log`: unchanged reviewer regression, **2 tests OK**.
- `git diff --check` passes.

Final clean implementation/recovery checkpoint: **`908ff2b376a015de104b697523fb8d317837c9e7`**, following clause-parser implementation `4067a8de3c1e1ff9a8340a405b635883f0e15bc0`. Current tip additionally contains the dossier-only final receipt commit; reviewers must verify that diff separately.

Completed the final clean-checkpoint historical differential in **`clause-calendar-historical-run/`**:
- Inspected `final-boundary-independent-historical-repro.py`, its verifier and `proof.py`. Derived `clause-calendar-historical-repro.py` and `clause-calendar-historical-verify.py` change only the pinned FIXED SHA and fresh default run name. Commands: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests python3 <report-root>/clause-calendar-historical-repro.py`, then `PYTHONDONTWRITEBYTECODE=1 python3 <report-root>/clause-calendar-historical-verify.py`; both exit 0. Logs use those stems. Baseline imports the archived original dossier base; repaired workers import the owned worktree, never the dirty storage checkout.
- **120 original SKIPs / 120 verified byte locators / 9,735,875 bytes**, all six protected hashes unchanged. Both arms: **111 inputs / 888 pending**. Baseline: 126 settled / 762 unusable / 24 independent tail contributions. Final repair: **252 settled / 636 unusable / 47 independent = 24 tail + 23 range**; 75 strict settlements excluded by cutoff.
- Stdlib verification recomputes raw/source/input/resolution provenance, exact identities, strict chronology, interval membership and independence. Range gains remain 14 high / 9 low, 11 NWS / 12 Open-Meteo, 12 markets / 12 physical events. All 47 eligible repaired observations lack a positive book ask. Direct/materialized semantic parity holds in both cohort arms; tail semantics unchanged.
- `clause-calendar-comparison.py`, exit 0, asserts the expected counts plus **20 byte-identical eligible/independent file comparisons** against all five prior passing historical runs (before and after). `clause-calendar-comparison.json` records these comparisons and 27 receipt hashes, independently reread and verified. `clause-calendar-reviewer-probe.json` reruns the unchanged three-case reviewer probe at the final implementation: all ambiguous, no implied outcome, zero strict writer/direct samples.
- Final after hashes: eligible `f9003b0a1ef359d767470089f12d9235d3561efedfdb53ac3b6ca0207f1f7848`; independent `8b2e44b95fede13775fbb3968e2909b04f4c6053ed8a03855949a16a09cbdc6f`. Full input/import/command receipts are in the fresh run's `provenance.json`, `commands.json`, reports and `independent-verification.json`.

**Next action: fresh independent read-only review of `f7163b3b..908ff2b3` plus the dossier-only tip, reconciling the full original-beta-base-to-tip diff and the final `clause-calendar-*` acceptance receipts.** Initial `clause-historical-run` is deliberately preserved as failed acceptance evidence. Parent must obtain a new verdict and separately check current-beta compatibility before integration; no earlier passing tests or review waive this gate. **No independent approval is claimed.**

No merge, push, runtime/service/config changes, raw ledger/index changes, wallet or trading actions. Historical results remain source-correctness diagnostics, not exchange quantization parity, executable P&L or promotion evidence.

## Cross-origin inheritance repair (current handoff; supersedes above)

Started from `ba8076a446a78d913e138a896bfd1f205d731c61`. Read the clause-final independent BLOCK report and exact probe/RED script first; original reproduction confirms 2 tests / 6 failures (`cross-origin-original-red.log`). Fetched origin beta read-only; no integration or runtime checkout changes. Parent owns independent review and beta integration.

- Resolve the existing Fahrenheit context across all affirmative contract-text origins before parsing any origin, including unitless questions whose unit appears only in a later subtitle/rule. Temperature predicates and bare numeric subtitles inherit that context; unrelated numeric report prose does not. Reconcile all parsed bounds, explicit inclusivity and measurement using the shared owner, without replacing explicit endpoints by printed defaults.
- Audit covers question, subtitle, market_subtitle, yes_subtitle, rules_primary and rules_secondary uniformly. Existing strike-pair Fahrenheit semantics remain intact. The retained no_subtitle is NO-side, not another affirmative interval: relevant numeric predicates remain unscored pending complement grammar, while ordinary `Outside range` prose remains valid. No invented metadata fields, general NLP or compound-interval evaluator.
- Unsupported explicit non-Fahrenheit names/letters cannot disappear merely because the degree marker is absent. An unresolved point before later interval evidence now rejects independently of field order. Complete-clause/date regressions remain intact.
- Explanatory/empty returned rule prose without an interval predicate is nonbinding; unknown interval rules, conditional/negated numeric rules and quantization stay unscored. No raw, executable quote, risk, action or settlement gate changes.
- Parameterized parser and real writer/direct fixtures cover every ordered pair of affirmative origins: agreement, conflicting bounds, unitless open comparisons, explicit unit conflict and measurement conflict; additional explicit-inclusivity conflicts, bare subtitles, empty/nonpredicate/numeric report prose and NO-side cases. Generation/manifest v6 is independently hash-tested against v3/v4/v5.

Receipts under the existing derived report root, explicit owned-worktree `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests`:
- `cross-origin-family-red.log`: 4 tests / 266 failures before implementation. The initial cross-subtitle valid fixture lacked a range-shaped question and thus correctly failed strict source-target proof; fixed its question to a unitless dated range rather than relaxing production proof. Rules that explicitly declare closed endpoints correctly conflict with open endpoints; fixture expectation corrected.
- `cross-origin-explicit-units-red.log`: 4 tests / 140 failures for C/Celsius without degree markers, prior to unit repair.
- `python3 -m unittest discover -s tests -p 'test_source*.py'`: **76 tests OK**, 20.128s, `cross-origin-focused-final.log`.
- `python3 -m unittest discover -s tests`: **1,181 tests OK, 7 skipped**, 50.203s, `cross-origin-full-final.log`.
- Unchanged `clause-final-independent-cross-origin-red.py`: **2 tests OK**, 0.116s, `cross-origin-original-green-final.log`; both original forms excluded by both strict consumers.
- Unchanged `final-boundary-independent-grammar-red.py`: **2 tests OK**, 0.198s, `cross-origin-prior-grammar-green.log`.
- Historical differential completed on clean implementation checkpoint **`eba521d7b1896f6b7de9cd55c1ec9007e9e263e8`**. The current tip adds only this dossier receipt; fresh independent review remains mandatory.

Historical commands (owned-worktree cwd): `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests python3 <report-root>/cross-origin-historical-repro.py`, then `PYTHONDONTWRITEBYTECODE=1 python3 <report-root>/cross-origin-historical-verify.py`; both exit 0. Scripts change only the prior review script's pinned fixed SHA and fresh default run name. A repeat requires a fresh `HISTORICAL_RANGE_RUN_NAME`; the repro deliberately checks HEAD equals the implementation checkpoint and the worktree is clean, so review a clean archived/detached implementation worktree or minimally repin a derived copy to the reviewed dossier-only tip before rerunning. Never bypass import isolation or overwrite a completed run.

`cross-origin-historical-run/independent-verification.json` proves **120 original SKIPs, 120 byte hashes, 9,735,875 bytes, six protected hashes unchanged**. Both arms retain 111 inputs / 888 pending. Baseline: 126 settled / 762 unusable / 24 independent tail; repaired: 252 settled / 636 unusable / **47 independent = 24 tail + 23 range**, with 75 strict settlements excluded by cutoff. Range gains: 14 high / 9 low, 11 NWS / 12 Open-Meteo, 12 markets / 12 events. All 47 repaired eligible observations lack positive book asks. Both arms pass direct/materialized semantic parity, and tail semantics remain unchanged.

`cross-origin-comparison.json` asserts required cohort counts and four byte-identical before/after eligible/independent files against `clause-final-independent-historical-run`. Final repaired hashes remain eligible `f9003b0a1ef359d767470089f12d9235d3561efedfdb53ac3b6ca0207f1f7848` and independent `8b2e44b95fede13775fbb3968e2909b04f4c6053ed8a03855949a16a09cbdc6f`. The original unchanged probe also ran successfully to `cross-origin-original-probe-final.json`: original two contradictory subtitles now have no implied outcome and zero writer/direct strict samples. Test/replay/probe receipts are hashed in `cross-origin-receipts.json`.

Final `git diff --check` is clean (an extra dossier EOF blank line in the checkpoint was removed in this handoff-only commit). Parent should independently review `ba8076a4..eba521d7` plus this dossier-only tip, reconcile the full original-base diff, rerun the original repro and suites, and separately verify beta compatibility. No independent approval, merge, push, deployment, runtime/config/raw mutation or trading claim is made.
