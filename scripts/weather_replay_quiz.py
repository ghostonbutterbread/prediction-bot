#!/usr/bin/env python3
"""Build lightweight historical weather replay quiz records and optionally score answers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.analysis import load_historical_csv_samples  # noqa: E402
from bot.weather.replay import DEFAULT_REPLAY_FEE_RATE, ReplayFeeModel, build_weather_replay_dataset, score_replay_answers  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="data/historical/kalshi.csv",
        help="Historical Kalshi CSV to use as replay source.",
    )
    parser.add_argument(
        "--output",
        default="data/summaries/weather_replay_quiz.jsonl",
        help="Path for replay quiz payload JSONL.",
    )
    parser.add_argument(
        "--answer-key-output",
        default="data/summaries/weather_replay_answer_key.jsonl",
        help="Path for replay answer-key JSONL.",
    )
    parser.add_argument(
        "--score-answers",
        default="",
        help="Optional JSONL file containing replay answers to score.",
    )
    parser.add_argument(
        "--score-output",
        default="data/summaries/weather_replay_scores.json",
        help="Path for scored-answer summary JSON when --score-answers is provided.",
    )
    parser.add_argument(
        "--fee-rate",
        type=float,
        default=DEFAULT_REPLAY_FEE_RATE,
        help="Fee rate charged on positive gross replay profit (default: 0.07).",
    )
    parser.add_argument(
        "--notional-fee-rate",
        type=float,
        default=0.0,
        help="Optional fee rate charged on entry notional for every filled replay trade.",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=0.0,
        help="Optional entry slippage in basis points applied against the replayed trade.",
    )
    parser.add_argument(
        "--late-entry-penalty-rate",
        type=float,
        default=0.0,
        help="Optional conservative penalty applied to remaining upside to approximate later entry.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=50,
        help="Max replay records to emit after filtering weather history.",
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Keep every matching weather row instead of one record per series.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = _resolve_path(args.input)
    output_path = _resolve_path(args.output)
    answer_key_path = _resolve_path(args.answer_key_output)

    records = load_historical_csv_samples(input_path, one_per_series=not args.full_history)
    if args.max_records > 0:
        records = records[: args.max_records]

    dataset = build_weather_replay_dataset(records)
    _write_jsonl(output_path, dataset["quiz_payloads"])
    _write_jsonl(answer_key_path, dataset["answer_keys"])

    summary = dataset["summary"]
    print(f"Wrote {output_path.relative_to(PROJECT_ROOT) if output_path.is_relative_to(PROJECT_ROOT) else output_path}")
    print(
        "Replay records "
        f"count={summary['records']} "
        f"mapped_cities={summary['mapped_cities']} "
        f"with_prices={summary['with_prices']}"
    )
    print(
        f"Wrote {answer_key_path.relative_to(PROJECT_ROOT) if answer_key_path.is_relative_to(PROJECT_ROOT) else answer_key_path}"
    )

    if args.score_answers:
        answers_path = _resolve_path(args.score_answers)
        score_output_path = _resolve_path(args.score_output)
        answers = _load_jsonl(answers_path)
        scored = score_replay_answers(
            answers,
            dataset["answer_keys"],
            fee_model=ReplayFeeModel(
                profit_fee_rate=args.fee_rate,
                notional_fee_rate=args.notional_fee_rate,
                slippage_bps=args.slippage_bps,
                late_entry_penalty_rate=args.late_entry_penalty_rate,
            ),
        )
        score_output_path.parent.mkdir(parents=True, exist_ok=True)
        score_output_path.write_text(json.dumps(scored, indent=2) + "\n", encoding="utf-8")

        score_summary = scored["summary"]
        print(
            "Scored answers "
            f"count={score_summary['answers_scored']} "
            f"win_rate={score_summary['win_rate']} "
            f"gross_pnl={score_summary['net_pnl']} "
            f"fees={score_summary['fees_total']} "
            f"fee_adjusted_pnl={score_summary['fee_adjusted_pnl']}"
        )
        print(f"Score summary JSON {json.dumps(score_summary, sort_keys=True)}")
        print(
            f"Wrote {score_output_path.relative_to(PROJECT_ROOT) if score_output_path.is_relative_to(PROJECT_ROOT) else score_output_path}"
        )

    return 0


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True))
            handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
