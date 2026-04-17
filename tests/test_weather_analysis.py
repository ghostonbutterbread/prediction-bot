import json
import tempfile
import unittest
from pathlib import Path

from bot.weather import WeatherRegistry
from bot.weather.analysis import (
    WeatherSampleRecord,
    compare_sample_records,
    is_weather_market,
    load_historical_csv_samples,
    load_scan_samples,
    load_simulation_samples,
    load_snapshot_samples,
)
from bot.weather.historical import build_historical_city_coverage, load_historical_weather_records


class WeatherAnalysisTests(unittest.TestCase):
    def test_registry_alias_can_map_nyc_when_legacy_city_is_missing(self):
        record = WeatherSampleRecord(
            sample_kind="resolved_trade",
            source_path="memory",
            observed_at="2026-03-24T20:02:54+00:00",
            resolved_at="2026-04-02T17:00:00+00:00",
            market_id="KXHIGHNY-26MAR25-T51",
            category="",
            question="Will the **high temp in NYC** be <51° on Mar 25, 2026?",
            outcome="NO",
        )

        [comparison] = compare_sample_records([record], registry=WeatherRegistry.from_file())

        self.assertIsNone(comparison["baseline_city"])
        self.assertEqual(comparison["registry_city_id"], "new_york_ny")
        self.assertEqual(comparison["registry_primary_source_id"], "src_nws_okx")
        self.assertEqual(comparison["city_fit"], "registry_only")

    def test_ticker_based_baseline_and_registry_align_for_generic_miami_market(self):
        record = WeatherSampleRecord(
            sample_kind="snapshot",
            source_path="memory",
            observed_at="2026-04-14T12:00:00+00:00",
            resolved_at=None,
            market_id="KXHIGHMIA-26APR15-T77",
            category="KXHIGHMIA",
            question="Will the maximum temperature be  <77° on Apr 15, 2026?",
        )

        [comparison] = compare_sample_records([record], registry=WeatherRegistry.from_file())

        self.assertEqual(comparison["baseline_city"], "miami")
        self.assertEqual(comparison["baseline_city_source"], "series_ticker")
        self.assertEqual(comparison["baseline_market_type"], "temperature")
        self.assertEqual(comparison["normalized_market_type"], "high_temp")
        self.assertEqual(comparison["registry_city_id"], "miami_fl")
        self.assertTrue(comparison["registry_supports_market_type"])
        self.assertEqual(comparison["city_fit"], "aligned")

    def test_registry_mapping_aligns_for_generic_new_orleans_market(self):
        record = WeatherSampleRecord(
            sample_kind="scan_signal",
            source_path="memory",
            observed_at="2026-03-23T20:19:48+00:00",
            resolved_at=None,
            market_id="KXHIGHTNOLA-26MAR24-T84",
            category="KXHIGHTNOLA",
            question="Will the maximum temperature be  >84° on Mar 24, 2026?",
        )

        [comparison] = compare_sample_records([record], registry=WeatherRegistry.from_file())

        self.assertEqual(comparison["baseline_city"], "new orleans")
        self.assertEqual(comparison["registry_city_id"], "new_orleans_la")
        self.assertEqual(comparison["registry_primary_source_id"], "src_nws_lix")
        self.assertEqual(comparison["city_fit"], "aligned")

    def test_is_weather_market_rejects_non_temperature_kxhigh_series(self):
        self.assertFalse(
            is_weather_market(
                market_id="KXHIGHMOVKH-24NOV05-HI",
                question="Will California be the state that goes for Harris by the highest margin?",
                category="KXHIGHMOVKH",
            )
        )

    def test_historical_csv_loader_and_coverage_ignore_non_weather_kxhigh_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "kalshi.csv"
            history_path.write_text(
                "\n".join(
                    [
                        "EVENT_TICKER,MARKET_TICKER,MARKET_TITLE,RESULT,END_DT,CLOSED_DT",
                        'KXHIGHAUS-24DEC01,KXHIGHAUS-24DEC01-B70.5,"Will the **high temp in Austin** be 70-71° on Dec 1, 2024?",yes,2024-12-02T05:59:00.000000Z,2024-12-02T13:00:47.453684Z',
                        'KXHIGHMOVKH-24NOV05,KXHIGHMOVKH-24NOV05-HI,"Will California be the state that goes for Harris by the highest margin?",yes,2024-11-06T05:59:00.000000Z,2024-11-06T13:00:47.453684Z',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            samples = load_historical_csv_samples(history_path, one_per_series=True)
            history_records = load_historical_weather_records(history_path, one_per_series=True)

            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].market_id, "KXHIGHAUS-24DEC01-B70.5")
            self.assertEqual(samples[0].category, "KXHIGHAUS")

            report = build_historical_city_coverage(history_records, registry=WeatherRegistry.from_file())
            self.assertEqual(report["summary"]["records_examined"], 1)
            self.assertEqual(report["summary"]["unique_historical_cities"], 1)
            self.assertEqual(report["summary"]["registry_covered_cities"], 1)
            self.assertEqual(report["cities"][0]["city"], "austin")

    def test_loaders_normalize_snapshot_scan_and_resolved_trade_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-14T00:00:00+00:00",
                        "markets": [
                            {
                                "id": "KXHIGHMIA-26APR15-T77",
                                "question": "Will the maximum temperature be  <77° on Apr 15, 2026?",
                                "yes_price": 0.03,
                                "no_price": 0.98,
                                "volume": 550,
                                "category": "KXHIGHMIA",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            scan_path = root / "scan.jsonl"
            scan_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-14T14:30:24.641260+00:00",
                        "top_signals": [
                            {
                                "market_id": "KXHIGHMIA-26APR15-T77",
                                "market_price": 0.03,
                                "question": "Will the **high temp in Miami** be <77° on Apr 15, 2026?",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            sim_path = root / "sim.json"
            sim_path.write_text(
                json.dumps(
                    {
                        "trades": [
                            {
                                "market_id": "KXHIGHNY-26MAR25-T51",
                                "question": "Will the **high temp in NYC** be <51° on Mar 25, 2026?",
                                "timestamp": "2026-03-24T20:02:54.855758+00:00",
                                "resolved": True,
                                "resolved_at": "2026-04-02T17:00:00+00:00",
                                "market_price": 0.06,
                                "outcome": "no",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            [snapshot] = load_snapshot_samples(snapshot_path)
            [scan] = load_scan_samples(scan_path)
            [trade] = load_simulation_samples(sim_path, resolved_only=True)

            self.assertEqual(snapshot.sample_kind, "snapshot")
            self.assertEqual(snapshot.category, "KXHIGHMIA")
            self.assertEqual(scan.sample_kind, "scan_signal")
            self.assertEqual(scan.category, "KXHIGHMIA")
            self.assertEqual(trade.sample_kind, "resolved_trade")
            self.assertEqual(trade.outcome, "NO")


if __name__ == "__main__":
    unittest.main()
