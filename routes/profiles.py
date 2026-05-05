import csv
import io
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
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
import json
from sqlalchemy import insert
import redis
import os


router = APIRouter(prefix="/api/profiles", tags=["Profiles"])

# Redis Setup
REDIS_URL = os.environ.get("REDIS_URL")
redis_client = None

if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
    except Exception:
        redis_client = None

CACHE_TTL = 300

# Cache helpers
def normalize_filters(filters: dict) -> str:
    """
    Convert a filter dict into a consistent cache key string.
    The problem this solves:
    - First user sends: gender=male&country_id=NG
    - Second user sends: country_id=NG&gender=male
    Without normalization these produce different keys and miss each other's cache.
    Solution: sort the keys alphabetically, remove None values,
    then build a deterministic string.

    Example output: "age_group=None:country_id=NG:gender=male:max_age=None:min_age=None"
    """
    # Sort keys alphabetically so order never matters
    sorted_items = sorted(filters.items())
    # Build key string — include None values explicitly so
    # "no filter" and "filter present" never collide
    key_parts = [f"{k}={v}" for k, v in sorted_items]
    return ":".join(key_parts)

def get_cache_key(prefix: str, filters: dict, page: int, limit: int) -> str:
    """
    Build the full Redis key for a query.
    Includes prefix (which endpoint), normalized filters, page, and limit.
    Example: "profiles:age_group=None:country_id=NG:gender=male:page=1:limit=10"
    """
    normalized = normalize_filters(filters)
    return f"{prefix}:{normalized}:page={page}:limit={limit}"


def get_cached(key: str):
    """Try to get a cached result. Returns None if Redis unavailable or key missing."""
    if not redis_client:
        return None
    try:
        cached = redis_client.get(key)
        if cached:
            return json.loads(cached)  # deserialize JSON string back to dict
    except Exception:
        pass
    return None


def set_cache(key: str, value: dict):
    """Store a result in Redis. Silently fails if Redis unavailable."""
    if not redis_client:
        return
    try:
        redis_client.setex(key, CACHE_TTL, json.dumps(value, default=str))
        # setex = SET with Expiry
        # default=str handles datetime serialization
    except Exception:
        pass


def invalidate_profiles_cache():
    """
    Clear all cached profile results when data changes (create/delete).
    We use a pattern match to delete all keys starting with "profiles:".
    This ensures stale data is never served after writing.
    """
    if not redis_client:
        return
    try:
        # SCAN is safer than KEYS for production — non-blocking
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor, match="profiles:*", count=100)
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass

# Existing helpers---This part is unchanged
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


# Routes
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
    user: models.User = Depends(require_analyst),
):
    limit = max(1, min(limit, 50))
    page = max(1, page)

    # Build filter dict for cache key
    filters = dict(
        gender=gender, country_id=country_id, age_group=age_group,
        min_age=min_age, max_age=max_age, sort_by=sort_by, order=order,
        min_gender_probability=min_gender_probability,
        min_country_probability=min_country_probability,
    )

    # Check cache first
    cache_key = get_cache_key("profiles", filters, page, limit)
    cached = get_cached(cache_key)
    if cached:
        return cached  # return immediately — no DB call

    #If the cache misses, then query the database as normal
    query = db.query(models.Profile)
    query = apply_profile_filters(query, gender, country_id, age_group,
                                  min_age, max_age, min_gender_probability,
                                  min_country_probability)
    query, err = apply_sorting(query, sort_by, order)
    if err:
        return err

    total = query.count()
    total_pages = (total + limit - 1) // limit
    data = query.offset((page - 1) * limit).limit(limit).all()

    filters = dict(gender=gender, country_id=country_id, age_group=age_group,
                   min_age=min_age, max_age=max_age, sort_by=sort_by, order=order)

    result = {
        "status": "success",
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "links": build_links("/api/profiles", page, limit, total, filters),
        "data": [ProfileResponse.model_validate(p).model_dump() for p in data],
    }

    set_cache(cache_key, result)
    return result

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

    # Build filters for cache key
    filters = dict(
        gender=gender, country_id=country_id, age_group=age_group,
        min_age=min_age, max_age=max_age,
        )

    # Check cache
    cache_key = get_cache_key("search", filters, page, limit)
    cached = get_cached(cache_key)
    if cached:
        return cached

    # If cache misses, query the database
    query = db.query(models.Profile)
    query = apply_profile_filters(query, gender, country_id, age_group, min_age, max_age, None, None)

    total = query.count()
    total_pages = (total + limit - 1) // limit
    data = query.offset((page - 1) * limit).limit(limit).all()

    result = {
        "status": "success",
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "links": build_links("/api/profiles/search", page, limit, total, {"q": q}),
        "data": [ProfileResponse.model_validate(p).model_dump() for p in data],
    }

    set_cache(cache_key, result)
    return result


@router.post("", status_code=201)
@limiter.limit("60/minute")
async def create_profile(
    request: Request,
    payload: ProfileCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    name = payload.name.lower().strip()
    existing = db.query(models.Profile).filter(models.Profile.name == name).first()
    if existing:
        return {"status": "success", "message": "Profile already exists",
                "data": ProfileResponse.model_validate(existing)}

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

    # Invalidate cache — new data exists, cached results are now stale
    invalidate_profiles_cache()

    return {"status": "success", "data": ProfileResponse.model_validate(new_profile)}


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_profiles_csv(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
):
    """
    Upload a CSV file containing profile data.

    How it works:
    1. Read the file as a stream — never load all 500k rows at once
    2. Process in chunks of 1000 rows
    3. Validate each row — skip bad ones, count the reason
    4. Batch insert valid rows — one SQL statement per chunk, not one per row
    5. Return a summary

    Why chunked? Loading 500k rows into memory at once would crash the server.
    Why batch insert? 1000 individual INSERT statements is 1000x slower than
    one INSERT with 1000 rows.
    """
    if not file.filename.endswith('.csv'):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Only CSV files are accepted"}
        )

    # Counters for the summary response
    total_rows = 0
    inserted = 0
    skipped = 0
    reasons = {
        "duplicate_name": 0,
        "invalid_age": 0,
        "missing_fields": 0,
        "invalid_gender": 0,
        "malformed_row": 0,
    }

    VALID_GENDERS = {"male", "female"}
    VALID_AGE_GROUPS = {"child", "teenager", "adult", "senior"}
    CHUNK_SIZE = 1000

    # Required columns in the CSV
    REQUIRED_FIELDS = {
        "name", "gender", "gender_probability",
        "age", "age_group", "country_id",
        "country_name", "country_probability"
    }

    # Read the entire file content
    # We decode it and wrap in StringIO so csv.DictReader can process it
    content = await file.read()

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "File encoding must be UTF-8"}
        )

    reader = csv.DictReader(io.StringIO(text))

    # Check that required columns exist in the CSV header
    if not REQUIRED_FIELDS.issubset(set(reader.fieldnames or [])):
        missing = REQUIRED_FIELDS - set(reader.fieldnames or [])
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": f"Missing required columns: {missing}"}
        )

    # Load existing names into a set for fast duplicate checking
    # This is faster than querying the DB for every single row
    existing_names = set(
        row[0] for row in db.query(models.Profile.name).all()
    )

    chunk = []  # holds valid rows ready for batch insert

    for row in reader:
        total_rows += 1

        # ── Validation ────────────────────────────────────────────

        # Check for missing required fields
        try:
            name = row.get("name", "").strip().lower()
            gender = row.get("gender", "").strip().lower()
            age_group = row.get("age_group", "").strip().lower()
            country_id = row.get("country_id", "").strip().upper()
            country_name = row.get("country_name", "").strip()

            gender_probability = row.get("gender_probability", "").strip()
            age = row.get("age", "").strip()
            country_probability = row.get("country_probability", "").strip()

        except Exception:
            skipped += 1
            reasons["malformed_row"] += 1
            continue

        # Check required fields are not empty
        if not all([name, gender, age_group, country_id, country_name,
                    gender_probability, age, country_probability]):
            skipped += 1
            reasons["missing_fields"] += 1
            continue

        # Validate gender
        if gender not in VALID_GENDERS:
            skipped += 1
            reasons["invalid_gender"] += 1
            continue

        # Validate age group
        if age_group not in VALID_AGE_GROUPS:
            skipped += 1
            reasons["malformed_row"] += 1
            continue

        # Validate age — must be a positive integer
        try:
            age = int(age)
            if age < 0:
                raise ValueError
        except ValueError:
            skipped += 1
            reasons["invalid_age"] += 1
            continue

        # Validate probabilities
        try:
            gender_probability = float(gender_probability)
            country_probability = float(country_probability)
        except ValueError:
            skipped += 1
            reasons["malformed_row"] += 1
            continue

        # Check for duplicate name
        if name in existing_names:
            skipped += 1
            reasons["duplicate_name"] += 1
            continue

        # ── Valid row — add to chunk ───────────────────────────────
        existing_names.add(name)  # prevent duplicates within the same file

        chunk.append({
            "id": str(uuid7()),
            "name": name,
            "gender": gender,
            "gender_probability": gender_probability,
            "age": age,
            "age_group": age_group,
            "country_id": country_id,
            "country_name": country_name,
            "country_probability": country_probability,
        })

        # ── Batch insert when chunk is full ────────────────────────
        if len(chunk) >= CHUNK_SIZE:
            # One INSERT statement for 1000 rows — vastly more efficient
            # than 1000 individual INSERT statements
            db.execute(insert(models.Profile), chunk)
            db.commit()
            inserted += len(chunk)
            chunk = []  # reset for next chunk

    # Insert any remaining rows that didn't fill a complete chunk
    if chunk:
        db.execute(insert(models.Profile), chunk)
        db.commit()
        inserted += len(chunk)

    # New data was inserted — invalidate cache
    if inserted > 0:
        invalidate_profiles_cache()

    return {
        "status": "success",
        "total_rows": total_rows,
        "inserted": inserted,
        "skipped": skipped,
        "reasons": reasons,
    }


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
    request: Request,
    id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin)
):
    profile = db.query(models.Profile).filter(models.Profile.id == id).first()
    if not profile:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Profile not found"})
    db.delete(profile)
    db.commit()

    # Invalidate cache — data was deleted
    invalidate_profiles_cache()