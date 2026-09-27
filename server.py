#!/usr/bin/env python3
"""Vera bot HTTP server — zero third-party dependencies (Python 3.10+ stdlib only).

Endpoints: GET /v1/healthz, GET /v1/metadata, POST /v1/context, POST /v1/tick,
POST /v1/reply, POST /v1/teardown.  Run: python server.py  (PORT env, default 8080)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from version import __version__  # noqa: E402
import llm  # noqa: E402
from engine import ENGINE, SCOPES  # noqa: E402
import replies  # noqa: E402

MAX_BODY = 600_000

METADATA = {
    "team_name": os.environ.get("VERA_TEAM_NAME", "Team Vera"),
    "team_members": [m.strip() for m in os.environ.get("VERA_TEAM_MEMBERS", "Your Name").split(",")],
    "model": llm.model_name(),
    "approach": ("fact-pack resolver + per-trigger-kind decision playbooks (send/skip, angle, recommendation, CTA) "
                 "+ number-grounding validator; rule-based reply state machine (auto-reply, intent, hostile, "
                 "off-topic); optional validated LLM polish"),
    "contact_email": os.environ.get("VERA_CONTACT_EMAIL", "you@example.com"),
    "version": __version__,
    "submitted_at": os.environ.get("VERA_SUBMITTED_AT", "2026-09-27T00:00:00Z"),
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Handler(BaseHTTPRequestHandler):
    server_version = "VeraBot/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        if os.environ.get("VERA_LOG", "1") == "1":
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _send(self, code: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("payload too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/healthz", "/healthz", ""):
            return self._send(200, {"status": "ok", "uptime_seconds": int(time.time() - ENGINE.started),
                                    "contexts_loaded": ENGINE.counts()})
        if path == "/v1/metadata":
            return self._send(200, METADATA)
        if path == "/v1/debug/decisions":
            return self._send(200, {"decisions": ENGINE.decision_log[-200:]})
        return self._send(404, {"error": "not_found"})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            body = self._json()
        except Exception as e:
            return self._send(400, {"accepted": False, "reason": "malformed_json", "details": str(e)})
        try:
            if path == "/v1/context":
                return self._context(body)
            if path == "/v1/tick":
                actions = ENGINE.tick(body.get("now") or _now_iso(), body.get("available_triggers") or [])
                return self._send(200, {"actions": actions})
            if path == "/v1/reply":
                return self._send(200, replies.handle(ENGINE, body))
            if path == "/v1/teardown":
                ENGINE.reset()
                return self._send(200, {"ok": True, "wiped_at": _now_iso()})
            return self._send(404, {"error": "not_found"})
        except Exception as e:  # never return a 500 with non-JSON
            traceback.print_exc()
            if path == "/v1/tick":
                return self._send(200, {"actions": []})
            if path == "/v1/reply":
                return self._send(200, {"action": "wait", "wait_seconds": 1800,
                                        "rationale": f"internal error handled safely: {type(e).__name__}"})
            return self._send(500, {"error": type(e).__name__})

    def _context(self, body):
        scope, cid, ver = body.get("scope"), body.get("context_id"), body.get("version")
        payload = body.get("payload")
        if scope not in SCOPES:
            return self._send(400, {"accepted": False, "reason": "invalid_scope", "details": f"scope={scope!r}"})
        if not cid or not isinstance(payload, dict):
            return self._send(400, {"accepted": False, "reason": "invalid_payload",
                                    "details": "context_id and object payload required"})
        try:
            ver = int(ver)
        except (TypeError, ValueError):
            return self._send(400, {"accepted": False, "reason": "invalid_version", "details": str(ver)})
        ok, cur = ENGINE.push_context(scope, cid, ver, payload)
        if not ok:
            return self._send(409, {"accepted": False, "reason": "stale_version", "current_version": cur})
        return self._send(200, {"accepted": True, "ack_id": f"ack_{cid}_v{ver}", "stored_at": _now_iso()})


def main():
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    print(f"Vera bot v{__version__} listening on :{port}  (LLM: {llm.model_name()})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
