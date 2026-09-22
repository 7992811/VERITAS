"""
VERITAS v80.1 bootstrap loader.

Fixes:
1) correct v78.1 -> v79.0 patch order;
2) add missing hashlib import required by v80 portfolio canonical setup IDs;
3) make bootstrap idempotent: if intelligence + portfolio are already v80,
   do not re-apply the legacy patch chain.

Trading logic is unchanged.
"""

from pathlib import Path
from urllib.request import Request, urlopen
import sys

BASE = Path(__file__).resolve().parent
TARGET = BASE / "veritas_intelligence.py"
PORTFOLIO_TARGET = BASE / "veritas_portfolio.py"

PINNED_URL = (
    "https://raw.githubusercontent.com/7992811/VERITAS/"
    "8d7fd5f3e2cccf262b677707bad57d6e1e5acbc2/sitecustomize.py"
)

INTEL_VERSION = "veritas-max-product-v80.0-unified-execution-core"
PORTFOLIO_VERSION = "veritas-portfolio-v6-v80-unified-execution"

START_MARKER = "    # ----- v78.1 Rule & Experience Arbitration -----"
END_MARKER = "    # ----- v79.0 Portfolio Trade Integrity -----"
INSERT_MARKER = "    # ----- v79.0 Trade Integrity / Win-Rate Layer -----"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _already_v80() -> bool:
    return (
        TARGET.exists()
        and PORTFOLIO_TARGET.exists()
        and INTEL_VERSION in _read(TARGET)
        and PORTFOLIO_VERSION in _read(PORTFOLIO_TARGET)
    )


def _ensure_portfolio_hashlib() -> bool:
    """v80 portfolio canonical setup IDs use hashlib.sha256."""
    if not PORTFOLIO_TARGET.exists():
        raise RuntimeError("veritas_portfolio.py missing")

    src = _read(PORTFOLIO_TARGET)

    # Only patch when v80 code actually needs hashlib.
    if "hashlib.sha256" not in src:
        return False

    head = "\n".join(src.splitlines()[:30])
    has_import = (
        "import hashlib" in head
        or "hashlib," in head
        or ", hashlib" in head
    )
    if has_import:
        return False

    anchor = "from __future__ import annotations\n"
    if anchor in src:
        dst = src.replace(anchor, anchor + "import hashlib\n", 1)
    else:
        dst = "import hashlib\n" + src

    PORTFOLIO_TARGET.write_text(dst, encoding="utf-8")
    return True


def _load_payload() -> str:
    req = Request(
        PINNED_URL,
        headers={"User-Agent": "VERITAS-v80.1-bootstrap/1.0"},
    )
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _fix_patch_order(src: str) -> str:
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
    pp = fixed.find("PORTFOLIO_PATCHES = (")

    if not (0 <= p78 < p79 < pp):
        raise RuntimeError("v80 patch ordering repair failed")

    # Ensure the v78.1 intelligence block was moved out of portfolio patches.
    if START_MARKER in fixed[pp:]:
        raise RuntimeError("v78.1 block still present inside PORTFOLIO_PATCHES")

    return fixed


def _verify_final_state() -> None:
    intel = _read(TARGET)
    portfolio = _read(PORTFOLIO_TARGET)

    checks = {
        "intelligence_v80": INTEL_VERSION in intel,
        "portfolio_v80": PORTFOLIO_VERSION in portfolio,
        "intelligence_unified_core": "def _uec_asset_candidates(" in intel,
        "intelligence_unified_shadow": "def sync_shadow_trade_lifecycle(" in intel,
        "portfolio_canonical_setup": "def _portfolio_canonical_setup_id(" in portfolio,
        "portfolio_signal_first": "def _signal_first_admission(" in portfolio,
        "portfolio_hashlib": (
            "hashlib.sha256" not in portfolio
            or "import hashlib" in "\n".join(portfolio.splitlines()[:30])
            or "hashlib," in "\n".join(portfolio.splitlines()[:30])
            or ", hashlib" in "\n".join(portfolio.splitlines()[:30])
        ),
    }

    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("v80 final verification failed: " + ", ".join(failed))


def _execute_pinned_bootstrap() -> None:
    payload = _load_payload()
    fixed = _fix_patch_order(payload)

    glb = {
        "__name__": "__veritas_v80_fixed_bootstrap__",
        "__file__": str(Path(__file__).resolve()),
        "__package__": None,
    }
    exec(compile(fixed, str(Path(__file__).resolve()), "exec"), glb, glb)


def _run() -> None:
    try:
        # On second/subsequent Python invocations during the same Render build
        # or at runtime, do not re-run the legacy patch chain.
        if _already_v80():
            changed = _ensure_portfolio_hashlib()
            _verify_final_state()
            print(
                "[VERITAS BOOTSTRAP] v80.1 READY: "
                "already_v80=true; "
                f"portfolio_hashlib_fixed={str(changed).lower()}",
                flush=True,
            )
            return

        _execute_pinned_bootstrap()

        changed = _ensure_portfolio_hashlib()
        _verify_final_state()

        print(
            "[VERITAS BOOTSTRAP] v80.1 VERIFIED: "
            "patch_order=ok; intelligence_v80=ok; portfolio_v80=ok; "
            f"portfolio_hashlib_fixed={str(changed).lower()}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP] v80.1 FAILED: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )


_run()
