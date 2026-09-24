from __future__ import annotations
from pathlib import Path
import sys,re

root=Path(sys.argv[1]).resolve()
p=root/'veritas_v86/portfolios.py'
s=p.read_text(encoding='utf-8')
# Align portfolio constraints with the agreed paper mandate:
# gross leverage <= 2.0x, single-asset exposure <= 100%.
# RelativeValue remains stricter at 1.0x / 50%.
before=s
# Only replace the standard 2.5 profiles; preserve explicitly stricter values.
s=s.replace('max_gross=D("2.5"),asset_cap=D("2.5")',
            'max_gross=D("2.0"),asset_cap=D("1.0")')
if s==before:
    raise SystemExit('V86_RISK_LIMIT_ALIGNMENT_ANCHOR_NOT_FOUND')
p.write_text(s,encoding='utf-8')
print('V86_RISK_LIMIT_ALIGNMENT_ACTIVE')
