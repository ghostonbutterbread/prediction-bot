"""Freeze independent resolution-feed bytes and bind exact forward paper intents.

Derived-only; this never settles an account or contacts an exchange. The caller
must supply the independently produced feed, not a decision-side outcome label.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast
import hashlib
import json
import os
import shutil
import tempfile

from bot.composed_lane_wallet import _reject_outcome_fields
from bot.forward_composed_paper import _IDENTITY, _digest
from bot.paper_adapters import _exact_assertions, _exact_clock, _exact_outcome, _same_source_stat


@dataclass(frozen=True)
class ComposedBindingArtifacts:
    frozen_resolutions_path: Path
    bindings_path: Path
    unbound_path: Path
    manifest_path: Path


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    for row in rows)


def bind_composed_finalized_outcomes(
    *, intents: Sequence[Mapping[str, Any]], strict_resolutions_path: str | Path,
    output_dir: str | Path,
) -> ComposedBindingArtifacts:
    """Produce immutable source copy and exact receipt bindings for BUY intents.

    The source path must be an independently fetched finalized-resolution feed.
    Its hash attests the frozen bytes, not the trustworthiness of a caller's path.
    """
    accepted = []
    seen = set()
    for intent in intents:
        if not isinstance(intent, Mapping):
            raise ValueError("invalid composed intent")
        _reject_outcome_fields(intent)
        identity = {field: intent.get(field) for field in _IDENTITY}
        if (intent.get("lane_id") != "forward_composed_paper"
                or intent.get("action") not in {"BUY_YES", "BUY_NO"}
                or any(not isinstance(value, str) or not value for value in identity.values())
                or intent.get("decision_id") != "forward-combined:" + _digest(cast(Mapping[str, str], identity))):
            raise ValueError("invalid forward-composed BUY identity")
        key = (intent["decision_id"], intent["shared_candidate_id"], intent["run_id"], intent["market_id"])
        if key in seen:
            raise ValueError("duplicate composed decision identity")
        seen.add(key)
        accepted.append(key)

    source = Path(strict_resolutions_path).resolve(strict=True)
    target = Path(output_dir).resolve()
    if source == target or source in target.parents or target in source.parents or target.exists():
        raise ValueError("binding output must be a new separate derived directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".composed-binding-", dir=target.parent))
    try:
        wanted_markets = {key[3] for key in accepted}
        frozen = staging / "strict_resolutions.jsonl"
        sha = hashlib.sha256()
        rows: dict[str, tuple[int, dict[str, Any], str]] = {}
        counts: Counter[str] = Counter()
        with source.open("rb") as reader, frozen.open("xb") as writer:
            before = os.fstat(reader.fileno())
            for line_number, raw in enumerate(reader, start=1):
                writer.write(raw)
                sha.update(raw)
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid full strict resolution feed") from exc
                if not isinstance(row, dict):
                    raise ValueError("invalid full strict resolution feed")
                market = row.get("market_id")
                if isinstance(market, str):
                    counts[market] += 1
                    if counts[market] == 1 and market in wanted_markets:
                        rows[market] = (line_number, row, hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest())
            if not _same_source_stat(before, os.fstat(reader.fileno())):
                raise ValueError("resolution feed changed during freeze")
            writer.flush()
            os.fsync(writer.fileno())
        if not _same_source_stat(before, source.stat()):
            raise ValueError("resolution feed changed during freeze")
        digest = sha.hexdigest()
        bindings: list[dict[str, Any]] = []
        unbound: list[dict[str, Any]] = []
        for decision_id, candidate, run, market in accepted:
            reason = "missing_authoritative_resolution"
            if counts[market] > 1:
                reason = "ambiguous_market_resolution"
            elif market in rows:
                line_number, row, row_hash = rows[market]
                try:
                    if (row.get("requested_market_id") != market
                            or row.get("returned_market_id") != market or not row.get("resolution_id")):
                        raise ValueError("wrong resolution identity")
                    assertions = _exact_assertions(row, "outcome")
                    outcome = _exact_outcome(row, {"outcome": assertions[0], "market_status": "finalized"})
                    settlement = _exact_clock(row, {"settlement_ts": row.get("settlement_ts")},
                                              "settlement_ts", required=True)
                    if settlement is None:
                        raise ValueError("missing settlement clock")
                    known_assertions = _exact_assertions(row, "outcome_known_at")
                    known = (_exact_clock(row, {"outcome_known_at": known_assertions[0]},
                                          "outcome_known_at", required=False)
                             if known_assertions else None)
                    receipt = {"composed_decision_id": decision_id, "shared_candidate_id": candidate,
                               "run_id": run, "market_id": market, "outcome": outcome,
                               "settlement_ts": settlement.isoformat(),
                               "strict_resolution_source_sha256": digest,
                               "strict_resolution_source_line_number": line_number,
                               "raw_resolution_row_sha256": row_hash}
                    if known is not None:
                        receipt["outcome_known_at"] = known.isoformat()
                    bindings.append(receipt)
                    continue
                except (IndexError, KeyError, TypeError, ValueError, OverflowError):
                    reason = "invalid_authoritative_resolution"
            unbound.append({"composed_decision_id": decision_id, "shared_candidate_id": candidate,
                            "run_id": run, "market_id": market, "reason_code": reason})
        binding_bytes, unbound_bytes = _jsonl(bindings), _jsonl(unbound)
        (staging / "exact_bindings.jsonl").write_bytes(binding_bytes)
        (staging / "unbound.jsonl").write_bytes(unbound_bytes)
        manifest = {"schema_name": "composed_exact_resolution_binding", "schema_version": 1,
                    "paper_only": True, "non_mutating": True, "source_path": str(source),
                    "strict_resolution_source_sha256": digest, "input_count": len(accepted),
                    "bound_count": len(bindings), "unbound_count": len(unbound),
                    "bindings_sha256": hashlib.sha256(binding_bytes).hexdigest(),
                    "unbound_sha256": hashlib.sha256(unbound_bytes).hexdigest()}
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ComposedBindingArtifacts(target / "strict_resolutions.jsonl",
                                    target / "exact_bindings.jsonl", target / "unbound.jsonl",
                                    target / "manifest.json")
