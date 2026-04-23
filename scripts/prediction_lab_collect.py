#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / '.env')

from bot.prediction_lab_collect import PredictionLabCollectorDaemon


def main() -> int:
    parser = argparse.ArgumentParser(description='Run Prediction Lab collector loop')
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--idle-sleep-seconds', type=float, default=5.0)
    parser.add_argument('--max-cycles', type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    daemon = PredictionLabCollectorDaemon(
        PROJECT_ROOT / args.config,
        demo=args.demo,
        verbose=args.verbose,
    )
    status = daemon.run(max_cycles=args.max_cycles, idle_sleep_seconds=args.idle_sleep_seconds)
    print({
        'collect_runs': status.collect_runs,
        'resolve_runs': status.resolve_runs,
        'skipped_collects': status.skipped_collects,
        'pause_reason': status.pause_reason,
        'warning_emitted': status.warning_emitted,
        'owner_lock_acquired': status.owner_lock_acquired,
        'exit_reason': status.exit_reason,
    })
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
