"""
Unscented Kalman Filter (UKF) for Price State Estimation.

Theory (Julier & Uhlmann 1997):
  The UKF approximates a nonlinear Gaussian state-space model using the
  Unscented Transform: 2n+1 sigma points are propagated through the
  nonlinear function, capturing mean and covariance to 3rd order.

State vector:  x = [price, velocity, acceleration, log_spread]
Observation:   z = [observed_price, gas_price_gwei]

The UKF provides:
  1. Optimal price estimate with uncertainty bounds
  2. Price velocity (trend direction)
  3. Spread estimate for profitability forecasting
  4. Gas price smoothing

Applications in NEXUS-ARB:
  - Price direction filter: skip if UKF predicts adverse movement
  - Uncertainty-adjusted profitability: scale position by 1/σ_price
  - Gas price prediction: 1-block lookahead for timing execution
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class UKFState:
    price: float
    velocity: float
    acceleration: float
    log_spread: float
    price_std: float          # 1σ price uncertainty
    velocity_std: float       # 1σ velocity uncertainty
    is_trending_up: bool
    prediction_1block: float  # Predicted price 1 block ahead


class PriceUKF:
    """
    4-dimensional Unscented Kalman Filter for price dynamics.

    State: x = [price, ṗ (velocity), p̈ (acceleration), log_spread]
    Observation: z = [price_obs]

    Process model (constant-acceleration):
      price_t+1     = price_t + dt * ṗ_t + 0.5 * dt² * p̈_t
      ṗ_t+1        = ṗ_t + dt * p̈_t
      p̈_t+1       = p̈_t   (slowly varying)
      log_spread_t+1 = log_spread_t  (slowly varying)

    dt = Arbitrum block time ≈ 0.25s
    """

    def __init__(
        self,
        config: dict,
        dt: float = 0.25,       # Arbitrum block time seconds
        dim_x: int = 4,
        dim_z: int = 1
    ) -> None:
        ukf_cfg = config.get("algorithms", {}).get("ukf", {})

        self.dt    = dt
        self.n     = dim_x
        self.m     = dim_z
        self.initialized = False

        # UKF tuning parameters
        alpha  = ukf_cfg.get("alpha", 1e-3)
        beta   = ukf_cfg.get("beta",  2.0)
        kappa  = ukf_cfg.get("kappa", 0.0)
        lam    = alpha ** 2 * (self.n + kappa) - self.n

        # Sigma point weights
        n = self.n
        self.Wm = np.full(2 * n + 1, 1.0 / (2 * (n + lam)))
        self.Wm[0] = lam / (n + lam)
        self.Wc = self.Wm.copy()
        self.Wc[0] += (1 - alpha ** 2 + beta)
        self.lambda_ = lam
        self.alpha   = alpha

        # Process noise covariance Q
        q = ukf_cfg.get("process_noise", 0.001)
        self.Q = np.diag([q, q * 10, q * 100, q * 0.1])

        # Measurement noise covariance R
        r = ukf_cfg.get("measurement_noise", 0.01)
        self.R = np.array([[r]])

        # State and covariance
        self.x = np.zeros(self.n)
        self.P = np.eye(self.n) * 1000   # Large initial uncertainty

    def _state_transition(self, x: np.ndarray) -> np.ndarray:
        """Nonlinear process model f(x)."""
        dt = self.dt
        price, vel, acc, log_spread = x
        new_price      = price + dt * vel + 0.5 * dt ** 2 * acc
        new_vel        = vel + dt * acc
        new_acc        = acc * 0.95            # Mean-reverting acceleration
        new_log_spread = log_spread * 0.99     # Slowly decaying spread
        return np.array([new_price, new_vel, new_acc, new_log_spread])

    def _observation_model(self, x: np.ndarray) -> np.ndarray:
        """Observation function h(x) = price."""
        return np.array([x[0]])

    def _sigma_points(self, x: np.ndarray, P: np.ndarray) -> np.ndarray:
        """Generate 2n+1 sigma points around x with covariance P."""
        n = self.n
        lam = self.lambda_
        try:
            L = np.linalg.cholesky((n + lam) * P)
        except np.linalg.LinAlgError:
            # Fallback: regularize P
            P += np.eye(n) * 1e-6
            L = np.linalg.cholesky((n + lam) * P)

        sigmas = np.zeros((2 * n + 1, n))
        sigmas[0] = x
        for i in range(n):
            sigmas[i + 1]     = x + L[:, i]
            sigmas[n + i + 1] = x - L[:, i]
        return sigmas

    def initialize(self, price: float, spread: float = 0.001) -> None:
        """Initialize filter with first observation."""
        self.x = np.array([price, 0.0, 0.0, np.log(max(spread, 1e-8))])
        self.P = np.diag([price * 0.01, price * 0.001, price * 0.0001, 1.0])
        self.initialized = True

    def update(self, price_obs: float) -> UKFState:
        """
        Perform one predict-update cycle.

        Args:
            price_obs: Observed price from DEX

        Returns:
            Updated UKFState with estimates and uncertainty
        """
        if not self.initialized:
            self.initialize(price_obs)
            return self._to_state()

        # ── Predict Step ──────────────────────────────────────
        sigmas = self._sigma_points(self.x, self.P)

        # Propagate sigma points through process model
        sigmas_f = np.array([self._state_transition(s) for s in sigmas])

        # Predicted mean and covariance
        x_pred = np.dot(self.Wm, sigmas_f)
        P_pred = self.Q.copy()
        for i, s in enumerate(sigmas_f):
            d = s - x_pred
            P_pred += self.Wc[i] * np.outer(d, d)

        # ── Update Step ───────────────────────────────────────
        sigmas_h = np.array([self._observation_model(s) for s in sigmas_f])

        # Predicted measurement mean
        z_pred = np.dot(self.Wm, sigmas_h)

        # Innovation covariance
        S = self.R.copy()
        for i, s in enumerate(sigmas_h):
            d = s - z_pred
            S += self.Wc[i] * np.outer(d, d)

        # Cross-covariance
        Pxz = np.zeros((self.n, self.m))
        for i, (sf, sh) in enumerate(zip(sigmas_f, sigmas_h)):
            Pxz += self.Wc[i] * np.outer(sf - x_pred, sh - z_pred)

        # Kalman gain
        K = Pxz @ np.linalg.inv(S)

        # Innovation
        z = np.array([price_obs])
        innovation = z - z_pred

        # Posterior
        self.x = x_pred + K @ innovation
        self.P = P_pred - K @ S @ K.T

        # Ensure symmetry and positive definiteness
        self.P = 0.5 * (self.P + self.P.T)
        self.P += np.eye(self.n) * 1e-10

        return self._to_state()

    def _to_state(self) -> UKFState:
        price, vel, acc, log_spread = self.x
        P_diag = np.diag(self.P)
        dt = self.dt
        pred_price = price + dt * vel + 0.5 * dt ** 2 * acc

        return UKFState(
            price=float(price),
            velocity=float(vel),
            acceleration=float(acc),
            log_spread=float(log_spread),
            price_std=float(np.sqrt(max(P_diag[0], 0))),
            velocity_std=float(np.sqrt(max(P_diag[1], 0))),
            is_trending_up=float(vel) > 0,
            prediction_1block=float(pred_price)
        )

    def predict_n_blocks(self, n: int) -> float:
        """Multi-step price prediction."""
        x = self.x.copy()
        for _ in range(n):
            x = self._state_transition(x)
        return float(x[0])


class MultiTokenUKF:
    """Manages one UKF per (token_in, token_out) pair."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self._filters: dict[tuple[str, str], PriceUKF] = {}

    def update(self, token_in: str, token_out: str, price: float) -> UKFState:
        key = (token_in, token_out)
        if key not in self._filters:
            self._filters[key] = PriceUKF(self.config)
        return self._filters[key].update(price)

    def is_price_moving_favorably(
        self,
        token_in: str,
        token_out: str
    ) -> bool:
        """True if price is increasing (we benefit from buying token_out now)."""
        key = (token_in, token_out)
        if key not in self._filters:
            return True  # No data = assume favorable
        state = self._filters[key]._to_state()
        return state.is_trending_up
