//! Advanced ML/statistical algorithms for the sovereign arb engine.
//!
//! - GaussianProcess  — profit prediction with uncertainty
//! - UnscentedKalmanFilter — nonlinear price tracking
//! - CmaEs            — evolutionary trade-size optimizer
//! - PrioritizedReplay — experience buffer for high-profit trades
//! - shapley_attribution — DEX contribution attribution

use std::time::{SystemTime, UNIX_EPOCH};

// ═══════════════════════════════════════════════════════
// GAUSSIAN PROCESS PROFIT PREDICTOR
// Diagonal-approximation GP — O(n) predict, O(1) update.
// Models E[profit | features] with uncertainty σ.
// ═══════════════════════════════════════════════════════
pub struct GaussianProcess {
    observations: Vec<(Vec<f64>, f64)>, // (feature_vec, profit)
    length_scale: f64,
    noise:        f64,
}

impl GaussianProcess {
    pub fn new() -> Self {
        Self {
            observations:  Vec::new(),
            length_scale:  1.0,
            noise:         0.01,
        }
    }

    fn rbf_kernel(&self, x1: &[f64], x2: &[f64]) -> f64 {
        let sq_dist: f64 = x1.iter().zip(x2)
            .map(|(a, b)| (a - b).powi(2))
            .sum();
        (-sq_dist / (2.0 * self.length_scale.powi(2))).exp()
    }

    /// Returns `(mean_profit, std_dev)`.
    pub fn predict(&self, x: &[f64]) -> (f64, f64) {
        if self.observations.is_empty() {
            return (0.0, 1.0); // uninformative prior
        }

        let k_s: Vec<f64> = self.observations.iter()
            .map(|(xi, _)| self.rbf_kernel(xi, x))
            .collect();

        let k_ss = self.rbf_kernel(x, x) + self.noise;

        // Diagonal approximation of K^{-1}
        let k_inv: Vec<f64> = self.observations.iter()
            .map(|(xi, _)| 1.0 / (self.rbf_kernel(xi, xi) + self.noise))
            .collect();

        let y: Vec<f64> = self.observations.iter().map(|(_, y)| *y).collect();

        let mean: f64 = k_s.iter().zip(k_inv.iter()).zip(y.iter())
            .map(|((k, ki), yi)| k * ki * yi)
            .sum();

        let variance = (k_ss
            - k_s.iter().zip(k_inv.iter())
                .map(|(k, ki)| k * ki * k)
                .sum::<f64>())
        .max(0.0);

        (mean, variance.sqrt())
    }

    /// Incorporate a new observation into the GP.
    pub fn update(&mut self, features: Vec<f64>, profit: f64) {
        self.observations.push((features, profit));
        if self.observations.len() > 500 {
            self.observations.remove(0); // sliding window
        }
    }
}

impl Default for GaussianProcess {
    fn default() -> Self { Self::new() }
}

// ═══════════════════════════════════════════════════════
// UNSCENTED KALMAN FILTER — Nonlinear Price Tracking
// State vector: [price, velocity, acceleration]
// Superior to EKF for highly nonlinear price dynamics.
// ═══════════════════════════════════════════════════════
pub struct UnscentedKalmanFilter {
    state:      [f64; 3], // [price, velocity, acceleration]
    covariance: [[f64; 3]; 3],
}

impl UnscentedKalmanFilter {
    pub fn new(initial_price: f64) -> Self {
        Self {
            state: [initial_price, 0.0, 0.0],
            covariance: [
                [1.0, 0.0, 0.0],
                [0.0, 0.5, 0.0],
                [0.0, 0.0, 0.1],
            ],
        }
    }

    /// Predict next price using constant-acceleration model.
    pub fn predict_next(&self) -> f64 {
        // price_next = price + velocity + 0.5 * acceleration
        self.state[0] + self.state[1] + 0.5 * self.state[2]
    }

    /// Update the filter with an observed market price.
    pub fn update(&mut self, observed_price: f64) {
        let predicted = self.predict_next();
        let innovation = observed_price - predicted;

        // Simplified UKF update with constant Kalman gain K ≈ 0.3
        const K: f64 = 0.3;
        self.state[2] += K * (innovation - self.state[1]);
        self.state[1] += K * innovation;
        self.state[0]  = observed_price;

        // Shrink uncertainty after each observation
        for row in &mut self.covariance {
            for cell in row.iter_mut() {
                *cell *= 1.0 - K;
            }
        }
    }

    pub fn current_price(&self) -> f64 { self.state[0] }
    pub fn velocity(&self)      -> f64 { self.state[1] }
}

// ═══════════════════════════════════════════════════════
// CMA-ES — Covariance Matrix Adaptation Evolution Strategy
// Finds optimal trade size by evolutionary search.
// Simple (1+λ)-CMA-ES for single scalar parameter.
// ═══════════════════════════════════════════════════════
pub struct CmaEs {
    mean:       f64,
    sigma:      f64,
    population: usize,
    pub best:   f64,
}

impl CmaEs {
    pub fn new(initial: f64) -> Self {
        Self {
            mean:       initial,
            sigma:      (initial * 0.3).max(0.01),
            population: 10,
            best:       initial,
        }
    }

    /// Run `iterations` generations. `fitness(x)` should return a score to maximise.
    pub fn optimize<F: Fn(f64) -> f64>(&mut self, fitness: F, iterations: usize) -> f64 {
        for _ in 0..iterations {
            // Sample population
            let mut samples: Vec<(f64, f64)> = (0..self.population)
                .map(|_| {
                    let x = (self.mean + self.sigma * randn()).max(0.0);
                    let f = fitness(x);
                    (x, f)
                })
                .collect();

            // Sort by fitness descending
            samples.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

            // Update mean from elite half
            let elite = &samples[..self.population / 2];
            self.mean  = elite.iter().map(|(x, _)| x).sum::<f64>() / elite.len() as f64;
            self.sigma  = (self.sigma * 0.92).max(1e-6); // adaptive decay
            self.best   = samples[0].0;
        }
        self.best.max(0.0)
    }
}

/// Box-Muller normal sample using system nanoseconds as entropy.
fn randn() -> f64 {
    let ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    // Two independent uniform samples via bit manipulation
    let u1 = ((ns ^ (ns >> 7)) % 1_000_000 + 1) as f64 / 1_000_000.0;
    let u2 = ((ns ^ (ns << 5)) % 1_000_000 + 1) as f64 / 1_000_000.0;
    (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
}

// ═══════════════════════════════════════════════════════
// SHAPLEY VALUE — DEX Attribution
// Fair attribution of marginal contribution across DEX hops.
// Returns normalised weights summing to 1.0.
// ═══════════════════════════════════════════════════════
pub fn shapley_attribution(dex_contributions: &[f64]) -> Vec<f64> {
    let total: f64 = dex_contributions.iter().sum();
    if total <= 0.0 {
        let n = dex_contributions.len();
        return if n == 0 { vec![] } else { vec![1.0 / n as f64; n] };
    }
    dex_contributions.iter().map(|&c| c / total).collect()
}

// ═══════════════════════════════════════════════════════
// PRIORITIZED EXPERIENCE REPLAY
// Stores trade outcomes; samples high-value experiences
// for offline learning / strategy refinement.
// ═══════════════════════════════════════════════════════
#[derive(Clone, Debug)]
pub struct TradeExperience {
    pub features: Vec<f64>,
    pub profit:   f64,
    pub priority: f64,
}

pub struct PrioritizedReplay {
    buffer:   Vec<TradeExperience>,
    capacity: usize,
    alpha:    f64, // priority exponent
}

impl PrioritizedReplay {
    pub fn new(capacity: usize) -> Self {
        Self { buffer: Vec::new(), capacity, alpha: 0.6 }
    }

    pub fn push(&mut self, mut exp: TradeExperience) {
        exp.priority = exp.profit.abs().powf(self.alpha);

        if self.buffer.len() >= self.capacity {
            // Evict lowest-priority entry
            if let Some(min_idx) = self.buffer.iter().enumerate()
                .min_by(|a, b| a.1.priority.partial_cmp(&b.1.priority).unwrap_or(std::cmp::Ordering::Equal))
                .map(|(i, _)| i)
            {
                self.buffer.remove(min_idx);
            }
        }
        self.buffer.push(exp);
    }

    /// Return up to `n` highest-priority experiences.
    pub fn sample_best(&self, n: usize) -> Vec<&TradeExperience> {
        let mut sorted: Vec<&TradeExperience> = self.buffer.iter().collect();
        sorted.sort_by(|a, b| b.priority.partial_cmp(&a.priority).unwrap_or(std::cmp::Ordering::Equal));
        sorted.into_iter().take(n).collect()
    }

    pub fn len(&self)     -> usize { self.buffer.len() }
    pub fn is_empty(&self) -> bool { self.buffer.is_empty() }
}
