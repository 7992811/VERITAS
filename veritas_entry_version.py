"""Immutable entry provenance for comparable strategy evidence.

No historical row is upgraded from the current deployment or current policy.
Missing stamps remain missing; an epoch alone is never exact version evidence.
"""
import hashlib
import json
from collections import defaultdict
import veritas_canonical_constitution as CTC
import veritas_release as RELEASE
from veritas_trade_audit import payload

POLICY_HASH_VERSION='ENTRY_POLICIES_V2'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()[:24]


def policy_hash():
    """Full entry/lifecycle policy identity for a newly created trade."""
    return digest({'schema':POLICY_HASH_VERSION,'costs':CTC.COST_POLICY,
                   'roles':CTC.STRATEGY_ROLE_POLICY,'portfolios':CTC.PORTFOLIO_POLICIES,
                   'lifecycle':CTC.LIFECYCLE_POLICY,
                   'structural_entry':CTC.STRUCTURAL_ENTRY_POLICY,
                   'daily_ma_rebound':CTC.MA_REBOUND_POLICY})


def _stamp(value):
    return value.strip() if isinstance(value,str) and value.strip() else None


def version_identity(trade=None):
    if trade is None:
        epoch,sha,policy=CTC.STRATEGY_EPOCH,RELEASE.deployment_sha(),policy_hash()
    else:
        p=payload(trade.get('payload'))
        epoch,sha,policy=(p.get(key) for key in
                          ('strategy_epoch','strategy_entry_sha','strategy_policy_hash'))
    values=tuple(_stamp(value) for value in (epoch,sha,policy))
    return dict(zip(('strategy_epoch','strategy_entry_sha','strategy_policy_hash'),values),
                complete=all(values))


def matches_current_version(trade,current=None):
    current=current or version_identity()
    observed=version_identity(trade)
    return bool(current['complete'] and observed['complete'] and observed==current)


def partition_versions(rows,current,limit=50):
    """Bounded display groups; omitted rows remain in full financial totals."""
    grouped=defaultdict(list)
    for trade in rows:
        identity=version_identity(trade)
        key=tuple(identity[field] for field in
                  ('strategy_epoch','strategy_entry_sha','strategy_policy_hash'))
        grouped[key].append(trade)
    ordered=sorted(grouped.values(),
        key=lambda group:max(str(t.get('opened_at') or '') for t in group),reverse=True)
    displayed=[]
    for group in ordered[:limit]:
        identity=version_identity(group[0])
        displayed.append({'entry_version':identity,
            'assignment':'CURRENT_VERSION' if matches_current_version(group[0],current) else
                         'PRIOR_VERSION' if identity['complete'] else 'MISSING_VERSION_STAMP',
            'first_entry_at':min(str(t.get('opened_at') or '') for t in group),
            'last_entry_at':max(str(t.get('opened_at') or '') for t in group),
            'trades':group})
    return displayed,len(ordered),sum(len(group) for group in ordered[limit:])
