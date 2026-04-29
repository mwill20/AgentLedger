# Limitations

## Project Status

AgentLedger v0.1.0 is a local proof of concept. It is not documented as production-ready and should not be treated as a deployed commercial service.

## Known Limitations

- No root `LICENSE` file currently exists. Usage rights are unclear until the owner selects a license.
- Layer 3 testnet deployment is deferred; local and code-complete paths exist, but live chain writes are not part of the default POC quickstart.
- Full Layer 2 credential issuance requires `ISSUER_PRIVATE_JWK`.
- Redis failure behavior is fail open for the POC.
- Compliance exports are structured evidence packages, not certified legal, regulatory, insurance, or compliance determinations.
- Liability attribution outputs are evidence-based computation outputs, not legal rulings.
- Local Docker Compose is the documented deployment target for v0.1.0.
- Hosted/cloud deployment behavior is not yet measured.

## Model And Embedding Limitations

- Local Docker Compose defaults to deterministic hash embeddings for reproducibility and fast startup.
- `EMBEDDING_MODE=model` can use sentence-transformers, but model download/cache behavior depends on the environment.
- Semantic search quality in hash mode is not equivalent to model embedding quality.
- Model license/version details need owner review before external publication.

## Dataset And Ontology Limitations

- The repository uses an ontology file, not a training dataset.
- Ontology coverage is limited to the v0.1 tag set.
- Bias, coverage, and taxonomy completeness have not been externally evaluated.

## Unsupported Scenarios

- Production handling of real user data without additional security, privacy, legal, and operational review.
- Binding legal liability adjudication.
- Licensed insurance underwriting.
- Financial custody or escrow.
- Live blockchain deployment without configured contracts, RPC, signer, and funded wallet.

## Future Work

- TODO: Select and add a repository license.
- TODO: Add a formal security contact.
- TODO: Add production deployment architecture if production use becomes a goal.
- TODO: Add measured resource usage and repeatable performance benchmarks.
- TODO: Add model license/version details if `EMBEDDING_MODE=model` is used for publication.
