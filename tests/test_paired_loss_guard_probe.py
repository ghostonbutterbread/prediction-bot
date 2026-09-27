"""End-to-end synthetic receipts for the fixed-locator, recorded-decision probe."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.paired_loss_guard_probe import build_paired_loss_guard_probe

ROOT = Path(__file__).resolve().parents[1]


def _row(market, action="BUY_YES", probability=.70, yes_ask=.40, no_ask=.60, event="EV-1"):
    return {
        "market_id": market, "shared_snapshot_id": "snapshot-1",
        "shared_candidate_id": f"candidate-{market}", "observed_at": "2026-08-01T12:00:00Z",
        "main_decision": {"action": action},
        "decision_artifact": {
            "strategy_signal": {"model_probability": probability},
            "order_book_snapshot": {"source": "book", "data": {"best_yes_ask": yes_ask, "best_no_ask": no_ask}},
            "execution_snapshot": {"estimated_fill_price": yes_ask},
        },
        "shared_candidate": {
            "candidate_id": f"candidate-{market}", "market_id": market,
            "shared_snapshot_id": "snapshot-1", "observed_at": "2026-08-01T12:00:00Z",
            "market": {"event_ticker": event},
            "prices": {"order_book_snapshot": {
                "source": "book", "market_id": market,
                "observed_at": "2026-08-01T11:59:59Z",
                "data": {"best_yes_ask": yes_ask, "best_no_ask": no_ask},
            }},
        },
    }


def _resolution(market, outcome, **changes):
    receipt = {
        "market_id": market, "requested_market_id": market, "returned_market_id": market,
        "market_status": "finalized", "resolution_id": f"resolution-{market}",
        "settlement_ts": "2026-08-02T12:00:00Z", "kalshi_result": outcome.lower(),
    }
    receipt.update(changes)
    return receipt


class PairedLossGuardProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "immutable_raw.jsonl"
        self.manifest = self.base / "fixed_probe.json"
        self.resolutions = self.base / "strict_resolutions.jsonl"
        self.output = ROOT / "data" / "derived_reports" / f"paired-loss-test-{self.base.name}"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.output, ignore_errors=True)
        self.temp.cleanup()

    def fixture(self, rows, resolutions, *, corrupt=None):
        locators = []
        with self.source.open("wb") as stream:
            for number, row in enumerate(rows, 1):
                raw = (json.dumps(row, sort_keys=True) + "\n").encode()
                locators.append({
                    "row_number": number, "byte_offset": stream.tell(), "byte_length": len(raw),
                    "payload_sha256": hashlib.sha256(raw).hexdigest(),
                    **{key: row[key] for key in ("market_id", "shared_snapshot_id", "shared_candidate_id", "observed_at")},
                })
                stream.write(raw)
        if corrupt:
            locators[0].update(corrupt)
        self.manifest.write_text(json.dumps({
            "schema_name": "paired_loss_guard_probe", "schema_version": 1,
            "source_path": str(self.source.resolve()),
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "locators": locators,
        }))
        self.resolutions.write_text("".join(json.dumps(r) + "\n" for r in resolutions))

    def run_probe(self, **settings):
        return build_paired_loss_guard_probe(
            probe_manifest_path=self.manifest, strict_resolutions_path=self.resolutions,
            output_dir=self.output, **settings,
        )

    def test_paired_same_market_counts_void_unresolved_and_proxy(self):
        rows = [
            _row("win", probability=.8),
            _row("loss", probability=.43),
            _row("no-win", action="BUY_NO", probability=.20, no_ask=.45),
            _row("void", probability=.80),
            _row("unresolved", probability=.8),
            _row("missing-ask", probability=.8, yes_ask=0),
            _row("missing-prob", probability=None),
        ]
        receipts = [_resolution("win", "YES"), _resolution("loss", "NO"),
                    _resolution("no-win", "NO"), _resolution("void", "VOID"),
                    _resolution("missing-ask", "NO"), _resolution("missing-prob", "YES")]
        self.fixture(rows, receipts)
        report = self.run_probe()
        self.assertEqual(report["counts"]["rows"], 7)
        self.assertEqual(report["counts"]["unresolved"], 1)
        self.assertEqual(report["counts"]["void"], 1)
        self.assertEqual(report["counts"]["missing_quote"], 1)
        self.assertEqual(report["counts"]["missing_probability"], 1)
        self.assertEqual(report["control"]["winning_buys"], 3)
        self.assertEqual(report["control"]["losing_buys"], 2)
        self.assertEqual(report["candidate"]["winning_buys"], 2)
        self.assertEqual(report["candidate"]["losing_buys"], 0)
        self.assertAlmostEqual(report["win_retention"], 2 / 3)
        self.assertEqual(report["candidate"]["proxy_pnl_usd"], 27.2222)
        self.assertEqual(report["events"]["EV-1"]["markets"], 7)
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["paper_parity_claim"])
        self.assertTrue(report["non_mutating"])
        sealed = [json.loads(line) for line in (self.output / "sealed_decisions.jsonl").read_text().splitlines()]
        paired = [json.loads(line) for line in (self.output / "paired_rows.jsonl").read_text().splitlines()]
        self.assertEqual(len(sealed), len(paired))
        self.assertTrue(all("outcome" not in r and "resolution" not in r for r in sealed))
        self.assertEqual(paired[3]["blocker"], "void_resolution")
        self.assertEqual(paired[4]["blocker"], "missing_resolution")
        self.assertEqual(paired[0]["resolution"]["source_path"], str(self.resolutions.resolve()))
        self.assertEqual(paired[0]["resolution"]["raw_row_sha256"], hashlib.sha256((json.dumps(receipts[0]) + "\n").encode()).hexdigest())
        self.assertEqual(report["inputs"]["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(report["inputs"]["probe_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(report["inputs"]["resolution_sha256"], hashlib.sha256(self.resolutions.read_bytes()).hexdigest())

    def test_loss_guard_uses_recorded_worse_fill_and_minimum_side_ask(self):
        costly = _row("costly", probability=.80, yes_ask=.40)
        costly["decision_artifact"]["execution_snapshot"]["estimated_fill_price"] = .78
        cheap = _row("cheap", probability=.90, yes_ask=.19)
        self.fixture([costly, cheap], [_resolution("costly", "YES"), _resolution("cheap", "YES")])
        report = self.run_probe()
        self.assertEqual(report["control"]["winning_buys"], 2)
        self.assertEqual(report["candidate"]["winning_buys"], 0)
        sealed = [json.loads(line) for line in (self.output / "sealed_decisions.jsonl").read_text().splitlines()]
        self.assertEqual(sealed[0]["candidate_reason_code"], "fee_aware_edge_below_floor")
        self.assertEqual(sealed[0]["payout_aware"]["price_basis"], "recorded_fill_estimate")
        self.assertEqual(sealed[1]["candidate_reason_code"], "executable_side_ask_below_floor")

    def test_outcome_like_fields_never_enter_sealed_decision_or_gate(self):
        row = _row("leak", probability=.8)
        row["outcome"] = "NO"
        row["decision_artifact"]["strategy_signal"]["future_pnl_inputs"] = {"resolved_outcome": "NO"}
        row["decision_artifact"]["order_book_snapshot"]["data"]["resolved_at"] = "2030-01-01"
        self.fixture([row], [_resolution("leak", "YES")])
        self.run_probe()
        sealed_bytes = (self.output / "sealed_decisions.jsonl").read_text()
        self.assertNotIn("NO", sealed_bytes)
        self.assertNotIn("resolved_at", sealed_bytes)
        self.assertEqual(json.loads(sealed_bytes)["candidate_action"], "BUY_YES")

    def test_bad_locator_or_source_hash_fails_before_output(self):
        self.fixture([_row("one")], [_resolution("one", "YES")], corrupt={"row_number": 2})
        with self.assertRaisesRegex(ValueError, "locator"):
            self.run_probe()
        self.assertFalse(self.output.exists())
        self.fixture([_row("one")], [_resolution("one", "YES")], corrupt={"shared_candidate_id": "wrong"})
        with self.assertRaisesRegex(ValueError, "identity"):
            self.run_probe()
        self.assertFalse(self.output.exists())
        self.fixture([_row("one")], [_resolution("one", "YES")])
        self.source.write_bytes(self.source.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "source sha256"):
            self.run_probe()
        self.assertFalse(self.output.exists())

    def test_conflicting_or_mismatched_resolutions_remain_unresolved(self):
        self.fixture([_row("conflict"), _row("mismatch"), _row("early"), _row("retrieval")], [
            _resolution("conflict", "YES"), _resolution("conflict", "NO"),
            _resolution("mismatch", "YES", returned_market_id="other"),
            _resolution("early", "YES", settlement_ts="2026-08-01T11:00:00Z"),
            {key: value for key, value in _resolution("retrieval", "YES", resolved_at="2026-08-03T00:00:00Z").items() if key != "settlement_ts"},
        ])
        report = self.run_probe()
        self.assertEqual(report["counts"]["unresolved"], 4)
        paired = [json.loads(line) for line in (self.output / "paired_rows.jsonl").read_text().splitlines()]
        self.assertEqual([row["blocker"] for row in paired], [
            "ambiguous_resolution", "invalid_resolution", "settlement_not_after_decision", "invalid_resolution",
        ])
        self.assertEqual(paired[1]["resolution"]["source_path"], str(self.resolutions.resolve()))
        self.assertEqual(paired[1]["resolution"]["raw_row_sha256"], hashlib.sha256(
            (json.dumps(_resolution("mismatch", "YES", returned_market_id="other")) + "\n").encode()
        ).hexdigest())

    def test_reference_snapshot_ask_does_not_count_as_order_book_quote(self):
        row = _row("reference", probability=.8)
        row["decision_artifact"]["order_book_snapshot"]["source"] = "reference"
        self.fixture([row], [_resolution("reference", "YES")])
        report = self.run_probe()
        self.assertEqual(report["counts"]["missing_quote"], 1)
        self.assertEqual(report["candidate"]["winning_buys"], 0)
        self.assertEqual(report["control"]["proxy_priced_buys"], 0)

    def test_reference_snapshot_does_not_fall_back_to_unproven_order_book(self):
        row = _row("reference", probability=.8)
        row["decision_artifact"]["order_book_snapshot"]["source"] = "reference"
        row["decision_artifact"]["order_book"] = {"best_yes_ask": .4, "best_no_ask": .6}
        self.fixture([row], [_resolution("reference", "YES")])
        report = self.run_probe()
        self.assertEqual(report["counts"]["missing_quote"], 1)
        self.assertEqual(report["candidate"]["winning_buys"], 0)
        self.assertEqual(report["control"]["proxy_priced_buys"], 0)

    def test_conflicting_recorded_actions_fail_before_output(self):
        row = _row("conflict", action="BUY_NO", probability=.8)
        row["decision_artifact"]["final_action"] = "BUY_YES"
        self.fixture([row], [_resolution("conflict", "YES")])
        with self.assertRaisesRegex(ValueError, "conflicting recorded actions"):
            self.run_probe()
        self.assertFalse(self.output.exists())

    def test_saved_market_id_must_match_requested_and_returned(self):
        self.fixture([_row("one")], [_resolution("one", "YES", saved_market_id="other")])
        report = self.run_probe()
        self.assertEqual(report["counts"]["unresolved"], 1)
        row = json.loads((self.output / "paired_rows.jsonl").read_text())
        self.assertEqual(row["blocker"], "invalid_resolution")

    def test_invalid_settings_and_duplicate_market_fail_before_output(self):
        self.fixture([_row("same"), _row("same")], [_resolution("same", "YES")])
        with self.assertRaisesRegex(ValueError, "duplicate market"):
            self.run_probe()
        self.assertFalse(self.output.exists())
        self.fixture([_row("one")], [_resolution("one", "YES")])
        with self.assertRaisesRegex(ValueError, "min_side_ask"):
            self.run_probe(min_side_ask=-.1)
        self.assertFalse(self.output.exists())

    def test_existing_fixed_probe_shape_admits_393_locators_without_scanning_full_source(self):
        rows = [_row(f"market-{i}", action="SKIP") for i in range(393)]
        self.fixture(rows, [])
        old = json.loads(self.manifest.read_text())
        original_size = self.source.stat().st_size
        self.manifest.write_text(json.dumps({
            "status": "completed_bounded_read_only_probe",
            "snapshot": {"snapshot_id": "snapshot-1", "candidate_count": 393},
            "manifest": {"source_path": str(self.source.resolve()), "indexed_source_bytes": original_size},
            "locators": old["locators"],
        }))
        # Collector append after the probe must not force a 57-GB-style source hash.
        with self.source.open("ab") as stream:
            stream.write(b'{"new_unselected_row":true}\n')
        from scripts import paired_loss_guard_probe as probe
        real_hash = probe._sha256_file
        def forbid_source_hash(path):
            if Path(path) == self.source:
                raise AssertionError("unbounded source hash")
            return real_hash(path)
        with patch.object(probe, "_sha256_file", side_effect=forbid_source_hash):
            report = self.run_probe()
        self.assertEqual(report["counts"]["rows"], 393)
        self.assertEqual(report["inputs"]["source_indexed_bytes"], original_size)
        self.assertEqual(report["counts"]["unresolved"], 393)

    def test_output_can_use_collector_owned_derived_reports_root(self):
        self.fixture([_row("one")], [_resolution("one", "YES")])
        collector_root = self.base / "collector-volume"
        output = collector_root / "data" / "derived_reports" / "paired-actual"
        with patch.dict(os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": str(collector_root)}):
            report = build_paired_loss_guard_probe(
                probe_manifest_path=self.manifest, strict_resolutions_path=self.resolutions,
                output_dir=output,
            )
        self.assertEqual(report["counts"]["rows"], 1)
        self.assertTrue((output / "report.json").is_file())

    def test_cli_executes_actual_runner(self):
        self.fixture([_row("one")], [_resolution("one", "YES")])
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "paired_loss_guard_probe.py"),
                                 "--probe-manifest", str(self.manifest), "--strict-resolutions", str(self.resolutions),
                                 "--output-dir", str(self.output)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["candidate"]["winning_buys"], 1)


if __name__ == "__main__":
    unittest.main()
