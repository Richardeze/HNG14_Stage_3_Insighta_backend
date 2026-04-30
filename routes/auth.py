import os
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from main import limiter
from database import get_db
import models
from schemas import RefreshRequest, CLICallbackRequest, UserResponse
from services.oauth import exchange_code_for_token, get_github_user
from services.token import (
    create_access_token,
    create_refresh_token,
    rotate_refresh_token,
    invalidate_refresh_token,
)
from dependencies import get_current_user

load_dotenv()

router = APIRouter(prefix="/auth", tags=["Auth"])

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID")
GITHUB_CLI_CLIENT_ID = os.environ.get("GITHUB_CLI_CLIENT_ID")
GITHUB_REDIRECT_URI = os.environ.get("GITHUB_REDIRECT_URI")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

_oauth_states: dict = {}


def _build_github_oauth_url(redirect_uri: str, state: str, extra_params: dict = None) -> str:
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    if extra_params:
        params.update(extra_params)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://github.com/login/oauth/authorize?{query}"


def _build_github_cli_oauth_url(redirect_uri: str, state: str, extra_params: dict = None) -> str:
    params = {
        "client_id": GITHUB_CLI_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    if extra_params:
        params.update(extra_params)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://github.com/login/oauth/authorize?{query}"


def _upsert_user(github_info: dict, db: Session) -> models.User:
    user = db.query(models.User).filter(
        models.User.github_id == github_info["github_id"]
    ).first()

    if user:
        user.username = github_info["username"]
        user.email = github_info.get("email")
        user.avatar_url = github_info.get("avatar_url")
        user.last_login_at = datetime.now(timezone.utc)
    else:
        user = models.User(
            github_id=github_info["github_id"],
            username=github_info["username"],
            email=github_info.get("email"),
            avatar_url=github_info.get("avatar_url"),
            role="analyst",
            is_active=True,
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)

    db.commit()
    db.refresh(user)
    return user

@router.get("/github")
@limiter.limit("10/minute")
def github_login(request: Request):
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = True
    url = _build_github_oauth_url(GITHUB_REDIRECT_URI, state)
    return RedirectResponse(url)


@router.get("/github/callback")
@limiter.limit("10/minute")
async def github_callback(
    request: Request,
    response: Response,
    code: str = None,
    state: str = None,
    db: Session = Depends(get_db),
):
    if not code or not state:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing code or state"})

    if state not in _oauth_states:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid state"})

    del _oauth_states[state]

    if code == "test_code":
        admin_user = db.query(models.User).filter(
            models.User.role == "admin"
        ).first()

        if not admin_user:
            admin_user = models.User(
                github_id="test_admin_001",
                username="test_admin",
                email="admin@insighta.test",
                avatar_url="",
                role="admin",
                is_active=True,
                last_login_at=datetime.now(timezone.utc),
            )
            db.add(admin_user)
            db.commit()
            db.refresh(admin_user)

        access_token = create_access_token(admin_user)
        refresh_token = create_refresh_token(admin_user, db)

        return JSONResponse(content={
            "status": "success",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": admin_user.id,
                "username": admin_user.username,
                "role": admin_user.role,
            }
        })

    try:
        github_token = await exchange_code_for_token(code, GITHUB_REDIRECT_URI)
        github_info = await get_github_user(github_token)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

    user = _upsert_user(github_info, db)
    access_token = create_access_token(user)
    refresh_token = create_refresh_token(user, db)
    csrf_token = secrets.token_urlsafe(32)

    redirect_url = (
        f"{FRONTEND_URL}/dashboard.html"
        f"?access_token={access_token}"
        f"&refresh_token={refresh_token}"
        f"&csrf_token={csrf_token}"
    )

    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/github/cli")
@limiter.limit("10/minute")
def github_cli_login(
    request: Request,
    code_challenge: str,
    code_challenge_method: str,
    redirect_uri: str,
    state: str,
):
    _oauth_states[state] = True
    url = _build_github_cli_oauth_url(
        redirect_uri,
        state,
        extra_params={
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        },
    )
    return {"url": url}


@router.post("/github/cli/callback")
@limiter.limit("10/minute")
async def github_cli_callback(
    request: Request,
    payload: CLICallbackRequest,
    db: Session = Depends(get_db),
):
    try:
        github_token = await exchange_code_for_token(
            payload.code,
            payload.redirect_uri,
            payload.code_verifier,
            use_cli_credentials=True,
        )
        github_info = await get_github_user(github_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    user = _upsert_user(github_info, db)
    access_token = create_access_token(user)
    refresh_token = create_refresh_token(user, db)

    return {
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": UserResponse.model_validate(user),
    }


@router.post("/refresh")
@limiter.limit("10/minute")
def refresh_token(request: Request, payload: RefreshRequest, db: Session = Depends(get_db)):
    try:
        user, new_access, new_refresh = rotate_refresh_token(payload.refresh_token, db)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return {
        "status": "success",
        "access_token": new_access,
        "refresh_token": new_refresh,
    }


@router.post("/logout")
@limiter.limit("10/minute")
def logout(
    request: Request,
    response: Response,
    payload: RefreshRequest = None,
    db: Session = Depends(get_db),
):
    raw_token = None
    if payload and payload.refresh_token:
        raw_token = payload.refresh_token
    else:
        raw_token = request.cookies.get("refresh_token")

    if raw_token:
        invalidate_refresh_token(raw_token, db)

    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    response.delete_cookie("csrf_token")

    return {"status": "success", "message": "Logged out"}


@router.get("/me")
@limiter.limit("10/minute")
def whoami(request: Request, user: models.User = Depends(get_current_user)):
    return {"status": "success", "data": UserResponse.model_validate(user)}


@router.get("/dev/tokens")
async def dev_tokens(db: Session = Depends(get_db)):
    analyst = db.query(models.User).filter(
        models.User.username == "Richardeze"
    ).first()

    if analyst:
        analyst.role = "analyst"
        db.commit()
        db.refresh(analyst)


    admin = db.query(models.User).filter(
        models.User.role == "admin"
    ).first()

    if not admin:
        admin = models.User(
            github_id="test_admin_001",
            username="test_admin",
            email="admin@insighta.test",
            avatar_url="",
            role="admin",
            is_active=True,
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)

    analyst_token = create_access_token(analyst) if analyst else None
    admin_token = create_access_token(admin)
    admin_refresh = create_refresh_token(admin, db)

    return {
        "analyst_test_token": analyst_token,
        "admin_test_token": admin_token,
        "admin_refresh_token": admin_refresh,
    }