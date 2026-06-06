#!/usr/bin/env python3
"""端到端自检：临时 DATA_ROOT + wiki 样本 → 双路 /recall HTTP 断言（不调用 LLM）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = tempfile.mkdtemp(prefix="pathy_recall_verify_")
    data_root = Path(root)
    for layer in ("raw", "wiki", "schema", "media"):
        (data_root / layer).mkdir(parents=True, exist_ok=True)

    wiki_body = """# 顶层概述

泛泛而谈的介绍段落。

## 召回机制详解

本节说明 **pathy_bm25_unique_token** 在服务端的路径。英文词 **karpathywiki** 仅用于联调测试。
"""
    (data_root / "wiki" / "demo_recall.md").write_text(wiki_body, encoding="utf-8")

    os.environ["DATA_ROOT"] = str(data_root)

    cwd = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(cwd))

    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    hc = client.get("/health")
    assert hc.status_code == 200, hc.text

    payload = {
        "query": "pathy_bm25_unique_token karpathywiki 怎么用",
        "wiki_prefixes": [],
        "top_k_chunks": 4,
    }
    r = client.post("/api/v1/dialogue/recall", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("recall_method") == "hybrid_bm25_vector", body
    assert "merged_media" in body
    assert body.get("merged_media") == [], body.get("merged_media")
    assert body.get("files_scanned") == 1
    terms = body.get("query_terms") or []
    # 英文数字段不含下划线，pathy_bm25_unique_token 会拆成 pathy / bm25 / unique / token
    assert "karpathywiki" in terms, terms
    assert {"pathy", "bm25", "unique", "token"} <= set(terms), terms
    ctx = body.get("injected_context") or ""
    assert "pathy_bm25_unique_token" in ctx, ctx[:500]
    # 标题切块注入应带 Markdown 语义路径行
    assert "顶层概述" in ctx or "召回机制" in ctx, ctx[:800]
    hits = body.get("recall_hits") or []
    assert len(hits) >= 1, hits
    bm25 = body.get("bm25") or {}
    vec = body.get("vector") or {}
    assert bm25.get("status") == "ok", bm25
    assert vec.get("status") in ("ok", "skipped_no_api_key", "error_embedding"), vec

    # 1x1 PNG：上传 + wiki 占位符 + 召回 media 字段 + 下载 + 反向索引
    mini_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    up = client.post(
        "/api/v1/media/upload",
        files={"file": ("pixel.png", mini_png, "image/png")},
        data={"title": "verify"},
    )
    assert up.status_code == 200, up.text
    mcode = up.json().get("code")
    assert mcode, up.json()
    wiki_with_media = wiki_body + (
        f"\n## 图示与联调\n\n![[MEDIA:{mcode}]]\n\n英文 **karpathywiki** 与上图用于联调。\n"
    )
    (data_root / "wiki" / "demo_recall.md").write_text(wiki_with_media, encoding="utf-8")
    r2 = client.post("/api/v1/dialogue/recall", json=payload)
    assert r2.status_code == 200, r2.text
    hits2 = r2.json().get("recall_hits") or []
    assert len(hits2) >= 1, hits2
    assert "media" not in hits2[0], hits2[0]
    merged2 = r2.json().get("merged_media") or []
    assert any(x.get("code") == mcode for x in merged2), merged2
    ctx2 = r2.json().get("injected_context") or ""
    assert "![[MEDIA:" not in ctx2 and "<!-- media:" not in ctx2.lower(), ctx2[:800]
    assert mcode not in ctx2, "injected_context 不得含媒体 code 或关联说明"
    assert "本段关联媒体" not in ctx2 and "GET /api/v1/media" not in ctx2, ctx2[:800]
    gbin = client.get(f"/api/v1/media/{mcode}")
    assert gbin.status_code == 200, gbin.text
    assert gbin.content[:4] == b"\x89PNG"
    rx = client.post("/api/v1/media/reindex-backrefs")
    assert rx.status_code == 200, rx.text
    br = client.get(f"/api/v1/media/{mcode}/backrefs")
    assert br.status_code == 200, br.text
    entries = br.json().get("entries") or []
    assert any(e.get("wiki_path") == "demo_recall.md" for e in entries), entries

    # polish-text 路由存在且无 404（无密钥时为 503）
    p = client.post("/api/v1/tasks/polish-text", json={"content": "hello world test"})
    assert p.status_code != 404, p.text

    print("OK: health + recall + media upload/backrefs + polish-text route reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
