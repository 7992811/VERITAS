"""Dedicated VERITAS Learning 2.0 service.

This process owns continuous learning, strategy-quality review and heavy history
maintenance. It deliberately does not start the market loop, broker connection,
paper portfolio execution or real-order services.

The production web service remains the source of the database lease. The lease
endpoint is authenticated by a shared random bridge token, so the database URL
does not need to be copied through ChatGPT or committed to the repository.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

os.environ.setdefault("VERITAS_ROLE", "learning")
os.environ.setdefault("VERITAS_EXTERNAL_LEARNING", "1")
os.environ.setdefault("VERITAS_FULL_OVERVIEW_ENABLED", "0")
os.environ.setdefault("VERITAS_KNOWLEDGE_AUTOMATION", "0")
os.environ.setdefault("VERITAS_BACKTEST_ENABLED", "0")
os.environ.setdefault("VERITAS_EVENT_WEB_SCAN_ENABLED", "0")

_STATE = {
    "ready": False,
    "started_at": time.time(),
    "database_lease": False,
    "continuous_learning": False,
    "quality_delivery": False,
    "heavy_learning": False,
    "replay": "STARTING",
    "error": None,
}


def _database_lease():
    source = os.getenv("VERITAS_SOURCE_URL", "").strip().rstrip("/")
    token = os.getenv("VERITAS_V90_BRIDGE_TOKEN", "").strip()
    if not source or not token:
        raise RuntimeError("VERITAS_SOURCE_URL and VERITAS_V90_BRIDGE_TOKEN are required")
    with httpx.Client(timeout=15.0) as client:
        response = client.get(source + "/internal/v90/database-lease",
                              headers={"X-Veritas-V90-Token": token})
        response.raise_for_status()
        data = response.json()
    value = str(data.get("database_url") or "").strip()
    if data.get("status") != "OK" or not value:
        raise RuntimeError("database lease unavailable: " + str(data.get("reason") or data.get("status")))
    os.environ["DATABASE_URL"] = value
    _STATE["database_lease"] = True


class H(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def _reply(self, code=200):
        body = json.dumps({
            "ok": bool(_STATE["ready"]),
            "role": "learning",
            "uptime_s": round(time.time() - _STATE["started_at"], 1),
            **_STATE,
        }, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200 if _STATE["ready"] or self.path.startswith("/healthz") else 503)


def _serve():
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), H)
    server.serve_forever()


def _heavy(v):
    try:
        _STATE["heavy_learning"] = True
        v.heavy_learning_maintenance_loop()
    except BaseException as exc:
        _STATE["heavy_learning"] = False
        _STATE["error"] = "heavy_learning: " + type(exc).__name__ + ": " + str(exc)


def _replay_loop(v):
    import veritas_learning_replay as REPLAY
    time.sleep(120)
    while True:
        try:
            if getattr(v._continuous_learning, "ready", False):
                result = REPLAY.run(v.__dict__, v.pg_connect, max_candidates=2)
                _STATE["replay"] = result.get("status") or "OK"
                print(json.dumps({"event": "VERITAS_LEARNING_V2_REPLAY",
                                  "result": result}, ensure_ascii=False, default=str), flush=True)
            else:
                _STATE["replay"] = "BOOTSTRAP_PENDING"
        except BaseException as exc:
            _STATE["replay"] = "ERROR"
            _STATE["error"] = "replay: " + type(exc).__name__ + ": " + str(exc)
            print(json.dumps({"event": "VERITAS_LEARNING_V2_REPLAY_ERROR",
                              "error": _STATE["error"]}, ensure_ascii=False), flush=True)
        time.sleep(max(300, int(os.getenv("VERITAS_LEARNING_REPLAY_INTERVAL_SECONDS", "900"))))


def main():
    threading.Thread(target=_serve, daemon=True, name="veritas-learning-health").start()
    try:
        _database_lease()

        # DATABASE_URL must exist before this import because veritas_intelligence
        # resolves its database configuration at module import time.
        import veritas_intelligence as v
        import veritas_quality_delivery as QUALITY

        boot = v.pg_init()
        if not boot.get("ok"):
            raise RuntimeError("PostgreSQL bootstrap failed: " + str(boot))
        migration = v.v90_migrate_core_data()
        if str(migration.get("status") or "").upper() in ("ERROR", "POSTGRES_REQUIRED"):
            raise RuntimeError("v90 migration failed: " + str(migration))

        v._continuous_learning.start()
        _STATE["continuous_learning"] = True

        QUALITY.install(v.__dict__)
        _STATE["quality_delivery"] = True

        threading.Thread(target=_heavy, args=(v,), daemon=True,
                         name="veritas-heavy-learning-v2").start()
        threading.Thread(target=_replay_loop, args=(v,), daemon=True,
                         name="veritas-learning-replay-v2").start()

        _STATE["ready"] = True
        print(json.dumps({"event": "VERITAS_LEARNING_V2_READY",
                          "version": getattr(v, "VERSION", None),
                          "migration": migration.get("status"),
                          "real_order_authority": False},
                         ensure_ascii=False, default=str), flush=True)

        # All actual work is scheduled by the durable bounded maintenance lane.
        while True:
            time.sleep(30)
    except BaseException as exc:
        _STATE["ready"] = False
        _STATE["error"] = type(exc).__name__ + ": " + str(exc)
        print(json.dumps({"event": "VERITAS_LEARNING_V2_FAILED",
                          "error": _STATE["error"]},
                         ensure_ascii=False), flush=True)
        while True:
            time.sleep(30)


if __name__ == "__main__":
    main()
