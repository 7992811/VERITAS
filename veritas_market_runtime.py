from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from veritas_quote_time import moex_observed_at

VERSION = "veritas-market-runtime-guard-v2"


def install_market_runtime_guard(ns):
    if ns.get("_V90_MARKET_RUNTIME_GUARD_INSTALLED"):
        return
    ns["_V90_MARKET_RUNTIME_GUARD_INSTALLED"] = True

    import veritas_brent_market as BRM
    ns["_v90_brent_market"] = lambda: BRM.fetch_market(quote_fetcher=ns["_v90r61_profinance_quote"])

    emit = ns.get("emit")
    market_cache = ns.get("market_cache", {})
    market_cache_lock = ns.get("market_cache_lock")
    moex_block = ns["_moex_block"]
    moex_parse_dt = ns["_moex_parse_dt"]
    source_row = ns.get("_source_row")
    set_source_quality = ns.get("_set_source_quality")

    def _emit(event, **kw):
        if callable(emit):
            try:
                emit(event, **kw)
            except Exception:
                pass

    def yahoo_series(symbol, range_="30d", interval="1h", prepost=False):
        ttl = 45 if interval in ("1m", "2m", "5m") else 240 if interval in ("15m", "30m", "60m", "1h") else 1800
        key = (symbol, range_, interval, bool(prepost))
        now_ts = time.time()
        if market_cache_lock is not None:
            with market_cache_lock:
                z = market_cache.get(key)
                if z and now_ts - z["cached_at"] <= ttl:
                    return z["rows"], z["meta"]

        deadline = time.monotonic() + 10.0
        err = None
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                timeout = httpx.Timeout(min(6.0, max(1.0, remaining)), connect=min(3.0, max(1.0, remaining)))
                with httpx.Client(timeout=timeout, headers={"User-Agent": "Mozilla/5.0 VERITAS"}) as h:
                    r = h.get(
                        f"https://{host}/v8/finance/chart/{symbol}",
                        params={
                            "range": range_,
                            "interval": interval,
                            "includePrePost": "true" if prepost else "false",
                            "events": "div,splits",
                        },
                    )
                    r.raise_for_status()
                    j = r.json()
                result = ((j.get("chart") or {}).get("result") or [None])[0]
                if not result:
                    raise RuntimeError("YAHOO_EMPTY_RESULT")
                q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
                ts = result.get("timestamp") or []
                rows = []
                close = q.get("close") or [None] * len(ts)
                opens = q.get("open") or close
                highs = q.get("high") or close
                lows = q.get("low") or close
                vols = q.get("volume") or [0] * len(ts)
                for i, t in enumerate(ts):
                    c = close[i] if i < len(close) else None
                    if c is None:
                        continue
                    o = opens[i] if i < len(opens) else c
                    hi = highs[i] if i < len(highs) else c
                    lo = lows[i] if i < len(lows) else c
                    vol = vols[i] if i < len(vols) else 0
                    rows.append(
                        {
                            "ts": int(t),
                            "open": float(c if o is None else o),
                            "high": float(c if hi is None else hi),
                            "low": float(c if lo is None else lo),
                            "close": float(c),
                            "volume": float(vol or 0),
                        }
                    )
                if rows:
                    meta = result.get("meta") or {}
                    if market_cache_lock is not None:
                        with market_cache_lock:
                            market_cache[key] = {"cached_at": now_ts, "rows": rows, "meta": meta}
                    return rows, meta
            except Exception as ex:
                err = ex
        raise RuntimeError(f"YAHOO_SERIES_BUDGET_FAIL {symbol}: {err}")

    ns["_yahoo_series"] = yahoo_series

    def futures_candles(secid, start_ts, end_ts, interval=60):
        interval = int(interval or 60)
        now_ts = time.time()
        anchor = min(float(end_ts), now_ts + 3600.0)
        lookback = 30 * 3600.0 if interval <= 1 else 5 * 86400.0 if interval <= 5 else 60 * 86400.0
        start_ts = max(float(start_ts), anchor - lookback)
        end_ts = min(float(end_ts), now_ts + 3600.0)
        frm = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        till = datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        base = f"https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{secid}/candles.json"
        out = []
        start = 0
        deadline = time.monotonic() + 9.0
        page_limit = 12
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), headers={"User-Agent": "VERITAS/9.0 live"}) as h:
                for _ in range(page_limit):
                    if time.monotonic() >= deadline:
                        _emit("market_history_budget_exhausted", asset=secid, interval=interval, rows=len(out))
                        break
                    r = h.get(
                        base,
                        params={
                            "from": frm,
                            "till": till,
                            "interval": interval,
                            "start": start,
                            "iss.meta": "off",
                            "iss.only": "candles",
                        },
                    )
                    r.raise_for_status()
                    rows = moex_block(r.json(), "candles")
                    if not rows:
                        break
                    for x in rows:
                        dt = moex_parse_dt(x.get("begin") or x.get("BEGIN"))
                        de = moex_parse_dt(x.get("end") or x.get("END"))
                        if not dt:
                            continue
                        cl = float(x.get("close") or x.get("CLOSE") or 0)
                        if cl <= 0:
                            continue
                        op = float(x.get("open") or x.get("OPEN") or cl)
                        hi = float(x.get("high") or x.get("HIGH") or cl)
                        lo = float(x.get("low") or x.get("LOW") or cl)
                        vol = float(x.get("volume") or x.get("VOLUME") or x.get("value") or x.get("VALUE") or 0)
                        out.append(
                            [
                                int(dt.timestamp() * 1000),
                                str(op),
                                str(hi),
                                str(lo),
                                str(cl),
                                str(vol),
                                int((de or (dt + timedelta(minutes=max(1, interval)))).timestamp() * 1000) - 1,
                                "0",
                                "0",
                                str(vol * 0.5),
                                "0",
                                "0",
                            ]
                        )
                    if len(rows) < 100:
                        break
                    start += len(rows)
        except Exception as ex:
            _emit("moex_live_history_error", asset=secid, interval=interval, rows=len(out), error=f"{type(ex).__name__}: {ex}")
        ded = {int(x[0]): x for x in out}
        return [ded[k] for k in sorted(ded)]

    ns["_moex_futures_candles_between"] = futures_candles

    def current_quote(secid):
        url = f"https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{secid}.json"
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), headers={"User-Agent": "VERITAS/9.0 live"}) as h:
            r = h.get(url, params={"iss.meta": "off", "iss.only": "marketdata"})
            r.raise_for_status()
            rows = moex_block(r.json(), "marketdata")
        if not rows:
            raise RuntimeError(f"MOEX_FORTS_NO_MARKETDATA {secid}")
        row = rows[0]
        if str(secid).startswith("BR") and (len(rows) != 1 or str(row.get("SECID") or "") != str(secid)):
            # A Brent position owns one exact expiry. Do not relabel a missing
            # or foreign marketdata identity as the requested oil contract.
            raise RuntimeError(f"MOEX_FORTS_CONTRACT_MISMATCH {secid}")
        price = None
        price_field = None
        for key in ("LAST", "MARKETPRICE", "SETTLEPRICE"):
            if row.get(key) not in (None, ""):
                try:
                    price = float(row[key])
                    price_field = key
                    break
                except Exception:
                    pass
        if price is None:
            raise RuntimeError(f"MOEX_FORTS_NO_PRICE {secid}")
        return {"price": price, "price_field": price_field,
                "observed_at": moex_observed_at(row), "row": row}

    ns["_moex_futures_current_quote"] = current_quote

    add_months = ns.get("_v90_add_months")
    age_seconds = ns.get("_age_seconds")

    def front_brent():
        msk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow"))
        month_codes = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}
        secids = []
        for delta in range(4):
            yy, mm = add_months(msk.year, msk.month, delta)
            secids.append((delta, f"BR{month_codes[mm]}{str(yy)[-1:]}"))
        candidates = []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="veritas-brent-front") as pool:
            futures = {pool.submit(current_quote, secid): (delta, secid) for delta, secid in secids}
            done, pending = wait(futures, timeout=6.0)
            for fut in pending:
                fut.cancel()
            for fut in done:
                delta, secid = futures[fut]
                try:
                    q = fut.result()
                    age = age_seconds(q.get("observed_at"))
                    if age is None or age > 86400:
                        continue
                    row = q.get("row") or {}
                    activity = 0.0
                    for key, weight in (("VALTODAY",1.0),("VOLTODAY",1000.0),("NUMTRADES",10000.0),("OPENPOSITION",100.0)):
                        try:
                            activity += max(0.0, float(row.get(key) or 0.0)) * weight
                        except Exception:
                            pass
                    candidates.append((activity, -delta, secid, q))
                except Exception:
                    pass
        if not candidates:
            raise RuntimeError("MOEX_BRENT_FRONT_CONTRACT_NOT_FOUND")
        candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
        return candidates[0][2], candidates[0][3]

    ns["_v90_moex_front_brent_contract"] = front_brent

    def moex_index_candles(start_ts, end_ts):
        now_ts = time.time()
        anchor = min(float(end_ts), now_ts + 3600.0)
        start_ts = max(float(start_ts), anchor - 60 * 86400.0)
        end_ts = min(float(end_ts), now_ts + 3600.0)
        frm = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        till = datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        urls = [
            "https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX/candles.json",
            "https://iss.moex.com/iss/history/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json",
        ]
        deadline = time.monotonic() + 8.0
        for base in urls:
            if time.monotonic() >= deadline:
                break
            try:
                out = []
                start = 0
                with httpx.Client(timeout=httpx.Timeout(4.0, connect=2.5), headers={"User-Agent":"VERITAS/9.0 live"}) as h:
                    for _ in range(10):
                        if time.monotonic() >= deadline:
                            break
                        r = h.get(base, params={"from":frm,"till":till,"interval":60,"start":start,"iss.meta":"off","iss.only":"candles"})
                        r.raise_for_status()
                        rows = moex_block(r.json(), "candles")
                        if not rows:
                            break
                        for x in rows:
                            dt = moex_parse_dt(x.get("begin") or x.get("BEGIN"))
                            de = moex_parse_dt(x.get("end") or x.get("END"))
                            if not dt:
                                continue
                            cl = float(x.get("close") or x.get("CLOSE") or 0)
                            if cl <= 0:
                                continue
                            op = float(x.get("open") or x.get("OPEN") or cl)
                            hi = float(x.get("high") or x.get("HIGH") or cl)
                            lo = float(x.get("low") or x.get("LOW") or cl)
                            vol = float(x.get("value") or x.get("VALUE") or x.get("volume") or x.get("VOLUME") or 1.0)
                            out.append([int(dt.timestamp()*1000),str(op),str(hi),str(lo),str(cl),str(vol),
                                        int((de or (dt+timedelta(hours=1))).timestamp()*1000)-1,"0","0",str(vol*0.5),"0","0"])
                        if len(rows) < 100:
                            break
                        start += len(rows)
                if out:
                    ded = {int(x[0]):x for x in out}
                    return [ded[k] for k in sorted(ded)]
            except Exception as ex:
                _emit("moex_live_index_history_error", provider=base, error=f"{type(ex).__name__}: {ex}")

        # Canonical structural history must retain the official MOEX source.
        # A Yahoo fallback cannot be labelled as the execution source.
        return []

    ns["_moex_candles_between"] = moex_index_candles

    def moex_outcome_5m(start_ts, end_ts):
        bars = ns["_v90r16_moex_index_5m"]()
        out = []
        for x in bars or []:
            try:
                ts = float(x.get("ts") or 0.0)
                if ts < float(start_ts) - 600 or ts > float(end_ts):
                    continue
                vol = float(x.get("volume") or 0.0)
                out.append([
                    int(ts * 1000), str(x.get("open")), str(x.get("high")),
                    str(x.get("low")), str(x.get("close")), str(vol),
                    int((ts + 300) * 1000) - 1, "0", "0", str(vol * 0.5), "0", "0",
                ])
            except Exception:
                continue
        return out

    ns["_v90_moex_outcome_5m"] = moex_outcome_5m

    original_fetch = ns["_fetch_asset_bundle"]
    assets = ns["ASSETS"]

    def _r63_timeout_bundle(asset):
        cache = ns.get("_v90r62_bundle_cache") or {}
        lock = ns.get("_v90r62_bundle_cache_lock")
        try:
            if lock is not None:
                with lock:
                    z = dict(cache.get(str(asset)) or {})
            else:
                z = dict(cache.get(str(asset)) or {})
            age = time.time() - float(z.get("at") or 0.0)
            bundle = dict(z.get("bundle") or {})
            if age <= 600.0 and bundle.get("raw") is not None and not bundle.get("error"):
                bundle["elapsed_seconds"] = 0.0
                bundle["reused_market_bundle"] = True
                bundle["reused_market_bundle_timeout_fallback"] = True
                bundle["reused_market_bundle_age_seconds"] = age
                return bundle
        except Exception:
            pass
        return None

    def prefetch_market_bundles():
        # Every configured asset must start within the same 25s budget. A pool
        # of six for seven assets left ETH queued behind slow exchange feeds.
        # Keep deterministic submission order, but never spend an asset's
        # freshness budget waiting for another asset to release a worker.
        priority = {"MOEX":0, "CNYRUBF":1, "NQ":2, "GOLD":3, "BRENT":4, "BTC":5, "ETH":6}
        items = [(symbol, asset, cb_product) for symbol, (asset, cb_product) in assets.items()]
        items.sort(key=lambda x: priority.get(str(x[1]), 99))
        out = {}
        t0 = time.time()
        workers = max(1, len(items))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="veritas-market")
        futures = {pool.submit(original_fetch, *item): item for item in items}
        done, pending = wait(futures, timeout=25.0)
        for fut in done:
            symbol, asset, cb_product = futures[fut]
            try:
                out[asset] = fut.result()
            except Exception as ex:
                out[asset] = {"symbol":symbol,"asset":asset,"cb_product":cb_product,"raw":None,"deriv":None,
                              "elapsed_seconds":time.time()-t0,"error":f"{type(ex).__name__}: {ex}"}
        timed_out = []
        cache_fallback = []
        for fut in pending:
            symbol, asset, cb_product = futures[fut]
            fut.cancel()
            fallback = _r63_timeout_bundle(asset)
            if fallback is not None:
                out[asset] = fallback
                cache_fallback.append(asset)
                _emit("market_prefetch_timeout_cache_fallback", asset=asset,
                      age_seconds=fallback.get("reused_market_bundle_age_seconds"))
            else:
                out[asset] = {"symbol":symbol,"asset":asset,"cb_product":cb_product,"raw":None,"deriv":None,
                              "elapsed_seconds":time.time()-t0,"error":"MARKET_PREFETCH_TIMEOUT_25S"}
                timed_out.append(asset)
                _emit("market_prefetch_timeout", asset=asset, budget_seconds=25.0)
        pool.shutdown(wait=False, cancel_futures=True)
        wall = time.time() - t0
        fetch_sum = sum(float(x.get("elapsed_seconds") or 0) for x in out.values())
        return out, {"wall_seconds":wall,"sum_asset_seconds":fetch_sum,
                     "parallel_wait_saved_estimate_seconds":max(0.0,fetch_sum-wall),"workers":workers,
                     "timed_out_assets":timed_out,"timeout_cache_fallback_assets":cache_fallback,
                     "priority_order":[x[1] for x in items]}

    original_trim = ns.get("_v90_trim_memory")
    if callable(original_trim):
        def trim_memory(phase="unknown", force=False, *, preserve_active_cycle=False):
            # The low-memory loop fetches one asset at a time. Once that asset
            # has been compacted, provider rows cannot be reused by the next
            # asset and retaining NQ/BRENT rows has exhausted the 512 MiB
            # process while GOLD was being evaluated. Keep all decision,
            # structural-fact and portfolio caches intact.
            released = 0
            memory_soft_limit = int(ns.get("MEMORY_SOFT_LIMIT_MB") or 400)
            protect_mb = float(ns.get("V90_MEMORY_PROTECT_MB") or 340.0)
            rss = ns.get("rss_mb")
            level = rss() if callable(rss) else None
            if (memory_soft_limit <= 320 and str(phase).startswith("asset_")
                    and level is not None and float(level) >= protect_mb):
                try:
                    if market_cache_lock is not None:
                        with market_cache_lock:
                            released = len(market_cache)
                            market_cache.clear()
                    else:
                        released = len(market_cache)
                        market_cache.clear()
                except Exception:
                    released = 0
                if released:
                    _emit("completed_asset_provider_cache_released",
                          asset=str(phase)[len("asset_"):], entries=released,
                          rss_mb=float(level))
            result = original_trim(phase, force=force,
                                   preserve_active_cycle=preserve_active_cycle)
            if released and isinstance(result, dict):
                result = dict(result)
                result["completed_asset_provider_cache_released"] = released
            return result

        ns["_v90_trim_memory"] = trim_memory

    ns["prefetch_market_bundles"] = prefetch_market_bundles
    ns["MARKET_RUNTIME_GUARD_VERSION"] = VERSION
