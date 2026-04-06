"""
FlashLoanExecutor — Encodes and submits flash loan transactions.

Responsibilities:
  1. ABI-encode the SwapStep[] calldata for NexusFlashReceiver
  2. Estimate gas with safety buffer
  3. Set optimal gas price (EIP-1559 on Arbitrum)
  4. Sign and submit transaction
  5. Return tx hash for monitoring

Gas strategy on Arbitrum:
  - Arbitrum uses EIP-1559: maxFeePerGas + maxPriorityFeePerGas
  - Base fee is very low (~0.1 gwei) but can spike
  - Priority fee (tip) is typically 0.01-0.1 gwei
  - We set maxFee = base_fee * 2 + priority_fee as safety margin
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from eth_abi import encode as abi_encode
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3
from web3.types import TxParams

from .web3_manager import Web3Manager
from .vault import Vault

log = logging.getLogger(__name__)

# ─── NexusFlashReceiver ABI (minimal — only what we call) ────
NEXUS_ABI = [
    {
        "inputs": [
            {"name": "asset",  "type": "address"},
            {"name": "amount", "type": "uint256"},
            {
                "components": [
                    {"name": "dexType",      "type": "uint8"},
                    {"name": "tokenIn",      "type": "address"},
                    {"name": "tokenOut",     "type": "address"},
                    {"name": "amountIn",     "type": "uint256"},
                    {"name": "minAmountOut", "type": "uint256"},
                    {"name": "extraData",    "type": "bytes"}
                ],
                "name": "steps",
                "type": "tuple[]"
            }
        ],
        "name": "executeArbitrage",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "asset",     "type": "address"},
            {"name": "amount",    "type": "uint256"},
            {"name": "premium",   "type": "uint256"},
            {"name": "initiator", "type": "address"},
            {"name": "params",    "type": "bytes"}
        ],
        "name": "executeOperation",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

DEX_UNISWAP_V3 = 0
DEX_CURVE      = 1
DEX_BALANCER   = 2
DEX_CAMELOT_V3 = 3


class SwapStepBuilder:
    """Fluent builder for SwapStep tuples expected by NexusFlashReceiver."""

    def __init__(self) -> None:
        self._steps: list[tuple] = []

    def add_uniswap_v3(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        min_amount_out: int,
        amount_in: int = 0,
        router_override: str = "0x0000000000000000000000000000000000000000"
    ) -> "SwapStepBuilder":
        extra = abi_encode(["uint24", "address"], [fee, router_override])
        self._steps.append((
            DEX_UNISWAP_V3,
            token_in,
            token_out,
            amount_in,
            min_amount_out,
            extra
        ))
        return self

    def add_curve(
        self,
        token_in: str,
        token_out: str,
        route: list[str],       # address[11]
        swap_params: list,      # uint256[5][5]
        pools: list[str],       # address[5]
        min_amount_out: int,
        amount_in: int = 0
    ) -> "SwapStepBuilder":
        # Pad route to 11, pools to 5
        ZERO = "0x0000000000000000000000000000000000000000"
        route  = (route  + [ZERO] * 11)[:11]
        pools  = (pools  + [ZERO] * 5)[:5]
        # swap_params: 5x5 matrix
        while len(swap_params) < 5:
            swap_params.append([0, 0, 0, 0, 0])
        swap_params = [row + [0] * (5 - len(row)) for row in swap_params[:5]]

        extra = abi_encode(
            ["address[11]", "uint256[5][5]", "address[5]"],
            [route, swap_params, pools]
        )
        self._steps.append((
            DEX_CURVE,
            token_in,
            token_out,
            amount_in,
            min_amount_out,
            extra
        ))
        return self

    def add_balancer(
        self,
        token_in: str,
        token_out: str,
        pool_id: bytes,
        min_amount_out: int,
        amount_in: int = 0
    ) -> "SwapStepBuilder":
        extra = abi_encode(["bytes32"], [pool_id])
        self._steps.append((
            DEX_BALANCER,
            token_in,
            token_out,
            amount_in,
            min_amount_out,
            extra
        ))
        return self

    def build(self) -> list[tuple]:
        return self._steps


class FlashLoanExecutor:
    """
    Builds and submits flash loan arbitrage transactions.

    Uses EIP-1559 transactions optimised for Arbitrum L2.
    """

    def __init__(self, config: dict, web3_mgr: Web3Manager, vault: Vault) -> None:
        self.cfg     = config
        self.w3_mgr  = web3_mgr
        self.vault   = vault
        self.account: Optional[LocalAccount] = None
        self._contract_addr = os.getenv("NEXUS_RECEIVER_ADDRESS", "")
        self._gas_buffer    = config.get("trading", {}).get("gas_limit_buffer", 1.3)
        self._max_gas_gwei  = config.get("trading", {}).get("max_gas_price_gwei", 2.0)

    async def setup(self) -> None:
        self.account = self.vault.get_account()
        log.info(f"FlashLoanExecutor: wallet={self.account.address}")
        if not self._contract_addr:
            log.warning("NEXUS_RECEIVER_ADDRESS not set — cannot execute trades!")

    def _get_contract(self, w3: AsyncWeb3):
        return w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self._contract_addr),
            abi=NEXUS_ABI
        )

    async def estimate_gas(
        self,
        w3: AsyncWeb3,
        asset: str,
        amount_wei: int,
        steps: list[tuple]
    ) -> int:
        """Estimate gas for a flash loan call."""
        contract = self._get_contract(w3)
        try:
            gas = await contract.functions.executeArbitrage(
                AsyncWeb3.to_checksum_address(asset),
                amount_wei,
                steps
            ).estimate_gas({"from": self.account.address})
            return int(gas * self._gas_buffer)
        except Exception as e:
            log.warning(f"Gas estimation failed: {e}. Using default 800_000")
            return 800_000

    async def get_fee_params(self, w3: AsyncWeb3) -> dict:
        """Get EIP-1559 fee parameters for Arbitrum."""
        block = await w3.eth.get_block("latest")
        base_fee = block.get("baseFeePerGas", 100_000_000)  # ~0.1 gwei default

        # Arbitrum tip is usually very low
        priority_fee = await w3.eth.max_priority_fee
        priority_fee = max(priority_fee, 10_000_000)  # Minimum 0.01 gwei

        max_fee = int(base_fee * 2 + priority_fee)

        # Safety cap
        max_fee_cap = int(self._max_gas_gwei * 1e9)
        if max_fee > max_fee_cap:
            log.warning(f"Gas too high: {max_fee/1e9:.3f} gwei > cap {self._max_gas_gwei}")
            raise RuntimeError(f"Gas price {max_fee/1e9:.2f} gwei exceeds cap")

        return {
            "maxFeePerGas":         max_fee,
            "maxPriorityFeePerGas": priority_fee
        }

    async def execute(
        self,
        asset_address: str,
        amount_wei: int,
        steps: list[tuple],
        slippage_bps: int = 50,
        dry_run: bool = False
    ) -> Optional[str]:
        """
        Build, sign, and submit the flash loan transaction.

        Returns:
            Transaction hash as hex string, or None if dry_run.
        """
        if not self._contract_addr:
            raise RuntimeError("Contract address not configured")

        w3 = self.w3_mgr.best_http()

        if dry_run:
            log.info(f"DRY RUN: Would execute flash loan of {amount_wei/1e18:.4f} ETH")
            return None

        contract = self._get_contract(w3)
        asset    = AsyncWeb3.to_checksum_address(asset_address)

        # Estimate gas
        gas_limit = await self.estimate_gas(w3, asset, amount_wei, steps)

        # Fee params
        fee_params = await self.get_fee_params(w3)

        # Nonce
        nonce = await w3.eth.get_transaction_count(self.account.address, "pending")

        # Build transaction
        chain_id = self.cfg.get("network", {}).get("chain_id", 42161)
        tx: TxParams = {
            "from":                 self.account.address,
            "to":                   self._contract_addr,
            "data":                 contract.encodeABI(
                                        fn_name="executeArbitrage",
                                        args=[asset, amount_wei, steps]
                                    ),
            "gas":                  gas_limit,
            "maxFeePerGas":         fee_params["maxFeePerGas"],
            "maxPriorityFeePerGas": fee_params["maxPriorityFeePerGas"],
            "nonce":                nonce,
            "chainId":              chain_id,
            "type":                 2
        }

        # Sign
        signed = self.account.sign_transaction(tx)

        # Submit
        t0 = time.perf_counter()
        tx_hash = await w3.eth.send_raw_transaction(signed.rawTransaction)
        latency_ms = (time.perf_counter() - t0) * 1000

        tx_hex = tx_hash.hex()
        log.info(
            f"TX submitted: {tx_hex} "
            f"({amount_wei/1e18:.4f} ETH, "
            f"gas={gas_limit}, "
            f"submit_latency={latency_ms:.1f}ms)"
        )
        return tx_hex

    async def get_wallet_balance_eth(self) -> float:
        w3 = self.w3_mgr.best_http()
        balance = await w3.eth.get_balance(self.account.address)
        return balance / 1e18
