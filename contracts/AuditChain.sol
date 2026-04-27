// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

contract AuditChain is Initializable, AccessControlUpgradeable, UUPSUpgradeable {
    bytes32 public constant ANCHOR_ROLE = keccak256("ANCHOR_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");

    event BatchAnchorCommitted(
        bytes32 indexed batchId,
        bytes32 merkleRoot,
        uint256 recordCount,
        uint256 anchoredAt
    );

    event AuditRecordAnchored(
        bytes32 indexed agentDid,
        bytes32 indexed serviceId,
        string ontologyTag,
        bytes32 recordHash,
        bytes32 sessionAssertionId,
        uint256 anchoredAt
    );

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address defaultAdmin) public initializer {
        __AccessControl_init();

        _grantRole(DEFAULT_ADMIN_ROLE, defaultAdmin);
        _grantRole(ADMIN_ROLE, defaultAdmin);
    }

    function commitBatch(
        bytes32 batchId,
        bytes32 merkleRoot,
        uint256 recordCount
    ) external onlyRole(ANCHOR_ROLE) {
        emit BatchAnchorCommitted(batchId, merkleRoot, recordCount, block.timestamp);
    }

    function _authorizeUpgrade(address newImplementation)
        internal
        view
        override
        onlyRole(ADMIN_ROLE)
    {
        require(newImplementation != address(0), "invalid implementation");
    }
}
