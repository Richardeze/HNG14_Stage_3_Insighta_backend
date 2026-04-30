import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("insighta")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = round((time.time() - start) * 1000, 2)

        logger.info(
            f"{request.method} {request.url.path} "
            f"→ {response.status_code} ({duration_ms}ms)"
        )
        return response