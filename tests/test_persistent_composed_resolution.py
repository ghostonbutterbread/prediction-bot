"""Exact composed-paper settlement: synthetic, independent source only."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot.simulator import SimTrade, Simulator


class PersistentComposedResolutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paper = self.root / "composed" / "paper"
        self.sim = self.reload()
        (self.paper / "composed_binding.json").write_text(json.dumps({"schema_name": "persistent_composed_paper_binding", "paper_only": True}))
        self.add_trade("one")
        self.add_trade("two")
        self.sim._save_session()
        self.source = self.root / "strict_resolutions.jsonl"
        self.binding = self.root / "exact_bindings.jsonl"

    def reload(self):
        return Simulator({"data_dir": str(self.paper), "starting_balance": 100.0,
                          "enable_social": False, "strategy": {"enable_ai": False, "enable_news": False,
                          "enable_social": False}, "trading": {"mode": "paper"},
                          "paper_wallets": {"allow_legacy_parent_session_lookup": False}})

    def add_trade(self, key, direction="BUY_YES"):
        trade = SimTrade(id="sim-" + key, timestamp="2026-10-13T10:00:00+00:00",
                         exchange="kalshi", market_id="KXTEST-26OCT13-T80", question="Synthetic?",
                         direction=direction, model_probability=0.8, market_price=0.4,
                         edge=0.4, confidence=0.9, position_size=10.0,
                         reserved_capital=10.0, entry_price=0.4, fee_rate=self.sim.kelly.fee_rate,
                         signals={"composed_decision_id": "decision-" + key,
                                  "shared_candidate_id": "candidate-" + key, "run_id": "run-1"})
        self.sim.trades.append(trade)
        self.sim.balance = 100.0
        self.sim.state_adapter.refresh_capital_state()
        return trade

    def source_and_binding(self, outcome="YES", **changes):
        source_row = {"market_id": "KXTEST-26OCT13-T80", "requested_market_id": "KXTEST-26OCT13-T80",
                      "returned_market_id": "KXTEST-26OCT13-T80", "market_status": "finalized",
                      "kalshi_result": outcome.lower(), "resolution": {"outcome": outcome},
                      "resolution_id": "independent-resolution-1", "settlement_ts": "2026-10-13T11:00:00+00:00"}
        source_row.update(changes.pop("source_changes", {}))
        raw = (json.dumps(source_row, sort_keys=True) + "\n").encode()
        self.source.write_bytes(raw)
        binding_row = {"composed_decision_id": "decision-one", "shared_candidate_id": "candidate-one",
                       "run_id": "run-1", "market_id": "KXTEST-26OCT13-T80",
                       "outcome": outcome, "settlement_ts": source_row["settlement_ts"],
                       "strict_resolution_source_sha256": hashlib.sha256(raw).hexdigest(),
                       "strict_resolution_source_line_number": 1,
                       "raw_resolution_row_sha256": hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()}
        binding_row.update(changes)
        self.binding.write_text(json.dumps(binding_row) + "\n")
        return binding_row

    def resolve(self, as_of="2026-10-13T12:00:00+00:00"):
        return self.sim.resolution_adapter.resolve_exact_positions(
            binding_path=self.binding, strict_resolutions_path=self.source, as_of=as_of)

    def test_exact_source_settles_only_matched_same_market_trade_and_reloads_idempotently(self):
        receipt = self.source_and_binding()
        events = self.resolve()
        self.assertEqual([event.position_id for event in events], ["sim-one"])
        self.assertTrue(self.sim.trades[0].resolved)
        self.assertFalse(self.sim.trades[1].resolved)
        self.assertEqual(self.sim.reserved_capital, 10.0)
        self.assertEqual(self.sim.trades[0].resolution_row_sha256, receipt["raw_resolution_row_sha256"])
        self.assertEqual(self.sim.trades[0].settlement_ts, receipt["settlement_ts"])
        self.assertEqual(self.sim.trades[0].pnl, 13.95)
        self.sim = self.reload()
        self.assertEqual(self.sim.trades[0].resolution_row_sha256, receipt["raw_resolution_row_sha256"])
        self.assertEqual(self.resolve(), [])
        self.assertEqual(self.sim.balance, 113.95)
        self.assertEqual(self.sim.risk.state.daily_pnl, 13.95)

    def test_exact_source_is_streamed_not_loaded_whole_into_memory(self):
        self.source_and_binding()
        original = Path.read_bytes
        def bounded_read(path):
            if path == self.source:
                raise AssertionError("whole strict source read")
            return original(path)
        with patch.object(Path, "read_bytes", bounded_read):
            self.assertEqual(len(self.resolve()), 1)

    def test_source_change_during_preflight_does_not_book_settlement(self):
        self.source_and_binding()
        original = self.sim.risk._find_trade_record
        def change_source_before_accounting(trade_id):
            self.source.write_text("{changed evidence}\n")
            return original(trade_id)
        with patch.object(self.sim.risk, "_find_trade_record", side_effect=change_source_before_accounting):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.resolve()
        self.assertEqual(self.sim.balance, 100.0)
        self.assertFalse(self.sim.trades[0].resolved)
        self.assertFalse((self.paper / "composed_settlement_unsafe.json").exists())

    def test_duplicate_saved_position_id_refuses_settlement_before_accounting(self):
        self.sim.trades[1].id = self.sim.trades[0].id
        self.sim._save_session()
        self.source_and_binding()
        with self.assertRaisesRegex(ValueError, "duplicate.*trade id"):
            self.resolve()
        self.assertFalse(any(trade.resolved for trade in self.sim.trades))
        self.assertEqual(self.sim.balance, 100.0)
        self.assertFalse((self.paper / "composed_settlement_unsafe.json").exists())

    def test_composed_binding_excludes_conflicting_source_and_untrusted_intent_outcome(self):
        from bot.composed_resolution_binding import bind_composed_finalized_outcomes
        from bot.forward_composed_paper import _digest
        identity = {"shared_candidate_id": "candidate-one", "shared_snapshot_id": "snapshot-one",
                    "run_id": "run-1", "market_id": "KXTEST-26OCT13-T80",
                    "observed_at": "2026-10-13T10:00:00+00:00"}
        intent = {**identity, "decision_id": "forward-combined:" + _digest(identity),
                  "lane_id": "forward_composed_paper", "action": "BUY_YES"}
        self.source_and_binding(source_changes={"resolution": {"outcome": "NO"}})
        result = bind_composed_finalized_outcomes(intents=[intent], strict_resolutions_path=self.source,
                                                  output_dir=self.root / "invalid-source")
        self.assertEqual(result.bindings_path.read_text(), "")
        self.assertEqual(json.loads(result.unbound_path.read_text())["reason_code"], "invalid_authoritative_resolution")
        with self.assertRaises(ValueError):
            bind_composed_finalized_outcomes(intents=[{**intent, "outcome": "YES"}],
                                             strict_resolutions_path=self.source,
                                             output_dir=self.root / "untrusted-intent")
        self.assertFalse((self.root / "untrusted-intent").exists())
        self.source_and_binding()
        self.source.write_bytes(self.source.read_bytes() * 2)
        duplicate = bind_composed_finalized_outcomes(intents=[intent], strict_resolutions_path=self.source,
                                                     output_dir=self.root / "duplicate-source")
        self.assertEqual(json.loads(duplicate.unbound_path.read_text())["reason_code"], "ambiguous_market_resolution")

    def test_missing_wrong_or_nonfinal_evidence_leaves_both_reservations(self):
        cases = ({"strict_resolution_source_sha256": "a" * 64},
                 {"strict_resolution_source_line_number": 2},
                 {"composed_decision_id": "decision-other"},
                 {"source_changes": {"market_status": "closed"}},
                 {"source_changes": {"requested_market_id": "other"}},
                 {"source_changes": {"settlement_ts": None}})
        for change in cases:
            with self.subTest(change=change):
                self.source_and_binding(**change)
                self.assertEqual(self.resolve(), [])
                self.assertEqual(self.sim.reserved_capital, 20.0)
                self.assertEqual(self.sim.balance, 100.0)

    def test_all_outcome_status_and_clock_assertions_must_agree(self):
        cases = ({"source_changes": {"resolution": {"outcome": "NO"}}},
                 {"source_changes": {"metadata": {"outcome": "VOID"}}},
                 {"source_changes": {"resolution": {"status": "closed", "outcome": "YES"}}},
                 {"source_changes": {"resolution": {"settlement_ts": "2026-10-13T12:00:00Z", "outcome": "YES"}}},
                 {"resolution": {"outcome": "NO"}},
                 {"metadata": {"outcome_known_at": "2026-10-13T10:30:00Z"}},
                 {"source_changes": {"settlement_value": "NO"}})
        for change in cases:
            with self.subTest(change=change):
                self.source_and_binding(**change)
                self.assertEqual(self.resolve(), [])
                self.assertFalse(self.sim.trades[0].resolved)

    def test_conflicting_duplicate_binding_and_duplicate_source_are_unresolved(self):
        row = self.source_and_binding()
        self.binding.write_text(json.dumps(row) + "\n" + json.dumps({**row, "outcome": "NO"}) + "\n")
        self.assertEqual(self.resolve(), [])
        self.source_and_binding()
        self.source.write_bytes(self.source.read_bytes() * 2)
        self.assertEqual(self.resolve(), [])
        self.assertEqual(self.sim.reserved_capital, 20.0)

    def test_entry_chronology_and_outcome_availability_are_distinct(self):
        self.source_and_binding(source_changes={"settlement_ts": "2026-10-13T10:00:00+00:00"})
        self.assertEqual(self.resolve(), [])
        self.source_and_binding(source_changes={"outcome_known_at": "2026-10-13T14:00:00+00:00"})
        self.assertEqual(self.resolve(), [])
        self.assertEqual(self.resolve(as_of="2026-10-13T14:00:00+00:00")[0].position_id, "sim-one")
        self.assertEqual(self.sim.trades[0].outcome_known_at, "2026-10-13T14:00:00+00:00")

    def test_no_side_uses_simulated_fill(self):
        self.sim.trades[0].direction = "BUY_NO"
        self.sim._save_session()
        self.source_and_binding(outcome="NO")
        self.assertEqual(self.resolve()[0].pnl, 13.95)
        self.assertEqual(self.sim.balance, 113.95)

    def test_void_releases_reservation_without_economic_credit(self):
        self.source_and_binding(outcome="VOID")
        before = (self.sim.balance, self.sim.risk.state.daily_pnl, self.sim.risk.state.consecutive_wins)
        event = self.resolve()[0]
        self.assertEqual(event.position_id, "sim-one")
        self.assertEqual(event.status, "void_resolution")
        self.assertIsNone(event.pnl)
        self.assertIsNone(self.sim.trades[0].pnl)
        self.assertEqual(self.sim.reserved_capital, 10.0)
        self.assertEqual((self.sim.balance, self.sim.risk.state.daily_pnl, self.sim.risk.state.consecutive_wins), before)

    def test_prior_same_market_resolution_conflict_leaves_other_position_open(self):
        self.source_and_binding(outcome="YES")
        self.assertEqual(len(self.resolve()), 1)
        row = self.source_and_binding(outcome="NO")
        row.update(composed_decision_id="decision-two", shared_candidate_id="candidate-two")
        self.binding.write_text(json.dumps(row) + "\n")
        self.assertEqual(self.resolve(), [])
        self.assertEqual(self.sim.reserved_capital, 10.0)

    def test_generic_market_resolution_is_fenced_for_composed_account(self):
        with self.assertRaisesRegex(ValueError, "exact resolution"):
            self.sim.resolve_open_positions(object())

    def test_paper_drawdown_halt_never_sends_external_alert(self):
        self.sim.risk.max_session_drawdown_pct = 0.01
        self.source_and_binding(outcome="NO")
        with patch.object(self.sim.risk, "_send_drawdown_alert", side_effect=AssertionError("external alert")):
            self.assertEqual(self.resolve()[0].pnl, -10.0)
        self.assertTrue(self.sim.risk.state.max_drawdown_halt)

    def test_crash_after_risk_write_fences_reloaded_account_and_does_not_retry(self):
        self.source_and_binding(outcome="NO")
        original = self.sim.risk.record_outcome
        def write_risk_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated crash after risk save")
        with patch.object(self.sim.risk, "record_outcome", side_effect=write_risk_then_crash):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.resolve()
        self.assertTrue((self.paper / "composed_settlement_unsafe.json").exists())
        self.sim = self.reload()
        self.assertEqual(self.sim.risk.state.daily_pnl, -10.0)
        self.assertFalse(self.sim.trades[0].resolved)
        self.assertEqual(self.sim.reserved_capital, 20.0)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.resolve()
        decision = self.sim.risk.check_trade({"question": "Synthetic?"}, 1.0, available_cash=80.0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.metadata.get("reason_code"), "composed_settlement_unsafe")

    def test_corrupt_risk_file_after_settlement_cannot_be_rebuilt_as_fresh_account(self):
        self.source_and_binding()
        self.assertEqual(len(self.resolve()), 1)
        risk_path = self.sim.risk.data_path
        risk_path.write_text("{corrupt")
        with self.assertRaisesRegex(ValueError, "composed.*risk"):
            self.reload()
        self.assertEqual(risk_path.read_text(), "{corrupt")

    def test_composed_risk_save_failure_is_not_silently_reported_as_success(self):
        with patch("bot.risk.json.dump", side_effect=OSError("synthetic risk write failure")):
            with self.assertRaisesRegex(OSError, "synthetic risk write failure"):
                self.sim.risk._save_state()

    def test_corrupt_bound_session_cannot_start_new_paper_account(self):
        path = self.paper / f"sim_{self.sim.session_id}.json"
        path.write_text("{corrupt")
        with self.assertRaisesRegex(Exception, "composed.*session"):
            self.reload()
        self.assertEqual(path.read_text(), "{corrupt")

    def test_crash_after_session_write_fences_reloaded_account(self):
        self.source_and_binding(outcome="NO")
        original = self.sim.session_store.save_session
        def write_session_then_crash():
            original()
            raise RuntimeError("simulated crash after session save")
        with patch.object(self.sim.session_store, "save_session", side_effect=write_session_then_crash):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.resolve()
        self.sim = self.reload()
        self.assertTrue(self.sim.trades[0].resolved)
        self.assertEqual(self.sim.risk.state.daily_pnl, -10.0)
        self.assertFalse(self.sim.risk.check_trade({"question": "Synthetic?"}, 1.0).approved)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.resolve()

    def test_dual_write_must_be_durable_before_unsafe_marker_is_removed(self):
        self.source_and_binding(outcome="NO")
        with patch("bot.paper_adapters.os.fsync", side_effect=[None, None, OSError("disk flush failed")]):
            with self.assertRaisesRegex(OSError, "disk flush failed"):
                self.resolve()
        self.assertTrue((self.paper / "composed_settlement_unsafe.json").exists())
        self.assertFalse(self.reload().risk.check_trade({"question": "Synthetic?"}, 1.0).approved)


if __name__ == "__main__":
    unittest.main()
