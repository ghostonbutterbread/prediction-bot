"""
Historical Data Loader — Feed past sports data to the prediction bot for learning.

Data Sources:
1. Kingsets.com — Free daily CSV downloads of Kalshi/Polymarket data
2. Kalshi Historical API — Get settled markets with outcomes
3. Local JSON files — Manual/historical data

Usage:
    loader = HistoricalDataLoader()
    
    # Load from Kingsets CSV
    markets = loader.load_kingsets_csv("kalshi_markets.csv")
    
    # Load from Kalshi API (settled markets)
    markets = loader.load_kalshi_settled(exchange, days_back=30, category="sports")
    
    # Run backtest against historical data
    backtester = Backtester(config)
    result = backtester.run(markets)
    
    # Learn: adjust strategy based on results
    loader.analyze_performance(result)
"""

import json
import csv
import logging
import os
import httpx
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / "projects" / "prediction-bot" / "data" / "historical"


@dataclass
class HistoricalMarket:
    """A historical market with known outcome."""
    ticker: str
    title: str
    category: str  # sports, politics, crypto, etc.
    yes_price: float  # Price at some point before resolution
    no_price: float
    outcome: str  # "YES" or "NO"
    volume: float
    close_time: str
    resolved_time: str
    sport: str = ""  # nba, nfl, soccer, etc.
    metadata: dict = field(default_factory=dict)


class HistoricalDataLoader:
    """Load historical prediction market data for backtesting."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ─── Kingsets.com CSV ────────────────────────────────────────────

    def download_kingsets(self, date: str = None) -> str:
        """
        Download daily Kalshi data from Kingsets.com.
        
        Args:
            date: YYYY-MM-DD format, defaults to yesterday
        
        Returns:
            Path to downloaded CSV
        """
        if date is None:
            date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        url = f"https://kingsets.com/data/kalshi_markets_{date}.csv"
        output_path = DATA_DIR / f"kingsets_{date}.csv"
        
        if output_path.exists():
            logger.info(f"Already have {output_path}")
            return str(output_path)
        
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            output_path.write_text(resp.text)
            logger.info(f"Downloaded {len(resp.text)} bytes from Kingsets for {date}")
            return str(output_path)
        except Exception as e:
            logger.warning(f"Kingsets download failed for {date}: {e}")
            # Try alternative URL format
            alt_url = f"https://kingsets.com/data/{date}/markets.csv"
            try:
                resp = httpx.get(alt_url, timeout=30, follow_redirects=True)
                resp.raise_for_status()
                output_path.write_text(resp.text)
                return str(output_path)
            except Exception:
                return ""

    def load_kingsets_csv(self, csv_path: str, 
                          category_filter: str = "sports") -> list[HistoricalMarket]:
        """Load markets from Kingsets CSV."""
        markets = []
        
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = row.get('title', row.get('name', ''))
                
                # Filter by category
                if category_filter:
                    if category_filter.lower() not in title.lower() and \
                       category_filter.lower() not in row.get('category', '').lower():
                        continue
                
                # Skip unresolved
                outcome = row.get('result', row.get('outcome', row.get('resolution', '')))
                if not outcome or outcome.upper() not in ('YES', 'NO'):
                    continue
                
                market = HistoricalMarket(
                    ticker=row.get('ticker', row.get('id', '')),
                    title=title,
                    category=row.get('category', ''),
                    yes_price=float(row.get('yes_price', row.get('yes_ask', 0)) or 0),
                    no_price=float(row.get('no_price', row.get('no_ask', 0)) or 0),
                    outcome=outcome.upper(),
                    volume=float(row.get('volume', 0) or 0),
                    close_time=row.get('close_time', row.get('close_date', '')),
                    resolved_time=row.get('resolved_time', row.get('end_date', '')),
                    sport=self._detect_sport(title),
                )
                markets.append(market)
        
        logger.info(f"Loaded {len(markets)} resolved {category_filter} markets from {csv_path}")
        return markets

    # ─── Kalshi Historical API ───────────────────────────────────────

    def load_kalshi_settled(self, exchange, days_back: int = 30, 
                            category: str = "sports",
                            limit: int = 200) -> list[HistoricalMarket]:
        """
        Load settled markets from Kalshi API.
        
        Args:
            exchange: KalshiExchange instance
            days_back: How many days of history
            category: Filter by category
            limit: Max markets to load
        """
        markets = []
        
        try:
            # Get events with settled status
            events_resp = exchange.client.get_events(
                limit=min(limit // 5, 50),
                status="settled"
            )
            events = getattr(events_resp, 'events', []) or []
            
            for event in events:
                event_ticker = getattr(event, 'event_ticker', '')
                event_title = getattr(event, 'title', '')
                event_category = getattr(event, 'category', '')
                
                # Filter by category
                if category and category.lower() not in event_title.lower() and \
                   category.lower() not in event_category.lower():
                    continue
                
                try:
                    mresp = exchange.client.get_markets(event_ticker=event_ticker)
                    raw_markets = getattr(mresp, 'markets', []) or []
                    
                    for m in raw_markets:
                        status = getattr(m, 'status', '')
                        if status != 'settled':
                            continue
                        
                        result = getattr(m, 'result', '')
                        if result not in ('yes', 'no'):
                            continue
                        
                        market = HistoricalMarket(
                            ticker=getattr(m, 'ticker', ''),
                            title=getattr(m, 'title', ''),
                            category=event_category,
                            yes_price=(getattr(m, 'yes_ask_dollars', 0) or 0),
                            no_price=(getattr(m, 'no_ask_dollars', 0) or 0),
                            outcome=result.upper(),
                            volume=float(getattr(m, 'volume_fp', 0) or 0),
                            close_time=str(getattr(m, 'close_time', '')),
                            resolved_time=str(getattr(m, 'close_time', '')),
                            sport=self._detect_sport(getattr(m, 'title', '')),
                        )
                        markets.append(market)
                        
                except Exception as e:
                    logger.debug(f"Error fetching markets for {event_ticker}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error loading settled markets: {e}")
        
        logger.info(f"Loaded {len(markets)} settled {category} markets from Kalshi")
        return markets

    # ─── Convert to Backtester Format ────────────────────────────────

    def to_backtest_format(self, markets: list[HistoricalMarket]) -> list[dict]:
        """Convert to backtester JSON format."""
        return [
            {
                "ticker": m.ticker,
                "title": m.title,
                "yes_price": m.yes_price,
                "outcome": m.outcome,
                "volume": m.volume,
                "category": m.category,
                "sport": m.sport,
                "close_time": m.close_time,
            }
            for m in markets
        ]

    def save_for_backtest(self, markets: list[HistoricalMarket], 
                          filename: str = None) -> str:
        """Save markets in backtest format."""
        if filename is None:
            filename = f"backtest_{datetime.now().strftime('%Y%m%d')}.json"
        
        output_path = DATA_DIR / filename
        data = self.to_backtest_format(markets)
        output_path.write_text(json.dumps(data, indent=2))
        logger.info(f"Saved {len(data)} markets to {output_path}")
        return str(output_path)

    # ─── Performance Analysis ────────────────────────────────────────

    def analyze_performance(self, backtest_result: dict) -> dict:
        """
        Analyze backtest results and suggest strategy improvements.
        
        Returns recommendations for adjusting strategy parameters.
        """
        trades = backtest_result.get("trades", [])
        if not trades:
            return {"status": "no_trades", "recommendations": []}
        
        wins = sum(1 for t in trades if t.get("outcome") == t.get("direction", "").replace("BUY_", ""))
        total = len(trades)
        win_rate = wins / total if total > 0 else 0
        
        recommendations = []
        
        # Win rate analysis
        if win_rate < 0.45:
            recommendations.append({
                "type": "threshold",
                "message": f"Win rate {win_rate:.1%} is low. Increase min_edge from 0.015 to 0.025",
                "action": "increase_edge_threshold",
                "value": 0.025,
            })
        elif win_rate > 0.60:
            recommendations.append({
                "type": "threshold",
                "message": f"Win rate {win_rate:.1%} is strong. Consider lowering min_edge to catch more trades",
                "action": "decrease_edge_threshold",
                "value": 0.010,
            })
        
        # Edge analysis
        edges = [t.get("edge", 0) for t in trades if t.get("edge")]
        avg_edge = sum(edges) / len(edges) if edges else 0
        
        if avg_edge < 0.02:
            recommendations.append({
                "type": "signal",
                "message": f"Average edge {avg_edge:.1%} is thin. Signals need refinement.",
                "action": "review_signal_quality",
            })
        
        # Category performance
        by_sport = {}
        for t in trades:
            sport = t.get("category", "unknown")
            by_sport.setdefault(sport, {"total": 0, "wins": 0})
            by_sport[sport]["total"] += 1
        
        best_sport = max(by_sport.items(), key=lambda x: x[1]["total"]) if by_sport else None
        if best_sport:
            recommendations.append({
                "type": "focus",
                "message": f"Most trades in {best_sport[0]} ({best_sport[1]['total']} trades). Consider focusing here.",
                "action": "focus_category",
                "value": best_sport[0],
            })
        
        return {
            "status": "analyzed",
            "win_rate": win_rate,
            "total_trades": total,
            "avg_edge": avg_edge,
            "recommendations": recommendations,
        }

    # ─── Helpers ─────────────────────────────────────────────────────

    def _detect_sport(self, title: str) -> str:
        """Detect sport type from market title."""
        title_lower = title.lower()
        
        sport_keywords = {
            "nba": ["nba", "basketball", "lakers", "celtics", "warriors"],
            "nfl": ["nfl", "football", "touchdown", "quarterback"],
            "soccer": ["soccer", "premier league", "la liga", "champions", "goals"],
            "nhl": ["nhl", "hockey", "ice hockey"],
            "mlb": ["mlb", "baseball", "home runs", "innings"],
            "mma": ["ufc", "mma", "fight", "boxing", "knockout"],
            "tennis": ["tennis", "grand slam", "wimbledon", "us open"],
            "golf": ["golf", "pga", "masters", "birdie"],
        }
        
        for sport, keywords in sport_keywords.items():
            if any(kw in title_lower for kw in keywords):
                return sport
        
        return "other"
