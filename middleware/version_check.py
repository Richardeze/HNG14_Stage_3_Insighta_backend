from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from fastapi.responses import JSONResponse


class APIVersionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/"):
            version = request.headers.get("X-API-Version")
            if version != "1":
                return JSONResponse(
                    status_code=400,
                    content={"status": "error", "message": "API version header required"}
                )
        return await call_next(request)