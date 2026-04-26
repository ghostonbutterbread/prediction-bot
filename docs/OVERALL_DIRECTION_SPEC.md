# Prediction Bot — Overall Direction Spec

_Last updated: 2026-04-26_

## Purpose

This is the high-level routing spec for the current `feature/prediction-lab` branch and the next branch after it.

It exists to answer:
- what this branch is trying to accomplish
- what is good enough to checkpoint now
- what is not finished yet
- what the next implementation branch should focus on
- which more detailed specs govern each workstream

This is the umbrella doc. The detailed docs below remain the source of truth for their own lanes.

---

## Current Branch Thesis

This branch currently represents two related but distinct outcomes:

1. **Prediction Lab**
   - a long-run research/data-collection environment
   - meant to score decisions at scale
   - meant to show what the bot's logic would do across much more of the market than normal constrained trading
   - useful for repeated observation, data collection, and training/review loops

2. **Parity groundwork**
   - paper and live now share much more of the decision path
   - paper can more closely approximate live execution-time decision behavior
   - audit and parity visibility are materially better than before
   - parity is no longer the main blocker for moving the project forward

The branch does **not** need to make either of these perfect right now.
It needs to make them good enough for their intended jobs, clean enough to commit, and clear enough to build on safely.

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
**Relatively complete for this branch's purpose.**

That means:
- the core concept appears implemented strongly enough
- the remaining work is more about polish, ops, and long-run ergonomics than validating the main idea

### Parity status
**Substantially advanced, but not fully complete.**

What is strong now:
- shared decision logic
- execution-time revalidation path
- parity metadata/audit groundwork
- first parity reporting surface

What is still incomplete:
- stricter canonical write-time execution/audit row contract
- stronger parity diff/report ergonomics
- broader restart/recovery parity proofs
- more lifecycle/report hardening around edge states

### Live operational readiness status
**Not ready for unattended live yet.**

The next real blocker is no longer "does parity exist?"
The next blocker is:

> **is live behavior safe and trustworthy under messy real conditions?**

---

## Strategic Direction From Here

### Recommended sequence
1. **Get this branch into a clean, committable state**
2. **Commit and merge the Prediction Lab + current parity groundwork**
3. **Open a new branch for live lifecycle hardening**
4. **Return after that pass to the remaining parity follow-ups**

This sequence is intentional.

If we keep trying to perfect parity before checkpointing, we risk mixing:
- Prediction Lab work
- parity cleanup
- live lifecycle hardening

into one muddy branch.

The cleaner split is:
- **current branch** = Prediction Lab + parity checkpoint
- **next branch** = live operational hardening
- **later follow-up** = remaining parity cleanup after live hardening reaches a stable merge point

---

## What The Next Branch Should Focus On

The next branch should focus on **live correctness**, not new parity ambition.

Main goals:
- canonical live lifecycle states
- reconciliation severity and correction rules
- blocked/degraded/safe startup behavior
- restart/recovery safety with unresolved orders/positions
- duplicate-submission and uncertain-confirmation protection
- stronger settlement/accounting semantics
- kill-switch/invariant behavior when state becomes contradictory

This is the path from:
- parity-capable / canary-capable

to:
- unattended-live-trustworthy

---

## Deferred Work We Are Explicitly Not Losing

The following parity items are intentionally deferred, not abandoned:
- canonical execution/audit row enforcement across paper and live
- richer parity diff/report ergonomics
- broader restart/recovery parity proofs
- remaining lifecycle/report cleanup that improves parity visibility

These should stay documented in:
- project todo
- parity spec
- live hardening spec sequencing notes

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
- the next branch after this one
- live lifecycle safety
- reconciliation and restart/recovery
- canary-to-unattended-live hardening

### 6. `docs/LIVE_CANARY_READINESS.md`
Use for:
- a tiny supervised live canary only
- risk caps, launch checklist, and readiness framing

---

## Practical Working Rule

When deciding whether work belongs on this branch or the next one, ask:

### Keep it on this branch if it primarily improves:
- Prediction Lab's ability to run long-term and collect useful decision data
- parity's ability to explain decision drift clearly enough for current review
- branch cleanup needed to commit/merge this work coherently

### Push it to the next branch if it primarily improves:
- live order lifecycle correctness
- reconciliation safety
- restart/recovery behavior
- exchange-truth enforcement
- unattended-live safety

---

## Bottom Line

- **Prediction Lab**: good enough if it supports long-run scaled decision capture with controlled intervals and useful logging
- **Parity**: not finished, but strong enough to checkpoint
- **Live hardening**: the next most important implementation branch
- **Recommended move**: clean and merge this branch, then branch again for live lifecycle work

That keeps the work legible, versionable, and easier to review.
