# pathy-knowledge-server API 接口文档

> 版本：0.1.0 · 基于源码 `app/routers/*` 与 `app/models/schemas.py` 整理  
> 交互式文档：`GET /docs`（Swagger UI）、`GET /redoc`、`GET /openapi.json`

---

## 1. 概述

**pathy-knowledge-server** 是 Karpathy 式知识库的 REST 服务，提供四层本地存储（`raw` / `wiki` / `schema` / `media`）、LLM 编译与维护任务、wiki 双路召回（BM25 + 向量）、多媒体资源管理等功能。

| 项目 | 说明 |
|------|------|
| 默认 Base URL | `http://127.0.0.1:8765` |
| API 前缀 | 业务接口多为 `/api/v1/...`；健康检查为 `/health` |
| 内容类型 | JSON（`application/json`）；文件上传为 `multipart/form-data` |
| 鉴权 | **无内置 API Key**；假定部署在可信内网，公网需网关鉴权 |
| CORS | 允许任意来源（`allow_origins=["*"]`） |
| 请求追踪 | 响应头 `X-Request-ID`（可客户端传入同名请求头） |

### 1.1 存储层（Layer）

| 层 | 路径 | 说明 |
|----|------|------|
| `raw` | `DATA_ROOT/raw/` | 原始素材 |
| `wiki` | `DATA_ROOT/wiki/` | 编译后 wiki Markdown |
| `schema` | `DATA_ROOT/schema/` | 规范与 Agent 说明 |
| `media` | `DATA_ROOT/media/` | 图片/视频/APK 等；**不走** layers 文本读写，用 `/api/v1/media` |

环境变量 `DATA_ROOT` 默认 `./data`（相对进程工作目录）。

---

## 2. 通用约定

### 2.1 HTTP 状态码

| 状态码 | 场景 |
|--------|------|
| 200 | 成功 |
| 400 | 参数非法、media 层误用 layers 接口等 |
| 404 | 资源不存在（如删除不存在的 media code） |
| 413 | 上传超过 `max_file_bytes` 或媒体配额 |
| 415 | 文本上传无法解码为 UTF-8/GB18030 |

### 2.2 路径参数 `layer`

枚举值：`raw` | `wiki` | `schema` | `media`

- `media` 层仅支持 **列举目录** 与 **ZIP 打包下载**；文本读写请使用 `/api/v1/media`。

### 2.3 公共模型

#### TaskUsage（Token 用量）

```json
{
  "prompt_tokens": 100,
  "completion_tokens": 50,
  "total_tokens": 150
}
```

#### LLMFieldSource

`env` | `file` | `default` — 表示配置项生效来源。

---

## 3. 健康检查

### GET `/health`

存活检测，**无需鉴权**。

**响应** `HealthResponse`

```json
{
  "status": "ok",
  "service": "pathy-knowledge-server"
}
```

---

## 4. 元数据

### GET `/api/v1/config`

配置摘要（**不含密钥**）。

**响应** `ConfigSummaryResponse`

| 字段 | 类型 | 说明 |
|------|------|------|
| `data_root` | string | 数据根目录 |
| `data_root_resolved` | string | 解析后的绝对路径 |
| `llm_base_url` | string \| null | 生效的 LLM Base URL |
| `llm_model` | string | 生效的 LLM 模型 ID |
| `embedding_base_url` | string \| null | Embedding Base URL |
| `embedding_model` | string | Embedding 模型 ID |
| `rerank_base_url` | string \| null | Rerank Base URL |
| `rerank_model` | string | Rerank 模型 ID |
| `layers` | string[] | 固定 `["raw","wiki","schema","media"]` |

---

## 5. 模型配置

运行时配置写入 `DATA_ROOT/.pathy/llm.json` 与密钥文件；**进程环境变量同名项优先**（`env_locks` 标明是否被锁定）。

### 5.1 LLM

#### GET `/api/v1/settings/llm`

获取 LLM 有效配置与来源。

**响应** `LLMSettingsResponse`（节选）

| 字段 | 说明 |
|------|------|
| `openai_model` | 生效模型 |
| `openai_model_source` | `env` / `file` / `default` |
| `openai_base_url` | Base URL，可为 null |
| `openai_timeout_seconds` | 超时（秒） |
| `openai_max_tokens` | max_tokens |
| `openai_api_key_configured` | 是否已配置密钥 |
| `env_locks` | 各键是否被环境变量锁定 |
| `runtime_llm_json` | 默认 `.pathy/llm.json` |

#### PUT `/api/v1/settings/llm`

更新运行时 LLM 配置（写入数据目录）。

**请求体** `LLMSettingsUpdateRequest`

| 字段 | 类型 | 说明 |
|------|------|------|
| `openai_model` | string? | 非空 |
| `openai_base_url` | string? | 空字符串表示清除 |
| `openai_timeout_seconds` | float? | (0, 3600] |
| `openai_max_tokens` | int? | 1–200000 |
| `openai_api_key` | string? | 写入 `.pathy/openai_api_key`；空串删除文件 |

**响应** `LLMSettingsUpdateResult`：`settings` + `warnings[]`（环境变量锁定时写入被跳过）

#### POST `/api/v1/settings/llm/test`

测试 OpenAI 兼容连接（极小 Chat 请求，**不写盘**）。

**请求体** `LLMConnectionTestRequest`（均可选）

| 字段 | 说明 |
|------|------|
| `openai_model` | 覆盖本次探测的模型 |
| `openai_base_url` | 覆盖本次探测的 Base URL |

**响应** `LLMTestResponse`

| 字段 | 说明 |
|------|------|
| `ok` | 是否成功 |
| `model` | 使用的模型 |
| `base_url` | 使用的 Base URL |
| `elapsed_ms` | 耗时 |
| `message` | 说明 |
| `usage` | Token 用量 |
| `error` | 失败时的错误摘要 |

### 5.2 Embedding

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/settings/embedding` | 获取 embedding 配置 |
| PUT | `/api/v1/settings/embedding` | 更新（请求体 `BasicModelSettingsUpdateRequest`） |
| POST | `/api/v1/settings/embedding/test` | 连通性测试 |

**PUT 请求体** `BasicModelSettingsUpdateRequest`

| 字段 | 说明 |
|------|------|
| `model` | 模型 ID |
| `openai_base_url` | Base URL |
| `openai_timeout_seconds` | 超时 |
| `openai_max_tokens` | max_tokens |
| `openai_api_key` | 写入 `.pathy/embedding_api_key` |

**响应** `BasicModelSettingsUpdateResult`：`settings`（`BasicModelSettingsResponse`）+ `warnings`

### 5.3 Rerank

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/settings/rerank` | 获取 rerank 配置 |
| PUT | `/api/v1/settings/rerank` | 更新（同 Embedding 请求体结构） |
| POST | `/api/v1/settings/rerank/test` | 连通性测试 |

---

## 6. 三层存储（文本层）

前缀：`/api/v1/layers`

### 6.1 GET `/{layer}/entries`

列出目录（可按前缀）。

**Query**

| 参数 | 默认 | 说明 |
|------|------|------|
| `prefix` | `""` | 层内子路径前缀，如 `notes/` |

**响应** `ListLayerResponse`

```json
{
  "layer": "wiki",
  "prefix": "notes/",
  "entries": [
    {
      "name": "foo.md",
      "path": "notes/foo.md",
      "is_dir": false,
      "size": 1024,
      "embedding_status": "embedded"
    }
  ]
}
```

`embedding_status` 仅 wiki 文件：`embedded` | `stale` | `not_embedded`

### 6.2 GET `/{layer}/files`

递归列出层内文件相对路径（下拉选择等）。

**Query**

| 参数 | 默认 | 说明 |
|------|------|------|
| `suffix` | null | 如 `.md`，仅包含该后缀 |
| `max_files` | 5000 | 1–20000，超出则 `truncated: true` |

**响应** `LayerFileListResponse`：`layer`, `paths[]`, `truncated`

### 6.3 GET `/{layer}/file`

读取单个文本文件。

**Query**：`path`（必填）— 层内相对路径

**响应** `FileContentResponse`：`layer`, `path`, `content`, `size`

### 6.4 POST `/{layer}/upload`

上传文件（`multipart/form-data`）。

| 表单字段 | 说明 |
|----------|------|
| `file` | 文件内容（必填） |
| `path` | 层内相对路径；留空则用上传文件名的 basename |

编码：优先 UTF-8（含 BOM），失败尝试 GB18030。wiki 上传后会清理该文件向量索引。

**响应**：同 `FileContentResponse`

### 6.5 PUT `/{layer}/file`

创建或覆盖文件。

**Query**：`path`（必填）

**请求体** `FileWriteRequest`

```json
{ "content": "# Markdown 正文" }
```

wiki 写入后同步清理向量。`media` 层返回 400。

### 6.6 DELETE `/{layer}/file`

删除文件或目录。

**Query**：`path`（必填）

**响应**

```json
{ "ok": true, "deleted": "notes/foo.md" }
```

若 `forbid_delete_wiki_glob=true`，禁止删除 wiki 任意路径。

### 6.7 GET `/{layer}/archive.zip`

打包下载整层或子目录为 ZIP。

**Query**：`prefix`（可选子路径）

**响应**：`application/zip`，`Content-Disposition: attachment; filename="{layer}.zip"`

---

## 7. 存储结构（目录树维护）

前缀：`/api/v1/data-structure`

### 7.1 GET `/tree/{layer}`

某层目录树（**仅文件夹**）。

**Query**

| 参数 | 默认 | 范围 |
|------|------|------|
| `max_depth` | 16 | 1–32 |
| `max_nodes` | 400 | 1–2000 |

**响应** `DataFolderTreeNode`（递归）

```json
{
  "path": "",
  "title": "wiki",
  "children": [
    { "path": "reef/", "title": "reef", "children": [] }
  ]
}
```

`path`：层根为 `""`；子目录以 `/` 结尾。

### 7.2 POST `/folders`

在层根下新增**单层**子目录。

**请求体** `DataStructureFolderCreateRequest`

| 字段 | 说明 |
|------|------|
| `layer` | `raw` / `wiki` / `schema` / `media` |
| `name` | 单段目录名，如 `reef` → `raw/reef/` |

### 7.3 PATCH `/folders/rename`

重命名目录（**仅当目录为空**）。

**请求体** `DataStructureFolderRenameRequest`：`layer`, `path`, `new_name`

### 7.4 DELETE `/folders`

删除空目录。

**Query**：`layer`, `path`（如 `reef` 或 `reef/`）

**响应** `DataStructureFolderOpResponse`：`ok`, `layer`, `path`

---

## 8. LLM 任务

前缀：`/api/v1/tasks`（同步调用，需配置 LLM API Key）

### 8.1 POST `/compile`

编译任务：原始层 → 编译层。

**请求体** `CompileTaskRequest`

| 字段 | 必填 | 说明 |
|------|------|------|
| `input_paths` | 是 | raw 层相对路径列表，如 `["notes/foo.md"]` |
| `output_path` | 是 | wiki 层写入路径 |
| `schema_paths` | 否 | schema 注入路径；默认含 `AGENTS.md`（若存在） |
| `extra_instructions` | 否 | 附加编译说明 |

**响应** `CompileTaskResponse`：`model`, `usage`, `output_path`, `written_files[]`, `message`

### 8.2 POST `/lint`

Lint / 一致性报告。

**请求体** `LintTaskRequest`

| 字段 | 默认 | 说明 |
|------|------|------|
| `wiki_paths` | null | 待检查路径；空则扫描整个 wiki（谨慎） |
| `auto_fix` | false | 是否尝试自动改写 |
| `max_files` | 50 | 最多检查的文件数 |

**响应** `LintTaskResponse`：`model`, `usage`, `report`, `files_inspected[]`, `auto_fix_applied`

### 8.3 POST `/polish-text`

文本润色（规范层 Markdown 等）。

**请求体** `PolishTextRequest`

| 字段 | 说明 |
|------|------|
| `content` | 待润色 Markdown |
| `instruction` | 可选额外说明 |

**响应** `PolishTextResponse`：`content`, `model`, `usage`

---

## 9. Wiki 向量嵌入

前缀：`/api/v1/wiki`

索引文件：`DATA_ROOT/.pathy/wiki_embedding_index.json`

### POST `/embed`

手动嵌入单个 wiki 文件。

**请求体** `WikiEmbedRequest`

```json
{ "path": "notes/foo.md" }
```

仅支持 `.md`。

**响应** `WikiEmbedResponse`：`path`, `chunk_count`, `model`, `updated_at`, `message`

---

## 10. 对话召回

前缀：`/api/v1/dialogue`

召回方法标识：`hybrid_bm25_vector`（BM25 + Embedding 向量双路 → 合并去重 → 轻量 rerank）。

### 10.1 共用参数（`DialogueRecallBaseParams`）

| 字段 | 默认 | 范围 | 说明 |
|------|------|------|------|
| `query` | — | 必填 | 自然语言问句 |
| `wiki_prefixes` | `[]` | | 仅扫描这些 wiki 子路径（空为整层） |
| `max_files` | 80 | 1–500 | 最多扫描的 .md 数 |
| `bm25_top_n` | 10 | 1–100 | BM25 路候选数 |
| `vector_top_n` | 10 | 1–100 | 向量路候选数 |
| `top_k_chunks` | 6 | 1–32 | 最终注入条数 |
| `chunk_max_chars` | 1200 | 400–8000 | 分块最大字符 |
| `context_budget_chars` | 12000 | 2000–100000 | 参考资料总字符上限 |

### 10.2 POST `/recall`

**仅召回**，不调用 LLM。

**请求体**：`DialogueRecallRequest`（即共用参数）

**响应** `DialogueRecallResponse`

| 字段 | 说明 |
|------|------|
| `user_query` | 原始问句 |
| `recall_method` | `hybrid_bm25_vector` |
| `query_terms` | 参与打分的词项 |
| `files_scanned` | 扫描文件数 |
| `recall_hits` | 命中列表（`path`, `score`, `snippet`, `heading_path`） |
| `merged_media` | 命中片段内媒体，按 code 去重 |
| `bm25` / `vector` | 各路状态 `DialogueRecallLaneStatus` |
| `injected_context` | 拼接后的纯文本参考资料 |
| `context_truncated` | 是否因预算截断 |
| `message` | 补充说明 |

**`DialogueRecallLaneStatus.status` 取值**

- BM25：`ok` | `skipped_no_chunks` | `skipped_no_terms` | `no_hits`
- 向量：`ok` | `skipped_no_api_key` | `error_embedding`

### 10.3 POST `/recall-test`

召回 + 注入 LLM → 返回模型回答。

**请求体** `DialogueRecallTestRequest`：共用参数 + 可选 `system_prompt`

**响应** `DialogueRecallTestResponse`：在 `DialogueRecallResponse` 字段基础上增加 `assistant_reply`, `model`, `usage`

### 10.4 GET `/stopwords`

获取召回停用词（运行时文件优先于内置默认）。

**响应** `RecallStopwordsResponse`：`words[]`, `source`, `runtime_path`, `count`, `message`

### 10.5 PUT `/stopwords`

更新停用词（写入 `DATA_ROOT/.pathy/recall_stopwords.txt`）。

**请求体** `RecallStopwordsUpdateRequest`

```json
{ "words": ["的", "了", "是"] }
```

---

## 11. 多媒体（media）

前缀：`/api/v1/media`

存储：`DATA_ROOT/media/manifest.json` + `media/objects/...`  
Wiki 占位符：`![[MEDIA:code]]` 或 `<!-- media:code -->`

### 11.1 POST `/upload`

上传图片 / 视频 / APK。

**multipart 表单**

| 字段 | 说明 |
|------|------|
| `file` | 必填 |
| `title` | 可选标题 |
| `target_folder` | 可选，`media/` 下子目录；空则 `objects/aa/bb/...` |

同 `sha256` 自动去重。默认单文件上限 **200MB**（`MEDIA_MAX_UPLOAD_BYTES`）。

**响应** `MediaUploadResponse`：`code`, `deduplicated`, `mime`, `size`, `message`

### 11.2 GET `/items`

列出 manifest 中已登记媒体（路由须在 `/{code}` 之前注册）。

**响应** `MediaListResponse`：`items[]`, `count`, `bytes_total`

### 11.3 GET `/{code}`

按 code 下载媒体二进制；支持 **HTTP Range**（视频播放）。

**响应**：原始字节流，`Content-Type` 来自 manifest

### 11.4 GET `/{code}/backrefs`

查询引用该媒体的 wiki 位置（需先 `reindex-backrefs`）。

**响应** `MediaBackrefsResponse`：`code`, `entries[]`（`wiki_path`, `heading_path`）, `message`

### 11.5 POST `/reindex-backrefs`

扫描 wiki，重建 `.pathy/media_backrefs.json`。

**响应** `MediaReindexBackrefsResponse`：`codes_with_refs`, `total_ref_rows`, `message`

### 11.6 POST `/export-zip`

多选导出 ZIP。

**请求体** `MediaExportZipRequest`

```json
{ "codes": ["abc123", "def456"] }
```

ZIP 内含 `pathy_media_export.json`（manifest + wiki 反向引用）与各资源二进制。

**响应**：`application/zip`

### 11.7 POST `/import-zip`

从导出 ZIP 导入。

**multipart**

| 字段 | 说明 |
|------|------|
| `file` | 本服务导出的 zip |
| `target_folder` | 可选，`media/` 下子目录 |
| `target_dir` | **兼容旧字段**：`media/objects/` 下子目录；与 `target_folder` 同时存在时以 `target_folder` 为准 |

**响应** `MediaImportZipResponse`：`results[]`, `message`, `target_folder_normalized`, `warning`

**`MediaImportZipRow.status`**：`imported` | `remapped` | `skipped_identical` | `deduplicated_existing` | `error`

### 11.8 POST `/batch-delete`

批量删除。

**请求体** `MediaDeleteBatchRequest`：`codes[]`（1–5000）

**响应** `MediaDeleteBatchResponse`：`results[]`, `deleted_count`, `not_found_count`, `message`

### 11.9 DELETE `/{code}`

删除单条；不存在返回 **404**。

**响应** `MediaDeleteOneResponse`：`code`, `deleted`, `removed_file`, `message`

### 11.10 POST `/resolve-from-text`

解析正文中的媒体标签并查询 manifest。

**请求体** `MediaResolveFromTextRequest`

| 字段 | 说明 |
|------|------|
| `text` | 含 `![[MEDIA:…]]` 或 `<!-- media:… -->` 的正文 |
| `codes` | 额外 code 列表（如召回 `merged_media`） |

**响应** `MediaResolveFromTextResponse`：`codes[]`, `items[]`（含 `registered` 布尔）

### 11.11 GET `/meta/summary`

媒体层用量摘要（调试）：`count`, `bytes_registered`

---

## 12. 接口索引（速查）

| 方法 | 路径 | 标签 |
|------|------|------|
| GET | `/health` | 健康 |
| GET | `/api/v1/config` | 元数据 |
| GET/PUT | `/api/v1/settings/llm` | 模型配置 |
| POST | `/api/v1/settings/llm/test` | 模型配置 |
| GET/PUT | `/api/v1/settings/embedding` | 模型配置 |
| POST | `/api/v1/settings/embedding/test` | 模型配置 |
| GET/PUT | `/api/v1/settings/rerank` | 模型配置 |
| POST | `/api/v1/settings/rerank/test` | 模型配置 |
| GET | `/api/v1/layers/{layer}/entries` | 三层存储 |
| GET | `/api/v1/layers/{layer}/files` | 三层存储 |
| GET/PUT/DELETE | `/api/v1/layers/{layer}/file` | 三层存储 |
| POST | `/api/v1/layers/{layer}/upload` | 三层存储 |
| GET | `/api/v1/layers/{layer}/archive.zip` | 三层存储 |
| GET | `/api/v1/data-structure/tree/{layer}` | 存储结构 |
| POST/PATCH/DELETE | `/api/v1/data-structure/folders*` | 存储结构 |
| POST | `/api/v1/tasks/compile` | LLM 任务 |
| POST | `/api/v1/tasks/lint` | LLM 任务 |
| POST | `/api/v1/tasks/polish-text` | LLM 任务 |
| POST | `/api/v1/wiki/embed` | 向量嵌入 |
| POST | `/api/v1/dialogue/recall` | 对话召回 |
| POST | `/api/v1/dialogue/recall-test` | 对话召回 |
| GET/PUT | `/api/v1/dialogue/stopwords` | 对话召回 |
| POST | `/api/v1/media/upload` | 多媒体 |
| GET | `/api/v1/media/items` | 多媒体 |
| GET | `/api/v1/media/{code}` | 多媒体 |
| GET | `/api/v1/media/{code}/backrefs` | 多媒体 |
| DELETE | `/api/v1/media/{code}` | 多媒体 |
| POST | `/api/v1/media/reindex-backrefs` | 多媒体 |
| POST | `/api/v1/media/export-zip` | 多媒体 |
| POST | `/api/v1/media/import-zip` | 多媒体 |
| POST | `/api/v1/media/batch-delete` | 多媒体 |
| POST | `/api/v1/media/resolve-from-text` | 多媒体 |
| GET | `/api/v1/media/meta/summary` | 多媒体 |

---

## 13. 环境变量（与 API 行为相关）

| 变量 | 默认 | 说明 |
|------|------|------|
| `DATA_ROOT` | `./data` | 知识库根目录 |
| `OPENAI_API_KEY` | — | LLM 密钥 |
| `OPENAI_BASE_URL` | — | LLM 网关 |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM 模型 |
| `EMBEDDING_*` / `RERANK_*` | 见 README | 向量路与 rerank |
| `CONFIG_FILE` | — | 可选 YAML 配置 |
| `MEDIA_MAX_UPLOAD_BYTES` | 209715200 | 单媒体上传上限 |
| `MEDIA_TOTAL_QUOTA_BYTES` | 2147483648 | 媒体总配额 |
| `MEDIA_REINDEX_MAX_FILES` | 500 | 反向索引扫描 wiki 上限 |

完整列表见项目 [README.md](../README.md)。

---

## 14. 相关链接

- 本地 Swagger：`http://127.0.0.1:8765/docs`
- OpenAPI JSON：`http://127.0.0.1:8765/openapi.json`
- 前端管理端：同仓库 `pathy-knowledge-web`
