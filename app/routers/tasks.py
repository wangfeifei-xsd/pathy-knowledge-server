from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.models.schemas import (
    CompileTaskRequest,
    CompileTaskResponse,
    LintTaskRequest,
    LintTaskResponse,
    PolishTextRequest,
    PolishTextResponse,
)
from app.services import llm_tasks

router = APIRouter(prefix="/api/v1/tasks", tags=["LLM 任务"])


@router.post(
    "/compile",
    response_model=CompileTaskResponse,
    summary="编译任务（同步）：原始层 → 编译层",
)
async def task_compile(
    body: CompileTaskRequest,
    settings: Settings = Depends(get_settings),
) -> CompileTaskResponse:
    return await llm_tasks.run_compile(settings, body)


@router.post(
    "/lint",
    response_model=LintTaskResponse,
    summary="Lint / 一致性报告任务",
)
async def task_lint(
    body: LintTaskRequest,
    settings: Settings = Depends(get_settings),
) -> LintTaskResponse:
    return await llm_tasks.run_lint(settings, body)


@router.post(
    "/polish-text",
    response_model=PolishTextResponse,
    summary="文本润色（规范层 Markdown 等）",
)
async def task_polish_text(
    body: PolishTextRequest,
    settings: Settings = Depends(get_settings),
) -> PolishTextResponse:
    return await llm_tasks.run_polish_text(settings, body)
