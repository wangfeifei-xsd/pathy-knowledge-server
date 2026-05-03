from fastapi import APIRouter

from app.models.schemas import HealthResponse

router = APIRouter(tags=["健康"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health() -> HealthResponse:
    return HealthResponse()
