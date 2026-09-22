"""
VERITAS v80 bootstrap loader - fixed patch ordering.

Purpose:
- load the exact v80 payload from the pinned GitHub commit;
- move the v78.1 Rule & Experience Arbitration block before v79.0;
- execute the corrected bootstrap in-memory.

This avoids changing trading logic: only the patch application order is corrected.
"""

from pathlib import Path
from urllib.request import Request, urlopen
import sys

PINNED_URL = (
    "https://raw.githubusercontent.com/7992811/VERITAS/"
    "8d7fd5f3e2cccf262b677707bad57d6e1e5acbc2/sitecustomize.py"
)

START_MARKER = "    # ----- v78.1 Rule & Experience Arbitration -----"
END_MARKER = "    # ----- v79.0 Portfolio Trade Integrity -----"
INSERT_MARKER = "    # ----- v79.0 Trade Integrity / Win-Rate Layer -----"


def _load_payload() -> str:
    req = Request(
        PINNED_URL,
        headers={"User-Agent": "VERITAS-v80-bootstrap/1.0"},
    )
    with urlopen(req, timeout=30) as resp:
        data = resp.read()
    return data.decode("utf-8")


def _fix_order(src: str) -> str:
    s = src.find(START_MARKER)
    e = src.find(END_MARKER)
    if s < 0 or e < 0 or e <= s:
        raise RuntimeError("v78.1 block markers not found in pinned v80 payload")

    block = src[s:e]
    src = src[:s] + src[e:]

    ins = src.find(INSERT_MARKER)
    if ins < 0:
        raise RuntimeError("v79.0 insertion marker not found in pinned v80 payload")

    fixed = src[:ins] + block + "\n" + src[ins:]

    p78 = fixed.find(START_MARKER)
    p79 = fixed.find(INSERT_MARKER)
    if not (0 <= p78 < p79):
        raise RuntimeError("v80 patch ordering repair failed")

    return fixed


def _run():
    try:
        payload = _load_payload()
        fixed = _fix_order(payload)

        glb = {
            "__name__": "__veritas_v80_fixed_bootstrap__",
            "__file__": str(Path(__file__).resolve()),
            "__package__": None,
        }
        exec(compile(fixed, str(Path(__file__).resolve()), "exec"), glb, glb)

    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP LOADER] v80 fixed loader failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )


_run()
