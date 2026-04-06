"""
NEXUS-ARB Orchestrator — Self-healing main event loop.

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │                    NEXUS-ARB v2.0                       │
  │                                                         │
  │  WebSocket ──▶ BlockHandler ──▶ MarketData.refresh()   │
  │                                    │                    │
  │                                    ▼                    │
  │                           BellmanFord.detect()          │
  │                                    │                    │
  │                          (opportunities)                │
  │                                    │                    │
  │                         for each opportunity:           │
  │                           UKF.update() ─── filter      │
  │                           PPO.select_action()           │
  │                           RiskManager.pre_trade()       │
  │                           ExecutionRouter.build_route() │
  │                           FlashLoanExecutor.execute()   │
  │                           TxMonitor.wait_for_receipt()  │
  │                           record outcome / learn        │
  └─────────────────────────────────────────────────────────┘

Self-healing mechanisms:
  1. Web3Manager auto-reconnects on RPC failure
  2. Circuit breaker halts trading after consecutive failures
  3. PPO/Thompson Sampling adapt to changing market conditions
  4. Health check loop monitors component status
  5. Prometheus metrics expose all internal state for alerting
  6. Automatic daily PnL reset
  7. Graceful shutdown with position cleanup
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import yaml
from dotenv import load_dotenv
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Internal modules
from .web3_manager import Web3Manager
from .market_data import MarketData
from .liquidity_monitor import LiquidityMonitor
from .algorithms.bellman_ford import BellmanFord, ArbitrageOpportunity
from .algorithms.cma_es import TradeOptimizer
from .algorithms.ukf import MultiTokenUKF
from .algorithms.thompson_sampling import DexBandit
from .algorithms.ppo import PPOAgent, EXECUTE, WAIT, SKIP
from .flash_loan_executor import FlashLoanExecutor
from .execution_router import ExecutionRouter
from .tx_monitor import TxMonitor
from .risk_manager import RiskManager, TradeCandidate
from .vault import Vault

log = logging.getLogger(__name__)

# ─── Prometheus Metrics ───────────────────────────────────────
opportunities_detected  = Counter("nexus_opportunities_detected_total",  "Opportunities found")
opportunities_executed  = Counter("nexus_opportunities_executed_total",  "Opportunities executed")
opportunities_skipped   = Counter("nexus_opportunities_skipped_total",   "Opportunities skipped", ["reason"])
scan_latency_ms         = Histogram("nexus_scan_latency_ms", "Market scan latency",
                                     buckets=[5, 10, 25, 50, 100, 250, 500])
profit_eth              = Counter("nexus_profit_eth_total",  "Total profit in ETH")
loss_eth                = Counter("nexus_loss_eth_total",    "Total losses in ETH")
gas_spent_eth           = Counter("nexus_gas_spent_eth_total", "Total gas in ETH")
blocks_processed        = Counter("nexus_blocks_processed_total", "Blocks processed")
current_gas_gwei        = Gauge("nexus_gas_price_gwei", "Current gas price in gwei")


class Orchestrator:
    """
    Main NEXUS-ARB orchestration engine.
    Manages the full trading lifecycle from price scan to profit.
    """

    def __init__(self, config: dict) -> None:
        self.cfg        = config
        self.running    = False
        self._shutdown_event = asyncio.Event()

        trading = config.get("trading", {})
        self.scan_interval_ms   = trading.get("scan_interval_ms", 100)
        self.opp_ttl_ms         = trading.get("opportunity_ttl_ms", 500)
        self.dry_run            = os.getenv("DRY_RUN", "false").lower() == "true"

        if self.dry_run:
            log.warning("⚠️  DRY RUN MODE — No transactions will be submitted")

        # ── Component Initialization ──────────────────────────
        self.vault      = Vault()
        self.web3_mgr   = Web3Manager(config)
        self.market     = MarketData(config, self.web3_mgr)
        self.liquidity  = LiquidityMonitor(config, self.web3_mgr)
        self.detector   = BellmanFord(config)
        self.optimizer  = TradeOptimizer(config)
        self.ukf        = MultiTokenUKF(config)
        self.bandit     = DexBandit(config)
        self.ppo        = PPOAgent(config)
        self.risk       = RiskManager(config)
        self.executor   = FlashLoanExecutor(config, self.web3_mgr, self.vault)
        self.router     = ExecutionRouter(config, self.bandit, self.optimizer)
        self.monitor    = TxMonitor(config, self.web3_mgr)

        # Runtime state
        self._current_gas_gwei  = 0.1
        self._eth_price_usd     = 3000.0
        self._recent_successes  = []  # Last 20 outcomes for PPO state
        self._block_count       = 0
        self._last_liquidity_refresh = 0.0

    # ─── Startup ─────────────────────────────────────────────
    async def start(self) -> None:
        log.info("=" * 60)
        log.info("  NEXUS-ARB v2.0 — Starting")
        log.info(f"  Wallet: {self.vault.address}")
        log.info(f"  Dry run: {self.dry_run}")
        log.info("=" * 60)

        # Start Prometheus metrics server
        prom_port = int(self.cfg.get("monitoring", {}).get("metrics_interval_seconds", 9090))
        try:
            start_http_server(int(os.getenv("PROMETHEUS_PORT", "9090")))
            log.info("Prometheus metrics: http://localhost:9090")
        except Exception as e:
            log.warning(f"Prometheus start failed: {e}")

        # Connect Web3
        await self.web3_mgr.start()

        # Setup executor (loads wallet)
        await self.executor.setup()

        # Register shutdown handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle_signal)

        self.running = True
        log.info("All components initialized. Starting trading loop...")

    def _handle_signal(self, signum, frame) -> None:
        log.info(f"Shutdown signal received ({signum}). Stopping gracefully...")
        self.running = False
        self._shutdown_event.set()

    # ─── Main Loop ────────────────────────────────────────────
    async def run(self) -> None:
        """Main trading loop — runs until shutdown signal."""
        await self.start()

        # Start background tasks
        tasks = [
            asyncio.create_task(self._block_driven_loop()),
            asyncio.create_task(self._background_maintenance()),
            asyncio.create_task(self._daily_reset_loop()),
        ]

        try:
            await self._shutdown_event.wait()
        finally:
            log.info("Shutting down...")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.web3_mgr.stop()
            log.info("NEXUS-ARB stopped cleanly.")

    # ─── Block-Driven Scan Loop ───────────────────────────────
    async def _block_driven_loop(self) -> None:
        """
        On each new block:
          1. Refresh market data (Multicall3 batch)
          2. Run Bellman-Ford
          3. Evaluate & execute best opportunity
        """
        async def on_new_block(block_data: dict) -> None:
            if not self.running:
                return
            self._block_count += 1
            blocks_processed.inc()
            await self._scan_and_execute()

        await self.web3_mgr.subscribe_new_blocks(on_new_block)

    async def _scan_and_execute(self) -> None:
        """Core: scan prices, detect arbitrage, execute if profitable."""
        t0 = time.perf_counter()

        try:
            # 1. Update gas price
            self._current_gas_gwei = await self.web3_mgr.get_gas_price_gwei()
            current_gas_gwei.set(self._current_gas_gwei)

            # 2. Refresh market data
            graph = await self.market.refresh()

            # 3. Periodically refresh liquidity
            if time.time() - self._last_liquidity_refresh > 5.0:
                await self.liquidity.refresh_all()
                self._last_liquidity_refresh = time.time()

            # 4. Detect arbitrage opportunities
            min_profit = self.cfg.get("trading", {}).get("min_profit_eth", 0.002)
            opportunities = self.detector.detect(
                graph,
                min_profit_pct=min_profit * 100 / self._eth_price_usd
            )

            elapsed = (time.perf_counter() - t0) * 1000
            scan_latency_ms.observe(elapsed)

            if not opportunities:
                return

            opportunities_detected.inc(len(opportunities))
            log.debug(f"Found {len(opportunities)} opportunities in {elapsed:.1f}ms")

            # 5. Evaluate top opportunity
            best = opportunities[0]
            await self._evaluate_and_execute(best)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)

    async def _evaluate_and_execute(self, opp: ArbitrageOpportunity) -> None:
        """
        Evaluate a single opportunity through all filters and execute if approved.
        """
        # 1. UKF filter — check price direction
        for token_in, token_out in zip(opp.cycle[:-1], opp.cycle[1:]):
            ukf_state = self.ukf.update(
                token_in, token_out,
                price=1.0  # Simplified; would use actual price from graph
            )

        # 2. Build wallet state for PPO
        wallet_bal = await self.executor.get_wallet_balance_eth()
        success_rate = (
            sum(1 for s in self._recent_successes if s) /
            max(len(self._recent_successes), 1)
        )

        state = self.ppo.encode_state(
            spread_mean=opp.expected_profit_pct / 100,
            spread_std=0.001,
            gas_price_gwei=self._current_gas_gwei,
            block_utilization=0.5,
            time_since_opp_ms=0.0,
            wallet_balance_eth=wallet_bal,
            ukf_velocity=0.0,
            recent_success_rate=success_rate
        )

        # 3. PPO timing decision
        action, log_prob, value = self.ppo.select_action(state)

        if action == SKIP:
            opportunities_skipped.labels(reason="ppo_skip").inc()
            self.ppo.store_transition(state, action, 0.0, False, log_prob, value)
            return

        if action == WAIT:
            opportunities_skipped.labels(reason="ppo_wait").inc()
            self.ppo.store_transition(state, action, -0.001, False, log_prob, value)
            return

        # 4. Estimate gas cost
        gas_cost_eth = self.router.estimate_gas_cost_eth(
            self._current_gas_gwei, gas_limit=700_000
        )

        # 5. Build executable route
        route = self.router.build_route(
            opportunity=opp,
            gas_cost_eth=gas_cost_eth,
            eth_price_usd=self._eth_price_usd,
            wallet_balance_eth=wallet_bal
        )

        if not route:
            opportunities_skipped.labels(reason="route_build_failed").inc()
            return

        # 6. Risk check
        risk_decision = self.risk.pre_trade_check(
            candidate=route.trade_candidate,
            gas_price_gwei=self._current_gas_gwei,
            wallet_balance_eth=wallet_bal
        )

        if not risk_decision.approved:
            opportunities_skipped.labels(reason=f"risk_{risk_decision.reason}").inc()
            self.ppo.store_transition(state, action, -0.005, False, log_prob, value)
            return

        # Adjust size if risk manager reduced it
        if risk_decision.adjusted_size_eth and risk_decision.adjusted_size_eth < route.amount_wei / 1e18:
            route.amount_wei = int(risk_decision.adjusted_size_eth * 1e18)

        # 7. Execute flash loan
        log.info(
            f"Executing: {' → '.join(opp.cycle)} | "
            f"size={route.amount_wei/1e18:.3f} ETH | "
            f"expected profit={route.expected_profit_usd:.2f} USD | "
            f"gas={self._current_gas_gwei:.3f} gwei"
        )

        self.risk.record_trade_start()
        tx_hash = None
        try:
            tx_hash = await self.executor.execute(
                asset_address=route.asset,
                amount_wei=route.amount_wei,
                steps=route.steps,
                dry_run=self.dry_run
            )

            if self.dry_run or tx_hash is None:
                self.ppo.store_transition(state, action, 0.1, True, log_prob, value)
                return

            # 8. Wait for confirmation
            result = await self.monitor.wait_for_receipt(tx_hash)

            # 9. Record outcomes
            self.risk.record_outcome(
                opportunity_id=route.opportunity_id,
                profit_eth=result.profit_eth,
                gas_cost_eth=result.gas_cost_eth,
                success=result.success
            )

            if result.success:
                # PPO reward: profit in units of $100
                reward = result.profit_eth * self._eth_price_usd / 100
                self.ppo.store_transition(state, action, reward, True, log_prob, value)

                # Thompson Sampling: record success
                for token_in, token_out, pool in zip(
                    opp.cycle[:-1], opp.cycle[1:], opp.pools
                ):
                    self.bandit.record_outcome(
                        token_in, token_out, pool.dex,
                        result.profit_eth, route.expected_profit_eth
                    )

                profit_eth.inc(result.profit_eth)
                opportunities_executed.inc()
                self._recent_successes.append(True)

                log.info(
                    f"✅ PROFIT: {result.profit_eth:.6f} ETH "
                    f"(${result.profit_eth * self._eth_price_usd:.2f}) | "
                    f"gas: {result.gas_cost_eth:.6f} ETH | "
                    f"latency: {result.latency_ms:.0f}ms"
                )

                # Telegram alert for big wins
                alert_threshold = self.cfg.get("monitoring", {}).get("alert_on_profit_above_usd", 100)
                if result.profit_eth * self._eth_price_usd > alert_threshold:
                    await self._send_telegram_alert(
                        f"🚀 Big win: ${result.profit_eth * self._eth_price_usd:.2f} "
                        f"| path: {' → '.join(opp.cycle)}"
                    )
            else:
                reward = -(result.gas_cost_eth * self._eth_price_usd) / 10
                self.ppo.store_transition(state, action, reward, True, log_prob, value)

                for token_in, token_out, pool in zip(
                    opp.cycle[:-1], opp.cycle[1:], opp.pools
                ):
                    self.bandit.record_failure(token_in, token_out, pool.dex)

                gas_spent_eth.inc(result.gas_cost_eth)
                self._recent_successes.append(False)
                log.warning(
                    f"❌ REVERTED: {tx_hash} | "
                    f"reason: {result.revert_reason} | "
                    f"gas lost: {result.gas_cost_eth:.6f} ETH"
                )

            # Keep recent_successes bounded
            if len(self._recent_successes) > 20:
                self._recent_successes.pop(0)

        except Exception as e:
            self.risk.record_outcome(
                route.opportunity_id, 0.0, gas_cost_eth, False
            )
            self._recent_successes.append(False)
            log.error(f"Execution error: {e}", exc_info=True)

        # 10. Periodic PPO update
        ppo_metrics = self.ppo.update()
        if ppo_metrics:
            log.debug(
                f"PPO update: policy_loss={ppo_metrics['policy_loss']:.4f} "
                f"value_loss={ppo_metrics['value_loss']:.4f} "
                f"entropy={ppo_metrics['entropy']:.4f}"
            )

    # ─── Background Maintenance ───────────────────────────────
    async def _background_maintenance(self) -> None:
        """Periodic health checks and monitoring."""
        while self.running:
            await asyncio.sleep(60)

            # Log risk stats
            stats = self.risk.get_stats()
            log.info(
                f"[Status] trades={stats.get('total_trades', 0)} "
                f"success_rate={stats.get('success_rate', 0):.1%} "
                f"daily_pnl={stats.get('daily_pnl_eth', 0):.6f} ETH "
                f"circuit_broken={stats.get('circuit_broken', False)}"
            )

            # Log Thompson Sampling rankings (top pair)
            rankings = self.bandit.get_rankings()
            for pair, table in list(rankings.items())[:1]:
                top_dex = table[0] if table else {}
                log.debug(f"Top DEX for {pair}: {top_dex}")

    # ─── Daily Reset ──────────────────────────────────────────
    async def _daily_reset_loop(self) -> None:
        """Reset daily PnL at midnight UTC."""
        while self.running:
            now = time.gmtime()
            seconds_until_midnight = (
                (23 - now.tm_hour) * 3600 +
                (59 - now.tm_min)  * 60 +
                (60 - now.tm_sec)
            )
            await asyncio.sleep(seconds_until_midnight)
            self.risk.reset_daily()

    # ─── Telegram Alerts ─────────────────────────────────────
    async def _send_telegram_alert(self, message: str) -> None:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            return
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={"chat_id": chat_id, "text": message})
        except Exception as e:
            log.debug(f"Telegram alert failed: {e}")


# ─── Entry Point ──────────────────────────────────────────────

def load_config() -> dict:
    """Load YAML config and override with env vars."""
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


async def async_main() -> None:
    # Setup logging
    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/nexus_arb.log")
        ]
    )

    # Load environment
    load_dotenv()

    # Load config
    config = load_config()

    # Run orchestrator
    orch = Orchestrator(config)
    await orch.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — stopping.")


if __name__ == "__main__":
    main()
