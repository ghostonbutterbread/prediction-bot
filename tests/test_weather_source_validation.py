import json
import tempfile
import unittest
from pathlib import Path

from bot.weather.source_validation import (
    SourceValidationError,
    build_source_validation_report,
    load_source_validation_pilot,
)
from scripts.weather_source_validation_report import main as validation_report_main


def write_history(path: Path, rows: list[str]) -> None:
    path.write_text(
        "\n".join(
            [
                "EVENT_TICKER,MARKET_TICKER,MARKET_TITLE,MARKET_SUBTITLE,YES_SUBTITLE,NO_SUBTITLE,RESULT,END_DT,CLOSED_DT",
                *rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class WeatherSourceValidationTests(unittest.TestCase):
    def test_report_scores_directional_entries_against_threshold_markets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_path = root / "kalshi.csv"
            source_dir = root / "sources"
            source_dir.mkdir()
            write_history(
                history_path,
                [
                    'KXHIGHMIA,KXHIGHMIA-26MAR14-T86,"Will the **high temp in Miami** be >86° on Mar 14, 2026?",>86°,Yes,No,no,2026-03-15T05:59:00Z,2026-03-15T11:00:00Z',
                    'KXLOWTMIA,KXLOWTMIA-26MAR14-T72,"Will the minimum temperature be >72° on Mar 14, 2026?",>72°,Yes,No,no,2026-03-15T05:59:00Z,2026-03-15T11:00:00Z',
                ],
            )
            (source_dir / "miami_fl.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "city_id": "miami_fl",
                        "city": "Miami",
                        "pilot_notes": [],
                        "source_groups": {
                            "official_sources": [
                                {
                                    "source_id": "src_nws_mfl",
                                    "registry_source_id": "src_nws_mfl",
                                    "name": "NWS Miami-South Florida",
                                    "platform": "weather_gov",
                                    "url": "https://www.weather.gov/mfl/",
                                    "status": "primary_reference",
                                    "notes": [],
                                }
                            ],
                            "resolution_adjacent_sources": [
                                {
                                    "source_id": "src_station_kmia",
                                    "registry_source_id": "src_station_kmia",
                                    "name": "NWS Station Observation KMIA",
                                    "platform": "weather_gov_api",
                                    "url": "https://api.weather.gov/stations/KMIA/observations/latest",
                                    "status": "settlement_reference",
                                    "notes": [],
                                }
                            ],
                            "candidate_sources": [
                                {
                                    "source_id": "candidate_weather_com_miami",
                                    "name": "Weather.com Miami",
                                    "scope": "global",
                                    "platform": "weather_com",
                                    "url": "https://example.com/weather",
                                    "status": "candidate",
                                    "notes": [],
                                },
                                {
                                    "source_id": "candidate_local_tv_miami",
                                    "name": "Local TV Miami Weather",
                                    "scope": "local",
                                    "platform": "local_tv",
                                    "url": "https://example.com/local",
                                    "status": "candidate",
                                    "notes": [],
                                },
                            ],
                        },
                        "archive_reference_markets": [
                            {"market_ticker": "KXHIGHMIA-26MAR14-T86", "notes": ["Reference market"]}
                        ],
                        "validation_entries": [
                            {
                                "source_id": "candidate_weather_com_miami",
                                "market_ticker": "KXHIGHMIA-26MAR14-T86",
                                "predicted_direction": "below",
                                "evidence": "Forecast high stayed under 86F",
                                "notes": [],
                            },
                            {
                                "source_id": "candidate_local_tv_miami",
                                "market_ticker": "KXLOWTMIA-26MAR14-T72",
                                "predicted_value_f": 75,
                                "evidence": "Broadcast low forecast was 75F",
                                "notes": [],
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_source_validation_report(source_dir=source_dir, history_path=history_path)

            self.assertEqual(report["summary"]["cities"], 1)
            self.assertEqual(report["summary"]["sources"], 4)
            self.assertEqual(report["summary"]["matched_validation_entries"], 2)
            self.assertEqual(report["summary"]["correct"], 1)
            self.assertEqual(report["summary"]["incorrect"], 1)
            self.assertEqual(report["summary"]["accuracy"], 0.5)

            [city_report] = report["cities"]
            self.assertEqual(city_report["summary"]["inventory_counts"]["candidate_sources"], 2)
            self.assertEqual(city_report["archive_reference_markets"][0]["question_side"], "above")
            self.assertEqual(city_report["validation_entries"][0]["predicted_outcome"], "NO")
            self.assertTrue(city_report["validation_entries"][0]["was_correct"])
            self.assertEqual(city_report["validation_entries"][1]["predicted_direction"], "above")
            self.assertFalse(city_report["validation_entries"][1]["was_correct"])

    def test_loader_rejects_candidate_without_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "city_id": "new_york_ny",
                        "city": "New York",
                        "source_groups": {
                            "official_sources": [],
                            "resolution_adjacent_sources": [],
                            "candidate_sources": [
                                {
                                    "source_id": "candidate_missing_scope",
                                    "name": "Missing Scope",
                                    "platform": "example",
                                    "url": "https://example.com",
                                    "status": "candidate",
                                    "notes": [],
                                }
                            ],
                        },
                        "archive_reference_markets": [],
                        "validation_entries": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(SourceValidationError):
                load_source_validation_pilot(path)


class WeatherSourceValidationScriptTests(unittest.TestCase):
    def test_script_writes_json_report_for_city_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_path = root / "kalshi.csv"
            source_dir = root / "sources"
            source_dir.mkdir()
            write_history(
                history_path,
                [
                    'KXHIGHTSEA,KXHIGHTSEA-26MAR14-T50,"Will the maximum temperature be >50° on Mar 14, 2026?",>50°,Yes,No,no,2026-03-15T05:59:00Z,2026-03-15T11:00:00Z',
                ],
            )
            (source_dir / "seattle_wa.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "city_id": "seattle_wa",
                        "city": "Seattle",
                        "pilot_notes": [],
                        "source_groups": {
                            "official_sources": [
                                {
                                    "source_id": "src_nws_sew",
                                    "registry_source_id": "src_nws_sew",
                                    "name": "NWS Seattle, WA",
                                    "platform": "weather_gov",
                                    "url": "https://www.weather.gov/sew/",
                                    "status": "primary_reference",
                                    "notes": [],
                                }
                            ],
                            "resolution_adjacent_sources": [],
                            "candidate_sources": [
                                {
                                    "source_id": "candidate_kiro7_weather",
                                    "name": "KIRO 7 Weather",
                                    "scope": "local",
                                    "platform": "kiro7",
                                    "url": "https://www.kiro7.com/weather/",
                                    "status": "candidate",
                                    "notes": [],
                                }
                            ],
                        },
                        "archive_reference_markets": [
                            {"market_ticker": "KXHIGHTSEA-26MAR14-T50", "notes": []}
                        ],
                        "validation_entries": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output_path = root / "report.json"

            exit_code = validation_report_main(
                [
                    "--input-dir",
                    str(source_dir),
                    "--history",
                    str(history_path),
                    "--output",
                    str(output_path),
                    "--city",
                    "seattle_wa",
                ]
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["cities"], 1)
            self.assertEqual(payload["cities"][0]["city_id"], "seattle_wa")
            self.assertEqual(payload["cities"][0]["archive_reference_markets"][0]["threshold_value"], 50.0)
