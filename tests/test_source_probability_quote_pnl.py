"""Synthetic strict-identity tests for the direct source-probability resolver."""
import hashlib
import json
import unittest

from bot.weather.collector_source_router_replay import (
    build_sealed_source_probability_decisions,
    resolve_sealed_source_probability_decisions,
)
from test_collector_source_router_replay import finalized_outcome, replay_input


def builder_record(*, market_id, side, price):
    record = replay_input(market_id=market_id, observed_at="2026-01-01T00:00:00Z",
                          raw_hash="a" * 64, predicted_prob=0.8 if side == "YES" else 0.2)
    record["market"]["best_" + side.lower() + "_ask"] = price
    del record["canonical_input_sha256"]
    record["canonical_input_sha256"] = hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    return record


def seal_digest(decision):
    payload = {key: value for key, value in decision.items() if key != "decision_id"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def fixture(*, side="YES", outcome="YES", price=0.4, stake=1.0, basis="recorded_executable_ask"):
    key = {"shared_snapshot_id": "snapshot", "shared_candidate_id": "candidate",
           "market_id": "SYNTHETIC", "observed_at_utc": "2026-01-01T00:00:00Z",
           "raw_row_sha256": "a" * 64}
    identity = {"market_id": "SYNTHETIC", "decision_key": key, "canonical_input_sha256": "b" * 64}
    decision = {**identity, "decision_id": "synthetic", "side": side, "action": "BUY_" + side,
                "entry_price": price, "fixed_stake_usd": stake, "price_basis": basis}
    resolution = {**identity, "official_outcome": outcome, "market_status": "finalized",
                  "settlement_ts": "2026-01-02T00:00:00Z"}
    return decision, resolution


class SourceProbabilityQuotePnlTests(unittest.TestCase):
    def test_builder_seals_precise_tiny_prices_for_resolution(self):
        for side, outcome, price in (("YES", "YES", 0.0000014), ("YES", "NO", 0.0000004),
                                     ("NO", "NO", 0.0000004), ("NO", "YES", 0.0000014)):
            with self.subTest(side=side, outcome=outcome, price=price):
                record = builder_record(market_id="SYNTHETIC-" + side + outcome, side=side, price=price)
                control, _, _ = build_sealed_source_probability_decisions([record], [])
                decision = json.loads(json.dumps(control[0]))
                self.assertEqual(decision["action"], "BUY_" + side)
                self.assertEqual(decision["entry_price"], price)
                self.assertEqual(decision["decision_id"], seal_digest(decision))
                resolution = finalized_outcome(record, outcome=outcome, settlement_ts="2026-01-02T00:00:00Z")
                report = resolve_sealed_source_probability_decisions([decision], [resolution])
                row = report["resolved_decisions"][0]
                expected = round(1 / price - 1, 6) if side == outcome else -1.0
                self.assertEqual(row["hypothetical_ask_quote_pnl_usd"], expected)
                self.assertEqual(report["summary"]["hypothetical_ask_quote_pnl_total_usd"], expected)
                self.assertEqual(report["economic_status"], "hypothetical_price_proxy_not_fill")
                self.assertEqual(report["source_time_status"], "unverified")
                self.assertFalse(report["diagnostic_eligible"])

    def test_aggregate_rounds_unrounded_builder_position_values(self):
        for basis in ("recorded_executable_ask", "recorded_market_reference"):
            with self.subTest(basis=basis):
                records = [builder_record(market_id=f"SYNTHETIC-{basis}-{i}", side="YES", price=0.6)
                           for i in range(3)]
                if basis == "recorded_market_reference":
                    for record in records:
                        record["market"]["yes_price"] = record["market"].pop("best_yes_ask")
                        del record["canonical_input_sha256"]
                        record["canonical_input_sha256"] = hashlib.sha256(json.dumps(
                            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                        ).encode()).hexdigest()
                outcomes = [finalized_outcome(record, outcome="YES", settlement_ts="2026-01-02T00:00:00Z")
                            for record in records]
                decisions = build_sealed_source_probability_decisions(records, [])[0]
                report = resolve_sealed_source_probability_decisions(json.loads(json.dumps(decisions)), outcomes)
                category = "hypothetical_ask_quote_pnl" if basis == "recorded_executable_ask" else "hypothetical_reference_price_pnl"
                self.assertEqual([row[category + "_usd"] for row in report["resolved_decisions"]], [0.666667] * 3)
                self.assertEqual(report["summary"][category + "_decisions"], 3)
                self.assertEqual(report["summary"][category + "_total_usd"], 2.0)

    def test_precision_seals_and_directional_only_keeps_decision_integrity(self):
        records = [builder_record(market_id="SYNTHETIC-" + str(i), side="YES", price=price)
                   for i, price in enumerate((0.4, 0.4000004))]
        outcomes = [finalized_outcome(record, outcome="YES", settlement_ts="2026-01-02T00:00:00Z")
                    for record in records]
        decisions, _, _ = build_sealed_source_probability_decisions(records, outcomes)
        unchanged, _, _ = build_sealed_source_probability_decisions(records, [])
        self.assertEqual(decisions, unchanged)
        for decision, record in zip(decisions, records):
            self.assertEqual(decision["decision_id"], seal_digest(decision))
            self.assertEqual(decision["entry_price"], record["market"]["best_yes_ask"])
            self.assertNotIn("official_outcome", decision)
        legacy_content = {**decisions[0], "entry_price": round(decisions[0]["entry_price"], 6)}
        self.assertEqual(decisions[0]["decision_id"], seal_digest(legacy_content))
        rounded_content = {**decisions[1], "entry_price": round(decisions[1]["entry_price"], 6)}
        self.assertNotEqual(decisions[1]["decision_id"], seal_digest(rounded_content))
        report = resolve_sealed_source_probability_decisions(json.loads(json.dumps(decisions)), outcomes,
                                                             directional_only=True)
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["economic_status"], "excluded_quote_only")
        self.assertEqual(report["source_time_status"], "unverified")
        self.assertFalse(report["diagnostic_eligible"])
        self.assertFalse(any("pnl" in key for key in report["summary"]))
        self.assertTrue(all(not any("pnl" in key for key in row) for row in report["resolved_decisions"]))

    def test_fixed_dollar_stake_yes_no_wins_losses_at_different_prices(self):
        for side, outcome, price, expected in (("YES", "YES", 0.4, 1.5),
                                                ("YES", "NO", 0.4, -1.0),
                                                ("NO", "NO", 0.25, 3.0),
                                                ("NO", "YES", 0.8, -1.0)):
            with self.subTest(side=side, outcome=outcome, price=price):
                decision, resolution = fixture(side=side, outcome=outcome, price=price)
                report = resolve_sealed_source_probability_decisions([decision], [resolution])
                self.assertEqual(report["summary"]["exactly_resolved_decisions"], 1)
                row = report["resolved_decisions"][0]
                # The fallback makes the pre-fix RED identify the arithmetic defect.
                pnl = row.get("hypothetical_ask_quote_pnl_usd", row.get("executable_pnl_usd"))
                self.assertEqual(pnl, expected)
                self.assertNotIn("executable_pnl_usd", row)
                self.assertEqual(report["fixed_stake_policy"]["fee_model_version"], "none_v1")
                self.assertEqual(report["summary"]["hypothetical_ask_quote_pnl_total_usd"], expected)
                self.assertEqual(report["economic_status"], "hypothetical_price_proxy_not_fill")
        decision, resolution = fixture(price=0.4, stake=2.0)
        self.assertEqual(resolve_sealed_source_probability_decisions([decision], [resolution])
                         ["resolved_decisions"][0]["hypothetical_ask_quote_pnl_usd"], 3.0)

    def test_invalid_price_and_stake_are_not_calculable(self):
        for field, value in (("entry_price", None), ("entry_price", 0), ("entry_price", -0.1),
                             ("entry_price", 1), ("entry_price", 1.1), ("entry_price", float("nan")),
                             ("entry_price", True), ("fixed_stake_usd", None),
                             ("fixed_stake_usd", 0), ("fixed_stake_usd", -1),
                             ("fixed_stake_usd", float("inf")), ("fixed_stake_usd", True)):
            with self.subTest(field=field, value=value):
                decision, resolution = fixture()
                decision[field] = value
                row = resolve_sealed_source_probability_decisions([decision], [resolution])["resolved_decisions"][0]
                self.assertIsNone(row["pnl_category"])
                self.assertIsNone(row.get("hypothetical_ask_quote_pnl_usd", row.get("executable_pnl_usd")))
        for field in ("entry_price", "fixed_stake_usd"):
            decision, resolution = fixture()
            del decision[field]
            row = resolve_sealed_source_probability_decisions([decision], [resolution])["resolved_decisions"][0]
            self.assertIsNone(row["pnl_category"])
        decision, resolution = fixture()
        decision["action"] = "BUY_NO"
        self.assertIsNone(resolve_sealed_source_probability_decisions([decision], [resolution])
                          ["resolved_decisions"][0]["pnl_category"])

    def test_reference_proxy_is_hypothetical_not_fill(self):
        decision, resolution = fixture(basis="recorded_market_reference")
        report = resolve_sealed_source_probability_decisions([decision], [resolution])
        row = report["resolved_decisions"][0]
        self.assertEqual(row.get("hypothetical_reference_price_pnl_usd", row.get("reference_price_proxy_pnl_usd")), 1.5)
        self.assertNotIn("reference_price_proxy_pnl_usd", row)
        self.assertEqual(report["schema_version"], 5)
        self.assertIn("not", report["pnl_semantics"]["hypothetical_ask_quote_pnl"].lower())
        self.assertEqual(report["summary"]["hypothetical_reference_price_pnl_total_usd"], 1.5)

    def test_directional_only_remains_pnl_free(self):
        decision, resolution = fixture()
        report = resolve_sealed_source_probability_decisions([decision], [resolution], directional_only=True)
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["economic_status"], "excluded_quote_only")
        self.assertFalse(any("pnl" in key for key in report["resolved_decisions"][0]))
        self.assertFalse(any("pnl" in key for key in report["summary"]))
