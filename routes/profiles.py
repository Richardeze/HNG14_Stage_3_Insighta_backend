import csv
import io
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional
from main import limiter
from database import get_db
import models
from schemas import ProfileCreate, ProfileResponse, ProfileListResponse
from services.external_apis import fetch_external_data
from dependencies import require_admin, require_analyst
from uuid6 import uuid7

router = APIRouter(prefix="/api/profiles", tags=["Profiles"])


def build_links(base_path: str, page: int, limit: int, total: int, filters: dict) -> dict:
    """Build pagination prev/next/self links."""
    total_pages = (total + limit - 1) // limit

    def make_url(p: int) -> str:
        params = {k: v for k, v in filters.items() if v is not None}
        params["page"] = p
        params["limit"] = limit
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base_path}?{qs}"

    return {
        "self": make_url(page),
        "next": make_url(page + 1) if page < total_pages else None,
        "prev": make_url(page - 1) if page > 1 else None,
    }


def apply_profile_filters(query, gender, country_id, age_group, min_age, max_age,
                           min_gender_probability, min_country_probability):
    if gender:
        query = query.filter(models.Profile.gender == gender.lower())
    if country_id:
        query = query.filter(models.Profile.country_id == country_id.upper())
    if age_group:
        query = query.filter(models.Profile.age_group == age_group.lower())
    if min_age is not None:
        query = query.filter(models.Profile.age >= min_age)
    if max_age is not None:
        query = query.filter(models.Profile.age <= max_age)
    if min_gender_probability is not None:
        query = query.filter(models.Profile.gender_probability >= min_gender_probability)
    if min_country_probability is not None:
        query = query.filter(models.Profile.country_probability >= min_country_probability)
    return query


def apply_sorting(query, sort_by, order):
    valid_sort = ["age", "created_at", "gender_probability"]
    if sort_by:
        if sort_by not in valid_sort:
            return None, JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Invalid query parameters"}
            )
        column = getattr(models.Profile, sort_by)
        column = column.desc() if order == "desc" else column.asc()
        query = query.order_by(column)
    else:
        query = query.order_by(models.Profile.created_at.desc())
    return query, None


@router.get("", response_model=ProfileListResponse)
@limiter.limit("60/minute")
def get_profiles(
    request: Request,
    gender: Optional[str] = None,
    country_id: Optional[str] = None,
    age_group: Optional[str] = None,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    min_gender_probability: Optional[float] = None,
    min_country_probability: Optional[float] = None,
    sort_by: Optional[str] = None,
    order: str = "asc",
    page: int = 1,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_analyst),   # auth guard
):
    limit = max(1, min(limit, 50))
    page = max(1, page)

    query = db.query(models.Profile)
    query = apply_profile_filters(query, gender, country_id, age_group,
                                  min_age, max_age, min_gender_probability, min_country_probability)
    query, err = apply_sorting(query, sort_by, order)
    if err:
        return err

    total = query.count()
    total_pages = (total + limit - 1) // limit
    data = query.offset((page - 1) * limit).limit(limit).all()

    filters = dict(gender=gender, country_id=country_id, age_group=age_group,
                   min_age=min_age, max_age=max_age, sort_by=sort_by, order=order)

    return {
        "status": "success",
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "links": build_links("/api/profiles", page, limit, total, filters),
        "data": data,
    }


@router.get("/export")
@limiter.limit("60/minute")
def export_profiles(
    request: Request,
    format: str = Query("csv"),
    gender: Optional[str] = None,
    country_id: Optional[str] = None,
    age_group: Optional[str] = None,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    sort_by: Optional[str] = None,
    order: str = "asc",
    db: Session = Depends(get_db),
    user: models.User = Depends(require_analyst),
):
    if format != "csv":
        return JSONResponse(status_code=400, content={"status": "error", "message": "Only format=csv is supported"})

    query = db.query(models.Profile)
    query = apply_profile_filters(query, gender, country_id, age_group, min_age, None, None, None)
    query, err = apply_sorting(query, sort_by, order)
    if err:
        return err

    profiles = query.all()

    # Build CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "gender", "gender_probability", "age", "age_group",
                     "country_id", "country_name", "country_probability", "created_at"])

    for p in profiles:
        writer.writerow([
            p.id, p.name, p.gender, p.gender_probability, p.age, p.age_group,
            p.country_id, p.country_name, p.country_probability,
            p.created_at.isoformat() if p.created_at else "",
        ])

    output.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"profiles_{timestamp}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/search")
@limiter.limit("60/minute")
def search_profiles(
    request: Request,
    q: str = Query(...),
    page: int = 1,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_analyst),
):
    if not q or not q.strip():
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid query parameters"})

    limit = max(1, min(limit, 50))
    page = max(1, page)
    ql = q.lower().strip()

    gender = None
    min_age = None
    max_age = None
    country_id = None
    age_group = None

    has_male = "male" in ql
    has_female = "female" in ql
    if has_male and not has_female:
        gender = "male"
    elif has_female and not has_male:
        gender = "female"

    if "teenager" in ql or "teen" in ql:
        age_group = "teenager"
    elif "child" in ql or "children" in ql:
        age_group = "child"
    elif "senior" in ql or "elderly" in ql:
        age_group = "senior"
    elif "adult" in ql:
        age_group = "adult"

    if "young" in ql:
        min_age = 16
        max_age = 24

    words = ql.split()
    for i, w in enumerate(words):
        if w.isdigit():
            val = int(w)
            prev_word = words[i - 1] if i > 0 else ""
            if prev_word in ("above", "over", "older", "than"):
                min_age = val
            elif prev_word in ("below", "under", "younger"):
                max_age = val
            else:
                before = " ".join(words[:i])
                if any(kw in before for kw in ("above", "over")):
                    min_age = val
                elif any(kw in before for kw in ("below", "under")):
                    max_age = val

    country_map = {
        "nigeria": "NG", "kenya": "KE", "ghana": "GH", "angola": "AO",
        "benin": "BJ", "cameroon": "CM", "ethiopia": "ET", "south africa": "ZA",
        "tanzania": "TZ", "uganda": "UG", "senegal": "SN", "ivory coast": "CI",
        "rwanda": "RW", "mozambique": "MZ", "zambia": "ZM", "zimbabwe": "ZW",
        "mali": "ML", "niger": "NE", "burkina faso": "BF", "togo": "TG",
        "sierra leone": "SL", "liberia": "LR", "guinea": "GN",
    }
    for name, code in country_map.items():
        if name in ql:
            country_id = code
            break

    if not any([gender, min_age, max_age, country_id, age_group]):
        return JSONResponse(status_code=200, content={"status": "error", "message": "Unable to interpret query"})

    query = db.query(models.Profile)
    query = apply_profile_filters(query, gender, country_id, age_group, min_age, max_age, None, None)

    total = query.count()
    total_pages = (total + limit - 1) // limit
    data = query.offset((page - 1) * limit).limit(limit).all()

    return {
        "status": "success",
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "links": build_links("/api/profiles/search", page, limit, total, {"q": q}),
        "data": [ProfileResponse.model_validate(p) for p in data],
    }


@router.post("", status_code=201)
@limiter.limit("60/minute")
async def create_profile(
    request: Request,
    payload: ProfileCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),  # ADMIN ONLY
):
    name = payload.name.lower().strip()
    existing = db.query(models.Profile).filter(models.Profile.name == name).first()
    if existing:
        return {"status": "success", "message": "Profile already exists", "data": ProfileResponse.model_validate(existing)}

    data = await fetch_external_data(name)
    new_profile = models.Profile(
        id=str(uuid7()), name=name,
        gender=data["gender"], gender_probability=data["gender_probability"],
        age=data["age"], age_group=data["age_group"],
        country_id=data["country_id"], country_name=data["country_name"],
        country_probability=data["country_probability"],
    )
    db.add(new_profile)
    db.commit()
    db.refresh(new_profile)
    return {"status": "success", "data": ProfileResponse.model_validate(new_profile)}


@router.get("/{id}")
@limiter.limit("60/minute")
def get_profile(
    request: Request,
    id: str, db: Session = Depends(get_db),
    user: models.User = Depends(require_analyst)):
    profile = db.query(models.Profile).filter(models.Profile.id == id).first()
    if not profile:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Profile not found"})
    return {"status": "success", "data": ProfileResponse.model_validate(profile)}


@router.delete("/{id}", status_code=204)
@limiter.limit("60/minute")
def delete_profile(
    request:Request,
    id: str, db: Session = Depends(get_db),
    user: models.User = Depends(require_admin)):
    profile = db.query(models.Profile).filter(models.Profile.id == id).first()
    if not profile:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Profile not found"})
    db.delete(profile)
    db.commit()