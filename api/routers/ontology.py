"""GET /ontology endpoint."""

from fastapi import APIRouter, Depends

from api.dependencies import require_api_key
from api.models.service import OntologyResponse
from api.services.registry import build_ontology_response

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/ontology", response_model=OntologyResponse)
async def get_ontology() -> OntologyResponse:
    """Return the full capability ontology from the source-of-truth file."""
    return build_ontology_response()
