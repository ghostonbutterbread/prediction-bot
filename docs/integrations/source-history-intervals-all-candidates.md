# Source history: quote-independent history and range forecast classification

## Ownership / boundary

- Branch: `fix/source-history-intervals-all-candidates`.
- Owned worktree: `/home/ryushe/worktrees/prediction-bot-source-history-intervals-all-candidates`.
- Original beta base: `ada72a2b85a2e37058ffbd94cb40a64640530cd9`.
- Accepted quote-independent implementation: `6c7b361f0204c19640f8a1b20343e7cdae143e69`; resumed checkpoint `4323e1963c57aef9e945e736c90e92c32f95affb`.
- Target **beta only**, explicit user instruction supersedes stale main-based local guidance. Parent owns Kanban, independent review and integration. No merge/push/deploy authority.
- Initial status clean; fetched origin beta without changing working tree. No sibling branch or worktree edits.
- Status: range implementation and isolated tests complete; **requested new historical differential receipt blocked by a pending tool approval**. Not integration-ready until that receipt and independent rereview are completed.

## Intent and implemented contract

Source correctness is independent of actions, quotes, edge, price guards, risk and Kelly. The accepted patch remains intact: both direct history and SourceWriter opt into blind quote-independent replay projection; strict executable/default projection remains unchanged. Absent prices are not imputed. Confidence rank is not converted into probability.

The new range owner is `bot/weather/thresholds.py`, reused by canonical pending/implied-outcome materialization. It retains two recorded bounds, origin/text evidence, endpoint policy and a named convention, not merely a scalar threshold. The existing per-source forecast extractor still selects the captured high or low and its target proof. Pending records retain interval evidence; exact independently bound outcomes produce YES/NO source correctness. Direct and materialized consumers share this owner.

### Forecast classification is not observation quantization

`recorded_interval_unrounded_forecast_v1` uses the existing repository closed, unrounded point-forecast convention (`weather_pro`, `historical_provider`, offline characterization of `audit_paper_resolution`). Question/subtitle wording anchored to temperature units can establish a range; no invented explicit inclusivity fields are required. Zero, negative and decimal bounds are supported. Ticker/scalar reconstruction is forbidden.

Each retained interval explicitly says `forecast_transform: none`, `observation_quantization: not_established`, and `exchange_settlement_parity_verified: false`. In particular **91.5 predicts NO for [92,93]**. This is not proof that an official station observation of 91.5 would settle NO. Legacy rows are source-forecast diagnostics under this declared methodology, not verified exchange quantization parity.

Returned `floor_strike`, `cap_strike`, `strike_type`, rules and subtitles survive raw/SDK producer conversion and the replay allowlist. Producer paths covered by the code change: daily raw listings, direct raw listings, raw converter (including direct listing/fallback), and SDK event objects. Existing source-context metadata transport remains unchanged. No new API call or quote-capture repair.

Supported explicit direct comparison sentences override default endpoint inclusion; matching rules/printed bounds must agree. Unknown/conditional/negated rules, conflicting bounds/endpoint rules, reversed/incomplete/nonfinite strikes, unsupported units and explicit unproven quantization fail closed with named interval statuses. Only supported direct-comparison grammar is implemented, not arbitrary legal-text interpretation. No quantizer is invented; future captured quantization rules still need a separately proven implementation.

SourceWriter generation hash and published manifest now declare `all_candidates_range_forecast_v2`, avoiding reuse of old byte-identical history generations. Publication/service activation remains separate.

## Files

Additional changes relative to resumed checkpoint:
- `bot/weather/thresholds.py`: interval extraction and unrounded membership; signed range side recognition.
- `bot/weather/source_observation_ledger.py`: pending interval transport, range/bucket implied side and named unusable reason.
- `bot/replay_decision_input.py`: minimal decision-time contract metadata allowlist.
- `bot/exchanges/kalshi.py`: returned contract metadata retention across raw and SDK paths.
- `bot/auto_source_router_promotion.py`: generation semantics version and visible manifest tag.
- `tests/test_source_history_ranges.py`: parser boundaries, explicit-rule precedence/rejection, producer/sanitizer transport, raw-to-writer/direct high/low SKIP/quoted/absent-price/duplicate/VOID/cutoff fixtures.

## Actual verification receipts

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

## Historical evidence: old results versus pending new results

Prior independent review verified 120 original byte locators, all original actions SKIP, 111 exported inputs, 888 pending observations, 24 pre-cutoff independent tail contributions, and selected-corpus direct/materialized parity. Those are **pre-range results**, not this implementation's recovery counts.

A tool approval is pending for a bounded differential script that would:
1. Derive a separate `range-proof.py` from existing audited `proof.py`, changing only history opt-in/output arm naming.
2. Extract immutable `4323e196` code into `/tmp/prediction-range-baseline-4323e19` via `git archive` (no branch/worktree mutation).
3. Run old and new implementations with the same existing `bounded_original_rows.jsonl`, captured resolutions and existing index, cutoff `2026-09-03T00:00:00+00:00`, separate `range-before`/`range-after` reports.

**The approval-gated batch made zero tool calls. It has not run and must not be represented as evidence.** Do not silently reissue it: the tool explicitly requires awaiting the user's approval decision. No new recovered-range total or current-capture coverage total is claimed yet. Resume by resolving that pending approval, then run/read the original-byte verifier, reconcile old/new interval counts and identical hashes, verify selected real-corpus direct/materialized parity and no imputed quotes. Expand only via bounded locators/current small capture if the cohort is insufficient; no huge raw scan.

## Recovery / next action

Recovery checkpoint: `295b13358cc69ed2ce23c20d93599767a9c8b124` contains the range implementation, tests and updated dossier on `fix/source-history-intervals-all-candidates`. The branch tip additionally contains a later dossier-only checkpoint annotation; review it separately. Preserve accepted patch `6c7b361`. Parent must independently rereview the new commit, complete the blocked bounded evidence run, and inspect whether any exact captured rules need a narrowly supported grammar extension. Do not call the full user acceptance complete before the historical receipts exist.

No services/configs, raw ledgers, active index, wallet, trading controls, or other worktrees changed. No merge, push or deployment. Quote capture, stale publication behavior and unrelated throughput work remain deferred. This dossier is branch-local temporary handoff metadata, not permanent beta/main history.
