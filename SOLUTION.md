# SOLUTION.md — Stage 4B: System Optimization & Data Ingestion

## 1. Query Performance Optimization

### What I changed and why

**Database indexes (`models.py`)**

Before this stage, only `id` and `name` had indexes on the Profile table. Every filter query — by gender, country, age, 
age_group — performed a full table scan, reading every row to find matches.
I added `index=True` to the following columns: `gender`, `country_id`, `age`, `age_group`, `created_at`.
These are exactly the columns used in filter queries. PostgreSQL now builds a lookup structure for each one, allowing it
to jump directly to matching rows instead of scanning the entire table.

Trade-off: indexes slightly slow down INSERT operations because the index must be updated for each new row. Given that 
this system is read-heavy (analysts query far more than admins write), this trade-off is justified.

**Connection pooling (`database.py`)**

Before: SQLAlchemy used default pool settings with no explicit configuration. Under concurrent load, this risked 
exhausting Neon's connection limit and added per-request connection overhead.

After: Configured QueuePool with:
- `pool_size=10` — 10 permanent connections always ready
- `max_overflow=10` — up to 10 additional connections during spikes (20 total max)
- `pool_recycle=1800` — recycle connections every 30 minutes to prevent stale connection errors from Neon's idle timeout
- `pool_pre_ping=True` — test each connection before use, automatically replacing dead ones

Trade-off: The pool holds connections open during quiet periods. This is acceptable given Neon's free tier connection 
limit.

**Redis caching (`routes/profiles.py`)**

Before: Every request to `GET /api/profiles` and `GET /api/profiles/search` hit the database regardless of whether the 
same query had been run recently.

After: Before executing any database query, the system checks Redis for a cached result. When it hits a cache, the 
result is returned immediately. And when it misses a cache, the database is queried and the result is stored in Redis 
with a 5-minute TTL.

Cache is invalidated immediately when profiles are created, deleted, or uploaded in bulk — ensuring users never see results that are more than 5 minutes stale after writing.

If Redis is unavailable, the system falls back to querying the database directly. The cache layer is optional, not required for correctness.

### Before/After comparison

| Scenario | Before (estimated) | After (estimated) |
|---|---|---|
| First request, no cache, no indexes | 800ms–3s at scale | 80–200ms with indexes |
| Repeated identical query | 800ms–3s (hits DB every time) | <10ms (Redis cache hit) |
| Concurrent connections at peak | Risk of connection limit errors | Stable, max 20 connections |

Note: These are estimates based on typical PostgreSQL behavior with and without indexes on filtered columns. Actual 
numbers depend on dataset size and hardware.

---

## 2. Query Normalization

### The problem

Users express the same query in different parameter orders:
- `gender=male&country_id=NG`
- `country_id=NG&gender=male`

Without normalization, these produce different cache keys and bypass each other's cached results, causing redundant 
database calls.

### The solution

Before checking the cache, all filter parameters are normalized into a canonical form:

1. Sort all filter keys alphabetically
2. Include all keys including those with None values (so "no filter" and "filter present" never collide)
3. Join into a deterministic string: `"age_group=None:country_id=NG:gender=male:min_age=None"`

This is deterministic (same input always produces same output), does not alter query meaning, and requires no AI or 
external dependencies.

The cache key also includes the endpoint prefix, page number, and limit to prevent collisions between different 
paginated views of the same filters.

---

## 3. CSV Data Ingestion

### Approach

The upload endpoint `POST /api/profiles/upload` processes CSV files containing up to 500,000 rows using chunked 
processing:

1. File is read once and decoded from UTF-8
2. Parsed row by row using `csv.DictReader` — no loading entire file into memory as structured objects
3. Valid rows accumulate in a chunk list of up to 1,000 rows
4. When a chunk reaches 1,000 rows, one bulk INSERT statement executes for the entire chunk
5. Remaining rows after the final chunk are inserted in one final statement

**Why batch insert?** One INSERT for 1,000 rows is orders of magnitude faster than 1,000 individual INSERTs. Each individual INSERT requires a round trip to the database — at 500,000 rows that is 500,000 round trips vs 500 with chunked batching.

### Validation

Each row is validated before being added to a chunk:
- Missing required fields → skipped, counted as `missing_fields`
- Invalid gender (not male/female) → skipped, counted as `invalid_gender`
- Invalid age_group (not child/teenager/adult/senior) → skipped, counted as `malformed_row`
- Negative or non-numeric age → skipped, counted as `invalid_age`
- Name already exists in database → skipped, counted as `duplicate_name`
- Duplicate name within the same file → skipped, counted as `duplicate_name`
- Malformed row (encoding issues, wrong column count) → skipped, counted as `malformed_row`

A single bad row never fails the entire upload. Rows already inserted before a failure remain — no rollback.

### Duplicate detection

All existing profile names are loaded into a Python set before processing begins. Set lookups are O(1) — instant regardless of size — making duplicate checking fast without querying the database for every row. Names from valid rows within the same file are added to the set immediately, preventing intra-file duplicates.

### Concurrency

Uploads do not block read traffic because:
- Reads use the connection pool and get connections independently
- Bulk inserts commit per chunk, not in one giant transaction
- Cache invalidation happens only after all inserts complete

### Failure handling

If processing fails midway through a file, all rows inserted in committed chunks remain in the database. The upload does not roll back committed data. This matches the requirement: partial failures leave already-inserted rows intact.

---

## Summary of design decisions

| Decision | Justification |
|---|---|
| Indexes on filter columns | Read-heavy workload, filtered queries are the primary operation |
| Connection pool size 10+10 | Balances Neon connection limits with concurrent request handling |
| 5-minute cache TTL | Data changes slowly, slight staleness acceptable for analytics |
| Chunk size 1,000 rows | Balances memory usage vs number of database round trips |
| Set for duplicate detection | O(1) lookup vs O(n) database query per row |
| Cache invalidation on write | Ensures correctness without complex cache update logic |