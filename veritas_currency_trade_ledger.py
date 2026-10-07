"""Explicit, disabled-by-default Currency allocation ledger for real executions.

Only exact broker trade executions change exposure. ACKs, aggregate order prices
and research positions are not inputs. The supplied PostgreSQL connection must
return mapping rows. Every public database operation uses an explicit transaction
even with autocommit connections; *_on methods require a caller-owned active
transaction and allow atomic ingestion with an approval repository.

The allocation is 10,000 RUB per explicitly bound account. A binding owns exactly
one instrument UID; contract migration and clearing-basis resets are deliberately
not inferred. Futures P&L uses exchange point value, never margin as notional.
Variation margin is not a second cash credit on top of this mark-to-market P&L.
Funding requires actual broker charge/credit evidence. The settlement reconciler
can retain a complete observation as an authoritative replacement, so a broker
correction is not a second fictional event. Fee observations are cumulative per
order and never added twice to individual execution commissions.

No imports initialize a database, inspect environment/credentials, contact a
broker, or enable execution. Callers supply verified broker snapshots; this
module cannot establish their provenance. A position mismatch latches an entry
freeze. Recording actual fills and valuing the position remain available while
frozen; exit authorization belongs to the separate reduce-only execution path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from collections.abc import Mapping


VERSION = "currency-live-ledger-v1"
ALLOCATION_RUB = Decimal("10000")
ACCOUNTS = "veritas_currency_live_accounts"
FILLS = "veritas_currency_live_fills"
FEES = "veritas_currency_live_fees"
FUNDING = "veritas_currency_live_funding"
RECONCILIATIONS = "veritas_currency_live_reconciliations"
SETTLEMENTS = "veritas_currency_live_settlements"
ZERO = Decimal("0")


class LedgerError(ValueError):
    """Stable diagnostic code; errors never include private broker payloads."""


def exact(value, *, positive=False, nonnegative=False):
    if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, int, str)):
        raise LedgerError("EXACT_DECIMAL_REQUIRED")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise LedgerError("EXACT_DECIMAL_REQUIRED") from None
    if (not number.is_finite() or abs(number.adjusted()) > 30
            or len(number.as_tuple().digits) > 60
            or (positive and number <= 0) or (nonnegative and number < 0)):
        raise LedgerError("DECIMAL_OUT_OF_RANGE")
    return number


def lots(value, *, positive=False, nonnegative=False):
    number = exact(value)
    if (number != number.to_integral_value() or abs(number) > 2**31-1
            or (positive and number <= 0) or (nonnegative and number < 0)):
        raise LedgerError("INTEGER_LOTS_REQUIRED")
    return int(number)


def identifier(value):
    if (not isinstance(value, str) or not value or value.strip() != value
            or len(value) > 256 or any(ord(x) < 32 for x in value)):
        raise LedgerError("IDENTIFIER_REQUIRED")
    return value


def utc(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise LedgerError("AWARE_TIMESTAMP_REQUIRED") from None


def _fresh(value, now, seconds, code):
    age = (utc(now)-utc(value)).total_seconds()
    if age < -2 or age > seconds:
        raise LedgerError(code)


def _canonical(value):
    if isinstance(value, float):
        raise LedgerError("EXACT_DECIMAL_REQUIRED")
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(_canonical(value), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _entry_terms(fill, direction):
    metadata = fill.get("metadata") or {}
    try:
        if (metadata.get("action") != "OPEN" or metadata.get("direction") != direction
                or not isinstance(metadata.get("source_identity"), Mapping)
                or not metadata["source_identity"]):
            return None
        return {"stop_price":exact(metadata["stop_price"], positive=True),
                "target_price":exact(metadata["target_price"], positive=True),
                "horizon":identifier(metadata["horizon"]),
                "canonical_event_id":identifier(metadata["canonical_event_id"]),
                "source_identity":_canonical(metadata["source_identity"]),
                "entry_context":_canonical(metadata.get("entry_context") or {}),
                "direction":direction, "entry_client_order_id":fill["client_order_id"],
                "opened_at":utc(fill["executed_at"]), "policy_version":metadata.get("policy_version")}
    except (KeyError, TypeError, LedgerError):
        return None


@dataclass(frozen=True)
class InstrumentValuation:
    instrument_uid: str
    tick_size: Decimal
    tick_value_rub: Decimal
    lot_size: int = 1

    def __post_init__(self):
        identifier(self.instrument_uid)
        object.__setattr__(self, "tick_size", exact(self.tick_size, positive=True))
        object.__setattr__(self, "tick_value_rub", exact(self.tick_value_rub, positive=True))
        object.__setattr__(self, "lot_size", lots(self.lot_size, positive=True))

    @classmethod
    def from_contract(cls, spec):
        def get(key):
            return spec.get(key) if isinstance(spec, Mapping) else getattr(spec, key, None)
        if str(get("ticker") or "").upper() != "CNYRUBF":
            raise LedgerError("EXACT_CNY_INSTRUMENT_REQUIRED")
        return cls(get("instrument_uid"), get("tick_size"), get("tick_value_rub"), get("lot_size"))

    @property
    def rub_per_price_unit_per_lot(self):
        with localcontext() as ctx:
            ctx.prec = 64
            return self.tick_value_rub / self.tick_size * self.lot_size

    @property
    def revision(self):
        return fingerprint(self.identity())

    def identity(self):
        return {"instrument_uid": self.instrument_uid, "tick_size": self.tick_size,
                "tick_value_rub": self.tick_value_rub, "lot_size": self.lot_size}

    def execution_price(self, price):
        result = exact(price, positive=True)
        with localcontext() as ctx:
            ctx.prec = 64
            if result % self.tick_size:
                raise LedgerError("FILL_PRICE_NOT_ON_TICK")
        return result


def fills_fingerprint(fills):
    """Stable economic ownership; late fills invalidate settlement coverage."""
    keys = ("trade_id", "client_order_id", "broker_order_id", "side", "lots", "price", "executed_at")
    return fingerprint([{k:f[k] for k in keys} for f in sorted(fills, key=lambda f:str(f["trade_id"]))])


def project(fills, fee_observations, funding_adjustments, spec, *, settlement=None):
    """Replay immutable executions; late-arriving trades retain broker chronology.

    Individual fees may be unknown (None). A cumulative order commission is known
    only through its reported filled-lot count. The largest actual cumulative
    charge and the sum of known per-fill charges are alternative descriptions of
    the same fees, not amounts to add together.
    """
    if not isinstance(spec, InstrumentValuation):
        raise LedgerError("INSTRUMENT_VALUATION_REQUIRED")
    ordered = sorted(fills, key=lambda f: (utc(f["executed_at"]), str(f["trade_id"])))
    signed, average, realized = 0, None, ZERO
    held_terms, metadata_ok = None, True
    orders = {}
    with localcontext() as ctx:
        ctx.prec = 64
        for fill in ordered:
            count = lots(fill["lots"], positive=True)
            side = fill["side"]
            if side not in ("BUY", "SELL"):
                raise LedgerError("BROKER_SIDE_REQUIRED")
            price = spec.execution_price(fill["price"])
            delta = count if side == "BUY" else -count
            previous = signed
            signed += delta
            if not previous:
                average = price
                held_terms = _entry_terms(fill, "LONG" if signed > 0 else "SHORT")
                metadata_ok &= held_terms is not None
            elif previous * delta > 0:
                average = (abs(previous)*average + count*price) / abs(signed)
                # More partial fills of the original OPEN are the same event.
                # A later ADD may confirm another event, never replace SL/TP/TF.
                metadata = fill.get("metadata") or {}
                if held_terms and fill["client_order_id"] != held_terms["entry_client_order_id"]:
                    metadata_ok &= bool(metadata.get("action") == "ADD"
                                        and metadata.get("horizon") == held_terms["horizon"]
                                        and _canonical(metadata.get("source_identity")) == held_terms["source_identity"]
                                        and metadata.get("direction") == held_terms["direction"])
            else:
                closed = min(abs(previous), count)
                realized += closed*(price-average)*(1 if previous > 0 else -1)*spec.rub_per_price_unit_per_lot
                average = None if signed == 0 else price if previous*signed < 0 else average
                if not signed:
                    held_terms = None
                elif previous*signed < 0:
                    # A broker overfill is recorded truthfully, but cannot
                    # authorize a new opposite strategy position.
                    held_terms, metadata_ok = None, False
            order = orders.setdefault(identifier(fill["client_order_id"]),
                                      {"lots":0, "known_fees":ZERO, "all_known":True})
            order["lots"] += count
            fee = fill.get("fee_rub")
            order["all_known"] &= fee is not None
            if fee is not None:
                order["known_fees"] += exact(fee, nonnegative=True)
        for observed in fee_observations:
            order = orders.get(identifier(observed["client_order_id"]))
            if order is None:
                raise LedgerError("FEE_ORDER_HAS_NO_EXECUTIONS")
            amount = exact(observed["cumulative_fee_rub"], nonnegative=True)
            covered = lots(observed["filled_lots"], nonnegative=True)
            order["cumulative_fee"] = max(order.get("cumulative_fee", ZERO), amount)
            order["fee_covered_lots"] = max(order.get("fee_covered_lots", 0), covered)
        for observed in (settlement or {}).get("order_fees", []):
            order = orders.get(identifier(observed["client_order_id"]))
            if order is None:
                raise LedgerError("FEE_ORDER_HAS_NO_EXECUTIONS")
            # Only a complete, independently attributed report can supersede a
            # prior fee, including a downward correction. A partial observation
            # must not erase fees on other fills of the same order.
            if (observed.get("authoritative") is True
                    and lots(observed["filled_lots"], nonnegative=True) == order["lots"]):
                order["authoritative_fee"] = exact(observed["fee_rub"], nonnegative=True)
                order["all_known"] = True
                order["fee_covered_lots"] = order["lots"]
        fees = sum((o.get("authoritative_fee", max(o["known_fees"], o.get("cumulative_fee", ZERO)))
                    for o in orders.values()), ZERO)
        fees += exact((settlement or {}).get("other_fees_rub", ZERO), nonnegative=True)
        costs_known = all(o.get("fee_covered_lots", -1) <= o["lots"]
                          and (o["all_known"] or o.get("fee_covered_lots", -1) == o["lots"])
                          for o in orders.values())
        funding = sum((exact(a["cost_rub"]) for a in funding_adjustments), ZERO)
        if settlement is not None and settlement.get("funding_rub") is not None:
            if funding_adjustments:
                # A legacy manual record cannot be silently added to or replace
                # the same cash flow observed by a second authority.
                costs_known = False
            else:
                funding = exact(settlement["funding_rub"])
    return {"signed_lots":signed, "average_entry_price":average, "realized_pnl_rub":realized,
            "fees_rub":fees, "funding_rub":funding, "costs_reconciled":costs_known,
            "last_execution_at":utc(ordered[-1]["executed_at"]) if ordered else None,
            "held_terms":held_terms, "metadata_reconciled":metadata_ok}


def valuation(state, mark_price, spec, *, funding_reconciled=None):
    if funding_reconciled is not None and type(funding_reconciled) is not bool:
        raise LedgerError("EXPLICIT_FUNDING_RECONCILIATION_REQUIRED")
    price = exact(mark_price, positive=True)
    signed = lots(state["signed_lots"])
    average = exact(state["average_entry_price"], positive=True) if signed else None
    with localcontext() as ctx:
        ctx.prec = 64
        unrealized = (price-average)*signed*spec.rub_per_price_unit_per_lot if signed else ZERO
        nav = (exact(state["allocation_rub"], positive=True) + exact(state["realized_pnl_rub"])
               - exact(state["fees_rub"], nonnegative=True) - exact(state["funding_rub"]) + unrealized)
        old_hwm = exact(state["high_water_rub"], positive=True)
        # A profitable mark/close cannot establish a peak before both actual
        # commissions and settlement completeness have been verified.
        funding_known = (funding_reconciled is True or
                         (funding_reconciled is None and state.get("last_execution_at") is None
                          and exact(state["funding_rub"]) == ZERO))
        hwm = max(old_hwm, nav) if state["costs_reconciled"] and funding_known else old_hwm
        drawdown = max(ZERO, 1-nav/hwm)
    return {"currency_nav_rub":nav, "high_water_rub":hwm,
            "unrealized_pnl_rub":unrealized, "drawdown":drawdown}


def _require_transaction(c):
    if getattr(getattr(c, "info", None), "transaction_status", None) != 2:
        raise LedgerError("ACTIVE_POSTGRES_TRANSACTION_REQUIRED")


class CurrencyTradeLedger:
    def __init__(self, connect, *, enabled=False, clock=lambda: datetime.now(timezone.utc)):
        if type(enabled) is not bool or not callable(connect) or not callable(clock):
            raise LedgerError("EXPLICIT_LEDGER_CONFIGURATION_REQUIRED")
        self.connect, self.enabled, self.clock = connect, enabled, clock

    def __repr__(self):
        return f"CurrencyTradeLedger(enabled={self.enabled}, broker_io=False)"

    def _run(self, method, *args, **kwargs):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        with self.connect() as c, c.transaction():
            return method(c, *args, **kwargs)

    def ensure_schema(self):
        return self._run(self.ensure_schema_on)

    def ensure_schema_on(self, c):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        _require_transaction(c)
        c.execute(f"""
        CREATE TABLE IF NOT EXISTS {ACCOUNTS}(
          account_id TEXT PRIMARY KEY, instrument_uid TEXT NOT NULL,
          owner_user_id BIGINT NOT NULL CHECK(owner_user_id>0),
          allocation_rub NUMERIC NOT NULL CHECK(allocation_rub=10000),
          tick_size NUMERIC NOT NULL CHECK(tick_size>0),
          tick_value_rub NUMERIC NOT NULL CHECK(tick_value_rub>0),
          lot_size BIGINT NOT NULL CHECK(lot_size>0), spec_revision TEXT NOT NULL,
          signed_lots BIGINT NOT NULL DEFAULT 0, average_entry_price NUMERIC,
          realized_pnl_rub NUMERIC NOT NULL DEFAULT 0,
          fees_rub NUMERIC NOT NULL DEFAULT 0, funding_rub NUMERIC NOT NULL DEFAULT 0,
          high_water_rub NUMERIC NOT NULL DEFAULT 10000,
          costs_reconciled BOOLEAN NOT NULL DEFAULT TRUE,
          metadata_reconciled BOOLEAN NOT NULL DEFAULT TRUE, held_terms JSONB,
          ledger_revision BIGINT NOT NULL DEFAULT 0,
          entries_frozen BOOLEAN NOT NULL DEFAULT FALSE, freeze_reason TEXT,
          reconciled_revision BIGINT, broker_signed_lots BIGINT,
          broker_open_order_count BIGINT NOT NULL DEFAULT 0,
          broker_snapshot_id TEXT, broker_observed_at TIMESTAMPTZ,
          last_execution_at TIMESTAMPTZ, last_mark_price NUMERIC,
          last_mark_observed_at TIMESTAMPTZ, bound_at TIMESTAMPTZ NOT NULL,
          UNIQUE(account_id,instrument_uid),
          CHECK((signed_lots=0 AND average_entry_price IS NULL) OR
                (signed_lots<>0 AND average_entry_price>0))
        );
        CREATE TABLE IF NOT EXISTS {FILLS}(
          account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL, trade_id TEXT NOT NULL,
          client_order_id TEXT NOT NULL, broker_order_id TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
          lots BIGINT NOT NULL CHECK(lots>0), price NUMERIC NOT NULL CHECK(price>0),
          fee_rub NUMERIC CHECK(fee_rub>=0), executed_at TIMESTAMPTZ NOT NULL,
          metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb, metadata_hash TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          PRIMARY KEY(account_id,instrument_uid,trade_id),
          FOREIGN KEY(account_id,instrument_uid) REFERENCES {ACCOUNTS}(account_id,instrument_uid)
        );
        CREATE INDEX IF NOT EXISTS currency_live_fills_order ON {FILLS}(account_id,instrument_uid,client_order_id);
        CREATE TABLE IF NOT EXISTS {FEES}(
          account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL, observation_id TEXT NOT NULL,
          client_order_id TEXT NOT NULL, broker_order_id TEXT NOT NULL,
          cumulative_fee_rub NUMERIC NOT NULL CHECK(cumulative_fee_rub>=0),
          filled_lots BIGINT NOT NULL CHECK(filled_lots>=0), observed_at TIMESTAMPTZ NOT NULL,
          fingerprint TEXT NOT NULL,
          PRIMARY KEY(account_id,instrument_uid,observation_id),
          FOREIGN KEY(account_id,instrument_uid) REFERENCES {ACCOUNTS}(account_id,instrument_uid)
        );
        CREATE TABLE IF NOT EXISTS {FUNDING}(
          account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL, adjustment_id TEXT NOT NULL,
          cost_rub NUMERIC NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
          fingerprint TEXT NOT NULL,
          PRIMARY KEY(account_id,instrument_uid,adjustment_id),
          FOREIGN KEY(account_id,instrument_uid) REFERENCES {ACCOUNTS}(account_id,instrument_uid)
        );
        CREATE TABLE IF NOT EXISTS {RECONCILIATIONS}(
          account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL, snapshot_id TEXT NOT NULL,
          broker_signed_lots BIGINT NOT NULL, broker_open_order_count BIGINT NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL, ledger_revision BIGINT NOT NULL,
          expected_signed_lots BIGINT NOT NULL, matched BOOLEAN NOT NULL, fingerprint TEXT NOT NULL,
          PRIMARY KEY(account_id,instrument_uid,snapshot_id),
          FOREIGN KEY(account_id,instrument_uid) REFERENCES {ACCOUNTS}(account_id,instrument_uid)
        );
        ALTER TABLE {ACCOUNTS} ADD COLUMN IF NOT EXISTS settlement JSONB;
        ALTER TABLE {ACCOUNTS} ADD COLUMN IF NOT EXISTS fill_fingerprint TEXT;
        CREATE TABLE IF NOT EXISTS {SETTLEMENTS}(
          account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL, observation_id TEXT NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL, requested_through TIMESTAMPTZ NOT NULL,
          statement_id TEXT, statement_revision BIGINT,
          evidence JSONB NOT NULL, assessment JSONB NOT NULL, fingerprint TEXT NOT NULL,
          PRIMARY KEY(account_id,instrument_uid,observation_id),
          FOREIGN KEY(account_id,instrument_uid) REFERENCES {ACCOUNTS}(account_id,instrument_uid)
        );
        """)
        return {"status":"READY", "version":VERSION}

    def _load(self, c, account_id, instrument_uid):
        _require_transaction(c)
        row = c.execute(f"SELECT * FROM {ACCOUNTS} WHERE account_id=%s FOR UPDATE",
                        (identifier(account_id),)).fetchone()
        if not row:
            raise LedgerError("CURRENCY_ACCOUNT_NOT_BOUND")
        row = dict(row)
        if row["instrument_uid"] != identifier(instrument_uid):
            raise LedgerError("BOUND_INSTRUMENT_MISMATCH")
        return row

    @staticmethod
    def _spec(row, supplied=None):
        spec = InstrumentValuation(row["instrument_uid"], row["tick_size"], row["tick_value_rub"], row["lot_size"])
        if supplied is not None and (not isinstance(supplied, InstrumentValuation) or supplied.revision != spec.revision):
            raise LedgerError("VALUATION_SPEC_MISMATCH")
        return spec

    def account_bind(self, account_id, instrument_uid, **kwargs):
        return self._run(self.account_bind_on, account_id, instrument_uid, **kwargs)

    def bind(self, account_id, valuation, **kwargs):
        return self.account_bind(account_id, valuation.instrument_uid, spec=valuation, **kwargs)

    def account_bind_on(self, c, account_id, instrument_uid, *, owner_user_id, spec,
                        broker_signed_lots, broker_snapshot_id, observed_at, verified=False,
                        broker_open_order_count=0, allocation_rub=ALLOCATION_RUB):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        _require_transaction(c)
        account_id, instrument_uid = identifier(account_id), identifier(instrument_uid)
        owner_number = exact(owner_user_id, positive=True)
        if owner_number != owner_number.to_integral_value() or owner_number > 2**63-1:
            raise LedgerError("OWNER_USER_ID_REQUIRED")
        owner = int(owner_number)
        if verified is not True or lots(broker_signed_lots) or lots(broker_open_order_count, nonnegative=True):
            raise LedgerError("VERIFIED_FLAT_BROKER_ACCOUNT_REQUIRED")
        _fresh(observed_at, self.clock(), 30, "BROKER_SNAPSHOT_STALE")
        if exact(allocation_rub, positive=True) != ALLOCATION_RUB:
            raise LedgerError("CURRENCY_ALLOCATION_MUST_BE_10000_RUB")
        if not isinstance(spec, InstrumentValuation) or spec.instrument_uid != instrument_uid:
            raise LedgerError("EXACT_INSTRUMENT_VALUATION_REQUIRED")
        c.execute(f"""INSERT INTO {ACCOUNTS}(account_id,instrument_uid,owner_user_id,allocation_rub,
          tick_size,tick_value_rub,lot_size,spec_revision,reconciled_revision,broker_signed_lots,
          broker_snapshot_id,broker_observed_at,bound_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s,%s)
          ON CONFLICT(account_id) DO NOTHING""",
          (account_id,instrument_uid,owner,ALLOCATION_RUB,spec.tick_size,spec.tick_value_rub,
           spec.lot_size,spec.revision,identifier(broker_snapshot_id),utc(observed_at),utc(self.clock())))
        row = self._load(c, account_id, instrument_uid)
        self._spec(row, spec)
        if row["owner_user_id"] != owner:
            raise LedgerError("BOUND_OWNER_MISMATCH")
        # Repeated bind never resets capital, projection, HWM or a safety freeze.
        return self._view(row, status="BOUND")

    def _insert_event(self, c, table, id_name, row, values):
        values = {"account_id":row["account_id"], "instrument_uid":row["instrument_uid"], **values}
        digest = fingerprint(values)
        previous = c.execute(f"SELECT fingerprint FROM {table} WHERE account_id=%s AND instrument_uid=%s AND {id_name}=%s",
                             (row["account_id"],row["instrument_uid"],values[id_name])).fetchone()
        if previous:
            if previous["fingerprint"] != digest:
                raise LedgerError("BROKER_EVENT_ID_CONFLICT")
            return False
        values["fingerprint"] = digest
        placeholders = ["%s::jsonb" if key == "metadata" else "%s" for key in values]
        parameters = tuple(json.dumps(_canonical(value), separators=(",", ":"), allow_nan=False)
                           if key == "metadata" else value for key, value in values.items())
        c.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join(placeholders)})", parameters)
        return True

    def _order_identity(self, c, row, client_order_id, broker_order_id, *, required=False, metadata_hash=None):
        existing = c.execute(f"SELECT DISTINCT client_order_id,broker_order_id,metadata_hash FROM {FILLS} "
                             "WHERE account_id=%s AND instrument_uid=%s AND (client_order_id=%s OR broker_order_id=%s)",
                             (row["account_id"],row["instrument_uid"],client_order_id,broker_order_id)).fetchall()
        if required and not existing:
            raise LedgerError("FEE_ORDER_HAS_NO_EXECUTIONS")
        if any(x["client_order_id"] != client_order_id or x["broker_order_id"] != broker_order_id for x in existing):
            raise LedgerError("BROKER_ORDER_IDENTITY_CONFLICT")
        if metadata_hash is not None and any(x["metadata_hash"] != metadata_hash for x in existing):
            raise LedgerError("ORDER_METADATA_CHANGED")

    def _rebuild(self, c, row):
        params = (row["account_id"], row["instrument_uid"])
        all_rows = lambda table: c.execute(f"SELECT * FROM {table} WHERE account_id=%s AND instrument_uid=%s", params).fetchall()
        fills = all_rows(FILLS)
        projection = project(fills, all_rows(FEES), all_rows(FUNDING), self._spec(row),
                             settlement=row.get("settlement"))
        projection["fill_fingerprint"] = fills_fingerprint(fills)
        # Polling may repeat the same cumulative fee with a new observation ID.
        # Preserve the financial revision and reconciliation when facts did not
        # change, so an unchanged broker poll cannot invalidate every approval.
        if all(_canonical(row.get(key)) == _canonical(value) for key,value in projection.items()):
            return row
        row.update(projection)
        row["ledger_revision"] += 1
        row["reconciled_revision"] = None
        # A closed allocation establishes a cash peak only after the durable
        # numeric settlement covers these exact fills and remains fresh.
        if not row["signed_lots"] and row["costs_reconciled"] and self._funding_ready(row):
            with localcontext() as ctx:
                ctx.prec = 64
                cash_nav = row["allocation_rub"] + row["realized_pnl_rub"] - row["fees_rub"] - row["funding_rub"]
                row["high_water_rub"] = max(row["high_water_rub"], cash_nav)
        fields = ("signed_lots", "average_entry_price", "realized_pnl_rub", "fees_rub", "funding_rub",
                  "costs_reconciled", "metadata_reconciled", "last_execution_at", "ledger_revision", "reconciled_revision", "high_water_rub", "fill_fingerprint")
        c.execute(f"UPDATE {ACCOUNTS} SET {','.join(k+'=%s' for k in fields)} WHERE account_id=%s",
                  tuple(row[k] for k in fields)+(row["account_id"],))
        c.execute(f"UPDATE {ACCOUNTS} SET held_terms=%s::jsonb WHERE account_id=%s",
                  (json.dumps(_canonical(row["held_terms"]), separators=(",", ":")),row["account_id"]))
        return row

    def record_fill(self, account_id, instrument_uid, **kwargs):
        return self._run(self.record_fill_on, account_id, instrument_uid, **kwargs)

    def record_fill_on(self, c, account_id, instrument_uid, *, trade_id, client_order_id, broker_order_id,
                       side, lots_count, price, executed_at, spec, fee_rub=None, currency="RUB", price_type="POINT",
                       metadata=None):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        row = self._load(c, account_id, instrument_uid)
        self._spec(row, spec)
        if currency != "RUB" or price_type != "POINT" or side not in ("BUY", "SELL"):
            raise LedgerError("RUB_POINT_EXECUTION_REQUIRED")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise LedgerError("EXECUTION_METADATA_OBJECT_REQUIRED")
        metadata = _canonical(dict(metadata or {}))
        for key, expected in (("account_id",account_id),("instrument_uid",instrument_uid),("side",side)):
            if key in metadata and metadata[key] != expected:
                raise LedgerError("EXECUTION_METADATA_IDENTITY_MISMATCH")
        values = {"trade_id":identifier(trade_id), "client_order_id":identifier(client_order_id),
                  "broker_order_id":identifier(broker_order_id), "side":side,
                  "lots":lots(lots_count, positive=True), "price":spec.execution_price(price),
                  "fee_rub":None if fee_rub is None else exact(fee_rub, nonnegative=True),
                  "executed_at":utc(executed_at), "metadata":metadata, "metadata_hash":fingerprint(metadata)}
        if (values["executed_at"]-utc(self.clock())).total_seconds() > 2:
            raise LedgerError("EXECUTION_FROM_FUTURE")
        if values["executed_at"] < row["bound_at"]:
            raise LedgerError("EXECUTION_PREDATES_ALLOCATION_BINDING")
        self._order_identity(c, row, values["client_order_id"], values["broker_order_id"], metadata_hash=values["metadata_hash"])
        added = self._insert_event(c, FILLS, "trade_id", row, values)
        if added:
            self._rebuild(c, row)
        return self._view(row, status="RECORDED" if added else "DUPLICATE")

    def record_order_fee(self, account_id, instrument_uid, **kwargs):
        return self._run(self.record_order_fee_on, account_id, instrument_uid, **kwargs)

    def record_order_commission(self, account_id, instrument_uid, **kwargs):
        return self.record_order_fee(account_id, instrument_uid, **kwargs)

    def record_order_fee_on(self, c, account_id, instrument_uid, *, observation_id, client_order_id,
                            broker_order_id, cumulative_fee_rub, filled_lots, observed_at, currency="RUB"):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        row = self._load(c, account_id, instrument_uid)
        if currency != "RUB":
            raise LedgerError("RUB_COMMISSION_REQUIRED")
        client, broker = identifier(client_order_id), identifier(broker_order_id)
        self._order_identity(c, row, client, broker, required=True)
        old_fees = row["fees_rub"]
        values = {"observation_id":identifier(observation_id), "client_order_id":client,
                  "broker_order_id":broker, "cumulative_fee_rub":exact(cumulative_fee_rub, nonnegative=True),
                  "filled_lots":lots(filled_lots, nonnegative=True), "observed_at":utc(observed_at)}
        if (values["observed_at"]-utc(self.clock())).total_seconds() > 2:
            raise LedgerError("COMMISSION_FROM_FUTURE")
        added = self._insert_event(c, FEES, "observation_id", row, values)
        if added:
            self._rebuild(c, row)
        return dict(self._view(row, status="RECORDED" if added else "DUPLICATE"),
                    fee_delta_rub=row["fees_rub"]-old_fees)

    def record_funding(self, account_id, instrument_uid, **kwargs):
        return self._run(self.record_funding_on, account_id, instrument_uid, **kwargs)

    def record_funding_on(self, c, account_id, instrument_uid, *, adjustment_id, cost_rub,
                          occurred_at, kind="FUNDING", currency="RUB"):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        row = self._load(c, account_id, instrument_uid)
        if kind != "FUNDING" or currency != "RUB":
            raise LedgerError("ONLY_ACTUAL_RUB_FUNDING_ADJUSTMENTS_SUPPORTED")
        values = {"adjustment_id":identifier(adjustment_id), "cost_rub":exact(cost_rub), "occurred_at":utc(occurred_at)}
        if values["occurred_at"] < row["bound_at"] or (values["occurred_at"]-utc(self.clock())).total_seconds() > 2:
            raise LedgerError("FUNDING_TIME_OUTSIDE_ALLOCATION")
        added = self._insert_event(c, FUNDING, "adjustment_id", row, values)
        if added:
            self._rebuild(c, row)
        return self._view(row, status="RECORDED" if added else "DUPLICATE")

    def reconcile_broker_positions(self, account_id, instrument_uid, **kwargs):
        return self._run(self.reconcile_broker_positions_on, account_id, instrument_uid, **kwargs)

    def reconcile_broker_positions_on(self, c, account_id, instrument_uid, *, broker_signed_lots,
                                      broker_snapshot_id, observed_at, verified=False, broker_open_order_count=0):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        row = self._load(c, account_id, instrument_uid)
        if verified is not True:
            raise LedgerError("VERIFIED_BROKER_SNAPSHOT_REQUIRED")
        _fresh(observed_at, self.clock(), 30, "BROKER_SNAPSHOT_STALE")
        observed = utc(observed_at)
        if row["last_execution_at"] and observed < row["last_execution_at"]:
            raise LedgerError("BROKER_SNAPSHOT_PREDATES_EXECUTION")
        count, working = lots(broker_signed_lots), lots(broker_open_order_count, nonnegative=True)
        matched = count == row["signed_lots"]
        values = {"snapshot_id":identifier(broker_snapshot_id), "broker_signed_lots":count,
                  "broker_open_order_count":working, "observed_at":observed,
                  "ledger_revision":row["ledger_revision"], "expected_signed_lots":row["signed_lots"], "matched":matched}
        self._insert_event(c, RECONCILIATIONS, "snapshot_id", row, values)
        frozen = row["entries_frozen"] or not matched
        reason = row["freeze_reason"] or ("BROKER_POSITION_MISMATCH" if not matched else None)
        c.execute(f"UPDATE {ACCOUNTS} SET entries_frozen=%s,freeze_reason=%s,reconciled_revision=%s,"
                  "broker_signed_lots=%s,broker_open_order_count=%s,broker_snapshot_id=%s,broker_observed_at=%s WHERE account_id=%s",
                  (frozen,reason,row["ledger_revision"] if matched else None,count,working,
                   values["snapshot_id"],observed,row["account_id"]))
        row.update(entries_frozen=frozen,freeze_reason=reason,
                   reconciled_revision=row["ledger_revision"] if matched else None,
                   broker_signed_lots=count,broker_open_order_count=working,
                   broker_snapshot_id=values["snapshot_id"],broker_observed_at=observed)
        return self._view(row, status="FROZEN" if frozen else "RECONCILED" if not working else "WORKING_ORDERS")

    def snapshot(self, account_id, instrument_uid, **kwargs):
        return self._run(self.snapshot_on, account_id, instrument_uid, **kwargs)

    def snapshot_on(self, c, account_id, instrument_uid, *, mark_price, mark_observed_at, spec,
                    allow_high_water_update=True, funding_reconciled=None):
        if not self.enabled:
            return {"status":"DISABLED", "entries_allowed":False}
        row = self._load(c, account_id, instrument_uid)
        self._spec(row, spec)
        _fresh(mark_observed_at, self.clock(), 15, "MARK_SNAPSHOT_STALE")
        observed = utc(mark_observed_at)
        if row["last_execution_at"] and observed < row["last_execution_at"]:
            raise LedgerError("MARK_PREDATES_EXECUTION")
        if row["last_mark_observed_at"] and observed < row["last_mark_observed_at"]:
            raise LedgerError("MARK_OUT_OF_ORDER")
        if funding_reconciled is not None and type(funding_reconciled) is not bool:
            raise LedgerError("EXPLICIT_FUNDING_RECONCILIATION_REQUIRED")
        # Compatibility callers may withhold readiness, but a caller-supplied
        # True never substitutes for the account's durable settlement evidence.
        funding_ready = (self._funding_ready(row) and allow_high_water_update is True
                         and funding_reconciled is not False)
        result = valuation(row, mark_price, spec, funding_reconciled=funding_ready)
        c.execute(f"UPDATE {ACCOUNTS} SET high_water_rub=%s,last_mark_price=%s,last_mark_observed_at=%s WHERE account_id=%s",
                  (result["high_water_rub"],exact(mark_price, positive=True),observed,row["account_id"]))
        row.update(high_water_rub=result["high_water_rub"],last_mark_price=exact(mark_price),last_mark_observed_at=observed)
        return dict(self._view(row, status="FROZEN" if row["entries_frozen"] else "SNAPSHOT"), **result)

    def _funding_ready(self, row):
        if row.get("last_execution_at") is None:
            return exact(row.get("funding_rub", ZERO)) == ZERO
        if row.get("costs_reconciled") is not True:
            return False
        evidence = row.get("settlement") or {}
        if (evidence.get("funding_reconciled") is not True
                or evidence.get("costs_reconciled") is not True
                or evidence.get("fill_fingerprint") != row.get("fill_fingerprint")):
            return False
        try:
            for key in ("observed_as_of", "requested_through"):
                _fresh(evidence[key], self.clock(), 30, "SETTLEMENT_OBSERVATION_STALE")
            through = utc(evidence["settlement_through"])
            return (through >= utc(evidence["requested_through"])
                    or (evidence.get("flat_after_settlement") is True and row["signed_lots"] == 0
                        and utc(row["last_execution_at"]) <= through))
        except (KeyError, LedgerError):
            return False

    def _view(self, row, *, status):
        matched = row["reconciled_revision"] == row["ledger_revision"]
        fresh = False
        if row.get("broker_observed_at"):
            age = (utc(self.clock())-utc(row["broker_observed_at"])).total_seconds()
            fresh = -2 <= age <= 30
        reconciled = bool(matched and fresh and row["metadata_reconciled"]
                          and not row["entries_frozen"] and not row["broker_open_order_count"])
        funding_ready = self._funding_ready(row)
        settlement = row.get("settlement") or {}
        return {"status":status, "version":VERSION, "account_id":row["account_id"],
                "instrument_uid":row["instrument_uid"], "allocation_rub":row["allocation_rub"],
                "signed_lots":row["signed_lots"], "managed_signed_lots":row["signed_lots"],
                "average_entry_price":row["average_entry_price"], "realized_pnl_rub":row["realized_pnl_rub"],
                "fees_rub":row["fees_rub"], "funding_rub":row["funding_rub"],
                "high_water_rub":row["high_water_rub"], "ledger_revision":row["ledger_revision"],
                "spec_revision":row["spec_revision"], "costs_reconciled":row["costs_reconciled"],
                "metadata_reconciled":row["metadata_reconciled"], "held_terms":row["held_terms"],
                "reconciled":reconciled, "entries_allowed":reconciled and row["costs_reconciled"] and funding_ready,
                "funding_reconciled":funding_ready,
                "settlement_observed_as_of":settlement.get("observed_as_of"),
                "settlement_through":settlement.get("settlement_through"),
                "settlement_reasons":settlement.get("reasons", []),
                "entries_frozen":row["entries_frozen"], "freeze_reason":row["freeze_reason"],
                "broker_signed_lots":row["broker_signed_lots"], "broker_snapshot_id":row["broker_snapshot_id"],
                "broker_observed_at":row["broker_observed_at"], "mark_observed_at":row["last_mark_observed_at"]}
