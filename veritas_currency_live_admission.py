"""Whole-account authority for Currency OPEN/ADD orders; never a broker writer.

The factory always installs a PostgreSQL evidence loader.  Its absence/failure,
an unaudited model, or incomplete account history is a blocker, not a paper-mode
permission. ``publish_audited_artifact`` archives independently reviewed bytes.
Production additionally requires the independent issuer's signed v2 envelope,
verified at protected import and again at runtime for these exact order terms.
See docs/currency-live-authority.md for the evidence trust boundary and schema.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import base64
import hashlib
import hmac
import json
import math
import re
import threading
import uuid
from typing import Mapping
from zoneinfo import ZoneInfo

import veritas_canonical_constitution as CTC
import veritas_currency_trade_plan as P
import veritas_entry_version as EV
import veritas_execution as VX
import veritas_instruments as VI
import veritas_live as LIVE
import veritas_promotion as PROM
import veritas_risk as RISK
from veritas_tbank_trading import quotation_to_decimal

VERSION = "CURRENCY_WHOLE_ACCOUNT_LIVE_AUTHORITY_V1"
EVIDENCE_SCHEMA = "VERITAS_AUDITED_LIVE_EVIDENCE_V1"
EVIDENCE_TABLE = "public.veritas_currency_live_evidence_v1"
OBSERVATION_TABLE = "public.veritas_currency_live_account_observations_v1"
ARTIFACT_TABLE = "public.veritas_currency_live_artifact_blobs_v1"
TABLE = "currency_live_admission_evidence"
SIGNED_EVIDENCE_SCHEMA = "currency-live-admission-v2"
MAX_BYTES = 60 * 1024
MAX_SIGNED_MODEL_AGE = timedelta(seconds=900)
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ACCOUNT_ROWS = 128
MAX_MODEL_AGE = timedelta(days=7)
MAX_ACCOUNT_AGE = timedelta(seconds=30)
MODEL_DOMAINS = {"oos": "INDEPENDENT_OOS", "vault": "SEALED_VAULT",
    "high_cost": "COST_STRESS", "calibration": "HELD_OUT_CALIBRATION",
    "shadow": "VERSIONED_SHADOW", "ci": "CODE_CI", "data_parity": "DATA_PARITY"}
ACCOUNT_DOMAINS = {"nav": "BROKER_UNIT_NAV_LEDGER", "operations": "TBANK_BROKER_OPERATIONS"}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_D = Decimal
_UTC = timezone.utc


class AdmissionBlocked(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


# The independently authenticated HTTP intake keeps its published exception
# interface; the production authority uses the same fail-closed codes.
LiveAdmissionError = AdmissionBlocked


def _require(condition, code):
    if not condition:
        raise AdmissionBlocked(code)


def _decimal(value, code, *, positive=False):
    try:
        _require(not isinstance(value, bool) and value is not None, code)
        result = _D(str(value))
        _require(result.is_finite() and math.isfinite(float(result)) and (not positive or result > 0), code)
        return result
    except (ValueError, ArithmeticError):
        raise AdmissionBlocked(code) from None


def _integer(value, code, *, minimum=None):
    number = _decimal(value, code)
    _require(number == number.to_integral_value(), code)
    result = int(number)
    _require(abs(result) <= 2**63-1 and (minimum is None or result >= minimum), code)
    return result


def _date(value, code="LIVE_EVIDENCE_TIMESTAMP_INVALID"):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        _require(result.tzinfo is not None, code)
        return result.astimezone(_UTC)
    except (TypeError, ValueError, OverflowError):
        raise AdmissionBlocked(code) from None


def _number(value, code, *, minimum=None, maximum=None):
    _require(type(value) in (int, float), code)
    result = float(value)
    _require(math.isfinite(result) and (minimum is None or result >= minimum)
             and (maximum is None or result <= maximum), code)
    return result


def _text(value, code):
    _require(isinstance(value, str) and value.strip() == value and 0 < len(value) <= 256, code)
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise AdmissionBlocked("LIVE_EVIDENCE_JSON_INVALID") from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _payload(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
        _require(isinstance(value, dict), "LIVE_EVIDENCE_PAYLOAD_INVALID")
        return deepcopy(value)
    except (TypeError, ValueError):
        raise AdmissionBlocked("LIVE_EVIDENCE_PAYLOAD_INVALID") from None


def _money(raw, code, *, positive=False):
    _require(isinstance(raw, Mapping) and str(raw.get("currency", "")).upper() == "RUB", code)
    try:
        value = quotation_to_decimal(raw)
    except Exception:
        raise AdmissionBlocked(code) from None
    return _decimal(value, code, positive=positive)


def _quotation(raw, code):
    try:
        return _decimal(quotation_to_decimal(raw), code)
    except Exception:
        raise AdmissionBlocked(code) from None


def _rows(raw, key):
    rows = raw.get(key, [])
    _require(isinstance(rows, list) and len(rows) <= MAX_ACCOUNT_ROWS
             and all(isinstance(row, dict) for row in rows), "LIVE_ACCOUNT_RESPONSE_INVALID:" + key)
    return rows


def policy_identity():
    """Exact runtime policy/deployment binding; no historical stamps are filled in."""
    identity = EV.version_identity()
    _require(identity.get("complete") is True, "LIVE_DEPLOYMENT_IDENTITY_REQUIRED")
    return {**{k: identity[k] for k in ("strategy_epoch", "strategy_entry_sha", "strategy_policy_hash")},
            "live_policy_sha256": _hash(CTC.LIVE_RISK_POLICY), "policy_version": CTC.VERSION}


def model_binding(terms, environment="production"):
    context = P.approved_entry_context(terms)
    event = context.get("event") or {}
    source = terms.get("source_identity")
    _require(isinstance(source, dict) and source and source == P.json_safe(context.get("source_identity")),
             "LIVE_SIGNAL_SOURCE_IDENTITY_REQUIRED")
    _require(terms.get("policy_version") == CTC.VERSION, "LIVE_CANDIDATE_POLICY_MISMATCH")
    binding = {key: _text(terms.get(key), "LIVE_CANDIDATE_IDENTITY_REQUIRED:" + key)
               for key in ("account_id", "instrument_uid", "asset", "horizon", "direction", "model_version")}
    binding.update(environment=environment,
                   event_family=_text(event.get("event_type"), "LIVE_EVENT_FAMILY_REQUIRED"),
                   source_identity_sha256=P.fingerprint(source), **policy_identity())
    return binding


def canonical_json(value):
    """Stable, bounded issuer HMAC representation; document JSON stays a string.

    The inner audited document may contain finite native numerical metrics. Its
    exact JSON string survives JSONB readback without changing the signed bytes.
    The outer transport has only strings, integers, booleans, lists and objects.
    """
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
    raw = _json(value)
    _require(len(raw.encode()) <= MAX_BYTES, "EVIDENCE_TOO_LARGE")
    return raw


def evidence_scope(terms):
    """Bind an independent issuer's attestation to exact immutable order terms."""
    import veritas_currency_manual as M
    if M.applies(terms):
        from veritas_currency_manual_admission import binding as manual_binding
        binding = manual_binding(terms)
    else:
        binding = model_binding(terms)
    environment = terms.get("execution_environment", "production")
    _require(environment == "production", "LIVE_PRODUCTION_BROKER_REQUIRED")
    return {"account_id": binding["account_id"], "instrument_uid": binding["instrument_uid"],
            "execution_environment": environment, "asset": binding["asset"],
            "model_version": binding["model_version"], "policy_version": binding["policy_version"],
            "horizon": binding["horizon"], "source_hash": binding["source_identity_sha256"],
            "terms_hash": P.fingerprint(terms), "model_binding": binding}


def audited_evidence_payload(terms, document, artifact_bytes, review, *, issuer_id=None, evidence_id=None):
    """Package reviewed originals for an independent issuer; never sign or approve.

    The issuer signs canonical_json(result) with its separate HMAC-SHA256 key.
    Original artifacts are base64 bytes, or null references to already staged
    originals that are read and verified again at both import and admission.
    This v2 envelope intentionally cannot promote the old data-only v1 reports.
    """
    doc, review = _payload(document), _payload(review)
    _require(artifact_bytes is None or isinstance(artifact_bytes, Mapping), "LIVE_ARTIFACT_BYTES_REQUIRED")
    if artifact_bytes is None:
        _require(isinstance(doc.get("artifacts"), dict), "LIVE_EVIDENCE_MANIFEST_REQUIRED")
        artifact_bytes = {entry.get("sha256"): None for entry in doc["artifacts"].values() if isinstance(entry, dict)}
    blobs = {}
    for digest, raw in artifact_bytes.items():
        _require(isinstance(digest, str) and _HEX.fullmatch(digest)
                 and (raw is None or isinstance(raw, bytes) and raw), "LIVE_ARTIFACT_BYTES_REQUIRED")
        blobs[digest] = base64.b64encode(raw).decode("ascii") if raw is not None else None
    observed = _date(review.get("reviewed_at"))
    maximum = MAX_SIGNED_MODEL_AGE if doc.get("kind") == "MODEL" else MAX_ACCOUNT_AGE
    result = {"version": SIGNED_EVIDENCE_SCHEMA, "kind": {"MODEL": "MODEL_ADMISSION",
        "ACCOUNT_HISTORY": "ACCOUNT_CONTROLS"}.get(doc.get("kind")),
        "issuer_id": _text(issuer_id or review.get("reviewer"), "LIVE_EVIDENCE_ISSUER_REQUIRED"),
        "observed_at": observed.isoformat(),
        "valid_until": min(_date(doc.get("valid_until")), observed+maximum).isoformat(),
        "scope": evidence_scope(terms), "document_json": _json(doc), "review_json": _json(review),
        "artifact_bytes": blobs}
    result["evidence_id"] = evidence_id or str(uuid.uuid5(uuid.NAMESPACE_URL, _hash(result)))
    canonical_json(result)
    return result


def _validate_document(document, now, *, kind=None, binding=None, allow_revoked=False):
    doc = _payload(document)
    _require(len(_json(doc).encode()) <= MAX_ARTIFACT_BYTES, "LIVE_ARTIFACT_TOO_LARGE")
    _require(doc.get("schema") == EVIDENCE_SCHEMA, "LIVE_EVIDENCE_SCHEMA_MISMATCH")
    _require(doc.get("kind") in ("MODEL", "ACCOUNT_HISTORY"), "LIVE_EVIDENCE_KIND_INVALID")
    _require(kind is None or doc["kind"] == kind, "LIVE_EVIDENCE_KIND_MISMATCH")
    scope = doc.get("binding")
    _require(isinstance(scope, dict) and scope.get("environment") == "production"
             and isinstance(scope.get("account_id"), str) and scope["account_id"], "LIVE_EVIDENCE_SCOPE_INVALID")
    _require(binding is None or _json(scope) == _json(binding), "LIVE_EVIDENCE_BINDING_MISMATCH")
    observed = _date(doc.get("observed_at"))
    expires = _date(doc.get("valid_until"))
    bound = MAX_MODEL_AGE if doc["kind"] == "MODEL" else MAX_ACCOUNT_AGE
    _require(observed <= now + timedelta(seconds=2), "LIVE_EVIDENCE_FROM_FUTURE")
    _require(observed < expires <= observed + bound, "LIVE_EVIDENCE_VALIDITY_INVALID")
    _require(now < expires and now - observed <= bound, "LIVE_" + doc["kind"] + "_EVIDENCE_STALE")
    _require(type(doc.get("revoked", False)) is bool, "LIVE_EVIDENCE_REVOCATION_INVALID")
    _require(allow_revoked or not doc.get("revoked", False), "LIVE_" + doc["kind"] + "_EVIDENCE_REVOKED")
    _require(isinstance(doc.get("payload"), dict), "LIVE_EVIDENCE_PAYLOAD_INVALID")
    domains = MODEL_DOMAINS if doc["kind"] == "MODEL" else ACCOUNT_DOMAINS
    manifest = doc.get("artifacts")
    _require(isinstance(manifest, dict) and len(manifest) <= 12, "LIVE_EVIDENCE_MANIFEST_REQUIRED")
    for domain, origin in domains.items():
        item = manifest.get(domain)
        _require(isinstance(item, dict) and item.get("origin") == origin
                 and isinstance(item.get("sha256"), str) and _HEX.fullmatch(item["sha256"]),
                 "LIVE_INDEPENDENT_ARTIFACT_REQUIRED:" + domain)
    _require(len({manifest[domain]["sha256"] for domain in domains}) == len(domains),
             "LIVE_DISTINCT_INDEPENDENT_ARTIFACTS_REQUIRED")
    return doc


def ensure_schema(c):
    """Run in the application's explicit PostgreSQL transaction, lazily once."""
    c.execute(f"""CREATE TABLE IF NOT EXISTS {ARTIFACT_TABLE} (
        sha256 TEXT PRIMARY KEY, payload BYTEA NOT NULL,
        CHECK(length(payload)>0 AND length(payload)<=2097152)
    )""")
    c.execute(f"""CREATE TABLE IF NOT EXISTS {EVIDENCE_TABLE} (
        content_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL,
        document JSONB NOT NULL, review JSONB NOT NULL,
        CHECK(kind IN ('MODEL','ACCOUNT_HISTORY'))
    )""")
    c.execute(f"""CREATE INDEX IF NOT EXISTS veritas_currency_live_evidence_scope_v1
        ON {EVIDENCE_TABLE}(kind,binding_sha256,reviewed_at DESC)""")
    c.execute(f"""CREATE TABLE IF NOT EXISTS {OBSERVATION_TABLE} (
        observation_sha256 TEXT PRIMARY KEY, account_binding_sha256 TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL, snapshot_sha256 TEXT NOT NULL, payload JSONB NOT NULL
    )""")
    c.execute(f"""CREATE INDEX IF NOT EXISTS veritas_currency_live_observations_scope_v1
        ON {OBSERVATION_TABLE}(account_binding_sha256,observed_at DESC)""")


def publish_audited_artifact(connect, document, artifact_bytes, review, *, now=None):
    """Internal reviewer boundary: persist exact audited bytes, never approve trades.

    Every referenced artifact must be supplied and hash-checked.  The caller is
    responsible for independent review; a hash proves identity, not correctness.
    Neither paper summaries nor the research calibration read model satisfy this
    schema.  Identical submissions are idempotent; existing rows are never edited.
    """
    now = _date(now or datetime.now(_UTC))
    doc = _validate_document(document, now, allow_revoked=True)
    review = _payload(review)
    for key in ("reviewer", "audit_id"):
        _text(review.get(key), "LIVE_AUDIT_REVIEW_REQUIRED:" + key)
    _require(review.get("decision") == "ACCEPT_EVIDENCE", "LIVE_AUDIT_REVIEW_REQUIRED")
    reviewed = _date(review.get("reviewed_at"))
    _require(_date(doc["observed_at"]) <= reviewed <= now + timedelta(seconds=2)
             and now - reviewed <= MAX_MODEL_AGE, "LIVE_AUDIT_REVIEW_TIMESTAMP_INVALID")
    _require(isinstance(artifact_bytes, Mapping), "LIVE_ARTIFACT_BYTES_REQUIRED")
    size = len(_json(doc).encode()) + len(_json(review).encode())
    for entry in doc["artifacts"].values():
        _require(isinstance(entry, dict) and isinstance(entry.get("sha256"), str), "LIVE_EVIDENCE_MANIFEST_REQUIRED")
        content = artifact_bytes.get(entry["sha256"])
        _require(isinstance(content, bytes) and content, "LIVE_ARTIFACT_BYTES_REQUIRED")
        size += len(content)
        _require(size <= MAX_ARTIFACT_BYTES, "LIVE_ARTIFACT_TOO_LARGE")
        _require(hashlib.sha256(content).hexdigest() == entry["sha256"], "LIVE_ARTIFACT_DIGEST_MISMATCH")
    # Validate typed evidence and computation at ingestion as well as admission.
    if doc["kind"] == "MODEL":
        _model_evidence(doc)
    else:
        _account_history(doc, None, now)
    content_hash = _hash({"document": doc, "review": review})
    with connect() as c, c.transaction():
        ensure_schema(c)
        for digest in {entry["sha256"] for entry in doc["artifacts"].values()}:
            c.execute(f"""INSERT INTO {ARTIFACT_TABLE}(sha256,payload) VALUES (%s,%s)
                ON CONFLICT(sha256) DO NOTHING""", (digest, artifact_bytes[digest]))
        c.execute(f"""INSERT INTO {EVIDENCE_TABLE}
            (content_sha256,kind,binding_sha256,reviewed_at,document,review)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT(content_sha256) DO NOTHING""",
            (content_hash, doc["kind"], _hash(doc["binding"]), reviewed, _json(doc), _json(review)))
    return {"content_sha256": content_hash, "kind": doc["kind"], "status": "EVIDENCE_RECORDED",
            "trade_permission": False}


def publish_validation_snapshot(connect, snapshot_id, artifact_bytes, review, *, now=None):
    """Import an existing audited validation snapshot in the exact export schema.

    Legacy payloads have no implicit conversion: they fail with an explicit
    schema blocker.  This is an intake path for a validation producer, not a
    promotion inferred from paper statistics.
    """
    _require(type(snapshot_id) is int and snapshot_id > 0, "LIVE_VALIDATION_SNAPSHOT_ID_INVALID")
    with connect() as c:
        # Use the application's canonical search_path (veritas_v90 in the live
        # application); legacy validation_snapshots is not a public-schema table.
        row = c.execute("SELECT payload FROM validation_snapshots WHERE snapshot_id=%s", (snapshot_id,)).fetchone()
    _require(row is not None, "LIVE_VALIDATION_SNAPSHOT_MISSING")
    source = _payload(row["payload"])
    _require(isinstance(source.get("live_authority_artifact"), dict), "LIVE_VALIDATION_ARTIFACT_EXPORT_REQUIRED")
    return publish_audited_artifact(connect, source["live_authority_artifact"], artifact_bytes, review, now=now)


class EvidenceStore:
    def __init__(self, connect):
        _require(callable(connect), "LIVE_DURABLE_CONNECT_REQUIRED")
        self.connect = connect
        self._schema_ready = False
        self._lock = threading.Lock()

    def _schema(self):
        with self._lock:
            if not self._schema_ready:
                with self.connect() as c, c.transaction():
                    ensure_schema(c)
                self._schema_ready = True

    def load(self, kind, binding, now):
        self._schema()
        with self.connect() as c:
            rows = c.execute(f"""SELECT content_sha256,reviewed_at,document,review
                FROM {EVIDENCE_TABLE} WHERE kind=%s AND binding_sha256=%s
                ORDER BY reviewed_at DESC,content_sha256 DESC LIMIT 2""", (kind, _hash(binding))).fetchall()
        _require(rows, "LIVE_" + kind + "_EVIDENCE_REQUIRED")
        _require(len(rows) == 1 or _date(rows[0]["reviewed_at"]) != _date(rows[1]["reviewed_at"]),
                 "LIVE_" + kind + "_EVIDENCE_AMBIGUOUS")
        row = rows[0]
        doc, review = _payload(row["document"]), _payload(row["review"])
        _require(_hash({"document": doc, "review": review}) == row["content_sha256"], "LIVE_EVIDENCE_DIGEST_MISMATCH")
        _require(review.get("decision") == "ACCEPT_EVIDENCE", "LIVE_AUDIT_REVIEW_REQUIRED")
        for key in ("reviewer", "audit_id"):
            _text(review.get(key), "LIVE_AUDIT_REVIEW_REQUIRED:" + key)
        _require(_date(review.get("reviewed_at")) == _date(row["reviewed_at"])
                 and _date(review["reviewed_at"]) <= now + timedelta(seconds=2), "LIVE_AUDIT_REVIEW_TIMESTAMP_INVALID")
        doc = _validate_document(doc, now, kind=kind, binding=binding)
        digests = {item["sha256"] for item in doc["artifacts"].values()}
        _require(len(digests) <= 12, "LIVE_EVIDENCE_MANIFEST_INVALID")
        with self.connect() as c:
            placeholders = ",".join("%s" for _ in digests)
            artifacts = c.execute(f"SELECT sha256,payload FROM {ARTIFACT_TABLE} WHERE sha256 IN ({placeholders})",
                                  tuple(digests)).fetchall()
        _require({item["sha256"] for item in artifacts} == digests, "LIVE_ARTIFACT_BYTES_REQUIRED")
        total = len(_json(doc).encode()) + len(_json(review).encode())
        for item in artifacts:
            raw = item["payload"]
            _require(isinstance(raw, (bytes, memoryview)), "LIVE_ARTIFACT_BYTES_REQUIRED")
            raw = bytes(raw)
            total += len(raw)
            _require(raw and total <= MAX_ARTIFACT_BYTES, "LIVE_ARTIFACT_TOO_LARGE")
            _require(hashlib.sha256(raw).hexdigest() == item["sha256"], "LIVE_ARTIFACT_DIGEST_MISMATCH")
        return doc

    def record_snapshot(self, snapshot, observed_at):
        self._schema()
        observation = {"snapshot": snapshot, "observed_at": observed_at.isoformat()}
        with self.connect() as c, c.transaction():
            c.execute(f"""INSERT INTO {OBSERVATION_TABLE}
                (observation_sha256,account_binding_sha256,observed_at,snapshot_sha256,payload)
                VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(observation_sha256) DO NOTHING""",
                (_hash(observation), _hash(snapshot["binding"]), observed_at, _hash(snapshot), _json(observation)))


class LiveAdmissionEvidenceRepository:
    """Authenticated issuer intake plus the original immutable artifact store.

    Operator/service access alone cannot create accepted evidence. Every runtime
    load rechecks the independent issuer's MAC, exact terms scope, current
    document/baseline, and every original artifact byte. Unsigned audit imports
    are retained for review but are never enough for production admission.
    """
    def __init__(self, connect, issuer_key=None, *, clock=None):
        _require(issuer_key is None or isinstance(issuer_key, bytes) and len(issuer_key) >= 32,
                 "INVALID_LIVE_EVIDENCE_KEY")
        self.connect, self._key = connect, issuer_key
        self.clock = clock or (lambda: datetime.now(_UTC))
        self._store = EvidenceStore(connect)
        self._lock = threading.Lock()
        self._state = {"configured": issuer_key is not None, "initialized": False,
            "state": "AWAITING_VERIFIED_EVIDENCE" if issuer_key else "NOT_CONFIGURED",
            "last_import_at": None, "last_evidence_kind": None,
            "schema": SIGNED_EVIDENCE_SCHEMA, "original_artifacts_required": True}

    def status(self):
        with self._lock:
            return {**deepcopy(self._state), "configured": self._key is not None}

    def initialize(self):
        with self._lock:
            if self._state["initialized"]:
                return
            try:
                self._store._schema()
                with self.connect() as c, c.transaction():
                    c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                        evidence_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_hash TEXT NOT NULL,
                        observed_at TIMESTAMPTZ NOT NULL, imported_at TIMESTAMPTZ NOT NULL,
                        valid_until TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL,
                        payload_hash TEXT NOT NULL, signature TEXT NOT NULL)""")
                    c.execute(f"CREATE INDEX IF NOT EXISTS currency_live_evidence_scope ON {TABLE}(scope_hash,kind,observed_at DESC)")
                self._state["initialized"] = True
            except Exception:
                raise AdmissionBlocked("LIVE_EVIDENCE_STORAGE_UNAVAILABLE", 503) from None

    def _verify(self, payload, signature, now, *, allow_revoked=False):
        _require(self._key is not None, "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED")
        raw = canonical_json(payload)
        if not (isinstance(signature, str) and _HEX.fullmatch(signature)
                and hmac.compare_digest(signature, hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest())):
            raise AdmissionBlocked("LIVE_EVIDENCE_SIGNATURE_INVALID", 403)
        _require(isinstance(payload, dict) and payload.get("version") == SIGNED_EVIDENCE_SCHEMA,
                 "LIVE_AUDITED_ARTIFACT_SCHEMA_REQUIRED")
        _require(set(payload) == {"version", "evidence_id", "kind", "issuer_id", "observed_at", "valid_until",
            "scope", "document_json", "review_json", "artifact_bytes"}, "INVALID_EVIDENCE_FIELDS")
        try:
            _require(str(uuid.UUID(payload["evidence_id"])) == payload["evidence_id"], "INVALID_EVIDENCE_ID")
        except (ValueError, TypeError, AttributeError):
            raise AdmissionBlocked("INVALID_EVIDENCE_ID") from None
        _text(payload["issuer_id"], "LIVE_EVIDENCE_ISSUER_REQUIRED")
        scope = payload["scope"]
        required = {"account_id", "instrument_uid", "execution_environment", "asset", "model_version",
                    "policy_version", "horizon", "source_hash", "terms_hash", "model_binding"}
        _require(isinstance(scope, dict) and set(scope) == required, "INVALID_EVIDENCE_SCOPE")
        for key in required - {"model_binding"}:
            _text(scope[key], "INVALID_EVIDENCE_SCOPE")
        _require(scope["execution_environment"] == "production" and scope["asset"] == "CNYRUBF"
                 and _HEX.fullmatch(scope["source_hash"]) and _HEX.fullmatch(scope["terms_hash"]),
                 "INVALID_EVIDENCE_SCOPE")
        binding = scope["model_binding"]
        _require(isinstance(binding, dict), "INVALID_EVIDENCE_SCOPE")
        for key in ("account_id", "instrument_uid", "asset", "model_version", "policy_version", "horizon"):
            _require(binding.get(key) == scope[key], "LIVE_EVIDENCE_BINDING_MISMATCH")
        _require(binding.get("environment") == "production" and
                 binding.get("source_identity_sha256") == scope["source_hash"], "LIVE_EVIDENCE_BINDING_MISMATCH")
        for key in ("document_json", "review_json"):
            _require(isinstance(payload[key], str), "LIVE_AUDITED_ARTIFACT_SCHEMA_REQUIRED")
        doc = _validate_document(payload["document_json"], now, allow_revoked=allow_revoked)
        review = _payload(payload["review_json"])
        _require(payload["document_json"] == _json(doc) and payload["review_json"] == _json(review),
                 "LIVE_EVIDENCE_NONCANONICAL_DOCUMENT")
        kind = {"MODEL": "MODEL_ADMISSION", "ACCOUNT_HISTORY": "ACCOUNT_CONTROLS"}[doc["kind"]]
        _require(payload["kind"] == kind, "LIVE_EVIDENCE_KIND_MISMATCH")
        expected = binding if doc["kind"] == "MODEL" else {"account_id": scope["account_id"], "environment": "production"}
        _require(doc["binding"] == expected, "LIVE_EVIDENCE_BINDING_MISMATCH")
        for key in ("reviewer", "audit_id"):
            _text(review.get(key), "LIVE_AUDIT_REVIEW_REQUIRED:" + key)
        _require(review.get("decision") == "ACCEPT_EVIDENCE", "LIVE_AUDIT_REVIEW_REQUIRED")
        observed, expires = _date(payload["observed_at"]), _date(payload["valid_until"])
        maximum = MAX_SIGNED_MODEL_AGE if doc["kind"] == "MODEL" else MAX_ACCOUNT_AGE
        _require(observed == _date(review.get("reviewed_at")) and
                 _date(doc["observed_at"]) <= observed <= now+timedelta(seconds=2), "LIVE_AUDIT_REVIEW_TIMESTAMP_INVALID")
        _require(observed < expires <= min(_date(doc["valid_until"]), observed+maximum), "INVALID_EVIDENCE_VALIDITY")
        _require(now < expires, "LIVE_" + doc["kind"] + "_EVIDENCE_STALE")
        if doc["kind"] == "MODEL":
            _model_evidence(doc)
        else:
            _account_history(doc, None, now)
        blobs = payload["artifact_bytes"]
        digests = {entry["sha256"] for entry in doc["artifacts"].values()}
        _require(isinstance(blobs, dict) and set(blobs) == digests and len(blobs) <= 12,
                 "LIVE_ARTIFACT_BYTES_REQUIRED")
        staged = {}
        references = [digest for digest, encoded in blobs.items() if encoded is None]
        if references:
            try:
                with self.connect() as c:
                    placeholders = ",".join("%s" for _ in references)
                    stored = c.execute(f"SELECT sha256,payload FROM {ARTIFACT_TABLE} WHERE sha256 IN ({placeholders})",
                                       tuple(references)).fetchall()
                staged = {row["sha256"]: row["payload"] for row in stored}
            except Exception:
                raise AdmissionBlocked("LIVE_EVIDENCE_STORAGE_UNAVAILABLE", 503) from None
            _require(set(staged) == set(references), "LIVE_ARTIFACT_BYTES_REQUIRED")
        artifacts = {}
        total = len(payload["document_json"].encode()) + len(payload["review_json"].encode())
        for digest, encoded in blobs.items():
            if encoded is None:
                _require(isinstance(staged[digest], (bytes, memoryview)), "LIVE_ARTIFACT_BYTES_REQUIRED")
                content = bytes(staged[digest])
            else:
                try:
                    _require(isinstance(encoded, str), "LIVE_ARTIFACT_BYTES_REQUIRED")
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    raise AdmissionBlocked("LIVE_ARTIFACT_BYTES_REQUIRED") from None
                _require(base64.b64encode(content).decode("ascii") == encoded, "LIVE_ARTIFACT_BYTES_REQUIRED")
            total += len(content)
            _require(content and total <= MAX_ARTIFACT_BYTES, "LIVE_ARTIFACT_TOO_LARGE")
            _require(hashlib.sha256(content).hexdigest() == digest, "LIVE_ARTIFACT_DIGEST_MISMATCH")
            artifacts[digest] = content
        return doc, review, artifacts

    def publish(self, payload, signature, *, now=None):
        stamp = _date(now or self.clock())
        doc, review, artifacts = self._verify(payload, signature, stamp, allow_revoked=True)
        record = deepcopy(payload)
        digest = _hash(record)
        self.initialize()
        try:
            with self.connect() as c:
                prior = c.execute(f"SELECT payload_hash FROM {TABLE} WHERE evidence_id=%s",
                                  (record["evidence_id"],)).fetchone()
            _require(prior is None or prior["payload_hash"] == digest, "LIVE_EVIDENCE_ID_REUSED")
            receipt = publish_audited_artifact(self.connect, doc, artifacts, review, now=stamp)
            with self.connect() as c, c.transaction():
                c.execute(f"""INSERT INTO {TABLE}
                    (evidence_id,kind,scope_hash,observed_at,imported_at,valid_until,payload,payload_hash,signature)
                    VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(evidence_id) DO NOTHING""",
                    (record["evidence_id"], record["kind"], _hash(record["scope"]), record["observed_at"],
                     stamp, record["valid_until"], canonical_json(record), digest, signature))
                saved = c.execute(f"SELECT payload_hash FROM {TABLE} WHERE evidence_id=%s",
                                  (record["evidence_id"],)).fetchone()
                _require(saved and saved["payload_hash"] == digest, "LIVE_EVIDENCE_ID_REUSED")
        except AdmissionBlocked:
            raise
        except Exception:
            raise AdmissionBlocked("LIVE_EVIDENCE_STORAGE_UNAVAILABLE", 503) from None
        with self._lock:
            self._state.update(state="EVIDENCE_IMPORTED", last_import_at=stamp.isoformat(),
                               last_evidence_kind=record["kind"])
        return {**receipt, "ok": True, "evidence_id": record["evidence_id"], "scope_hash": _hash(record["scope"]),
                "valid_until": record["valid_until"], "payload_hash": digest}

    def load(self, kind, binding, now, *, terms):
        _require(self._key is not None, "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED")
        self.initialize()
        scope = evidence_scope(terms)
        signed_kind = {"MODEL": "MODEL_ADMISSION", "ACCOUNT_HISTORY": "ACCOUNT_CONTROLS"}[kind]
        with self.connect() as c:
            rows = c.execute(f"""SELECT payload,payload_hash,signature,observed_at FROM {TABLE}
                WHERE kind=%s AND scope_hash=%s ORDER BY observed_at DESC,imported_at DESC,evidence_id DESC LIMIT 2""",
                (signed_kind, _hash(scope))).fetchall()
        _require(rows, "LIVE_" + kind + "_EVIDENCE_REQUIRED")
        record = _payload(rows[0]["payload"])
        _require(_hash(record) == rows[0]["payload_hash"], "LIVE_EVIDENCE_STORAGE_INTEGRITY")
        doc, review, _ = self._verify(record, rows[0]["signature"], now)
        _require(record["scope"] == scope and doc["binding"] == binding
                 and record["kind"] == signed_kind, "LIVE_EVIDENCE_SCOPE_MISMATCH")
        if len(rows) > 1 and _date(rows[0]["observed_at"]) == _date(rows[1]["observed_at"]):
            previous = _payload(rows[1]["payload"])
            _require((record["document_json"], record["review_json"]) ==
                     (previous.get("document_json"), previous.get("review_json")), "LIVE_" + kind + "_EVIDENCE_AMBIGUOUS")
        # The latest audited report still wins globally for this model/account
        # binding, including a later failure or revocation for another proposal.
        # The original archive also rechecks the actual persisted artifact bytes.
        latest = self._store.load(kind, binding, now)
        _require(latest == doc, "LIVE_" + kind + "_ISSUER_REATTESTATION_REQUIRED")
        doc["issuer_valid_until"] = record["valid_until"]
        return doc

    def record_snapshot(self, snapshot, observed_at):
        return self._store.record_snapshot(snapshot, observed_at)


def _model_evidence(doc):
    data = doc["payload"]
    raw = data.get("promotion")
    names = {field.name for field in fields(PROM.PromotionEvidence)}
    _require(isinstance(raw, dict) and set(raw) == names, "LIVE_PROMOTION_EVIDENCE_INCOMPLETE")
    evidence = PROM.PromotionEvidence(**raw)
    result = PROM.promotion_gate(evidence)
    _require(not result.get("invalid_evidence_fields"), "LIVE_PROMOTION_EVIDENCE_INVALID")
    _require(evidence.model_version == doc["binding"].get("model_version"), "LIVE_PROMOTION_MODEL_MISMATCH")
    calibration = data.get("calibration")
    _require(isinstance(calibration, dict) and calibration.get("method") == "EMPIRICAL_HELD_OUT_COHORT",
             "LIVE_CALIBRATION_MODEL_REQUIRED")
    _require(calibration.get("population_binding_sha256") == _hash(doc["binding"]), "LIVE_CALIBRATION_POPULATION_MISMATCH")
    n, successes = calibration.get("n"), calibration.get("successes")
    _require(type(n) is int and n > 0 and type(successes) is int and 0 <= successes <= n
             and n == evidence.calibration_n, "LIVE_CALIBRATION_COUNTS_INVALID")
    # This probability comes from the reviewed, versioned held-out cohort.
    # A signal's pwin/confidence/paper eligibility is intentionally never read.
    return evidence, successes / n, result


def _account_history(doc, snapshot, now):
    data = doc["payload"]
    _require(data.get("method") == "BROKER_CASHFLOW_ADJUSTED_UNIT_NAV", "LIVE_ACCOUNT_NAV_METHOD_REQUIRED")
    _require(data.get("cashflows_reconciled") is True, "LIVE_ACCOUNT_CASHFLOW_RECONCILIATION_REQUIRED")
    observed = _date(doc["observed_at"])
    _require(_date(data.get("cashflows_reconciled_through")) >= observed
             and _date(data["cashflows_reconciled_through"]) <= now + timedelta(seconds=2),
             "LIVE_ACCOUNT_CASHFLOW_COVERAGE_REQUIRED")
    # Broker account periods follow its Moscow trading calendar, not host TZ.
    local = now.astimezone(ZoneInfo("Europe/Moscow"))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    _require(_date(data.get("day_start_at"), "LIVE_DAILY_NAV_BASELINE_REQUIRED") == day_start.astimezone(_UTC), "LIVE_DAILY_NAV_BASELINE_REQUIRED")
    _require(_date(data.get("week_start_at"), "LIVE_WEEKLY_NAV_BASELINE_REQUIRED") == week_start.astimezone(_UTC), "LIVE_WEEKLY_NAV_BASELINE_REQUIRED")
    epoch = _date(data.get("risk_epoch_start_at"))
    _require(epoch <= week_start.astimezone(_UTC)
             and _date(data.get("history_covered_from")) <= epoch, "LIVE_ACCOUNT_HISTORY_COVERAGE_REQUIRED")
    equity = _decimal(data.get("equity_rub"), "LIVE_ACCOUNT_HISTORY_EQUITY_REQUIRED", positive=True)
    units = _decimal(data.get("units_outstanding"), "LIVE_ACCOUNT_NAV_UNITS_REQUIRED", positive=True)
    current = equity / units
    daily = _decimal(data.get("day_start_unit_nav"), "LIVE_DAILY_NAV_BASELINE_REQUIRED", positive=True)
    weekly = _decimal(data.get("week_start_unit_nav"), "LIVE_WEEKLY_NAV_BASELINE_REQUIRED", positive=True)
    peak = _decimal(data.get("high_water_unit_nav"), "LIVE_HIGH_WATER_NAV_REQUIRED", positive=True)
    _require(peak >= max(daily, weekly), "LIVE_HIGH_WATER_NAV_INCONSISTENT")
    _require(isinstance(data.get("snapshot_sha256"), str) and _HEX.fullmatch(data["snapshot_sha256"]),
             "LIVE_ACCOUNT_HISTORY_SNAPSHOT_BINDING_REQUIRED")
    if snapshot is not None:
        _require(data["snapshot_sha256"] == _hash(snapshot)
                 and equity == _decimal(snapshot["equity_rub"], "LIVE_ACCOUNT_EQUITY_REQUIRED", positive=True),
                 "LIVE_ACCOUNT_HISTORY_SNAPSHOT_MISMATCH")
    _require(type(data.get("kill_switch")) is bool, "LIVE_ACCOUNT_KILL_SWITCH_STATE_REQUIRED")
    return {"daily_pnl_pct": float(current / daily - 1), "weekly_pnl_pct": float(current / weekly - 1),
            "drawdown": float(max(_D(0), 1-current/max(current, peak))), "kill_switch": data["kill_switch"]}


def _futures_spec(raw, uid):
    _require(isinstance(raw, dict) and raw.get("uid") == uid
             and raw.get("realExchange") == "REAL_EXCHANGE_MOEX"
             and str(raw.get("currency", "")).upper() == "RUB", "LIVE_ACCOUNT_FUTURE_SPEC_REQUIRED:" + uid)
    lot = _integer(raw.get("lot"), "LIVE_ACCOUNT_FUTURE_LOT_INVALID", minimum=1)
    tick = _quotation(raw.get("minPriceIncrement"), "LIVE_ACCOUNT_FUTURE_TICK_INVALID")
    value = _quotation(raw.get("minPriceIncrementAmount"), "LIVE_ACCOUNT_FUTURE_TICK_AMOUNT_INVALID")
    _require(tick > 0 and value > 0, "LIVE_ACCOUNT_FUTURE_SPEC_INVALID")
    return lot, tick, value, value/tick


def _positions_view(raw):
    _require(isinstance(raw, dict), "LIVE_ACCOUNT_POSITIONS_INVALID")
    _require(raw.get("limitsLoadingInProgress", False) is False, "LIVE_ACCOUNT_LIMITS_LOADING")
    holdings = []
    seen = set()
    for kind in ("futures", "securities", "options"):
        for row in _rows(raw, kind):
            uid = _text(row.get("instrumentUid"), "LIVE_ACCOUNT_POSITION_ID_REQUIRED")
            _require(uid not in seen, "LIVE_ACCOUNT_DUPLICATE_POSITION")
            seen.add(uid)
            quantity = _integer(row.get("balance", "0"), "LIVE_ACCOUNT_POSITION_QUANTITY_INVALID")
            blocked = _integer(row.get("blocked", "0"), "LIVE_ACCOUNT_BLOCKED_QUANTITY_INVALID", minimum=0)
            _require(blocked == 0, "LIVE_ACCOUNT_BLOCKED_POSITION_UNRECONCILED:" + uid)
            if quantity:
                _require(kind == "futures", "LIVE_ACCOUNT_UNSUPPORTED_POSITION:" + uid)
                holdings.append({"instrument_uid": uid, "quantity": quantity, "kind": kind})
    cash = []
    for row in _rows(raw, "money"):
        amount = _quotation(row, "LIVE_ACCOUNT_CASH_INVALID")
        currency = str(row.get("currency", "")).upper()
        _require(currency and not any(x["currency"] == currency for x in cash), "LIVE_ACCOUNT_CASH_INVALID")
        _require(currency == "RUB" or amount == 0, "LIVE_ACCOUNT_FX_EXPOSURE_UNSUPPORTED:" + currency)
        cash.append({"currency": currency, "amount": str(amount)})
    for row in _rows(raw, "blocked"):
        _require(_quotation(row, "LIVE_ACCOUNT_BLOCKED_CASH_INVALID") == 0, "LIVE_ACCOUNT_BLOCKED_CASH_UNRECONCILED")
    return {"positions": sorted(holdings, key=lambda x: x["instrument_uid"]),
            "cash": sorted(cash, key=lambda x: x["currency"])}


def _correlations(doc, assets):
    if len(assets) <= 1:
        return {}
    data = doc["payload"].get("correlations")
    manifest = doc["artifacts"].get("correlations")
    _require(isinstance(data, dict) and isinstance(manifest, dict)
             and manifest.get("origin") == "VERSIONED_RETURN_CORRELATION", "LIVE_CORRELATION_EVIDENCE_REQUIRED")
    _require(type(data.get("observations")) is int and data["observations"] >= 2
             and isinstance(data.get("matrix"), dict), "LIVE_CORRELATION_EVIDENCE_INVALID")
    matrix = data["matrix"]
    result = {}
    for index, a in enumerate(assets):
        for b in assets[index+1:]:
            ab = matrix.get(a, {}).get(b) if isinstance(matrix.get(a, {}), dict) else None
            ba = matrix.get(b, {}).get(a) if isinstance(matrix.get(b, {}), dict) else None
            value = ab if ab is not None else ba
            value = _number(value, "LIVE_CORRELATION_PAIR_REQUIRED:" + a + ":" + b, minimum=-1, maximum=1)
            if ab is not None and ba is not None:
                _require(value == _number(ba, "LIVE_CORRELATION_VALUE_INVALID", minimum=-1, maximum=1),
                         "LIVE_CORRELATION_ASYMMETRIC")
            result.setdefault(a, {})[b] = value
    return result


class WholeAccountLiveAdmission:
    def __init__(self, connect, adapter, account_id, clock=None, *, evidence=None):
        self.adapter = adapter
        self.account_id = _text(account_id, "LIVE_EXPLICIT_ACCOUNT_REQUIRED")
        self.clock = clock or (lambda: datetime.now(_UTC))
        _require(callable(self.clock), "LIVE_CLOCK_REQUIRED")
        self.store = evidence if evidence is not None else LiveAdmissionEvidenceRepository(connect, clock=self.clock)
        _require(isinstance(self.store, LiveAdmissionEvidenceRepository), "LIVE_AUTHENTICATED_EVIDENCE_LOADER_REQUIRED")
        self._status_lock = threading.Lock()
        self._last = {"eligible": False, "status": "NOT_EVALUATED", "version": VERSION,
                      "blockers": ["LIVE_MODEL_EVIDENCE_NOT_CHECKED", "LIVE_ACCOUNT_HISTORY_EVIDENCE_NOT_CHECKED"],
                      "scope": "WHOLE_BROKER_ACCOUNT", "evidence_loader_configured": True,
                      "required_checks": ["INDEPENDENT_ISSUER_SIGNATURE_AND_EXACT_TERMS",
                          "CURRENT_MODEL_OOS_VAULT_COSTS_CALIBRATION_SHADOW_CI_PARITY",
                          "EXACT_BROKER_CONTRACT_AND_FRESH_QUOTE", "WHOLE_ACCOUNT_EQUITY_POSITIONS_WORKING_ORDERS",
                          "CASHFLOW_ADJUSTED_DAILY_WEEKLY_HIGH_WATER_NAV", "DURABLE_ACCOUNT_OBSERVATION",
                          "LIVE_ECONOMICS_AND_PORTFOLIO_RISK", "LIVE_ENABLED_AND_ARMED"]}

    def status(self):
        """Safe cached diagnostics. A display read never calls a broker or grants admission."""
        with self._status_lock:
            result = deepcopy(self._last)
        result["admission_cached_only"] = True
        result["evidence"] = self.store.status()
        if not result["evidence"]["configured"]:
            result["blockers"].append("LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED")
        arm = LIVE._armed()
        result["live_switch"] = arm
        if not arm["enabled"]:
            result["blockers"].append("LIVE_EXECUTION_DISABLED")
        if not arm["armed"]:
            result["blockers"].append("LIVE_EXECUTION_NOT_ARMED")
        if result.get("eligible") and _date(self.clock()) >= _date(result["valid_until"]):
            result.update(eligible=False, status="STALE")
            result["blockers"].append("LIVE_ADMISSION_RECHECK_REQUIRED")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        if result["blockers"]:
            result["eligible"] = False
            if result["status"] == "PASS":
                result["status"] = "BLOCK"
        return result

    def _snapshot(self, terms, facts, now):
        account = self.account_id
        _require(getattr(self.adapter, "environment", None) == "production", "LIVE_PRODUCTION_BROKER_REQUIRED")
        accounts = self.adapter.get_accounts()
        _require(isinstance(accounts, list) and len(accounts) <= MAX_ACCOUNT_ROWS, "LIVE_ACCOUNT_RESPONSE_INVALID")
        matching = [row for row in accounts if isinstance(row, dict) and row.get("id") == account]
        _require(len(matching) == 1 and matching[0].get("status") == "ACCOUNT_STATUS_OPEN"
                 and matching[0].get("accessLevel") == "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS", "LIVE_ACCOUNT_ACCESS_REQUIRED")
        portfolio = self.adapter.get_portfolio(account)
        raw_positions = self.adapter.get_positions(account)
        _require(isinstance(portfolio, dict) and portfolio.get("accountId") == account
                 and isinstance(raw_positions, dict) and raw_positions.get("accountId", account) == account,
                 "LIVE_WHOLE_ACCOUNT_IDENTITY_MISMATCH")
        equity = _money(portfolio.get("totalAmountPortfolio"), "LIVE_ACCOUNT_EQUITY_REQUIRED", positive=True)
        view = _positions_view(raw_positions)
        _require(not _rows(portfolio, "virtualPositions"), "LIVE_ACCOUNT_VIRTUAL_EXPOSURE_UNSUPPORTED")
        portfolio_positions = {}
        for row in _rows(portfolio, "positions"):
            quantity = _quotation(row.get("quantity"), "LIVE_PORTFOLIO_QUANTITY_INVALID")
            if not quantity:
                continue
            if row.get("instrumentType") == "currency" and row.get("figi") == "RUB000UTSTOM":
                continue
            uid = _text(row.get("instrumentUid"), "LIVE_PORTFOLIO_POSITION_ID_REQUIRED")
            _require(row.get("instrumentType") == "futures", "LIVE_ACCOUNT_UNSUPPORTED_POSITION:" + uid)
            _require(uid not in portfolio_positions, "LIVE_PORTFOLIO_DUPLICATE_POSITION")
            portfolio_positions[uid] = quantity
        _require(portfolio_positions == {row["instrument_uid"]: _D(row["quantity"]) for row in view["positions"]},
                 "LIVE_ACCOUNT_POSITION_RECONCILIATION_REQUIRED")
        orders = self.adapter.list_orders(account)
        stops = self.adapter.list_stop_orders(account, status="ACTIVE")
        _require(isinstance(orders, list) and len(orders) <= MAX_ACCOUNT_ROWS
                 and isinstance(stops, list) and len(stops) <= MAX_ACCOUNT_ROWS, "LIVE_ACCOUNT_ORDERS_INVALID")
        # Unknown pending exposure cannot be treated as zero or assumed to reduce.
        _require(not orders, "LIVE_WHOLE_ACCOUNT_WORKING_ORDERS_UNRECONCILED")
        for row in stops:
            _require(isinstance(row, dict) and row.get("status") == "STOP_ORDER_STATUS_ACTIVE",
                     "LIVE_ACCOUNT_STOP_STATUS_INVALID")
        uid = facts.spec.instrument_uid
        broker_spec = self.adapter.get_future(uid)
        lot, tick, value, _ = _futures_spec(broker_spec, uid)
        _require(str(broker_spec.get("ticker", "")).upper() == "CNYRUBF" and lot == facts.spec.lot_size
                 and tick == _D(facts.spec.tick_size) and value == _D(facts.spec.tick_value_rub),
                 "LIVE_CNY_CONTRACT_SPEC_MISMATCH")
        _require(broker_spec.get("apiTradeAvailableFlag") is True, "LIVE_INSTRUMENT_API_TRADING_UNAVAILABLE")
        flag = "buyAvailableFlag" if terms.get("side") == "BUY" else "sellAvailableFlag"
        _require(broker_spec.get(flag) is True, "LIVE_INSTRUMENT_DIRECTION_UNAVAILABLE")
        cny_units = next((row["quantity"] for row in view["positions"] if row["instrument_uid"] == uid), 0)
        _require(cny_units == facts.account.signed_lots * lot, "LIVE_CNY_POSITION_CHANGED")
        risks, costs, gross, used_stops = [], {}, _D(0), set()
        other_snapshots = []
        for position in view["positions"]:
            other_uid = position["instrument_uid"]
            if other_uid == uid:
                continue
            other_spec = self.adapter.get_future(other_uid)
            other_lot, _, _, rub_per_point = _futures_spec(other_spec, other_uid)
            quantity = position["quantity"]
            _require(quantity % other_lot == 0, "LIVE_ACCOUNT_NON_LOT_POSITION:" + other_uid)
            book = self.adapter.get_order_book(other_uid, depth=1)
            _require(isinstance(book, dict) and book.get("instrumentUid") == other_uid, "LIVE_ACCOUNT_QUOTE_IDENTITY_MISMATCH")
            P.fresh(book.get("orderbookTs"), now, 15, "LIVE_ACCOUNT_QUOTE_STALE")
            bids, asks = _rows(book, "bids"), _rows(book, "asks")
            _require(bids and asks, "LIVE_ACCOUNT_TWO_SIDED_QUOTE_REQUIRED")
            bid, ask = (_quotation(rows[0].get("price"), "LIVE_ACCOUNT_QUOTE_INVALID") for rows in (bids, asks))
            _require(0 < bid <= ask, "LIVE_ACCOUNT_QUOTE_INVALID")
            price = ask if quantity > 0 else bid
            protecting = [(index, stop) for index, stop in enumerate(stops) if stop.get("instrumentUid") == other_uid]
            _require(len(protecting) == 1, "LIVE_ACCOUNT_PROTECTIVE_STOP_REQUIRED:" + other_uid)
            index, stop = protecting[0]
            _require(stop.get("orderType") == "STOP_ORDER_TYPE_STOP_LOSS"
                     and stop.get("direction") == ("STOP_ORDER_DIRECTION_SELL" if quantity > 0 else "STOP_ORDER_DIRECTION_BUY")
                     and _integer(stop.get("lotsRequested"), "LIVE_ACCOUNT_STOP_QUANTITY_INVALID", minimum=1) * other_lot == abs(quantity),
                     "LIVE_ACCOUNT_PROTECTIVE_STOP_MISMATCH:" + other_uid)
            if stop.get("expirationTime"):
                _require(_date(stop["expirationTime"]) > now + MAX_ACCOUNT_AGE, "LIVE_ACCOUNT_STOP_EXPIRING")
            stop_rub = _money(stop.get("stopPrice"), "LIVE_ACCOUNT_STOP_PRICE_CURRENCY_REQUIRED", positive=True)
            stop_price = stop_rub / rub_per_point
            _require((bid-stop_price if quantity > 0 else stop_price-ask) > 0,
                     "LIVE_ACCOUNT_STOP_ALREADY_TRIGGERED:" + other_uid)
            notional = abs(quantity) * price * rub_per_point
            asset = "TBANK:" + other_uid
            fraction = notional / equity
            risks.append(RISK.PositionRisk(asset, "LONG" if quantity > 0 else "SHORT", float(fraction), float(price), float(stop_price)))
            spread = float((ask-bid)/price * 10000)
            costs[asset] = float(fraction) * VX.round_trip_cost_pct(spread)
            gross += notional
            used_stops.add(index)
            other_snapshots.append({**position, "notional_rub": str(notional), "stop_price_points": str(stop_price),
                                    "quote_observed_at": _date(book["orderbookTs"]).isoformat()})
        _require(len(used_stops) == len(stops), "LIVE_ACCOUNT_UNMATCHED_STOP_ORDERS")
        last_positions = self.adapter.get_positions(account)
        _require(isinstance(last_positions, dict) and last_positions.get("accountId", account) == account
                 and _positions_view(last_positions) == view, "LIVE_ACCOUNT_CHANGED_DURING_ADMISSION")
        last_orders = self.adapter.list_orders(account)
        last_stops = self.adapter.list_stop_orders(account, status="ACTIVE")
        _require(isinstance(last_orders, list) and not last_orders and isinstance(last_stops, list)
                 and sorted(_json(P.json_safe(row)) for row in last_stops) == sorted(_json(P.json_safe(row)) for row in stops),
                 "LIVE_ACCOUNT_ORDERS_CHANGED_DURING_ADMISSION")
        snapshot = {"binding": {"account_id": account, "environment": "production"},
                    "equity_rub": str(equity), **view,
                    "working_orders": [], "protective_stops": sorted([P.json_safe(row) for row in stops], key=lambda row: str(row.get("stopOrderId")))}
        return snapshot, risks, costs, gross, other_snapshots

    def __call__(self, *, terms, facts, now):
        result = {"eligible": False, "status": "BLOCK", "blockers": [], "version": VERSION,
                  "scope": "WHOLE_BROKER_ACCOUNT", "evidence_loader_configured": True}
        try:
            started = max(_date(now), _date(self.clock()))
            _require(isinstance(terms, dict) and terms.get("asset") == "CNYRUBF"
                     and terms.get("action") in ("OPEN", "ADD") and terms.get("reduce_only") is False,
                     "LIVE_AUTHORITY_REQUIRES_NEW_RISK_CANDIDATE")
            _require(terms.get("account_id") == self.account_id == facts.account.account_id
                     and terms.get("instrument_uid") == facts.spec.instrument_uid, "LIVE_CANDIDATE_ACCOUNT_OR_INSTRUMENT_MISMATCH")
            facts.spec.validate(started)
            facts.account.validate(started, reducing=False)
            facts.quote.validate(facts.spec, started)
            _require(facts.account.reconciled is True and facts.account.costs_reconciled is True,
                     "LIVE_BROKER_RECONCILIATION_REQUIRED")
            _require(facts.quote.limit_orders_available is True and facts.spec.api_trade_available is True,
                     "LIVE_DIRECT_QUOTE_VALIDATION_REQUIRED")
            _require(terms.get("spec_hash") == P.fingerprint(facts.spec.identity()), "LIVE_CANDIDATE_SPEC_MISMATCH")
            _require(_integer(terms.get("position_before_lots"), "LIVE_POSITION_BASELINE_REQUIRED") == facts.account.signed_lots
                     and _integer(terms.get("managed_before_lots"), "LIVE_POSITION_BASELINE_REQUIRED") == facts.account.managed_signed_lots,
                     "LIVE_CANDIDATE_POSITION_MISMATCH")
            binding = model_binding(terms)
            # The protected prepare flow may expose this immutable review input
            # before any proposal/owner request exists. It is never a permission.
            requested_scope = evidence_scope(terms)
            result["evidence_request"] = {"schema": SIGNED_EVIDENCE_SCHEMA, "terms": deepcopy(terms),
                "scope": requested_scope, "scope_hash": _hash(requested_scope), "evaluated_at": started.isoformat(),
                "trade_permission": False}
            _require(self.store.status()["configured"], "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED")
            snapshot, current, costs, other_gross, other_snapshots = self._snapshot(terms, facts, started)
            completed = max(started, _date(self.clock()))
            _require(completed-started <= MAX_ACCOUNT_AGE, "LIVE_ACCOUNT_SNAPSHOT_STALE")
            facts.quote.validate(facts.spec, completed)
            for position in other_snapshots:
                P.fresh(position["quote_observed_at"], completed, 15, "LIVE_ACCOUNT_QUOTE_STALE")
            result["observed_at"] = started.isoformat()
            result["account_equity_rub"] = snapshot["equity_rub"]
            result["account_snapshot_sha256"] = _hash(snapshot)
            result["evidence_request"]["account_snapshot_sha256"] = result["account_snapshot_sha256"]
            result["broker_positions"] = len(snapshot["positions"])
            result["broker_working_orders"] = 0
            # A real, durable broker observation is a prerequisite and an intake
            # source for the independent unit-NAV reconciler.
            try:
                self.store.record_snapshot(snapshot, started)
            except AdmissionBlocked:
                raise
            except Exception:
                raise AdmissionBlocked("LIVE_EVIDENCE_STORE_UNAVAILABLE") from None
            documents = {}
            for kind, scope in (("MODEL", binding), ("ACCOUNT_HISTORY", snapshot["binding"])):
                try:
                    documents[kind] = self.store.load(kind, scope, completed, terms=terms)
                except AdmissionBlocked as error:
                    result["blockers"].append(error.code)
                except Exception:
                    result["blockers"].append("LIVE_EVIDENCE_STORE_UNAVAILABLE")
            if result["blockers"]:
                return self._finish(result)
            evidence, probability, promotion = _model_evidence(documents["MODEL"])
            history = _account_history(documents["ACCOUNT_HISTORY"], snapshot, completed)
            result["promotion_blockers"] = promotion["blockers"]
            result["blockers"].extend(promotion["blockers"])
            direction = terms.get("direction")
            side = terms.get("side")
            _require(direction in ("LONG", "SHORT") and side == ("BUY" if direction == "LONG" else "SELL"),
                     "LIVE_CANDIDATE_DIRECTION_INVALID")
            signed = facts.account.signed_lots
            _require(not signed or (signed > 0) == (direction == "LONG"), "LIVE_ADD_CANNOT_REVERSE_POSITION")
            _require(terms["action"] == ("ADD" if signed else "OPEN"), "LIVE_POSITION_ACTION_MISMATCH")
            lots = _integer(terms.get("lots"), "LIVE_CANDIDATE_LOTS_INVALID", minimum=1)
            price = _decimal(terms.get("limit_price"), "LIVE_CANDIDATE_PRICE_INVALID", positive=True)
            stop = _decimal(terms.get("stop_price"), "LIVE_CANDIDATE_STOP_INVALID", positive=True)
            target = _decimal(terms.get("target_price"), "LIVE_CANDIDATE_TARGET_INVALID", positive=True)
            _require((price-stop if direction == "LONG" else stop-price) > 0
                     and (target-price if direction == "LONG" else price-target) > 0, "LIVE_CANDIDATE_GEOMETRY_INVALID")
            _require(price % facts.spec.tick_size == 0 and stop % facts.spec.tick_size == 0
                     and target % facts.spec.tick_size == 0, "LIVE_APPROVED_PRICE_OFF_TICK")
            _require(facts.quote.ask <= price if side == "BUY" else facts.quote.bid >= price,
                     "LIVE_PRICE_OUTSIDE_APPROVED_LIMIT")
            if signed:
                held = facts.held_terms
                _require(isinstance(held, dict) and _decimal(held.get("stop_price"), "LIVE_HELD_STOP_REQUIRED", positive=True) == stop
                         and held.get("source_identity") == terms["source_identity"] and held.get("horizon") == terms["horizon"],
                         "LIVE_HELD_RISK_BASELINE_MISMATCH")
                opened_at = _date(held.get("opened_at"), "LIVE_HELD_OPEN_TIME_REQUIRED")
                _require(opened_at <= completed, "LIVE_HELD_OPEN_TIME_INVALID")
            equity = _decimal(snapshot["equity_rub"], "LIVE_ACCOUNT_EQUITY_REQUIRED", positive=True)
            exposure = (abs(signed)+lots) * price * facts.spec.rub_per_price_unit_per_lot
            fraction = exposure / equity
            _require(terms.get("economics_mode") == "LIVE" and
                     terms.get("target_execution_policy") == "SINGLE_TARGET_SEPARATE_CONFIRMATION",
                     "EXPLICIT_LIVE_ECONOMICS_APPROVAL_REQUIRED")
            context = P._broker_context(P.approved_entry_context(terms), facts.spec, facts.quote, direction, completed)
            _require(context.get("timeframe") == terms["horizon"], "BROKER_SIGNAL_TIMEFRAME_MISMATCH")
            # Native proof is checked against both actual execution and the
            # owner's cap. Its paper ladder is not an approved broker schedule.
            P._structural(context, facts.quote.ask if side == "BUY" else facts.quote.bid, direction, completed)
            P._structural(context, price, direction, completed)
            plan = P.live_economics_plan(direction, price, stop, target, facts.quote, terms["horizon"],
                fraction=fraction, expected_hold_seconds=terms.get("expected_hold_seconds"))
            if signed:
                # ADD retains the actual first-fill age; it cannot restart the
                # canonical free-funding interval for an existing position.
                plan["position_age_seconds"] = (completed-opened_at).total_seconds()
            source_gate = VX.production_source_gate("CNYRUBF", {"source_gate_pass": True,
                "market_open": facts.quote.limit_orders_available, "production_direct_feed": True,
                "instrument_uid": facts.spec.instrument_uid, "quote_observed_at": facts.quote.observed_at.isoformat()})
            registry = VI.InstrumentRegistry()
            registry.put(VI.InstrumentSpec("CNYRUBF", "MOEX", facts.spec.instrument_uid, "FUTURES", "RUB", "RUB",
                float(facts.spec.tick_size), facts.spec.lot_size,
                float(facts.spec.tick_value_rub/facts.spec.tick_size), facts.spec.lot_size,
                "TBANK_EXACT_INSTRUMENT", facts.spec.observed_at.isoformat()))
            correlations = _correlations(documents["MODEL"], sorted(["CNYRUBF"]+[item.asset for item in current]))
            candidate = LIVE.LiveCandidate("CNYRUBF", direction, float(fraction), float(price), float(stop),
                float((exposure+other_gross)/equity), history["drawdown"], probability, terms["model_version"],
                plan, source_gate, history["daily_pnl_pct"], history["weekly_pnl_pct"])
            risk_profile = CTC.currency_live_risk_policy(history["drawdown"])
            entry_mode = str(terms.get("currency_entry_mode") or "")
            probability_bypass = terms.get("currency_probability_bypass") is True
            probability_required = terms.get("currency_probability_required") is True
            if entry_mode == "GAME_CHANGER":
                _require(probability_bypass and not probability_required,
                         "CURRENCY_GAME_CHANGER_BYPASS_PROOF_REQUIRED")
                probability_policy = {
                    "required": False,
                    "expectancy_required": False,
                    "minimum_probability": float(terms.get("currency_probability_threshold") or
                                                 CTC.PORTFOLIO_POLICIES["Currency"]["threshold"]),
                    "mode": "GAME_CHANGER_STRUCTURAL_BYPASS",
                }
            else:
                # Canonical Currency admission owns the trading thesis. A signed
                # NORMAL structural entry may intentionally mark calibration as
                # sizing-only. LIVE independently rechecks source, economics,
                # promotion, broker/account state and risk, but must not restore
                # a probability veto that canonical admission explicitly removed.
                _require(entry_mode == "NORMAL",
                         "CURRENCY_NORMAL_ROLE_PROOF_REQUIRED")
                structural_bypass = probability_bypass and not probability_required
                legacy_calibrated = probability_required and not probability_bypass
                _require(structural_bypass or legacy_calibrated,
                         "CURRENCY_NORMAL_ROLE_PROOF_REQUIRED")
                probability_policy = {
                    "required": False if structural_bypass else True,
                    "expectancy_required": False if structural_bypass else True,
                    "minimum_probability": float(terms.get("currency_probability_threshold") or
                                                 CTC.PORTFOLIO_POLICIES["Currency"]["threshold"]),
                    "mode": "NORMAL_STRUCTURAL_AUTHORITY" if structural_bypass else "NORMAL_CALIBRATED_LEGACY",
                }
            gate = LIVE.authorize_candidate(candidate, current, correlations, registry, evidence,
                durable_storage=True, broker_reconciled=True, kill_switch=history["kill_switch"],
                risk_profile=risk_profile, probability_policy=probability_policy)
            result["blockers"].extend(gate["blockers"])
            result["live_authorization"] = gate
            result["account_risk"] = {"fraction_nav_after": float(fraction), "gross_after": candidate.gross_after,
                                      "portfolio_risk_profile": risk_profile,
                                      "currency_entry_mode": entry_mode, **history}
            # Preserve the original policy and additionally include costs in the
            # same caps.  Real protective stop prices above remain unmodified.
            econ = gate["order_gate"]["economics"]
            costs["CNYRUBF"] = float(fraction) * float(econ["modeled_round_trip_cost_pct"])
            net_risks = {asset: risk+costs.get(asset, 0.0) for asset, risk in gate["risk"]["by_asset"].items()}
            net_total = sum(net_risks.values())
            net_correlated = max((sum(net_risks[asset] for asset in group["assets"])
                                  for group in gate["risk"]["correlation_components"]), default=0.0)
            result["account_risk"].update(net_stop_risk_nav=net_risks["CNYRUBF"],
                net_total_open_stop_risk_nav=net_total, net_correlated_stop_risk_nav=net_correlated)
            for measured, key, blocker in ((net_risks["CNYRUBF"], "max_stop_risk_nav", "STOP_RISK_LIMIT"),
                    (net_total, "max_total_open_stop_risk_nav", "TOTAL_OPEN_STOP_RISK_LIMIT"),
                    (net_correlated, "max_correlated_stop_risk_nav", "CORRELATED_STOP_RISK_LIMIT")):
                if measured > float(risk_profile[key]):
                    result["blockers"].append(blocker)
            # Existing non-Currency positions remain part of whole-account stop
            # and correlation risk. Exposure limits for this dedicated Currency
            # authorization are governed by the Currency portfolio profile.
            result["valid_until"] = min(min(_date(documents[k]["valid_until"]),
                _date(documents[k]["issuer_valid_until"])) for k in documents).isoformat()
            result["valid_until"] = min(_date(result["valid_until"]),
                _date(facts.quote.observed_at)+timedelta(seconds=15), started+MAX_ACCOUNT_AGE).isoformat()
            for position in other_snapshots:
                result["valid_until"] = min(_date(result["valid_until"]),
                    _date(position["quote_observed_at"])+timedelta(seconds=15)).isoformat()
        except (AdmissionBlocked, P.TradePlanBlocked) as error:
            result["blockers"].append(getattr(error, "code", str(error)))
        except Exception:
            # Do not expose credentials, broker payloads, or DB connection strings.
            result["blockers"].append("LIVE_AUTHORITY_DEPENDENCY_UNAVAILABLE")
        return self._finish(result)

    def _finish(self, result):
        if not result["blockers"]:
            if result.get("live_authorization", {}).get("eligible") is not True or not result.get("valid_until"):
                result["blockers"].append("LIVE_AUTHORITY_INCOMPLETE")
            elif _date(self.clock()) >= _date(result["valid_until"]):
                result["blockers"].append("LIVE_ADMISSION_RECHECK_REQUIRED")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        result["eligible"] = not result["blockers"]
        result["status"] = "PASS" if result["eligible"] else "BLOCK"
        with self._status_lock:
            self._last = deepcopy(result)
        return result


def create_live_admission(connect, adapter, account_id, clock=None, *, evidence=None):
    """Operational factory: no optional evidence callback and no readiness default."""
    return WholeAccountLiveAdmission(connect, adapter, account_id, clock, evidence=evidence)


class CurrencyLiveAdmission(WholeAccountLiveAdmission):
    """Retained integration constructor, using the same strict whole-account authority."""
    def __init__(self, *, adapter, evidence, account_id, instrument_uid, environment, clock=None):
        _require(environment in {"production", "sandbox"}, "INVALID_EXECUTION_ENVIRONMENT")
        self.instrument_uid = _text(instrument_uid, "LIVE_EXPLICIT_INSTRUMENT_REQUIRED")
        self.environment = environment
        super().__init__(evidence.connect, adapter, account_id, clock, evidence=evidence)

    def __call__(self, *, terms, facts, now):
        if (not isinstance(terms, dict) or terms.get("instrument_uid") != self.instrument_uid
                or terms.get("execution_environment", "production") != self.environment):
            return self._finish({"eligible": False, "status": "BLOCK", "version": VERSION,
                "blockers": ["LIVE_ACCOUNT_SCOPE_MISMATCH"], "scope": "WHOLE_BROKER_ACCOUNT"})
        return super().__call__(terms=terms, facts=facts, now=now)
