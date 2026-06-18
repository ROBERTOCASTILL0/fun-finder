#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, send_file

from public_family_events import public_events_payload

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "public_dashboard.html"

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
    return send_file(DASHBOARD, mimetype="text/html; charset=utf-8")

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "san-diego-fun-finder", "public_safe": True})

@app.get("/api/events")
def api_events():
    # Public endpoint intentionally ignores user-triggered refresh parameters.
    # Refresh is performed by startup/manual/scheduled server-side jobs only.
    return jsonify(public_events_payload(force=False))

@app.errorhandler(404)
def not_found(_err):
    return jsonify({"ok": False, "error": "not found"}), 404

@app.errorhandler(500)
def server_error(_err):
    return jsonify({"ok": False, "error": "server error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4885"))
    app.run(host="0.0.0.0", port=port, debug=False)
