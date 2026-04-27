require("dotenv").config();
require("@nomicfoundation/hardhat-toolbox");
require("@openzeppelin/hardhat-upgrades");

const accounts = process.env.CHAIN_SIGNER_PRIVATE_KEY
  ? [process.env.CHAIN_SIGNER_PRIVATE_KEY]
  : [];

const polygonAmoyUrl = process.env.AMOY_RPC_URL || "";
const polygonUrl = process.env.POLYGON_RPC_URL || process.env.WEB3_PROVIDER_URL || "";

module.exports = {
  solidity: {
    version: "0.8.23",
    settings: {
      optimizer: {
        enabled: true,
        runs: 200
      }
    }
  },
  paths: {
    sources: "./contracts",
    tests: "./contracts/test",
    cache: "./contracts/cache",
    artifacts: "./contracts/artifacts"
  },
  networks: {
    hardhat: {},
    polygonAmoy: {
      url: polygonAmoyUrl,
      accounts,
      chainId: 80002
    },
    polygon: {
      url: polygonUrl,
      accounts,
      chainId: 137
    }
  }
};
