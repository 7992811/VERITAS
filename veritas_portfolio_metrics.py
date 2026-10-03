"""Read-only performance summaries over the complete closed-trade ledger."""

CLOSED_METRICS_SQL = """
    COALESCE(SUM(net_pnl_rub) FILTER (WHERE status='CLOSED' AND net_pnl_rub>0),0) AS winning_pnl,
    COALESCE(-SUM(net_pnl_rub) FILTER (WHERE status='CLOSED' AND net_pnl_rub<0),0) AS losing_pnl,
    AVG(net_pnl_rub) FILTER (WHERE status='CLOSED' AND net_pnl_rub>0) AS avg_win,
    -AVG(net_pnl_rub) FILTER (WHERE status='CLOSED' AND net_pnl_rub<0) AS avg_loss,
    COALESCE(SUM(gross_pnl_rub) FILTER (WHERE status='CLOSED'),0) AS closed_gross,
    COALESCE(SUM(fees_rub) FILTER (WHERE status='CLOSED'),0) AS closed_fees,
    COALESCE(SUM(funding_rub) FILTER (WHERE status='CLOSED'),0) AS closed_funding
"""


def closed_trade_metrics(row, count):
    """Use null, never Infinity or a fabricated zero, for undefined ratios."""
    gain = float(row.get('winning_pnl') or 0)
    loss = float(row.get('losing_pnl') or 0)
    avg_win = float(row['avg_win']) if row.get('avg_win') is not None else None
    avg_loss = float(row['avg_loss']) if row.get('avg_loss') is not None else None
    state = ('NO_TRADES' if not count else 'DEFINED' if loss > 0
             else 'NO_LOSSES' if gain > 0 else 'FLAT')
    return {
        'closed_metrics_scope': 'ALL_CLOSED',
        'profit_factor': gain / loss if loss > 0 else None,
        'profit_factor_state': state,
        'avg_win_rub': avg_win,
        'avg_loss_rub': avg_loss,
        'payoff_ratio': avg_win / avg_loss if avg_win is not None and avg_loss else None,
        'closed_gross_pnl_rub': float(row.get('closed_gross') or 0),
        'closed_fees_rub': float(row.get('closed_fees') or 0),
        'closed_funding_rub': float(row.get('closed_funding') or 0),
    }
