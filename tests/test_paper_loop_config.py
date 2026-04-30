import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_loop


class PaperLoopConfigTests(unittest.TestCase):
    def test_get_config_loads_scan_limit_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
trading:
  mode: paper
scan:
  markets_per_exchange: 321
"""
            )

            with patch.dict(os.environ, {"PAPER_MODE": "true"}, clear=False):
                os.environ.pop("MARKETS_PER_EXCHANGE", None)
                cfg = paper_loop.get_config(cfg_path)

        self.assertEqual(cfg["scan"]["markets_per_exchange"], 321)

    def test_get_config_allows_markets_per_exchange_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
trading:
  mode: paper
scan:
  markets_per_exchange: 321
"""
            )

            with patch.dict(os.environ, {"PAPER_MODE": "true", "MARKETS_PER_EXCHANGE": "222"}, clear=False):
                cfg = paper_loop.get_config(cfg_path)

        self.assertEqual(cfg["scan"]["markets_per_exchange"], 222)


if __name__ == "__main__":
    unittest.main()
