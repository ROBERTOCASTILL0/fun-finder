#!/usr/bin/env python3
from __future__ import annotations

import hmac
import os
from pathlib import Path

from flask import Flask, jsonify, request

from analytics import AnalyticsStore
from public_family_events import public_events_payload

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "public_dashboard.html"
ANALYTICS_DASHBOARD = ROOT / "analytics_dashboard.html"
DEFAULT_ANALYTICS_DB = Path.home() / ".local" / "share" / "sd-fun-finder" / "fun_finder_analytics.sqlite3"

app = Flask(__name__)
app.config.update(
    JSON_SORT_KEYS=False,
    MAX_CONTENT_LENGTH=1024 * 1024,
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "public, max-age=120",
}


@app.after_request
def add_security_headers(resp):
    for key, value in SECURITY_HEADERS.items():
        resp.headers.setdefault(key, value)
    # Permit the inline single-file UI, but do not allow arbitrary remote scripts.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )
    return resp


@app.get("/")
def index():
    return serve_html(DASHBOARD)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "san-diego-fun-finder", "public_safe": True})


@app.get("/api/events")
def api_events():
    # Public endpoint intentionally ignores user-triggered refresh parameters.
    # Refresh is performed by startup/manual/scheduled server-side jobs only.
    return jsonify(public_events_payload(force=False))


@app.post("/api/analytics")
def api_analytics_track():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid analytics payload"}), 400
    payload.setdefault("path", request.path)
    payload.setdefault("referrer", request.referrer or "direct")
    try:
        result = get_analytics_store().track(payload)
    except ValueError as err:
        return jsonify({"ok": False, "error": str(err)}), 400
    resp = jsonify(result)
    resp.status_code = 202
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/analytics/summary")
def api_analytics_summary():
    auth_error = analytics_auth_error()
    if auth_error is not None:
        return auth_error
    days = clamp_days(request.args.get("days", "30"))
    resp = jsonify(get_analytics_store().summary(days=days))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/analytics")
def analytics_dashboard():
    resp = serve_html(ANALYTICS_DASHBOARD)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"ok": False, "error": "not found"}), 404


@app.errorhandler(500)
def server_error(_err):
    return jsonify({"ok": False, "error": "server error"}), 500


def get_analytics_store() -> AnalyticsStore:
    db_path = os.environ.get("FUN_FINDER_ANALYTICS_DB_PATH", str(DEFAULT_ANALYTICS_DB))
    return AnalyticsStore(db_path)


def clamp_days(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 30
    return max(1, min(value, 90))


def analytics_auth_error():
    configured_key = (os.environ.get("FUN_FINDER_ANALYTICS_KEY") or "").strip()
    if not configured_key:
        resp = jsonify({"ok": False, "error": "analytics key not configured"})
        resp.status_code = 503
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Vary"] = "Authorization, X-Analytics-Key"
        return resp
    provided_key = analytics_request_key()
    if not provided_key or not hmac.compare_digest(provided_key, configured_key):
        resp = jsonify({"ok": False, "error": "forbidden"})
        resp.status_code = 403
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Vary"] = "Authorization, X-Analytics-Key"
        return resp
    return None


def analytics_request_key() -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return (request.headers.get("X-Analytics-Key") or "").strip()


def serve_html(path: Path):
    return app.response_class(path.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4885"))
    app.run(host="0.0.0.0", port=port, debug=False)
