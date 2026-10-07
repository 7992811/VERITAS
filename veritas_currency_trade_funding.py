"""Bounded funding history and explicit, reviewed settlement attestations.

The broker cursor API is a history snapshot, not a settlement-finality oracle.
Its operation IDs may change. We therefore replace a complete half-open period
and post only the change in its accepted total. IDs deduplicate a *single read*;
they never identify a permanent cash posting. VM and commissions are audit-only.

A signed receipt is an owner's explicit review of a broker statement AND the
applicable settlement calendar. It does not certify authenticity of those files
or turn API pagination into broker finality. The trusted signer must check the
actual reports, their allocation attribution, and the next applicable clearing.
Use sign_statement_receipt offline with a separate key after that review. The
authenticated service accepts the receipt and signature, never a bare `settled`
boolean. No import, constructor, or disabled operation performs I/O.

All tables belong in the ledger's isolated production/sandbox schema. Locks are
taken ledger-account first; delta posting and accepted receipt commit together.
Unknown, corrected, expired, or incomplete evidence blocks *new risk* only;
this module neither approves orders nor obstructs reductions.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import hmac
import json
import re
import uuid

from veritas_currency_trade_ledger import (
    ACCOUNTS, FILLS, LedgerError, _canonical, _require_transaction,
    exact, fingerprint, identifier, utc,
)

VERSION = "currency-funding-statement-v1"
AUTHORITY = "OWNER_REVIEWED_BROKER_STATEMENT_AND_SETTLEMENT_CALENDAR"
WINDOWS = "veritas_currency_funding_windows"
SNAPSHOTS = "veritas_currency_funding_snapshots"
RECEIPTS = "veritas_currency_funding_receipts"
ZERO = Decimal("0")
TYPE_FUNDING = 70
TYPE_VM = {26, 27}
TYPE_COMMISSION = {14, 19, 66}
UNSUPPORTED_ALLOCATION_COST = {14, 66}
TYPES = {"OPERATION_TYPE_FUNDING": 70,
         "OPERATION_TYPE_ACCRUING_VARMARGIN": 26,
         "OPERATION_TYPE_WRITING_OFF_VARMARGIN": 27,
         "OPERATION_TYPE_BROKER_FEE": 19,
         "OPERATION_TYPE_MARGIN_FEE": 14,
         "OPERATION_TYPE_OTHER_FEE": 66}
STATES = {"OPERATION_STATE_EXECUTED": 1, "OPERATION_STATE_CANCELED": 2,
          "OPERATION_STATE_PROGRESS": 3}
DECLARATIONS = ("statement_period_complete", "allocation_attribution_verified",
                "calendar_verified", "no_unreported_settlement_before_next_due")


class FundingError(ValueError):
    """Stable error code, never private payload or key material."""


def _json(value):
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FundingError("SHA256_REQUIRED")
    return value


def _key(value):
    if isinstance(value, str):
        value = value.encode()
    if not isinstance(value, bytes) or len(value) < 32:
        raise FundingError("SEPARATE_STATEMENT_KEY_REQUIRED")
    return value


def sign_statement_receipt(statement_key, receipt):
    """Offline issuance helper; signing asserts review, it does not do the review."""
    if not isinstance(receipt, Mapping):
        raise FundingError("STATEMENT_RECEIPT_REQUIRED")
    return hmac.new(_key(statement_key), (VERSION+"\n"+_json(receipt)).encode(), hashlib.sha256).hexdigest()


def verify_statement_signature(statement_key, receipt, signature):
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise FundingError("STATEMENT_SIGNATURE_INVALID")
    expected = sign_statement_receipt(statement_key, receipt)
    if not hmac.compare_digest(expected, signature):
        raise FundingError("STATEMENT_SIGNATURE_INVALID")


def _integer(value):
    number = exact(value)
    if number != number.to_integral_value() or abs(number) > 2**63-1:
        raise FundingError("BROKER_INTEGER_REQUIRED")
    return int(number)


def _enum(value, names):
    if isinstance(value, str) and value in names:
        return names[value]
    if isinstance(value, str) and value.startswith("OPERATION_"):
        return value
    return _integer(value)


def _money(value):
    if not isinstance(value, Mapping) or str(value.get("currency", "")).upper() != "RUB":
        raise FundingError("ACTUAL_RUB_PAYMENT_REQUIRED")
    units, nano = _integer(value.get("units", 0)), _integer(value.get("nano", 0))
    if abs(nano) >= 1_000_000_000 or (units > 0 and nano < 0) or (units < 0 and nano > 0):
        raise FundingError("BROKER_MONEY_INVALID")
    with localcontext() as context:
        context.prec = 64
        return Decimal(units)+Decimal(nano)/Decimal(1_000_000_000)


def _sum(values):
    with localcontext() as context:
        context.prec = 64
        return sum(values, ZERO)


def _funding_payment(item, instrument_uid):
    """Attribute a payment once; never add a parent and its children together."""
    uid = item.get("instrumentUid") or ""
    children = item.get("childOperations", [])
    if not isinstance(children, list):
        raise FundingError("FUNDING_CHILDREN_INVALID")
    if uid and uid != instrument_uid:
        if any(isinstance(x, Mapping) and x.get("instrumentUid") == instrument_uid for x in children):
            raise FundingError("FUNDING_ATTRIBUTION_CONFLICT")
        return None
    if children:
        attributed, whole = [], []
        for child in children:
            if not isinstance(child, Mapping) or not child.get("instrumentUid"):
                raise FundingError("FUNDING_ATTRIBUTION_UNRESOLVED")
            child_uid = identifier(child["instrumentUid"])
            payment = _money(child.get("payment"))
            whole.append(payment)
            if uid and child_uid != uid:
                raise FundingError("FUNDING_ATTRIBUTION_CONFLICT")
            if child_uid == instrument_uid:
                attributed.append(payment)
        if _sum(whole) != _money(item.get("payment")):
            raise FundingError("FUNDING_CHILDREN_TOTAL_MISMATCH")
        return _sum(attributed) if uid == instrument_uid or any(x["instrumentUid"] == instrument_uid for x in children) else None
    if uid == instrument_uid:
        return _money(item.get("payment"))
    raise FundingError("FUNDING_ATTRIBUTION_UNRESOLVED")


def _check_other_allocation_cost(item, instrument_uid):
    """Order commissions do not prove separately charged margin/other fees paid.

    A known foreign instrument or explicitly cancelled operation is irrelevant.
    An unattributed nonzero expense can belong to this allocation and therefore
    cannot be silently omitted from a claim that its actual costs reconcile.
    """
    code = "NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED"
    uid = item.get("instrumentUid") or ""
    children = item.get("childOperations", [])
    if not isinstance(children, list):
        raise FundingError(code)
    child_is_target = any(isinstance(child, Mapping) and child.get("instrumentUid") == instrument_uid
                          for child in children)
    if uid and uid != instrument_uid and not child_is_target:
        return
    try:
        state = _enum(item.get("state"), STATES)
        if state == 2:
            return
        if state not in (1, 3):
            raise FundingError(code)
        payment = _money(item.get("payment"))
        # A net-zero parent can contain a real allocation debit offset by a
        # credit to another child instrument; inspect relevant child amounts.
        child_payments = [_money(child.get("payment")) for child in children
                          if isinstance(child, Mapping) and child.get("instrumentUid") == instrument_uid]
        if payment or any(child_payments):
            raise FundingError(code)
    except (FundingError, LedgerError):
        raise FundingError(code) from None


def collect_snapshot(operations_reader, account_id, instrument_uid, *, window_start,
                     window_end, max_pages=100, page_size=1000, max_items=50000):
    """Collect one bounded history read. `complete_fetch` is NOT finality.

    Reader receives account_id and from_time/to_time/cursor/limit keyword args.
    It must request all operation types/states and must not exclude commissions,
    trades, or overnight operations. Local filtering enforces [start,end) even
    when an API includes its upper boundary. Duplicate IDs within pages must be
    byte-equivalent after removing only the per-row cursor.
    """
    account_id, instrument_uid = identifier(account_id), identifier(instrument_uid)
    start, end = utc(window_start), utc(window_end)
    if start >= end:
        raise FundingError("SETTLEMENT_WINDOW_INVALID")
    if (type(max_pages) is not int or max_pages < 1 or type(page_size) is not int
            or not 3 <= page_size <= 1000 or type(max_items) is not int or max_items < 1):
        raise FundingError("BOUNDED_PAGINATION_REQUIRED")
    cursor, cursors, by_id, funding, audit = "", set(), {}, [], []
    raw_count, pages = 0, 0
    pending = 0
    for _ in range(max_pages):
        page = operations_reader(account_id, from_time=start, to_time=end, cursor=cursor, limit=page_size)
        pages += 1
        if not isinstance(page, Mapping) or type(page.get("hasNext", False)) is not bool:
            raise FundingError("OPERATIONS_PAGE_INVALID")
        items = page.get("items", [])
        if not isinstance(items, list):
            raise FundingError("OPERATIONS_PAGE_INVALID")
        raw_count += len(items)
        if raw_count > max_items:
            raise FundingError("OPERATIONS_ITEM_LIMIT")
        for item in items:
            if not isinstance(item, Mapping) or item.get("brokerAccountId") != account_id:
                raise FundingError("OPERATION_ACCOUNT_MISMATCH")
            oid = identifier(item.get("id"))
            raw_digest = fingerprint({k: v for k, v in item.items() if k != "cursor"})
            if oid in by_id:
                if by_id[oid] != raw_digest:
                    raise FundingError("OPERATION_CHANGED_DURING_READ")
                continue
            by_id[oid] = raw_digest
            occurred = utc(item.get("date"))
            if not start <= occurred < end:
                continue
            op_type = _enum(item.get("type"), TYPES)
            if op_type in UNSUPPORTED_ALLOCATION_COST:
                _check_other_allocation_cost(item, instrument_uid)
            if op_type != TYPE_FUNDING:
                if item.get("instrumentUid") == instrument_uid and op_type in TYPE_VM | TYPE_COMMISSION:
                    audit.append({"id": oid, "type": op_type, "date": occurred,
                                  "classification": "VM_NOT_POSTED" if op_type in TYPE_VM else "COMMISSION_NOT_POSTED"})
                continue
            payment = _funding_payment(item, instrument_uid)
            if payment is None:
                continue
            state = _enum(item.get("state"), STATES)
            if state not in (1, 2, 3):
                raise FundingError("FUNDING_STATE_UNKNOWN")
            if state == 3:
                pending += 1
            funding.append({"date": occurred, "state": state, "payment_rub": payment})
            audit.append({"id": oid, "parent_id": item.get("parentOperationId") or None,
                          "type": 70, "date": occurred, "state": state, "payment_rub": payment})
        if not page.get("hasNext", False):
            break
        next_cursor = page.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor or next_cursor in cursors:
            raise FundingError("OPERATIONS_CURSOR_STALLED")
        cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise FundingError("OPERATIONS_PAGE_LIMIT")
    # Multiset, not set: two equal real charges remain two charges. A duplicated
    # alias under different mutable IDs requires the statement reviewer to reject
    # the resulting total; the importer cannot infer that it is the same payment.
    funding.sort(key=_json)
    with localcontext() as context:
        context.prec = 64
        total = -sum((row["payment_rub"] for row in funding if row["state"] == 1), ZERO)
    economics = {"window_start": start, "window_end": end, "funding": funding}
    return {"complete_fetch": True, "broker_finality": False,
            "snapshot_digest": fingerprint(economics), "funding_cost_rub": total,
            "pending_funding_count": pending, "funding_count": len(funding),
            "pages": pages, "raw_items": raw_count, "audit": audit, "economics": economics}


def validate_receipt(receipt, *, expected, now):
    """Validate signed declarations against independent stored observations."""
    if not isinstance(receipt, Mapping):
        raise FundingError("STATEMENT_RECEIPT_REQUIRED")
    now = utc(now)
    for key, value in expected.items():
        if key == "funding_cost_rub":
            matches = exact(receipt.get(key)) == exact(value)
        elif key in ("bound_at", "window_start", "window_end"):
            matches = utc(receipt.get(key)) == utc(value)
        else:
            matches = receipt.get(key) == value and type(receipt.get(key)) is type(value)
        if not matches:
            raise FundingError("STATEMENT_SCOPE_OR_SNAPSHOT_MISMATCH")
    if receipt.get("version") != VERSION or receipt.get("authority") != AUTHORITY:
        raise FundingError("EXPLICIT_OWNER_STATEMENT_AUTHORITY_REQUIRED")
    if any(receipt.get(key) is not True for key in DECLARATIONS):
        raise FundingError("EXPLICIT_STATEMENT_DECLARATIONS_REQUIRED")
    for key in ("statement_sha256", "calendar_evidence_sha256"):
        _sha(receipt.get(key))
    for key in ("statement_reference", "calendar_evidence_reference"):
        identifier(receipt.get(key))
    start, end = utc(receipt["window_start"]), utc(receipt["window_end"])
    issued, reviewed = utc(receipt.get("statement_issued_at")), utc(receipt.get("reviewed_at"))
    due = utc(receipt.get("next_settlement_due_at"))
    if (start < utc(receipt["bound_at"]) or start >= end or end > now
            or utc(receipt.get("settled_through")) != end or issued < end
            or reviewed < issued or reviewed > now or due <= reviewed or due <= end):
        raise FundingError("STATEMENT_CUTOFF_INVALID")
    return {"settled_through": end, "next_settlement_due_at": due, "reviewed_at": reviewed}


def _validate_stored_snapshot(snapshot):
    payload = snapshot.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("economics"), Mapping):
        raise FundingError("STORED_SNAPSHOT_INTEGRITY_INVALID")
    economics = payload["economics"]
    funding = economics.get("funding")
    if not isinstance(funding, list):
        raise FundingError("STORED_SNAPSHOT_INTEGRITY_INVALID")
    try:
        total = _sum(exact(item["payment_rub"]) for item in funding if item["state"] == 1).copy_negate()
        pending = sum(item["state"] == 3 for item in funding)
        valid = (fingerprint(economics) == snapshot["snapshot_digest"] == payload.get("snapshot_digest")
                 and total == exact(snapshot["funding_cost_rub"]) == exact(payload.get("funding_cost_rub"))
                 and pending == snapshot["pending_funding_count"] == payload.get("pending_funding_count")
                 and payload.get("complete_fetch") is True and payload.get("broker_finality") is False
                 and utc(economics["window_start"]) == snapshot["window_start"]
                 and utc(economics["window_end"]) == snapshot["window_end"])
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise FundingError("STORED_SNAPSHOT_INTEGRITY_INVALID")


class FundingReconciler:
    def __init__(self, connect, ledger, operations_reader, *, environment, owner_user_id,
                 statement_key, clock=lambda: datetime.now(timezone.utc), enabled=False,
                 max_pages=100, page_size=1000, max_items=50000,
                 max_observation_age_seconds=60, max_windows=366):
        if (type(enabled) is not bool or environment not in ("production", "sandbox")
                or not callable(connect) or not callable(clock) or not callable(operations_reader)):
            raise FundingError("EXPLICIT_FUNDING_CONFIGURATION_REQUIRED")
        self.owner_user_id = _integer(owner_user_id)
        if self.owner_user_id <= 0:
            raise FundingError("OWNER_REQUIRED")
        self._statement_key = None if statement_key in (None, "", b"") else _key(statement_key)
        if (type(max_observation_age_seconds) is not int or not 1 <= max_observation_age_seconds <= 300
                or type(max_windows) is not int or not 1 <= max_windows <= 1000):
            raise FundingError("BOUNDED_REFRESH_CONFIGURATION_REQUIRED")
        self.max_observation_age_seconds, self.max_windows = max_observation_age_seconds, max_windows
        self.connect, self.ledger, self.operations_reader = connect, ledger, operations_reader
        self.environment, self.clock, self.enabled = environment, clock, enabled
        self.bounds = {"max_pages": max_pages, "page_size": page_size, "max_items": max_items}

    def __repr__(self):
        return f"FundingReconciler(enabled={self.enabled}, environment={self.environment!r})"

    @staticmethod
    def _disabled():
        return {"status": "DISABLED", "reconciled": False, "block_reason": "FUNDING_RECONCILIATION_DISABLED"}

    def _run(self, method, *args, **kwargs):
        if not self.enabled:
            return self._disabled()
        with self.connect() as c, c.transaction():
            return method(c, *args, **kwargs)

    def ensure_schema(self):
        return self._run(self.ensure_schema_on)

    def ensure_schema_on(self, c):
        if not self.enabled:
            return self._disabled()
        _require_transaction(c)
        c.execute(f"""
        CREATE TABLE IF NOT EXISTS {WINDOWS}(
          window_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL,
          environment TEXT NOT NULL CHECK(environment IN ('production','sandbox')),
          window_start TIMESTAMPTZ NOT NULL, window_end TIMESTAMPTZ NOT NULL,
          current_snapshot_id TEXT, accepted_receipt_id TEXT, accepted_cost_rub NUMERIC NOT NULL DEFAULT 0,
          refresh_failure TEXT, read_attempt_token TEXT,
          UNIQUE(account_id,instrument_uid,window_start,window_end), CHECK(window_start<window_end),
          FOREIGN KEY(account_id,instrument_uid) REFERENCES {ACCOUNTS}(account_id,instrument_uid)
        );
        ALTER TABLE {WINDOWS} ADD COLUMN IF NOT EXISTS refresh_failure TEXT;
        ALTER TABLE {WINDOWS} ADD COLUMN IF NOT EXISTS read_attempt_token TEXT;
        CREATE TABLE IF NOT EXISTS {SNAPSHOTS}(
          snapshot_id TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES {WINDOWS}(window_id),
          snapshot_digest TEXT NOT NULL, fill_digest TEXT NOT NULL, funding_cost_rub NUMERIC NOT NULL,
          pending_funding_count BIGINT NOT NULL, read_started_at TIMESTAMPTZ NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {RECEIPTS}(
          receipt_id TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES {WINDOWS}(window_id),
          snapshot_id TEXT NOT NULL REFERENCES {SNAPSHOTS}(snapshot_id),
          snapshot_digest TEXT NOT NULL, fill_digest TEXT NOT NULL, funding_cost_rub NUMERIC NOT NULL,
          delta_cost_rub NUMERIC NOT NULL, settled_through TIMESTAMPTZ NOT NULL,
          next_settlement_due_at TIMESTAMPTZ NOT NULL, accepted_at TIMESTAMPTZ NOT NULL,
          receipt JSONB NOT NULL, signature TEXT NOT NULL
        );
        """)
        return {"status": "READY", "version": VERSION}

    def _binding(self, c, account_id, instrument_uid):
        _require_transaction(c)
        row = self.ledger._load(c, account_id, instrument_uid)
        if row["owner_user_id"] != self.owner_user_id:
            raise FundingError("BOUND_OWNER_MISMATCH")
        return row

    def _scope(self, row):
        return {"version": VERSION, "authority": AUTHORITY, "account_id": row["account_id"],
                "instrument_uid": row["instrument_uid"], "environment": self.environment,
                "owner_user_id": self.owner_user_id, "bound_at": utc(row["bound_at"]).isoformat(),
                "spec_revision": row["spec_revision"]}

    @staticmethod
    def _fill_digest(c, row, end):
        # Prefix includes earlier holdings and late executions; posting funding
        # changes ledger_revision but must not invalidate its own fill evidence.
        rows = c.execute(f"SELECT trade_id,fingerprint FROM {FILLS} WHERE account_id=%s AND instrument_uid=%s "
                         "AND executed_at<%s ORDER BY trade_id",
                         (row["account_id"], row["instrument_uid"], end)).fetchall()
        return fingerprint([dict(item) for item in rows])

    def _window(self, c, row, start, end):
        if start < row["bound_at"] or start >= end or end > utc(self.clock()):
            raise FundingError("SETTLEMENT_WINDOW_OUTSIDE_ALLOCATION")
        windows = c.execute(f"SELECT * FROM {WINDOWS} WHERE account_id=%s AND instrument_uid=%s "
                            "ORDER BY window_start", (row["account_id"], row["instrument_uid"])).fetchall()
        for window in windows:
            if window["environment"] != self.environment:
                raise FundingError("FUNDING_ENVIRONMENT_MISMATCH")
            if window["window_start"] == start and window["window_end"] == end:
                return dict(window)
            if start < window["window_end"] and end > window["window_start"]:
                raise FundingError("SETTLEMENT_WINDOWS_OVERLAP")
        expected = windows[-1]["window_end"] if windows else row["bound_at"]
        if len(windows) >= self.max_windows:
            raise FundingError("SETTLEMENT_WINDOW_LIMIT")
        if start != expected:
            raise FundingError("SETTLEMENT_WINDOW_GAP")
        window_id = fingerprint({**self._scope(row), "start": start, "end": end})
        c.execute(f"INSERT INTO {WINDOWS}(window_id,account_id,instrument_uid,environment,window_start,window_end) "
                  "VALUES(%s,%s,%s,%s,%s,%s)",
                  (window_id,row["account_id"],row["instrument_uid"],self.environment,start,end))
        return {"window_id": window_id, "window_start": start, "window_end": end,
                "current_snapshot_id": None, "accepted_receipt_id": None, "accepted_cost_rub": ZERO}

    def observe(self, account_id, instrument_uid, *, window_start, window_end):
        if not self.enabled:
            return self._disabled()
        start, end = utc(window_start), utc(window_end)
        started = utc(self.clock())
        attempt_token = uuid.uuid4().hex
        with self.connect() as c, c.transaction():
            row = self._binding(c, account_id, instrument_uid)
            # Validate/create before network I/O. A failed read leaves an
            # unaccepted window and therefore cannot accidentally grant admission.
            window = self._window(c, row, start, end)
            c.execute(f"UPDATE {WINDOWS} SET refresh_failure=%s,read_attempt_token=%s WHERE window_id=%s",
                      ("FUNDING_HISTORY_REFRESH_INCOMPLETE",attempt_token,window["window_id"]))
        try:
            snapshot = collect_snapshot(self.operations_reader, account_id, instrument_uid,
                                        window_start=start, window_end=end, **self.bounds)
        except FundingError as error:
            if str(error) == "NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED":
                with self.connect() as c, c.transaction():
                    self._binding(c, account_id, instrument_uid)
                    c.execute(f"UPDATE {WINDOWS} SET refresh_failure=%s WHERE window_id=%s AND read_attempt_token=%s",
                              (str(error),window["window_id"],attempt_token))
            raise
        observed = utc(self.clock())
        if observed < started:
            raise FundingError("OBSERVATION_CLOCK_REVERSED")
        with self.connect() as c, c.transaction():
            row = self._binding(c, account_id, instrument_uid)
            window = self._window(c, row, start, end)
            # Account lock serializes attempt ownership, including failed reads.
            # Timestamps alone cannot order two attempts at the same clock tick,
            # and a newer failure need not have produced any snapshot at all.
            if window.get("read_attempt_token") != attempt_token:
                raise FundingError("OBSERVATION_SUPERSEDED")
            if window["current_snapshot_id"]:
                prior = c.execute(f"SELECT read_started_at FROM {SNAPSHOTS} WHERE snapshot_id=%s",
                                  (window["current_snapshot_id"],)).fetchone()
                if prior and prior["read_started_at"] > started:
                    raise FundingError("OBSERVATION_SUPERSEDED")
            snapshot_id = uuid.uuid4().hex
            fill_digest = self._fill_digest(c, row, end)
            c.execute(f"INSERT INTO {SNAPSHOTS}(snapshot_id,window_id,snapshot_digest,fill_digest,funding_cost_rub,"
                      "pending_funding_count,read_started_at,observed_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                      (snapshot_id,window["window_id"],snapshot["snapshot_digest"],fill_digest,snapshot["funding_cost_rub"],
                       snapshot["pending_funding_count"],started,observed,_json(snapshot)))
            c.execute(f"UPDATE {WINDOWS} SET current_snapshot_id=%s,refresh_failure=NULL "
                      "WHERE window_id=%s AND read_attempt_token=%s",
                      (snapshot_id,window["window_id"],attempt_token))
            expected = {**self._scope(row), "window_start": start.isoformat(), "window_end": end.isoformat(),
                        "snapshot_id": snapshot_id, "snapshot_digest": snapshot["snapshot_digest"],
                        "fill_digest": fill_digest, "funding_cost_rub": _canonical(snapshot["funding_cost_rub"])}
            return {"status": "OBSERVED_REQUIRES_STATEMENT_REVIEW", "broker_finality": False,
                    "snapshot_id": snapshot_id, "snapshot_digest": snapshot["snapshot_digest"],
                    "funding_cost_rub": expected["funding_cost_rub"], "pending_funding_count": snapshot["pending_funding_count"],
                    "receipt_template": expected, "observed_at": observed.isoformat(),
                    "reconciliation": self.status_on(c, account_id, instrument_uid, now=observed)}

    def attest(self, account_id, instrument_uid, *, receipt, signature):
        if not self.enabled:
            return self._disabled()
        if self._statement_key is None:
            raise FundingError("STATEMENT_ATTESTATION_NOT_CONFIGURED")
        verify_statement_signature(self._statement_key, receipt, signature)
        return self._run(self.attest_on, account_id, instrument_uid, receipt=receipt, signature=signature)

    def attest_on(self, c, account_id, instrument_uid, *, receipt, signature):
        if not self.enabled:
            return self._disabled()
        if self._statement_key is None:
            raise FundingError("STATEMENT_ATTESTATION_NOT_CONFIGURED")
        verify_statement_signature(self._statement_key, receipt, signature)
        row = self._binding(c, account_id, instrument_uid)
        receipt_id = fingerprint({"receipt": receipt, "signature": signature})
        old = c.execute(f"SELECT receipt_id FROM {RECEIPTS} WHERE receipt_id=%s", (receipt_id,)).fetchone()
        # Scope check precedes the replay shortcut too.
        for key, value in self._scope(row).items():
            if receipt.get(key) != value or type(receipt.get(key)) is not type(value):
                raise FundingError("STATEMENT_SCOPE_OR_SNAPSHOT_MISMATCH")
        if old:
            return {"status": "DUPLICATE", "receipt_id": receipt_id, "delta_cost_rub": "0",
                    "reconciliation": self.status_on(c, account_id, instrument_uid)}
        snapshot = c.execute(f"SELECT s.*,w.account_id,w.instrument_uid,w.environment,w.window_start,w.window_end,"
                             "w.current_snapshot_id,w.accepted_cost_rub FROM "+SNAPSHOTS+" s JOIN "+WINDOWS+
                             " w ON w.window_id=s.window_id WHERE s.snapshot_id=%s",
                             (identifier(receipt.get("snapshot_id")),)).fetchone()
        if (not snapshot or snapshot["account_id"] != account_id or snapshot["instrument_uid"] != instrument_uid
                or snapshot["environment"] != self.environment or snapshot["current_snapshot_id"] != snapshot["snapshot_id"]):
            raise FundingError("STATEMENT_SNAPSHOT_SUPERSEDED")
        _validate_stored_snapshot(snapshot)
        if snapshot["pending_funding_count"]:
            raise FundingError("PENDING_FUNDING_OPERATIONS")
        if (utc(self.clock())-snapshot["read_started_at"]).total_seconds() > self.max_observation_age_seconds:
            raise FundingError("FUNDING_HISTORY_REFRESH_REQUIRED")
        fill_digest = self._fill_digest(c, row, snapshot["window_end"])
        if fill_digest != snapshot["fill_digest"]:
            raise FundingError("SETTLEMENT_FILL_EVIDENCE_CHANGED")
        expected = {**self._scope(row), "window_start": snapshot["window_start"].isoformat(),
                    "window_end": snapshot["window_end"].isoformat(), "snapshot_id": snapshot["snapshot_id"],
                    "snapshot_digest": snapshot["snapshot_digest"], "fill_digest": fill_digest,
                    "funding_cost_rub": snapshot["funding_cost_rub"]}
        now = utc(self.clock())
        dates = validate_receipt(receipt, expected=expected, now=now)
        if dates["reviewed_at"] < snapshot["observed_at"] or dates["next_settlement_due_at"] <= now:
            raise FundingError("STATEMENT_REVIEW_STALE")
        delta = _sum([exact(snapshot["funding_cost_rub"]), exact(snapshot["accepted_cost_rub"]).copy_negate()])
        # The immutable adjustment identifies an accepted *window revision*, not
        # a mutable broker operation ID. Zero totals still record the receipt.
        if delta:
            self.ledger.record_funding_on(c, account_id, instrument_uid,
                adjustment_id="settlement-"+receipt_id, cost_rub=delta, occurred_at=snapshot["window_end"])
        c.execute(f"INSERT INTO {RECEIPTS}(receipt_id,window_id,snapshot_id,snapshot_digest,fill_digest,funding_cost_rub,"
                  "delta_cost_rub,settled_through,next_settlement_due_at,accepted_at,receipt,signature) "
                  "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                  (receipt_id,snapshot["window_id"],snapshot["snapshot_id"],snapshot["snapshot_digest"],fill_digest,
                   snapshot["funding_cost_rub"],delta,dates["settled_through"],dates["next_settlement_due_at"],now,_json(receipt),signature))
        c.execute(f"UPDATE {WINDOWS} SET accepted_receipt_id=%s,accepted_cost_rub=%s WHERE window_id=%s",
                  (receipt_id,snapshot["funding_cost_rub"],snapshot["window_id"]))
        return {"status": "OWNER_ATTESTATION_ACCEPTED", "receipt_id": receipt_id,
                "delta_cost_rub": _canonical(delta), "reconciliation": self.status_on(c, account_id, instrument_uid, now=now)}

    def status(self, account_id, instrument_uid, *, now=None):
        return self._run(self.status_on, account_id, instrument_uid, now=now)

    def refresh(self, account_id, instrument_uid):
        """Refresh every known period before new risk; never hold locks over I/O.

        No new interval or calendar boundary is invented. A late correction to
        *any* prior window invalidates its attestation; the owner must review a
        fresh receipt before new risk resumes. Page/read failures remain durable.
        """
        if not self.enabled:
            return self._disabled()
        with self.connect() as c, c.transaction():
            self._binding(c, account_id, instrument_uid)
            windows = c.execute(f"SELECT window_start,window_end FROM {WINDOWS} WHERE account_id=%s "
                                "AND instrument_uid=%s ORDER BY window_start", (account_id,instrument_uid)).fetchall()
        if len(windows) > self.max_windows:
            raise FundingError("SETTLEMENT_WINDOW_LIMIT")
        for window in windows:
            self.observe(account_id, instrument_uid, window_start=window["window_start"], window_end=window["window_end"])
        return self.status(account_id, instrument_uid)

    def status_on(self, c, account_id, instrument_uid, *, now=None):
        if not self.enabled:
            return self._disabled()
        row = self._binding(c, account_id, instrument_uid)
        now = utc(self.clock() if now is None else now)
        observed_window_end = row["bound_at"]
        def result(code, ok=False, **fields):
            return {"status": "RECONCILED_BY_OWNER_ATTESTATION" if ok else "UNRECONCILED",
                    "reconciled": ok, "block_reason": None if ok else code, "broker_finality": False,
                    "bound_at": row["bound_at"].isoformat(),
                    "observed_window_end": observed_window_end.isoformat(), **fields}
        windows = c.execute(f"SELECT w.*,s.snapshot_digest,s.fill_digest,s.pending_funding_count,s.read_started_at,s.payload,"
                            "s.funding_cost_rub AS observed_cost_rub,r.snapshot_id AS accepted_snapshot_id,"
                            "r.funding_cost_rub AS receipt_cost_rub,r.settled_through AS accepted_through,"
                            "r.snapshot_digest AS accepted_digest,r.fill_digest AS accepted_fill_digest,"
                            "r.next_settlement_due_at,r.receipt,r.signature FROM "+WINDOWS+" w LEFT JOIN "+SNAPSHOTS+
                            " s ON s.snapshot_id=w.current_snapshot_id LEFT JOIN "+RECEIPTS+
                            " r ON r.receipt_id=w.accepted_receipt_id WHERE w.account_id=%s AND w.instrument_uid=%s "
                            "ORDER BY w.window_start", (account_id,instrument_uid)).fetchall()
        if windows:
            observed_window_end = windows[-1]["window_end"]
        has_fills = c.execute(f"SELECT trade_id FROM {FILLS} WHERE account_id=%s AND instrument_uid=%s LIMIT 1",
                             (account_id,instrument_uid)).fetchone() is not None
        if not windows:
            if not has_fills and exact(row["funding_rub"]) == 0:
                return {**result(None, True), "status": "NO_EXECUTIONS", "settled_through": row["bound_at"].isoformat()}
            if self._statement_key is None:
                return result("STATEMENT_ATTESTATION_NOT_CONFIGURED")
            return result("FUNDING_COMPLETENESS_UNVERIFIED")
        if self._statement_key is None:
            return result("STATEMENT_ATTESTATION_NOT_CONFIGURED")
        frontier, accepted_total, next_due = row["bound_at"], ZERO, None
        for window in windows:
            if window["environment"] != self.environment:
                return result("FUNDING_ENVIRONMENT_MISMATCH")
            if window["window_start"] != frontier:
                return result("SETTLEMENT_WINDOW_GAP")
            if window["refresh_failure"]:
                reason = window["refresh_failure"]
                return result(reason if reason == "NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED"
                              else "FUNDING_HISTORY_REFRESH_INCOMPLETE")
            if window["read_started_at"] is None or not 0 <= (now-window["read_started_at"]).total_seconds() <= self.max_observation_age_seconds:
                return result("FUNDING_HISTORY_REFRESH_REQUIRED")
            if not window["accepted_receipt_id"]:
                return result("FUNDING_STATEMENT_ATTESTATION_REQUIRED")
            try:
                verify_statement_signature(self._statement_key, window["receipt"], window["signature"])
                signed = window["receipt"]
                expected = {**self._scope(row), "window_start": window["window_start"].isoformat(),
                            "window_end": window["window_end"].isoformat(),
                            "snapshot_id": window["accepted_snapshot_id"],
                            "snapshot_digest": window["accepted_digest"], "fill_digest": window["accepted_fill_digest"],
                            "funding_cost_rub": window["accepted_cost_rub"]}
                dates = validate_receipt(signed, expected=expected, now=now)
                if (window["accepted_receipt_id"] != fingerprint({"receipt": signed, "signature": window["signature"]})
                        or dates["next_settlement_due_at"] != window["next_settlement_due_at"]
                        or dates["settled_through"] != window["accepted_through"]
                        or exact(window["receipt_cost_rub"]) != exact(window["accepted_cost_rub"])):
                    raise FundingError("STORED_STATEMENT_INTEGRITY_INVALID")
                _validate_stored_snapshot({**window, "funding_cost_rub": window["observed_cost_rub"]})
            except (FundingError, LedgerError):
                return result("STORED_STATEMENT_OR_SNAPSHOT_INVALID")
            if (window["snapshot_digest"] != window["accepted_digest"] or window["pending_funding_count"]):
                return result("SETTLEMENT_HISTORY_CORRECTED")
            current_fills = self._fill_digest(c, row, window["window_end"])
            if window["fill_digest"] != window["accepted_fill_digest"] or current_fills != window["accepted_fill_digest"]:
                return result("SETTLEMENT_FILL_EVIDENCE_CHANGED")
            frontier = window["window_end"]
            accepted_total = _sum([accepted_total, exact(window["accepted_cost_rub"])])
            next_due = window["next_settlement_due_at"]
        if accepted_total != exact(row["funding_rub"]):
            return result("FUNDING_LEDGER_TOTAL_MISMATCH")
        if now < frontier or now >= next_due:
            return result("SETTLEMENT_CHECKPOINT_DUE", settled_through=frontier.isoformat(),
                          next_settlement_due_at=next_due.isoformat())
        return result(None, True, settled_through=frontier.isoformat(), next_settlement_due_at=next_due.isoformat(),
                      funding_cost_rub=_canonical(accepted_total), authority=AUTHORITY)
