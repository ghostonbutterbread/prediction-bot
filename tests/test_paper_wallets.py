import tempfile
import unittest
from pathlib import Path

from bot.config import load_config
from bot.paper_wallets import (
    BETA_PAPER_WALLET_ID,
    STABLE_PAPER_WALLET_ID,
    build_paper_accounting_ref,
    resolve_active_paper_wallet_id,
    resolve_paper_wallet_contract,
)


class PaperWalletContractTests(unittest.TestCase):
    def test_default_runtime_exposes_stable_and_beta_wallet_contracts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
trading:
  mode: paper
"""
            )

            config = load_config(config_path)

        wallets = config["paper_wallets"]
        self.assertEqual(wallets["active_wallet_id"], STABLE_PAPER_WALLET_ID)
        self.assertEqual(wallets["active_policy_id"], "stable")
        self.assertEqual(wallets[STABLE_PAPER_WALLET_ID]["namespace"], "paper_stable")
        self.assertTrue(wallets[STABLE_PAPER_WALLET_ID]["root_dir"].endswith("/data/paper"))
        self.assertTrue(wallets[BETA_PAPER_WALLET_ID]["root_dir"].endswith("/data/beta_shadow/paper"))
        self.assertTrue(wallets[BETA_PAPER_WALLET_ID]["risk_state_path"].endswith("/data/beta_shadow/paper/risk_state.json"))

    def test_beta_shadow_config_maps_active_wallet_to_beta_paper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            beta_base = Path(tmpdir) / "data" / "beta_shadow"
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f"""
runtime:
  base_dir: {beta_base}
trading:
  mode: paper
strategy_policy:
  version: beta
  beta:
    mode: shadow
"""
            )

            config = load_config(config_path)

        self.assertEqual(resolve_active_paper_wallet_id(config), BETA_PAPER_WALLET_ID)
        contract = resolve_paper_wallet_contract(config, session_id="session-1")
        self.assertEqual(contract.wallet_id, BETA_PAPER_WALLET_ID)
        self.assertEqual(contract.policy_id, "beta")
        self.assertEqual(contract.namespace, "paper_beta")
        self.assertTrue(str(contract.root_dir).endswith("/data/beta_shadow/paper"))
        self.assertTrue(str(contract.risk_state_path).endswith("/data/beta_shadow/paper/risk_state.json"))
        self.assertTrue(str(contract.session_path).endswith("/data/beta_shadow/paper/sim_session-1.json"))

    def test_beta_policy_without_root_override_still_resolves_isolated_beta_wallet_root(self):
        config = {
            "strategy_policy": {"version": "beta", "beta": {"mode": "shadow"}},
            "data_dir": "data/paper",
        }

        contract = resolve_paper_wallet_contract(config, session_id="session-2")

        self.assertEqual(contract.wallet_id, BETA_PAPER_WALLET_ID)
        self.assertEqual(str(contract.root_dir), "data/beta_shadow/paper")
        self.assertEqual(str(contract.session_path), "data/beta_shadow/paper/sim_session-2.json")

    def test_accounting_ref_uses_canonical_namespace_and_current_session_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ref = build_paper_accounting_ref(
                {"data_dir": str(Path(tmpdir) / "paper")},
                session_id="20260513_010203",
                data_dir=Path(tmpdir) / "paper",
                mutates_accounting=True,
                trade_id="trade-1",
            )

        self.assertEqual(ref["wallet_id"], STABLE_PAPER_WALLET_ID)
        self.assertEqual(ref["policy_id"], "stable")
        self.assertEqual(ref["namespace"], str(Path(tmpdir) / "paper"))
        self.assertEqual(ref["wallet_namespace"], "paper_stable")
        self.assertEqual(ref["trade_id"], "trade-1")
        self.assertTrue(ref["root_path"].endswith("/paper"))
        self.assertTrue(ref["risk_state_path"].endswith("/paper/risk_state.json"))
        self.assertTrue(ref["session_path"].endswith("/paper/sim_20260513_010203.json"))
        self.assertEqual(ref["ledger_path"], ref["session_path"])
        self.assertTrue(ref["mutates_balance"])
        self.assertTrue(ref["mutates_accounting"])
        self.assertFalse(ref["places_live_orders"])

    def test_beta_mode_off_stays_on_stable_paper_wallet(self):
        config = {
            "strategy_policy": {"version": "beta", "beta": {"mode": "off"}},
            "data_dir": "data/paper",
        }

        self.assertEqual(resolve_active_paper_wallet_id(config), STABLE_PAPER_WALLET_ID)
        contract = resolve_paper_wallet_contract(config, session_id="session-off")

        self.assertEqual(contract.wallet_id, STABLE_PAPER_WALLET_ID)
        self.assertEqual(contract.policy_id, "stable")
        self.assertEqual(str(contract.root_dir), "data/paper")
        self.assertEqual(str(contract.session_path), "data/paper/sim_session-off.json")

    def test_wallet_contracts_reject_overlapping_stable_and_beta_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir) / "wallets" / "shared"
            config = {
                "runtime": {"base_dir": str(Path(tmpdir) / "data")},
                "trading": {"mode": "paper"},
                "paper_wallets": {
                    "stable_paper": {"root_dir": str(shared_root)},
                    "beta_paper": {"root_dir": str(shared_root)},
                },
            }

            from bot.paper_wallets import build_paper_wallet_contracts
            with self.assertRaises(ValueError):
                build_paper_wallet_contracts(config)

    def test_explicit_wallet_root_overrides_survive_config_materialization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            stable_root = Path(tmpdir) / "wallets" / "control"
            beta_root = Path(tmpdir) / "wallets" / "challenger"
            config_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
trading:
  mode: paper
paper_wallets:
  active_wallet_id: beta_paper
  stable_paper:
    root_dir: {stable_root}
  beta_paper:
    root_dir: {beta_root}
"""
            )

            config = load_config(config_path)

        wallets = config["paper_wallets"]
        self.assertEqual(wallets["active_wallet_id"], BETA_PAPER_WALLET_ID)
        self.assertEqual(wallets[STABLE_PAPER_WALLET_ID]["root_dir"], str(stable_root / "paper"))
        self.assertEqual(wallets[BETA_PAPER_WALLET_ID]["root_dir"], str(beta_root / "paper"))


if __name__ == "__main__":
    unittest.main()
