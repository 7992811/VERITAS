"""Comparable, net-of-cost launch evidence. Never controls paper entry or exit.

The current cohort is the immutable entry SHA, full entry-policy digest and
epoch. NAV sampling and source-locked quote-path coverage are separate proofs;
neither is manufactured from the first and last timestamps.
"""
from datetime import datetime, timezone
from veritas_trade_audit import summarize, evidence_exclusion, payload, number
import veritas_learning_integrity as LI
import veritas_strategy_quality as Q
import veritas_trade_diagnostics as DIAG

COHORT = 'R72_COST_AWARE_STRUCTURAL'
MAX_CANDIDATE_ROWS = 5000
NAV_GAP_MULTIPLE = 2.0
MIN_NAV_INTERVALS = 3


def comparable_trade(row, current=None):
    p = payload(row.get('payload'))
    return (Q.matches_current_version(row, current)
        and p.get('entry_rule_revision') == COHORT and bool(p.get('r66_event_id'))
        and evidence_exclusion(row) is None)


def utc(value):
    try:
        v = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return v.astimezone(timezone.utc) if v.tzinfo else None
    except (TypeError,ValueError):
        return None


def accounting_complete(row):
    values = [number(row.get(k)) for k in ('net_pnl_rub','gross_pnl_rub','fees_rub','funding_rub')]
    if any(v is None for v in values):
        return False
    net,gross,fees,funding = values
    return abs(net-(gross-fees-funding)) <= max(.01,abs(net)*1e-8)


def nav_coverage(history, first, last):
    """Only attest to observed NAV sampling, never to continuous market prices."""
    history = dict(history or {})
    start,end = utc(history.get('first_observation')),utc(history.get('last_observation'))
    count,intervals,cadence,gap,gaps=(number(history.get(key)) for key in
        ('observation_count','interval_count','observed_cadence_seconds','max_gap_seconds','gap_count'))
    bounds=bool(first and last and start and end and last>=first
        and start <= first.replace(microsecond=0) and end >= last.replace(microsecond=0))
    measured=bool(count is not None and intervals is not None and intervals>=MIN_NAV_INTERVALS
        and count==intervals+1 and cadence is not None and cadence>0
        and gap is not None and gap>=0 and gaps is not None and gaps>=0)
    allowed=NAV_GAP_MULTIPLE*cadence if cadence is not None and cadence>0 else None
    no_gaps=bool(measured and gaps==0 and gap<=allowed)
    valid=history.get('invalid_observations')==0
    complete=bool(bounds and measured and no_gaps and valid)
    reason=('OBSERVED_NAV_SAMPLING_COMPLETE' if complete else
            'NAV_BOUNDARIES_NOT_COVERED' if not bounds else
            'NAV_CADENCE_NOT_MEASURED' if not measured else
            'NAV_INVALID_VALUES' if not valid else 'NAV_INTERNAL_GAP')
    return {'complete':complete,'status':'OBSERVED' if complete else 'INSUFFICIENT',
            'reason':reason,'scope':'DISCRETE_PORTFOLIO_NAV_NOT_QUOTE_PATH',
            'cadence_method':'MEDIAN_POSITIVE_OBSERVED_INTERVALS',
            'minimum_intervals':MIN_NAV_INTERVALS,'gap_multiple':NAV_GAP_MULTIPLE,
            'observation_count':count,'interval_count':intervals,
            'observed_cadence_seconds':cadence,'max_gap_seconds':gap,
            'allowed_gap_seconds':allowed,'gap_count':gaps,'boundaries_covered':bounds}


def path_coverage(rows):
    import veritas_observation_path as VOP
    reasons={}; eligible=0
    for row in rows:
        assessment=VOP.assessment(row)
        eligible+=assessment.get('eligible') is True
        if not assessment.get('eligible'):
            reason=assessment.get('reason') or assessment.get('status') or 'PATH_UNVERIFIED'
            reasons[reason]=reasons.get(reason,0)+1
    complete=bool(rows) and eligible==len(rows)
    return {'complete':complete,'status':'OBSERVED' if complete else 'INSUFFICIENT',
            'trades':len(rows),'covered_trades':eligible,'excluded_trades':len(rows)-eligible,
            'exclusions':reasons,'scope':'SOURCE_LOCKED_OBSERVED_QUOTE_PATH'}


def rule_evidence(rows):
    reasons={};eligible=0
    for row in rows:
        diagnosis=DIAG.diagnose(row)
        eligible+=diagnosis.get('strategy_quality_eligible') is True
        if not diagnosis.get('strategy_quality_eligible'):
            reason=diagnosis.get('exclusion_reason') or diagnosis.get('status') or 'RULE_EVIDENCE_UNVERIFIED'
            reasons[reason]=reasons.get(reason,0)+1
    complete=bool(rows) and eligible==len(rows)
    return {'complete':complete,'trades':len(rows),'verified_rule_outcomes':eligible,
            'excluded_trades':len(rows)-eligible,'exclusions':reasons}


def evaluate_evidence(rows, history, name, *, current=None, total_closed_trades=None, history_truncated=False):
    current=current or Q.version_identity()
    all_rows = list(rows)
    comparable = [r for r in all_rows if comparable_trade(r,current)]
    stats = summarize(comparable)
    opened = [utc(r.get('opened_at')) for r in comparable]
    closed = [utc(r.get('closed_at')) for r in comparable]
    first = min((t for t in opened if t),default=None)
    last = max((t for t in closed if t),default=None)
    unique = len({(r.get('asset'),r.get('direction'),payload(r.get('payload'))['r66_event_id']) for r in comparable})
    history = dict(history or {})
    nav=nav_coverage(history,first if all(opened) else None,last if all(closed) else None)
    paths=path_coverage(comparable)
    rules=rule_evidence(comparable)
    coverage=bool(nav['complete'] and paths['complete'] and not history_truncated)
    pf_state = 'FINITE' if stats['profit_factor'] is not None else 'NO_LOSSES' if stats['wins'] else 'NO_PROFITS'
    ledger_count=len(all_rows) if total_closed_trades is None else total_closed_trades
    return dict(portfolio=name,closed_trades=stats['trades'],unique_episodes=unique,
        wins=stats['wins'],win_rate=stats['win_rate'],net_pnl_rub=stats['net_pnl_rub'],
        avg_net_pnl_rub=stats['avg_net_pnl_rub'],profit_factor=stats['profit_factor'],
        profit_factor_state=pf_state,
        costs_rub=stats['costs_rub'],unknown_exits=sum(str(payload(r.get('payload')).get('exit_reason')
            or payload(r.get('payload')).get('close_reason') or '').upper() in ('','UNKNOWN') for r in comparable),
        max_drawdown=number(history.get('max_drawdown')) if coverage else None,
        observed_nav_max_drawdown=number(history.get('max_drawdown')),
        nav_history_coverage=nav,path_coverage=paths,history_complete=coverage,
        rule_evidence=rules,rule_evidence_complete=rules['complete'],
        accounting_complete=bool(comparable) and all(accounting_complete(r) for r in comparable),
        evidence_epoch=first.isoformat() if first else None,execution_cohort=COHORT,
        current_entry_version=current,cohort_assignment='EXACT_IMMUTABLE_ENTRY_SHA_POLICY_AND_EPOCH',
        closed_ledger_trades=ledger_count,history_truncated=history_truncated,
        noncomparable_closed_trades_excluded=max(0,ledger_count-len(comparable)),
        administrative_rebase_excluded=True,read_only_evidence=True)


def candidate_projection_sql():
    # Reuse the bounded source/event/MA-proof whitelist. Selecting horizon is
    # essential: observed_event verifies it against the event's native TF.
    provenance="jsonb_build_object("+','.join("'"+key+"',t.payload->'"+key+"'" for key in
        ('entry_rule_revision','strategy_epoch','strategy_entry_sha','strategy_policy_hash'))+")"
    return (','.join('t.'+key for key in LI.TRADE_FIELDS)+
            ',('+LI.payload_sql('t')+' || '+provenance+') AS payload')


NAV_HISTORY_SQL="""WITH raw_series AS (
    SELECT observed_at,nav_rub,
           LAG(observed_at) OVER(ORDER BY observed_at) AS previous_at,
           MAX(CASE WHEN nav_rub>0 AND nav_rub NOT IN ('NaN'::float8,'Infinity'::float8)
                    THEN nav_rub END) OVER(ORDER BY observed_at) AS hwm
    FROM paper_nav_history WHERE portfolio_name=%s
      AND observed_at >= %s::timestamptz-INTERVAL '5 minutes'
      AND observed_at <= %s::timestamptz+INTERVAL '5 minutes'
), series AS (
    SELECT *,EXTRACT(EPOCH FROM observed_at-previous_at)::float8 AS gap_seconds FROM raw_series
), cadence AS (
    SELECT percentile_cont(0.5) WITHIN GROUP(ORDER BY gap_seconds)
           FILTER(WHERE gap_seconds>0) AS seconds FROM series
)
SELECT MIN(observed_at) AS first_observation,MAX(observed_at) AS last_observation,
       COUNT(*) AS observation_count,COUNT(*) FILTER(WHERE gap_seconds>0) AS interval_count,
       MAX(cadence.seconds) AS observed_cadence_seconds,MAX(gap_seconds) AS max_gap_seconds,
       COUNT(*) FILTER(WHERE gap_seconds>cadence.seconds*%s) AS gap_count,
       COUNT(*) FILTER(WHERE nav_rub IS NULL OR nav_rub<=0
                          OR nav_rub IN ('NaN'::float8,'Infinity'::float8)) AS invalid_observations,
       MAX(CASE WHEN hwm>0 AND nav_rub>0
                     AND nav_rub NOT IN ('NaN'::float8,'Infinity'::float8)
                THEN (hwm-nav_rub)/hwm END) AS max_drawdown
FROM series CROSS JOIN cadence"""


def candidate_metrics(conn, name):
    current=Q.version_identity()
    ledger=conn.execute("""SELECT COUNT(*) AS closed_trades,
        CASE WHEN COUNT(net_pnl_rub)=COUNT(*) THEN SUM(net_pnl_rub) END AS net_pnl_rub,
        CASE WHEN COUNT(gross_pnl_rub)=COUNT(*) THEN SUM(gross_pnl_rub) END AS gross_pnl_rub,
        CASE WHEN COUNT(fees_rub)=COUNT(*) THEN SUM(fees_rub) END AS fees_rub,
        CASE WHEN COUNT(funding_rub)=COUNT(*) THEN SUM(funding_rub) END AS funding_rub
        FROM paper_trades WHERE portfolio_name=%s
        AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))""",(name,)).fetchone() or {}
    rows=[]
    if current['complete']:
        rows=conn.execute('SELECT '+candidate_projection_sql()+""" FROM paper_trades t
            WHERE t.portfolio_name=%s
              AND (t.closed_at IS NOT NULL OR t.status IN ('CLOSED','CLOSE','EXITED'))
              AND t.payload->>'strategy_epoch'=%s
              AND t.payload->>'strategy_entry_sha'=%s
              AND t.payload->>'strategy_policy_hash'=%s
            ORDER BY t.closed_at DESC NULLS LAST,t.trade_id LIMIT %s""",
            (name,current['strategy_epoch'],current['strategy_entry_sha'],current['strategy_policy_hash'],
             MAX_CANDIDATE_ROWS+1)).fetchall()
    truncated=len(rows)>MAX_CANDIDATE_ROWS
    rows=rows[:MAX_CANDIDATE_ROWS]
    comparable=[r for r in rows if comparable_trade(r,current)]
    first=min((utc(r.get('opened_at')) for r in comparable if utc(r.get('opened_at'))),default=None)
    last=max((utc(r.get('closed_at')) for r in comparable if utc(r.get('closed_at'))),default=None)
    history={}
    if first and last:
        history=conn.execute(NAV_HISTORY_SQL,(name,first,last,NAV_GAP_MULTIPLE)).fetchone() or {}
    result=evaluate_evidence(rows,history,name,current=current,
        total_closed_trades=int(ledger.get('closed_trades') or 0),history_truncated=truncated)
    result['ledger_totals']=ledger
    result['ledger_scope']='ALL_CLOSED_FINANCIAL_RECORDS_NOT_CURRENT_RULE_PROOF'
    return result
