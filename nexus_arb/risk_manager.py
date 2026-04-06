"""
RiskManager — Multi-layer risk controls for production trading.

Layers:
  1. Pre-trade checks    — Validate every opportunity before submission
  2. Position limits     — Max exposure per trade and per day
  3. Circuit breakers    — Auto-halt on consecutive failures or drawdown
  4. Gas controls        — Skip if gas makes trade unprofitable
  5. Value-at-Risk (VaR) — Statistical risk estimation
  6. Emergency stops     — Immediate halt on severe conditions
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from prometheus_client import Counter, Gauge

log = logging.getLogger(__name__)

risk_checks_passed  = Counter("nexus_risk_checks_passed_total", "Risk checks passed")
risk_checks_failed  = Counter("nexus_risk_checks_failed_total", "Risk checks failed", ["reason"])
circuit_breaker_on  = Gauge("nexus_circuit_breaker_active", "Circuit breaker state (1=active)")
daily_pnl_eth       = Gauge("nexus_daily_pnl_eth", "Daily PnL in ETH")


@dataclass
class TradeCandidate:
    opportunity_id: str
    token_in: str
    token_out: str
    flash_loan_amount_eth: float
    expected_profit_eth: float
    expected_profit_usd: float
    gas_cost_eth: float
    net_profit_eth: float
    cycle: list[str]
    confidence: float = 1.0


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    adjusted_size_eth: Optional[float] = None
    max_gas_price_gwei: Optional[float] = None


@dataclass
class TradeRecord:
    timestamp: float
    profit_eth: float
    gas_cost_eth: float
    success: bool
    opportunity_id: str


class RiskManager:
    """
    Production risk management system.

    All trades must pass pre_trade_check() before execution.
    Post-trade, call record_outcome() to update internal state.
    """

    def __init__(self, config: dict) -> None:
        t = config.get("trading", {})
        r = config.get("risk", {})

        self.min_profit_usd        = t.get("min_profit_usd", 5.0)
        self.min_profit_eth        = t.get("min_profit_eth", 0.002)
        self.min_profit_multiplier = t.get("min_profit_after_gas_multiplier", 1.5)
        self.max_flash_loan_eth    = t.get("max_flash_loan_eth", 500.0)
        self.min_flash_loan_eth    = t.get("min_flash_loan_eth", 0.1)
        self.max_slippage_bps      = t.get("max_slippage_bps", 50)
        self.max_gas_gwei          = t.get("max_gas_price_gwei", 2.0)
        self.max_consecutive_fail  = t.get("max_consecutive_failures", 5)
        self.failure_cooldown      = t.get("failure_cooldown_seconds", 60)
        self.max_daily_loss_eth    = t.get("max_daily_loss_eth", 1.0)
        self.max_open_positions    = r.get("max_open_positions", 1)

        # State
        self._consecutive_failures = 0
        self._circuit_break_until  = 0.0
        self._open_positions       = 0
        self._trade_history: deque[TradeRecord] = deque(maxlen=1000)
        self._daily_start = time.time()
        self._daily_pnl   = 0.0

        # VaR parameters
        self._var_confidence = r.get("var_confidence", 0.95)
        self._returns: deque[float] = deque(maxlen=200)

    # ─── Pre-Trade Risk Check ────────────────────────────────
    def pre_trade_check(
        self,
        candidate: TradeCandidate,
        gas_price_gwei: float,
        wallet_balance_eth: float
    ) -> RiskDecision:
        """
        Multi-layer pre-trade validation.
        Returns RiskDecision(approved=True) if trade is safe to execute.
        """
        # 1. Circuit breaker
        if self._is_circuit_broken():
            return self._deny("circuit_breaker")

        # 2. Open position limit
        if self._open_positions >= self.max_open_positions:
            return self._deny("max_positions")

        # 3. Gas price check
        if gas_price_gwei > self.max_gas_gwei:
            return self._deny("gas_too_high")

        # 4. Minimum profit
        if candidate.net_profit_eth < self.min_profit_eth:
            return self._deny("below_min_profit_eth")

        if candidate.expected_profit_usd < self.min_profit_usd:
            return self._deny("below_min_profit_usd")

        # 5. Gas-adjusted profitability
        if candidate.net_profit_eth < candidate.gas_cost_eth * self.min_profit_multiplier:
            return self._deny("profit_too_small_vs_gas")

        # 6. Flash loan size
        loan = candidate.flash_loan_amount_eth
        if loan < self.min_flash_loan_eth:
            return self._deny("loan_too_small")
        if loan > self.max_flash_loan_eth:
            # Try reducing to max allowed
            loan = self.max_flash_loan_eth

        # 7. Wallet has enough for gas
        gas_reserve = gas_price_gwei * 1e-9 * 2_000_000 * 1.5  # Rough ETH gas
        if wallet_balance_eth < gas_reserve:
            return self._deny("insufficient_gas_reserve")

        # 8. Daily loss limit
        if self._daily_pnl < -self.max_daily_loss_eth:
            return self._deny("daily_loss_limit")

        # 9. VaR check
        if not self._var_check(candidate.flash_loan_amount_eth):
            return self._deny("var_exceeded")

        risk_checks_passed.inc()
        return RiskDecision(
            approved=True,
            reason="all_checks_passed",
            adjusted_size_eth=loan,
            max_gas_price_gwei=self.max_gas_gwei
        )

    def _deny(self, reason: str) -> RiskDecision:
        risk_checks_failed.labels(reason=reason).inc()
        log.debug(f"Risk denied: {reason}")
        return RiskDecision(approved=False, reason=reason)

    # ─── Circuit Breaker ─────────────────────────────────────
    def _is_circuit_broken(self) -> bool:
        if time.time() < self._circuit_break_until:
            circuit_breaker_on.set(1)
            return True
        circuit_breaker_on.set(0)
        return False

    def _maybe_trigger_circuit_breaker(self) -> None:
        if self._consecutive_failures >= self.max_consecutive_fail:
            self._circuit_break_until = time.time() + self.failure_cooldown
            log.warning(
                f"Circuit breaker triggered: {self._consecutive_failures} "
                f"consecutive failures. Cooling down for {self.failure_cooldown}s"
            )

    # ─── VaR Check ───────────────────────────────────────────
    def _var_check(self, position_size_eth: float) -> bool:
        """
        Parametric VaR: estimate potential loss at 95% confidence.
        Position fails if expected loss > max_daily_loss_eth / 5 (daily budget).
        """
        if len(self._returns) < 10:
            return True  # Not enough history

        returns = np.array(self._returns)
        mu    = returns.mean()
        sigma = returns.std()

        # 95th percentile loss (one-tailed)
        z = 1.645
        var_pct = -(mu - z * sigma)  # Loss as fraction of position
        var_eth = var_pct * position_size_eth

        budget = self.max_daily_loss_eth / 5
        if var_eth > budget:
            log.warning(f"VaR check failed: VaR={var_eth:.4f} ETH > budget={budget:.4f}")
            return False
        return True

    # ─── Post-Trade Updates ──────────────────────────────────
    def record_outcome(
        self,
        opportunity_id: str,
        profit_eth: float,
        gas_cost_eth: float,
        success: bool
    ) -> None:
        """Must be called after every trade attempt."""
        record = TradeRecord(
            timestamp=time.time(),
            profit_eth=profit_eth,
            gas_cost_eth=gas_cost_eth,
            success=success,
            opportunity_id=opportunity_id
        )
        self._trade_history.append(record)
        self._open_positions = max(0, self._open_positions - 1)

        # Update PnL
        net = profit_eth - gas_cost_eth
        self._daily_pnl += net
        daily_pnl_eth.set(self._daily_pnl)

        # Update returns history
        if profit_eth > 0:
            self._returns.append(net / max(profit_eth, 1e-9))

        if success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            self._maybe_trigger_circuit_breaker()

        log.info(
            f"Trade outcome: {'✓' if success else '✗'} "
            f"profit={profit_eth:.6f} ETH, "
            f"daily_pnl={self._daily_pnl:.6f} ETH"
        )

    def record_trade_start(self) -> None:
        self._open_positions += 1

    def reset_daily(self) -> None:
        """Call at midnight to reset daily PnL."""
        log.info(f"Daily reset. Final PnL: {self._daily_pnl:.6f} ETH")
        self._daily_pnl  = 0.0
        self._daily_start = time.time()

    # ─── Statistics ──────────────────────────────────────────
    def get_stats(self) -> dict:
        history = list(self._trade_history)
        if not history:
            return {"total_trades": 0}

        profits = [t.profit_eth for t in history if t.success]
        losses  = [t.gas_cost_eth for t in history if not t.success]

        return {
            "total_trades":        len(history),
            "successful_trades":   sum(1 for t in history if t.success),
            "success_rate":        sum(1 for t in history if t.success) / len(history),
            "total_profit_eth":    sum(profits),
            "total_gas_spent_eth": sum(t.gas_cost_eth for t in history),
            "daily_pnl_eth":       self._daily_pnl,
            "consecutive_failures": self._consecutive_failures,
            "circuit_broken":      self._is_circuit_broken()
        }
