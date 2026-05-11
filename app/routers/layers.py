from pathlib import Path as FsPath
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.config import Settings, get_settings
from app.models.schemas import (
    FileContentResponse,
    FileWriteRequest,
    LayerFileListResponse,
    LayerName,
    ListLayerResponse,
)
from app.services import storage
from app.services.vector_index import delete_wiki_vectors, get_wiki_embedding_status_map

router = APIRouter(prefix="/api/v1/layers", tags=["三层存储"])


def _decode_upload_text(raw: bytes) -> str:
    """按 UTF-8（含 BOM）优先；失败则尝试 GB18030（常见的中文 Windows 记事本保存）。"""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=415,
            detail="无法作为文本解码：请用 UTF-8 保存后再上传；PDF/图片等二进制不适用本接口。",
        ) from None


@router.get(
    "/{layer}/entries",
    response_model=ListLayerResponse,
    summary="列出目录（可按前缀）",
)
async def list_entries(
    layer: LayerName,
    prefix: str = Query("", description="层内子路径前缀，如 notes/"),
    settings: Settings = Depends(get_settings),
) -> ListLayerResponse:
    storage.ensure_layer_tree(settings.data_root.resolve())
    status_map = get_wiki_embedding_status_map(settings.data_root.resolve()) if layer == LayerName.wiki else None
    pfx, entries = storage.list_dir(settings.data_root.resolve(), layer, prefix, embedding_status=status_map)
    return ListLayerResponse(layer=layer, prefix=pfx, entries=entries)


@router.get(
    "/{layer}/files",
    response_model=LayerFileListResponse,
    summary="递归列出层内文件相对路径（用于下拉选择）",
)
async def list_layer_files(
    layer: LayerName,
    suffix: Optional[str] = Query(
        None,
        description="仅包含此后缀名结尾的路径，例如 .md；不传则包含全部文件",
    ),
    max_files: int = Query(5000, ge=1, le=20000),
    settings: Settings = Depends(get_settings),
) -> LayerFileListResponse:
    storage.ensure_layer_tree(settings.data_root.resolve())
    paths, truncated = storage.list_all_file_paths(
        settings.data_root.resolve(),
        layer,
        suffix=suffix,
        max_files=max_files,
    )
    return LayerFileListResponse(layer=layer, paths=paths, truncated=truncated)


@router.get(
    "/{layer}/file",
    response_model=FileContentResponse,
    summary="读取单个文件",
)
async def read_file(
    layer: LayerName,
    path: str = Query(..., description="层内相对文件路径"),
    settings: Settings = Depends(get_settings),
) -> FileContentResponse:
    storage.ensure_layer_tree(settings.data_root.resolve())
    text, size = storage.read_file(settings.data_root.resolve(), layer, path, settings.max_file_bytes)
    return FileContentResponse(layer=layer, path=path, content=text, size=size)


@router.post(
    "/{layer}/upload",
    response_model=FileContentResponse,
    summary="上传文件（multipart/form-data；文本优先 UTF-8，可选 GB18030）",
)
async def upload_file(
    layer: LayerName,
    file: UploadFile = File(..., description="文件内容"),
    path: str = Form(
        "",
        description="层内相对路径（含文件名）；留空则使用上传文件名的 basename，保存在当前层根目录",
    ),
    settings: Settings = Depends(get_settings),
) -> FileContentResponse:
    storage.ensure_layer_tree(settings.data_root.resolve())
    raw = await file.read()
    if len(raw) > settings.max_file_bytes:
        raise HTTPException(status_code=413, detail="文件超过大小限制")
    rel = path.strip().replace("\\", "/").lstrip("/")
    if not rel:
        fn = file.filename or "uploaded.txt"
        rel = FsPath(fn).name
        if not rel or rel in (".", ".."):
            rel = "uploaded.txt"
    text = _decode_upload_text(raw)
    size = storage.write_file(
        settings.data_root.resolve(),
        layer,
        rel,
        text,
        settings.max_file_bytes,
    )
    if layer == LayerName.wiki:
        delete_wiki_vectors(settings.data_root.resolve(), rel)
    return FileContentResponse(layer=layer, path=rel, content=text, size=size)


@router.put(
    "/{layer}/file",
    response_model=FileContentResponse,
    summary="创建或覆盖文件",
)
async def put_file(
    layer: LayerName,
    body: FileWriteRequest,
    path: str = Query(..., description="层内相对文件路径"),
    settings: Settings = Depends(get_settings),
) -> FileContentResponse:
    storage.ensure_layer_tree(settings.data_root.resolve())
    size = storage.write_file(
        settings.data_root.resolve(),
        layer,
        path,
        body.content,
        settings.max_file_bytes,
    )
    if layer == LayerName.wiki:
        delete_wiki_vectors(settings.data_root.resolve(), path)
    return FileContentResponse(layer=layer, path=path, content=body.content, size=size)


@router.delete("/{layer}/file", summary="删除文件或目录")
async def delete_file(
    layer: LayerName,
    path: str = Query(..., description="层内相对路径"),
    settings: Settings = Depends(get_settings),
) -> dict:
    storage.ensure_layer_tree(settings.data_root.resolve())
    storage.delete_path(
        settings.data_root.resolve(),
        layer,
        path,
        settings.forbid_delete_wiki_glob,
    )
    if layer == LayerName.wiki:
        delete_wiki_vectors(settings.data_root.resolve(), path.rstrip("/"))
    return {"ok": True, "deleted": path}


@router.get("/{layer}/archive.zip", summary="打包下载为一层或子目录 ZIP")
async def download_zip(
    layer: LayerName,
    prefix: str = Query("", description="可选子路径前缀"),
    settings: Settings = Depends(get_settings),
):
    storage.ensure_layer_tree(settings.data_root.resolve())
    data = storage.zip_layer_bytes(settings.data_root.resolve(), layer, prefix)
    filename = f"{layer.value}.zip"
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
