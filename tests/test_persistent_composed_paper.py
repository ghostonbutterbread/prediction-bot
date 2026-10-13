"""Isolated, outcome-free persistent paper entry tests."""
import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot.persistent_composed_paper import consume_persistent_composed_paper
from bot.simulator import Simulator
from bot.strategies.enhanced import KellySizer


class PersistentComposedPaperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.paper_root = self.base / "composed" / "paper"
        self.protected = {name: self.base / name / "paper" for name in ("stable", "beta", "live")}
        self.config_path = self.base / "selection.yaml"
        self.config_path.write_text((Path(__file__).resolve().parents[1] / "lane_compositions" /
                                     "pre_risk_router_guard_kelly.yaml").read_text())
        self.now = datetime.now(timezone.utc)
        self.state = {"publisher": {"runtime_kind": "collector", "instance_id": "owner"},
                      "latest_snapshot": {"snapshot_id": "snap-1", "publisher_runtime": "collector",
                                          "publisher_instance_id": "owner", "published_at": self.now.isoformat()}}
        self.account = {"starting_balance": 100.0, "enable_social": False,
                        "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                        "risk": {"kelly_fraction": 0.5, "max_bet_pct": 0.1}}

    def rows(self, candidate="one", market="KXHIGHNY-26OCT13-T80", action="BUY_YES", ask=0.4, estimate=0.45):
        identity = {"shared_candidate_id": candidate, "shared_snapshot_id": "snap-1", "run_id": "run-1",
                    "market_id": market, "observed_at": self.now.isoformat()}
        stable = {**identity, "policy": "control_stable", "decision_id": "stable-" + candidate, "action": "SKIP"}
        picker = {**identity, "policy": "shadow_pre_risk_picker", "decision_id": "picker-" + candidate,
                  "action": action, "model_probability": 0.8 if action == "BUY_YES" else 0.2,
                  "confidence": 0.9, "question": "Will NYC high temperature be above 80 degrees?",
                  "exchange": "kalshi", "event_ticker": "KXHIGHNY-26OCT13", "entry_price": ask,
                  "market_route": {"allowed": True, "handler_id": "weather.daily_temperature.v1"},
                  "provenance": {"picker_intent_source": "decision_artifact.strategy_signal"}}
        router = {**identity, "policy": "shadow_source_router", "decision_id": "router-" + candidate,
                  "action": action, "provenance": {"source_router": {"direct_history": {"mode": "committed_index_direct_read"}}}}
        guard = {**identity, "policy": "shadow_pre_risk_fee_aware_loss_guard", "decision_id": "guard-" + candidate,
                 "action": action, "entry_price": estimate,
                 "provenance": {"payout_aware": {"selected_side_ask": ask, "entry_price": estimate,
                                                  "quote_not_fill": True}}}
        return [stable, picker, router, guard]

    def consume(self, rows, **kwargs):
        params = dict(saved_config_path=self.config_path, lane_rows=rows, cohort_id="new-cohort",
                      publisher_state=self.state, paper_root=self.paper_root,
                      protected_roots=self.protected, account_config=self.account, enabled=True, now=self.now)
        params.update(kwargs)
        return consume_persistent_composed_paper(**params)

    def test_disabled_is_no_write_and_never_constructs_simulator(self):
        with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
            result = consume_persistent_composed_paper(saved_config_path=self.config_path,
                lane_rows=self.rows(), cohort_id="new-cohort", publisher_state=self.state,
                paper_root=self.paper_root, protected_roots=self.protected,
                account_config=self.account)
        self.assertEqual(result["reason_code"], "paper_consumer_disabled")
        self.assertFalse(self.paper_root.exists())

    def test_qualified_buy_and_veto_skip_only_open_one_simulated_position(self):
        rows = self.rows() + self.rows("two", "KXHIGHNY-26OCT13-T81")
        rows[-1] = {**rows[-1], "action": "SKIP", "reason_code": "guard_disagrees"}
        report = self.consume(rows)
        self.assertEqual([r["status"] for r in report["decisions"]], ["opened", "skipped"])
        self.assertEqual(report["decisions"][0]["fill_basis"], "paper_simulated_not_exchange_fill")
        self.assertEqual(report["decisions"][1]["reason_code"], "guard_disagrees")
        session = json.loads(next(self.paper_root.glob("sim_*.json")).read_text())
        self.assertEqual(len(session["trades"]), 1)
        trade = session["trades"][0]
        self.assertFalse(trade["resolved"])
        self.assertEqual(trade["market_price"], 0.45)
        self.assertEqual(trade["signals"]["selected_side_recorded_ask"], 0.4)
        self.assertEqual(trade["signals"]["conservative_estimate"], 0.45)
        self.assertEqual(trade["signals"]["composed_decision_id"], report["decisions"][0]["decision_id"])
        expected = KellySizer(kelly_fraction=0.5, max_bet_pct=0.1).calculate(0.8, 0.45, 100)
        self.assertEqual(trade["position_size"], expected)
        self.assertEqual(session["reserved_capital"], expected)
        self.assertEqual(session["available_cash"], 100 - expected)
        self.assertEqual(self.consume(rows)["decisions"][0]["status"], "already_processed")
        reloaded = Simulator({**self.account, "data_dir": str(self.paper_root), "trading": {"mode": "paper"},
                              "paper_wallets": {"allow_legacy_parent_session_lookup": False}})
        self.assertEqual(reloaded.available_cash, 100 - expected)
        self.assertEqual(reloaded.risk.state.total_exposure, expected)
        self.assertEqual(reloaded.risk.data_path, self.paper_root / "risk_state.json")
        self.assertEqual(len(reloaded.trades), 1)

    def test_append_only_ledger_enrolls_only_latest_publisher_snapshot(self):
        old = [{**row, "shared_candidate_id": "prior", "shared_snapshot_id": "old-snapshot",
                "run_id": "old-run", "market_id": "KXHIGHNY-26OCT12-T80",
                "observed_at": "2026-10-12T12:00:00+00:00"}
               for row in self.rows(candidate="prior", market="KXHIGHNY-26OCT12-T80")]
        current = self.rows(candidate="current", market="KXHIGHNY-26OCT13-T80")
        report = self.consume(old + current)
        self.assertEqual(len(report["decisions"]), 1)
        self.assertEqual(report["decisions"][0]["status"], "opened")
        session = json.loads(next(self.paper_root.glob("sim_*.json")).read_text())
        self.assertEqual([trade["market_id"] for trade in session["trades"]],
                         ["KXHIGHNY-26OCT13-T80"])

    def test_orphan_picker_buy_cannot_open_without_matching_control(self):
        control = self.rows(candidate="control", action="SKIP")
        orphan = [row for row in self.rows(candidate="orphan", market="KXHIGHNY-26OCT13-T81")
                  if row["policy"] != "control_stable"]
        with self.assertRaisesRegex(ValueError, "picker.*control"):
            self.consume(control + orphan)
        self.assertFalse(self.paper_root.exists())

    def test_current_snapshot_id_cannot_enroll_stale_observation(self):
        stale = [{**row, "shared_candidate_id": "stale", "observed_at": "2020-01-01T00:00:00+00:00"}
                 for row in self.rows(candidate="stale", market="KXHIGHNY-26OCT13-T81")]
        with self.assertRaisesRegex(ValueError, "observation.*stale"):
            self.consume(stale + self.rows(candidate="current"))
        self.assertFalse(self.paper_root.exists())

    def test_latest_snapshot_accepts_normal_observation_to_publication_lag(self):
        observed = (self.now - timedelta(seconds=30)).isoformat()
        rows = [{**row, "observed_at": observed} for row in self.rows()]
        self.assertEqual(self.consume(rows)["decisions"][0]["status"], "opened")

    def test_no_side_uses_one_minus_yes_probability_and_only_no_ask(self):
        result = self.consume(self.rows(action="BUY_NO", ask=0.35, estimate=0.4))
        self.assertEqual(result["decisions"][0]["status"], "opened")
        trade = json.loads(next(self.paper_root.glob("sim_*.json")).read_text())["trades"][0]
        self.assertEqual(trade["market_price"], 0.4)
        self.assertEqual(trade["model_probability"], 0.8)
        self.assertEqual(trade["signals"]["selected_side_recorded_ask"], 0.35)
        self.assertEqual(trade["direction"], "BUY_NO")
        self.assertEqual(trade["position_size"], KellySizer(kelly_fraction=0.5, max_bet_pct=0.1).calculate(0.8, 0.4, 100))

    def test_preflight_rejects_wrong_root_config_cohort_and_stale_publisher_before_simulator(self):
        rows = self.rows()
        for bad in (self.protected["stable"], self.protected["live"],
                    self.base / "stable" / "paper" / "other" / "paper",
                    self.base / "relative", self.base / "notpaper"):
            with self.subTest(root=bad), patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
                with self.assertRaisesRegex(ValueError, "root|overlaps"):
                    self.consume(rows, paper_root=bad)
        self.assertFalse(self.paper_root.exists())
        invalid = self.config_path.read_text().replace("shadow_pre_risk_picker", "control_stable")
        self.config_path.write_text(invalid)
        with self.assertRaises(ValueError):
            self.consume(rows)
        self.assertFalse(self.paper_root.exists())
        self.config_path.write_text(invalid.replace("control_stable", "shadow_pre_risk_picker"))
        old = self.state["latest_snapshot"]["published_at"]
        self.state["latest_snapshot"]["published_at"] = "2020-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.consume(rows)
        self.state["latest_snapshot"]["published_at"] = old
        incomplete = [row for row in rows if row["policy"] != "shadow_pre_risk_picker"]
        with self.assertRaisesRegex(ValueError, "picker incomplete"):
            self.consume(incomplete)
        self.assertFalse(self.paper_root.exists())
        self.consume(rows)
        with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
            with self.assertRaisesRegex(ValueError, "binding mismatch"):
                self.consume(rows, cohort_id="other")
            self.config_path.write_text(self.config_path.read_text() + "\n# changed")
            with self.assertRaisesRegex(ValueError, "binding mismatch"):
                self.consume(rows)

    def test_effective_risk_environment_change_cannot_reuse_bound_account(self):
        rows = self.rows()
        with patch.dict("os.environ", {"MAX_BET_PCT": "0.05555"}):
            self.consume(rows)
        with patch.dict("os.environ", {"MAX_BET_PCT": "0.01111"}):
            with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
                with self.assertRaisesRegex(ValueError, "binding mismatch"):
                    self.consume(rows)

    def test_crash_after_trade_save_reconciles_marker_without_second_entry(self):
        rows = self.rows()
        original = Simulator.submit_paper_signal
        def save_then_crash(sim, signal, blockers=None, *, persist=True):
            original(sim, signal, blockers, persist=persist)
            raise RuntimeError("crash after trade save")
        with patch("bot.persistent_composed_paper.Simulator.submit_paper_signal", save_then_crash):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.consume(rows)
        self.assertEqual(len(json.loads(next(self.paper_root.glob("sim_*.json")).read_text())["trades"]), 1)
        replay = self.consume(rows)
        self.assertEqual(replay["decisions"][0]["status"], "already_processed")
        self.assertEqual(len(json.loads(next(self.paper_root.glob("sim_*.json")).read_text())["trades"]), 1)

    def test_crash_before_trade_save_remains_pending_and_never_resubmits(self):
        rows = self.rows()
        with patch("bot.persistent_composed_paper.Simulator.submit_paper_signal", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.consume(rows)
        with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
            replay = self.consume(rows)
        self.assertEqual(replay["decisions"][0]["status"], "pending_review")
        self.assertFalse(list(self.paper_root.glob("sim_*.json")))

    def test_unreconciled_pending_fences_all_later_buy_decisions(self):
        first = self.rows()
        with patch("bot.persistent_composed_paper.Simulator.submit_paper_signal", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.consume(first)
        added = first + self.rows("two", "KXHIGHNY-26OCT13-T81")
        result = self.consume(added)
        self.assertEqual(result["decisions"][0]["status"], "pending_review")
        self.assertEqual(result["decisions"][1]["status"], "blocked_pending_review")
        self.assertFalse(list(self.paper_root.glob("sim_*.json")))

    def test_simulator_risk_skip_is_journaled_without_trade(self):
        self.account["max_entry_price"] = 0.3
        result = self.consume(self.rows())
        self.assertEqual(result["decisions"][0]["status"], "skipped")
        self.assertEqual(len(json.loads(next(self.paper_root.glob("sim_*.json")).read_text())["trades"]), 0)
        replay = self.consume(self.rows())
        self.assertEqual(replay["decisions"][0]["status"], "already_processed")

    def test_unquoted_or_wrong_side_guard_is_skip_without_simulator(self):
        for index, modified in enumerate((lambda rows: {**rows[-1], "entry_price": 0.2,
                                        "provenance": {"payout_aware": {"selected_side_ask": 0.4,
                                                                         "entry_price": 0.2, "quote_not_fill": True}}},
                         lambda rows: {**rows[-1], "action": "BUY_NO"})):
            with self.subTest(index=index):
                self.paper_root = self.base / f"composed-{index}" / "paper"
                rows = self.rows()
                rows[-1] = modified(rows)
                with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
                    result = self.consume(rows)
                self.assertEqual(result["decisions"][0]["status"], "skipped")
                self.assertFalse(list(self.paper_root.glob("sim_*.json")))

    def test_mismatched_publisher_and_changed_decision_fail_before_new_trade(self):
        rows = self.rows()
        self.state["latest_snapshot"]["publisher_instance_id"] = "other"
        with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
            with self.assertRaisesRegex(ValueError, "publisher mismatched"):
                self.consume(rows)
        self.assertFalse(self.paper_root.exists())
        self.state["latest_snapshot"]["publisher_instance_id"] = "owner"
        self.consume(rows)
        changed = list(rows)
        changed[-1] = {**changed[-1], "action": "SKIP"}
        with self.assertRaisesRegex(ValueError, "processed decision identity changed"):
            self.consume(changed)
        self.assertEqual(len(json.loads(next(self.paper_root.glob("sim_*.json")).read_text())["trades"]), 1)

    def test_externally_settled_session_fails_closed_without_new_buy(self):
        rows = self.rows()
        self.consume(rows)
        session_path = next(self.paper_root.glob("sim_*.json"))
        session = json.loads(session_path.read_text())
        session["trades"][0]["resolved"] = True
        session["trades"][0]["outcome"] = "YES"
        session_path.write_text(json.dumps(session))
        with patch("bot.persistent_composed_paper.Simulator", side_effect=AssertionError("constructed")):
            with self.assertRaisesRegex(ValueError, "external settlement"):
                self.consume(rows + self.rows("two", "KXHIGHNY-26OCT13-T81"))

    def test_verified_exact_settlement_allows_next_fresh_paper_entry(self):
        first = self.rows()
        self.consume(first)
        session = json.loads(next(self.paper_root.glob("sim_*.json")).read_text())
        trade = session["trades"][0]
        market = trade["market_id"]
        settled = (datetime.fromisoformat(trade["timestamp"]) + timedelta(minutes=1)).isoformat()
        source = self.base / "strict_resolutions.jsonl"
        binding = self.base / "exact_bindings.jsonl"
        raw = (json.dumps({"market_id": market, "requested_market_id": market,
                           "returned_market_id": market, "market_status": "finalized",
                           "resolution_id": "test-settlement-1", "kalshi_result": "yes",
                           "settlement_ts": settled}) + "\n").encode()
        source.write_bytes(raw)
        binding.write_text(json.dumps({"composed_decision_id": trade["signals"]["composed_decision_id"],
                                       "shared_candidate_id": trade["signals"]["shared_candidate_id"],
                                       "run_id": trade["signals"]["run_id"], "market_id": market,
                                       "outcome": "YES", "settlement_ts": settled,
                                       "strict_resolution_source_sha256": hashlib.sha256(raw).hexdigest(),
                                       "strict_resolution_source_line_number": 1,
                                       "raw_resolution_row_sha256": hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()}) + "\n")
        simulator = Simulator({**self.account, "data_dir": str(self.paper_root), "trading": {"mode": "paper"},
                               "paper_wallets": {"allow_legacy_parent_session_lookup": False}})
        events = simulator.resolution_adapter.resolve_exact_positions(
            binding_path=binding, strict_resolutions_path=source,
            as_of=(datetime.fromisoformat(settled) + timedelta(minutes=1)).isoformat())
        self.assertEqual(len(events), 1)
        later = self.consume(first + self.rows("two", "KXHIGHNY-26OCT13-T81"))
        self.assertEqual([r["status"] for r in later["decisions"]], ["already_processed", "opened"])
        self.assertEqual(len(json.loads(next(self.paper_root.glob("sim_*.json")).read_text())["trades"]), 2)

    def test_corrupt_nonfinite_settlement_cannot_seed_next_paper_entry(self):
        rows = self.rows()
        self.consume(rows)
        session_path = next(self.paper_root.glob("sim_*.json"))
        session = json.loads(session_path.read_text())
        trade = session["trades"][0]
        trade["resolved"] = True
        trade["outcome"] = "YES"
        trade["resolution_type"] = "settled"
        trade["settlement_ts"] = (datetime.fromisoformat(trade["timestamp"]) + timedelta(minutes=1)).isoformat()
        trade["resolution_identity"] = {"composed_decision_id": trade["signals"]["composed_decision_id"],
                                        "shared_candidate_id": trade["signals"]["shared_candidate_id"],
                                        "run_id": trade["signals"]["run_id"], "market_id": trade["market_id"]}
        trade["resolution_source_path"] = str(self.base / "strict_resolutions.jsonl")
        trade["resolution_source_line_number"] = 1
        trade["resolution_row_sha256"] = "a" * 64
        trade["resolution_source_sha256"] = "b" * 64
        trade["pnl"] = float("nan")
        session_path.write_text(json.dumps(session))
        with self.assertRaisesRegex(ValueError, "exact composed resolution"):
            self.consume(rows + self.rows("two", "KXHIGHNY-26OCT13-T81"))
