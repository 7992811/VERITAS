from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

# Legacy files are frozen ceilings, not targets. New canonical code belongs in
# small zero-duplication modules rather than another Rxx layer.
LIMITS = {
    "veritas_direct_cny.py": {"max_lines":250,"max_redefinitions":0,"max_duplicate_names":0},
    "veritas_strategy_quality.py": {"max_lines":380,"max_redefinitions":0,"max_duplicate_names":0},
    "veritas_strategy_roles.py": {"max_lines":120,"max_redefinitions":0,"max_duplicate_names":0},
    "veritas_intelligence.py": {"max_lines": 19254, "max_redefinitions": 35, "max_duplicate_names": 32},
    "veritas_portfolio.py": {"max_lines": 8698, "max_redefinitions": 68, "max_duplicate_names": 20},
    "veritas_portfolio_runtime.py": {"max_lines": 5394, "max_redefinitions": 49, "max_duplicate_names": 14},
    "veritas_canonical_constitution.py": {"max_lines": 500, "max_redefinitions": 0, "max_duplicate_names": 0},
    "veritas_canonical_runtime.py": {"max_lines": 420, "max_redefinitions": 0, "max_duplicate_names": 0},
    "veritas_release.py": {"max_lines": 80, "max_redefinitions": 0, "max_duplicate_names": 0},
    "veritas_decision_signature.py": {"max_lines": 50, "max_redefinitions": 0, "max_duplicate_names": 0},
    "veritas_asset_management_intelligence.py": {"max_lines": 540, "max_redefinitions": 0, "max_duplicate_names": 0},
}

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
    canonical = Path("veritas_canonical_runtime.py").read_text(encoding="utf-8")
    runtime = Path("veritas_portfolio_runtime.py").read_text(encoding="utf-8")
    portfolio = Path("veritas_portfolio.py").read_text(encoding="utf-8")
    intelligence = Path("veritas_intelligence.py").read_text(encoding="utf-8")
    failures = []

    for marker in (
        'VERSION = "CTC_V2_2026_10_06"',
        'BASIS_RUNTIME = "CTC_V2_CANONICAL_RUNTIME"',
        "def runtime_portfolio_policy(name):",
        "def veto_severity(code):",
        "IMPLEMENTATION_GAPS = []",
    ):
        if marker not in constitution:
            failures.append(f"missing CTC v2 marker: {marker}")

    for marker in (
        "import veritas_canonical_runtime as VCR",
        "class CanonicalAdmissionEngine",
        "out=dict(VCR.evaluate(",
        "FINAL_RUNTIME_AUTHORITY_VERSION=CTC.BASIS_RUNTIME",
        "_candidate_book_v84=VCR.candidate_book",
        "_currency_candidate_book=VCR.currency_candidate_book",
        "_desired_fraction=_canonical_desired_fraction",
        "FINAL_OPEN_OR_ADD=canonical_open_or_add",
        "FINAL_CLOSE_OR_REDUCE=canonical_close_or_reduce",
    ):
        if marker not in runtime:
            failures.append(f"missing canonical runtime binding: {marker}")

    final = runtime.split("# CANONICAL FINAL RUNTIME AUTHORITY", 1)[-1]
    for forbidden in (
        "_v90r79_base_admission(",
        "_v90r79_base_open_or_add(",
        "_R85_POLICY_ADMISSION(",
        "LEGACY_R85_POLICY_ADMISSION(",
    ):
        if forbidden in final:
            failures.append(f"legacy strategy call reachable from final authority: {forbidden}")

    for marker in (
        "POLICIES={name:CTC.runtime_portfolio_policy(name) for name in CTC.PORTFOLIO_ORDER}",
        "_signal_first_admission=_VERITAS_RUNTIME.FINAL_SIGNAL_FIRST_ADMISSION",
        "_open_or_add=_VERITAS_RUNTIME.FINAL_OPEN_OR_ADD",
        "_close_or_reduce=_VERITAS_RUNTIME.FINAL_CLOSE_OR_REDUCE",
        "CANONICAL_ACCOUNTING_OPEN_OR_ADD=_open_or_add",
        "CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE=_close_or_reduce",
        "elif mode=='CURRENCY':",
        "base_book=_currency_candidate_book(summary)",
    ):
        if marker not in portfolio:
            failures.append(f"missing portfolio canonical contract: {marker}")

    if "V90_CANONICAL_PORTFOLIOS=tuple(VR.PORTFOLIOS)" not in intelligence:
        failures.append("main service does not consume canonical release portfolio registry")
    initializer = next((node for node in ast.parse(intelligence).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_v90r24_ensure_canonical_portfolios"), None)
    initializer_source = ast.get_source_segment(intelligence, initializer) if initializer else ""
    if "VP.ensure_schema(pg_connect)" not in initializer_source:
        failures.append("main service bypasses canonical portfolio policy initialization")
    if any(statement in initializer_source.upper() for statement in
           ("INSERT INTO PAPER_PORTFOLIOS", "UPDATE PAPER_PORTFOLIOS")):
        failures.append("main service duplicates canonical portfolio policy writes")

    if Path("veritas_start.py").exists():
        failures.append("legacy v72 source-rewrite launcher remains in production root")
    if not Path("legacy/veritas_start_v72.py").exists():
        failures.append("archived v72 launcher missing from audit archive")

    if "import veritas_portfolio" in canonical:
        failures.append("canonical runtime must not import legacy portfolio engine")
    for marker in ("def currency_candidate_book(summary, now=None):","def local_confirmation_gate(row,event=None):"):
        if marker not in canonical:
            failures.append(f"canonical execution guard missing: {marker}")
    return failures

def main():
    failed = False
    for path, lim in LIMITS.items():
        a = audit(path)
        print(path, a)
        if a["lines"] > lim["max_lines"]:
            print(f"FAIL: {path} line count grew above frozen/canonical ceiling")
            failed = True
        if a["redefinitions"] > lim["max_redefinitions"]:
            print(f"FAIL: {path} function redefinitions exceed ceiling")
            failed = True
        if a["duplicate_names"] > lim["max_duplicate_names"]:
            print(f"FAIL: {path} duplicate function-name count exceeds ceiling")
            failed = True

    for failure in _canonical_static_contract():
        print("FAIL:", failure)
        failed = True
    if failed:
        raise SystemExit(1)

    print("CTC v2 architecture guard PASS: one canonical policy/admission/sizing authority.")
    print("Legacy reduction targets remain:", REDUCTION_TARGETS)

if __name__ == "__main__":
    main()
