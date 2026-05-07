import unittest

from bot.shared_core.weather_risk import build_weather_source_confidence_evidence
from bot.weather.station_mapping import parse_weather_market_city_code, resolve_weather_station
from bot.weather_market_risk import (
    apply_weather_size_limits,
    assess_weather_market_risk,
    classify_weather_market,
)


class WeatherMarketRiskTests(unittest.TestCase):
    def test_parses_kalshi_weather_city_codes_from_tickers_and_categories(self):
        self.assertEqual(parse_weather_market_city_code("KXHIGHMIA-26APR26-B82.5"), "MIA")
        self.assertEqual(parse_weather_market_city_code("KXLOWTOKC-26APR27-T67"), "OKC")
        self.assertEqual(parse_weather_market_city_code("KXHIGHTATL"), "ATL")
        self.assertEqual(parse_weather_market_city_code("KXLOWTDEN-26APR21-B49.5"), "DEN")
        self.assertEqual(parse_weather_market_city_code("KXHIGHNY-26APR22-B59.5"), "NY")
        self.assertIsNone(parse_weather_market_city_code("KXTEST"))

    def test_resolves_static_exact_and_inferred_station_mappings_for_baseline_codes(self):
        exact_cases = {
            "MIA": ("miami_fl", "KMIA", "MIA"),
            "NY": ("new_york_ny", "KNYC", "NYC"),
            "ATL": ("atlanta_ga", "KATL", "ATL"),
            "AUS": ("austin_tx", "KAUS", "AUS"),
            "DEN": ("denver_co", "KDEN", "DEN"),
            "OKC": ("oklahoma_city_ok", "KOKC", "OKC"),
            "PHX": ("phoenix_az", "KPHX", "PHX"),
            "SFO": ("san_francisco_ca", "KSFO", "SFO"),
            "LAX": ("los_angeles_ca", "KLAX", "LAX"),
            "SATX": ("san_antonio_tx", "KSAT", "SAT"),
        }
        for city_code, expected in exact_cases.items():
            with self.subTest(city_code=city_code):
                resolution = resolve_weather_station({"market_id": f"KXHIGH{city_code}-26APR26-B80.5"})
                self.assertEqual(resolution.mapping, "exact")
                self.assertEqual((resolution.city_id, resolution.station_id, resolution.station_cli), expected)

        for city_code, city_id in {"DAL": "dallas_tx", "HOU": "houston_tx", "CHI": "chicago_il"}.items():
            with self.subTest(city_code=city_code):
                resolution = resolve_weather_station({"market_id": f"KXHIGH{city_code}-26APR26-B80.5"})
                self.assertEqual(resolution.mapping, "inferred")
                self.assertEqual(resolution.city_id, city_id)
                self.assertIsNone(resolution.station_id)

    def test_shared_source_confidence_helper_infers_station_mapping_levels(self):
        exact = build_weather_source_confidence_evidence(
            {
                "market_id": "KXHIGHTSEA-26APR26-T64",
                "question": "Will the maximum temperature be <64° on Apr 26?",
                "station_id": "KSEA",
                "confidence": 0.96,
                "signals": {"live": 0.38, "price": 0.37},
            }
        )
        inferred = build_weather_source_confidence_evidence(
            {
                "market_id": "KXHIGHTDAL-26APR23-B84.5",
                "question": "Will the maximum temperature be 84-85° on Apr 23?",
                "confidence": 0.91,
                "signals": {"live": 0.33, "price": 0.34},
            }
        )
        unknown = build_weather_source_confidence_evidence(
            {
                "market_id": "TEST-1",
                "question": "Will unrelated event happen?",
                "confidence": 0.75,
                "signals": {"price": 0.55, "news": 0.35},
            }
        )

        self.assertEqual(exact["weather_station_mapping"], "exact")
        self.assertEqual(exact["weather_station_resolution"]["station_id"], "KSEA")
        self.assertGreaterEqual(exact["weather_confidence_score"], 0.9)
        self.assertGreaterEqual(exact["source_agreement_score"], 0.95)
        self.assertEqual(inferred["weather_station_mapping"], "inferred")
        self.assertEqual(inferred["weather_station_resolution"]["city_code"], "DAL")
        self.assertIsNone(inferred["weather_station_resolution"]["station_id"])
        self.assertEqual(unknown["weather_station_mapping"], "unknown")

    def test_shared_source_confidence_helper_upgrades_known_ticker_codes_to_exact_mapping(self):
        evidence = build_weather_source_confidence_evidence(
            {
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "confidence": 0.91,
                "signals": {"live": 0.33, "price": 0.34},
            }
        )

        self.assertEqual(evidence["weather_station_mapping"], "exact")
        self.assertEqual(evidence["weather_station_resolution"]["city_code"], "MIA")
        self.assertEqual(evidence["weather_station_resolution"]["station_id"], "KMIA")

    def test_classifies_bucket_and_tail_markets(self):
        self.assertEqual(
            classify_weather_market("Will the high temp in Miami be 82-83° on Apr 26?", "KXHIGHMIA-26APR26-B82.5"),
            "bucket",
        )
        self.assertEqual(
            classify_weather_market("Will the high temp in NYC be <51° on Apr 26?", "KXHIGHNY-26APR26-T51"),
            "tail_low",
        )
        self.assertEqual(
            classify_weather_market("Will the maximum temperature be >83° on Apr 26?", "KXHIGHTPHX-26APR26-T83"),
            "tail_high",
        )
        self.assertEqual(classify_weather_market("Will unrelated event happen?", "KXTEST"), "unknown")

    def test_narrow_bucket_with_unknown_volume_gets_stricter_size_limits(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
            },
            entry_price=0.22,
            win_probability=0.55,
        )

        self.assertEqual(assessment.shape, "bucket")
        self.assertFalse(assessment.volume_known)
        self.assertIn("volume_unknown", assessment.flags)
        self.assertLess(assessment.size_multiplier, 1.0)
        self.assertEqual(assessment.max_position_usd, 10.0)
        self.assertEqual(apply_weather_size_limits(100.0, assessment, current_balance=1000.0), 10.0)

    def test_suspicious_hidden_gem_is_allowed_with_penalty(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-T90",
                "question": "Will the high temp in Miami be >90° on Apr 26?",
                "market_volume": 900,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.82,
                "source_agreement_score": 0.72,
            },
            entry_price=0.03,
            win_probability=0.36,  # 12x price
        )

        self.assertEqual(assessment.hidden_gem_tier, "suspicious")
        self.assertFalse(assessment.should_skip)
        self.assertIn("extreme_disagreement_suspicious", assessment.flags)
        self.assertLess(assessment.size_multiplier, 0.5)

    def test_normal_weather_hidden_gem_skips_without_strong_weather_evidence(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-T90",
                "question": "Will the high temp in Miami be >90° on Apr 26?",
                "market_volume": 900,
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(assessment.hidden_gem_tier, "normal")
        self.assertEqual(assessment.reason_code, "weather_hidden_gem_without_strong_evidence")

    def test_bucket_hidden_gem_skips_without_distribution_probability(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 900,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            },
            entry_price=0.03,
            win_probability=0.15,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(assessment.hidden_gem_tier, "normal")
        self.assertEqual(assessment.reason_code, "weather_bucket_hidden_gem_missing_distribution_probability")

    def test_bucket_hidden_gem_skips_when_distribution_probability_below_entry_buffer(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 900,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
                "distribution_probability": 0.07,
            },
            entry_price=0.03,
            win_probability=0.15,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(
            assessment.reason_code,
            "weather_bucket_hidden_gem_distribution_probability_below_entry_plus_buffer",
        )

    def test_bucket_hidden_gem_skips_when_distribution_probability_below_price_multiple(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 900,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
                "distribution_probability": 0.12,
            },
            entry_price=0.05,
            win_probability=0.20,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(
            assessment.reason_code,
            "weather_bucket_hidden_gem_distribution_probability_below_multiple",
        )

    def test_bucket_hidden_gem_allows_strong_distribution_probability(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 900,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
                "distribution_probability": 0.24,
            },
            entry_price=0.03,
            win_probability=0.15,
        )

        self.assertFalse(assessment.should_skip)
        self.assertEqual(assessment.hidden_gem_tier, "normal")

    def test_bucket_hidden_gem_skips_when_source_or_station_quality_is_weak(self):
        base_signal = {
            "market_id": "KXHIGHMIA-26APR26-B82.5",
            "question": "Will the high temp in Miami be 82-83° on Apr 26?",
            "market_volume": 900,
            "weather_confidence_score": 0.94,
            "distribution_probability": 0.24,
        }
        cases = [
            {**base_signal, "weather_station_mapping": "inferred", "source_agreement_score": 0.9},
            {**base_signal, "weather_station_mapping": "exact", "source_agreement_score": 0.6},
        ]

        for signal in cases:
            with self.subTest(signal=signal):
                assessment = assess_weather_market_risk(
                    signal,
                    entry_price=0.03,
                    win_probability=0.15,
                )

                self.assertTrue(assessment.should_skip)
                self.assertEqual(
                    assessment.reason_code,
                    "weather_bucket_hidden_gem_source_station_quality_below_minimum",
                )

    def test_tail_hidden_gem_skips_when_live_probability_rejects_candidate_side(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHTSEA-26APR26-T64",
                "question": "Will the maximum temperature be <64° on Apr 26?",
                "market_volume": 900,
                "candidate_direction": "BUY_YES",
                "source_agreement_score": 0.86,
                "signal_details": {
                    "live": {
                        "signal_type": "weather",
                        "predicted_prob": 0.12,
                        "confidence": 0.62,
                    }
                },
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(assessment.reason_code, "weather_tail_hidden_gem_live_probability_mismatch")
        self.assertIn("tail_directional_mismatch", assessment.flags)
        self.assertEqual(assessment.tail_probability_source, "bridge")

    def test_tail_hidden_gem_directional_mismatch_is_symmetric_for_buy_no(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHATL-26APR26-T90",
                "question": "Will Atlanta high temperature be above 90 degrees?",
                "market_volume": 900,
                "candidate_direction": "BUY_NO",
                "source_agreement_score": 0.86,
                "signal_details": {"live": {"signal_type": "weather", "predicted_prob": 0.91, "confidence": 0.62}},
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(assessment.reason_code, "weather_tail_hidden_gem_live_probability_mismatch")
        self.assertAlmostEqual(assessment.tail_candidate_probability, 0.09)

    def test_tail_hidden_gem_does_not_skip_on_low_confidence_live_probability(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHATL-26APR26-T90",
                "question": "Will Atlanta high temperature be above 90 degrees?",
                "market_volume": 900,
                "candidate_direction": "BUY_YES",
                "source_agreement_score": 0.86,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.82,
                "signal_details": {"live": {"signal_type": "weather", "predicted_prob": 0.12, "confidence": 0.40}},
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertFalse(assessment.should_skip)
        self.assertEqual(assessment.hidden_gem_tier, "normal")
        self.assertEqual(assessment.tail_probability_source, "bridge")
        self.assertEqual(
            assessment.tail_probability_reason_code,
            "weather_tail_hidden_gem_bridge_insufficient_evidence",
        )

    def test_tail_hidden_gem_uses_distribution_probability_before_live_bridge(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHATL-26APR26-T90",
                "question": "Will Atlanta high temperature be above 90 degrees?",
                "market_volume": 900,
                "candidate_direction": "BUY_YES",
                "source_agreement_score": 0.86,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.82,
                "distribution_probability": 0.24,
                "signal_details": {"live": {"signal_type": "weather", "predicted_prob": 0.05, "confidence": 0.92}},
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertFalse(assessment.should_skip)
        self.assertEqual(assessment.hidden_gem_tier, "normal")
        self.assertEqual(assessment.tail_probability_source, "distribution")
        self.assertEqual(
            assessment.tail_probability_reason_code,
            "weather_tail_hidden_gem_distribution_probability_passed",
        )
        self.assertIn("tail_distribution_probability_used", assessment.flags)

    def test_tail_hidden_gem_skips_when_distribution_probability_rejects_candidate_side(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHATL-26APR26-T90",
                "question": "Will Atlanta high temperature be above 90 degrees?",
                "market_volume": 900,
                "candidate_direction": "BUY_YES",
                "source_agreement_score": 0.86,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.82,
                "distribution_probability": 0.12,
                "signal_details": {"live": {"signal_type": "weather", "predicted_prob": 0.75, "confidence": 0.92}},
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(
            assessment.reason_code,
            "weather_tail_hidden_gem_distribution_probability_below_threshold",
        )
        self.assertEqual(assessment.tail_probability_source, "distribution")

    def test_tail_hidden_gem_does_not_invert_candidate_distribution_for_buy_no(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHATL-26APR26-T90",
                "question": "Will Atlanta high temperature be above 90 degrees?",
                "market_volume": 900,
                "candidate_direction": "BUY_NO",
                "source_agreement_score": 0.86,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.82,
                "distribution_probability": 0.12,
                "signal_details": {"live": {"signal_type": "weather", "predicted_prob": 0.05, "confidence": 0.92}},
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(assessment.tail_probability_source, "distribution")
        self.assertAlmostEqual(assessment.tail_candidate_probability, 0.12)
        self.assertEqual(
            assessment.reason_code,
            "weather_tail_hidden_gem_distribution_probability_below_threshold",
        )

    def test_tail_hidden_gem_threshold_probability_keeps_bridge_behavior(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHATL-26APR26-T90",
                "question": "Will Atlanta high temperature be above 90 degrees?",
                "market_volume": 900,
                "candidate_direction": "BUY_YES",
                "source_agreement_score": 0.86,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.82,
                "threshold_probability": 0.24,
                "signal_details": {"live": {"signal_type": "weather", "predicted_prob": 0.05, "confidence": 0.92}},
            },
            entry_price=0.04,
            win_probability=0.16,
        )

        self.assertTrue(assessment.should_skip)
        self.assertEqual(assessment.tail_probability_source, "bridge")
        self.assertEqual(assessment.reason_code, "weather_tail_hidden_gem_live_probability_mismatch")

    def test_exceptional_hidden_gem_skips_without_perfect_evidence(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXHIGHTSEA-26APR26-T64",
                "question": "Will the maximum temperature be <64° on Apr 26?",
                "market_volume": 700,
                "weather_station_mapping": "inferred",
                "weather_confidence_score": 0.8,
                "source_agreement_score": 0.7,
                "distribution_probability": 0.22,
            },
            entry_price=0.03,
            win_probability=0.48,  # 16x price
        )

        self.assertEqual(assessment.hidden_gem_tier, "exceptional")
        self.assertTrue(assessment.should_skip)
        self.assertFalse(assessment.evidence_perfect)
        self.assertEqual(assessment.reason_code, "weather_extreme_disagreement_without_perfect_evidence")

    def test_exceptional_hidden_gem_allowed_when_evidence_is_perfect(self):
        assessment = assess_weather_market_risk(
            {
                "market_id": "KXLOWTOKC-26APR27-T67",
                "question": "Will the minimum temperature be >67° on Apr 27?",
                "market_volume": 4500,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
                "distribution_probability": 0.28,
            },
            entry_price=0.02,
            win_probability=0.38,  # 19x price
        )

        self.assertEqual(assessment.hidden_gem_tier, "exceptional")
        self.assertFalse(assessment.should_skip)
        self.assertTrue(assessment.evidence_perfect)
        self.assertAlmostEqual(assessment.size_multiplier, 0.1)


if __name__ == "__main__":
    unittest.main()
