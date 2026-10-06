"""Source-labelled native OHLC for the canonical timeframe entry rule."""
from datetime import datetime, timezone
import veritas_price_source as VPS
import veritas_timeframe_structure as TS
import veritas_canonical_constitution as CTC


def native_ohlc(rows):
    """Preserve actual exchange timestamps and opens, never reconstruct from closes."""
    return [dict(ts=float(x[0])/1000, open=float(x[1]), high=float(x[2]),
                 low=float(x[3]), close=float(x[4]), volume=float(x[5]))
            for x in rows or [] if len(x) >= 6]


def attach(raw, now=None):
    r = dict(raw or {})
    asset = str(r.get('asset') or '')
    clock = datetime.now(timezone.utc) if now is None else now
    r.pop('_same_tf_context_cache', None)
    identity = VPS.identity(asset, r)
    r['structure_source_identity'] = identity
    r['structure_bars_by_timeframe'] = {}
    if not identity:
        return r
    mapping = {}
    if identity['key'].startswith('PROFINANCE:'):
        from veritas_profinance_history import fetch_history_bundle
        bundle = fetch_history_bundle(asset=asset, now=clock)
        r['structure_history_status'] = bundle.get('status_by_timeframe') or {}
        if not VPS.same(identity, bundle.get('source_identity')):
            r['structure_history_error'] = 'SAME_TF_SOURCE_MISMATCH'
            return r
        mapping = dict(bundle.get('bars_by_timeframe') or {})
        for tf, partials in (bundle.get('forming_bars_by_timeframe') or {}).items():
            mapping[tf]=list(mapping.get(tf) or [])+list(partials or [])
        # All structural consumers receive the same provider, including their
        # old feature names. Forecast history elsewhere remains research only.
        r['structure_minute_bars'] = mapping.get('1m', [])
        r['structure_intraday_bars'] = mapping.get('5m', [])
    elif identity['key'].startswith(('BINANCE:', 'MOEX:', 'TBANK_GRPC:')):
        mapping = {'1m':r.get('structure_minute_bars') or [],
                   '5m':r.get('canonical_five_minute_bars') or [],
                   '1h':r.get('canonical_hourly_bars') or [],
                   '1d':r.get('canonical_daily_bars') or []}
    else:
        # A direct quote and foreign history are not an executable setup.
        r['structure_history_error'] = 'SAME_TF_HISTORY_SOURCE_UNAVAILABLE'
        return r
    labelled = {}
    for tf, rows in mapping.items():
        labelled[tf] = [dict(b, timeframe=tf, source_identity=identity)
                        for b in rows[-500:]
                        if (not b.get('source_identity') or VPS.same(identity,b['source_identity']))
                        and (not b.get('timeframe') or b['timeframe']==tf)]
    for source_tf, tf in (('1h','4h'), ('1h','1d'), ('1d','3d'), ('1d','7d')):
        if not labelled.get(tf) and labelled.get(source_tf):
            # Fixed calendar buckets. Weekly anchor is Monday 00:00 UTC;
            # multi-day buckets never slide when a new quote arrives.
            phase = float(labelled[source_tf][0]['ts']) % 86400 if source_tf=='1d' else 0
            offset = phase if phase <= 43200 else phase - 86400
            anchor = (345600 if tf == '7d' else 0) + offset
            labelled[tf] = TS.aggregate_closed_bars(labelled[source_tf], source_tf, tf,
                                                   clock, anchor=anchor)
    r['structure_bars_by_timeframe'] = labelled
    return r


def context(raw, horizon, now=None):
    clock = datetime.now(timezone.utc) if now is None else now
    stamp = TS.timestamp(clock)
    cache = raw.setdefault('_same_tf_context_cache', {})
    cached = cache.get(horizon)
    if cached is None or cached['as_of'] != stamp:
        rows = (raw.get('structure_bars_by_timeframe') or {}).get(horizon) or []
        result = TS.build_context(rows, horizon, clock,
                    asset=raw.get('asset') or '', source_identity=raw.get('structure_source_identity'),
                    config=CTC.STRUCTURAL_ENTRY_POLICY)
        cache[horizon] = {'as_of':stamp, 'context':result}
    return cache[horizon]['context']
