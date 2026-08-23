import unittest

from bot.weather.strict_source_compatibility import summarize_strict_source_replay_compatibility


def _sealed_record(source: dict) -> dict:
    return {
        "snapshot_provenance": {"raw_row_sha256": "a" * 64},
        "source_inputs": {"source_context": {"data": {"weather_source_snapshot": {"sources": [source]}}}},
    }


class StrictSourceReplayCompatibilityTests(unittest.TestCase):
    def test_reports_source_field_coverage_and_named_missing_fields(self):
        complete = _sealed_record({
            "source_id": "nws", "source_as_of": "2026-08-01T08:00:00+00:00",
            "source_location_city": "Miami", "source_target_date": "2026-08-02",
            "target_mapping": {"source_timezone": "UTC-04:00"},
            "forecast_measurement_kind": "high", "contract_shape": "threshold", "question_side": "above",
        })
        missing_city = _sealed_record({
            "source_id": "nws", "source_as_of": "2026-08-01T08:00:00+00:00",
            "source_target_date": "2026-08-02", "target_mapping": {"source_timezone": "UTC-04:00"},
            "forecast_measurement_kind": "high", "contract_shape": "threshold", "question_side": "above",
        })

        report = summarize_strict_source_replay_compatibility([complete, missing_city])

        self.assertEqual(report["records_seen"], 2)
        self.assertEqual(report["source_rows_seen"], 2)
        self.assertEqual(report["strict_contract_complete"], 1)
        self.assertEqual(report["strict_contract_incomplete"], 1)
        self.assertEqual(report["missing_field_counts"]["source_location_city"], 1)

    def test_excludes_explicit_observation_and_unavailable_sources_from_strict_contract(self):
        report = summarize_strict_source_replay_compatibility([
            _sealed_record({"evidence_type": "observation", "source_id": "station"}),
            _sealed_record({"evidence_type": "forecast_unavailable", "source_name": "fallback"}),
        ])

        self.assertEqual(report["non_strict_source_rows"], 2)
        self.assertEqual(report["non_strict_source_observation_only"], 1)
        self.assertEqual(report["non_strict_source_forecast_unavailable"], 1)
        self.assertNotIn("strict_contract_incomplete", report)


if __name__ == "__main__":
    unittest.main()
