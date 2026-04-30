# Dataset And Data Documentation

## Dataset Status

This project does not include or require a machine-learning training dataset for the local proof of concept.

AgentLedger uses:

- structured service manifests submitted at runtime,
- an ontology file at `ontology/v0.1.json`,
- database records created by local API use and migrations,
- optional external chain data when Layer 3 web3 mode is configured.

## Ontology Summary

| Field | Description |
|---|---|
| Source | Repository-local file: `ontology/v0.1.json`. |
| License | Covered by the repository MIT license unless a future ontology-specific license is added. |
| Version | `0.1` as returned by `/v1/ontology`. |
| Number of tags | 65 observed in local `/v1/ontology` response. |
| Domains | TRAVEL, FINANCE, HEALTH, COMMERCE, PRODUCTIVITY. |
| Format | JSON. |
| Required fields | `tag`, `domain`, `function`, `label`, `description`, `sensitivity_tier`. |
| Preprocessing | Seeded into PostgreSQL by `db/seed_ontology.py`. |
| Known limitations | Coverage and taxonomy completeness have not been externally evaluated. |

## Runtime Data

Runtime data can include service manifests, agent identities, context profiles, disclosures, workflow executions, liability snapshots, claims, and compliance export logs.

Do not use real user data in the POC without additional security, privacy, and legal review.

## Bias And Representation

Not yet measured. The ontology may encode assumptions about supported agent-service domains and should be reviewed before production use.
