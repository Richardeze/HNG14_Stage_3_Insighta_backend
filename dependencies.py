from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Optional
from database import get_db
import models
from services.token import verify_access_token

bearer_scheme = HTTPBearer(auto_error=False)

def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:

    token = None

    if credentials and credentials.credentials:
        token = credentials.credentials  # from Authorization header

    if not token:
        token = request.cookies.get("access_token")  # from cookie

    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        payload = verify_access_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user = db.query(models.User).filter(models.User.id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated")

    return user


def require_analyst(user: models.User = Depends(get_current_user)) -> models.User:

    if user.role not in ("analyst", "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:

    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user