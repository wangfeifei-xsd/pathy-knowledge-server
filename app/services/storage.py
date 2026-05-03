import io
import zipfile
from pathlib import Path
from typing import Optional, Tuple

from fastapi import HTTPException

from app.config import Settings
from app.models.schemas import DirEntry, LayerName


def layer_root(data_root: Path, layer: LayerName) -> Path:
    return (data_root / layer.value).resolve()


def safe_resolve_under(base: Path, rel: str) -> Path:
    """将相对路径解析到 base 内，禁止跳出目录。"""
    if not rel or rel.strip() == "":
        return base
    # 统一为正斜杠风格再拆段
    parts = Path(rel.replace("\\", "/")).parts
    cur = base
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            raise HTTPException(status_code=400, detail="路径不允许包含 '..'")
        cur = (cur / p).resolve()
    try:
        cur.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="路径越界") from None
    return cur


def ensure_layer_tree(data_root: Path) -> None:
    for layer in LayerName:
        layer_root(data_root, layer).mkdir(parents=True, exist_ok=True)


def list_dir(
    data_root: Path,
    layer: LayerName,
    prefix: str = "",
) -> tuple[str, list[DirEntry]]:
    base = layer_root(data_root, layer)
    target = safe_resolve_under(base, prefix) if prefix else base
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")
    entries: list[DirEntry] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rel = child.relative_to(base)
        posix = rel.as_posix()
        entries.append(
            DirEntry(
                name=child.name,
                path=posix + ("/" if child.is_dir() else ""),
                is_dir=child.is_dir(),
                size=None if child.is_dir() else child.stat().st_size,
            )
        )
    return (prefix.rstrip("/"), entries)


def read_file(data_root: Path, layer: LayerName, rel_path: str, max_bytes: int) -> tuple[str, int]:
    base = layer_root(data_root, layer)
    path = safe_resolve_under(base, rel_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    size = path.stat().st_size
    if size > max_bytes:
        raise HTTPException(status_code=413, detail="文件超过大小限制")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="仅支持 UTF-8 文本")
    return text, len(data)


def write_file(data_root: Path, layer: LayerName, rel_path: str, content: str, max_bytes: int) -> int:
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise HTTPException(status_code=413, detail="内容超过大小限制")
    base = layer_root(data_root, layer)
    path = safe_resolve_under(base, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return len(encoded)


def delete_path(data_root: Path, layer: LayerName, rel_path: str, forbid_wiki_delete: bool) -> None:
    if forbid_wiki_delete and layer == LayerName.wiki:
        raise HTTPException(status_code=403, detail="已禁止删除编译层")
    base = layer_root(data_root, layer)
    path = safe_resolve_under(base, rel_path)
    if path == base:
        raise HTTPException(status_code=400, detail="不能删除层根目录")
    if not path.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.is_dir():
        import shutil

        shutil.rmtree(path)
    else:
        path.unlink()


def list_all_file_paths(
    data_root: Path,
    layer: LayerName,
    *,
    suffix: Optional[str] = None,
    max_files: int = 5000,
) -> Tuple[list[str], bool]:
    """递归列出层内所有文件的相对路径；suffix 如 .md 则过滤后缀。"""
    base = layer_root(data_root, layer)
    if not base.is_dir():
        return [], False
    collected: list[str] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        if suffix is not None and suffix != "":
            if not rel.lower().endswith(suffix.lower()):
                continue
        collected.append(rel)
    collected.sort()
    truncated = len(collected) > max_files
    if truncated:
        collected = collected[:max_files]
    return collected, truncated


def zip_layer_bytes(data_root: Path, layer: LayerName, prefix: str = "") -> bytes:
    base = layer_root(data_root, layer)
    root = safe_resolve_under(base, prefix) if prefix else base
    if not root.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    buf = io.BytesIO()
    arc_prefix = f"{layer.value}/"
    if prefix:
        arc_prefix += prefix.strip("/") + "/"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if root.is_file():
            zf.write(root, arcname=f"{layer.value}/{prefix}")
        else:
            for p in root.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(base)
                    zf.write(p, arcname=f"{layer.value}/{rel.as_posix()}")
    return buf.getvalue()
