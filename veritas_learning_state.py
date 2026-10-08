"""Small durable learning snapshots and fenced jobs; never an execution authority.

Initialize once with ensure_schema. The *_in_transaction variants let a caller
atomically consume an evidence ID and publish its resulting aggregate. They do
not commit, open connections, or change the caller's timeout settings.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import uuid

VERSION = "LEARNING_STATE_V1"
MAX_SNAPSHOTS = 32
MAX_JOBS = 32
MAX_SNAPSHOT_BYTES = 262144
MAX_CURSOR_BYTES = 16384
MAX_RESULT_BYTES = 65536
_LOCK = 198617301
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_BAD = ("ERROR", "UNAVAILABLE", "DEFERRED", "RUNNING", "PENDING", "BACKGROUND_PENDING")


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("invalid learning state name/version")
    return value


def _json(value, maximum):
    # Bound traversal and individual strings before encoding: no giant object
    # copy is needed just to discover that a result exceeds its storage budget.
    count = 0
    def visit(x, depth=0):
        nonlocal count
        count += 1
        if count > 30000 or depth > 16:
            raise ValueError("learning state is too complex")
        if x is None or type(x) in (bool, int):
            return
        if type(x) is float:
            if not math.isfinite(x):
                raise ValueError("nonfinite learning state")
            return
        if isinstance(x, str):
            if len(x) > maximum:
                raise ValueError("learning state string too large")
            return
        if isinstance(x, dict):
            if len(x) > 10000:
                raise ValueError("too many learning state fields")
            for key, item in x.items():
                if not isinstance(key, str):
                    raise ValueError("learning state keys must be strings")
                visit(key, depth+1); visit(item, depth+1)
            return
        if isinstance(x, (list, tuple)):
            if len(x) > 10000:
                raise ValueError("too many learning state items")
            for item in x:
                visit(item, depth+1)
            return
        raise ValueError("learning state must be JSON")
    visit(value)
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("learning state payload exceeds byte limit")
    return encoded


def _time(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("learning watermark time must have timezone")
    return value.astimezone(timezone.utc)


def _seconds(value, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("invalid learning job interval")
    return float(value)


def _good(payload):
    if not isinstance(payload, dict):
        raise ValueError("a completed learning snapshot must be an object")
    status = str(payload.get("status") or "").upper()
    if any(status == prefix or status.startswith(prefix+"_") for prefix in _BAD):
        raise ValueError("incomplete/failed result cannot replace last good snapshot")


@contextmanager
def _transaction(pg_connect):
    with pg_connect() as c, c.transaction():
        c.execute("SET LOCAL statement_timeout = '2000ms'; SET LOCAL lock_timeout = '250ms'")
        yield c


def ensure_schema(pg_connect):
    with _transaction(pg_connect) as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        c.execute("""CREATE TABLE IF NOT EXISTS veritas_learning_snapshots (
            name text PRIMARY KEY CHECK(length(name)<=80), version text NOT NULL CHECK(length(version)<=80),
            payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=524288),
            payload_hash text NOT NULL, watermark text, observed_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
        c.execute("""CREATE TABLE IF NOT EXISTS veritas_learning_jobs (
            name text PRIMARY KEY CHECK(length(name)<=80), version text NOT NULL CHECK(length(version)<=80),
            status text NOT NULL, cursor jsonb, result jsonb, last_good jsonb,
            owner text, fence bigint NOT NULL DEFAULT 0, lease_until timestamptz,
            next_run_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK(cursor IS NULL OR octet_length(cursor::text)<=32768),
            CHECK(result IS NULL OR octet_length(result::text)<=131072),
            CHECK(last_good IS NULL OR octet_length(last_good::text)<=131072))""")
    return True


def load_snapshot_in_transaction(c, name, version, *, for_update=False):
    name, version = _name(name), _name(version)
    if for_update:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
    row = c.execute("""SELECT name,version,payload,watermark,observed_at,updated_at
        FROM veritas_learning_snapshots WHERE name=%s AND version=%s"""+
        (" FOR UPDATE" if for_update else ""), (name, version)).fetchone()
    return dict(row) if row else None


def load_snapshot(pg_connect, name, version, max_age_seconds=None):
    with _transaction(pg_connect) as c:
        row = load_snapshot_in_transaction(c, name, version)
    if row and max_age_seconds is not None:
        age = (datetime.now(timezone.utc)-_time(row["observed_at"])).total_seconds()
        if age > _seconds(max_age_seconds, 365*86400):
            return None
    return row


def _lease_valid(c, lease):
    if not isinstance(lease, dict):
        raise ValueError("missing learning job lease")
    # Recheck expiry after the materialized row-lock boundary. An unchanged
    # tuple can qualify before waiting and arrive after its lease has expired.
    return c.execute("""WITH held AS MATERIALIZED (
        SELECT lease_until FROM veritas_learning_jobs
        WHERE name=%s AND version=%s AND owner=%s AND fence=%s
          AND lease_until>clock_timestamp() FOR UPDATE
        ) SELECT 1 AS ok FROM held WHERE lease_until>clock_timestamp()""",
        (_name(lease["name"]), _name(lease["version"]), lease["owner"], int(lease["fence"]))).fetchone() is not None


def publish_snapshot_in_transaction(c, name, version, payload, *, watermark=None, observed_at=None, lease=None,
                                    locked_snapshot=None, before_write=None, max_payload_bytes=MAX_SNAPSHOT_BYTES):
    """Publish without committing or changing the caller's timeout settings.

    Internal callers may pass the row returned by load_snapshot_in_transaction
    with for_update=True on this same connection and still-open transaction.
    Its global and row locks must remain held. That existing slot can be updated
    without repeating the lock/capacity reads; this path never inserts a slot.
    before_write runs after preparation, immediately before the write statement.
    max_payload_bytes may only tighten the existing serialized UTF-8 byte limit.
    """
    name, version = _name(name), _name(version)
    if type(max_payload_bytes) is not int or not 1 <= max_payload_bytes <= MAX_SNAPSHOT_BYTES:
        raise ValueError("invalid learning snapshot byte limit")
    _good(payload)
    encoded = _json(payload, max_payload_bytes)
    stamp = _time(observed_at)
    if watermark is not None and (not isinstance(watermark, str) or len(watermark.encode("utf-8"))>512):
        raise ValueError("invalid learning snapshot watermark")
    if locked_snapshot is not None and (not isinstance(locked_snapshot, dict)
            or locked_snapshot.get("name") != name or locked_snapshot.get("version") != version):
        raise ValueError("locked learning snapshot name/version mismatch")
    if before_write is not None and not callable(before_write):
        raise ValueError("learning snapshot before_write must be callable")
    if locked_snapshot is None:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
    if lease is not None and not _lease_valid(c, lease):
        return False
    if locked_snapshot is None:
        old = c.execute("SELECT name FROM veritas_learning_snapshots WHERE name=%s", (name,)).fetchone()
        if not old and c.execute("SELECT count(*) AS n FROM veritas_learning_snapshots").fetchone()["n"] >= MAX_SNAPSHOTS:
            raise ValueError("learning snapshot capacity reached; no slots evicted")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if before_write is not None:
        before_write()
    if locked_snapshot is not None:
        row = c.execute("""UPDATE veritas_learning_snapshots AS stored SET
            version=incoming.version,payload=incoming.payload,payload_hash=incoming.payload_hash,
            watermark=incoming.watermark,observed_at=incoming.observed_at,
            updated_at=CASE WHEN stored.payload_hash=incoming.payload_hash
                 AND stored.version=incoming.version
                 AND stored.watermark IS NOT DISTINCT FROM incoming.watermark
                 AND stored.observed_at=incoming.observed_at
               THEN stored.updated_at ELSE clock_timestamp() END
            FROM (VALUES (%s,%s,%s::jsonb,%s,%s,%s::timestamptz))
                AS incoming(name,version,payload,payload_hash,watermark,observed_at)
            WHERE stored.name=incoming.name AND stored.version=incoming.version
              AND incoming.observed_at>=stored.observed_at RETURNING stored.name""",
            (name,version,encoded,digest,watermark,stamp)).fetchone()
        return bool(row)
    row = c.execute("""INSERT INTO veritas_learning_snapshots
        (name,version,payload,payload_hash,watermark,observed_at) VALUES (%s,%s,%s::jsonb,%s,%s,%s)
        ON CONFLICT(name) DO UPDATE SET version=EXCLUDED.version,payload=EXCLUDED.payload,
            payload_hash=EXCLUDED.payload_hash,watermark=EXCLUDED.watermark,observed_at=EXCLUDED.observed_at,
            updated_at=CASE WHEN veritas_learning_snapshots.payload_hash=EXCLUDED.payload_hash
                 AND veritas_learning_snapshots.version=EXCLUDED.version
                 AND veritas_learning_snapshots.watermark IS NOT DISTINCT FROM EXCLUDED.watermark
                 AND veritas_learning_snapshots.observed_at=EXCLUDED.observed_at
               THEN veritas_learning_snapshots.updated_at ELSE clock_timestamp() END
        WHERE EXCLUDED.observed_at>=veritas_learning_snapshots.observed_at RETURNING name""",
        (name,version,encoded,digest,watermark,stamp)).fetchone()
    return bool(row)


def publish_snapshot(pg_connect, name, version, payload, *, watermark=None, observed_at=None, lease=None):
    # Validate before opening a database connection.
    _good(payload); _json(payload, MAX_SNAPSHOT_BYTES)
    with _transaction(pg_connect) as c:
        return publish_snapshot_in_transaction(c,name,version,payload,watermark=watermark,observed_at=observed_at,lease=lease)


def job_state(pg_connect, name, version):
    with _transaction(pg_connect) as c:
        row = c.execute("SELECT * FROM veritas_learning_jobs WHERE name=%s AND version=%s",
                        (_name(name), _name(version))).fetchone()
    return dict(row) if row else None


def claim_job(pg_connect, name, version, *, lease_seconds=60, owner=None):
    name, version = _name(name), _name(version)
    seconds = _seconds(lease_seconds, 3600)
    if seconds < 1:
        raise ValueError("lease must last at least one second")
    owner = _name(owner or uuid.uuid4().hex)
    with _transaction(pg_connect) as c:
        # Existing jobs only contend with their own fenced row. Keep the row
        # locked through UPSERT so concurrent deletion cannot create a new slot
        # outside the serialized capacity check.
        old = c.execute("SELECT name FROM veritas_learning_jobs WHERE name=%s FOR UPDATE", (name,)).fetchone()
        if not old:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
            # Another creator may have installed this name while we waited.
            old = c.execute("SELECT name FROM veritas_learning_jobs WHERE name=%s", (name,)).fetchone()
            if not old and c.execute("SELECT count(*) AS n FROM veritas_learning_jobs").fetchone()["n"] >= MAX_JOBS:
                raise ValueError("learning job capacity reached; no jobs evicted")
        row = c.execute("""INSERT INTO veritas_learning_jobs
            (name,version,status,owner,fence,lease_until) VALUES(%s,%s,'RUNNING',%s,1,clock_timestamp()+%s*interval '1 second')
            ON CONFLICT(name) DO UPDATE SET version=EXCLUDED.version,status='RUNNING',owner=EXCLUDED.owner,
              fence=veritas_learning_jobs.fence+1,lease_until=EXCLUDED.lease_until,updated_at=clock_timestamp(),
              cursor=CASE WHEN veritas_learning_jobs.version=EXCLUDED.version THEN veritas_learning_jobs.cursor END,
              result=CASE WHEN veritas_learning_jobs.version=EXCLUDED.version THEN veritas_learning_jobs.result END,
              last_good=CASE WHEN veritas_learning_jobs.version=EXCLUDED.version THEN veritas_learning_jobs.last_good END
            WHERE (veritas_learning_jobs.lease_until IS NULL OR veritas_learning_jobs.lease_until<=clock_timestamp())
              AND (veritas_learning_jobs.version<>EXCLUDED.version OR veritas_learning_jobs.next_run_at<=clock_timestamp())
            RETURNING *""", (name,version,owner,seconds)).fetchone()
    return dict(row) if row else None


def checkpoint_job(pg_connect, lease, *, status, cursor=None, result=None, retry_after_seconds=0, release=True):
    status = _name(status)
    delay = _seconds(retry_after_seconds)
    cur = None if cursor is None else _json(cursor, MAX_CURSOR_BYTES)
    res = _json(result, MAX_RESULT_BYTES)
    good = status in ("OK", "COMPLETE", "BUILDING", "INSUFFICIENT_DATA")
    if good and result is not None:
        _good(result)
    with _transaction(pg_connect) as c:
        # This updates an existing row and consumes no global capacity slot.
        # _lease_valid holds its row lock until this transaction finishes.
        if not _lease_valid(c, lease):
            return False
        row = c.execute("""UPDATE veritas_learning_jobs SET status=%s,cursor=COALESCE(%s::jsonb,cursor),result=%s::jsonb,
            last_good=CASE WHEN %s THEN %s::jsonb ELSE last_good END,
            next_run_at=clock_timestamp()+%s*interval '1 second',updated_at=clock_timestamp(),
            owner=CASE WHEN %s THEN NULL ELSE owner END,
            lease_until=CASE WHEN %s THEN NULL ELSE lease_until END
            WHERE name=%s AND version=%s AND owner=%s AND fence=%s
              AND lease_until>clock_timestamp() RETURNING name""",
            (status,cur,res,good and result is not None,res,delay,release,release,
             lease["name"],lease["version"],lease["owner"],lease["fence"])).fetchone()
    return bool(row)
