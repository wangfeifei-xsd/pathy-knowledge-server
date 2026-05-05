"""自然语言 wiki 召回：BM25 + Markdown 标题切块 + 标题命中加权；对话测试在此基础上再调 LLM。"""

from __future__ import annotations

import math
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
from app.services.recall_stopwords import read_effective_stopwords

_re_word = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# BM25（Okapi）常数
_BM25_K1 = 1.2
_BM25_B = 0.75
# 标题路径中与 query term 命中时的附加权重（按 IDF 缩放）
_TITLE_HIT_WEIGHT = 0.4


@dataclass(frozen=True)
class _IndexedChunk:
    rel: str
    heading_path: str
    body: str

    def doc_for_match(self) -> str:
        if self.heading_path:
            return f"{self.heading_path}\n\n{self.body}"
        return self.body


def _extract_query_terms(q: str) -> list[str]:
    """从问句提取匹配词条；不含单字中文（降低噪声）；英文数字须长度≥2。"""
    s = (q or "").strip().lower()
    if not s:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _re_word.finditer(s):
        w = m.group(0)
        if re.match(r"^[a-z0-9]+$", w):
            if len(w) >= 2 and w not in seen:
                seen.add(w)
                out.append(w)
            continue
        # CJK：不再加入单字
        if len(w) == 1:
            continue
        if len(w) <= 8 and w not in seen:
            seen.add(w)
            out.append(w)
        for i in range(len(w) - 1):
            bg = w[i : i + 2]
            if bg not in seen:
                seen.add(bg)
                out.append(bg)
    return out


def _filter_terms(terms: list[str], stopwords: set[str]) -> list[str]:
    """停用词过滤。"""
    return [t for t in terms if t and t not in stopwords]


def _markdown_heading_present(text: str) -> bool:
    return bool(re.search(r"(?m)^#{1,6}\s+\S", text or ""))


def _split_oversized_body(path: str, body: str, max_chars: int) -> list[tuple[str, str]]:
    """单节过长时按原有滑窗切分，保留同一标题路径。"""
    parts = _split_chunks(body, max_chars)
    return [(path, p) for p in parts]


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
        step = max_chars - 120
        if step < 120:
            step = 120
        i = 0
        while i < len(p):
            piece = p[i : i + max_chars]
            chunks.append(piece.strip())
            i += step
    return [c for c in chunks if c]


def _parse_md_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切成 (heading_path, body)。path 为「父级 > 当前」链。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    out: list[tuple[str, str]] = []

    def path_str() -> str:
        return " > ".join(t for _, t in stack)

    def flush() -> None:
        if not buffer and not stack:
            return
        if not stack:
            body = "\n".join(buffer).rstrip()
            if body:
                out.append(("", body))
            buffer.clear()
            return
        p = path_str()
        body = "\n".join(buffer).rstrip()
        out.append((p, body))
        buffer.clear()

    for line in lines:
        m = _HEADING_LINE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buffer.append(line)
    flush()
    return out


def _wiki_indexed_chunks(rel: str, full: str, max_chars: int) -> list[_IndexedChunk]:
    full = (full or "").replace("\r\n", "\n")
    if not full.strip():
        return []
    if _markdown_heading_present(full):
        sections = _parse_md_sections(full)
        indexed: list[_IndexedChunk] = []
        for path, body in sections:
            if not body.strip() and not path:
                continue
            if len(body) <= max_chars:
                indexed.append(_IndexedChunk(rel, path, body))
            else:
                for pth, piece in _split_oversized_body(path, body, max_chars):
                    indexed.append(_IndexedChunk(rel, pth, piece))
        return indexed
    return [_IndexedChunk(rel, "", c) for c in _split_chunks(full, max_chars)]


def _bm25_idf(n_docs: int, df: int) -> float:
    return math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)


def _score_chunks_bm25(chunks: list[_IndexedChunk], terms: list[str]) -> list[tuple[float, _IndexedChunk]]:
    """对所有切片计算 BM25 + 标题路径命中加权，返回 (score, chunk) 且 score>0。"""
    n = len(chunks)
    if not n or not terms:
        return []

    docs_lower = [c.doc_for_match().lower() for c in chunks]
    titles_lower = [c.heading_path.lower() for c in chunks]
    dls = [len(d) for d in docs_lower]
    avgdl = sum(dls) / n if n else 1.0
    if avgdl <= 0:
        avgdl = 1.0

    df_map: dict[str, int] = {}
    for t in terms:
        df_map[t] = sum(1 for dl in docs_lower if dl.count(t) > 0)

    idf_map: dict[str, float] = {t: _bm25_idf(n, df_map[t]) for t in terms}

    scored: list[tuple[float, _IndexedChunk]] = []
    for i, ch in enumerate(chunks):
        dl = dls[i]
        doc_low = docs_lower[i]
        tit_low = titles_lower[i]
        s = 0.0
        for t in terms:
            idf = idf_map[t]
            tf = doc_low.count(t)
            if tf > 0:
                denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (dl / avgdl))
                s += idf * (tf * (_BM25_K1 + 1.0)) / denom
            tft = tit_low.count(t)
            if tft > 0:
                s += _TITLE_HIT_WEIGHT * idf * min(tft, 5)
        if s > 0.0:
            scored.append((s, ch))
    return scored


def _format_injected_block(rel: str, heading_path: str, body: str) -> str:
    b = (body or "").strip()
    if heading_path:
        return f"### {rel}\n\n**{heading_path}**\n\n{b}"
    return f"### {rel}\n\n{b}"


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


_INJECTED_EMPTY_FOR_LLM = (
    "(本次召回未命中 wiki 片段；仍将你的问题发给模型，请其说明依据不足。)"
)
_INJECTED_EMPTY_RECALL_ONLY = "(本次召回未命中 wiki 片段。)"

RECALL_METHOD_BM25 = "bm25"


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
    """wiki 层 BM25 召回（标题切块、停用词、标题加权）。"""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query 不能为空")

    data_root = settings.data_root.resolve()
    storage.ensure_layer_tree(data_root)

    raw_terms = _extract_query_terms(q)
    stopwords = set(read_effective_stopwords(settings))
    terms = _filter_terms(raw_terms, stopwords)

    prefix = (body.wiki_prefix or "").strip()
    pairs = _collect_wiki_pairs(data_root, prefix, body.max_files, settings.max_file_bytes)

    all_chunks: list[_IndexedChunk] = []
    for rel, full in pairs:
        all_chunks.extend(_wiki_indexed_chunks(rel, full, body.chunk_max_chars))

    scored = _score_chunks_bm25(all_chunks, terms)
    scored.sort(key=lambda x: (-x[0], x[1].rel, x[1].heading_path))
    top = scored[: body.top_k_chunks]

    candidates: list[tuple[float, str, str, str]] = []
    block_lines: list[str] = []
    for sc, ch in top:
        candidates.append((sc, ch.rel, ch.heading_path, ch.body))
        block_lines.append(_format_injected_block(ch.rel, ch.heading_path, ch.body))

    blocks, ctx_truncated = _trim_context(block_lines, body.context_budget_chars)
    kept_n = len(blocks)
    hits: list[DialogueRecallHit] = []
    for sc, rel, hpath, chunk_body in candidates[:kept_n]:
        snip = (chunk_body or "").replace("\r\n", "\n").strip()
        if hpath:
            snip = f"{hpath}\n{snip}".strip()
        if len(snip) > 320:
            snip = snip[:320] + "…"
        hits.append(DialogueRecallHit(path=rel, score=round(sc, 6), snippet=snip))

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
        recall_method=RECALL_METHOD_BM25,
        query_terms=art.query_terms,
        files_scanned=art.files_scanned,
        recall_hits=art.recall_hits,
        injected_context=art.injected_context,
        context_truncated=art.context_truncated,
        message="已完成 wiki BM25 召回（未调用 LLM）",
    )


async def run_dialogue_recall_test(
    settings: Settings,
    body: DialogueRecallTestRequest,
) -> DialogueRecallTestResponse:
    art = perform_wiki_keyword_recall(settings, body)
    injected = art.injected_context

    system = (body.system_prompt or "").strip() or (
        "你是知识库问答助手。用户会提供「参考资料」片段（来自本地 wiki 编译层的 BM25 关键词召回）。\n"
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
        recall_method=RECALL_METHOD_BM25,
        query_terms=art.query_terms,
        files_scanned=art.files_scanned,
        recall_hits=art.recall_hits,
        injected_context=injected,
        context_truncated=art.context_truncated,
        assistant_reply=reply,
        message="已完成召回并调用模型（全流程测试）",
    )
