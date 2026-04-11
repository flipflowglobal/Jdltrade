//! Arbitrage cycle detection via Floyd-Warshall-style enumeration.
//! Finds 2-hop and 3-hop cycles where the product of rates > 1 (profit).

use ethers::types::Address;
use std::collections::HashMap;

/// A directed edge in the price graph between two tokens on a specific DEX.
#[derive(Clone, Debug)]
pub struct Edge {
    pub from:      Address,
    pub to:        Address,
    pub dex:       String,
    /// Negative log of the exchange rate: -ln(rate).
    /// A negative cycle in this space = arbitrage opportunity.
    pub log_rate:  f64,
    pub liquidity: ethers::types::U256,
}

/// Detect arbitrage cycles among `tokens` given the `edges` (price quotes).
///
/// Returns cycles as ordered sequences of token *indices* into `tokens`.
/// The first and last element of each cycle are the same (closed loop).
/// e.g. `[0, 2, 1, 0]` = WETH → USDT → USDC → WETH
///
/// Threshold: > 0.2% gross gain for 2-hop, > 0.3% for 3-hop (to exceed typical fees).
pub fn bellman_ford(tokens: &[Address], edges: &[Edge]) -> Vec<Vec<usize>> {
    let n = tokens.len();
    if n < 2 || edges.is_empty() {
        return vec![];
    }

    // Index map: Address → position in `tokens`
    let idx: HashMap<Address, usize> = tokens
        .iter()
        .enumerate()
        .map(|(i, &a)| (a, i))
        .collect();

    // Build best-rate matrix: rate[i][j] = best exchange rate for i → j across all DEXes
    let mut rate = vec![vec![0.0f64; n]; n];
    for edge in edges {
        let (Some(&u), Some(&v)) = (idx.get(&edge.from), idx.get(&edge.to)) else {
            continue;
        };
        // Recover actual rate from log_rate: rate = e^(-log_rate)
        let r = (-edge.log_rate).exp();
        if r > rate[u][v] {
            rate[u][v] = r;
        }
    }

    let mut cycles: Vec<Vec<usize>> = Vec::new();

    // ── 2-hop: i → j → i ─────────────────────────────
    for i in 0..n {
        for j in (i + 1)..n {
            if rate[i][j] > 0.0 && rate[j][i] > 0.0 {
                let gain = rate[i][j] * rate[j][i];
                if gain > 1.002 {
                    // > 0.2% net (covers Aave 0.05% + UniV2 0.3% × 2 hops edge case)
                    cycles.push(vec![i, j, i]);
                }
            }
        }
    }

    // ── 3-hop: i → j → k → i ─────────────────────────
    for i in 0..n {
        for j in 0..n {
            if i == j || rate[i][j] == 0.0 {
                continue;
            }
            for k in 0..n {
                if k == i || k == j || rate[j][k] == 0.0 || rate[k][i] == 0.0 {
                    continue;
                }
                let gain = rate[i][j] * rate[j][k] * rate[k][i];
                if gain > 1.003 {
                    // > 0.3% net for 3-hop (higher threshold due to 3× fees)
                    cycles.push(vec![i, j, k, i]);
                }
            }
        }
    }

    // Sort: most profitable first (descending gain)
    cycles.sort_by(|a, b| {
        let gain_a = cycle_gain(&rate, a);
        let gain_b = cycle_gain(&rate, b);
        gain_b.partial_cmp(&gain_a).unwrap_or(std::cmp::Ordering::Equal)
    });

    // Limit to top 5 opportunities per scan to avoid overloading the executor
    cycles.truncate(5);
    cycles
}

fn cycle_gain(rate: &[Vec<f64>], cycle: &[usize]) -> f64 {
    if cycle.len() < 2 {
        return 0.0;
    }
    cycle.windows(2).fold(1.0, |acc, w| acc * rate[w[0]][w[1]])
}
