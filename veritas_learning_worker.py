"""Standalone entrypoint for the future dedicated VERITAS learning service.

The production service currently owns database credentials.  This process is
safe to deploy only when DATABASE_URL is supplied by Render as a service secret.
It never connects to a broker and refuses to start without an explicit role.
"""
import os, sys, time

ROLE=os.getenv("VERITAS_PROCESS_ROLE","").strip().lower()
if ROLE!="learning":
    raise SystemExit("VERITAS_PROCESS_ROLE=learning is required")

# Importing the monolith would also start market/trading runtime, so the worker
# intentionally stays dormant until the DB-facing adapter is moved behind a
# side-effect-free package boundary.
from veritas_learning_v2 import VERSION

print(f"VERITAS_LEARNING_WORKER_READY version={VERSION} mode=shadow_only", flush=True)
while True:
    time.sleep(60)
