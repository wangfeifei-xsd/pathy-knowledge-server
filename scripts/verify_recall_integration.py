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
    for layer in ("raw", "wiki", "schema"):
        (data_root / layer).mkdir(parents=True, exist_ok=True)

    wiki_body = """# 顶层概述

泛泛而谈的介绍段落。

## 召回机制详解

本节说明 **pathy_bm25_unique_token** 在服务端的路径。英文词 **karpathywiki** 仅用于联调测试。
"""
    (data_root / "wiki" / "demo_recall.md").write_text(wiki_body, encoding="utf-8")

    os.environ["DATA_ROOT"] = str(data_root)
    # 不设 API_KEY：与本地开发一致，Bearer 可选
    os.environ.pop("API_KEY", None)

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
        "wiki_prefix": "",
        "top_k_chunks": 4,
    }
    r = client.post("/api/v1/dialogue/recall", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("recall_method") == "hybrid_bm25_vector", body
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

    # polish-text 路由存在且无 404（无密钥时为 503）
    p = client.post("/api/v1/tasks/polish-text", json={"content": "hello world test"})
    assert p.status_code != 404, p.text

    print("OK: health + /dialogue/recall (hybrid_bm25_vector) + polish-text route reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
