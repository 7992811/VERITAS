"""Small exit-fill accounting helper extracted from the legacy portfolio monolith.

Execution authority stays in veritas_portfolio; this module only reconstructs
durable realized-exit totals from paper_orders and adds the current fill.
"""


def realized_exit_metrics(c, trade_id, executed_notional, close_units):
    prior=c.execute(
        """SELECT COALESCE(SUM(notional_rub),0) AS exit_notional_rub,
                  COALESCE(SUM(CASE WHEN price>0 THEN notional_rub/price ELSE 0 END),0) AS exit_units,
                  COUNT(*) AS exit_fill_count
             FROM paper_orders
            WHERE trade_id=%s AND side IN ('SELL','BUY_TO_COVER')""",
        (trade_id,)).fetchone() or {}
    total_notional=float(prior.get('exit_notional_rub') or 0.0)+float(executed_notional)
    total_units=float(prior.get('exit_units') or 0.0)+float(close_units)
    return (total_notional,total_units,total_notional/max(total_units,1e-12),
            int(prior.get('exit_fill_count') or 0)+1)
