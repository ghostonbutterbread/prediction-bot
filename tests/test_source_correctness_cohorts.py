import unittest

from bot.weather.source_correctness_cohorts import select_source_correctness_cohort


def candidate(
    decision_id: str,
    *,
    market_id: str,
    timestamp: str,
    shape: str = "tail",
    event_ticker: str | None = "KXHIGHMIA-26AUG01",
) -> dict:
    row = {
        "decision_id": decision_id,
        "lane_id": "source_router_candidate_v1",
        "market_id": market_id,
        "decision_timestamp": timestamp,
        "selected_source_id": "nws",
        "side": "YES",
        "city_id": "miami_fl",
        "market_kind": "high",
        "contract_shape": shape,
        "subcategory": shape,
        "question_side": "above",
    }
    if event_ticker is not None:
        row["event_ticker"] = event_ticker
        row["unit_id"] = event_ticker
        row["independence_quality"] = "event_ticker"
    else:
        row["unit_id"] = market_id
        row["independence_quality"] = "contract_only"
    return row


def correctness(decision_id: str, value: str = "correct") -> dict:
    return {"decision_id": decision_id, "source_selection_correctness": value}


class SourceCorrectnessCohortTests(unittest.TestCase):
    def test_snapshots_and_same_shape_contracts_collapse_to_one_oldest_representative(self):
        decisions = [
            candidate("new-snapshot", market_id="KX-M1", timestamp="2026-08-01T11:00:00Z"),
            candidate("old-snapshot", market_id="KX-M1", timestamp="2026-08-01T10:00:00Z"),
            candidate("same-event-other-contract", market_id="KX-M2", timestamp="2026-08-01T10:30:00Z"),
        ]
        report = select_source_correctness_cohort(decisions, [correctness(row["decision_id"]) for row in decisions], per_shape_target=30)

        tail = report["per_shape"][0]
        self.assertEqual(tail["available_unique_units"], 1)
        self.assertEqual(tail["selected_count"], 1)
        self.assertEqual(tail["duplicate_or_reobservation_exclusions"], 2)
        self.assertEqual(tail["selected_representatives"][0]["decision_id"], "old-snapshot")
        self.assertEqual(tail["shortfall"], 29)

    def test_same_event_can_represent_each_shape_but_global_report_warns_about_shared_events(self):
        decisions = [
            candidate("tail", market_id="KX-TAIL", timestamp="2026-08-01T10:00:00Z", shape="tail"),
            candidate("range", market_id="KX-RANGE", timestamp="2026-08-01T10:01:00Z", shape="range"),
        ]
        report = select_source_correctness_cohort(decisions, [correctness("tail"), correctness("range", "incorrect")], per_shape_target=1)

        self.assertEqual([row["contract_shape"] for row in report["per_shape"]], ["range", "tail"])
        self.assertEqual(report["aggregate"]["selected_global_unique_event_count"], 1)
        self.assertEqual(report["aggregate"]["selected_count"], 2)
        self.assertEqual(report["aggregate"]["cross_shape_shared_event_count"], 1)
        self.assertTrue(report["aggregate"]["cross_shape_shared_event_warning"])

    def test_exact_thirty_quota_and_chronology_are_deterministic_and_outcome_blind(self):
        decisions = [
            candidate("thirty-first", market_id="KX-31", timestamp="2026-09-01T00:00:00Z", event_ticker="E31"),
            candidate("first-b", market_id="KX-1B", timestamp="2026-08-01T00:00:00Z", event_ticker="E1B"),
            candidate("first-a", market_id="KX-1A", timestamp="2026-08-01T00:00:00Z", event_ticker="E1A"),
        ] + [
            candidate(f"event-{index:02}", market_id=f"KX-{index:02}", timestamp=f"2026-08-{index:02}T00:00:00Z", event_ticker=f"E{index:02}")
            for index in range(2, 30)
        ]
        observations = [correctness(row["decision_id"], "correct" if index % 2 else "incorrect") for index, row in enumerate(decisions)]
        report = select_source_correctness_cohort(decisions, observations, per_shape_target=30)
        flipped = select_source_correctness_cohort(
            decisions,
            [correctness(row["decision_id"], "incorrect" if row["source_selection_correctness"] == "correct" else "correct") for row in observations],
            per_shape_target=30,
        )

        selected = report["per_shape"][0]["selected_representatives"]
        self.assertEqual(len(selected), 30)
        self.assertEqual([row["decision_id"] for row in selected[:3]], ["first-a", "first-b", "event-02"])
        self.assertEqual(report["per_shape"][0]["shortfall"], 0)
        self.assertEqual(report["per_shape"][0]["quota_exclusions"], 1)
        self.assertEqual(
            [row["decision_id"] for row in flipped["per_shape"][0]["selected_representatives"]],
            [row["decision_id"] for row in selected],
        )

    def test_event_ticker_absence_is_explicit_contract_only_fallback(self):
        decision = candidate("contract-only", market_id="KX-ONLY", timestamp="2026-08-01T00:00:00Z", event_ticker=None)
        report = select_source_correctness_cohort([decision], [correctness("contract-only")], per_shape_target=1)

        representative = report["per_shape"][0]["selected_representatives"][0]
        self.assertEqual(representative["unit_id"], "KX-ONLY")
        self.assertEqual(representative["independence_quality"], "contract_only")
        self.assertEqual(report["aggregate"]["selected_global_unique_event_count"], 0)
        self.assertEqual(report["aggregate"]["selected_contract_only_unit_count"], 1)


if __name__ == "__main__":
    unittest.main()
