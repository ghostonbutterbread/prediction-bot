"""Support helpers for Prediction Lab runtime integration."""

from __future__ import annotations

import os
from pathlib import Path

from bot.runner import PredictionBot


def build_prediction_lab_exchange(config: dict, *, demo: bool = False):
    bot = PredictionBot(config)
    api_key = os.getenv('KALSHI_API_KEY_ID')
    private_key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH', 'kalshi_private_key')
    bot.add_kalshi(api_key, private_key_path, demo=demo)
    connections = bot.connect_all()
    if not any(connections.values()):
        bot.close()
        raise SystemExit('No exchanges connected')
    exchange = next(iter(bot.exchanges.values()))
    return bot, exchange
