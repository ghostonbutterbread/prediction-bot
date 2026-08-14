# Disabled Forward Source-Router Paper Handoff

**Status:** manifest contract only; disabled and not activated.

The handoff builder creates one fresh control-versus-router cohort manifest. It
does not start a collector, evaluator, resolver, wallet, service, or timer.

```text
one collector snapshot manifest
        │
        ├── control_stable decision/cursor/paper-wallet namespaces
        └── shadow_source_router_no_price_guard decision/cursor/paper-wallet namespaces
```

Both lanes must validate the same complete collector decision identity after the
manifest's UTC start time. Pre-start snapshots, a different collector manifest,
or a control/candidate identity mismatch fail closed.

Decision idempotency is the SHA-256 of:

```text
(decision_key, environment, lane_id, policy_bundle_hash,
 evaluator_version, decision_role)
```

Execution-intent and paper-wallet identities are intentionally separate and
lane-scoped. A future `live` environment can reserve distinct names only; this
contract never enables it.

The router may receive only decision-time collector fields. Resolution, wallet,
P&L, payout, outcome, and settlement inputs are rejected. Forward paper still
requires a recorded executable quote before Kelly/capacity or P&L can be
evaluated; the current archive does not justify inventing either.
