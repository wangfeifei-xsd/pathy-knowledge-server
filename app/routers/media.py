"""多媒体资源：上传、按 code 下载（支持 Range）、反向索引。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.models.schemas import (
    MediaBackrefsResponse,
    MediaListItem,
    MediaListResponse,
    MediaReindexBackrefsResponse,
    MediaResolveFromTextRequest,
    MediaResolveFromTextResponse,
    MediaUploadResponse,
)
from app.services.media_codes import merge_extracted_and_extra_codes
from app.services.media_store import (
    ensure_media_tree,
    get_backrefs_for_code,
    get_media_file_path,
    get_media_item,
    list_manifest_items,
    list_media_manifest_summary,
    reindex_media_backrefs,
    register_upload,
    resolve_media_codes_metadata,
)

router = APIRouter(prefix="/api/v1/media", tags=["多媒体"])


@router.post(
    "/upload",
    response_model=MediaUploadResponse,
    summary="上传图片/视频",
    description="写入 data/media/objects/… 并登记 manifest；同 sha256 自动去重返回已有 code。",
)
async def upload_media(
    file: UploadFile = File(..., description="图片或视频文件"),
    title: Optional[str] = Form(None, description="可选标题，写入 manifest"),
    settings: Settings = Depends(get_settings),
) -> MediaUploadResponse:
    data_root = settings.data_root.resolve()
    ensure_media_tree(data_root)
    raw = await file.read()
    name = (file.filename or "upload.bin").strip() or "upload.bin"
    code, dedup = register_upload(
        data_root,
        filename=name,
        data=raw,
        upload_content_type=file.content_type,
        title=title,
        max_upload_bytes=settings.media_max_upload_bytes,
        total_quota_bytes=settings.media_total_quota_bytes,
    )
    meta = get_media_item(data_root, code)
    return MediaUploadResponse(
        code=code,
        deduplicated=dedup,
        mime=str(meta.get("mime") or "application/octet-stream"),
        size=int(meta.get("size") or 0),
        message="已去重返回已有条目" if dedup else "上传成功",
    )


@router.post(
    "/reindex-backrefs",
    response_model=MediaReindexBackrefsResponse,
    summary="重建媒体反向索引",
    description="扫描 wiki 中 ![[MEDIA:code]] / <!-- media:code -->，写入 .pathy/media_backrefs.json。",
)
def post_reindex_media_backrefs(settings: Settings = Depends(get_settings)) -> MediaReindexBackrefsResponse:
    data_root = settings.data_root.resolve()
    n_codes, n_rows = reindex_media_backrefs(
        data_root,
        max_files=settings.media_reindex_max_files,
        max_file_bytes=settings.max_file_bytes,
        chunk_max_chars=1200,
    )
    return MediaReindexBackrefsResponse(
        codes_with_refs=n_codes,
        total_ref_rows=n_rows,
        message=f"已扫描 wiki 至多 {settings.media_reindex_max_files} 个文件，索引 {n_codes} 个 code",
    )


@router.get(
    "/items",
    response_model=MediaListResponse,
    summary="列出已登记媒体",
    description="读取 media/manifest.json；需在通配路由 /{code} 之前注册，避免 code=items 被误匹配。",
)
def get_media_items(settings: Settings = Depends(get_settings)) -> MediaListResponse:
    data_root = settings.data_root.resolve()
    rows, n, total = list_manifest_items(data_root)
    items = [MediaListItem.model_validate(r) for r in rows]
    return MediaListResponse(items=items, count=n, bytes_total=total)


@router.post(
    "/resolve-from-text",
    response_model=MediaResolveFromTextResponse,
    summary="解析正文中的多媒体标签并查询登记信息",
    description=(
        "从 text 解析 `![[MEDIA:code]]` 与 `<!-- media:code -->`，并与 body.codes（如召回结果 merged_media 中的 code）"
        "按出现顺序合并去重；返回每条 code 是否在 manifest 中及元数据。二进制流仍用 GET /api/v1/media/{code}。"
    ),
)
def post_resolve_media_from_text(
    body: MediaResolveFromTextRequest,
    settings: Settings = Depends(get_settings),
) -> MediaResolveFromTextResponse:
    data_root = settings.data_root.resolve()
    merged = merge_extracted_and_extra_codes(body.text, body.codes)
    items = resolve_media_codes_metadata(data_root, merged)
    return MediaResolveFromTextResponse(codes=[i.code for i in items], items=items)


@router.get(
    "/meta/summary",
    summary="媒体层用量摘要（调试用）",
    description="返回 manifest 中条目数与登记总字节（不含孤儿文件）。",
)
def media_summary(settings: Settings = Depends(get_settings)) -> dict:
    data_root = settings.data_root.resolve()
    n, used = list_media_manifest_summary(data_root)
    return {"count": n, "bytes_registered": used}


@router.get(
    "/{code}/backrefs",
    response_model=MediaBackrefsResponse,
    summary="按媒体 code 查绑定的 wiki 位置",
    description="依赖最近一次 reindex-backrefs；若为空请先 POST /reindex-backrefs。",
)
def get_media_backrefs(code: str, settings: Settings = Depends(get_settings)) -> MediaBackrefsResponse:
    data_root = settings.data_root.resolve()
    entries = get_backrefs_for_code(data_root, code)
    msg = ""
    if not entries:
        msg = "无记录：请先 POST /api/v1/media/reindex-backrefs，或 wiki 中未引用该 code。"
    return MediaBackrefsResponse(code=code, entries=entries, message=msg)


@router.get(
    "/{code}",
    summary="按 code 获取媒体文件",
    description="返回原始字节流；图片/视频设置 Content-Type；视频支持 Range 分段（Starlette FileResponse）。",
    response_class=FileResponse,
)
def get_media_binary(code: str, settings: Settings = Depends(get_settings)) -> FileResponse:
    data_root = settings.data_root.resolve()
    path, meta = get_media_file_path(data_root, code)
    mime = str(meta.get("mime") or "application/octet-stream")
    fname = str(meta.get("original_name") or code)
    return FileResponse(
        path,
        media_type=mime,
        filename=fname,
        content_disposition_type="inline",
    )
