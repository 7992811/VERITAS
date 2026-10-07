"""Bounded native D1 history for the already selected execution source.

Public market-data GET only. T-Invest reuses its read-only candle cache without
new account/order RPCs. No foreign source/contract fallback, worker, retry loop
or artificial non-trading day is introduced.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import threading
import time
from zoneinfo import ZoneInfo
import httpx
import veritas_daily_averages as DA
import veritas_price_source as VPS
from veritas_timeframe_structure import timestamp

VERSION = "SOURCE_LOCKED_NATIVE_DAILY_V2"
MAX_BARS = 500
MAX_RESPONSE_BYTES = 512*1024
TTL_SECONDS = 900
RETRY_SECONDS = 60
MOSCOW = ZoneInfo("Europe/Moscow")
_NETWORK_GATE = threading.BoundedSemaphore(2)


def _identity(raw):
    return VPS.identity(str((raw or {}).get("asset") or ""),raw)


def _supplied_profinance_daily(raw):
    mapping=raw.get("structure_bars_by_timeframe") or {}
    attached=raw.get("native_source_history_attached") is True
    if attached and "native_daily_evidence" in raw:
        # The source bundle's untouched D1 observations carry first-proof and
        # revision times, even when a historical structural cutoff hides them.
        return True,raw.get("native_daily_evidence") or []
    return attached and "1d" in mapping,mapping.get("1d") or []



def _number(value):
    if isinstance(value,bool):
        return None
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _bar(start,end,prices,identity,native_interval,*,volume=None,available_at=None):
    vals=dict(zip(("open","high","low","close"),map(_number,prices)))
    if (start is None or end is None or not start<end
            or any(x is None or x<=0 for x in vals.values())
            or vals["low"]>min(vals["open"],vals["close"])
            or vals["high"]<max(vals["open"],vals["close"])):
        raise ValueError("NATIVE_DAILY_INVALID_OHLC")
    return {**vals,"ts":float(start),"end_ts":float(end),
            "available_at":float(end if available_at is None else max(end,available_at)),
            "timeframe":"1d","native_timeframe":"1d","native_interval":native_interval,
            "aggregation":"NATIVE","source_identity":deepcopy(identity),
            "volume":_number(volume),"volume_available":_number(volume) is not None,
            "finalized":True}


def parse_binance(items,identity,now):
    """Spot native UTC D1; supplied closeTime is inclusive milliseconds."""
    asof=timestamp(now)
    if not isinstance(items,list) or asof is None:
        raise ValueError("BINANCE_DAILY_RESPONSE_INVALID")
    bars=[]
    for item in items:
        if not isinstance(item,(list,tuple)) or len(item)<7:
            raise ValueError("BINANCE_DAILY_RESPONSE_INVALID")
        start,close_ms=_number(item[0]),_number(item[6])
        if start is None or close_ms is None:
            raise ValueError("BINANCE_DAILY_TIMESTAMP_INVALID")
        start,end=start/1000,(close_ms+1)/1000
        if start%DA.DAY or end-start != DA.DAY:
            raise ValueError("BINANCE_DAILY_INTERVAL_MISMATCH")
        if end>asof:
            continue
        bars.append(_bar(start,end,item[1:5],identity,"1d",volume=item[5]))
    return bars[-MAX_BARS:]


def _moscow_time(value):
    try:
        at=datetime.fromisoformat(str(value))
        return at.replace(tzinfo=MOSCOW).timestamp() if at.tzinfo is None else at.timestamp()
    except (ValueError,TypeError):
        return None


def parse_moex(payload,identity,now):
    """Native ISS interval=24; current Moscow date remains incomplete.

    ISS can expose a still-forming day's last trade as end. Final availability
    cannot precede the next Moscow midnight even if the current end is earlier.
    """
    block=(payload or {}).get("candles") or {}
    columns,data=block.get("columns"),block.get("data")
    asof=timestamp(now)
    if not isinstance(columns,list) or not isinstance(data,list) or asof is None:
        raise ValueError("MOEX_DAILY_RESPONSE_INVALID")
    names=[str(k).lower() for k in columns]
    if not {"begin","end","open","high","low","close"}.issubset(names):
        raise ValueError("MOEX_DAILY_COLUMNS_INVALID")
    bars=[]
    for values in data:
        if not isinstance(values,list) or len(values)!=len(names):
            raise ValueError("MOEX_DAILY_RESPONSE_INVALID")
        row=dict(zip(names,values))
        start,provider_end=_moscow_time(row["begin"]),_moscow_time(row["end"])
        if start is None or provider_end is None or not start<=provider_end:
            raise ValueError("MOEX_DAILY_TIMESTAMP_INVALID")
        local=datetime.fromtimestamp(start,MOSCOW)
        end=(local.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp()
        if provider_end>=end:
            raise ValueError("MOEX_DAILY_INTERVAL_MISMATCH")
        if end>asof:
            continue
        item=_bar(start,end,[row[k] for k in ("open","high","low","close")],
                  identity,24,volume=row.get("volume"))
        item["provider_end_ts"]=provider_end
        bars.append(item)
    return bars


def parse_tbank(snapshot,raw,identity,now):
    """Exact cached broker D1 with the quote's validated normalization."""
    uid=identity.get("contract_id")
    if (not uid or snapshot.get("instrument_uid")!=uid
            or snapshot.get("interval")!="1d" or snapshot.get("aggregation")!="NATIVE"
            or snapshot.get("source")!="TBANK_GRPC" or snapshot.get("status") not in ("OK","STALE")):
        raise ValueError("TBANK_DAILY_SOURCE_CONTRACT_OR_INTERVAL_MISMATCH")
    factor=_number((raw.get("contract") or {}).get("normalization_factor"))
    if factor is None or factor<=0:
        raise ValueError("TBANK_DAILY_PRICE_UNIT_UNVERIFIED")
    asof=timestamp(now)
    if asof is None:
        raise ValueError("TBANK_DAILY_TIMESTAMP_INVALID")
    bars=[]
    for item in snapshot.get("candles") or []:
        if (item.get("is_complete") is False or item.get("finalized") is False
                or item.get("candle_source")=="CANDLE_SOURCE_DEALER_WEEKEND"):
            continue
        start=timestamp(item.get("time",item.get("ts")))
        if start is None:
            raise ValueError("TBANK_DAILY_TIMESTAMP_INVALID")
        # Existing cache filters is_complete. Broker daily bars open at UTC
        # midnight; retain the full boundary, never promote the current day.
        end=start+DA.DAY
        if start%DA.DAY:
            raise ValueError("TBANK_DAILY_INTERVAL_MISMATCH")
        if end>asof:
            continue
        prices=[_number(item.get(k)) for k in ("open","high","low","close")]
        if any(p is None for p in prices):
            raise ValueError("TBANK_DAILY_INVALID_OHLC")
        bars.append(_bar(start,end,[p*factor for p in prices],identity,
                         "CANDLE_INTERVAL_DAY",volume=item.get("volume_lots")))
    return bars[-MAX_BARS:]


def _http_json(url,params,remaining):
    deadline=time.monotonic()+remaining
    timeout=httpx.Timeout(max(.05,remaining),connect=min(1.5,max(.05,remaining)))
    with httpx.Client(timeout=timeout,follow_redirects=False,
                      headers={"User-Agent":"VERITAS native daily market data"},
                      limits=httpx.Limits(max_connections=1,max_keepalive_connections=0)) as client:
        with client.stream("GET",url,params=params) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0)>MAX_RESPONSE_BYTES:
                raise ValueError("NATIVE_DAILY_RESPONSE_TOO_LARGE")
            parts,count=[],0
            for part in response.iter_bytes():
                count+=len(part)
                if count>MAX_RESPONSE_BYTES or time.monotonic()>deadline:
                    raise TimeoutError("NATIVE_DAILY_BUDGET_EXHAUSTED")
                parts.append(part)
            return json.loads(b"".join(parts))


class DailyHistoryCache:
    """Seven source/contract entries, 500 days each, shared and single-flight."""
    def __init__(self,fetch_json=None,clock=time.time,monotonic=time.monotonic,
                 tbank_snapshot=None,profinance_fetch=None):
        self._fetch_json=fetch_json or _http_json
        self._clock,self._monotonic=clock,monotonic
        self._tbank_snapshot,self._profinance_fetch=tbank_snapshot,profinance_fetch
        self._lock=threading.Lock()
        self._cache,self._attempts,self._running,self._errors={},{},set(),{}

    def _get(self,url,params,deadline):
        remaining=deadline-self._monotonic()
        if remaining<=0 or not _NETWORK_GATE.acquire(timeout=max(0,remaining)):
            raise TimeoutError("NATIVE_DAILY_BUDGET_EXHAUSTED")
        try:
            remaining=deadline-self._monotonic()
            if remaining<=0:
                raise TimeoutError("NATIVE_DAILY_BUDGET_EXHAUSTED")
            return self._fetch_json(url,params,remaining)
        finally:
            _NETWORK_GATE.release()

    def _load(self,raw,identity,now,deadline):
        asset,key=str(raw.get("asset")),identity["key"]
        if key.startswith("PROFINANCE:"):
            attached,supplied=_supplied_profinance_daily(raw)
            # A shared source bundle already exhausted this cycle\'s budget.
            # Its explicit empty D1 is evidence of unavailability, not a reason
            # to start another provider request for the same asset and cycle.
            if attached:
                return deepcopy(supplied)
            if supplied and any(DA._native(b) for b in supplied):
                return deepcopy(supplied)
            fetch=self._profinance_fetch
            if fetch is None:
                from veritas_profinance_history import fetch_history_bundle
                fetch=fetch_history_bundle
            bundle=fetch(asset=asset,timeframes=("1d",),now=now,
                         budget_seconds=max(0,deadline-self._monotonic()))
            if not VPS.same(identity,bundle.get("source_identity")):
                raise ValueError("PROFINANCE_DAILY_SOURCE_MISMATCH")
            return deepcopy((bundle.get("bars_by_timeframe") or {}).get("1d") or [])
        if key in ("BINANCE:BTCUSDT","BINANCE:ETHUSDT"):
            symbol=key.split(":",1)[1]
            if identity.get("contract_id") not in (None,symbol):
                raise ValueError("BINANCE_DAILY_CONTRACT_MISMATCH")
            payload=self._get("https://api.binance.com/api/v3/klines",
                              {"symbol":symbol,"interval":"1d","limit":MAX_BARS,
                               "endTime":int(now*1000)-1},deadline)
            return parse_binance(payload,identity,now)
        if key in ("MOEX:MOEX","MOEX:CNYRUBF"):
            secid="IMOEX" if asset=="MOEX" else "CNYRUBF"
            if identity.get("contract_id") not in (None,secid):
                raise ValueError("MOEX_DAILY_CONTRACT_MISMATCH")
            market="stock/markets/index" if asset=="MOEX" else "futures/markets/forts/boards/RFUD"
            url=f"https://iss.moex.com/iss/engines/{market}/securities/{secid}/candles.json"
            local=datetime.fromtimestamp(now,MOSCOW)
            params={"from":(local-timedelta(days=500)).date().isoformat(),
                    "till":local.date().isoformat(),"interval":24,"iss.meta":"off"}
            bars,start=[],0
            for _ in range(6):
                payload=self._get(url,{**params,"start":start},deadline)
                rows=(payload.get("candles") or {}).get("data") or []
                bars.extend(parse_moex(payload,identity,now))
                if len(rows)<100:
                    return bars[-MAX_BARS:]
                start+=len(rows)
            raise ValueError("MOEX_DAILY_PAGINATION_INCOMPLETE")
        if key=="TBANK_GRPC:CNYRUBF":
            snapshot=self._tbank_snapshot
            if snapshot is None:
                from veritas_tbank import connection
                snapshot=connection.candle_snapshot
            return parse_tbank(snapshot(asset,"1d"),raw,identity,now)
        raise ValueError("NATIVE_DAILY_SOURCE_UNAVAILABLE")

    def fetch(self,raw,now=None,budget_seconds=4.):
        asof=self._clock() if now is None else timestamp(now)
        identity=_identity(raw)
        result={"version":VERSION,"asset":(raw or {}).get("asset"),"timeframe":"1d",
                "source_identity":deepcopy(identity),"bars":[],"status":"UNAVAILABLE",
                "as_of":asof,"fetched_at":None,"cache_reused":False,"reason":None}
        if asof is None or identity is None:
            result["reason"]="INVALID_TIME_OR_SOURCE"
            return result
        if identity["key"].startswith("BINANCE:") and (
                "SPOT" not in str(identity.get("primary_source","")).upper()
                or "FUTURE" in str(identity.get("primary_source","")).upper()):
            # Legacy key alone does not distinguish spot and futures.
            result["reason"]="BINANCE_SPOT_SOURCE_REQUIRED"
            return result
        key=(identity["key"],identity.get("contract_id"))
        at=self._clock()
        with self._lock:
            cached=deepcopy(self._cache.get(key))
            attempt=self._attempts.get(key)
            error=self._errors.get(key)
            attached,supplied=_supplied_profinance_daily(raw)
            supplied_update=(identity["key"].startswith("PROFINANCE:") and cached is not None
                             and attached and supplied != cached["bars"])
            # Fresh native bundles were already fetched under the shared budget.
            # Their revision proof must not be hidden by this secondary cache.
            due=bool(supplied_update or not cached or not 0<=at-cached["fetched_at"]<TTL_SECONDS)
            if cached and cached["bars"]:
                valid,_=DA.validated_bars(cached["bars"],asof,source_identity=identity)
                if valid:
                    phase=valid[-1]["end_ts"]%DA.DAY
                    boundary=math.floor((asof-phase)/DA.DAY)*DA.DAY+phase
                    due=due or valid[-1]["end_ts"]<boundary
                else:
                    due=True
            can_fetch=(due and key not in self._running
                       and (supplied_update or attempt is None or at-attempt>=RETRY_SECONDS or at<attempt))
            if can_fetch:
                self._running.add(key)
                self._attempts[key]=at
                if len(self._attempts)>14:
                    old=min((k for k in self._attempts if k not in self._running),
                            key=lambda k:self._attempts[k],default=None)
                    if old is not None:
                        self._attempts.pop(old,None)
                        self._errors.pop(old,None)
        if can_fetch:
            try:
                budget=_number(budget_seconds)
                if budget is None or budget<=0:
                    raise TimeoutError("NATIVE_DAILY_BUDGET_EXHAUSTED")
                bars=self._load(raw,identity,asof,self._monotonic()+min(8.,budget))
                profinance=identity["key"].startswith("PROFINANCE:")
                if not profinance:
                    bars=[b for b in bars if timestamp(b.get("end_ts")) is None
                          or timestamp(b.get("end_ts"))<=asof]
                # A real completion proof can be later than a historical replay
                # clock. Keep it (and any revision watermark) in the transport;
                # DA controls when that observation can contribute to an SMA.
                valid,diagnostics=DA.validated_bars(bars,asof,source_identity=identity)
                certified_future=profinance and any(
                    isinstance(b,dict) and VPS.same(identity,b.get("source_identity"))
                    and DA._native(b) for b in bars)
                if not valid and not certified_future:
                    raise ValueError("NATIVE_DAILY_NO_VALID_CLOSED_BARS")
                cached={"bars":deepcopy(bars[-MAX_BARS:]),"fetched_at":self._clock(),
                        "diagnostics":diagnostics}
                with self._lock:
                    if key not in self._cache and len(self._cache)>=7:
                        oldest=min(self._cache,key=lambda k:self._cache[k]["fetched_at"])
                        self._cache.pop(oldest,None)
                        self._attempts.pop(oldest,None)
                        self._errors.pop(oldest,None)
                    self._cache[key]=deepcopy(cached)
                    self._errors.pop(key,None)
                error=None
            except Exception as exc:
                # No response body, URL parameters, tokens or broker details.
                error=(str(exc) if isinstance(exc,(ValueError,TimeoutError))
                       and str(exc).replace("_","").isalnum() else type(exc).__name__)
                with self._lock:
                    self._errors[key]=error
            finally:
                with self._lock:
                    self._running.discard(key)
        if cached:
            result.update(bars=deepcopy(cached["bars"]),fetched_at=cached["fetched_at"],
                          cache_reused=not can_fetch or error is not None)
            context=DA.build_context(result["bars"],asof,asset=result["asset"],source_identity=identity)
            result.update(status="READY" if context["status"]=="OK" else context["status"],
                          last_closed_at=context.get("provenance",{}).get("last_closed_at"),
                          daily_asof=context.get("daily_asof"),
                          daily_asof_basis=context.get("daily_asof_basis"),
                          known_at=context.get("known_at"),diagnostics=context.get("diagnostics"))
        result["reason"]=error or ("FETCH_IN_PROGRESS_OR_COOLDOWN" if due and not can_fetch else None)
        return result


_HISTORY=DailyHistoryCache()


def fetch_native_daily(raw,now=None,budget_seconds=4.):
    return _HISTORY.fetch(raw,now,budget_seconds)
