"""Prompt templates for the OpenRouter quant researcher.

Each market type has its own prompt style. The researcher uses these
to analyze markets, review trade outcomes, and suggest strategy updates.

To add prompts for a new market type, add a function here and reference
it in config.yaml under market_types.{type}.researcher_prompts.
"""

from typing import Optional


# === Shared system prompts ===

SYSTEM_ANALYST = """You are a quantitative analyst specializing in prediction markets.
Your job is to identify mispriced markets by analyzing the question, current prices,
volume, and any contextual information provided.
Be concise. Output JSON only. No explanations unless asked."""

SYSTEM_REVIEWER = """You are a trading performance reviewer. Analyze trade outcomes
to find patterns in wins and losses. Identify what signals are working and what
needs adjustment. Be brutally honest about what's not working.
Output structured analysis with specific, actionable recommendations."""

SYSTEM_STRATEGIST = """You are a strategy optimization expert. Given historical
trade data and outcomes, suggest specific parameter adjustments to improve
the trading edge. Consider: edge thresholds, confidence levels, position sizing,
and timing. Output specific numeric recommendations."""


# === Market-specific analysis prompts ===

def sports_analysis(market: dict, context: dict = None) -> tuple[str, str]:
    """Generate analysis prompt for a sports market."""
    ctx = context or {}
    
    prompt = f"""Analyze this sports prediction market for trading opportunity:

**Market:** {market.get('question', 'N/A')}
**Yes Price:** {market.get('yes_price', 0):.2%}
**No Price:** {market.get('no_price', 0):.2%}
**Volume:** ${market.get('volume', 0):,.0f}
**Closes:** {market.get('closes_at', 'unknown')}
**Category:** {market.get('category', 'sports')}

"""

    if ctx.get("news"):
        prompt += f"**Recent News:** {ctx['news'][:500]}\n\n"
    
    if ctx.get("social_signals"):
        prompt += f"**Social Signals:** {ctx['social_signals'][:300]}\n\n"

    if ctx.get("historical_context"):
        prompt += f"**Historical:** {ctx['historical_context'][:300]}\n\n"

    prompt += """Respond in JSON:
{
  "assessment": "overvalued | undervalued | fair",
  "true_probability": 0.XX,
  "edge": 0.XX,
  "confidence": 0.XX,
  "direction": "BUY_YES" | "BUY_NO" | "SKIP",
  "reasoning": "one sentence",
  "key_factors": ["factor1", "factor2"]
}"""

    return SYSTEM_ANALYST, prompt


def politics_analysis(market: dict, context: dict = None) -> tuple[str, str]:
    """Generate analysis prompt for a politics market."""
    ctx = context or {}
    
    prompt = f"""Analyze this political prediction market:

**Market:** {market.get('question', 'N/A')}
**Yes Price:** {market.get('yes_price', 0):.2%}
**No Price:** {market.get('no_price', 0):.2%}
**Volume:** ${market.get('volume', 0):,.0f}
**Closes:** {market.get('closes_at', 'unknown')}
"""

    if ctx.get("polls"):
        prompt += f"**Poll Data:** {ctx['polls'][:400]}\n\n"
    if ctx.get("news"):
        prompt += f"**Recent News:** {ctx['news'][:400]}\n\n"

    prompt += """Respond in JSON:
{
  "assessment": "overvalued | undervalued | fair",
  "true_probability": 0.XX,
  "edge": 0.XX,
  "confidence": 0.XX,
  "direction": "BUY_YES" | "BUY_NO" | "SKIP",
  "reasoning": "one sentence",
  "key_factors": ["factor1", "factor2"]
}"""

    return SYSTEM_ANALYST, prompt


# === Trade review prompts ===

def daily_review(trades: list[dict], stats: dict) -> tuple[str, str]:
    """Generate daily performance review prompt."""
    
    # Summarize trades concisely
    trade_summaries = []
    for t in trades[-20:]:  # last 20 trades max
        trade_summaries.append(
            f"- {t.get('direction', '?')} | edge={t.get('edge', 0):.2%} | "
            f"conf={t.get('confidence', 0):.2%} | size=${t.get('position_size', 0):.2f} | "
            f"outcome={'WIN' if t.get('pnl', 0) > 0 else 'LOSS' if t.get('pnl', 0) < 0 else 'pending'}"
        )
    
    trades_text = "\n".join(trade_summaries) if trade_summaries else "No trades today."

    prompt = f"""Review today's trading performance:

**Stats:**
- Total trades: {stats.get('total_trades', 0)}
- Win rate: {stats.get('win_rate', 0):.1%}
- Total P&L: ${stats.get('total_pnl', 0):+.2f}
- Avg edge: {stats.get('avg_edge', 0):.2%}
- Avg confidence: {stats.get('avg_confidence', 0):.2%}
- Scans run: {stats.get('scans', 0)}

**Trades:**
{trades_text}

Analyze:
1. Are wins correlated with specific edge/confidence ranges?
2. Are losses clustering around certain market types or times?
3. Is the edge threshold (min_edge) set correctly?
4. Is the confidence threshold (min_confidence) set correctly?
5. Any specific patterns in losing trades?

Respond in JSON:
{
  "summary": "one sentence overall assessment",
  "win_patterns": ["pattern1", "pattern2"],
  "loss_patterns": ["pattern1", "pattern2"],
  "recommendations": {
    "min_edge": {"current": 0.XX, "suggested": 0.XX, "reason": "..."},
    "min_confidence": {"current": 0.XX, "suggested": 0.XX, "reason": "..."}
  },
  "markets_to_avoid": ["description"],
  "markets_to_focus": ["description"]
}"""

    return SYSTEM_REVIEWER, prompt


# === Strategy optimization prompts ===

def strategy_tune(historical_stats: list[dict]) -> tuple[str, str]:
    """Generate strategy tuning prompt from historical performance."""
    
    stats_text = "\n".join([
        f"- Day {s.get('date', '?')}: {s.get('trades', 0)} trades, "
        f"WR={s.get('win_rate', 0):.1%}, P&L=${s.get('pnl', 0):+.2f}, "
        f"avg_edge={s.get('avg_edge', 0):.2%}"
        for s in historical_stats[-7:]  # last 7 days
    ])

    prompt = f"""Optimize strategy parameters based on recent performance:

**Last 7 Days:**
{stats_text or "No historical data yet."}

Based on this data, recommend specific parameter adjustments.
Consider: too aggressive = more losses, too conservative = missed opportunities.

Respond in JSON:
{
  "analysis": "one sentence",
  "adjustments": {
    "min_edge": {"from": "0.XX", "to": "0.XX", "reason": "..."},
    "min_confidence": {"from": "0.XX", "to": "0.XX", "reason": "..."},
    "kelly_fraction": {"from": "0.XX", "to": "0.XX", "reason": "..."}
  },
  "focus_areas": ["area1", "area2"],
  "avoid": ["thing1", "thing2"]
}"""

    return SYSTEM_STRATEGIST, prompt


# === Prompt registry ===
# Maps config.yaml market_types.X.researcher_prompts to functions
ANALYSIS_PROMPTS = {
    "sports": sports_analysis,
    "politics": politics_analysis,
}

REVIEW_PROMPTS = {
    "daily": daily_review,
    "tune": strategy_tune,
}


def get_analysis_prompt(market_type: str, market: dict, context: dict = None) -> tuple[str, str]:
    """Get the appropriate analysis prompt for a market type."""
    prompt_fn = ANALYSIS_PROMPTS.get(market_type, sports_analysis)
    return prompt_fn(market, context)
