# Model Card

## Model Usage Status

AgentLedger does not train or fine-tune a model in this repository.

The registry search path can use embeddings for semantic search:

- `EMBEDDING_MODE=hash`: deterministic CPU-only fallback used for local POC and fast tests.
- `EMBEDDING_MODE=model`: uses sentence-transformers when available.

## Model Details

| Field | Value |
|---|---|
| Model name | `all-MiniLM-L6-v2` is referenced in `api/services/embedder.py`. |
| Source | sentence-transformers package/model loading path. TODO: add authoritative model URL before publication. |
| Version | TODO: document exact model revision or cache artifact if `EMBEDDING_MODE=model` is used for evaluation. |
| License | TODO: document model license before publication. |
| Training data | Not controlled by this repository. TODO: cite upstream model documentation. |
| Fine-tuning | None in this repository. |
| Local default | Docker Compose defaults to `EMBEDDING_MODE=hash`. |

## Intended Use

Semantic service search over registered service capability descriptions.

## Out-Of-Scope Use

- Legal, medical, financial, or safety-critical ranking without external review.
- Production claims about semantic relevance without measured evaluation.
- Bias-sensitive ranking without a documented evaluation set.

## Inputs And Outputs

| Item | Description |
|---|---|
| Input | Free-text search query or capability description. |
| Output | Embedding vector used for similarity ranking. |

## Evaluation

Not yet measured with a labeled relevance dataset.

The repository has automated tests for search behavior, but there is no documented benchmark dataset for semantic relevance.

## Limitations

- Hash embeddings are deterministic but approximate token-overlap behavior.
- Model embeddings may require network/model-cache setup.
- Search quality depends on service capability descriptions.
