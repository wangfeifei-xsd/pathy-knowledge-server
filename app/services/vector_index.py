from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from openai import AsyncOpenAI

from app.config import Settings
from app.models.schemas import LayerName
from app.services import storage
from app.services.llm_config import compute_effective_embedding_model, resolve_embedding_api_key
from app.services.media_codes import extract_media_codes

INDEX_FILE = "wiki_embedding_index.json"
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _index_path(data_root: Path) -> Path:
    p = data_root / ".pathy"
    p.mkdir(parents=True, exist_ok=True)
    return p / INDEX_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_index(data_root: Path) -> dict[str, Any]:
    p = _index_path(data_root)
    if not p.is_file():
        return {"files": {}, "chunks": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}, "chunks": {}}
    if not isinstance(raw, dict):
        return {"files": {}, "chunks": {}}
    raw.setdefault("files", {})
    raw.setdefault("chunks", {})
    return raw


def _save_index(data_root: Path, data: dict[str, Any]) -> None:
    _index_path(data_root).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _chunk_id(path: str, heading_path: str, body: str) -> str:
    return _sha256(f"{path}\n{heading_path}\n{body}")


def _markdown_heading_present(text: str) -> bool:
    return bool(re.search(r"(?m)^#{1,6}\s+\S", text or ""))


def _split_chunks(text: str, max_chars: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"\n{3,}", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= max_chars:
            out.append(p)
            continue
        step = max(max_chars - 120, 120)
        i = 0
        while i < len(p):
            out.append(p[i : i + max_chars].strip())
            i += step
    return [x for x in out if x]


def _parse_md_sections(text: str) -> list[tuple[str, str]]:
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
        out.append((path_str(), "\n".join(buffer).rstrip()))
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


def _is_meaningful_body(body: str) -> bool:
    """与 dialogue_recall._is_meaningful_body 等价：判断 section 是否有实质内容。

    去掉空白、`---`/`___`/`***`/`===` 等分隔符与孤立标点后仍非空即为有意义。
    """
    if not body:
        return False
    s = body.strip()
    if not s:
        return False
    cleaned = re.sub(r"[\s\-_*=]+", "", s)
    return bool(cleaned)


def _wiki_chunks(rel: str, full: str, max_chars: int) -> list[tuple[str, str, str]]:
    full = (full or "").replace("\r\n", "\n")
    if not full.strip():
        return []
    out: list[tuple[str, str, str]] = []
    if _markdown_heading_present(full):
        for path, body in _parse_md_sections(full):
            # 过滤"只有标题没有正文"以及"全为分隔符"的 section，与召回侧行为对齐，
            # 避免把无意义片段写进向量索引（既浪费 embedding API 调用，也污染召回）。
            if not _is_meaningful_body(body):
                continue
            if len(body) <= max_chars:
                out.append((rel, path, body))
            else:
                for p in _split_chunks(body, max_chars):
                    out.append((rel, path, p))
        return out
    for c in _split_chunks(full, max_chars):
        out.append((rel, "", c))
    return out


def mark_wiki_file_stale(data_root: Path, rel_path: str) -> None:
    idx = _load_index(data_root)
    f = idx["files"].get(rel_path)
    if isinstance(f, dict):
        f["status"] = "stale"
    _save_index(data_root, idx)


def delete_wiki_vectors(data_root: Path, rel_path: str) -> None:
    idx = _load_index(data_root)
    prefix = rel_path.rstrip("/")
    for p in list(idx["files"].keys()):
        if p == prefix or p.startswith(prefix + "/"):
            idx["files"].pop(p, None)
    to_del = [k for k, v in idx["chunks"].items() if isinstance(v, dict) and v.get("path") == rel_path]
    if prefix:
        to_del.extend(
            [
                k
                for k, v in idx["chunks"].items()
                if isinstance(v, dict)
                and isinstance(v.get("path"), str)
                and str(v.get("path")).startswith(prefix + "/")
            ]
        )
    for k in to_del:
        idx["chunks"].pop(k, None)
    _save_index(data_root, idx)


def get_wiki_embedding_status_map(data_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    idx = _load_index(data_root)
    for path, meta in idx.get("files", {}).items():
        if isinstance(meta, dict):
            st = str(meta.get("status") or "")
            out[path] = st if st in {"embedded", "stale", "not_embedded"} else "not_embedded"
    return out


@dataclass(frozen=True)
class VectorCandidate:
    rel: str
    heading_path: str
    body: str
    score: float


def _cosine_dense(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(len(a)):
        av = a[i]
        bv = b[i]
        dot += av * bv
        na += av * av
        nb += bv * bv
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def search_wiki_vectors(settings: Settings, query_vector: list[float], wiki_prefix: str, top_n: int) -> list[VectorCandidate]:
    idx = _load_index(settings.data_root.resolve())
    out: list[VectorCandidate] = []
    prefix = (wiki_prefix or "").strip().rstrip("/")
    for _, chunk in idx.get("chunks", {}).items():
        if not isinstance(chunk, dict):
            continue
        rel = str(chunk.get("path") or "")
        if prefix and not rel.startswith(prefix + "/") and rel != prefix:
            continue
        file_meta = idx.get("files", {}).get(rel)
        if not isinstance(file_meta, dict) or file_meta.get("status") != "embedded":
            continue
        vec = chunk.get("vector")
        if not isinstance(vec, list):
            continue
        score = _cosine_dense(query_vector, [float(v) for v in vec])
        if score > 0.0:
            out.append(
                VectorCandidate(
                    rel=rel,
                    heading_path=str(chunk.get("heading_path") or ""),
                    body=str(chunk.get("body") or ""),
                    score=score,
                )
            )
    out.sort(key=lambda x: (-x.score, x.rel, x.heading_path))
    return out[:top_n]


async def embed_wiki_file(settings: Settings, rel_path: str) -> tuple[int, str, str]:
    rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="仅支持 .md 文件嵌入")
    data_root = settings.data_root.resolve()
    text, _ = storage.read_file(data_root, LayerName.wiki, rel, settings.max_file_bytes)
    model_cfg = compute_effective_embedding_model(settings)
    chunks = _wiki_chunks(rel, text, 1200)
    if not chunks:
        delete_wiki_vectors(data_root, rel)
        return 0, model_cfg.model, _now_iso()

    key = resolve_embedding_api_key(settings)
    if not key:
        raise HTTPException(
            status_code=503,
            detail="未配置 embedding API 密钥（EMBEDDING_API_KEY 或数据目录 .pathy/embedding_api_key）",
        )
    kwargs: dict[str, Any] = {"api_key": key, "timeout": model_cfg.timeout_seconds, "max_retries": 0}
    if model_cfg.base_url:
        kwargs["base_url"] = model_cfg.base_url
    client = AsyncOpenAI(**kwargs)
    try:
        emb = await client.embeddings.create(
            model=model_cfg.model,
            input=[f"{c[1]}\n\n{c[2]}".strip()[:8000] for c in chunks],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"embedding 调用失败：{e}") from e

    idx = _load_index(data_root)
    delete_keys = [k for k, v in idx["chunks"].items() if isinstance(v, dict) and v.get("path") == rel]
    for k in delete_keys:
        idx["chunks"].pop(k, None)

    for i, c in enumerate(chunks):
        cid = _chunk_id(rel, c[1], c[2])
        idx["chunks"][cid] = {
            "chunk_id": cid,
            "path": rel,
            "heading_path": c[1],
            "body": c[2],
            "media_codes": extract_media_codes(c[2]),
            "updated_at": _now_iso(),
            "vector": emb.data[i].embedding,
        }

    ts = _now_iso()
    idx["files"][rel] = {
        "path": rel,
        "content_hash": _sha256(text),
        "status": "embedded",
        "chunk_count": len(chunks),
        "updated_at": ts,
        "embedding_model": model_cfg.model,
    }
    _save_index(data_root, idx)
    return len(chunks), model_cfg.model, ts
