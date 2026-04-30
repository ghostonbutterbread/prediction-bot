# Prediction Bot — Overall Direction Spec

_Last updated: 2026-04-29_

## Purpose

This is the high-level routing spec for the current prediction-bot direction.

It exists to answer:
- where the project is now
- what is already strong enough to use
- what still blocks unattended live deployment
- what the immediate high-value direction should be
- which more detailed specs govern each workstream

This is the umbrella doc. The detailed docs below remain the source of truth for their own lanes.

---

## Current Direction Thesis

The project now represents three related but distinct lanes:

1. **Prediction Lab**
   - a long-run research/data-collection and review environment
   - meant to score decisions at scale
   - meant to show what the bot's logic would do across much more of the market than normal constrained trading
   - useful for repeated observation, data collection, calibration, and training/review loops

2. **Parity + lifecycle hardening groundwork**
   - paper and live share much more of the decision path
   - paper can approximate live execution-time decision behavior more closely
   - audit, settlement, and lifecycle semantics are materially stronger than before
   - tiny supervised live is now plausible with strict caps

3. **Current immediate direction: logic tuning via Prediction Lab**
   - use Prediction Lab as the main discovery surface for improving trade-finding logic
   - keep live small and supervised while strategy quality improves
   - avoid conflating operational hardening work with logic-tuning work in one muddy pass

The project does **not** need every lane to be perfect before useful progress continues.
It needs each lane to be clear enough to drive the next decision safely.

---

## What "Good Enough" Means Right Now

### Prediction Lab
Prediction Lab should be considered successful on this branch if it can:
- run for long periods
- scan markets at controlled intervals (for example every 15 minutes)
- avoid hammering the market/API unnecessarily
- record decisions and market snapshots at scale
- let us inspect what the bot would have wanted to do across a broad surface
- support review of decision logic without requiring full live execution integration

Prediction Lab is **not** primarily a parity feature.
If it benefits from parity/shared helpers, great — but its core purpose is:

> **long-term scale, data capture, and decision-logic visibility**

The current config direction already reflects that:
- `mode: seed_and_watch`
- `collector_interval_seconds: 900`
- broad market seeding / scoring
- hypothetical notional behavior
- storage-cap-aware collection
- periodic resolution/check loops

That is close to the intended role.

### Parity
Parity should be considered successful enough to checkpoint if:
- paper and live share the main decision logic
- paper parity mode can revalidate against execution-time pricing semantics
- audit rows preserve original vs execution-time reasoning well enough to inspect decision drift
- parity reporting exists and is already useful for diagnosis

Parity is **not finished**.
But it is far enough along that the project should not stall here waiting for perfection.

---

## Current Judgment

### Prediction Lab status
**Strong enough to become the primary logic-tuning surface now.**

That means:
- the core concept appears implemented strongly enough
- the remaining work is more about analysis ergonomics, calibration loops, and long-run operator usefulness
- it should now be used to improve trade-selection quality rather than treated as a side feature

### Parity + lifecycle status
**Substantially advanced and useful, but not fully complete.**

What is strong now:
- shared decision logic
- execution-time revalidation path
- stronger audit and settlement normalization
- materially better live lifecycle/reconciliation behavior than before
- first operator-visible parity/reporting surfaces

What is still incomplete:
- stricter canonical write-time execution/audit row contract everywhere
- stronger parity diff/report ergonomics
- broader restart/recovery and messy lifecycle coverage
- richer live audit snapshots and reconciliation artifacts

### Live operational readiness status
**Reasonable for a tiny supervised canary, not ready for unattended live yet.**

The main blocker is no longer merely "does parity exist?"
The unresolved blocker is:

> **can live stay conservative and explainable under messy real conditions, while the strategy itself becomes more trustworthy?**

---

## Strategic Direction From Here

### Recommended sequence
1. **Keep docs aligned with the current live-readiness and Prediction Lab state**
2. **Use Prediction Lab as the primary discovery surface for trade-finding improvements**
3. **Implement narrow, evidence-backed logic/ranking changes one at a time**
4. **Promote only the strongest changes into tiny supervised live**
5. **Continue closing the remaining unattended-live hardening gaps in parallel or in follow-up passes**

This sequence is intentional.

If we mix too many goals into one pass, we risk blurring:
- strategy tuning
- Prediction Lab ergonomics
- parity/report cleanup
- live lifecycle hardening

The cleaner framing now is:
- **Prediction Lab lane** = discover and calibrate better trade-selection logic
- **live lane** = remain conservative and supervised while validating operational correctness
- **deployment-hardening lane** = continue reducing the gap to unattended live

---

## What The Current Direction Should Focus On

The immediate highest-value direction should focus on **better trade finding and calibration**, using Prediction Lab as the evidence surface.

Main goals:
- identify which trades the current logic is finding vs missing
- rank recurring rejection reasons and almost-good opportunities
- tune hidden-gem and market-family-specific behavior carefully
- improve confidence/ranking calibration from collected evidence
- keep live promotion narrow and supervised

In parallel, the remaining live-hardening goals stay active:
- canonical live lifecycle states
- reconciliation severity and correction rules
- blocked/degraded/safe startup behavior
- restart/recovery safety with unresolved orders/positions
- duplicate-submission and uncertain-confirmation protection
- stronger settlement/accounting semantics
- kill-switch/invariant behavior when state becomes contradictory

This is the path from:
- canary-capable and evidence-rich

to:
- genuinely trustworthy live deployment

---

## Work We Are Explicitly Not Losing

The following items remain active even while Prediction Lab tuning becomes the immediate focus:
- canonical execution/audit row enforcement across paper and live
- richer parity diff/report ergonomics
- broader restart/recovery parity proofs
- remaining lifecycle/report cleanup that improves parity visibility
- messy live lifecycle edge coverage: partials, cancels, rejects, stale orders
- reconciliation mismatch severity, artifacts, and safety-pause behavior

These should stay documented in:
- project todo
- parity spec
- live hardening spec sequencing notes
- deployment-readiness plan

So future work remains explicit and recoverable.

---

## Detailed Specs and Their Roles

### 1. `LIVE_PARITY_CHECKLIST.md`
Use for:
- quick reality check on what is done vs partial vs missing in parity
- deciding whether parity is "good enough" to checkpoint

### 2. `docs/LIVE_PARITY_MODE_SPEC.md`
Use for:
- the design intent of parity mode
- why parity exists
- what paper parity should and should not do
- config semantics and architecture rules for parity behavior

### 3. `docs/LIVE_PARITY_IMPLEMENTATION_BRIEF.md`
Use for:
- the bridge from parity build-out into row-contract/report cleanup
- understanding why schema/report quality matters before deeper hardening

### 4. `docs/EXECUTION_AUDIT_ROW_SCHEMA_SPEC.md`
Use for:
- the canonical contract for execution/audit row semantics
- shared writer/reader expectations
- invariant expectations for row fields

### 5. `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`
Use for:
- live lifecycle safety
- reconciliation and restart/recovery
- canary-to-unattended-live hardening
- the remaining deployment-safety gap

### 6. `docs/LIVE_CANARY_READINESS.md`
Use for:
- a tiny supervised live canary only
- risk caps, launch checklist, and readiness framing

### 7. `docs/PREDICTION_LAB_TUNING_DIRECTION_SPEC.md`
Use for:
- the current immediate strategy direction
- how Prediction Lab should drive trade-finding improvements
- how to promote only strong logic changes into supervised live

---

## Practical Working Rule

When deciding where work belongs, ask:

### Prioritize it now if it primarily improves:
- Prediction Lab's ability to surface useful decision data
- analysis of why trades were admitted, rejected, or almost admitted
- calibration of trade-finding and ranking logic
- parity visibility needed to explain current strategy behavior clearly

### Treat it as deployment-hardening lane if it primarily improves:
- live order lifecycle correctness
- reconciliation safety
- restart/recovery behavior
- exchange-truth enforcement
- unattended-live safety

---

## Bottom Line

- **Prediction Lab**: strong enough to become the main trade-finding and calibration surface now
- **Parity + lifecycle groundwork**: materially stronger, but still incomplete
- **Live**: good enough for tiny supervised validation, not for unattended trust
- **Recommended move**: use Prediction Lab to improve trade selection deliberately, promote only narrow wins into supervised live, and keep closing the remaining live-hardening gaps in parallel

That keeps the work legible, versionable, and easier to review.
