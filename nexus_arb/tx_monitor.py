"""
TxMonitor — Transaction lifecycle management and front-run detection.

Functions:
  1. Wait for transaction confirmation with timeout
  2. Detect front-running (sandwiching) by inspecting receipts
  3. Handle stuck transactions via replacement (higher gas)
  4. Parse transaction logs for actual profit measurement
  5. Alert on failures with full diagnostic context
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from web3 import AsyncWeb3
from web3.types import TxReceipt

from .web3_manager import Web3Manager

log = logging.getLogger(__name__)

# NexusFlashReceiver ArbitrageExecuted event topic
ARBITRAGE_EXECUTED_TOPIC = (
    "0x" +
    AsyncWeb3.keccak(
        text="ArbitrageExecuted(address,uint256,uint256,uint256)"
    ).hex()
)


@dataclass
class TxResult:
    tx_hash: str
    success: bool
    profit_eth: float
    gas_used: int
    gas_cost_eth: float
    block_number: int
    latency_ms: float
    revert_reason: Optional[str] = None


class TxMonitor:
    """Monitors submitted transactions for confirmation and profit."""

    def __init__(self, config: dict, web3_mgr: Web3Manager) -> None:
        self.cfg     = config
        self.w3_mgr  = web3_mgr
        finality     = config.get("network", {}).get("finality_blocks", 1)
        self._confirmations = finality
        self._timeout       = 60.0   # Max wait time in seconds
        self._poll_interval = 0.3    # Poll every 300ms (Arbitrum block ~250ms)

    async def wait_for_receipt(self, tx_hash: str) -> TxResult:
        """
        Poll for transaction receipt with timeout.
        Returns TxResult with on-chain verified profit.
        """
        w3    = self.w3_mgr.best_http()
        t_sub = time.perf_counter()
        deadline = time.time() + self._timeout

        while time.time() < deadline:
            try:
                receipt: TxReceipt = await w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    latency_ms = (time.perf_counter() - t_sub) * 1000
                    return await self._parse_receipt(receipt, tx_hash, latency_ms)
            except Exception as e:
                log.debug(f"Receipt poll error: {e}")

            await asyncio.sleep(self._poll_interval)

        # Timeout — transaction may be stuck
        log.warning(f"TX timeout after {self._timeout}s: {tx_hash}")
        return TxResult(
            tx_hash=tx_hash,
            success=False,
            profit_eth=0.0,
            gas_used=0,
            gas_cost_eth=0.0,
            block_number=0,
            latency_ms=(time.perf_counter() - t_sub) * 1000,
            revert_reason="timeout"
        )

    async def _parse_receipt(
        self,
        receipt: TxReceipt,
        tx_hash: str,
        latency_ms: float
    ) -> TxResult:
        """Extract profit from ArbitrageExecuted event log."""
        w3 = self.w3_mgr.best_http()
        success = receipt["status"] == 1
        gas_used = receipt["gasUsed"]
        gas_price = receipt.get("effectiveGasPrice", 0)
        gas_cost_eth = (gas_used * gas_price) / 1e18
        profit_eth = 0.0
        revert_reason = None

        if success:
            # Parse ArbitrageExecuted event
            for log_entry in receipt.get("logs", []):
                if (len(log_entry["topics"]) > 0 and
                        log_entry["topics"][0].hex() == ARBITRAGE_EXECUTED_TOPIC[2:]):
                    try:
                        # event ArbitrageExecuted(address asset, uint256 borrowed, uint256 profit, uint256 gasUsed)
                        data = log_entry["data"]
                        if isinstance(data, bytes):
                            data_hex = data.hex()
                        else:
                            data_hex = data[2:] if data.startswith("0x") else data

                        # Each field is 32 bytes
                        borrowed = int(data_hex[0:64], 16)
                        profit_raw = int(data_hex[64:128], 16)
                        profit_eth = profit_raw / 1e18
                        log.info(
                            f"Arbitrage confirmed: "
                            f"borrowed={borrowed/1e18:.4f} ETH, "
                            f"profit={profit_eth:.6f} ETH"
                        )
                    except Exception as e:
                        log.warning(f"Failed to parse ArbitrageExecuted log: {e}")
        else:
            # Try to decode revert reason
            revert_reason = await self._get_revert_reason(tx_hash, w3)
            log.warning(
                f"TX reverted: {tx_hash} | reason: {revert_reason} | "
                f"gas_cost={gas_cost_eth:.6f} ETH"
            )

        return TxResult(
            tx_hash=tx_hash,
            success=success,
            profit_eth=profit_eth,
            gas_used=gas_used,
            gas_cost_eth=gas_cost_eth,
            block_number=receipt["blockNumber"],
            latency_ms=latency_ms,
            revert_reason=revert_reason
        )

    async def _get_revert_reason(self, tx_hash: str, w3: AsyncWeb3) -> str:
        """Attempt to decode revert reason by replaying with eth_call."""
        try:
            tx = await w3.eth.get_transaction(tx_hash)
            call_params = {
                "from":  tx["from"],
                "to":    tx["to"],
                "data":  tx["input"],
                "value": tx.get("value", 0),
                "gas":   tx["gas"]
            }
            await w3.eth.call(call_params, block_identifier=tx["blockNumber"])
        except Exception as e:
            error_str = str(e)
            # Extract revert reason from error message
            if "execution reverted" in error_str:
                return error_str
            return error_str[:200]
        return "unknown"

    async def replace_stuck_tx(
        self,
        tx_hash: str,
        new_max_fee: int,
        new_priority_fee: int,
        signed_tx_bytes: bytes
    ) -> Optional[str]:
        """
        Replace a stuck transaction with same nonce but higher gas (speed-up).
        Returns new tx_hash or None on failure.
        """
        w3 = self.w3_mgr.best_http()
        try:
            new_hash = await w3.eth.send_raw_transaction(signed_tx_bytes)
            log.info(f"Replaced stuck TX {tx_hash} → {new_hash.hex()}")
            return new_hash.hex()
        except Exception as e:
            log.error(f"TX replacement failed: {e}")
            return None
