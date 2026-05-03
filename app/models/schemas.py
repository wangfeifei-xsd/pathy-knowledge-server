from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class LayerName(str, Enum):
    raw = "raw"
    wiki = "wiki"
    schema = "schema"


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "pathy-knowledge-server"


class ConfigSummaryResponse(BaseModel):
    data_root: str
    data_root_resolved: str
    openai_base_url_configured: bool
    openai_model: str
    openai_timeout_seconds: float = 120.0
    openai_max_tokens: int = 8192
    openai_api_key_configured: bool = False
    layers: list[str] = Field(default_factory=lambda: ["raw", "wiki", "schema"])
    auth_enabled: bool


class LLMFieldSource(str, Enum):
    env = "env"
    file = "file"
    default = "default"


class LLMSettingsResponse(BaseModel):
    openai_model: str
    openai_model_source: LLMFieldSource
    openai_base_url: Optional[str] = None
    openai_base_url_source: LLMFieldSource
    openai_timeout_seconds: float
    openai_timeout_source: LLMFieldSource
    openai_max_tokens: int
    openai_max_tokens_source: LLMFieldSource
    openai_api_key_configured: bool = Field(
        description="进程环境、.env 或 .pathy 密钥文件任一则为 True",
    )
    env_locks: dict[str, bool] = Field(
        default_factory=dict,
        description="各键是否被进程环境变量锁定（此时运行时文件不生效）",
    )
    runtime_llm_json: str = Field(
        default=".pathy/llm.json",
        description="相对数据根目录的 LLM 配置路径",
    )


class LLMSettingsUpdateRequest(BaseModel):
    openai_model: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_timeout_seconds: Optional[float] = None
    openai_max_tokens: Optional[int] = None
    openai_api_key: Optional[str] = Field(
        default=None,
        description="写入数据根 .pathy/openai_api_key；传空字符串可删除该文件",
    )


class LLMSettingsUpdateResult(BaseModel):
    settings: LLMSettingsResponse
    warnings: list[str] = Field(default_factory=list)


class DirEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None


class ListLayerResponse(BaseModel):
    layer: LayerName
    prefix: str
    entries: list[DirEntry]


class LayerFileListResponse(BaseModel):
    layer: LayerName
    paths: list[str]
    truncated: bool = Field(default=False, description="是否因数量上限截断")


class FileContentResponse(BaseModel):
    layer: LayerName
    path: str
    content: str
    size: int


class FileWriteRequest(BaseModel):
    content: str = Field(..., description="UTF-8 文本内容")


class TaskUsage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class LLMConnectionTestRequest(BaseModel):
    """可选：用表单草稿覆盖本次探测（不写盘）；字段均可省略，省略时用服务端当前有效配置。"""

    openai_model: Optional[str] = None
    openai_base_url: Optional[str] = None


class LLMTestResponse(BaseModel):
    ok: bool
    model: str
    base_url: Optional[str] = None
    elapsed_ms: float = 0.0
    message: str = ""
    usage: Optional[TaskUsage] = None
    error: Optional[str] = None


class CompileTaskRequest(BaseModel):
    input_paths: list[str] = Field(
        ...,
        description="原始层内相对路径列表（如 notes/foo.md）",
    )
    output_path: str = Field(..., description="编译层写入路径（相对 wiki）")
    schema_paths: Optional[list[str]] = Field(
        default=None,
        description="规范层待注入文件相对路径；默认包含 AGENTS.md（若存在）",
    )
    extra_instructions: Optional[str] = Field(default=None, description="附加编译说明")


class CompileTaskResponse(BaseModel):
    model: str
    usage: Optional[TaskUsage] = None
    output_path: str
    written_files: list[str] = Field(default_factory=list)
    message: str = ""


class LintTaskRequest(BaseModel):
    wiki_paths: Optional[list[str]] = Field(
        default=None,
        description="待检查 wiki 相对路径；为空则扫描整个 wiki 层（谨慎）",
    )
    auto_fix: bool = Field(default=False, description="是否根据报告尝试自动改写（MVP 通常为 False）")
    max_files: int = Field(default=50, description="最多检查的 markdown 文件数")


class LintTaskResponse(BaseModel):
    model: str
    usage: Optional[TaskUsage] = None
    report: str
    files_inspected: list[str] = Field(default_factory=list)
    auto_fix_applied: bool = False


class PolishTextRequest(BaseModel):
    content: str = Field(..., description="待润色的 Markdown 正文")
    instruction: Optional[str] = Field(default=None, description="对模型的额外说明（可选）")


class PolishTextResponse(BaseModel):
    content: str
    model: str
    usage: Optional[TaskUsage] = None


class DialogueRecallBaseParams(BaseModel):
    """wiki 关键词召回共用参数（与是否调用 LLM 无关）。"""

    query: str = Field(..., description="用户自然语言问句或指令")
    wiki_prefix: str = Field(
        default="",
        description="仅在此 wiki 子路径下扫描（相对路径，空为整层）",
    )
    max_files: int = Field(default=80, ge=1, le=500, description="最多参与扫描的 .md 文件数")
    top_k_chunks: int = Field(default=6, ge=1, le=32, description="召回并注入的片段条数上限")
    chunk_max_chars: int = Field(
        default=1200,
        ge=400,
        le=8000,
        description="单片段在分块时的最大字符数",
    )
    context_budget_chars: int = Field(
        default=12000,
        ge=2000,
        le=100_000,
        description="参考资料总字符上限（仅召回时亦为拼接预算；全流程时拼入用户消息）",
    )


class DialogueRecallRequest(DialogueRecallBaseParams):
    """自然语言 → 仅 wiki 关键词召回（不调用 LLM）。"""


class DialogueRecallTestRequest(DialogueRecallBaseParams):
    """对话召唤全流程测试：自然语言 → wiki 关键词召回 → 注入上下文 → LLM 回答。"""

    system_prompt: Optional[str] = Field(
        default=None,
        description="覆盖默认 system 提示；为空则用内置问答约束",
    )


class DialogueRecallHit(BaseModel):
    path: str = Field(description="wiki 相对路径")
    score: float = Field(description="关键词重叠得分（越大越相关）")
    snippet: str = Field(description="片段预览")


class DialogueRecallResponse(BaseModel):
    """仅召回阶段结果（无 LLM）。"""

    user_query: str
    recall_method: str = Field(default="keyword_overlap", description="召回实现标识")
    query_terms: list[str] = Field(default_factory=list, description="从问句解析出的匹配词条")
    files_scanned: int = Field(default=0, description="实际扫描的 wiki 文件数")
    recall_hits: list[DialogueRecallHit] = Field(default_factory=list)
    injected_context: str = Field(description="按预算拼接后的参考资料正文（与全流程注入块一致）")
    context_truncated: bool = Field(
        default=False,
        description="是否因 context_budget_chars 截断了部分片段",
    )
    message: str = ""


class DialogueRecallTestResponse(BaseModel):
    model: str
    usage: Optional[TaskUsage] = None
    user_query: str
    recall_method: str = Field(default="keyword_overlap", description="召回实现标识")
    query_terms: list[str] = Field(default_factory=list, description="从问句解析出的匹配词条")
    files_scanned: int = Field(default=0, description="实际扫描的 wiki 文件数")
    recall_hits: list[DialogueRecallHit] = Field(default_factory=list)
    injected_context: str = Field(description="实际拼入用户消息的参考资料正文")
    context_truncated: bool = Field(
        default=False,
        description="是否因 context_budget_chars 截断了部分片段",
    )
    assistant_reply: str
    message: str = ""
