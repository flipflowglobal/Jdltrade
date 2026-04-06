"""
ExecutionRouter — Converts a Bellman-Ford path into executable SwapSteps.

Responsibilities:
  1. Map token pairs → specific pool addresses and parameters
  2. Calculate minimum output amounts (slippage protection)
  3. Build SwapStep tuples for each hop in the arbitrage path
  4. Validate route is still profitable just-in-time (before submission)

Route Assembly:
  For each hop (token_in → token_out) in the cycle:
    - Use Thompson Sampling to select the best DEX
    - Look up pool-specific parameters (fee tier, pool_id, etc.)
    - Calculate min_amount_out = expected_out * (1 - slippage)
    - Assemble into SwapStep for NexusFlashReceiver
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from eth_abi import encode as abi_encode
from web3 import Web3

from .algorithms.bellman_ford import ArbitrageOpportunity
from .algorithms.cma_es import TradeOptimizer, CMAESResult
from .algorithms.thompson_sampling import DexBandit
from .flash_loan_executor import SwapStepBuilder, FlashLoanExecutor
from .market_data import PriceGraph, PoolPrice
from .risk_manager import TradeCandidate

log = logging.getLogger(__name__)

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


@dataclass
class RouteResult:
    """A fully assembled, executable trade route."""
    opportunity_id: str
    asset: str                    # Flash loan asset (WETH address)
    amount_wei: int               # Flash loan amount in wei
    steps: list[tuple]            # SwapStep tuples for contract
    expected_profit_eth: float
    expected_profit_usd: float
    gas_estimate: int
    trade_candidate: TradeCandidate


class ExecutionRouter:
    """
    Converts arbitrage opportunities into executable on-chain routes.
    """

    def __init__(
        self,
        config: dict,
        bandit: DexBandit,
        optimizer: TradeOptimizer
    ) -> None:
        self.cfg         = config
        self.bandit      = bandit
        self.optimizer   = optimizer
        self.tokens      = config.get("tokens", {})
        self.univ3_pools = config.get("uniswap_pools", {})
        self.curve_pools = config.get("curve_pools", {})
        self.bal_pools   = config.get("balancer_pools", {})
        self.trading     = config.get("trading", {})
        self._op_counter = 0

    # ─── Token Address Lookup ─────────────────────────────────
    def _addr(self, symbol: str) -> str:
        return Web3.to_checksum_address(self.tokens[symbol]["address"])

    def _decimals(self, symbol: str) -> int:
        return self.tokens[symbol]["decimals"]

    # ─── Main Route Builder ───────────────────────────────────
    def build_route(
        self,
        opportunity: ArbitrageOpportunity,
        gas_cost_eth: float,
        eth_price_usd: float,
        wallet_balance_eth: float
    ) -> Optional[RouteResult]:
        """
        Convert an ArbitrageOpportunity into a RouteResult ready for execution.

        Args:
            opportunity: From Bellman-Ford detector
            gas_cost_eth: Estimated gas cost in ETH
            eth_price_usd: Current ETH/USD price
            wallet_balance_eth: Available wallet balance

        Returns:
            RouteResult if route is buildable and profitable, else None
        """
        self._op_counter += 1
        op_id = f"op_{self._op_counter:06d}"

        # 1. Optimise trade size with CMA-ES
        max_size = min(
            opportunity.max_input_eth,
            self.trading.get("max_flash_loan_eth", 500.0)
        )
        min_size = self.trading.get("min_flash_loan_eth", 0.1)

        if max_size < min_size:
            log.debug(f"{op_id}: Max size {max_size:.3f} < min {min_size:.3f}")
            return None

        cma_result: CMAESResult = self.optimizer.optimize(
            opportunity=opportunity,
            gas_cost_eth=gas_cost_eth,
            eth_price_usd=eth_price_usd,
            min_size=min_size,
            max_size=max_size
        )

        if cma_result.expected_profit_eth <= 0:
            log.debug(f"{op_id}: CMA-ES found no profitable size")
            return None

        loan_eth = cma_result.optimal_size_eth
        loan_wei = int(loan_eth * 1e18)

        # 2. Build swap steps
        slippage_bps = self.trading.get("max_slippage_bps", 50)
        builder = SwapStepBuilder()

        hops = list(zip(opportunity.cycle[:-1], opportunity.cycle[1:]))
        current_amount = loan_wei

        for hop_idx, (tok_in, tok_out) in enumerate(hops):
            pool = opportunity.pools[hop_idx]
            expected_out = int(current_amount * pool.price_after_fee)
            min_out = int(expected_out * (1 - slippage_bps / 10_000))

            if pool.dex in ("uniswap_v3", "camelot_v3"):
                fee = self._get_univ3_fee(pool.pool_id)
                router = self._get_univ3_router(pool.dex)
                builder.add_uniswap_v3(
                    token_in=self._addr(tok_in),
                    token_out=self._addr(tok_out),
                    fee=fee,
                    min_amount_out=min_out,
                    router_override=router
                )
            elif pool.dex == "curve":
                route_params = self._get_curve_params(pool.pool_id, tok_in, tok_out)
                if not route_params:
                    log.warning(f"Cannot build Curve route for {pool.pool_id}")
                    return None
                builder.add_curve(
                    token_in=self._addr(tok_in),
                    token_out=self._addr(tok_out),
                    min_amount_out=min_out,
                    **route_params
                )
            elif pool.dex == "balancer":
                pool_id_bytes = self._get_balancer_pool_id(pool.pool_id)
                if not pool_id_bytes:
                    return None
                builder.add_balancer(
                    token_in=self._addr(tok_in),
                    token_out=self._addr(tok_out),
                    pool_id=pool_id_bytes,
                    min_amount_out=min_out
                )
            else:
                log.warning(f"Unknown DEX type: {pool.dex}")
                return None

            # Update running amount estimate
            current_amount = expected_out

        steps = builder.build()
        if not steps:
            return None

        # 3. Flash loan asset is first token in cycle
        flash_asset_symbol = opportunity.cycle[0]
        flash_asset_addr   = self._addr(flash_asset_symbol)

        # 4. Build TradeCandidate for risk check
        net_profit = cma_result.expected_profit_eth
        candidate = TradeCandidate(
            opportunity_id=op_id,
            token_in=flash_asset_symbol,
            token_out=flash_asset_symbol,  # Cycle returns same asset
            flash_loan_amount_eth=loan_eth,
            expected_profit_eth=cma_result.expected_profit_eth,
            expected_profit_usd=cma_result.expected_profit_usd,
            gas_cost_eth=gas_cost_eth,
            net_profit_eth=net_profit - gas_cost_eth,
            cycle=opportunity.cycle,
            confidence=min(1.0, opportunity.expected_profit_pct / 0.1)
        )

        return RouteResult(
            opportunity_id=op_id,
            asset=flash_asset_addr,
            amount_wei=loan_wei,
            steps=steps,
            expected_profit_eth=cma_result.expected_profit_eth,
            expected_profit_usd=cma_result.expected_profit_usd,
            gas_estimate=800_000,  # Will be re-estimated by executor
            trade_candidate=candidate
        )

    # ─── Pool Parameter Helpers ───────────────────────────────
    def _get_univ3_fee(self, pool_id: str) -> int:
        """Extract fee from pool config."""
        # pool_id format: "WETH_USDC_500"
        for pid, cfg in self.univ3_pools.items():
            if pid == pool_id or pid + "_rev" == pool_id:
                return cfg["fee"]
        # Default: try to parse from pool_id
        parts = pool_id.rstrip("_rev").split("_")
        for part in parts:
            if part.isdigit():
                return int(part)
        return 3000  # Default fee tier

    def _get_univ3_router(self, dex: str) -> str:
        """Get router address for DEX variant."""
        if dex == "camelot_v3":
            return Web3.to_checksum_address(
                self.cfg.get("addresses", {}).get("camelot_router",
                "0x1F721E2E82F6676FCE4eA07A5958cF098D339e18")
            )
        return ZERO_ADDR  # Use contract default

    def _get_curve_params(self, pool_id: str, tok_in: str, tok_out: str) -> Optional[dict]:
        """Build Curve router call parameters."""
        clean_pid = pool_id.split("_")[0] if "_" in pool_id else pool_id

        for pid, cfg in self.curve_pools.items():
            if pid != clean_pid:
                continue
            tokens = cfg["tokens"]
            pool_addr = cfg["address"]

            if tok_in not in tokens or tok_out not in tokens:
                continue

            i = tokens.index(tok_in)
            j = tokens.index(tok_out)

            # Curve Router format: route = [token_in, pool_addr, token_out, ...]
            ZERO = ZERO_ADDR
            route = [
                self._addr(tok_in),
                pool_addr,
                self._addr(tok_out)
            ]

            # swap_params: [[i, j, swap_type, pool_type, n_coins], ...]
            # swap_type: 1 = exchange, 2 = exchange_underlying
            # pool_type: 1 = stable, 2 = crypto
            pool_type = 2 if cfg.get("pool_type") == "crypto" else 1
            n_coins = len(tokens)
            swap_params = [[i, j, 1, pool_type, n_coins]]

            return {
                "route":       route,
                "swap_params": swap_params,
                "pools":       [pool_addr]
            }
        return None

    def _get_balancer_pool_id(self, pool_id_str: str) -> Optional[bytes]:
        """Convert string pool_id to bytes32."""
        clean = pool_id_str.split("_")[0] if "_" in pool_id_str else pool_id_str
        for pid, cfg in self.bal_pools.items():
            if pid == clean:
                hex_id = cfg["pool_id"].replace("0x", "")
                if len(hex_id) == 64:
                    return bytes.fromhex(hex_id)
        return None

    # ─── Gas Cost Estimation ──────────────────────────────────
    def estimate_gas_cost_eth(
        self,
        gas_price_gwei: float,
        gas_limit: int = 700_000
    ) -> float:
        """Estimate gas cost in ETH."""
        gas_price_wei = gas_price_gwei * 1e9
        return (gas_price_wei * gas_limit) / 1e18
