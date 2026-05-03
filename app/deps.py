import logging
import time
import uuid
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

logger = logging.getLogger("pathy")

security = HTTPBearer(auto_error=False)


async def request_logging_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = rid
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = rid
    logger.info(
        "request id=%s %s %s -> %s %.2fms",
        rid,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def verify_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
) -> None:
    expected = settings.api_key
    if not expected:
        return
    token = creds.credentials if creds and creds.scheme.lower() == "bearer" else None
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="未授权或无效令牌")
