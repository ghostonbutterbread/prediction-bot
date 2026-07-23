#!/usr/bin/env python3
"""Build a read-only replay corpus from prediction-bot decisions and replays.

The corpus is a derived research artifact. It never mutates runtime ledgers,
wallets, accounting state, live orders, or resolution-feed state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.paper_shadow_lanes import (  # noqa: E402
    _build_lane_resolution_row,
    _resolution_index,
)

DEFAULT_OUTPUT_DIR = "data/derived_reports/unified_replay_corpus_current"
DEFAULT_DECISION_LEDGER_GLOBS = (
    "data/beta_shadow/paper/*/paper_shadow_lane_decisions.jsonl",
    "data/beta_shadow/paper/prediction_lab/agent_decisions.jsonl",
    "data/paper/agent_decisions.jsonl",
)
DEFAULT_RESOLUTION_GLOBS = (
    "data/beta_shadow/resolutions/latest_resolutions.jsonl",
    "data/beta_shadow/resolution_feed/*/latest_resolutions.jsonl",
)
DEFAULT_RESOLVED_REPLAY_GLOBS = (
    "data/summaries/lane_compositions/*/resolved_rows.jsonl",
    "data/derived_reports/*/resolved_rows.jsonl",
    "data/derived_reports/source_router_rule_discovery_current/joined_source_router_rows.jsonl",
    "data/summaries/source_router_shadow_resolved_rows*.jsonl",
    "data/summaries/scoreboard_beta_lane_resolutions*.jsonl",
)
SAFE_OUTPUT_ROOTS = (
    ROOT / "data" / "derived_reports",
    ROOT / "data" / "summaries",
    ROOT / "data" / "beta_shadow" / "summaries",
)
CORPUS_SCHEMA_VERSION = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--decision-ledger-glob", action="append", default=[])
    parser.add_argument("--decision-ledger-path", action="append", default=[])
    parser.add_argument("--resolution-glob", action="append", default=[])
    parser.add_argument("--resolution-path", action="append", default=[])
    parser.add_argument("--resolved-replay-glob", action="append", default=[])
    parser.add_argument("--resolved-replay-path", action="append", default=[])
    parser.add_argument("--no-defaults", action="store_true", help="Use only explicitly provided paths/globs.")
    parser.add_argument("--max-rows-per-source", type=int, default=None)
    parser.add_argument("--dedupe", choices=("exact", "none"), default="exact")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_unified_replay_corpus(
        output_dir=_safe_output_dir(_root_path(args.output_dir)),
        decision_paths=_discover_paths(
            explicit=args.decision_ledger_path,
            patterns=args.decision_ledger_glob,
            defaults=DEFAULT_DECISION_LEDGER_GLOBS,
            include_defaults=not args.no_defaults,
        ),
        resolution_paths=_discover_paths(
            explicit=args.resolution_path,
            patterns=args.resolution_glob,
            defaults=DEFAULT_RESOLUTION_GLOBS,
            include_defaults=not args.no_defaults,
        ),
        resolved_replay_paths=_discover_paths(
            explicit=args.resolved_replay_path,
            patterns=args.resolved_replay_glob,
            defaults=DEFAULT_RESOLVED_REPLAY_GLOBS,
            include_defaults=not args.no_defaults,
        ),
        max_rows_per_source=args.max_rows_per_source,
        dedupe=args.dedupe,
    )
    if args.format == "json":
        print(json.dumps(result["summary"], indent=2, sort_keys=True))
    else:
        print(_text_report(result["summary"]))
        print(f"output_dir={_display_path(result['output_dir'])}")
    return 0


def build_unified_replay_corpus(
    *,
    output_dir: Path,
    decision_paths: Iterable[Path],
    resolution_paths: Iterable[Path],
    resolved_replay_paths: Iterable[Path],
    max_rows_per_source: int | None = None,
    dedupe: str = "exact",
) -> dict[str, Any]:
    """Write a normalized replay corpus and compact coverage artifacts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    decision_paths = _unique_existing_paths(decision_paths)
    resolution_paths = _unique_existing_paths(resolution_paths)
    resolved_replay_paths = _unique_existing_paths(resolved_replay_paths)
    resolution_rows = _dedupe_resolution_rows(
        _iter_resolution_rows(resolution_paths, max_rows_per_source=max_rows_per_source)
    )
    resolution_idx = _resolution_index(resolution_rows)

    corpus_path = output_dir / "corpus_rows.jsonl"
    seen: set[str] = set()
    stats = _Stats()

    with corpus_path.open("w", encoding="utf-8") as handle:
        for path in decision_paths:
            for row_number, row in _iter_jsonl(path, max_rows=max_rows_per_source):
                corpus_row = _decision_corpus_row(row, path=path, row_number=row_number, resolution_index=resolution_idx)
                if _should_skip(corpus_row):
                    stats.skipped["missing_market_id"] += 1
                    continue
                if dedupe == "exact" and _dedupe_seen(corpus_row, seen):
                    stats.skipped["duplicate_exact"] += 1
                    continue
                _write_corpus_row(handle, corpus_row)
                stats.add(corpus_row)

        for path in resolved_replay_paths:
            for row_number, row in _iter_jsonl(path, max_rows=max_rows_per_source):
                corpus_row = _resolved_replay_corpus_row(row, path=path, row_number=row_number)
                if _should_skip(corpus_row):
                    stats.skipped["missing_market_id"] += 1
                    continue
                if dedupe == "exact" and _dedupe_seen(corpus_row, seen):
                    stats.skipped["duplicate_exact"] += 1
                    continue
                _write_corpus_row(handle, corpus_row)
                stats.add(corpus_row)

    summary = {
        "schema_name": "unified_replay_corpus_summary",
        "schema_version": CORPUS_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "non_mutating": True,
        "corpus_path": _display_path(corpus_path),
        "input_sources": {
            "decision_paths": [_display_path(path) for path in decision_paths],
            "resolution_paths": [_display_path(path) for path in resolution_paths],
            "resolved_replay_paths": [_display_path(path) for path in resolved_replay_paths],
        },
        "limits": {"max_rows_per_source": max_rows_per_source, "dedupe": dedupe},
        "resolution_rows_loaded": len(resolution_rows),
        **stats.summary(),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(_markdown_report(summary), encoding="utf-8")
    return {"output_dir": output_dir, "corpus_path": corpus_path, "summary": summary}


class _Stats:
    def __init__(self) -> None:
        self.rows = 0
        self.resolved_rows = 0
        self.pnl_calculable_rows = 0
        self.buy_rows = 0
        self.winning_buy_rows = 0
        self.losing_buy_rows = 0
        self.total_stake_usd = 0.0
        self.total_pnl_usd = 0.0
        self.unique_markets: set[str] = set()
        self.unique_candidates: set[str] = set()
        self.source_kind_counts: Counter[str] = Counter()
        self.lane_counts: Counter[str] = Counter()
        self.split_counts: Counter[str] = Counter()
        self.blockers: Counter[str] = Counter()
        self.skipped: Counter[str] = Counter()
        self.by_split: dict[str, dict[str, Any]] = defaultdict(_empty_metric_bucket)

    def add(self, row: Mapping[str, Any]) -> None:
        self.rows += 1
        market_id = _text(row.get("market_id"))
        candidate_id = _text(row.get("shared_candidate_id"))
        if market_id:
            self.unique_markets.add(market_id)
        if candidate_id:
            self.unique_candidates.add(candidate_id)
        self.source_kind_counts[str(row.get("source_kind") or "unknown")] += 1
        self.lane_counts[str(row.get("lane_id") or "unknown")] += 1
        split = str(row.get("split") or "unknown")
        self.split_counts[split] += 1
        blocker = _text(row.get("blocker"))
        if blocker:
            self.blockers[blocker] += 1
        if row.get("resolution_outcome") not in (None, ""):
            self.resolved_rows += 1

        pnl = _number(row.get("pnl_usd"))
        stake = _number(row.get("stake_usd"))
        if pnl is not None and stake is not None:
            self.pnl_calculable_rows += 1
            self.total_pnl_usd += pnl
            self.total_stake_usd += stake
            bucket = self.by_split[split]
            bucket["rows"] += 1
            bucket["total_pnl_usd"] += pnl
            bucket["total_stake_usd"] += stake
            if _is_buy(row.get("action")):
                self.buy_rows += 1
                bucket["buy_rows"] += 1
                if row.get("won") is True:
                    self.winning_buy_rows += 1
                    bucket["winning_buy_rows"] += 1
                elif row.get("won") is False:
                    self.losing_buy_rows += 1
                    bucket["losing_buy_rows"] += 1

    def summary(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "unique_markets": len(self.unique_markets),
            "unique_candidates": len(self.unique_candidates),
            "resolved_rows": self.resolved_rows,
            "pnl_calculable_rows": self.pnl_calculable_rows,
            "buy_rows": self.buy_rows,
            "winning_buy_rows": self.winning_buy_rows,
            "losing_buy_rows": self.losing_buy_rows,
            "win_rate_pct": _pct(self.winning_buy_rows, self.buy_rows),
            "total_stake_usd": round(self.total_stake_usd, 4),
            "total_pnl_usd": round(self.total_pnl_usd, 4),
            "roi_pct": _pct(self.total_pnl_usd, self.total_stake_usd),
            "source_kind_counts": dict(sorted(self.source_kind_counts.items())),
            "lane_counts": dict(sorted(self.lane_counts.items())),
            "split_counts": dict(sorted(self.split_counts.items())),
            "blocker_counts": dict(sorted(self.blockers.items())),
            "skipped_counts": dict(sorted(self.skipped.items())),
            "splits": {name: _finalize_metric_bucket(bucket) for name, bucket in sorted(self.by_split.items())},
        }


def _decision_corpus_row(
    row: Mapping[str, Any],
    *,
    path: Path,
    row_number: int,
    resolution_index: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _build_lane_resolution_row(row, resolution_index)
    return _corpus_row_from_resolved(resolved, source_kind="decision_ledger", path=path, row_number=row_number)


def _resolved_replay_corpus_row(row: Mapping[str, Any], *, path: Path, row_number: int) -> dict[str, Any]:
    if isinstance(row.get("pnl"), Mapping) or isinstance(row.get("resolution"), Mapping):
        return _corpus_row_from_resolved(row, source_kind="resolved_replay", path=path, row_number=row_number)
    market_id = _first_text(row.get("market_id"), row.get("ticker"), row.get("market_ticker"))
    action = _first_text(row.get("action"), row.get("recommended_action"))
    side = _first_text(row.get("side"), row.get("recommended_side"))
    pnl = _number(row.get("pnl_usd"), row.get("net_pnl"))
    stake = _number(row.get("stake_usd"), row.get("notional_usd"), row.get("position_size_usd"))
    won = row.get("won")
    if not isinstance(won, bool):
        won = _first_text(row.get("outcome"), row.get("resolution_outcome")) == side if side else None
    return _base_corpus_row(
        source_kind="resolved_replay",
        path=path,
        row_number=row_number,
        lane_id=_lane_id(row),
        market_id=market_id,
        shared_candidate_id=_first_text(row.get("shared_candidate_id"), row.get("candidate_id")),
        observed_at=_first_text(row.get("observed_at"), row.get("snapshot_as_of"), row.get("created_at")),
        action=action,
        side=side,
        entry_price=_number(row.get("entry_price"), row.get("fill_price"), row.get("price")),
        fill_price=_number(row.get("fill_price"), row.get("entry_price"), row.get("price")),
        notional_usd=stake,
        resolution_outcome=_first_text(row.get("resolution_outcome"), row.get("outcome"), row.get("actual_outcome")),
        pnl_usd=pnl,
        stake_usd=stake,
        won=won,
        blocker=_first_text(row.get("blocker")),
        raw_schema_name=_first_text(row.get("schema_name")),
    )


def _corpus_row_from_resolved(
    row: Mapping[str, Any],
    *,
    source_kind: str,
    path: Path,
    row_number: int,
) -> dict[str, Any]:
    pnl = row.get("pnl") if isinstance(row.get("pnl"), Mapping) else {}
    resolution = row.get("resolution") if isinstance(row.get("resolution"), Mapping) else {}
    return _base_corpus_row(
        source_kind=source_kind,
        path=path,
        row_number=row_number,
        lane_id=_lane_id(row),
        market_id=_first_text(row.get("market_id"), resolution.get("market_id")),
        shared_candidate_id=_first_text(row.get("shared_candidate_id"), resolution.get("shared_candidate_id")),
        observed_at=_first_text(row.get("observed_at")),
        action=_first_text(row.get("action")),
        side=_first_text(row.get("side")),
        entry_price=_number(row.get("entry_price")),
        fill_price=_number(row.get("fill_price"), row.get("entry_price")),
        notional_usd=_number(row.get("notional_usd"), row.get("approved_position_size_usd")),
        resolution_outcome=_first_text(resolution.get("outcome"), row.get("outcome"), row.get("actual_outcome")),
        pnl_usd=_number(pnl.get("pnl_usd"), row.get("pnl_usd")),
        stake_usd=_number(pnl.get("stake_usd"), row.get("stake_usd"), row.get("notional_usd")),
        won=pnl.get("won") if isinstance(pnl.get("won"), bool) else row.get("won"),
        blocker=_first_text(row.get("blocker")),
        raw_schema_name=_first_text(row.get("schema_name")),
    )


def _base_corpus_row(
    *,
    source_kind: str,
    path: Path,
    row_number: int,
    lane_id: str | None,
    market_id: str | None,
    shared_candidate_id: str | None,
    observed_at: str | None,
    action: str | None,
    side: str | None,
    entry_price: float | None,
    fill_price: float | None,
    notional_usd: float | None,
    resolution_outcome: str | None,
    pnl_usd: float | None,
    stake_usd: float | None,
    won: Any,
    blocker: str | None,
    raw_schema_name: str | None,
) -> dict[str, Any]:
    market_id = market_id or ""
    row = {
        "schema_name": "unified_replay_corpus_row",
        "schema_version": CORPUS_SCHEMA_VERSION,
        "non_mutating": True,
        "source_kind": source_kind,
        "source_path": _display_path(path),
        "source_row_number": row_number,
        "raw_schema_name": raw_schema_name,
        "lane_id": lane_id or "unknown",
        "market_id": market_id,
        "shared_candidate_id": shared_candidate_id,
        "observed_at": observed_at,
        "action": action,
        "side": side,
        "entry_price": entry_price,
        "fill_price": fill_price,
        "notional_usd": notional_usd,
        "resolution_outcome": resolution_outcome,
        "pnl_usd": pnl_usd,
        "stake_usd": stake_usd,
        "won": won if isinstance(won, bool) else None,
        "blocker": blocker,
        "split": _market_split(market_id),
    }
    row["dedupe_key"] = _dedupe_key(row)
    return row


def _iter_resolution_rows(paths: Iterable[Path], *, max_rows_per_source: int | None) -> Iterator[dict[str, Any]]:
    for path in paths:
        for _, row in _iter_jsonl(path, max_rows=max_rows_per_source):
            if isinstance(row, Mapping):
                out = dict(row)
                out.setdefault("resolution_source_path", _display_path(path))
                yield out


def _dedupe_resolution_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_market: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        market_id = _resolution_market_id(row_dict)
        if not market_id:
            passthrough.append(row_dict)
            continue
        existing = by_market.get(market_id)
        if existing is None:
            by_market[market_id] = row_dict
            continue
        if _resolution_outcome(existing) == _resolution_outcome(row_dict):
            continue
        conflicts.append(row_dict)
    return [*by_market.values(), *conflicts, *passthrough]


def _resolution_market_id(row: Mapping[str, Any]) -> str | None:
    resolution = row.get("resolution") if isinstance(row.get("resolution"), Mapping) else {}
    return _first_text(
        row.get("market_id"),
        row.get("ticker"),
        row.get("market_ticker"),
        resolution.get("market_id"),
        resolution.get("ticker"),
        resolution.get("market_ticker"),
    )


def _resolution_outcome(row: Mapping[str, Any]) -> str | None:
    resolution = row.get("resolution") if isinstance(row.get("resolution"), Mapping) else {}
    value = _first_text(
        row.get("resolved_outcome"),
        row.get("actual_outcome"),
        row.get("outcome"),
        row.get("result"),
        resolution.get("resolved_outcome"),
        resolution.get("actual_outcome"),
        resolution.get("outcome"),
        resolution.get("result"),
    )
    return value.upper() if value else None


def _iter_jsonl(path: Path, *, max_rows: int | None = None) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            if max_rows is not None and index > max_rows:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield index, row


def _discover_paths(
    *,
    explicit: Iterable[str],
    patterns: Iterable[str],
    defaults: Iterable[str],
    include_defaults: bool,
) -> list[Path]:
    paths: list[Path] = []
    for value in explicit:
        paths.append(_root_path(value))
    for pattern in (list(defaults) if include_defaults else []) + list(patterns):
        paths.extend(_root_path(match) for match in glob(str(_root_path(pattern)), recursive=True))
    return _unique_existing_paths(paths)


def _unique_existing_paths(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = path if path.is_absolute() else ROOT / path
        try:
            normalized = normalized.resolve()
        except OSError:
            continue
        if normalized in seen or not normalized.exists() or not normalized.is_file():
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _write_corpus_row(handle: Any, row: Mapping[str, Any]) -> None:
    handle.write(json.dumps(dict(row), sort_keys=True) + "\n")


def _should_skip(row: Mapping[str, Any]) -> bool:
    return row.get("market_id") in (None, "")


def _dedupe_seen(row: Mapping[str, Any], seen: set[str]) -> bool:
    key = str(row.get("dedupe_key") or "")
    if key in seen:
        return True
    seen.add(key)
    return False


def _dedupe_key(row: Mapping[str, Any]) -> str:
    parts = [
        row.get("source_kind"),
        row.get("lane_id"),
        row.get("market_id"),
        row.get("shared_candidate_id"),
        row.get("observed_at"),
        row.get("action"),
        row.get("side"),
        row.get("entry_price"),
        row.get("notional_usd"),
        row.get("resolution_outcome"),
        row.get("pnl_usd"),
    ]
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _market_split(market_id: str) -> str:
    if not market_id:
        return "unknown"
    bucket = int(hashlib.sha256(market_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < 60:
        return "train"
    if bucket < 80:
        return "validation"
    return "holdout"


def _lane_id(row: Mapping[str, Any]) -> str:
    return (
        _first_text(row.get("lane_id"), row.get("selected_lane"), row.get("policy"), row.get("agent_id"))
        or "unknown"
    )


def _is_buy(value: Any) -> bool:
    return str(value or "").upper() in {"BUY_YES", "BUY_NO"}


def _empty_metric_bucket() -> dict[str, Any]:
    return {
        "rows": 0,
        "buy_rows": 0,
        "winning_buy_rows": 0,
        "losing_buy_rows": 0,
        "total_stake_usd": 0.0,
        "total_pnl_usd": 0.0,
    }


def _finalize_metric_bucket(bucket: Mapping[str, Any]) -> dict[str, Any]:
    stake = float(bucket.get("total_stake_usd") or 0.0)
    buy_rows = int(bucket.get("buy_rows") or 0)
    wins = int(bucket.get("winning_buy_rows") or 0)
    out = dict(bucket)
    out["total_stake_usd"] = round(stake, 4)
    out["total_pnl_usd"] = round(float(bucket.get("total_pnl_usd") or 0.0), 4)
    out["win_rate_pct"] = _pct(wins, buy_rows)
    out["roi_pct"] = _pct(out["total_pnl_usd"], stake)
    return out


def _pct(numerator: float | int, denominator: float | int) -> float | None:
    denominator_f = float(denominator or 0.0)
    if not denominator_f:
        return None
    return round((float(numerator) / denominator_f) * 100.0, 2)


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text:
            return text
    return None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if text else None


def _root_path(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _safe_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    if not any(resolved == root.resolve() or root.resolve() in resolved.parents for root in SAFE_OUTPUT_ROOTS):
        allowed = ", ".join(_display_path(root) for root in SAFE_OUTPUT_ROOTS)
        raise ValueError(f"output-dir must be under one of: {allowed}")
    return resolved


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _text_report(summary: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "Unified replay corpus",
            f"rows={summary['rows']} unique_markets={summary['unique_markets']} "
            f"resolved={summary['resolved_rows']} pnl_calculable={summary['pnl_calculable_rows']}",
            f"buy_rows={summary['buy_rows']} pnl=${summary['total_pnl_usd']} "
            f"roi={summary['roi_pct']}% win={summary['win_rate_pct']}%",
            f"source_kinds={summary['source_kind_counts']}",
            f"splits={summary['split_counts']}",
        ]
    )


def _markdown_report(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Unified Replay Corpus",
        "",
        f"- Rows: {summary['rows']}",
        f"- Unique markets: {summary['unique_markets']}",
        f"- Unique candidates: {summary['unique_candidates']}",
        f"- Resolved rows: {summary['resolved_rows']}",
        f"- PnL-calculable rows: {summary['pnl_calculable_rows']}",
        f"- Buy rows: {summary['buy_rows']}",
        f"- PnL: ${summary['total_pnl_usd']}",
        f"- ROI: {summary['roi_pct']}%",
        f"- Win rate: {summary['win_rate_pct']}%",
        "",
        "## Source Kinds",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in summary["source_kind_counts"].items())
    lines.extend(["", "## Splits", ""])
    lines.extend(f"- {name}: {count}" for name, count in summary["split_counts"].items())
    lines.extend(["", "## Inputs", ""])
    for kind, paths in summary["input_sources"].items():
        lines.append(f"- {kind}: {len(paths)}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
