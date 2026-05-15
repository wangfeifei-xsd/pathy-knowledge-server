from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.models.schemas import ConfigSummaryResponse
from app.services.llm_config import (
    compute_effective_embedding_model,
    compute_effective_llm,
    compute_effective_rerank_model,
)

router = APIRouter(prefix="/api/v1", tags=["元数据"])


@router.get("/config", response_model=ConfigSummaryResponse, summary="配置摘要（不含密钥）")
async def config_summary(settings: Settings = Depends(get_settings)) -> ConfigSummaryResponse:
    root = settings.data_root
    resolved = root.resolve()
    eff_llm = compute_effective_llm(settings)
    eff_emb = compute_effective_embedding_model(settings)
    eff_rr = compute_effective_rerank_model(settings)
    return ConfigSummaryResponse(
        data_root=str(root),
        data_root_resolved=str(resolved),
        llm_base_url=eff_llm.base_url,
        llm_model=eff_llm.model,
        embedding_base_url=eff_emb.base_url,
        embedding_model=eff_emb.model,
        rerank_base_url=eff_rr.base_url,
        rerank_model=eff_rr.model,
        layers=["raw", "wiki", "schema", "media"],
    )
