"""Support helpers for Prediction Lab runtime integration."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bot.runner import PredictionBot

logger = logging.getLogger(__name__)


def build_prediction_lab_exchange(config: dict, *, demo: bool = False, verbose: bool = False):
    if verbose:
        logger.info('prediction_lab_support: building exchange demo=%s', demo)
    bot = PredictionBot(config)
    api_key = os.getenv('KALSHI_API_KEY_ID')
    private_key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH', 'kalshi_private_key')
    if verbose:
        logger.info(
            'prediction_lab_support: env loaded api_key_set=%s key_path=%s key_exists=%s',
            bool(api_key),
            private_key_path,
            Path(private_key_path).exists(),
        )
        logger.info('prediction_lab_support: adding kalshi exchange')
    bot.add_kalshi(api_key, private_key_path, demo=demo)
    if verbose:
        logger.info('prediction_lab_support: connecting exchanges')
    connections = bot.connect_all()
    if verbose:
        logger.info('prediction_lab_support: connect_all result=%s', connections)
    if not any(connections.values()):
        bot.close()
        raise SystemExit('No exchanges connected')
    exchange = next(iter(bot.exchanges.values()))
    allowed_groups = [
        str(group).strip().lower()
        for group in ((config.get('scan', {}) or {}).get('allowed_market_groups') or [])
        if str(group).strip()
    ]
    setter = getattr(exchange, 'set_allowed_market_groups', None)
    if callable(setter) and allowed_groups:
        setter(allowed_groups)
        if verbose:
            logger.info('prediction_lab_support: applied allowed_market_groups=%s', allowed_groups)
    if verbose:
        logger.info('prediction_lab_support: selected exchange=%s', getattr(exchange, 'name', type(exchange).__name__))
    return bot, exchange
