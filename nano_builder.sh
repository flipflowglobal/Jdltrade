#!/data/data/com.termux/files/usr/bin/bash
# ================================================================
# JDL Trade — Nano File Builder for Termux
# Build crypto project files interactively using nano
# Usage: bash nano_builder.sh [project_name] [template]
# ================================================================

set -e

# ── Colors ───────────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
CYN='\033[0;36m'
BLD='\033[1m'
DIM='\033[2m'
RST='\033[0m'

# ── Helpers ──────────────────────────────────────────────────────
banner() {
    clear
    echo -e "${CYN}${BLD}"
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║   JDL Trade  ·  Nano File Builder    ║"
    echo "  ║   Crypto Systems  ·  Termux Edition  ║"
    echo "  ╚══════════════════════════════════════╝"
    echo -e "${RST}"
}

info()    { echo -e "  ${GRN}[+]${RST} $*"; }
warn()    { echo -e "  ${YLW}[!]${RST} $*"; }
err()     { echo -e "  ${RED}[✗]${RST} $*"; }
step()    { echo -e "\n  ${BLU}${BLD}──── $* ────${RST}"; }
ask()     { echo -en "  ${CYN}▶${RST} $* "; }

# Open file in nano, creating parent dirs and skeleton content first
nano_edit() {
    local file="$1"
    local skeleton="$2"
    mkdir -p "$(dirname "$file")"
    if [ ! -f "$file" ] && [ -n "$skeleton" ]; then
        echo "$skeleton" > "$file"
    fi
    echo -e "\n  ${YLW}Opening nano:${RST} $file"
    echo -e "  ${DIM}Ctrl+O Save  ·  Ctrl+X Exit  ·  Ctrl+K Cut line  ·  Ctrl+U Paste${RST}\n"
    sleep 0.5
    nano "$file"
    local lines
    lines=$(wc -l < "$file" 2>/dev/null || echo 0)
    info "Saved: $file  (${lines} lines)"
}

# ── Template skeletons ────────────────────────────────────────────

skel_env() {
cat << 'EOF'
# ── Exchange API Keys ─────────────────────────────────────────────
BINANCE_API_KEY=
BINANCE_SECRET_KEY=

COINBASE_API_KEY=
COINBASE_SECRET_KEY=

KRAKEN_API_KEY=
KRAKEN_SECRET_KEY=

# ── Blockchain RPC ────────────────────────────────────────────────
ETH_RPC_URL=https://mainnet.infura.io/v3/YOUR_KEY
BSC_RPC_URL=https://bsc-dataseed.binance.org/
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
ARBITRUM_RPC_URL=https://arb1.arbitrum.io/rpc
BASE_RPC_URL=https://mainnet.base.org

# ── Wallets (NEVER commit real private keys) ──────────────────────
ETH_PRIVATE_KEY=
ETH_ADDRESS=

SOL_PRIVATE_KEY_BASE58=
SOL_ADDRESS=

# ── Notifications ─────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── App Settings ──────────────────────────────────────────────────
LOG_LEVEL=INFO
DRY_RUN=true
EOF
}

skel_price_bot() {
cat << 'EOF'
#!/usr/bin/env python3
"""
Crypto Price Monitor — JDL Trade
Tracks prices and fires alerts on threshold breaches.
"""

import asyncio
import os
import time
from datetime import datetime

import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()


class PriceMonitor:
    def __init__(self, exchange_id: str = "binance"):
        self.exchange = getattr(ccxt, exchange_id)({
            "apiKey": os.getenv(f"{exchange_id.upper()}_API_KEY", ""),
            "secret": os.getenv(f"{exchange_id.upper()}_SECRET_KEY", ""),
            "enableRateLimit": True,
        })
        self.alerts: dict[str, dict] = {}  # symbol -> {above, below, last_alert}

    def add_alert(self, symbol: str, above: float = None, below: float = None):
        self.alerts[symbol] = {"above": above, "below": below, "last_alert": 0}

    async def check_prices(self):
        symbols = list(self.alerts.keys())
        if not symbols:
            return

        try:
            tickers = await self.exchange.fetch_tickers(symbols)
            now = time.time()

            for symbol, ticker in tickers.items():
                price = ticker["last"]
                cfg = self.alerts.get(symbol, {})
                last = cfg.get("last_alert", 0)

                # Cooldown: only alert once per 5 minutes
                if now - last < 300:
                    continue

                msg = None
                if cfg.get("above") and price > cfg["above"]:
                    msg = f"🚀 {symbol} ABOVE ${cfg['above']:,.2f}  →  ${price:,.2f}"
                elif cfg.get("below") and price < cfg["below"]:
                    msg = f"📉 {symbol} BELOW ${cfg['below']:,.2f}  →  ${price:,.2f}"

                if msg:
                    ts = datetime.utcnow().strftime("%H:%M:%S")
                    print(f"[{ts}] ALERT: {msg}")
                    cfg["last_alert"] = now

        except Exception as e:
            print(f"[ERROR] Price check failed: {e}")

    async def run(self, interval: float = 10.0):
        print(f"Price monitor started. Checking every {interval}s.")
        print(f"Watching: {list(self.alerts.keys())}")
        try:
            while True:
                await self.check_prices()
                await asyncio.sleep(interval)
        finally:
            await self.exchange.close()


async def main():
    monitor = PriceMonitor("binance")

    # ── Configure your alerts here ────────────────────────────────
    monitor.add_alert("BTC/USDT", above=100000, below=80000)
    monitor.add_alert("ETH/USDT", above=5000,   below=2500)
    monitor.add_alert("SOL/USDT", above=300,    below=100)

    await monitor.run(interval=15)


if __name__ == "__main__":
    asyncio.run(main())
EOF
}

skel_arb_bot() {
cat << 'EOF'
#!/usr/bin/env python3
"""
CEX Arbitrage Scanner — JDL Trade
Detects price discrepancies between exchanges for the same pair.
"""

import asyncio
import os
import time
from itertools import combinations

import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────
EXCHANGES     = ["binance", "kraken", "coinbasepro", "bybit"]
SYMBOLS       = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
MIN_SPREAD_PCT = 0.3   # minimum % spread to report
TAKER_FEE      = 0.001 # 0.1% per leg (adjust per exchange)
POLL_INTERVAL  = 5.0   # seconds


class ArbScanner:
    def __init__(self):
        self.clients: dict[str, ccxt.Exchange] = {}

    async def init_exchanges(self):
        for ex_id in EXCHANGES:
            try:
                cls = getattr(ccxt, ex_id, None)
                if cls is None:
                    continue
                self.clients[ex_id] = cls({"enableRateLimit": True})
                await self.clients[ex_id].load_markets()
                print(f"  Connected: {ex_id}")
            except Exception as e:
                print(f"  Skipped {ex_id}: {e}")

    async def fetch_prices(self, symbol: str) -> dict[str, float]:
        tasks = {
            ex_id: asyncio.create_task(client.fetch_ticker(symbol))
            for ex_id, client in self.clients.items()
            if symbol in client.markets
        }
        prices = {}
        for ex_id, task in tasks.items():
            try:
                ticker = await task
                if ticker.get("ask") and ticker.get("bid"):
                    prices[ex_id] = {"ask": ticker["ask"], "bid": ticker["bid"]}
            except Exception:
                pass
        return prices

    def find_arb(self, symbol: str, prices: dict) -> list[dict]:
        opportunities = []
        for (buy_ex, sell_ex) in combinations(prices.keys(), 2):
            buy_price  = prices[buy_ex]["ask"]
            sell_price = prices[sell_ex]["bid"]
            net_spread = (sell_price - buy_price) / buy_price * 100
            net_spread -= TAKER_FEE * 200  # subtract both legs

            if net_spread >= MIN_SPREAD_PCT:
                opportunities.append({
                    "symbol": symbol,
                    "buy_on": buy_ex,   "buy_price": buy_price,
                    "sell_on": sell_ex, "sell_price": sell_price,
                    "spread_pct": round(net_spread, 4),
                })

            # Check reverse direction
            buy_price  = prices[sell_ex]["ask"]
            sell_price = prices[buy_ex]["bid"]
            net_spread = (sell_price - buy_price) / buy_price * 100
            net_spread -= TAKER_FEE * 200

            if net_spread >= MIN_SPREAD_PCT:
                opportunities.append({
                    "symbol": symbol,
                    "buy_on": sell_ex,  "buy_price": buy_price,
                    "sell_on": buy_ex,  "sell_price": sell_price,
                    "spread_pct": round(net_spread, 4),
                })

        return sorted(opportunities, key=lambda x: x["spread_pct"], reverse=True)

    async def scan(self):
        print(f"\n[{time.strftime('%H:%M:%S')}] Scanning {len(SYMBOLS)} pairs on {len(self.clients)} exchanges...")
        found_any = False
        for symbol in SYMBOLS:
            prices = await self.fetch_prices(symbol)
            if len(prices) < 2:
                continue
            opps = self.find_arb(symbol, prices)
            for opp in opps:
                found_any = True
                print(
                    f"  💰 {opp['symbol']:12s}  "
                    f"BUY  {opp['buy_on']:14s} @ ${opp['buy_price']:>12,.4f}  "
                    f"SELL {opp['sell_on']:14s} @ ${opp['sell_price']:>12,.4f}  "
                    f"NET: {opp['spread_pct']:+.3f}%"
                )
        if not found_any:
            print("  No opportunities above threshold.")

    async def run(self):
        print("Initializing exchange connections...")
        await self.init_exchanges()
        try:
            while True:
                await self.scan()
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            for client in self.clients.values():
                await client.close()


if __name__ == "__main__":
    asyncio.run(ArbScanner().run())
EOF
}

skel_dex_sniper() {
cat << 'EOF'
#!/usr/bin/env python3
"""
DEX Token Sniper — JDL Trade
Monitors Uniswap V3 / PancakeSwap for new pool creation events
and executes buys on qualifying tokens.

WARNING: DRY_RUN=true by default. Review carefully before going live.
"""

import asyncio
import os
import json
from web3 import AsyncWeb3, WebSocketProvider
from eth_account import Account
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────
WS_URL          = os.getenv("ETH_WS_URL", "wss://mainnet.infura.io/ws/v3/YOUR_KEY")
PRIVATE_KEY     = os.getenv("ETH_PRIVATE_KEY", "")
WALLET          = os.getenv("ETH_ADDRESS", "")
DRY_RUN         = os.getenv("DRY_RUN", "true").lower() == "true"

BUY_AMOUNT_ETH  = 0.01       # ETH to spend per snipe
MAX_GAS_GWEI    = 50         # max gas price
SLIPPAGE_PCT    = 10         # % slippage tolerance
MIN_LIQUIDITY   = 1.0        # minimum ETH in pool to snipe

# Uniswap V3 Factory
UNI_V3_FACTORY  = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
POOL_CREATED_SIG = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# WETH address (ETH wrapper)
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


async def on_new_pool(w3: AsyncWeb3, event: dict):
    """Called when a new Uniswap V3 pool is created."""
    topics = event.get("topics", [])
    if len(topics) < 3:
        return

    token0 = "0x" + topics[1][-40:]
    token1 = "0x" + topics[2][-40:]

    # Find which token is not WETH
    new_token = None
    if token0.lower() == WETH.lower():
        new_token = token1
    elif token1.lower() == WETH.lower():
        new_token = token0
    else:
        return  # Neither is WETH — skip non-ETH pairs

    pool_addr = event.get("address", "?")
    block     = event.get("blockNumber", 0)

    print(f"\n[POOL DETECTED] Block {block}")
    print(f"  Pool:      {pool_addr}")
    print(f"  New token: {new_token}")
    print(f"  DRY_RUN:   {DRY_RUN}")

    if DRY_RUN:
        print(f"  [DRY] Would buy {BUY_AMOUNT_ETH} ETH of {new_token}")
        return

    # ── Live execution (implement swap logic here) ──────────────
    # await execute_swap(w3, new_token, BUY_AMOUNT_ETH)


async def main():
    print(f"DEX Sniper starting (DRY_RUN={DRY_RUN})")
    print(f"Wallet: {WALLET or '(not set)'}")
    print(f"Listening for Uniswap V3 pool creation events...\n")

    if not PRIVATE_KEY and not DRY_RUN:
        raise ValueError("ETH_PRIVATE_KEY not set and DRY_RUN=false — refusing to start")

    w3 = AsyncWeb3(WebSocketProvider(WS_URL))

    if not await w3.is_connected():
        raise ConnectionError(f"Cannot connect to WebSocket RPC: {WS_URL}")

    print(f"Connected to chain ID: {await w3.eth.chain_id}")

    # Subscribe to factory logs
    subscription = await w3.eth.subscribe(
        "logs",
        {
            "address": UNI_V3_FACTORY,
            "topics": [POOL_CREATED_SIG],
        },
    )

    async for event in subscription:
        try:
            await on_new_pool(w3, event)
        except Exception as e:
            print(f"[ERROR] Event handling failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
EOF
}

skel_grid_bot() {
cat << 'EOF'
#!/usr/bin/env python3
"""
Grid Trading Bot — JDL Trade
Places buy/sell limit orders at fixed price intervals within a range.
Profits from volatility — the more the price oscillates, the more it earns.
"""

import asyncio
import os
import math
from dataclasses import dataclass, field
from typing import Optional

import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()

# ── Grid Configuration ────────────────────────────────────────────
SYMBOL        = "BTC/USDT"
EXCHANGE_ID   = "binance"
LOWER_PRICE   = 85_000.0   # bottom of grid range
UPPER_PRICE   = 105_000.0  # top of grid range
GRID_LEVELS   = 20         # number of grid lines
INVESTMENT    = 1000.0     # total USDT to deploy
DRY_RUN       = os.getenv("DRY_RUN", "true").lower() == "true"


@dataclass
class GridLevel:
    price: float
    side: str           # 'buy' or 'sell'
    order_id: Optional[str] = None
    filled: bool = False


class GridBot:
    def __init__(self):
        self.exchange = getattr(ccxt, EXCHANGE_ID)({
            "apiKey": os.getenv(f"{EXCHANGE_ID.upper()}_API_KEY", ""),
            "secret": os.getenv(f"{EXCHANGE_ID.upper()}_SECRET_KEY", ""),
            "enableRateLimit": True,
        })
        self.grid: list[GridLevel] = []
        self.profit_usdt: float = 0.0

    def build_grid(self) -> list[GridLevel]:
        """Build evenly spaced price levels across the range."""
        step = (UPPER_PRICE - LOWER_PRICE) / (GRID_LEVELS - 1)
        levels = []
        per_level_usdt = INVESTMENT / GRID_LEVELS

        for i in range(GRID_LEVELS):
            price = LOWER_PRICE + i * step
            # Levels below mid = buy orders, above mid = sell orders
            mid = (UPPER_PRICE + LOWER_PRICE) / 2
            side = "buy" if price < mid else "sell"
            levels.append(GridLevel(price=round(price, 2), side=side))

        print(f"Grid built: {GRID_LEVELS} levels  "
              f"${LOWER_PRICE:,.0f} → ${UPPER_PRICE:,.0f}  "
              f"step=${step:,.0f}  per_level=${INVESTMENT/GRID_LEVELS:,.2f}")
        return levels

    async def place_order(self, level: GridLevel, quantity: float) -> Optional[str]:
        if DRY_RUN:
            fake_id = f"dry_{level.side}_{level.price}"
            print(f"  [DRY] {level.side.upper():4s} {quantity:.6f} @ ${level.price:,.2f}")
            return fake_id

        try:
            order = await self.exchange.create_limit_order(
                SYMBOL, level.side, quantity, level.price
            )
            return order["id"]
        except Exception as e:
            print(f"  [ERROR] Order failed {level.side} @ {level.price}: {e}")
            return None

    async def run(self):
        print(f"Grid Bot starting ({SYMBOL}, DRY_RUN={DRY_RUN})")
        await self.exchange.load_markets()

        ticker = await self.exchange.fetch_ticker(SYMBOL)
        current_price = ticker["last"]
        print(f"Current price: ${current_price:,.2f}")

        self.grid = self.build_grid()

        # Place all initial orders
        print(f"\nPlacing {GRID_LEVELS} grid orders...")
        market = self.exchange.markets[SYMBOL]
        min_qty = market["limits"]["amount"]["min"] or 0.00001
        per_level = INVESTMENT / GRID_LEVELS

        for level in self.grid:
            qty = max(per_level / level.price, min_qty)
            qty = round(qty, 6)
            order_id = await self.place_order(level, qty)
            if order_id:
                level.order_id = order_id

        print(f"\nGrid active. Monitoring fills every 30s...")

        try:
            while True:
                await asyncio.sleep(30)
                await self.check_fills()
        finally:
            await self.exchange.close()

    async def check_fills(self):
        """Check for filled orders and re-place on the opposite side."""
        for level in self.grid:
            if not level.order_id or level.filled:
                continue
            try:
                order = await self.exchange.fetch_order(level.order_id, SYMBOL)
                if order["status"] == "closed":
                    level.filled = True
                    fill_price = order["average"] or level.price
                    print(f"  FILLED: {level.side.upper():4s} @ ${fill_price:,.2f}")
                    # TODO: place counter-order on opposite side
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(GridBot().run())
EOF
}

skel_main() {
cat << 'EOF'
#!/usr/bin/env python3
"""
Main entry point — JDL Trade Project
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jdltrade")


async def main():
    log.info("JDL Trade starting...")
    # TODO: initialize and run your components here


if __name__ == "__main__":
    asyncio.run(main())
EOF
}

skel_reqs() {
cat << 'EOF'
# Core
ccxt>=4.3.0
web3>=6.15.0
python-dotenv>=1.0.0
httpx>=0.27.0
websockets>=12.0
aiofiles>=23.2.1

# Data & Analysis
pandas>=2.2.0
numpy>=1.26.0
pandas-ta>=0.3.14b

# Crypto-specific
eth-account>=0.11.0
solana>=0.32.0
pycoingecko>=3.1.0

# Utilities
rich>=13.7.0
pydantic>=2.6.0
EOF
}

# ── Project Templates ─────────────────────────────────────────────

build_price_monitor() {
    local name="$1"
    step "Building: Price Monitor Bot"
    mkdir -p "$name"
    info "Creating project structure..."

    nano_edit "$name/.env"                 "$(skel_env)"
    nano_edit "$name/requirements.txt"     "$(skel_reqs)"
    nano_edit "$name/price_monitor.py"     "$(skel_price_bot)"
    nano_edit "$name/main.py"              "$(skel_main)"

    info "Installing dependencies..."
    cd "$name" && pip install -r requirements.txt -q && cd ..
    info "Done! Run: python $name/price_monitor.py"
}

build_arb_scanner() {
    local name="$1"
    step "Building: Arbitrage Scanner"
    mkdir -p "$name"

    nano_edit "$name/.env"             "$(skel_env)"
    nano_edit "$name/requirements.txt" "$(skel_reqs)"
    nano_edit "$name/arb_scanner.py"   "$(skel_arb_bot)"
    nano_edit "$name/main.py"          "$(skel_main)"

    info "Installing dependencies..."
    cd "$name" && pip install -r requirements.txt -q && cd ..
    info "Done! Run: python $name/arb_scanner.py"
}

build_dex_sniper() {
    local name="$1"
    step "Building: DEX Sniper"
    mkdir -p "$name"

    nano_edit "$name/.env"             "$(skel_env)"
    nano_edit "$name/requirements.txt" "$(skel_reqs)"
    nano_edit "$name/sniper.py"        "$(skel_dex_sniper)"
    nano_edit "$name/main.py"          "$(skel_main)"

    info "Installing dependencies..."
    cd "$name" && pip install -r requirements.txt -q && cd ..
    info "Done! Run: python $name/sniper.py"
}

build_grid_bot() {
    local name="$1"
    step "Building: Grid Trading Bot"
    mkdir -p "$name"

    nano_edit "$name/.env"             "$(skel_env)"
    nano_edit "$name/requirements.txt" "$(skel_reqs)"
    nano_edit "$name/grid_bot.py"      "$(skel_grid_bot)"
    nano_edit "$name/main.py"          "$(skel_main)"

    info "Installing dependencies..."
    cd "$name" && pip install -r requirements.txt -q && cd ..
    info "Done! Run: python $name/grid_bot.py"
}

build_custom() {
    local name="$1"
    step "Building: Custom Project"
    mkdir -p "$name"

    ask "Files to create (space-separated, e.g.: main.py config.py utils.py):"
    read -r filelist

    for f in $filelist; do
        nano_edit "$name/$f" ""
    done

    ask "Install requirements.txt? [y/N]:"
    read -r install_deps
    if [[ "$install_deps" =~ ^[Yy]$ ]]; then
        nano_edit "$name/requirements.txt" "$(skel_reqs)"
        cd "$name" && pip install -r requirements.txt -q && cd ..
    fi

    info "Done! Project: $name/"
}

# ── Single file quick-edit mode ──────────────────────────────────

quick_edit() {
    local file="$1"
    info "Quick edit: $file"
    nano_edit "$file" ""
}

# ── Main menu ────────────────────────────────────────────────────

main() {
    # Direct file argument: nano_builder.sh myfile.py
    if [ -n "$1" ] && [[ "$1" != --* ]] && echo "$1" | grep -q '\.'; then
        quick_edit "$1"
        exit 0
    fi

    # Project name from arg, or prompt
    local proj_name="${1:-}"
    local template="${2:-}"

    banner

    if [ -z "$proj_name" ]; then
        ask "Project name [crypto_bot]:"
        read -r proj_name
        proj_name="${proj_name:-crypto_bot}"
    fi

    # Sanitize
    proj_name=$(echo "$proj_name" | tr ' ' '_' | tr -cd '[:alnum:]_-')

    if [ -d "$proj_name" ]; then
        warn "Directory '$proj_name' already exists."
        ask "Continue and add files? [y/N]:"
        read -r cont
        [[ "$cont" =~ ^[Yy]$ ]] || exit 0
    fi

    if [ -z "$template" ]; then
        echo ""
        echo -e "  ${BLD}Select template:${RST}"
        echo -e "  ${CYN}1)${RST} Price Monitor Bot   — real-time alerts on CEX price thresholds"
        echo -e "  ${CYN}2)${RST} Arbitrage Scanner   — detect spread between exchanges"
        echo -e "  ${CYN}3)${RST} DEX Sniper          — detect & buy new Uniswap pool listings"
        echo -e "  ${CYN}4)${RST} Grid Trading Bot    — place orders at fixed price intervals"
        echo -e "  ${CYN}5)${RST} Custom / Blank      — create your own files from scratch"
        echo ""
        ask "Choice [1-5]:"
        read -r template
    fi

    case "$template" in
        1|price)    build_price_monitor "$proj_name" ;;
        2|arb)      build_arb_scanner   "$proj_name" ;;
        3|sniper)   build_dex_sniper    "$proj_name" ;;
        4|grid)     build_grid_bot      "$proj_name" ;;
        5|custom|*) build_custom        "$proj_name" ;;
    esac

    echo ""
    echo -e "  ${GRN}${BLD}╔══════════════════════════════════╗${RST}"
    echo -e "  ${GRN}${BLD}║  Project '$proj_name' is ready!  ║${RST}"
    echo -e "  ${GRN}${BLD}╚══════════════════════════════════╝${RST}"
    echo ""
    echo -e "  ${DIM}Files created in: $(pwd)/$proj_name/${RST}"
    echo -e "  ${DIM}To re-edit a file:  bash nano_builder.sh $proj_name/main.py${RST}"
    echo ""
}

main "$@"
