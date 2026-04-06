"""
MarketData — High-frequency on-chain price and liquidity data pipeline.

Data sources (all on-chain, no external APIs):
  1. Uniswap V3 — slot0 sqrtPriceX96 → spot price, liquidity
  2. Curve Finance — get_dy() for exact quote
  3. Balancer V2 — queryBatchSwap for exact quote
  4. Chainlink — time-weighted oracle price (reference only)

All queries batched via Multicall3 to minimise latency.
Prices stored in a directed price graph for Bellman-Ford arbitrage detection.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from eth_abi import decode as abi_decode, encode as abi_encode

from .web3_manager import Web3Manager

log = logging.getLogger(__name__)

# ─── Function Selectors ───────────────────────────────────────
# Precomputed keccak256 selectors for hot-path calldata encoding

def _sel(sig: str) -> bytes:
    from web3 import Web3
    return Web3.keccak(text=sig)[:4]

SEL_SLOT0      = _sel("slot0()")
SEL_LIQUIDITY  = _sel("liquidity()")
SEL_GET_DY     = _sel("get_dy(int128,int128,uint256)")
SEL_LATEST_ANS = _sel("latestAnswer()")


@dataclass
class PoolPrice:
    """Normalised price quote from a single pool."""
    pool_id:    str           # e.g. "WETH_USDC_500"
    dex:        str           # "uniswap_v3" | "curve" | "balancer"
    token_in:   str           # Token symbol
    token_out:  str           # Token symbol
    price:      float         # Units of token_out per 1 token_in
    liquidity:  float         # Available liquidity in token_in units
    fee_bps:    int           # Fee in basis points
    timestamp:  float = field(default_factory=time.time)

    @property
    def price_after_fee(self) -> float:
        return self.price * (1 - self.fee_bps / 10_000)

    @property
    def log_price(self) -> float:
        """Used as edge weight in Bellman-Ford graph."""
        if self.price_after_fee <= 0:
            return float("inf")
        return -math.log(self.price_after_fee)

    def is_stale(self, max_age_ms: float = 2000) -> bool:
        return (time.time() - self.timestamp) * 1000 > max_age_ms


@dataclass
class PriceGraph:
    """Directed weighted graph of token exchange rates."""
    edges: dict[tuple[str, str], list[PoolPrice]] = field(default_factory=dict)

    def add(self, pp: PoolPrice) -> None:
        key = (pp.token_in, pp.token_out)
        if key not in self.edges:
            self.edges[key] = []
        # Keep only best price per pool_id
        self.edges[key] = [e for e in self.edges[key] if e.pool_id != pp.pool_id]
        self.edges[key].append(pp)

    def best_price(self, token_in: str, token_out: str) -> Optional[PoolPrice]:
        """Return pool with highest price_after_fee."""
        edges = self.edges.get((token_in, token_out), [])
        if not edges:
            return None
        return max(edges, key=lambda e: e.price_after_fee)

    def tokens(self) -> set[str]:
        t = set()
        for a, b in self.edges:
            t.add(a)
            t.add(b)
        return t

    def to_weight_matrix(self) -> tuple[list[str], dict[tuple[str, str], float]]:
        """Returns (token_list, weight_dict) for Bellman-Ford."""
        tokens = sorted(self.tokens())
        weights: dict[tuple[str, str], float] = {}
        for (a, b), pools in self.edges.items():
            if pools:
                best = min(pools, key=lambda p: p.log_price)
                weights[(a, b)] = best.log_price
        return tokens, weights


class MarketData:
    """
    Continuously refreshes on-chain prices using Multicall3 batching.

    After each refresh cycle, emits an updated PriceGraph.
    Refresh latency target: <50ms on Arbitrum.
    """

    def __init__(self, config: dict, web3_mgr: Web3Manager) -> None:
        self.cfg    = config
        self.w3_mgr = web3_mgr
        self.graph  = PriceGraph()
        self._tokens = config.get("tokens", {})
        self._univ3_pools  = config.get("uniswap_pools", {})
        self._curve_pools  = config.get("curve_pools", {})
        self._balancer_pools = config.get("balancer_pools", {})
        self._addrs = config.get("addresses", {})
        self._last_refresh = 0.0

    # ─── Token Utilities ─────────────────────────────────────
    def _token_addr(self, symbol: str) -> str:
        return self._tokens[symbol]["address"]

    def _token_decimals(self, symbol: str) -> int:
        return self._tokens[symbol]["decimals"]

    def _normalize(self, raw: int, decimals: int) -> float:
        return raw / (10 ** decimals)

    # ─── Uniswap V3 Price from sqrtPriceX96 ─────────────────
    @staticmethod
    def _sqrt_price_x96_to_price(
        sqrt_price_x96: int,
        token0_decimals: int,
        token1_decimals: int,
        token0_is_in: bool
    ) -> float:
        """
        Convert Uniswap V3 sqrtPriceX96 to human-readable price.

        sqrtPriceX96 = sqrt(token1/token0) * 2^96
        price (token1 per token0) = (sqrtPriceX96 / 2^96)^2
        """
        Q96 = 2 ** 96
        price_raw = (sqrt_price_x96 / Q96) ** 2
        # Adjust for decimal difference
        decimal_adj = 10 ** (token0_decimals - token1_decimals)
        price_token1_per_token0 = price_raw * decimal_adj

        if token0_is_in:
            return price_token1_per_token0
        else:
            return 1.0 / price_token1_per_token0 if price_token1_per_token0 > 0 else 0.0

    # ─── Build Multicall Batch for Uniswap V3 ────────────────
    def _build_univ3_calls(self) -> list[dict]:
        calls = []
        for pool_id, pool_cfg in self._univ3_pools.items():
            addr = pool_cfg["address"]
            # slot0: returns sqrtPriceX96, tick, ...
            calls.append({
                "target": addr,
                "callData": SEL_SLOT0,
                "allowFailure": True,
                "meta": ("univ3_slot0", pool_id, pool_cfg)
            })
            # liquidity
            calls.append({
                "target": addr,
                "callData": SEL_LIQUIDITY,
                "allowFailure": True,
                "meta": ("univ3_liq", pool_id, pool_cfg)
            })
        return calls

    # ─── Build Multicall Batch for Chainlink ─────────────────
    def _build_chainlink_calls(self) -> list[dict]:
        calls = []
        for feed_name, feed_addr in self._addrs.get("chainlink", {}).items():
            calls.append({
                "target": feed_addr,
                "callData": SEL_LATEST_ANS,
                "allowFailure": True,
                "meta": ("chainlink", feed_name)
            })
        return calls

    # ─── Main Refresh ─────────────────────────────────────────
    async def refresh(self) -> PriceGraph:
        """
        Refresh all prices in a single Multicall3 batch.
        Returns updated PriceGraph.
        """
        t0 = time.perf_counter()

        univ3_calls      = self._build_univ3_calls()
        chainlink_calls  = self._build_chainlink_calls()

        # Strip meta before sending to multicall
        all_calls = univ3_calls + chainlink_calls
        mc_calls = [{"target": c["target"], "callData": c["callData"],
                     "allowFailure": c.get("allowFailure", True)} for c in all_calls]

        results = await self.w3_mgr.multicall(mc_calls)

        # Process results
        liquidity_cache: dict[str, int] = {}

        for i, (call, result) in enumerate(zip(all_calls, results)):
            if not result["success"]:
                continue
            meta = call.get("meta", ())

            if meta[0] == "univ3_slot0":
                _, pool_id, pool_cfg = meta
                try:
                    decoded = abi_decode(
                        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                        result["returnData"]
                    )
                    sqrt_price_x96 = decoded[0]
                    token0 = pool_cfg["token0"]
                    token1 = pool_cfg["token1"]
                    d0 = self._token_decimals(token0)
                    d1 = self._token_decimals(token1)
                    fee_bps = pool_cfg["fee"] // 10  # fee in bps (500 → 5 bps * 10)

                    # Price: token1 per token0
                    price_t1_per_t0 = self._sqrt_price_x96_to_price(
                        sqrt_price_x96, d0, d1, True
                    )
                    # Price: token0 per token1
                    price_t0_per_t1 = 1.0 / price_t1_per_t0 if price_t1_per_t0 > 0 else 0.0

                    # Add both directions
                    liq = liquidity_cache.get(pool_id, 1_000_000)

                    self.graph.add(PoolPrice(
                        pool_id=pool_id,
                        dex="uniswap_v3",
                        token_in=token0,
                        token_out=token1,
                        price=price_t1_per_t0,
                        liquidity=self._normalize(liq, d0),
                        fee_bps=pool_cfg["fee"] // 10
                    ))
                    self.graph.add(PoolPrice(
                        pool_id=f"{pool_id}_rev",
                        dex="uniswap_v3",
                        token_in=token1,
                        token_out=token0,
                        price=price_t0_per_t1,
                        liquidity=self._normalize(liq, d1),
                        fee_bps=pool_cfg["fee"] // 10
                    ))
                except Exception as e:
                    log.debug(f"Decode error {pool_id} slot0: {e}")

            elif meta[0] == "univ3_liq":
                _, pool_id, _ = meta
                try:
                    liq = abi_decode(["uint128"], result["returnData"])[0]
                    liquidity_cache[pool_id] = liq
                except Exception:
                    pass

            elif meta[0] == "chainlink":
                _, feed_name = meta
                try:
                    price_raw = abi_decode(["int256"], result["returnData"])[0]
                    # Chainlink 8-decimal feeds
                    price_usd = price_raw / 1e8
                    log.debug(f"Chainlink {feed_name}: ${price_usd:.2f}")
                except Exception:
                    pass

        # Fetch Curve and Balancer quotes (separate calls, harder to batch)
        await asyncio.gather(
            self._refresh_curve(),
            self._refresh_balancer(),
            return_exceptions=True
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._last_refresh = time.time()
        log.debug(f"Market refresh: {len(self.graph.edges)} edges in {elapsed_ms:.1f}ms")

        return self.graph

    # ─── Curve Price Fetch ────────────────────────────────────
    async def _refresh_curve(self) -> None:
        """Query Curve pools for get_dy quotes."""
        w3 = self.w3_mgr.best_http()
        calls = []

        for pool_id, pool_cfg in self._curve_pools.items():
            tokens = pool_cfg["tokens"]
            addr   = pool_cfg["address"]
            n = len(tokens)

            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    token_in  = tokens[i]
                    token_out = tokens[j]
                    decimals_in = self._token_decimals(token_in)
                    # Quote for 1 unit of token_in
                    dx = 10 ** decimals_in

                    calldata = SEL_GET_DY + abi_encode(
                        ["int128", "int128", "uint256"],
                        [i, j, dx]
                    )
                    calls.append({
                        "target": addr,
                        "callData": calldata,
                        "allowFailure": True,
                        "meta": ("curve", pool_id, pool_cfg, i, j, token_in, token_out)
                    })

        if not calls:
            return

        mc_calls = [{"target": c["target"], "callData": c["callData"],
                     "allowFailure": True} for c in calls]
        try:
            results = await self.w3_mgr.multicall(mc_calls)
        except Exception as e:
            log.warning(f"Curve multicall failed: {e}")
            return

        for call, result in zip(calls, results):
            if not result["success"]:
                continue
            meta = call["meta"]
            _, pool_id, pool_cfg, i, j, token_in, token_out = meta
            try:
                dy = abi_decode(["uint256"], result["returnData"])[0]
                dec_in  = self._token_decimals(token_in)
                dec_out = self._token_decimals(token_out)
                price   = dy / (10 ** dec_out)  # tokens out per 1 token in
                fee_bps = int(pool_cfg.get("fee", 4))  # Curve ~0.04%

                self.graph.add(PoolPrice(
                    pool_id=f"{pool_id}_{i}_{j}",
                    dex="curve",
                    token_in=token_in,
                    token_out=token_out,
                    price=price,
                    liquidity=1_000_000,  # Updated separately by LiquidityMonitor
                    fee_bps=fee_bps
                ))
            except Exception as e:
                log.debug(f"Curve decode error {pool_id}: {e}")

    # ─── Balancer Price Fetch ─────────────────────────────────
    async def _refresh_balancer(self) -> None:
        """Use queryBatchSwap for exact Balancer quotes (view call)."""
        BALANCER_VAULT_ABI = [{
            "inputs": [
                {"name": "kind",  "type": "uint8"},
                {"components": [
                    {"name": "poolId",        "type": "bytes32"},
                    {"name": "assetInIndex",  "type": "uint256"},
                    {"name": "assetOutIndex", "type": "uint256"},
                    {"name": "amount",        "type": "uint256"},
                    {"name": "userData",      "type": "bytes"}
                ], "name": "swaps", "type": "tuple[]"},
                {"name": "assets", "type": "address[]"},
                {"components": [
                    {"name": "sender",             "type": "address"},
                    {"name": "fromInternalBalance", "type": "bool"},
                    {"name": "recipient",          "type": "address"},
                    {"name": "toInternalBalance",  "type": "bool"}
                ], "name": "funds", "type": "tuple"}
            ],
            "name": "queryBatchSwap",
            "outputs": [{"name": "assetDeltas", "type": "int256[]"}],
            "stateMutability": "nonpayable",
            "type": "function"
        }]

        w3 = self.w3_mgr.best_http()
        vault_addr = self._addrs.get("balancer_vault", "")
        if not vault_addr:
            return

        vault = w3.eth.contract(address=vault_addr, abi=BALANCER_VAULT_ABI)
        ZERO_ADDR = "0x0000000000000000000000000000000000000000"

        for pool_id, pool_cfg in self._balancer_pools.items():
            pool_bytes32 = bytes.fromhex(pool_cfg["pool_id"].replace("0x", ""))
            tokens_in_pool = pool_cfg.get("tokens", [])

            for ti, token_in in enumerate(tokens_in_pool):
                for tj, token_out in enumerate(tokens_in_pool):
                    if ti == tj:
                        continue
                    try:
                        dec_in = self._token_decimals(token_in)
                        amount_in = 10 ** dec_in  # 1 unit

                        fund_management = (ZERO_ADDR, False, ZERO_ADDR, False)
                        assets = [self._token_addr(t) for t in tokens_in_pool]

                        deltas = await vault.functions.queryBatchSwap(
                            0,  # GIVEN_IN
                            [(pool_bytes32, ti, tj, amount_in, b"")],
                            assets,
                            fund_management
                        ).call()

                        amount_out = -deltas[tj] if deltas[tj] < 0 else 0
                        if amount_out > 0:
                            dec_out = self._token_decimals(token_out)
                            price = amount_out / (10 ** dec_out)
                            self.graph.add(PoolPrice(
                                pool_id=f"{pool_id}_{ti}_{tj}",
                                dex="balancer",
                                token_in=token_in,
                                token_out=token_out,
                                price=price,
                                liquidity=1_000_000,
                                fee_bps=30  # Balancer ~0.3%
                            ))
                    except Exception as e:
                        log.debug(f"Balancer query error {pool_id}: {e}")
