import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot.exchanges.base import Market
from bot.strategies.enhanced import EnhancedStrategyEngine
from bot.weather import ObservationLog, RegistryValidationError, WeatherRegistry
from bot.weather.market_mapping import WeatherMarketCityMapper


def make_market(
    question: str,
    *,
    market_id: str = "mkt-1",
    yes_price: float = 0.55,
    category: str = "KXHIGHMIA",
) -> Market:
    return Market(
        id=market_id,
        exchange="kalshi",
        question=question,
        yes_price=yes_price,
        no_price=1 - yes_price,
        volume=5000,
        liquidity=5000,
        closes_at=datetime.now(timezone.utc) + timedelta(days=1),
        category=category,
        metadata={},
    )


class WeatherRegistryTests(unittest.TestCase):
    def test_loads_starter_registry_and_exposes_city_sources(self):
        registry = WeatherRegistry.from_file()

        city = registry.get_city("miami_fl")
        sources = registry.get_sources("miami_fl")

        self.assertEqual(city["city"], "Miami")
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["source_id"], "src_nws_mfl")
        self.assertEqual(city["watch_only"], ["src_station_kmia"])

    def test_update_source_score_is_in_memory_until_saved(self):
        registry = WeatherRegistry.from_file()

        updated = registry.update_source_score(
            "src_nws_mfl",
            92,
            reviewed_at="2026-04-14T18:00:00Z",
            sample_size=4,
            reason="Strong recent high-temp calls",
        )

        self.assertEqual(updated["trust_score"], 92.0)
        self.assertEqual(updated["last_reviewed"], "2026-04-14T18:00:00Z")
        self.assertEqual(updated["metrics"]["sample_size"], 4)
        self.assertEqual(updated["notes"][-1], "Strong recent high-temp calls")

    def test_validation_rejects_unknown_source_reference(self):
        bad_registry = {
            "version": 1,
            "cities": [
                {
                    "city_id": "miami_fl",
                    "city": "Miami",
                    "state": "FL",
                    "country": "US",
                    "timezone": "America/New_York",
                    "nws_office": "MFL",
                    "default_market_types": ["high_temp"],
                    "resolution_notes": None,
                    "status": "active",
                    "trusted_primary": ["missing_source"],
                    "trusted_secondary": [],
                    "watch_only": [],
                    "rejected": [],
                    "updated_at": None,
                    "notes": [],
                }
            ],
            "sources": [],
        }

        with self.assertRaises(RegistryValidationError):
            WeatherRegistry(bad_registry)


class ObservationLogTests(unittest.TestCase):
    def test_skips_identical_record_inside_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "observations.jsonl"
            log = ObservationLog(log_path, identical_cooldown_seconds=3600, max_bytes=4096)
            record = {
                "ts": "2026-04-14T18:00:00Z",
                "market_id": "KXHIGHMIA-26APR14-T85",
                "city_id": "miami_fl",
                "market_type": "high_temp",
                "source_id": "src_nws_mfl",
                "kind": "forecast_update",
                "value": {"forecast_temp_f": 86, "direction": "above"},
                "confidence": 0.78,
            }

            first_write = log.append(record)
            second_write = log.append({**record, "ts": "2026-04-14T18:10:00Z"})

            self.assertTrue(first_write)
            self.assertFalse(second_write)
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            written = json.loads(lines[0])
            self.assertTrue(written["content_hash"].startswith("sha256:"))

    def test_allows_same_content_after_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "observations.jsonl"
            log = ObservationLog(log_path, identical_cooldown_seconds=600, max_bytes=4096)
            record = {
                "ts": "2026-04-14T18:00:00Z",
                "market_id": "KXHIGHMIA-26APR14-T85",
                "city_id": "miami_fl",
                "source_id": "src_nws_mfl",
                "kind": "forecast_update",
                "value": {"forecast_temp_f": 86},
            }

            self.assertTrue(log.append(record))
            self.assertTrue(log.append({**record, "ts": "2026-04-14T18:20:00Z"}))
            self.assertEqual(len(log_path.read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_rotates_to_archive_when_threshold_is_exceeded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "observations.jsonl"
            archive_dir = Path(tmpdir) / "archived"
            log = ObservationLog(log_path, identical_cooldown_seconds=0, max_bytes=250, archive_dir=archive_dir)

            first = {
                "ts": "2026-04-14T18:00:00Z",
                "market_id": "M1",
                "city_id": "miami_fl",
                "source_id": "src_nws_mfl",
                "kind": "forecast_update",
                "value": {"forecast_temp_f": 86, "direction": "above", "note": "first snapshot"},
            }
            second = {
                "ts": "2026-04-14T18:30:00Z",
                "market_id": "M2",
                "city_id": "miami_fl",
                "source_id": "src_nws_mfl",
                "kind": "forecast_update",
                "value": {"forecast_temp_f": 87, "direction": "above", "note": "second snapshot"},
            }

            self.assertTrue(log.append(first))
            self.assertTrue(log.append(second))

            archived_files = list(archive_dir.glob("observations_*.jsonl"))
            self.assertEqual(len(archived_files), 1)
            self.assertEqual(len(log_path.read_text(encoding="utf-8").strip().splitlines()), 1)
            self.assertEqual(len(archived_files[0].read_text(encoding="utf-8").strip().splitlines()), 1)


class WeatherMarketCityMapperTests(unittest.TestCase):
    def setUp(self):
        self.mapper = WeatherMarketCityMapper(WeatherRegistry.from_file())

    def test_resolves_city_from_question_text(self):
        context = self.mapper.resolve("Will the high temp in New York City be above 82°?")

        self.assertIsNotNone(context)
        self.assertEqual(context.city_id, "new_york_ny")
        self.assertEqual(context.primary_source_id, "src_nws_okx")

    def test_resolves_city_from_series_ticker_when_question_is_generic(self):
        context = self.mapper.resolve("Will the high temp be above 88°?", "KXHIGHAUSMIA")

        self.assertIsNotNone(context)
        self.assertEqual(context.city_id, "miami_fl")

    def test_resolves_new_orleans_from_registry_ticker_alias(self):
        context = self.mapper.resolve("Will the maximum temperature be >84°?", "KXHIGHTNOLA")

        self.assertIsNotNone(context)
        self.assertEqual(context.city_id, "new_orleans_la")
        self.assertEqual(context.primary_source_id, "src_nws_lix")

    def test_returns_none_for_unknown_city(self):
        self.assertIsNone(self.mapper.resolve("Will the high temp in Washington be above 88°?", "KXHIGHTDC"))


class WeatherObservationIntegrationTests(unittest.TestCase):
    def _build_engine(self, log_path: Path) -> EnhancedStrategyEngine:
        engine = EnhancedStrategyEngine(
            {
                "min_edge": 0.01,
                "min_confidence": 0.50,
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
                "enable_weather_observation_log": True,
                "weather_observation_log_path": str(log_path),
                "weather_observation_cooldown_seconds": 21600,
            }
        )
        self.addCleanup(engine.live_feeds.close)
        return engine

    def test_logs_compact_weather_observation_for_mapped_market(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "weather_observations.jsonl"
            engine = self._build_engine(log_path)
            market = make_market("Will the high temp in Miami be above 84°?", category="KXHIGHMIA")
            current_ts = datetime.now(timezone.utc).isoformat()
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.84,
                "confidence": 0.76,
                "source_timestamp": current_ts,
                "question_side": "above",
                "data": {
                    "forecast_high": 86.2,
                    "forecast_low": 77.4,
                    "current_temp": 83.1,
                    "actual_temp_used": 86.2,
                    "predicted_temp": 86.2,
                    "threshold": 84,
                    "agreement": 0.91,
                    "sources": ["nws", "open-meteo"],
                },
            }

            with patch.object(engine.live_feeds, "get_signal", return_value=weather_signal):
                engine.analyze_market(market)

            self.assertTrue(log_path.exists())
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["city_id"], "miami_fl")
            self.assertEqual(record["source_id"], "src_nws_mfl")
            self.assertEqual(record["market_type"], "high_temp")
            self.assertNotIn("predicted_prob", record.get("value", {}))
            self.assertEqual(record["value"]["sources"], ["nws", "open-meteo"])

    def test_repeat_analysis_does_not_spam_identical_weather_observations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "weather_observations.jsonl"
            engine = self._build_engine(log_path)
            market = make_market("Will the high temp in Miami be above 84°?", category="KXHIGHMIA")
            first_ts = datetime.now(timezone.utc)
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.84,
                "confidence": 0.76,
                "question_side": "above",
                "data": {
                    "forecast_high": 86.2,
                    "forecast_low": 77.4,
                    "current_temp": 83.1,
                    "actual_temp_used": 86.2,
                    "predicted_temp": 86.2,
                    "threshold": 84,
                    "agreement": 0.91,
                    "sources": ["open-meteo", "nws"],
                },
            }

            with patch.object(engine.live_feeds, "get_signal", side_effect=[
                {**weather_signal, "source_timestamp": first_ts.isoformat()},
                {**weather_signal, "source_timestamp": (first_ts + timedelta(minutes=10)).isoformat()},
            ]):
                engine.analyze_market(market)
                engine.analyze_market(market)

            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_unknown_city_market_skips_observation_logging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "weather_observations.jsonl"
            engine = self._build_engine(log_path)
            market = make_market(
                "Will the high temp in Washington be above 84°?",
                market_id="mkt-dc",
                category="KXHIGHTDC",
            )
            current_ts = datetime.now(timezone.utc).isoformat()
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.84,
                "confidence": 0.76,
                "source_timestamp": current_ts,
                "question_side": "above",
                "data": {
                    "forecast_high": 86.2,
                    "current_temp": 83.1,
                    "actual_temp_used": 86.2,
                    "threshold": 84,
                },
            }

            with patch.object(engine.live_feeds, "get_signal", return_value=weather_signal):
                engine.analyze_market(market)

            self.assertFalse(log_path.exists())


if __name__ == "__main__":
    unittest.main()
