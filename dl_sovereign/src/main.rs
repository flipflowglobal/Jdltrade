//! D.L Sovereign Flash Loan Arbitrage Engine v4.0
//!
//! Architecture:
//!   • Streams Ethereum mempool via WebSocket (Alchemy)
//!   • Quotes every token-pair on UniV2 + Sushi
//!   • Detects 2/3-hop arbitrage cycles (Floyd-Warshall)
//!   • GP predicts profit, UKF tracks price velocity, CMA-ES sizes the trade
//!   • Executes zero-capital flash loans via Aave V3 → SovereignArb.sol
//!   • All profit routed to WALLET_ADDRESS

use std::env;
use std::sync::Arc;

use dashmap::DashMap;
use dotenv::dotenv;
use ethers::prelude::*;
use eyre::Result;
use futures::StreamExt;
use tokio::sync::RwLock;

mod engine;
mod math;

use engine::compiler::SolcEngine;
use math::{bellman_ford, ewma_gas, kelly_criterion, Edge};
use math::advanced::{
    shapley_attribution, CmaEs, GaussianProcess, PrioritizedReplay, TradeExperience,
    UnscentedKalmanFilter,
};

// ─── Token universe (Ethereum mainnet) ────────────────────────────────────────
const WETH: &str = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2";
const USDC: &str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48";
const USDT: &str = "0xdAC17F958D2ee523a2206206994597C13D831ec7";
const DAI:  &str = "0x6B175474E89094C44Da98b954EedeAC495271d0F";
const WBTC: &str = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599";

// ─── ABIs ─────────────────────────────────────────────────────────────────────
// Human-readable ABI — no data location qualifiers (calldata/memory not supported here)
abigen!(
    IUniV2,
    r#"[function getAmountsOut(uint256,address[]) external view returns (uint256[])]"#
);

// ISovereignArb — encode executeArb calldata manually using ethers::abi.
// This avoids abigen entirely for the tuple type, which the inline ABI parser
// does not support. We build the 4-byte selector + ABI-encoded arguments.
fn encode_execute_arb(
    token:      Address,
    amount:     U256,
    path:       Vec<Address>,
    routers:    Vec<Address>,
    dex_types:  Vec<u8>,
    extra_data: Vec<Bytes>,
    min_profit: U256,
) -> Bytes {
    use ethers::abi::{encode, short_signature, ParamType, Token};

    // keccak256 first 4 bytes of canonical function signature
    let selector = short_signature(
        "executeArb",
        &[
            ParamType::Address,
            ParamType::Uint(256),
            ParamType::Tuple(vec![
                ParamType::Array(Box::new(ParamType::Address)),
                ParamType::Array(Box::new(ParamType::Address)),
                ParamType::Array(Box::new(ParamType::Uint(8))),
                ParamType::Array(Box::new(ParamType::Bytes)),
                ParamType::Uint(256),
            ]),
        ],
    );

    let params_tuple = Token::Tuple(vec![
        Token::Array(path.into_iter().map(Token::Address).collect()),
        Token::Array(routers.into_iter().map(Token::Address).collect()),
        Token::Array(dex_types.into_iter().map(|t| Token::Uint(U256::from(t))).collect()),
        Token::Array(
            extra_data.into_iter()
                .map(|b| Token::Bytes(b.to_vec()))
                .collect(),
        ),
        Token::Uint(min_profit),
    ]);

    let encoded = encode(&[Token::Address(token), Token::Uint(amount), params_tuple]);
    let mut calldata = selector.to_vec();
    calldata.extend_from_slice(&encoded);
    Bytes::from(calldata)
}

// ─── Main ─────────────────────────────────────────────────────────────────────
#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    tracing_subscriber::fmt::init();

    let ws_url      = env::var("ALCHEMY_WSS_URL").expect("Set ALCHEMY_WSS_URL in .env");
    let private_key = env::var("PRIVATE_KEY").expect("Set PRIVATE_KEY in .env");
    let wallet_addr = env::var("WALLET_ADDRESS").expect("Set WALLET_ADDRESS in .env");

    let wallet = private_key
        .parse::<LocalWallet>()?
        .with_chain_id(1u64);

    println!("╔═══════════════════════════════════════════════════════╗");
    println!("║   D.L SOVEREIGN FLASH LOAN ENGINE v4.0               ║");
    println!("╠═══════════════════════════════════════════════════════╣");
    println!("║  Wallet   : {}  ║", &wallet_addr);
    println!("║  Algo     : GP + UKF + CMA-ES + Floyd-Warshall       ║");
    println!("║  Flash    : Aave V3 Mainnet                           ║");
    println!("║  DEXes    : UniswapV2 + Sushiswap (+ V3/Curve ready) ║");
    println!("╚═══════════════════════════════════════════════════════╝\n");

    // ── 1. Compile + deploy SovereignArb ──────────────────────────────────────
    println!("[INIT] Compiling SovereignArb.sol...");
    let compiler     = SolcEngine::new();
    let compiled     = compiler.compile("contracts/SovereignArb.sol")?;

    let provider = Arc::new(Provider::<Ws>::connect(&ws_url).await?);
    let client   = Arc::new(SignerMiddleware::new(provider.clone(), wallet));

    let profit_wallet: Address = wallet_addr.parse()?;

    println!("[INIT] Deploying contract...");
    let contract_addr = compiler.deploy(&compiled, client.clone(), profit_wallet).await?;

    // ── 2. ML state ───────────────────────────────────────────────────────────
    let gp          = Arc::new(RwLock::new(GaussianProcess::new()));
    let ukf_map     = Arc::new(DashMap::<String, UnscentedKalmanFilter>::new());
    let replay      = Arc::new(RwLock::new(PrioritizedReplay::new(10_000)));
    let gas_history = Arc::new(RwLock::new(vec![20_000_000_000u64; 10]));
    let profit_total = Arc::new(RwLock::new(0u128));

    // ── 3. Token + DEX config ─────────────────────────────────────────────────
    let tokens: Vec<Address> = vec![
        WETH.parse()?,
        USDC.parse()?,
        USDT.parse()?,
        DAI.parse()?,
        WBTC.parse()?,
    ];
    // 'static str is fine; no heap allocation needed
    let token_names: Vec<&'static str> = vec!["WETH", "USDC", "USDT", "DAI", "WBTC"];

    // Routers used for quoting AND execution (V2-compatible)
    let routers: Vec<(&'static str, Address)> = vec![
        ("UniV2",  "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".parse()?),
        ("Sushi",  "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F".parse()?),
    ];

    // ── 4. Mempool stream ─────────────────────────────────────────────────────
    println!("[ENGINE] Streaming mempool — scanning every 30 tx...\n");
    let mut stream = provider.subscribe_pending_txs().await?;
    let mut tick: u64 = 0;

    while stream.next().await.is_some() {
        tick += 1;
        if tick % 30 != 0 {
            continue;
        }

        // Clone all Arcs — cheap, O(1)
        let provider_c   = provider.clone();
        let client_c     = client.clone();
        let gp_c         = gp.clone();
        let ukf_c        = ukf_map.clone();
        let replay_c     = replay.clone();
        let gas_c        = gas_history.clone();
        let tokens_c     = tokens.clone();
        let routers_c    = routers.clone();
        let names_c      = token_names.clone();
        let profit_c     = profit_total.clone();
        let contract     = contract_addr;
        let wallet_c     = profit_wallet;

        tokio::spawn(async move {
            if let Err(e) = scan_and_execute(
                provider_c, client_c, gp_c, ukf_c, replay_c,
                gas_c, tokens_c, routers_c, names_c,
                profit_c, contract, wallet_c,
            ).await {
                eprintln!("[SCAN ERR] {e}");
            }
        });
    }

    Ok(())
}

// ─── Core scan-and-execute loop ───────────────────────────────────────────────
async fn scan_and_execute(
    provider:    Arc<Provider<Ws>>,
    client:      Arc<SignerMiddleware<Arc<Provider<Ws>>, LocalWallet>>,
    gp:          Arc<RwLock<GaussianProcess>>,
    ukf_map:     Arc<DashMap<String, UnscentedKalmanFilter>>,
    replay:      Arc<RwLock<PrioritizedReplay>>,
    gas_history: Arc<RwLock<Vec<u64>>>,
    tokens:      Vec<Address>,
    routers:     Vec<(&'static str, Address)>,
    names:       Vec<&'static str>,
    profit_total: Arc<RwLock<u128>>,
    contract:    Address,
    wallet:      Address,
) -> eyre::Result<()> {

    // ── A. Gas price tracking ─────────────────────────────────────────────────
    let gas_price = provider
        .get_gas_price()
        .await
        .unwrap_or_else(|_| U256::from(20_000_000_000u64))
        .as_u64();

    {
        let mut g = gas_history.write().await;
        g.push(gas_price);
        if g.len() > 200 { g.remove(0); }
    }

    let optimal_gas = {
        let g = gas_history.read().await;
        ewma_gas(&g, 0.15, 1.12)
    };

    // ── B. Build price graph ──────────────────────────────────────────────────
    let one_eth = U256::from(1_000_000_000_000_000_000u128); // 1e18
    let mut edges: Vec<Edge> = Vec::with_capacity(tokens.len() * tokens.len() * routers.len());

    for (i, &token_in) in tokens.iter().enumerate() {
        for (j, &token_out) in tokens.iter().enumerate() {
            if i == j { continue; }

            for (dex_name, router_addr) in &routers {
                let uni = IUniV2::new(*router_addr, provider.clone());

                let amounts_result: Result<Vec<U256>, _> =
                    uni.get_amounts_out(one_eth, vec![token_in, token_out]).call().await;
                match amounts_result {
                    Ok(amounts) if amounts.len() >= 2 => {
                        let out: U256 = amounts[1];
                        if out.is_zero() { continue; }

                        // Rate = output_amount / input_amount (dimensionless)
                        // Both denominated in wei — cancel out. Approximation valid
                        // for stable/ETH pairs since input is 1 ETH = 1e18 wei.
                        let rate = out.as_u128() as f64 / 1e18;
                        if rate <= 0.0 { continue; }

                        // UKF price tracking
                        let key = format!("{}-{}-{dex_name}", names[i], names[j]);
                        ukf_map.entry(key)
                            .and_modify(|ukf| ukf.update(rate))
                            .or_insert_with(|| {
                                let mut ukf = UnscentedKalmanFilter::new(rate);
                                ukf.update(rate);
                                ukf
                            });

                        edges.push(Edge {
                            from:      token_in,
                            to:        token_out,
                            dex:       dex_name.to_string(),
                            log_rate:  -rate.ln(), // negative log so negative cycle = arb
                            liquidity: out,
                        });
                    }
                    _ => {} // Pair may not exist on this DEX — skip silently
                }
            }
        }
    }

    if edges.is_empty() {
        return Ok(());
    }

    // ── C. Cycle detection ────────────────────────────────────────────────────
    let cycles = bellman_ford(&tokens, &edges);
    if cycles.is_empty() {
        return Ok(());
    }

    // ── D. Process each cycle ─────────────────────────────────────────────────
    for cycle in &cycles {
        let path: Vec<Address> = cycle
            .iter()
            .filter_map(|&i| tokens.get(i).copied())
            .collect();

        if path.len() < 3 {
            continue; // need at least borrow → swap → repay
        }

        let hop_count = path.len() - 1;

        // D1. GP profit prediction
        let features = vec![
            gas_price as f64 / 1e9,
            hop_count as f64,
            edges.len() as f64,
        ];
        let (predicted_profit, uncertainty) = {
            gp.read().await.predict(&features)
        };

        // Skip if GP is confident it's unprofitable
        if predicted_profit < 0.0 && uncertainty < 0.4 {
            continue;
        }

        // D2. CMA-ES trade-size optimisation
        let gas_cost_eth = (optimal_gas as f64 * 500_000.0) / 1e18;
        let mut cma = CmaEs::new(0.5); // start search at 0.5 ETH
        let optimal_size_eth = cma.optimize(
            |size_eth| {
                // Simplified model: 0.8% gross edge, minus fees, minus gas
                let gross  = size_eth * 0.008;
                let fees   = size_eth * 0.003 * hop_count as f64; // ~0.3% per hop
                let profit = gross - fees - gas_cost_eth;
                profit
            },
            25,
        );
        let trade_eth = optimal_size_eth.max(0.05).min(3.0);

        // D3. Kelly fraction
        let kelly    = kelly_criterion(0.63, 0.009, 0.003, 0.25);
        let sized_wei = (trade_eth * 1e18 * kelly) as u128;
        let min_wei   = 50_000_000_000_000_000u128; // 0.05 ETH absolute floor
        let final_size = U256::from(sized_wei.max(min_wei));

        // D4. Shapley attribution — which DEX contributes most alpha?
        let dex_contributions: Vec<f64> = edges.iter()
            .map(|e| (-e.log_rate).exp() - 1.0)
            .collect();
        let attribution = shapley_attribution(&dex_contributions);
        let best_dex = attribution
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
            .and_then(|(i, _)| edges.get(i))
            .map(|e| e.dex.as_str())
            .unwrap_or("?");

        println!(
            "[ARB] {hop_count}-hop | size={trade_eth:.4}ETH | gas={gas_cost_eth:.6}ETH \
             | GP={predicted_profit:.6}±{uncertainty:.3} | DEX={best_dex}"
        );

        // D5. ABI-encode the executeArb calldata (manual encoding avoids abigen tuple limitations)
        let arb_routers: Vec<Address> = vec![routers[0].1; hop_count];
        let dex_types:   Vec<u8>      = vec![0u8; hop_count]; // 0 = UniV2-compatible
        let extra_data:  Vec<Bytes>   = vec![Bytes::default(); hop_count];
        let min_profit   = U256::from(3_000_000_000_000_000u64); // 0.003 ETH floor
        let borrow_token = path[0]; // borrow the starting token

        let calldata = encode_execute_arb(
            borrow_token,
            final_size,
            path.clone(),
            arb_routers,
            dex_types,
            extra_data,
            min_profit,
        );

        // D6. Execute flash loan via raw signed transaction
        let tx = TransactionRequest::new()
            .to(contract)
            .data(calldata)
            .gas_price(U256::from(optimal_gas))
            .gas(600_000u64);

        match client.send_transaction(tx, None).await {
            Ok(pending_tx) => {
                let tx_hash = pending_tx.tx_hash();
                println!("[FLASH] ✓ Sent: {tx_hash:?}");
                println!("[PROFIT] → Routing to: {wallet:?}");

                // Update GP with provisional profit estimate
                let actual_profit = predicted_profit.max(0.001);
                {
                    let mut gp_w = gp.write().await;
                    gp_w.update(features.clone(), actual_profit);
                }

                // Store in replay buffer
                {
                    let mut rep = replay.write().await;
                    rep.push(TradeExperience {
                        features: vec![gas_price as f64, trade_eth, actual_profit],
                        profit:   actual_profit,
                        priority: 0.0,
                    });
                }

                {
                    let mut total = profit_total.write().await;
                    *total += (actual_profit * 1e18) as u128;
                    println!(
                        "[STATS] Cumulative estimated profit: {:.6} ETH",
                        *total as f64 / 1e18
                    );
                }
            }
            Err(e) => {
                println!("[SKIP] Tx rejected: {e}");
                // Update GP with negative signal so it learns to skip bad setups
                let mut gp_w = gp.write().await;
                gp_w.update(features, -gas_cost_eth);
            }
        }
    }

    Ok(())
}
