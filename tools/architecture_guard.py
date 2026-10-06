from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

# CTC v1 freezes the legacy monolith at the audited migration boundary.
# Historical Rxx functions remain readable for replay/audit, but production
# authority is now externalized through the canonical constitution/runtime lock.
# These are HARD no-growth limits, not architectural targets.
LIMITS = {
    "veritas_intelligence.py": {"max_lines": 19216, "max_redefinitions": 35, "max_duplicate_names": 32},
    "veritas_portfolio.py": {"max_lines": 8636, "max_redefinitions": 68, "max_duplicate_names": 20},
    "veritas_portfolio_runtime.py": {"max_lines": 5385, "max_redefinitions": 49, "max_duplicate_names": 14},
    "veritas_canonical_constitution.py": {"max_lines": 500, "max_redefinitions": 0, "max_duplicate_names": 0},
    "veritas_decision_signature.py": {"max_lines": 50, "max_redefinitions": 0, "max_duplicate_names": 0},
    "veritas_asset_management_intelligence.py": {"max_lines": 540, "max_redefinitions": 0, "max_duplicate_names": 0},
}

# Long-term refactor targets remain explicit so accepting the CTC migration
# baseline cannot be mistaken for declaring the legacy debt desirable.
REDUCTION_TARGETS = {
    "veritas_intelligence.py": {"lines": 18000, "redefinitions": 35, "duplicate_names": 32},
    "veritas_portfolio.py": {"lines": 8200, "redefinitions": 68, "duplicate_names": 20},
    "veritas_portfolio_runtime.py": {"lines": 1450, "redefinitions": 16, "duplicate_names": 8},
}


def audit(path: str):
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text, filename=path)
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    counts = Counter(names)
    duplicate_names = {k: v for k, v in counts.items() if v > 1}
    redefinitions = sum(v - 1 for v in duplicate_names.values())
    return {
        "lines": len(text.splitlines()),
        "functions": len(names),
        "duplicate_names": len(duplicate_names),
        "redefinitions": redefinitions,
        "top_duplicates": sorted(duplicate_names.items(), key=lambda x: (-x[1], x[0]))[:20],
    }


def _canonical_static_contract():
    constitution = Path("veritas_canonical_constitution.py").read_text(encoding="utf-8")
    runtime = Path("veritas_portfolio_runtime.py").read_text(encoding="utf-8")
    portfolio = Path("veritas_portfolio.py").read_text(encoding="utf-8")
    failures = []
    required = (
        "class CanonicalAdmissionEngine",
        "FINAL_RUNTIME_AUTHORITY_VERSION='CTC_V1_FINAL_AUTHORITY'",
        "FINAL_SIGNAL_FIRST_ADMISSION=canonical_signal_first_admission",
    )
    for marker in required:
        if marker not in runtime:
            failures.append(f"missing canonical runtime marker: {marker}")
    for marker in (
        "_signal_first_admission=_VERITAS_RUNTIME.FINAL_SIGNAL_FIRST_ADMISSION",
        "_open_or_add=_VERITAS_RUNTIME.FINAL_OPEN_OR_ADD",
        "_close_or_reduce=_VERITAS_RUNTIME.FINAL_CLOSE_OR_REDUCE",
    ):
        if marker not in portfolio:
            # Import-order safe binding is conditional, but the exact frozen
            # assignment must still exist in the source contract.
            failures.append(f"missing frozen portfolio authority binding: {marker}")
    if "IMPLEMENTATION_GAPS = []" not in constitution:
        failures.append("canonical implementation gaps are not closed")
    return failures


def main():
    failed = False
    for path, lim in LIMITS.items():
        a = audit(path)
        print(path, a)
        if a["lines"] > lim["max_lines"]:
            print(f"FAIL: {path} line count grew above CTC v1 frozen baseline")
            failed = True
        if a["redefinitions"] > lim["max_redefinitions"]:
            print(f"FAIL: {path} function redefinitions increased above CTC v1 baseline")
            failed = True
        if a["duplicate_names"] > lim["max_duplicate_names"]:
            print(f"FAIL: {path} duplicate function-name count increased above CTC v1 baseline")
            failed = True

    for failure in _canonical_static_contract():
        print("FAIL:", failure)
        failed = True

    if failed:
        raise SystemExit(1)

    print("Architecture debt is frozen at CTC v1; canonical layer has zero duplicate function names.")
    print("Legacy reduction targets remain:", REDUCTION_TARGETS)


if __name__ == "__main__":
    main()
