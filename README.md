# pathy-knowledge-server

Karpathy 式知识库 **REST** 服务（MVP）：**原始层 raw / 编译层 wiki / 规范层 schema**，OpenAPI 3 + Swagger UI，可选 Bearer 鉴权，OpenAI 兼容 Chat Completions。

## 项目简介

本服务实现「**由 LLM 维护的 Markdown 知识库**」的最小可部署形态：把素材放进原始层，在规范层约定下由 LLM 整理为带结构与交叉引用的编译层 wiki，并通过统一 REST 接口完成读写、编译与维护类任务。适合**本机单机**或**服务器单进程**部署，数据落在进程约定的本地目录，不依赖对象存储。

更完整的需求边界、验收与非目标见仓库内 **[需求方案](../docs/需求方案.md)**。

## 原理

| 概念 | 说明 |
|------|------|
| **原始层 `raw/`** | 未编译或半结构化来源（剪藏、摘录、上传的 Markdown/文本等），作为编译任务的输入。 |
| **编译层 `wiki/`** | 由 LLM 根据原始层与规范生成的 wiki 型 Markdown（索引、条目、交叉引用），编译任务写入目标。 |
| **规范层 `schema/`** | 约束目录、命名与 Agent 行为的说明（如 `AGENTS.md`），供服务与提示词引用；编译 / lint 前会注入或显式引用。 |

**数据流（闭环）**：向原始层写入素材 → 调用编译类接口 → 编译层出现对应条目与索引更新 → 可选 lint/报告类任务返回一致性说明。LLM 调用遵循 **OpenAI Chat Completions** 协议（`base_url` + `api_key` + `model`），便于切换兼容供应商。

**实现要点**：全部能力通过 **HTTP REST** 暴露；交互式 API 文档为 **OpenAPI 3 + Swagger UI**；持久化仅在 **`DATA_ROOT` 下本地文件系统** 内解析路径，禁止路径逃逸。

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
