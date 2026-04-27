const { ethers } = require("hardhat");

function usage() {
  console.error(
    "Usage: npx hardhat run contracts/scripts/grant_roles.js --network <network> (with GRANT_ATTESTATION_LEDGER_ADDRESS, GRANT_AUDIT_CHAIN_ADDRESS, and GRANT_SIGNER_ADDRESS set) or node contracts/scripts/grant_roles.js -- <attestationLedger> <auditChain> <signerAddress>"
  );
}

function parseArgs(argv) {
  if (
    process.env.GRANT_ATTESTATION_LEDGER_ADDRESS &&
    process.env.GRANT_AUDIT_CHAIN_ADDRESS &&
    process.env.GRANT_SIGNER_ADDRESS
  ) {
    return {
      attestationLedgerAddress: process.env.GRANT_ATTESTATION_LEDGER_ADDRESS,
      auditChainAddress: process.env.GRANT_AUDIT_CHAIN_ADDRESS,
      signerAddress: process.env.GRANT_SIGNER_ADDRESS
    };
  }

  const args = argv.slice(2);
  const separatorIndex = args.indexOf("--");
  const positional = separatorIndex >= 0 ? args.slice(separatorIndex + 1) : [];

  if (positional.length !== 3) {
    usage();
    process.exit(1);
  }

  const [attestationLedgerAddress, auditChainAddress, signerAddress] = positional;
  return {
    attestationLedgerAddress,
    auditChainAddress,
    signerAddress
  };
}

async function grantRoleIfMissing(contract, role, account) {
  const alreadyGranted = await contract.hasRole(role, account);
  if (alreadyGranted) {
    return null;
  }

  const tx = await contract.grantRole(role, account);
  const receipt = await tx.wait();
  return {
    txHash: receipt.hash,
    blockNumber: receipt.blockNumber
  };
}

async function main() {
  const { attestationLedgerAddress, auditChainAddress, signerAddress } = parseArgs(
    process.argv
  );

  const attestationLedger = await ethers.getContractAt(
    "AttestationLedger",
    attestationLedgerAddress
  );
  const auditChain = await ethers.getContractAt("AuditChain", auditChainAddress);

  const adminGrant = await grantRoleIfMissing(
    attestationLedger,
    await attestationLedger.ADMIN_ROLE(),
    signerAddress
  );
  const auditorGrant = await grantRoleIfMissing(
    attestationLedger,
    await attestationLedger.AUDITOR_ROLE(),
    signerAddress
  );
  const anchorGrant = await grantRoleIfMissing(
    auditChain,
    await auditChain.ANCHOR_ROLE(),
    signerAddress
  );

  console.log(
    JSON.stringify(
      {
        targetSigner: signerAddress,
        attestationLedger: attestationLedgerAddress,
        auditChain: auditChainAddress,
        grants: {
          adminRole: adminGrant || "already_granted",
          auditorRole: auditorGrant || "already_granted",
          anchorRole: anchorGrant || "already_granted"
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
