"""Read-only service state: readiness, partial failures and observed providers."""
import time
from datetime import datetime, timezone
import veritas_price_source as SOURCE


def readiness(bootstrap_ready, database_configured, database_health, now=None):
    stamp = time.time() if now is None else now
    checked = database_health.get('checked_at') or 0
    age = max(0., stamp-checked)
    checks = {'bootstrap': bool(bootstrap_ready),
              'database_configured': bool(database_configured),
              'recent_postgres_connection': database_health.get('ok') is True and age <= 120}
    ready = all(checks.values())
    return {'ok': ready, 'status': 'READY' if ready else 'NOT_READY',
            'scope': 'HTTP_SERVICE_AND_STORAGE', 'checks': checks,
            'database_observation_age_seconds': round(age, 1) if checked else None,
            'trading_readiness_included': False}


def learning_operation(jobs, ready):
    failed, deferred, pending = [], [], []
    for name, job in jobs.items():
        state = str(job.get('status') or 'QUEUED')
        if state in ('ERROR', 'UNAVAILABLE', 'RETRY'):
            failed.append(name)
        elif state.startswith('DEFERRED'):
            deferred.append(name)
        elif not job.get('last_success_at'):
            pending.append(name)
    status = ('STARTING' if not ready else 'DEGRADED' if failed else
              'WAITING_RESOURCES' if deferred else 'WARMING_UP' if pending else 'RUNNING')
    return {'status': status, 'failed_jobs': failed, 'deferred_jobs': deferred,
            'pending_jobs': pending,
            'capacity_review_required': any(j.get('capacity_review_required') for j in jobs.values())}


def protection_operation(state, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(state.get('checked_at')).replace('Z','+00:00'))
        age = max(0., (now-stamp).total_seconds())
    except (TypeError, ValueError):
        age = None
    target = float(state.get('interval_seconds') or 15)
    duration = float(state.get('duration_seconds') or 0)
    status = ('NOT_OBSERVED' if age is None else 'STALE' if age>max(45.,target*3) else
              'DEADLINE_EXCEEDED' if duration>target else 'WITHIN_TARGET')
    return {'status': status, 'target_interval_seconds': target,
            'last_pass_age_seconds': round(age,1) if age is not None else None,
            'last_pass_duration_seconds': duration,
            'deadline_met': status=='WITHIN_TARGET'}


def asset_catalog(assets, rows):
    """Describe actually observed identities; do not infer exchange contracts."""
    result = {}
    for asset in assets:
        observed = [row for row in rows if row.get('asset') == asset]
        identities = {}
        for row in observed:
            ident = SOURCE.identity(asset, row)
            if ident:
                identities[(ident['key'], ident.get('contract_id'))] = ident
        values = list(identities.values())
        result[asset] = {
            'status': 'OBSERVED' if values else 'NOT_OBSERVED',
            'primary': values[0]['primary_source'] if len(values) == 1 else None,
            'source_status': 'CONSISTENT' if len(values) == 1 else 'MIXED' if values else 'UNKNOWN',
            'source_identities': values,
            'verified_cells': sum(row.get('source_gate_pass') is True for row in observed),
            'observed_cells': len(observed),
            'position_source_policy': 'IMMUTABLE_ENTRY_IDENTITY',
            'execution_gate': 'CURRENT_QUOTE_AND_CANONICAL_ADMISSION_REQUIRED',
        }
    return result
