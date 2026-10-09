"""Commit each independent paper portfolio before yielding to protection.

Candidate routing and completed-book cleanup do not own the accounting lock.
All mutations, risk checks, funding and protection flags of one portfolio still
commit or roll back together. No broker operations or trading policies live here.
"""
from datetime import datetime, timezone
import time

import veritas_accounting_io as AIO
import veritas_position_guard as VPG
import veritas_profit_protection as VPP


VERSION = 'PER_PORTFOLIO_ACCOUNTING_CYCLE_V2_CURRENCY_FIRST'


def emit_diagnostic(emit, event, **fields):
    if emit is not None:
        try:
            emit(event, **fields)
        except Exception:
            # Diagnostics after COMMIT cannot turn a committed book into an
            # alleged rollback or prevent processing another independent book.
            pass


def run_books(*, pg_connect, policies, make_book, step_one, prices, ruonia,
              usdrub, observed_at, commission_rate, summary, emit=None,
              execution_clock=None):
    started = time.monotonic()
    results, committed, uncertain, failures, timings = [], [], [], {}, []
    original_names = list(policies)
    execution_sequence = []
    # Currency is the latency-sensitive 1m/5m book.  Each portfolio still owns
    # an independent transaction, but running Currency first prevents it from
    # aging behind four unrelated paper books on every cycle.  Python's sort is
    # stable, so the relative order of all other portfolios is unchanged.
    policy_items = list(policies.items())
    policy_items.sort(key=lambda item: 0 if str(item[0]) == 'Currency' else 1)
    for name, policy in policy_items:
        execution_sequence.append(name)
        book = result = None
        timing = {'portfolio': name, 'status': 'NOT_STARTED'}
        stage, did_commit, outcome = 'candidate_routing', False, False
        book_started = time.monotonic()
        try:
            prepare_started = time.monotonic()
            book = make_book(name, policy)
            timing['candidate_seconds'] = time.monotonic() - prepare_started
            stage = 'connection'
            with pg_connect() as connection:
                stage = 'accounting'
                with VPG.book_transaction(connection, lane='PORTFOLIO', timing=timing):
                    # A protective pass may have advanced last_mark_at while
                    # we waited. Live accounting must use its actual new clock.
                    ts = (execution_clock() if execution_clock is not None else
                          observed_at or datetime.now(timezone.utc).isoformat())
                    timing['execution_observed_at'] = ts
                    emit_diagnostic(emit, 'paper_portfolio_phase', phase='book_start',
                            portfolio=name, execution_observed_at=ts)
                    work_started = time.monotonic()
                    cpu_started = time.thread_time()
                    measured_connection = AIO.AccountingConnection(connection)
                    try:
                        result = step_one(measured_connection, name, policy, book, prices,
                                          ruonia, usdrub, ts, commission_rate, summary)
                        if not isinstance(result, dict) or result.get('name') != name:
                            raise ValueError('PORTFOLIO_RESULT_REQUIRED')
                    finally:
                        timing['accounting_seconds'] = time.monotonic() - work_started
                        timing['accounting_cpu_seconds'] = time.thread_time() - cpu_started
                        timing['accounting_io'] = measured_connection.snapshot()
                        measured_connection = None
                    stage = 'protection'
                    emit_diagnostic(emit, 'paper_portfolio_phase', phase='protection_start',
                            portfolio=name)
                    protection_started = time.monotonic()
                    protection_at = execution_clock() if execution_clock is not None else ts
                    VPP.refresh(connection, name=name, now=protection_at,
                                commission=commission_rate)
                    timing['protection_seconds'] = time.monotonic() - protection_started
                    emit_diagnostic(emit, 'paper_portfolio_phase', phase='protection_done',
                            portfolio=name)
                    stage = 'commit'
                did_commit = True
                outcome = True
            committed.append(name)
            results.append(dict(result, execution_observed_at=ts,
                                accounting_committed=True))
        except Exception as error:
            # Other portfolios already committed. Preserve that fact and never
            # publish a success/fill returned by a subsequently rolled-back book.
            outcome = True if did_commit else None if timing.get('status') == 'COMMIT_UNKNOWN' else False
            if outcome is None:
                uncertain.append(name)
            detail = {'stage': stage, 'error_code': type(error).__name__,
                      'error': str(error)[:240], 'accounting_committed': outcome}
            failures[name] = detail
            results.append(dict(name=name, status='ERROR', **detail))
            if did_commit and name not in committed:
                committed.append(name)
            emit_diagnostic(emit, 'paper_portfolio_error', portfolio=name, **detail)
        finally:
            # Release only local references; source evidence and committed
            # diagnostic results remain owned by their original consumers.
            book = result = None
            timing['book_seconds_before_cleanup'] = time.monotonic() - book_started
            cleanup_started = time.monotonic()
            emit_diagnostic(emit, 'paper_portfolio_phase',
                    phase='book_done' if did_commit else 'book_uncertain' if outcome is None else 'book_failed',
                    committed=outcome, **timing)
            timing['boundary_seconds'] = time.monotonic() - cleanup_started
            timing['total_seconds'] = time.monotonic() - book_started
            timings.append(timing)
    # Preserve the public portfolio ordering even though Currency executed first.
    order = {name: index for index, name in enumerate(original_names)}
    results.sort(key=lambda row: order.get(row.get('name'), len(order)))
    timings.sort(key=lambda row: order.get(row.get('portfolio'), len(order)))
    committed = [name for name in original_names if name in committed]
    uncertain = [name for name in original_names if name in uncertain]
    return {'status': 'PARTIAL' if failures else 'OK', 'portfolios': results,
            'committed_portfolios': committed, 'errors': failures,
            'uncertain_portfolios': uncertain,
            'accounting_snapshot_basis': 'SEQUENTIAL_INDEPENDENT_PORTFOLIO_COMMITS',
            'timing': {'version': VERSION, 'total_seconds': time.monotonic() - started,
                       'execution_sequence': execution_sequence,
                       'portfolios': timings}}
