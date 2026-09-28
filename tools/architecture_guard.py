from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

LIMITS = {
    "veritas_intelligence.py": {"max_lines": 18000, "max_redefinitions": 36, "max_duplicate_names": 34},
    "veritas_portfolio.py": {"max_lines": 8200, "max_redefinitions": 68, "max_duplicate_names": 20},
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


def main():
    failed = False
    for path, lim in LIMITS.items():
        a = audit(path)
        print(path, a)
        if a["lines"] > lim["max_lines"]:
            print(f"FAIL: {path} line count grew above canonical debt baseline")
            failed = True
        if a["redefinitions"] > lim["max_redefinitions"]:
            print(f"FAIL: {path} function redefinitions increased")
            failed = True
        if a["duplicate_names"] > lim["max_duplicate_names"]:
            print(f"FAIL: {path} duplicate function-name count increased")
            failed = True
    if failed:
        raise SystemExit(1)
    print("Architecture debt did not increase. Next objective: reduce these baselines.")


if __name__ == "__main__":
    main()
