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


def seed_user_teaching(pg_event, read_event=None):
    """Idempotently seed the existing ledger; never equate False with success.

    ``pg_event`` receives six positional arguments, matching the production
    event_ts signature and adapters whose final argument is named created_at.
    An acknowledgement does not distinguish INSERT from ON CONFLICT. Optional
    ``read_event(event_type, entity_key)`` must return the stored payload (or a
    row containing ``payload``) to verify durable, matching content. Without
    readback neither True nor False establishes that evidence. No DB scan or
    new table is needed. Exceptions are reported by type only to avoid secrets.
    """
    payload = policy_snapshot()
    result = {"teaching_id": TEACHING_ID, "event_key": EVENT_TYPE + ":" + TEACHING_ID,
              "status": "UNVERIFIED", "durable": None, "inserted": False,
              "payload_sha256": _digest(payload)}
    try:
        written = pg_event(EVENT_TYPE, TEACHING_ID, _copy(payload), None, None, SOURCE_TIMESTAMP)
        if read_event is not None:
            stored = read_event(EVENT_TYPE, TEACHING_ID)
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
    if body.get("trace_version") != TRACE_VERSION or body.get("teaching_id") != TEACHING_ID:
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
    trace["trace_sha256"] = _digest(trace)
    return trace
