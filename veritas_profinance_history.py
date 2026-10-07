"""Bounded, same-source ProFinance OHLC for normalized paper decisions.

The public chart's ``/refresh`` and ``/history`` protocol is documented by its
own fastChart JavaScript. History prices are Last (ba=2), not bid/ask or proxy
prices. A chart candle never becomes an executable quote. Intraday labels use
Moscow candle-opening times. Date-only daily labels do not certify a session
boundary: a later observed native date is required to prove daily completion.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
import math
import re
import threading
import time
from zoneinfo import ZoneInfo

import httpx

import veritas_price_source as VPS
from veritas_profinance import SYMBOLS


VERSION = "CTC_PROFINANCE_NATIVE_OHLC_V2"
BASE = "https://charts.profinance.ru/html/charts/"
PROTOCOL_SOURCE = "https://www.profinance.ru/charts/20230926/js/fastChart.min.js?v1"
MOSCOW = ZoneInfo("Europe/Moscow")
# Exact canonical chart tickers returned by /refresh for the quote labels.
CHART_SYMBOLS = {"NQ": "NASD100_FUT", "GOLD": "gold", "BRENT": "brent"}
# name: (native tt, seconds, maximum cache lifetime between bar boundaries)
TIMEFRAMES = {
    "1m": (1, 60, 15), "5m": (3, 300, 60),
    "1h": (6, 3600, 240), "4h": (8, 14400, 300),
    "1d": (9, 86400, 900),
}
# Fast entry contexts have the shortest useful lifetime. A slow senior request
# must not consume the shared budget before either fast timeframe is attempted.
DEFAULT_TIMEFRAMES = ("1m", "5m", "1h", "4h", "1d")
DAILY_COMPLETION_KIND = "NEXT_NATIVE_DAILY_OBSERVED"
MAX_BARS = 500
MAX_RESPONSE_BYTES = 256 * 1024
SESSION_SECONDS = 20 * 60
RETRY_SECONDS = 5
_NETWORK_GATE = threading.BoundedSemaphore(2)


def _epoch(value):
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("now must include a timezone")
        value = value.timestamp()
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("now must be finite")
    return value


def _identity(asset):
    if asset=='BRENT':
        return VPS.brent_feed_pin_identity()
    return VPS.identity(asset, {"source": "ProFinance", "raw_label": SYMBOLS[asset]})


def _boundary(now, timeframe):
    """Intraday boundary, or nominal daily label index (not closure proof)."""
    if timeframe == "1d":
        local = datetime.fromtimestamp(now, MOSCOW)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    seconds = TIMEFRAMES[timeframe][1]
    return int(now) // seconds * seconds


def parse_refresh(text, asset):
    """Accept only the requested instrument and the observed Last/TF mapping."""
    sid = ticker = None
    labels, tickers = set(), set()
    last = False
    periods = {}
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 2:
            continue
        if fields[0] == "1" and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", fields[1]):
            sid = fields[1]
        elif fields[0] == "t":
            ticker = fields[1]
            tickers.add(ticker)
        elif fields[0] == "s":
            labels.add(fields[1])
        elif len(fields) >= 3 and fields[:3] == ["n", "Last", "2"]:
            last = True
        elif len(fields) >= 4 and fields[0] == "6" and fields[3] == "bar":
            periods[fields[1]] = fields[2]
    expected = {"1Min": "1", "5Min": "3", "1Hour": "6", "4Hour": "8", "1Day": "9"}
    if ticker != CHART_SYMBOLS[asset]:
        raise ValueError("PROFINANCE_HISTORY_INSTRUMENT_MISMATCH")
    if asset=='BRENT' and (labels!={SYMBOLS[asset]} or tickers!={CHART_SYMBOLS[asset]}):
        raise ValueError("PROFINANCE_HISTORY_INSTRUMENT_MISMATCH")
    if not sid or not last or any(periods.get(k) != v for k, v in expected.items()):
        raise ValueError("PROFINANCE_HISTORY_PROTOCOL_MISMATCH")
    result={"sid": sid, "ticker": ticker, "price_type": "Last", "ba": 2}
    if asset=='BRENT':
        result.update(raw_label=SYMBOLS[asset],raw_ticker=ticker,
                      provider_chart_identity_verified=True,
                      source_pin_version=VPS.BRENT_FEED_PIN_VERSION)
    return result


def _daily_ohlc_sha256(row):
    values = [float(row[key]) for key in ("open", "high", "low", "close")]
    return hashlib.sha256(json.dumps(values, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _certify_daily_rows(rows, observation, asof, identity):
    """A successor proves a prior date complete only when actually observed.

    The date indices retain their existing representation for ordering. Neither
    index is a verified opening/closing time, and no daily trigger may use them
    as an interval boundary. The latest native date remains unconfirmed.
    """
    if observation is None or not 0 < observation <= asof:
        return []
    observed = [rows[t] for t in sorted(rows) if t <= observation]
    complete = []
    for current, successor in zip(observed, observed[1:]):
        if current["provider_period_label"] >= successor["provider_period_label"]:
            continue
        row = dict(current)
        row.update(finalized=True, available_at=max(row["end_ts"], observation),
                   completion_proof={
                       "kind": DAILY_COMPLETION_KIND,
                       "period_label": row["provider_period_label"],
                       "successor_label": successor["provider_period_label"],
                       "observed_at": observation,
                       "source_identity": {"key": identity["key"],
                                           "contract_id": identity.get("contract_id")},
                       "ohlc_sha256": _daily_ohlc_sha256(row)})
        complete.append(row)
    return complete


def _retain_daily_proofs(parsed, cached):
    """Keep first unchanged evidence; a revised closed OHLC is newly known."""
    prior = {(bar.get("provider_period_label"), (bar.get("source_identity") or {}).get("key"),
              (bar.get("source_identity") or {}).get("contract_id")): bar
             for bar in (cached or {}).get("bars", ())
             if (bar.get("completion_proof") or {}).get("kind") == DAILY_COMPLETION_KIND}
    if prior and parsed["bars"]:
        current = {row["provider_period_label"] for row in parsed["bars"]}
        oldest, newest = min(current), max(current)
        known = {key[0] for key in prior}
        if newest < max(known):
            raise ValueError("PROFINANCE_DAILY_PERIOD_REGRESSION")
        if any(period >= oldest and period not in current for period in known):
            raise ValueError("PROFINANCE_DAILY_KNOWN_PERIOD_MISSING")
    for row in parsed["bars"]:
        identity = row["source_identity"]
        old = prior.get((row["provider_period_label"], identity["key"], identity.get("contract_id")))
        if not old:
            continue
        proof = row["completion_proof"]
        old_proof = old["completion_proof"]
        if (old_proof.get("ohlc_sha256") == proof["ohlc_sha256"]
                and _daily_ohlc_sha256(old) == proof["ohlc_sha256"]):
            row["completion_proof"] = copy.deepcopy(old_proof)
            row["available_at"] = max(row["end_ts"], old_proof["observed_at"])
            if old.get("revision_observed_at") is not None:
                row["revision_observed_at"] = old["revision_observed_at"]
        else:
            # Prior values are not retained. Consumers must explicitly refuse
            # snapshots before this watermark, not backfill an older SMA day.
            row["revision_observed_at"] = proof["observed_at"]
    return parsed


def parse_history(text, asset, timeframe, now=None, limit=MAX_BARS, observed_at=None):
    """Parse native OHLC; reject malformed rows, future/partial bars and dupes.

    Returns a data/diagnostics object. Missing volume remains None. Intraday
    bars align in UTC, including 4h at 00/04/08/12/16/20 UTC. A daily date is
    an ordering label, not a documented daily close time. Only an actual
    observation of a later valid native date certifies a completed D1 OHLC.
    """
    if asset not in CHART_SYMBOLS or timeframe not in TIMEFRAMES:
        raise ValueError("Unsupported ProFinance asset or native timeframe")
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("PROFINANCE_HISTORY_RESPONSE_TOO_LARGE")
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].split(";")[1:5] != ["Open", "High", "Low", "Close"]:
        raise ValueError("PROFINANCE_HISTORY_INVALID_HEADER")
    asof = _epoch(now)
    observation = (_epoch(observed_at) if observed_at is not None
                   and not isinstance(observed_at, bool) else None)
    seconds = TIMEFRAMES[timeframe][1]
    identity = _identity(asset)
    rows, forming, conflicts = {}, {}, set()
    rejected = partial = duplicates = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(";")
        try:
            if len(parts) != 6:
                raise ValueError("wrong column count")
            values = [float(value[:-1] if value.endswith("b") else value) for value in parts[1:5]]
            op, hi, lo, cl = values
            if (any(not math.isfinite(v) or v <= 0 for v in values)
                    or lo > min(op, cl) or hi < max(op, cl) or lo > hi):
                raise ValueError("invalid OHLC")
            fmt = "%d.%m.%Y" if timeframe == "1d" else (
                "%d.%m.%Y %H:%M" if timeframe in ("1m", "5m") else "%d.%m.%Y %H")
            local = datetime.strptime(parts[5], fmt).replace(tzinfo=MOSCOW)
            stamp = local.timestamp()
            if timeframe != "1d" and stamp % seconds:
                raise ValueError("misaligned native candle")
            end = (local + timedelta(days=1)).timestamp() if timeframe == "1d" else stamp + seconds
            unfinished = timeframe != "1d" and end > asof
            if unfinished:
                partial += 1
                # Only a real fetch can certify partial OHLC observation. A
                # historical caller's replay clock never manufactures it.
                if observation is None or not stamp <= observation <= asof < end:
                    continue
            row = {"ts": stamp, "available_at": end, "end_ts": end,
                   "open": op, "high": hi, "low": lo, "close": cl,
                   "volume": None, "volume_available": False,
                   "source": "ProFinance " + SYMBOLS[asset],
                   "source_key": identity["key"], "source_identity": dict(identity),
                   "raw_label": SYMBOLS[asset], "chart_symbol": CHART_SYMBOLS[asset],
                   "timeframe": timeframe, "price_type": "Last",
                   "finalized": not unfinished and timeframe != "1d"}
            if timeframe == "1d":
                row.update(provider_period_label=local.date().isoformat(),
                           native_timeframe="1d", native_interval=9,
                           native_time_basis="PROVIDER_DATE_LABEL_ONLY",
                           interval_boundary_verified=False)
            target_rows = forming if unfinished else rows
            if unfinished:
                row["observed_at"] = observation
            if stamp in target_rows:
                duplicates += 1
                if any(target_rows[stamp][k] != row[k] for k in ("open", "high", "low", "close")):
                    conflicts.add(stamp)
            else:
                target_rows[stamp] = row
        except (ValueError, TypeError, OverflowError):
            rejected += 1
    for stamp in conflicts:
        rows.pop(stamp, None)
        forming.pop(stamp, None)
    # A tainted daily response cannot silently replace a missing day in an
    # averaging window with an older retained observation.
    valid_rows = ((_certify_daily_rows(rows, observation, asof, identity)
                   if not rejected and not conflicts else [])
                  if timeframe == "1d" else [rows[t] for t in sorted(rows)])
    if timeframe == "1d":
        partial = len(rows) - len(valid_rows)
    selected = valid_rows[-max(1, min(MAX_BARS, int(limit))):]
    return {"bars": selected, "forming_bars": [forming[t] for t in sorted(forming)][-1:],
            "rejected_rows": rejected, "excluded_partial_rows": partial,
            "duplicate_rows": duplicates, "conflicting_timestamps": len(conflicts),
            "volume_available": False}


def _http_text(url, params, remaining):
    deadline = time.monotonic() + remaining
    timeout = httpx.Timeout(max(.05, remaining), connect=min(1.5, max(.05, remaining)))
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 VERITAS public chart history"},
                      limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > MAX_RESPONSE_BYTES:
                raise ValueError("PROFINANCE_HISTORY_RESPONSE_TOO_LARGE")
            chunks, count = [], 0
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise TimeoutError("PROFINANCE_HISTORY_BUDGET_EXHAUSTED")
                count += len(chunk)
                if count > MAX_RESPONSE_BYTES:
                    raise ValueError("PROFINANCE_HISTORY_RESPONSE_TOO_LARGE")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="strict")


class HistoryCache:
    """Three assets × five native TFs; at most 500 candles per cache entry.

    Calls for the same asset are single-flight. Native periods load sequentially
    in priority order, sharing a total time budget, and retry missing periods in
    later cycles. A process-wide semaphore caps simultaneous HTTP calls at two;
    this module starts no threads, workers, timers or background loops.
    """
    def __init__(self, fetch_text=None, clock=time.time, monotonic=time.monotonic):
        self._fetch_text = fetch_text or _http_text
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._gates = {asset: threading.Lock() for asset in CHART_SYMBOLS}
        self._cache, self._sessions, self._attempts, self._errors = {}, {}, {}, {}
        self._access_backoff = {}

    def _get(self, path, params, deadline):
        remaining = deadline - self._monotonic()
        if remaining <= 0 or not _NETWORK_GATE.acquire(timeout=max(0., remaining)):
            raise TimeoutError("PROFINANCE_HISTORY_BUDGET_EXHAUSTED")
        try:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("PROFINANCE_HISTORY_BUDGET_EXHAUSTED")
            return self._fetch_text(BASE + path, params, remaining)
        finally:
            _NETWORK_GATE.release()

    def _session(self, asset, deadline):
        with self._lock:
            cached = self._sessions.get(asset)
            if cached and 0 <= self._clock() - cached["at"] < SESSION_SECONDS:
                return dict(cached)
        result = parse_refresh(self._get("refresh", {"s": SYMBOLS[asset]}, deadline), asset)
        result["at"] = self._clock()
        with self._lock:
            self._sessions[asset] = dict(result)
        return result

    def fetch_bundle(self, asset="NQ", timeframes=DEFAULT_TIMEFRAMES, now=None, budget_seconds=8.):
        asset = str(asset).upper()
        if asset not in CHART_SYMBOLS or any(tf not in TIMEFRAMES for tf in timeframes):
            raise ValueError("Unsupported ProFinance asset or native timeframe")
        requested = set(timeframes)
        ordered = [tf for tf in DEFAULT_TIMEFRAMES if tf in requested]
        deadline = self._monotonic() + max(0., min(15., float(budget_seconds)))
        refreshed, skipped, stale_session = set(), {}, False
        acquired = self._gates[asset].acquire(blocking=False)
        try:
            if acquired:
                session = None
                for tf in ordered:
                    key = (asset, tf)
                    current = self._clock()
                    with self._lock:
                        cached = self._cache.get(key)
                        attempt = self._attempts.get(key)
                        access_backoff = self._access_backoff.get(asset, 0.)
                    if current < access_backoff:
                        skipped[tf] = "ACCESS_DENIED_BACKOFF"
                        continue
                    if (cached and 0 <= current - cached["fetched_at"] < TIMEFRAMES[tf][2]
                            and (tf == "1d" or cached["bars"][-1]["end_ts"] >= _boundary(current, tf))):
                        # D1 has no verified clock boundary. Its bounded TTL
                        # refreshes successor evidence without inventing one.
                        continue
                    if attempt is not None and 0 <= current - attempt < RETRY_SECONDS:
                        skipped[tf] = "RETRY_COOLDOWN"
                        continue
                    if self._monotonic() >= deadline:
                        skipped[tf] = "BUDGET_EXHAUSTED"
                        continue
                    with self._lock:
                        self._attempts[key] = current
                    try:
                        session = session or self._session(asset, deadline)
                        native = TIMEFRAMES[tf][0]
                        text = self._get("history", {"SID": session["sid"], "s": session["ticker"],
                            "h": 700, "w": 1500, "pt": 2, "tt": native,
                            "z": 1, "ba": 2, "left": 0, "T": int(self._clock() * 1000)}, deadline)
                        received_at = self._clock()
                        parsed = parse_history(text, asset, tf, now=received_at, observed_at=received_at)
                        if asset=='BRENT':
                            # The chart proves its label/ticker mapping. Its
                            # endpoint does not expose the quote-table I field;
                            # that separate observed proof belongs to /q.
                            chart_proof={"raw_ticker":session["ticker"],
                                "provider_chart_identity_verified":session.get("provider_chart_identity_verified") is True,
                                "source_pin_version":session.get("source_pin_version")}
                            parsed.update(chart_proof)
                            for row in parsed["bars"]+parsed["forming_bars"]:
                                row.update(chart_proof)
                        if tf == "1d":
                            parsed = _retain_daily_proofs(parsed, cached)
                        if not parsed["bars"]:
                            raise ValueError("PROFINANCE_HISTORY_NO_CLOSED_BARS")
                        if (tf != "1d" and parsed["bars"][-1]["end_ts"] < _boundary(received_at, tf)):
                            # A successful HTTP response may still contain an
                            # old chart window. Retire its session for the next
                            # bounded poll, without redating candles or retrying
                            # around access-denial/rate-limit backoff.
                            stale_session = True
                            skipped[tf] = "STALE_HISTORY_SESSION_RETIRED"
                        parsed["fetched_at"] = self._clock()
                        with self._lock:
                            self._cache[key] = parsed
                            self._errors.pop(key, None)
                        refreshed.add(tf)
                    except Exception as exc:
                        # Keep the last same-source data with its original
                        # fetched_at; a failure never renews a stale cache.
                        with self._lock:
                            self._errors[key] = type(exc).__name__ + ": " + str(exc)[:180]
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403, 429):
                            with self._lock:
                                self._access_backoff[asset] = self._clock() + 300.
                                for missing in ordered:
                                    self._errors[(asset, missing)] = "PROFINANCE_HISTORY_ACCESS_DENIED_OR_RATE_LIMITED"
                            skipped.update({missing: "ACCESS_DENIED_BACKOFF" for missing in ordered})
                            break
                        if session is None or "INVALID_HEADER" in str(exc):
                            with self._lock:
                                self._sessions.pop(asset, None)
                            break
            else:
                skipped = {tf: "FETCH_IN_PROGRESS" for tf in ordered}
        finally:
            if acquired:
                if stale_session:
                    with self._lock:
                        self._sessions.pop(asset, None)
                self._gates[asset].release()
        asof = self._clock() if now is None else _epoch(now)
        identity = _identity(asset)
        bars_by_tf, forming_by_tf, status_by_tf = {}, {}, {}
        with self._lock:
            snapshot = {tf: copy.deepcopy(self._cache.get((asset, tf))) for tf in ordered}
            errors = {tf: self._errors.get((asset, tf)) for tf in ordered}
        for tf in ordered:
            cached = snapshot[tf]
            bars = [bar for bar in (cached or {}).get("bars", ()) if bar["end_ts"] <= asof]
            bars_by_tf[tf] = bars
            forming_by_tf[tf] = [bar for bar in (cached or {}).get("forming_bars", ())
                                 if bar["ts"] <= bar["observed_at"] <= asof < bar["end_ts"]]
            last_end = bars[-1]["end_ts"] if bars else None
            age = asof - last_end if last_end is not None else None
            expected = None if tf == "1d" else _boundary(asof, tf)
            fresh = bool(last_end is not None and -5 <= age <= TIMEFRAMES[tf][1] + 30)
            status = "READY" if fresh else "STALE" if bars else "UNAVAILABLE"
            status_by_tf[tf] = {"status": status, "fresh": fresh, "bars": len(bars),
                "last_closed_at": last_end, "age_seconds": age,
                "expected_last_closed_at": expected,
                "latest_completed_period_present": (bool(last_end is not None and last_end >= expected)
                                                    if expected is not None else None),
                "interval_boundary_verified": tf != "1d",
                "native_time_basis": "PROVIDER_DATE_LABEL_ONLY" if tf == "1d" else "NATIVE_INTRADAY",
                "fetched_at": cached["fetched_at"] if cached else None,
                "cache_age_seconds": max(0., self._clock() - cached["fetched_at"]) if cached else None,
                "cache_reused": tf not in refreshed, "fetch_error": errors[tf],
                "reason": skipped.get(tf) or ("OK" if fresh else "OLD_CANDLE_END" if bars else "NO_HISTORY"),
                "rejected_rows": (cached or {}).get("rejected_rows", 0),
                "conflicting_timestamps": (cached or {}).get("conflicting_timestamps", 0),
                "volume_available": False}
        result={"version": VERSION, "asset": asset, "source": "ProFinance",
            "source_key": identity["key"], "source_identity": identity,
            "raw_label": SYMBOLS[asset], "chart_symbol": CHART_SYMBOLS[asset],
            "price_type": "Last", "same_source": True, "paper_only": True,
            "volume_available": False, "as_of": asof, "retrieved_at": self._clock(),
            "bars_by_timeframe": bars_by_tf, "status_by_timeframe": status_by_tf,
            "forming_bars_by_timeframe": forming_by_tf,
            "source_url": BASE + "history", "protocol_source": PROTOCOL_SOURCE,
            "time_basis": "Europe/Moscow intraday labels; D1 provider date labels require observed successor proof"}
        if asset=='BRENT':
            available=[entry for entry in snapshot.values() if entry and entry.get('bars')]
            chart_verified=bool(available) and all(
                entry.get('provider_chart_identity_verified') is True
                and entry.get('raw_ticker')==CHART_SYMBOLS[asset]
                and entry.get('source_pin_version')==VPS.BRENT_FEED_PIN_VERSION
                for entry in available)
            result.update(raw_ticker=CHART_SYMBOLS[asset] if chart_verified else None,
                          provider_chart_identity_verified=chart_verified,
                          source_pin_version=VPS.BRENT_FEED_PIN_VERSION)
        return result


_HISTORY = HistoryCache()


def fetch_history_bundle(asset="NQ", timeframes=DEFAULT_TIMEFRAMES, now=None, budget_seconds=8.):
    return _HISTORY.fetch_bundle(asset, timeframes, now, budget_seconds)
