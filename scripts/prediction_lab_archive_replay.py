#!/usr/bin/env python3
"""Run Prediction Lab against imported historical archive snapshots.

Example:
  python3 scripts/prediction_lab_archive_replay.py \
    --archive-dir data/external/prediction_market_analysis/data \
    --group weather --limit 1000 --data-dir data/archive_replay
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.archive_exchange import HistoricalKalshiArchiveExchange
from bot.config import load_config
from bot.prediction_lab import PredictionLab
from bot.weather.historical_provider import HistoricalOpenMeteoWeatherEngine


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def build_config(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(Path(args.config))
    patch = {
        "data_dir": args.data_dir,
        "strategy": {
            # Archive replay should be deterministic and not depend on live feeds.
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
        },
        "prediction_lab": {
            "enabled": True,
            "mode": "collector",
            "observer_mode": True,
            "groups": [args.group],
            "max_markets_per_run": args.limit,
            "collector_fetch_mode": "direct_markets",
            "collector_record_market_snapshots": True,
            "collector_record_predictions": True,
            "record_all_scored": True,
            "score_only": args.score_only,
            "min_confidence_to_record": args.min_confidence,
            "min_edge_to_record": args.min_edge,
            "experiment_id": args.experiment_id,
            "strategy_version": args.strategy_version,
            "disable_news": True,
            "disable_social": True,
            "disable_ai": True,
        },
    }
    return _deep_merge(cfg, patch)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Prediction Lab over imported Kalshi archive snapshots")
    parser.add_argument("--config", default="config.yaml", help="Base config path")
    parser.add_argument("--archive-dir", default="data/external/prediction_market_analysis/data")
    parser.add_argument("--data-dir", default="data/archive_replay", help="Where replay ledgers should be written")
    parser.add_argument("--group", default="weather", choices=["weather", "sports"], help="Market group to replay")
    parser.add_argument("--limit", type=int, default=1000, help="Max archived markets to score this run")
    parser.add_argument("--as-of", default=None, help="Optional archive _fetched_at cutoff timestamp")
    parser.add_argument("--score-only", action="store_true", help="Only write market snapshots, not prediction rows")
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--min-edge", type=float, default=-1.0)
    parser.add_argument("--experiment-id", default="archive-replay-v1")
    parser.add_argument("--strategy-version", default="archive-v1")
    parser.add_argument(
        "--historical-weather",
        action="store_true",
        default=None,
        help="Use historical observed weather with date-match validation. Defaults on for --group weather.",
    )
    parser.add_argument(
        "--no-historical-weather",
        action="store_false",
        dest="historical_weather",
        help="Disable historical weather injection. For weather archive replay this may use live-current weather and is unsafe for evaluation.",
    )
    parser.add_argument(
        "--historical-weather-cache",
        default=None,
        help="Optional JSON cache path for historical weather archive API responses.",
    )
    args = parser.parse_args()

    cfg = build_config(args)
    use_historical_weather = args.historical_weather if args.historical_weather is not None else args.group == "weather"
    historical_weather_engine = None
    if use_historical_weather:
        # PredictionLab constructs LiveFeedAggregator internally. Swap the
        # weather engine factory before instantiation so archive replay cannot
        # accidentally use current forecasts for old weather markets.
        import bot.feeds.live_data as live_data

        cache_path = args.historical_weather_cache or str(Path(args.data_dir) / "historical_weather_cache.json")
        historical_weather_engine = HistoricalOpenMeteoWeatherEngine(cache_path=cache_path)
        live_data.ProWeatherEngine = lambda: historical_weather_engine
    exchange = HistoricalKalshiArchiveExchange(
        args.archive_dir,
        as_of=args.as_of,
        groups=[args.group],
        dedupe_latest=True,
        include_closed=True,
    )
    if not exchange.connect():
        raise SystemExit(f"No archive parquet files found under {args.archive_dir}")

    lab = PredictionLab(cfg)
    result = lab.run(exchange)
    resolution = lab.resolve_open_predictions(exchange) if not args.score_only else {"resolved": 0, "skipped": 0}
    exchange.close()
    if historical_weather_engine is not None:
        historical_weather_engine.close()

    payload = {
        "run_id": result.run_id,
        "scanned_markets": result.scanned_markets,
        "recorded_predictions": result.recorded_predictions,
        "group_counts": result.group_counts,
        "series_counts_top": dict(sorted(result.series_counts.items(), key=lambda item: item[1], reverse=True)[:20]),
        "ledger_path": result.ledger_path,
        "market_snapshots_path": str(lab.market_snapshots_path),
        "resolutions_path": str(lab.resolutions_path),
        "resolution": resolution,
        "historical_weather": bool(use_historical_weather),
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
