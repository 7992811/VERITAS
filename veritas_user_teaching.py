"""Owner-authored operational teaching and immutable entry provenance.

CTC owns execution parameters. This module records their provenance without
promoting a trading hypothesis, fitting a model or claiming an empirical edge.
The existing ``ledger_events``/``pg_event`` interface supplies durable storage.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import veritas_canonical_constitution as CTC

TEACHING_ID = "USER_TF_STRUCTURE_2026_10_06"
SOURCE_TIMESTAMP = "2026-10-06T19:56:32Z"
EVENT_TYPE = "user_teaching"
TRACE_VERSION = "USER_TEACHING_ENTRY_TRACE_V1"
MA_TEACHING_ID = "USER_DAILY_MA_REBOUND_2026_10_07"
MA_SOURCE_TIMESTAMP = "2026-10-06T21:26:00Z"
BREAKOUT_TEACHING_ID = "USER_INTRABAR_STRUCTURE_2026_10_07"
BREAKOUT_SOURCE_TIMESTAMP = "2026-10-07T08:23:55Z"
ACCELERATION_TEACHING_ID = "USER_TREND_ACCELERATION_2026_10_09"
ACCELERATION_SOURCE_TIMESTAMP = "2026-10-09T08:41:00Z"
ACCELERATION_USER_CORRECTION_RU = (
    "Стремиться к верхней границе эффективности: при резком подтверждённом движении "
    "увеличивать прибыльную позицию ступенчато по мере подтверждения тренда. "
    "Размер добора определять реальным риском до стопа, а не номинальной долей NAV. "
    "Разрешить младшим таймфреймам закрывать устаревшую противоположную позицию, "
    "не требуя от них полномочия немедленно открыть крупную новую. "
    "После устойчивого MFE от 0,15% защищать сделку безубытком с издержками и далее "
    "структурным трейлингом. Использовать промежуточные подтверждения 15m/30m и "
    "увеличивать позицию сильнее в режиме ускорения тренда."
)
BREAKOUT_USER_CORRECTION_RU = (
    "Сигнал в лонг должен появляться или подтверждать удержание при пробое "
    "предыдущей локальной вершины, включая открытие рынка. Пример CNYRUBf: "
    "07:00, пробой 12,727, стоп ниже 12,693. Затем пробой 12,75 около 10:14: "
    "сразу открыть или увеличить Long, проверить объём и стоп всей позиции "
    "ниже защищённого минимума 12,693. Тейки на предыдущих более крупных "
    "зонах проторговки 12,805 и 12,84. Новый пробой следующего уровня — новое "
    "подтверждение Long с той же логикой стопов и целей. Применить ко всем инструментам."
)
MA_USER_CORRECTION_RU = (
    "В правилах используется анализ скользящих средних? Если цена находится у "
    "50 или 200 дневной средней это часто является уровнем поддержки или "
    "сопротивления, где можно открываться позиции на отскок от уровня"
)
MA_USER_AUTHORIZATION_RU = (
    "Давай внедрим эти изменения.\n"
    "И проверь почему по нефти не открываются сделки, хотя сигналы есть, "
    "и хорошая волатильность в течение дня, а вход постоянно заблокирован? Это ошибка"
)
USER_CORRECTION_RU = (
    "Тут ошибка в логике открытия сделок, на старшем таймфрейме лонг сформировался "
    "при пробитии предыдущего максимума, и также на меньших таймфреймах, а стоп "
    "должен стоять ниже предыдущего минимума того же таймфрейма по которому "
    "открывается сделка, и размеры стопов и тейков должны учитывать волатильность "
    "именно этого таймфрейма. Иначе получается тренд растущий, а система только "
    "в самом конце покупает, это ошибка. Внеси эти из прения, найдите учётом этих "
    "правил более эффективные точки входа и применяй их во всех портфелях как "
    "обучение от меня"
)


def _copy(value):
    """Detach JSON evidence, rejecting non-finite numbers and opaque objects."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def policy_snapshot():
    """Return the declared active policy; this is not a persistence receipt."""
    policy = _copy(CTC.STRUCTURAL_ENTRY_POLICY)
    if policy.get("teaching_id") != TEACHING_ID:
        raise ValueError("CTC teaching identity differs from recorded owner instruction")
    return {
        "teaching_id": TEACHING_ID,
        "source_type": "USER_AUTHORED_OPERATIONAL_POLICY",
        "source_timestamp": SOURCE_TIMESTAMP,
        "source_text_ru": USER_CORRECTION_RU,
        "status": "ACTIVE_OPERATIONAL_POLICY",
        "ctc_version": CTC.VERSION,
        "runtime_authority": CTC.BASIS_RUNTIME,
        "structural_policy_version": policy["version"],
        "portfolios": list(CTC.PORTFOLIO_ORDER),
        "scope": "ALL_CONFIGURED_PAPER_PORTFOLIOS_WITH_EXISTING_ASSET_AND_RISK_LIMITS",
        "execution_policy": policy,
        "requirements": {
            "long": "Break a previously known high; stop below the preceding low of the entry timeframe.",
            "short": "Symmetric implementation: break a known low; stop above the preceding high of that timeframe.",
            "volatility": "Trigger, swing stop, ATR and target use the same entry timeframe.",
            "source": "Structural candles and execution quote must represent the same approved source and instrument.",
            "time": "Preserve the original closed-bar confirmation time and event ID; polling cannot create a fresh event.",
            "late_entry": "Reject an aged or extended event; await a new structural confirmation.",
            "management": "Do not replace a senior position's structural stop with a lower-timeframe or synthetic stop.",
        },
        "parameter_validation": {
            "status": "SHADOW_OOS_REQUIRED",
            "runtime_defaults_status": policy.get("parameter_validation_status", "UNVALIDATED_DEFAULTS"),
            "ml_training_performed": False,
            "validated_profitability": False,
            "note": "The user authored the structural invariant. Numerical defaults are implementation choices; changes require independent post-cost OOS evidence.",
        },
        "storage": {"table": "ledger_events", "event_type": EVENT_TYPE,
                    "entity_key": TEACHING_ID, "event_key": EVENT_TYPE + ":" + TEACHING_ID},
    }


def ma_policy_snapshot():
    """New owner instruction has its own immutable key; preserve the old record."""
    policy = _copy(CTC.MA_REBOUND_POLICY)
    return {
        "teaching_id": MA_TEACHING_ID,
        "source_type": "USER_AUTHORED_OPERATIONAL_POLICY",
        "source_timestamp": MA_SOURCE_TIMESTAMP,
        "source_timestamp_precision": "MINUTE",
        "source_text_ru": MA_USER_CORRECTION_RU,
        "authorization_text_ru": MA_USER_AUTHORIZATION_RU,
        "status": "ACTIVE_OPERATIONAL_POLICY",
        "parent_teaching_id": TEACHING_ID,
        "ctc_version": CTC.VERSION,
        "runtime_authority": CTC.BASIS_RUNTIME,
        "portfolios": list(CTC.PORTFOLIO_ORDER),
        "scope": "ALL_CONFIGURED_PAPER_PORTFOLIOS_WITH_EXISTING_ASSET_AND_RISK_LIMITS",
        "execution_policy": policy,
        "requirements": {
            "context": "SMA50/SMA200 from completed native daily candles of the execution source and instrument.",
            "entry": "Touch, hold/reclaim and a new closed-bar break of the local rebound high/low.",
            "volatility": "Entry confirmation, stop, ATR and target use the selected execution timeframe.",
            "causality": "Freeze the daily snapshot before the touch; retain the original event time and identity.",
            "independence": "Missing daily history disables only the MA scenario, not structural breakouts.",
        },
        "parameter_validation": {
            "status": "SHADOW_OOS_REQUIRED",
            "runtime_defaults_status": policy["parameter_validation_status"],
            "ml_training_performed": False, "validated_profitability": False,
        },
        "storage": {"table": "ledger_events", "event_type": EVENT_TYPE,
                    "entity_key": MA_TEACHING_ID, "event_key": EVENT_TYPE + ":" + MA_TEACHING_ID},
    }


def breakout_policy_snapshot():
    """A new, separately addressed owner instruction; examples are not prices in code."""
    policy = _copy(CTC.BREAKOUT_LIFECYCLE_POLICY)
    return {
        "teaching_id": BREAKOUT_TEACHING_ID,
        "source_type": "USER_AUTHORED_OPERATIONAL_POLICY",
        "source_timestamp": BREAKOUT_SOURCE_TIMESTAMP,
        "source_text_ru": BREAKOUT_USER_CORRECTION_RU,
        "status": "ACTIVE_OPERATIONAL_POLICY",
        "parent_teaching_id": TEACHING_ID,
        "refines": ["CLOSED_BAR_ONLY_CONFIRMATION", "TRIGGER_AND_STOP_MUST_HAVE_IDENTICAL_TIMEFRAME",
                    "FIXED_GROSS_R_TARGET", "UNIVERSAL_NET_RR_1_15_FOR_STRUCTURAL_BREAKOUTS"],
        "ctc_version": CTC.VERSION, "runtime_authority": CTC.BASIS_RUNTIME,
        "scope": "ALL_CONFIGURED_PAPER_PORTFOLIOS_WITH_EXISTING_ASSET_AND_RISK_LIMITS",
        "portfolios": list(CTC.PORTFOLIO_ORDER), "execution_policy": policy,
        "requirements": {
            "entry": "A fresh same-source quote crosses a previously available structural level; do not wait for the trigger candle to close.",
            "continuation": "A distinct later level break confirms HOLD or earns one ADD; polling cannot duplicate an allocation.",
            "stop": "Protect the parent structural swing. Record trigger, structural, stop and ATR timeframes separately.",
            "size": "Recheck full held-position plus incremental stop risk, costs, gross exposure and drawdown before every add.",
            "targets": "Use previously observed larger consolidation zones, partial TP1 then TP2, with weighted post-cost economics.",
            "short": "Mirror levels, stop, targets, sizing and causality for SHORT.",
            "causality": "No future candles, retrospective tick timestamps, source substitution or new event time from a refresh.",
        },
        "examples": {
            "asset": "CNYRUBF", "date": "2026-10-07", "timezone": "Europe/Moscow",
            "opening": {"user_time": "07:00", "trigger": 12.727, "protected_low": 12.693},
            "continuation": {"user_time": "10:14", "user_trigger": 12.75,
                "protected_low": 12.693, "user_targets": [12.805, 12.84]},
            "evidence": "tests/fixtures/cny_structural_20261007.json",
            "verification_note": "Native minute bars recross 12.750 at 10:13 after an earlier 10:00 crossing; 10:14 crosses previously known 12.756/12.760 highs. OHLC cannot prove the exact intraminute execution price.",
            "runtime_price_hardcodes": False,
        },
        "parameter_validation": {"status": "OWNER_POLICY_CASE_REPLAY_REQUIRED",
            "ml_training_performed": False, "validated_profitability": False},
        "storage": {"table": "ledger_events", "event_type": EVENT_TYPE,
            "entity_key": BREAKOUT_TEACHING_ID, "event_key": EVENT_TYPE + ":" + BREAKOUT_TEACHING_ID},
    }


def acceleration_policy_snapshot():
    """Owner-approved trend-acceleration, reversal-exit and winner-protection policy."""
    policy=_copy(CTC.TREND_ACCELERATION_POLICY)
    return {
        "teaching_id": ACCELERATION_TEACHING_ID,
        "source_type": "USER_AUTHORED_OPERATIONAL_POLICY",
        "source_timestamp": ACCELERATION_SOURCE_TIMESTAMP,
        "source_timestamp_precision": "MINUTE",
        "source_text_ru": ACCELERATION_USER_CORRECTION_RU,
        "status": "ACTIVE_OPERATIONAL_POLICY",
        "parent_teaching_id": BREAKOUT_TEACHING_ID,
        "ctc_version": CTC.VERSION,
        "runtime_authority": CTC.BASIS_RUNTIME,
        "scope": policy.get("scope"),
        "portfolios": ["Impulse","Aggressive","Champion","Challenger"],
        "execution_policy": policy,
        "requirements": {
            "exit_vs_entry": (
                "Confirmed 1m/5m opposite structure may close stale exposure; "
                "the opposite entry must still pass canonical economics, source, "
                "risk, drawdown and execution gates."
            ),
            "pyramiding": (
                "A fresh causal confirmation may earn one larger add only while "
                "the position is a winner and remaining target progress is not spent."
            ),
            "risk": (
                "Size by forward net stop-risk after costs. Nominal allocation is "
                "not stop-risk; position and gross caps remain separate constraints."
            ),
            "profit_protection": (
                "MFE >=0.15 percentage points starts persistence testing; after "
                "sustained favorable movement, move protection to cost-covered "
                "breakeven and then trail confirmed structure."
            ),
            "intermediate_timeframes": (
                "Build 15m and 30m confirmation only from completed native 5m bars; "
                "never use future or incomplete buckets."
            ),
            "runner": (
                "Harvest partial targets while retaining a protected runner when "
                "trend structure remains confirmed."
            ),
            "currency_scope": "Currency/live-account behavior is explicitly excluded from this policy.",
        },
        "parameter_validation": {
            "status": policy.get("parameter_validation_status","SHADOW_OOS_REQUIRED"),
            "ml_training_performed": False,
            "validated_profitability": False,
            "note": (
                "Owner policy is active for paper execution. Numerical stage sizes "
                "remain subject to replay, walk-forward, OOS and post-cost validation."
            ),
        },
        "storage": {"table":"ledger_events","event_type":EVENT_TYPE,
                    "entity_key":ACCELERATION_TEACHING_ID,
                    "event_key":EVENT_TYPE+":"+ACCELERATION_TEACHING_ID},
    }


def seed_all_user_teachings(pg_event, read_event=None):
    return [seed_user_teaching(pg_event, read_event, snapshot=payload)
            for payload in (policy_snapshot(), ma_policy_snapshot(),
                            breakout_policy_snapshot(), acceleration_policy_snapshot())]


def seed_user_teaching(pg_event, read_event=None, *, snapshot=None):
    """Idempotently seed the existing ledger; never equate False with success.

    ``pg_event`` receives six positional arguments, matching the production
    event_ts signature and adapters whose final argument is named created_at.
    An acknowledgement does not distinguish INSERT from ON CONFLICT. Optional
    ``read_event(event_type, entity_key)`` must return the stored payload (or a
    row containing ``payload``) to verify durable, matching content. Without
    readback neither True nor False establishes that evidence. No DB scan or
    new table is needed. Exceptions are reported by type only to avoid secrets.
    """
    payload = policy_snapshot() if snapshot is None else _copy(snapshot)
    teaching_id = payload["teaching_id"]
    result = {"teaching_id": teaching_id, "event_key": EVENT_TYPE + ":" + teaching_id,
              "status": "UNVERIFIED", "durable": None, "inserted": False,
              "payload_sha256": _digest(payload)}
    try:
        written = pg_event(EVENT_TYPE, teaching_id, _copy(payload), None, None, payload["source_timestamp"])
        if read_event is not None:
            stored = read_event(EVENT_TYPE, teaching_id)
            if isinstance(stored, Mapping) and "payload" in stored:
                stored = stored["payload"]
            if isinstance(stored, str):
                stored = json.loads(stored)
            if stored is not None and _digest(stored) == result["payload_sha256"]:
                result.update(status="STORED_VERIFIED" if written is True else "ALREADY_PRESENT",
                              durable=True, inserted=None if written is True else False)
            elif stored is not None:
                result.update(status="STORED_PAYLOAD_DIFFERS", durable=False)
            else:
                result.update(status="NOT_CONFIRMED", durable=False)
        elif written is True:
            result.update(status="WRITE_ACKNOWLEDGED", durable=None, inserted=None)
    except Exception as exc:
        result.update(status="ERROR", durable=False, error_type=type(exc).__name__)
    return result


def verify_entry_trace(trace):
    """Check snapshot integrity; this hash is not a signature or trading gate."""
    if not isinstance(trace, Mapping):
        return False
    body = dict(trace)
    digest = body.pop("trace_sha256", None)
    if body.get("trace_version") != TRACE_VERSION or body.get("teaching_id") not in (TEACHING_ID, MA_TEACHING_ID, BREAKOUT_TEACHING_ID):
        return False
    try:
        return bool(digest and digest == _digest(body))
    except (TypeError, ValueError):
        return False


def _event_id(context):
    event = context.get("event") or context
    return event.get("event_id") or event.get("entry_event_id")


def entry_trace(context, portfolio=None, existing=None):
    """Freeze an already-admitted same-TF context without reading any clock.

    ``context`` is the canonical timeframe_entry_context, not a current signal
    row. Admission validates geometry; this helper only preserves provenance.
    An existing valid trace is returned unchanged, so quote refreshes, ATR
    updates and trailing cannot rewrite the entry. A new event needs a new
    trace, which the accounting layer appends as a separate entry/add record.
    """
    if not isinstance(context, Mapping) or not context:
        raise ValueError("An admitted structural context is required")
    if portfolio is not None and portfolio not in CTC.PORTFOLIO_ORDER:
        raise ValueError("Unknown portfolio")
    if existing is not None:
        if not verify_entry_trace(existing):
            raise ValueError("Existing entry trace failed integrity verification")
        original = existing["timeframe_entry_context"]
        event_id, prior_id = _event_id(context), _event_id(original)
        if event_id and prior_id and event_id != prior_id:
            raise ValueError("A new event requires a separate entry trace")
        if portfolio is not None and existing.get("portfolio") != portfolio:
            raise ValueError("A different portfolio requires a separate entry trace")
        return _copy(existing)
    policy = CTC.STRUCTURAL_ENTRY_POLICY
    trace = {
        "trace_version": TRACE_VERSION,
        "teaching_id": TEACHING_ID,
        "teaching_source_timestamp": SOURCE_TIMESTAMP,
        "ctc_version": CTC.VERSION,
        "structural_policy_version": policy["version"],
        "portfolio": portfolio,
        "timeframe_entry_context": _copy(dict(context)),
        "parameter_validation_status": policy.get("parameter_validation_status", "UNVALIDATED_DEFAULTS"),
    }
    if (context.get("event") or {}).get("event_type") == "DAILY_MA_REBOUND":
        trace.update(teaching_id=MA_TEACHING_ID, teaching_source_timestamp=MA_SOURCE_TIMESTAMP,
                     parent_teaching_id=TEACHING_ID,
                     daily_ma_policy_snapshot=_copy(CTC.MA_REBOUND_POLICY))
    import veritas_structural_breakout as SB
    if SB.applies(context):
        trace.update(teaching_id=BREAKOUT_TEACHING_ID,
            teaching_source_timestamp=BREAKOUT_SOURCE_TIMESTAMP,
            parent_teaching_id=TEACHING_ID,
            structural_policy_version=CTC.BREAKOUT_LIFECYCLE_POLICY["version"],
            parameter_validation_status=CTC.BREAKOUT_LIFECYCLE_POLICY['parameter_validation_status'],
            breakout_lifecycle_policy_snapshot=_copy(CTC.BREAKOUT_LIFECYCLE_POLICY))
    trace["trace_sha256"] = _digest(trace)
    return trace
