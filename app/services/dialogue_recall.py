"""自然语言 wiki 召回：BM25 + 向量双路召回，合并去重后轻量 rerank。"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from openai import AsyncOpenAI

from app.config import Settings
from app.models.schemas import (
    DialogueRecallBaseParams,
    DialogueRecallHit,
    DialogueRecallLaneStatus,
    DialogueRecallRequest,
    DialogueRecallResponse,
    DialogueRecallTestRequest,
    DialogueRecallTestResponse,
    LayerName,
    MediaRef,
    TaskUsage,
)
from app.services import storage
from app.services.llm_tasks import (
    _build_openai_client,
    _raise_http_from_openai_error,
    _strip_think_blocks,
    _usage_from_completion,
)
from app.services.llm_config import compute_effective_embedding_model, resolve_embedding_api_key
from app.services.recall_stopwords import read_effective_stopwords
from app.services.media_codes import extract_media_codes, strip_media_tags
from app.services.media_store import enrich_codes_to_refs
from app.services.vector_index import search_wiki_vectors

_re_word = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
logger = logging.getLogger("pathy")

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


def _is_meaningful_body(text: str) -> bool:
    """过滤空正文与仅分隔符（如 --- / *** / ___）的无意义片段。"""
    s = (text or "").strip()
    if not s:
        return False
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if not lines:
        return False
    # 全部行为 Markdown 水平分隔线时，视为无意义正文。
    for ln in lines:
        if re.fullmatch(r"[-*_]{3,}", ln):
            continue
        return True
    return False


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
            if not _is_meaningful_body(body):
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
    """拼接注入 LLM 的单块正文：仅路径/标题/纯文本，不含任何媒体占位或 code 说明。"""
    b = (body or "").strip()
    if heading_path:
        return f"### {rel}\n\n**{heading_path}**\n\n{b}"
    return f"### {rel}\n\n{b}"


def _normalize_wiki_prefixes(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        p = (item or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


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


def _collect_wiki_pairs_for_prefixes(
    data_root: Path,
    rel_prefixes: list[str],
    max_files: int,
    max_bytes: int,
) -> list[tuple[str, str]]:
    prefixes = _normalize_wiki_prefixes(rel_prefixes)
    if not prefixes:
        return _collect_wiki_pairs(data_root, "", max_files, max_bytes)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for prefix in prefixes:
        if len(out) >= max_files:
            break
        pairs = _collect_wiki_pairs(data_root, prefix, max_files - len(out), max_bytes)
        for rel, text in pairs:
            if rel in seen:
                continue
            seen.add(rel)
            out.append((rel, text))
            if len(out) >= max_files:
                break
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
RECALL_METHOD_HYBRID = "hybrid_bm25_vector"


def _merge_per_hit_media_lists(per_hit: list[list[MediaRef]]) -> list[MediaRef]:
    """按命中顺序展平各片段的 media，再按 code 去重。"""
    seen: set[str] = set()
    out: list[MediaRef] = []
    for group in per_hit:
        for m in group:
            if m.code in seen:
                continue
            seen.add(m.code)
            out.append(m)
    return out


@dataclass(frozen=True)
class WikiKeywordRecallArtifacts:
    user_query: str
    query_terms: list[str]
    files_scanned: int
    recall_hits: list[DialogueRecallHit]
    merged_media: list[MediaRef]
    bm25: DialogueRecallLaneStatus
    vector: DialogueRecallLaneStatus
    injected_context: str
    context_truncated: bool


async def _score_chunks_vector_with_model(
    settings: Settings,
    query: str,
    wiki_prefixes: list[str],
    top_n: int,
) -> tuple[list[tuple[float, _IndexedChunk]], DialogueRecallLaneStatus]:
    """模型向量召回：query embedding 后在已嵌入索引里做余弦检索；同时返回该路状态。"""
    if not query.strip():
        return [], DialogueRecallLaneStatus(
            status="skipped_empty_query",
            candidate_count=0,
            detail="问句为空，跳过向量召回",
            embedding_model=None,
        )
    em = compute_effective_embedding_model(settings)
    key = resolve_embedding_api_key(settings)
    if not key:
        logger.warning("dialogue_recall vector embedding_api_key_missing")
        return [], DialogueRecallLaneStatus(
            status="skipped_no_api_key",
            candidate_count=0,
            detail="未配置 embedding API 密钥（EMBEDDING_API_KEY、服务端配置或数据目录 .pathy/embedding_api_key）",
            embedding_model=em.model,
        )
    kwargs: dict[str, object] = {"api_key": key, "timeout": em.timeout_seconds, "max_retries": 0}
    if em.base_url:
        kwargs["base_url"] = em.base_url
    client = AsyncOpenAI(**kwargs)
    try:
        emb = await client.embeddings.create(model=em.model, input=[query])
    except Exception as e:
        logger.warning("dialogue_recall vector embedding_failed model=%s error=%s", em.model, str(e))
        err = str(e).strip()
        if len(err) > 400:
            err = err[:400] + "…"
        return [], DialogueRecallLaneStatus(
            status="error_embedding",
            candidate_count=0,
            detail=err or "embedding 请求失败",
            embedding_model=em.model,
        )
    qv = emb.data[0].embedding
    prefixes = _normalize_wiki_prefixes(wiki_prefixes)
    hits = search_wiki_vectors(settings, qv, prefixes, top_n)
    scored = [(h.score, _IndexedChunk(h.rel, h.heading_path, h.body)) for h in hits]
    logger.info(
        "dialogue_recall vector_done prefixes=%s top_n=%d hits=%d model=%s",
        prefixes or ["/"],
        top_n,
        len(scored),
        em.model,
    )
    return scored, DialogueRecallLaneStatus(
        status="ok",
        candidate_count=len(scored),
        detail=None,
        embedding_model=em.model,
    )


def _min_max_norm(v: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0 if v > 0 else 0.0
    x = (v - lo) / (hi - lo)
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass(frozen=True)
class _MergedCandidate:
    chunk: _IndexedChunk
    bm25_score: float
    vector_score: float
    rerank_score: float


def _merge_and_rerank_candidates(
    bm25_scored: list[tuple[float, _IndexedChunk]],
    vector_scored: list[tuple[float, _IndexedChunk]],
    terms: list[str],
) -> list[_MergedCandidate]:
    merged: dict[tuple[str, str, str], dict[str, float | _IndexedChunk]] = {}
    for sc, ch in bm25_scored:
        k = (ch.rel, ch.heading_path, ch.body)
        cur = merged.get(k)
        if not cur:
            merged[k] = {"chunk": ch, "bm25": sc, "vector": 0.0}
        else:
            cur["bm25"] = max(float(cur["bm25"]), sc)
    for sc, ch in vector_scored:
        k = (ch.rel, ch.heading_path, ch.body)
        cur = merged.get(k)
        if not cur:
            merged[k] = {"chunk": ch, "bm25": 0.0, "vector": sc}
        else:
            cur["vector"] = max(float(cur["vector"]), sc)

    bm_vals = [float(v["bm25"]) for v in merged.values()]
    vec_vals = [float(v["vector"]) for v in merged.values()]
    bm_lo, bm_hi = (min(bm_vals), max(bm_vals)) if bm_vals else (0.0, 0.0)
    ve_lo, ve_hi = (min(vec_vals), max(vec_vals)) if vec_vals else (0.0, 0.0)

    out: list[_MergedCandidate] = []
    for v in merged.values():
        ch = v["chunk"]
        bm = float(v["bm25"])
        ve = float(v["vector"])
        nb = _min_max_norm(bm, bm_lo, bm_hi)
        nv = _min_max_norm(ve, ve_lo, ve_hi)
        title_low = ch.heading_path.lower()
        title_hit = 1.0 if any(t in title_low for t in terms) else 0.0
        # 轻量规则 rerank：BM25 与向量加权融合 + 标题命中微调
        rr = 0.55 * nb + 0.4 * nv + 0.05 * title_hit
        out.append(_MergedCandidate(chunk=ch, bm25_score=bm, vector_score=ve, rerank_score=rr))

    out.sort(
        key=lambda x: (
            -x.rerank_score,
            -x.bm25_score,
            -x.vector_score,
            x.chunk.rel,
            x.chunk.heading_path,
        )
    )
    return out


async def perform_wiki_keyword_recall(
    settings: Settings,
    body: DialogueRecallBaseParams,
    *,
    empty_injected_text: str = _INJECTED_EMPTY_FOR_LLM,
) -> WikiKeywordRecallArtifacts:
    """wiki 层双路召回（BM25 + 向量）并轻量 rerank。"""
    started_at = time.perf_counter()
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query 不能为空")

    data_root = settings.data_root.resolve()
    storage.ensure_layer_tree(data_root)

    raw_terms = _extract_query_terms(q)
    stopwords = set(read_effective_stopwords(settings))
    terms = _filter_terms(raw_terms, stopwords)
    prefixes = _normalize_wiki_prefixes(body.wiki_prefixes)
    logger.info(
        "dialogue_recall start query_len=%d wiki_prefixes=%s max_files=%d top_k=%d bm25_top_n=%d vector_top_n=%d terms_raw=%d terms_kept=%d",
        len(q),
        prefixes or ["/"],
        body.max_files,
        body.top_k_chunks,
        body.bm25_top_n,
        body.vector_top_n,
        len(raw_terms),
        len(terms),
    )

    pairs = _collect_wiki_pairs_for_prefixes(
        data_root, prefixes, body.max_files, settings.max_file_bytes
    )

    all_chunks: list[_IndexedChunk] = []
    for rel, full in pairs:
        all_chunks.extend(_wiki_indexed_chunks(rel, full, body.chunk_max_chars))
    logger.info(
        "dialogue_recall indexed files_scanned=%d chunks_total=%d",
        len(pairs),
        len(all_chunks),
    )

    bm25_scored = _score_chunks_bm25(all_chunks, terms)
    bm25_scored.sort(key=lambda x: (-x[0], x[1].rel, x[1].heading_path))
    bm25_top = bm25_scored[: body.bm25_top_n]
    logger.info(
        "dialogue_recall bm25_done candidates=%d kept_top=%d",
        len(bm25_scored),
        len(bm25_top),
    )

    if not all_chunks:
        bm25_lane = DialogueRecallLaneStatus(
            status="skipped_no_chunks",
            candidate_count=0,
            detail="扫描范围内无可用 wiki 切片（无文件或正文为空）",
            embedding_model=None,
        )
    elif not terms:
        bm25_lane = DialogueRecallLaneStatus(
            status="skipped_no_terms",
            candidate_count=0,
            detail="问句分词后无可用词项，或均被停用词过滤",
            embedding_model=None,
        )
    elif not bm25_scored:
        bm25_lane = DialogueRecallLaneStatus(
            status="no_hits",
            candidate_count=0,
            detail="BM25 未产生正分匹配",
            embedding_model=None,
        )
    else:
        bm25_lane = DialogueRecallLaneStatus(
            status="ok",
            candidate_count=len(bm25_scored),
            detail=None,
            embedding_model=None,
        )

    vector_scored, vector_lane = await _score_chunks_vector_with_model(
        settings,
        q,
        prefixes,
        body.vector_top_n,
    )
    vector_scored.sort(key=lambda x: (-x[0], x[1].rel, x[1].heading_path))
    vector_top = vector_scored[: body.vector_top_n]

    merged_ranked = _merge_and_rerank_candidates(bm25_top, vector_top, terms)
    # 兜底过滤：无论来自 BM25 还是向量索引，均剔除空正文/仅分隔符正文。
    meaningful_ranked = [it for it in merged_ranked if _is_meaningful_body(it.chunk.body)]
    top = meaningful_ranked[: body.top_k_chunks]
    logger.info(
        "dialogue_recall merge_done vector_candidates=%d vector_top=%d merged=%d meaningful=%d top_k_selected=%d",
        len(vector_scored),
        len(vector_top),
        len(merged_ranked),
        len(meaningful_ranked),
        len(top),
    )

    candidates: list[tuple[float, str, str, str]] = []
    block_lines: list[str] = []
    for item in top:
        ch = item.chunk
        candidates.append((item.rerank_score, ch.rel, ch.heading_path, ch.body))
        body_for_llm = strip_media_tags(ch.body)
        block_lines.append(_format_injected_block(ch.rel, ch.heading_path, body_for_llm))

    blocks, ctx_truncated = _trim_context(block_lines, body.context_budget_chars)
    kept_n = len(blocks)
    hits: list[DialogueRecallHit] = []
    per_hit_media: list[list[MediaRef]] = []
    for sc, rel, hpath, chunk_body in candidates[:kept_n]:
        snip = (chunk_body or "").replace("\r\n", "\n").strip()
        if hpath:
            snip = f"{hpath}\n{snip}".strip()
        snip = strip_media_tags(snip)
        if len(snip) > 320:
            snip = snip[:320] + "…"
        mrefs = enrich_codes_to_refs(data_root, extract_media_codes(chunk_body))
        per_hit_media.append(mrefs)
        hits.append(
            DialogueRecallHit(
                path=rel,
                score=round(sc, 6),
                snippet=snip,
                heading_path=hpath or "",
            )
        )

    merged_media = _merge_per_hit_media_lists(per_hit_media)
    injected = "\n\n---\n\n".join(blocks) if blocks else empty_injected_text
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "dialogue_recall done recall_hits=%d context_truncated=%s injected_chars=%d elapsed_ms=%.2f",
        len(hits),
        str(ctx_truncated).lower(),
        len(injected),
        elapsed_ms,
    )

    return WikiKeywordRecallArtifacts(
        user_query=q,
        query_terms=terms,
        files_scanned=len(pairs),
        recall_hits=hits,
        merged_media=merged_media,
        bm25=bm25_lane,
        vector=vector_lane,
        injected_context=injected,
        context_truncated=ctx_truncated,
    )


async def run_dialogue_recall_only(settings: Settings, body: DialogueRecallRequest) -> DialogueRecallResponse:
    art = await perform_wiki_keyword_recall(
        settings,
        body,
        empty_injected_text=_INJECTED_EMPTY_RECALL_ONLY,
    )
    return DialogueRecallResponse(
        user_query=art.user_query,
        recall_method=RECALL_METHOD_HYBRID,
        query_terms=art.query_terms,
        files_scanned=art.files_scanned,
        recall_hits=art.recall_hits,
        merged_media=art.merged_media,
        bm25=art.bm25,
        vector=art.vector,
        injected_context=art.injected_context,
        context_truncated=art.context_truncated,
        message="已完成 wiki 双路召回（BM25 + 向量）并 rerank（未调用 LLM）",
    )


async def run_dialogue_recall_test(
    settings: Settings,
    body: DialogueRecallTestRequest,
) -> DialogueRecallTestResponse:
    art = await perform_wiki_keyword_recall(settings, body)
    injected = art.injected_context

    system = (body.system_prompt or "").strip() or (
        "你是一位资深海水鱼健康管理与疾病防控顾问，代号「检疫神克隆体」。\n"
        "用户消息中会附带从本地 wiki 编译层经 BM25 + 向量双路召回并 rerank 得到的「知识库召回片段」；片段为纯文字摘录，可能不完整、顺序打散或含轻微噪声。\n"
        "\n"
        "【作答原则】\n"
        "1. 以召回片段为首要依据：能引用处请把机制讲清楚（生理、药理、水质、病原体与鱼体状态如何勾连），语气专业、笃定而克制；在科学前提下可适当用比喻或现场感描写，让解释好读、好记，避免干巴巴的关键词堆砌。\n"
        "2. 证据不足或关键参数缺失（如药物浓度、药浴时长、水温、曝气、换水节奏等）时，须明确写出「在现有资料下只能判断到…」「尚需…才能定论」，不得编造数据或虚构文献。\n"
        "3. 当片段与问题明显无关、或无法从中提炼有效依据时，须明确写出「知识库中未找到依据」；若给出一两句常识性提醒，须标注为常识推断而非库内结论。\n"
        "4. 若存在多轮对话，请把用户后续补充与先前描述一并纳入，形成一条连贯的分析链，并在末段收束到用户当下最关心的结论或行动建议。\n"
        "5. 结构建议：可用小标题、短列表或表格组织信息；优先给可操作的检疫/治疗/观察要点，少用空话套话。"
    )
    user_msg = (
        f"【待答问题】\n{art.user_query}\n\n"
        f"---\n【知识库召回片段】\n（经检索与合并，可能截断；请严格据此并结合对话上文作答）\n\n{injected}"
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
        recall_method=RECALL_METHOD_HYBRID,
        query_terms=art.query_terms,
        files_scanned=art.files_scanned,
        recall_hits=art.recall_hits,
        merged_media=art.merged_media,
        bm25=art.bm25,
        vector=art.vector,
        injected_context=injected,
        context_truncated=art.context_truncated,
        assistant_reply=reply,
        message="已完成召回并调用模型（全流程测试）",
    )
