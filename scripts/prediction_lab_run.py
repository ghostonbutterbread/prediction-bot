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
from bot.prediction_lab_support import build_prediction_lab_exchange


def main() -> int:
    parser = argparse.ArgumentParser(description='Run Prediction Lab scoring pass')
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()

    config = load_config(PROJECT_ROOT / args.config)
    config['_config_path'] = str(PROJECT_ROOT / args.config)

    bot, exchange = build_prediction_lab_exchange(config, demo=args.demo)
    try:
        lab = PredictionLab(config)
        result = lab.run(exchange)
        print({
            'run_id': result.run_id,
            'scanned_markets': result.scanned_markets,
            'recorded_predictions': result.recorded_predictions,
            'group_counts': result.group_counts,
            'ledger_path': result.ledger_path,
        })
    finally:
        bot.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
