#!/usr/bin/env python3
"""Audit and optionally repair paper session resolutions against Kalshi public market results.

This script is intentionally conservative:
- finalized Kalshi markets use the public `result` field as source of truth
- closed-but-unsettled daily weather markets can optionally be resolved from NWS CLI reports
- active markets remain open
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.trade_audit import calculate_realized_accounting, enrich_trade_audit_fields, summarize_event_performance

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = "prediction-bot-paper-audit/1.0"

CLI_BY_NAME = {
    "Miami": "MIA",
    "Miami International Airport": "MIA",
    "Central Park, New York": "NYC",
    "NYC": "NYC",
    "Chicago Midway, IL": "MDW",
    "Austin": "AUS",
    "Austin Bergstrom": "AUS",
    "Seattle": "SEA",
    "Philadelphia International Airport": "PHL",
    "Miami": "MIA",
    "Phoenix": "PHX",
    "Minneapolis": "MSP",
}


def get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def market(ticker: str) -> dict[str, Any]:
    return get_json(f"{KALSHI}/markets/{ticker}").get("market", {})


def nws_products(cli: str) -> list[dict[str, Any]]:
    data = get_json(f"https://api.weather.gov/products/types/CLI/locations/{cli}")
    return data.get("@graph", []) or []


def nws_text(product_id: str) -> str:
    return get_json(f"https://api.weather.gov/products/{product_id}").get("productText", "")


def parse_cli_values(text: str) -> tuple[int | None, int | None]:
    max_v = min_v = None
    for line in text.splitlines():
        m = re.match(r"\s*MAXIMUM\s+(-?\d+)\b", line)
        if m:
            max_v = int(m.group(1))
        m = re.match(r"\s*MINIMUM\s+(-?\d+)\b", line)
        if m:
            min_v = int(m.group(1))
    return max_v, min_v


def find_cli_report(cli: str, yyyymmdd: str) -> tuple[str | None, int | None, int | None]:
    # Products are newest first. Match the climate summary date text for the target date.
    yyyy, mm, dd = int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])
    month = datetime(yyyy, mm, dd).strftime("%B").upper()
    needle = f"FOR {month} {dd} {yyyy}"
    for item in nws_products(cli)[:30]:
        pid = item.get("id")
        if not pid:
            continue
        txt = nws_text(pid)
        if needle in txt.upper():
            mx, mn = parse_cli_values(txt)
            return pid, mx, mn
        time.sleep(0.05)
    return None, None, None


def cli_from_rules(m: dict[str, Any]) -> str | None:
    text = f"{m.get('rules_primary') or ''}\n{m.get('rules_secondary') or ''}"
    mm = re.search(r"Data for CLI([A-Z0-9]+)", text)
    if mm:
        return mm.group(1)
    # fallback by location phrase in rules_primary
    rp = m.get("rules_primary") or ""
    for name, cli in sorted(CLI_BY_NAME.items(), key=lambda x: -len(x[0])):
        if name in rp:
            return cli
    # fallback series snippets
    ticker = m.get("ticker", "")
    for prefix, cli in {
        "MIA": "MIA", "NY": "NYC", "CHI": "MDW", "AUS": "AUS", "TSEA": "SEA",
        "PHIL": "PHL", "TPHX": "PHX", "PHX": "PHX", "TMIN": "MSP", "DEN": "DEN",
        "TATL": "ATL", "ATL": "ATL", "TDAL": "DFW", "TDEN": "DEN", "LAX": "LAX",
        "TSFO": "SFO", "TOKC": "OKC", "TLV": "LAS", "THOU": "IAH", "TSATX": "SAT",
    }.items():
        if f"KXHIGH{prefix}" in ticker or f"KXLOW{prefix}" in ticker:
            return cli
    return None


def date_from_ticker(ticker: str) -> str | None:
    mm = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker)
    if not mm:
        return None
    yy, mon, dd = mm.groups()
    months = {m.upper(): i for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
    return f"20{yy}{months[mon]:02d}{int(dd):02d}"


def outcome_from_weather(m: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    ticker = m.get("ticker", "")
    ymd = date_from_ticker(ticker)
    cli = cli_from_rules(m)
    if not ymd or not cli:
        return None, {"reason": "no_cli_or_date", "cli": cli, "date": ymd}
    pid, mx, mn = find_cli_report(cli, ymd)
    if mx is None and mn is None:
        return None, {"reason": "no_nws_report", "cli": cli, "date": ymd, "product": pid}
    rp = m.get("rules_primary") or ""
    temp_kind = "minimum" if re.search(r"minimum|min temp", rp, re.I) or "KXLOW" in ticker else "maximum"
    actual = mn if temp_kind == "minimum" else mx
    if actual is None:
        return None, {"reason": "missing_actual", "cli": cli, "date": ymd, "product": pid, "max": mx, "min": mn}
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    strike_type = str(m.get("strike_type") or "").lower()
    yes = None
    # Use explicit rules text for greater/less when available because T tickers can be top or bottom tail.
    if re.search(r"greater than\s+(-?\d+)", rp, re.I):
        threshold = int(re.search(r"greater than\s+(-?\d+)", rp, re.I).group(1))
        yes = actual > threshold
    elif re.search(r"less than\s+(-?\d+)", rp, re.I):
        threshold = int(re.search(r"less than\s+(-?\d+)", rp, re.I).group(1))
        yes = actual < threshold
    elif floor is not None and cap is not None:
        yes = float(floor) <= actual <= float(cap)
    elif strike_type == "between" and floor is not None and cap is not None:
        yes = float(floor) <= actual <= float(cap)
    if yes is None:
        return None, {"reason": "cannot_eval_rule", "cli": cli, "date": ymd, "product": pid, "max": mx, "min": mn, "rules": rp}
    return ("YES" if yes else "NO"), {"source": "nws_cli", "cli": cli, "product": pid, "date": ymd, "max": mx, "min": mn, "actual": actual, "temp_kind": temp_kind}


def normalized_result(m: dict[str, Any], allow_nws: bool) -> tuple[str | None, str, dict[str, Any]]:
    r = str(m.get("result") or "").strip().upper()
    if r in {"YES", "NO"}:
        return r, "kalshi_result", {}
    sv = m.get("settlement_value_dollars")
    if sv not in (None, ""):
        try:
            return ("YES" if float(sv) >= 0.5 else "NO"), "kalshi_settlement_value", {}
        except Exception:
            pass
    if allow_nws and str(m.get("status") or "").lower() in {"closed", "finalized", "settled"}:
        out, meta = outcome_from_weather(m)
        if out:
            return out, "nws_official_cli", meta
        return None, "unresolved_no_nws", meta
    return None, "unresolved", {}


def reserved_capital(trade: dict[str, Any]) -> float:
    return round(float(trade.get("reserved_capital") if trade.get("reserved_capital") is not None else trade.get("position_size") or 0), 2)


def build_report(data: dict[str, Any]) -> dict[str, Any]:
    trades = data.get("trades", [])
    resolved = [t for t in trades if t.get("resolved")]
    trusted = [t for t in resolved if t.get("integrity_status") == "ok"]
    pnls = [float(t.get("net_pnl", t.get("pnl")) or 0) for t in trusted]
    wins = [t for t in trusted if float(t.get("net_pnl", t.get("pnl")) or 0) > 0]
    losses = [t for t in trusted if float(t.get("net_pnl", t.get("pnl")) or 0) < 0]
    event_summary = summarize_event_performance(trusted)
    by_direction = Counter(t.get("direction", "unknown") for t in trades)
    return {
        "session": data.get("session_id", ""),
        "total_trades": len(trades),
        "resolved_trades": len(resolved),
        "trusted_resolved_trades": len(trusted),
        "invalid_resolved_trades": len(resolved) - len(trusted),
        "open_trades": len(trades) - len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trusted), 4) if trusted else 0,
        "starting_balance": data.get("starting_balance", 100),
        "current_balance": data.get("balance", 100),
        "total_equity": data.get("balance", 100),
        "available_cash": data.get("available_cash", data.get("balance", 100)),
        "reserved_capital": data.get("reserved_capital", 0.0),
        "pnl": round(data.get("balance", 100) - data.get("starting_balance", 100), 2),
        "pnl_pct": round((data.get("balance", 100) - data.get("starting_balance", 100)) / data.get("starting_balance", 100) * 100, 2),
        "avg_edge": round(sum(float(t.get("edge", 0) or 0) for t in trades) / len(trades), 4) if trades else 0,
        "max_edge": round(max(float(t.get("edge", 0) or 0) for t in trades), 4) if trades else 0,
        "avg_confidence": round(sum(float(t.get("confidence", 0) or 0) for t in trades) / len(trades), 4) if trades else 0,
        "avg_position_size": round(sum(float(t.get("position_size", 0) or 0) for t in trades) / len(trades), 2) if trades else 0,
        "total_exposure": data.get("reserved_capital", 0.0),
        "total_realized_pnl": round(sum(pnls), 4),
        "avg_pnl_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0,
        "resolved_events": event_summary["resolved_events"],
        "event_wins": event_summary["wins"],
        "event_losses": event_summary["losses"],
        "event_win_rate": round(event_summary["win_rate"] / 100, 4) if event_summary["resolved_events"] else 0,
        "by_direction": dict(by_direction),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?", default="data/paper/sim_20260420_194414.json")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--nws", action="store_true", help="Resolve closed/unsettled weather markets from official NWS CLI reports")
    args = ap.parse_args()
    path = Path(args.session)
    data = json.loads(path.read_text())
    original = copy.deepcopy(data)
    audit = []
    counts = Counter()
    corrected = []

    for t in data.get("trades", []):
        mid = t.get("market_id")
        if not mid:
            continue
        try:
            m = market(mid)
        except Exception as e:
            audit.append({"market_id": mid, "error": repr(e)})
            continue
        outcome, source, meta = normalized_result(m, args.nws)
        status = str(m.get("status") or "")
        prior = t.get("outcome") if t.get("resolved") else None
        counts[(status, source, outcome or "OPEN")] += 1
        row = {"market_id": mid, "status": status, "source": source, "actual_outcome": outcome, "prior_outcome": prior, "metadata": meta}
        if outcome in {"YES", "NO"}:
            entry = float(t.get("entry_price") or t.get("market_price"))
            size = float(t.get("position_size") or 0)
            acct = calculate_realized_accounting(
                direction=t.get("direction"),
                entry_price=entry,
                position_size=size,
                outcome=outcome,
                fee_rate=0.07,
            )
            old_net = t.get("net_pnl", t.get("pnl")) if t.get("resolved") else None
            if prior != outcome or old_net is None or round(float(old_net), 4) != round(float(acct["net_pnl"]), 4):
                corrected.append({
                    "market_id": mid,
                    "question": t.get("question"),
                    "direction": t.get("direction"),
                    "entry_price": entry,
                    "position_size": size,
                    "prior_outcome": prior,
                    "actual_outcome": outcome,
                    "old_net_pnl": old_net,
                    "new_net_pnl": round(acct["net_pnl"], 4),
                    "source": source,
                    "metadata": meta,
                })
            t["resolved"] = True
            t["outcome"] = outcome
            t["pnl"] = round(acct["net_pnl"], 4)
            t["resolution_type"] = "settled" if source.startswith("kalshi") else "official_nws_audit"
            t["resolved_at"] = t.get("resolved_at") or datetime.now(timezone.utc).isoformat()
            t["exit_price"] = 1.0 if outcome == "YES" else 0.0
            t["current_price"] = t["exit_price"]
            t["settlement_value"] = round(reserved_capital(t) + acct["net_pnl"], 4)
            t["audit_resolution_source"] = source
            if meta:
                t["audit_resolution_metadata"] = meta
        else:
            # If previously marked resolved but the public market is not settled and NWS couldn't resolve, reopen it.
            if t.get("resolved") and source.startswith("unresolved"):
                corrected.append({
                    "market_id": mid,
                    "question": t.get("question"),
                    "direction": t.get("direction"),
                    "prior_outcome": prior,
                    "actual_outcome": None,
                    "old_net_pnl": t.get("net_pnl", t.get("pnl")),
                    "new_net_pnl": None,
                    "source": source,
                    "metadata": meta,
                })
                t["resolved"] = False
                t["outcome"] = "pending_settlement" if status == "closed" else None
                t["resolution_type"] = "closed_unsettled" if status == "closed" else None
                t["pnl"] = None
                t["net_pnl"] = None
                t["gross_pnl"] = None
                t["fee_paid"] = None
                t["resolved_at"] = None
        enrich_trade_audit_fields(t, fee_rate=0.07)
        audit.append(row)
        time.sleep(0.05)

    total_realized = round(sum(float(t.get("net_pnl", t.get("pnl")) or 0) for t in data["trades"] if t.get("resolved") and t.get("integrity_status") == "ok"), 4)
    total_reserved = round(sum(reserved_capital(t) for t in data["trades"] if not t.get("resolved")), 2)
    data["balance"] = round(float(data.get("starting_balance", 100)) + total_realized, 2)
    data["reserved_capital"] = total_reserved
    data["available_cash"] = round(data["balance"] - total_reserved, 2)
    data["last_audited_at"] = datetime.now(timezone.utc).isoformat()
    data["report"] = build_report(data)

    outdir = ROOT / "data" / "paper" / "audits"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = outdir / f"paper_resolution_audit_{stamp}.json"
    audit_path.write_text(json.dumps({"counts": {str(k): v for k, v in counts.items()}, "corrected": corrected, "audit": audit}, indent=2))

    print(json.dumps({
        "session": data.get("session_id"),
        "trades": len(data.get("trades", [])),
        "resolved": sum(1 for t in data.get("trades", []) if t.get("resolved")),
        "open": sum(1 for t in data.get("trades", []) if not t.get("resolved")),
        "corrected_count": len(corrected),
        "old_balance": original.get("balance"),
        "new_balance": data["balance"],
        "old_available_cash": original.get("available_cash"),
        "new_available_cash": data["available_cash"],
        "old_reserved_capital": original.get("reserved_capital"),
        "new_reserved_capital": data["reserved_capital"],
        "audit_path": str(audit_path),
        "top_corrections": corrected[:10],
    }, indent=2))

    if args.write:
        backup = path.with_suffix(path.suffix + f".bak_audit_{stamp}")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(data, indent=2, default=str))
        print(f"WROTE {path} backup={backup}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
