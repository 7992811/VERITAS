"""Durable, exact Currency funding observations and settlement-report bridge.

Official contracts checked 2026-10-07:
https://developer.tbank.ru/invest/services/operations/methods
https://developer.tbank.ru/invest/services/operations/operations_problems
https://www.moex.com/ru/derivatives/perpetual-futures

GetOperationsByCursor includes OPERATION_TYPE_FUNDING (70), but its cursor is
not a clearing-finality watermark. GetBrokerReport exposes trade commissions,
not a perpetual funding/variation-margin breakdown. We therefore distinguish
actual funding *observed* from complete accounting through a stated instant.

The optional statement_provider is a trusted broker-document adapter, never an
HTTP/client-supplied `verified` flag. It must parse an actual final broker cash
settlement document and retain its original bytes under artifact_sha256. The
normalized report schema is documented by validate_statement below. A report
has to close a numeric economic-P&L/cash bridge, cover every managed execution,
identify the exact observed operations, and account for all three trade fee
components. No native TBank API response is relabelled as that document.

Corrections replace the cumulative observed authority; they do not invent
broker adjustment events. Immutable evidence is retained for each observation.
No VM cash credit is added on top of the ledger's price P&L. No scheduling,
environment inspection, broker writes, or network access occurs at import.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import re

from veritas_currency_trade_ledger import (
    ACCOUNTS, FILLS, FUNDING, SETTLEMENTS, LedgerError, _canonical,
    exact, lots, identifier, utc, fingerprint, fills_fingerprint, project,
)
from veritas_tbank_trading import quotation_to_decimal, TradingError

VERSION = "CURRENCY_SETTLEMENT_V1"
REPORT_VERSION = "CURRENCY_SETTLEMENT_REPORT_V1"
ZERO = Decimal("0")
VM = {"OPERATION_TYPE_ACCRUING_VARMARGIN", "OPERATION_TYPE_WRITING_OFF_VARMARGIN"}
TRADE = {"OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL", "OPERATION_TYPE_BUY_MARGIN", "OPERATION_TYPE_SELL_MARGIN"}
FEE = {"OPERATION_TYPE_BROKER_FEE", "OPERATION_TYPE_SERVICE_FEE", "OPERATION_TYPE_MARGIN_FEE",
       "OPERATION_TYPE_SUCCESS_FEE", "OPERATION_TYPE_OTHER_FEE", "OPERATION_TYPE_CASH_FEE",
       "OPERATION_TYPE_OUT_FEE", "OPERATION_TYPE_OVER_COM", "OPERATION_TYPE_ADVICE_FEE",
       "OPERATION_TYPE_TRACK_MFEE", "OPERATION_TYPE_TRACK_PFEE"}
TRANSFER = {"OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT", "OPERATION_TYPE_INPUT_SWIFT",
            "OPERATION_TYPE_OUTPUT_SWIFT", "OPERATION_TYPE_INPUT_ACQUIRING", "OPERATION_TYPE_OUTPUT_ACQUIRING",
            "OPERATION_TYPE_INP_MULTI", "OPERATION_TYPE_OUT_MULTI"}
ENUM = {1:"OPERATION_TYPE_INPUT", 9:"OPERATION_TYPE_OUTPUT", 12:"OPERATION_TYPE_SERVICE_FEE",
        14:"OPERATION_TYPE_MARGIN_FEE", 15:"OPERATION_TYPE_BUY", 18:"OPERATION_TYPE_SELL_MARGIN",
        19:"OPERATION_TYPE_BROKER_FEE", 20:"OPERATION_TYPE_BUY_MARGIN", 22:"OPERATION_TYPE_SELL",
        26:"OPERATION_TYPE_ACCRUING_VARMARGIN", 27:"OPERATION_TYPE_WRITING_OFF_VARMARGIN",
        66:"OPERATION_TYPE_OTHER_FEE", 67:"OPERATION_TYPE_OTHER", 70:"OPERATION_TYPE_FUNDING"}


class SettlementError(LedgerError):
    pass


def _money(value):
    if not isinstance(value, Mapping) or str(value.get("currency", "")).upper() != "RUB":
        raise SettlementError("SETTLEMENT_RUB_AMOUNT_REQUIRED")
    try:
        return quotation_to_decimal(dict(value))
    except TradingError:
        raise SettlementError("SETTLEMENT_EXACT_MONEY_REQUIRED") from None


def _kind(value):
    if type(value) is int:
        return ENUM.get(value, "OPERATION_TYPE_UNRECOGNIZED")
    if not isinstance(value, str) or not value.startswith("OPERATION_TYPE_"):
        raise SettlementError("SETTLEMENT_OPERATION_TYPE_REQUIRED")
    return value


def _state(value):
    states = {1:"OPERATION_STATE_EXECUTED", 2:"OPERATION_STATE_CANCELED", 3:"OPERATION_STATE_PROGRESS"}
    return states.get(value, value) if type(value) is int else value


def _attribution(item, uid, parents):
    """Return exact instrument cash, or None if it cannot be allocated safely."""
    direct = item.get("instrumentUid")
    children = item.get("childOperations", [])
    if not isinstance(children, list):
        raise SettlementError("SETTLEMENT_INVALID_CHILD_OPERATIONS")
    if children:
        amounts = {}
        for child in children:
            if not isinstance(child, Mapping):
                raise SettlementError("SETTLEMENT_INVALID_CHILD_OPERATIONS")
            child_uid = identifier(child.get("instrumentUid"))
            if child_uid in amounts:
                raise SettlementError("SETTLEMENT_AMBIGUOUS_CHILD_ALLOCATION")
            amounts[child_uid] = _money(child.get("payment"))
        if sum(amounts.values(), ZERO) != _money(item.get("payment")):
            raise SettlementError("SETTLEMENT_CHILD_TOTAL_MISMATCH")
        if direct and (len(amounts) != 1 or direct not in amounts):
            raise SettlementError("SETTLEMENT_CHILD_IDENTITY_MISMATCH")
        return amounts.get(uid, ZERO)
    if direct:
        return _money(item.get("payment")) if direct == uid else ZERO
    if item.get("parentOperationId") in parents:
        return _money(item.get("payment"))
    return None


def _observed_operations(snapshot, account, uid, bound_at, fills, spec, now):
    if (snapshot.get("source") != "TBANK_OPERATIONS_BY_CURSOR"
            or snapshot.get("account_id") != account or snapshot.get("instrument_uid") != uid):
        raise SettlementError("SETTLEMENT_OBSERVATION_SCOPE_MISMATCH")
    if snapshot.get("retrieval_complete") is not True:
        raise SettlementError("SETTLEMENT_OPERATIONS_INCOMPLETE")
    start, end, observed = utc(snapshot.get("from")), utc(snapshot.get("to")), utc(snapshot.get("received_at"))
    if start != utc(bound_at) or not start < end or not end <= observed:
        raise SettlementError("SETTLEMENT_OBSERVATION_INTERVAL_MISMATCH")
    if (utc(now)-observed).total_seconds() < -2 or (utc(now)-end).total_seconds() < -2:
        raise SettlementError("SETTLEMENT_OBSERVATION_IN_FUTURE")
    rows = snapshot.get("operations")
    if not isinstance(rows, list):
        raise SettlementError("SETTLEMENT_OPERATIONS_REQUIRED")
    by_trade, covered, parents, ids, reasons = {f["trade_id"]:f for f in fills}, set(), set(), set(), set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("brokerAccountId") != account:
            raise SettlementError("SETTLEMENT_OPERATION_ACCOUNT_MISMATCH")
        oid = identifier(row.get("id"))
        if oid in ids:
            raise SettlementError("SETTLEMENT_DUPLICATE_OPERATION_ID")
        ids.add(oid)
        if not start <= utc(row.get("date")) <= end:
            raise SettlementError("SETTLEMENT_OPERATION_OUTSIDE_INTERVAL")
        kind, state = _kind(row.get("type")), _state(row.get("state"))
        if state not in ("OPERATION_STATE_EXECUTED", "OPERATION_STATE_CANCELED", "OPERATION_STATE_PROGRESS"):
            reasons.add("BROKER_OPERATION_STATE_UNRESOLVED")
        if row.get("instrumentUid") == uid and state == "OPERATION_STATE_PROGRESS":
            reasons.add("BROKER_OPERATIONS_PENDING")
        if kind in TRADE and row.get("instrumentUid") == uid:
            parents.add(oid)
            trades = (row.get("tradesInfo") or {}).get("trades", [])
            if not isinstance(trades, list):
                raise SettlementError("SETTLEMENT_INVALID_EXECUTION_LIST")
            for trade in trades:
                tid = identifier(trade.get("num"))
                fill = by_trade.get(tid)
                expected_side = "BUY" if kind in ("OPERATION_TYPE_BUY", "OPERATION_TYPE_BUY_MARGIN") else "SELL"
                if (fill is None or fill["side"] != expected_side
                        or lots(trade.get("quantity"), positive=True) != fill["lots"] * spec.lot_size
                        or utc(trade.get("date")) != utc(fill["executed_at"])):
                    reasons.add("BROKER_EXECUTION_OWNERSHIP_UNRESOLVED")
                elif tid in covered:
                    reasons.add("BROKER_EXECUTION_DUPLICATED_IN_OPERATIONS")
                else:
                    covered.add(tid)
    if covered != set(by_trade):
        reasons.add("BROKER_EXECUTIONS_NOT_FULLY_OBSERVED")
    explicit, vm_cash, fee_cash, relevant = ZERO, ZERO, ZERO, []
    for row in rows:
        kind, state = _kind(row.get("type")), _state(row.get("state"))
        if kind in TRADE:
            continue
        if state == "OPERATION_STATE_CANCELED":
            continue
        amount = _attribution(row, uid, parents)
        if amount is None:
            if kind not in TRANSFER:
                reasons.add("BROKER_COST_ALLOCATION_UNRESOLVED")
            continue
        if amount == ZERO and row.get("instrumentUid") not in (None, "", uid):
            continue
        if kind in VM | FEE | {"OPERATION_TYPE_FUNDING"}:
            if state != "OPERATION_STATE_EXECUTED":
                reasons.add("BROKER_COST_OPERATION_PENDING")
                continue
            relevant.append(row["id"])
        if kind == "OPERATION_TYPE_FUNDING":
            explicit -= amount
        elif kind in VM:
            if ((kind == "OPERATION_TYPE_ACCRUING_VARMARGIN" and amount < 0)
                    or (kind == "OPERATION_TYPE_WRITING_OFF_VARMARGIN" and amount > 0)):
                raise SettlementError("SETTLEMENT_VARIATION_MARGIN_SIGN_MISMATCH")
            vm_cash += amount
        elif kind in FEE:
            fee_cash -= amount
        elif kind not in TRANSFER:
            reasons.add("BROKER_COST_TYPE_UNSUPPORTED")
    return {"explicit_funding_rub":explicit, "variation_margin_cash_rub":vm_cash,
            "fee_cash_rub":fee_cash, "operation_ids":sorted(relevant), "reasons":sorted(reasons),
            "requested_through":end, "observed_as_of":observed}


def validate_statement(statement, *, account, uid, bound_at, fills, spec, observed):
    """Validate a normalized final broker settlement document, never an API flag.

    Required identity: version/source/account_id/instrument_uid/ticker/currency,
    statement_id, positive revision, original artifact_sha256, status FINAL,
    period_from, settled_through, issued_at. It must cover every fill. A historical
    closed period is reusable only if the managed allocation is flat thereafter
    and a fresh complete history still matches every cash reference and total.
    Open exposure beyond the report cutoff remains incomplete. cash_operation_ids
    identifies every attributed VM,
    funding and fee row from that observation.

    Required numeric bridge: closing_signed_lots, closing_settlement_price_points
    (None only when flat), variation_margin_cash_rub, explicit_funding_rub,
    embedded_funding_rub, other_fees_rub, fee_cash_rub. Unsettled price P&L is not
    permitted: this is a settled boundary. Each trade_fees row supplies trade_id,
    broker_order_id, quantity_lots and separate nonnegative broker, exchange and
    clearing commissions. The cash bridge is computed independently from fills:
      economic price P&L at settlement = VM cash + embedded funding.
    Explicit funding is a separate actual charge/credit, not included twice.
    """
    if not isinstance(statement, Mapping):
        raise SettlementError("BROKER_SETTLEMENT_STATEMENT_REQUIRED")
    expected = {"version":REPORT_VERSION, "source":"BROKER_SETTLEMENT_STATEMENT",
                "account_id":account, "instrument_uid":uid, "ticker":"CNYRUBF", "currency":"RUB", "status":"FINAL"}
    if any(statement.get(k) != v for k,v in expected.items()):
        raise SettlementError("BROKER_SETTLEMENT_STATEMENT_IDENTITY_MISMATCH")
    identifier(statement.get("statement_id"))
    lots(statement.get("revision"), positive=True)
    if not re.fullmatch(r"[a-f0-9]{64}", str(statement.get("artifact_sha256", ""))):
        raise SettlementError("BROKER_SETTLEMENT_ARTIFACT_DIGEST_REQUIRED")
    through, issued = utc(statement.get("settled_through")), utc(statement.get("issued_at"))
    if (utc(statement.get("period_from")) != utc(bound_at)
            or through > observed["requested_through"]
            or not through <= issued <= observed["observed_as_of"]
            or any(utc(f["executed_at"]) > through for f in fills)):
        raise SettlementError("BROKER_SETTLEMENT_STATEMENT_COVERAGE_MISMATCH")
    if statement.get("cash_operation_ids") != observed["operation_ids"]:
        raise SettlementError("BROKER_SETTLEMENT_OPERATION_COVERAGE_MISMATCH")
    for field in ("variation_margin_cash_rub", "explicit_funding_rub", "fee_cash_rub"):
        if exact(statement.get(field)) != observed[field]:
            raise SettlementError("BROKER_SETTLEMENT_CASH_TOTAL_MISMATCH")
    projected = project(fills, [], [], spec)
    if lots(statement.get("closing_signed_lots")) != projected["signed_lots"]:
        raise SettlementError("BROKER_SETTLEMENT_POSITION_MISMATCH")
    if through < observed["requested_through"] and projected["signed_lots"]:
        raise SettlementError("BROKER_SETTLEMENT_OPEN_EXPOSURE_AFTER_COVERAGE")
    with localcontext() as ctx:
        ctx.prec = 64
        economic = projected["realized_pnl_rub"]
        if projected["signed_lots"]:
            mark = spec.execution_price(statement.get("closing_settlement_price_points"))
            economic += (mark-projected["average_entry_price"])*projected["signed_lots"]*spec.rub_per_price_unit_per_lot
        elif statement.get("closing_settlement_price_points") is not None:
            raise SettlementError("BROKER_FLAT_SETTLEMENT_MARK_UNEXPECTED")
        embedded = exact(statement.get("embedded_funding_rub"))
        if economic != observed["variation_margin_cash_rub"] + embedded:
            raise SettlementError("BROKER_SETTLEMENT_ECONOMIC_BRIDGE_MISMATCH")
    fee_rows = statement.get("trade_fees")
    if not isinstance(fee_rows, list):
        raise SettlementError("BROKER_SETTLEMENT_ALL_FEE_COMPONENTS_REQUIRED")
    known, covered, order_fees = {f["trade_id"]:f for f in fills}, set(), {}
    for item in fee_rows:
        tid = identifier(item.get("trade_id"))
        fill = known.get(tid)
        if (fill is None or tid in covered or item.get("broker_order_id") != fill["broker_order_id"]
                or lots(item.get("quantity_lots"), positive=True) != fill["lots"]):
            raise SettlementError("BROKER_SETTLEMENT_FEE_OWNERSHIP_MISMATCH")
        covered.add(tid)
        fee = sum((exact(item.get(k), nonnegative=True) for k in
                   ("broker_commission_rub", "exchange_commission_rub", "clearing_commission_rub")), ZERO)
        target = order_fees.setdefault(fill["client_order_id"], {"client_order_id":fill["client_order_id"],
            "broker_order_id":fill["broker_order_id"], "filled_lots":0, "fee_rub":ZERO, "authoritative":True})
        target["filled_lots"] += fill["lots"]
        target["fee_rub"] += fee
    if covered != set(known):
        raise SettlementError("BROKER_SETTLEMENT_FEES_INCOMPLETE")
    other = exact(statement.get("other_fees_rub"), nonnegative=True)
    total_fees = sum((x["fee_rub"] for x in order_fees.values()), ZERO) + other
    if total_fees != observed["fee_cash_rub"]:
        raise SettlementError("BROKER_SETTLEMENT_FEE_CASH_MISMATCH")
    return {"embedded_funding_rub":embedded, "funding_rub":embedded+observed["explicit_funding_rub"],
            "other_fees_rub":other, "order_fees":list(order_fees.values()),
            "settlement_through":through, "statement_id":statement["statement_id"],
            "statement_revision":lots(statement["revision"], positive=True),
            "flat_after_settlement":projected["signed_lots"] == 0}


def assess(observation, *, account, uid, bound_at, fills, spec, now, previous=None, legacy_funding=False):
    previous = previous or {}
    if not fills and observation.get("no_managed_executions") is True:
        return {"version":VERSION, "status":"NO_MANAGED_EXECUTIONS", "funding_reconciled":True,
                "costs_reconciled":True, "reasons":[], "fill_fingerprint":fills_fingerprint(fills)}
    snapshot = observation.get("operations")
    if not isinstance(snapshot, Mapping):
        raise SettlementError("BROKER_OPERATIONS_OBSERVATION_REQUIRED")
    observed = _observed_operations(snapshot, account, uid, bound_at, fills, spec, now)
    reasons = set(observed["reasons"])
    if observation.get("statement_error"):
        reasons.add("BROKER_SETTLEMENT_STATEMENT_UNAVAILABLE")
    embedded = exact(previous.get("embedded_funding_rub", ZERO))
    result = {"version":VERSION, **observed, "retrieval_complete":True,
              "finality_proven":False, "funding_reconciled":False, "costs_reconciled":False,
              "fill_fingerprint":fills_fingerprint(fills), "embedded_funding_rub":embedded,
              "funding_rub":observed["explicit_funding_rub"]+embedded,
              "other_fees_rub":previous.get("other_fees_rub", ZERO),
              "order_fees":previous.get("order_fees", []), "settlement_through":None}
    statement = observation.get("statement")
    if statement is None:
        reasons.add("BROKER_SETTLEMENT_STATEMENT_REQUIRED")
    else:
        try:
            validated = validate_statement(statement, account=account, uid=uid, bound_at=bound_at,
                                           fills=fills, spec=spec, observed=observed)
            result.update(validated)
        except (LedgerError, KeyError, TypeError) as exc:
            reasons.add(str(exc) if isinstance(exc, LedgerError) else "BROKER_SETTLEMENT_STATEMENT_INVALID")
    if legacy_funding:
        reasons.add("MULTIPLE_FUNDING_AUTHORITIES_UNRESOLVED")
    if (utc(now)-observed["observed_as_of"]).total_seconds() > 30 or (utc(now)-observed["requested_through"]).total_seconds() > 30:
        reasons.add("BROKER_SETTLEMENT_OBSERVATION_STALE")
    result.update(reasons=sorted(reasons), status="RECONCILED_AS_OF" if not reasons else "INCOMPLETE",
                  funding_reconciled=not reasons, costs_reconciled=not reasons,
                  finality_proven=not reasons)
    return result


class CurrencySettlementReconciler:
    def __init__(self, ledger, broker, statement_provider=None, *, clock=None):
        self.ledger, self.broker, self.statement_provider = ledger, broker, statement_provider
        self.clock = clock or (lambda:datetime.now(timezone.utc))

    def _scope_on(self, c, account_id, instrument_uid):
        row = self.ledger._load(c, account_id, instrument_uid)
        cached = None
        if row["last_execution_at"] is not None:
            previous = c.execute(f"SELECT evidence FROM {SETTLEMENTS} WHERE account_id=%s AND instrument_uid=%s "
                "AND assessment->>'finality_proven'='true' ORDER BY observed_at DESC,statement_revision DESC LIMIT 1",
                (account_id,instrument_uid)).fetchone()
            if previous:
                cached = previous["evidence"].get("statement")
        return {"bound_at":row["bound_at"], "last_execution_at":row["last_execution_at"],
                "statement":cached}

    def collect(self, account_id, instrument_uid):
        """Broker reads outside the caller's accounting transaction."""
        if not self.ledger.enabled:
            return {"disabled":True}
        scope = self.ledger._run(self._scope_on, account_id, instrument_uid)
        if scope["last_execution_at"] is None:
            return {"no_managed_executions":True}
        through = utc(self.clock())
        snapshot = self.broker.get_operations(account_id, instrument_uid,
            from_time=utc(scope["bound_at"]).isoformat(), to_time=through.isoformat())
        statement = scope.get("statement")
        statement_error = None
        if self.statement_provider is not None:
            try:
                latest = self.statement_provider(account_id=account_id, instrument_uid=instrument_uid,
                    from_time=scope["bound_at"], to_time=through, operations=snapshot)
                if latest is not None:
                    statement = latest
            except Exception:
                # Preserve observed cash flows even if the independent document
                # reader is unavailable. Never expose a private provider error.
                statement_error = "BROKER_SETTLEMENT_STATEMENT_UNAVAILABLE"
        return {"operations":snapshot, "statement":statement, "statement_error":statement_error}

    def reconcile_on(self, c, account_id, instrument_uid, *, observation):
        if not self.ledger.enabled:
            return {"status":"DISABLED", "funding_reconciled":False, "costs_reconciled":False}
        row = self.ledger._load(c, account_id, instrument_uid)
        params = (account_id, instrument_uid)
        fills = list(c.execute(f"SELECT * FROM {FILLS} WHERE account_id=%s AND instrument_uid=%s", params).fetchall())
        legacy = c.execute(f"SELECT 1 FROM {FUNDING} WHERE account_id=%s AND instrument_uid=%s LIMIT 1", params).fetchone()
        result = assess(observation, account=account_id, uid=instrument_uid, bound_at=row["bound_at"],
            fills=fills, spec=self.ledger._spec(row), now=self.clock(), previous=row.get("settlement"), legacy_funding=bool(legacy))
        if result["status"] == "NO_MANAGED_EXECUTIONS":
            return result
        stamp, through = utc(result["observed_as_of"]), utc(result["requested_through"])
        previous = row.get("settlement") or {}
        if previous.get("observed_as_of") and stamp < utc(previous["observed_as_of"]):
            raise SettlementError("SETTLEMENT_OBSERVATION_OUT_OF_ORDER")
        if previous.get("requested_through") and through < utc(previous["requested_through"]):
            raise SettlementError("SETTLEMENT_COVERAGE_REGRESSED")
        statement = observation.get("statement")
        statement_id = result.get("statement_id")
        revision = result.get("statement_revision")
        if statement_id is not None:
            existing = c.execute(f"SELECT statement_revision,evidence FROM {SETTLEMENTS} "
                "WHERE account_id=%s AND instrument_uid=%s AND statement_id=%s "
                "ORDER BY statement_revision DESC LIMIT 1", params+(statement_id,)).fetchone()
            if existing:
                if revision < existing["statement_revision"]:
                    raise SettlementError("SETTLEMENT_STATEMENT_REVISION_REGRESSED")
                old = existing["evidence"].get("statement")
                if revision == existing["statement_revision"] and fingerprint(statement) != fingerprint(old):
                    raise SettlementError("SETTLEMENT_STATEMENT_REVISION_CONFLICT")
        digest = fingerprint(observation)
        evidence_json = json.dumps(_canonical(observation), separators=(",", ":"))
        assessment_json = json.dumps(_canonical(result), separators=(",", ":"))
        c.execute(f"INSERT INTO {SETTLEMENTS}(account_id,instrument_uid,observation_id,observed_at,requested_through,"
                  "statement_id,statement_revision,evidence,assessment,fingerprint) "
                  "VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) ON CONFLICT DO NOTHING",
                  params+(digest,stamp,through,statement_id,revision,evidence_json,assessment_json,digest))
        c.execute(f"UPDATE {ACCOUNTS} SET settlement=%s::jsonb WHERE account_id=%s AND instrument_uid=%s",
                  (assessment_json,)+params)
        row["settlement"] = _canonical(result)
        self.ledger._rebuild(c, row)
        return result

    def reconcile(self, account_id, instrument_uid, *, observation):
        return self.ledger._run(self.reconcile_on, account_id, instrument_uid, observation=observation)
