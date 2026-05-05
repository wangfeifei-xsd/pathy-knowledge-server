from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置：环境变量优先，可选 YAML 文件覆盖默认值（显式 env 仍优先）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="0.0.0.0", description="监听地址")
    port: int = Field(default=8765, description="监听端口")

    data_root: Path = Field(default=Path("./data"), description="知识库根目录")
    config_file: Optional[Path] = Field(default=None, alias="CONFIG_FILE", description="可选 YAML 配置文件路径")

    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: Optional[str] = Field(default=None, alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    embedding_model: str = Field(default="text-embedding-3-large", alias="EMBEDDING_MODEL")
    rerank_model: str = Field(default="gpt-4o-mini", alias="RERANK_MODEL")
    embedding_api_key: Optional[str] = Field(default=None, alias="EMBEDDING_API_KEY")
    rerank_api_key: Optional[str] = Field(default=None, alias="RERANK_API_KEY")
    embedding_base_url: Optional[str] = Field(default=None, alias="EMBEDDING_BASE_URL")
    rerank_base_url: Optional[str] = Field(default=None, alias="RERANK_BASE_URL")
    openai_timeout_seconds: float = Field(default=120.0, alias="OPENAI_TIMEOUT")
    openai_max_tokens: int = Field(default=8192, alias="OPENAI_MAX_TOKENS")
    embedding_timeout_seconds: float = Field(default=120.0, alias="EMBEDDING_TIMEOUT")
    rerank_timeout_seconds: float = Field(default=120.0, alias="RERANK_TIMEOUT")
    embedding_max_tokens: int = Field(default=8192, alias="EMBEDDING_MAX_TOKENS")
    rerank_max_tokens: int = Field(default=8192, alias="RERANK_MAX_TOKENS")

    api_key: Optional[str] = Field(default=None, alias="API_KEY", description="若设置则启用 Bearer 鉴权")
    max_file_bytes: int = Field(default=2_097_152, description="单文件最大字节数（默认 2MB）")
    forbid_delete_wiki_glob: bool = Field(
        default=False,
        description="若为 True，禁止删除 wiki 层任意路径（只读编译层）",
    )


def _coerce_yaml_value(key: str, value: Any) -> Any:
    if key in ("data_root", "config_file") and isinstance(value, str):
        return Path(value)
    return value


def _load_yaml_kwargs(config_file: Optional[Path]) -> dict[str, Any]:
    if not config_file or not config_file.is_file():
        return {}
    with config_file.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): _coerce_yaml_value(str(k), v) for k, v in raw.items()}


@lru_cache
def get_settings() -> Settings:
    """环境变量优先于 YAML 文件中的同名字段（pydantic-settings 默认行为）。"""
    probe = Settings()
    yaml_kwargs = _load_yaml_kwargs(probe.config_file)
    return Settings(**yaml_kwargs)
