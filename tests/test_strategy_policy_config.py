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
        self.assertFalse(policy.feature_enabled("confidence_slow_profit"))
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

    def test_config_composition_merges_base_overlays_and_explicit_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            base_path = tmp_path / "stable.yaml"
            config_path = tmp_path / "shadow.yaml"
            base_path.write_text(
                """
openrouter:
  model: stable-model
runtime:
  base_dir: stable_data
strategy:
  min_edge: 0.04
  enable_news: true
"""
            )
            config_path.write_text(
                """
config_composition:
  base: stable.yaml
  overlays:
    - beta_shadow_observability_all
openrouter:
  model: explicit-model
strategy:
  enable_news: true
strategy_lanes:
  confidence_slow_profit:
    min_edge: 0.015
"""
            )

            with patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)

            policy = config["strategy_policy_normalized"]
            self.assertNotIn("config_composition", config)
            self.assertEqual(config["config_profile"]["base"], "stable.yaml")
            self.assertEqual(config["config_profile"]["overlays"], ["beta_shadow_observability_all"])
            self.assertEqual(config["openrouter"]["model"], "explicit-model")
            self.assertEqual(config["strategy"]["min_edge"], 0.04)
            self.assertTrue(config["strategy"]["enable_news"])
            self.assertTrue(policy.is_shadow)
            self.assertTrue(policy.feature_enabled("weather_hidden_gem_evidence_card"))
            self.assertTrue(config["strategy_lanes"]["confidence_slow_profit"]["enabled"])
            self.assertEqual(config["strategy_lanes"]["confidence_slow_profit"]["min_edge"], 0.015)
            self.assertEqual(config["strategy_lanes"]["confidence_slow_profit"]["min_confidence"], 0.75)

    def test_config_composition_unknown_overlay_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "shadow.yaml"
            config_path.write_text(
                """
config_composition:
  overlays:
    - typo_overlay
"""
            )

            with self.assertRaisesRegex(ValueError, "Unknown config_composition overlay"):
                load_config(config_path)

    def test_config_composition_malformed_block_fails_fast(self):
        cases = {
            "scalar": "config_composition: beta_shadow_observability_all\n",
            "list": "config_composition:\n  - beta_shadow_observability_all\n",
            "empty": "config_composition: {}\n",
        }
        for name, yaml_text in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    config_path = Path(tmpdir) / "shadow.yaml"
                    config_path.write_text(yaml_text)

                    with self.assertRaisesRegex(ValueError, "config_composition"):
                        load_config(config_path)

    def test_config_composition_malformed_overlays_fails_fast(self):
        cases = {
            "mapping": "config_composition:\n  overlays:\n    beta_shadow_observability_all: true\n",
            "number": "config_composition:\n  overlays: 7\n",
            "non_string_item": "config_composition:\n  overlays:\n    - beta_shadow_observability_all\n    - 7\n",
        }
        for name, yaml_text in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    config_path = Path(tmpdir) / "shadow.yaml"
                    config_path.write_text(yaml_text)

                    with self.assertRaisesRegex(ValueError, "config_composition.overlays"):
                        load_config(config_path)

    def test_repo_beta_shadow_configs_normalize_active_shadow_policy(self):
        stable_path = REPO_ROOT / "config.yaml"
        paper_path = REPO_ROOT / "config.paper_beta_shadow_weather.yaml"
        lab_path = REPO_ROOT / "config.prediction_lab_beta_shadow_weather.yaml"

        with patch.dict(os.environ, {}, clear=True):
            stable_config = load_config(stable_path)
            paper_config = load_config(paper_path)
            lab_config = load_config(lab_path)

        for config in (paper_config, lab_config):
            self.assertNotIn("config_composition", config)
            self.assertEqual(config["config_profile"]["base"], "config.yaml")
            self.assertIn("beta_shadow_observability_all", config["config_profile"]["overlays"])
            self.assertEqual(config["openrouter"]["model"], stable_config["openrouter"]["model"])
            self.assertEqual(config["risk"]["max_open_positions"], stable_config["risk"]["max_open_positions"])
            policy = config["strategy_policy_normalized"]
            self.assertEqual(policy["version"], "beta")
            self.assertEqual(policy["beta_mode"], "shadow")
            self.assertTrue(policy.is_active)
            self.assertTrue(policy.is_shadow)
            self.assertFalse(policy.is_enforce)
            self.assertTrue(policy.feature_enabled("weather_hidden_gem_evidence_card"))
            self.assertTrue(policy.feature_enabled("bucket_distribution_scoring"))
            self.assertTrue(policy.feature_enabled("hidden_gem_lane_gates"))
            self.assertTrue(policy.feature_enabled("confidence_slow_profit"))
            self.assertTrue(policy.feature_enabled("lane_sizing_caps"))
            self.assertTrue(config["strategy_lanes"]["enabled"])
            self.assertIn("confidence_slow_profit", config["strategy_lanes"]["enabled_lanes"])
            self.assertTrue(config["strategy_lanes"]["confidence_slow_profit"]["enabled"])
            self.assertEqual(config["strategy_lanes"]["confidence_slow_profit"]["min_edge"], 0.02)
            self.assertEqual(config["strategy_lanes"]["confidence_slow_profit"]["min_confidence"], 0.75)
            self.assertEqual(config["strategy_lanes"]["sizing"]["hidden_gem"]["max_position_usd"], 3.0)
            self.assertEqual(config["strategy_lanes"]["sizing"]["confidence_slow_profit"]["max_position_usd"], 2.0)
            self.assertEqual(Path(config["data_dir"]), Path("data/beta_shadow/paper"))
            self.assertEqual(Path(config["log_dir"]), Path("data/beta_shadow/paper"))
            self.assertFalse(config["storage"]["logs"]["auto_prune"])
            self.assertFalse(config["alerts"]["enabled"])
            self.assertFalse(config["alerts"]["telegram_enabled"])
            self.assertFalse(config["alerts"]["trade_events"])
            self.assertFalse(config["alerts"]["status_events"])
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
