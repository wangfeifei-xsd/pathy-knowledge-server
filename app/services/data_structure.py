"""data 下各层目录树：查询、在层根下新增单层子目录、空目录重命名/删除。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.models.schemas import DataFolderTreeNode, LayerName
from app.services import storage
from app.services.vector_index import delete_wiki_vectors


def _norm_rel_dir(rel: str) -> str:
    s = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not s:
        return ""
    return s.rstrip("/") + "/"


def _single_segment(name: str) -> str:
    n = (name or "").strip().replace("\\", "/").strip("/")
    if not n or "/" in n or n in (".", ".."):
        raise HTTPException(status_code=400, detail="目录名须为单层路径段，且不能为 . 或 ..")
    return n


def dir_is_empty(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    return False


def build_layer_folder_tree(
    data_root: Path,
    layer: LayerName,
    *,
    max_depth: int = 16,
    max_nodes: int = 400,
) -> DataFolderTreeNode:
    """递归构建某层下仅目录的树（用于前端 TreeSelect / 维护页）。"""
    base = storage.layer_root(data_root, layer)
    storage.ensure_layer_tree(data_root)
    if not base.is_dir():
        raise HTTPException(status_code=500, detail="层根目录不可用")

    counter = 0

    def walk(physical: Path, depth: int) -> DataFolderTreeNode | None:
        nonlocal counter
        if counter >= max_nodes:
            return None
        counter += 1
        try:
            rel = physical.relative_to(base).as_posix()
        except ValueError:
            raise HTTPException(status_code=400, detail="路径越界") from None
        if rel == ".":
            path_key = ""
            title = "层根目录"
        else:
            path_key = rel + "/"
            title = physical.name
        if depth >= max_depth:
            return DataFolderTreeNode(path=path_key, title=title, children=[])
        children: list[DataFolderTreeNode] = []
        if physical.is_dir():
            subs = sorted(
                [p for p in physical.iterdir() if p.is_dir()],
                key=lambda p: p.name.lower(),
            )
            for sub in subs:
                ch = walk(sub, depth + 1)
                if ch is not None:
                    children.append(ch)
        return DataFolderTreeNode(path=path_key, title=title, children=children)

    root = walk(base, 0)
    if root is None:
        raise HTTPException(status_code=500, detail="无法构建目录树")
    return root


def create_folder_under_layer_root(data_root: Path, layer: LayerName, name: str) -> str:
    """仅在层根下创建一层子目录，如 raw/reef。"""
    seg = _single_segment(name)
    base = storage.layer_root(data_root, layer)
    storage.ensure_layer_tree(data_root)
    target = (base / seg).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="路径越界") from None
    if target.exists():
        raise HTTPException(status_code=409, detail="该目录已存在")
    target.mkdir(parents=False, exist_ok=False)
    return seg + "/"


def resolve_dir_under_layer(data_root: Path, layer: LayerName, rel_dir: str) -> Path:
    """rel_dir 为相对层根目录前缀，如 reef/ 或 a/b/；返回物理路径（须为已存在目录）。"""
    rel = _norm_rel_dir(rel_dir)
    if rel == "":
        p = storage.layer_root(data_root, layer)
    else:
        p = storage.safe_resolve_under(storage.layer_root(data_root, layer), rel.rstrip("/"))
    if not p.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")
    return p


def rename_folder(
    data_root: Path,
    layer: LayerName,
    rel_dir: str,
    new_name: str,
) -> str:
    """重命名目录（须为空目录）。new_name 为最终目录名单段。"""
    new_seg = _single_segment(new_name)
    src = resolve_dir_under_layer(data_root, layer, rel_dir)
    if not dir_is_empty(src):
        raise HTTPException(status_code=400, detail="目录非空时不允许修改名称")
    parent = src.parent
    dst = (parent / new_seg).resolve()
    try:
        dst.relative_to(storage.layer_root(data_root, layer).resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="路径越界") from None
    if dst.exists():
        raise HTTPException(status_code=409, detail="目标名称已存在")
    src.rename(dst)
    try:
        rel_after = dst.relative_to(storage.layer_root(data_root, layer)).as_posix()
    except ValueError:
        raise HTTPException(status_code=500, detail="重命名结果异常") from None
    return rel_after + "/" if rel_after != "." else ""


def delete_empty_folder(data_root: Path, layer: LayerName, rel_dir: str, forbid_wiki_delete: bool) -> None:
    rel = _norm_rel_dir(rel_dir)
    if rel == "":
        raise HTTPException(status_code=400, detail="不能删除层根目录")
    path = resolve_dir_under_layer(data_root, layer, rel)
    if path == storage.layer_root(data_root, layer):
        raise HTTPException(status_code=400, detail="不能删除层根目录")
    if not dir_is_empty(path):
        raise HTTPException(status_code=400, detail="目录非空时不允许删除")
    rel_key = rel.rstrip("/")
    storage.delete_path(data_root, layer, rel_key, forbid_wiki_delete)
    if layer == LayerName.wiki and rel_key:
        delete_wiki_vectors(data_root, rel_key)
