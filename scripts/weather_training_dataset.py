#!/usr/bin/env python3
"""Build an offline provenance-safe weather training dataset from JSONL rows."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.training_dataset import (  # noqa: E402
    DATASET_ARCHIVE_REPLAY,
    DATASET_FIRST_PARTY,
    dedupe_rows,
    default_dataset_id,
    load_resolution_index,
    normalize_input_rows,
    split_train_validation,
    summarize_rows,
    write_json,
    write_jsonl,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Prediction Lab market_snapshots.jsonl or predictions.jsonl input. May be repeated.",
    )
    parser.add_argument(
        "--resolution-input",
        action="append",
        default=[],
        help="Optional Prediction Lab resolutions.jsonl input. May be repeated.",
    )
    parser.add_argument("--output", required=True, help="Normalized deduplicated dataset JSONL output.")
    parser.add_argument("--summary-output", required=True, help="Summary JSON output.")
    parser.add_argument("--train-output", required=True, help="Train split JSONL output.")
    parser.add_argument("--validation-output", required=True, help="Validation split JSONL output.")
    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=0.2,
        help="Fraction of usable training rows reserved for the latest-date validation split.",
    )
    parser.add_argument(
        "--source-label",
        choices=("auto", DATASET_FIRST_PARTY, DATASET_ARCHIVE_REPLAY),
        default="auto",
        help="Override dataset source classification for all inputs.",
    )
    parser.add_argument(
        "--dataset-id",
        default="",
        help="Optional stable dataset id. Defaults to weather-training-dataset-YYYYMMDD-HHMMSS.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0 <= args.validation_fraction <= 1:
        raise SystemExit("--validation-fraction must be between 0 and 1")

    dataset_id = args.dataset_id or default_dataset_id()
    resolution_index = load_resolution_index(args.resolution_input)
    normalized_rows = normalize_input_rows(
        args.input,
        resolution_index=resolution_index,
        dataset_id=dataset_id,
        source_label=args.source_label,
    )
    deduped_rows, dedupe_stats = dedupe_rows(normalized_rows)
    train_rows, validation_rows, split_stats = split_train_validation(
        deduped_rows,
        validation_fraction=args.validation_fraction,
    )
    summary = summarize_rows(
        deduped_rows,
        raw_input_rows=len(normalized_rows),
        dedupe_stats=dedupe_stats,
        split_stats=split_stats,
    )
    summary["dataset_id"] = dataset_id
    summary["inputs"] = {
        "market_snapshot_paths": list(args.input),
        "resolution_paths": list(args.resolution_input),
        "source_label": args.source_label,
    }

    write_jsonl(args.output, deduped_rows)
    write_jsonl(args.train_output, train_rows)
    write_jsonl(args.validation_output, validation_rows)
    write_json(args.summary_output, summary)

    print(
        "weather training dataset: "
        f"{len(deduped_rows)} rows, "
        f"{len(train_rows)} train, "
        f"{len(validation_rows)} validation, "
        f"{dedupe_stats['dropped_rows']} dropped duplicates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
