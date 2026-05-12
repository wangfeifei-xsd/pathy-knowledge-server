import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.deps import request_logging_middleware
from app.routers import dialogue_recall, health, layers, data_structure, llm_settings, meta, tasks, wiki_embedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_OPENAPI_TAGS = [
    {"name": "健康", "description": "存活检测（无需鉴权）"},
    {"name": "元数据", "description": "数据目录与配置摘要"},
    {
        "name": "模型配置",
        "description": "LLM 配置：`GET`/`PUT` `/api/v1/settings/llm`；连通性 `POST` `/api/v1/settings/llm/test`",
    },
    {"name": "三层存储", "description": "raw / wiki / schema 列举与文件读写"},
    {"name": "存储结构", "description": "data 下各层目录树查询；层根下单层子目录新增；空目录重命名与删除"},
    {"name": "LLM 任务", "description": "编译与 Lint 任务"},
    {
        "name": "对话召回",
        "description": "自然语言 → wiki BM25 + 向量双路召回（topN）→ 合并去重与轻量 rerank → topK 注入 LLM → 回答（测试流水线）",
    },
]

app = FastAPI(
    title="pathy-knowledge-server",
    description="Karpathy 式知识库 REST 服务（原始层 / 编译层 / 规范层）",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=_OPENAPI_TAGS,
)

app.middleware("http")(request_logging_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(meta.router)
app.include_router(llm_settings.router)
app.include_router(layers.router)
app.include_router(data_structure.router)
app.include_router(tasks.router)
app.include_router(dialogue_recall.router)
app.include_router(wiki_embedding.router)


@app.on_event("startup")
async def startup() -> None:
    settings = get_settings()
    settings.data_root.mkdir(parents=True, exist_ok=True)
    for name in ("raw", "wiki", "schema"):
        (settings.data_root / name).mkdir(parents=True, exist_ok=True)
