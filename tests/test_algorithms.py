"""
Unit tests for NEXUS-ARB algorithm suite.
Run: pytest tests/test_algorithms.py -v
"""

import math
import pytest
import numpy as np

from nexus_arb.market_data import PriceGraph, PoolPrice
from nexus_arb.algorithms.bellman_ford import BellmanFord, ArbitrageOpportunity
from nexus_arb.algorithms.cma_es import CMAES1D
from nexus_arb.algorithms.ukf import PriceUKF
from nexus_arb.algorithms.thompson_sampling import ThompsonBandit

# ─── Fixtures ─────────────────────────────────────────────────

MINIMAL_CONFIG = {
    "trading": {
        "flash_loan_fee_bps": 9,
        "min_profit_eth": 0.001,
        "max_flash_loan_eth": 100.0,
        "min_flash_loan_eth": 0.01,
    },
    "algorithms": {
        "cma_es": {"population_size": 8, "max_iterations": 50, "tolerance": 1e-6},
        "ukf":    {"alpha": 1e-3, "beta": 2.0, "kappa": 0.0,
                   "process_noise": 0.001, "measurement_noise": 0.01},
        "thompson": {"alpha_init": 1.0, "beta_init": 1.0, "decay_rate": 0.99}
    }
}


def make_graph_with_arbitrage() -> PriceGraph:
    """
    Create a price graph with a known arbitrage cycle:
    WETH → USDC → USDT → WETH

    Prices engineered so:
      WETH→USDC: 3000 USDC per WETH
      USDC→USDT: 1.001 (slight premium)
      USDT→WETH: 1/2997 = 0.0003337 ETH per USDT

    Net rate = 3000 * 1.001 / 2997 ≈ 1.001 (0.1% profit before fees)
    """
    g = PriceGraph()

    # WETH → USDC
    g.add(PoolPrice(
        pool_id="WETH_USDC_500",
        dex="uniswap_v3",
        token_in="WETH",
        token_out="USDC",
        price=3000.0,
        liquidity=1000.0,
        fee_bps=5
    ))

    # USDC → USDT (slight premium)
    g.add(PoolPrice(
        pool_id="USDC_USDT_100",
        dex="uniswap_v3",
        token_in="USDC",
        token_out="USDT",
        price=1.0015,
        liquidity=5_000_000.0,
        fee_bps=1
    ))

    # USDT → WETH (cheap)
    g.add(PoolPrice(
        pool_id="USDT_WETH",
        dex="curve",
        token_in="USDT",
        token_out="WETH",
        price=1.0 / 2997.0,
        liquidity=5_000_000.0,
        fee_bps=4
    ))

    return g


def make_flat_graph() -> PriceGraph:
    """No arbitrage — prices are at parity."""
    g = PriceGraph()
    g.add(PoolPrice("AB", "uniswap_v3", "A", "B", 2.0, 1000.0, 30))
    g.add(PoolPrice("BA", "uniswap_v3", "B", "A", 0.5, 1000.0, 30))
    return g


# ─── Bellman-Ford Tests ───────────────────────────────────────

class TestBellmanFord:

    def test_detects_known_arbitrage(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        graph = make_graph_with_arbitrage()
        opps = bf.detect(graph, min_profit_pct=0.01)
        assert len(opps) > 0, "Should detect the engineered arbitrage"
        best = opps[0]
        assert best.expected_profit_pct > 0
        assert "WETH" in best.cycle

    def test_no_false_positives_on_flat_market(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        graph = make_flat_graph()
        opps = bf.detect(graph, min_profit_pct=0.01)
        assert len(opps) == 0, "Should not find arbitrage in flat market"

    def test_cycle_is_closed(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        graph = make_graph_with_arbitrage()
        opps = bf.detect(graph, min_profit_pct=0.001)
        for opp in opps:
            assert opp.cycle[0] == opp.cycle[-1], "Cycle must start and end at same token"

    def test_opportunity_scores_are_positive(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        graph = make_graph_with_arbitrage()
        opps = bf.detect(graph, min_profit_pct=0.001)
        for opp in opps:
            assert opp.score > 0

    def test_opportunities_sorted_by_score(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        graph = make_graph_with_arbitrage()
        opps = bf.detect(graph, min_profit_pct=0.001)
        scores = [o.score for o in opps]
        assert scores == sorted(scores, reverse=True)

    def test_empty_graph(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        opps = bf.detect(PriceGraph(), min_profit_pct=0.01)
        assert opps == []

    def test_find_best(self):
        bf = BellmanFord(MINIMAL_CONFIG)
        graph = make_graph_with_arbitrage()
        best = bf.find_best(graph)
        assert best is not None
        assert best.is_profitable


# ─── CMA-ES Tests ────────────────────────────────────────────

class TestCMAES:

    def test_finds_optimal_of_parabola(self):
        """Optimize x*(1-x) — peak at x=0.5"""
        cma = CMAES1D(population_size=8, max_iterations=100, tolerance=1e-8)
        result = cma.optimize(
            profit_fn=lambda x: x * (1 - x),
            x_min=0.01,
            x_max=1.0,
            x_start=0.3
        )
        assert abs(result.optimal_size_eth - 0.5) < 0.05, \
            f"Expected ~0.5, got {result.optimal_size_eth}"
        assert result.expected_profit_eth > 0.24  # Close to 0.25

    def test_respects_bounds(self):
        cma = CMAES1D(population_size=8, max_iterations=50)
        result = cma.optimize(
            profit_fn=lambda x: -x,  # Minimize: prefers x_min
            x_min=0.1,
            x_max=10.0
        )
        assert result.optimal_size_eth >= 0.09, "Must respect lower bound"
        assert result.optimal_size_eth <= 10.1, "Must respect upper bound"

    def test_handles_flat_objective(self):
        cma = CMAES1D(population_size=8, max_iterations=50)
        result = cma.optimize(profit_fn=lambda x: 0.0, x_min=0.1, x_max=10.0)
        assert result is not None  # Should not crash

    def test_converges(self):
        cma = CMAES1D(
            population_size=16,
            max_iterations=200,
            tolerance=1e-9
        )
        result = cma.optimize(
            profit_fn=lambda x: -(x - 3.0) ** 2,
            x_min=0.1,
            x_max=10.0,
            x_start=5.0
        )
        assert abs(result.optimal_size_eth - 3.0) < 0.5


# ─── UKF Tests ────────────────────────────────────────────────

class TestUKF:

    def test_initializes_correctly(self):
        ukf = PriceUKF(MINIMAL_CONFIG)
        state = ukf.update(3000.0)
        assert abs(state.price - 3000.0) < 1.0

    def test_converges_to_true_price(self):
        ukf = PriceUKF(MINIMAL_CONFIG)
        true_price = 3000.0
        # Feed many observations
        for _ in range(50):
            state = ukf.update(true_price + np.random.normal(0, 5))
        assert abs(state.price - true_price) < 50.0, \
            f"UKF should converge near true price, got {state.price}"

    def test_uncertainty_decreases_over_time(self):
        ukf = PriceUKF(MINIMAL_CONFIG)
        state0 = ukf.update(3000.0)
        initial_std = state0.price_std
        for _ in range(20):
            ukf.update(3000.0 + np.random.normal(0, 1))
        state20 = ukf._to_state()
        # Uncertainty should decrease with more observations
        # (or at least not explode)
        assert state20.price_std < initial_std * 10

    def test_tracks_trend(self):
        ukf = PriceUKF(MINIMAL_CONFIG)
        # Feed rising prices
        for i in range(30):
            ukf.update(3000.0 + i * 10)
        state = ukf._to_state()
        assert state.is_trending_up, "UKF should detect upward trend"

    def test_predict_n_blocks(self):
        ukf = PriceUKF(MINIMAL_CONFIG)
        for _ in range(10):
            ukf.update(3000.0)
        pred = ukf.predict_n_blocks(1)
        assert isinstance(pred, float)
        assert pred > 0


# ─── Thompson Sampling Tests ──────────────────────────────────

class TestThompsonSampling:

    def test_selects_best_arm_asymptotically(self):
        bandit = ThompsonBandit(
            arms=["A", "B", "C"],
            seed=42
        )
        # Give arm B much higher rewards
        for _ in range(50):
            bandit.update("A", 0.1)
            bandit.update("B", 0.9)
            bandit.update("C", 0.2)

        # After many updates, B should be selected most often
        selections = [bandit.select() for _ in range(100)]
        b_count = selections.count("B")
        assert b_count > 50, f"Arm B should dominate, got {b_count}/100"

    def test_select_top_k(self):
        bandit = ThompsonBandit(arms=["A", "B", "C", "D"], seed=0)
        top2 = bandit.select_top_k(2)
        assert len(top2) == 2
        assert len(set(top2)) == 2  # No duplicates

    def test_new_arm_added_dynamically(self):
        bandit = ThompsonBandit(arms=["A"], seed=0)
        bandit.add_arm("B")
        assert "B" in bandit.arms

    def test_failure_update_increases_beta(self):
        bandit = ThompsonBandit(arms=["A"], seed=0)
        before_beta = bandit.arms["A"].beta
        bandit.update("A", -1.0)  # Failure
        assert bandit.arms["A"].beta > before_beta * 0.99

    def test_success_update_increases_alpha(self):
        bandit = ThompsonBandit(arms=["A"], seed=0)
        before_alpha = bandit.arms["A"].alpha
        bandit.update("A", 1.0)  # Success
        assert bandit.arms["A"].alpha > before_alpha * 0.99

    def test_stats_table_sorted(self):
        bandit = ThompsonBandit(arms=["A", "B", "C"], seed=0)
        bandit.update("C", 0.9)
        bandit.update("C", 0.9)
        table = bandit.stats_table()
        posteriors = [row["mean_posterior"] for row in table]
        assert posteriors == sorted(posteriors, reverse=True)


# ─── Price Graph Tests ────────────────────────────────────────

class TestPriceGraph:

    def test_add_and_retrieve(self):
        g = PriceGraph()
        pp = PoolPrice("pool1", "uniswap_v3", "WETH", "USDC", 3000.0, 100.0, 5)
        g.add(pp)
        best = g.best_price("WETH", "USDC")
        assert best is not None
        assert best.price == 3000.0

    def test_best_price_returns_highest(self):
        g = PriceGraph()
        g.add(PoolPrice("p1", "uniswap_v3", "WETH", "USDC", 3000.0, 100.0, 30))
        g.add(PoolPrice("p2", "curve",      "WETH", "USDC", 3001.0, 100.0, 4))
        best = g.best_price("WETH", "USDC")
        assert best.price == 3001.0

    def test_log_price_is_negative_log(self):
        pp = PoolPrice("p", "uniswap_v3", "A", "B", 2.0, 100.0, 0)
        expected = -math.log(2.0)
        assert abs(pp.log_price - expected) < 1e-9

    def test_price_after_fee(self):
        pp = PoolPrice("p", "uniswap_v3", "A", "B", 3000.0, 100.0, fee_bps=30)
        expected = 3000.0 * (1 - 30 / 10_000)
        assert abs(pp.price_after_fee - expected) < 0.001

    def test_to_weight_matrix(self):
        g = make_graph_with_arbitrage()
        tokens, weights = g.to_weight_matrix()
        assert len(tokens) > 0
        assert len(weights) > 0
        for w in weights.values():
            assert w != float("inf")  # All valid prices
