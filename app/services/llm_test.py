"""一次性探测 OpenAI 兼容端点连通性（极小请求）。"""

from __future__ import annotations

import time
from typing import Optional

from openai import AsyncOpenAI

from app.config import Settings
from app.models.schemas import LLMTestResponse, TaskUsage
from app.services.llm_config import compute_effective_llm, resolve_openai_api_key


def _usage(u: Optional[object]) -> Optional[TaskUsage]:
    if u is None:
        return None
    return TaskUsage(
        prompt_tokens=getattr(u, "prompt_tokens", None),
        completion_tokens=getattr(u, "completion_tokens", None),
        total_tokens=getattr(u, "total_tokens", None),
    )


async def run_connection_test(
    settings: Settings,
    draft_model: Optional[str],
    draft_base_url: Optional[str],
) -> LLMTestResponse:
    """draft_* 若给出则仅用于本次探测（不写盘）；否则用当前有效配置。"""
    eff = compute_effective_llm(settings)
    model = (draft_model or "").strip() or eff.model
    if draft_base_url is not None:
        bu = draft_base_url.strip() or None
    else:
        bu = eff.base_url

    key = resolve_openai_api_key(settings)
    if not key:
        return LLMTestResponse(
            ok=False,
            model=model,
            base_url=bu,
            elapsed_ms=0.0,
            message="",
            error="未配置 API 密钥（环境变量或 .pathy/openai_api_key）",
        )

    kwargs: dict = {"api_key": key, "timeout": eff.timeout_seconds}
    if bu:
        kwargs["base_url"] = bu
    client = AsyncOpenAI(**kwargs)

    t0 = time.perf_counter()
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=min(16, eff.max_tokens),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return LLMTestResponse(
            ok=True,
            model=model,
            base_url=bu,
            elapsed_ms=round(elapsed_ms, 2),
            message="Chat Completions 调用成功",
            usage=_usage(completion.usage),
        )
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        err = str(e).strip()
        if len(err) > 800:
            err = err[:800] + "…"
        return LLMTestResponse(
            ok=False,
            model=model,
            base_url=bu,
            elapsed_ms=round(elapsed_ms, 2),
            message="",
            error=err or type(e).__name__,
        )


async def run_connection_test_raw(
    *,
    model: str,
    base_url: Optional[str],
    timeout_seconds: float,
    max_tokens: int,
    api_key: Optional[str],
    missing_key_error: str,
    api_kind: str = "chat",
) -> LLMTestResponse:
    if not api_key:
        return LLMTestResponse(
            ok=False,
            model=model,
            base_url=base_url,
            elapsed_ms=0.0,
            message="",
            error=missing_key_error,
        )
    kwargs: dict = {"api_key": api_key, "timeout": timeout_seconds}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    t0 = time.perf_counter()
    try:
        usage_obj: Optional[object] = None
        ok_message = "Chat Completions 调用成功"
        if api_kind == "embedding":
            emb = await client.embeddings.create(
                model=model,
                input=["ping"],
            )
            usage_obj = getattr(emb, "usage", None)
            ok_message = "Embeddings 调用成功"
        else:
            completion = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=min(16, max_tokens),
            )
            usage_obj = completion.usage
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return LLMTestResponse(
            ok=True,
            model=model,
            base_url=base_url,
            elapsed_ms=round(elapsed_ms, 2),
            message=ok_message,
            usage=_usage(usage_obj),
        )
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        err = str(e).strip()
        if len(err) > 800:
            err = err[:800] + "…"
        return LLMTestResponse(
            ok=False,
            model=model,
            base_url=base_url,
            elapsed_ms=round(elapsed_ms, 2),
            message="",
            error=err or type(e).__name__,
        )
