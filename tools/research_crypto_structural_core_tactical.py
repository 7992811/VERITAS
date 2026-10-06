import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def minute_to_hour(path):
    x = pd.read_pickle(path).copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    x = x.set_index("dt").sort_index()
    return x.resample("1h", label="right", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna()


def okx_hour(path):
    x = pd.read_pickle(path).copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    x = x.set_index("dt").sort_index()
    x.index = x.index + pd.Timedelta(hours=1)
    return x[["open", "high", "low", "close"]]


def components(btc, eth):
    idx = btc.index.intersection(eth.index)
    b = btc.loc[idx]
    e = eth.loc[idx]
    rb = b["close"].pct_change().fillna(0.0)
    re = e["close"].pct_change().fillna(0.0)
    port_r = 0.5 * rb + 0.5 * re

    bd = b["close"].resample("1D", label="right", closed="left").last().dropna()
    ed = e["close"].resample("1D", label="right", closed="left").last().dropna()
    didx = bd.index.intersection(ed.index)
    bd, ed = bd.loc[didx], ed.loc[didx]
    pr = 0.5 * bd.pct_change() + 0.5 * ed.pct_change()

    s = {
        "b7": np.sign(bd.pct_change(7)),
        "b28": np.sign(bd.pct_change(28)),
        "e7": np.sign(ed.pct_change(7)),
        "e28": np.sign(ed.pct_change(28)),
        "vol": pr.rolling(28, min_periods=20).std(ddof=0) * np.sqrt(365.0),
        "down": np.sqrt(
            (pr.clip(upper=0.0) ** 2).rolling(28, min_periods=20).mean()
        ) * np.sqrt(365.0),
    }

    b4 = b["close"].resample("4h", label="right", closed="left").last().dropna()
    e4 = e["close"].resample("4h", label="right", closed="left").last().dropna()
    s["b4"] = np.sign(b4.rolling(18).mean() - b4.rolling(50).mean())
    s["e4"] = np.sign(e4.rolling(18).mean() - e4.rolling(50).mean())

    f = pd.DataFrame(index=idx)
    for k, v in s.items():
        f[k] = v.reindex(idx, method="ffill")

    breadth = (
        f[["b7", "b28", "e7", "e28", "b4", "e4"]].sum(axis=1) / 6.0
    ).clip(lower=0.0)
    vol_cap = (0.20 / f["vol"]).clip(upper=1.0)
    down_cap = ((0.20 / np.sqrt(2.0)) / f["down"]).clip(upper=1.0)
    risk_cap = (vol_cap * down_cap).clip(0.0, 1.0).fillna(0.0)
    base = (breadth * risk_cap).clip(0.0, 1.0).fillna(0.0)

    return f, pd.DataFrame(
        {"port_r": port_r, "risk_cap": risk_cap, "base_signal": base},
        index=idx,
    )


def candidate_from_pair(f, c, pair):
    # No fitted threshold. Two existing votes account for 2/6 of the frozen
    # breadth score. If both are positive, retain that exact structural share.
    state = ((f[pair[0]] > 0) & (f[pair[1]] > 0)).astype(float)
    floor = state * c["risk_cap"] * (2.0 / 6.0)
    out = c.copy()
    out["state"] = state
    out["candidate_signal"] = np.maximum(c["base_signal"], floor)
    return out


def apply_cost(x, one_way_turnover_cost):
    y = x.copy()
    for label, col in (("base", "base_signal"), ("candidate", "candidate_signal")):
        exp = y[col].shift(1).fillna(0.0)
        turn = exp.diff().abs().fillna(exp.abs())
        gross = exp * y["port_r"]
        net = gross - one_way_turnover_cost * turn
        y[f"exp_{label}"] = exp
        y[f"turn_{label}"] = turn
        y[f"net_{label}"] = net
    return y


def metrics(x, label):
    r = x[f"net_{label}"].fillna(0.0)
    eq = (1.0 + r).cumprod()
    sd = float(r.std(ddof=0))
    return {
        "return": float(eq.iloc[-1] - 1.0),
        "sharpe": float(r.mean() / sd * np.sqrt(365.0 * 24.0)) if sd > 0 else None,
        "max_drawdown": float((eq / eq.cummax() - 1.0).min()),
        "turnover": float(x[f"turn_{label}"].sum()),
        "avg_exposure": float(x[f"exp_{label}"].mean()),
    }


def campaigns(x, label):
    active = x[f"exp_{label}"] > 1e-12
    gid = (active != active.shift(fill_value=False)).cumsum()
    vals = []
    for _, g in x[active].groupby(gid[active]):
        vals.append(float((1.0 + g[f"net_{label}"].fillna(0.0)).prod() - 1.0))
    if not vals:
        return {"n": 0, "win_rate": None}
    a = np.asarray(vals)
    return {
        "n": int(len(a)),
        "win_rate": float((a > 0).mean()),
        "mean_return": float(a.mean()),
        "median_return": float(np.median(a)),
    }


def sl(x, start, end):
    return x[(x.index >= pd.Timestamp(start, tz="UTC")) &
             (x.index <= pd.Timestamp(end, tz="UTC"))]


def period_report(x, periods):
    out = {}
    for name, start, end in periods:
        z = sl(x, start, end)
        out[name] = {
            "base": metrics(z, "base"),
            "candidate": metrics(z, "candidate"),
            "base_campaigns": campaigns(z, "base"),
            "candidate_campaigns": campaigns(z, "candidate"),
        }
    return out


def shifted_control(f, c, start, end, cost):
    state = ((f["b28"] > 0) & (f["e28"] > 0)).astype(float).values
    base = c["base_signal"].values
    risk = c["risk_cap"].values
    pr = c["port_r"].values
    idx = c.index
    mask = ((idx >= pd.Timestamp(start, tz="UTC")) &
            (idx <= pd.Timestamp(end, tz="UTC")))

    def one(st):
        sig = np.maximum(base, st * risk / 3.0)
        exp = np.r_[0.0, sig[:-1]]
        turn = np.abs(np.diff(np.r_[0.0, exp]))
        net = exp * pr - cost * turn
        r = net[mask]
        ret = float(np.exp(np.log1p(r).sum()) - 1.0)
        sd = float(r.std())
        sh = float(r.mean() / sd * np.sqrt(365.0 * 24.0)) if sd > 0 else None
        return ret, sh, float(turn[mask].sum())

    observed = one(state)
    full_days = len(state) // 24
    shifts = np.arange(1, full_days)
    if len(shifts) > 3000:
        shifts = np.unique(np.linspace(1, full_days - 1, 3000).astype(int))
    ctrl = np.asarray([one(np.roll(state, 24 * int(k))) for k in shifts], dtype=float)

    base_x = c.copy()
    base_x["candidate_signal"] = c["base_signal"]
    base_x = apply_cost(base_x, cost)
    bz = sl(base_x, start, end)
    b = metrics(bz, "base")
    dret = observed[0] - b["return"]
    dsh = observed[1] - b["sharpe"]
    return {
        "observed": {"return": observed[0], "sharpe": observed[1], "turnover": observed[2]},
        "base": b,
        "n_shifts": int(len(shifts)),
        "return_percentile": float((ctrl[:, 0] < observed[0]).mean()),
        "sharpe_percentile": float((ctrl[:, 1] < observed[1]).mean()),
        "p_return_improvement": float((1 + np.sum((ctrl[:, 0] - b["return"]) >= dret)) / (len(ctrl) + 1)),
        "p_sharpe_improvement": float((1 + np.sum((ctrl[:, 1] - b["sharpe"]) >= dsh)) / (len(ctrl) + 1)),
    }


def dataset(btc, eth, cost, periods):
    f, c = components(btc, eth)
    result = {}
    for name, pair in {
        "structural_28d_floor": ("b28", "e28"),
        "short_7d_floor_control": ("b7", "e7"),
        "short_4h_floor_control": ("b4", "e4"),
    }.items():
        result[name] = period_report(apply_cost(candidate_from_pair(f, c, pair), cost), periods)
    return f, c, result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manager", default="manager_library_out")
    ap.add_argument("--external", default="external_holdout_out")
    ap.add_argument("--okx", default="okx_out")
    ap.add_argument("--out", default="structural_core_tactical_out")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(exist_ok=True)

    mb = minute_to_hour(Path(args.manager) / "data/BTC.pkl")
    me = minute_to_hour(Path(args.manager) / "data/ETH.pkl")
    eb = minute_to_hour(Path(args.external) / "data/BTC_2020_21.pkl")
    ee = minute_to_hour(Path(args.external) / "data/ETH_2020_21.pkl")
    ob = okx_hour(Path(args.okx) / "data/BTC-USDT_1h.pkl")
    oe = okx_hour(Path(args.okx) / "data/ETH-USDT_1h.pkl")

    bp = [
        ("2022", "2022-01-01", "2022-12-31 23:59"),
        ("2023", "2023-01-01", "2023-12-31 23:59"),
        ("2024", "2024-01-01", "2024-12-31 23:59"),
        ("2025", "2025-01-01", "2025-12-31 23:59"),
        ("2026", "2026-01-01", "2026-10-03 23:59"),
        ("2022_2026", "2022-01-01", "2026-10-03 23:59"),
    ]
    ep = [
        ("2020", "2020-01-01", "2020-12-31 23:59"),
        ("2021", "2021-01-01", "2021-12-31 23:59"),
    ]
    op = [
        ("2022", "2022-01-01", "2022-12-31 23:59"),
        ("2023", "2023-01-01", "2023-12-31 23:59"),
        ("2024", "2024-01-01", "2024-12-31 23:59"),
        ("2025", "2025-01-01", "2025-12-31 23:59"),
        ("2026", "2026-01-01", "2026-10-05 19:59"),
        ("2022_2026", "2022-01-01", "2026-10-05 19:59"),
    ]

    result = {
        "method": {
            "baseline": "Frozen 50/50 BTC+ETH sleeve with completed 7d/28d direction votes, completed 4h SMA18/50 votes, 20% volatility cap and 28d downside-semivolatility cap.",
            "candidate": "Retain a structural 28d core equal to the exact 2/6 contribution of the two existing 28d votes when both BTC and ETH 28d votes are positive. Shorter 7d/4h context may remove only the tactical remainder, not this core.",
            "selection": "No fitted thresholds and no parameter grid. 7d and 4h floors are negative controls using the same 2/6 accounting.",
            "causality": "Completed bars only; exposure changes are applied one hour after the signal state.",
        },
        "costs": {"base": 0.00125, "stress": 0.00250},
    }

    for tag, cost in (("base_cost", 0.00125), ("stress_cost", 0.00250)):
        bf, bc, br = dataset(mb, me, cost, bp)
        ef, ec, er = dataset(eb, ee, cost, ep)
        of, oc, orr = dataset(ob, oe, cost, op)
        result[tag] = {"binance": br, "external": er, "okx": orr}
        if tag == "base_cost":
            result["circular_shift_control"] = {
                "binance": shifted_control(bf, bc, "2022-01-01", "2026-10-03 23:59", cost),
                "external": shifted_control(ef, ec, "2020-01-01", "2021-12-31 23:59", cost),
                "okx": shifted_control(of, oc, "2022-01-01", "2026-10-05 19:59", cost),
            }

    # Research gate: aggregate improvement alone is insufficient. The floor is
    # rejected as an incremental rule if it fails timing controls and degrades
    # at least one major validation year on both venues.
    b24 = result["base_cost"]["binance"]["structural_28d_floor"]["2024"]
    o24 = result["base_cost"]["okx"]["structural_28d_floor"]["2024"]
    cb = result["circular_shift_control"]["binance"]
    co = result["circular_shift_control"]["okx"]
    rejected = (
        b24["candidate"]["return"] < b24["base"]["return"]
        and o24["candidate"]["return"] < o24["base"]["return"]
        and cb["p_sharpe_improvement"] > 0.05
        and co["p_sharpe_improvement"] > 0.05
    )
    result["conclusion"] = {
        "material_rejection": bool(rejected),
        "interpretation": (
            "Do not hard-code a minimum structural core floor. The 28d floor can "
            "improve aggregate return and turnover, but the incremental timing edge "
            "is not distinguished from circular-shift controls and it degrades 2024 "
            "on both venues. Preserve the frozen breadth/downside baseline instead."
        ),
    }

    with open(outdir / "result.json", "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result["conclusion"], indent=2))


if __name__ == "__main__":
    main()
