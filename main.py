import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv

load_dotenv()

limiter = Limiter(key_func=get_remote_address)

from database import Base, engine
from routes.profiles import router as profiles_router
from routes.auth import router as auth_router
from middleware.version_check import APIVersionMiddleware
from middleware.logging_middleware import RequestLoggingMiddleware
from middleware.csrf_middleware import CSRFMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(title="Insighta Labs+", version="1.0.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5500")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:5500", "http://127.0.0.1:5500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Version", "X-CSRF-Token"],
)
app.add_middleware(CSRFMiddleware)
app.add_middleware(APIVersionMiddleware)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(auth_router)
app.include_router(profiles_router)


@app.get("/")
def root():
    return {"status": "success", "message": "Insighta Labs+ API v1"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail}
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": "Invalid input"}
    )


port = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)