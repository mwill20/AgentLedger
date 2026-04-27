// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

contract AttestationLedger is Initializable, AccessControlUpgradeable, UUPSUpgradeable {
    bytes32 public constant AUDITOR_ROLE = keccak256("AUDITOR_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");

    mapping(bytes32 => bytes32) public latestManifestHash;
    mapping(bytes32 => bool) public isGloballyRevoked;

    event AttestationRecorded(
        bytes32 indexed serviceId,
        bytes32 indexed auditorRef,
        string ontologyScope,
        string certificationRef,
        uint256 expiresAt,
        bytes32 evidenceHash
    );

    event RevocationRecorded(
        bytes32 indexed serviceId,
        bytes32 indexed auditorRef,
        string reasonCode,
        bytes32 evidenceHash
    );

    event VersionRecorded(
        bytes32 indexed serviceId,
        bytes32 manifestHash,
        uint256 recordedAt
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

    function recordAttestation(
        bytes32 serviceId,
        string calldata ontologyScope,
        string calldata certificationRef,
        uint256 expiresAt,
        bytes32 evidenceHash
    ) external onlyRole(AUDITOR_ROLE) {
        emit AttestationRecorded(
            serviceId,
            keccak256(abi.encodePacked(msg.sender)),
            ontologyScope,
            certificationRef,
            expiresAt,
            evidenceHash
        );
    }

    function recordRevocation(
        bytes32 serviceId,
        string calldata reasonCode,
        bytes32 evidenceHash
    ) external onlyRole(AUDITOR_ROLE) {
        isGloballyRevoked[serviceId] = true;
        emit RevocationRecorded(
            serviceId,
            keccak256(abi.encodePacked(msg.sender)),
            reasonCode,
            evidenceHash
        );
    }

    function recordVersion(
        bytes32 serviceId,
        bytes32 manifestHash
    ) external onlyRole(ADMIN_ROLE) {
        latestManifestHash[serviceId] = manifestHash;
        emit VersionRecorded(serviceId, manifestHash, block.timestamp);
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
