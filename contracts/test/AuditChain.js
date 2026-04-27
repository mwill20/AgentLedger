const { expect } = require("chai");
const { ethers, upgrades } = require("hardhat");

describe("AuditChain", function () {
  async function deployFixture() {
    const [admin, anchor] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("AuditChain");
    const contract = await upgrades.deployProxy(Factory, [admin.address], {
      kind: "uups"
    });
    await contract.waitForDeployment();
    await contract.grantRole(await contract.ANCHOR_ROLE(), anchor.address);
    return { contract, anchor };
  }

  it("commits one audit batch root", async function () {
    const { contract, anchor } = await deployFixture();
    const batchId = ethers.keccak256(ethers.toUtf8Bytes("batch-1"));
    const root = ethers.keccak256(ethers.toUtf8Bytes("root"));

    await expect(contract.connect(anchor).commitBatch(batchId, root, 4)).to.emit(
      contract,
      "BatchAnchorCommitted"
    );
  });
});
