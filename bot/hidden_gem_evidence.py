from __future__ import annotations

from collections import Counter
from typing import Any


def extract_hidden_gem_evidence_card(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the Phase 3B hidden-gem evidence card from known artifact shapes."""
    if not isinstance(row, dict):
        return None

    direct = row.get("hidden_gem_evidence_card")
    if isinstance(direct, dict):
        return direct

    for reasoning in (
        row.get("reasoning"),
        row.get("decision_trace"),
        _shared_core_decision(row).get("reasoning"),
    ):
        if isinstance(reasoning, dict) and isinstance(reasoning.get("hidden_gem_evidence_card"), dict):
            return reasoning["hidden_gem_evidence_card"]

    return None


def summarize_hidden_gem_evidence_cards(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schema_version": 1,
        "basis": "hidden_gem_evidence_card",
        "rows_scanned": 0,
        "card_rows": 0,
        "no_card_rows": 0,
        "insufficient_data_rows": 0,
        "approved_cards": 0,
        "rejected_cards": 0,
        "beta_rejected_cards": 0,
        "unknown_outcome_cards": 0,
        "by_shape_tier_reason": [],
        "reason_code_counts": {},
        "tail_probability_source_counts": {},
        "tail_scoring_reason_code_counts": {},
    }
    groups: dict[tuple[str, str, str], Counter] = {}
    reason_counts: Counter[str] = Counter()
    tail_source_counts: Counter[str] = Counter()
    tail_scoring_reason_counts: Counter[str] = Counter()

    for row in rows or []:
        summary["rows_scanned"] += 1
        if not isinstance(row, dict):
            summary["no_card_rows"] += 1
            continue

        card = extract_hidden_gem_evidence_card(row)
        if not isinstance(card, dict):
            summary["no_card_rows"] += 1
            continue

        final_reason_code = _final_reason_code(row)
        reason_codes = card.get("reason_codes") if isinstance(card.get("reason_codes"), dict) else {}
        beta_reject = _clean_optional(reason_codes.get("beta_reject"))
        weather_reject = _clean_optional(reason_codes.get("weather_reject"))
        reason_code = beta_reject or weather_reject or final_reason_code or "unknown"
        weather_shape = _clean_label(card.get("weather_shape"))
        hidden_gem_tier = _clean_label(card.get("hidden_gem_tier"))
        if "unknown" in {weather_shape, hidden_gem_tier, reason_code}:
            summary["insufficient_data_rows"] += 1

        rejected = _is_final_rejected(row, final_reason_code=final_reason_code)
        approved = (not rejected) and _is_final_approved(row, final_reason_code=final_reason_code)
        beta_rejected = bool(beta_reject)

        summary["card_rows"] += 1
        summary["approved_cards"] += int(approved)
        summary["rejected_cards"] += int(rejected)
        summary["beta_rejected_cards"] += int(beta_rejected)
        if not approved and not rejected:
            summary["unknown_outcome_cards"] += 1

        key = (weather_shape, hidden_gem_tier, reason_code)
        bucket = groups.setdefault(key, Counter())
        bucket["count"] += 1
        bucket["approved"] += int(approved)
        bucket["rejected"] += int(rejected)
        bucket["beta_rejected"] += int(beta_rejected)
        bucket["unknown_outcome"] += int(not approved and not rejected)
        reason_counts[reason_code] += 1

        tail = card.get("tail") if isinstance(card.get("tail"), dict) else {}
        scoring = tail.get("probability_scoring") if isinstance(tail.get("probability_scoring"), dict) else {}
        tail_source = _clean_optional(scoring.get("source"))
        tail_scoring_reason = _clean_optional(scoring.get("reason_code"))
        if tail_source:
            tail_source_counts[tail_source] += 1
        if tail_scoring_reason:
            tail_scoring_reason_counts[tail_scoring_reason] += 1

    summary["by_shape_tier_reason"] = [
        {
            "weather_shape": weather_shape,
            "hidden_gem_tier": hidden_gem_tier,
            "reason_code": reason_code,
            "count": counts["count"],
            "approved": counts["approved"],
            "rejected": counts["rejected"],
            "beta_rejected": counts["beta_rejected"],
            "unknown_outcome": counts["unknown_outcome"],
        }
        for (weather_shape, hidden_gem_tier, reason_code), counts in sorted(
            groups.items(),
            key=lambda item: (-item[1]["count"], item[0][0], item[0][1], item[0][2]),
        )
    ]
    summary["reason_code_counts"] = dict(sorted(reason_counts.items()))
    summary["tail_probability_source_counts"] = dict(sorted(tail_source_counts.items()))
    summary["tail_scoring_reason_code_counts"] = dict(sorted(tail_scoring_reason_counts.items()))
    return summary


def format_hidden_gem_evidence_summary(summary: dict[str, Any] | None, *, max_groups: int = 3) -> str | None:
    if not isinstance(summary, dict) or int(summary.get("rows_scanned") or 0) <= 0:
        return None

    parts = [
        f"Hidden-gem evidence: cards {int(summary.get('card_rows') or 0)}/{int(summary.get('rows_scanned') or 0)}",
        (
            f"final approved {int(summary.get('approved_cards') or 0)} "
            f"rejected {int(summary.get('rejected_cards') or 0)}"
        ),
        f"beta rejected {int(summary.get('beta_rejected_cards') or 0)}",
        (
            f"no-card {int(summary.get('no_card_rows') or 0)} "
            f"insufficient {int(summary.get('insufficient_data_rows') or 0)}"
        ),
    ]
    groups = list(summary.get("by_shape_tier_reason") or [])[:max(0, int(max_groups))]
    if groups:
        rendered = "; ".join(
            (
                f"{group.get('weather_shape', 'unknown')}/"
                f"{group.get('hidden_gem_tier', 'unknown')}/"
                f"{group.get('reason_code', 'unknown')}={int(group.get('count') or 0)}"
            )
            for group in groups
        )
        parts.append(f"top {rendered}")
    tail_sources = summary.get("tail_probability_source_counts")
    if isinstance(tail_sources, dict) and tail_sources:
        rendered = " ".join(
            f"{source} {int(count or 0)}"
            for source, count in sorted(tail_sources.items())
        )
        parts.append(f"tail probability {rendered}")
    return " | ".join(parts)


def _shared_core_decision(row: dict[str, Any]) -> dict[str, Any]:
    decision = row.get("shared_core_decision")
    if isinstance(decision, dict):
        return decision
    artifact = row.get("decision_artifact")
    if isinstance(artifact, dict) and isinstance(artifact.get("shared_core_decision"), dict):
        return artifact["shared_core_decision"]
    return {}


def _decision_artifact(row: dict[str, Any]) -> dict[str, Any]:
    artifact = row.get("decision_artifact")
    return artifact if isinstance(artifact, dict) else {}


def _final_reason_code(row: dict[str, Any]) -> str | None:
    artifact = _decision_artifact(row)
    shared_pipeline = row.get("shared_pipeline") if isinstance(row.get("shared_pipeline"), dict) else {}
    decision = _shared_core_decision(row)
    for value in (
        artifact.get("final_reason_code"),
        shared_pipeline.get("final_reason_code"),
        decision.get("reason_code"),
        row.get("final_reason_code"),
        row.get("decision_reason_code"),
        row.get("skip_reason_code"),
    ):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned
    return None


def _final_action(row: dict[str, Any]) -> str | None:
    artifact = _decision_artifact(row)
    shared_pipeline = row.get("shared_pipeline") if isinstance(row.get("shared_pipeline"), dict) else {}
    for value in (
        artifact.get("final_action"),
        shared_pipeline.get("final_action"),
        row.get("final_action"),
        row.get("direction"),
    ):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned.upper()
    return None


def _is_final_approved(row: dict[str, Any], *, final_reason_code: str | None) -> bool:
    decision = _shared_core_decision(row)
    if decision.get("approved") is True:
        return True
    action = _final_action(row)
    if action in {"BUY_YES", "BUY_NO"}:
        return True
    decision_type = _clean_optional(row.get("decision_type"))
    if decision_type in {"buy_yes", "buy_no"}:
        return True
    return final_reason_code == "approved"


def _is_final_rejected(row: dict[str, Any], *, final_reason_code: str | None) -> bool:
    decision = _shared_core_decision(row)
    if decision.get("approved") is False:
        return True
    if _final_action(row) == "SKIP":
        return True
    if _clean_optional(row.get("decision_type")) == "skip":
        return True
    if _clean_optional(row.get("status")) in {"rejected", "failed", "skip"}:
        return True
    return False


def _clean_label(value: Any) -> str:
    cleaned = _clean_optional(value)
    return cleaned if cleaned else "unknown"


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
