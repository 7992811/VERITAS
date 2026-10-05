"""VERITAS asset-specific BTC/ETH state policy research.

Research only. BTC and ETH are tuned and validated independently.
The policy is chosen only from anchored walk-forward years before the final 2026
evaluation. It deliberately prefers recent, repeated edge and vetoes stale states.

Important: 2026 has been inspected by earlier VERITAS research, so this run is
research validation, not a pristine untouched holdout. Production is unchanged.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research_crypto_manager_library import load, features, ts, TEST_START, TEST_END
from research_crypto_state_router import (
    add_local_levels, add_indicators, add_other, trigger_events, one_trade,
    PROFILES, FEE, SLIP, STRESS, json_default
)

PATTERNS = {
    "C0": ["trigger", "d", "align"],
    "C1": ["trigger", "d", "align", "vol"],
    "C2": ["trigger", "d", "align", "vol", "cross"],
    "C3": ["trigger", "d", "align", "strength", "cross"],
    "C4": ["trigger", "d", "vol", "volume", "body", "cross"],
    "C5": ["trigger", "d", "align", "vol", "strength", "fresh_pb"],
}

COOLDOWN = 15 * 60

def pf(vals):
    a = np.asarray(vals, float)
    if not len(a):
        return 0.0
    p = a[a > 0].sum()
    n = -a[a < 0].sum()
    return float(p / n) if n > 0 else 99.0

def summarize(vals):
    a = np.asarray(vals, float)
    if not len(a):
        return {"n": 0, "win_rate": None, "avg": None, "sum": 0.0, "pf": 0.0, "dd": 0.0}
    eq = np.cumsum(a)
    pk = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    return {
        "n": int(len(a)),
        "win_rate": float((a > 0).mean()),
        "avg": float(a.mean()),
        "sum": float(a.sum()),
        "pf": pf(a),
        "dd": float((pk - eq).max()),
    }

def quality_mask(df, mode):
    if mode == "Q1":
        return (
            df["align"].isin(["A3", "A2", "A2M"])
            & (df["volume"] != "VLOW")
            & (df["body"] != "BLOW")
        )
    if mode == "Q2":
        return (
            df["align"].isin(["A3", "A2"])
            & df["volume"].isin(["V2", "V15", "V12"])
            & df["body"].isin(["B70", "B55", "B40"])
            & df["strength"].isin(["STRONG", "MID"])
        )
    raise ValueError(mode)

def make_labels(asset, raw, other):
    x = features(raw)
    x = add_local_levels(x)
    x = add_indicators(raw, x)
    x = add_other(other, x, "btc" if asset == "ETH" else "eth")
    ev = trigger_events(asset, x)
    rows = []
    for pname, buf, rr in PROFILES:
        for e in ev:
            y = int(e["year"])
            if y < 2022 or y > 2026:
                continue
            start = ts(f"{y}-01-01T00:00:00Z") if y <= 2025 else ts(TEST_START)
            end = ts(f"{y+1}-01-01T00:00:00Z") if y <= 2025 else ts(TEST_END)
            tr = one_trade(x, e, buf, rr, start, end, 0.0)
            if tr is None:
                continue
            r = {k: e[k] for k in (
                "year", "d", "trigger", "align", "vol", "volume", "body",
                "strength", "cross", "fresh_pb", "ext"
            )}
            r.update({
                "profile": pname,
                "net": float(tr["net"]),
                "open_ts": int(tr["open_ts"]),
                "close_ts": int(tr["close_ts"]),
                "reason": tr["reason"],
            })
            rows.append(r)
    del x
    gc.collect()
    return pd.DataFrame(rows)

def stats_block(g):
    a = g.net.to_numpy(float)
    n = len(a)
    if not n:
        return None
    mean = float(a.mean())
    sd = float(a.std(ddof=1)) if n > 1 else abs(mean) + 0.005
    # Shrink mean toward zero and use a mild one-sided confidence haircut.
    shrunk = mean * n / (n + 30.0)
    lcb = shrunk - 0.67449 * sd / math.sqrt(n + 30.0)
    return {
        "n": int(n),
        "avg": mean,
        "pf": pf(a),
        "win_rate": float((a > 0).mean()),
        "stress_avg": float((a - STRESS).mean()),
        "stress_pf": pf(a - STRESS),
        "shrunk": float(shrunk),
        "lcb75": float(lcb),
    }

def build_catalog(df, train_end, quality):
    z = df[(df.year <= train_end) & quality_mask(df, quality)].copy()
    rows = []
    years = sorted(int(y) for y in z.year.unique())
    recent_years = [y for y in years if y >= train_end - 1]
    for pname in z.profile.unique():
        zp = z[z.profile == pname]
        for pattern, cols in PATTERNS.items():
            for key, g in zp.groupby(cols, dropna=False, sort=False):
                if not isinstance(key, tuple):
                    key = (key,)
                total = stats_block(g)
                if total is None:
                    continue
                recent = stats_block(g[g.year.isin(recent_years)])
                latest = stats_block(g[g.year == train_end])
                pos_years = 0
                represented = 0
                yearly = {}
                for y in years:
                    gy = g[g.year == y]
                    if len(gy) >= 4:
                        s = stats_block(gy)
                        yearly[str(y)] = s
                        represented += 1
                        if s["avg"] > 0:
                            pos_years += 1
                rows.append({
                    "pattern": pattern,
                    "cols": cols,
                    "key": list(key),
                    "profile": pname,
                    "d": int(g.d.iloc[0]),
                    "total": total,
                    "recent": recent,
                    "latest": latest,
                    "represented": represented,
                    "pos_years": pos_years,
                    "yearly": yearly,
                })
    return rows

def candidate_configs(asset):
    # Same search space, independent selection per asset. No 2026 statistics enter here.
    out = []
    for quality in ("Q1", "Q2"):
        for side in ("BOTH", "LONG", "SHORT"):
            for recent_pf in (1.05, 1.20, 1.35):
                for latest in ("NONNEG", "POS"):
                    out.append({
                        "quality": quality,
                        "side": side,
                        "recent_pf": recent_pf,
                        "latest": latest,
                        "min_n": 28 if quality == "Q1" else 20,
                        "min_recent_n": 10 if quality == "Q1" else 8,
                        "min_recent_avg": 0.0,
                        "min_stress_pf": 1.02,
                        "max_rules": 48 if quality == "Q1" else 36,
                    })
    return out

def side_ok(d, side):
    return side == "BOTH" or (side == "LONG" and d > 0) or (side == "SHORT" and d < 0)

def select_rules(catalog, cfg, train_end):
    chosen = []
    for r in catalog:
        if not side_ok(r["d"], cfg["side"]):
            continue
        t = r["total"]
        q = r["recent"]
        l = r["latest"]
        if q is None or l is None:
            continue
        if t["n"] < cfg["min_n"] or q["n"] < cfg["min_recent_n"]:
            continue
        if t["stress_avg"] <= 0 or t["stress_pf"] < cfg["min_stress_pf"]:
            continue
        if q["avg"] <= cfg["min_recent_avg"] or q["pf"] < cfg["recent_pf"] or q["stress_avg"] <= 0:
            continue
        if r["represented"] < 2 or r["pos_years"] < 2:
            continue
        if l["n"] < 4:
            continue
        if cfg["latest"] == "POS" and l["avg"] <= 0:
            continue
        if cfg["latest"] == "NONNEG" and l["avg"] < -0.00025:
            continue
        # Recent edge dominates; total history acts as a prior.
        cev = min(
            max(t["lcb75"], -0.02),
            q["stress_avg"],
            0.65 * q["avg"] + 0.35 * t["shrunk"],
        )
        if cev <= 0:
            continue
        score = (
            125.0 * cev
            + 0.50 * q["win_rate"]
            + 0.22 * min(q["pf"], 3.0)
            + 0.08 * min(t["stress_pf"], 3.0)
        )
        x = dict(r)
        x["score"] = float(score)
        x["conservative_ev"] = float(cev)
        chosen.append(x)

    # Pick one stop/target profile for each state and pattern.
    best = {}
    for r in chosen:
        k = (r["pattern"], tuple(r["key"]))
        if k not in best or r["score"] > best[k]["score"]:
            best[k] = r
    chosen = sorted(best.values(), key=lambda r: r["score"], reverse=True)

    # Diversity cap: prevent one trigger+direction family from filling the policy.
    out = []
    counts = {}
    for r in chosen:
        trig = str(r["key"][0])
        d = int(r["d"])
        k = (trig, d, r["pattern"])
        if counts.get(k, 0) >= 3:
            continue
        counts[k] = counts.get(k, 0) + 1
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
            "_score": r["score"],
            "_cev": r["conservative_ev"],
            "_pattern": pattern,
        } for r in rr])
        m = val.merge(rt, on=["profile"] + cols, how="inner")
        if not m.empty:
            pieces.append(m)
    if not pieces:
        return {"summary": summarize([]), "stress": summarize([]), "by_direction": {}, "trades": []}
    cand = pd.concat(pieces, ignore_index=True)
    cand = cand.sort_values(["open_ts", "_score"], ascending=[True, False])

    # Same event may match coarse and specific states. Keep the best score.
    cand = cand.drop_duplicates(["open_ts", "close_ts", "d", "trigger"], keep="first")
    picks = []
    free = -1
    for r in cand.itertuples(index=False):
        if int(r.open_ts) < free:
            continue
        size = 0.25 if r._cev < 0.00075 else (0.50 if r._cev < 0.0015 else (0.75 if r._cev < 0.003 else 1.0))
        picks.append({
            "open_ts": int(r.open_ts),
            "close_ts": int(r.close_ts),
            "d": int(r.d),
            "trigger": str(r.trigger),
            "profile": str(r.profile),
            "net": float(r.net),
            "score": float(r._score),
            "conservative_ev": float(r._cev),
            "size": float(size),
        })
        free = int(r.close_ts) + COOLDOWN

    vals = [p["net"] for p in picks]
    stress = [p["net"] - STRESS for p in picks]
    by = {
        "LONG": summarize([p["net"] for p in picks if p["d"] > 0]),
        "SHORT": summarize([p["net"] for p in picks if p["d"] < 0]),
    }
    weighted = summarize([p["net"] * p["size"] for p in picks])
    s = summarize(vals)
    s["weighted"] = weighted
    s["avg_size"] = float(np.mean([p["size"] for p in picks])) if picks else None
    return {"summary": s, "stress": summarize(stress), "by_direction": by, "trades": picks}

def walkforward_score(parts):
    nonempty = [p for p in parts if p["summary"]["n"] > 0]
    if len(nonempty) < 2:
        return None
    if any(p["summary"]["n"] < 8 for p in nonempty):
        return None
    if any((p["summary"]["avg"] or -9) <= 0 for p in nonempty):
        return None
    if any(p["summary"]["pf"] < 1.05 for p in nonempty):
        return None
    vals = []
    stress = []
    for p in nonempty:
        vals.extend(t["net"] for t in p["trades"])
        stress.extend(t["net"] - STRESS for t in p["trades"])
    s = summarize(vals)
    ss = summarize(stress)
    if s["n"] < 20 or s["avg"] <= 0 or s["pf"] < 1.15:
        return None
    if ss["avg"] <= 0 or ss["pf"] < 1.05:
        return None
    # User priority: win-rate first, then profit factor / EV, then drawdown.
    return (
        1.20 * s["win_rate"]
        + 0.28 * min(s["pf"], 3.0)
        + 70.0 * s["avg"]
        - 0.08 * s["dd"]
    ), s, ss

def choose_config(asset, df):
    catalogs = {}
    for quality in ("Q1", "Q2"):
        for train_end in (2023, 2024):
            catalogs[(quality, train_end)] = build_catalog(df, train_end, quality)

    ranked = []
    for cfg in candidate_configs(asset):
        parts = []
        for y in (2024, 2025):
            rules = select_rules(catalogs[(cfg["quality"], y - 1)], cfg, y - 1)
            parts.append(evaluate(df, rules, y, cfg["quality"]))
        sc = walkforward_score(parts)
        if sc is None:
            continue
        score, agg, stress = sc
        ranked.append({
            "score": float(score),
            "config": cfg,
            "walkforward": agg,
            "walkforward_stress": stress,
            "years": {
                "2024": parts[0]["summary"],
                "2025": parts[1]["summary"],
            },
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked

def run(asset):
    btc = load("BTC")
    eth = load("ETH")
    raw = btc if asset == "BTC" else eth
    other = eth if asset == "BTC" else btc
    df = make_labels(asset, raw, other)
    print(asset, "labels", len(df), flush=True)

    ranked = choose_config(asset, df)
    out = {
        "asset": asset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Independent asset policy; recency-veto state EV; config selected on 2024-2025 anchored walk-forward only.",
        "caveat": "2026 was exposed to earlier VERITAS research; treat final 2026 as research validation, not pristine holdout.",
        "n_labels": int(len(df)),
        "selected": None,
        "alternatives": ranked[:8],
    }
    if not ranked:
        print(asset, "NO_WALKFORWARD_CONFIG_PASSED", flush=True)
        return out

    best = ranked[0]
    cfg = best["config"]
    final_catalog = build_catalog(df, 2025, cfg["quality"])
    rules = select_rules(final_catalog, cfg, 2025)
    test = evaluate(df, rules, 2026, cfg["quality"])

    out["selected"] = {
        "config": cfg,
        "walkforward": best["walkforward"],
        "walkforward_stress": best["walkforward_stress"],
        "walkforward_years": best["years"],
        "n_final_rules": len(rules),
        "final_rules": [{
            "pattern": r["pattern"],
            "key": r["key"],
            "profile": r["profile"],
            "score": r["score"],
            "conservative_ev": r["conservative_ev"],
            "total": r["total"],
            "recent": r["recent"],
            "latest": r["latest"],
        } for r in rules],
        "test_2026": test["summary"],
        "test_2026_stress_5bp": test["stress"],
        "by_direction": test["by_direction"],
        "trades": test["trades"],
    }
    print(asset, "SELECTED", json.dumps({
        "config": cfg,
        "walkforward": best["walkforward"],
        "walkforward_stress": best["walkforward_stress"],
        "n_final_rules": len(rules),
        "test_2026": test["summary"],
        "test_2026_stress_5bp": test["stress"],
        "by_direction": test["by_direction"],
    }, separators=(",", ":"), default=json_default), flush=True)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", choices=["BTC", "ETH"], required=True)
    args = ap.parse_args()
    out = run(args.asset)
    p = Path(f"asset_specific_out_{args.asset.lower()}")
    p.mkdir(exist_ok=True)
    (p / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False, default=json_default))
    print("VERITAS_ASSET_SPECIFIC=" + json.dumps({
        "asset": args.asset,
        "selected": out.get("selected"),
    }, ensure_ascii=False, separators=(",", ":"), default=json_default), flush=True)

if __name__ == "__main__":
    main()
