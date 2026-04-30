# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `main` | Best effort for the local proof of concept. |
| `v0.1.0` | Best effort for the tagged proof-of-concept release. |

## Security Status

AgentLedger v0.1.0 is a local proof of concept. It is not documented as production-hardened.

## Security Assumptions

- Local Docker Compose is the primary supported execution environment.
- API keys in `.env.example` are development placeholders only.
- Real secrets must be supplied through local `.env` or a secret manager and must not be committed.
- Full Layer 2 issuance requires an owner-provided `ISSUER_PRIVATE_JWK`.
- Layer 3 testnet or production writes require separately configured RPC credentials, signer key, and contract addresses.
- Compliance and liability exports are evidence packages, not legal or regulatory certifications.

## Threat Model Summary

| Asset | Threat | Control / Current Status |
|---|---|---|
| API endpoints | Unauthorized access | API key checks on protected endpoints; TODO: production auth strategy. |
| Service manifests | Malformed or malicious input | Pydantic validation, ontology validation, null-byte rejection. |
| Agent credentials | Forged or replayed assertions | DID/key validation and nonce/TTL checks where configured. |
| Context data | Over-requesting or inappropriate disclosure | Context profiles, mismatch detection, disclosure audit trail. |
| Sensitive context fields | Plaintext leakage | HMAC commitment path for committed fields; TODO: full ZKP deferred. |
| Redis-backed controls | Cache/rate-limit unavailability | POC fail-open behavior; TODO: production fail-closed review. |
| Liability evidence | Post-event tampering | Synchronous snapshots and append-oriented evidence records. |
| Secrets | Accidental commit | `.env` is gitignored; maintainers must avoid committing keys/logs/dumps. |

## Agent Safety Boundaries

AgentLedger is not an autonomous runtime agent. It stores and serves infrastructure records for agent platforms.

This project is allowed to:

- register and query service manifests,
- manage local identity/session records,
- record trust, context, workflow, and liability evidence,
- generate local compliance/evidence exports.

This project is not intended to:

- execute arbitrary agent workflows,
- make binding legal determinations,
- provide licensed insurance underwriting,
- custody or transfer funds,
- process real user data in production without further review.

Human approval/configuration is required for:

- production secrets,
- full issuer private key setup,
- Layer 3 testnet or production deployment,
- legal/security/compliance sign-off,
- license selection.

## Sensitive Data Handling

Do not commit:

- `.env`,
- API keys,
- private JWKs,
- blockchain signer keys,
- database dumps,
- production logs,
- real user context data,
- generated compliance exports containing sensitive records.

Use synthetic data for examples and tests.

## Abuse Cases

- Registering deceptive service manifests.
- Over-requesting context fields.
- Attempting to game workflow quality scores.
- Filing duplicate or abusive liability claims.
- Treating POC evidence exports as certified compliance reports.

## Vulnerability Reporting

For sensitive vulnerabilities, use GitHub private vulnerability reporting for this repository if enabled.

If private vulnerability reporting is not enabled, contact the maintainer out of band before sharing exploitable details. Open a GitHub issue for non-sensitive security documentation issues only. Do not include secrets, private keys, live credentials, personal data, or exploitable details in a public issue.
