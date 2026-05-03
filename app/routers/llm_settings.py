from fastapi import APIRouter, Body, Depends, HTTPException

from app.config import Settings, get_settings
from app.deps import verify_api_key
from app.models.schemas import (
    LLMConnectionTestRequest,
    LLMFieldSource,
    LLMSettingsResponse,
    LLMSettingsUpdateRequest,
    LLMSettingsUpdateResult,
    LLMTestResponse,
)
from app.services.llm_config import (
    api_key_configured,
    compute_effective_llm,
    env_locks,
    patch_llm_json,
    write_api_key_file,
)
from app.services.llm_test import run_connection_test

router = APIRouter(prefix="/api/v1", tags=["模型配置"], dependencies=[Depends(verify_api_key)])


def _to_response(settings: Settings) -> LLMSettingsResponse:
    eff = compute_effective_llm(settings)
    return LLMSettingsResponse(
        openai_model=eff.model,
        openai_model_source=LLMFieldSource(eff.model_source),
        openai_base_url=eff.base_url,
        openai_base_url_source=LLMFieldSource(eff.base_url_source),
        openai_timeout_seconds=eff.timeout_seconds,
        openai_timeout_source=LLMFieldSource(eff.timeout_source),
        openai_max_tokens=eff.max_tokens,
        openai_max_tokens_source=LLMFieldSource(eff.max_tokens_source),
        openai_api_key_configured=api_key_configured(settings),
        env_locks=env_locks(),
    )


@router.get("/settings/llm", response_model=LLMSettingsResponse, summary="获取 LLM 有效配置与来源")
async def get_llm_settings(settings: Settings = Depends(get_settings)) -> LLMSettingsResponse:
    return _to_response(settings)


@router.post(
    "/settings/llm/test",
    response_model=LLMTestResponse,
    summary="测试 OpenAI 兼容连接（极小 Chat 请求，不写盘）",
)
async def post_llm_test(
    settings: Settings = Depends(get_settings),
    body: LLMConnectionTestRequest = Body(default=LLMConnectionTestRequest()),
) -> LLMTestResponse:
    return await run_connection_test(settings, body.openai_model, body.openai_base_url)


@router.put("/settings/llm", response_model=LLMSettingsUpdateResult, summary="更新运行时 LLM 配置（写入数据目录）")
async def put_llm_settings(
    body: LLMSettingsUpdateRequest,
    settings: Settings = Depends(get_settings),
) -> LLMSettingsUpdateResult:
    locks = env_locks()
    warnings: list[str] = []
    root = settings.data_root.resolve()
    patch: dict = {}

    if body.openai_model is not None:
        m = body.openai_model.strip()
        if not m:
            raise HTTPException(status_code=400, detail="openai_model 不能为空")
        if locks["openai_model"]:
            warnings.append("OPENAI_MODEL 已由环境变量锁定，未写入运行时文件")
        else:
            patch["openai_model"] = m

    if body.openai_base_url is not None:
        if locks["openai_base_url"]:
            warnings.append("OPENAI_BASE_URL 已由环境变量锁定，未写入运行时文件")
        else:
            u = body.openai_base_url.strip()
            patch["openai_base_url"] = u if u else None

    if body.openai_timeout_seconds is not None:
        if locks["openai_timeout_seconds"]:
            warnings.append("OPENAI_TIMEOUT 已由环境变量锁定，未写入运行时文件")
        else:
            t = float(body.openai_timeout_seconds)
            if t <= 0 or t > 3600:
                raise HTTPException(status_code=400, detail="openai_timeout_seconds 需在 (0, 3600] 内")
            patch["openai_timeout_seconds"] = t

    if body.openai_max_tokens is not None:
        if locks["openai_max_tokens"]:
            warnings.append("OPENAI_MAX_TOKENS 已由环境变量锁定，未写入运行时文件")
        else:
            n = int(body.openai_max_tokens)
            if n < 1 or n > 200_000:
                raise HTTPException(status_code=400, detail="openai_max_tokens 超出合理范围")
            patch["openai_max_tokens"] = n

    if patch:
        patch_llm_json(root, patch)

    if body.openai_api_key is not None:
        if locks["openai_api_key"]:
            warnings.append("OPENAI_API_KEY 已由环境变量锁定，未写入密钥文件")
        else:
            key = body.openai_api_key.strip()
            write_api_key_file(root, key if key else None)

    return LLMSettingsUpdateResult(settings=_to_response(settings), warnings=warnings)
