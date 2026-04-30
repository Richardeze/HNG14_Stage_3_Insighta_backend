from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from fastapi.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                return await call_next(request)

            csrf_header = request.headers.get("X-CSRF-Token")
            csrf_cookie = request.cookies.get("csrf_token")

            if not csrf_header or not csrf_cookie or csrf_header != csrf_cookie:
                return JSONResponse(
                    status_code=403,
                    content={"status": "error", "message": "CSRF validation failed"}
                )

        return await call_next(request)