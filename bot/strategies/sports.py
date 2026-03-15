"""Sports Analyzer — Real-time sports data for prediction market trading.

Pulls live stats, team form, injuries, and player performance
to make informed bets on short-term sports markets.

Uses browser-based scraping for real-time data.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SportsMarket:
    """A sports betting market from Kalshi."""
    title: str
    market_id: str
    close_time: str
    yes_price: float = 0.0
    no_price: float = 0.0
    category: str = ""  # nba, nfl, soccer, hockey
    team_home: str = ""
    team_away: str = ""
    game_time: str = ""
    
    @property
    def hours_until_close(self) -> float:
        try:
            close = datetime.fromisoformat(self.close_time.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            return max(0, (close - now).total_seconds() / 3600)
        except:
            return 24


@dataclass 
class GameAnalysis:
    """Analysis of a sports game for prediction."""
    market: SportsMarket
    recommendation: str  # BUY_YES, BUY_NO, SKIP
    confidence: float = 0.0
    edge: float = 0.0
    reasons: list = field(default_factory=list)
    
    # Stats that influenced decision
    home_form: str = ""
    away_form: str = ""
    key_factors: list = field(default_factory=list)


class MarketFilter:
    """Filter Kalshi markets for quick-resolution opportunities."""
    
    # Sports keywords
    SPORTS_KEYWORDS = {
        'nba': ['nba', 'basketball', 'points', 'rebounds', 'assists', '3-pointers', 'cleveland', 'detroit', 'oklahoma', 'boston', 'lakers', 'warriors', 'celtics', 'miami', 'chicago', 'houston', 'denver', 'phoenix', 'brooklyn', 'atlanta', 'charlotte', 'orlando', 'washington', 'toronto', 'indiana', 'milwaukee', 'minnesota', 'portland', 'sacramento', 'san antonio', 'dallas', 'memphis', 'new orleans', 'utah', 'philadelphia', 'new york', 'clippers'],
        'nfl': ['nfl', 'football', 'touchdown', 'quarterback', 'passing yards', 'rushing'],
        'soccer': ['soccer', 'goals', 'premier league', 'la liga', 'champions league', 'barcelona', 'real madrid', 'liverpool', 'manchester', 'arsenal', 'chelsea', 'bayern', 'psg', 'juventus', 'inter milan', 'ac milan'],
        'hockey': ['nhl', 'hockey', 'ice hockey', 'overtime', 'hockey goals'],
        'baseball': ['mlb', 'baseball', 'home runs', 'innings', 'pitching'],
        'mma': ['ufc', 'mma', 'fight', 'boxing', 'knockout', 'round'],
        'tennis': ['tennis', 'grand slam', 'set', 'match'],
        'golf': ['golf', 'pga', 'masters', 'birdie', 'eagle'],
    }
    
    # Player prop keywords
    PLAYER_PROP_KEYWORDS = ['+', 'points', 'rebounds', 'assists', 'yards', 'goals', 'saves']
    
    @classmethod
    def classify_market(cls, title: str) -> Optional[str]:
        """Classify a market as a sport type. Returns None if not sports."""
        title_lower = title.lower()
        for sport, keywords in cls.SPORTS_KEYWORDS.items():
            if any(kw in title_lower for kw in keywords):
                return sport
        return None
    
    @classmethod
    def is_player_prop(cls, title: str) -> bool:
        """Check if this is a player prop market."""
        title_lower = title.lower()
        return any(kw in title_lower for kw in cls.PLAYER_PROP_KEYWORDS) and any(c.isdigit() for c in title)
    
    @classmethod
    def is_game_outcome(cls, title: str) -> bool:
        """Check if this is a game outcome market."""
        title_lower = title.lower()
        return any(w in title_lower for w in ['wins', 'win', 'beat', 'victory', 'score'])
    
    @classmethod
    def filter_sports(cls, markets: list[dict], max_hours: float = 48) -> list[SportsMarket]:
        """Filter markets for sports events closing within max_hours."""
        sports = []
        
        for m in markets:
            title = m.get('title', m.get('subtitle', ''))
            if not title:
                continue
            
            sport = cls.classify_market(title)
            if not sport:
                continue
            
            sm = SportsMarket(
                title=title,
                market_id=m.get('ticker', m.get('id', '')),
                close_time=m.get('close_time', m.get('close_date', '')),
                yes_price=float(m.get('yes_ask', m.get('yes_price', 0)) or 0),
                no_price=float(m.get('no_ask', m.get('no_price', 0)) or 0),
                category=sport,
            )
            
            # Only include if closing within timeframe
            if sm.hours_until_close <= max_hours:
                sports.append(sm)
        
        # Sort by closest to close (most urgent first)
        sports.sort(key=lambda x: x.hours_until_close)
        
        return sports


class QuickBetStrategy:
    """
    Strategy for quick-resolution sports markets.
    
    Optimized for:
    - Small capital ($10-100)
    - Fast feedback (same-day resolution)
    - Higher risk tolerance than long-term markets
    - Data-driven edge detection
    """
    
    # Confidence thresholds (lower than long-term since we need action)
    MIN_EDGE = 0.015      # 1.5% minimum edge
    MIN_CONFIDENCE = 0.52  # 52% minimum confidence
    STRONG_EDGE = 0.05     # 5% edge = max bet
    STRONG_CONF = 0.60     # 60% confidence = high conviction
    
    def analyze_market(self, market: SportsMarket) -> dict:
        """Generate a signal for a sports market."""
        signal = {
            "market_id": market.market_id,
            "question": market.title,
            "exchange": "kalshi",
            "category": market.category,
            "close_hours": market.hours_until_close,
            "direction": "BUY_YES",
            "model_probability": 0.5,
            "market_price": market.yes_price,
            "edge": 0.0,
            "confidence": 0.0,
            "should_trade": False,
            "signals": {},
        }
        
        # Parse the market for analysis context
        title_lower = market.title.lower()
        
        # === Analysis Rules ===
        
        # 1. Over/Under totals markets
        if 'over' in title_lower or 'under' in title_lower:
            signal.update(self._analyze_total(market))
        
        # 2. Player props (X+ points/rebounds/assists)
        elif MarketFilter.is_player_prop(market.title):
            signal.update(self._analyze_player_prop(market))
        
        # 3. Game outcomes (team wins)
        elif MarketFilter.is_game_outcome(market.title):
            signal.update(self._analyze_game_outcome(market))
        
        # 4. Default: slight contrarian on extreme prices
        else:
            signal.update(self._analyze_value(market))
        
        # Apply thresholds
        signal["should_trade"] = (
            signal["edge"] >= self.MIN_EDGE and 
            signal["confidence"] >= self.MIN_CONFIDENCE
        )
        
from bot.strategies.injury_sniper import InjuryImpactAnalyzer, Player, InjuryAlert #Add imports


          
        
        # === Injury News Scanning ===
        try:
            from bot.feeds.twitter import SocialFeed
            # Get all the matches and search them here.  
            feed = SocialFeed()
            tweets = feed.get_tweets(market.title) # get the tweets for a specific market.
            if tweets:
                from bot.strategies.injury_sniper import InjuryDetector
                for tweet in tweets:
                    # Parse them and return the individual tweets here to a dictionary!
                    alert = InjuryDetector.parse((tweet.get("text", "")))
                    if alert:
                        from bot.strategies.injury_sniper import InjuryImpactAnalyzer
                        injury_signal = InjuryImpactAnalyzer.analyze(alert)
                        if injury_signal:
                             return injury_signal
            
        except Exception as e:
            logger.warning(f"Error with Injury analysis: {e}")

        return signal
    
    def _analyze_total(self, market: SportsMarket) -> dict:
        """Analyze over/under markets."""
        # Extract the line number
        import re
        numbers = re.findall(r'(\d+\.?\d*)', market.title)
        line = float(numbers[0]) if numbers else 200
        
        # NBA average total is ~215-225
        # If the line is significantly above/below average, there's potential edge
        if market.category == 'nba':
            avg_total = 220
            if line > avg_total + 10:  # Over 230 = slightly favor under
                return {
                    "direction": "BUY_NO",
                    "model_probability": 0.54,
                    "market_price": market.no_price,
                    "edge": abs(0.54 - (1 - market.yes_price)),
                    "confidence": 0.53,
                    "signals": {"type": "total_high", "line": line, "avg": avg_total},
                }
            elif line < avg_total - 10:  # Under 210 = slightly favor over
                return {
                    "direction": "BUY_YES",
                    "model_probability": 0.55,
                    "market_price": market.yes_price,
                    "edge": abs(0.55 - market.yes_price),
                    "confidence": 0.54,
                    "signals": {"type": "total_low", "line": line, "avg": avg_total},
                }
        
        return {"edge": 0.01, "confidence": 0.50, "signals": {"type": "total_neutral"}}
    
    def _analyze_player_prop(self, market: SportsMarket) -> dict:
        """Analyze player prop markets."""
        import re
        numbers = re.findall(r'(\d+)', market.title)
        threshold = int(numbers[0]) if numbers else 10
        
        # Lower thresholds are more likely (1+ points = almost certain)
        if threshold <= 2:
            # Very likely event, but market price probably reflects that
            model_prob = 0.95
            price = market.yes_price
            edge = model_prob - price
            return {
                "model_probability": model_prob,
                "market_price": price,
                "edge": edge,
                "confidence": 0.58,
                "signals": {"type": "player_prop_easy", "threshold": threshold},
            }
        elif threshold >= 25:
            # Hard threshold, slight value on NO
            model_prob = 0.35
            no_price = market.no_price or (1 - market.yes_price)
            edge = (1 - model_prob) - no_price
            if edge > 0:
                return {
                    "direction": "BUY_NO",
                    "model_probability": 1 - model_prob,
                    "market_price": no_price,
                    "edge": edge,
                    "confidence": 0.52,
                    "signals": {"type": "player_prop_hard", "threshold": threshold},
                }
        
        return {"edge": 0.01, "confidence": 0.50, "signals": {"type": "player_prop_neutral"}}
    
    def _analyze_game_outcome(self, market: SportsMarket) -> dict:
        """Analyze game outcome markets."""
        # Simple value betting: if yes_price is very low, there might be value on NO
        # If yes_price is very high (>0.8), NO at 0.2+ might have value
        
        if market.yes_price > 0.85:
            no_price = 1 - market.yes_price
            return {
                "direction": "BUY_NO",
                "model_probability": 0.20,
                "market_price": no_price,
                "edge": 0.20 - no_price if no_price < 0.20 else 0.01,
                "confidence": 0.53,
                "signals": {"type": "heavy_favorite", "yes_price": market.yes_price},
            }
        elif market.yes_price < 0.25:
            return {
                "direction": "BUY_YES",
                "model_probability": 0.30,
                "market_price": market.yes_price,
                "edge": 0.30 - market.yes_price,
                "confidence": 0.52,
                "signals": {"type": "big_underdog", "yes_price": market.yes_price},
            }
        
        return {"edge": 0.01, "confidence": 0.50, "signals": {"type": "game_neutral"}}
    
    def _analyze_value(self, market: SportsMarket) -> dict:
        """Generic value analysis for any market."""
        # Contrarian on extreme prices
        if market.yes_price > 0.90:
            no_price = 1 - market.yes_price
            return {
                "direction": "BUY_NO",
                "model_probability": 0.15,
                "market_price": no_price,
                "edge": max(0.01, 0.15 - no_price),
                "confidence": 0.51,
                "signals": {"type": "contrarian_no"},
            }
        elif market.yes_price < 0.10:
            return {
                "direction": "BUY_YES",
                "model_probability": 0.15,
                "market_price": market.yes_price,
                "edge": max(0.01, 0.15 - market.yes_price),
                "confidence": 0.51,
                "signals": {"type": "contrarian_yes"},
            }
        
        return {"edge": 0.005, "confidence": 0.50, "signals": {"type": "skip"}}
