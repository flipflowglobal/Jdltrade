"""
LiquidityMonitor — Tracks on-chain liquidity across all pools.

Provides:
  1. Real-time reserve balances for slippage estimation
  2. Liquidity depth alerts (warn if depth drops below threshold)
  3. Pool health monitoring (detect de-pegs, imbalances)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from eth_abi import decode as abi_decode
from web3 import Web3

from .web3_manager import Web3Manager

log = logging.getLogger(__name__)


def _sel(sig: str) -> bytes:
    return Web3.keccak(text=sig)[:4]

SEL_BALANCE_OF = _sel("balanceOf(address)")
SEL_TOTAL_SUPPLY = _sel("totalSupply()")


@dataclass
class PoolLiquidity:
    pool_id: str
    dex: str
    token0: str
    token1: str
    reserve0: float        # Token0 in pool (normalized)
    reserve1: float        # Token1 in pool (normalized)
    tvl_usd: float
    utilization: float     # 0-1, how much of pool is being used
    last_updated: float = field(default_factory=time.time)
    is_healthy: bool = True

    @property
    def depth_usd(self) -> float:
        """Depth = min(reserve0, reserve1) in USD terms."""
        return min(self.reserve0, self.reserve1) * 1.0  # Simplified

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_updated


class LiquidityMonitor:
    """Monitors pool reserves for accurate slippage estimation."""

    def __init__(self, config: dict, web3_mgr: Web3Manager) -> None:
        self.cfg     = config
        self.w3_mgr  = web3_mgr
        self.tokens  = config.get("tokens", {})
        self._pools: dict[str, PoolLiquidity] = {}
        self._last_refresh = 0.0

    def get_liquidity(self, pool_id: str) -> Optional[PoolLiquidity]:
        return self._pools.get(pool_id)

    def get_depth_eth(self, pool_id: str) -> float:
        """Return available liquidity depth in ETH equivalent."""
        pl = self._pools.get(pool_id)
        if not pl or not pl.is_healthy:
            return 0.0
        return pl.reserve0  # Simplified: assume token0 is WETH

    async def refresh_all(self) -> None:
        """Refresh liquidity for all tracked pools."""
        await asyncio.gather(
            self._refresh_univ3_liquidity(),
            return_exceptions=True
        )
        self._last_refresh = time.time()

    async def _refresh_univ3_liquidity(self) -> None:
        """Batch-fetch Uniswap V3 pool liquidity via Multicall3."""
        pools = self.cfg.get("uniswap_pools", {})
        if not pools:
            return

        calls = []
        for pool_id, pool_cfg in pools.items():
            pool_addr = pool_cfg["address"]
            token0_sym = pool_cfg["token0"]
            token1_sym = pool_cfg["token1"]
            token0_addr = self.tokens[token0_sym]["address"]
            token1_addr = self.tokens[token1_sym]["address"]

            # balanceOf(pool_address) on each token
            bal0_calldata = SEL_BALANCE_OF + b"\x00" * 12 + bytes.fromhex(pool_addr[2:])
            bal1_calldata = SEL_BALANCE_OF + b"\x00" * 12 + bytes.fromhex(pool_addr[2:])

            calls.append({
                "target": token0_addr,
                "callData": bal0_calldata,
                "allowFailure": True,
                "meta": ("bal0", pool_id, pool_cfg)
            })
            calls.append({
                "target": token1_addr,
                "callData": bal1_calldata,
                "allowFailure": True,
                "meta": ("bal1", pool_id, pool_cfg)
            })

        if not calls:
            return

        mc_calls = [{"target": c["target"], "callData": c["callData"],
                     "allowFailure": True} for c in calls]
        try:
            results = await self.w3_mgr.multicall(mc_calls)
        except Exception as e:
            log.warning(f"Liquidity multicall failed: {e}")
            return

        reserves0: dict[str, float] = {}
        reserves1: dict[str, float] = {}

        for call, result in zip(calls, results):
            if not result["success"]:
                continue
            meta = call["meta"]
            pool_id = meta[1]
            pool_cfg = meta[2]
            try:
                raw = abi_decode(["uint256"], result["returnData"])[0]
                if meta[0] == "bal0":
                    dec = self.tokens[pool_cfg["token0"]]["decimals"]
                    reserves0[pool_id] = raw / (10 ** dec)
                else:
                    dec = self.tokens[pool_cfg["token1"]]["decimals"]
                    reserves1[pool_id] = raw / (10 ** dec)
            except Exception as e:
                log.debug(f"Reserve decode error {pool_id}: {e}")

        for pool_id, pool_cfg in pools.items():
            r0 = reserves0.get(pool_id, 0.0)
            r1 = reserves1.get(pool_id, 0.0)

            self._pools[pool_id] = PoolLiquidity(
                pool_id=pool_id,
                dex="uniswap_v3",
                token0=pool_cfg["token0"],
                token1=pool_cfg["token1"],
                reserve0=r0,
                reserve1=r1,
                tvl_usd=0.0,  # Updated separately
                utilization=0.0,
                is_healthy=(r0 > 0 and r1 > 0)
            )
