"""多媒体资源：manifest、落盘路径、配额、反向索引（wiki → code）。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import zipfile
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
EXPORT_JSON_NAME = "pathy_media_export.json"
_SUBDIR_SEG = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

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
    ".apk": "application/vnd.android.package-archive",
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


def _validate_subdir_segments(parts: list[str], *, field: str) -> None:
    if len(parts) > 24:
        raise HTTPException(status_code=400, detail=f"{field} 层级过多（最多 24 段）")
    for p in parts:
        if p == "..":
            raise HTTPException(status_code=400, detail=f"{field} 不允许包含 '..'")
        if not _SUBDIR_SEG.match(p):
            raise HTTPException(
                status_code=400,
                detail=f"{field} 每段仅允许字母、数字、下划线、连字符，且非空",
            )


def normalize_media_objects_subdir(raw: str) -> str:
    """media/objects 下的可选子路径，如 batch2024/handbook；空表示默认 objects/ab/cd/…"""
    s = (raw or "").strip().replace("\\", "/").strip("/")
    if not s:
        return ""
    parts = [p for p in s.split("/") if p and p != "."]
    _validate_subdir_segments(parts, field="target_dir")
    return "/".join(parts)


def normalize_media_subdir(raw: str) -> str:
    """media/ 下的可选子路径（含首段，可以是 objects 或任意已存在子目录），如 HSYJY、objects/foo。"""
    s = (raw or "").strip().replace("\\", "/").strip("/")
    if not s:
        return ""
    parts = [p for p in s.split("/") if p and p != "."]
    _validate_subdir_segments(parts, field="target_folder")
    return "/".join(parts)


def _object_rel_path(code: str, ext_with_dot: str, *, objects_subdir: str = "") -> str:
    """旧入口：在 media/objects/ 之下拼路径。"""
    a, b = code[:2], code[2:4] if len(code) > 2 else "xx"
    safe_ext = ext_with_dot if ext_with_dot.startswith(".") else f".{ext_with_dot}"
    tail = f"{a}/{b}/{code}{safe_ext}"
    if objects_subdir:
        return f"{OBJECTS_DIR}/{objects_subdir}/{tail}"
    return f"{OBJECTS_DIR}/{tail}"


def _object_rel_path_in_media(code: str, ext_with_dot: str, *, media_subdir: str) -> str:
    """新入口：在 media/<media_subdir>/ 之下拼路径；media_subdir 为空时退回到 objects/ 默认分层。"""
    a, b = code[:2], code[2:4] if len(code) > 2 else "xx"
    safe_ext = ext_with_dot if ext_with_dot.startswith(".") else f".{ext_with_dot}"
    tail = f"{a}/{b}/{code}{safe_ext}"
    if media_subdir:
        return f"{media_subdir}/{tail}"
    return f"{OBJECTS_DIR}/{tail}"


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


def delete_media_codes(data_root: Path, codes: list[str]) -> list[dict[str, Any]]:
    """
    从 manifest 移除登记；若 rel_storage 无其他条目引用则删除磁盘对象文件。
    同时从 .pathy/media_backrefs.json 移除对应 code。
    返回每行：code, status(deleted|not_found|skipped_invalid), removed_file, detail。
    """
    ensure_media_tree(data_root)
    man = _load_manifest(data_root)
    items_raw = man.get("items")
    if not isinstance(items_raw, dict):
        items_raw = {}
    items: dict[str, Any] = items_raw
    man["items"] = items

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in codes:
        try:
            c = validate_code(raw)
        except HTTPException:
            results.append(
                {
                    "code": str(raw).strip() or "?",
                    "status": "skipped_invalid",
                    "removed_file": False,
                    "detail": "无效的 media code",
                }
            )
            continue
        if c in seen:
            continue
        seen.add(c)
        ordered.append(c)

    br = _load_backrefs(data_root)
    any_deleted = False

    for code in ordered:
        it = items.get(code)
        if not isinstance(it, dict):
            results.append(
                {"code": code, "status": "not_found", "removed_file": False, "detail": "未在 manifest 中登记"}
            )
            continue

        rel = str(it.get("rel_storage") or "").replace("\\", "/")
        del items[code]
        br.pop(code, None)
        any_deleted = True

        removed_file = False
        if rel:
            still_used = any(
                isinstance(v, dict)
                and str(v.get("rel_storage") or "").replace("\\", "/") == rel
                for v in items.values()
            )
            if not still_used:
                try:
                    path = _abs_object_path(data_root, rel)
                    if path.is_file():
                        path.unlink()
                        removed_file = True
                except OSError:
                    pass

        results.append(
            {
                "code": code,
                "status": "deleted",
                "removed_file": removed_file,
                "detail": "已移除登记" + ("；已删除对象文件" if removed_file else ""),
            }
        )

    if any_deleted:
        _save_manifest(data_root, man)
        _save_backrefs(data_root, br)

    return results


def _folder_from_rel_storage(rel_storage: str) -> str:
    """从 rel_storage（如 'objects/ab/cd/xxx.png' 或 'HSYJY/ab/cd/xxx.png'）反推所属 media 子目录。

    规则：去掉尾部三段（aa/bb/<code>.<ext>），剩余即所属子目录；不足三段则返回空串。
    """
    s = (rel_storage or "").replace("\\", "/").strip("/")
    if not s:
        return ""
    parts = s.split("/")
    if len(parts) <= 3:
        return ""
    return "/".join(parts[:-3])


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
        rel = str(v.get("rel_storage") or "").replace("\\", "/")
        rows.append(
            {
                "code": code,
                "mime": str(v.get("mime") or ""),
                "size": _item_size(v),
                "title": v.get("title"),
                "original_name": v.get("original_name"),
                "created_at": str(v.get("created_at") or ""),
                "sha256": str(v.get("sha256") or ""),
                "folder": _folder_from_rel_storage(rel),
            }
        )
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    total = _total_bytes_used(raw_items)
    return rows, len(rows), total


def _zip_arcname_norm(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def _find_code_by_sha256(items: dict[str, Any], h: str) -> Optional[str]:
    for cid, meta in items.items():
        if isinstance(meta, dict) and isinstance(cid, str) and str(meta.get("sha256") or "") == h:
            return cid
    return None


def build_media_export_zip_bytes(data_root: Path, codes: list[str]) -> tuple[str, bytes]:
    """多选导出：ZIP 内含 pathy_media_export.json（manifest 全字段 + 反向引用）与 objects 相对路径二进制。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in codes:
        c = validate_code(raw)
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    if not ordered:
        raise HTTPException(status_code=400, detail="请至少选择一个有效的 media code")

    doc: dict[str, Any] = {
        "version": 1,
        "exported_at": _now_iso(),
        "codes_order": [],
        "items": {},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for code in ordered:
            item = dict(get_media_item(data_root, code))
            rel_old = str(item.get("rel_storage") or "")
            if not rel_old:
                raise HTTPException(status_code=500, detail=f"{code}: manifest 缺少 rel_storage")
            path_bin, _ = get_media_file_path(data_root, code)
            arc = _zip_arcname_norm(rel_old)
            backs = get_backrefs_for_code(data_root, code)
            doc["codes_order"].append(code)
            doc["items"][code] = {
                "manifest": item,
                "backrefs": [{"wiki_path": e.wiki_path, "heading_path": e.heading_path} for e in backs],
            }
            zf.write(path_bin, arc)
        zf.writestr(
            EXPORT_JSON_NAME,
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        )
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"pathy-media-export-{ts}.zip"
    return fname, buf.getvalue()


def import_media_zip(
    data_root: Path,
    zip_bytes: bytes,
    target_subdir: str,
    *,
    max_upload_bytes: int,
    total_quota_bytes: int,
    max_zip_bytes: int,
) -> tuple[list[dict[str, Any]], str]:
    """
    解析本服务导出的 ZIP，将媒体登记进 manifest；二进制落在 media/<target_subdir>/…。

    target_subdir 为相对 media/ 层根的完整子路径（含首段；空字符串表示默认 objects/aa/bb/…）。
    调用方负责把 `target_dir`（objects/ 下）或 `target_folder`（media/ 下）翻译成 target_subdir 后传入。
    返回 (行结果列表, 摘要 message)。
    """
    if len(zip_bytes) > max_zip_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"zip 超过上限 {max_zip_bytes} 字节",
        )
    sub = normalize_media_subdir(target_subdir)
    ensure_media_tree(data_root)
    man = _load_manifest(data_root)
    items_raw = man.get("items")
    if not isinstance(items_raw, dict):
        items_raw = {}
    items: dict[str, Any] = items_raw
    man["items"] = items

    try:
        zf_ctx = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise HTTPException(status_code=400, detail="无效的 zip 文件") from e

    rows: list[dict[str, Any]] = []
    backrefs_delta: dict[str, list[dict[str, str]]] = {}
    used = _total_bytes_used(items)
    extra_bytes = 0

    with zf_ctx as zf:
        name_map = {_zip_arcname_norm(n): n for n in zf.namelist()}
        export_key = name_map.get(EXPORT_JSON_NAME)
        if export_key is None:
            for k, v in name_map.items():
                if k == EXPORT_JSON_NAME or k.endswith("/" + EXPORT_JSON_NAME):
                    export_key = v
                    break
        if export_key is None:
            raise HTTPException(
                status_code=400,
                detail=f"zip 中缺少 {EXPORT_JSON_NAME}（请使用本服务导出的多媒体包）",
            )
        try:
            export_doc = json.loads(zf.read(export_key).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(status_code=400, detail="导出描述 JSON 损坏或无法解码") from e

        if not isinstance(export_doc, dict):
            raise HTTPException(status_code=400, detail="导出描述格式错误")
        raw_items = export_doc.get("items")
        if not isinstance(raw_items, dict):
            raise HTTPException(status_code=400, detail="导出描述缺少 items")
        codes_order = export_doc.get("codes_order")
        if not isinstance(codes_order, list) or not codes_order:
            codes_order = list(raw_items.keys())

        allowed_bin: set[str] = set()
        for _c, wrap in raw_items.items():
            if not isinstance(wrap, dict):
                continue
            m = wrap.get("manifest")
            if isinstance(m, dict):
                rel = str(m.get("rel_storage") or "")
                if rel:
                    allowed_bin.add(_zip_arcname_norm(rel))

        for source_code in codes_order:
            if not isinstance(source_code, str):
                continue
            try:
                source_code_v = validate_code(source_code)
            except HTTPException:
                rows.append(
                    {
                        "source_code": str(source_code),
                        "result_code": "",
                        "status": "error",
                        "detail": "无效的 source code",
                    }
                )
                continue
            wrap = raw_items.get(source_code_v)
            if not isinstance(wrap, dict):
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": "导出 items 中无此 code",
                    }
                )
                continue
            m = wrap.get("manifest")
            if not isinstance(m, dict):
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": "缺少 manifest",
                    }
                )
                continue
            rel_in_zip = _zip_arcname_norm(str(m.get("rel_storage") or ""))
            if not rel_in_zip or rel_in_zip not in allowed_bin:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": "manifest.rel_storage 无效",
                    }
                )
                continue
            zip_inner = name_map.get(rel_in_zip)
            if not zip_inner:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": f"zip 内缺少文件 {rel_in_zip}",
                    }
                )
                continue
            try:
                data = zf.read(zip_inner)
            except OSError as e:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": f"读取 zip 内文件失败: {e}",
                    }
                )
                continue
            exp_sha = str(m.get("sha256") or "")
            got_sha = _sha256_bytes(data)
            if exp_sha and exp_sha != got_sha:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": "sha256 与导出描述不一致",
                    }
                )
                continue
            if len(data) > max_upload_bytes:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": f"单文件超过上限 {max_upload_bytes} 字节",
                    }
                )
                continue

            existing_by_sha = _find_code_by_sha256(items, got_sha)
            if existing_by_sha is not None:
                rc = existing_by_sha
                if rc == source_code_v:
                    rows.append(
                        {
                            "source_code": source_code_v,
                            "result_code": rc,
                            "status": "skipped_identical",
                            "detail": "已登记且内容相同",
                        }
                    )
                else:
                    rows.append(
                        {
                            "source_code": source_code_v,
                            "result_code": rc,
                            "status": "deduplicated_existing",
                            "detail": "内容已存在于其他 code，未重复落盘",
                        }
                    )
                br = wrap.get("backrefs")
                if isinstance(br, list) and rc:
                    backrefs_delta.setdefault(rc, []).extend(
                        r
                        for r in br
                        if isinstance(r, dict) and isinstance(r.get("wiki_path"), str)
                    )
                continue

            cur = items.get(source_code_v)
            if isinstance(cur, dict) and str(cur.get("sha256") or "") == got_sha:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": source_code_v,
                        "status": "skipped_identical",
                        "detail": "已登记且内容相同",
                    }
                )
                br = wrap.get("backrefs")
                if isinstance(br, list):
                    backrefs_delta.setdefault(source_code_v, []).extend(
                        r
                        for r in br
                        if isinstance(r, dict) and isinstance(r.get("wiki_path"), str)
                    )
                continue

            if isinstance(cur, dict) and str(cur.get("sha256") or "") != got_sha:
                new_code = secrets.token_hex(12)
                result_code = new_code
                detail = "目标环境已有同 code 不同内容，已分配新 code"
            else:
                new_code = None
                result_code = source_code_v
                detail = ""

            target_code = result_code
            ext = _normalize_ext(str(m.get("original_name") or rel_in_zip))
            if ext not in _EXT_WHITELIST:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": f"不允许的扩展名 {ext or '(空)'}",
                    }
                )
                continue

            if used + extra_bytes + len(data) > total_quota_bytes:
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": "",
                        "status": "error",
                        "detail": f"超过媒体总配额 {total_quota_bytes} 字节",
                    }
                )
                continue

            rel_new = _object_rel_path_in_media(target_code, ext, media_subdir=sub)
            path_new = _abs_object_path(data_root, rel_new)
            path_new.parent.mkdir(parents=True, exist_ok=True)
            path_new.write_bytes(data)
            extra_bytes += len(data)

            new_row: dict[str, Any] = {
                "code": target_code,
                "sha256": got_sha,
                "mime": str(m.get("mime") or resolve_mime(ext, None)),
                "size": len(data),
                "rel_storage": rel_new.replace("\\", "/"),
                "title": m.get("title"),
                "original_name": m.get("original_name"),
                "created_at": str(m.get("created_at") or _now_iso()),
            }
            if new_code:
                items[target_code] = new_row
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": target_code,
                        "status": "remapped",
                        "detail": detail,
                    }
                )
            else:
                items[target_code] = new_row
                rows.append(
                    {
                        "source_code": source_code_v,
                        "result_code": target_code,
                        "status": "imported",
                        "detail": "已写入并登记",
                    }
                )

            br = wrap.get("backrefs")
            if isinstance(br, list):
                backrefs_delta.setdefault(target_code, []).extend(
                    r
                    for r in br
                    if isinstance(r, dict) and isinstance(r.get("wiki_path"), str)
                )

    dirty_manifest = any(r.get("status") in ("imported", "remapped") for r in rows)
    if dirty_manifest:
        _save_manifest(data_root, man)
    if backrefs_delta:
        mp = _load_backrefs(data_root)
        for code, add_rows in backrefs_delta.items():
            cur_list = list(mp.get(code) or [])
            seen_k: set[tuple[str, str]] = set()
            for row in cur_list:
                if isinstance(row, dict) and isinstance(row.get("wiki_path"), str):
                    seen_k.add((row["wiki_path"], str(row.get("heading_path") or "")))
            for r in add_rows:
                wp = str(r.get("wiki_path") or "")
                hp = str(r.get("heading_path") or "")
                key = (wp, hp)
                if not wp or key in seen_k:
                    continue
                seen_k.add(key)
                cur_list.append({"wiki_path": wp, "heading_path": hp})
            mp[code] = cur_list
        _save_backrefs(data_root, mp)

    n_ok = sum(1 for r in rows if r.get("status") in ("imported", "remapped"))
    n_skip = sum(1 for r in rows if r.get("status") in ("skipped_identical", "deduplicated_existing"))
    n_err = sum(1 for r in rows if r.get("status") == "error")
    msg = f"完成：新增 {n_ok}，跳过/去重 {n_skip}，失败 {n_err}；对象子目录={sub or '(默认)'}"
    return rows, msg
