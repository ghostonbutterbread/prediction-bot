"""Offline real producer→consumer regressions for beta handoff contracts.

Only external transport is blocked. Collector serialization, replay/index,
strict history promotion, paper lane writing and Kelly consumption are real.
All artifacts live in temporary directories; no runtime/account state is used.
"""
import json
import os
import socket
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bot.auto_source_router_promotion import auto_populate_source_router_history
from bot.collector_lane_replay_kelly import replay_recorded_collector_lane_with_kelly
from bot.collector_replay_index import build_collector_replay_index, load_indexed_collector_rows
from bot.paper_evaluator_input import _build_signal_from_normalized_candidate
from bot.paper_shadow_lanes import build_paper_shadow_lane_resolution_rows, write_paper_shadow_lane_decisions
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.scoreboard_resolution_backfill import backfill_scoreboard_resolutions
from bot.shared_market_feed import normalize_shared_candidate_input


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _collector_row(day=date(2026, 8, 6), *, observed_at=None):
    """Exercise the pure collector serializer, never its account-owning init."""
    lab = PredictionLab.__new__(PredictionLab)
    lab.config = {}
    lab.collector_interval_seconds = 30
    lab.paper_lab_mode = "observer"
    lab.opportunity_bankroll_usd = lab.fresh_wallet_bankroll_usd = 0
    lab.hypothetical_mode = "none"
    lab.observer_mode = True
    lab.mode = "collector"
    observed_at = observed_at or f"{day.isoformat()}T12:00:00+00:00"
    source_as_of = (datetime.fromisoformat(observed_at) - timedelta(minutes=6)).isoformat()
    market_id = "KXHIGHSEA-" + day.strftime("%y%b%d").upper() + "-T70"
    question = f"Will Seattle high temperature be above 70 degrees on {day.strftime('%B %d, %Y')}?"
    source = {
        "source_id": "nws", "source_name": "NWS", "source_location_city": "Seattle",
        "forecast_measurement_kind": "high", "contract_shape": "tail", "question_side": "above",
        "forecast_high": 75.0, "source_as_of": source_as_of,
        "source_evidence_version": 1, "evidence_type": "forecast", "scoreable_forecast": True,
        "target_mapping": {"market_target_date": day.isoformat(), "source_target_date": day.isoformat(),
                           "source_timezone": "America/Los_Angeles"},
    }
    metadata = {
        "market_group": "weather", "series": "daily_temperature", "event_ticker": market_id.rsplit("-", 1)[0],
        "city": "Seattle", "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "tail",
        "market_date": day.isoformat(),
    }
    market = SimpleNamespace(id=market_id, question=question, exchange="kalshi", category="weather",
                             yes_price=0.42, no_price=0.58, volume=1200, liquidity=1200,
                             closes_at=None, metadata=metadata)
    artifact = {"source_context": {"source": "provided", "mode": "prediction_lab", "as_of": source_as_of,
                "data": {"market_metadata": metadata, "weather_source_snapshot": {
                    "market_id": market_id, "question": question, "market_date": day.isoformat(),
                    "station_resolution": {"city_id": "seattle_wa", "city": "Seattle"}, "sources": [source]}}}}
    return lab._build_market_snapshot_row(
        "run-" + day.isoformat(), market,
        {"direction": "SKIP", "confidence": 0.8, "edge": 0.1}, decision_type="skip",
        prediction_recorded=False, decision_artifact=artifact, observed_at=observed_at,
    )


class BetaHandoffIdentityAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="beta-handoff-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        from bot.collector_paths import COLLECTOR_ROOT_ENV
        self.enterContext(patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(self.root)}))
        self.network = self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.addCleanup(self.network.assert_not_called)

    def _history(self):
        rows = [_collector_row(date(2026, 1, 1) + timedelta(days=i)) for i in range(100)]
        raw, index, manifest = [self.root / name for name in ("collector.jsonl", "index.jsonl", "manifest.json")]
        _write_rows(raw, rows)
        build_collector_replay_index(raw, index, manifest)
        self.assertEqual(list(load_indexed_collector_rows(index, manifest)), rows)
        responses = {}
        for row in rows:
            day = date.fromisoformat(row["observed_at"][:10])
            responses[row["market_id"]] = {
                "ticker": row["market_id"], "status": "finalized", "result": "yes",
                "settlement_ts": f"{(day + timedelta(days=1)).isoformat()}T00:00:00+00:00",
            }
        resolved = backfill_scoreboard_resolutions(
            [raw], output_path=self.root / "resolutions.jsonl", fetch_market=responses.__getitem__,
            request_interval_seconds=0, retry_delay_seconds=0, max_fetch_attempts=1,
            fetched_at="2026-08-07T00:00:00Z",
        )
        history = auto_populate_source_router_history(
            collector_snapshots_path=raw, strict_resolutions_path=resolved.output_path,
            output_root=self.root / "data" / "derived_reports" / "history",
        )
        self.assertEqual(history.counts["eligible"], 100)
        return history

    def _write_lane(self, row, *, history=None, adapter="paper", signal_updates=None):
        normalized = normalize_shared_candidate_input(row)
        self.assertTrue(normalized.ok, normalized.reason_code)
        assert normalized.shared_candidate is not None
        assert normalized.shared_candidate_id is not None
        path = self.root / "candidate.jsonl"
        _write_rows(path, [row])
        signal = (
            _build_signal_from_normalized_candidate(
                normalized.shared_candidate, row, shared_candidate_id=normalized.shared_candidate_id,
                candidate_dataset_path=path,
            ) if adapter == "paper" else dict(normalized.signal)
        )
        signal.update(signal_updates or {})
        lane_id = "shadow_source_router" if history else "control_stable"
        config = {"paper_shadow_lanes": {"enabled": True, "enabled_lanes": [lane_id]}}
        if history:
            config["paper_shadow_lanes"][lane_id] = {"parameters": {"scoreboard_path": str(history.scoreboard_path)}}
        result = write_paper_shadow_lane_decisions(
            config=config, candidate_dataset_path=path,
            inputs_by_shared_candidate_id={normalized.shared_candidate_id: {"fixture": SimpleNamespace(
                signal=signal, shared_candidate=normalized.shared_candidate)}},
            wallet_decision_rows={}, wallet_runs={}, ledger_root=self.root / f"lane-{adapter}",
        )
        return _read_rows(result.decision_path)[-1]

    def test_canonical_candidate_time_reaches_strict_router_cutoff(self):
        history = self._history()
        row = _collector_row(observed_at="2026-08-05T12:00:00+00:00")
        for adapter in ("paper", "generic"):
            with self.subTest(adapter=adapter):
                receipt = self._write_lane(row, history=history, adapter=adapter)
                self.assertEqual(receipt["action"], "BUY_YES", receipt["reason_code"])
                self.assertEqual(receipt["observed_at"], row["observed_at"])

    def test_collector_snapshot_identity_survives_both_feed_receipts(self):
        row = _collector_row()
        for adapter in ("paper", "generic"):
            with self.subTest(adapter=adapter):
                receipt = self._write_lane(row, adapter=adapter)
                self.assertEqual(receipt.get("shared_snapshot_id"), row["shared_snapshot_id"])
                self.assertEqual(receipt["shared_candidate"]["shared_snapshot_id"], row["shared_snapshot_id"])
                self.assertEqual(receipt["shared_candidate_id"], row["shared_candidate_id"])

    def test_explicit_snapshot_identity_is_not_replaced_with_run_id(self):
        row = _collector_row()
        row["shared_snapshot_id"] = "recorded-snapshot-distinct-from-run"
        raw, index, manifest = [self.root / name for name in ("raw.jsonl", "index.jsonl", "manifest.json")]
        _write_rows(raw, [row])
        build_collector_replay_index(raw, index, manifest)
        replay = build_replay_decision_input_v1(row)
        self.assertTrue(replay.ok, replay.errors)
        assert replay.record is not None
        self.assertEqual(_read_rows(index)[0]["shared_snapshot_id"], replay.record["shared_snapshot_id"])
        self.assertEqual(list(load_indexed_collector_rows(index, manifest)), [row])
        for adapter in ("paper", "generic"):
            receipt = self._write_lane(row, adapter=adapter)
            self.assertEqual(receipt["shared_snapshot_id"], row["shared_snapshot_id"])

    def test_explicit_identity_conflicts_are_rejected_at_every_handoff(self):
        from bot.paper_evaluator_input import _normalize_shared_candidate_row
        for field in ("shared_candidate_id", "shared_snapshot_id", "market_id", "run_id"):
            row = _collector_row()
            if field == "shared_snapshot_id":
                row["shared_candidate"][field] = "wrong-snapshot"
            else:
                row[field] = "wrong-identity"
            for consumer in ("generic", "paper", "replay", "index"):
                with self.subTest(field=field, consumer=consumer):
                    if consumer == "generic":
                        result = normalize_shared_candidate_input(row)
                        self.assertFalse(result.ok)
                        self.assertEqual(result.reason_code, field + "_mismatch")
                    elif consumer == "paper":
                        result, skip = _normalize_shared_candidate_row(0, row)
                        self.assertIsNone(result)
                        assert skip is not None
                        self.assertEqual(skip.reason_code, field + "_mismatch")
                    elif consumer == "replay":
                        result = build_replay_decision_input_v1(row)
                        self.assertFalse(result.ok)
                        self.assertIn(field + "_mismatch", [error.code for error in result.errors])
                    else:
                        raw, index, manifest = [self.root / field / name for name in ("raw.jsonl", "index.jsonl", "manifest.json")]
                        _write_rows(raw, [row])
                        result = build_collector_replay_index(raw, index, manifest)
                        self.assertEqual(result["indexed_rows"], 0)
                        self.assertEqual(result["invalid_rows"], 1)
                        self.assertEqual(list(load_indexed_collector_rows(index, manifest)), [])

    def _frozen_decision(self):
        return {
            "decision_id": "decision-A", "run_id": "run-A", "shared_snapshot_id": "snapshot-A",
            "shared_candidate_id": "candidate-A", "market_id": "market-A",
            "policy": "shadow_source_scoreboard", "observed_at": "2026-09-07T12:00:00+00:00",
            "action": "BUY_YES", "entry_price": 0.4, "approved_position_size_usd": 10.0,
            "model_probability": 0.7,
        }

    def _independent_resolution(self) -> dict[str, Any]:
        return {
            "shared_candidate_id": "candidate-A", "run_id": "run-A", "shared_snapshot_id": "snapshot-A",
            "market_id": "market-A", "outcome": "YES", "resolution_id": "resolution-A",
            "settlement_ts": "2026-09-08T12:00:00+00:00", "resolved_at": "2026-09-10T12:00:00+00:00",
        }

    def test_shadow_resolution_rejects_supplied_identity_conflicts(self):
        decision = self._frozen_decision()
        for field in ("market_id", "run_id", "shared_candidate_id", "shared_snapshot_id"):
            for location in ("root", "nested"):
                with self.subTest(field=field, location=location):
                    resolution = self._independent_resolution()
                    target = resolution if location == "root" else resolution.setdefault("resolution", {})
                    target[field] = "wrong-identity"
                    [receipt] = build_paper_shadow_lane_resolution_rows(lane_rows=[decision], resolution_rows=[resolution])
                    self.assertEqual(receipt["blocker"], "resolution_identity_mismatch")
                    self.assertIsNone(receipt["pnl"])
                    self.assertFalse(receipt["resolution"]["matched"])
                    wallet = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=[receipt])
                    self.assertEqual(wallet["summary"]["settled_positions"], 0)
                    self.assertEqual(wallet["summary"]["open_positions"], 1)

    def test_real_shadow_receipt_preserves_authoritative_time_for_kelly(self):
        for action in ("BUY_YES", "BUY_NO"):
            for outcome in ("YES", "NO", "VOID"):
                for location, timestamp_key in (("root", "settlement_ts"), ("root", "outcome_known_at"),
                                                ("nested", "settlement_ts"), ("nested", "outcome_known_at")):
                    with self.subTest(action=action, outcome=outcome, location=location, timestamp_key=timestamp_key):
                        decision = self._frozen_decision()
                        decision.update(action=action, model_probability=0.7 if action == "BUY_YES" else 0.3)
                        resolution = self._independent_resolution()
                        resolution["outcome"] = outcome
                        timestamp = resolution.pop("settlement_ts")
                        target = resolution if location == "root" else resolution.setdefault("resolution", {})
                        target[timestamp_key] = timestamp
                        source_path = self.root / "independent.jsonl"
                        _write_rows(source_path, [resolution])
                        receipts = build_paper_shadow_lane_resolution_rows(lane_rows=[decision], resolution_path=source_path)
                        # Consume serialized producer output verbatim, never repair it in the fixture.
                        receipt_path = self.root / "receipts.jsonl"
                        _write_rows(receipt_path, receipts)
                        [receipt] = _read_rows(receipt_path)
                        self.assertEqual(receipt["resolution"].get("settlement_ts"), timestamp)
                        self.assertEqual(receipt["resolution"].get(timestamp_key), timestamp)
                        self.assertEqual(receipt["resolution"].get("run_id"), decision["run_id"])
                        wallet = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=[receipt])
                        self.assertEqual(wallet["summary"]["settled_positions"], 1)
                        self.assertEqual(wallet["summary"]["open_positions"], 0)
                        self.assertEqual(wallet["summary"]["reserved_capital_usd"], 0)
                        self.assertEqual(wallet["settlement_rows"][0]["settlement_ts"], timestamp)
                        self.assertEqual(wallet["settlement_rows"][0]["resolution_receipt"]["resolution_source_path"], str(source_path))
                        if outcome == "VOID":
                            self.assertEqual(receipt["blocker"], "void_resolution")
                            self.assertIsNone(receipt["pnl"])
                            self.assertEqual(wallet["summary"]["available_cash_usd"], 100)
                            self.assertIsNone(wallet["settlement_rows"][0]["pnl_usd"])
                        else:
                            expected_cash = 115 if action == "BUY_" + outcome else 90
                            self.assertEqual(wallet["summary"]["available_cash_usd"], expected_cash)

    def test_explicit_non_later_settlement_is_blocked_before_pnl_and_kelly(self):
        decision = self._frozen_decision()
        for timestamp in ("2026-09-06T12:00:00+00:00", decision["observed_at"]):
            for field in ("settlement_ts", "outcome_known_at"):
                for location in ("root", "nested"):
                    with self.subTest(timestamp=timestamp, field=field, location=location):
                        resolution = self._independent_resolution()
                        # Retain a valid later primary when testing secondary aliases:
                        # it must not conceal an explicit earlier settlement assertion.
                        target = resolution if location == "root" else resolution.setdefault("resolution", {})
                        target[field] = timestamp
                        [receipt] = build_paper_shadow_lane_resolution_rows(lane_rows=[decision], resolution_rows=[resolution])
                        self.assertEqual(receipt["blocker"], "settlement_not_after_decision")
                        self.assertIsNone(receipt["pnl"])
                        self.assertFalse(receipt["resolution"]["matched"])
                        wallet = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=[receipt])
                        self.assertEqual(wallet["summary"]["settled_positions"], 0)
                        self.assertEqual(wallet["summary"]["open_positions"], 1)

    def test_legacy_resolution_without_authoritative_time_does_not_infer_kelly_settlement(self):
        decision = self._frozen_decision()
        resolution = self._independent_resolution()
        del resolution["settlement_ts"]
        for key in ("run_id", "shared_snapshot_id"):
            del resolution[key]
        [receipt] = build_paper_shadow_lane_resolution_rows(lane_rows=[decision], resolution_rows=[resolution])
        self.assertIsNone(receipt["blocker"])
        self.assertEqual(receipt["pnl"]["pnl_usd"], 15)
        self.assertIsNone(receipt["resolution"].get("settlement_ts"))
        self.assertIsNone(receipt["resolution"].get("outcome_known_at"))
        wallet = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=[receipt])
        self.assertEqual(wallet["summary"]["settled_positions"], 0)
        self.assertEqual(wallet["summary"]["open_positions"], 1)

    def test_explicit_settlement_requires_usable_recorded_chronology(self):
        for case in ("missing_decision_timestamp", "invalid_settlement_timestamp"):
            with self.subTest(case=case):
                decision = self._frozen_decision()
                resolution = self._independent_resolution()
                if case == "missing_decision_timestamp":
                    del decision["observed_at"]
                else:
                    resolution["settlement_ts"] = "not-a-time"
                [receipt] = build_paper_shadow_lane_resolution_rows(lane_rows=[decision], resolution_rows=[resolution])
                self.assertEqual(receipt["blocker"], case)
                self.assertIsNone(receipt["pnl"])
                self.assertFalse(receipt["resolution"]["matched"])

    def test_missing_candidate_time_never_becomes_wall_clock_history_or_receipt(self):
        history = self._history()
        row = _collector_row()
        row["observed_at"] = row["timestamp"] = row["shared_candidate"]["observed_at"] = None
        from bot.paper_shadow_lanes import _LaneDefinition, _source_router_decision
        normalized = normalize_shared_candidate_input(row)
        decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router",
                            parameters={"scoreboard_path": str(history.scoreboard_path)}),
            normalized.signal, None, normalized.shared_candidate,
        )
        self.assertEqual(decision["action"], "SKIP")
        # The agent ledger requires an observation timestamp. Reject the write
        # rather than invent a timestamp merely to satisfy that downstream schema.
        with self.assertRaisesRegex(ValueError, "missing candidate observed_at"):
            self._write_lane(row, history=history)

    def test_router_explicit_cutoff_precedence_and_settlement_boundary(self):
        history = self._history()
        boundary = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=100)
        later = (boundary + timedelta(seconds=1)).isoformat()
        row = _collector_row(observed_at=later)
        for observed_at, expected in ((boundary.isoformat(), "SKIP"), (later, "BUY_YES"), ("not-a-time", "SKIP")):
            with self.subTest(observed_at=observed_at):
                receipt = self._write_lane(row, history=history, signal_updates={"observed_at": observed_at})
                self.assertEqual(receipt["action"], expected)

    def test_duplicate_candidate_receipts_cannot_hide_explicit_contradictions(self):
        decision = self._frozen_decision()
        for field, value, blocker in (("run_id", "wrong-run", "resolution_identity_mismatch"),
                                      ("settlement_ts", "2026-09-06T12:00:00Z", "settlement_not_after_decision")):
            for reversed_order in (False, True):
                with self.subTest(field=field, reversed_order=reversed_order):
                    good = self._independent_resolution()
                    bad = {**good, field: value}
                    resolutions = [bad, good] if reversed_order else [good, bad]
                    [receipt] = build_paper_shadow_lane_resolution_rows(lane_rows=[decision], resolution_rows=resolutions)
                    self.assertEqual(receipt["blocker"], blocker)
                    self.assertIsNone(receipt["pnl"])
                    self.assertFalse(receipt["resolution"]["matched"])


if __name__ == "__main__":
    unittest.main()
