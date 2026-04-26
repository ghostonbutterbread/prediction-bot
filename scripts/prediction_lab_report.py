#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from bot.config import load_config
from bot.prediction_lab import PredictionLab


def main() -> int:
    parser = argparse.ArgumentParser(description='Report Prediction Lab accuracy/calibration summary')
    parser.add_argument('--config', default='config.yaml')
    args = parser.parse_args()

    config = load_config(PROJECT_ROOT / args.config)
    lab = PredictionLab(config)
    print(lab.summarize())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
