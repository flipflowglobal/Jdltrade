// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@aave/core-v3/contracts/flashloan/base/FlashLoanSimpleReceiverBase.sol";
import "@aave/core-v3/contracts/interfaces/IPoolAddressesProvider.sol";
import "@aave/core-v3/contracts/interfaces/IPool.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns (uint[] memory);
}

interface IUniswapV3Router {
    struct ExactInputParams {
        bytes   path;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

interface ICurvePool {
    function exchange(int128 i, int128 j, uint256 dx, uint256 min_dy) external returns (uint256);
}

interface IBalancerVault {
    enum SwapKind { GIVEN_IN, GIVEN_OUT }
    struct SingleSwap {
        bytes32 poolId;
        SwapKind kind;
        address assetIn;
        address assetOut;
        uint256 amount;
        bytes userData;
    }
    struct FundManagement {
        address sender;
        bool fromInternalBalance;
        address payable recipient;
        bool toInternalBalance;
    }
    function swap(
        SingleSwap memory singleSwap,
        FundManagement memory funds,
        uint256 limit,
        uint256 deadline
    ) external returns (uint256 amountCalculated);
}

contract SovereignArb is FlashLoanSimpleReceiverBase, ReentrancyGuard, Ownable {
    using SafeERC20 for IERC20;

    // ═══════════════════════════════════
    // STATE
    // ═══════════════════════════════════
    address public profitWallet;
    uint256 public totalProfit;
    uint256 public totalTrades;
    uint256 public minProfitBps = 30; // 0.3% minimum

    // DEX Routers — Mainnet
    address constant UNI_V2    = 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D;
    address constant UNI_V3    = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address constant SUSHI     = 0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F;
    address constant BAL_VAULT = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;

    // Aave V3 Mainnet Pool Provider
    address constant AAVE_PROVIDER = 0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e;

    // ═══════════════════════════════════
    // STRUCTS
    // ═══════════════════════════════════
    struct ArbParams {
        address[] path;
        address[] routers;
        uint8[]   dexTypes;   // 0=UniV2/Sushi, 1=UniV3, 2=Curve, 3=Balancer
        bytes[]   extraData;  // encoded pool ids, fees, curve indices
        uint256   minProfit;
    }

    // ═══════════════════════════════════
    // EVENTS
    // ═══════════════════════════════════
    event ArbExecuted(
        address indexed token,
        uint256 borrowed,
        uint256 profit,
        uint256 timestamp
    );
    event ProfitWithdrawn(address indexed to, uint256 amount);

    // ═══════════════════════════════════
    // CONSTRUCTOR
    // ═══════════════════════════════════
    constructor(address _profitWallet)
        FlashLoanSimpleReceiverBase(IPoolAddressesProvider(AAVE_PROVIDER))
        Ownable(msg.sender)
    {
        require(_profitWallet != address(0), "Zero wallet");
        profitWallet = _profitWallet;
    }

    // ═══════════════════════════════════
    // FLASH LOAN ENTRY
    // ═══════════════════════════════════
    function executeArb(
        address token,
        uint256 amount,
        ArbParams calldata params
    ) external onlyOwner nonReentrant {
        require(params.path.length >= 2, "Path too short");
        require(params.routers.length == params.path.length - 1, "Router/path mismatch");
        bytes memory data = abi.encode(params);
        POOL.flashLoanSimple(address(this), token, amount, data, 0);
    }

    // ═══════════════════════════════════
    // AAVE CALLBACK — CORE LOGIC
    // ═══════════════════════════════════
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(POOL), "Unauthorized caller");
        require(initiator == address(this), "Unauthorized initiator");

        ArbParams memory arb = abi.decode(params, (ArbParams));
        uint256 totalDebt = amount + premium;

        // Execute multi-hop swap chain
        uint256 amountIn = amount;
        for (uint256 i = 0; i < arb.routers.length; i++) {
            amountIn = _executeSwap(
                arb.dexTypes[i],
                arb.routers[i],
                arb.path[i],
                arb.path[i + 1],
                amountIn,
                arb.extraData[i]
            );
        }

        uint256 balAfter = IERC20(asset).balanceOf(address(this));
        require(balAfter >= totalDebt, "Unprofitable: cannot repay");

        uint256 profit = balAfter - totalDebt;
        require(profit >= arb.minProfit, "Below min profit threshold");

        // Repay Aave — approve exact debt amount
        IERC20(asset).forceApprove(address(POOL), totalDebt);

        // Route profit to sovereign wallet
        if (profit > 0) {
            IERC20(asset).safeTransfer(profitWallet, profit);
            totalProfit += profit;
            totalTrades++;
            emit ArbExecuted(asset, amount, profit, block.timestamp);
        }

        return true;
    }

    // ═══════════════════════════════════
    // INTERNAL SWAP ROUTER
    // ═══════════════════════════════════
    function _executeSwap(
        uint8   dexType,
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        bytes   memory extraData
    ) internal returns (uint256 amountOut) {
        // Reset any stale approval
        IERC20(tokenIn).forceApprove(router, 0);
        IERC20(tokenIn).forceApprove(router, amountIn);

        if (dexType == 0) {
            // UniswapV2 / Sushiswap
            address[] memory path = new address[](2);
            path[0] = tokenIn;
            path[1] = tokenOut;
            uint[] memory amounts = IUniswapV2Router(router).swapExactTokensForTokens(
                amountIn,
                0,
                path,
                address(this),
                block.timestamp + 60
            );
            amountOut = amounts[amounts.length - 1];

        } else if (dexType == 1) {
            // UniswapV3
            uint24 fee = abi.decode(extraData, (uint24));
            bytes memory path3 = abi.encodePacked(tokenIn, fee, tokenOut);
            IUniswapV3Router.ExactInputParams memory p = IUniswapV3Router.ExactInputParams({
                path:             path3,
                recipient:        address(this),
                deadline:         block.timestamp + 60,
                amountIn:         amountIn,
                amountOutMinimum: 0
            });
            amountOut = IUniswapV3Router(router).exactInput(p);

        } else if (dexType == 2) {
            // Curve
            (int128 i, int128 j) = abi.decode(extraData, (int128, int128));
            amountOut = ICurvePool(router).exchange(i, j, amountIn, 0);

        } else if (dexType == 3) {
            // Balancer
            bytes32 poolId = abi.decode(extraData, (bytes32));
            IBalancerVault.SingleSwap memory singleSwap = IBalancerVault.SingleSwap({
                poolId:   poolId,
                kind:     IBalancerVault.SwapKind.GIVEN_IN,
                assetIn:  tokenIn,
                assetOut: tokenOut,
                amount:   amountIn,
                userData: ""
            });
            IBalancerVault.FundManagement memory funds = IBalancerVault.FundManagement({
                sender:             address(this),
                fromInternalBalance: false,
                recipient:          payable(address(this)),
                toInternalBalance:  false
            });
            amountOut = IBalancerVault(BAL_VAULT).swap(singleSwap, funds, 0, block.timestamp + 60);
        } else {
            revert("Unknown dex type");
        }
    }

    // ═══════════════════════════════════
    // ADMIN
    // ═══════════════════════════════════
    function setProfitWallet(address _wallet) external onlyOwner {
        require(_wallet != address(0), "Zero wallet");
        profitWallet = _wallet;
    }

    function setMinProfitBps(uint256 _bps) external onlyOwner {
        require(_bps <= 1000, "Max 10%");
        minProfitBps = _bps;
    }

    function withdrawToken(address token, uint256 amount) external onlyOwner {
        IERC20(token).safeTransfer(profitWallet, amount);
        emit ProfitWithdrawn(profitWallet, amount);
    }

    function withdrawETH() external onlyOwner {
        uint256 bal = address(this).balance;
        require(bal > 0, "No ETH");
        payable(profitWallet).transfer(bal);
    }

    receive() external payable {}
}
