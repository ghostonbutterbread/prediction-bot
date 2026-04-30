"""Archive-backed exchange adapters for offline Prediction Lab replay.

These adapters let historical Parquet archives look like normal exchange objects
without changing the strategy/Prediction Lab code path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot.exchanges.base import BaseExchange, Market, Order, Position


class ArchiveDependencyError(RuntimeError):
    """Raised when optional archive dependencies are missing."""


def _load_duckdb():
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise ArchiveDependencyError(
            "Archive replay requires duckdb. Install project archive dependencies "
            "or run via an environment that provides duckdb/pyarrow."
        ) from exc
    return duckdb


class HistoricalKalshiArchiveExchange(BaseExchange):
    """Read Kalshi market snapshots from the imported Parquet archive.

    The class intentionally implements the small subset Prediction Lab needs:
    `get_markets_direct`, `get_markets`, `get_market`, and `_fetch_market_raw`.
    Order methods are no-ops because archive replay is observation-only.
    """

    name = "kalshi_archive"

    def __init__(
        self,
        archive_dir: str | Path = "data/external/prediction_market_analysis/data",
        *,
        as_of: str | datetime | None = None,
        groups: list[str] | None = None,
        dedupe_latest: bool = True,
        include_closed: bool = True,
    ):
        self.archive_dir = Path(archive_dir)
        self.markets_dir = self.archive_dir / "kalshi" / "markets"
        self.trades_dir = self.archive_dir / "kalshi" / "trades"
        self.as_of = self._parse_dt(as_of)
        self.groups = [str(group).strip().lower() for group in (groups or []) if str(group).strip()]
        self.dedupe_latest = dedupe_latest
        self.include_closed = include_closed
        self._con = None
        self._last_raw_by_ticker: dict[str, dict[str, Any]] = {}

    def connect(self) -> bool:
        self._ensure_connection()
        return self.markets_dir.exists() and bool(self._market_files())

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    def set_allowed_market_groups(self, groups: list[str]) -> None:
        self.groups = [str(group).strip().lower() for group in (groups or []) if str(group).strip()]

    def get_markets_direct(self, limit: int = 500, page_size: int = 200, max_pages: int = 10) -> list[Market]:
        # page_size/max_pages are accepted for Prediction Lab compatibility; the
        # archive query is already bounded by `limit`.
        return self.get_markets(limit=limit)

    def get_markets(self, limit: int = 50, category: str = None) -> list[Market]:
        con = self._ensure_connection()
        files_expr = self._read_parquet_expr(self._market_files())
        where = self._where_clause(category=category)
        if self.dedupe_latest:
            sql = f"""
                WITH ranked AS (
                    SELECT *,
                           row_number() OVER (
                               PARTITION BY ticker
                               ORDER BY _fetched_at DESC NULLS LAST, close_time DESC NULLS LAST
                           ) AS rn
                    FROM {files_expr}
                    WHERE {where}
                )
                SELECT * EXCLUDE (rn)
                FROM ranked
                WHERE rn = 1
                ORDER BY close_time ASC NULLS LAST, volume DESC NULLS LAST
                LIMIT ?
            """
        else:
            sql = f"""
                SELECT *
                FROM {files_expr}
                WHERE {where}
                ORDER BY _fetched_at ASC NULLS LAST, close_time ASC NULLS LAST, volume DESC NULLS LAST
                LIMIT ?
            """
        rows = self._fetch_dicts(sql, [max(1, int(limit or 50))])
        markets = []
        for row in rows:
            market = self._market_from_row(row)
            if market is not None:
                markets.append(market)
                self._last_raw_by_ticker[market.id] = self._raw_from_row(row)
        return markets

    def get_market(self, market_id: str) -> Optional[Market]:
        raw = self._fetch_market_raw(market_id)
        if not raw:
            return None
        return self._market_from_row(raw)

    def _fetch_market_raw(self, market_id: str) -> Optional[dict[str, Any]]:
        if market_id in self._last_raw_by_ticker:
            return dict(self._last_raw_by_ticker[market_id])
        con = self._ensure_connection()
        files_expr = self._read_parquet_expr(self._market_files())
        where = self._where_clause(extra="ticker = ?")
        sql = f"""
            SELECT *
            FROM {files_expr}
            WHERE {where}
            ORDER BY _fetched_at DESC NULLS LAST, close_time DESC NULLS LAST
            LIMIT 1
        """
        rows = self._fetch_dicts(sql, [market_id])
        if not rows:
            return None
        raw = self._raw_from_row(rows[0])
        self._last_raw_by_ticker[market_id] = raw
        return dict(raw)

    def get_order_book(self, market_id: str) -> Optional[dict]:
        raw = self._fetch_market_raw(market_id)
        if not raw:
            return None
        yes_ask = self._cents_to_unit(raw.get("yes_ask"))
        yes_bid = self._cents_to_unit(raw.get("yes_bid"))
        no_ask = self._cents_to_unit(raw.get("no_ask"))
        no_bid = self._cents_to_unit(raw.get("no_bid"))
        mid_yes = None
        if yes_ask is not None and yes_bid is not None:
            mid_yes = (yes_ask + yes_bid) / 2
        elif yes_ask is not None:
            mid_yes = yes_ask
        elif yes_bid is not None:
            mid_yes = yes_bid
        return {
            "best_yes_ask": yes_ask,
            "best_yes_bid": yes_bid,
            "best_no_ask": no_ask,
            "best_no_bid": no_bid,
            "mid_yes": mid_yes,
        }

    def place_order(self, market_id: str, side: str, price: float, size: float) -> Optional[Order]:
        return None

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_positions(self) -> list[Position]:
        return []

    def get_balance(self) -> float:
        return 0.0

    def _ensure_connection(self):
        if self._con is None:
            duckdb = _load_duckdb()
            self._con = duckdb.connect()
        return self._con

    def _fetch_dicts(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        con = self._ensure_connection()
        cursor = con.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _market_files(self) -> list[Path]:
        return sorted(p for p in self.markets_dir.glob("*.parquet") if not p.name.startswith("._"))

    @staticmethod
    def _read_parquet_expr(files: list[Path]) -> str:
        if not files:
            raise FileNotFoundError("No Kalshi archive market parquet files found")
        return f"read_parquet({json.dumps([str(path) for path in files])})"

    def _where_clause(self, *, category: str | None = None, extra: str | None = None) -> str:
        clauses = ["ticker IS NOT NULL", "ticker != ''"]
        if not self.include_closed:
            clauses.append("lower(coalesce(status, '')) IN ('active', 'open')")
        if self.as_of is not None:
            clauses.append(f"_fetched_at <= TIMESTAMP '{self.as_of.isoformat(sep=' ', timespec='seconds')}'")
        group_clause = self._group_sql(category)
        if group_clause:
            clauses.append(group_clause)
        if extra:
            clauses.append(f"({extra})")
        return " AND ".join(f"({clause})" for clause in clauses)

    def _group_sql(self, category: str | None = None) -> str:
        groups = list(self.groups)
        if category:
            groups.append(str(category).lower())
        groups = sorted(set(groups))
        parts = []
        if "weather" in groups:
            parts.append(
                "(" 
                "upper(coalesce(ticker,'')) LIKE 'KXHIGH%' OR "
                "upper(coalesce(ticker,'')) LIKE 'KXLOW%' OR "
                "upper(coalesce(event_ticker,'')) LIKE 'KXHIGH%' OR "
                "upper(coalesce(event_ticker,'')) LIKE 'KXLOW%' OR "
                "lower(coalesce(title,'')) LIKE '%temperature%' OR "
                "lower(coalesce(title,'')) LIKE '%weather%' OR "
                "lower(coalesce(title,'')) LIKE '%rain%' OR "
                "lower(coalesce(title,'')) LIKE '%snow%'"
                ")"
            )
        if "sports" in groups:
            parts.append(
                "(" 
                "lower(coalesce(title,'')) LIKE '% nba %' OR "
                "lower(coalesce(title,'')) LIKE '% nfl %' OR "
                "lower(coalesce(title,'')) LIKE '% mlb %' OR "
                "lower(coalesce(title,'')) LIKE '% nhl %' OR "
                "lower(coalesce(title,'')) LIKE '%game%' OR "
                "lower(coalesce(title,'')) LIKE '%match%' OR "
                "lower(coalesce(event_ticker,'')) LIKE '%sport%'"
                ")"
            )
        return " OR ".join(parts)

    def _market_from_row(self, row: dict[str, Any]) -> Optional[Market]:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            return None
        yes_price = self._entry_price(row, "yes")
        no_price = self._entry_price(row, "no")
        if yes_price is None and no_price is None:
            return None
        if yes_price is None:
            yes_price = max(0.0, min(1.0, 1.0 - float(no_price)))
        if no_price is None:
            no_price = max(0.0, min(1.0, 1.0 - float(yes_price)))
        result = str(row.get("result") or "").strip().lower()
        close_price = 1.0 if result == "yes" else (0.0 if result == "no" else None)
        event_ticker = str(row.get("event_ticker") or "")
        metadata = {
            "source": "prediction_market_analysis_archive",
            "archive_exchange": "kalshi",
            "event_ticker": event_ticker,
            "series": event_ticker,
            "status": row.get("status"),
            "result": result,
            "_fetched_at": self._iso(row.get("_fetched_at")),
            "open_time": self._iso(row.get("open_time")),
            "market_group": self._infer_group(row),
        }
        return Market(
            id=ticker,
            exchange="kalshi_archive",
            question=str(row.get("title") or ticker),
            yes_price=float(yes_price),
            no_price=float(no_price),
            volume=float(row.get("volume") or 0.0),
            liquidity=0.0,
            closes_at=self._parse_dt(row.get("close_time")),
            category=event_ticker or "archive",
            metadata=metadata,
            close_price=close_price,
            yes_bid=self._cents_to_unit(row.get("yes_bid")),
            no_bid=self._cents_to_unit(row.get("no_bid")),
        )

    def _raw_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        raw = dict(row)
        if raw.get("result"):
            raw["result"] = str(raw["result"]).upper()
        return raw

    def _entry_price(self, row: dict[str, Any], side: str) -> float | None:
        if side == "yes":
            for key in ("yes_ask", "last_price", "yes_bid"):
                price = self._cents_to_unit(row.get(key))
                if price is not None and 0 < price < 1:
                    return price
        else:
            for key in ("no_ask", "no_bid"):
                price = self._cents_to_unit(row.get(key))
                if price is not None and 0 < price < 1:
                    return price
            yes = self._cents_to_unit(row.get("last_price"))
            if yes is not None and 0 < yes < 1:
                return max(0.0, min(1.0, 1.0 - yes))
        return None

    @staticmethod
    def _cents_to_unit(value: Any) -> float | None:
        try:
            if value is None:
                return None
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric <= 0:
            return None
        if numeric <= 1:
            return numeric
        if numeric <= 100:
            return numeric / 100.0
        return None

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _iso(value: Any) -> str:
        parsed = HistoricalKalshiArchiveExchange._parse_dt(value)
        return parsed.isoformat() if parsed else ""

    @staticmethod
    def _infer_group(row: dict[str, Any]) -> str:
        text = f"{row.get('ticker','')} {row.get('event_ticker','')} {row.get('title','')}".lower()
        if any(token in text for token in ("temperature", "weather", "rain", "snow", "kxhigh", "kxlow")):
            return "weather"
        if any(token in text for token in (" nba ", " nfl ", " mlb ", " nhl ", "game", "match", "sports")):
            return "sports"
        return "unknown"
