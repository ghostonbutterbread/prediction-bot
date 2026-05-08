import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from bot.config import load_config
from bot.strategy_policy import normalize_strategy_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


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

    def test_repo_beta_shadow_configs_normalize_active_shadow_policy(self):
        paper_path = REPO_ROOT / "config.paper_beta_shadow_weather.yaml"
        lab_path = REPO_ROOT / "config.prediction_lab_beta_shadow_weather.yaml"

        with patch.dict(os.environ, {}, clear=True):
            paper_config = load_config(paper_path)
            lab_config = load_config(lab_path)

        for config in (paper_config, lab_config):
            policy = config["strategy_policy_normalized"]
            self.assertEqual(policy["version"], "beta")
            self.assertEqual(policy["beta_mode"], "shadow")
            self.assertTrue(policy.is_active)
            self.assertTrue(policy.is_shadow)
            self.assertFalse(policy.is_enforce)
            self.assertTrue(policy.feature_enabled("weather_hidden_gem_evidence_card"))
            self.assertTrue(policy.feature_enabled("bucket_distribution_scoring"))
            self.assertTrue(policy.feature_enabled("hidden_gem_lane_gates"))
            self.assertTrue(policy.feature_enabled("lane_sizing_caps"))
            self.assertEqual(Path(config["data_dir"]), Path("data/beta_shadow/paper"))
            self.assertEqual(Path(config["log_dir"]), Path("data/beta_shadow/paper"))
            self.assertFalse(config["storage"]["logs"]["auto_prune"])
            include_paths = config["storage"]["logs"]["include_paths"]
            self.assertTrue(all(str(path).startswith("data/beta_shadow/") for path in include_paths))
            self.assertNotIn("data/paper_loop.log", include_paths)
            self.assertNotIn("logs/", include_paths)

        self.assertEqual(paper_config["trading"]["mode"], "paper")
        self.assertTrue(paper_config["trading"]["enabled"])
        self.assertEqual(lab_config["trading"]["mode"], "paper")
        self.assertFalse(lab_config["trading"]["enabled"])
        self.assertFalse(lab_config["trading_enabled"])
        self.assertTrue(lab_config["prediction_lab"]["observer_mode"])
        self.assertTrue(lab_config["prediction_lab"]["score_only"])
        self.assertEqual(lab_config["prediction_lab"]["experiment_id"], "weather-beta-shadow")


if __name__ == "__main__":
    unittest.main()
