import json
from pathlib import Path

from bot.weather.training_dataset import (
    TIER_ARCHIVE_HISTORICAL_POST_FACTO,
    TIER_ARCHIVE_MISSING_WEATHER,
    TIER_FIRST_PARTY_RECORDED_AS_OF,
    dedupe_rows,
    load_resolution_index,
    normalize_input_rows,
    normalize_row,
    split_train_validation,
    summarize_rows,
)


def make_snapshot(
    market_id: str,
    day: str,
    *,
    source_mode: str,
    outcome: str | None = "YES",
    high: float = 84.0,
) -> dict:
    source = "historical_post_facto" if source_mode == "historical_post_facto" else "provided"
    source_provenance = "historical_post_facto_backfill" if source_mode == "historical_post_facto" else "recorded_collection"
    anti_hindsight = (
        "post_facto_weather_not_recorded_as_of"
        if source_mode == "historical_post_facto"
        else "recorded_at_decision_time"
    )
    weather_snapshot = {
        "mode": source_mode,
        "source_provenance": source_provenance,
        "provenance": {
            "source_mode": source_mode,
            "source_provenance": source_provenance,
            "anti_hindsight": anti_hindsight,
        },
        "market_id": market_id,
        "question": "Will the high temp in New York be >82.5 on Oct 24, 2024?",
        "market_date": day,
        "weather_date": day,
        "date_validation": {
            "ok": True,
            "reason": "dates_match",
            "market_date": day,
            "weather_date": day,
            "source": "ticker:market_ticker",
        },
        "settlement_source": "nws",
        "station_id": "KNYC",
        "station_cli": "NYC",
        "station_resolution": {
            "city_id": "new_york_ny",
            "city": "New York",
            "state": "NY",
            "station_id": "KNYC",
        },
        "forecast": {
            "high": high,
            "low": 73.0,
            "current": 80.0,
            "actual_temp_used": high,
            "threshold": 82.5,
            "question_side": "above",
        },
    }
    return {
        "timestamp": "2026-05-11T01:05:40+00:00",
        "observed_at": "2026-05-11T01:05:40+00:00",
        "run_id": "plab_test",
        "market_id": market_id,
        "group": "weather",
        "market_route": {
            "family": "daily_temperature",
            "subcategory": "tail_high",
            "evidence": {
                "series_ticker": "KXHIGHNY",
                "event_ticker": "KXHIGHNY-24OCT24",
                "market_family": "daily_temperature",
                "shape": "tail_high",
            },
        },
        "series": "KXHIGHNY",
        "question": "Will the high temp in New York be >82.5 on Oct 24, 2024?",
        "yes_price": 0.37,
        "no_price": 0.64,
        "direction": "SKIP",
        "decision_type": "skip",
        "decision_artifact": {
            "market_id": market_id,
            "logic_version": "test-v1",
            "final_action": "SKIP",
            "final_reason_code": "test_skip",
            "strategy_trace": {
                "ensemble_signal": {
                    "model_probability": 0.58,
                    "edge": 0.21,
                }
            },
            "source_context": {
                "source": source,
                "source_mode": source_mode,
                "source_provenance": source_provenance,
                "provenance": {
                    "source_mode": source_mode,
                    "source_provenance": source_provenance,
                    "anti_hindsight": anti_hindsight,
                },
                "data": {
                    "market_metadata": {
                        "event_ticker": "KXHIGHNY-24OCT24",
                        "series": "KXHIGHNY",
                        "market_family": "daily_temperature",
                        "result": outcome,
                    },
                    "weather_source_snapshot": weather_snapshot,
                },
            },
            "source_snapshots": [
                {
                    "mode": source_mode,
                    "source": "weather",
                    "source_provenance": source_provenance,
                    "snapshot_ref": "source_context.data.weather_source_snapshot",
                    "weather_date": day,
                }
            ],
            "order_book_snapshot": {
                "source": "book",
                "data": {
                    "best_yes_ask": 0.38,
                    "best_yes_bid": 0.36,
                    "best_no_ask": 0.65,
                    "best_no_bid": 0.63,
                    "mid_yes": 0.37,
                },
            },
        },
    }


def test_archive_post_facto_is_training_usable_but_never_production_replay_grade():
    row = make_snapshot("KXHIGHNY-24OCT24-B82.5", "2024-10-24", source_mode="historical_post_facto")

    normalized = normalize_row(
        row,
        input_path="data/archive_replay/run/prediction_lab/market_snapshots.jsonl",
        input_line=1,
        dataset_id="test-dataset",
        dataset_source="archive_replay",
    )

    assert normalized["provenance_tier"] == TIER_ARCHIVE_HISTORICAL_POST_FACTO
    assert normalized["source_mode"] == "historical_post_facto"
    assert normalized["anti_hindsight"] == "post_facto_weather_not_recorded_as_of"
    assert normalized["quality"]["usable_for_training"] is True
    assert normalized["quality"]["usable_for_production_replay"] is False
    assert "historical_post_facto_not_production_replay_grade" in normalized["quality"]["warnings"]


def test_first_party_recorded_as_of_can_be_production_replay_grade():
    row = make_snapshot("KXHIGHNY-24OCT24-B82.5", "2024-10-24", source_mode="recorded_as_of")

    normalized = normalize_row(
        row,
        input_path="data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl",
        input_line=1,
        dataset_id="test-dataset",
        dataset_source="first_party",
    )

    assert normalized["provenance_tier"] == TIER_FIRST_PARTY_RECORDED_AS_OF
    assert normalized["source_mode"] == "recorded_as_of"
    assert normalized["anti_hindsight"] == "recorded_at_decision_time"
    assert normalized["quality"]["usable_for_training"] is True
    assert normalized["quality"]["usable_for_production_replay"] is True


def test_archive_missing_weather_is_separate_and_not_training_usable():
    row = make_snapshot("KXHIGHNY-24OCT24-B82.5", "2024-10-24", source_mode="historical_post_facto")
    row["decision_artifact"]["source_context"]["data"].pop("weather_source_snapshot")
    row["decision_artifact"]["source_snapshots"] = []

    normalized = normalize_row(
        row,
        input_path="data/archive_replay/run/prediction_lab/market_snapshots.jsonl",
        input_line=1,
        dataset_id="test-dataset",
        dataset_source="archive_replay",
    )

    assert normalized["provenance_tier"] == TIER_ARCHIVE_MISSING_WEATHER
    assert normalized["source_mode"] == "missing"
    assert normalized["quality"]["usable_for_training"] is False
    assert "missing_weather" in normalized["quality"]["warnings"]


def test_dedupe_keeps_highest_quality_row_within_same_provenance_tier():
    unresolved = normalize_row(
        make_snapshot("KXHIGHNY-24OCT24-B82.5", "2024-10-24", source_mode="recorded_as_of", outcome=None),
        input_path="data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl",
        input_line=1,
        dataset_id="test-dataset",
        dataset_source="first_party",
    )
    resolved = normalize_row(
        make_snapshot("KXHIGHNY-24OCT24-B82.5", "2024-10-24", source_mode="recorded_as_of", outcome="YES"),
        input_path="data/beta_shadow/paper/prediction_lab/market_snapshots.jsonl",
        input_line=2,
        dataset_id="test-dataset",
        dataset_source="first_party",
    )

    rows, stats = dedupe_rows([unresolved, resolved])

    assert len(rows) == 1
    assert rows[0]["input_line"] == 2
    assert rows[0]["resolution"]["outcome"] == "YES"
    assert stats["dropped_rows"] == 1
    assert stats["dropped_by_provenance_tier"] == {TIER_FIRST_PARTY_RECORDED_AS_OF: 1}


def test_split_is_time_based_and_summary_counts_provenance(tmp_path: Path):
    rows = []
    for index, day in enumerate(["2024-10-20", "2024-10-21", "2024-10-22", "2024-10-23", "2024-10-24"], start=1):
        rows.append(
            normalize_row(
                make_snapshot(f"KXHIGHNY-24OCT{19 + index}-B82.5", day, source_mode="historical_post_facto"),
                input_path="data/archive_replay/run/prediction_lab/market_snapshots.jsonl",
                input_line=index,
                dataset_id="test-dataset",
                dataset_source="archive_replay",
            )
        )

    train_rows, validation_rows, split_stats = split_train_validation(rows, validation_fraction=0.4)
    summary = summarize_rows(rows, split_stats=split_stats)

    assert [row["market"]["event_date"] for row in train_rows] == ["2024-10-20", "2024-10-21", "2024-10-22"]
    assert [row["market"]["event_date"] for row in validation_rows] == ["2024-10-23", "2024-10-24"]
    assert summary["counts"]["by_provenance_tier"] == {TIER_ARCHIVE_HISTORICAL_POST_FACTO: 5}
    assert summary["quality"]["usable_for_training"] == 5
    assert summary["quality"]["usable_for_production_replay"] == 0


def test_file_normalizer_joins_resolution_inputs(tmp_path: Path):
    market_path = tmp_path / "archive_replay" / "run" / "prediction_lab" / "market_snapshots.jsonl"
    resolution_path = tmp_path / "archive_replay" / "run" / "prediction_lab" / "resolutions.jsonl"
    market_path.parent.mkdir(parents=True)
    row = make_snapshot("KXHIGHNY-24OCT24-B82.5", "2024-10-24", source_mode="historical_post_facto", outcome=None)
    market_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    resolution_path.write_text(
        json.dumps(
            {
                "market_id": "KXHIGHNY-24OCT24-B82.5",
                "resolution": {
                    "outcome": "NO",
                    "resolved_at": "2024-10-25T00:00:00+00:00",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = normalize_input_rows(
        [market_path],
        resolution_index=load_resolution_index([resolution_path]),
        dataset_id="test-dataset",
    )

    assert len(rows) == 1
    assert rows[0]["dataset_source"] == "archive_replay"
    assert rows[0]["resolution"]["outcome"] == "NO"
    assert rows[0]["resolution"]["resolved_at"] == "2024-10-25T00:00:00+00:00"
