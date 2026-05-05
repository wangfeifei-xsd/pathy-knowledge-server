from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.deps import verify_api_key
from app.models.schemas import WikiEmbedRequest, WikiEmbedResponse
from app.services.vector_index import embed_wiki_file

router = APIRouter(prefix="/api/v1/wiki", tags=["向量嵌入"], dependencies=[Depends(verify_api_key)])


@router.post("/embed", response_model=WikiEmbedResponse, summary="手动嵌入单个 wiki 文件")
async def post_wiki_embed(
    body: WikiEmbedRequest,
    settings: Settings = Depends(get_settings),
) -> WikiEmbedResponse:
    cnt, model, updated_at = await embed_wiki_file(settings, body.path)
    return WikiEmbedResponse(
        path=body.path,
        chunk_count=cnt,
        model=model,
        updated_at=updated_at,
        message="已完成嵌入并写入向量索引",
    )
