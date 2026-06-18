# San Diego Fun Finder

Public, standalone San Diego family-friendly event finder.

## Security boundary

This app exposes only:

- `GET /`
- `GET /health`
- `GET /api/events`

It does not need secrets, does not read private files, does not execute shell commands, and does not proxy requests to Hermes or RobertoGPT. Public users cannot trigger a live source refresh; `/api/events` serves the server-side cache and refreshes only when the server decides the cache is stale.

## Local run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python public_family_events.py refresh --json
python app.py
```

Then open `http://127.0.0.1:4885`.

## Render

Build command:

```bash
pip install -r requirements.txt && python public_family_events.py refresh --json >/dev/null || true
```

Start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
```
