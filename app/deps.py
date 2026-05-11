import logging
import time
import uuid

from fastapi import Request

logger = logging.getLogger("pathy")


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
