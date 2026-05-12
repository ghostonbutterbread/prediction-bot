from bot.agent_decision_ledger import summarize_agent_decision_reporting


def _decision(
    *,
    shared_candidate_id: str | None = "candidate-1",
    decision_id: str,
    agent_id: str = "prediction_lab",
    runtime: str = "prediction_lab",
    policy: str = "normal",
    decision_role: str = "normal",
    action: str = "BUY_YES",
    reason_code: str = "approved",
    legacy_candidate_identity: dict | None = None,
) -> dict:
    row = {
        "decision_id": decision_id,
        "agent_run_id": f"{agent_id}:run-1",
        "agent_id": agent_id,
        "runtime": runtime,
        "policy": policy,
        "decision_role": decision_role,
        "shared_candidate_id": shared_candidate_id,
        "candidate_dataset_path": "data/prediction_lab/market_snapshots.jsonl",
        "run_id": "run-1",
        "market_id": "KXHIGHNY-26APR29-T80",
        "observed_at": "2026-05-12T10:00:00+00:00",
        "decided_at": "2026-05-12T10:00:01+00:00",
        "action": action,
        "reason_code": reason_code,
    }
    if legacy_candidate_identity:
        row["legacy_candidate_identity"] = legacy_candidate_identity
    return row


def test_reporting_coverage_includes_agent_runtime_policy_and_role_counts():
    report = summarize_agent_decision_reporting(
        [
            _decision(decision_id="d1", agent_id="prediction_lab", runtime="prediction_lab", policy="normal", decision_role="normal"),
            _decision(decision_id="d2", agent_id="shadow", runtime="prediction_lab", policy="shadow", decision_role="shadow"),
        ]
    )

    coverage = report["coverage"]
    assert coverage["total_rows"] == 2
    assert coverage["by_agent_id"] == {"prediction_lab": 1, "shadow": 1}
    assert coverage["by_runtime"] == {"prediction_lab": 2}
    assert coverage["by_policy"] == {"normal": 1, "shadow": 1}
    assert coverage["by_decision_role"] == {"normal": 1, "shadow": 1}


def test_reporting_policy_drift_detects_normal_buy_yes_vs_shadow_skip():
    report = summarize_agent_decision_reporting(
        [
            _decision(decision_id="d1", policy="normal", decision_role="normal", action="BUY_YES", reason_code="approved"),
            _decision(decision_id="d2", policy="shadow", decision_role="shadow", action="SKIP", reason_code="shadow_price_cap"),
        ]
    )

    drift = report["policy_drift"]
    assert drift["candidate_count_with_action_drift"] == 1
    assert drift["candidate_count_with_reason_drift"] == 1
    assert drift["by_policy_pair"] == {
        "normal|shadow": {
            "candidate_count": 1,
            "action_drift_count": 1,
            "reason_drift_count": 1,
        }
    }
    assert drift["by_candidate"][0]["actions"] == ["BUY_YES", "SKIP"]
    assert drift["by_candidate"][0]["reason_codes"] == ["approved", "shadow_price_cap"]


def test_reporting_policy_drift_ignores_same_policy_role_action_variance():
    report = summarize_agent_decision_reporting(
        [
            _decision(decision_id="d1", policy="normal", decision_role="main", action="BUY_YES", reason_code="approved"),
            _decision(decision_id="d2", policy="normal", decision_role="paper", action="SKIP", reason_code="paper_skip"),
        ]
    )

    drift = report["policy_drift"]
    assert drift["candidate_count_with_action_drift"] == 0
    assert drift["candidate_count_with_reason_drift"] == 0
    assert drift["by_policy_pair"] == {}
    assert drift["by_candidate"] == []


def test_reporting_overlap_detects_multiple_agents_policies_and_duplicate_actions():
    report = summarize_agent_decision_reporting(
        [
            _decision(decision_id="d1", agent_id="prediction_lab", policy="normal", decision_role="normal", action="BUY_YES"),
            _decision(decision_id="d2", agent_id="paper", runtime="paper", policy="normal", decision_role="paper", action="BUY_YES"),
            _decision(decision_id="d3", agent_id="shadow", policy="shadow", decision_role="shadow", action="SKIP"),
        ]
    )

    overlap = report["overlap"]
    assert overlap["candidate_count_with_multiple_decisions"] == 1
    assert overlap["candidate_count_with_multiple_agents"] == 1
    assert overlap["candidate_count_with_multiple_policies"] == 1
    assert overlap["duplicate_identity_rows"] == 1
    assert overlap["duplicate_identity_action_groups"] == 1
    assert overlap["top_overlaps"][0]["agents"] == ["paper", "prediction_lab", "shadow"]
    assert overlap["top_overlaps"][0]["policies"] == ["normal", "shadow"]


def test_reporting_overlap_uses_legacy_fingerprint_when_shared_candidate_id_is_missing():
    legacy_identity = {
        "identity_type": "legacy_prediction_lab_market_snapshot",
        "row_fingerprint_sha256": "abc123",
    }
    report = summarize_agent_decision_reporting(
        [
            _decision(shared_candidate_id=None, decision_id="d1", agent_id="backfill", legacy_candidate_identity=legacy_identity),
            _decision(shared_candidate_id=None, decision_id="d2", agent_id="paper", legacy_candidate_identity=legacy_identity),
        ]
    )

    overlap = report["overlap"]
    assert overlap["candidate_count_with_multiple_decisions"] == 1
    assert overlap["candidate_count_with_multiple_agents"] == 1
    assert overlap["top_overlaps"][0]["identity_key"] == "legacy_fingerprint:abc123"


def test_reporting_outcomes_join_replay_rows_and_keep_delta_flags_candidate_level():
    report = summarize_agent_decision_reporting(
        [
            _decision(decision_id="d1", action="BUY_YES", policy="normal", decision_role="normal"),
            _decision(decision_id="d2", action="SKIP", policy="shadow", decision_role="shadow"),
            _decision(shared_candidate_id="candidate-2", decision_id="d3", action="BUY_NO", policy="normal", decision_role="normal"),
        ],
        replay_rows=[
            {
                "shared_candidate_id": "candidate-1",
                "outcome": "YES",
                "missed_win": True,
                "bad_buy_added": False,
                "bad_buy_removed": True,
            }
        ],
    )

    outcomes = report["outcomes"]
    assert outcomes["joined_rows"] == 2
    assert outcomes["unresolved_rows"] == 1
    assert outcomes["by_outcome"] == {"YES": 2}
    assert outcomes["candidate_delta_flags"]["missed_win_candidate_count"] == 1
    assert outcomes["candidate_delta_flags"]["bad_buy_removed_candidate_count"] == 1
    assert outcomes["candidate_delta_flags"]["bad_buy_added_candidate_count"] == 0
    assert outcomes["candidate_delta_flags"]["flagged_candidate_count"] == 1
    assert outcomes["by_action"]["BUY_YES"]["wins"] == 1
    assert outcomes["by_action"]["SKIP"]["skips"] == 1
    assert outcomes["by_action"]["BUY_NO"]["unresolved_rows"] == 1
    assert "missed_win_count" not in outcomes["by_policy"]["shadow"]


def test_reporting_void_buy_is_joined_but_not_win_or_loss():
    report = summarize_agent_decision_reporting(
        [
            _decision(decision_id="d1", action="BUY_YES"),
            _decision(decision_id="d2", action="BUY_NO"),
        ],
        replay_rows=[
            {
                "shared_candidate_id": "candidate-1",
                "outcome": "VOID",
            }
        ],
    )

    outcomes = report["outcomes"]
    assert outcomes["joined_rows"] == 2
    assert outcomes["unresolved_rows"] == 0
    assert outcomes["by_outcome"] == {"VOID": 2}
    assert outcomes["by_action"]["BUY_YES"]["wins"] == 0
    assert outcomes["by_action"]["BUY_YES"]["losses"] == 0
    assert outcomes["by_action"]["BUY_NO"]["wins"] == 0
    assert outcomes["by_action"]["BUY_NO"]["losses"] == 0
