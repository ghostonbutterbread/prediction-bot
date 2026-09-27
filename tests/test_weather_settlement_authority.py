"""Synthetic decision-time rule text; not historical rule capture or resolved weather."""
import unittest
from datetime import datetime, timezone

from bot.exchanges.kalshi import _market_from_raw
from bot.feeds.live_data import LiveFeedAggregator
from bot.feeds.weather_pro import MultiSourceForecast, ProWeatherEngine, WeatherSnapshot
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_v1
from bot.strategies.enhanced import EnhancedStrategyEngine
from test_replay_decision_input import _snapshot_row


TWC_RULE = ("The minimum temperature recorded at CLIPHX on September 24, 2026 "
            "will be less than 73°F according to The Weather Company.")
NWS_RULE = ("The minimum temperature in Phoenix on August 1, 2026 will be less "
            "than 85°F according to the NWS Climatological Report (Daily).")


def _market(ticker, rule):
    return _market_from_raw({
        "ticker": ticker, "title": "Will the low temperature in Phoenix be below 73° on Sep 24, 2026?"
        if "SEP" in ticker else "Will the low temperature in Phoenix be below 85° on Aug 1, 2026?",
        "rules_primary": rule, "series_ticker": "KXLOWTPHX", "yes_ask_dollars": "0.50",
    })


class WeatherSettlementAuthorityTests(unittest.TestCase):
    def test_market_rules_do_not_change_forecast_value_confidence_or_driver(self):
        engine = ProWeatherEngine()
        now = datetime.now(timezone.utc)
        def source(name, low):
            return WeatherSnapshot(city="phoenix", source=name, high_temp_f=100,
                                   low_temp_f=low, current_temp_f=85, fetched_at=now,
                                   forecast_hours_ahead=2, confidence=0.85)
        engine.nws.get_forecast = lambda *_args, **_kwargs: source("nws", 72)
        engine.nws.get_station_observation = lambda *_args: None
        engine.open_meteo.get_forecast = lambda *_args, **_kwargs: source("open-meteo", 70)
        try:
            signals = [engine.score_temperature_market_with_context(
                _market("KXLOWTPHX-26SEP24-T73", rule).question, 0.5,
                category="KXLOWTPHX-26SEP24-T73", market_metadata={"rules_primary": rule},
            ) for rule in (TWC_RULE, NWS_RULE)]
            self.assertEqual([s["data"]["settlement_source"] for s in signals], ["the_weather_company", "nws"])
            self.assertEqual([s["data"]["predicted_temp"] for s in signals], [72, 72])
            self.assertEqual(signals[0]["confidence"], signals[1]["confidence"])
            self.assertEqual([s["data"]["forecast_driver"] for s in signals], ["nws", "nws"])
            self.assertEqual([{r["source_name"]: r["weight"] for r in s["data"]["source_details"] if "weight" in r}
                              for s in signals], [{"nws": 1.0, "open-meteo": 0.0}] * 2)
        finally:
            engine.close()

    def test_lab_legacy_signal_does_not_elevate_nws_source_to_rule_authority(self):
        lab = PredictionLab.__new__(PredictionLab)
        market = _market("KXLOWTPHX-26SEP24-T73", "")
        artifact = {"strategy_signal": {"signal_details": {"live": {
            "signal_type": "weather", "confidence": 0.82, "predicted_prob": 0.75,
            "data": {"forecast_low": 72, "sources": ["nws"], "settlement_source": "nws"},
        }}}}
        snapshot = lab._build_weather_source_snapshot(artifact, market)
        self.assertEqual(snapshot["settlement_source"], "unknown")
        self.assertFalse(snapshot.get("settlement_authority"))

    def test_lab_does_not_infer_equal_forecast_weights_from_settlement_metadata(self):
        rows = PredictionLab._weather_snapshot_sources(
            {"sources": ["nws", "open-meteo"], "settlement_source": "the_weather_company"},
            settlement_source="the_weather_company", market_date="2026-09-24",
            as_of="2026-09-23T12:00:00Z", station_resolution={}, source_agreement=None,
        )
        self.assertEqual([row.get("settlement_source") for row in rows], ["the_weather_company"] * 2)
        self.assertTrue(all("weight" not in row and "contribution" not in row for row in rows))

    def test_legacy_settlement_primary_role_is_only_a_forecast_contribution(self):
        rows = PredictionLab._weather_snapshot_sources(
            {"source_details": [
                {"source_name": "nws", "role": "settlement_primary", "weight": 1.0},
                {"source_name": "open-meteo", "role": "cross_validation", "weight": 0.0},
            ]}, settlement_source="unknown", market_date="2026-09-24",
            as_of="2026-09-23T12:00:00Z", station_resolution={}, source_agreement=None,
        )
        self.assertEqual(rows[0]["role"], "forecast_primary")
        self.assertEqual(rows[0]["settlement_source"], "unknown")
        self.assertEqual(rows[0]["weight"], 1.0)

    def test_recorded_primary_weight_without_role_remains_forecast_primary(self):
        rows = PredictionLab._weather_snapshot_sources(
            {"source_details": [
                {"source_name": "nws", "weight": 1.0},
                {"source_name": "open-meteo"},
            ]}, settlement_source="unknown", market_date="2026-09-24",
            as_of="2026-09-23T12:00:00Z", station_resolution={}, source_agreement=None,
        )
        self.assertEqual([(row["source_name"], row["role"], row.get("weight")) for row in rows],
                         [("nws", "forecast_primary", 1.0), ("open-meteo", "cross_validation", 0.0)])

    def test_recorded_primary_weight_overrides_stale_cross_validation_role(self):
        rows = PredictionLab._weather_snapshot_sources(
            {"source_details": [
                {"source_name": "nws", "role": "cross_validation", "weight": 1.0},
                {"source_name": "open-meteo", "role": "cross_validation", "weight": 0.0},
            ]}, settlement_source="the_weather_company", market_date="2026-09-24",
            as_of="2026-09-23T12:00:00Z", station_resolution={}, source_agreement=None,
        )
        self.assertEqual([(row["role"], row["weight"]) for row in rows],
                         [("forecast_primary", 1.0), ("cross_validation", 0.0)])

    def test_recorded_primary_weight_recovers_source_and_name_aliases(self):
        for alias in ("source", "name"):
            with self.subTest(alias=alias):
                rows = PredictionLab._weather_snapshot_sources(
                    {"source_details": [
                        {alias: "nws", "weight": 1.0},
                        {alias: "open-meteo"},
                    ]}, settlement_source="the_weather_company", market_date="2026-09-24",
                    as_of="2026-09-23T12:00:00Z", station_resolution={}, source_agreement=None,
                )
                self.assertEqual([(row["source_name"], row["role"], row.get("weight")) for row in rows],
                                 [("nws", "forecast_primary", 1.0), ("open-meteo", "cross_validation", 0.0)])

    def test_paper_label_diagnostics_do_not_substitute_settlement_provider_or_night_proxy_for_daily_actual(self):
        from bot.paper_shadow_lanes import _label_target, _label_source_name, _label_source_classification

        self.assertEqual(_label_target({}, {}, {}, {"settlement_source": "the_weather_company"}), "unknown")
        self.assertEqual(_label_source_name({"settlement_source": "nws"}), "unknown")
        self.assertEqual(_label_source_classification(
            "nws_daily_observed_archive", settlement_source="the_weather_company"), "independent")
        self.assertEqual(_label_source_classification(
            "nws_daily_observed_archive", settlement_source="nws"), "settlement_derived")
        self.assertNotEqual(_label_source_classification(
            "nws_forecast_night_low_proxy", settlement_source="the_weather_company"), "independent")

    def test_nws_daily_rule_positive_control_and_unknown_conflict_do_not_invent_nws(self):
        from bot.feeds.weather_pro import _settlement_authority

        nws_market = _market("KXLOWTPHX-26AUG01-T85", NWS_RULE)
        self.assertEqual(_settlement_authority(nws_market.metadata), {
            "source": "nws", "status": "identified", "evidence_field": "rules_primary",
            "evidence_origin": "kalshi_market_metadata",
        })
        for metadata, status in (
            ({}, "unknown_rules"),
            ({"rules_primary": "The recorded minimum is less than 73°F."}, "unknown_rules"),
            ({"rules_primary": TWC_RULE, "rules_secondary": NWS_RULE}, "conflicting_rules"),
            ({"rules_primary": NWS_RULE, "rules_secondary": TWC_RULE}, "conflicting_rules"),
            ({"rules_primary": "The minimum resolves according to an unlisted provider.",
              "rules_secondary": NWS_RULE}, "conflicting_rules"),
            ({"rules_primary": "NWS forecasts are nonbinding; the market resolves according to an unlisted provider."}, "unknown_rules"),
        ):
            authority = _settlement_authority(metadata)
            self.assertEqual(authority["source"], "unknown")
            self.assertEqual(authority["status"], status)
        self.assertEqual(_settlement_authority({
            "rules_primary": "NWS forecasts are only indicative. " + TWC_RULE,
        })["source"], "the_weather_company")

    def test_negated_provider_attribution_does_not_claim_settlement_authority(self):
        from bot.feeds.weather_pro import _settlement_authority

        for text in (
            "The minimum is not determined according to The Weather Company; an independent daily report determines resolution.",
            "The daily low is not reported by NWS; an independent report determines resolution.",
            "The minimum will not be determined according to The Weather Company; an independent daily report determines resolution.",
            "The daily low will not be reported by NWS; an independent report determines resolution.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_settlement_authority({"rules_primary": text})["source"], "unknown")

    def test_twc_market_rules_cross_exchange_signal_snapshot_and_sealed_input_without_rerouting_forecast(self):
        market = _market("KXLOWTPHX-26SEP24-T73", TWC_RULE)
        self.assertEqual(market.metadata["rules_primary"], TWC_RULE)
        aggregator = LiveFeedAggregator()
        now = datetime.now(timezone.utc)
        aggregator.weather.get_forecast = lambda *_args, **_kwargs: MultiSourceForecast(
            city="phoenix", high_temp_f=100, low_temp_f=72, current_temp_f=85,
            sources_used=["nws", "open-meteo"], confidence=0.82, fetched_at=now,
            source_agreement=0.9, details={
                "individual_lows": {"nws": 72, "open-meteo": 70},
                "individual_highs": {"nws": 100, "open-meteo": 98},
                "forecast_driver": "nws", "source_snapshots": [],
            },
        )
        strategy = EnhancedStrategyEngine.__new__(EnhancedStrategyEngine)
        strategy.live_feeds = aggregator
        try:
            signal = strategy._live_data_signal(market)
            self.assertIsNotNone(signal)
            self.assertEqual(signal["data"]["settlement_source"], "the_weather_company")
            self.assertEqual(signal["data"]["settlement_authority"]["evidence_field"], "rules_primary")
            self.assertEqual(signal["data"]["forecast_driver"], "nws")
            self.assertEqual(signal["data"]["predicted_temp"], 72)
            self.assertEqual([(s["source_name"], s["role"], s["weight"]) for s in signal["data"]["source_details"]],
                             [("nws", "forecast_primary", 1.0), ("open-meteo", "cross_validation", 0.0)])
            lab = PredictionLab.__new__(PredictionLab)
            artifact = {"strategy_signal": {"signal_details": {"live": signal}}, "strategy_trace": {}}
            snapshot = lab._build_weather_source_snapshot(artifact, market)
            self.assertEqual(snapshot["settlement_source"], "the_weather_company")
            self.assertEqual(snapshot["settlement_authority"], signal["data"]["settlement_authority"])
            self.assertEqual(snapshot["forecast_driver"], "nws")
            self.assertEqual(snapshot["sources"][0]["role"], "forecast_primary")
            self.assertEqual(snapshot["sources"][0]["weight"], 1.0)
            self.assertEqual(snapshot["sources"][1]["weight"], 0.0)
            self.assertEqual(snapshot["sources"][0]["settlement_authority"], signal["data"]["settlement_authority"])
            row = _snapshot_row()
            row["market_id"] = market.id
            row["question"] = market.question
            row["decision_artifact"]["source_context"]["data"]["market_metadata"] = market.metadata
            row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"] = snapshot
            sealed = build_replay_decision_input_v1(row)
            self.assertTrue(sealed.ok, sealed.errors)
            captured = sealed.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]
            self.assertEqual(captured["settlement_authority"], snapshot["settlement_authority"])
            self.assertEqual(captured["forecast_driver"], "nws")
            self.assertEqual(captured["sources"][0]["settlement_authority"], snapshot["settlement_authority"])
            self.assertEqual(sealed.record["source_inputs"]["source_context"]["data"]["market_metadata"]["rules_primary"], TWC_RULE)
            self.assertTrue(verify_replay_decision_input_v1(sealed.record, sealed.canonical_input_json))
        finally:
            aggregator.close()
