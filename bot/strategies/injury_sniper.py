"""Injury News Sniper — React to player injuries faster than the market.

Core insight from the Polymarket alpha thread:
"The bot's logic: if a tweet contains 'LeBron won't play today' → 
instantly reject a Lakers win before retailers even finish reading the tweet."

Key principle: Not all injuries matter equally.
- Superstar injury (LeBron, Steph, Giannis) = massive market swing
- All-Star injury = significant swing  
- Starter injury = moderate swing
- Bench player = noise, don't bet

The edge is SPEED + IMPACT ASSESSMENT:
1. Detect injury news via Twitter/RSS
2. Classify player tier (superstar vs bench)
3. Calculate estimated probability swing
4. Bet if market hasn't adjusted yet
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Player:
    """NBA player with impact rating."""
    name: str
    team: str
    tier: int  # 1=superstar, 2=all-star, 3=starter, 4=role player
    position: str = ""
    impact_score: float = 0.0  # 0-100, how much this player affects win probability
    
    @property
    def tier_label(self) -> str:
        return {1: "SUPERSTAR", 2: "ALL-STAR", 3: "STARTER", 4: "ROLE PLAYER"}.get(self.tier, "UNKNOWN")


@dataclass
class InjuryAlert:
    """A detected injury report."""
    player_name: str
    team: str
    status: str  # out, doubtful, questionable, probable, day-to-day
    source: str  # twitter, rss
    source_text: str
    timestamp: str = ""
    confidence: float = 0.0  # How confident we are this is real
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class InjurySignal:
    """A betting signal generated from an injury alert."""
    alert: InjuryAlert
    player: Optional[Player]
    recommendation: str  # BUY_NO, BUY_YES, SKIP
    impact: str  # HIGH, MEDIUM, LOW
    estimated_swing: float  # Estimated probability change
    reasoning: str = ""
    should_trade: bool = False


# ==================== NBA PLAYER DATABASE ====================
# Top players by team with impact scores
# Impact score: 100 = team's entire chances depend on this player
#               50 = significant but team can compete
#               20 = starter level, moderate impact
#               5 = role player, minimal impact

NBA_PLAYERS = {
    # === SUPERSTARS (Tier 1) - Impact 70-100 ===
    "lebron": Player("LeBron James", "Lakers", 1, "F", 85),
    "lebron james": Player("LeBron James", "Lakers", 1, "F", 85),
    "anthony davis": Player("Anthony Davis", "Lakers", 1, "C", 80),
    "ad": Player("Anthony Davis", "Lakers", 1, "C", 80),
    "steph": Player("Stephen Curry", "Warriors", 1, "G", 85),
    "steph curry": Player("Stephen Curry", "Warriors", 1, "G", 85),
    "stephen curry": Player("Stephen Curry", "Warriors", 1, "G", 85),
    "curry": Player("Stephen Curry", "Warriors", 1, "G", 85),
    "giannis": Player("Giannis Antetokounmpo", "Bucks", 1, "F", 90),
    "giannis antetokounmpo": Player("Giannis Antetokounmpo", "Bucks", 1, "F", 90),
    "luka": Player("Luka Doncic", "Mavericks", 1, "G", 88),
    "luka doncic": Player("Luka Doncic", "Mavericks", 1, "G", 88),
    "doncic": Player("Luka Doncic", "Mavericks", 1, "G", 88),
    "jokic": Player("Nikola Jokic", "Nuggets", 1, "C", 92),
    "nikola jokic": Player("Nikola Jokic", "Nuggets", 1, "C", 92),
    "embiid": Player("Joel Embiid", "76ers", 1, "C", 82),
    "joel embiid": Player("Joel Embiid", "76ers", 1, "C", 82),
    "tatum": Player("Jayson Tatum", "Celtics", 1, "F", 78),
    "jayson tatum": Player("Jayson Tatum", "Celtics", 1, "F", 78),
    "sga": Player("Shai Gilgeous-Alexander", "Thunder", 1, "G", 85),
    "shai": Player("Shai Gilgeous-Alexander", "Thunder", 1, "G", 85),
    "shai gilgeous-alexander": Player("Shai Gilgeous-Alexander", "Thunder", 1, "G", 85),
    "gilgeous-alexander": Player("Shai Gilgeous-Alexander", "Thunder", 1, "G", 85),
    "durant": Player("Kevin Durant", "Suns", 1, "F", 78),
    "kevin durant": Player("Kevin Durant", "Suns", 1, "F", 78),
    "kd": Player("Kevin Durant", "Suns", 1, "F", 78),
    "booker": Player("Devin Booker", "Suns", 1, "G", 72),
    "devin booker": Player("Devin Booker", "Suns", 1, "G", 72),
    "mitchell": Player("Donovan Mitchell", "Cavaliers", 1, "G", 70),
    "donovan mitchell": Player("Donovan Mitchell", "Cavaliers", 1, "G", 70),
    
    # === ALL-STARS (Tier 2) - Impact 40-69 ===
    "butler": Player("Jimmy Butler", "Heat", 2, "F", 60),
    "jimmy butler": Player("Jimmy Butler", "Heat", 2, "F", 60),
    "brown": Player("Jaylen Brown", "Celtics", 2, "G", 55),
    "jaylen brown": Player("Jaylen Brown", "Celtics", 2, "G", 55),
    "irving": Player("Kyrie Irving", "Mavericks", 2, "G", 58),
    "kyrie": Player("Kyrie Irving", "Mavericks", 2, "G", 58),
    "kyrie irving": Player("Kyrie Irving", "Mavericks", 2, "G", 58),
    "harden": Player("James Harden", "Clippers", 2, "G", 55),
    "james harden": Player("James Harden", "Clippers", 2, "G", 55),
    "george": Player("Paul George", "76ers", 2, "F", 52),
    "paul george": Player("Paul George", "76ers", 2, "F", 52),
    "lillard": Player("Damian Lillard", "Bucks", 2, "G", 58),
    "damian lillard": Player("Damian Lillard", "Bucks", 2, "G", 58),
    "maxey": Player("Tyrese Maxey", "76ers", 2, "G", 50),
    "tyrese maxey": Player("Tyrese Maxey", "76ers", 2, "G", 50),
    "edwards": Player("Anthony Edwards", "Timberwolves", 1, "G", 75),
    "ant": Player("Anthony Edwards", "Timberwolves", 1, "G", 75),
    "anthony edwards": Player("Anthony Edwards", "Timberwolves", 1, "G", 75),
    "morant": Player("Ja Morant", "Grizzlies", 2, "G", 65),
    "ja morant": Player("Ja Morant", "Grizzlies", 2, "G", 65),
    "haliburton": Player("Tyrese Haliburton", "Pacers", 2, "G", 60),
    "tyrese haliburton": Player("Tyrese Haliburton", "Pacers", 2, "G", 60),
    "mobley": Player("Evan Mobley", "Cavaliers", 2, "F", 50),
    "evan mobley": Player("Evan Mobley", "Cavaliers", 2, "F", 50),
    "cade": Player("Cade Cunningham", "Pistons", 2, "G", 55),
    "cade cunningham": Player("Cade Cunningham", "Pistons", 2, "G", 55),
    "randle": Player("Julius Randle", "Timberwolves", 2, "F", 45),
    "julius randle": Player("Julius Randle", "Timberwolves", 2, "F", 45),
    "westbrook": Player("Russell Westbrook", "Nuggets", 3, "G", 30),
    "russell westbrook": Player("Russell Westbrook", "Nuggets", 3, "G", 30),
    "gobert": Player("Rudy Gobert", "Timberwolves", 2, "C", 48),
    "rudy gobert": Player("Rudy Gobert", "Timberwolves", 2, "C", 48),
    "holmgren": Player("Chet Holmgren", "Thunder", 2, "C", 45),
    "chet holmgren": Player("Chet Holmgren", "Thunder", 2, "C", 45),
}


# Team name aliases for matching
TEAM_ALIASES = {
    "lakers": "Lakers", "la lakers": "Lakers",
    "warriors": "Warriors", "gsw": "Warriors", "golden state": "Warriors",
    "celtics": "Celtics", "boston": "Celtics",
    "bucks": "Bucks", "milwaukee": "Bucks",
    "nuggets": "Nuggets", "denver": "Nuggets",
    "76ers": "76ers", "sixers": "76ers", "philadelphia": "76ers",
    "suns": "Suns", "phoenix": "Suns",
    "mavericks": "Mavericks", "mavs": "Mavericks", "dallas": "Mavericks",
    "thunder": "Thunder", "okc": "Thunder", "oklahoma city": "Thunder",
    "cavaliers": "Cavaliers", "cavs": "Cavaliers", "cleveland": "Cavaliers",
    "heat": "Heat", "miami": "Heat",
    "timberwolves": "Timberwolves", "wolves": "Timberwolves", "minnesota": "Timberwolves",
    "grizzlies": "Grizzlies", "memphis": "Grizzlies",
    "pacers": "Pacers", "indiana": "Pacers",
    "pistons": "Pistons", "detroit": "Pistons",
}

# Injury keywords that signal impact
INJURY_KEYWORDS_OUT = ["out", "ruled out", "won't play", "will not play", "dnp", "sidelined", "out indefinitely", "out for season"]
INJURY_KEYWORDS_DOUBTFUL = ["doubtful", "unlikely", "probably won't", "expected to miss"]
INJURY_KEYWORDS_QUESTIONABLE = ["questionable", "game-time decision", "gtd", "50/50", "may not play"]
INJURY_KEYWORDS_RETURN = ["returning", "back", "will play", "cleared", "available", "good to go"]

# Status impact multiplier
STATUS_MULTIPLIER = {
    "out": 1.0,
    "doubtful": 0.7,
    "questionable": 0.3,
    "probable": -0.1,  # Return from injury, slight negative signal
    "returning": -0.2,  # Player returning = opponent's edge decreases
}

# Trusted injury reporters (instant trust)
TRUSTED_REPORTERS = [
    "shamscharania", "wojespn", "thenbacentral", "espn",
    "woj", "shams", "nbabreaking", "theathleticnba",
]


class InjuryDetector:
    """Detect injury news from text."""
    
    @staticmethod
    def parse(text: str) -> Optional[InjuryAlert]:
        """Parse text for injury information."""
        text_lower = text.lower()
        
        # Check for injury keywords
        status = None
        for kw in INJURY_KEYWORDS_OUT:
            if kw in text_lower:
                status = "out"
                break
        if not status:
            for kw in INJURY_KEYWORDS_DOUBTFUL:
                if kw in text_lower:
                    status = "doubtful"
                    break
        if not status:
            for kw in INJURY_KEYWORDS_QUESTIONABLE:
                if kw in text_lower:
                    status = "questionable"
                    break
        
        if not status:
            return None
        
        # Find player name
        player_name = InjuryDetector._extract_player(text_lower)
        if not player_name:
            return None
        
        # Find team
        team = InjuryDetector._extract_team(text_lower)
        
        return InjuryAlert(
            player_name=player_name,
            team=team or "",
            status=status,
            source="twitter",
            source_text=text[:200],
            confidence=0.8,
        )
    
    @staticmethod
    def _extract_player(text: str) -> Optional[str]:
        """Extract player name from text."""
        for key, player in NBA_PLAYERS.items():
            if key in text:
                return player.name
        return None
    
    @staticmethod
    def _extract_team(text: str) -> Optional[str]:
        """Extract team name from text."""
        for alias, team in TEAM_ALIASES.items():
            if alias in text:
                return team
        return None


class InjuryImpactAnalyzer:
    """Analyze the betting impact of an injury."""
    
    @staticmethod
    def analyze(alert: InjuryAlert) -> InjurySignal:
        """Analyze an injury alert and generate a signal."""
        
        # Look up player
        player = InjuryImpactAnalyzer._lookup_player(alert.player_name)
        
        if not player:
            return InjurySignal(
                alert=alert,
                player=None,
                recommendation="SKIP",
                impact="LOW",
                estimated_swing=0,
                reasoning=f"Unknown player: {alert.player_name}",
                should_trade=False,
            )
        
        # Calculate estimated probability swing
        base_swing = player.impact_score / 100 * 0.3  # Max 30% swing for superstar out
        status_mult = STATUS_MULTIPLIER.get(alert.status, 0.5)
        estimated_swing = base_swing * status_mult
        
        # Determine impact level
        if player.tier == 1 and alert.status == "out":
            impact = "HIGH"
            recommendation = "BUY_NO"  # Bet against the team
        elif player.tier == 1 and alert.status == "questionable":
            impact = "MEDIUM"
            recommendation = "SKIP"  # Wait for confirmation
        elif player.tier == 2 and alert.status == "out":
            impact = "MEDIUM"
            recommendation = "BUY_NO"
        elif player.tier <= 2 and alert.status in ("out", "doubtful"):
            impact = "MEDIUM" if player.tier == 2 else "LOW"
            recommendation = "BUY_NO" if alert.status == "out" else "SKIP"
        else:
            impact = "LOW"
            recommendation = "SKIP"
        
        should_trade = (
            recommendation != "SKIP" and
            player.tier <= 2 and  # Only Tier 1-2 players
            alert.status in ("out", "doubtful") and
            estimated_swing > 0.05  # At least 5% swing
        )
        
        reasoning = (
            f"{player.tier_label} ({player.name}, impact: {player.impact_score}) "
            f"status: {alert.status} → estimated {estimated_swing*100:.1f}% probability swing. "
            f"Team: {player.team}"
        )
        
        return InjurySignal(
            alert=alert,
            player=player,
            recommendation=recommendation,
            impact=impact,
            estimated_swing=estimated_swing,
            reasoning=reasoning,
            should_trade=should_trade,
        )
    
    @staticmethod
    def _lookup_player(name: str) -> Optional[Player]:
        """Look up a player in the database."""
        name_lower = name.lower().strip()
        
        # Direct lookup
        if name_lower in NBA_PLAYERS:
            return NBA_PLAYERS[name_lower]
        
        # Partial match
        for key, player in NBA_PLAYERS.items():
            if key in name_lower or name_lower in key:
                return player
            # Last name match
            last_name = player.name.split()[-1].lower()
            if name_lower == last_name:
                return player
        
        return None


class InjurySniper:
    """
    Monitor news sources for injury reports and generate instant betting signals.
    
    This is the "news sniper" strategy from the Polymarket alpha thread:
    - Connect to Twitter/RSS feeds
    - Parse for injury keywords + player names
    - Assess impact (superstar vs bench)
    - Generate instant BUY_NO signal before market adjusts
    """
    
    def __init__(self):
        self.detector = InjuryDetector()
        self.analyzer = InjuryImpactAnalyzer()
        self.seen_alerts: set = set()  # Avoid duplicate signals
    
    def scan_text(self, text: str, source: str = "twitter") -> Optional[InjurySignal]:
        """Scan a piece of text for injury news."""
        alert = self.detector.parse(text)
        
        if not alert:
            return None
        
        # Deduplicate
        alert_key = f"{alert.player_name}:{alert.status}:{alert.timestamp[:13]}"
        if alert_key in self.seen_alerts:
            return None
        
        self.seen_alerts.add(alert_key)
        
        # Analyze impact
        signal = self.analyzer.analyze(alert)
        
        if signal.should_trade:
            logger.info(
                f"🏀 INJURY SNIPER: {signal.player.name} ({signal.player.tier_label}) "
                f"→ {alert.status} | Team: {signal.player.team} | "
                f"Swing: {signal.estimated_swing*100:.1f}% | "
                f"Action: {signal.recommendation}"
            )
        
        return signal
    
    def scan_markets_for_injuries(self, markets: list[dict]) -> list[dict]:
        """
        Scan market titles for injury-related content.
        Sometimes the market itself tells you about injuries.
        """
        signals = []
        for m in markets:
            title = m.get('title', '') + ' ' + m.get('subtitle', '')
            signal = self.scan_text(title)
            if signal and signal.should_trade:
                signals.append({
                    "market_id": m.get('ticker', m.get('id', '')),
                    "question": m.get('title', ''),
                    "exchange": "kalshi",
                    "direction": signal.recommendation,
                    "edge": signal.estimated_swing,
                    "confidence": 0.6,
                    "model_probability": 0.5 + signal.estimated_swing,
                    "market_price": float(m.get('yes_ask', 0.5) or 0.5),
                    "signals": {
                        "type": "injury_sniper",
                        "player": signal.player.name if signal.player else "unknown",
                        "tier": signal.player.tier if signal.player else 0,
                        "status": signal.alert.status,
                        "impact": signal.impact,
                        "reasoning": signal.reasoning,
                    },
                    "should_trade": True,
                })
        return signals
