# San Diego Fun Finder

Public, standalone San Diego family-friendly event finder.

## Security boundary

This app exposes only:

- `GET /`
- `GET /health`
- `GET /api/events`
- `POST /api/analytics`
- `GET /api/analytics/summary` (protected by `FUN_FINDER_ANALYTICS_KEY`)
- `GET /analytics` (public shell; private data still requires `FUN_FINDER_ANALYTICS_KEY`)

It does not need user accounts, does not read private files, does not execute shell commands, and does not proxy requests to Hermes or RobertoGPT. Public users cannot trigger a live source refresh; `/api/events` serves the server-side cache and refreshes only when the server decides the cache is stale.

Analytics collection is first-party and privacy-conscious:

- anonymous browser session IDs only (stored in localStorage)
- no raw IP storage
- `navigator.sendBeacon()` with fetch fallback
- respects browser Do Not Track / Global Privacy Control when available

Protected analytics data access requires `FUN_FINDER_ANALYTICS_KEY` sent either as an `Authorization: Bearer ...` header or an `X-Analytics-Key` header.
The dashboard shell stores the key in `sessionStorage` for the current tab instead of putting secrets in the URL.

## Local run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python public_family_events.py refresh --json
python app.py
```

Then open `http://127.0.0.1:4885`.

To view the private analytics dashboard locally:

```bash
FUN_FINDER_ANALYTICS_KEY=dev-analytics python app.py
```

Then open `http://127.0.0.1:4885/analytics`, enter the key in the page, and the dashboard will call the summary API with the `X-Analytics-Key` header.

Example direct API check:

```bash
curl -H 'X-Analytics-Key: dev-analytics' 'http://127.0.0.1:4885/api/analytics/summary?days=30'
```

## Render

Build command:

```bash
pip install -r requirements.txt && python public_family_events.py refresh --json >/dev/null || true
```

Start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
```

## Render persistence caveat

This analytics implementation uses a local SQLite file by default:

- default path: `~/.local/share/sd-fun-finder/fun_finder_analytics.sqlite3`
- override path: `FUN_FINDER_ANALYTICS_DB_PATH`

Important: Render free web services do **not** support persistent disks. That means local analytics data can be lost whenever the service restarts or redeploys.

Recommended production upgrade paths:

1. move the service to a Render plan with persistent disk support and point `FUN_FINDER_ANALYTICS_DB_PATH` at the mounted disk, or
2. later swap the storage layer to a managed database.
