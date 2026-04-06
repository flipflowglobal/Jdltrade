/*!
 * Matrix operations for CMA-ES and UKF.
 *
 * Performance-critical matrix routines implemented in Rust
 * and exposed to Python via numpy arrays.
 */

use pyo3::prelude::*;
use pyo3::types::PyList;

/// Compute covariance matrix from a batch of samples.
/// samples: n_samples × n_dims (row-major, flat Vec<f64>)
/// Returns: n_dims × n_dims covariance matrix (flat Vec<f64>)
#[pyfunction]
pub fn compute_covariance(
    _py: Python,
    samples: Vec<f64>,
    n_samples: usize,
    n_dims: usize,
) -> PyResult<Vec<f64>> {
    if samples.len() != n_samples * n_dims {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "samples.len() != n_samples * n_dims"
        ));
    }

    let mut mean = vec![0.0_f64; n_dims];
    for s in 0..n_samples {
        for d in 0..n_dims {
            mean[d] += samples[s * n_dims + d];
        }
    }
    for d in 0..n_dims {
        mean[d] /= n_samples as f64;
    }

    let mut cov = vec![0.0_f64; n_dims * n_dims];
    for s in 0..n_samples {
        for i in 0..n_dims {
            let xi = samples[s * n_dims + i] - mean[i];
            for j in 0..n_dims {
                let xj = samples[s * n_dims + j] - mean[j];
                cov[i * n_dims + j] += xi * xj;
            }
        }
    }
    let norm = (n_samples - 1).max(1) as f64;
    for v in &mut cov {
        *v /= norm;
    }

    Ok(cov)
}

/// Cholesky decomposition L such that A = L·Lᵀ.
/// A must be symmetric positive-definite (n×n, flat row-major).
/// Returns L (lower triangular, flat row-major).
#[pyfunction]
pub fn cholesky_decompose(
    _py: Python,
    a: Vec<f64>,
    n: usize,
) -> PyResult<Vec<f64>> {
    if a.len() != n * n {
        return Err(pyo3::exceptions::PyValueError::new_err("a.len() != n*n"));
    }

    let mut l = vec![0.0_f64; n * n];

    for i in 0..n {
        for j in 0..=i {
            let mut sum = a[i * n + j];
            for k in 0..j {
                sum -= l[i * n + k] * l[j * n + k];
            }
            if i == j {
                if sum <= 0.0 {
                    // Add small regularization if not PD
                    l[i * n + j] = (sum.abs() + 1e-9).sqrt();
                } else {
                    l[i * n + j] = sum.sqrt();
                }
            } else {
                let ljj = l[j * n + j];
                if ljj.abs() < 1e-14 {
                    l[i * n + j] = 0.0;
                } else {
                    l[i * n + j] = sum / ljj;
                }
            }
        }
    }

    Ok(l)
}
