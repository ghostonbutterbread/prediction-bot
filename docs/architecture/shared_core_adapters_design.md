# Shared Core + Adapters Design

## Goal
Refactor the prediction bot so paper and live trading share the same decision engine, while execution/state handling is separated behind adapters.

## Why
Right now, paper improvements risk drifting away from what live trading will actually do.
We want paper mode to act as a realistic preflight environment, not a separate fake universe.

---

## Architecture Overview

### 1. Shared Core
This layer should contain logic that is identical in paper and live modes.

Owns:
- strategy engine
- signal generation
- source trust / weather registry logic
- position sizing inputs
- risk policy rules
- trade decision schema
- trade justification / reasoning

Does NOT own:
- wallet balance source
- order execution
- fill state
- settlement source
- persistence details specific to one mode

### 2. State Adapter
Provides the core with the current account state.

Paper state adapter:
- available cash
- reserved capital
- open simulated positions
- simulated realized P&L

Live state adapter:
- wallet balance from API
- open positions from API
- realized/unrealized P&L from API when available
- exchange-specific position metadata

### 3. Execution Adapter
Responsible for placing or simulating trades.

Paper execution adapter:
- simulate entry
- reserve capital
- record synthetic fills

Live execution adapter:
- place real orders
- fetch fill/order state from API
- handle partial fills/cancellations/exchange errors

### 4. Resolution Adapter
Responsible for market outcome and settlement data.

Paper resolution adapter:
- simulate resolution from market data / resolver logic

Live resolution adapter:
- trust exchange/API settlement data first
- do not reconstruct what the exchange already knows unless needed for audit

---

## Recommended Shared Interfaces

### Decision Input
```python
@dataclass
class TradeContext:
    market_id: str
    question: str
    market_price: float | None
    yes_price: float | None
    no_price: float | None
    metadata: dict
    account_state: AccountState
    source_context: dict
```

### Decision Output
```python
@dataclass
class TradeDecision:
    action: str          # BUY_YES / BUY_NO / SKIP
    confidence: float
    edge: float | None
    position_size: float | None
    reasoning: dict
```

### Adapter Interfaces
```python
class AccountStateProvider:
    def get_account_state(self) -> AccountState: ...

class StateAdapter(AccountStateProvider):
    def list_open_positions(self) -> Sequence[PositionState]: ...

class PaperStateAdapter(StateAdapter):
    def get_paper_session_state(self) -> PaperSessionState: ...

class LiveStateAdapter(StateAdapter):
    def list_resting_orders(self) -> Sequence[OrderState]: ...

class ExecutionAdapter:
    def execute(self, decision: TradeDecision, context: TradeContext) -> ExecutionResult: ...

class PaperExecutionAdapter(ExecutionAdapter):
    ...

class LiveExecutionAdapter(ExecutionAdapter):
    def get_order_status(self, order_id: str) -> OrderState | None: ...
    def cancel_order(self, order_id: str) -> CancelOrderResult: ...

class ResolutionAdapter:
    def resolve_open_positions(self) -> list[ResolutionEvent]: ...
```

### Supporting Data Shapes
- `PositionState`
  Open position snapshot shared across paper and live adapters.
- `PaperSessionState`
  Paper-only session metadata such as `session_id`, scan counters, and storage path.
- `OrderState`
  Exchange order lifecycle snapshot for live execution and live state polling.
- `CancelOrderResult`
  Stable cancellation response shape for live adapters.

---

## Migration Strategy

### Phase 1 — Spec + interface layer
- define adapter interfaces
- define shared account/decision dataclasses
- do not rewrite everything yet

### Phase 2 — Paper adapter extraction
- move simulator-specific wallet/execution logic behind paper adapters
- keep existing behavior but route through interfaces

### Phase 3 — Live adapter scaffold
- define live wallet/order/resolution adapter shells
- pull real balance/position/resolution data from API where possible

### Phase 4 — Shared decision pipeline
- route both paper and live through the same trade decision path

---

## Design Rules

1. **Shared logic first**
   If code answers "should we trade?", it belongs in shared core.

2. **Adapters own environment differences**
   If code answers "how do we execute in this mode?", it belongs in an adapter.

3. **Live trusts API truth**
   If live API provides balance, fills, or settlement, use that instead of reconstructing it.

4. **Paper should mimic live constraints**
   Reserve cash, track positions realistically, and avoid fantasy accounting.

5. **Reasoning should be portable**
   The same explanation for why a trade was chosen should work in both paper and live.

---

## Risks to Watch
- paper/live divergence if shared logic leaks back into adapters
- exchange-specific hacks creeping into core
- duplicated risk logic in multiple places
- paper mode using assumptions live mode never sees

---

## Recommended Next Build Slice (later)
1. Add shared dataclasses/interfaces
2. Extract paper account state provider
3. Extract paper execution adapter
4. Leave live adapter mostly stubbed but shaped correctly

This keeps the refactor controlled and reviewable.
