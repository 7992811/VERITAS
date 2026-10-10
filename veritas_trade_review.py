"""Compact closed-trade review helpers; no market I/O and no trading authority."""
from __future__ import annotations
import json, math
from datetime import datetime, timezone, timedelta
import veritas_trade_diagnostics as VTD

CLOSED_EXTRA_FIELDS=(
    "initial_stop_price","exit_position_stop_price","exit_trailing_stop_price",
    "exit_effective_stop_price","exit_effective_stop_reason","add_count","add_fee_rub",
    "last_add_fee_rub","last_add_at","last_add_price","mfe_before_last_add_pct",
    "mfe_since_last_add_pct","mae_since_last_add_pct","initial_entry_price",
    "initial_entry_units","initial_entry_fee_rub","initial_tranche_final_exit_gross_rub",
    "initial_tranche_final_exit_net_proxy_rub","initial_tranche_counterfactual_basis",
)

def _num(value,default=0.0):
    try:
        value=float(value)
        return value if math.isfinite(value) else default
    except (TypeError,ValueError,OverflowError):
        return default

def exit_stop_patch(position,payload,reason,effective_stop):
    p=payload if isinstance(payload,dict) else {}
    return {"initial_stop_price":p.get("initial_stop_price"),
            "exit_position_stop_price":(position or {}).get("stop_price"),
            "exit_trailing_stop_price":p.get("trailing_stop"),
            "exit_effective_stop_price":effective_stop,
            "exit_effective_stop_reason":str(reason)}

def initial_tranche_counterfactual(payload,fill_price,sign,commission):
    p=payload if isinstance(payload,dict) else {}
    entry=_num(p.get("initial_entry_price")); units=abs(_num(p.get("initial_entry_units")))
    fee=max(0.0,_num(p.get("initial_entry_fee_rub"))); fill=_num(fill_price); rate=max(0.0,_num(commission))
    if entry<=0 or units<=0 or fill<=0:
        return {}
    gross=float(sign)*units*(fill-entry)
    return {"initial_tranche_final_exit_gross_rub":gross,
            "initial_tranche_final_exit_net_proxy_rub":gross-fee-units*fill*rate,
            "initial_tranche_counterfactual_basis":"INITIAL_TRANCHE_HELD_TO_FINAL_EXIT_NO_ADDS_PROXY"}

def lifetime_mfe(payload):
    p=payload if isinstance(payload,dict) else {}
    for key in ("r55_lifetime_mfe_pct","r_accel_mfe_pct","mfe_pct"):
        if p.get(key) is not None:
            return p.get(key)
    return None

def closed_trade_fields(payload):
    p=payload if isinstance(payload,dict) else {}
    return {field:p.get(field) for field in CLOSED_EXTRA_FIELDS}


def json_object(value):
    if isinstance(value,dict):
        return dict(value)
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def finite_number(value,default=None):
    try:
        if value is None:
            return default
        parsed=float(value)
        return parsed if math.isfinite(parsed) else default
    except Exception:
        return default


def iso_value(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value,'isoformat') else str(value)


def msk_date(value):
    if value is None:
        return None
    try:
        if isinstance(value,str):
            value=datetime.fromisoformat(value.replace('Z','+00:00'))
        if value.tzinfo is None:
            value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone(timedelta(hours=3))).date()
    except Exception:
        return None


def episode_key(trade,payload):
    p=payload or {}
    key=p.get('canonical_setup_id') or p.get('setup_id') or p.get('canonical_trade_id')
    if key:
        return str(key)
    opened=(trade or {}).get('opened_at')
    try:
        if isinstance(opened,str):
            opened=datetime.fromisoformat(opened.replace('Z','+00:00'))
        if opened and opened.tzinfo is None:
            opened=opened.replace(tzinfo=timezone.utc)
        bucket=opened.astimezone(timezone.utc).replace(second=0,microsecond=0).isoformat() if opened else 'UNKNOWN'
    except Exception:
        bucket=str(opened or 'UNKNOWN')[:16]
    return '|'.join(str(v or '—') for v in
                    ((trade or {}).get('asset'),(trade or {}).get('direction'),
                     (trade or {}).get('horizon'),(trade or {}).get('setup'),bucket))


def learning_label(net,price_return,mfe,mae,giveback,exit_reason,recovered=False):
    if recovered:
        return 'RECOVERED_HISTORICAL_NO_LEARNING'
    value=finite_number(net)
    if value is None:
        return 'UNVERIFIED_TRADE_EVIDENCE'
    return ('PROFIT_OBSERVED_RULES_UNVERIFIED' if value>0 else
            'LOSS_OBSERVED_RULES_UNVERIFIED' if value<0 else 'FLAT_OBSERVED_RULES_UNVERIFIED')


def learning_conclusion(label,trade):
    if label in ('UNVERIFIED_TRADE_EVIDENCE','PROFIT_OBSERVED_RULES_UNVERIFIED',
                 'LOSS_OBSERVED_RULES_UNVERIFIED','FLAT_OBSERVED_RULES_UNVERIFIED'):
        return VTD.conclusion({'status':'UNVERIFIED'})
    if label in ('VALID_STRUCTURAL_STOP_LOSS','VALID_LOSING_TRADE','VALID_PROFITABLE_TRADE','VALID_FLAT_TRADE'):
        return VTD.conclusion({'status':'VERIFIED_RULE_OUTCOME',
                              'outcome':'LOSS' if label in ('VALID_STRUCTURAL_STOP_LOSS','VALID_LOSING_TRADE') else 'PROFIT'})
    if label in ('PROVEN_ENTRY_RULE_VIOLATION','PROVEN_STOP_OR_ATR_RULE_VIOLATION'):
        return VTD.conclusion({'status':'RULE_VIOLATION'})
    t=trade or {}
    setup=str(t.get('setup') or 'setup'); regime=str(t.get('regime') or 'regime')
    if label=='RIGHT_DIRECTION_HIGH_CAPTURE':
        return f'{setup} / {regime}: прибыльное исполнение с высокой реализацией благоприятного хода; сохранять логику сопровождения.'
    if label=='RIGHT_DIRECTION_LOW_CAPTURE':
        return f'{setup} / {regime}: направление монетизировано, но захват MFE низкий; проверять TP/trailing и преждевременное сокращение.'
    if label=='RIGHT_DIRECTION_STOP_ERROR':
        return f'{setup} / {regime}: до стопа был благоприятный ход; проверять ширину/структуру стопа, не штрафовать направление автоматически.'
    if label=='FAVORABLE_PATH_NOT_MONETIZED':
        return f'{setup} / {regime}: рынок давал благоприятный ход, но Net не стал положительным; изучать выход и giveback отдельно от направления.'
    if label=='RIGHT_DIRECTION_PREMATURE_EXIT':
        return f'{setup} / {regime}: выход/сокращение произошло до полной реализации движения; проверять подтверждение разворота и удержание позиции.'
    if label=='DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH':
        return f'{setup} / {regime}: устойчивого благоприятного хода до закрытия не было; проверять направление, момент входа и режим.'
    if label=='RECOVERED_HISTORICAL_NO_LEARNING':
        return 'Историческая запись восстановлена частично; результат хранится, но неполная телеметрия не усиливает правила модели.'
    return f'{setup} / {regime}: смешанный результат; использовать только как слабое execution-evidence до накопления выборки.'
