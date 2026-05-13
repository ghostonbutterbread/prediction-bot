"""Canonical paper wallet identity and storage helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.strategy_policy import coerce_strategy_policy

STABLE_PAPER_WALLET_ID = "stable_paper"
BETA_PAPER_WALLET_ID = "beta_paper"

_PAPER_WALLET_METADATA = {
    STABLE_PAPER_WALLET_ID: {
        "policy_id": "stable",
        "namespace": "paper_stable",
        "label": "stable/control",
    },
    BETA_PAPER_WALLET_ID: {
        "policy_id": "beta",
        "namespace": "paper_beta",
        "label": "beta/challenger",
    },
}


@dataclass(frozen=True, slots=True)
class PaperWalletContract:
    """Resolved paper wallet identity and storage contract."""

    wallet_id: str
    policy_id: str
    namespace: str
    label: str
    root_dir: Path
    risk_state_path: Path
    session_path: Path | None
    mutates_accounting: bool = True
    places_live_orders: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_id": self.wallet_id,
            "policy_id": self.policy_id,
            "namespace": self.namespace,
            "label": self.label,
            "root_dir": str(self.root_dir),
            "risk_state_path": str(self.risk_state_path),
            "session_path": str(self.session_path) if self.session_path is not None else None,
            "mutates_accounting": bool(self.mutates_accounting),
            "places_live_orders": bool(self.places_live_orders),
        }


def build_paper_wallet_contracts(
    config: Mapping[str, Any] | None = None,
    *,
    session_id: str | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return canonical stable/beta paper wallet contracts for a config/root."""

    stable_contract = resolve_paper_wallet_contract(
        config,
        wallet_id=STABLE_PAPER_WALLET_ID,
        session_id=session_id,
        data_dir=data_dir,
    )
    beta_contract = resolve_paper_wallet_contract(
        config,
        wallet_id=BETA_PAPER_WALLET_ID,
        session_id=session_id,
        data_dir=data_dir,
    )
    _assert_wallet_contracts_are_isolated(stable_contract, beta_contract)
    active_wallet_id = resolve_active_paper_wallet_id(config, data_dir=data_dir)
    active_contract = stable_contract if active_wallet_id == STABLE_PAPER_WALLET_ID else beta_contract
    return {
        "active_wallet_id": active_contract.wallet_id,
        "active_policy_id": active_contract.policy_id,
        STABLE_PAPER_WALLET_ID: stable_contract.to_dict(),
        BETA_PAPER_WALLET_ID: beta_contract.to_dict(),
    }


def resolve_active_paper_wallet_id(
    config: Mapping[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> str:
    explicit = _configured_wallet_id(config)
    if explicit is not None:
        return explicit

    storage_base = _active_storage_base(config, data_dir=data_dir)
    policy = coerce_strategy_policy(_mapping(config).get("strategy_policy_normalized") or _mapping(config).get("strategy_policy"))
    if policy.is_active or _looks_like_beta_shadow_root(storage_base):
        return BETA_PAPER_WALLET_ID
    return STABLE_PAPER_WALLET_ID


def resolve_paper_wallet_contract(
    config: Mapping[str, Any] | None = None,
    *,
    wallet_id: str | None = None,
    session_id: str | None = None,
    data_dir: str | Path | None = None,
) -> PaperWalletContract:
    resolved_wallet_id = _normalize_wallet_id(wallet_id or resolve_active_paper_wallet_id(config, data_dir=data_dir))
    metadata = _PAPER_WALLET_METADATA[resolved_wallet_id]
    root_dir = resolve_paper_wallet_root(config, wallet_id=resolved_wallet_id, data_dir=data_dir)
    return PaperWalletContract(
        wallet_id=resolved_wallet_id,
        policy_id=str(metadata["policy_id"]),
        namespace=str(metadata["namespace"]),
        label=str(metadata["label"]),
        root_dir=root_dir,
        risk_state_path=root_dir / "risk_state.json",
        session_path=(root_dir / f"sim_{session_id}.json") if session_id not in (None, "") else None,
    )


def resolve_paper_wallet_root(
    config: Mapping[str, Any] | None = None,
    *,
    wallet_id: str | None = None,
    data_dir: str | Path | None = None,
) -> Path:
    resolved_wallet_id = _normalize_wallet_id(wallet_id or resolve_active_paper_wallet_id(config, data_dir=data_dir))
    configured_root = _configured_wallet_root(config, resolved_wallet_id)
    if configured_root is not None:
        return _ensure_mode_storage_dir(configured_root, "paper")

    active_storage_base = _active_storage_base(config, data_dir=data_dir)
    active_wallet_id = resolve_active_paper_wallet_id(config, data_dir=data_dir)
    if resolved_wallet_id == active_wallet_id:
        if resolved_wallet_id == STABLE_PAPER_WALLET_ID:
            return _ensure_mode_storage_dir(_stable_storage_base(active_storage_base), "paper")
        return _ensure_mode_storage_dir(_beta_storage_base(active_storage_base), "paper")

    if resolved_wallet_id == STABLE_PAPER_WALLET_ID:
        return _ensure_mode_storage_dir(_stable_storage_base(active_storage_base), "paper")
    return _ensure_mode_storage_dir(_beta_storage_base(active_storage_base), "paper")


def build_paper_accounting_ref(
    config: Mapping[str, Any] | None = None,
    *,
    session_id: str,
    wallet_id: str | None = None,
    data_dir: str | Path | None = None,
    trade_id: str | None = None,
    mutates_accounting: bool,
    mutates_balance: bool | None = None,
) -> dict[str, Any]:
    """Build canonical accounting metadata for a paper decision row."""

    contract = resolve_paper_wallet_contract(
        config,
        wallet_id=wallet_id,
        session_id=session_id,
        data_dir=data_dir,
    )
    balance_mutation = bool(mutates_accounting if mutates_balance is None else mutates_balance)
    ref = {
        "wallet_id": contract.wallet_id,
        "policy_id": contract.policy_id,
        "namespace": str(contract.root_dir),
        "wallet_namespace": contract.namespace,
        "root_path": str(contract.root_dir),
        "risk_state_path": str(contract.risk_state_path),
        "session_path": str(contract.session_path) if contract.session_path is not None else None,
        "ledger_path": str(contract.session_path) if contract.session_path is not None else None,
        "mutates_balance": balance_mutation,
        "mutates_accounting": bool(mutates_accounting),
        "places_live_orders": contract.places_live_orders,
        "balance_model": "paper_balance",
    }
    if trade_id not in (None, ""):
        ref["trade_id"] = str(trade_id)
    return ref


def _assert_wallet_contracts_are_isolated(stable_contract: PaperWalletContract, beta_contract: PaperWalletContract) -> None:
    stable_root = stable_contract.root_dir.resolve(strict=False)
    beta_root = beta_contract.root_dir.resolve(strict=False)
    if stable_root == beta_root or stable_root in beta_root.parents or beta_root in stable_root.parents:
        raise ValueError(
            "stable_paper and beta_paper wallet roots must be isolated "
            f"(stable_root={stable_root}, beta_root={beta_root})"
        )
    stable_risk = stable_contract.risk_state_path.resolve(strict=False)
    beta_risk = beta_contract.risk_state_path.resolve(strict=False)
    if stable_risk == beta_risk:
        raise ValueError(
            "stable_paper and beta_paper risk state paths must be isolated "
            f"(risk_state_path={stable_risk})"
        )
    if stable_contract.session_path is not None and beta_contract.session_path is not None:
        stable_session = stable_contract.session_path.resolve(strict=False)
        beta_session = beta_contract.session_path.resolve(strict=False)
        if stable_session == beta_session:
            raise ValueError(
                "stable_paper and beta_paper session paths must be isolated "
                f"(session_path={stable_session})"
            )


def _configured_wallet_id(config: Mapping[str, Any] | None) -> str | None:
    paper_wallets = _mapping(_mapping(config).get("paper_wallets"))
    for key in ("active_wallet_id", "wallet_id"):
        candidate = paper_wallets.get(key)
        if candidate in _PAPER_WALLET_METADATA:
            return str(candidate)

    runtime = _mapping(_mapping(config).get("runtime"))
    candidate = runtime.get("paper_wallet_id")
    if candidate in _PAPER_WALLET_METADATA:
        return str(candidate)
    return None


def _configured_wallet_root(config: Mapping[str, Any] | None, wallet_id: str) -> Path | None:
    paper_wallets = _mapping(_mapping(config).get("paper_wallets"))
    for key in (wallet_id, _PAPER_WALLET_METADATA[wallet_id]["policy_id"]):
        wallet_cfg = _mapping(paper_wallets.get(key))
        candidate = wallet_cfg.get("root_dir") or wallet_cfg.get("base_dir")
        if candidate not in (None, ""):
            return Path(candidate)
    return None


def _active_storage_base(
    config: Mapping[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> Path:
    runtime = _mapping(_mapping(config).get("runtime"))
    base_dir = runtime.get("base_dir")
    if base_dir in (None, ""):
        base_dir = data_dir if data_dir not in (None, "") else _mapping(config).get("data_dir", "data")
    return _normalize_storage_base(Path(base_dir or "data"))


def _stable_storage_base(active_storage_base: Path) -> Path:
    normalized = _normalize_storage_base(active_storage_base)
    if _looks_like_beta_shadow_root(normalized):
        return normalized.parent
    return normalized


def _beta_storage_base(active_storage_base: Path) -> Path:
    normalized = _normalize_storage_base(active_storage_base)
    if _looks_like_beta_shadow_root(normalized):
        return normalized
    return normalized / "beta_shadow"


def _normalize_wallet_id(wallet_id: str) -> str:
    if wallet_id not in _PAPER_WALLET_METADATA:
        raise ValueError(f"unknown paper wallet id: {wallet_id}")
    return str(wallet_id)


def _looks_like_beta_shadow_root(path: Path) -> bool:
    return path.name == "beta_shadow"


def _normalize_storage_base(path: Path) -> Path:
    if path.name in {"paper", "live"}:
        return path.parent
    return path


def _ensure_mode_storage_dir(path: str | Path, mode: str) -> Path:
    base = Path(path)
    normalized_mode = "live" if str(mode or "").strip().lower() == "live" else "paper"
    if base.name == normalized_mode:
        return base
    if base.name in {"paper", "live"}:
        return base.parent / normalized_mode
    return base / normalized_mode


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "BETA_PAPER_WALLET_ID",
    "PaperWalletContract",
    "STABLE_PAPER_WALLET_ID",
    "build_paper_accounting_ref",
    "build_paper_wallet_contracts",
    "resolve_active_paper_wallet_id",
    "resolve_paper_wallet_contract",
    "resolve_paper_wallet_root",
]
