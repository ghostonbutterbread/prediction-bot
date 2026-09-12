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

SourceWriter generation hash and published manifest now declare `all_candidates_range_forecast_v4`, avoiding reuse of generations produced by the reviewed defective parser. Publication/service activation remains separate.

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
- `tests/test_source_history_range_review.py` now parameterizes unsupported degree units across attached/spaced/speIled-degree forms and trailing temperature predicates across conjunctions, punctuation and words. The exact three reviewer variants are included. Each case exercises parser, real SourceWriter and indexed direct history; writer assertions use a nested subtest so a failing writer does not prevent the direct RED reproduction. Positive pipeline cases retain Fahrenheit variants, bare degrees, dated question prose, signed decimal/zero endpoints and explicit comparisons. Existing high/low, VOID, strict cutoff, dedup and quote/executable-price tests are unchanged.
- Bump source-history generation semantics to v4; regression independently recomputes each actual v4 generation hash and proves it differs from v3 for identical raw/resolution bytes. Existing external references to old generations are not automatically rebuilt or deployed.

Actual isolated receipts in the report root, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD:$PWD/tests`, cwd owned worktree:
- `boundary-red.log`: initial boundary additions before owner edits, 3 tests / **69 failing subtests**, including all three supplied cases through writer and direct history.
- `boundary-green-verified.log`: `python3 -m unittest test_source_history_range_review test_source_history_ranges`, **11 tests OK**, 3.588s.
- `boundary-focused.log`: `python3 -m unittest discover -s tests -p 'test_source*.py'`, **76 tests OK**, 5.089s.
- `boundary-full.log`: `python3 -m unittest discover -s tests`, **1,181 tests OK, 7 skipped**, 35.677s.
- `boundary-additional-probe.json`: unchanged final reviewer script, all three cases unscored with zero writer/direct strict samples. `boundary-original-probe.json`: unchanged original five-case script retains the repaired outcomes.
- `git diff --check` passes. Intermediate failures exposed a missing newline in the test's independent canonical hash and the prior missing-bounds status for a lone temperature literal; both corrected before the above verified receipts. No production price/history gates were changed.

Implementation checkpoint is the commit containing this section and the three source/test files; a following dossier-only receipt will name its immutable SHA. **Historical rerun pending this clean checkpoint**: derive `boundary-historical-range-repro.py` from the read repair script by changing only FIXED and its default run name to `boundary-historical-range-run`; similarly derive the verifier with only the new default run name. Execute both in the owned worktree, then compare eligible/independent arrays against prior runs. Do not invoke `proof.prepare()`, rescan, overwrite original scripts/receipts, or alter raw/index bytes. Rereview and current-beta compatibility remain parent-owned gates; no integration authorization.

Recovery/implementation checkpoint: **`6cfd1defd9847a504246058d4945e31577e7979c`** on `fix/source-history-intervals-all-candidates` contains the repair, tests, v3 generation identity and dossier. Current tip additionally contains a later dossier-only historical-evidence/recovery commit; verify that diff separately. Previous range checkpoint `295b13358cc69ed2ce23c20d93599767a9c8b124` and reviewed tip `fcc92059b52d0b4ffdb118a8eacbb81222d444bb` remain reachable. Fresh independent review is still REQUIRED; earlier green tests/review do not approve this repair. Parent owns the Kanban/review/integration gate and must check current beta compatibility separately. Resume with fresh read-only review of `fcc92059..6cfd1def`, exact reviewer probe and new regressions, full base-to-tip contract boundaries, and historical receipts above.

No merge, push, runtime/service/config changes, raw ledger/index changes, wallet or trading actions. Historical results remain source-correctness diagnostics, not exchange quantization parity, executable P&L or promotion evidence.
