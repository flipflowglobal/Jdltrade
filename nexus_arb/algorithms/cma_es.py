"""
CMA-ES — Covariance Matrix Adaptation Evolution Strategy.

Purpose: Find the optimal flash loan size for a given arbitrage path,
         accounting for non-linear slippage, gas costs, and loan fees.

Theory (Hansen 2006):
  CMA-ES maintains a multivariate Gaussian distribution N(m, σ²C)
  over the search space. Each generation:
    1. Sample λ candidate solutions xₖ ~ m + σ · N(0, C)
    2. Evaluate objective f(xₖ) = net_profit(trade_size=xₖ)
    3. Update mean m toward best solutions (weighted recombination)
    4. Adapt covariance C using rank-μ update + rank-one update (CSA/CMA)
    5. Adapt step-size σ via cumulative step-size adaptation (CSA)

Advantages over gradient methods:
  - No gradient required (black-box optimization)
  - Handles non-smooth, non-convex profit landscapes
  - Naturally handles slippage curves from AMM invariants

We use a 1D variant (optimizing trade size ∈ [min_eth, max_eth]).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class CMAESResult:
    optimal_size_eth: float
    expected_profit_eth: float
    expected_profit_usd: float
    iterations: int
    converged: bool


class CMAES1D:
    """
    1-dimensional CMA-ES optimiser for trade size selection.

    The profit function is estimated by the caller (slippage model).
    """

    def __init__(
        self,
        population_size: int = 16,
        initial_sigma: float = 0.5,
        max_iterations: int = 100,
        tolerance: float = 1e-9,
        seed: Optional[int] = None
    ) -> None:
        self.lambda_ = max(population_size, 4)
        self.mu      = self.lambda_ // 2
        self.sigma0  = initial_sigma
        self.max_iter = max_iterations
        self.tol     = tolerance
        self.rng     = np.random.default_rng(seed)

        # Weights for recombination
        self.weights = np.log(self.mu + 0.5) - np.log(np.arange(1, self.mu + 1))
        self.weights /= self.weights.sum()
        self.mueff = 1.0 / (self.weights ** 2).sum()

        # Adaptation constants (Hansen & Ostermeier 2001)
        self.cs   = (self.mueff + 2) / (1 + self.mueff + 5)
        self.ds   = 1 + 2 * max(0, math.sqrt((self.mueff - 1) / 2) - 1) + self.cs
        self.cc   = (4 + self.mueff) / 5
        self.c1   = 2 / ((1 + 0.3) ** 2 + self.mueff)
        self.cmu  = min(1 - self.c1,
                        2 * (self.mueff - 2 + 1 / self.mueff) /
                        ((1 + 1.3) ** 2 + self.mueff))

        # Expected value of |N(0,1)| (chi_1)
        self.chiN = math.sqrt(1) * (1 - 1 / (4 * 1) + 1 / (21 * 1 ** 2))

    def optimize(
        self,
        profit_fn: Callable[[float], float],
        x_min: float = 0.01,
        x_max: float = 500.0,
        x_start: Optional[float] = None,
        eth_price_usd: float = 3000.0
    ) -> CMAESResult:
        """
        Maximize profit_fn(trade_size_eth) subject to [x_min, x_max].

        profit_fn should return net profit in ETH (negative if losing).
        We negate internally since CMA-ES minimises.
        """
        x_start = x_start or (x_min + x_max) / 3
        # Work in log-space to respect positivity constraint
        log_min  = math.log(max(x_min, 1e-6))
        log_max  = math.log(x_max)
        log_x0   = math.log(max(x_start, x_min))

        # CMA-ES state (1D)
        m    = log_x0
        sigma = self.sigma0
        pc   = 0.0   # Evolution path for covariance
        ps   = 0.0   # Evolution path for step-size
        C    = 1.0   # Variance

        best_x     = x_start
        best_profit = profit_fn(x_start)
        converged  = False

        for gen in range(self.max_iter):
            # Sample candidates in log-space
            zk   = self.rng.standard_normal(self.lambda_)
            dk   = math.sqrt(C) * zk
            xk_log = np.clip(m + sigma * dk, log_min, log_max)
            xk   = np.exp(xk_log)

            # Evaluate (negate for minimisation)
            fk   = np.array([profit_fn(x) for x in xk])
            fk_neg = -fk

            # Sort by fitness (ascending cost = descending profit)
            order  = np.argsort(fk_neg)
            xk_sorted = xk_log[order]
            zk_sorted = zk[order]
            fk_sorted = fk[order[::-1]]  # descending profit for tracking

            # Best in this generation
            if fk_sorted[0] > best_profit:
                best_profit = fk_sorted[0]
                best_x      = xk[order[0]]

            # Weighted recombination
            m_old = m
            m     = float(np.dot(self.weights, xk_sorted[:self.mu]))

            # Step-size control path
            C_inv_sqrt = 1.0 / math.sqrt(C) if C > 0 else 1.0
            ps = (1 - self.cs) * ps + math.sqrt(self.cs * (2 - self.cs) * self.mueff) \
                 * C_inv_sqrt * float(np.dot(self.weights, zk_sorted[:self.mu]))

            # Heaviside for hsig
            hsig = abs(ps) / math.sqrt(1 - (1 - self.cs) ** (2 * (gen + 1))) / self.chiN < 1.4 + 2 / 2

            # Covariance evolution path
            pc = (1 - self.cc) * pc + (1 if hsig else 0) \
                 * math.sqrt(self.cc * (2 - self.cc) * self.mueff) \
                 * math.sqrt(C) * float(np.dot(self.weights, zk_sorted[:self.mu]))

            # Covariance update
            rank_one = self.c1 * pc ** 2
            rank_mu  = self.cmu * float(np.dot(self.weights,
                                               (math.sqrt(C) * zk_sorted[:self.mu]) ** 2))
            C = (1 - self.c1 - self.cmu) * C + rank_one + rank_mu

            # Step-size update (CSA)
            sigma *= math.exp((self.cs / self.ds) * (abs(ps) / self.chiN - 1))
            sigma  = max(min(sigma, (log_max - log_min)), 1e-10)

            # Convergence check
            if sigma < self.tol or abs(m - m_old) < 1e-12:
                converged = True
                log.debug(f"CMA-ES converged at iteration {gen+1}")
                break

        # Evaluate best once more at final mean
        final_x = float(np.exp(np.clip(m, log_min, log_max)))
        final_profit = profit_fn(final_x)
        if final_profit > best_profit:
            best_x = final_x
            best_profit = final_profit

        return CMAESResult(
            optimal_size_eth=best_x,
            expected_profit_eth=best_profit,
            expected_profit_usd=best_profit * eth_price_usd,
            iterations=gen + 1,
            converged=converged
        )


class TradeOptimizer:
    """
    Uses CMA-ES to find optimal trade size for an arbitrage opportunity.

    Models AMM slippage using the constant-product formula:
        output = reserve_out * amount_in / (reserve_in + amount_in)

    This gives a realistic profit curve that peaks somewhere before
    depleting pool liquidity.
    """

    def __init__(self, config: dict) -> None:
        cma_cfg = config.get("algorithms", {}).get("cma_es", {})
        self.cma = CMAES1D(
            population_size=cma_cfg.get("population_size", 32),
            initial_sigma=cma_cfg.get("initial_sigma", 0.3),
            max_iterations=cma_cfg.get("max_iterations", 100),
            tolerance=cma_cfg.get("tolerance", 1e-9)
        )
        self.flash_fee = config.get("trading", {}).get("flash_loan_fee_bps", 9) / 10_000

    def build_profit_function(
        self,
        opportunity,
        gas_cost_eth: float,
        eth_price_usd: float = 3000.0
    ) -> Callable[[float], float]:
        """
        Build a slippage-aware profit function for the given opportunity.

        Profit(x) = output_after_all_swaps(x) - x*(1+flash_fee) - gas_cost
        """
        pools = opportunity.pools

        def simulate_path(amount_in_eth: float) -> float:
            """Simulate the full trade path with slippage."""
            amount = amount_in_eth
            for pool in pools:
                if pool.liquidity <= 0:
                    return -float("inf")
                # Constant-product slippage model
                # For complex AMMs this is approximate
                fee_mult = 1 - pool.fee_bps / 10_000
                # Effective rate with slippage
                slippage = amount / (pool.liquidity + amount)
                effective_rate = pool.price * fee_mult * (1 - slippage)
                amount = amount * effective_rate

            repay = amount_in_eth * (1 + self.flash_fee)
            net = amount - repay - gas_cost_eth
            return net

        return simulate_path

    def optimize(
        self,
        opportunity,
        gas_cost_eth: float,
        eth_price_usd: float,
        min_size: float,
        max_size: float
    ) -> CMAESResult:
        profit_fn = self.build_profit_function(opportunity, gas_cost_eth, eth_price_usd)
        start = min(opportunity.max_input_eth, max_size)
        return self.cma.optimize(
            profit_fn,
            x_min=min_size,
            x_max=max_size,
            x_start=start,
            eth_price_usd=eth_price_usd
        )
