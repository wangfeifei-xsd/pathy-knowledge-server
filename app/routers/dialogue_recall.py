from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.models.schemas import (
    DialogueRecallRequest,
    DialogueRecallResponse,
    DialogueRecallTestRequest,
    DialogueRecallTestResponse,
    RecallStopwordsResponse,
    RecallStopwordsUpdateRequest,
)
from app.services.dialogue_recall import run_dialogue_recall_only, run_dialogue_recall_test
from app.services.recall_stopwords import (
    parse_stopwords_text,
    read_effective_stopwords,
    read_runtime_stopwords,
    runtime_stopwords_path,
    write_runtime_stopwords,
)

router = APIRouter(prefix="/api/v1/dialogue", tags=["对话召回"])


@router.post(
    "/recall",
    response_model=DialogueRecallResponse,
    summary="自然语言召回知识",
    description="自然语言问句 → wiki 编译层 BM25 + 向量双路召回 → 合并去重 + 轻量 rerank；不调用 LLM（与全流程测试共用召回实现）。",
)
async def dialogue_recall(
    body: DialogueRecallRequest,
    settings: Settings = Depends(get_settings),
) -> DialogueRecallResponse:
    return await run_dialogue_recall_only(settings, body)


@router.post(
    "/recall-test",
    response_model=DialogueRecallTestResponse,
    summary="对话召唤测试（全流程）",
    description="模拟自然语言输入 → wiki BM25 + 向量双路召回并 rerank → 将片段注入 LLM 上下文 → 返回模型回答。",
)
async def dialogue_recall_test(
    body: DialogueRecallTestRequest,
    settings: Settings = Depends(get_settings),
) -> DialogueRecallTestResponse:
    return await run_dialogue_recall_test(settings, body)


@router.get(
    "/stopwords",
    response_model=RecallStopwordsResponse,
    summary="获取召回停用词（运行时优先）",
)
def get_recall_stopwords(
    settings: Settings = Depends(get_settings),
) -> RecallStopwordsResponse:
    runtime_words = read_runtime_stopwords(settings)
    words = runtime_words or read_effective_stopwords(settings)
    source = "runtime_file" if runtime_words else "default_builtin"
    return RecallStopwordsResponse(
        words=words,
        source=source,
        runtime_path=runtime_stopwords_path(settings).as_posix(),
        count=len(words),
        message="已加载召回停用词",
    )


@router.put(
    "/stopwords",
    response_model=RecallStopwordsResponse,
    summary="更新召回停用词（写入 data/.pathy）",
)
def put_recall_stopwords(
    body: RecallStopwordsUpdateRequest,
    settings: Settings = Depends(get_settings),
) -> RecallStopwordsResponse:
    # 复用解析规则（统一小写、去重），保持与文件读取行为一致。
    words = parse_stopwords_text("\n".join(body.words))
    n, rel = write_runtime_stopwords(settings, words)
    return RecallStopwordsResponse(
        words=words,
        source="runtime_file",
        runtime_path=(settings.data_root.resolve() / rel).as_posix(),
        count=n,
        message="已保存召回停用词",
    )
