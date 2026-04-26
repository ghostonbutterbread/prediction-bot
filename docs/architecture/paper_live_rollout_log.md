# Paper ↔ Live Rollout Log

## capital_constrained_standby_mode

### Intent
Pause active execution scanning when capital constraints make meaningful new trades unlikely, then resume only after trading capacity materially improves.

### Shared design surface
Shared execution policy expressed through the risk/state layer, with paper-mode enforcement first and live reuse planned through the same standby state machine concepts.

### Paper implementation status
- status: partial
- key files changed:
  - `bot/risk.py`
  - `bot/simulator.py`
  - `bot/paper_adapters.py`
  - `paper_loop.py`
  - `bot/dashboard.py`
  - `tests/test_risk_and_simulator.py`
  - `tests/test_status_module.py`
- tests added:
  - standby entry threshold coverage
  - standby resume coverage
  - simulator standby scan coverage
  - status formatting coverage

### Live implementation target
- hook into live scan/execution flow after shared risk blockers are surfaced in `bot/runner.py`
- reuse the same blocker classification and resume checks where live account/order state can provide reliable capacity data
- verify live parity for available cash, exposure headroom, and open-position counts before enabling live standby

### Promotion blockers
- live runner does not yet honor standby state
- live account state may need richer exposure/headroom inputs for parity
- no live-specific observability yet for standby transitions

### Observability
- persisted standby state in `RiskState`
- status snapshot/status message fields for standby activity and resume reason
- paper summary logs include standby state, reasons, and useful trade capacity
- dashboard shows standby banner while active

### Rollout recommendation
paper-ready / live-design-ready
