"""
VERITAS v72 startup wrapper.

Purpose:
- eliminate dashboard AbortError caused by 12-second browser-side timeout;
- avoid overlapping dashboard refresh calls;
- keep trading / analytics logic unchanged.

Render start command:
    python veritas_start.py
"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "veritas_intelligence.py"

REPLACEMENTS = (
    ("setTimeout(()=>ctl.abort(),12000)", "setTimeout(()=>ctl.abort(),30000)"),
    ("setInterval(load,30000)", "setInterval(load,45000)"),
)

def patch_dashboard() -> None:
    if not TARGET.exists():
        raise FileNotFoundError(f"Missing {TARGET.name}")

    src = TARGET.read_text(encoding="utf-8")
    dst = src
    changed = []

    for old, new in REPLACEMENTS:
        if old in dst:
            dst = dst.replace(old, new, 1)
            changed.append((old, new))

    # If already patched, continue normally.
    if not changed:
        if all(new in dst for _, new in REPLACEMENTS):
            print("[VERITAS START] dashboard hotfix already present", flush=True)
            return
        raise RuntimeError(
            "Expected dashboard patterns not found; refusing to modify unknown source layout"
        )

    # Safety: only the two expected substitutions are allowed.
    if not all(new in dst for _, new in REPLACEMENTS):
        raise RuntimeError("Hotfix verification failed")

    TARGET.write_text(dst, encoding="utf-8")
    print(
        "[VERITAS START] dashboard hotfix applied: timeout 12s->30s, refresh 30s->45s",
        flush=True,
    )

def main() -> None:
    patch_dashboard()
    # Execute the real service exactly as the main script.
    sys.argv[0] = str(TARGET)
    runpy.run_path(str(TARGET), run_name="__main__")

if __name__ == "__main__":
    main()
