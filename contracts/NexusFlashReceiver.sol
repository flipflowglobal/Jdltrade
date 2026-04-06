// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  NexusFlashReceiver
 * @notice Production-grade Aave V3 flash loan receiver for multi-DEX arbitrage
 *         on Arbitrum. Executes atomic swap sequences across Uniswap V3,
 *         Curve Finance, and Balancer V2.
 *
 * Architecture:
 *   1. Python bot detects arbitrage opportunity off-chain
 *   2. Bot encodes trade route and calls executeArbitrage()
 *   3. Contract borrows asset via Aave V3 flashLoanSimple
 *   4. Aave calls executeOperation() with borrowed funds
 *   5. Contract executes DEX swaps in sequence
 *   6. Contract repays loan + premium, keeps profit
 *   7. Profit returned to owner wallet
 */

import "./interfaces/IAaveV3Pool.sol";
import "./interfaces/IFlashLoanSimpleReceiver.sol";
import "./interfaces/IUniswapV3Router.sol";
import "./interfaces/ICurvePool.sol";
import "./interfaces/IBalancerVault.sol";

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
}

contract NexusFlashReceiver is IFlashLoanSimpleReceiver {

    // ─── Constants ───────────────────────────────────────────────
    uint8 constant DEX_UNISWAP_V3  = 0;
    uint8 constant DEX_CURVE       = 1;
    uint8 constant DEX_BALANCER    = 2;
    uint8 constant DEX_CAMELOT_V3  = 3;

    // ─── Immutable Addresses ──────────────────────────────────────
    IAaveV3Pool     public immutable aavePool;
    IUniswapV3SwapRouter public immutable uniswapRouter;
    ICurveRouter    public immutable curveRouter;
    IBalancerVault  public immutable balancerVault;

    // ─── State ────────────────────────────────────────────────────
    address public owner;
    address public executor;        // Python bot's hot wallet
    bool    public paused;
    uint256 public totalProfitWei;  // Lifetime profit accumulator

    // ─── Events ───────────────────────────────────────────────────
    event ArbitrageExecuted(
        address indexed asset,
        uint256 borrowed,
        uint256 profit,
        uint256 gasUsed
    );
    event Paused(bool state);
    event ExecutorUpdated(address newExecutor);
    event EmergencyWithdraw(address token, uint256 amount);

    // ─── Errors ───────────────────────────────────────────────────
    error Unauthorized();
    error ContractPaused();
    error InvalidCaller();
    error InsufficientProfit(uint256 repayAmount, uint256 balance);
    error SwapFailed(uint8 dexType, uint256 step);
    error ZeroAmount();

    // ─── Structs ──────────────────────────────────────────────────
    /// @param dexType    DEX identifier constant (0=UniV3, 1=Curve, 2=Balancer, 3=Camelot)
    /// @param tokenIn    Input token address
    /// @param tokenOut   Output token address
    /// @param amountIn   Exact input amount (0 = use full contract balance)
    /// @param minAmountOut Minimum acceptable output (slippage protection)
    /// @param extraData  ABI-encoded DEX-specific parameters
    struct SwapStep {
        uint8   dexType;
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint256 minAmountOut;
        bytes   extraData;
    }

    // ─── Constructor ──────────────────────────────────────────────
    constructor(
        address _aavePool,
        address _uniswapRouter,
        address _curveRouter,
        address _balancerVault
    ) {
        owner         = msg.sender;
        executor      = msg.sender;
        aavePool      = IAaveV3Pool(_aavePool);
        uniswapRouter = IUniswapV3SwapRouter(_uniswapRouter);
        curveRouter   = ICurveRouter(_curveRouter);
        balancerVault = IBalancerVault(_balancerVault);
    }

    // ─── Modifiers ────────────────────────────────────────────────
    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier onlyExecutor() {
        if (msg.sender != executor && msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier notPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    // ─── Entry Point ──────────────────────────────────────────────
    /**
     * @notice Initiates a flash loan arbitrage trade.
     * @param asset     Token to borrow (e.g., WETH)
     * @param amount    Amount to borrow in wei
     * @param steps     Ordered array of swap steps to execute
     */
    function executeArbitrage(
        address asset,
        uint256 amount,
        SwapStep[] calldata steps
    ) external onlyExecutor notPaused {
        if (amount == 0) revert ZeroAmount();

        bytes memory params = abi.encode(steps);

        aavePool.flashLoanSimple(
            address(this),
            asset,
            amount,
            params,
            0   // referral code
        );
    }

    // ─── Aave V3 Flash Loan Callback ─────────────────────────────
    /**
     * @notice Called by Aave after flash loan is disbursed.
     *         Must repay amount + premium before returning.
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        // Security: only Aave pool can call this
        if (msg.sender != address(aavePool)) revert InvalidCaller();
        if (initiator != address(this))      revert InvalidCaller();

        uint256 gasStart = gasleft();

        // Decode trade route
        SwapStep[] memory steps = abi.decode(params, (SwapStep[]));

        // Execute each swap in sequence
        for (uint256 i = 0; i < steps.length; i++) {
            _executeSwap(steps[i]);
        }

        // Verify profitability: ensure we can repay loan + premium
        uint256 repayAmount = amount + premium;
        uint256 balance     = IERC20(asset).balanceOf(address(this));

        if (balance < repayAmount) {
            revert InsufficientProfit(repayAmount, balance);
        }

        uint256 profit = balance - repayAmount;

        // Approve Aave to pull repayment
        _safeApprove(asset, address(aavePool), repayAmount);

        // Transfer profit to owner
        if (profit > 0) {
            IERC20(asset).transfer(owner, profit);
            totalProfitWei += profit;
        }

        emit ArbitrageExecuted(asset, amount, profit, gasStart - gasleft());
        return true;
    }

    // ─── Internal Swap Dispatcher ─────────────────────────────────
    function _executeSwap(SwapStep memory step) internal {
        uint256 amountIn = step.amountIn == 0
            ? IERC20(step.tokenIn).balanceOf(address(this))
            : step.amountIn;

        if (step.dexType == DEX_UNISWAP_V3 || step.dexType == DEX_CAMELOT_V3) {
            _swapUniswapV3(step, amountIn);
        } else if (step.dexType == DEX_CURVE) {
            _swapCurve(step, amountIn);
        } else if (step.dexType == DEX_BALANCER) {
            _swapBalancer(step, amountIn);
        }
    }

    // ─── Uniswap V3 Swap ─────────────────────────────────────────
    function _swapUniswapV3(SwapStep memory step, uint256 amountIn) internal {
        // Decode: (uint24 fee, address routerOverride)
        (uint24 fee, address routerAddr) = abi.decode(step.extraData, (uint24, address));

        address router = routerAddr != address(0) ? routerAddr : address(uniswapRouter);

        _safeApprove(step.tokenIn, router, amountIn);

        IUniswapV3SwapRouter(router).exactInputSingle(
            IUniswapV3SwapRouter.ExactInputSingleParams({
                tokenIn:           step.tokenIn,
                tokenOut:          step.tokenOut,
                fee:               fee,
                recipient:         address(this),
                amountIn:          amountIn,
                amountOutMinimum:  step.minAmountOut,
                sqrtPriceLimitX96: 0
            })
        );
    }

    // ─── Curve Swap ───────────────────────────────────────────────
    function _swapCurve(SwapStep memory step, uint256 amountIn) internal {
        // Decode: (address[11] route, uint256[5][5] swapParams, address[5] pools)
        (
            address[11] memory route,
            uint256[5][5] memory swapParams,
            address[5] memory pools
        ) = abi.decode(step.extraData, (address[11], uint256[5][5], address[5]));

        _safeApprove(step.tokenIn, address(curveRouter), amountIn);

        curveRouter.exchange(
            route,
            swapParams,
            amountIn,
            step.minAmountOut,
            pools
        );
    }

    // ─── Balancer V2 Swap ────────────────────────────────────────
    function _swapBalancer(SwapStep memory step, uint256 amountIn) internal {
        // Decode: (bytes32 poolId)
        bytes32 poolId = abi.decode(step.extraData, (bytes32));

        _safeApprove(step.tokenIn, address(balancerVault), amountIn);

        balancerVault.swap(
            IBalancerVault.SingleSwap({
                poolId:   poolId,
                kind:     IBalancerVault.SwapKind.GIVEN_IN,
                assetIn:  step.tokenIn,
                assetOut: step.tokenOut,
                amount:   amountIn,
                userData: ""
            }),
            IBalancerVault.FundManagement({
                sender:            address(this),
                fromInternalBalance: false,
                recipient:         payable(address(this)),
                toInternalBalance: false
            }),
            step.minAmountOut,
            block.timestamp
        );
    }

    // ─── Admin Functions ──────────────────────────────────────────
    function setExecutor(address _executor) external onlyOwner {
        executor = _executor;
        emit ExecutorUpdated(_executor);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit Paused(_paused);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        owner = newOwner;
    }

    /// @notice Emergency sweep of any token stuck in contract
    function emergencyWithdraw(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance > 0) {
            IERC20(token).transfer(owner, balance);
            emit EmergencyWithdraw(token, balance);
        }
    }

    /// @notice Emergency ETH withdrawal
    function emergencyWithdrawETH() external onlyOwner {
        uint256 balance = address(this).balance;
        if (balance > 0) {
            payable(owner).transfer(balance);
        }
    }

    // ─── Internal Helpers ────────────────────────────────────────
    function _safeApprove(address token, address spender, uint256 amount) internal {
        // Reset to 0 first (some tokens require this)
        IERC20(token).approve(spender, 0);
        IERC20(token).approve(spender, amount);
    }

    receive() external payable {}
}
