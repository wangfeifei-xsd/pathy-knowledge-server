# pathy-knowledge-server

Karpathy 式知识库 **REST** 服务（MVP）：**原始层 raw / 编译层 wiki / 规范层 schema**，OpenAPI 3 + Swagger UI，可选 Bearer 鉴权，OpenAI 兼容 Chat Completions。

## 项目简介

本服务实现「**由 LLM 维护的 Markdown 知识库**」的最小可部署形态：把素材放进原始层，在规范层约定下由 LLM 整理为带结构与交叉引用的编译层 wiki，并通过统一 REST 接口完成读写、编译与维护类任务。适合**本机单机**或**服务器单进程**部署，数据落在进程约定的本地目录，不依赖对象存储。

## 原理

| 概念 | 说明 |
|------|------|
| **原始层 `raw/`** | 未编译或半结构化来源（剪藏、摘录、上传的 Markdown/文本等），作为编译任务的输入。 |
| **编译层 `wiki/`** | 由 LLM 根据原始层与规范生成的 wiki 型 Markdown（索引、条目、交叉引用），编译任务写入目标。 |
| **规范层 `schema/`** | 约束目录、命名与 Agent 行为的说明（如 `AGENTS.md`），供服务与提示词引用；编译 / lint 前会注入或显式引用。 |

**数据流（闭环）**：向原始层写入素材 → 调用编译类接口 → 编译层出现对应条目与索引更新 → 可选 lint/报告类任务返回一致性说明。LLM 调用遵循 **OpenAI Chat Completions** 协议（`base_url` + `api_key` + `model`），便于切换兼容供应商。

**实现要点**：全部能力通过 **HTTP REST** 暴露；交互式 API 文档为 **OpenAPI 3 + Swagger UI**；持久化仅在 **`DATA_ROOT` 下本地文件系统** 内解析路径，禁止路径逃逸。

## 对话召回（`bm25`）

自然语言问句在 **wiki 编译层** 上做片段召回，实现见 `app/services/dialogue_recall.py`，停用词表见 `app/services/recall_stopwords.py`。接口：`POST /api/v1/dialogue/recall`（仅召回）、`POST /api/v1/dialogue/recall-test`（召回后拼进用户消息再调 LLM）。**不写回 wiki 文件**。

### 与「解析词条再匹配」的区别

- **没有**向量嵌入。  
- **没有**「先建独立词条表再逐条比对」；但会按 **Markdown 标题层级** 将正文切成带 **标题路径**（如 `父 > 子`）的片段，并在该路径上为 query term 命中**额外加权**。  
- 排序使用 **Okapi BM25**（在当次请求扫描到的所有片段上现算 **IDF**），比简单「子串出现次数求和」更抗常见词泛匹配。

### 处理步骤（与代码对应）

1. **问句 → terms**（`_extract_query_terms` + `_filter_terms`）  
   正则切出中文连续段、英文数字连续段。英文数字段长度 ≥ 2 记为一个 term（小写）。中文：**不**再使用单字 term；长度 ≤ 8 的整段可成 term；更长中文再生成**相邻二字（bigram）**。然后经 **停用词表** 过滤（`recall_stopwords.STOPWORDS`）。

2. **读 wiki**（`_collect_wiki_pairs`）  
   在 `wiki_prefix` 下扫描 `*.md`（或单文件），最多 `max_files` 篇。

3. **全文 → 片段**（`_wiki_indexed_chunks`）  
   若文中存在 ATX 标题行（`#`…`######`），则按标题栈拆成多节，每节带 `heading_path`；节内过长仍用原 **滑窗**（`_split_chunks`）再切。若**无**标题，则整篇只走滑窗切块（`heading_path` 为空）。

4. **BM25 打分**（`_score_chunks_bm25`）  
   以「`heading_path` + 正文」拼成**匹配用全文**（小写子串计 **TF**），在当次语料上算各 term 的 **DF/IDF**，对每段求 **Okapi BM25**；若 term 还出现在 `heading_path` 中，按 **IDF 加一项标题奖励**（避免纯常见词在标题里刷分，与 TF 项共用 IDF 尺度）。

5. **排序与截断**  
   正分片段按分数降序，只取前 **`top_k_chunks`**；再按 **`context_budget_chars`** 做块级截断。注入格式为 `### 文件相对路径` + 可选 `**标题路径**` + 正文。

6. **输出**  
   - `recall_method` 字段为 `bm25`。  
   - `query_terms` 为**过滤停用词之后**实际参与打分的词项。

### 流程图

```mermaid
flowchart TD
  A[用户 query] --> B[_extract_query_terms]
  B --> C[_filter_terms 停用词]
  D[wiki_prefix + max_files] --> E[_collect_wiki_pairs]
  E --> F["每篇 markdown"]
  F --> G["_wiki_indexed_chunks 标题切块或滑窗"]
  G --> H[索引片段列表]
  C --> I["_score_chunks_bm25"]
  H --> I
  I --> J{BM25 > 0?}
  J -->|否| K[丢弃]
  J -->|是| L[排序取 top_k_chunks]
  L --> M["格式化块 + _trim_context"]
  M --> N["injected_context + recall_hits"]
```

### 时序图（仅召回 vs 带 LLM）

```mermaid
sequenceDiagram
  participant Client as 调用方
  participant API as FastAPI
  participant Recall as perform_wiki_keyword_recall
  participant FS as wiki 存储
  participant LLM as OpenAI 兼容 API

  Client->>API: POST recall 或 recall-test
  API->>Recall: query 与 wiki_prefix 等参数
  Recall->>FS: 列举并读取 .md
  FS-->>Recall: 多文件全文
  Note over Recall: 抽词过滤 → 标题/滑窗切块 → BM25 topN → 预算截断
  Recall-->>API: injected_context, hits 等

  alt 仅 recall
    API-->>Client: JSON 含 injected_context
  else recall-test
    API->>LLM: system + user(问题 + 参考资料)
    LLM-->>API: assistant_reply
    API-->>Client: JSON 含召回字段与模型回答
  end
```

## 快速开始

```bash
cd pathy-knowledge-server
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

浏览器打开：`http://127.0.0.1:8765/docs`（Swagger）、`http://127.0.0.1:8765/health`。

## 重启服务

服务是 **Uvicorn 单进程**，修改了环境变量、`.env`、依赖或代码后，需要**停掉旧进程再启动**新进程才会生效（`get_settings()` 等也会在重启后重新加载）。

**前台运行**（终端里直接执行的 `uvicorn`）：在该终端按 **`Ctrl + C`** 结束进程，再执行与上文相同的 `uvicorn app.main:app ...` 命令。

**后台或占用端口时**（示例端口 `8765`，按你实际端口修改）：

```bash
# 按端口查 PID 并结束
lsof -i :8765
kill <PID>

# 或按命令行匹配进程结束（慎用多实例）
pkill -f "uvicorn app.main:app"

# 然后再前台启动；或用 nohup/systemd/docker compose 等你已有的托管方式拉起
```

确认重启的是**当前项目目录**下的虚拟环境与代码，避免旧目录或旧 Docker 镜像仍在运行。

## 环境变量（节选）

| 变量 | 说明 |
|------|------|
| `DATA_ROOT` | 数据根目录，默认 `./data`（相对进程工作目录） |
| `OPENAI_API_KEY` | LLM 密钥（不写入日志与响应） |
| `OPENAI_BASE_URL` | 可选，兼容网关 |
| `OPENAI_MODEL` | 默认模型名，默认 `gpt-4o-mini` |
| `API_KEY` | 若设置，则 `/api/*` 需 `Authorization: Bearer <token>` |
| `CONFIG_FILE` | 可选 YAML 配置文件路径；同名字段可被环境变量覆盖 |

## 目录结构

在 `DATA_ROOT` 下自动创建：

- `raw/` — 原始层  
- `wiki/` — 编译层  
- `schema/` — 规范层（如 `AGENTS.md`）

运行时 LLM 配置（可由 Web「模型配置」页或 `PUT /api/v1/settings/llm` 写入）：

- `.pathy/llm.json` — 模型名、base_url、超时、max_tokens（**进程环境变量同名项优先**）
- `.pathy/openai_api_key` — 可选密钥文件（权限尽量 `0600`；若已设置 `OPENAI_API_KEY` 环境变量则不会写入）
- 连通性探测：`POST /api/v1/settings/llm/test`（极小 Chat 请求，可选 body 覆盖本次测试用的 `openai_model` / `openai_base_url`）

备份与迁移：复制整个 `DATA_ROOT` 目录即可。

## 安全说明

生产环境建议启用 `API_KEY` 并置于 HTTPS 反向代理之后；所有文件路径在数据根内规范化解析，禁止 `../` 逃逸。
