// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

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

    struct BatchSwapStep {
        bytes32 poolId;
        uint256 assetInIndex;
        uint256 assetOutIndex;
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
        SingleSwap calldata singleSwap,
        FundManagement calldata funds,
        uint256 limit,
        uint256 deadline
    ) external payable returns (uint256 amountCalculated);

    function batchSwap(
        SwapKind kind,
        BatchSwapStep[] calldata swaps,
        address[] calldata assets,
        FundManagement calldata funds,
        int256[] calldata limits,
        uint256 deadline
    ) external payable returns (int256[] memory assetDeltas);

    function queryBatchSwap(
        SwapKind kind,
        BatchSwapStep[] calldata swaps,
        address[] calldata assets,
        FundManagement calldata funds
    ) external returns (int256[] memory assetDeltas);

    function getPoolTokens(bytes32 poolId)
        external
        view
        returns (
            address[] memory tokens,
            uint256[] memory balances,
            uint256 lastChangeBlock
        );
}
