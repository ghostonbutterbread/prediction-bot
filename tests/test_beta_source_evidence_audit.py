"""Offline source-evidence regressions through actual promotion/runtime boundaries."""
import copy
import json
import os
import socket
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.auto_source_router_promotion import auto_populate_source_router_history
from bot.collector_paths import COLLECTOR_ROOT_ENV
from bot.paper_shadow_lanes import _LaneDefinition, _source_router_decision
from bot.prediction_lab import PredictionLab
from bot.shared_market_feed import normalize_shared_candidate_input
from bot.weather.source_observation_ledger import is_eligible_for_future_history


def _collector_row(day, *, observed=None, source_as_of=None, source_patch=None):
    observed = observed or f"{day.isoformat()}T12:00:00+00:00"
    source_as_of = source_as_of or f"{day.isoformat()}T11:54:00+00:00"
    market_id = "KXHIGHSEA-" + day.strftime("%y%b%d").upper() + "-T70"
    question = f"Will Seattle high temperature be above 70 degrees on {day:%B %d, %Y}?"
    source = {
        "source_id": "nws", "source_name": "NWS", "source_location_city": "Seattle",
        "forecast_measurement_kind": "high", "contract_shape": "tail", "question_side": "above",
        "forecast_high": 75.0, "source_as_of": source_as_of,
        "source_evidence_version": 1, "evidence_type": "forecast", "scoreable_forecast": True,
        "target_mapping": {"market_target_date": day.isoformat(), "source_target_date": day.isoformat(),
                           "source_timezone": "America/Los_Angeles"},
    }
    source.update(source_patch or {})
    metadata = {
        "market_group": "weather", "series": "daily_temperature", "event_ticker": market_id.rsplit("-", 1)[0],
        "city": "Seattle", "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "tail",
        "market_date": day.isoformat(),
    }
    market = SimpleNamespace(
        id=market_id, question=question, exchange="kalshi", category="weather", yes_price=0.42,
        no_price=0.58, volume=1200, liquidity=1200, closes_at=None, metadata=metadata,
    )
    artifact = {"source_context": {"source": "provided", "mode": "prediction_lab", "as_of": source_as_of,
        "data": {"market_metadata": metadata, "weather_source_snapshot": {
            "market_id": market_id, "question": question, "market_date": day.isoformat(),
            "station_resolution": {"city_id": "seattle_wa", "city": "Seattle"}, "sources": [source],
        }}}}
    # Use the real collector serializer without acquiring providers/accounts.
    lab = PredictionLab.__new__(PredictionLab)
    lab.config = {}
    lab.collector_interval_seconds = 30
    lab.paper_lab_mode = "observer"
    lab.opportunity_bankroll_usd = 0
    lab.hypothetical_mode = "none"
    lab.fresh_wallet_bankroll_usd = 0
    lab.observer_mode = True
    lab.mode = "collector"
    return lab._build_market_snapshot_row(
        "run-" + day.isoformat(), market, {"direction": "SKIP", "confidence": 0.8, "edge": 0.1},
        decision_type="skip", prediction_recorded=False, decision_artifact=artifact, observed_at=observed,
    )


def _source(row):
    return row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]


def _read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class BetaSourceEvidenceAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(self.root / "collector")})
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def _promote(self, rows, *, name="history", settlement=None):
        root = self.root / name
        root.mkdir()
        archive, outcomes = root / "collector.jsonl", root / "resolutions.jsonl"
        resolutions = []
        for row in rows:
            target = datetime.fromisoformat(_source(row)["target_mapping"]["market_target_date"])
            resolutions.append({
                "market_id": row["market_id"], "requested_market_id": row["market_id"],
                "returned_market_id": row["market_id"], "market_status": "finalized", "kalshi_result": "yes",
                "settlement_ts": settlement or (target + timedelta(days=1)).replace(tzinfo=timezone.utc).isoformat(),
                "resolution_id": "resolution-" + row["market_id"],
            })
        archive.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        outcomes.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in resolutions))
        before = archive.read_bytes(), outcomes.read_bytes()
        result = auto_populate_source_router_history(
            collector_snapshots_path=archive, strict_resolutions_path=outcomes,
            output_root=self.root / "collector" / "data" / "derived_reports" / name,
        )
        self.assertEqual((archive.read_bytes(), outcomes.read_bytes()), before)
        return result

    def _runtime(self, history, row):
        normalized = normalize_shared_candidate_input(row)
        self.assertTrue(normalized.ok, normalized.reason_code)
        signal = dict(normalized.signal)
        signal["decision_artifact"] = copy.deepcopy(row["decision_artifact"])
        # Isolate source qualification from the separately owned cutoff adapter.
        signal["observed_at"] = row["observed_at"]
        return _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router",
                            parameters={"scoreboard_path": str(history.scoreboard_path)}),
            signal, None, normalized.shared_candidate,
        )

    def test_strict_history_requires_capture_before_settlement(self):
        settlement = "2026-08-07T00:00:00+00:00"
        for name, observed, eligible in (
            ("before", "2026-08-06T23:59:59+00:00", 1),
            ("equal", settlement, 0),
            ("after", "2026-08-07T00:00:01+00:00", 0),
        ):
            with self.subTest(name=name):
                row = _collector_row(date(2026, 8, 6), observed=observed, source_as_of=observed)
                history = self._promote([row], name=name, settlement=settlement)
                self.assertEqual(history.counts["eligible"], eligible)
                ledger = _read_rows(history.history_path)
                if eligible:
                    [settled] = ledger
                    self.assertFalse(is_eligible_for_future_history(settled, settlement))
                    self.assertTrue(is_eligible_for_future_history(settled, "2026-08-07T00:00:01+00:00"))
                else:
                    self.assertEqual(ledger, [])
                    [unusable] = _read_rows(history.history_path.with_name("unsettled_or_unusable_source_observations.jsonl"))
                    self.assertEqual(unusable["disposition_reason"], "input_observed_at_not_before_settlement")
                    self.assertEqual(unusable["input_observed_at"], observed)
                    self.assertEqual(datetime.fromisoformat(unusable["settlement_ts"]), datetime.fromisoformat(settlement))
                    self.assertFalse(unusable.get("eligible_for_source_history", False))

    def test_future_history_helper_rechecks_source_capture_chronology(self):
        history = self._promote([_collector_row(date(2026, 1, 1))])
        [settled] = _read_rows(history.history_path)
        for mutation in (
            {"input_observed_at": "2026-08-05T12:00:00Z"},
            {"source_as_of": "2026-01-01T12:00:01Z"},
            {"source_as_of": None},
        ):
            with self.subTest(mutation=mutation):
                self.assertFalse(is_eligible_for_future_history(
                    {**settled, **mutation}, "2026-08-04T12:00:00Z",
                ))

    def test_future_source_is_retained_unusable_by_actual_promotion(self):
        for field in ("source_as_of", "source_fetched_at"):
            with self.subTest(field=field):
                row = _collector_row(date(2026, 1, 1), source_patch={field: "2026-01-01T12:00:01Z"})
                history = self._promote([row], name=field)
                self.assertEqual(history.counts["eligible"], 0)
                self.assertEqual(_read_rows(history.history_path), [])
                [unusable] = _read_rows(history.history_path.with_name("unsettled_or_unusable_source_observations.jsonl"))
                self.assertEqual(unusable["disposition_reason"], f"{field}_after_immutable_observed_at")
                self.assertEqual(unusable[field], _source(row)[field])

    def test_post_settlement_history_cannot_supply_runtime_votes(self):
        rows = [_collector_row(date(2026, 1, 1) + timedelta(days=i),
                               observed="2026-08-05T12:00:00Z", source_as_of="2026-08-05T11:54:00Z")
                for i in range(100)]
        history = self._promote(rows)
        self.assertEqual(history.counts["eligible"], 0)
        self.assertEqual(len(_read_rows(history.history_path.with_name(
            "unsettled_or_unusable_source_observations.jsonl"))), 100)
        candidate = _collector_row(date(2026, 8, 6), observed="2026-08-04T12:00:00Z",
                                   source_as_of="2026-08-04T11:54:00Z")
        decision = self._runtime(history, candidate)
        self.assertEqual(decision["action"], "SKIP")
        self.assertEqual(decision["source_router"]["sources_used"], [])

    def test_present_source_period_dates_must_agree_with_target_proof(self):
        candidate = _collector_row(date(2026, 8, 6), observed="2026-08-05T12:00:00Z",
                                   source_as_of="2026-08-05T11:54:00Z")
        for name, mutation, eligible in (
            ("no_optional_period", {}, True),
            ("exact_day", {"forecast_start": "2026-08-06T06:00:00-07:00",
                           "forecast_end": "2026-08-06T18:00:00-07:00"}, True),
            ("source_local_naive", {"forecast_start": "2026-08-06T00:00:00",
                                    "forecast_end": "2026-08-06T23:00:00"}, True),
            ("overnight", {"forecast_start": "2026-08-06T18:00:00-07:00",
                           "forecast_end": "2026-08-07T06:00:00-07:00"}, True),
            ("utc_on_target_locally", {"forecast_start": "2026-08-07T01:00:00Z",
                                       "forecast_end": "2026-08-07T06:00:00Z"}, True),
            ("wrong_date", {"forecast_start": "2026-08-05T06:00:00-07:00",
                            "forecast_end": "2026-08-05T18:00:00-07:00"}, False),
            ("utc_previous_day_locally", {"forecast_start": "2026-08-06T01:00:00Z",
                                          "forecast_end": "2026-08-06T06:00:00Z"}, False),
            ("wrong_end", {"forecast_end": "2026-08-05T18:00:00-07:00"}, False),
            ("conflicting_alias", {"forecast_start": "2026-08-06T06:00:00-07:00",
                                   "forecast_period_start": "2026-08-05T06:00:00-07:00"}, False),
            ("mapping_only_wrong", {"target_mapping": {
                **_source(candidate)["target_mapping"], "source_period_start": "2026-08-05T06:00:00-07:00",
                "source_period_end": "2026-08-05T18:00:00-07:00",
            }}, False),
        ):
            with self.subTest(name=name):
                row = copy.deepcopy(candidate)
                _source(row).update(mutation)
                history = self._promote([row], name=name)
                self.assertEqual(history.counts["eligible"], int(eligible))
                [pending] = _read_rows(history.history_path.with_name("pending_source_observations.jsonl"))
                if eligible:
                    self.assertEqual(pending["strict_source_proof"]["status"], "eligible")
                else:
                    self.assertEqual(_read_rows(history.history_path), [])
                    [unusable] = _read_rows(history.history_path.with_name("unsettled_or_unusable_source_observations.jsonl"))
                    self.assertEqual(unusable["disposition_reason"], "unusable_source_period_target_mismatch")
                    self.assertIn("source_period_target_mismatch", pending["strict_source_proof"]["reasons"])

    def test_runtime_qualifies_explicit_source_evidence_before_voting(self):
        rows = [_collector_row(date(2026, 1, 1) + timedelta(days=i)) for i in range(100)]
        history = self._promote(rows)
        self.assertEqual(history.counts["eligible"], 100)
        candidate = _collector_row(date(2026, 8, 6), observed="2026-08-05T12:00:00+00:00",
                                   source_as_of="2026-08-05T11:54:00+00:00")
        self.assertEqual(self._runtime(history, candidate)["action"], "BUY_YES")
        cases = {
            "observation": ({"evidence_type": "observation", "scoreable_forecast": False},
                            "unusable_v1_forecast_not_scoreable"),
            "unavailable": ({"evidence_type": "forecast_unavailable", "scoreable_forecast": False,
                             "availability_reason": "target_period_missing"}, "unusable_v1_forecast_not_scoreable"),
            "wrong_target": ({"target_mapping": {"market_target_date": "2026-08-06",
                              "source_target_date": "2026-08-05", "source_timezone": "America/Los_Angeles"}},
                             "unusable_v1_target_mismatch"),
            "future_source": ({"source_as_of": "2026-08-07T00:00:00Z"},
                              "source_as_of_after_immutable_observed_at"),
            "future_capture": ({"source_fetched_at": "2026-08-07T00:00:00Z"},
                               "source_fetched_at_after_immutable_observed_at"),
            "wrong_period": ({"forecast_start": "2026-08-05T06:00:00-07:00",
                              "forecast_end": "2026-08-05T18:00:00-07:00"},
                             "unusable_source_period_target_mismatch"),
        }
        for name, (mutation, reason) in cases.items():
            with self.subTest(name=name):
                invalid = copy.deepcopy(candidate)
                _source(invalid).update(mutation)
                before = copy.deepcopy(invalid)
                decision = self._runtime(history, invalid)
                self.assertEqual(decision["action"], "SKIP")
                router = decision["source_router"]
                self.assertEqual(router["sources_used"], [])
                self.assertEqual(router["sources_excluded"][0]["reason_code"], reason)
                [observation] = router["source_observations"]
                self.assertEqual(observation["source_id"], "nws")
                self.assertIsNone(observation["forecast_temp_f"])
                self.assertEqual(observation["evidence_type"], _source(invalid)["evidence_type"])
                self.assertEqual(invalid, before)

        # Missing optional modern enrichment does not disable legacy runtime data.
        legacy = copy.deepcopy(candidate)
        for key in ("source_evidence_version", "evidence_type", "scoreable_forecast", "target_mapping", "source_as_of"):
            _source(legacy).pop(key)
        self.assertEqual(self._runtime(history, legacy)["action"], "BUY_YES")


if __name__ == "__main__":
    unittest.main()
