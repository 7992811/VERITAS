# VERITAS Markets v70 release candidate

Target: repository `7992811/VERITAS`, branch `main`, service `veritas-intelligence-v1`.

## Files
- `veritas_v70.py` — new quality/orchestration layer. Standard library only; no new background threads or network calls.
- `apply_v70_patch.py` — idempotent v27 -> v70 patcher for `veritas_intelligence.py`.
- `test_veritas_v70.py` — smoke/unit tests.

## Safety design
- Existing v27 engines are reused rather than duplicated.
- New pre-trade governance runs in `shadow` by default.
- It may be promoted with `VERITAS_V70_GATE_MODE=enforce` only after OOS non-degradation is demonstrated.
- Missing data returns `DATA_REQUIRED`/`PROFILE_REQUIRED`; no synthetic market facts are inserted.
- No additional always-on worker thread is introduced.
- v70 board is cached and can be excluded from `/overview` with `VERITAS_V70_OVERVIEW_ENABLED=0`.

## Apply
```bash
python apply_v70_patch.py veritas_intelligence.py
python -m py_compile veritas_intelligence.py veritas_v70.py
python -m unittest -v test_veritas_v70.py
```

Then commit `veritas_intelligence.py`, `veritas_v70.py`, and `test_veritas_v70.py` together.

## Runtime
- API: `/api/v1/v70`
- Version: `veritas-max-product-v70.0-market-os`
- Default: `VERITAS_V70_ENABLED=1`
- Governance: `VERITAS_V70_GATE_MODE=shadow`
- Overview integration: `VERITAS_V70_OVERVIEW_ENABLED=1`

The v70 layer maps every planned version v28-v70. Features that require unavailable external data remain explicitly data-gated instead of being simulated.
