from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class LayerName(str, Enum):
    raw = "raw"
    wiki = "wiki"
    schema = "schema"
    media = "media"


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "pathy-knowledge-server"


class ConfigSummaryResponse(BaseModel):
    data_root: str
    data_root_resolved: str
    llm_base_url: Optional[str] = Field(
        default=None,
        description="生效的 LLM OpenAI 兼容 Base URL；未配置则为 null（SDK 默认端点）",
    )
    llm_model: str = Field(description="生效的 LLM 模型 ID")
    embedding_base_url: Optional[str] = Field(
        default=None,
        description="生效的 Embedding Base URL；未配置则为 null",
    )
    embedding_model: str = Field(description="生效的 Embedding 模型 ID")
    rerank_base_url: Optional[str] = Field(
        default=None,
        description="生效的 Rerank Base URL；未配置则为 null",
    )
    rerank_model: str = Field(description="生效的 Rerank 模型 ID")
    layers: list[str] = Field(
        default_factory=lambda: ["raw", "wiki", "schema", "media"],
        description="逻辑层：media 为本地多媒体目录（见 /api/v1/media），不走 layers 文本读写接口",
    )


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


class BasicModelSettingsResponse(BaseModel):
    model: str
    model_source: LLMFieldSource
    openai_base_url: Optional[str] = None
    openai_base_url_source: LLMFieldSource
    openai_timeout_seconds: float
    openai_timeout_source: LLMFieldSource
    openai_max_tokens: int
    openai_max_tokens_source: LLMFieldSource
    openai_api_key_configured: bool = False
    env_locks: dict[str, bool] = Field(default_factory=dict)
    runtime_llm_json: str = Field(default=".pathy/llm.json")


class BasicModelSettingsUpdateRequest(BaseModel):
    model: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_timeout_seconds: Optional[float] = None
    openai_max_tokens: Optional[int] = None
    openai_api_key: Optional[str] = None


class BasicModelSettingsUpdateResult(BaseModel):
    settings: BasicModelSettingsResponse
    warnings: list[str] = Field(default_factory=list)


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
    embedding_status: Optional[str] = Field(
        default=None,
        description="仅 wiki 文件可用：embedded / stale / not_embedded",
    )


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
    """wiki 双路召回共用参数（与是否调用 LLM 无关）。"""

    query: str = Field(..., description="用户自然语言问句或指令")
    wiki_prefix: str = Field(
        default="",
        description="仅在此 wiki 子路径下扫描（相对路径，空为整层）",
    )
    max_files: int = Field(default=80, ge=1, le=500, description="最多参与扫描的 .md 文件数")
    bm25_top_n: int = Field(default=10, ge=1, le=100, description="BM25 路召回候选条数")
    vector_top_n: int = Field(default=10, ge=1, le=100, description="向量路召回候选条数")
    top_k_chunks: int = Field(default=6, ge=1, le=32, description="合并 rerank 后最终注入的 topK 条数")
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


class RecallStopwordsUpdateRequest(BaseModel):
    words: list[str] = Field(default_factory=list, description="停用词列表（将统一小写、去重）")


class RecallStopwordsResponse(BaseModel):
    words: list[str] = Field(default_factory=list)
    source: str = Field(default="runtime_or_default", description="runtime_file 或 default_builtin")
    runtime_path: str = Field(default=".pathy/recall_stopwords.txt")
    count: int = 0
    message: str = ""


class MediaRef(BaseModel):
    """召回或索引中引用的媒体摘要（客户端再 GET /api/v1/media/{code}）。"""

    code: str
    mime: str = Field(description="Content-Type 主类型")
    title: Optional[str] = Field(default=None, description="可选标题")
    size: int = Field(default=0, ge=0, description="字节数")


class MediaBackrefEntry(BaseModel):
    wiki_path: str = Field(description="wiki 相对路径（.md）")
    heading_path: str = Field(default="", description="标题路径，无标题则为空")


class MediaUploadResponse(BaseModel):
    code: str
    deduplicated: bool = Field(default=False, description="是否因 sha256 已存在而返回已有条目")
    mime: str
    size: int
    message: str = ""


class MediaReindexBackrefsResponse(BaseModel):
    codes_with_refs: int = Field(description="至少出现一次的 media code 数")
    total_ref_rows: int = Field(description="去重后的 (code, wiki, heading) 条数")
    message: str = ""


class MediaBackrefsResponse(BaseModel):
    code: str
    entries: list[MediaBackrefEntry] = Field(default_factory=list)
    message: str = Field(
        default="",
        description="若 entries 为空，可能尚未运行 POST /api/v1/media/reindex-backrefs",
    )


class MediaListItem(BaseModel):
    """manifest 中单条媒体（供控制台列表，不含磁盘 rel_storage）。"""

    code: str
    mime: str = ""
    size: int = Field(default=0, ge=0)
    title: Optional[str] = None
    original_name: Optional[str] = None
    created_at: str = Field(default="", description="ISO 时间")
    sha256: str = Field(default="", description="内容哈希")


class MediaListResponse(BaseModel):
    items: list[MediaListItem] = Field(default_factory=list)
    count: int = Field(default=0, ge=0, description="条目数")
    bytes_total: int = Field(default=0, ge=0, description="登记字节合计")


class MediaResolveFromTextRequest(BaseModel):
    text: str = Field(
        default="",
        max_length=500_000,
        description="任意正文（多为召回 injected_context 纯文本，或含 wiki 的 Markdown）；解析 ![[MEDIA:…]] 与 <!-- media:… -->",
    )
    codes: list[str] = Field(
        default_factory=list,
        description="额外 code（如召回 merged_media 中的 code），与正文解析结果按序合并去重",
    )


class MediaResolvedItem(BaseModel):
    code: str
    registered: bool = Field(description="manifest 中是否已登记该 code")
    mime: str = ""
    size: int = Field(default=0, ge=0)
    title: Optional[str] = None
    original_name: Optional[str] = None
    created_at: str = ""
    sha256: str = ""


class MediaResolveFromTextResponse(BaseModel):
    codes: list[str] = Field(default_factory=list, description="合并去重后的 code 顺序")
    items: list[MediaResolvedItem] = Field(
        default_factory=list,
        description="与 codes 一一对应；registered=false 表示正文提及但未在 manifest 登记",
    )


class MediaExportZipRequest(BaseModel):
    codes: list[str] = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="要导出的 media code 列表（多选）；会去重并校验",
    )


class MediaImportZipRow(BaseModel):
    source_code: str = Field(description="zip 内的导出 code")
    result_code: str = Field(default="", description="导入后在当前环境的 code（去重或冲突映射时可能与 source 不同）")
    status: str = Field(
        description="imported / remapped / skipped_identical / deduplicated_existing / error",
    )
    detail: str = Field(default="", description="说明或错误原因")


class MediaImportZipResponse(BaseModel):
    results: list[MediaImportZipRow] = Field(default_factory=list)
    message: str = Field(default="", description="汇总说明")
    target_dir_normalized: str = Field(
        default="",
        description="实际写入的 media/objects 子目录（空为默认分层路径）",
    )


class MediaDeleteBatchRequest(BaseModel):
    codes: list[str] = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="待删除的 media code 列表；会去重并按序处理",
    )


class MediaDeleteBatchRow(BaseModel):
    code: str
    status: str = Field(description="deleted / not_found / skipped_invalid")
    removed_file: bool = Field(default=False, description="是否尝试删除了 objects 下的文件")
    detail: str = Field(default="", description="说明")


class MediaDeleteBatchResponse(BaseModel):
    results: list[MediaDeleteBatchRow] = Field(default_factory=list)
    deleted_count: int = Field(default=0, ge=0)
    not_found_count: int = Field(default=0, ge=0)
    message: str = Field(default="", description="汇总说明")


class MediaDeleteOneResponse(BaseModel):
    code: str
    deleted: bool
    removed_file: bool = Field(default=False, description="是否删除了磁盘上的对象文件")
    message: str = Field(default="")


class WikiEmbedRequest(BaseModel):
    path: str = Field(description="wiki 相对路径，仅支持 .md 文件")


class WikiEmbedResponse(BaseModel):
    path: str
    chunk_count: int
    model: str
    updated_at: str
    message: str = ""


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
    score: float = Field(description="融合重排得分（BM25 + 向量 + 轻量规则，越大越相关）")
    snippet: str = Field(description="片段预览")
    heading_path: str = Field(
        default="",
        description="该片段在文内的 Markdown 标题路径（父 > 子）；无标题切块时为空",
    )


class DialogueRecallLaneStatus(BaseModel):
    """单路召回（BM25 或向量）的参与状态；均为服务端已计算信息，无额外 IO。"""

    status: str = Field(
        description=(
            "BM25: ok | skipped_no_chunks | skipped_no_terms | no_hits。"
            "向量: ok | skipped_no_api_key | error_embedding。"
            "含义见各接口文档或服务端日志。"
        )
    )
    candidate_count: int = Field(
        default=0,
        ge=0,
        description="该路产生的候选条数（BM25 为得分>0 的切片数；向量为 embedding 成功后检索返回条数）",
    )
    detail: Optional[str] = Field(
        default=None,
        description="补充说明，如停用词/无切片原因或 embedding 错误摘要",
    )
    embedding_model: Optional[str] = Field(
        default=None,
        description="向量路拟使用的 embedding 模型 id；BM25 路为 null",
    )


class DialogueRecallResponse(BaseModel):
    """仅召回阶段结果（无 LLM）。"""

    user_query: str
    recall_method: str = Field(default="hybrid_bm25_vector", description="召回实现标识（当前为 hybrid_bm25_vector）")
    query_terms: list[str] = Field(default_factory=list, description="从问句解析出的匹配词条")
    files_scanned: int = Field(default=0, description="实际扫描的 wiki 文件数")
    recall_hits: list[DialogueRecallHit] = Field(default_factory=list)
    merged_media: list[MediaRef] = Field(
        default_factory=list,
        description="各命中片段正文内解析出的媒体，按命中顺序合并后按 code 去重（不再逐条挂在 recall_hits 上）",
    )
    bm25: DialogueRecallLaneStatus = Field(description="BM25 一路状态")
    vector: DialogueRecallLaneStatus = Field(description="向量一路状态")
    injected_context: str = Field(
        description="按预算拼接后的纯文本参考资料（不含多媒体占位与媒体 code；媒体见 merged_media）",
    )
    context_truncated: bool = Field(
        default=False,
        description="是否因 context_budget_chars 截断了部分片段",
    )
    message: str = ""


class DialogueRecallTestResponse(BaseModel):
    model: str
    usage: Optional[TaskUsage] = None
    user_query: str
    recall_method: str = Field(default="hybrid_bm25_vector", description="召回实现标识（当前为 hybrid_bm25_vector）")
    query_terms: list[str] = Field(default_factory=list, description="从问句解析出的匹配词条")
    files_scanned: int = Field(default=0, description="实际扫描的 wiki 文件数")
    recall_hits: list[DialogueRecallHit] = Field(default_factory=list)
    merged_media: list[MediaRef] = Field(
        default_factory=list,
        description="各命中片段正文内解析出的媒体，按命中顺序合并后按 code 去重（不再逐条挂在 recall_hits 上）",
    )
    bm25: DialogueRecallLaneStatus = Field(description="BM25 一路状态")
    vector: DialogueRecallLaneStatus = Field(description="向量一路状态")
    injected_context: str = Field(
        description="实际拼入用户消息的纯文本参考资料（不含多媒体占位与媒体 code；媒体见 merged_media）",
    )
    context_truncated: bool = Field(
        default=False,
        description="是否因 context_budget_chars 截断了部分片段",
    )
    assistant_reply: str
    message: str = ""


class DataFolderTreeNode(BaseModel):
    """单层或多层子目录树节点；path 为相对该层的目录前缀（层根为 ''，子目录以 / 结尾）。"""

    path: str = Field(description="相对层根：'' 表示层根；否则 posix 且以 / 结尾，如 reef/")
    title: str
    children: list["DataFolderTreeNode"] = Field(default_factory=list)


class DataStructureFolderCreateRequest(BaseModel):
    layer: LayerName
    name: str = Field(..., description="仅在层根下创建的单段目录名，例如 reef → raw/reef/")


class DataStructureFolderRenameRequest(BaseModel):
    layer: LayerName
    path: str = Field(..., description="要重命名的目录相对路径，如 reef/ 或 reef")
    new_name: str = Field(..., description="新目录名单段")


class DataStructureFolderOpResponse(BaseModel):
    ok: bool = True
    layer: LayerName
    path: str = Field(description="创建或重命名后的目录相对前缀（以 / 结尾）；删除时为被删路径")


DataFolderTreeNode.model_rebuild()
