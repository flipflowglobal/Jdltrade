"""
Thompson Sampling — Bayesian Multi-Armed Bandit for DEX/Route Selection.

Theory:
  Each DEX is modelled as a Bernoulli arm with unknown success probability θ.
  We maintain a Beta(α, β) posterior for each arm:
    - α increases when a trade on this DEX succeeds
    - β increases when a trade fails or is suboptimal
    - Thompson sampling: draw θ̃ ~ Beta(α, β) and pick arm with highest θ̃

  Advantages:
    - Naturally balances exploration vs exploitation
    - Posterior concentrates around true success rate over time
    - Decay factor prevents stale beliefs from dominating

Extended to reward-weighted Thompson Sampling:
  Instead of binary success/failure, we update with:
    - Success: α += profit_normalized (weighted by profit magnitude)
    - Failure: β += 1

This makes the bandit prefer not just reliable DEXes, but profitable ones.

Applications:
  1. DEX selection: which DEX to route through for a given token pair
  2. Route selection: which multi-hop path to use
  3. Trade size: which size bucket is most consistently profitable
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class ArmStats:
    """Posterior parameters for a single bandit arm."""
    alpha: float = 1.0      # Prior successes
    beta:  float = 1.0      # Prior failures
    n_pulls: int = 0        # Total number of pulls
    n_wins: int = 0         # Successful outcomes
    total_reward: float = 0.0
    last_pulled: float = field(default_factory=time.time)

    @property
    def mean_reward(self) -> float:
        return self.total_reward / max(self.n_pulls, 1)

    @property
    def ucb(self) -> float:
        """Upper Confidence Bound (used as tiebreaker)."""
        if self.n_pulls == 0:
            return float("inf")
        return self.mean_reward + 2 * np.sqrt(np.log(self.n_pulls + 1) / self.n_pulls)

    def sample(self, rng: np.random.Generator) -> float:
        """Draw a sample from the posterior Beta distribution."""
        return float(rng.beta(self.alpha, self.beta))

    def update(self, reward: float, decay: float = 0.995) -> None:
        """
        Update posterior with observed reward.

        Args:
            reward: Normalised reward in [0, 1]. Positive = success.
            decay:  Shrink existing counts toward prior (prevents stale learning)
        """
        self.alpha *= decay
        self.beta  *= decay

        if reward > 0:
            # Weight the alpha update by reward magnitude
            self.alpha += reward
            self.n_wins += 1
        else:
            self.beta += 1.0

        self.n_pulls += 1
        self.total_reward += max(reward, 0)
        self.last_pulled = time.time()


class ThompsonBandit:
    """
    Multi-armed bandit with Thompson Sampling.

    Arms represent distinct choices (DEXes, routes, sizes).
    """

    def __init__(
        self,
        arms: list[str],
        alpha_init: float = 1.0,
        beta_init: float  = 1.0,
        decay_rate: float = 0.995,
        seed: Optional[int] = None
    ) -> None:
        self.arms: dict[str, ArmStats] = {
            arm: ArmStats(alpha=alpha_init, beta=beta_init)
            for arm in arms
        }
        self.decay = decay_rate
        self.rng   = np.random.default_rng(seed)

    def select(self) -> str:
        """Draw from each arm's posterior and return the arm with highest sample."""
        samples = {
            arm: stats.sample(self.rng)
            for arm, stats in self.arms.items()
        }
        return max(samples, key=samples.__getitem__)

    def select_top_k(self, k: int) -> list[str]:
        """Return the top-k arms by Thompson sample (for parallel evaluation)."""
        samples = [
            (arm, stats.sample(self.rng))
            for arm, stats in self.arms.items()
        ]
        samples.sort(key=lambda x: x[1], reverse=True)
        return [arm for arm, _ in samples[:k]]

    def update(self, arm: str, reward: float) -> None:
        """
        Update arm posterior.

        Args:
            arm:    The arm that was pulled
            reward: Observed reward. Use normalised profit, e.g. profit_eth / max_profit
        """
        if arm not in self.arms:
            self.arms[arm] = ArmStats(alpha=1.0, beta=1.0)
        self.arms[arm].update(reward, decay=self.decay)

    def add_arm(self, arm: str) -> None:
        if arm not in self.arms:
            self.arms[arm] = ArmStats()

    def stats_table(self) -> list[dict]:
        """Return sorted stats table for logging/monitoring."""
        rows = []
        for arm, s in self.arms.items():
            rows.append({
                "arm": arm,
                "alpha": round(s.alpha, 3),
                "beta": round(s.beta, 3),
                "mean_posterior": round(s.alpha / (s.alpha + s.beta), 4),
                "n_pulls": s.n_pulls,
                "n_wins": s.n_wins,
                "mean_reward": round(s.mean_reward, 6)
            })
        rows.sort(key=lambda r: r["mean_posterior"], reverse=True)
        return rows


class DexBandit:
    """
    Specialised Thompson Sampling bandit for DEX × token-pair routing.

    Maintains separate bandits per token pair, so the system learns which
    DEX is most reliable for each specific market.
    """

    def __init__(self, config: dict) -> None:
        ts_cfg = config.get("algorithms", {}).get("thompson", {})
        self.alpha_init = ts_cfg.get("alpha_init", 1.0)
        self.beta_init  = ts_cfg.get("beta_init",  1.0)
        self.decay      = ts_cfg.get("decay_rate", 0.995)

        # Maps (token_in, token_out) → ThompsonBandit over DEX names
        self._bandits: dict[tuple[str, str], ThompsonBandit] = {}

    def _get_bandit(self, token_in: str, token_out: str) -> ThompsonBandit:
        key = (token_in, token_out)
        if key not in self._bandits:
            self._bandits[key] = ThompsonBandit(
                arms=["uniswap_v3", "curve", "balancer", "camelot_v3"],
                alpha_init=self.alpha_init,
                beta_init=self.beta_init,
                decay_rate=self.decay
            )
        return self._bandits[key]

    def select_dex(self, token_in: str, token_out: str) -> str:
        """Select the best DEX for this token pair via Thompson Sampling."""
        return self._get_bandit(token_in, token_out).select()

    def select_top_dexes(self, token_in: str, token_out: str, k: int = 2) -> list[str]:
        """Return top-k DEXes for this pair."""
        return self._get_bandit(token_in, token_out).select_top_k(k)

    def record_outcome(
        self,
        token_in: str,
        token_out: str,
        dex: str,
        profit_eth: float,
        max_expected_profit_eth: float = 1.0
    ) -> None:
        """Record a trade outcome and update posteriors."""
        # Normalise reward to [0, 1]
        reward = max(0.0, profit_eth / max(max_expected_profit_eth, 1e-9))
        reward = min(reward, 1.0)
        self._get_bandit(token_in, token_out).update(dex, reward)

    def record_failure(self, token_in: str, token_out: str, dex: str) -> None:
        """Record a complete failure (reverted tx, slippage exceeded, etc.)."""
        self._get_bandit(token_in, token_out).update(dex, -1.0)

    def get_rankings(self) -> dict[tuple[str, str], list[dict]]:
        return {k: b.stats_table() for k, b in self._bandits.items()}
