import json
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bot.weather.source_router_replay_wallet import (
    simulate_legacy_unbound_immediate_settlement_diagnostic,
    simulate_replay_wallet_lanes,
)
from scripts.weather_source_router_replay import main as source_router_main


class SourceRouterReplayWalletTests(unittest.TestCase):
    def test_is_explicitly_an_unbound_immediate_settlement_diagnostic(self):
        decisions = [
            {
                "source_router_decision_id": "late",
                "observed_at": "2026-07-03T12:00:00+00:00",
                "comparison": {
                    "source_router_action": "BUY_NO",
                    "source_router_side": "NO",
                    "source_router_side_price": 0.50,
                },
                "resolution_join": {"official_outcome": "NO"},
            },
            {
                "source_router_decision_id": "early",
                "observed_at": "2026-07-02T12:00:00+00:00",
                "comparison": {
                    "source_router_action": "BUY_YES",
                    "source_router_side": "YES",
                    "source_router_side_price": 0.50,
                },
                "resolution_join": {"official_outcome": "NO"},
            },
        ]

        result = simulate_legacy_unbound_immediate_settlement_diagnostic(decisions, fixed_stake_usd=10.0)

        fixed = result["legacy_fixed_stake_diagnostic"]
        self.assertEqual([row["source_router_decision_id"] for row in fixed["trades"]], ["early", "late"])
        self.assertEqual(fixed["summary"]["buy_count"], 2)
        self.assertEqual(fixed["summary"]["total_hypothetical_pnl_usd"], 0.0)
        self.assertNotIn("final_balance_usd", fixed["summary"])
        self.assertEqual(result["mode"], "legacy_unbound_immediate_settlement_diagnostic")
        self.assertEqual(
            result["blockers"],
            [
                "market_level_legacy_outcome_join",
                "immediate_settlement",
                "no_pending_position_or_capital_reservation",
                "no_executable_quote_guarantee",
            ],
        )
        self.assertNotIn("sequential_synthetic_wallet", result)
        self.assertNotIn("max_drawdown_usd", fixed["summary"])
        self.assertEqual(
            simulate_replay_wallet_lanes(decisions, fixed_stake_usd=10.0)["mode"],
            "legacy_unbound_immediate_settlement_diagnostic",
        )

    def test_cli_writes_only_the_explicitly_legacy_diagnostic_artifact(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            history_ledger = tmp / "history.jsonl"
            history_ledger.write_text(json.dumps({
                "market_id": "KXEARLY", "shared_candidate_id": "early", "source_id": "open_meteo",
                "source_name": "Open-Meteo", "city_id": "miami_fl", "market_kind": "high",
                "contract_shape": "threshold", "question_side": "above", "observed_at": "2026-07-01T12:00:00+00:00",
                "source_implied_side": "YES", "source_side_price": 0.5, "actual_outcome": "YES",
                "eligible_for_reliability": True, "source_correctness_eligibility": "eligible_strict_source_proof",
                "eligible_for_source_history": True,
                "strict_source_proof": {"status": "eligible", "reasons": []},
                "source_provenance": {"source_record_sha256": "a" * 64, "canonical_input_sha256": "b" * 64},
                "settlement_ts": "2026-07-02T12:00:00+00:00",
                "known_after": "2026-07-02T12:00:00+00:00",
            }) + "\n", encoding="utf-8")
            ledger = tmp / "ledger.jsonl"
            ledger.write_text(json.dumps({
                "market_id": "KXLATE", "shared_candidate_id": "late", "source_id": "open_meteo",
                "source_name": "Open-Meteo", "city_id": "miami_fl", "market_kind": "high",
                "contract_shape": "threshold", "question_side": "above", "observed_at": "2026-07-03T12:00:00+00:00",
                "source_implied_side": "YES", "source_side_price": 0.5, "actual_outcome": "NO",
                "eligible_for_reliability": True, "known_after": "2026-07-04T12:00:00+00:00",
            }) + "\n", encoding="utf-8")
            outcomes = tmp / "outcomes.jsonl"
            outcomes.write_text('{"market_id":"KXLATE","official_outcome":"YES"}\n', encoding="utf-8")
            output = tmp / "out"
            with patch("sys.argv", [
                "weather_source_router_replay.py", "--ledger-input", str(ledger),
                "--history-ledger-input", str(history_ledger),
                "--outcome-input", str(outcomes), "--output-dir", str(output),
                "--min-sample-count", "1", "--fixed-stake-usd", "10",
            ]):
                self.assertEqual(source_router_main(), 0)
            payload = __import__("json").loads((output / "source_router_legacy_unbound_immediate_settlement_diagnostic.json").read_text())
            self.assertEqual(payload["mode"], "legacy_unbound_immediate_settlement_diagnostic")
            self.assertEqual(payload["legacy_fixed_stake_diagnostic"]["summary"]["buy_count"], 1)
            self.assertFalse((output / "source_router_wallet_lanes.json").exists())


if __name__ == "__main__":
    unittest.main()
