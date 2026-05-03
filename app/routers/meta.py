from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.deps import verify_api_key
from app.models.schemas import ConfigSummaryResponse
from app.services.llm_config import api_key_configured, compute_effective_llm

router = APIRouter(prefix="/api/v1", tags=["元数据"], dependencies=[Depends(verify_api_key)])


@router.get("/config", response_model=ConfigSummaryResponse, summary="配置摘要（不含密钥）")
async def config_summary(settings: Settings = Depends(get_settings)) -> ConfigSummaryResponse:
    root = settings.data_root
    resolved = root.resolve()
    eff = compute_effective_llm(settings)
    return ConfigSummaryResponse(
        data_root=str(root),
        data_root_resolved=str(resolved),
        openai_base_url_configured=bool(eff.base_url),
        openai_model=eff.model,
        openai_timeout_seconds=eff.timeout_seconds,
        openai_max_tokens=eff.max_tokens,
        openai_api_key_configured=api_key_configured(settings),
        layers=["raw", "wiki", "schema"],
        auth_enabled=bool(settings.api_key),
    )
