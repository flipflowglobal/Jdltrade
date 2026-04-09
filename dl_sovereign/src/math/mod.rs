pub mod advanced;
mod graph;

pub use graph::{Edge, bellman_ford};

/// Kelly Criterion — optimal fractional position sizing.
///
/// `win_rate`    — estimated probability of profitable trade
/// `avg_win`     — average profit as a fraction (e.g. 0.009 = 0.9%)
/// `avg_loss`    — average loss as a fraction (e.g. 0.003 = 0.3%)
/// `max_fraction`— hard cap (e.g. 0.25 = never bet more than 25% of capital)
pub fn kelly_criterion(win_rate: f64, avg_win: f64, avg_loss: f64, max_fraction: f64) -> f64 {
    if avg_win <= 0.0 || avg_loss <= 0.0 || win_rate <= 0.0 {
        return 0.0;
    }
    let q = 1.0 - win_rate;
    // f* = (p / b_loss) - (q / b_win)
    let kelly = (win_rate / avg_loss) - (q / avg_win);
    kelly.max(0.0).min(max_fraction)
}

/// Exponential Weighted Moving Average of gas prices with a safety multiplier.
///
/// `alpha`             — smoothing factor (0 < α < 1); higher = more reactive
/// `safety_multiplier` — e.g. 1.12 bids 12% above EWMA to avoid replacement
pub fn ewma_gas(history: &[u64], alpha: f64, safety_multiplier: f64) -> u64 {
    if history.is_empty() {
        return 20_000_000_000; // 20 gwei fallback
    }
    let mut ewma = history[0] as f64;
    for &g in &history[1..] {
        ewma = alpha * g as f64 + (1.0 - alpha) * ewma;
    }
    let result = (ewma * safety_multiplier) as u64;
    result.max(1_000_000_000) // floor at 1 gwei
}
