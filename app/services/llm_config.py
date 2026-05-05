"""模型有效配置：LLM / Embedding / Rerank 三套独立配置。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from app.config import Settings

LLM_JSON_NAME = "llm.json"
KEY_FILE_NAME = "openai_api_key"
EMBEDDING_KEY_FILE_NAME = "embedding_api_key"
RERANK_KEY_FILE_NAME = "rerank_api_key"


def _pathy_dir(data_root: Path) -> Path:
    return data_root / ".pathy"


def llm_json_path(data_root: Path) -> Path:
    return _pathy_dir(data_root) / LLM_JSON_NAME


def api_key_file_path(data_root: Path) -> Path:
    return _pathy_dir(data_root) / KEY_FILE_NAME


def embedding_api_key_file_path(data_root: Path) -> Path:
    return _pathy_dir(data_root) / EMBEDDING_KEY_FILE_NAME


def rerank_api_key_file_path(data_root: Path) -> Path:
    return _pathy_dir(data_root) / RERANK_KEY_FILE_NAME


def load_llm_json(data_root: Path) -> dict[str, Any]:
    p = llm_json_path(data_root)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_llm_json(data_root: Path, data: dict[str, Any]) -> None:
    d = _pathy_dir(data_root)
    d.mkdir(parents=True, exist_ok=True)
    llm_json_path(data_root).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


FieldSource = Literal["env", "file", "default"]


@dataclass(frozen=True)
class EffectiveLLM:
    model: str
    model_source: FieldSource
    base_url: Optional[str]
    base_url_source: FieldSource
    timeout_seconds: float
    timeout_source: FieldSource
    max_tokens: int
    max_tokens_source: FieldSource


@dataclass(frozen=True)
class EffectiveModelEndpoint:
    model: str
    model_source: FieldSource
    base_url: Optional[str]
    base_url_source: FieldSource
    timeout_seconds: float
    timeout_source: FieldSource
    max_tokens: int
    max_tokens_source: FieldSource


def _pick_str(
    env_name: str,
    file_dict: dict[str, Any],
    file_key: str,
    settings_value: Optional[str],
    *,
    default_when_missing: Optional[str] = None,
) -> tuple[Optional[str], FieldSource]:
    if env_name in os.environ:
        v = os.environ.get(env_name)
        return (v if v is not None else None, "env")
    if file_key in file_dict and file_dict[file_key] is not None:
        return (str(file_dict[file_key]).strip() or None, "file")
    if settings_value is not None and settings_value != "":
        return (settings_value, "default")
    return (default_when_missing, "default")


def _pick_float(
    env_name: str,
    file_dict: dict[str, Any],
    file_key: str,
    settings_value: float,
) -> tuple[float, FieldSource]:
    if env_name in os.environ:
        try:
            return (float(os.environ[env_name]), "env")
        except ValueError:
            pass
    if file_key in file_dict and file_dict[file_key] is not None:
        try:
            return (float(file_dict[file_key]), "file")
        except (TypeError, ValueError):
            pass
    return (settings_value, "default")


def _pick_int(
    env_name: str,
    file_dict: dict[str, Any],
    file_key: str,
    settings_value: int,
) -> tuple[int, FieldSource]:
    if env_name in os.environ:
        try:
            return (int(os.environ[env_name]), "env")
        except ValueError:
            pass
    if file_key in file_dict and file_dict[file_key] is not None:
        try:
            return (int(file_dict[file_key]), "file")
        except (TypeError, ValueError):
            pass
    return (settings_value, "default")


def compute_effective_llm(settings: Settings) -> EffectiveLLM:
    data_root = settings.data_root.resolve()
    f = load_llm_json(data_root)

    model, ms = _pick_str(
        "OPENAI_MODEL",
        f,
        "openai_model",
        settings.openai_model,
        default_when_missing=settings.openai_model,
    )
    assert model is not None

    base_url, bs = _pick_str(
        "OPENAI_BASE_URL",
        f,
        "openai_base_url",
        settings.openai_base_url,
    )

    timeout, ts = _pick_float(
        "OPENAI_TIMEOUT",
        f,
        "openai_timeout_seconds",
        settings.openai_timeout_seconds,
    )

    max_tokens, mt = _pick_int(
        "OPENAI_MAX_TOKENS",
        f,
        "openai_max_tokens",
        settings.openai_max_tokens,
    )

    return EffectiveLLM(
        model=model,
        model_source=ms,
        base_url=base_url,
        base_url_source=bs,
        timeout_seconds=timeout,
        timeout_source=ts,
        max_tokens=max_tokens,
        max_tokens_source=mt,
    )


def resolve_openai_api_key(settings: Settings) -> Optional[str]:
    """解析顺序：进程环境变量 > pydantic 合并值（含 .env）> 数据目录下密钥文件。"""
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"].strip() or None
    if settings.openai_api_key:
        return settings.openai_api_key.strip() or None
    p = api_key_file_path(settings.data_root.resolve())
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def api_key_configured(settings: Settings) -> bool:
    return resolve_openai_api_key(settings) is not None


def env_locks() -> dict[str, bool]:
    return {
        "openai_model": "OPENAI_MODEL" in os.environ,
        "embedding_model": "EMBEDDING_MODEL" in os.environ,
        "rerank_model": "RERANK_MODEL" in os.environ,
        "openai_base_url": "OPENAI_BASE_URL" in os.environ,
        "embedding_base_url": "EMBEDDING_BASE_URL" in os.environ,
        "rerank_base_url": "RERANK_BASE_URL" in os.environ,
        "openai_timeout_seconds": "OPENAI_TIMEOUT" in os.environ,
        "embedding_timeout_seconds": "EMBEDDING_TIMEOUT" in os.environ,
        "rerank_timeout_seconds": "RERANK_TIMEOUT" in os.environ,
        "openai_max_tokens": "OPENAI_MAX_TOKENS" in os.environ,
        "embedding_max_tokens": "EMBEDDING_MAX_TOKENS" in os.environ,
        "rerank_max_tokens": "RERANK_MAX_TOKENS" in os.environ,
        "openai_api_key": "OPENAI_API_KEY" in os.environ,
        "embedding_api_key": "EMBEDDING_API_KEY" in os.environ,
        "rerank_api_key": "RERANK_API_KEY" in os.environ,
    }


def patch_llm_json(data_root: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入 llm.json；仅允许白名单键。"""
    allowed = {
        "openai_model",
        "embedding_model",
        "rerank_model",
        "openai_base_url",
        "embedding_base_url",
        "rerank_base_url",
        "openai_timeout_seconds",
        "embedding_timeout_seconds",
        "rerank_timeout_seconds",
        "openai_max_tokens",
        "embedding_max_tokens",
        "rerank_max_tokens",
    }
    cur = load_llm_json(data_root)
    for k, v in patch.items():
        if k not in allowed:
            continue
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    save_llm_json(data_root, cur)
    return cur


def write_api_key_file(data_root: Path, api_key: Optional[str]) -> None:
    p = api_key_file_path(data_root)
    d = _pathy_dir(data_root)
    d.mkdir(parents=True, exist_ok=True)
    if not api_key:
        if p.is_file():
            p.unlink()
        return
    p.write_text(api_key.strip() + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _write_key_file(path: Path, api_key: Optional[str]) -> None:
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    if not api_key:
        if path.is_file():
            path.unlink()
        return
    path.write_text(api_key.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_embedding_api_key_file(data_root: Path, api_key: Optional[str]) -> None:
    _write_key_file(embedding_api_key_file_path(data_root), api_key)


def write_rerank_api_key_file(data_root: Path, api_key: Optional[str]) -> None:
    _write_key_file(rerank_api_key_file_path(data_root), api_key)


def resolve_embedding_api_key(settings: Settings) -> Optional[str]:
    if os.environ.get("EMBEDDING_API_KEY"):
        return os.environ["EMBEDDING_API_KEY"].strip() or None
    if settings.embedding_api_key:
        return settings.embedding_api_key.strip() or None
    p = embedding_api_key_file_path(settings.data_root.resolve())
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def resolve_rerank_api_key(settings: Settings) -> Optional[str]:
    if os.environ.get("RERANK_API_KEY"):
        return os.environ["RERANK_API_KEY"].strip() or None
    if settings.rerank_api_key:
        return settings.rerank_api_key.strip() or None
    p = rerank_api_key_file_path(settings.data_root.resolve())
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def embedding_api_key_configured(settings: Settings) -> bool:
    return resolve_embedding_api_key(settings) is not None


def rerank_api_key_configured(settings: Settings) -> bool:
    return resolve_rerank_api_key(settings) is not None


def compute_effective_embedding_model(settings: Settings) -> EffectiveModelEndpoint:
    data_root = settings.data_root.resolve()
    f = load_llm_json(data_root)
    model, ms = _pick_str(
        "EMBEDDING_MODEL",
        f,
        "embedding_model",
        settings.embedding_model,
        default_when_missing=settings.embedding_model,
    )
    assert model is not None
    base_url, bs = _pick_str(
        "EMBEDDING_BASE_URL",
        f,
        "embedding_base_url",
        settings.embedding_base_url,
    )
    timeout, ts = _pick_float(
        "EMBEDDING_TIMEOUT",
        f,
        "embedding_timeout_seconds",
        settings.embedding_timeout_seconds,
    )
    max_tokens, mt = _pick_int(
        "EMBEDDING_MAX_TOKENS",
        f,
        "embedding_max_tokens",
        settings.embedding_max_tokens,
    )
    return EffectiveModelEndpoint(
        model=model,
        model_source=ms,
        base_url=base_url,
        base_url_source=bs,
        timeout_seconds=timeout,
        timeout_source=ts,
        max_tokens=max_tokens,
        max_tokens_source=mt,
    )


def compute_effective_rerank_model(settings: Settings) -> EffectiveModelEndpoint:
    data_root = settings.data_root.resolve()
    f = load_llm_json(data_root)
    model, ms = _pick_str(
        "RERANK_MODEL",
        f,
        "rerank_model",
        settings.rerank_model,
        default_when_missing=settings.rerank_model,
    )
    assert model is not None
    base_url, bs = _pick_str(
        "RERANK_BASE_URL",
        f,
        "rerank_base_url",
        settings.rerank_base_url,
    )
    timeout, ts = _pick_float(
        "RERANK_TIMEOUT",
        f,
        "rerank_timeout_seconds",
        settings.rerank_timeout_seconds,
    )
    max_tokens, mt = _pick_int(
        "RERANK_MAX_TOKENS",
        f,
        "rerank_max_tokens",
        settings.rerank_max_tokens,
    )
    return EffectiveModelEndpoint(
        model=model,
        model_source=ms,
        base_url=base_url,
        base_url_source=bs,
        timeout_seconds=timeout,
        timeout_source=ts,
        max_tokens=max_tokens,
        max_tokens_source=mt,
    )
