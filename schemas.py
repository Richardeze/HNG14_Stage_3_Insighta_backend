from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional


class ProfileCreate(BaseModel):
    name: str


class ProfileResponse(BaseModel):
    id: str
    name: str
    gender: str
    gender_probability: float
    age: int
    age_group: str
    country_id: str
    country_name: str
    country_probability: float
    created_at: datetime

    class Config:
        from_attributes = True


class PaginationLinks(BaseModel):
    self: str
    next: Optional[str] = None
    prev: Optional[str] = None


class ProfileListResponse(BaseModel):
    status: str
    page: int
    limit: int
    total: int
    total_pages: int
    links: PaginationLinks
    data: List[ProfileResponse]

    class Config:
        from_attributes = True


class UserResponse(BaseModel):
    id: str
    github_id: str
    username: str
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    status: str
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class RefreshRequest(BaseModel):
    refresh_token: str


class CLICallbackRequest(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str