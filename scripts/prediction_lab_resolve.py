#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

from bot.config import load_config
from bot.prediction_lab import PredictionLab
from bot.runner import PredictionBot


def main() -> int:
    parser = argparse.ArgumentParser(description='Resolve matured Prediction Lab predictions')
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()

    config = load_config(PROJECT_ROOT / args.config)
    config['_config_path'] = str(PROJECT_ROOT / args.config)

    bot = PredictionBot(config)
    try:
        api_key = os.getenv('KALSHI_API_KEY_ID')
        private_key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH', 'kalshi_private_key')
        bot.add_kalshi(api_key, private_key_path, demo=args.demo)
        connections = bot.connect_all()
        if not any(connections.values()):
            raise SystemExit('No exchanges connected')
        exchange = next(iter(bot.exchanges.values()))
        lab = PredictionLab(config)
        result = lab.resolve_open_predictions(exchange)
        print(result)
    finally:
        bot.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
