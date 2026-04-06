require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config({ path: "../.env" });

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.20",
    settings: {
      optimizer: {
        enabled: true,
        runs: 200
      },
      evmVersion: "paris"
    }
  },

  networks: {
    // Arbitrum mainnet fork for testing
    hardhat: {
      forking: {
        url: process.env.ARB_HTTP_1 || "https://arb1.arbitrum.io/rpc",
        blockNumber: undefined  // Latest block
      },
      chainId: 42161
    },

    // Real Arbitrum mainnet
    arbitrum: {
      url: process.env.ARB_HTTP_1 || "https://arb1.arbitrum.io/rpc",
      accounts: process.env.PRIVATE_KEY ? [process.env.PRIVATE_KEY] : [],
      chainId: 42161,
      gasPrice: "auto"
    }
  },

  // Arbiscan verification
  etherscan: {
    apiKey: {
      arbitrumOne: process.env.ARBISCAN_API_KEY || ""
    }
  },

  paths: {
    sources: "../contracts",
    artifacts: "./artifacts",
    tests: "./test"
  }
};
