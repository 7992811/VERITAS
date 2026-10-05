"""VERITAS derivative-state router for BTC and ETH.

Research only. Adds causal USD-M perpetual futures information to the existing
spot state/event framework:
- 5m taker-buy imbalance (standardized on trailing 7d history)
- futures volume expansion
- perpetual-vs-spot basis z-score and 1h basis impulse
- futures-vs-spot 1h lead/lag

BTC and ETH are selected independently. Policy selection uses only anchored
walk-forward 2024 and 2025. Apr-Oct 2026 is research validation only and is not
used to choose parameters. Production is unchanged.
"""
from __future__ import annotations

import argparse
import csv
import gc
import io
import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from research_crypto_manager_library import load
from research_crypto_asset_specific import (
    make_labels, quality_mask, summarize, stats_block
)
from research_crypto_state_router import STRESS, json_default

OUT = Path("derivative_router_out")
CACHE = OUT / "data"
OUT.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
BAR = 300
COOLDOWN = 15 * 60

PATTERNS = {
    "D0": ["trigger", "d", "align", "flow3", "basis_state", "ext_state"],
    "D1": ["trigger", "d", "align", "flow3", "basis_impulse", "futvol"],
    "D2": ["trigger", "d", "align", "flow12", "lead_state", "ext_state"],
    "D3": ["trigger", "d", "strength", "flow3", "basis_state", "lead_state"],
    "D4": ["trigger", "d", "vol", "flow3", "flow_accel", "basis_state"],
    "D5": ["trigger", "d", "align", "flow3", "flow12", "futvol", "ext_state"],
}

def get(url):
    last = None
    for k in range(5):
        try:
            req = Request(url, headers={"User-Agent": "VERITAS-derivative-research/1.0"})
            with urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.0 + 1.5 * k)
    raise RuntimeError(f"{url}: {last}")

def readzip_5m(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode("utf-8")
    rows = []
    for r in csv.reader(io.StringIO(raw)):
        if not r:
            continue
        try:
            t = int(r[0])
        except Exception:
            continue
        if t > 10**14:
            t //= 1000000
        elif t > 10**11:
            t //= 1000
        if len(r) < 11:
            continue
        rows.append((
            t,
            float(r[1]), float(r[2]), float(r[3]), float(r[4]),
            float(r[5]), float(r[7]), float(r[8]),
            float(r[9]), float(r[10]),
        ))
    return pd.DataFrame(rows, columns=[
        "open_ts", "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_buy", "taker_buy_quote",
    ])

def months_between(a, b):
    p = pd.Timestamp(a, tz="UTC").to_period("M")
    q = pd.Timestamp(b, tz="UTC").to_period("M")
    out = []
    while p <= q:
        out.append((p.year, p.month))
        p += 1
    return out

def download_futures_5m(asset):
    p = CACHE / f"{asset}_fut5.pkl"
    if p.exists():
        return pd.read_pickle(p)

    sym = SYMBOLS[asset]
    parts = []
    # Full selection history + warm-up.
    months = months_between("2021-12-01", "2025-12-01")
    # Test warm-up and months that are safely archived as monthly files.
    months += months_between("2026-03-01", "2026-08-01")
    for y, m in months:
        url = (
            f"https://data.binance.vision/data/futures/um/monthly/klines/"
            f"{sym}/5m/{sym}-5m-{y}-{m:02d}.zip"
        )
        print(asset, "download_fut_month", f"{y}-{m:02d}", flush=True)
        parts.append(readzip_5m(get(url)))

    # September plus Oct 1-3: use daily archives to avoid assuming monthly publication.
    for day in pd.date_range("2026-09-01", "2026-10-03", freq="D", tz="UTC"):
        ds = day.strftime("%Y-%m-%d")
        url = (
            f"https://data.binance.vision/data/futures/um/daily/klines/"
            f"{sym}/5m/{sym}-5m-{ds}.zip"
        )
        print(asset, "download_fut_day", ds, flush=True)
        parts.append(readzip_5m(get(url)))

    f = pd.concat(parts, ignore_index=True)
    f = f.drop_duplicates("open_ts").sort_values("open_ts").reset_index(drop=True)
    f.to_pickle(p)
    return f

def add_derivative_features(fut, spot):
    f = fut.copy()
    # A 5m futures bar becomes observable only after it has closed.
    f["avail_ts"] = f.open_ts + BAR

    # Match the spot minute that closes with the futures 5m bar.
    spot_s = pd.Series(spot.close.to_numpy(), index=spot.ts.to_numpy())
    end_minute = f.open_ts.to_numpy() + 240
    f["spot_close"] = spot_s.reindex(end_minute).to_numpy()
    f = f[np.isfinite(f.spot_close)].copy()

    f["imb"] = 2.0 * f.taker_buy / f.volume.replace(0, np.nan) - 1.0
    f["imb3"] = f.imb.rolling(3, min_periods=3).mean()
    f["imb12"] = f.imb.rolling(12, min_periods=12).mean()

    week = 7 * 24 * 12
    mu3 = f.imb3.rolling(week, min_periods=288).mean().shift(1)
    sd3 = f.imb3.rolling(week, min_periods=288).std().shift(1)
    mu12 = f.imb12.rolling(week, min_periods=288).mean().shift(1)
    sd12 = f.imb12.rolling(week, min_periods=288).std().shift(1)
    f["imb3_z"] = (f.imb3 - mu3) / sd3.replace(0, np.nan)
    f["imb12_z"] = (f.imb12 - mu12) / sd12.replace(0, np.nan)
    f["imb_accel_z"] = f.imb3_z - f.imb12_z

    f["vol_med"] = f.volume.rolling(288, min_periods=96).median().shift(1)
    f["fut_vol_ratio"] = f.volume / f.vol_med.replace(0, np.nan)

    f["basis"] = f.close / f.spot_close - 1.0
    bmu = f.basis.rolling(week, min_periods=288).mean().shift(1)
    bsd = f.basis.rolling(week, min_periods=288).std().shift(1)
    f["basis_z"] = (f.basis - bmu) / bsd.replace(0, np.nan)
    f["basis_delta12"] = f.basis - f.basis.shift(12)
    bd_sd = f.basis_delta12.rolling(week, min_periods=288).std().shift(1)
    f["basis_impulse_z"] = f.basis_delta12 / bd_sd.replace(0, np.nan)

    f["fut_ret12"] = f.close / f.close.shift(12) - 1.0
    f["spot_ret12"] = f.spot_close / f.spot_close.shift(12) - 1.0
    f["lead12"] = f.fut_ret12 - f.spot_ret12
    lead_sd = f.lead12.rolling(week, min_periods=288).std().shift(1)
    f["lead_z"] = f.lead12 / lead_sd.replace(0, np.nan)

    cols = [
        "avail_ts", "imb3_z", "imb12_z", "imb_accel_z", "fut_vol_ratio",
        "basis_z", "basis_impulse_z", "lead_z",
    ]
    return f[cols].dropna().reset_index(drop=True)

def three(v, hi=.75, lo=-.75):
    if v >= hi:
        return "SUPPORT"
    if v <= lo:
        return "AGAINST"
    return "NEUTRAL"

def attach_states(df, futfeat):
    z = df.copy()
    sig_ts = z.open_ts.to_numpy(np.int64) - 60
    ft = futfeat.avail_ts.to_numpy(np.int64)
    ix = np.searchsorted(ft, sig_ts, side="right") - 1
    good = ix >= 0
    z = z.loc[good].copy()
    ix = ix[good]

    imb3 = futfeat.imb3_z.to_numpy()[ix]
    imb12 = futfeat.imb12_z.to_numpy()[ix]
    accel = futfeat.imb_accel_z.to_numpy()[ix]
    vr = futfeat.fut_vol_ratio.to_numpy()[ix]
    bz = futfeat.basis_z.to_numpy()[ix]
    bi = futfeat.basis_impulse_z.to_numpy()[ix]
    lead = futfeat.lead_z.to_numpy()[ix]
    d = z.d.to_numpy(np.int8)

    z["flow3"] = [three(float(a*b)) for a, b in zip(d, imb3)]
    z["flow12"] = [three(float(a*b)) for a, b in zip(d, imb12)]
    z["flow_accel"] = [three(float(a*b), .60, -.60) for a, b in zip(d, accel)]

    dbz = d * bz
    z["basis_state"] = np.where(dbz >= 1.25, "CROWDED",
                          np.where(dbz <= -0.75, "DISCOUNT", "NORMAL"))
    dbi = d * bi
    z["basis_impulse"] = np.where(dbi >= .60, "SUPPORT",
                            np.where(dbi <= -.60, "AGAINST", "NEUTRAL"))
    dl = d * lead
    z["lead_state"] = np.where(dl >= .60, "FUT_LEADS",
                         np.where(dl <= -.60, "FUT_LAGS", "SYNC"))
    z["futvol"] = np.where(vr >= 1.40, "HIGH",
                     np.where(vr <= .75, "LOW", "NORMAL"))
    z["ext_state"] = np.where(z.ext <= .15, "EARLY",
                        np.where(z.ext <= .30, "MID", "LATE"))
    return z

def side_ok(d, side):
    return side == "BOTH" or (side == "LONG" and d > 0) or (side == "SHORT" and d < 0)

def build_catalog(df, train_end, quality):
    q = df[(df.year <= train_end) & quality_mask(df, quality)].copy()
    years = sorted(int(y) for y in q.year.unique())
    recent_years = [y for y in years if y >= train_end - 1]
    out = []
    for profile in q.profile.unique():
        qp = q[q.profile == profile]
        for pattern, cols in PATTERNS.items():
            for key, g in qp.groupby(cols, dropna=False, sort=False):
                if not isinstance(key, tuple):
                    key = (key,)
                total = stats_block(g)
                recent = stats_block(g[g.year.isin(recent_years)])
                latest = stats_block(g[g.year == train_end])
                if total is None:
                    continue
                represented = 0
                pos_years = 0
                yearly = {}
                for y in years:
                    gy = g[g.year == y]
                    if len(gy) >= 4:
                        s = stats_block(gy)
                        yearly[str(y)] = s
                        represented += 1
                        if s["avg"] > 0:
                            pos_years += 1
                out.append({
                    "pattern": pattern, "cols": cols, "key": list(key),
                    "profile": profile, "d": int(g.d.iloc[0]),
                    "total": total, "recent": recent, "latest": latest,
                    "represented": represented, "pos_years": pos_years,
                    "yearly": yearly,
                })
    return out

def configs():
    out = []
    for quality in ("Q1", "Q2"):
        for side in ("BOTH", "LONG", "SHORT"):
            for recent_pf in (1.05, 1.20, 1.35):
                out.append({
                    "quality": quality, "side": side, "recent_pf": recent_pf,
                    "min_n": 24 if quality == "Q1" else 18,
                    "min_recent_n": 8,
                    "max_rules": 36,
                })
    return out

def select_rules(catalog, cfg):
    cand = []
    for r in catalog:
        if not side_ok(r["d"], cfg["side"]):
            continue
        t, q, l = r["total"], r["recent"], r["latest"]
        if q is None or l is None:
            continue
        if t["n"] < cfg["min_n"] or q["n"] < cfg["min_recent_n"] or l["n"] < 4:
            continue
        if r["represented"] < 2 or r["pos_years"] < 2:
            continue
        if t["stress_avg"] <= 0 or t["stress_pf"] < 1.02:
            continue
        if q["avg"] <= 0 or q["stress_avg"] <= 0 or q["pf"] < cfg["recent_pf"]:
            continue
        # Hard recent-year veto: no stale edge.
        if l["avg"] <= 0:
            continue
        cev = min(t["lcb75"], q["stress_avg"], .65*q["avg"] + .35*t["shrunk"])
        if cev <= 0:
            continue
        score = 135*cev + .55*q["win_rate"] + .22*min(q["pf"], 3.0) + .08*min(t["stress_pf"], 3.0)
        x = dict(r)
        x["conservative_ev"] = float(cev)
        x["score"] = float(score)
        cand.append(x)

    # One stop/target profile per state.
    best = {}
    for r in cand:
        k = (r["pattern"], tuple(r["key"]))
        if k not in best or r["score"] > best[k]["score"]:
            best[k] = r
    cand = sorted(best.values(), key=lambda r: r["score"], reverse=True)

    out = []
    cap = {}
    for r in cand:
        k = (str(r["key"][0]), int(r["d"]), r["pattern"])
        if cap.get(k, 0) >= 3:
            continue
        cap[k] = cap.get(k, 0) + 1
        out.append(r)
        if len(out) >= cfg["max_rules"]:
            break
    return out

def evaluate(df, rules, year, quality):
    if not rules:
        return {"summary": summarize([]), "stress": summarize([]), "by_direction": {}, "trades": []}
    val = df[(df.year == year) & quality_mask(df, quality)].copy()
    pieces = []
    for pattern, cols in PATTERNS.items():
        rr = [r for r in rules if r["pattern"] == pattern]
        if not rr:
            continue
        rt = pd.DataFrame([{
            **{c: v for c, v in zip(cols, r["key"])},
            "profile": r["profile"],
            "rule_score": r["score"],
            "rule_cev": r["conservative_ev"],
        } for r in rr])
        m = val.merge(rt, on=["profile"] + cols, how="inner")
        if not m.empty:
            pieces.append(m)
    if not pieces:
        return {"summary": summarize([]), "stress": summarize([]), "by_direction": {}, "trades": []}
    cand = pd.concat(pieces, ignore_index=True)
    cand = cand.sort_values(["open_ts", "rule_score"], ascending=[True, False])
    cand = cand.drop_duplicates(["open_ts", "close_ts", "d", "trigger"], keep="first")

    picks = []
    free = -1
    for r in cand.itertuples(index=False):
        if int(r.open_ts) < free:
            continue
        picks.append({
            "open_ts": int(r.open_ts), "close_ts": int(r.close_ts),
            "d": int(r.d), "trigger": str(r.trigger), "profile": str(r.profile),
            "net": float(r.net), "score": float(r.rule_score),
            "conservative_ev": float(r.rule_cev),
        })
        free = int(r.close_ts) + COOLDOWN

    vals = [p["net"] for p in picks]
    stress = [p["net"] - STRESS for p in picks]
    return {
        "summary": summarize(vals),
        "stress": summarize(stress),
        "by_direction": {
            "LONG": summarize([p["net"] for p in picks if p["d"] > 0]),
            "SHORT": summarize([p["net"] for p in picks if p["d"] < 0]),
        },
        "trades": picks,
    }

def aggregate(parts):
    vals = []
    stress = []
    for p in parts:
        vals.extend(t["net"] for t in p["trades"])
        stress.extend(t["net"] - STRESS for t in p["trades"])
    return summarize(vals), summarize(stress)

def wf_pass(parts):
    if any(p["summary"]["n"] < 8 for p in parts):
        return None
    if any((p["summary"]["avg"] or -9) <= 0 or p["summary"]["pf"] < 1.05 for p in parts):
        return None
    s, ss = aggregate(parts)
    if s["n"] < 20 or s["avg"] <= 0 or s["pf"] < 1.15:
        return None
    if ss["avg"] <= 0 or ss["pf"] < 1.05:
        return None
    score = 1.25*s["win_rate"] + .30*min(s["pf"], 3.0) + 75*s["avg"] - .08*s["dd"]
    return float(score), s, ss

def diag_score(parts):
    s, ss = aggregate(parts)
    avgs = [p["summary"]["avg"] if p["summary"]["n"] else -0.05 for p in parts]
    pfs = [p["summary"]["pf"] if p["summary"]["n"] else 0.0 for p in parts]
    return float(180*min(avgs) + .5*min(pfs) + .25*min(s["pf"],3.0) + .6*(s["win_rate"] or 0) + 35*(ss["avg"] or -0.05)), s, ss

def run(asset):
    btc = load("BTC")
    eth = load("ETH")
    raw = btc if asset == "BTC" else eth
    other = eth if asset == "BTC" else btc

    labels = make_labels(asset, raw, other)
    print(asset, "spot_labels", len(labels), flush=True)
    fut = download_futures_5m(asset)
    futfeat = add_derivative_features(fut, raw)
    df = attach_states(labels, futfeat)
    print(asset, "derivative_labels", len(df), flush=True)
    del fut, futfeat, labels
    gc.collect()

    catalogs = {}
    for quality in ("Q1", "Q2"):
        for train_end in (2023, 2024):
            catalogs[(quality, train_end)] = build_catalog(df, train_end, quality)

    passed = []
    diagnostics = []
    for cfg in configs():
        parts = []
        counts = []
        for y in (2024, 2025):
            rules = select_rules(catalogs[(cfg["quality"], y-1)], cfg)
            counts.append(len(rules))
            parts.append(evaluate(df, rules, y, cfg["quality"]))
        ds, da, dss = diag_score(parts)
        diagnostics.append({
            "score": ds, "config": cfg, "walkforward": da, "stress": dss,
            "rule_counts": {"2024": counts[0], "2025": counts[1]},
            "years": {"2024": parts[0]["summary"], "2025": parts[1]["summary"]},
            "by_direction": {"2024": parts[0]["by_direction"], "2025": parts[1]["by_direction"]},
        })
        p = wf_pass(parts)
        if p is not None:
            score, agg, ss = p
            passed.append({"score": score, "config": cfg, "walkforward": agg, "stress": ss,
                           "years": {"2024": parts[0]["summary"], "2025": parts[1]["summary"]}})

    diagnostics.sort(key=lambda r: r["score"], reverse=True)
    passed.sort(key=lambda r: r["score"], reverse=True)
    out = {
        "asset": asset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Causal 5m USD-M derivative state + spot event router; independent BTC/ETH anchored walk-forward.",
        "selected": None,
        "diagnostics": diagnostics[:10],
    }
    if not passed:
        print(asset, "DERIVATIVE_NO_WF_PASS", flush=True)
        print(asset, "DERIVATIVE_NEAR", json.dumps(diagnostics[:5], separators=(",",":"), default=json_default), flush=True)
        return out

    best = passed[0]
    cfg = best["config"]
    final_catalog = build_catalog(df, 2025, cfg["quality"])
    rules = select_rules(final_catalog, cfg)
    test = evaluate(df, rules, 2026, cfg["quality"])
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
    print(asset, "DERIVATIVE_SELECTED", json.dumps({
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
    print("VERITAS_DERIVATIVE_RESULT=" + json.dumps({
        "asset": args.asset, "selected": out.get("selected"),
        "diagnostics": out.get("diagnostics", [])[:3],
    }, ensure_ascii=False, separators=(",",":"), default=json_default), flush=True)

if __name__ == "__main__":
    main()
