"""Source-labelled native OHLC for the canonical timeframe entry rule."""
from datetime import datetime, timezone
import veritas_price_source as VPS
import veritas_timeframe_structure as TS
import veritas_canonical_constitution as CTC
from veritas_entry_scenarios import daily_context, daily_features, select_context


def native_ohlc(rows):
    """Preserve actual exchange timestamps and opens, never reconstruct from closes."""
    return [dict(ts=float(x[0])/1000, open=float(x[1]), high=float(x[2]),
                 low=float(x[3]), close=float(x[4]), volume=float(x[5]))
            for x in rows or [] if len(x) >= 6]


def _complete_profinance_five_minute_tail(labelled, identity, clock, statuses, minutes):
    """Use complete same-source minutes only for absent closed native 5m bars."""
    end = TS.timestamp(clock)
    if end is None:
        return
    native = labelled.get('5m') or []
    closed = TS.closed_bars(native, '5m', clock)
    native_end = closed[-1]['available_at'] if closed else None
    expected = int(end) // 300 * 300
    if native_end is not None and native_end >= expected:
        return
    # Preserve missing/foreign duplicate provenance for the aggregation proof;
    # legacy attachment defaults and filtering cannot certify these inputs.
    minutes = list(minutes or [])[-500:]
    closed_minutes = TS.closed_bars(minutes, '1m', clock)
    if not closed_minutes or not 0 <= end - closed_minutes[-1]['available_at'] <= 60:
        return
    occupied = {TS.timestamp(bar.get('ts')) for bar in native}
    derived = [dict(bar, volume=None, volume_available=False,
                    derived_from_timeframe='1m', derived_from_complete_native_minutes=True)
               for bar in TS.aggregate_closed_bars(minutes, '1m', '5m', clock, anchor=0)
               if (native_end is None or bar['ts'] >= native_end)
               and bar['ts'] not in occupied
               and isinstance(bar.get('source_identity'), dict)
               and bar['source_identity'].get('asset') == identity.get('asset')
               and TS._source_token(bar['source_identity']) == TS._source_token(identity)]
    if not derived:
        return
    labelled['5m'] = sorted(native + derived, key=lambda bar: TS.timestamp(bar.get('ts')) or 0)[-500:]
    effective = TS.closed_bars(labelled['5m'], '5m', clock)
    last_end = effective[-1]['available_at']
    previous = dict(statuses.get('5m') or {})
    fresh = 0 <= end - last_end <= 300
    statuses['5m'] = dict(previous, native_history_status=dict(previous.get('native_history_status') or previous),
        status='READY' if fresh else 'STALE', fresh=fresh, bars=len(effective),
        last_closed_at=last_end, age_seconds=end-last_end,
        expected_last_closed_at=expected, latest_completed_period_present=last_end >= expected,
        reason='COMPLETE_SAME_SOURCE_1M_TAIL', derived_from_timeframe='1m',
        derived_source_fetched_at=(statuses.get('1m') or {}).get('fetched_at'),
        derived_closed_bars=sum(bool(bar.get('derived_from_complete_native_minutes')) for bar in labelled['5m']),
        derived_added_this_attach=len(derived), volume_available=False)


def attach(raw, now=None):
    r = dict(raw or {})
    asset = str(r.get('asset') or '')
    clock = datetime.now(timezone.utc) if now is None else now
    r.pop('_same_tf_context_cache', None)
    attached_mapping = r.get('structure_bars_by_timeframe') or {}
    attached_identity = r.get('structure_source_identity')
    identity = VPS.identity(asset, r)
    r['structure_source_identity'] = identity
    r['structure_bars_by_timeframe'] = {}
    if not identity:
        return r
    mapping = {}
    profinance = identity['key'].startswith('PROFINANCE:')
    if profinance:
        from veritas_profinance_history import fetch_history_bundle
        bundle = (dict(source_identity=attached_identity,
                       bars_by_timeframe=attached_mapping,
                       forming_bars_by_timeframe=r.get('structure_forming_bars_by_timeframe') or {},
                       status_by_timeframe=r.get('structure_history_status'))
                  if r.get('native_source_history_attached')
                  else fetch_history_bundle(asset=asset, now=now))
        if now is None:
            clock = datetime.now(timezone.utc)
        r['structure_history_status'] = dict(bundle.get('status_by_timeframe') or {})
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
        labelled[tf] = [dict(b, timeframe=tf, source_identity=dict(b.get('source_identity') or identity))
                        for b in rows[-500:]
                        if (not b.get('source_identity') or VPS.same(identity,b['source_identity']))
                        and (not b.get('timeframe') or b['timeframe']==tf)]
    if profinance:
        _complete_profinance_five_minute_tail(labelled, identity, clock, r['structure_history_status'], mapping.get('1m'))
        r['structure_minute_bars'] = labelled.get('1m', [])
        r['structure_intraday_bars'] = labelled.get('5m', [])
    # Fetch once per source/contract, before any hourly aggregation. Only the
    # native adapter can certify D1 as daily MA input.
    r['structure_bars_by_timeframe'] = labelled
    from veritas_native_daily import fetch_native_daily
    daily = fetch_native_daily(r, clock)
    r['native_daily_bars'] = daily.get('bars') or []
    r['native_daily_history_status'] = {k:v for k,v in daily.items() if k != 'bars'}
    if profinance:
        # A provider date label is sufficient to order certified MA observations,
        # but cannot establish the timestamp of a native structural breakout.
        for tf in ('1d', '3d', '7d'):
            labelled[tf] = [b for b in labelled.get(tf, [])
                            if b.get('interval_boundary_verified') is True]
    elif not labelled.get('1d') and r['native_daily_bars']:
        labelled['1d'] = r['native_daily_bars']
    for source_tf, tf in (('1h','4h'), ('1h','1d'), ('1d','3d'), ('1d','7d')):
        if not labelled.get(tf) and labelled.get(source_tf):
            # Fixed calendar buckets. Weekly anchor is Monday 00:00 UTC;
            # multi-day buckets never slide when a new quote arrives.
            phase = float(labelled[source_tf][0]['ts']) % 86400 if source_tf=='1d' else 0
            offset = phase if phase <= 43200 else phase - 86400
            anchor = (345600 if tf == '7d' else 0) + offset
            labelled[tf] = TS.aggregate_closed_bars(labelled[source_tf], source_tf, tf,
                                                   clock, anchor=anchor)
            if profinance:
                for bar in labelled[tf]:
                    bar.update(interval_boundary_verified=True,
                               native_time_basis='COMPLETE_OBSERVED_UTC_BUCKET')
    if profinance:
        statuses = dict(r.get('structure_history_status') or {})
        for tf in ('1d', '3d', '7d'):
            rows = labelled.get(tf) or []
            statuses[tf] = dict(statuses.get(tf) or {}, bars=len(rows),
                status='READY' if rows else 'UNAVAILABLE',
                reason='COMPLETE_OBSERVED_TIME_BUCKETS' if rows else 'NATIVE_DAILY_INTERVAL_UNVERIFIED',
                interval_boundary_verified=bool(rows))
        r['structure_history_status'] = statuses
    r['structure_bars_by_timeframe'] = labelled
    return r


def context(raw, horizon, now=None):
    clock = datetime.now(timezone.utc) if now is None else now
    stamp = TS.timestamp(clock)
    cache = raw.setdefault('_same_tf_context_cache', {})
    cached = cache.get(horizon)
    if cached is None or cached['as_of'] != stamp:
        rows = (raw.get('structure_bars_by_timeframe') or {}).get(horizon) or []
        identity = raw.get('structure_source_identity') or {}
        daily_clock_missing = bool(str(identity.get('key', '')).startswith('PROFINANCE:')
            and horizon in ('1d', '3d', '7d')
            and any(b.get('interval_boundary_verified') is not True for b in rows))
        if daily_clock_missing:
            rows = []
        result = TS.build_context(rows, horizon, clock,
                    asset=raw.get('asset') or '', source_identity=raw.get('structure_source_identity'),
                    config=CTC.STRUCTURAL_ENTRY_POLICY)
        if daily_clock_missing:
            result['input_issue'] = 'NATIVE_DAILY_INTERVAL_UNVERIFIED'
        else:
            result = select_context(raw, horizon, clock, result)
        cache[horizon] = {'as_of':stamp, 'context':result}
    return cache[horizon]['context']
