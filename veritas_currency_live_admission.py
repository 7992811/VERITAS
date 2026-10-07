"""Whole-account authority for approved Currency entries, inert until called.

The evidence issuer is independent of trade approval, delivery and statements.
Imports verify its HMAC and append immutable observations; no raw PASS flag is
accepted. The checker reads the actual entire broker account and reuses
veritas_live.authorize_candidate. Missing evidence cannot become zero risk.

Evidence producers use evidence_scope(approved_terms) and broker_snapshot(...)
to reproduce the exact scope and account digest. Research calibration reports
are not production evidence: MODEL_ADMISSION requires independently reviewed
OOS, vault, calibration, stress, shadow, CI and parity artifact references.
Neither publishing evidence nor checking it arms execution or submits orders.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import math
import re
import uuid

import veritas_canonical_constitution as CTC
import veritas_currency_trade_plan as P
import veritas_entry_version as EV
import veritas_execution as VX
import veritas_instruments as VI
import veritas_live as VL
import veritas_promotion as VP
import veritas_risk as VR
from veritas_tbank_trading import quotation_to_decimal

VERSION = "currency-live-admission-v1"
TABLE = "currency_live_admission_evidence"
MAX_BYTES = 60 * 1024
MAX_POSITIONS = 100
MAX_ACCOUNT_AGE = 30
MAX_MODEL_AGE = 900
KINDS = {"ACCOUNT_CONTROLS", "MODEL_ADMISSION"}
_HEX = re.compile(r"^[a-f0-9]{64}$")
_COUNTS = {"oos_n", "vault_n", "calibration_n", "shadow_trades"}
_FLAGS = {"code_ci_pass", "data_parity_pass"}
_PROMOTION = {f.name for f in fields(VP.PromotionEvidence)}
_ARTIFACTS = {"oos", "vault", "calibration", "high_cost", "shadow", "ci", "data_parity"}


class LiveAdmissionError(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def utcnow():
    return datetime.now(timezone.utc)


def _require(condition, code, status=400):
    if not condition:
        raise LiveAdmissionError(code, status)


def _text(value, code="INVALID_EVIDENCE_TEXT", maximum=256):
    _require(isinstance(value, str) and value.strip() == value and 0 < len(value) <= maximum, code)
    return value


def _number(value, *, positive=False):
    # Decimal strings preserve financial terms through JSONB and signed replay.
    _require(type(value) in (str, int), "INVALID_EVIDENCE_NUMBER")
    try:
        number = P.decimal(value, positive=positive)
    except ValueError:
        raise LiveAdmissionError("INVALID_EVIDENCE_NUMBER", 400) from None
    _require(math.isfinite(float(number)), "INVALID_EVIDENCE_NUMBER")
    return number


def _date(value):
    try:
        return P.utc(value)
    except ValueError:
        raise LiveAdmissionError("INVALID_EVIDENCE_TIMESTAMP", 400) from None


def _fresh(observed, now, maximum):
    age = (_date(now) - _date(observed)).total_seconds()
    _require(-2 <= age <= maximum, "LIVE_EVIDENCE_STALE", 409)


def canonical_json(value):
    """The public HMAC representation; floats/unknown objects are prohibited."""
    def visit(item, depth=0):
        _require(depth <= 20, "EVIDENCE_TOO_DEEP")
        if isinstance(item, dict):
            _require(all(isinstance(k, str) for k in item), "INVALID_EVIDENCE_KEY")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            _require(len(item) <= 500, "EVIDENCE_LIST_TOO_LARGE")
            for child in item:
                visit(child, depth + 1)
        else:
            _require(item is None or type(item) in (str, int, bool), "INVALID_EVIDENCE_JSON_TYPE")
    visit(value)
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise LiveAdmissionError("INVALID_EVIDENCE_JSON", 400) from None
    _require(len(raw.encode()) <= MAX_BYTES, "EVIDENCE_TOO_LARGE")
    return raw


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def snapshot_digest(snapshot):
    return _digest(snapshot)


def evidence_scope(terms):
    """Bind issuer results to this immutable proposal and current entry code."""
    _require(isinstance(terms, dict), "INVALID_APPROVED_TERMS")
    identity = EV.version_identity()
    _require(identity.get("complete") is True, "LIVE_CODE_IDENTITY_REQUIRED", 409)
    result = {key: _text(terms.get(key), "LIVE_EVIDENCE_SCOPE_REQUIRED") for key in
              ("account_id", "instrument_uid", "execution_environment", "model_version", "policy_version", "horizon")}
    _require(result["execution_environment"] in {"production", "sandbox"}, "INVALID_EVIDENCE_ENVIRONMENT")
    _require(terms.get("asset") == P.ASSET and terms.get("action") in {"OPEN", "ADD"}, "LIVE_ENTRY_TERMS_REQUIRED")
    _require(result["policy_version"] == CTC.VERSION, "LIVE_POLICY_VERSION_MISMATCH", 409)
    _require(isinstance(terms.get("source_identity"), dict) and terms["source_identity"], "LIVE_SOURCE_IDENTITY_REQUIRED")
    result.update(asset=P.ASSET, source_hash=P.fingerprint(terms.get("source_identity")),
                  terms_hash=P.fingerprint(terms), entry_version=identity)
    return result


def _money(raw, *, rub=False):
    _require(isinstance(raw, dict), "INVALID_BROKER_MONEY", 409)
    currency = _text(raw.get("currency"), "BROKER_MONEY_CURRENCY_REQUIRED").lower()
    _require(not rub or currency == "rub", "WHOLE_ACCOUNT_RUB_NAV_REQUIRED", 409)
    try:
        number = quotation_to_decimal(raw)
    except ValueError:
        raise LiveAdmissionError("INVALID_BROKER_MONEY", 409) from None
    return {"currency": currency, "value": format(number, "f")}


def _quote(raw):
    try:
        return quotation_to_decimal(raw)
    except ValueError:
        raise LiveAdmissionError("INVALID_BROKER_QUANTITY", 409) from None


def broker_snapshot(portfolio, positions, orders, *, account_id, environment):
    """Normalize the *entire* account, including cash and unmatched holdings.

    GetPositions balances are unblocked quantities, not account valuation.
    Blocked/virtual holdings and pending orders fail closed because this version
    does not reserve risk for their possible execution. See T-Bank Operations
    methods: https://developer.tbank.ru/invest/services/operations/methods
    """
    _require(isinstance(portfolio, dict) and isinstance(positions, dict), "INVALID_BROKER_ACCOUNT_RESPONSE", 409)
    _require(portfolio.get("accountId") == account_id and positions.get("accountId", account_id) == account_id,
             "WHOLE_ACCOUNT_SCOPE_MISMATCH", 409)
    _require(environment in {"production", "sandbox"}, "INVALID_EVIDENCE_ENVIRONMENT")
    _require(positions.get("limitsLoadingInProgress", False) is False, "WHOLE_ACCOUNT_LIMITS_LOADING", 409)
    _require(isinstance(orders, list) and not orders, "WHOLE_ACCOUNT_ACTIVE_ORDERS", 409)
    _require(not portfolio.get("virtualPositions"), "WHOLE_ACCOUNT_VIRTUAL_POSITIONS_UNSUPPORTED", 409)
    nav = _money(portfolio.get("totalAmountPortfolio"), rub=True)["value"]
    _number(nav, positive=True)
    balances = {}
    for kind in ("securities", "futures", "options"):
        rows = positions.get(kind, [])
        _require(isinstance(rows, list) and len(rows) <= MAX_POSITIONS, "INVALID_BROKER_HOLDINGS", 409)
        for row in rows:
            _require(isinstance(row, dict), "INVALID_BROKER_HOLDINGS", 409)
            balance, blocked = _number(row.get("balance", "0")), _number(row.get("blocked", "0"))
            _require(blocked == 0 and row.get("exchangeBlocked", False) is False,
                     "WHOLE_ACCOUNT_BLOCKED_POSITION", 409)
            if not balance:
                continue
            uid = _text(row.get("instrumentUid"), "WHOLE_ACCOUNT_INSTRUMENT_UID_REQUIRED")
            _require(uid not in balances, "DUPLICATE_BROKER_POSITION", 409)
            balances[uid] = balance
    holdings = []
    rows = portfolio.get("positions", [])
    _require(isinstance(rows, list) and len(rows) <= MAX_POSITIONS, "INVALID_BROKER_HOLDINGS", 409)
    seen = set()
    for row in rows:
        _require(isinstance(row, dict), "INVALID_BROKER_HOLDINGS", 409)
        quantity = _quote(row.get("quantity", {"units": "0", "nano": 0}))
        if not quantity:
            continue
        uid, kind = _text(row.get("instrumentUid"), "WHOLE_ACCOUNT_INSTRUMENT_UID_REQUIRED"), _text(row.get("instrumentType"))
        _require(uid not in seen, "DUPLICATE_BROKER_POSITION", 409)
        seen.add(uid)
        _require(row.get("blocked", False) is False and _quote(row.get("blockedLots", {"units": "0", "nano": 0})) == 0,
                 "WHOLE_ACCOUNT_BLOCKED_POSITION", 409)
        price = _money(row.get("currentPrice"))
        # The broker's RUB cash position has no market stop risk. Foreign cash is
        # retained as an exposure and requires exact risk/FX coverage below.
        is_rub_cash = (kind == "currency" and row.get("figi") == "RUB000UTSTOM" and
                       price["currency"] == "rub" and _number(price["value"]) == 1)
        if kind != "currency":
            _require(balances.pop(uid, None) == quantity, "WHOLE_ACCOUNT_HOLDINGS_NOT_RECONCILED", 409)
        holdings.append({"instrument_uid": uid, "instrument_type": kind, "quantity": format(quantity, "f"),
                         "current_price": price, "rub_cash": is_rub_cash})
    _require(not balances, "WHOLE_ACCOUNT_HOLDINGS_NOT_RECONCILED", 409)
    cash = {}
    for key in ("money", "blocked"):
        rows = positions.get(key, [])
        _require(isinstance(rows, list) and len(rows) <= MAX_POSITIONS, "INVALID_BROKER_CASH", 409)
        values = [_money(r) for r in rows]
        _require(len({r["currency"] for r in values}) == len(values), "DUPLICATE_BROKER_CASH", 409)
        cash[key] = sorted(values, key=lambda row: row["currency"])
    _require(all(_number(row["value"]) == 0 for row in cash["blocked"]), "WHOLE_ACCOUNT_BLOCKED_CASH", 409)
    # Non-RUB cash requires currency-to-instrument reconciliation in addition to
    # a price. That adapter is deliberately not guessed from a currency label.
    _require(all(row["currency"] == "rub" or _number(row["value"]) == 0 for row in cash["money"]),
             "WHOLE_ACCOUNT_FOREIGN_CASH_RECONCILIATION_REQUIRED", 409)
    return {"account_id": account_id, "environment": environment, "nav_rub": nav,
            "holdings": sorted(holdings, key=lambda row: row["instrument_uid"]), "cash": cash, "orders": []}


def _validate_scope(scope):
    _require(isinstance(scope, dict), "INVALID_EVIDENCE_SCOPE")
    expected = {"account_id", "instrument_uid", "execution_environment", "model_version", "policy_version", "horizon",
                "asset", "source_hash", "terms_hash", "entry_version"}
    _require(set(scope) == expected and scope.get("asset") == P.ASSET, "INVALID_EVIDENCE_SCOPE")
    for key in expected - {"entry_version"}:
        _text(scope[key])
    _require(scope["execution_environment"] in {"production", "sandbox"}, "INVALID_EVIDENCE_ENVIRONMENT")
    _require(_HEX.fullmatch(scope["source_hash"]) and _HEX.fullmatch(scope["terms_hash"]), "INVALID_EVIDENCE_DIGEST")
    identity = scope["entry_version"]
    _require(isinstance(identity, dict) and set(identity) == {"strategy_epoch", "strategy_entry_sha", "strategy_policy_hash", "complete"}
             and identity["complete"] is True, "INVALID_ENTRY_VERSION")
    for field in ("strategy_epoch", "strategy_entry_sha", "strategy_policy_hash"):
        _text(identity[field])


def _promotion(payload):
    _require(isinstance(payload, dict) and set(payload) == _PROMOTION, "INVALID_PROMOTION_EVIDENCE")
    values = {}
    for key, value in payload.items():
        if key == "model_version":
            values[key] = _text(value)
        elif key in _COUNTS:
            _require(type(value) is int and 0 <= value <= 1_000_000_000, "INVALID_PROMOTION_COUNT")
            values[key] = value
        elif key in _FLAGS:
            _require(type(value) is bool, "INVALID_PROMOTION_FLAG")
            values[key] = value
        else:
            values[key] = float(_number(value))
    evidence = VP.PromotionEvidence(**values)
    result = VP.promotion_gate(evidence)
    _require(not result.get("invalid_evidence_fields"), "INVALID_PROMOTION_METRICS")
    return evidence


def validate_evidence(payload, now):
    canonical_json(payload)
    _require(isinstance(payload, dict), "INVALID_EVIDENCE_PAYLOAD")
    _require(set(payload) == {"version", "evidence_id", "kind", "issuer_id", "observed_at", "valid_until", "scope", "provenance", "data"},
             "INVALID_EVIDENCE_FIELDS")
    _require(payload["version"] == VERSION and payload["kind"] in KINDS, "INVALID_EVIDENCE_VERSION_OR_KIND")
    try:
        _require(str(uuid.UUID(payload["evidence_id"])) == payload["evidence_id"], "INVALID_EVIDENCE_ID")
    except (ValueError, TypeError, AttributeError):
        raise LiveAdmissionError("INVALID_EVIDENCE_ID", 400) from None
    _text(payload["issuer_id"])
    _validate_scope(payload["scope"])
    observed, until = _date(payload["observed_at"]), _date(payload["valid_until"])
    maximum = MAX_ACCOUNT_AGE if payload["kind"] == "ACCOUNT_CONTROLS" else MAX_MODEL_AGE
    _require(observed < until <= observed + timedelta(seconds=maximum), "INVALID_EVIDENCE_VALIDITY")
    _fresh(observed, now, maximum)
    _require(_date(now) < until, "LIVE_EVIDENCE_EXPIRED", 409)
    provenance = payload["provenance"]
    _require(isinstance(provenance, dict) and set(provenance) == {"method", "artifact_sha256", "inputs_sha256"}, "INVALID_EVIDENCE_PROVENANCE")
    _text(provenance["method"])
    _require(all(isinstance(provenance[k], str) and _HEX.fullmatch(provenance[k]) for k in ("artifact_sha256", "inputs_sha256")),
             "INVALID_EVIDENCE_PROVENANCE")
    data = payload["data"]
    _require(isinstance(data, dict), "INVALID_EVIDENCE_DATA")
    if payload["kind"] == "MODEL_ADMISSION":
        _require(set(data) == {"calibrated_probability", "promotion", "artifacts"}, "INVALID_MODEL_EVIDENCE_FIELDS")
        _require(0 <= _number(data["calibrated_probability"]) <= 1, "INVALID_CALIBRATED_PROBABILITY")
        promotion = _promotion(data["promotion"])
        _require(promotion.model_version == payload["scope"]["model_version"], "MODEL_VERSION_MISMATCH")
        artifacts = data["artifacts"]
        _require(isinstance(artifacts, dict) and set(artifacts) == _ARTIFACTS and
                 all(isinstance(v, str) and _HEX.fullmatch(v) for v in artifacts.values()), "INDEPENDENT_MODEL_ARTIFACTS_REQUIRED")
        _require(len(set(artifacts.values())) == len(_ARTIFACTS), "INDEPENDENT_MODEL_ARTIFACTS_REQUIRED")
    else:
        _require(set(data) == {"snapshot_digest", "nav_rub", "high_water_nav_rub", "daily_pnl_pct", "weekly_pnl_pct",
                              "daily_period_start", "weekly_period_start", "risk_positions", "correlations", "kill_switch"},
                 "INVALID_ACCOUNT_EVIDENCE_FIELDS")
        _require(isinstance(data["snapshot_digest"], str) and _HEX.fullmatch(data["snapshot_digest"]), "INVALID_EVIDENCE_DIGEST")
        _require(_number(data["high_water_nav_rub"], positive=True) >= _number(data["nav_rub"], positive=True), "INVALID_WHOLE_ACCOUNT_HIGH_WATER")
        # Explicit UTC calendar boundaries prevent a short, profitable fragment
        # of a losing day/week from masquerading as the complete risk window.
        day_start = observed.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        current_day = _date(now).replace(hour=0, minute=0, second=0, microsecond=0)
        _require(day_start == current_day, "ACCOUNT_RISK_CALENDAR_ROLLED", 409)
        for key, expected_start in (("daily", day_start), ("weekly", week_start)):
            _number(data[key + "_pnl_pct"])
            start = _date(data[key + "_period_start"])
            _require(start == expected_start, "INVALID_ACCOUNT_RISK_WINDOW")
        _require(type(data["kill_switch"]) is bool, "INVALID_ACCOUNT_KILL_SWITCH")
        rows = data["risk_positions"]
        _require(isinstance(rows, list) and len(rows) <= MAX_POSITIONS, "INVALID_ACCOUNT_RISK_POSITIONS")
        seen = set()
        for row in rows:
            _require(isinstance(row, dict) and set(row) == {"instrument_uid", "asset", "quantity", "stop_price", "valuation_currency", "proof_sha256"},
                     "INVALID_ACCOUNT_RISK_POSITION")
            uid = _text(row["instrument_uid"])
            _require(uid not in seen, "DUPLICATE_ACCOUNT_RISK_POSITION")
            seen.add(uid)
            _text(row["asset"])
            _require(_number(row["quantity"]) != 0, "INVALID_ACCOUNT_RISK_QUANTITY")
            _number(row["stop_price"], positive=True)
            _require(row["valuation_currency"] in {"RUB", "POINT"}, "INVALID_RISK_VALUATION_CURRENCY")
            _require(isinstance(row["proof_sha256"], str) and _HEX.fullmatch(row["proof_sha256"]), "INVALID_POSITION_RISK_PROVENANCE")
        corr = data["correlations"]
        _require(isinstance(corr, dict) and len(corr) <= MAX_POSITIONS, "INVALID_CORRELATIONS")
        for asset, pairs in corr.items():
            _text(asset)
            _require(isinstance(pairs, dict) and len(pairs) <= MAX_POSITIONS, "INVALID_CORRELATIONS")
            for other, value in pairs.items():
                _text(other)
                _require(-1 <= _number(value) <= 1, "INVALID_CORRELATIONS")
    return deepcopy(payload)


class LiveAdmissionEvidenceRepository:
    """Explicitly initialized PostgreSQL store. Every read rechecks the issuer MAC."""
    def __init__(self, connect, issuer_key=None, *, clock=utcnow):
        _require(issuer_key is None or isinstance(issuer_key, bytes) and len(issuer_key) >= 32,
                 "INVALID_LIVE_EVIDENCE_KEY")
        self.connect, self._key, self.clock = connect, issuer_key, clock
        self._state = {"configured": issuer_key is not None, "initialized": False,
                       "state": "AWAITING_VERIFIED_EVIDENCE" if issuer_key else "NOT_CONFIGURED",
                       "last_import_at": None, "last_evidence_kind": None}

    def status(self):
        return deepcopy(self._state)

    def initialize(self):
        try:
            with self.connect() as c:
                with c.transaction():
                    c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                        evidence_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_hash TEXT NOT NULL,
                        observed_at TIMESTAMPTZ NOT NULL, imported_at TIMESTAMPTZ NOT NULL,
                        valid_until TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL,
                        payload_hash TEXT NOT NULL, signature TEXT NOT NULL)""")
                    c.execute(f"CREATE INDEX IF NOT EXISTS currency_live_evidence_scope ON {TABLE}(scope_hash,kind,observed_at DESC)")
            self._state["initialized"] = True
        except Exception:
            raise LiveAdmissionError("LIVE_EVIDENCE_STORAGE_UNAVAILABLE", 503) from None

    def _verify(self, payload, signature, now):
        _require(self._key is not None, "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED", 503)
        raw = canonical_json(payload)
        _require(isinstance(signature, str) and _HEX.fullmatch(signature) and
                 hmac.compare_digest(signature, hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()),
                 "LIVE_EVIDENCE_SIGNATURE_INVALID", 403)
        return validate_evidence(payload, now)

    def publish(self, payload, signature, *, now=None):
        stamp = _date(now or self.clock())
        record = self._verify(payload, signature, stamp)
        digest = _digest(record)
        try:
            with self.connect() as c:
                with c.transaction():
                    c.execute(f"""INSERT INTO {TABLE}
                        (evidence_id,kind,scope_hash,observed_at,imported_at,valid_until,payload,payload_hash,signature)
                        VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(evidence_id) DO NOTHING""",
                        (record["evidence_id"], record["kind"], _digest(record["scope"]), record["observed_at"],
                         stamp, record["valid_until"], canonical_json(record), digest, signature))
                    saved = c.execute(f"SELECT payload_hash FROM {TABLE} WHERE evidence_id=%s", (record["evidence_id"],)).fetchone()
                    _require(saved and saved["payload_hash"] == digest, "LIVE_EVIDENCE_ID_REUSED", 409)
        except LiveAdmissionError:
            raise
        except Exception:
            raise LiveAdmissionError("LIVE_EVIDENCE_STORAGE_UNAVAILABLE", 503) from None
        self._state.update(state="EVIDENCE_IMPORTED", last_import_at=stamp.isoformat(), last_evidence_kind=record["kind"])
        return {"ok": True, "evidence_id": record["evidence_id"], "kind": record["kind"],
                "scope_hash": _digest(record["scope"]), "valid_until": record["valid_until"], "payload_hash": digest}

    def load(self, kind, scope, *, now):
        _require(self._key is not None, "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED", 503)
        try:
            with self.connect() as c:
                row = c.execute(f"""SELECT payload,payload_hash,signature FROM {TABLE}
                    WHERE kind=%s AND scope_hash=%s ORDER BY observed_at DESC, imported_at DESC, evidence_id DESC LIMIT 1""",
                    (kind, _digest(scope))).fetchone()
        except Exception:
            raise LiveAdmissionError("LIVE_EVIDENCE_STORAGE_UNAVAILABLE", 503) from None
        _require(row is not None, "LIVE_" + kind + "_EVIDENCE_REQUIRED", 409)
        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        except (ValueError, TypeError):
            raise LiveAdmissionError("LIVE_EVIDENCE_STORAGE_INTEGRITY", 409) from None
        _require(_digest(payload) == row["payload_hash"], "LIVE_EVIDENCE_STORAGE_INTEGRITY", 409)
        record = self._verify(payload, row["signature"], now)
        _require(record["kind"] == kind and record["scope"] == scope, "LIVE_EVIDENCE_SCOPE_MISMATCH", 409)
        return record


class CurrencyLiveAdmission:
    def __init__(self, *, adapter, evidence, account_id, instrument_uid, environment, clock=utcnow):
        self.adapter, self.evidence, self.clock = adapter, evidence, clock
        self.account_id, self.instrument_uid = _text(account_id), _text(instrument_uid)
        _require(environment in {"production", "sandbox"}, "INVALID_EVIDENCE_ENVIRONMENT")
        self.environment = environment
        self._state = {"last_checked_at": None, "eligible": False, "blockers": ["LIVE_ACCOUNT_ADMISSION_NOT_CHECKED"]}

    def status(self):
        return {"version": VERSION, "evidence": self.evidence.status(), **deepcopy(self._state)}

    def __call__(self, *, terms, facts, now):
        try:
            result = self._check(deepcopy(terms), facts, now)
        except (LiveAdmissionError, P.TradePlanBlocked) as exc:
            result = {"eligible": False, "blockers": [str(exc)]}
        except Exception:
            # Broker/DB exceptions can contain URLs or tokens. Never reflect them.
            result = {"eligible": False, "blockers": ["LIVE_ACCOUNT_EVIDENCE_UNAVAILABLE"]}
        self._state = {"last_checked_at": _date(self.clock()).isoformat(), "eligible": result.get("eligible") is True,
                       "blockers": list(result.get("blockers") or [])}
        return result

    def _check(self, terms, facts, now):
        _require(terms.get("account_id") == self.account_id and terms.get("instrument_uid") == self.instrument_uid and
                 terms.get("execution_environment") == self.environment, "LIVE_ACCOUNT_SCOPE_MISMATCH", 409)
        _require(self.environment == "production" and terms.get("action") in {"OPEN", "ADD"}, "LIVE_ENTRY_TERMS_REQUIRED", 409)
        scope = evidence_scope(terms)
        # Load proof before broker I/O, so absent configuration stays inert.
        controls = self.evidence.load("ACCOUNT_CONTROLS", scope, now=now)
        model = self.evidence.load("MODEL_ADMISSION", scope, now=now)
        portfolio = self.adapter.get_portfolio(self.account_id)
        positions = self.adapter.get_positions(self.account_id)
        orders = self.adapter.list_orders(self.account_id)
        snapshot = broker_snapshot(portfolio, positions, orders, account_id=self.account_id, environment=self.environment)
        checked = _date(self.clock())
        _fresh(now, checked, 15)
        validate_evidence(controls, checked)
        validate_evidence(model, checked)
        facts.spec.validate(checked)
        facts.quote.validate(facts.spec, checked)
        _require(facts.account.account_id == self.account_id and facts.spec.instrument_uid == self.instrument_uid and
                 facts.account.reconciled is True and facts.account.costs_reconciled is True, "LIVE_CURRENCY_FACTS_NOT_RECONCILED", 409)
        _require(terms.get("spec_hash") == P.fingerprint(facts.spec.identity()), "CONTRACT_SPEC_CHANGED", 409)
        data = controls["data"]
        _require(data["snapshot_digest"] == _digest(snapshot) and _number(data["nav_rub"]) == _number(snapshot["nav_rub"]),
                 "WHOLE_ACCOUNT_SNAPSHOT_CHANGED", 409)
        nav = _number(snapshot["nav_rub"], positive=True)
        held = {row["instrument_uid"]: row for row in snapshot["holdings"] if not row["rub_cash"]}
        risks = {row["instrument_uid"]: row for row in data["risk_positions"]}
        _require(set(held) == set(risks), "WHOLE_ACCOUNT_STOP_COVERAGE_REQUIRED", 409)
        actual = _number(held[self.instrument_uid]["quantity"]) if self.instrument_uid in held else Decimal(0)
        _require(actual == facts.account.signed_lots * facts.spec.lot_size and
                 P.integer(terms.get("position_before_lots")) == facts.account.signed_lots,
                 "WHOLE_ACCOUNT_CURRENCY_POSITION_CHANGED", 409)
        price, stop, target = (P.decimal(terms.get(key), positive=True) for key in ("limit_price", "stop_price", "target_price"))
        context = P._broker_context(P.approved_entry_context(terms), facts.spec, facts.quote, terms.get("direction"), checked)
        P._structural(context, price, terms.get("direction"), checked)
        lots = P.integer(terms.get("lots"), nonnegative=True)
        direction = terms.get("direction")
        sign = 1 if direction == "LONG" else -1
        _require(lots > 0 and direction in {"LONG", "SHORT"} and terms.get("side") == ("BUY" if sign > 0 else "SELL") and
                 actual * sign >= 0 and sign * (price-stop) > 0 and sign * (target-price) > 0,
                 "LIVE_ORDER_GEOMETRY_INVALID", 409)
        _require(terms["action"] == ("ADD" if actual else "OPEN"), "LIVE_POSITION_ACTION_MISMATCH", 409)
        proposed_notional = price * facts.spec.rub_per_price_unit_per_lot * lots
        _require(proposed_notional == P.decimal(terms.get("order_notional_rub")), "LIVE_ORDER_NOTIONAL_MISMATCH", 409)
        current, gross, cny_gross, cny_stop_rub = [], Decimal(0), Decimal(0), Decimal(0)
        for uid, position in held.items():
            risk = risks[uid]
            qty = _number(position["quantity"])
            _require(qty == _number(risk["quantity"]), "WHOLE_ACCOUNT_RISK_QUANTITY_CHANGED", 409)
            asset = risk["asset"]
            _require((uid == self.instrument_uid) == (asset == P.ASSET), "WHOLE_ACCOUNT_ASSET_IDENTITY_MISMATCH", 409)
            if uid == self.instrument_uid:
                _require(position["instrument_type"] == "futures" and risk["valuation_currency"] == "POINT", "WHOLE_ACCOUNT_VALUATION_MISMATCH", 409)
                mark = P.decimal(facts.quote.bid if qty > 0 else facts.quote.ask, positive=True)
                multiplier = facts.spec.rub_per_price_unit_per_lot / facts.spec.lot_size
                _require(facts.held_terms and P.decimal(facts.held_terms.get("stop_price")) == _number(risk["stop_price"]) == stop,
                         "HELD_CURRENCY_STOP_CHANGED", 409)
            else:
                # Future prices are not cash notionals. Unknown products need a
                # verified contract adapter; they are never valued at price*lots.
                _require(position["instrument_type"] in {"share", "etf", "currency"} and
                         position["current_price"]["currency"] == "rub" and risk["valuation_currency"] == "RUB",
                         "WHOLE_ACCOUNT_INSTRUMENT_VALUATION_UNSUPPORTED", 409)
                mark, multiplier = _number(position["current_price"]["value"], positive=True), Decimal(1)
            stop_level = _number(risk["stop_price"], positive=True)
            _require((mark - stop_level) * qty > 0, "WHOLE_ACCOUNT_STOP_ALREADY_CROSSED", 409)
            notional = abs(qty) * mark * multiplier
            gross += notional
            if asset == P.ASSET:
                cny_gross += notional
                cny_stop_rub += abs(qty) * abs(mark - stop_level) * multiplier
            current.append(VR.PositionRisk(asset, "LONG" if qty > 0 else "SHORT", float(notional/nav), float(mark), float(stop_level)))
        assets = {position.asset for position in current} | {P.ASSET}
        correlations = {}
        for a in assets:
            correlations[a] = {}
            for b in assets:
                if a == b:
                    correlations[a][b] = 1.0
                    continue
                left, right = data["correlations"].get(a, {}).get(b), data["correlations"].get(b, {}).get(a)
                _require(left is not None and right is not None and _number(left) == _number(right), "WHOLE_ACCOUNT_CORRELATION_COVERAGE_REQUIRED", 409)
                correlations[a][b] = float(_number(left))
        _require((cny_gross + proposed_notional) / nav <= P.decimal(CTC.LIVE_RISK_POLICY["max_single_asset_fraction"]),
                 "SINGLE_ASSET_LIMIT", 409)
        _require(terms.get("economics_mode") == "LIVE" and
                 terms.get("target_execution_policy") == "SINGLE_TARGET_SEPARATE_CONFIRMATION",
                 "EXPLICIT_LIVE_ECONOMICS_APPROVAL_REQUIRED", 409)
        plan = P.live_economics_plan(direction, price, stop, target, facts.quote, terms["horizon"],
                                    fraction=proposed_notional/nav, expected_hold_seconds=terms.get("expected_hold_seconds"))
        source = VX.production_source_gate(P.ASSET, {"source_gate_pass": True, "market_open": facts.quote.limit_orders_available is True,
                                                   "production_direct_feed": True})
        registry = VI.InstrumentRegistry()
        registry.put(VI.InstrumentSpec(P.ASSET, "MOEX", self.instrument_uid, "futures", "RUB", "RUB",
            float(facts.spec.tick_size), float(facts.spec.lot_size), float(facts.spec.tick_value_rub/facts.spec.tick_size),
            float(facts.spec.lot_size), "TINVEST_EXACT_INSTRUMENT", facts.spec.observed_at.isoformat()))
        candidate = VL.LiveCandidate(P.ASSET, direction, float(proposed_notional/nav), float(price), float(stop),
            float((gross+proposed_notional)/nav), float(1-nav/_number(data["high_water_nav_rub"])),
            float(_number(model["data"]["calibrated_probability"])), terms["model_version"], plan, source,
            float(_number(data["daily_pnl_pct"])), float(_number(data["weekly_pnl_pct"])))
        verdict = VL.authorize_candidate(candidate, current, correlations, registry, _promotion(model["data"]["promotion"]),
                                        durable_storage=True, broker_reconciled=True, kill_switch=data["kill_switch"])
        # Existing authority measures distance-to-stop. Preserve that authority
        # and add actual modeled CNY execution costs to every applicable cap;
        # these costs cannot disappear merely because account NAV is larger.
        economics = verdict.get("order_gate", {}).get("economics", {})
        cost_rate = P.decimal(economics.get("modeled_round_trip_cost_pct"))
        _require(cost_rate >= 0, "LIVE_MODELED_COST_REQUIRED", 409)
        modeled_cost = (cny_gross + proposed_notional) * cost_rate / nav
        cny_risk = (cny_stop_rub + abs(price-stop) * facts.spec.rub_per_price_unit_per_lot * lots) / nav + modeled_cost
        blockers = list(verdict.get("blockers") or [])
        if cny_risk > P.decimal(CTC.LIVE_RISK_POLICY["max_stop_risk_nav"]):
            blockers.append("CNY_MODELED_STOP_RISK_LIMIT")
        risk_result = verdict.get("risk", {})
        if P.decimal(risk_result.get("total_open_stop_risk_nav")) + modeled_cost > P.decimal(CTC.LIVE_RISK_POLICY["max_total_open_stop_risk_nav"]):
            blockers.append("MODELED_TOTAL_STOP_RISK_LIMIT")
        cny_component = next((row for row in risk_result.get("correlation_components", []) if P.ASSET in row.get("assets", [])), None)
        _require(cny_component is not None, "LIVE_CORRELATED_RISK_REQUIRED", 409)
        if P.decimal(cny_component["stop_risk_nav"]) + modeled_cost > P.decimal(CTC.LIVE_RISK_POLICY["max_correlated_stop_risk_nav"]):
            blockers.append("MODELED_CORRELATED_STOP_RISK_LIMIT")
        verdict.update(eligible=verdict.get("eligible") is True and not blockers,
                       blockers=list(dict.fromkeys(blockers)), status="BLOCK" if blockers else verdict.get("status"))
        return {**verdict, "whole_account_nav_rub": format(nav, "f"), "account_snapshot_digest": _digest(snapshot),
                "cny_modeled_stop_risk_nav": format(cny_risk, "f"), "cny_modeled_cost_nav": format(modeled_cost, "f"),
                "evidence_ids": [controls["evidence_id"], model["evidence_id"]], "scope_hash": _digest(scope), "version": VERSION}
