"""POST /manifests endpoint."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_api_key
from api.models.manifest import ServiceManifest
from api.models.query import ManifestRegistrationResponse
from api.services import registry
from crawler.tasks.verify_domain import enqueue_domain_verification

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post(
    "/manifests",
    response_model=ManifestRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_manifest(
    manifest: ServiceManifest,
    db: AsyncSession = Depends(get_db),
) -> ManifestRegistrationResponse:
    """Register or update a service manifest."""
    response = await registry.register_manifest(db=db, manifest=manifest)
    enqueue_domain_verification(manifest.domain, response.service_id)
    return response
