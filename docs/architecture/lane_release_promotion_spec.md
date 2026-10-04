# Versioned Lane Release and Promotion Handoff

**Status:** proposed implementation contract; documentation only, not deployed.
**Owner/canonical path:** this document, `docs/architecture/lane_release_promotion_spec.md`, owns release-file ingestion and stage selection.
**Supersedes:** none. `collector-first-replay-lane-contract.md` owns blind replay, wallet, and settlement semantics; `forward-source-router-paper-handoff.md` owns shared-snapshot forward cohort identity. Neither is replaced here.
**Implementation commit:** pending — no implementation in this spec task.

## Goal

Try several policy lanes on the **same collector-owned candidates**, freeze a selected version once, and hand that exact version to replay and then a fresh forward-paper cohort without copying collector data or reassembling settings by hand. Preserve enough per-candidate trace to tell a weak trading rule from missing inputs, insufficient settled history, quote failure, or a wallet/risk rejection. Live is a separately authorized later consumer, never a side effect of loading a release file.

This is a thin file/loader handoff, not a new collector, strategy framework, service, or agent orchestrator. A Git **feature branch** is a place to develop an idea; a **lane branch** below is one alternative in a paired evaluation. Neither is a runtime stage or a durable release identity.

## Minimal artifacts

1. **Immutable release file** — `lanes/releases/<release-id>.yaml` (one per frozen version). It contains the fully resolved, decision-affecting configuration for the control and candidate lane branches, including trade brackets, source-history policy, pricing, sizing, and risk policy. Its source Git **commit** is executable provenance; an optional feature-branch name is only a human hint. Do not resolve a moving branch or current lane YAML at runtime. Freeze the effective definition *after* existing built-in/file/inline overrides have been applied.
2. **Stage binding** — a small selection in the existing replay/paper profile: release file path plus the expected **canonical-document** SHA-256 (not a hash of raw YAML bytes), stage, and fresh cohort ID where applicable. Loading a file is read-only; changing a binding does not start a service or enable trading. Paper activation remains an explicit separate operation. A future live profile may reference the same release only after separate review and operator approval.
3. **Derived receipts** — each run records release ID/hash, actual code commit, cohort/snapshot/candidate identity, effective policy/hash, source-history index/manifest/resolution receipts used for that decision, and a reason-coded trace. An enrollment receipt records the stage, release, evidence reviewed, operator decision, and prior binding for rollback. Keep these in the existing derived run/cohort output, not in raw evidence or another project task tracker.

Illustrative v1 file shape (field names are the proposed contract, not an implemented loader):

```yaml
schema: lane_release/v1
release_id: weather-router-01
source_commit: <full-40-character-git-commit>
source_branch_hint: feat/example-router-variant  # optional; never resolved by consumers
evidence_class: diagnostic_only  # current passthrough control is not re-execution
candidate_source: collector_shared_snapshot
branches:
  control:
    lane_id: control_stable
    effective_definition: {type: passthrough, source_wallet: stable_paper}
  candidate:
    lane_id: shadow_source_router_no_price_guard
    effective_definition:
      type: source_router
      source_wallet: stable_paper
      parameters: {allowed_actions: [BUY_NO], allowed_entry_price_ranges: [[0.60, 0.70], [0.80, 0.90]]}
policy:
  history: {mode: direct_verified_index, cutoff: settlement_strictly_before_candidate}
  pricing: {mode: recorded_selected_side_quote}
  sizing: {provider: existing_paper_policy, effective_config: {}}  # resolved at freeze time
  risk: {provider: existing_paper_policy, effective_config: {}}    # resolved at freeze time
```

The abbreviated policy maps above **must not be published as a real release**: a freezer writes complete effective configs and identifiers and refuses a missing decision-affecting value. This example is **diagnostic-only**: today's `control_stable` passthrough copies a previous paper decision, so it cannot claim blind control re-execution or enroll a trading paper wallet. A paper-eligible release needs an independently re-executing control and candidate plus the wallet-parity bridge below, while retaining the forward handoff's explicit lane identities until that builder is deliberately generalized. A release ID is a readable label; its SHA-256 over canonical JSON of the parsed release mapping (UTF-8, sorted keys, fixed separators; no self-hash field) is the immutable identity. The binding's expected SHA-256 is this **same** digest; comments/whitespace in YAML are not decision content, but an altered effective value fails. Freeze a forward-stage release only from a reviewed, integrated build; the consumer must run that pinned build, not a moving feature branch. A code or policy change means a new release, not editing the old file. The collector archive and index/resolution paths are **environment bindings**, not frozen copies of raw data: validate their verified triples at consumption and record their actual hashes on each decision. A paper cohort may use newly finalized history, but never a settlement at or after that candidate's observation time.

## One loader, three consumers

- **Replay:** compare several candidate releases on one frozen snapshot/candidate set **only when they share the pinned code commit and the identical frozen control policy/hash**; v1 does not mix moving code branches in one process. Different code commits require separate pinned builds and a later explicit matched-identity comparison, never a silent in-process merge. The release loader returns resolved lane/policy objects to the existing replay evaluator; it does not use previous lane actions as candidate inputs in a paper-shaped replay. Keep current passthrough, fixed-notional, and recorded-decision runs labelled diagnostic and ineligible for paper-wallet enrollment. For a paper-shaped replay, independently re-execute the control and candidate, use the declared action, selected-side price, real paper Kelly/risk seam, and chronological synthetic wallet with pending capital until independent settlement.
- **Forward paper:** bind exactly one release to a newly started cohort and feed the existing shared-market **consumer**, not a new market fetcher. The stage adapter must validate against the existing disabled `build_forward_source_router_handoff`: v1 supports only the `control_stable` / `shadow_source_router_no_price_guard` pair; any other candidate ID needs an explicit reviewed handoff extension. Preserve its `start_at`, shared collector snapshot manifest ID, separate lane policy-bundle versions/hashes, evaluator version, decision idempotency key, and distinct decision/execution/wallet namespaces; reject pre-start or mismatched snapshots. The same decision policy consumes new tranches, with a separate simulated wallet balance/open positions. The existing `paper_shadow_lanes.py` recommendation ledger alone is **not** paper-wallet enrollment; connect a sealed BUY intent to the paper account lifecycle and retain SKIP/rejection traces too.
- **Live:** reserve a separate binding/API, initially disabled. Do not permit a manifest or Git merge to access keys, mutate a real wallet, start services, or place orders. A future live implementation must consume the same versioned decision policy through its own quote/fill, risk, reconciliation, and kill-switch boundary, with explicit approval and evidence from fresh paper.

A narrow shared `load_lane_release(path, expected_sha256)` can validate schema, content hash, supported lane/policy providers, and installed commit, then return a frozen effective definition. Its adapters are the existing `scripts/collector_lane_replay.py`, `bot/paper_shadow_lanes.py`/`bot/paper_wallet_runner.py`, and (later) the live integration. Do not build a second evaluator merely for manifest ingestion. Existing configs without `lane_release_path` retain their behavior; release mode must not silently deep-merge newer inline/default parameters into the frozen version.

## Promotion behavior and evidence

`experiment → freeze release → comparable blind replay → fresh forward paper → separately approved live`

- **Freeze:** select the specific lane branches/policies, capture the effective config and pinned commit, compute the digest, and validate that the installed source can execute them. An agent can propose new versions and compare many releases, but cannot promote one by merely selecting the highest in-sample P&L.
- **Replay → paper:** an operator selects the release and a *new* cohort after checking identical candidate coverage versus control, input/quote coverage, history eligibility, reason-code distribution, and whether the claimed wallet path actually ran. If only diagnostic replay exists, it may motivate a shadow comparison but is not labelled paper-wallet parity. No arbitrary profitability threshold is invented by this contract.
- **Paper → live:** require forward, independently settled, full-overlap control comparison and review of fees, exposure, drawdown, missing data, and execution differences. Live enablement is a distinct authorization/rollout task. Rollback switches a stage binding to a previously verified release for a *new* cohort or explicit forward transition; prior decisions and artifacts stay attributable to the old release.

Every candidate trace records `captured → input contract → history eligibility → route/action → selected-side quote/bracket → sizing/risk → paper reservation/skip → separate resolution`, with stable reason codes and snapshot/market IDs. Outcome data does not reach the decision phase; `VOID`, ambiguous, untimed, or unbound outcomes remain visible without invented economic P&L. Compare releases by matched candidate IDs, not by summing disjoint ledgers.

## Small implementation slices / acceptance

1. **Freeze + read-only loader:** test full effective-config capture, canonical-document digest shared by release and binding, commit pin, optional branch-hint movement, unknown provider, changed effective value, missing config, and no runtime activation. This slice can make existing replay/shadow runs attributable to a release without claiming paper parity.
2. **Replay adapter + trace:** synthetic shared candidates run two releases on identical snapshots, persist complete decision/SKIP provenance, and reject outcome leakage, missing selected-side quote for economics, or future-settled Router history. Existing diagnostic reports remain marked `promotion_eligible: false`.
3. **Paper-wallet bridge:** characterize and reuse the actual paper sizing/risk/account lifecycle; test pending exposure, duplicate/event/re-entry rules, fees/rounding, exact independent settlement, and VOID against replay on a deterministic timeline. Then consume the *same* release in a fresh paper cohort from the one collector feed. Only this bridge can justify a paper-shaped promotion claim.
4. **Stage selection smoke:** prepare paper binding disabled; verify `start_at`, collector manifest, allowed control/candidate IDs, both policy hashes, evaluator version, exact snapshot identity, and decision idempotency against the existing forward handoff. Reject a diagnostic-only or unsupported lane release, pre-start/missing/stale/mismatched snapshots, and any action triggered merely by loading a binding. Compare reason-code receipts. Enabling paper, deploying, pushing, and any live order are separate authorized steps, not acceptance for this spec.

**Open engineering gate, not a reason to expand this schema:** cross-month direct history currently needs explicit verified archive/index/resolution inputs and may do expensive per-candidate validation/hydration. Measure and bound it before a continuously running forward paper Router relies on it. Retained real-archive coverage and wallet replay parity have not been established by the local synthetic code tests alone.
