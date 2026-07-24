import unittest
from pathlib import Path

from bot.config import load_config


ROOT = Path(__file__).resolve().parents[1]
COHORT_ROOT = "data/beta_shadow/forward_router_guard_comparison_TEMPLATE"


class SourceRouterGuardComparisonConfigTests(unittest.TestCase):
    def test_paper_profile_is_isolated_and_enables_only_direct_comparators(self):
        config = load_config(ROOT / "config.paper_source_router_guard_comparison.yaml")

        self.assertEqual(config["runtime"]["base_dir"], COHORT_ROOT)
        self.assertTrue(config["runtime"]["isolated"])
        self.assertEqual(
            config["shared_market"]["runtime_root"],
            f"{COHORT_ROOT}/shared_market_runtime",
        )
        self.assertEqual(
            config["paper_shadow_lanes"]["enabled_lanes"],
            ["control_stable", "shadow_source_router", "shadow_source_router_no_price_guard"],
        )
        self.assertTrue(config["paper_shadow_lanes"]["shadow_source_router"]["enabled"])
        guard = config["paper_shadow_lanes"]["shadow_source_router_no_price_guard"]
        self.assertTrue(guard["enabled"])
        self.assertEqual(guard["parameters"]["allowed_actions"], ["BUY_NO"])
        self.assertEqual(guard["parameters"]["allowed_entry_price_ranges"], [[0.60, 0.70], [0.80, 0.90]])
        self.assertEqual(config["resolution_feed"]["central_output_dir"], f"{COHORT_ROOT}/resolutions")

    def test_collector_profile_is_observer_only_and_uses_the_same_cohort_root(self):
        config = load_config(ROOT / "config.prediction_lab_source_router_guard_comparison.yaml")

        self.assertEqual(config["runtime"]["base_dir"], COHORT_ROOT)
        self.assertEqual(
            config["shared_market"]["runtime_root"],
            f"{COHORT_ROOT}/shared_market_runtime",
        )
        self.assertTrue(config["prediction_lab"]["observer_mode"])
        self.assertTrue(config["prediction_lab"]["score_only"])
        self.assertTrue(config["prediction_lab"]["collector_record_market_snapshots"])
        self.assertFalse(config["prediction_lab"]["collector_record_predictions"])


if __name__ == "__main__":
    unittest.main()
