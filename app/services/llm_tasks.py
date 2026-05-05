import re
from pathlib import Path
from typing import NoReturn, Optional

from fastapi import HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError

from app.config import Settings
from app.models.schemas import (
    CompileTaskRequest,
    CompileTaskResponse,
    LayerName,
    LintTaskRequest,
    LintTaskResponse,
    PolishTextRequest,
    PolishTextResponse,
    TaskUsage,
)
from app.services import storage
from app.services.vector_index import delete_wiki_vectors
from app.services.llm_config import EffectiveLLM, compute_effective_llm, resolve_openai_api_key

# 常见推理模型输出的思考片段（MiniMax / DeepSeek / Qwen 等），写入 wiki 前剥离
_THINK_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<redacted_thinking\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<reasoning\b[^>]*>.*?</reasoning>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<思考[^>]*>.*?</思考>", re.DOTALL),
    re.compile(r"^\s*```\s*think\s*\r?\n.*?^\s*```\s*$", re.DOTALL | re.MULTILINE | re.IGNORECASE),
)


def _strip_think_blocks(text: str) -> str:
    """去掉模型返回中的思考/推理块，避免写入 wiki 正文。"""
    if not text:
        return text
    out = text
    for _ in range(64):
        prev = out
        for pat in _THINK_BLOCK_PATTERNS:
            out = pat.sub("", out)
        if out == prev:
            break
    out = re.sub(r"\n{3,}", "\n\n", out.strip())
    return out


def _raise_http_from_openai_error(exc: BaseException) -> NoReturn:
    """将 OpenAI SDK 异常转为 HTTPException，避免未捕获导致 500。"""
    if not isinstance(exc, OpenAIError):
        raise exc

    if isinstance(exc, APITimeoutError):
        raise HTTPException(
            status_code=504,
            detail="模型接口在等待响应时超时。可适当增大 openai_timeout_seconds；若前面还有 nginx 等网关，请同步增大 proxy_read_timeout。",
        ) from exc

    if isinstance(exc, APIConnectionError):
        raise HTTPException(status_code=502, detail=f"无法连接模型接口：{exc}") from exc

    if isinstance(exc, APIStatusError):
        sc = exc.status_code
        if sc == 504:
            detail = (
                "上游网关超时(504)：多为反向代理在模型返回前断开。请在网关侧增大 proxy_read_timeout / "
                "send_timeout，或减少单次编译素材长度、换更快模型。"
            )
            client_sc = 504
        elif sc == 503:
            detail = "模型服务暂不可用(503)，请稍后重试"
            client_sc = 503
        elif sc == 502:
            detail = "模型网关返回 502，上游不可用或路由错误"
            client_sc = 502
        elif sc == 429:
            detail = "模型接口限流(429)，请稍后重试"
            client_sc = 429
        elif sc == 401:
            detail = "模型接口鉴权失败(401)，请检查 API Key 与 Base URL"
            client_sc = 401
        elif sc >= 500:
            detail = f"模型上游错误(HTTP {sc})"
            client_sc = 502
        else:
            msg = (getattr(exc, "message", None) or str(exc))[:400]
            detail = f"模型接口错误(HTTP {sc})：{msg}"
            client_sc = 502
        raise HTTPException(status_code=client_sc, detail=detail) from exc

    raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}") from exc


def _usage_from_completion(usage: Optional[object]) -> Optional[TaskUsage]:
    if usage is None:
        return None
    pt = getattr(usage, "prompt_tokens", None)
    ct = getattr(usage, "completion_tokens", None)
    tt = getattr(usage, "total_tokens", None)
    return TaskUsage(prompt_tokens=pt, completion_tokens=ct, total_tokens=tt)


def _build_openai_client(settings: Settings) -> tuple[AsyncOpenAI, EffectiveLLM]:
    key = resolve_openai_api_key(settings)
    if not key:
        raise HTTPException(
            status_code=503,
            detail="未配置 API 密钥（环境变量 OPENAI_API_KEY 或数据目录 .pathy/openai_api_key）",
        )
    eff = compute_effective_llm(settings)
    # 避免上游每次 504 时 SDK 默认再重试 2 次（长耗时、仍失败）
    kwargs: dict = {"api_key": key, "timeout": eff.timeout_seconds, "max_retries": 0}
    if eff.base_url:
        kwargs["base_url"] = eff.base_url
    return AsyncOpenAI(**kwargs), eff


async def run_compile(
    settings: Settings,
    body: CompileTaskRequest,
) -> CompileTaskResponse:
    client, eff = _build_openai_client(settings)
    data_root = settings.data_root.resolve()
    storage.ensure_layer_tree(data_root)

    schema_chunks: list[str] = []
    schema_paths = list(body.schema_paths or [])
    agents = storage.safe_resolve_under(
        storage.layer_root(data_root, LayerName.schema),
        "AGENTS.md",
    )
    if agents.is_file() and "AGENTS.md" not in schema_paths:
        schema_paths.insert(0, "AGENTS.md")
    for sp in schema_paths:
        text, _ = storage.read_file(data_root, LayerName.schema, sp, settings.max_file_bytes)
        schema_chunks.append(f"### 规范文件: {sp}\n\n{text}")

    raw_chunks: list[str] = []
    for rp in body.input_paths:
        text, _ = storage.read_file(data_root, LayerName.raw, rp, settings.max_file_bytes)
        raw_chunks.append(f"### 原始素材: {rp}\n\n{text}")

    system = (
        "你是知识库编译助手。根据「规范层」约束，将「原始层」材料整理为结构化 Markdown wiki 条目，"
        "保持标题层级、内部链接与交叉引用清晰。只输出最终 wiki 正文（Markdown），不要代码围栏包裹全文。"
        "不要在正文中输出推理思考标签（如 think/reasoning）或「思考」包裹块。"
    )
    user_parts = [
        "\n\n".join(schema_chunks) if schema_chunks else "(未提供规范文件)",
        "\n\n".join(raw_chunks),
    ]
    if body.extra_instructions:
        user_parts.append(f"附加说明:\n{body.extra_instructions}")
    user = (
        "请将以上内容编译为单篇 wiki 文档，输出路径目标为: "
        f"{body.output_path}\n\n---\n\n"
        + "\n\n---\n\n".join(user_parts)
    )

    try:
        completion = await client.chat.completions.create(
            model=eff.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=eff.max_tokens,
        )
    except Exception as e:
        _raise_http_from_openai_error(e)
    choice = completion.choices[0].message
    content = _strip_think_blocks((choice.content or "").strip())
    if not content:
        raise HTTPException(
            status_code=502,
            detail="模型返回空内容，或去除 think / reasoning 等思考块后无可用正文",
        )

    storage.write_file(
        data_root,
        LayerName.wiki,
        body.output_path,
        content,
        settings.max_file_bytes,
    )
    delete_wiki_vectors(data_root, body.output_path)
    written = [body.output_path]
    return CompileTaskResponse(
        model=eff.model,
        usage=_usage_from_completion(completion.usage),
        output_path=body.output_path,
        written_files=written,
        message="编译完成并已写入编译层",
    )


def _collect_wiki_markdown(
    data_root: Path,
    rel_prefix: str,
    max_files: int,
    max_bytes: int,
) -> list[tuple[str, str]]:
    base = storage.layer_root(data_root, LayerName.wiki)
    root = storage.safe_resolve_under(base, rel_prefix) if rel_prefix else base
    out: list[tuple[str, str]] = []
    if root.is_file():
        text, _ = storage.read_file(data_root, LayerName.wiki, rel_prefix, max_bytes)
        return [(rel_prefix, text)]
    for path in sorted(root.rglob("*.md")):
        if len(out) >= max_files:
            break
        rel = path.relative_to(base).as_posix()
        text, _ = storage.read_file(data_root, LayerName.wiki, rel, max_bytes)
        out.append((rel, text))
    return out


async def run_lint(
    settings: Settings,
    body: LintTaskRequest,
) -> LintTaskResponse:
    client, eff = _build_openai_client(settings)
    data_root = settings.data_root.resolve()
    storage.ensure_layer_tree(data_root)

    files_inspected: list[str] = []
    bundled: list[str] = []
    if body.wiki_paths:
        for wp in body.wiki_paths:
            text, _ = storage.read_file(data_root, LayerName.wiki, wp, settings.max_file_bytes)
            files_inspected.append(wp)
            bundled.append(f"### {wp}\n\n{text}")
    else:
        pairs = _collect_wiki_markdown(data_root, "", body.max_files, settings.max_file_bytes)
        for rel, text in pairs:
            files_inspected.append(rel)
            bundled.append(f"### {rel}\n\n{text}")

    if not bundled:
        return LintTaskResponse(
            model=eff.model,
            report="wiki 层未发现可检查的 Markdown 文件",
            files_inspected=[],
        )

    schema_hint = ""
    agents = storage.safe_resolve_under(
        storage.layer_root(data_root, LayerName.schema),
        "AGENTS.md",
    )
    if agents.is_file():
        s, _ = storage.read_file(data_root, LayerName.schema, "AGENTS.md", settings.max_file_bytes)
        schema_hint = f"\n\n规范参考 (AGENTS.md):\n{s}"

    system = (
        "你是知识库一致性检查助手。根据规范，列出编译层（wiki）中链接断裂、标题层级不当、"
        "术语不一致等问题，输出简洁的中文检查报告（Markdown 列表）。不要编造文件中不存在的内容。"
    )
    user = "待检查文件如下：" + schema_hint + "\n\n" + "\n\n---\n\n".join(bundled)

    try:
        completion = await client.chat.completions.create(
            model=eff.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=min(eff.max_tokens, 4096),
        )
    except Exception as e:
        _raise_http_from_openai_error(e)
    report = _strip_think_blocks((completion.choices[0].message.content or "").strip())
    if not report:
        raise HTTPException(status_code=502, detail="模型未返回报告，或去除思考块后无内容")

    auto_fix = body.auto_fix
    if auto_fix:
        # MVP：仅将报告落盘到 wiki/_lint_report.md 作为“可追溯输出”，不自动改条目正文
        report_path = "_lint_report.md"
        storage.write_file(
            data_root,
            LayerName.wiki,
            report_path,
            f"# Lint 报告\n\n{report}\n",
            settings.max_file_bytes,
        )
        files_inspected.append(report_path)

    return LintTaskResponse(
        model=eff.model,
        usage=_usage_from_completion(completion.usage),
        report=report,
        files_inspected=files_inspected,
        auto_fix_applied=auto_fix,
    )


async def run_polish_text(settings: Settings, body: PolishTextRequest) -> PolishTextResponse:
    """润色规范层/说明类 Markdown，供前端「创建」弹窗使用。"""
    client, eff = _build_openai_client(settings)
    system = (
        "你是技术文档与知识库规范编辑。请润色用户给出的 Markdown，用于「规范层」文件（如 AGENTS.md）。\n"
        "要求：层次清晰、语句通顺、列表与标题格式正确；可补充明显的章节引导句，但**不要编造**用户未提供的业务事实或具体数据。\n"
        "只输出润色后的完整 Markdown 正文，不要用 markdown 代码围栏包裹全文。"
    )
    user = body.content.strip()
    if not user:
        raise HTTPException(status_code=400, detail="content 不能为空")
    if body.instruction and body.instruction.strip():
        user = f"【附加说明】\n{body.instruction.strip()}\n\n---\n\n{user}"
    try:
        completion = await client.chat.completions.create(
            model=eff.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=min(eff.max_tokens, 16000),
        )
    except Exception as e:
        _raise_http_from_openai_error(e)
    out = _strip_think_blocks((completion.choices[0].message.content or "").strip())
    if not out:
        raise HTTPException(status_code=502, detail="模型返回空内容，或去除思考块后无内容")
    return PolishTextResponse(
        model=eff.model,
        usage=_usage_from_completion(completion.usage),
        content=out,
    )
