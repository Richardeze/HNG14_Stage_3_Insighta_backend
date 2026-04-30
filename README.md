# Insighta Labs+

## System Architecture
Three-component platform: FastAPI backend, Node.js CLI, and HTML/CSS/JS web portal. All components share a single PostgreSQL database hosted on Neon.tech.

## Authentication Flow
GitHub OAuth 2.0 with PKCE for CLI. Web portal uses standard OAuth flow. Backend issues JWT access tokens (3 min) and opaque refresh tokens (5 min) stored as SHA-256 hashes.

## CLI Usage
```bash
insighta login
insighta logout
insighta whoami
insighta profiles list
insighta profiles list --gender male --country NG
insighta profiles search "young males from nigeria"
insighta profiles get <id>
insighta profiles create --name "John Doe"
insighta profiles export --format csv
```

## Token Handling
- Access tokens: JWT, 3 minute expiry, contains user ID and role
- Refresh tokens: opaque random string, 5 minute expiry, stored as SHA-256 hash, single-use rotation
- CLI stores tokens at ~/.insighta/credentials.json
- Web portal receives tokens via URL params after OAuth redirect

## Role Enforcement
- analyst: read-only access to GET /api/profiles, GET /api/profiles/search, GET /api/profiles/export, GET /api/profiles/:id
- admin: full access including POST /api/profiles and DELETE /api/profiles/:id
- All routes check role via FastAPI dependencies (require_analyst, require_admin)

## Natural Language Parsing
Rule-based keyword matching. Supported keywords:
- Gender: "male", "female", "males", "females"
- Age groups: "young" (16-24), "teenager/teen", "adult", "senior/elderly"
- Age ranges: "above N", "over N", "below N", "under N"
- Countries: nigeria (NG), kenya (KE), ghana (GH), angola (AO), benin (BJ), and more

### Limitations
- No AI/ML — purely rule-based
- Cannot handle complex compound queries
- Country list limited to African countries
- Cannot handle typos or synonyms