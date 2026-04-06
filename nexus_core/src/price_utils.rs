/*!
 * Price utility functions for Uniswap V3 and general use.
 */

use pyo3::prelude::*;

/// Convert Uniswap V3 sqrtPriceX96 to human-readable float price.
///
/// sqrtPriceX96 encodes sqrt(token1/token0) * 2^96.
/// price = (sqrtPriceX96 / 2^96)^2 * 10^(dec0-dec1)
///
/// Args:
///   sqrt_price_x96: Raw sqrtPriceX96 from slot0()
///   token0_decimals: Decimal places for token0
///   token1_decimals: Decimal places for token1
///   invert: If true, return token0-per-token1 instead of token1-per-token0
///
/// Returns: Price as f64
#[pyfunction]
pub fn sqrt_price_x96_to_float(
    _py: Python,
    sqrt_price_x96: u128,
    token0_decimals: u32,
    token1_decimals: u32,
    invert: bool,
) -> f64 {
    const Q96: f64 = 79228162514264337593543950336.0;  // 2^96

    let sqrt_price = sqrt_price_x96 as f64 / Q96;
    let price_raw  = sqrt_price * sqrt_price;

    // Adjust for decimal difference
    let decimal_adj = 10_f64.powi(token0_decimals as i32 - token1_decimals as i32);
    let price = price_raw * decimal_adj;

    if invert {
        if price > 0.0 { 1.0 / price } else { 0.0 }
    } else {
        price
    }
}

/// Compute log-price weights for all edges in the price graph.
///
/// Input: list of (price_after_fee,) per edge
/// Output: list of -ln(price) per edge
///
/// Vectorized for fast graph construction.
#[pyfunction]
pub fn log_price_weights(_py: Python, prices: Vec<f64>) -> Vec<f64> {
    prices
        .into_iter()
        .map(|p| if p > 0.0 { -p.ln() } else { f64::INFINITY })
        .collect()
}
