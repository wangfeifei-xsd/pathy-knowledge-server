"""多媒体资源：manifest、落盘路径、配额、反向索引（wiki → code）。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from app.models.schemas import LayerName, MediaBackrefEntry, MediaRef, MediaResolvedItem
from app.services import storage
from app.services.media_codes import extract_media_codes

MANIFEST_NAME = "manifest.json"
OBJECTS_DIR = "objects"
BACKREFS_FILE = "media_backrefs.json"

# 扩展名（小写）→ 用于校验；MIME 优先用上传声明，否则由此映射
_EXT_WHITELIST: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def media_layer_root(data_root: Path) -> Path:
    return storage.layer_root(data_root, LayerName.media)


def _manifest_path(data_root: Path) -> Path:
    p = media_layer_root(data_root) / MANIFEST_NAME
    return p


def _backrefs_path(data_root: Path) -> Path:
    p = data_root / ".pathy"
    p.mkdir(parents=True, exist_ok=True)
    return p / BACKREFS_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_media_tree(data_root: Path) -> Path:
    root = media_layer_root(data_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / OBJECTS_DIR).mkdir(parents=True, exist_ok=True)
    return root


def _load_manifest(data_root: Path) -> dict[str, Any]:
    p = _manifest_path(data_root)
    if not p.is_file():
        return {"version": 1, "items": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "items": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "items": {}}
    raw.setdefault("version", 1)
    raw.setdefault("items", {})
    if not isinstance(raw["items"], dict):
        raw["items"] = {}
    return raw


def _save_manifest(data_root: Path, data: dict[str, Any]) -> None:
    ensure_media_tree(data_root)
    _manifest_path(data_root).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _total_bytes_used(items: dict[str, Any]) -> int:
    n = 0
    for v in items.values():
        if isinstance(v, dict):
            try:
                n += int(v.get("size") or 0)
            except (TypeError, ValueError):
                pass
    return n


def _normalize_ext(filename: str) -> str:
    lower = (filename or "").lower().strip()
    if "." not in lower:
        return ""
    return Path(lower).suffix


def resolve_mime(ext: str, upload_content_type: Optional[str]) -> str:
    ext = ext.lower()
    fallback = _EXT_WHITELIST.get(ext, "application/octet-stream")
    ct = (upload_content_type or "").split(";")[0].strip().lower()
    if not ct or ct == "application/octet-stream":
        return fallback
    return ct


def _object_rel_path(code: str, ext_with_dot: str) -> str:
    # objects/ab/cd/{code}.ext
    a, b = code[:2], code[2:4] if len(code) > 2 else "xx"
    safe_ext = ext_with_dot if ext_with_dot.startswith(".") else f".{ext_with_dot}"
    return f"{OBJECTS_DIR}/{a}/{b}/{code}{safe_ext}"


def _abs_object_path(data_root: Path, rel: str) -> Path:
    base = media_layer_root(data_root)
    return storage.safe_resolve_under(base, rel)


def validate_code(code: str) -> str:
    c = (code or "").strip()
    if not c or len(c) > 128:
        raise HTTPException(status_code=400, detail="无效的 media code")
    for ch in c:
        if ch.isalnum() or ch in ("_", "-"):
            continue
        raise HTTPException(status_code=400, detail="无效的 media code")
    return c


def get_media_item(data_root: Path, code: str) -> dict[str, Any]:
    code = validate_code(code)
    man = _load_manifest(data_root)
    items = man.get("items") or {}
    it = items.get(code)
    if not isinstance(it, dict):
        raise HTTPException(status_code=404, detail="媒体不存在")
    return it


def get_media_file_path(data_root: Path, code: str) -> tuple[Path, dict[str, Any]]:
    it = get_media_item(data_root, code)
    rel = str(it.get("rel_storage") or "")
    if not rel:
        raise HTTPException(status_code=500, detail="manifest 缺少 rel_storage")
    path = _abs_object_path(data_root, rel)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="媒体文件缺失")
    return path, it


def register_upload(
    data_root: Path,
    *,
    filename: str,
    data: bytes,
    upload_content_type: Optional[str],
    title: Optional[str],
    max_upload_bytes: int,
    total_quota_bytes: int,
) -> tuple[str, bool]:
    """写入对象存储并更新 manifest。若 sha256 已存在则返回已有 code（去重）。"""
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过单文件上限 {max_upload_bytes} 字节",
        )
    ext = _normalize_ext(filename)
    if ext not in _EXT_WHITELIST:
        raise HTTPException(
            status_code=400,
            detail=f"不允许的扩展名；允许：{', '.join(sorted(_EXT_WHITELIST))}",
        )
    ensure_media_tree(data_root)
    man = _load_manifest(data_root)
    items: dict[str, Any] = man.get("items") or {}
    h = _sha256_bytes(data)
    for cid, meta in items.items():
        if isinstance(meta, dict) and meta.get("sha256") == h:
            return str(cid), True

    used = _total_bytes_used(items)
    if used + len(data) > total_quota_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"超过媒体总配额 {total_quota_bytes} 字节（已用 {used}）",
        )

    code = secrets.token_hex(12)
    rel = _object_rel_path(code, ext)
    path = _abs_object_path(data_root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    mime = resolve_mime(ext, upload_content_type)
    items[code] = {
        "code": code,
        "sha256": h,
        "mime": mime,
        "size": len(data),
        "rel_storage": rel.replace("\\", "/"),
        "title": (title or "").strip() or None,
        "original_name": Path(filename).name,
        "created_at": _now_iso(),
    }
    man["items"] = items
    _save_manifest(data_root, man)
    return code, False


def _item_size(item: dict[str, Any]) -> int:
    s = item.get("size")
    if isinstance(s, int):
        return s
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def media_ref_from_item(code: str, item: dict[str, Any]) -> MediaRef:
    return MediaRef(
        code=code,
        mime=str(item.get("mime") or "application/octet-stream"),
        title=item.get("title"),
        size=_item_size(item),
    )


def enrich_codes_to_refs(data_root: Path, codes: list[str]) -> list[MediaRef]:
    if not codes:
        return []
    man = _load_manifest(data_root)
    items = man.get("items") or {}
    out: list[MediaRef] = []
    for c in codes:
        it = items.get(c)
        if isinstance(it, dict):
            out.append(media_ref_from_item(c, it))
    return out


def resolve_media_codes_metadata(data_root: Path, codes: list[str]) -> list[MediaResolvedItem]:
    """按 codes 顺序返回 manifest 登记情况（未登记仍返回一行 registered=false）。"""
    if not codes:
        return []
    man = _load_manifest(data_root)
    items = man.get("items") or {}
    out: list[MediaResolvedItem] = []
    for code in codes:
        try:
            code_v = validate_code(code)
        except HTTPException:
            continue
        it = items.get(code_v)
        if isinstance(it, dict):
            out.append(
                MediaResolvedItem(
                    code=code_v,
                    registered=True,
                    mime=str(it.get("mime") or ""),
                    size=_item_size(it),
                    title=it.get("title"),
                    original_name=it.get("original_name"),
                    created_at=str(it.get("created_at") or ""),
                    sha256=str(it.get("sha256") or ""),
                )
            )
        else:
            out.append(MediaResolvedItem(code=code_v, registered=False))
    return out


def _load_backrefs(data_root: Path) -> dict[str, list[dict[str, str]]]:
    p = _backrefs_path(data_root)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, list):
            continue
        rows: list[dict[str, str]] = []
        for row in v:
            if isinstance(row, dict) and isinstance(row.get("wiki_path"), str):
                rows.append(
                    {
                        "wiki_path": row["wiki_path"],
                        "heading_path": str(row.get("heading_path") or ""),
                    }
                )
        out[k] = rows
    return out


def _save_backrefs(data_root: Path, data: dict[str, list[dict[str, str]]]) -> None:
    _backrefs_path(data_root).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def reindex_media_backrefs(
    data_root: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    chunk_max_chars: int,
) -> tuple[int, int]:
    """扫描 wiki，重建 code → 出现位置列表；写入 .pathy/media_backrefs.json。"""
    from app.services.dialogue_recall import _collect_wiki_pairs, _wiki_indexed_chunks

    storage.ensure_layer_tree(data_root)
    ensure_media_tree(data_root)
    pairs = _collect_wiki_pairs(data_root, "", max_files, max_file_bytes)
    inv: dict[str, list[tuple[str, str]]] = {}
    for rel, full in pairs:
        for ch in _wiki_indexed_chunks(rel, full, chunk_max_chars):
            for code in extract_media_codes(ch.body):
                inv.setdefault(code, []).append((ch.rel, ch.heading_path))

    # 去重 (wiki_path, heading_path)
    serial: dict[str, list[dict[str, str]]] = {}
    for code, places in inv.items():
        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, str]] = []
        for wp, hp in places:
            key = (wp, hp)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"wiki_path": wp, "heading_path": hp})
        serial[code] = rows

    _save_backrefs(data_root, serial)
    n_codes = len(serial)
    n_refs = sum(len(v) for v in serial.values())
    return n_codes, n_refs


def get_backrefs_for_code(data_root: Path, code: str) -> list[MediaBackrefEntry]:
    validate_code(code)
    mp = _load_backrefs(data_root)
    rows = mp.get(code) or []
    return [MediaBackrefEntry(wiki_path=r["wiki_path"], heading_path=r.get("heading_path") or "") for r in rows]


def list_media_manifest_summary(data_root: Path) -> tuple[int, int]:
    man = _load_manifest(data_root)
    items = man.get("items") or {}
    n = len(items) if isinstance(items, dict) else 0
    used = _total_bytes_used(items if isinstance(items, dict) else {})
    return n, used


def list_manifest_items(data_root: Path) -> tuple[list[dict[str, Any]], int, int]:
    """返回 (条目 dict 列表, 条数, 字节合计)，按 created_at 新到旧排序。"""
    man = _load_manifest(data_root)
    raw_items = man.get("items") or {}
    if not isinstance(raw_items, dict):
        return [], 0, 0
    rows: list[dict[str, Any]] = []
    for code, v in raw_items.items():
        if not isinstance(v, dict) or not isinstance(code, str):
            continue
        rows.append(
            {
                "code": code,
                "mime": str(v.get("mime") or ""),
                "size": _item_size(v),
                "title": v.get("title"),
                "original_name": v.get("original_name"),
                "created_at": str(v.get("created_at") or ""),
                "sha256": str(v.get("sha256") or ""),
            }
        )
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    total = _total_bytes_used(raw_items)
    return rows, len(rows), total
