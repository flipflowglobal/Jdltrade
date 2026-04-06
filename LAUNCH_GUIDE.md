# NEXUS-ARB v2.0 — Launch Guide

## Quick Start (30 minutes to live trading)

### Prerequisites
- Python 3.11+
- Rust toolchain (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y`)
- Node.js 18+ (for Hardhat/contract compilation)
- Arbitrum wallet with ETH (minimum 0.05 ETH for gas)
- Alchemy/Infura/Blast RPC keys

---

### Step 1: Environment Setup

```bash
cd /home/user/Jdltrade
cp .env.example .env
# Edit .env with your values:
# - PRIVATE_KEY=0x...your wallet key...
# - ARB_HTTP_1=https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
# - ARB_WS_PRIMARY=wss://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
```

### Step 2: Install Python Dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Build Rust Extension (for maximum speed)

```bash
pip install maturin
cd nexus_core && maturin develop --release && cd ..
```

### Step 4: Compile & Deploy Smart Contract

```bash
# Install Hardhat
cd hardhat && npm install && cd ..

# Compile contract
cd hardhat && npx hardhat compile && cd ..

# Deploy to Arbitrum mainnet
python scripts/deploy.py
# This will:
# - Verify your wallet balance
# - Deploy NexusFlashReceiver
# - Save the address to .env automatically
```

### Step 5: Validate Everything

```bash
python scripts/validate_mainnet.py
# All 6 checks must pass before trading
```

### Step 6: Run Tests

```bash
# Python unit tests
pytest tests/ -v

# Mainnet fork integration tests (requires ARB_HTTP_1)
cd hardhat && npx hardhat test && cd ..
```

### Step 7: Start Trading

```bash
# Start with dry run first to verify logic
DRY_RUN=true python -m nexus_arb.orchestrator

# When satisfied, go live:
python -m nexus_arb.orchestrator
```

---

## Architecture Overview

```
nexus_arb/
├── orchestrator.py         Main event loop (block-driven)
├── web3_manager.py         Multi-RPC connection pool with latency EMA
├── market_data.py          On-chain price feed via Multicall3
├── liquidity_monitor.py    Pool reserve tracking
├── flash_loan_executor.py  Transaction builder and submitter
├── execution_router.py     Route encoding (Uniswap/Curve/Balancer)
├── tx_monitor.py           Receipt waiting and profit parsing
├── risk_manager.py         Pre-trade checks and circuit breakers
├── vault.py                Private key management (env/KMS/Vault)
└── algorithms/
    ├── bellman_ford.py     Arbitrage path detection (O(V·E))
    ├── cma_es.py           Optimal trade size (CMA-ES)
    ├── ukf.py              Price state estimation (UKF)
    ├── thompson_sampling.py DEX selection (Bayesian bandit)
    └── ppo.py              Execution timing (PPO RL)

contracts/
├── NexusFlashReceiver.sol  Flash loan callback contract
└── interfaces/             DEX/Aave interface definitions

nexus_core/                 Rust extension (15-25x faster graphs)
```

## Monitoring

- Prometheus: http://localhost:9090
- Grafana:    http://localhost:3000 (password: nexusarb2024)

Key metrics:
- `nexus_profit_eth_total`       — Cumulative ETH profit
- `nexus_opportunities_detected` — Scan rate
- `nexus_scan_latency_ms`        — Time from block to opportunity
- `nexus_circuit_breaker_active` — 1 if trading halted

## Safety Controls

| Control | Default | Description |
|---------|---------|-------------|
| Min profit | $5 USD | Skip sub-$5 opportunities |
| Max gas | 2 gwei | Skip if gas too expensive |
| Max flash loan | 500 ETH | Cap single trade size |
| Consecutive failures | 5 | Trigger circuit breaker |
| Daily loss limit | 1 ETH | Auto-stop on drawdown |
| Gas profit multiplier | 1.5x | Net profit must be 1.5x gas |

## Algorithm Stack

| Algorithm | Purpose | Source |
|-----------|---------|--------|
| Bellman-Ford | Find negative cycles = arbitrage | `algorithms/bellman_ford.py` |
| CMA-ES | Optimal trade size | `algorithms/cma_es.py` |
| UKF | Price trend filtering | `algorithms/ukf.py` |
| Thompson Sampling | DEX selection bandit | `algorithms/thompson_sampling.py` |
| PPO | Execution timing RL | `algorithms/ppo.py` |

## Troubleshooting

**"No healthy RPC endpoints"**: Check ARB_HTTP_* in .env

**"NEXUS_RECEIVER_ADDRESS not set"**: Run `python scripts/deploy.py`

**"InsufficientProfit" reverts**: Normal — means the opportunity moved.
The system will learn via Thompson Sampling.

**Circuit breaker triggered**: Wait 60s or check logs for root cause.
