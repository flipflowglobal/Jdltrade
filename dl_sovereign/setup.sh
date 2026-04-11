#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  D.L Sovereign — one-shot setup script
#  Run once: bash setup.sh
# ═══════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  D.L Sovereign Flash Loan Setup              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. Rust toolchain ───────────────────────────────────────
if ! command -v cargo &>/dev/null; then
    echo "[SETUP] Installing Rust..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    # shellcheck disable=SC1090
    source "$HOME/.cargo/env"
fi
echo "[SETUP] Rust: $(rustc --version)"

# ── 2. solc 0.8.24 via solc-select ─────────────────────────
if ! command -v solc-select &>/dev/null; then
    echo "[SETUP] Installing solc-select..."
    pip install --break-system-packages solc-select 2>/dev/null \
        || pip install solc-select
fi

if ! solc --version 2>/dev/null | grep -q "0.8.24"; then
    echo "[SETUP] Installing solc 0.8.24..."
    solc-select install 0.8.24
    solc-select use 0.8.24
fi
echo "[SETUP] solc: $(solc --version | head -1)"

# ── 3. Node / npm for Solidity dependencies ─────────────────
if ! command -v npm &>/dev/null; then
    echo "[SETUP] npm not found — install Node.js >= 18 and re-run"
    exit 1
fi

if [ ! -d node_modules/@aave ]; then
    echo "[SETUP] Installing Solidity packages..."
    npm init -y --quiet
    npm install --quiet \
        @aave/core-v3 \
        @openzeppelin/contracts
fi
echo "[SETUP] NPM packages: OK"

# ── 4. .env ──────────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  ┌─────────────────────────────────────────────────┐"
    echo "  │  .env created — fill in your keys before running │"
    echo "  │  nano .env                                        │"
    echo "  └─────────────────────────────────────────────────┘"
fi

# ── 5. Build release ─────────────────────────────────────────
echo ""
echo "[SETUP] Building release binary..."
cargo build --release 2>&1 | tail -20
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Build complete!                             ║"
echo "║  1. Edit .env with your keys                 ║"
echo "║  2. Run: ./target/release/dl_sovereign       ║"
echo "╚══════════════════════════════════════════════╝"
