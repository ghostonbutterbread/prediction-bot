"""Researcher module — OpenRouter-powered quant analysis.

Usage:
    from bot.researcher import OpenRouterClient, get_analysis_prompt, FeedbackTracker

    client = OpenRouterClient(config)
    system, prompt = get_analysis_prompt("sports", market, context)
    response = client.query(prompt, system)
"""

from bot.researcher.openrouter import OpenRouterClient
from bot.researcher.prompts import (
    get_analysis_prompt,
    daily_review,
    strategy_tune,
)
from bot.researcher.feedback import FeedbackTracker
