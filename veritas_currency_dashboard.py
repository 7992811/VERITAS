"""Unified Currency dashboard projection from the durable LIVE broker ledger.

This module is read-only. It never calls the broker, submits orders, mutates
accounting, or treats paper executions as live. When a production/sandbox
Currency allocation is bound, its durable live ledger becomes the display
authority for the Currency portfolio and trade journal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import os

import veritas_canonical_constitution as CTC

PORTFOLIO = "Currency"
ASSET = "CNYRUBF"
ALLOCATION_RUB = Decimal("10000")
SCHEMAS = {
    "production": "veritas_currency_live_production",
    "sandbox": "veritas_currency_live_sandbox",
}
ACCOUNTS = "veritas_currency_live_accounts"
FILLS = "veritas_currency_live_fills"
FEES = "veritas_currency_live_fees"
FUNDING = "veritas_currency_live_funding"
MAX_FILLS = 2000
ZERO = Decimal("0")


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _integer(value):
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _dt(value):
    if value is None:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value):
    parsed = _dt(value)
    return parsed.isoformat() if parsed else None


def enabled():
    """Read-only live dashboard overlay; independent from order/proposal switches.

    The projection only reads the durable broker ledger.  Keeping display
    visibility coupled to proposal generation made a valid live Currency book
    disappear whenever the proposal worker was disabled or restarting.
    """
    explicit = os.getenv("VERITAS_CURRENCY_DASHBOARD_LIVE_ENABLED", "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if explicit in ("1", "true", "yes", "on"):
        return True
    return (
        bool(os.getenv("TBANK_ACCOUNT_ID", "").strip())
        or os.getenv("VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED", "").strip().lower()
            in ("1", "true", "yes", "on")
        or os.getenv("VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED", "").strip().lower()
            in ("1", "true", "yes", "on")
    )


def _environment(value=None):
    env = str(value or os.getenv("VERITAS_CURRENCY_TRADE_ENVIRONMENT", "production")).strip().lower()
    return env if env in SCHEMAS else None


def _stable_id(*values):
    raw = "|".join(str(v or "") for v in values).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def _rowdict(row):
    return dict(row) if row is not None else None


def _tables_ready(connection, schema):
    row = connection.execute(
        "SELECT to_regclass(%s) AS accounts,to_regclass(%s) AS fills,"
        "to_regclass(%s) AS fees,to_regclass(%s) AS funding",
        (f"{schema}.{ACCOUNTS}", f"{schema}.{FILLS}",
         f"{schema}.{FEES}", f"{schema}.{FUNDING}"),
    ).fetchone()
    row = _rowdict(row) or {}
    return all(row.get(key) for key in ("accounts", "fills", "fees", "funding"))


def _selected_account(connection, schema, environment):
    configured = os.getenv("TBANK_ACCOUNT_ID", "").strip()
    if configured:
        row = connection.execute(
            f"SELECT account_id FROM {schema}.{ACCOUNTS} WHERE account_id=%s",
            (configured,),
        ).fetchone()
        if row:
            return configured
    console = connection.execute(
        "SELECT to_regclass('public.veritas_trade_console_config') AS config"
    ).fetchone()
    if console and console.get("config"):
        row = connection.execute(
            "SELECT account_id FROM public.veritas_trade_console_config "
            "WHERE environment=%s AND account_id IS NOT NULL",
            (environment,),
        ).fetchone()
        if row and row.get("account_id"):
            return str(row["account_id"])
    rows = connection.execute(
        f"SELECT account_id FROM {schema}.{ACCOUNTS} ORDER BY bound_at DESC LIMIT 2"
    ).fetchall()
    return str(rows[0]["account_id"]) if len(rows) == 1 else None


def _fee_rates(fills, observations):
    by_client = {}
    for fill in fills:
        client = str(fill.get("client_order_id") or "")
        lots = _integer(fill.get("lots"))
        if not client or not lots or lots <= 0:
            continue
        item = by_client.setdefault(client, {
            "lots": 0, "fill_fee": ZERO, "fill_fee_known": True,
            "observed_fee": ZERO, "covered": 0,
        })
        item["lots"] += lots
        fee = _decimal(fill.get("fee_rub"))
        if fee is None:
            item["fill_fee_known"] = False
        else:
            item["fill_fee"] += fee
    for observed in observations:
        client = str(observed.get("client_order_id") or "")
        item = by_client.get(client)
        if item is None:
            continue
        fee = _decimal(observed.get("cumulative_fee_rub"))
        covered = _integer(observed.get("filled_lots"))
        if fee is not None and fee >= 0:
            item["observed_fee"] = max(item["observed_fee"], fee)
        if covered is not None and covered >= 0:
            item["covered"] = max(item["covered"], covered)
    rates = {}
    for client, item in by_client.items():
        total = item["lots"]
        fee = None
        if item["covered"] >= total:
            fee = item["observed_fee"]
        elif item["fill_fee_known"]:
            fee = item["fill_fee"]
        rates[client] = (fee / Decimal(total)) if fee is not None and total else None
    return rates


def _new_episode(fill, direction):
    at = _dt(fill.get("executed_at"))
    metadata = {
        "action": fill.get("action"),
        "direction": fill.get("meta_direction"),
        "horizon": fill.get("horizon"),
        "stop_price": fill.get("stop_price"),
        "target_price": fill.get("target_price"),
        "exit_reason": fill.get("exit_reason"),
        "manual": fill.get("manual") is True,
        "signal_tier": fill.get("signal_tier"),
    }
    return {
        "direction": direction,
        "opened_at": at,
        "closed_at": None,
        "entry_value": ZERO,
        "entry_lots": 0,
        "entry_notional_rub": ZERO,
        "exit_value": ZERO,
        "exit_lots": 0,
        "gross_pnl_rub": ZERO,
        "fees_rub": ZERO,
        "fees_known": True,
        "funding_rub": ZERO,
        "funding_known": True,
        "max_lots": 0,
        "ids": [],
        "entry_metadata": metadata,
        "exit_metadata": {},
    }


def _add_fee(episode, rate, lots_count):
    if rate is None:
        episode["fees_known"] = False
    else:
        episode["fees_rub"] += rate * Decimal(lots_count)


def _project_episodes(account, fills, fees, funding):
    tick = _decimal(account.get("tick_size"))
    tick_value = _decimal(account.get("tick_value_rub"))
    lot_size = _integer(account.get("lot_size"))
    if tick is None or tick <= 0 or tick_value is None or tick_value <= 0 or not lot_size:
        return [], None, None
    multiplier = tick_value / tick * Decimal(lot_size)
    rates = _fee_rates(fills, fees)
    signed = 0
    average = None
    active = None
    closed = []

    for fill in sorted(fills, key=lambda x: (_dt(x.get("executed_at")) or datetime.min.replace(tzinfo=timezone.utc),
                                             str(x.get("trade_id") or ""))):
        side = str(fill.get("side") or "")
        quantity = _integer(fill.get("lots"))
        price = _decimal(fill.get("price"))
        at = _dt(fill.get("executed_at"))
        if side not in ("BUY", "SELL") or not quantity or quantity <= 0 or price is None or price <= 0 or at is None:
            continue
        delta_sign = 1 if side == "BUY" else -1
        remaining = quantity
        fee_rate = rates.get(str(fill.get("client_order_id") or ""))

        while remaining:
            if signed == 0:
                direction = "LONG" if delta_sign > 0 else "SHORT"
                active = _new_episode(fill, direction)
                active["entry_value"] += price * remaining
                active["entry_lots"] += remaining
                active["entry_notional_rub"] += price * Decimal(remaining) * multiplier
                active["max_lots"] = max(active["max_lots"], remaining)
                active["ids"].append(str(fill.get("trade_id") or ""))
                _add_fee(active, fee_rate, remaining)
                signed = delta_sign * remaining
                average = price
                remaining = 0
                continue

            if signed * delta_sign > 0:
                previous = abs(signed)
                new_abs = previous + remaining
                average = (Decimal(previous) * average + Decimal(remaining) * price) / Decimal(new_abs)
                active["entry_value"] += price * remaining
                active["entry_lots"] += remaining
                active["entry_notional_rub"] += price * Decimal(remaining) * multiplier
                active["max_lots"] = max(active["max_lots"], new_abs)
                active["ids"].append(str(fill.get("trade_id") or ""))
                _add_fee(active, fee_rate, remaining)
                signed += delta_sign * remaining
                remaining = 0
                continue

            closing = min(abs(signed), remaining)
            sign = 1 if signed > 0 else -1
            active["gross_pnl_rub"] += (
                Decimal(closing) * (price - average) * Decimal(sign) * multiplier
            )
            active["exit_value"] += price * closing
            active["exit_lots"] += closing
            active["ids"].append(str(fill.get("trade_id") or ""))
            _add_fee(active, fee_rate, closing)
            signed += delta_sign * closing
            remaining -= closing

            if signed == 0:
                active["closed_at"] = at
                active["exit_metadata"] = {
                    "action": fill.get("action"),
                    "exit_reason": fill.get("exit_reason"),
                    "manual": fill.get("manual") is True,
                    "currency_excursion_snapshot": fill.get("currency_excursion_snapshot") or {},
                }
                closed.append(active)
                active = None
                average = None

    total_funding = _decimal(account.get("funding_rub"))
    funding_rows = []
    for row in funding:
        amount = _decimal(row.get("cost_rub"))
        at = _dt(row.get("occurred_at"))
        if amount is not None and at is not None:
            funding_rows.append((at, amount))
    funding_rows_total = sum((amount for _, amount in funding_rows), ZERO)
    funding_events_complete = (
        total_funding is not None
        and ((total_funding == ZERO and not funding_rows)
             or funding_rows_total == total_funding)
    )

    episodes = closed + ([active] if active is not None else [])
    if funding_events_complete:
        for at, amount in funding_rows:
            matching = []
            for episode in episodes:
                opened = episode["opened_at"]
                ended = episode.get("closed_at")
                if opened is not None and opened <= at and (ended is None or at <= ended):
                    matching.append(episode)
            if len(matching) == 1:
                matching[0]["funding_rub"] += amount
            else:
                funding_events_complete = False
                break
    if not funding_events_complete:
        if total_funding is not None and total_funding != ZERO and len(episodes) == 1:
            episodes[0]["funding_rub"] = total_funding
        else:
            for episode in episodes:
                episode["funding_known"] = False

    return closed, active, multiplier


def _clean_episode(episode, multiplier, allocation, environment):
    entry_lots = episode["entry_lots"]
    exit_lots = episode["exit_lots"]
    entry = episode["entry_value"] / Decimal(entry_lots) if entry_lots else None
    exit_price = episode["exit_value"] / Decimal(exit_lots) if exit_lots else None
    fees = episode["fees_rub"] if episode["fees_known"] else None
    funding = episode["funding_rub"] if episode["funding_known"] else None
    exact_net = fees is not None and funding is not None
    net = episode["gross_pnl_rub"] - fees - funding if exact_net else None
    basis = episode["entry_notional_rub"] if episode["entry_notional_rub"] > 0 else None
    ret = (Decimal("100") * net / basis) if net is not None and basis else None
    opened, closed = episode["opened_at"], episode["closed_at"]
    meta, exit_meta = episode["entry_metadata"], episode["exit_metadata"]
    reason = exit_meta.get("exit_reason")
    if not reason:
        reason = "MANUAL_CLOSE" if exit_meta.get("manual") else "LIVE_CLOSE"
    excursion = exit_meta.get("currency_excursion_snapshot") or {}
    mfe_pct = _decimal(excursion.get("mfe_pct"))
    mae_pct = _decimal(excursion.get("mae_pct"))
    direction_sign = Decimal("1") if episode["direction"] == "LONG" else Decimal("-1")
    realized_move_pct = (
        Decimal("100") * direction_sign * (exit_price / entry - Decimal("1"))
        if entry is not None and exit_price is not None and entry > 0 else None
    )
    capture_ratio = None
    giveback_pct = None
    if mfe_pct is not None and mfe_pct > 0 and realized_move_pct is not None:
        capture_ratio = max(Decimal("0"), min(Decimal("1"), realized_move_pct / mfe_pct))
        giveback_pct = max(Decimal("0"), mfe_pct - max(Decimal("0"), realized_move_pct))
    mfe_threshold = Decimal("0.15")
    protection_candidate = bool(mfe_pct is not None and mfe_pct >= mfe_threshold)
    shadow_dynamic_tp = {
        "version": "CURRENCY_LIVE_MANAGEMENT_SHADOW_V1",
        "mode": "SHADOW_ONLY_NO_EXECUTION_CHANGE",
        "mfe_threshold_pct_points": float(mfe_threshold),
        "mfe_threshold_reached": protection_candidate,
        "observed_mfe_pct": float(mfe_pct) if mfe_pct is not None else None,
        "observed_mae_pct": float(mae_pct) if mae_pct is not None else None,
        "realized_move_pct": float(realized_move_pct) if realized_move_pct is not None else None,
        "capture_ratio": float(capture_ratio) if capture_ratio is not None else None,
        "giveback_pct": float(giveback_pct) if giveback_pct is not None else None,
        "profit_protection_candidate": protection_candidate,
        "dynamic_tp_candidate": bool(
            protection_candidate and giveback_pct is not None and giveback_pct >= Decimal("0.10")
        ),
        "automatic_action": False,
        "promotion_required": ["OOS", "COST_STRESS", "REGIME_STABILITY", "SUFFICIENT_SAMPLE"],
    }
    trade_id = "currency-live-" + _stable_id(*(episode["ids"] or [opened, closed]))
    return {
        "trade_id": trade_id,
        "portfolio_name": PORTFOLIO,
        "asset": ASSET,
        "direction": episode["direction"],
        "opened_at": _iso(opened),
        "closed_at": _iso(closed),
        "avg_entry_price": float(entry) if entry is not None else None,
        "avg_exit_price": float(exit_price) if exit_price is not None else None,
        "gross_pnl_rub": float(episode["gross_pnl_rub"]),
        "fees_rub": float(fees) if fees is not None else None,
        "funding_rub": float(funding) if funding is not None else None,
        "net_pnl_rub": float(net) if net is not None else None,
        "total_trade_pnl_rub": float(net) if net is not None else None,
        "trade_return_basis": "ENTRY_NOTIONAL",
        "trade_return_basis_rub": float(basis) if basis is not None else None,
        "total_trade_return_pct": float(ret) if ret is not None else None,
        "return_on_entry_nav": float(net / allocation) if net is not None and allocation > 0 else None,
        "profitable": bool(net > 0) if net is not None else None,
        "meaningful_win": bool(net > 0) if net is not None else None,
        "status": "CLOSED",
        "setup": "LIVE_BROKER",
        "horizon": meta.get("horizon"),
        "exit_reason": reason,
        "stop_price": meta.get("stop_price"),
        "take_price": meta.get("target_price"),
        "holding_duration_seconds": max(0.0, (closed-opened).total_seconds()) if opened and closed else None,
        "mfe_pct": float(mfe_pct) if mfe_pct is not None else None,
        "mae_pct": (-float(mae_pct)) if mae_pct is not None else None,
        "capture_ratio": float(capture_ratio) if capture_ratio is not None else None,
        "live_capture_ratio": float(capture_ratio) if capture_ratio is not None else None,
        "giveback_pct": float(giveback_pct) if giveback_pct is not None else None,
        "live_giveback_pct": float(giveback_pct) if giveback_pct is not None else None,
        "path_evidence_source": "CURRENCY_LIVE_DURABLE_EXCURSION",
        "opening_fraction_pct": (
            float(Decimal("100") * basis / allocation) if basis is not None and allocation > 0 else None
        ),
        "max_fraction_pct": None,
        "execution_source": "LIVE_BROKER_LEDGER",
        "execution_environment": environment,
        "payload": {
            "execution_source": "LIVE_BROKER_LEDGER",
            "execution_environment": environment,
            "trade_origin": "MANUAL" if meta.get("manual") else "MODEL",
            "signal_tier": meta.get("signal_tier"),
            "stop_price": meta.get("stop_price"),
            "target_price": meta.get("target_price"),
            "exit_reason": reason,
            "currency_excursion_snapshot": excursion,
            "currency_live_management_shadow": shadow_dynamic_tp,
            "management_evidence_status": (
                "DURABLE_LIVE_PATH" if mfe_pct is not None and mae_pct is not None
                else "INCOMPLETE_LIVE_PATH"
            ),
        },
    }


def _portfolio(account, active, closed_trades, multiplier, checked_at, environment, history_complete):
    allocation = _decimal(account.get("allocation_rub")) or ALLOCATION_RUB
    realized = _decimal(account.get("realized_pnl_rub"))
    fees = _decimal(account.get("fees_rub"))
    funding = _decimal(account.get("funding_rub"))
    high_water = _decimal(account.get("high_water_rub"))
    signed = _integer(account.get("signed_lots")) or 0
    average = _decimal(account.get("average_entry_price"))
    mark = _decimal(account.get("last_mark_price"))
    mark_at = _dt(account.get("last_mark_observed_at"))
    if realized is None or fees is None or funding is None:
        nav = None
    else:
        current_unrealized = ZERO
        if signed:
            if mark is None or average is None or multiplier is None:
                current_unrealized = None
            else:
                current_unrealized = (mark-average) * Decimal(signed) * multiplier
        nav = None if current_unrealized is None else allocation + realized - fees - funding + current_unrealized
    unrealized = None
    if signed and mark is not None and average is not None and multiplier is not None:
        unrealized = (mark-average) * Decimal(signed) * multiplier
    elif not signed:
        unrealized = ZERO

    gross = None
    if nav is not None and nav != 0 and mark is not None and multiplier is not None:
        gross = abs(Decimal(signed) * multiplier * mark) / abs(nav)
    elif not signed and nav is not None:
        gross = ZERO

    position = None
    if signed:
        held = {
            "stop_price": account.get("held_stop_price"),
            "target_price": account.get("held_target_price"),
            "horizon": account.get("held_horizon"),
            "opened_at": account.get("held_opened_at"),
        }
        direction = "LONG" if signed > 0 else "SHORT"
        units = abs(Decimal(signed) * multiplier) if multiplier is not None else None
        notional = abs(units * mark) if units is not None and mark is not None else None
        entry_notional = active.get("entry_notional_rub") if active else (
            abs(units * average) if units is not None and average is not None else None
        )
        active_fees = active.get("fees_rub") if active and active.get("fees_known") else None
        active_funding = active.get("funding_rub") if active and active.get("funding_known") else None
        active_realized = active.get("gross_pnl_rub") if active else ZERO
        current_total = None
        if unrealized is not None and active_fees is not None and active_funding is not None:
            current_total = active_realized + unrealized - active_fees - active_funding
        move_pct = (
            Decimal("100") * Decimal(1 if signed > 0 else -1) * (mark/average - 1)
            if mark is not None and average is not None and average > 0 else None
        )
        excursion_entry = _decimal(account.get("excursion_entry_price"))
        excursion_mfe = _decimal(account.get("mfe_price"))
        excursion_mae = _decimal(account.get("mae_price"))
        excursion_direction = str(account.get("excursion_direction") or "")
        mfe_pct = mae_pct = giveback_pct = None
        if (excursion_entry is not None and excursion_entry > 0
                and excursion_mfe is not None and excursion_mae is not None
                and excursion_direction in ("LONG","SHORT")):
            if excursion_direction == "LONG":
                mfe_pct = max(ZERO, Decimal("100")*(excursion_mfe-excursion_entry)/excursion_entry)
                mae_pct = max(ZERO, Decimal("100")*(excursion_entry-excursion_mae)/excursion_entry)
            else:
                mfe_pct = max(ZERO, Decimal("100")*(excursion_entry-excursion_mfe)/excursion_entry)
                mae_pct = max(ZERO, Decimal("100")*(excursion_mae-excursion_entry)/excursion_entry)
            if move_pct is not None:
                giveback_pct = max(ZERO, mfe_pct-max(ZERO, move_pct))
        management_shadow = {
            "version":"CURRENCY_LIVE_MANAGEMENT_SHADOW_V1",
            "mode":"SHADOW_ONLY_NO_EXECUTION_CHANGE",
            "mfe_threshold_pct_points":0.15,
            "mfe_threshold_reached":bool(mfe_pct is not None and mfe_pct >= Decimal("0.15")),
            "mfe_pct":float(mfe_pct) if mfe_pct is not None else None,
            "mae_pct":float(mae_pct) if mae_pct is not None else None,
            "current_move_pct":float(move_pct) if move_pct is not None else None,
            "giveback_pct":float(giveback_pct) if giveback_pct is not None else None,
            "profit_protection_candidate":bool(mfe_pct is not None and mfe_pct >= Decimal("0.15")),
            "dynamic_tp_review_candidate":bool(
                mfe_pct is not None and mfe_pct >= Decimal("0.15")
                and giveback_pct is not None and giveback_pct >= Decimal("0.10")
            ),
            "automatic_action":False,
        }
        mark_age = None
        checked_dt = _dt(checked_at)
        if mark_at is not None and checked_dt is not None:
            mark_age = max(0.0, (checked_dt-mark_at).total_seconds())
        price_status = "OK" if mark_age is not None and mark_age <= 60 else (
            "STALE_REPORTED_MARK" if mark is not None else "UNAVAILABLE"
        )
        opened_at = held["opened_at"] or (_iso(active.get("opened_at")) if active else None)
        position = {
            "portfolio_name": PORTFOLIO,
            "asset": ASSET,
            "direction": direction,
            "lots": abs(signed),
            "units": float(units) if units is not None else None,
            "avg_entry_price": float(average) if average is not None else None,
            "last_price": float(mark) if mark is not None else None,
            "opened_at": opened_at,
            "updated_at": _iso(mark_at or account.get("last_execution_at")),
            "stop_price": held["stop_price"],
            "take_price": held["target_price"],
            "target_price": held["target_price"],
            "target_fraction": float(gross) if gross is not None else None,
            "notional_rub": float(notional) if notional is not None else None,
            "unrealized_pnl_rub": float(unrealized) if unrealized is not None else None,
            "unrealized_return_pct": float(move_pct) if move_pct is not None else None,
            "mfe_pct": float(mfe_pct) if mfe_pct is not None else None,
            "mae_pct": (-float(mae_pct)) if mae_pct is not None else None,
            "live_giveback_pct": float(giveback_pct) if giveback_pct is not None else None,
            "management_evidence_status": (
                "DURABLE_LIVE_PATH" if mfe_pct is not None and mae_pct is not None
                else "INCOMPLETE_LIVE_PATH"
            ),
            "currency_live_management_shadow": management_shadow,
            "realized_gross_pnl_rub": float(active_realized),
            "trade_fees_rub": float(active_fees) if active_fees is not None else None,
            "trade_funding_rub": float(active_funding) if active_funding is not None else None,
            "total_trade_pnl_rub": float(current_total) if current_total is not None else None,
            "trade_return_basis": "ENTRY_NOTIONAL",
            "trade_return_basis_rub": float(entry_notional) if entry_notional is not None else None,
            "total_trade_return_pct": (
                float(Decimal("100")*current_total/entry_notional)
                if current_total is not None and entry_notional else None
            ),
            "execution_timeframe": held["horizon"] or (active or {}).get("entry_metadata", {}).get("horizon"),
            "horizon": held["horizon"] or (active or {}).get("entry_metadata", {}).get("horizon"),
            "position_source": "LIVE_BROKER_LEDGER",
            "execution_source": "LIVE_BROKER_LEDGER",
            "execution_environment": environment,
            "price_source_status": price_status,
            "last_mark_at": _iso(mark_at),
            "active_trade_id": "currency-live-open-" + _stable_id(opened_at, direction),
            "max_position_fraction": 10.0,
            "position_utilization_pct": float(Decimal("10")*gross) if gross is not None else None,
            "learning_focus": "ЗАЩИТА_ПРИБЫЛИ" if current_total is not None and current_total > 0 else "УДЕРЖАНИЕ_ДВИЖЕНИЯ",
            "payload": {
                "portfolio": PORTFOLIO,
                "asset": ASSET,
                "execution_source": "LIVE_BROKER_LEDGER",
                "execution_environment": environment,
                "stop_price": held["stop_price"],
                "target_price": held["target_price"],
                "execution_timeframe": held["horizon"],
                "currency_live_management_shadow": management_shadow,
                "mfe_pct": float(mfe_pct) if mfe_pct is not None else None,
                "mae_pct": (-float(mae_pct)) if mae_pct is not None else None,
                "live_giveback_pct": float(giveback_pct) if giveback_pct is not None else None,
            },
        }

    exact_closed = [t for t in closed_trades if t.get("net_pnl_rub") is not None]
    all_closed_exact = history_complete and len(exact_closed) == len(closed_trades)
    if not signed and nav is not None:
        closed_pnl = float(realized - fees - funding)
    elif all_closed_exact:
        closed_pnl = sum(float(t["net_pnl_rub"]) for t in exact_closed)
    else:
        closed_pnl = None
    wins = sum(1 for t in exact_closed if float(t["net_pnl_rub"]) > 0)
    drawdown = (
        Decimal("100") * max(ZERO, Decimal("1") - nav/high_water)
        if nav is not None and high_water is not None and high_water > 0 else None
    )
    risk_profile = CTC.currency_live_risk_policy(
        float(drawdown / Decimal("100")) if drawdown is not None else 0.0)
    return {
        "name": PORTFOLIO,
        "display_name": PORTFOLIO,
        "configuration_status": "CONFIGURED",
        "allowed_assets": [ASSET],
        "paper_trading_enabled": True,
        "live_trading_enabled": True,
        "execution_mode": "LIVE",
        "display_accounting_source": "LIVE_BROKER_LEDGER",
        "initial_nav_rub": float(allocation),
        "nav_rub": float(nav) if nav is not None else None,
        "total_return_pct": float(Decimal("100")*(nav/allocation-1)) if nav is not None and allocation > 0 else None,
        "drawdown_pct": float(drawdown) if drawdown is not None else None,
        "gross_leverage": float(gross) if gross is not None else None,
        "net_exposure": float(gross * Decimal(1 if signed > 0 else -1)) if gross is not None and signed else 0.0 if gross is not None else None,
        "cash_equivalent_fraction": max(0.0, 1.0-float(gross)) if gross is not None else None,
        "unrealized_pnl_rub": float(unrealized) if unrealized is not None else None,
        "realized_pnl_rub": float(realized) if realized is not None else None,
        "fees_rub": float(fees) if fees is not None else None,
        "funding_rub": float(funding) if funding is not None else None,
        "high_water_nav_rub": float(high_water) if high_water is not None else None,
        "positions": [position] if position else [],
        "positions_status": "COMPLETE",
        "positions_checked_at": _iso(checked_at),
        "positions_reason": None,
        "accounting_status": "COMPLETE" if nav is not None else "UNAVAILABLE",
        "accounting_source": "LIVE_BROKER_LEDGER",
        "closed_trades": len(closed_trades) if history_complete else None,
        "wins": wins if all_closed_exact else None,
        "win_rate": (wins/len(closed_trades)) if all_closed_exact and closed_trades else None,
        "closed_trade_pnl_rub": closed_pnl,
        "live_history_complete": history_complete,
        "live_costs_reconciled": account.get("costs_reconciled") is True,
        "live_ledger_revision": _integer(account.get("ledger_revision")),
        "live_reconciled": (
            account.get("reconciled_revision") is not None
            and _integer(account.get("reconciled_revision")) == _integer(account.get("ledger_revision"))
            and _integer(account.get("broker_signed_lots")) == signed
        ),
        "live_broker_observed_at": _iso(account.get("broker_observed_at")),
        "weekend_carry_allowed": True,
        "max_gross_limit": 10.0,
        "leverage_limit": 10.0,
        "hard_drawdown_limit_pct": 100.0 * float(risk_profile["hard_drawdown_stop"]),
        "risk_governor": {
            "state": risk_profile["state"],
            "new_risk": risk_profile["state"] != "HARD_STOP",
            "max_gross": risk_profile["max_gross"],
            "hard_drawdown_limit": risk_profile["hard_drawdown_stop"],
            "size_multiplier": risk_profile["size_multiplier"],
            "max_stop_risk_nav": risk_profile["max_stop_risk_nav"],
            "profile": "CURRENCY",
        },
        "live_risk_profile": risk_profile,
    }


def project_live_currency(account, fills, fees=(), funding=(), *, checked_at=None,
                          environment="production", history_complete=True):
    """Pure projection used by production reads and regression tests."""
    account = dict(account or {})
    environment = _environment(environment)
    if not environment:
        raise ValueError("INVALID_CURRENCY_ENVIRONMENT")
    checked_at = _dt(checked_at) or datetime.now(timezone.utc)
    closed, active, multiplier = _project_episodes(account, list(fills or []),
                                                    list(fees or []), list(funding or []))
    allocation = _decimal(account.get("allocation_rub")) or ALLOCATION_RUB
    trades = [_clean_episode(ep, multiplier, allocation, environment) for ep in closed]
    portfolio = _portfolio(account, active, trades, multiplier, checked_at, environment,
                           bool(history_complete))
    return {
        "bound": True,
        "environment": environment,
        "history_complete": bool(history_complete),
        "portfolio": portfolio,
        "trades": trades,
    }


def read_live_currency_on(connection, *, environment=None, checked_at=None, max_fills=MAX_FILLS):
    """Read one exact durable Currency allocation from an existing DB transaction."""
    environment = _environment(environment)
    if not environment:
        return {"bound": False, "status": "INVALID_ENVIRONMENT", "trades": []}
    schema = SCHEMAS[environment]
    if not _tables_ready(connection, schema):
        return {"bound": False, "status": "LIVE_LEDGER_NOT_INITIALIZED", "trades": []}
    account_id = _selected_account(connection, schema, environment)
    if not account_id:
        return {"bound": False, "status": "LIVE_ACCOUNT_NOT_UNIQUELY_BOUND", "trades": []}
    account = _rowdict(connection.execute(
        f"""SELECT account_id,instrument_uid,allocation_rub,tick_size,tick_value_rub,lot_size,
                   signed_lots,average_entry_price,realized_pnl_rub,fees_rub,funding_rub,
                   high_water_rub,costs_reconciled,ledger_revision,reconciled_revision,
                   broker_signed_lots,broker_observed_at,last_execution_at,last_mark_price,
                   last_mark_observed_at,bound_at,
                   excursion_entry_price,excursion_direction,mfe_price,mae_price,excursion_started_at,
                   held_terms->>'stop_price' AS held_stop_price,
                   held_terms->>'target_price' AS held_target_price,
                   held_terms->>'horizon' AS held_horizon,
                   held_terms->>'opened_at' AS held_opened_at
            FROM {schema}.{ACCOUNTS} WHERE account_id=%s""",
        (account_id,),
    ).fetchone())
    if not account:
        return {"bound": False, "status": "LIVE_ACCOUNT_NOT_FOUND", "trades": []}
    count_row = connection.execute(
        f"SELECT COUNT(*) AS n FROM {schema}.{FILLS} WHERE account_id=%s AND instrument_uid=%s",
        (account_id, account["instrument_uid"]),
    ).fetchone()
    fill_count = int((count_row or {}).get("n") or 0)
    history_complete = fill_count <= max(1, int(max_fills))
    fills, fee_rows, funding_rows = [], [], []
    if history_complete:
        fills = [dict(row) for row in connection.execute(
            f"""SELECT trade_id,client_order_id,side,lots,price,fee_rub,executed_at,
                       metadata->>'action' AS action,
                       metadata->>'direction' AS meta_direction,
                       metadata->>'horizon' AS horizon,
                       metadata->>'stop_price' AS stop_price,
                       metadata->>'target_price' AS target_price,
                       metadata->>'exit_reason' AS exit_reason,
                       (metadata ? 'manual_request' OR metadata ? 'manual_owner_user_id') AS manual,
                       COALESCE(metadata->>'entry_signal_tier',metadata->>'signal_tier') AS signal_tier,
                       metadata->'currency_excursion_snapshot' AS currency_excursion_snapshot
                FROM {schema}.{FILLS}
                WHERE account_id=%s AND instrument_uid=%s
                ORDER BY executed_at,trade_id""",
            (account_id, account["instrument_uid"]),
        ).fetchall()]
        fee_rows = [dict(row) for row in connection.execute(
            f"""SELECT client_order_id,MAX(cumulative_fee_rub) AS cumulative_fee_rub,
                       MAX(filled_lots) AS filled_lots
                FROM {schema}.{FEES}
                WHERE account_id=%s AND instrument_uid=%s
                GROUP BY client_order_id""",
            (account_id, account["instrument_uid"]),
        ).fetchall()]
        funding_rows = [dict(row) for row in connection.execute(
            f"""SELECT cost_rub,occurred_at FROM {schema}.{FUNDING}
                WHERE account_id=%s AND instrument_uid=%s ORDER BY occurred_at""",
            (account_id, account["instrument_uid"]),
        ).fetchall()]
    result = project_live_currency(
        account, fills, fee_rows, funding_rows,
        checked_at=checked_at or datetime.now(timezone.utc),
        environment=environment, history_complete=history_complete,
    )
    result.update(status="OK", fill_count=fill_count)
    return result


def overlay_portfolio(report, live):
    """Replace only Currency display accounting when a live allocation is bound."""
    if not isinstance(live, dict) or live.get("bound") is not True:
        return report
    result = dict(report or {})
    portfolio = dict(live["portfolio"])
    rows, replaced = [], False
    for original in result.get("portfolios") or []:
        if isinstance(original, dict) and original.get("name") == PORTFOLIO:
            row = dict(original)
            admission = row.get("admission_trace")
            current = row.get("current_cny_admission")
            row.update(portfolio)
            if admission is not None:
                row["admission_trace"] = admission
            if current is not None:
                row["current_cny_admission"] = current
            rows.append(row)
            replaced = True
        else:
            rows.append(original)
    if not replaced:
        rows.append(portfolio)
    result["portfolios"] = rows
    result["currency_display_source"] = "LIVE_BROKER_LEDGER"
    if portfolio.get("positions_status") != "COMPLETE":
        result["positions_complete"] = False
    if portfolio.get("accounting_status") != "COMPLETE":
        result["accounting_complete"] = False
        result["status"] = "PARTIAL"
    return result


def merge_trade_history(paper_trades, live, *, limit=80):
    """Prefer real Currency round trips; keep other portfolios' paper history."""
    rows = [dict(row) for row in (paper_trades or [])]
    if not isinstance(live, dict) or live.get("bound") is not True:
        return rows[:limit]
    rows = [row for row in rows if row.get("portfolio_name") != PORTFOLIO]
    rows.extend(dict(row) for row in live.get("trades") or [])
    rows.sort(key=lambda row: _dt(row.get("closed_at") or row.get("opened_at"))
              or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows[:max(1, int(limit))]
