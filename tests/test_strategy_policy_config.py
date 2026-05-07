import tempfile
import unittest
from pathlib import Path

from bot.config import load_config
from bot.strategy_policy import normalize_strategy_policy


class StrategyPolicyConfigTests(unittest.TestCase):
    def test_missing_config_defaults_to_stable_off(self):
        policy = normalize_strategy_policy(None)

        self.assertEqual(policy["version"], "stable")
        self.assertEqual(policy["beta_mode"], "off")
        self.assertFalse(policy.is_beta)
        self.assertFalse(policy.is_configured_beta)
        self.assertFalse(policy.is_active)
        self.assertFalse(policy.is_shadow)
        self.assertFalse(policy.is_enforce)
        self.assertFalse(policy.feature_enabled("weather_hidden_gem_evidence_card"))
        self.assertFalse(policy.feature_enabled("bucket_distribution_scoring"))
        self.assertFalse(policy.feature_enabled("hidden_gem_lane_gates"))
        self.assertFalse(policy.feature_enabled("lane_sizing_caps"))

    def test_beta_shadow_parses_correctly(self):
        policy = normalize_strategy_policy(
            {
                "version": "beta",
                "beta": {
                    "mode": "shadow",
                    "features": {
                        "weather_hidden_gem_evidence_card": True,
                        "bucket_distribution_scoring": "on",
                    },
                },
            }
        )

        self.assertEqual(policy["version"], "beta")
        self.assertEqual(policy["beta_mode"], "shadow")
        self.assertTrue(policy.is_beta)
        self.assertTrue(policy.is_configured_beta)
        self.assertTrue(policy.is_active)
        self.assertTrue(policy.is_shadow)
        self.assertFalse(policy.is_enforce)
        self.assertTrue(policy.feature_enabled("weather_hidden_gem_evidence_card"))
        self.assertTrue(policy.feature_enabled("bucket_distribution_scoring"))
        self.assertFalse(policy.feature_enabled("hidden_gem_lane_gates"))
        self.assertFalse(policy.feature_enabled("lane_sizing_caps"))

    def test_beta_enforce_parses_correctly(self):
        policy = normalize_strategy_policy(
            {
                "version": "beta",
                "beta_mode": "enforce",
                "beta": {
                    "features": {
                        "hidden_gem_lane_gates": True,
                    },
                },
            }
        )

        self.assertEqual(policy["version"], "beta")
        self.assertEqual(policy["beta_mode"], "enforce")
        self.assertTrue(policy.is_beta)
        self.assertTrue(policy.is_configured_beta)
        self.assertTrue(policy.is_active)
        self.assertFalse(policy.is_shadow)
        self.assertTrue(policy.is_enforce)
        self.assertTrue(policy.feature_enabled("hidden_gem_lane_gates"))
        self.assertFalse(policy.feature_enabled("weather_hidden_gem_evidence_card"))

    def test_beta_mode_off_keeps_true_features_inactive(self):
        policy = normalize_strategy_policy(
            {
                "version": "beta",
                "beta": {
                    "mode": "off",
                    "features": {"weather_hidden_gem_evidence_card": True},
                },
            }
        )

        self.assertTrue(policy.is_configured_beta)
        self.assertFalse(policy.is_active)
        self.assertTrue(policy["configured_features"]["weather_hidden_gem_evidence_card"])
        self.assertFalse(policy["features"]["weather_hidden_gem_evidence_card"])
        self.assertFalse(policy.feature_enabled("weather_hidden_gem_evidence_card"))

    def test_malformed_beta_section_fails_closed(self):
        policy = normalize_strategy_policy({"version": "beta", "beta": "shadow"})

        self.assertEqual(policy["version"], "stable")
        self.assertEqual(policy["beta_mode"], "off")
        self.assertFalse(policy.is_configured_beta)
        self.assertFalse(policy.is_active)
        self.assertFalse(policy.feature_enabled("weather_hidden_gem_evidence_card"))

    def test_unknown_feature_name_is_ignored(self):
        policy = normalize_strategy_policy(
            {
                "version": "beta",
                "beta": {
                    "mode": "shadow",
                    "features": {
                        "weather_hidden_gem_evidence_card": True,
                        "typo": True,
                    },
                },
            }
        )

        self.assertTrue(policy.feature_enabled("weather_hidden_gem_evidence_card"))
        self.assertFalse(policy.feature_enabled("typo"))
        self.assertNotIn("typo", policy["configured_features"])
        self.assertNotIn("typo", policy["features"])

    def test_stable_mode_keeps_true_features_inactive(self):
        policy = normalize_strategy_policy(
            {
                "version": "stable",
                "beta": {
                    "mode": "shadow",
                    "features": {"weather_hidden_gem_evidence_card": True},
                },
            }
        )

        self.assertFalse(policy.is_configured_beta)
        self.assertFalse(policy.is_active)
        self.assertEqual(policy["beta_mode"], "off")
        self.assertTrue(policy["configured_features"]["weather_hidden_gem_evidence_card"])
        self.assertFalse(policy["features"]["weather_hidden_gem_evidence_card"])
        self.assertFalse(policy.feature_enabled("weather_hidden_gem_evidence_card"))

    def test_invalid_version_or_mode_falls_back_safely(self):
        invalid_version = normalize_strategy_policy(
            {
                "version": "next",
                "beta": {
                    "mode": "shadow",
                    "features": {"weather_hidden_gem_evidence_card": True},
                },
            }
        )
        invalid_mode = normalize_strategy_policy({"version": "beta", "beta_mode": "trade"})

        for policy in (invalid_version, invalid_mode):
            self.assertEqual(policy["version"], "stable")
            self.assertEqual(policy["beta_mode"], "off")
            self.assertFalse(policy.is_beta)
            self.assertFalse(policy.is_configured_beta)
            self.assertFalse(policy.is_active)
            self.assertFalse(policy.is_shadow)
            self.assertFalse(policy.is_enforce)
            self.assertFalse(policy.feature_enabled("weather_hidden_gem_evidence_card"))
            self.assertFalse(policy["features"]["weather_hidden_gem_evidence_card"])

    def test_load_config_exposes_normalized_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
runtime:
  base_dir: runtime_data
strategy_policy:
  version: beta
  beta:
    mode: shadow
    features:
      weather_hidden_gem_evidence_card: true
prediction_lab:
  paused: true
"""
            )

            config = load_config(config_path)
            policy = config["strategy_policy_normalized"]

            self.assertTrue(config["prediction_lab"]["paused"])
            self.assertEqual(policy["version"], "beta")
            self.assertTrue(policy.is_shadow)
            self.assertTrue(policy.is_active)
            self.assertTrue(policy.feature_enabled("weather_hidden_gem_evidence_card"))


if __name__ == "__main__":
    unittest.main()
