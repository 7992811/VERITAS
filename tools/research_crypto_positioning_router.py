"""VERITAS positioning-state research for BTC and ETH.

Adds causal Binance USD-M 5m metrics to the existing spot event framework:
open interest, top-trader position/account ratios, all-account long/short ratio,
and taker buy/sell ratio.

Important timestamp normalization:
- before 2026-06-25, create_time T is end-labelled and observable at T;
- from 2026-06-25, archive row T contains the following 5m interval/snapshot,
  so availability is shifted to T+5m to prevent look-ahead.

Research only; production is unchanged.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from research_crypto_manager_library import load
from research_crypto_asset_specific import make_labels
import research_crypto_derivative_router as base
from research_crypto_state_router import json_default

OUT = Path("positioning_router_out")
CACHE = OUT / "data"
OUT.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
CUT = pd.Timestamp("2026-06-25T00:00:00Z")

PATTERNS = {
    "M0": ["trigger", "d", "align", "oi_state", "top_flow", "ext_state"],
    "M1": ["trigger", "d", "align", "oi_state", "top_crowd", "taker_flow"],
    "M2": ["trigger", "d", "strength", "oi_state", "top_flow", "retail_crowd"],
    "M3": ["trigger", "d", "vol", "top_flow", "top_crowd", "ext_state"],
    "M4": ["trigger", "d", "align", "smart_retail", "taker_flow", "ext_state"],
    "M5": ["trigger", "d", "align", "oi_state", "oi_accel", "top_flow", "top_crowd"],
}
base.PATTERNS = PATTERNS

def get(url):
    last = None
    for k in range(4):
        try:
            req = Request(url, headers={"User-Agent": "VERITAS-positioning-research/1.0"})
            with urlopen(req, timeout=30) as r:
                return r.read()
        except HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:
            last = e
        time.sleep(.5 + k)
    raise RuntimeError(f"{url}: {last}")

def parse_metrics(blob):
    if blob is None:
        return []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode("utf-8")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []
    header = [x.strip() for x in rows[0]]
    if "create_time" not in header:
        return []
    idx = {k: i for i, k in enumerate(header)}
    need = [
        "create_time", "sum_open_interest", "sum_open_interest_value",
        "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    ]
    if any(k not in idx for k in need):
        return []
    out = []
    for r in rows[1:]:
        try:
            out.append((
                r[idx["create_time"]],
                float(r[idx["sum_open_interest"]]),
                float(r[idx["sum_open_interest_value"]]),
                float(r[idx["count_toptrader_long_short_ratio"]]),
                float(r[idx["sum_toptrader_long_short_ratio"]]),
                float(r[idx["count_long_short_ratio"]]),
                float(r[idx["sum_taker_long_short_vol_ratio"]]),
            ))
        except Exception:
            continue
    return out

def fetch_day(asset, day):
    sym = SYMBOLS[asset]
    ds = day.strftime("%Y-%m-%d")
    url = f"https://data.binance.vision/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{ds}.zip"
    blob = get(url)
    return ds, parse_metrics(blob)

def download_metrics(asset):
    p = CACHE / f"{asset}_metrics5.pkl"
    if p.exists():
        return pd.read_pickle(p)
    days = list(pd.date_range("2022-01-01", "2025-12-31", freq="D", tz="UTC"))
    days += list(pd.date_range("2026-03-01", "2026-10-03", freq="D", tz="UTC"))
    all_rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(fetch_day, asset, d): d for d in days}
        for f in as_completed(futs):
            ds, rows = f.result()
            if rows:
                all_rows.extend(rows)
            done += 1
            if done % 200 == 0:
                print(asset, "metrics_days", done, "/", len(days), flush=True)
    m = pd.DataFrame(all_rows, columns=[
        "create_time", "oi", "oi_value", "top_account", "top_position",
        "all_account", "taker_ratio",
    ])
    m["create_time"] = pd.to_datetime(m.create_time, utc=True, errors="coerce")
    m = m.dropna(subset=["create_time"]).sort_values("create_time")
    # Preserve both transition rows by computing availability before de-duplication.
    m["avail_time"] = m.create_time
    new = m.create_time >= CUT
    m.loc[new, "avail_time"] = m.loc[new, "create_time"] + pd.Timedelta(minutes=5)
    m["avail_ts"] = (m.avail_time.astype("int64") // 10**9).astype("int64")
    m = m.sort_values(["avail_ts", "create_time"]).drop_duplicates("avail_ts", keep="last").reset_index(drop=True)
    m.to_pickle(p)
    print(asset, "metrics_rows", len(m), flush=True)
    return m

def zscore_causal(s, win=2016, minp=288):
    mu = s.rolling(win, min_periods=minp).mean().shift(1)
    sd = s.rolling(win, min_periods=minp).std().shift(1)
    return (s - mu) / sd.replace(0, np.nan)

def add_features(m):
    x = m.copy()
    for c in ["oi", "top_account", "top_position", "all_account", "taker_ratio"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x[(x.oi > 0) & (x.top_position > 0) & (x.all_account > 0) & (x.taker_ratio > 0)].copy()

    lo = np.log(x.oi)
    x["oi_d3"] = lo - lo.shift(3)
    x["oi_d12"] = lo - lo.shift(12)
    x["oi_d48"] = lo - lo.shift(48)
    x["oi12_z"] = zscore_causal(x.oi_d12)
    x["oi3_z"] = zscore_causal(x.oi_d3)
    x["oi_accel_z"] = x.oi3_z - x.oi12_z

    lp = np.log(x.top_position)
    la = np.log(x.all_account)
    x["top_z"] = zscore_causal(lp)
    x["retail_z"] = zscore_causal(la)
    x["top_d12_z"] = zscore_causal(lp - lp.shift(12))
    x["retail_d12_z"] = zscore_causal(la - la.shift(12))
    x["smart_retail_z"] = zscore_causal(lp - la)

    lt = np.log(x.taker_ratio)
    x["taker_z"] = zscore_causal(lt)
    return x[[
        "avail_ts", "oi12_z", "oi_accel_z", "top_z", "retail_z",
        "top_d12_z", "retail_d12_z", "smart_retail_z", "taker_z",
    ]].dropna().reset_index(drop=True)

def tri(v, hi=.65, lo=-.65):
    if v >= hi:
        return "SUPPORT"
    if v <= lo:
        return "AGAINST"
    return "NEUTRAL"

def attach_states(labels, feat):
    z = labels.copy()
    sig_ts = z.open_ts.to_numpy(np.int64) - 60
    ft = feat.avail_ts.to_numpy(np.int64)
    ix = np.searchsorted(ft, sig_ts, side="right") - 1
    good = ix >= 0
    z = z.loc[good].copy()
    ix = ix[good]

    oi = feat.oi12_z.to_numpy()[ix]
    oa = feat.oi_accel_z.to_numpy()[ix]
    top = feat.top_z.to_numpy()[ix]
    retail = feat.retail_z.to_numpy()[ix]
    topd = feat.top_d12_z.to_numpy()[ix]
    retaild = feat.retail_d12_z.to_numpy()[ix]
    smart = feat.smart_retail_z.to_numpy()[ix]
    taker = feat.taker_z.to_numpy()[ix]
    d = z.d.to_numpy(np.int8)

    z["oi_state"] = np.where(oi >= .65, "BUILD", np.where(oi <= -.65, "UNWIND", "FLAT"))
    z["oi_accel"] = np.where(oa >= .65, "ACCEL", np.where(oa <= -.65, "DECEL", "STEADY"))
    z["top_flow"] = [tri(float(a*b)) for a,b in zip(d,topd)]
    z["taker_flow"] = [tri(float(a*b)) for a,b in zip(d,taker)]

    dt = d * top
    dr = d * retail
    z["top_crowd"] = np.where(dt >= 1.20, "CROWDED", np.where(dt <= -.75, "OPPOSED", "NORMAL"))
    z["retail_crowd"] = np.where(dr >= 1.20, "CROWDED", np.where(dr <= -.75, "OPPOSED", "NORMAL"))

    # Directional top-vs-retail positioning.
    ds = d * smart
    same = np.sign(topd) == np.sign(retaild)
    z["smart_retail"] = np.where(ds >= .75, "TOP_OVERWEIGHT",
                           np.where(ds <= -.75, "RETAIL_OVERWEIGHT",
                             np.where(same, "SAME", "DIVERGENT")))
    z["ext_state"] = np.where(z.ext <= .15, "EARLY", np.where(z.ext <= .30, "MID", "LATE"))
    return z

def run(asset):
    btc = load("BTC")
    eth = load("ETH")
    raw = btc if asset == "BTC" else eth
    other = eth if asset == "BTC" else btc
    labels = make_labels(asset, raw, other)
    print(asset, "spot_labels", len(labels), flush=True)

    m = download_metrics(asset)
    feat = add_features(m)
    df = attach_states(labels, feat)
    print(asset, "positioning_labels", len(df), flush=True)

    catalogs = {}
    for quality in ("Q1", "Q2"):
        for train_end in (2023, 2024):
            catalogs[(quality, train_end)] = base.build_catalog(df, train_end, quality)

    passed = []
    diagnostics = []
    for cfg in base.configs():
        parts = []
        counts = []
        for y in (2024, 2025):
            rules = base.select_rules(catalogs[(cfg["quality"], y-1)], cfg)
            counts.append(len(rules))
            parts.append(base.evaluate(df, rules, y, cfg["quality"]))
        ds, da, dss = base.diag_score(parts)
        diagnostics.append({
            "score": ds, "config": cfg, "walkforward": da, "stress": dss,
            "rule_counts": {"2024": counts[0], "2025": counts[1]},
            "years": {"2024": parts[0]["summary"], "2025": parts[1]["summary"]},
            "by_direction": {"2024": parts[0]["by_direction"], "2025": parts[1]["by_direction"]},
        })
        p = base.wf_pass(parts)
        if p is not None:
            score, agg, ss = p
            passed.append({
                "score": score, "config": cfg, "walkforward": agg, "stress": ss,
                "years": {"2024": parts[0]["summary"], "2025": parts[1]["summary"]},
            })

    diagnostics.sort(key=lambda r: r["score"], reverse=True)
    passed.sort(key=lambda r: r["score"], reverse=True)
    out = {
        "asset": asset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Causal Binance futures positioning metrics + spot state/event router; BTC/ETH independent.",
        "selected": None,
        "diagnostics": diagnostics[:10],
    }
    if not passed:
        print(asset, "POSITIONING_NO_WF_PASS", flush=True)
        print(asset, "POSITIONING_NEAR", json.dumps(diagnostics[:5], separators=(",",":"), default=json_default), flush=True)
        return out

    best = passed[0]
    cfg = best["config"]
    final_catalog = base.build_catalog(df, 2025, cfg["quality"])
    rules = base.select_rules(final_catalog, cfg)
    test = base.evaluate(df, rules, 2026, cfg["quality"])
    out["selected"] = {
        "config": cfg,
        "walkforward": best["walkforward"],
        "walkforward_stress": best["stress"],
        "walkforward_years": best["years"],
        "n_final_rules": len(rules),
        "test_2026": test["summary"],
        "test_2026_stress_5bp": test["stress"],
        "by_direction": test["by_direction"],
        "rules": [{
            "pattern": r["pattern"], "key": r["key"], "profile": r["profile"],
            "score": r["score"], "conservative_ev": r["conservative_ev"],
            "recent": r["recent"], "latest": r["latest"],
        } for r in rules],
        "trades": test["trades"],
    }
    print(asset, "POSITIONING_SELECTED", json.dumps({
        "config": cfg,
        "walkforward": best["walkforward"],
        "walkforward_stress": best["stress"],
        "n_final_rules": len(rules),
        "test_2026": test["summary"],
        "test_2026_stress_5bp": test["stress"],
        "by_direction": test["by_direction"],
    }, separators=(",",":"), default=json_default), flush=True)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", choices=["BTC","ETH"], required=True)
    args = ap.parse_args()
    out = run(args.asset)
    p = OUT / args.asset.lower()
    p.mkdir(parents=True, exist_ok=True)
    (p / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False, default=json_default))
    print("VERITAS_POSITIONING_RESULT=" + json.dumps({
        "asset": args.asset, "selected": out.get("selected"),
        "diagnostics": out.get("diagnostics", [])[:3],
    }, ensure_ascii=False, separators=(",",":"), default=json_default), flush=True)

if __name__ == "__main__":
    main()
