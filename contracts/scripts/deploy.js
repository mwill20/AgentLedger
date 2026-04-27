const { ethers, upgrades } = require("hardhat");

async function deploymentMetadata(contract) {
  const deploymentTx = contract.deploymentTransaction();
  if (!deploymentTx) {
    return {
      txHash: null,
      blockNumber: null
    };
  }

  const receipt = await deploymentTx.wait();
  return {
    txHash: receipt.hash,
    blockNumber: receipt.blockNumber
  };
}

async function main() {
  const [deployer] = await ethers.getSigners();

  const AttestationLedger = await ethers.getContractFactory("AttestationLedger");
  const attestationLedger = await upgrades.deployProxy(
    AttestationLedger,
    [deployer.address],
    { kind: "uups" }
  );
  await attestationLedger.waitForDeployment();

  const AuditChain = await ethers.getContractFactory("AuditChain");
  const auditChain = await upgrades.deployProxy(
    AuditChain,
    [deployer.address],
    { kind: "uups" }
  );
  await auditChain.waitForDeployment();

  const attestationLedgerDeployment = await deploymentMetadata(attestationLedger);
  const auditChainDeployment = await deploymentMetadata(auditChain);

  console.log(
    JSON.stringify(
      {
        deployer: deployer.address,
        attestationLedger: {
          address: await attestationLedger.getAddress(),
          deployment: attestationLedgerDeployment
        },
        auditChain: {
          address: await auditChain.getAddress(),
          deployment: auditChainDeployment
        }
      },
      null,
      2
    )
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
