import json
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("profit_protection_out")
OUT.mkdir(exist_ok=True)


def _minute_to_hour(path):
    x = pd.read_pickle(path).copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    x = x.set_index("dt").sort_index()
    return x.resample("1h", label="right", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna()


def _okx_hour(path):
    x = pd.read_pickle(path).copy()
    x["dt"] = pd.to_datetime(x["ts"], unit="s", utc=True)
    x = x.set_index("dt").sort_index()
    x.index = x.index + pd.Timedelta(hours=1)
    return x[["open", "high", "low", "close"]]


def _build_baseline(btc, eth, one_way_turnover_cost):
    idx = btc.index.intersection(eth.index)
    b = btc.loc[idx].copy()
    e = eth.loc[idx].copy()

    rb = b["close"].pct_change().fillna(0.0)
    re = e["close"].pct_change().fillna(0.0)
    port_r = 0.5 * rb + 0.5 * re
    factor = (1.0 + port_r).cumprod()

    bd = b["close"].resample("1D", label="right", closed="left").last().dropna()
    ed = e["close"].resample("1D", label="right", closed="left").last().dropna()
    didx = bd.index.intersection(ed.index)
    bd, ed = bd.loc[didx], ed.loc[didx]
    br, er = bd.pct_change(), ed.pct_change()
    pr = 0.5 * br + 0.5 * er

    daily = {
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
    daily["b4"] = np.sign(b4.rolling(18).mean() - b4.rolling(50).mean())
    daily["e4"] = np.sign(e4.rolling(18).mean() - e4.rolling(50).mean())

    f = pd.DataFrame(index=idx)
    for k, s in daily.items():
        f[k] = s.reindex(idx, method="ffill")

    breadth = (f[["b7", "b28", "e7", "e28", "b4", "e4"]].sum(axis=1) / 6.0).clip(lower=0.0)
    vol_cap = (0.20 / f["vol"]).clip(upper=1.0)
    down_cap = ((0.20 / np.sqrt(2.0)) / f["down"]).clip(upper=1.0)
    signal = (breadth * vol_cap * down_cap).clip(0.0, 1.0).fillna(0.0)

    exposure = signal.shift(1).fillna(0.0)
    turnover = exposure.diff().abs().fillna(exposure.abs())
    gross = exposure * port_r
    net = gross - one_way_turnover_cost * turnover

    return pd.DataFrame(
        {
            "port_r": port_r,
            "factor": factor,
            "signal_base": signal,
            "exp_base": exposure,
            "turn_base": turnover,
            "gross_base": gross,
            "net_base": net,
        }
    )


def _profit_protection(base, one_way_turnover_cost):
    x = base.copy()
    factor = x["factor"]
    base_signal = x["signal_base"]

    f4 = factor.resample("4h", label="right", closed="left").last().dropna()
    prev = f4.shift(1)
    confirmed_swing_low = prev.where((prev < f4.shift(2)) & (prev <= f4))
    confirmed_hourly = confirmed_swing_low.reindex(x.index, method="ffill")

    out = np.zeros(len(x), dtype=float)
    prev_base = 0.0
    armed = False
    stopped = False
    entry_factor = np.nan
    floor = np.nan
    campaign_start = None
    arms = 0
    stops = 0

    for i, (t, row) in enumerate(x[["factor", "signal_base"]].iterrows()):
        b = float(row["signal_base"])
        fac = float(row["factor"])

        if b <= 1e-15:
            armed = False
            stopped = False
            entry_factor = np.nan
            floor = np.nan
            campaign_start = None
            out[i] = 0.0
        else:
            if prev_base <= 1e-15:
                entry_factor = fac
                campaign_start = t
                armed = False
                stopped = False
                floor = np.nan

            if stopped:
                out[i] = 0.0
            else:
                raw_profit = fac / entry_factor - 1.0 if entry_factor > 0 else 0.0

                # No fitted threshold: protection arms only after the sleeve has
                # earned its full round-trip turnover cost.
                if (not armed) and raw_profit >= 2.0 * one_way_turnover_cost:
                    armed = True
                    floor = entry_factor * (1.0 + 2.0 * one_way_turnover_cost)
                    arms += 1

                if armed:
                    swing = confirmed_hourly.iloc[i]
                    if pd.notna(swing) and t >= campaign_start + pd.Timedelta(hours=4):
                        floor = max(float(floor), float(swing))

                    # Decision uses only the just-completed hourly close.
                    # The exposure change is applied on the next hour.
                    if fac <= floor:
                        stopped = True
                        stops += 1
                        out[i] = 0.0
                    else:
                        out[i] = b
                else:
                    out[i] = b

        prev_base = b

    signal = pd.Series(out, index=x.index, name="signal_candidate")
    exposure = signal.shift(1).fillna(0.0)
    turnover = exposure.diff().abs().fillna(exposure.abs())
    gross = exposure * x["port_r"]
    net = gross - one_way_turnover_cost * turnover

    x["signal_candidate"] = signal
    x["exp_candidate"] = exposure
    x["turn_candidate"] = turnover
    x["gross_candidate"] = gross
    x["net_candidate"] = net
    return x, {"arms": int(arms), "stops": int(stops)}


def _metrics(x, prefix):
    r = x[f"net_{prefix}"].fillna(0.0)
    if len(r) == 0:
        return {}
    eq = (1.0 + r).cumprod()
    sd = float(r.std(ddof=0))
    sharpe = float(r.mean() / sd * np.sqrt(365.0 * 24.0)) if sd > 0 else None
    return {
        "return": float(eq.iloc[-1] - 1.0),
        "sharpe": sharpe,
        "max_drawdown": float((eq / eq.cummax() - 1.0).min()),
        "turnover": float(x[f"turn_{prefix}"].sum()),
        "avg_exposure": float(x[f"exp_{prefix}"].mean()),
    }


def _campaigns(x, prefix):
    active = x[f"exp_{prefix}"] > 1e-12
    gid = (active != active.shift(fill_value=False)).cumsum()
    vals = []
    for _, g in x[active].groupby(gid[active]):
        vals.append(float((1.0 + g[f"net_{prefix}"].fillna(0.0)).prod() - 1.0))
    if not vals:
        return {"n": 0, "win_rate": None}
    a = np.asarray(vals)
    return {
        "n": int(len(a)),
        "win_rate": float((a > 0).mean()),
        "avg_return": float(a.mean()),
        "median_return": float(np.median(a)),
        "mean_win": float(a[a > 0].mean()) if np.any(a > 0) else None,
        "mean_loss": float(a[a <= 0].mean()) if np.any(a <= 0) else None,
    }


def _slice(x, start, end=None):
    y = x[x.index >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        y = y[y.index <= pd.Timestamp(end, tz="UTC")]
    return y


def _evaluate(btc, eth, cost):
    base = _build_baseline(btc, eth, cost)
    x, events = _profit_protection(base, cost)
    return x, events


def _period_report(x, start, end=None):
    y = _slice(x, start, end)
    return {
        "baseline": _metrics(y, "base"),
        "profit_protection": _metrics(y, "candidate"),
        "baseline_campaigns": _campaigns(y, "base"),
        "profit_protection_campaigns": _campaigns(y, "candidate"),
    }


def _dataset_report(btc, eth, cost, periods):
    x, events = _evaluate(btc, eth, cost)
    out = {"events": events, "periods": {}}
    for name, start, end in periods:
        out["periods"][name] = _period_report(x, start, end)
    return out


def main():
    manager_btc = _minute_to_hour("manager_library_out/data/BTC.pkl")
    manager_eth = _minute_to_hour("manager_library_out/data/ETH.pkl")
    ext_btc = _minute_to_hour("external_holdout_out/data/BTC_2020_21.pkl")
    ext_eth = _minute_to_hour("external_holdout_out/data/ETH_2020_21.pkl")
    okx_btc = _okx_hour("okx_out/data/BTC-USDT_1h.pkl")
    okx_eth = _okx_hour("okx_out/data/ETH-USDT_1h.pkl")

    binance_periods = [
        ("2022", "2022-01-01", "2022-12-31 23:59"),
        ("2023", "2023-01-01", "2023-12-31 23:59"),
        ("2024", "2024-01-01", "2024-12-31 23:59"),
        ("2025", "2025-01-01", "2025-12-31 23:59"),
        ("2026_holdout", "2026-01-01", "2026-10-03 23:59"),
        ("2022_2026", "2022-01-01", "2026-10-03 23:59"),
    ]
    external_periods = [
        ("2020", "2020-01-01", "2020-12-31 23:59"),
        ("2021", "2021-01-01", "2021-12-31 23:59"),
    ]
    okx_periods = [
        ("2022", "2022-01-01", "2022-12-31 23:59"),
        ("2023", "2023-01-01", "2023-12-31 23:59"),
        ("2024", "2024-01-01", "2024-12-31 23:59"),
        ("2025", "2025-01-01", "2025-12-31 23:59"),
        ("2026", "2026-01-01", "2026-10-05 23:59"),
        ("2022_2026", "2022-01-01", "2026-10-05 23:59"),
    ]

    result = {
        "method": {
            "baseline": "Frozen 50/50 BTC+ETH sleeve: completed 7d/28d direction votes plus completed 4h SMA18/50 votes; positive breadth only; 20% annualized volatility cap; 28d downside-semivolatility cap at 20%/sqrt(2); no leverage.",
            "candidate": "Campaign profit protection: arm only after raw sleeve profit covers full round-trip turnover cost; then floor at max(cost-adjusted breakeven, latest causally confirmed 4h sleeve-factor swing low); on breach go to cash until the original frozen baseline campaign resets.",
            "causality": "Daily and 4h states use completed bars only. Decisions are applied one hour later. A 4h swing low is usable only after the following 4h close confirms it.",
            "selection": "Single economically motivated rule; no threshold grid or parameter search.",
        },
        "costs": {
            "base_one_way_turnover_cost": 0.00125,
            "stress_one_way_turnover_cost": 0.00250,
        },
        "base_cost": {
            "binance": _dataset_report(manager_btc, manager_eth, 0.00125, binance_periods),
            "external_2020_2021": _dataset_report(ext_btc, ext_eth, 0.00125, external_periods),
            "okx": _dataset_report(okx_btc, okx_eth, 0.00125, okx_periods),
        },
        "stress_cost": {
            "binance": _dataset_report(manager_btc, manager_eth, 0.00250, binance_periods),
            "external_2020_2021": _dataset_report(ext_btc, ext_eth, 0.00250, external_periods),
            "okx": _dataset_report(okx_btc, okx_eth, 0.00250, okx_periods),
        },
    }

    # Directional gate: a protection rule is rejected if it improves campaign
    # win-rate only by destroying most of the baseline return on both venues.
    b = result["base_cost"]["binance"]["periods"]["2022_2026"]
    o = result["base_cost"]["okx"]["periods"]["2022_2026"]
    br = b["baseline"]["return"]
    cr = b["profit_protection"]["return"]
    or0 = o["baseline"]["return"]
    oc = o["profit_protection"]["return"]
    bwr0 = b["baseline_campaigns"]["win_rate"]
    bwrc = b["profit_protection_campaigns"]["win_rate"]

    result["conclusion"] = {
        "material_rejection": bool(
            cr < 0.50 * br
            and oc < 0.50 * or0
            and bwrc is not None
            and bwr0 is not None
            and bwrc > bwr0
        ),
        "interpretation": "If rejected, the frozen sleeve's edge is right-tail dominated: hard breakeven/trailing exits convert some losers into small wins but truncate the few large campaigns that pay for the system. Do not productionize this exit.",
    }

    with open(OUT / "result.json", "w") as f:
        json.dump(result, f, indent=2)

    compact = {
        "conclusion": result["conclusion"],
        "binance": result["base_cost"]["binance"]["periods"]["2022_2026"],
        "okx": result["base_cost"]["okx"]["periods"]["2022_2026"],
        "external": result["base_cost"]["external_2020_2021"]["periods"],
        "stress_binance": result["stress_cost"]["binance"]["periods"]["2022_2026"],
        "stress_okx": result["stress_cost"]["okx"]["periods"]["2022_2026"],
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
