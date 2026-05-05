from fastapi import APIRouter, Body, Depends, HTTPException

from app.config import Settings, get_settings
from app.deps import verify_api_key
from app.models.schemas import (
    BasicModelSettingsResponse,
    BasicModelSettingsUpdateRequest,
    BasicModelSettingsUpdateResult,
    LLMConnectionTestRequest,
    LLMFieldSource,
    LLMSettingsResponse,
    LLMSettingsUpdateRequest,
    LLMSettingsUpdateResult,
    LLMTestResponse,
)
from app.services.llm_config import (
    api_key_configured,
    compute_effective_embedding_model,
    compute_effective_llm,
    compute_effective_rerank_model,
    env_locks,
    embedding_api_key_configured,
    patch_llm_json,
    rerank_api_key_configured,
    write_embedding_api_key_file,
    write_api_key_file,
    write_rerank_api_key_file,
    resolve_embedding_api_key,
    resolve_rerank_api_key,
)
from app.services.llm_test import run_connection_test, run_connection_test_raw

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


def _to_basic_model_response(
    *,
    model: str,
    source: str,
    base_url: str | None,
    base_url_source: str,
    timeout_seconds: float,
    timeout_source: str,
    max_tokens: int,
    max_tokens_source: str,
    api_key_configured_value: bool,
    lock_key: str,
    lock_base_url_key: str,
    lock_timeout_key: str,
    lock_max_tokens_key: str,
    lock_api_key: str,
) -> BasicModelSettingsResponse:
    return BasicModelSettingsResponse(
        model=model,
        model_source=LLMFieldSource(source),
        openai_base_url=base_url,
        openai_base_url_source=LLMFieldSource(base_url_source),
        openai_timeout_seconds=timeout_seconds,
        openai_timeout_source=LLMFieldSource(timeout_source),
        openai_max_tokens=max_tokens,
        openai_max_tokens_source=LLMFieldSource(max_tokens_source),
        openai_api_key_configured=api_key_configured_value,
        env_locks={
            lock_key: env_locks().get(lock_key, False),
            "openai_base_url": env_locks().get(lock_base_url_key, False),
            "openai_timeout_seconds": env_locks().get(lock_timeout_key, False),
            "openai_max_tokens": env_locks().get(lock_max_tokens_key, False),
            "openai_api_key": env_locks().get(lock_api_key, False),
        },
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


@router.get("/settings/embedding", response_model=BasicModelSettingsResponse, summary="获取 embedding 模型配置")
async def get_embedding_settings(settings: Settings = Depends(get_settings)) -> BasicModelSettingsResponse:
    eff = compute_effective_embedding_model(settings)
    return _to_basic_model_response(
        model=eff.model,
        source=eff.model_source,
        base_url=eff.base_url,
        base_url_source=eff.base_url_source,
        timeout_seconds=eff.timeout_seconds,
        timeout_source=eff.timeout_source,
        max_tokens=eff.max_tokens,
        max_tokens_source=eff.max_tokens_source,
        api_key_configured_value=embedding_api_key_configured(settings),
        lock_key="embedding_model",
        lock_base_url_key="embedding_base_url",
        lock_timeout_key="embedding_timeout_seconds",
        lock_max_tokens_key="embedding_max_tokens",
        lock_api_key="embedding_api_key",
    )


@router.put("/settings/embedding", response_model=BasicModelSettingsUpdateResult, summary="更新 embedding 模型配置")
async def put_embedding_settings(
    body: BasicModelSettingsUpdateRequest,
    settings: Settings = Depends(get_settings),
) -> BasicModelSettingsUpdateResult:
    locks = env_locks()
    warnings: list[str] = []
    patch: dict = {}
    if body.model is not None:
        m = body.model.strip()
        if not m:
            raise HTTPException(status_code=400, detail="model 不能为空")
        if locks["embedding_model"]:
            warnings.append("EMBEDDING_MODEL 已由环境变量锁定，未写入运行时文件")
        else:
            patch["embedding_model"] = m
    if body.openai_base_url is not None:
        if locks["embedding_base_url"]:
            warnings.append("EMBEDDING_BASE_URL 已由环境变量锁定，未写入运行时文件")
        else:
            u = body.openai_base_url.strip()
            patch["embedding_base_url"] = u if u else None
    if body.openai_timeout_seconds is not None:
        if locks["embedding_timeout_seconds"]:
            warnings.append("EMBEDDING_TIMEOUT 已由环境变量锁定，未写入运行时文件")
        else:
            t = float(body.openai_timeout_seconds)
            if t <= 0 or t > 3600:
                raise HTTPException(status_code=400, detail="openai_timeout_seconds 需在 (0, 3600] 内")
            patch["embedding_timeout_seconds"] = t
    if body.openai_max_tokens is not None:
        if locks["embedding_max_tokens"]:
            warnings.append("EMBEDDING_MAX_TOKENS 已由环境变量锁定，未写入运行时文件")
        else:
            n = int(body.openai_max_tokens)
            if n < 1 or n > 200_000:
                raise HTTPException(status_code=400, detail="openai_max_tokens 超出合理范围")
            patch["embedding_max_tokens"] = n
    if patch:
        patch_llm_json(settings.data_root.resolve(), patch)
    if body.openai_api_key is not None:
        if locks["embedding_api_key"]:
            warnings.append("EMBEDDING_API_KEY 已由环境变量锁定，未写入密钥文件")
        else:
            key = body.openai_api_key.strip()
            write_embedding_api_key_file(settings.data_root.resolve(), key if key else None)
    eff = compute_effective_embedding_model(settings)
    return BasicModelSettingsUpdateResult(
        settings=_to_basic_model_response(
            model=eff.model,
            source=eff.model_source,
            base_url=eff.base_url,
            base_url_source=eff.base_url_source,
            timeout_seconds=eff.timeout_seconds,
            timeout_source=eff.timeout_source,
            max_tokens=eff.max_tokens,
            max_tokens_source=eff.max_tokens_source,
            api_key_configured_value=embedding_api_key_configured(settings),
            lock_key="embedding_model",
            lock_base_url_key="embedding_base_url",
            lock_timeout_key="embedding_timeout_seconds",
            lock_max_tokens_key="embedding_max_tokens",
            lock_api_key="embedding_api_key",
        ),
        warnings=warnings,
    )


@router.post(
    "/settings/embedding/test",
    response_model=LLMTestResponse,
    summary="测试 Embedding 模型连接（极小 Chat 请求，不写盘）",
)
async def post_embedding_test(
    settings: Settings = Depends(get_settings),
    body: LLMConnectionTestRequest = Body(default=LLMConnectionTestRequest()),
) -> LLMTestResponse:
    eff = compute_effective_embedding_model(settings)
    model = (body.openai_model or "").strip() or eff.model
    base_url = (body.openai_base_url.strip() if body.openai_base_url is not None else None) or eff.base_url
    return await run_connection_test_raw(
        model=model,
        base_url=base_url,
        timeout_seconds=eff.timeout_seconds,
        max_tokens=eff.max_tokens,
        api_key=resolve_embedding_api_key(settings),
        missing_key_error="未配置 Embedding API 密钥（环境变量 EMBEDDING_API_KEY 或 .pathy/embedding_api_key）",
        api_kind="embedding",
    )


@router.get("/settings/rerank", response_model=BasicModelSettingsResponse, summary="获取 rerank 模型配置")
async def get_rerank_settings(settings: Settings = Depends(get_settings)) -> BasicModelSettingsResponse:
    eff = compute_effective_rerank_model(settings)
    return _to_basic_model_response(
        model=eff.model,
        source=eff.model_source,
        base_url=eff.base_url,
        base_url_source=eff.base_url_source,
        timeout_seconds=eff.timeout_seconds,
        timeout_source=eff.timeout_source,
        max_tokens=eff.max_tokens,
        max_tokens_source=eff.max_tokens_source,
        api_key_configured_value=rerank_api_key_configured(settings),
        lock_key="rerank_model",
        lock_base_url_key="rerank_base_url",
        lock_timeout_key="rerank_timeout_seconds",
        lock_max_tokens_key="rerank_max_tokens",
        lock_api_key="rerank_api_key",
    )


@router.put("/settings/rerank", response_model=BasicModelSettingsUpdateResult, summary="更新 rerank 模型配置")
async def put_rerank_settings(
    body: BasicModelSettingsUpdateRequest,
    settings: Settings = Depends(get_settings),
) -> BasicModelSettingsUpdateResult:
    locks = env_locks()
    warnings: list[str] = []
    patch: dict = {}
    if body.model is not None:
        m = body.model.strip()
        if not m:
            raise HTTPException(status_code=400, detail="model 不能为空")
        if locks["rerank_model"]:
            warnings.append("RERANK_MODEL 已由环境变量锁定，未写入运行时文件")
        else:
            patch["rerank_model"] = m
    if body.openai_base_url is not None:
        if locks["rerank_base_url"]:
            warnings.append("RERANK_BASE_URL 已由环境变量锁定，未写入运行时文件")
        else:
            u = body.openai_base_url.strip()
            patch["rerank_base_url"] = u if u else None
    if body.openai_timeout_seconds is not None:
        if locks["rerank_timeout_seconds"]:
            warnings.append("RERANK_TIMEOUT 已由环境变量锁定，未写入运行时文件")
        else:
            t = float(body.openai_timeout_seconds)
            if t <= 0 or t > 3600:
                raise HTTPException(status_code=400, detail="openai_timeout_seconds 需在 (0, 3600] 内")
            patch["rerank_timeout_seconds"] = t
    if body.openai_max_tokens is not None:
        if locks["rerank_max_tokens"]:
            warnings.append("RERANK_MAX_TOKENS 已由环境变量锁定，未写入运行时文件")
        else:
            n = int(body.openai_max_tokens)
            if n < 1 or n > 200_000:
                raise HTTPException(status_code=400, detail="openai_max_tokens 超出合理范围")
            patch["rerank_max_tokens"] = n
    if patch:
        patch_llm_json(settings.data_root.resolve(), patch)
    if body.openai_api_key is not None:
        if locks["rerank_api_key"]:
            warnings.append("RERANK_API_KEY 已由环境变量锁定，未写入密钥文件")
        else:
            key = body.openai_api_key.strip()
            write_rerank_api_key_file(settings.data_root.resolve(), key if key else None)
    eff = compute_effective_rerank_model(settings)
    return BasicModelSettingsUpdateResult(
        settings=_to_basic_model_response(
            model=eff.model,
            source=eff.model_source,
            base_url=eff.base_url,
            base_url_source=eff.base_url_source,
            timeout_seconds=eff.timeout_seconds,
            timeout_source=eff.timeout_source,
            max_tokens=eff.max_tokens,
            max_tokens_source=eff.max_tokens_source,
            api_key_configured_value=rerank_api_key_configured(settings),
            lock_key="rerank_model",
            lock_base_url_key="rerank_base_url",
            lock_timeout_key="rerank_timeout_seconds",
            lock_max_tokens_key="rerank_max_tokens",
            lock_api_key="rerank_api_key",
        ),
        warnings=warnings,
    )


@router.post(
    "/settings/rerank/test",
    response_model=LLMTestResponse,
    summary="测试 Rerank 模型连接（极小 Chat 请求，不写盘）",
)
async def post_rerank_test(
    settings: Settings = Depends(get_settings),
    body: LLMConnectionTestRequest = Body(default=LLMConnectionTestRequest()),
) -> LLMTestResponse:
    eff = compute_effective_rerank_model(settings)
    model = (body.openai_model or "").strip() or eff.model
    base_url = (body.openai_base_url.strip() if body.openai_base_url is not None else None) or eff.base_url
    return await run_connection_test_raw(
        model=model,
        base_url=base_url,
        timeout_seconds=eff.timeout_seconds,
        max_tokens=eff.max_tokens,
        api_key=resolve_rerank_api_key(settings),
        missing_key_error="未配置 Rerank API 密钥（环境变量 RERANK_API_KEY 或 .pathy/rerank_api_key）",
    )
