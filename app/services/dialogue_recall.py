"""自然语言 wiki 关键词召回；对话召唤测试在此基础上再调用 LLM。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from app.config import Settings
from app.models.schemas import (
    DialogueRecallBaseParams,
    DialogueRecallHit,
    DialogueRecallRequest,
    DialogueRecallResponse,
    DialogueRecallTestRequest,
    DialogueRecallTestResponse,
    LayerName,
    TaskUsage,
)
from app.services import storage
from app.services.llm_tasks import (
    _build_openai_client,
    _raise_http_from_openai_error,
    _strip_think_blocks,
    _usage_from_completion,
)


_re_word = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")


def _extract_query_terms(q: str) -> list[str]:
    """从自然语言问句中提取用于匹配的词条（英文词 + 中文二字串 + 短词整词）。"""
    s = (q or "").strip().lower()
    if not s:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _re_word.finditer(s):
        w = m.group(0)
        if re.match(r"^[a-z0-9]+$", w):
            if len(w) >= 2:
                if w not in seen:
                    seen.add(w)
                    out.append(w)
            continue
        # CJK
        if len(w) == 1:
            if w not in seen:
                seen.add(w)
                out.append(w)
        else:
            if len(w) <= 8 and w not in seen:
                seen.add(w)
                out.append(w)
            for i in range(len(w) - 1):
                bg = w[i : i + 2]
                if bg not in seen:
                    seen.add(bg)
                    out.append(bg)
    return out


def _score_text(terms: list[str], text: str) -> float:
    if not terms or not text:
        return 0.0
    low = text.lower()
    score = 0.0
    for t in terms:
        if len(t) <= 1:
            score += low.count(t) * 0.25
        else:
            score += low.count(t) * 1.0
    return score


def _split_chunks(text: str, max_chars: int) -> list[str]:
    if max_chars < 200:
        max_chars = 200
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"\n{3,}", text)
    chunks: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= max_chars:
            chunks.append(p)
            continue
        # 长段按固定长度切分，允许重叠以免截断句子中部
        step = max_chars - 120
        if step < 120:
            step = 120
        i = 0
        while i < len(p):
            piece = p[i : i + max_chars]
            chunks.append(piece.strip())
            i += step
    return [c for c in chunks if c]


def _collect_wiki_pairs(
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


def _trim_context(blocks: list[str], budget: int) -> tuple[list[str], bool]:
    """按块顺序拼接，直到总字符接近 budget。"""
    if budget <= 0:
        return [], True
    acc: list[str] = []
    n = 0
    for b in blocks:
        if n + len(b) > budget and acc:
            return acc, True
        acc.append(b)
        n += len(b)
        if n >= budget:
            return acc, len(blocks) > len(acc)
    return acc, False


# 无命中片段时写入 injected_context：全流程会把它交给 LLM；仅召回接口用简短说明。
_INJECTED_EMPTY_FOR_LLM = (
    "(本次召回未命中 wiki 片段；仍将你的问题发给模型，请其说明依据不足。)"
)
_INJECTED_EMPTY_RECALL_ONLY = "(本次召回未命中 wiki 片段。)"


@dataclass(frozen=True)
class WikiKeywordRecallArtifacts:
    user_query: str
    query_terms: list[str]
    files_scanned: int
    recall_hits: list[DialogueRecallHit]
    injected_context: str
    context_truncated: bool


def perform_wiki_keyword_recall(
    settings: Settings,
    body: DialogueRecallBaseParams,
    *,
    empty_injected_text: str = _INJECTED_EMPTY_FOR_LLM,
) -> WikiKeywordRecallArtifacts:
    """从自然语言问句做 wiki 层关键词重叠召回（与全流程测试共用）。"""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query 不能为空")

    data_root = settings.data_root.resolve()
    storage.ensure_layer_tree(data_root)

    terms = _extract_query_terms(q)
    prefix = (body.wiki_prefix or "").strip()
    pairs = _collect_wiki_pairs(data_root, prefix, body.max_files, settings.max_file_bytes)

    scored: list[tuple[float, str, str]] = []
    for rel, full in pairs:
        for ch in _split_chunks(full, body.chunk_max_chars):
            sc = _score_text(terms, ch)
            if sc > 0:
                scored.append((sc, rel, ch))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[: body.top_k_chunks]

    candidates: list[tuple[float, str, str]] = []
    block_lines: list[str] = []
    for sc, rel, chunk in top:
        candidates.append((sc, rel, chunk))
        block_lines.append(f"### {rel}\n\n{chunk.strip()}")

    blocks, ctx_truncated = _trim_context(block_lines, body.context_budget_chars)
    kept_n = len(blocks)
    hits: list[DialogueRecallHit] = []
    for sc, rel, chunk in candidates[:kept_n]:
        snip = chunk.replace("\r\n", "\n").strip()
        if len(snip) > 320:
            snip = snip[:320] + "…"
        hits.append(DialogueRecallHit(path=rel, score=round(sc, 4), snippet=snip))

    injected = "\n\n---\n\n".join(blocks) if blocks else empty_injected_text

    return WikiKeywordRecallArtifacts(
        user_query=q,
        query_terms=terms,
        files_scanned=len(pairs),
        recall_hits=hits,
        injected_context=injected,
        context_truncated=ctx_truncated,
    )


def run_dialogue_recall_only(settings: Settings, body: DialogueRecallRequest) -> DialogueRecallResponse:
    art = perform_wiki_keyword_recall(
        settings,
        body,
        empty_injected_text=_INJECTED_EMPTY_RECALL_ONLY,
    )
    return DialogueRecallResponse(
        user_query=art.user_query,
        recall_method="keyword_overlap",
        query_terms=art.query_terms,
        files_scanned=art.files_scanned,
        recall_hits=art.recall_hits,
        injected_context=art.injected_context,
        context_truncated=art.context_truncated,
        message="已完成 wiki 关键词召回（未调用 LLM）",
    )


async def run_dialogue_recall_test(
    settings: Settings,
    body: DialogueRecallTestRequest,
) -> DialogueRecallTestResponse:
    art = perform_wiki_keyword_recall(settings, body)
    injected = art.injected_context

    system = (body.system_prompt or "").strip() or (
        "你是知识库问答助手。用户会提供「参考资料」片段（来自本地 wiki 编译层的关键词召回）。\n"
        "请优先依据参考资料作答；若资料中没有相关信息，请明确说明「知识库中未找到依据」，不要编造事实。"
    )
    user_msg = (
        f"用户问题：\n{art.user_query}\n\n"
        f"---\n\n参考资料（可能不完整）：\n\n{injected}"
    )

    client, eff = _build_openai_client(settings)
    try:
        completion = await client.chat.completions.create(
            model=eff.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=min(eff.max_tokens, 4096),
        )
    except Exception as e:
        _raise_http_from_openai_error(e)

    raw = (completion.choices[0].message.content or "").strip()
    reply = _strip_think_blocks(raw)
    if not reply:
        raise HTTPException(
            status_code=502,
            detail="模型返回空内容，或去除 redacted_thinking / 思考块后无可用正文",
        )

    return DialogueRecallTestResponse(
        model=eff.model,
        usage=_usage_from_completion(completion.usage),
        user_query=art.user_query,
        recall_method="keyword_overlap",
        query_terms=art.query_terms,
        files_scanned=art.files_scanned,
        recall_hits=art.recall_hits,
        injected_context=injected,
        context_truncated=art.context_truncated,
        assistant_reply=reply,
        message="已完成召回并调用模型（全流程测试）",
    )
