"""
Web3Manager — Latency-adaptive, self-healing RPC connection pool.

Architecture:
  - Maintains N HTTP endpoints ranked by exponential-moving-average latency
  - WebSocket connections for real-time block/event subscriptions
  - Multicall3 batching to minimise round-trips (crucial for speed)
  - Automatic failover + exponential backoff on errors
  - Prometheus metrics for latency, error rate, and RPC selection
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp
from eth_abi import decode as abi_decode, encode as abi_encode
from web3 import AsyncWeb3, Web3
from web3.middleware import async_geth_poa_middleware
from web3.providers import AsyncHTTPProvider, WebsocketProviderV2
from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger(__name__)

# ─── Prometheus Metrics ───────────────────────────────────────
rpc_requests_total    = Counter("nexus_rpc_requests_total", "Total RPC requests", ["endpoint", "method"])
rpc_errors_total      = Counter("nexus_rpc_errors_total",   "Total RPC errors",   ["endpoint"])
rpc_latency_seconds   = Histogram("nexus_rpc_latency_seconds", "RPC call latency", ["endpoint"],
                                   buckets=[.005, .01, .025, .05, .1, .25, .5, 1.0])
rpc_best_endpoint     = Gauge("nexus_rpc_best_endpoint_latency_ms", "Best RPC endpoint latency")
multicall_batch_size  = Histogram("nexus_multicall_batch_size", "Calls per multicall batch",
                                   buckets=[1, 5, 10, 20, 50, 100, 200])

# ─── Multicall3 ABI (minimal) ────────────────────────────────
MULTICALL3_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "target",   "type": "address"},
                {"name": "callData", "type": "bytes"}
            ],
            "name": "calls", "type": "tuple[]"
        }],
        "name": "aggregate",
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "returnData",  "type": "bytes[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{
            "components": [
                {"name": "target",       "type": "address"},
                {"name": "allowFailure", "type": "bool"},
                {"name": "callData",     "type": "bytes"}
            ],
            "name": "calls", "type": "tuple[]"
        }],
        "name": "aggregate3",
        "outputs": [{
            "components": [
                {"name": "success",    "type": "bool"},
                {"name": "returnData", "type": "bytes"}
            ],
            "name": "returnData", "type": "tuple[]"
        }],
        "stateMutability": "view",
        "type": "function"
    }
]

MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"


@dataclass
class EndpointStats:
    url: str
    latency_ema_ms: float = 100.0   # Start with pessimistic estimate
    error_count: int = 0
    request_count: int = 0
    is_healthy: bool = True
    last_check: float = field(default_factory=time.time)

    def update_latency(self, latency_ms: float, alpha: float = 0.2) -> None:
        """Exponential moving average update."""
        self.latency_ema_ms = alpha * latency_ms + (1 - alpha) * self.latency_ema_ms

    @property
    def score(self) -> float:
        """Lower is better. Penalise errors heavily."""
        penalty = self.error_count * 50  # 50ms penalty per error
        return self.latency_ema_ms + penalty


class Web3Manager:
    """
    Manages a pool of Web3 connections with automatic failover.

    Usage:
        mgr = Web3Manager(config)
        await mgr.start()
        w3  = mgr.best_http()               # Get fastest endpoint
        result = await mgr.call(contract, fn, *args)
        results = await mgr.multicall(calls) # Batch queries
    """

    def __init__(self, config: dict) -> None:
        self.cfg         = config
        self.http_urls   = self._collect_http_urls()
        self.ws_urls     = self._collect_ws_urls()
        self.stats: dict[str, EndpointStats] = {
            url: EndpointStats(url=url) for url in self.http_urls
        }
        self._w3_pool: dict[str, AsyncWeb3] = {}
        self._ws_w3: Optional[AsyncWeb3]    = None
        self._multicall_cache: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._health_task: Optional[asyncio.Task] = None

    # ─── Setup ───────────────────────────────────────────────
    def _collect_http_urls(self) -> list[str]:
        import os
        urls = []
        for key in ["ARB_HTTP_1", "ARB_HTTP_2", "ARB_HTTP_3", "ARB_HTTP_4"]:
            url = os.getenv(key, "")
            if url:
                urls.append(url)
        return urls

    def _collect_ws_urls(self) -> list[str]:
        import os
        urls = []
        for key in ["ARB_WS_PRIMARY", "ARB_WS_SECONDARY"]:
            url = os.getenv(key, "")
            if url:
                urls.append(url)
        return urls

    async def start(self) -> None:
        """Initialise connections and start health-check loop."""
        await self._init_connections()
        self._health_task = asyncio.create_task(self._health_loop())
        log.info(f"Web3Manager started with {len(self._w3_pool)} endpoints")

    async def stop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

    async def _init_connections(self) -> None:
        for url in self.http_urls:
            try:
                provider = AsyncHTTPProvider(url, request_kwargs={"timeout": 5})
                w3 = AsyncWeb3(provider)
                w3.middleware_onion.inject(async_geth_poa_middleware, layer=0)
                # Quick health check
                await w3.eth.block_number
                self._w3_pool[url] = w3
                log.info(f"Connected: {url[:50]}...")
            except Exception as e:
                log.warning(f"Failed to connect {url[:50]}: {e}")
                self.stats[url].is_healthy = False

    # ─── Endpoint Selection ──────────────────────────────────
    def best_http(self) -> AsyncWeb3:
        """Return the Web3 instance with lowest EMA latency."""
        healthy = [
            (url, s) for url, s in self.stats.items()
            if s.is_healthy and url in self._w3_pool
        ]
        if not healthy:
            raise RuntimeError("No healthy RPC endpoints available")
        best_url = min(healthy, key=lambda x: x[1].score)[0]
        return self._w3_pool[best_url]

    def all_healthy_w3(self) -> list[AsyncWeb3]:
        return [
            self._w3_pool[url] for url, s in self.stats.items()
            if s.is_healthy and url in self._w3_pool
        ]

    # ─── Timed RPC Call ──────────────────────────────────────
    async def call(
        self,
        contract_fn,
        *args,
        w3: Optional[AsyncWeb3] = None,
        retries: int = 3
    ) -> Any:
        """Execute a contract call with latency tracking and retry."""
        w3 = w3 or self.best_http()
        url = str(w3.provider.endpoint_uri)

        for attempt in range(retries):
            t0 = time.perf_counter()
            try:
                result = await contract_fn(*args).call()
                latency_ms = (time.perf_counter() - t0) * 1000
                self.stats[url].update_latency(latency_ms)
                self.stats[url].request_count += 1
                rpc_latency_seconds.labels(endpoint=url[:40]).observe(latency_ms / 1000)
                return result
            except Exception as e:
                self.stats[url].error_count += 1
                rpc_errors_total.labels(endpoint=url[:40]).inc()
                if attempt == retries - 1:
                    raise
                # Failover to next best endpoint
                w3 = self._failover(url)
                url = str(w3.provider.endpoint_uri)
                await asyncio.sleep(0.1 * (2 ** attempt))

    def _failover(self, failed_url: str) -> AsyncWeb3:
        """Mark endpoint as degraded and return next best."""
        self.stats[failed_url].latency_ema_ms *= 2  # Penalise
        return self.best_http()

    # ─── Multicall3 Batch ────────────────────────────────────
    async def multicall(
        self,
        calls: list[dict],   # [{"target": addr, "callData": bytes, "allowFailure": bool}]
        w3: Optional[AsyncWeb3] = None,
        block: str = "latest"
    ) -> list[dict]:
        """
        Execute multiple eth_call in a single RPC round-trip via Multicall3.

        Args:
            calls: list of {"target": str, "callData": bytes, "allowFailure": bool}

        Returns:
            list of {"success": bool, "returnData": bytes}
        """
        w3 = w3 or self.best_http()
        mc = w3.eth.contract(
            address=MULTICALL3_ADDRESS,
            abi=MULTICALL3_ABI
        )

        call_tuples = [
            (c["target"], c.get("allowFailure", True), c["callData"])
            for c in calls
        ]

        multicall_batch_size.observe(len(call_tuples))

        t0 = time.perf_counter()
        try:
            results = await mc.functions.aggregate3(call_tuples).call(
                block_identifier=block
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            url = str(w3.provider.endpoint_uri)
            self.stats.get(url, EndpointStats(url)).update_latency(latency_ms)
            rpc_latency_seconds.labels(endpoint=url[:40]).observe(latency_ms / 1000)
            return [{"success": r[0], "returnData": r[1]} for r in results]
        except Exception as e:
            log.error(f"Multicall3 failed: {e}")
            raise

    # ─── Health Check Loop ───────────────────────────────────
    async def _health_loop(self) -> None:
        """Periodically measure latency of all endpoints."""
        interval = self.cfg.get("rpc", {}).get("health_check_interval_seconds", 10)
        while True:
            await asyncio.sleep(interval)
            await self._check_all_endpoints()

    async def _check_all_endpoints(self) -> None:
        tasks = [self._check_endpoint(url) for url in self.http_urls]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Log best endpoint
        healthy = [(url, s) for url, s in self.stats.items() if s.is_healthy]
        if healthy:
            best = min(healthy, key=lambda x: x[1].score)
            rpc_best_endpoint.set(best[1].latency_ema_ms)

    async def _check_endpoint(self, url: str) -> None:
        if url not in self._w3_pool:
            # Try to reconnect
            try:
                provider = AsyncHTTPProvider(url, request_kwargs={"timeout": 5})
                w3 = AsyncWeb3(provider)
                await w3.eth.block_number
                self._w3_pool[url] = w3
                self.stats[url].is_healthy = True
                log.info(f"Reconnected: {url[:40]}")
            except Exception:
                return

        w3 = self._w3_pool[url]
        t0 = time.perf_counter()
        try:
            await w3.eth.block_number
            latency_ms = (time.perf_counter() - t0) * 1000
            self.stats[url].update_latency(latency_ms)
            max_latency = self.cfg.get("rpc", {}).get("max_latency_ms", 500)
            self.stats[url].is_healthy = (latency_ms < max_latency)
        except Exception:
            self.stats[url].is_healthy = False
            self.stats[url].error_count += 1

    # ─── Block Subscription ──────────────────────────────────
    async def subscribe_new_blocks(self, callback) -> None:
        """
        Subscribe to new blocks via WebSocket.
        Calls callback(block_data) for each new block.
        Auto-reconnects on failure.
        """
        backoff = [1, 2, 4, 8, 16]
        attempt = 0

        while True:
            for ws_url in self.ws_urls:
                try:
                    log.info(f"Connecting WebSocket: {ws_url[:50]}")
                    async with AsyncWeb3(WebsocketProviderV2(ws_url)) as w3:
                        subscription_id = await w3.eth.subscribe("newHeads")
                        async for response in w3.socket.process_subscriptions():
                            block = response["result"]
                            await callback(block)
                            attempt = 0  # Reset on success
                except Exception as e:
                    log.warning(f"WebSocket error: {e}")

            wait = backoff[min(attempt, len(backoff) - 1)]
            log.info(f"WebSocket reconnecting in {wait}s...")
            await asyncio.sleep(wait)
            attempt += 1

    # ─── Gas Price ───────────────────────────────────────────
    async def get_gas_price_gwei(self) -> float:
        w3 = self.best_http()
        gas_price = await w3.eth.gas_price
        return gas_price / 1e9

    async def get_block_number(self) -> int:
        w3 = self.best_http()
        return await w3.eth.block_number
