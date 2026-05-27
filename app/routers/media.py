"""多媒体资源：上传、按 code 下载（支持 Range）、反向索引。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.config import Settings, get_settings
from app.models.schemas import (
    MediaBackrefsResponse,
    MediaDeleteBatchRequest,
    MediaDeleteBatchResponse,
    MediaDeleteBatchRow,
    MediaDeleteOneResponse,
    MediaExportZipRequest,
    MediaImportZipResponse,
    MediaImportZipRow,
    MediaListItem,
    MediaListResponse,
    MediaReindexBackrefsResponse,
    MediaResolveFromTextRequest,
    MediaResolveFromTextResponse,
    MediaUploadResponse,
)
from app.services.media_codes import merge_extracted_and_extra_codes
from app.services.media_store import (
    build_media_export_zip_bytes,
    delete_media_codes,
    ensure_media_tree,
    get_backrefs_for_code,
    get_media_file_path,
    get_media_item,
    import_media_zip,
    list_manifest_items,
    list_media_manifest_summary,
    normalize_media_objects_subdir,
    normalize_media_subdir,
    reindex_media_backrefs,
    register_upload,
    resolve_media_codes_metadata,
    validate_code,
)

router = APIRouter(prefix="/api/v1/media", tags=["多媒体"])


@router.post(
    "/upload",
    response_model=MediaUploadResponse,
    summary="上传图片/视频/APK",
    description=(
        "写入 data/media/ 并登记 manifest；可选 target_folder 指定 media/ 下子目录。"
        "空则走默认 objects/aa/bb/…；同 sha256 自动去重返回已有 code。"
    ),
)
async def upload_media(
    file: UploadFile = File(..., description="图片、视频或 APK 文件"),
    title: Optional[str] = Form(None, description="可选标题，写入 manifest"),
    target_folder: str = Form(
        "",
        description="可选，media/ 层下子目录（如 HSYJY、objects/batch2024）；空表示默认 objects/aa/bb/…",
    ),
    settings: Settings = Depends(get_settings),
) -> MediaUploadResponse:
    data_root = settings.data_root.resolve()
    ensure_media_tree(data_root)
    raw = await file.read()
    name = (file.filename or "upload.bin").strip() or "upload.bin"
    target_folder_norm = normalize_media_subdir(target_folder)
    code, dedup = register_upload(
        data_root,
        filename=name,
        data=raw,
        upload_content_type=file.content_type,
        title=title,
        max_upload_bytes=settings.media_max_upload_bytes,
        total_quota_bytes=settings.media_total_quota_bytes,
        media_subdir=target_folder_norm,
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
    "/export-zip",
    summary="多选导出为 ZIP",
    description=(
        "请求体提供 codes；返回 application/zip，内含 pathy_media_export.json（manifest 全量字段 + wiki 反向引用）"
        " 与各资源二进制（与 manifest.rel_storage 一致的相对路径）。"
    ),
    response_class=Response,
)
def post_export_media_zip(
    body: MediaExportZipRequest,
    settings: Settings = Depends(get_settings),
) -> Response:
    data_root = settings.data_root.resolve()
    fname, data = build_media_export_zip_bytes(data_root, body.codes)
    ascii_fallback = fname.encode("ascii", "replace").decode("ascii")
    disp = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(fname)}"
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": disp},
    )


@router.post(
    "/import-zip",
    response_model=MediaImportZipResponse,
    summary="从导出 ZIP 导入",
    description=(
        "multipart：file 为本服务导出的 zip；target_folder 为可选的 media/ 下子目录"
        "（含首段，可填 'objects'、'HSYJY' 或新建的任意层级如 'albums/2025'），用于归类落盘路径。"
        "留空则与上传默认一致为 objects/ab/cd/…。\n"
        "兼容旧字段：target_dir 表示 media/objects/ 下子目录（不含 'objects' 段）；"
        "若同时提供 target_dir 与 target_folder，将以 target_folder 为准并在响应 warning 中标注。\n"
        "若 code 冲突且内容不同将分配新 code；内容去重则合并到已有 code。"
        "导出包中的 backrefs 会尝试合并进 .pathy/media_backrefs.json。"
    ),
)
async def post_import_media_zip(
    file: UploadFile = File(..., description="pathy 导出的多媒体 zip"),
    target_dir: str = Form("", description="兼容字段：media/objects 下子目录，段间用 /"),
    target_folder: str = Form(
        "",
        description="media/ 下任意子目录（含首段，可为 'objects' 或自定义），段间用 /；留空默认 objects/ab/cd/…",
    ),
    settings: Settings = Depends(get_settings),
) -> MediaImportZipResponse:
    data_root = settings.data_root.resolve()

    target_dir_norm = normalize_media_objects_subdir(target_dir)
    target_folder_norm = normalize_media_subdir(target_folder)

    warning = ""
    if target_folder_norm and target_dir_norm:
        warning = "同时提供了 target_dir 与 target_folder，已以 target_folder 为准；target_dir 被忽略"
        effective_subdir = target_folder_norm
    elif target_folder_norm:
        effective_subdir = target_folder_norm
    elif target_dir_norm:
        effective_subdir = f"objects/{target_dir_norm}"
    else:
        effective_subdir = ""

    raw = await file.read()
    max_zip = max(
        settings.media_max_upload_bytes * 200,
        min(settings.media_total_quota_bytes, 2_147_483_648),
    )
    rows_raw, msg = import_media_zip(
        data_root,
        raw,
        effective_subdir,
        max_upload_bytes=settings.media_max_upload_bytes,
        total_quota_bytes=settings.media_total_quota_bytes,
        max_zip_bytes=max_zip,
    )
    rows = [MediaImportZipRow.model_validate(r) for r in rows_raw]
    return MediaImportZipResponse(
        results=rows,
        message=msg,
        target_dir_normalized=target_dir_norm,
        target_folder_normalized=effective_subdir,
        warning=warning,
    )


@router.post(
    "/batch-delete",
    response_model=MediaDeleteBatchResponse,
    summary="批量删除媒体",
    description=(
        "从 manifest 移除多条登记；若某 rel_storage 无其他条目引用则删除对应对象文件。"
        "并从 .pathy/media_backrefs.json 移除这些 code 的反向引用记录。"
    ),
)
def post_media_batch_delete(
    body: MediaDeleteBatchRequest,
    settings: Settings = Depends(get_settings),
) -> MediaDeleteBatchResponse:
    data_root = settings.data_root.resolve()
    raw_rows = delete_media_codes(data_root, body.codes)
    rows = [MediaDeleteBatchRow.model_validate(r) for r in raw_rows]
    n_del = sum(1 for r in rows if r.status == "deleted")
    n_nf = sum(1 for r in rows if r.status == "not_found")
    n_inv = sum(1 for r in rows if r.status == "skipped_invalid")
    msg = f"已处理 {len(rows)} 条：删除 {n_del}，未找到 {n_nf}，跳过无效 {n_inv}"
    return MediaDeleteBatchResponse(
        results=rows,
        deleted_count=n_del,
        not_found_count=n_nf,
        message=msg,
    )


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


@router.delete(
    "/{code}",
    response_model=MediaDeleteOneResponse,
    summary="按 code 删除单条媒体",
    description="同批量删除逻辑；若媒体不存在则 404。",
)
def delete_media_one(code: str, settings: Settings = Depends(get_settings)) -> MediaDeleteOneResponse:
    data_root = settings.data_root.resolve()
    validate_code(code)
    rows = delete_media_codes(data_root, [code])
    r = rows[0]
    if r["status"] == "not_found":
        raise HTTPException(status_code=404, detail="媒体不存在")
    if r["status"] == "skipped_invalid":
        raise HTTPException(status_code=400, detail=str(r.get("detail") or "无效的 media code"))
    removed = bool(r.get("removed_file"))
    return MediaDeleteOneResponse(
        code=code,
        deleted=True,
        removed_file=removed,
        message="已删除" + ("；对象文件已删除" if removed else "；对象文件未删除（仍被引用或不存在）"),
    )
