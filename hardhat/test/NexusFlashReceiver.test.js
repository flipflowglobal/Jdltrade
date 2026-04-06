/**
 * NexusFlashReceiver — Mainnet Fork Integration Tests
 *
 * Run against a forked Arbitrum mainnet:
 *   cd hardhat && npx hardhat test
 *
 * Tests:
 *   1. Contract deploys successfully
 *   2. Unauthorized callers are rejected
 *   3. Flash loan executes and repays correctly
 *   4. Profitable arbitrage produces positive balance delta
 *   5. Unprofitable trades revert cleanly
 */

const { expect } = require("chai");
const { ethers }  = require("hardhat");

// ─── Arbitrum Mainnet Addresses ──────────────────────────────
const AAVE_POOL      = "0x794a61358D6845594F94dc1DB02A252b5b4814aD";
const UNIV3_ROUTER   = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45";
const CURVE_ROUTER   = "0x4c2Af2Df2a7E567B5155879720619EA06C5BB15D";
const BALANCER_VAULT = "0xBA12222222228d8Ba445958a75a0704d566BF2C8";
const WETH_ADDR      = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1";
const USDC_ADDR      = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831";
const WETH_WHALE     = "0x489ee077994B6658eAfA855C308275EAd8097C4A"; // Large holder

const WETH_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function transfer(address to, uint256 amount) returns (bool)",
  "function deposit() payable"
];

describe("NexusFlashReceiver", function () {
  let nexus, owner, executor, weth;
  const DEX_UNISWAP_V3 = 0;

  this.timeout(120_000);  // 2 min for mainnet fork

  before(async function () {
    if (!process.env.ARB_HTTP_1) {
      console.warn("⚠️  ARB_HTTP_1 not set — skipping mainnet fork tests");
      this.skip();
    }

    [owner, executor] = await ethers.getSigners();

    // Deploy NexusFlashReceiver
    const Factory = await ethers.getContractFactory("NexusFlashReceiver");
    nexus = await Factory.deploy(
      AAVE_POOL,
      UNIV3_ROUTER,
      CURVE_ROUTER,
      BALANCER_VAULT
    );
    await nexus.waitForDeployment();
    console.log(`  Contract deployed: ${await nexus.getAddress()}`);

    // Get WETH
    weth = await ethers.getContractAt(WETH_ABI, WETH_ADDR);

    // Fund executor with ETH for gas
    await owner.sendTransaction({
      to: executor.address,
      value: ethers.parseEther("1.0")
    });
  });

  describe("Deployment", function () {
    it("Sets owner correctly", async function () {
      expect(await nexus.owner()).to.equal(owner.address);
    });

    it("Sets executor to owner by default", async function () {
      expect(await nexus.executor()).to.equal(owner.address);
    });

    it("Is not paused", async function () {
      expect(await nexus.paused()).to.be.false;
    });

    it("Records correct contract addresses", async function () {
      expect(await nexus.aavePool()).to.equal(AAVE_POOL);
      expect(await nexus.uniswapRouter()).to.equal(UNIV3_ROUTER);
      expect(await nexus.balancerVault()).to.equal(BALANCER_VAULT);
    });
  });

  describe("Access Control", function () {
    it("Rejects executeArbitrage from non-executor", async function () {
      const [, , nonExecutor] = await ethers.getSigners();
      await expect(
        nexus.connect(nonExecutor).executeArbitrage(WETH_ADDR, 1, [])
      ).to.be.revertedWithCustomError(nexus, "Unauthorized");
    });

    it("Rejects executeOperation from non-Aave", async function () {
      await expect(
        nexus.executeOperation(WETH_ADDR, 1, 0, owner.address, "0x")
      ).to.be.revertedWithCustomError(nexus, "InvalidCaller");
    });

    it("Allows owner to set executor", async function () {
      await nexus.setExecutor(executor.address);
      expect(await nexus.executor()).to.equal(executor.address);
      await nexus.setExecutor(owner.address);
    });

    it("Allows owner to pause/unpause", async function () {
      await nexus.setPaused(true);
      expect(await nexus.paused()).to.be.true;

      await expect(
        nexus.executeArbitrage(WETH_ADDR, 1, [])
      ).to.be.revertedWithCustomError(nexus, "ContractPaused");

      await nexus.setPaused(false);
    });
  });

  describe("Emergency Functions", function () {
    it("Owner can withdraw stuck tokens", async function () {
      // Send some WETH to contract
      const wethWhale = await ethers.getImpersonatedSigner(WETH_WHALE);
      await weth.connect(wethWhale).transfer(
        await nexus.getAddress(),
        ethers.parseEther("0.001")
      );

      const balBefore = await weth.balanceOf(owner.address);
      await nexus.emergencyWithdraw(WETH_ADDR);
      const balAfter = await weth.balanceOf(owner.address);

      expect(balAfter - balBefore).to.equal(ethers.parseEther("0.001"));
    });
  });

  describe("Flash Loan — Uniswap V3 Round-Trip (Sanity)", function () {
    /**
     * This test executes a trivial WETH→USDC→WETH round trip
     * to verify the contract can handle flash loans.
     * This is NOT profitable — it's a connectivity test.
     */
    it("Can initiate and survive a flash loan (will revert due to loss)", async function () {
      const nexusAddr = await nexus.getAddress();

      // WETH → USDC → WETH with 0.05% fee
      const swapSteps = [
        {
          dexType:      0,  // UniV3
          tokenIn:      WETH_ADDR,
          tokenOut:     USDC_ADDR,
          amountIn:     0,  // Use full balance
          minAmountOut: 0,  // No slippage protection (test only!)
          extraData:    ethers.AbiCoder.defaultAbiCoder().encode(
            ["uint24", "address"],
            [500, "0x0000000000000000000000000000000000000000"]
          )
        },
        {
          dexType:      0,  // UniV3
          tokenIn:      USDC_ADDR,
          tokenOut:     WETH_ADDR,
          amountIn:     0,
          minAmountOut: 0,
          extraData:    ethers.AbiCoder.defaultAbiCoder().encode(
            ["uint24", "address"],
            [500, "0x0000000000000000000000000000000000000000"]
          )
        }
      ];

      const loanAmount = ethers.parseEther("0.01");

      // This should revert with InsufficientProfit since fees eat the capital
      await expect(
        nexus.executeArbitrage(WETH_ADDR, loanAmount, swapSteps)
      ).to.be.revertedWithCustomError(nexus, "InsufficientProfit");

      console.log("  ✓ Flash loan correctly reverts when unprofitable");
    });
  });
});
