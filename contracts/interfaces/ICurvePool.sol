// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ICurvePool {
    function exchange(
        int128 i,
        int128 j,
        uint256 dx,
        uint256 min_dy
    ) external returns (uint256);

    function exchange_underlying(
        int128 i,
        int128 j,
        uint256 dx,
        uint256 min_dy
    ) external returns (uint256);

    function get_dy(
        int128 i,
        int128 j,
        uint256 dx
    ) external view returns (uint256);

    function get_dy_underlying(
        int128 i,
        int128 j,
        uint256 dx
    ) external view returns (uint256);

    function coins(uint256 i) external view returns (address);
    function balances(uint256 i) external view returns (uint256);
    function A() external view returns (uint256);
    function fee() external view returns (uint256);
}

interface ICurveRouter {
    function exchange(
        address[11] calldata _route,
        uint256[5][5] calldata _swap_params,
        uint256 _amount,
        uint256 _expected,
        address[5] calldata _pools
    ) external payable returns (uint256);

    function get_dy(
        address[11] calldata _route,
        uint256[5][5] calldata _swap_params,
        uint256 _amount,
        address[5] calldata _pools
    ) external view returns (uint256);
}
