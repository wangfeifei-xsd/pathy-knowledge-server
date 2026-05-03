from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.deps import verify_api_key
from app.models.schemas import (
    DialogueRecallRequest,
    DialogueRecallResponse,
    DialogueRecallTestRequest,
    DialogueRecallTestResponse,
)
from app.services.dialogue_recall import run_dialogue_recall_only, run_dialogue_recall_test

router = APIRouter(
    prefix="/api/v1/dialogue",
    tags=["对话召回"],
    dependencies=[Depends(verify_api_key)],
)


@router.post(
    "/recall",
    response_model=DialogueRecallResponse,
    summary="自然语言召回知识",
    description="自然语言问句 → wiki 编译层关键词重叠召回片段与拼接上下文；不调用 LLM（与全流程测试共用召回实现）。",
)
def dialogue_recall(
    body: DialogueRecallRequest,
    settings: Settings = Depends(get_settings),
) -> DialogueRecallResponse:
    return run_dialogue_recall_only(settings, body)


@router.post(
    "/recall-test",
    response_model=DialogueRecallTestResponse,
    summary="对话召唤测试（全流程）",
    description="模拟自然语言输入 → wiki 关键词召回 → 将片段注入 LLM 上下文 → 返回模型回答。",
)
async def dialogue_recall_test(
    body: DialogueRecallTestRequest,
    settings: Settings = Depends(get_settings),
) -> DialogueRecallTestResponse:
    return await run_dialogue_recall_test(settings, body)
