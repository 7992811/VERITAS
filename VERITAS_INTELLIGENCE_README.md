# VERITAS Intelligence v1.0
Isolated first-live-cycle service. It does not modify `bot.py`.

Start: `pip install -r requirements-intelligence.txt && python veritas_intelligence.py`
Health: `/health`

Current v1 writes BTC/ETH state, 5 specialist views plus deterministic committee decision for 4h/1d/3d/7d. Missing macro/derivatives feeds fail to NO_TRADE rather than being invented. Ledger is SQLite; set `VERITAS_LEDGER_PATH` to a persistent mount when available. PostgreSQL migration is the next persistence step.
