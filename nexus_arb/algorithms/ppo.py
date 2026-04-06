"""
PPO — Proximal Policy Optimization for Execution Timing.

Theory (Schulman et al. 2017):
  PPO is an on-policy actor-critic RL algorithm. The actor π_θ(a|s)
  outputs action probabilities; the critic V_φ(s) estimates state value.

  Objective (clipped surrogate):
    L_CLIP = E[min(r_t(θ) · Â_t, clip(r_t(θ), 1-ε, 1+ε) · Â_t)]
  where r_t(θ) = π_θ(a|s) / π_θ_old(a|s) is the probability ratio
  and Â_t = advantage estimate from GAE.

State space (what the agent observes):
  [spread_mean, spread_std, gas_price_norm, block_utilization,
   time_since_opportunity, wallet_balance_norm, ukf_velocity_norm,
   recent_success_rate]

Action space (discrete):
  0 = EXECUTE  — submit the flash loan transaction now
  1 = WAIT     — skip this block, re-evaluate next
  2 = SKIP     — abandon this opportunity

Reward:
  EXECUTE + profit  → R = profit_usd / 100
  EXECUTE + loss    → R = loss_usd / 10   (harsher penalty)
  WAIT (opp died)   → R = -0.01           (opportunity cost)
  SKIP              → R = 0               (neutral)

The agent learns to:
  - Execute when spreads are large and gas is low
  - Wait when gas spikes temporarily
  - Skip when profitability is marginal (not worth the gas)
"""

from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions import Categorical

log = logging.getLogger(__name__)

EXECUTE = 0
WAIT    = 1
SKIP    = 2
N_ACTIONS = 3

STATE_DIM = 8   # Must match features below


# ─── Neural Network Architecture ─────────────────────────────

class ActorCritic(nn.Module):
    """
    Shared-backbone Actor-Critic network.

    Architecture:
      Backbone: [LayerNorm → Linear(512) → GELU → Linear(256) → GELU]
      Actor head: Linear(256 → 128) → GELU → Linear(128 → 3) → Softmax
      Critic head: Linear(256 → 128) → GELU → Linear(128 → 1)

    Uses GELU activation (smooth, outperforms ReLU in value estimation tasks).
    LayerNorm for input normalisation (crucial for numerical stability).
    """

    def __init__(self, state_dim: int = STATE_DIM, hidden_dim: int = 256) -> None:
        super().__init__()

        self.backbone = nn.Sequential(
            nn.LayerNorm(state_dim),
            nn.Linear(state_dim, 512),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(512, hidden_dim),
            nn.GELU()
        )

        self.actor_head = nn.Sequential(
            nn.Linear(hidden_dim, 128),
            nn.GELU(),
            nn.Linear(128, N_ACTIONS)
        )

        self.critic_head = nn.Sequential(
            nn.Linear(hidden_dim, 128),
            nn.GELU(),
            nn.Linear(128, 1)
        )

        # Orthogonal initialisation (standard for RL)
        self._init_weights()

    def _init_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.orthogonal_(m.weight, gain=np.sqrt(2))
                nn.init.constant_(m.bias, 0.0)
        # Scale actor head output down
        nn.init.orthogonal_(self.actor_head[-1].weight, gain=0.01)

    def forward(self, x: torch.Tensor):
        feat   = self.backbone(x)
        logits = self.actor_head(feat)
        value  = self.critic_head(feat).squeeze(-1)
        return logits, value

    def act(self, state: np.ndarray) -> tuple[int, float, float]:
        """Sample action from policy."""
        x = torch.FloatTensor(state).unsqueeze(0)
        with torch.no_grad():
            logits, value = self.forward(x)
        dist   = Categorical(logits=logits)
        action = dist.sample()
        log_prob = dist.log_prob(action)
        return int(action.item()), float(log_prob.item()), float(value.item())

    def evaluate(
        self,
        states: torch.Tensor,
        actions: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Evaluate batch of states/actions for PPO update."""
        logits, values = self.forward(states)
        dist      = Categorical(logits=logits)
        log_probs = dist.log_prob(actions)
        entropy   = dist.entropy()
        return log_probs, values, entropy


# ─── Trajectory Buffer ────────────────────────────────────────

@dataclass
class Transition:
    state:    np.ndarray
    action:   int
    reward:   float
    done:     bool
    log_prob: float
    value:    float


class RolloutBuffer:
    """Stores transitions for PPO update."""

    def __init__(self, maxlen: int = 2048) -> None:
        self.buffer: deque[Transition] = deque(maxlen=maxlen)

    def add(self, t: Transition) -> None:
        self.buffer.append(t)

    def clear(self) -> None:
        self.buffer.clear()

    def __len__(self) -> int:
        return len(self.buffer)

    def compute_gae(
        self,
        last_value: float,
        gamma: float = 0.99,
        lam: float = 0.95
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Generalised Advantage Estimation (GAE-λ).

        Â_t = ∑_{l=0}^{∞} (γλ)^l · δ_{t+l}
        where δ_t = r_t + γ·V(s_{t+1}) - V(s_t)
        """
        transitions = list(self.buffer)
        n = len(transitions)

        states    = np.array([t.state    for t in transitions], dtype=np.float32)
        actions   = np.array([t.action   for t in transitions], dtype=np.int64)
        rewards   = np.array([t.reward   for t in transitions], dtype=np.float32)
        log_probs = np.array([t.log_prob for t in transitions], dtype=np.float32)
        values    = np.array([t.value    for t in transitions], dtype=np.float32)
        dones     = np.array([t.done     for t in transitions], dtype=np.float32)

        advantages = np.zeros(n, dtype=np.float32)
        gae = 0.0
        for t in reversed(range(n)):
            next_val = values[t + 1] if t < n - 1 else last_value
            delta = rewards[t] + gamma * next_val * (1 - dones[t]) - values[t]
            gae   = delta + gamma * lam * (1 - dones[t]) * gae
            advantages[t] = gae

        returns = advantages + values
        # Normalise advantages
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        return states, actions, returns, log_probs, advantages


# ─── PPO Agent ────────────────────────────────────────────────

class PPOAgent:
    """
    Full PPO agent for execution timing.

    Workflow:
      1. At each opportunity: encode state, call select_action()
      2. Execute trade (or wait/skip)
      3. Observe reward, call store_transition()
      4. Every update_interval steps: call update()
    """

    def __init__(self, config: dict) -> None:
        ppo_cfg = config.get("algorithms", {}).get("ppo", {})
        self.hidden_dim    = ppo_cfg.get("hidden_dim", 256)
        self.lr_actor      = ppo_cfg.get("lr_actor", 3e-4)
        self.lr_critic     = ppo_cfg.get("lr_critic", 1e-3)
        self.gamma         = ppo_cfg.get("gamma", 0.99)
        self.gae_lambda    = ppo_cfg.get("gae_lambda", 0.95)
        self.clip_eps      = ppo_cfg.get("clip_epsilon", 0.2)
        self.entropy_coef  = ppo_cfg.get("entropy_coef", 0.01)
        self.update_epochs = ppo_cfg.get("update_epochs", 10)
        self.batch_size    = ppo_cfg.get("batch_size", 64)
        self.checkpoint    = ppo_cfg.get("checkpoint_path", "data/ppo_checkpoint.pt")

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.net = ActorCritic(STATE_DIM, self.hidden_dim).to(self.device)
        self.optimizer = optim.Adam([
            {"params": self.net.actor_head.parameters(),  "lr": self.lr_actor},
            {"params": self.net.critic_head.parameters(), "lr": self.lr_critic},
            {"params": self.net.backbone.parameters(),    "lr": self.lr_actor}
        ])

        self.buffer = RolloutBuffer(maxlen=4096)
        self._steps   = 0
        self._updates = 0

        self._load_checkpoint()

    def _load_checkpoint(self) -> None:
        if os.path.exists(self.checkpoint):
            try:
                ckpt = torch.load(self.checkpoint, map_location=self.device)
                self.net.load_state_dict(ckpt["model"])
                self.optimizer.load_state_dict(ckpt["optimizer"])
                self._updates = ckpt.get("updates", 0)
                log.info(f"PPO checkpoint loaded ({self._updates} updates)")
            except Exception as e:
                log.warning(f"PPO checkpoint load failed: {e}")

    def _save_checkpoint(self) -> None:
        os.makedirs(os.path.dirname(self.checkpoint), exist_ok=True)
        torch.save({
            "model":     self.net.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "updates":   self._updates
        }, self.checkpoint)

    def encode_state(
        self,
        spread_mean: float,
        spread_std: float,
        gas_price_gwei: float,
        block_utilization: float,
        time_since_opp_ms: float,
        wallet_balance_eth: float,
        ukf_velocity: float,
        recent_success_rate: float
    ) -> np.ndarray:
        """
        Normalise raw observations into [0,1] feature vector.
        These normalization constants are calibrated for Arbitrum.
        """
        return np.array([
            np.clip(spread_mean / 0.02, 0, 1),         # Spread 0-2%
            np.clip(spread_std  / 0.01, 0, 1),          # Spread volatility
            np.clip(gas_price_gwei / 2.0, 0, 1),        # Gas 0-2 gwei
            np.clip(block_utilization, 0, 1),            # Block fullness
            np.clip(time_since_opp_ms / 500.0, 0, 1),   # TTL fraction
            np.clip(wallet_balance_eth / 10.0, 0, 1),   # Wallet size
            np.tanh(ukf_velocity * 100),                 # Normalised price velocity
            np.clip(recent_success_rate, 0, 1)           # Historical success
        ], dtype=np.float32)

    def select_action(self, state: np.ndarray) -> tuple[int, float, float]:
        """
        Select action via current policy.
        Returns (action, log_prob, value_estimate)
        """
        return self.net.act(state)

    def store_transition(
        self,
        state: np.ndarray,
        action: int,
        reward: float,
        done: bool,
        log_prob: float,
        value: float
    ) -> None:
        self.buffer.add(Transition(state, action, reward, done, log_prob, value))
        self._steps += 1

    def update(self) -> Optional[dict]:
        """
        Run PPO update on accumulated rollout.
        Returns dict of losses for monitoring.
        """
        if len(self.buffer) < self.batch_size:
            return None

        # Get last value estimate for GAE bootstrapping
        with torch.no_grad():
            last_state = torch.FloatTensor(
                self.buffer.buffer[-1].state
            ).unsqueeze(0).to(self.device)
            _, last_val = self.net.forward(last_state)
            last_value = float(last_val.item())

        states, actions, returns, old_log_probs, advantages = self.buffer.compute_gae(
            last_value, self.gamma, self.gae_lambda
        )

        states_t     = torch.FloatTensor(states).to(self.device)
        actions_t    = torch.LongTensor(actions).to(self.device)
        returns_t    = torch.FloatTensor(returns).to(self.device)
        old_lp_t     = torch.FloatTensor(old_log_probs).to(self.device)
        advantages_t = torch.FloatTensor(advantages).to(self.device)

        total_loss_p = total_loss_v = total_entropy = 0.0
        n_batches = 0

        for _ in range(self.update_epochs):
            idx = torch.randperm(len(states_t))
            for start in range(0, len(states_t), self.batch_size):
                b_idx   = idx[start:start + self.batch_size]
                b_st    = states_t[b_idx]
                b_ac    = actions_t[b_idx]
                b_ret   = returns_t[b_idx]
                b_olp   = old_lp_t[b_idx]
                b_adv   = advantages_t[b_idx]

                new_lp, values, entropy = self.net.evaluate(b_st, b_ac)

                # Probability ratio
                ratio = torch.exp(new_lp - b_olp)

                # Clipped surrogate loss
                surr1 = ratio * b_adv
                surr2 = torch.clamp(ratio, 1 - self.clip_eps, 1 + self.clip_eps) * b_adv
                loss_p = -torch.min(surr1, surr2).mean()

                # Value function loss
                loss_v = 0.5 * (values - b_ret).pow(2).mean()

                # Entropy bonus (exploration)
                loss_e = -entropy.mean()

                loss = loss_p + 0.5 * loss_v + self.entropy_coef * loss_e

                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.net.parameters(), 0.5)
                self.optimizer.step()

                total_loss_p += loss_p.item()
                total_loss_v += loss_v.item()
                total_entropy += (-loss_e).item()
                n_batches += 1

        self.buffer.clear()
        self._updates += 1

        if self._updates % 10 == 0:
            self._save_checkpoint()

        return {
            "policy_loss": total_loss_p / max(n_batches, 1),
            "value_loss":  total_loss_v / max(n_batches, 1),
            "entropy":     total_entropy / max(n_batches, 1),
            "updates":     self._updates
        }
