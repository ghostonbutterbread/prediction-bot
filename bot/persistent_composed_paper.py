"""Explicit, disabled-by-default paper-only entry from a saved pre-risk selector.

This consumer never calls a resolver. An uncertain journal entry remains pending
rather than replaying a possibly executed BUY. Only a saved Simulator trade marker
can reconcile it; no exchange fill or economic result is asserted here.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
import fcntl
import hashlib
import json
import os

from bot.file_ops import atomic_write_json
from bot.forward_composed_paper import PICKER, CONTROL, _IDENTITY, _digest as forward_digest, compose_forward_rows, validate_forward_combination
from bot.market_router import route_market
from bot.shared_market_runtime import shared_snapshot_is_fresh
from bot.simulator import Simulator
from scripts.paper_shadow_lane_compose_replay import _load_config, validate_saved_combination

_RISK_ENV_KEYS = (
    "KELLY_FRACTION", "MAX_BET_PCT", "KALSHI_FEE_RATE", "MAX_EXPOSURE_PCT",
    "DAILY_LOSS_LIMIT_PCT", "MAX_DRAWDOWN_PCT", "MAX_OPEN_POSITIONS",
    "COOLDOWN_AFTER_LOSSES", "MAX_TRADABLE_BALANCE_USD", "MAX_POSITION_SIZE_USD",
    "MAX_EVENT_EXPOSURE_PCT", "MAX_EVENT_POSITIONS", "RETRADE_EDGE_PREMIUM",
    "RETRADE_CONFIDENCE_PREMIUM", "RETRADE_SIZE_DECAY", "MIN_RETRADE_NET_EDGE",
    "MIN_RETRADE_EXPECTED_PROFIT_USD", "PRICE_IMPROVEMENT_TICKS", "FORCE_RESUME",
)


def consume_persistent_composed_paper(
    *, saved_config_path: str | Path, lane_rows: Sequence[Mapping[str, Any]],
    cohort_id: str, publisher_state: Mapping[str, Any], paper_root: str | Path,
    protected_roots: Mapping[str, str | Path], account_config: Mapping[str, Any],
    enabled: bool = False, now: datetime | None = None, max_snapshot_age_seconds: int = 120,
) -> dict[str, Any]:
    """Enter qualified decisions into one isolated Simulator account, or do nothing.

    ``paper_root`` is the final Simulator ``paper`` data directory. Callers must
    supply the current stable, beta and live roots explicitly; no default path is
    assumed. The cohort is append-only and includes the latest publisher snapshot.
    No settlement, scans, exchange access, or live order path is used.
    """
    if not enabled:
        return {"status": "skipped", "reason_code": "paper_consumer_disabled", "paper_only": True}
    root = _preflight_root(paper_root, protected_roots)
    content = Path(saved_config_path).read_bytes()
    combination = validate_saved_combination(_load_config(Path(saved_config_path), content=content))
    validate_forward_combination(combination)
    if combination["base_lane"] != PICKER:
        raise ValueError("persistent composed account requires a pre-risk picker selection")
    if not isinstance(cohort_id, str) or not cohort_id.strip():
        raise ValueError("cohort_id is required")
    if not isinstance(lane_rows, Sequence) or not lane_rows:
        raise ValueError("complete pre-risk lane cohort is required")
    snapshot = publisher_state.get("latest_snapshot") if isinstance(publisher_state, Mapping) else None
    publisher = publisher_state.get("publisher") if isinstance(publisher_state, Mapping) else None
    if (not isinstance(snapshot, Mapping) or not isinstance(publisher, Mapping)
            or not snapshot.get("snapshot_id") or not publisher.get("runtime_kind")
            or not publisher.get("instance_id")
            or snapshot.get("publisher_runtime") != publisher.get("runtime_kind")
            or snapshot.get("publisher_instance_id") != publisher.get("instance_id")
            or not shared_snapshot_is_fresh(snapshot, now=now,
                                            max_snapshot_age_seconds=max_snapshot_age_seconds)):
        raise ValueError("shared snapshot stale or publisher mismatched")
    latest = [row for row in lane_rows if row.get("shared_snapshot_id") == snapshot["snapshot_id"]]
    controls = Counter(tuple(str(row.get(k) or "") for k in _IDENTITY)
                       for row in latest if row.get("policy") == CONTROL)
    pickers = Counter(tuple(str(row.get(k) or "") for k in _IDENTITY)
                      for row in latest if row.get("policy") == PICKER)
    if not controls or any(pickers[key] != 1 or not all(key) for key in controls):
        raise ValueError("pre-risk picker incomplete for latest control snapshot")
    # The caller may supply an append-only ledger; only the fresh publisher
    # snapshot is eligible for new persistent entries.
    pairs, intents = compose_forward_rows(latest, combination=combination)
    selected = {intent["decision_id"]: intent for intent in intents
                if intent["lane_id"] == "forward_composed_paper"}
    prepared = []
    for pair in pairs:
        decision_id = "forward-combined:" + _identity_digest(pair)
        intent = selected.get(decision_id)
        if pair["combined_action"] in {"BUY_YES", "BUY_NO"} and intent is None:
            raise ValueError("composed BUY lacks complete qualified intent")
        signal = _prepare_signal(intent, pair, latest) if intent is not None else None
        prepared.append((decision_id, pair, signal))
    if not prepared:
        raise ValueError("cohort has no composed decisions")

    # Bind the exact saved bytes, not the name/normalized defaults. Preflight
    # account settings too: a changed sizing/risk policy cannot reuse the wallet.
    settings = _account_settings(account_config, root)
    binding = {"schema_name": "persistent_composed_paper_binding", "cohort_id": cohort_id,
               "composition_sha256": hashlib.sha256(content).hexdigest(),
               "account_sha256": _digest(settings),
               "risk_environment_sha256": _digest({key: os.environ.get(key) for key in _RISK_ENV_KEYS}),
               "paper_only": True}
    binding_path = root / "composed_binding.json"
    if root.exists():
        if not binding_path.is_file():
            raise ValueError("paper root already exists without a composed account binding")
        if json.loads(binding_path.read_text()) != binding:
            raise ValueError("paper root config/cohort/account binding mismatch")
    else:
        root.mkdir(parents=True)
        atomic_write_json(binding_path, binding)
    with (root / "composed.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Recheck under the lock, including previously saved trade/session state.
        if json.loads(binding_path.read_text()) != binding:
            raise ValueError("paper root binding changed")
        journal_dir = root / "composed_decisions"
        journal_dir.mkdir(exist_ok=True)
        sessions = list(root.glob("sim_*.json"))
        if len(sessions) > 1:
            raise ValueError("ambiguous composed Simulator sessions")
        saved_trades = []
        if sessions:
            session = json.loads(sessions[0].read_text())
            if (not isinstance(session.get("trades"), list) or not isinstance(session.get("session_id"), str)
                    or sessions[0].name != f"sim_{session['session_id']}.json"):
                raise ValueError("invalid composed Simulator session")
            saved_trades = session["trades"]
            if any(trade.get("resolved") or trade.get("outcome") is not None or trade.get("pnl") is not None
                   for trade in saved_trades):
                raise ValueError("external settlement detected; composed positions must remain open")
        marked = {}
        for trade in saved_trades:
            marker = (trade.get("signals") or {}).get("composed_decision_id")
            if marker:
                if marker in marked:
                    raise ValueError("duplicate composed trade marker")
                marked[marker] = trade
        # Reconcile the whole journal, not just rows in this batch: an older
        # uncertain entry must fence every new BUY until its trade is located.
        unresolved = False
        for existing in journal_dir.glob("*.json"):
            prior = json.loads(existing.read_text())
            marker = prior.get("decision_id")
            if prior.get("status") == "pending":
                if marker in marked:
                    prior.update(status="opened", trade_id=marked[marker]["id"])
                    atomic_write_json(existing, prior)
                else:
                    unresolved = True
            elif prior.get("status") == "opened" and marker not in marked:
                raise ValueError("journal opened trade absent from saved Simulator session")
            elif prior.get("status") not in {"opened", "skipped"}:
                raise ValueError("unknown composed journal status")
        # Never let Simulator silently start a fresh account after failed load.
        simulator = None
        results = []
        for decision_id, pair, signal in prepared:
            path = journal_dir / (decision_id.removeprefix("forward-combined:") + ".json")
            fingerprint = _digest({"pair": pair, "signal": signal})
            if path.exists():
                entry = json.loads(path.read_text())
                if entry.get("decision_id") != decision_id or entry.get("fingerprint") != fingerprint:
                    raise ValueError("processed decision identity changed")
                if entry.get("status") == "pending":
                    trade = marked.get(decision_id)
                    if trade is not None:
                        entry.update(status="opened", trade_id=trade["id"])
                        atomic_write_json(path, entry)
                        results.append(_receipt(entry, status="already_processed"))
                    else:
                        results.append(_receipt(entry, status="pending_review"))
                elif entry.get("status") in {"opened", "skipped"}:
                    if entry["status"] == "opened" and decision_id not in marked:
                        raise ValueError("journal opened trade absent from saved Simulator session")
                    results.append(_receipt(entry, status="already_processed"))
                else:
                    raise ValueError("unknown composed journal status")
                continue
            if decision_id in marked:
                raise ValueError("unbound composed trade marker in saved session")
            if signal is not None and unresolved:
                results.append({"decision_id": decision_id, "status": "blocked_pending_review",
                                "reason_code": "unreconciled_entry", "fill_basis": None, "trade_id": None})
                continue
            if signal is None:
                entry = {"decision_id": decision_id, "fingerprint": fingerprint, "status": "skipped",
                         "reason_code": pair["reason_code"]}
                _exclusive_json(path, entry)
                results.append(_receipt(entry))
                continue
            # Write-ahead fence: if execution or session save fails, never retry
            # this decision automatically, even if no saved trade can be found.
            entry = {"decision_id": decision_id, "fingerprint": fingerprint, "status": "pending",
                     "reason_code": "entry_pending_review"}
            _exclusive_json(path, entry)
            if simulator is None:
                simulator = Simulator(settings)
                if sessions and simulator.session_id != session["session_id"]:
                    raise ValueError("Simulator failed to reload bound paper session")
            blockers = Counter()
            try:
                trade = simulator.submit_paper_signal(signal, blockers)
            except Exception:
                # The pending entry is deliberate; do not turn ambiguous writes
                # into a retried BUY.
                raise
            if trade is not None:
                entry.update(status="opened", trade_id=trade.id,
                             reason_code="paper_simulated_entry")
                marked[decision_id] = {"id": trade.id}
            else:
                entry.update(status="skipped", reason_code=next(iter(blockers), "simulator_rejected"))
            atomic_write_json(path, entry)
            results.append(_receipt(entry))
        return {"status": "processed", "paper_only": True, "cohort_id": cohort_id,
                "composition_sha256": binding["composition_sha256"], "decisions": results,
                "open_positions": len([t for t in saved_trades if not t.get("resolved")]) if simulator is None else len(simulator.list_open_positions())}


def _preflight_root(raw: str | Path, protected: Mapping[str, str | Path]) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.name != "paper":
        raise ValueError("dedicated paper root must be an absolute paper directory")
    root = path.resolve()
    if set(protected) != {"stable", "beta", "live"}:
        raise ValueError("explicit stable, beta and live protected roots required")
    for name, raw_other in protected.items():
        other_path = Path(raw_other).expanduser()
        if not other_path.is_absolute():
            raise ValueError(f"{name} root must be absolute")
        other = other_path.resolve()
        if root == other or root in other.parents or other in root.parents:
            raise ValueError(f"dedicated paper root overlaps {name} root")
    return root


def _account_settings(config: Mapping[str, Any], root: Path) -> dict[str, Any]:
    # Only the paper sizing/decision inputs; never import a runtime's data paths,
    # publishers, wallet IDs, parity mode or trading/live switches.
    allowed = {"starting_balance", "risk", "strategy", "trade_economics", "kalshi_fee_rate",
               "min_edge", "min_confidence", "max_entry_price", "strategy_lanes", "strategy_policy"}
    unknown = set(config) - allowed - {"enable_social"}
    if unknown:
        raise ValueError(f"unsupported composed account setting: {sorted(unknown)[0]}")
    return {**{key: config[key] for key in allowed if key in config}, "data_dir": str(root),
            "trading": {"mode": "paper"}, "enable_social": False,
            "paper_wallets": {"allow_legacy_parent_session_lookup": False}}


def _prepare_signal(intent: Mapping[str, Any], pair: Mapping[str, Any],
                    lane_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    action = intent["action"]
    price = intent["entry_price"]
    probability = intent["model_probability"]
    selected = probability if action == "BUY_YES" else 1 - probability
    if not 0 < selected < 1 or not 0 < price < 1:
        raise ValueError("invalid selected-side probability or conservative estimate")
    guard_rows = [row for row in lane_rows if row.get("decision_id") == pair["guard_decision_id"]
                  and all(row.get(key) == pair[key] for key in _IDENTITY)]
    if len(guard_rows) != 1:
        raise ValueError("missing unique selected-side guard quote")
    economics = (guard_rows[0].get("provenance") or {}).get("payout_aware") or {}
    quote = economics.get("selected_side_ask")
    if (economics.get("quote_not_fill") is not True or isinstance(quote, bool)
            or not isinstance(quote, (int, float)) or not 0 < quote <= price
            or economics.get("entry_price") != price):
        raise ValueError("selected-side recorded ask/estimate mismatch")
    route = intent["source_context"]["market_route"]
    signal = {"market_id": intent["market_id"], "question": intent["question"],
              "exchange": intent["exchange"], "direction": action,
              "model_probability": probability, "confidence": intent["confidence"],
              "market_price": price, "edge": selected - price,
              "event_ticker": intent["event_key"],
              "shared_candidate_id": intent["shared_candidate_id"],
              "signals": {"composed_decision_id": intent["decision_id"],
                          "shared_candidate_id": intent["shared_candidate_id"],
                          "run_id": intent["run_id"], "shared_snapshot_id": intent["shared_snapshot_id"],
                          "selected_side_recorded_ask": quote, "conservative_estimate": price,
                          "quote_not_fill": True}}
    # Simulator re-routes from the market identity and question; it must agree
    # with the selected recorded route before a journal or account is created.
    if route.get("allowed") is not True or route.get("handler_id") != route_market(signal, {"scan": {
            "allowed_market_routes": ["weather.daily_temperature"]}}).handler_id:
        raise ValueError("saved market route does not match Simulator paper route")
    side_key = "yes_price" if action == "BUY_YES" else "no_price"
    ask_key = "best_yes_ask" if action == "BUY_YES" else "best_no_ask"
    signal[side_key] = price
    signal[ask_key] = quote
    return signal


def _identity_digest(row: Mapping[str, Any]) -> str:
    return forward_digest({key: row[key] for key in _IDENTITY})


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _receipt(entry: Mapping[str, Any], *, status: str | None = None) -> dict[str, Any]:
    return {"decision_id": entry["decision_id"], "status": status or entry["status"],
            "reason_code": entry["reason_code"], "trade_id": entry.get("trade_id"),
            "fill_basis": "paper_simulated_not_exchange_fill" if entry.get("trade_id") else None}
