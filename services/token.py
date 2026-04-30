import jwt
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
import models
import os

SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
ALGORITHM = "HS256"

def create_access_token(user: models.User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=3)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user: models.User, db: Session) -> str:
    raw_token = secrets.token_urlsafe(64)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    db_token = models.RefreshToken(
        token_hash=token_hash,
        user_id=user.id,
        expires_at=expires_at,
        used=False,
    )
    db.add(db_token)
    db.commit()

    return raw_token


def verify_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Access token expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid access token")


def rotate_refresh_token(raw_token: str, db: Session):
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db_token = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash
    ).first()

    if not db_token:
        raise ValueError("Invalid refresh token")
    if db_token.used:
        raise ValueError("Refresh token already used")


    now = datetime.now(timezone.utc)

    expires_at = db_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        raise ValueError("Refresh token expired")

    db_token.used = True
    db.commit()

    user = db.query(models.User).filter(models.User.id == db_token.user_id).first()
    if not user:
        raise ValueError("User not found")

    if not user.is_active:
        raise ValueError("User account is deactivated")

        # Issue new pair
    new_access = create_access_token(user)
    new_refresh = create_refresh_token(user, db)

    return user, new_access, new_refresh


def invalidate_refresh_token(raw_token: str, db: Session):
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db_token = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash
    ).first()
    if db_token:
        db_token.used = True
        db.commit()