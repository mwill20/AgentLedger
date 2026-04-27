const { expect } = require("chai");
const { ethers, upgrades } = require("hardhat");

describe("AttestationLedger", function () {
  async function deployFixture() {
    const [admin, auditor] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("AttestationLedger");
    const contract = await upgrades.deployProxy(Factory, [admin.address], {
      kind: "uups"
    });
    await contract.waitForDeployment();
    await contract.grantRole(await contract.AUDITOR_ROLE(), auditor.address);
    return { contract, admin, auditor };
  }

  it("records attestations from registered auditors", async function () {
    const { contract, auditor } = await deployFixture();
    const serviceId = ethers.keccak256(ethers.toUtf8Bytes("skybridge.example"));
    const evidenceHash = ethers.keccak256(ethers.toUtf8Bytes("evidence"));

    await expect(
      contract
        .connect(auditor)
        .recordAttestation(serviceId, "travel.*", "SOC2-2026", 0, evidenceHash)
    ).to.emit(contract, "AttestationRecorded");
  });

  it("marks services as revoked", async function () {
    const { contract, auditor } = await deployFixture();
    const serviceId = ethers.keccak256(ethers.toUtf8Bytes("skybridge.example"));
    const evidenceHash = ethers.keccak256(ethers.toUtf8Bytes("incident"));

    await contract
      .connect(auditor)
      .recordRevocation(serviceId, "security_incident", evidenceHash);

    expect(await contract.isGloballyRevoked(serviceId)).to.equal(true);
  });

  it("stores the latest manifest hash", async function () {
    const { contract, admin } = await deployFixture();
    const serviceId = ethers.keccak256(ethers.toUtf8Bytes("skybridge.example"));
    const manifestHash = ethers.keccak256(ethers.toUtf8Bytes("manifest-v2"));

    await contract.connect(admin).recordVersion(serviceId, manifestHash);

    expect(await contract.latestManifestHash(serviceId)).to.equal(manifestHash);
  });
});
