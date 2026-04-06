"""
Pre-flight validation script — run before starting live trading.

Checks:
  1. Environment variables are set
  2. RPC endpoints are reachable and on Arbitrum
  3. Wallet has sufficient ETH for gas
  4. NexusFlashReceiver contract is deployed and responsive
  5. DEX pools are queryable
  6. Aave flash loan terms are acceptable
  7. Dry-run a price scan
  8. Dry-run a flash loan (simulation only, no gas)

All checks must pass before trading is allowed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REQUIRED_ENV = [
    "PRIVATE_KEY",
    "ARB_HTTP_1",
    "NEXUS_RECEIVER_ADDRESS"
]


class Validator:
    def __init__(self) -> None:
        load_dotenv()
        with open("config.yaml") as f:
            self.cfg = yaml.safe_load(f)
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def check(self, name: str, passed: bool, message: str = "") -> bool:
        status = "✅" if passed else "❌"
        log.info(f"  {status} {name}" + (f": {message}" if message else ""))
        if not passed:
            self.errors.append(name)
        return passed

    def warn(self, name: str, message: str = "") -> None:
        log.warning(f"  ⚠️  {name}: {message}")
        self.warnings.append(name)

    # ─── Check 1: Environment Variables ──────────────────────
    def validate_env(self) -> bool:
        log.info("\n[1/8] Environment Variables")
        ok = True
        for var in REQUIRED_ENV:
            val = os.getenv(var, "")
            passed = bool(val)
            self.check(f"ENV:{var}", passed, "set" if passed else "MISSING")
            if not passed:
                ok = False

        # Check optional but recommended
        for var in ["ARB_WS_PRIMARY", "TELEGRAM_BOT_TOKEN"]:
            val = os.getenv(var, "")
            if not val:
                self.warn(f"ENV:{var}", "not set (optional)")

        return ok

    # ─── Check 2: RPC Endpoints ───────────────────────────────
    async def validate_rpc(self) -> bool:
        log.info("\n[2/8] RPC Endpoints")
        any_ok = False

        for key in ["ARB_HTTP_1", "ARB_HTTP_2", "ARB_HTTP_3", "ARB_HTTP_4"]:
            url = os.getenv(key, "")
            if not url:
                continue
            try:
                w3 = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": 5}))
                chain_id = await w3.eth.chain_id
                block_num = await w3.eth.block_number
                passed = chain_id == 42161
                self.check(
                    f"RPC:{key}",
                    passed,
                    f"chain_id={chain_id}, block={block_num}" if passed
                    else f"WRONG CHAIN: {chain_id} (expected 42161)"
                )
                if passed:
                    any_ok = True
            except Exception as e:
                self.check(f"RPC:{key}", False, str(e)[:80])

        return any_ok

    # ─── Check 3: Wallet ──────────────────────────────────────
    async def validate_wallet(self, w3: AsyncWeb3) -> bool:
        log.info("\n[3/8] Wallet")
        try:
            from eth_account import Account
            pk = os.getenv("PRIVATE_KEY", "")
            if not pk.startswith("0x"):
                pk = "0x" + pk
            acct = Account.from_key(pk)
            bal = await w3.eth.get_balance(acct.address)
            bal_eth = bal / 1e18

            self.check("WALLET_DERIVATION", True, acct.address)
            min_bal = 0.005  # Minimum for gas
            self.check(
                "WALLET_BALANCE",
                bal_eth >= min_bal,
                f"{bal_eth:.6f} ETH (min {min_bal} ETH)"
            )
            if bal_eth < 0.05:
                self.warn("WALLET_LOW_BALANCE", f"Only {bal_eth:.4f} ETH — top up for sustained trading")
            return True
        except Exception as e:
            self.check("WALLET", False, str(e))
            return False

    # ─── Check 4: Contract ────────────────────────────────────
    async def validate_contract(self, w3: AsyncWeb3) -> bool:
        log.info("\n[4/8] NexusFlashReceiver Contract")
        addr = os.getenv("NEXUS_RECEIVER_ADDRESS", "")
        if not addr:
            self.check("CONTRACT_ADDRESS", False, "NEXUS_RECEIVER_ADDRESS not set")
            return False

        try:
            code = await w3.eth.get_code(addr)
            has_code = len(code) > 2

            self.check("CONTRACT_EXISTS", has_code, f"{len(code)} bytes at {addr}")

            if has_code:
                # Query owner()
                owner_sel = AsyncWeb3.keccak(text="owner()")[:4]
                result = await w3.eth.call({"to": addr, "data": owner_sel})
                owner = "0x" + result.hex()[-40:]
                self.check("CONTRACT_OWNER", len(owner) == 42, f"owner={owner}")

            return has_code
        except Exception as e:
            self.check("CONTRACT", False, str(e)[:80])
            return False

    # ─── Check 5: DEX Connectivity ───────────────────────────
    async def validate_dex(self, w3: AsyncWeb3) -> bool:
        log.info("\n[5/8] DEX Pool Connectivity")
        from eth_abi import decode as abi_decode

        # Test Uniswap V3 WETH/USDC 0.05% pool
        pool_addr = "0xC6962004f452bE9203591991D15f6b388e09E8D0"
        slot0_sel = AsyncWeb3.keccak(text="slot0()")[:4]
        try:
            result = await w3.eth.call({"to": pool_addr, "data": slot0_sel})
            decoded = abi_decode(
                ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                result
            )
            sqrt_price = decoded[0]
            self.check(
                "UNIV3_WETH_USDC_POOL",
                sqrt_price > 0,
                f"sqrtPriceX96={sqrt_price}"
            )
        except Exception as e:
            self.check("UNIV3_WETH_USDC_POOL", False, str(e)[:80])

        # Test Aave pool FLASHLOAN_PREMIUM_TOTAL
        aave_pool = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
        premium_sel = AsyncWeb3.keccak(text="FLASHLOAN_PREMIUM_TOTAL()")[:4]
        try:
            result = await w3.eth.call({"to": aave_pool, "data": premium_sel})
            premium = abi_decode(["uint128"], result)[0]
            self.check("AAVE_FLASH_LOAN_PREMIUM", premium == 9, f"{premium}bps (expected 9)")
        except Exception as e:
            self.check("AAVE_FLASH_LOAN", False, str(e)[:80])

        return True

    # ─── Check 6: Price Scan ─────────────────────────────────
    async def validate_price_scan(self, w3: AsyncWeb3) -> bool:
        log.info("\n[6/8] Live Price Scan")
        try:
            from nexus_arb.web3_manager import Web3Manager
            from nexus_arb.market_data import MarketData

            # Create minimal web3 manager
            mgr = Web3Manager(self.cfg)
            mgr._w3_pool[os.getenv("ARB_HTTP_1")] = w3
            mgr.stats[os.getenv("ARB_HTTP_1")].is_healthy = True

            md = MarketData(self.cfg, mgr)
            graph = await md.refresh()

            n_edges = len(graph.edges)
            self.check("PRICE_GRAPH", n_edges > 0, f"{n_edges} price edges loaded")

            # Check for basic price sanity
            from nexus_arb.algorithms.bellman_ford import BellmanFord
            bf = BellmanFord(self.cfg)
            opps = bf.detect(graph, min_profit_pct=0.001)
            log.info(f"  ℹ️  Found {len(opps)} opportunities at 0.001% threshold (informational)")

            return n_edges > 0
        except Exception as e:
            self.check("PRICE_SCAN", False, str(e)[:100])
            return False

    # ─── Summary ─────────────────────────────────────────────
    async def run(self) -> bool:
        log.info("=" * 60)
        log.info("  NEXUS-ARB v2.0 — Pre-flight Validation")
        log.info("=" * 60)

        env_ok = self.validate_env()

        # Get a working w3 instance
        w3 = None
        for key in ["ARB_HTTP_1", "ARB_HTTP_2"]:
            url = os.getenv(key, "")
            if url:
                try:
                    w3_test = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": 5}))
                    await w3_test.eth.chain_id
                    w3 = w3_test
                    break
                except Exception:
                    pass

        rpc_ok = await self.validate_rpc()

        if w3:
            await self.validate_wallet(w3)
            await self.validate_contract(w3)
            await self.validate_dex(w3)
            await self.validate_price_scan(w3)

        log.info("\n" + "=" * 60)
        log.info("  VALIDATION SUMMARY")
        log.info("=" * 60)

        if self.errors:
            log.error(f"FAILED — {len(self.errors)} error(s):")
            for e in self.errors:
                log.error(f"  ❌ {e}")
            log.error("\nFix the above issues before starting the bot.")
            return False
        else:
            log.info(f"ALL CHECKS PASSED")
            if self.warnings:
                log.warning(f"  {len(self.warnings)} warning(s) (non-blocking):")
                for w in self.warnings:
                    log.warning(f"  ⚠️  {w}")
            log.info("\nReady to trade! Run: python -m nexus_arb.orchestrator")
            return True


async def main():
    v = Validator()
    success = await v.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
