from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, Text
from database import Base
from datetime import datetime
from sqlalchemy.sql import func
import uuid6

def generate_uuid7():
    return str(uuid6.uuid7())

class Profile(Base):
    __tablename__ = "profiles"

    id = Column(String, primary_key=True, index=True, default=generate_uuid7)

    name = Column(String(2), unique=True, index=True, nullable=False)

    gender = Column(String, nullable=False)
    gender_probability = Column(Float, nullable=False)

    age = Column(Integer, nullable=False)
    age_group = Column(String, nullable=False)

    country_id = Column(String, nullable=False)
    country_name = Column(String, nullable=False)
    country_probability = Column(Float, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid7)
    github_id = Column(String, unique=True, nullable=False)
    username = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    avatar_url = Column(Text, nullable=True)
    role = Column(String(50), nullable=False, default="analyst")
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(String, primary_key=True, default=generate_uuid7)
    token_hash = Column(String(255), unique=True, nullable=False)
    user_id = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())