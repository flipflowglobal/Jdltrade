"""
Bellman-Ford Arbitrage Detector.

Theory:
  - Model token exchange rates as a directed weighted graph
  - Edge (u, v) weight = -log(price_after_fee(u → v))
  - A negative-weight cycle in this graph corresponds to a profitable
    arbitrage cycle:  ∑ -log(r_i) < 0  ↔  ∏ r_i > 1
  - Standard Bellman-Ford detects negative cycles in O(V·E) time

Enhanced features:
  - Multi-source Bellman-Ford (start from all nodes simultaneously)
  - Path reconstruction to recover the actual trade route
  - Profit calculation including flash loan premium
  - Optimal entry size estimation via liquidity-constrained search
  - Try-it-all heuristic: enumerate all cycles found, return richest

References:
  - Bellman (1958): On a Routing Problem
  - Menger's theorem for path-weight duality
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..market_data import PoolPrice, PriceGraph

log = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    """A discovered profitable arbitrage cycle."""
    cycle: list[str]            # Token cycle e.g. ["WETH","USDC","USDT","WETH"]
    pools: list[PoolPrice]      # Pool used for each hop
    gross_rate: float           # Product of exchange rates (> 1.0 for profit)
    net_rate: float             # After all fees
    expected_profit_pct: float  # (net_rate - 1) * 100
    max_input_eth: float        # Estimated max profitable input
    score: float = 0.0          # Composite score for ranking

    @property
    def is_profitable(self) -> bool:
        return self.net_rate > 1.0

    def __repr__(self) -> str:
        path = " → ".join(self.cycle)
        return (f"ArbitrageOpportunity({path}, "
                f"profit={self.expected_profit_pct:.4f}%, "
                f"max_input={self.max_input_eth:.3f} ETH)")


class BellmanFord:
    """
    Multi-source Bellman-Ford arbitrage detector.

    Algorithm:
      1. Build weight matrix: w(u,v) = min(-log(rate)) over all pools for (u,v)
      2. Run V-1 relaxation passes
      3. On V-th pass, any further relaxation means negative cycle exists
      4. Reconstruct cycles using predecessor pointers
      5. Verify cycles and calculate exact profit
    """

    def __init__(self, config: dict) -> None:
        self.cfg = config
        self.flash_loan_fee = config.get("trading", {}).get("flash_loan_fee_bps", 9) / 10_000

    def detect(
        self,
        graph: PriceGraph,
        min_profit_pct: float = 0.05
    ) -> list[ArbitrageOpportunity]:
        """
        Find all profitable arbitrage cycles in the price graph.

        Args:
            graph: Current market price graph
            min_profit_pct: Minimum profit threshold (e.g., 0.05 = 0.05%)

        Returns:
            Sorted list of ArbitrageOpportunity (best first)
        """
        tokens, weights = graph.to_weight_matrix()
        n = len(tokens)
        if n < 2:
            return []

        tok_idx = {t: i for i, t in enumerate(tokens)}

        # Distance array (start from virtual source with 0 distance to all)
        INF = float("inf")
        dist: list[float] = [0.0] * n      # Multi-source: all nodes at dist=0
        pred: list[int]   = [-1] * n

        # Relax edges V-1 times
        for iteration in range(n - 1):
            updated = False
            for (u, v), w in weights.items():
                if u not in tok_idx or v not in tok_idx:
                    continue
                ui, vi = tok_idx[u], tok_idx[v]
                if dist[ui] + w < dist[vi] - 1e-12:
                    dist[vi] = dist[ui] + w
                    pred[vi] = ui
                    updated = True
            if not updated:
                break  # Early termination

        # V-th pass: detect negative cycles
        neg_cycle_nodes: set[int] = set()
        for (u, v), w in weights.items():
            if u not in tok_idx or v not in tok_idx:
                continue
            ui, vi = tok_idx[u], tok_idx[v]
            if dist[ui] + w < dist[vi] - 1e-12:
                neg_cycle_nodes.add(vi)

        if not neg_cycle_nodes:
            return []

        # Reconstruct all unique cycles
        opportunities: list[ArbitrageOpportunity] = []
        seen_cycles: set[frozenset] = set()

        for node_idx in neg_cycle_nodes:
            cycle_tokens = self._reconstruct_cycle(node_idx, pred, tokens, n)
            if not cycle_tokens:
                continue

            cycle_key = frozenset(cycle_tokens)
            if cycle_key in seen_cycles:
                continue
            seen_cycles.add(cycle_key)

            opp = self._evaluate_cycle(cycle_tokens, graph)
            if opp and opp.expected_profit_pct >= min_profit_pct:
                opportunities.append(opp)

        # Sort by composite score
        opportunities.sort(key=lambda o: o.score, reverse=True)
        log.debug(f"Bellman-Ford: {len(opportunities)} opportunities found")
        return opportunities

    def _reconstruct_cycle(
        self,
        start: int,
        pred: list[int],
        tokens: list[str],
        n: int
    ) -> list[str]:
        """Trace predecessor pointers to find the negative cycle."""
        # Walk back n steps to guarantee we're in the cycle
        v = start
        for _ in range(n):
            v = pred[v]
            if v == -1:
                return []

        # Now walk until we see v again
        cycle = []
        visited = set()
        u = v
        while u not in visited:
            visited.add(u)
            cycle.append(tokens[u])
            u = pred[u]
            if u == -1:
                return []

        # Trim to the actual cycle
        start_tok = tokens[u]
        idx = cycle.index(start_tok)
        cycle = cycle[idx:]
        cycle.append(start_tok)  # Close the cycle
        cycle.reverse()
        return cycle

    def _evaluate_cycle(
        self,
        cycle: list[str],
        graph: PriceGraph
    ) -> Optional[ArbitrageOpportunity]:
        """
        Precisely evaluate a candidate cycle.
        Calculate exact gross/net rates and maximum input size.
        """
        if len(cycle) < 3:
            return None

        # Remove duplicate last element for iteration
        hops = list(zip(cycle[:-1], cycle[1:]))
        pools_used: list[PoolPrice] = []
        gross_rate = 1.0
        net_rate   = 1.0
        min_liquidity = float("inf")

        for token_in, token_out in hops:
            best = graph.best_price(token_in, token_out)
            if best is None:
                return None
            if best.price <= 0:
                return None

            pools_used.append(best)
            gross_rate *= best.price
            net_rate   *= best.price_after_fee
            min_liquidity = min(min_liquidity, best.liquidity)

        # Apply flash loan fee on top
        net_rate_after_loan = net_rate / (1 + self.flash_loan_fee)
        profit_pct = (net_rate_after_loan - 1.0) * 100

        if net_rate_after_loan <= 1.0:
            return None

        # Estimate maximum profitable input
        # As trade size increases, slippage eats into profit
        # Conservative estimate: use 10% of minimum liquidity
        max_input_eth = min(min_liquidity * 0.1, 100.0)

        # Composite score: profit_pct * sqrt(max_input)
        score = profit_pct * math.sqrt(max_input_eth)

        return ArbitrageOpportunity(
            cycle=cycle,
            pools=pools_used,
            gross_rate=gross_rate,
            net_rate=net_rate_after_loan,
            expected_profit_pct=profit_pct,
            max_input_eth=max_input_eth,
            score=score
        )

    def find_best(
        self,
        graph: PriceGraph,
        min_profit_pct: float = 0.05
    ) -> Optional[ArbitrageOpportunity]:
        """Convenience method: return the single best opportunity."""
        opps = self.detect(graph, min_profit_pct)
        return opps[0] if opps else None
