from fastapi import APIRouter, Depends, Query

from app.config import Settings, get_settings
from app.models.schemas import (
    DataFolderTreeNode,
    DataStructureFolderCreateRequest,
    DataStructureFolderOpResponse,
    DataStructureFolderRenameRequest,
    LayerName,
)
from app.services import data_structure as ds_svc

router = APIRouter(prefix="/api/v1/data-structure", tags=["存储结构"])


@router.get(
    "/tree/{layer}",
    response_model=DataFolderTreeNode,
    summary="某层目录树（仅文件夹，用于上传位置选择与结构维护）",
)
async def get_layer_tree(
    layer: LayerName,
    max_depth: int = Query(16, ge=1, le=32),
    max_nodes: int = Query(400, ge=1, le=2000),
    settings: Settings = Depends(get_settings),
) -> DataFolderTreeNode:
    return ds_svc.build_layer_folder_tree(
        settings.data_root.resolve(),
        layer,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


@router.post(
    "/folders",
    response_model=DataStructureFolderOpResponse,
    summary="在层根下新增单层子目录（如 raw/reef）",
)
async def create_folder(
    body: DataStructureFolderCreateRequest,
    settings: Settings = Depends(get_settings),
) -> DataStructureFolderOpResponse:
    created = ds_svc.create_folder_under_layer_root(settings.data_root.resolve(), body.layer, body.name)
    return DataStructureFolderOpResponse(ok=True, layer=body.layer, path=created)


@router.patch(
    "/folders/rename",
    response_model=DataStructureFolderOpResponse,
    summary="重命名目录（仅当目录为空）",
)
async def rename_folder(
    body: DataStructureFolderRenameRequest,
    settings: Settings = Depends(get_settings),
) -> DataStructureFolderOpResponse:
    root = settings.data_root.resolve()
    new_path = ds_svc.rename_folder(root, body.layer, body.path, body.new_name)
    return DataStructureFolderOpResponse(ok=True, layer=body.layer, path=new_path)


@router.delete(
    "/folders",
    response_model=DataStructureFolderOpResponse,
    summary="删除空目录",
)
async def delete_folder(
    layer: LayerName,
    path: str = Query(..., description="相对该层的目录路径，如 reef 或 reef/"),
    settings: Settings = Depends(get_settings),
) -> DataStructureFolderOpResponse:
    norm = ds_svc._norm_rel_dir(path)
    ds_svc.delete_empty_folder(
        settings.data_root.resolve(),
        layer,
        norm,
        settings.forbid_delete_wiki_glob,
    )
    return DataStructureFolderOpResponse(ok=True, layer=layer, path=norm)
