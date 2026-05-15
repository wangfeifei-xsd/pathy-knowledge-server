"""从 wiki Markdown 正文中解析绑定的多媒体 code（与向量索引、召回一致）。"""

from __future__ import annotations

import re

# Obsidian 风格：![[MEDIA:abc123]]
_RE_MEDIA_WIKILINK = re.compile(r"\!\[\[MEDIA:([a-zA-Z0-9_-]+)\]\]", re.IGNORECASE)
# HTML 注释：<!-- media:abc123 -->
_RE_MEDIA_HTML_COMMENT = re.compile(r"<!--\s*media:\s*([a-zA-Z0-9_-]+)\s*-->", re.IGNORECASE)


def extract_media_codes(text: str) -> list[str]:
    """按出现顺序去重返回 media code 列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for rx in (_RE_MEDIA_WIKILINK, _RE_MEDIA_HTML_COMMENT):
        for m in rx.finditer(text or ""):
            c = m.group(1).strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
    return out


_CODE_TOKEN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def merge_extracted_and_extra_codes(text: str, extra: list[str]) -> list[str]:
    """正文解析出的 code 在前，再追加 extra 中尚未出现的合法 code（如召回 merged_media 中的 code）。"""
    out = extract_media_codes(text or "")
    seen = set(out)
    for raw in extra or []:
        c = (raw or "").strip()
        if not c or c in seen:
            continue
        if not _CODE_TOKEN.match(c):
            continue
        seen.add(c)
        out.append(c)
    return out


def strip_media_tags(text: str) -> str:
    """去掉 wiki / HTML 注释形式的多媒体占位，避免进入 LLM 注入正文。"""
    s = text or ""
    s = _RE_MEDIA_WIKILINK.sub("", s)
    s = _RE_MEDIA_HTML_COMMENT.sub("", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
