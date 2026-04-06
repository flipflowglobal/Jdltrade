"""
Deploy NexusFlashReceiver to Arbitrum mainnet.

Usage:
    python scripts/deploy.py [--dry-run]

Steps:
  1. Compile NexusFlashReceiver.sol (requires solc or hardhat)
  2. Verify wallet has enough ETH for deployment
  3. Deploy contract with correct constructor args
  4. Verify contract on Arbiscan
  5. Write deployed address to .env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv, set_key
from web3 import AsyncWeb3, Web3
from eth_account import Account

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─── Arbitrum Mainnet Contract Addresses ──────────────────────
AAVE_POOL     = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
UNIV3_ROUTER  = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
CURVE_ROUTER  = "0x4c2Af2Df2a7E567B5155879720619EA06C5BB15D"
BALANCER_VAULT = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"


def compile_contract() -> dict:
    """Compile NexusFlashReceiver using solc or hardhat."""
    # Try hardhat first
    hardhat_out = Path("hardhat/artifacts/contracts/NexusFlashReceiver.sol/NexusFlashReceiver.json")
    if hardhat_out.exists():
        with open(hardhat_out) as f:
            artifact = json.load(f)
        log.info(f"Using compiled artifact: {hardhat_out}")
        return {
            "abi":      artifact["abi"],
            "bytecode": artifact["bytecode"]
        }

    # Try solc
    try:
        result = subprocess.run(
            [
                "solc",
                "--optimize",
                "--optimize-runs", "200",
                "--combined-json", "abi,bin",
                "--base-path", ".",
                "--include-path", ".",
                "contracts/NexusFlashReceiver.sol"
            ],
            capture_output=True,
            text=True,
            check=True
        )
        compiled = json.loads(result.stdout)
        key = "contracts/NexusFlashReceiver.sol:NexusFlashReceiver"
        contract_data = compiled["contracts"][key]
        return {
            "abi":      json.loads(contract_data["abi"]),
            "bytecode": "0x" + contract_data["bin"]
        }
    except FileNotFoundError:
        log.error("Neither hardhat artifacts nor solc found!")
        log.error("Install: npm install --prefix hardhat && cd hardhat && npx hardhat compile")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        log.error(f"Compilation failed:\n{e.stderr}")
        sys.exit(1)


async def deploy(dry_run: bool = False) -> str:
    load_dotenv()

    pk = os.getenv("PRIVATE_KEY", "")
    if not pk:
        log.error("PRIVATE_KEY not set in .env")
        sys.exit(1)

    rpc_url = os.getenv("ARB_HTTP_1", "")
    if not rpc_url:
        log.error("ARB_HTTP_1 not set in .env")
        sys.exit(1)

    account = Account.from_key(pk)
    log.info(f"Deployer: {account.address}")

    # Connect
    from web3.providers import AsyncHTTPProvider
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))

    # Check chain
    chain_id = await w3.eth.chain_id
    if chain_id != 42161:
        log.error(f"Wrong network! Expected Arbitrum (42161), got {chain_id}")
        log.error("Set ARB_HTTP_1 to an Arbitrum RPC endpoint")
        sys.exit(1)

    # Check balance
    balance_wei = await w3.eth.get_balance(account.address)
    balance_eth = balance_wei / 1e18
    log.info(f"Wallet balance: {balance_eth:.6f} ETH")

    if balance_eth < 0.01:
        log.error(f"Insufficient ETH: {balance_eth:.6f} < 0.01")
        sys.exit(1)

    # Compile
    log.info("Compiling NexusFlashReceiver...")
    compiled = compile_contract()

    # Create contract object
    contract = w3.eth.contract(
        abi=compiled["abi"],
        bytecode=compiled["bytecode"]
    )

    # Estimate deployment gas
    log.info("Estimating deployment gas...")
    try:
        gas_estimate = await contract.constructor(
            AAVE_POOL,
            UNIV3_ROUTER,
            CURVE_ROUTER,
            BALANCER_VAULT
        ).estimate_gas({"from": account.address})
        gas_limit = int(gas_estimate * 1.3)
    except Exception as e:
        log.warning(f"Gas estimation failed: {e}. Using 3,000,000")
        gas_limit = 3_000_000

    # Get gas price
    block = await w3.eth.get_block("latest")
    base_fee = block.get("baseFeePerGas", 100_000_000)
    priority_fee = await w3.eth.max_priority_fee
    max_fee = int(base_fee * 2 + priority_fee)

    deploy_cost_eth = (gas_limit * max_fee) / 1e18
    log.info(f"Deployment cost estimate: {deploy_cost_eth:.6f} ETH")

    if dry_run:
        log.info("[DRY RUN] Would deploy NexusFlashReceiver")
        log.info(f"  Gas limit:   {gas_limit:,}")
        log.info(f"  Max fee:     {max_fee/1e9:.4f} gwei")
        log.info(f"  Total cost:  {deploy_cost_eth:.6f} ETH")
        return "0x_DRY_RUN_"

    # Deploy
    log.info("Deploying NexusFlashReceiver to Arbitrum mainnet...")
    nonce = await w3.eth.get_transaction_count(account.address)

    tx = await contract.constructor(
        AAVE_POOL,
        UNIV3_ROUTER,
        CURVE_ROUTER,
        BALANCER_VAULT
    ).build_transaction({
        "from":                 account.address,
        "nonce":                nonce,
        "gas":                  gas_limit,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "chainId":              42161,
        "type":                 2
    })

    signed = account.sign_transaction(tx)
    tx_hash = await w3.eth.send_raw_transaction(signed.rawTransaction)
    log.info(f"TX submitted: {tx_hash.hex()}")
    log.info("Waiting for confirmation...")

    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt["status"] != 1:
        log.error(f"Deployment FAILED! TX: {tx_hash.hex()}")
        sys.exit(1)

    contract_address = receipt["contractAddress"]
    log.info(f"✅ NexusFlashReceiver deployed at: {contract_address}")
    log.info(f"   Block: {receipt['blockNumber']}")
    log.info(f"   Gas used: {receipt['gasUsed']:,}")

    # Save to .env
    env_path = Path(".env")
    if env_path.exists():
        set_key(str(env_path), "NEXUS_RECEIVER_ADDRESS", contract_address)
        log.info(f"Address saved to .env: NEXUS_RECEIVER_ADDRESS={contract_address}")
    else:
        log.warning(f"No .env file found. Set manually: NEXUS_RECEIVER_ADDRESS={contract_address}")

    log.info("\nNext steps:")
    log.info("  1. Verify contract on Arbiscan:")
    log.info(f"     https://arbiscan.io/address/{contract_address}")
    log.info("  2. Run validation: python scripts/validate_mainnet.py")
    log.info("  3. Start trading: python -m nexus_arb.orchestrator")

    return contract_address


def main():
    parser = argparse.ArgumentParser(description="Deploy NexusFlashReceiver")
    parser.add_argument("--dry-run", action="store_true", help="Simulate deployment only")
    args = parser.parse_args()
    asyncio.run(deploy(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
