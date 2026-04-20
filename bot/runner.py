"""Main bot runner, multi-exchange prediction market bot."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.exchanges.base import BaseExchange
from bot.risk import RiskManager
from bot.shared_core import AccountState, TradeContext, build_trade_decision
from bot.status import build_snapshot
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LivePosition:
    market_id: str
    question: str
    direction: str
    price: float
    size: float
    order_id: str
    created_at: str


class PredictionBot:
    """Multi-exchange prediction market trading bot."""

    def __init__(self, config: dict = None):
        config = config or {}
        self.config = config

        self.exchanges: dict[str, BaseExchange] = {}
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.kelly = KellySizer()
        self.risk = RiskManager(config)

        self.running = False
        self.stats = {
            "scans": 0,
            "signals": 0,
            "trades": 0,
            "errors": 0,
            "blocked": 0,
        }
        self.last_block_reasons: dict[str, int] = {}
        self.open_positions: list[LivePosition] = []
        self.trade_history: list[dict] = []

        self.log_dir = Path(config.get("log_dir", "data"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def add_kalshi(self, api_key_id: str, private_key_path: str, demo: bool = True):
        from bot.exchanges.kalshi import KalshiExchange

        exchange = KalshiExchange(api_key_id, private_key_path, demo)
        self.exchanges["kalshi"] = exchange
        return exchange

    def connect_all(self) -> dict[str, bool]:
        results = {}
        for name, exchange in self.exchanges.items():
            try:
                results[name] = exchange.connect()
            except Exception as e:
                logger.error(f"Failed to connect to {name}: {e}")
                results[name] = False
        return results

    def scan_once(self) -> dict:
        logger.info(f"\n{'='*60}")
        logger.info(f"Scan #{self.stats['scans'] + 1} at {datetime.now(timezone.utc).isoformat()}")
        logger.info(f"{'='*60}")

        all_signals = []
        blocked_reasons: dict[str, int] = {}

        for exchange_name, exchange in self.exchanges.items():
            try:
                markets = exchange.get_markets(limit=30)
                if not markets:
                    continue

                logger.info(f"\n{exchange_name}: {len(markets)} markets")

                for market in markets:
                    try:
                        order_book = exchange.get_order_book(market.id)
                        signal = self.strategy.analyze_market(market, order_book)
                        if signal:
                            signal["exchange"] = exchange_name
                            signal["question"] = getattr(market, "question", signal.get("question", ""))
                            signal["yes_price"] = getattr(market, "yes_price", signal.get("market_price"))
                            signal["no_price"] = getattr(market, "no_price", None)
                            all_signals.append(signal)
                    except Exception as e:
                        logger.debug(f"Error analyzing {market.id}: {e}")
                        continue
            except Exception as e:
                logger.error(f"Error scanning {exchange_name}: {e}")
                self.stats["errors"] += 1

        all_signals.sort(key=lambda s: s.get("edge", 0), reverse=True)

        if all_signals:
            logger.info("\n📊 Top Signals:")
            for sig in all_signals[:5]:
                logger.info(
                    f"  {sig['direction']} | "
                    f"Edge: {sig['edge']:.2%} | "
                    f"Conf: {sig['confidence']:.2%} | "
                    f"Price: ${sig['market_price']:.2f} | "
                    f"[{sig['exchange']}]"
                )
        else:
            logger.info("  No signals this cycle")

        trades = 0
        for sig in all_signals[:3]:
            result = self._process_signal(sig)
            if result is None:
                continue
            if result.get("blocked_reason"):
                blocked_reasons[result["blocked_reason"]] = blocked_reasons.get(result["blocked_reason"], 0) + 1
                self.stats["blocked"] += 1
            elif result.get("order"):
                trades += 1

        self.stats["scans"] += 1
        self.stats["signals"] += len(all_signals)
        self.stats["trades"] += trades
        self.last_block_reasons = blocked_reasons

        self._log_scan(all_signals, trades, blocked_reasons)

        return {
            "markets_scanned": sum(len(exchange.get_markets(limit=5)) for exchange in self.exchanges.values()),
            "signals": len(all_signals),
            "trades": trades,
            "blocked_reasons": blocked_reasons,
        }

    def run_loop(self, interval_seconds: int = 120, max_scans: int = None):
        self.running = True
        logger.info(f"Bot started, scanning every {interval_seconds}s")

        count = 0
        while self.running:
            try:
                self.scan_once()
                count += 1

                if max_scans and count >= max_scans:
                    break

                time.sleep(interval_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Scan error: {e}", exc_info=True)
                time.sleep(interval_seconds)

        self.running = False
        logger.info(f"Bot stopped. Stats: {self.stats}")

    def build_status_snapshot(self, *, reason: str = "status", scan_num: int | None = None):
        status = self.risk.get_status()
        total_trades = len(self.trade_history)
        resolved_trades = sum(1 for trade in self.trade_history if trade.get("resolved"))
        open_trades = max(0, len(self.open_positions))
        balance_value = self._coerce_float(status.get("balance"), default=self.risk.state.current_balance)
        pnl_text = status.get("pnl", "$0.00 (+0.0%)")
        pnl_value, pnl_pct = self._parse_pnl_fields(pnl_text)
        extra = {
            "source": self.config.get("trading", {}).get("mode", "paper"),
            "blocked_last_scan": sum(self.last_block_reasons.values()),
        }
        if self.last_block_reasons:
            extra["top_blockers"] = ", ".join(
                f"{name}:{count}" for name, count in sorted(self.last_block_reasons.items(), key=lambda item: item[1], reverse=True)[:3]
            )
        return build_snapshot(
            mode=status.get("mode", "unknown"),
            trading_enabled=bool(status.get("trading_enabled")),
            tradable_cap=status.get("max_tradable_balance", "unlimited"),
            max_position_size=status.get("max_position_size_usd", "unlimited"),
            balance=balance_value,
            available_cash=status.get("available_cash", "$0.00"),
            reserved_capital=status.get("reserved_capital", "$0.00"),
            exposure=status.get("exposure", "$0.00 (0.0%)"),
            pnl=pnl_value,
            pnl_pct=pnl_pct,
            win_rate_pct=self._parse_percent(status.get("win_rate", "0%")),
            total_trades=total_trades,
            open_trades=open_trades,
            resolved_trades=resolved_trades,
            scan_num=scan_num if scan_num is not None else self.stats.get("scans", 0),
            session_id="live-runner",
            extra=extra,
        )

    def _process_signal(self, signal: dict) -> Optional[dict]:
        exchange = self.exchanges.get(signal.get("exchange"))
        if not exchange:
            return None

        context = self._build_trade_context(signal, exchange)
        strategy_cfg = self.config.get("strategy", {})
        decision = build_trade_decision(
            context,
            kelly_sizer=self.kelly,
            risk_policy=self.risk,
            min_edge=strategy_cfg.get("min_edge", self.config.get("min_edge", 0.02)),
            min_confidence=strategy_cfg.get("min_confidence", self.config.get("min_confidence", 0.50)),
            max_entry_price=self.config.get("max_entry_price", 0.70),
        )

        if not decision.approved:
            logger.info(f"🛑 Shared decision skipped: {decision.reason}")
            return {"blocked_reason": decision.reason_code, "decision": decision}

        result = self._execute_signal(signal, decision)
        if result:
            return {"order": result, "decision": decision}
        return {"blocked_reason": "execution_failed", "decision": decision}

    def _build_trade_context(self, signal: dict, exchange: BaseExchange) -> TradeContext:
        balance = self._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=0.0)
        available_cash = balance
        reserved_capital = sum(position.size for position in self.open_positions)
        total_exposure = reserved_capital
        effective_tradable_cash = available_cash
        if self.risk.max_tradable_balance and self.risk.max_tradable_balance > 0:
            effective_tradable_cash = min(effective_tradable_cash, self.risk.max_tradable_balance)

        account_state = AccountState(
            starting_balance=self.risk.state.starting_balance,
            current_balance=balance,
            available_cash=available_cash,
            reserved_capital=reserved_capital,
            total_exposure=total_exposure,
            open_positions=len(self.open_positions),
            daily_pnl=self.risk.state.daily_pnl,
            drawdown_pct=self.risk.state.drawdown_pct,
            consecutive_losses=self.risk.state.consecutive_losses,
            consecutive_wins=self.risk.state.consecutive_wins,
            metadata={
                "effective_tradable_cash": round(effective_tradable_cash, 2),
                "mode": self.config.get("trading", {}).get("mode", "paper"),
            },
        )

        self.risk.sync_account_state(
            current_balance=balance,
            available_cash=available_cash,
            reserved_capital=reserved_capital,
            total_exposure=total_exposure,
            open_positions=len(self.open_positions),
        )

        return TradeContext(
            exchange=signal.get("exchange", "unknown"),
            market_id=signal.get("market_id", ""),
            question=signal.get("question", ""),
            direction=signal.get("direction", "BUY_YES"),
            market_price=signal.get("market_price"),
            yes_price=signal.get("yes_price", signal.get("market_price")),
            no_price=signal.get("no_price"),
            model_probability=signal.get("model_probability"),
            edge=signal.get("edge"),
            confidence=signal.get("confidence"),
            account_state=account_state,
            source_context=dict(signal),
            metadata={"runner": "live"},
        )

    def _execute_signal(self, signal: dict, decision) -> Optional[dict]:
        exchange = self.exchanges.get(signal["exchange"])
        if not exchange:
            return None

        market_id = signal["market_id"]
        side = "YES" if decision.action == "BUY_YES" else "NO"

        try:
            market_bid_ask = exchange.get_market_bid_ask(market_id)
            if market_bid_ask and market_bid_ask.get("best_yes_ask", 0) > 0:
                yes_ask = market_bid_ask.get("best_yes_ask", 0)
                no_ask = market_bid_ask.get("best_no_ask", 0)
            else:
                logger.warning(f"No market price data for {market_id} - skipping")
                return None
        except Exception as e:
            logger.debug(f"Could not fetch market bid/ask for {market_id}: {e}")
            yes_ask = signal.get("market_price", 0.50)
            if yes_ask <= 0 or yes_ask >= 1:
                logger.warning(f"No valid market price for {market_id} - skipping")
                return None
            no_ask = 1 - yes_ask

        price = yes_ask if side == "YES" else no_ask
        price = max(0.01, min(price, 0.99))
        size = float(decision.position_size or 0.0)

        if size < 1:
            logger.info(f"Position too small after shared risk controls: ${size:.2f}")
            return None

        order = exchange.place_order(market_id, side, price, size)
        if not order:
            return None

        order_id = order.id if hasattr(order, "id") else str(order)
        self.open_positions.append(
            LivePosition(
                market_id=market_id,
                question=signal.get("question", ""),
                direction=decision.action,
                price=price,
                size=size,
                order_id=order_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.trade_history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_id": market_id,
                "question": signal.get("question", ""),
                "direction": decision.action,
                "size": size,
                "price": price,
                "resolved": False,
                "order_id": order_id,
                "decision_reason": decision.reason,
            }
        )
        self.risk.record_trade(
            {
                "trade_id": order_id,
                "question": signal.get("question", ""),
                "size": size,
                "reserved_capital": size,
                "resolved": False,
            }
        )

        logger.info(
            f"✅ Trade executed: {side} ${size:.2f} @ ${price:.4f} on {signal['exchange']}/{market_id}"
        )
        self._log_trade(signal, order, decision, size, price)
        return {"order": order, "signal": signal, "decision": decision}

    def _log_scan(self, signals: list, trades: int, blocked_reasons: dict[str, int]):
        log_file = self.log_dir / f"scans_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "signals": len(signals),
                "trades": trades,
                "blocked_reasons": blocked_reasons,
                "top_signals": signals[:3],
            }) + "\n")

    def _log_trade(self, signal: dict, order, decision, size: float, price: float):
        log_file = self.log_dir / "trades.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "signal": signal,
                "decision": {
                    "reason": decision.reason,
                    "reason_code": decision.reason_code,
                    "requested_position_size": decision.requested_position_size,
                    "position_size": decision.position_size,
                },
                "order_id": order.id if hasattr(order, 'id') else str(order),
                "fill_price": price,
                "size": size,
            }) + "\n")

    def stop(self):
        self.running = False

    def close(self):
        if hasattr(self.strategy, 'news'):
            self.strategy.news.close()
        for exchange in self.exchanges.values():
            exchange.close()

    @staticmethod
    def _coerce_float(value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_percent(value) -> float:
        text = str(value or "0").strip().replace("%", "")
        try:
            return float(text)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_pnl_fields(pnl_text: str) -> tuple[float, float]:
        text = str(pnl_text or "").strip()
        if not text:
            return 0.0, 0.0
        if "(" not in text:
            return PredictionBot._coerce_float(text.replace("$", "")), 0.0
        pnl_part, pct_part = text.split("(", 1)
        pnl_value = PredictionBot._coerce_float(pnl_part.replace("$", "").strip())
        pnl_pct = PredictionBot._parse_percent(pct_part.replace(")", "").strip())
        return pnl_value, pnl_pct
