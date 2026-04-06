/*!
 * nexus_core — High-performance Rust extension for NEXUS-ARB.
 *
 * Exposes Python bindings via PyO3 for:
 *   1. Bellman-Ford arbitrage detection (O(V·E) with early exit)
 *   2. CMA-ES profit surface optimisation (vectorized NumPy)
 *   3. Price graph utilities (log-price weight computation)
 *
 * All hot paths are parallelized with Rayon where beneficial.
 *
 * Build: cd nexus_core && maturin develop --release
 * Usage: import nexus_core; result = nexus_core.bellman_ford_detect(...)
 */

use pyo3::prelude::*;

mod bellman_ford;
mod matrix_ops;
mod price_utils;

use bellman_ford::PyArbitrageResult;

/// Main Python module entry point.
#[pymodule]
fn nexus_core(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bellman_ford::detect_arbitrage, m)?)?;
    m.add_function(wrap_pyfunction!(matrix_ops::compute_covariance, m)?)?;
    m.add_function(wrap_pyfunction!(matrix_ops::cholesky_decompose, m)?)?;
    m.add_function(wrap_pyfunction!(price_utils::log_price_weights, m)?)?;
    m.add_function(wrap_pyfunction!(price_utils::sqrt_price_x96_to_float, m)?)?;
    m.add_class::<PyArbitrageResult>()?;
    Ok(())
}
