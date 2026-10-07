"""Incremental academic discovery in the existing knowledge I/O worker.

One tick performs one provider page, one small screening batch, or one existing
auditor/compiler candidate. No thread is created and no market/history permit
is held across network I/O. Provider failures, empty results and compilation
progress are distinct, and cursors/cooldowns survive process replacement.

Provider contracts: info.arxiv.org/help/api/user-manual.html (term queries,
Atom errors, paging and pacing); help.openalex.org/api/authentication/ and
docs.openalex.org/how-to-use-the-api/get-lists-of-entities/search-entities;
semanticscholar.org/product/api/tutorial (explicit HTTP errors and field limits).
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx
import veritas_learning_state as STORE

VERSION = "INCREMENTAL_KNOWLEDGE_DISCOVERY_V1"
JOB = "knowledge_discovery"
MAX_ITEMS = 8
MAX_PAGES = 3
MAX_QUERY_OFFSET = 1000
MAX_QUERIES = 128
MAX_RESPONSE_BYTES = 1_048_576
MAX_ABSTRACT = 16_000
TICK_SECONDS = 60
PROVIDERS = ("OpenAlex", "arXiv", "SemanticScholar")
STAGES = ("discovery", "screening", "compilation")
_INSTALL_LOCK = threading.Lock()
URLS = {
    "OpenAlex": "https://api.openalex.org/works",
    "arXiv": "https://export.arxiv.org/api/query",
    "SemanticScholar": "https://api.semanticscholar.org/graph/v1/paper/search",
}


def _iso(stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc).isoformat()


def _text(value, limit=MAX_ABSTRACT):
    return " ".join(str(value or "").split())[:limit]


def _integer(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _candidate_id(doi, identifier, title):
    # Preserve the deployed identity, including cross-provider DOI deduplication.
    return "OA_"+hashlib.sha256((doi or identifier or title).strip().lower().encode()).hexdigest()[:24]


def term_query(query, provider, broaden=False):
    """Treat prose as terms, not one accidentally overconstrained exact phrase."""
    terms = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", str(query).lower(), re.UNICODE)
    terms = [t for t in terms if t not in {"the", "a", "an", "and", "or", "of", "in", "for", "with"}]
    terms = list(dict.fromkeys(terms))[:(3 if broaden else 10)]
    if not terms:
        raise ValueError("EMPTY_QUERY")
    if provider == "arXiv":
        return " AND ".join('all:"'+term+'"' for term in terms)
    return " ".join(terms)


class ProviderFailure(Exception):
    def __init__(self, provider, code, *, retry_after=120, http_status=None):
        super().__init__(provider+":"+code)
        self.provider, self.code = provider, code
        self.retry_after, self.http_status = retry_after, http_status


def _retry_after(headers, stamp):
    value = headers.get("Retry-After", "")
    try:
        return max(0., float(value))
    except (TypeError, ValueError):
        try:
            return max(0., parsedate_to_datetime(value).timestamp()-stamp)
        except (TypeError, ValueError, OverflowError):
            return 0.


def _inverted_abstract(index):
    # An untrusted maximum position must never determine an allocation size.
    if not isinstance(index, dict):
        return ""
    positions = {}
    for word, indexes in index.items():
        if not isinstance(indexes, list):
            continue
        for i in indexes[:4096]:
            if type(i) is int and 0 <= i < 4096:
                positions[i] = _text(word, 200)
    return " ".join(positions[i] for i in sorted(positions))[:MAX_ABSTRACT]


def parse_provider(provider, body, query):
    """Normalize bounded metadata; missing abstracts remain metadata-only."""
    records, out = [], []
    if provider == "arXiv":
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ProviderFailure(provider, "INVALID_XML")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise ProviderFailure(provider, "INVALID_XML") from None
        ns = {"a": "http://www.w3.org/2005/Atom", "v": "http://arxiv.org/schemas/atom"}
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ProviderFailure(provider, "INVALID_FEED")
        entries = root.findall("a:entry", ns)
        for entry in entries[:MAX_ITEMS]:
            title = _text(entry.findtext("a:title", "", ns), 1000)
            identifier = _text(entry.findtext("a:id", "", ns), 1000)
            if title.lower() == "error" or "/api/errors" in identifier:
                raise ProviderFailure(provider, "ATOM_ERROR")
            records.append({
                "title": title, "id": identifier,
                "doi": _text(entry.findtext("v:doi", "", ns), 300),
                "abstract": _text(entry.findtext("a:summary", "", ns)),
                "authors": "; ".join(_text(a.findtext("a:name", "", ns), 100)
                                     for a in entry.findall("a:author", ns)[:12]),
                "year": _integer(entry.findtext("a:published", "", ns)[:4], None),
                "venue": "arXiv", "cited_by_count": 0,
                "metadata": {"arxiv_id": identifier, "fallback_provider": provider},
            })
        raw_count = len(entries)
    else:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise ProviderFailure(provider, "INVALID_JSON") from None
        key = "results" if provider == "OpenAlex" else "data"
        if not isinstance(data, dict) or not isinstance(data.get(key), list) or data.get("error"):
            raise ProviderFailure(provider, "INVALID_RESPONSE_SCHEMA")
        raw_count = len(data[key])
        for item in data[key][:MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            if provider == "OpenAlex":
                if item.get("is_retracted"):
                    continue
                loc = item.get("primary_location") or {}
                records.append({
                    "title": _text(item.get("title") or item.get("display_name"), 1000),
                    "id": _text(item.get("id"), 1000), "doi": _text(item.get("doi"), 300),
                    "abstract": _inverted_abstract(item.get("abstract_inverted_index")),
                    "authors": "; ".join(_text((a.get("author") or {}).get("display_name"), 100)
                                         for a in (item.get("authorships") or [])[:12] if isinstance(a, dict)),
                    "year": _integer(item.get("publication_year"), None),
                    "venue": _text((loc.get("source") or {}).get("display_name"), 300),
                    "cited_by_count": _integer(item.get("cited_by_count")),
                    "metadata": {"openalex_id": item.get("id"), "type": item.get("type"),
                                 "topics": [_text(t.get("display_name"), 100)
                                            for t in (item.get("topics") or [])[:8] if isinstance(t, dict)]},
                })
            else:
                ext = item.get("externalIds") or {}
                records.append({
                    "title": _text(item.get("title"), 1000), "id": _text(item.get("paperId"), 1000),
                    "url": _text(item.get("url"), 1000), "doi": _text(ext.get("DOI"), 300),
                    "abstract": _text(item.get("abstract")),
                    "authors": "; ".join(_text(a.get("name"), 100)
                                         for a in (item.get("authors") or [])[:12] if isinstance(a, dict)),
                    "year": _integer(item.get("year"), None), "venue": _text(item.get("venue"), 300),
                    "cited_by_count": _integer(item.get("citationCount")),
                    "metadata": {"semantic_scholar_id": item.get("paperId"), "fallback_provider": provider},
                })
    for record in records:
        if not record["title"]:
            continue
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", record["doi"], flags=re.I)
        identifier = record.pop("id")
        record.update(candidate_id=_candidate_id(doi, identifier, record["title"]), doi=doi, query=query,
                      source_url="https://doi.org/"+doi if doi else record.pop("url", identifier))
        record["metadata"]["discovery_version"] = VERSION
        out.append(record)
    return out, raw_count


@contextmanager
def transaction(connect):
    with connect() as c, c.transaction():
        c.execute("SET LOCAL statement_timeout = '2000ms'")
        c.execute("SET LOCAL lock_timeout = '250ms'")
        yield c


class KnowledgeDiscovery:
    def __init__(self, ns, *, client_factory=None, clock=None):
        self.ns, self.connect = ns, ns["pg_connect"]
        self.client_factory, self.clock = client_factory or httpx.Client, clock or time.time
        queries = list(ns.get("DISCOVERY_QUERIES") or [])+list(ns.get("MULTILINGUAL_DISCOVERY_QUERIES") or [])
        self.queries = list(dict.fromkeys(_text(q, 256) for q in queries if _text(q)))[:MAX_QUERIES]
        self.plan_hash = hashlib.sha256(json.dumps(self.queries, ensure_ascii=False).encode()).hexdigest()[:16]
        self._lock, self._ready, self._next_tick = threading.Lock(), False, 0.
        self._state = {}
        self._public = {"version": VERSION, "status": "BUILDING", "stage": "bootstrap",
                        "candidates_seen": 0, "candidates_new": 0, "rules_imported": 0,
                        "errors": [], "providers": {}, "compiler_batch_limit": 1}

    def status(self):
        return deepcopy(self._public)

    def _publish(self, result):
        self._public = deepcopy(result)
        self.ns.setdefault("knowledge_automation_state", {}).update(deepcopy(result))
        return self.status()

    def _restore(self):
        STORE.ensure_schema(self.connect)
        saved = STORE.job_state(self.connect, JOB, VERSION) or {}
        self._state = deepcopy(saved.get("cursor") or {})
        summary = self._state.get("summary") or saved.get("last_good")
        if isinstance(summary, dict):
            self._publish(dict(summary, restored=True))
        if self._state.get("plan_hash") != self.plan_hash:
            self._state.update(plan_hash=self.plan_hash, provider_index=0)
            for provider in self._state.get("providers", {}).values():
                provider.update(query_index=0, offset=0, broaden=False, pages={}, page_pass=0)
        self._ready = True

    def _request(self, provider, query, offset, limit):
        headers = {"User-Agent": "VERITAS academic metadata discovery/1"}
        if provider == "OpenAlex":
            params = {"search": query, "per-page": limit, "page": offset//limit+1,
                      "filter": "is_retracted:false", "sort": "relevance_score:desc",
                      "select": "id,doi,title,authorships,publication_year,primary_location,cited_by_count,abstract_inverted_index,topics,type,is_retracted"}
            if self.ns.get("OPENALEX_API_KEY"):
                headers["Authorization"] = "Bearer "+self.ns["OPENALEX_API_KEY"]
        elif provider == "arXiv":
            params = {"search_query": query, "start": offset, "max_results": limit,
                      "sortBy": "relevance", "sortOrder": "descending"}
        else:
            params = {"query": query, "offset": offset, "limit": limit,
                      "fields": "title,authors,year,abstract,url,citationCount,externalIds,venue"}
            if self.ns.get("SEMANTIC_SCHOLAR_API_KEY"):
                headers["x-api-key"] = self.ns["SEMANTIC_SCHOLAR_API_KEY"]
        deadline = time.monotonic()+20
        try:
            with self.client_factory(timeout=httpx.Timeout(8, connect=4), headers=headers) as client:
                with client.stream("GET", URLS[provider], params=params) as response:
                    if response.status_code != 200:
                        retry = _retry_after(response.headers, self.clock())
                        if response.status_code == 429:
                            retry = max(retry, self.ns.get("SEMANTIC_SCHOLAR_COOLDOWN_SECONDS", 1800)
                                        if provider == "SemanticScholar" else 600)
                        elif response.status_code in (401, 403):
                            retry = max(retry, 3600)
                        raise ProviderFailure(provider, "HTTP_"+str(response.status_code),
                                              retry_after=max(120, retry), http_status=response.status_code)
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if len(body)+len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProviderFailure(provider, "RESPONSE_TOO_LARGE")
                        if time.monotonic() > deadline:
                            raise ProviderFailure(provider, "REQUEST_TIMEOUT")
                        body.extend(chunk)
                    return parse_provider(provider, bytes(body), query)
        except httpx.TimeoutException:
            raise ProviderFailure(provider, "REQUEST_TIMEOUT") from None
        except httpx.HTTPError:
            raise ProviderFailure(provider, "TRANSPORT_ERROR") from None

    def _provider_state(self, state, name):
        return state.setdefault("providers", {}).setdefault(name, {
            "query_index": 0, "offset": 0, "broaden": False, "next_run_at": 0.,
            "round_started_at": self.clock(), "status": "QUEUED", "attempts": 0, "failures": 0,
            "pages": {}, "page_pass": 0})

    def _advance(self, progress, *, raw_count, limit):
        progress["page_pass"] = progress.get("page_pass", 0)+1
        if raw_count >= limit and progress["offset"]+limit < MAX_QUERY_OFFSET:
            offset = progress["offset"]+limit
            if progress["page_pass"] < MAX_PAGES:
                progress["offset"] = offset
                return
            # Continue beyond the first highly cited/relevant page next sweep.
            # This small scalar map is bounded by MAX_QUERIES, never by papers.
            progress.setdefault("pages", {})[str(progress["query_index"])] = [offset, bool(progress.get("broaden"))]
        else:
            progress.setdefault("pages", {}).pop(str(progress["query_index"]), None)
        if not raw_count and not progress.get("broaden") and not progress["offset"]:
            progress["broaden"] = True
            return
        progress.update(query_index=progress["query_index"]+1, offset=0, broaden=False, page_pass=0)
        if progress["query_index"] >= len(self.queries):
            minimum = max(3600, int(self.ns.get("KNOWLEDGE_DISCOVERY_INTERVAL", 21600)))
            # arXiv asks production clients to cache an identical query for a day.
            if progress.get("provider") == "arXiv":
                minimum = max(86400, minimum)
            due = max(self.clock(), progress["round_started_at"]+minimum)
            progress.update(query_index=0, round_started_at=due, next_run_at=due)
        saved = progress.get("pages", {}).get(str(progress["query_index"]))
        if saved:
            progress.update(offset=int(saved[0]), broaden=bool(saved[1]))

    def _store(self, items):
        new = 0
        with transaction(self.connect) as c:
            for x in items[:MAX_ITEMS]:
                row = c.execute("""INSERT INTO knowledge_candidates
                    (candidate_id,discovered_at,query,title,authors,year,doi,source_url,venue,
                     cited_by_count,abstract,metadata,status)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(candidate_id) DO NOTHING RETURNING candidate_id""",
                    (x["candidate_id"], _iso(self.clock()), x["query"], x["title"], x["authors"], x["year"],
                     x["doi"], x["source_url"], x["venue"], x["cited_by_count"], x["abstract"],
                     json.dumps(x["metadata"], ensure_ascii=False),
                     "ready_for_compilation" if x["abstract"] else "metadata_only")).fetchone()
                new += int(row is not None)
                if row is None:
                    # Enrich a metadata-only record without reopening any audited or compiled record.
                    c.execute("""UPDATE knowledge_candidates SET cited_by_count=GREATEST(cited_by_count,%s),
                        abstract=CASE WHEN length(COALESCE(abstract,''))<length(%s) THEN %s ELSE abstract END,
                        status=CASE WHEN status='metadata_only' AND length(%s)>0
                                    THEN 'ready_for_compilation' ELSE status END
                        WHERE candidate_id=%s""",
                              (x["cited_by_count"], x["abstract"], x["abstract"], x["abstract"], x["candidate_id"]))
        return new

    def _discover(self, state):
        if not self.queries:
            return {"status": "ERROR", "reason": "NO_DISCOVERY_QUERIES"}
        stamp = self.clock()
        chosen = None
        for step in range(len(PROVIDERS)):
            index = (int(state.get("provider_index", 0))+step) % len(PROVIDERS)
            provider = PROVIDERS[index]
            progress = self._provider_state(state, provider)
            if provider == "SemanticScholar" and not self.ns.get("KNOWLEDGE_SEMANTIC_FALLBACK", True):
                progress.update(status="DISABLED", reason="PROVIDER_DISABLED")
                continue
            if progress.get("next_run_at", 0) <= stamp:
                chosen = (provider, progress)
                state["provider_index"] = (index+1) % len(PROVIDERS)
                break
        if chosen is None:
            return {"status": "DEFERRED_PROVIDER", "reason": "PROVIDER_COOLDOWN_OR_SWEEP_NOT_DUE"}
        provider, progress = chosen
        progress.update(provider=provider, attempts=progress.get("attempts", 0)+1, last_attempt_at=_iso(stamp))
        original = self.queries[progress["query_index"] % len(self.queries)]
        query = term_query(original, provider, progress.get("broaden", False))
        limit = max(1, min(MAX_ITEMS, int(self.ns.get("KNOWLEDGE_DISCOVERY_LIMIT", MAX_ITEMS))))
        try:
            items, raw_count = self._request(provider, query, progress["offset"], limit)
        except ProviderFailure as error:
            failures = progress.get("failures", 0)+1
            delay = max(error.retry_after, min(3600, 60*2**min(failures, 6)))
            progress.update(status="DEFERRED_PROVIDER" if error.http_status == 429 else "ERROR",
                            reason=error.code, next_run_at=stamp+delay, failures=failures)
            return {"status": progress["status"], "reason": error.code, "provider": provider,
                    "retry_at": _iso(progress["next_run_at"]), "raw_count": None, "received": 0}
        for item in items:
            item["query"] = original
            item["metadata"]["provider_query"] = query
        new = self._store(items) if items else 0
        progress.update(status="OK" if items else "EMPTY", reason=None if items else "NO_USABLE_RESULTS",
                        raw_count=raw_count, received=len(items), failures=0, next_run_at=stamp+3)
        if items:
            progress["last_success_at"] = _iso(stamp)
        self._advance(progress, raw_count=raw_count, limit=limit)
        return {"status": "OK" if items else "EMPTY", "reason": None if items else "NO_USABLE_RESULTS",
                "provider": provider, "raw_count": raw_count, "received": len(items), "new": new}

    def _screen(self):
        # Preserve the deployed relevance gate while bounding each SQL/result batch.
        with transaction(self.connect) as c:
            rows = c.execute("""SELECT candidate_id,query,left(title,1000) title,left(abstract,16000) abstract,
                cited_by_count,jsonb_build_object('topics',metadata->'topics') metadata
                FROM knowledge_candidates WHERE status='ready_for_compilation'
                ORDER BY discovered_at ASC,candidate_id ASC LIMIT 8 FOR UPDATE SKIP LOCKED""").fetchall()
            keep = reject = 0
            for row in rows:
                x = dict(row)
                relevance = self.ns["candidate_relevance"](x)
                accepted = len(x.get("abstract") or "") >= 250 and relevance >= float(self.ns.get("KNOWLEDGE_MIN_RELEVANCE", 2.5))
                status = "screened_in" if accepted else "screened_out"
                c.execute("""UPDATE knowledge_candidates SET status=%s,
                    metadata=metadata || %s::jsonb,
                    processed_at=CASE WHEN %s='screened_out' THEN %s ELSE processed_at END
                    WHERE candidate_id=%s AND status='ready_for_compilation'""",
                    (status, json.dumps({"relevance_score": relevance, "screen_version": VERSION}),
                     status, _iso(self.clock()), x["candidate_id"]))
                keep += int(accepted)
                reject += int(not accepted)
        return {"status": "OK" if rows else "EMPTY", "reason": None if rows else "NO_PENDING_SCREENING",
                "screened_in": keep, "screened_out": reject}

    def _recover_compiler_candidates(self, ids):
        if not ids:
            return
        with transaction(self.connect) as c:
            c.execute("""UPDATE knowledge_candidates SET status='screened_in',error=NULL,processed_at=NULL,
                metadata=metadata || %s::jsonb WHERE candidate_id=ANY(%s) AND status='quarantined'
                AND COALESCE(metadata->>'last_compile_error','') ~* '(429|50[0234]|timeout|timed out|connecterror|connectionerror)'""",
                (json.dumps({"compile_attempts": 0, "transient_recovery_version": VERSION}), ids[:1]))

    def _compile(self, state, lease):
        if not (self.ns.get("KNOWLEDGE_LLM_ENABLED") and self.ns.get("OPENAI_API_KEY")):
            return {"status": "DEFERRED_COMPILER", "reason": "COMPILER_NOT_CONFIGURED"}
        compiler = state.setdefault("compiler", {"failures": 0, "next_run_at": 0.})
        interval = _integer(self.ns.get("KNOWLEDGE_DISCOVERY_INTERVAL", 21600))
        limit = _integer(self.ns.get("KNOWLEDGE_COMPILE_LIMIT", 24))
        compiler.update(budget_interval_seconds=interval, budget_attempt_limit=limit)
        if not interval or not limit:
            return {"status": "DEFERRED_COMPILER", "reason": "COMPILER_BUDGET_DISABLED"}
        spacing = interval/limit
        stamp = self.clock()
        # Persist the old reservation across restarts/configuration changes.
        # A stricter new budget also applies to the most recent paid attempt.
        budget_due = max(float(compiler.get("budget_next_at", 0)),
                         float(compiler.get("last_attempt_epoch", 0))+spacing
                         if compiler.get("attempts", 0) else 0.)
        due = max(budget_due, float(compiler.get("next_run_at", 0)))
        compiler.update(attempt_spacing_seconds=spacing, budget_next_at=budget_due,
                        next_run_at=due)
        if stamp < due:
            reason = "COMPILER_COOLDOWN" if compiler.get("status") == "ERROR" else "COMPILER_BUDGET_SPACING"
            return {"status": "DEFERRED_COMPILER", "reason": reason,
                    "retry_at": _iso(due), "attempt_spacing_seconds": spacing}
        compiler.update(attempts=_integer(compiler.get("attempts"))+1,
                        last_attempt_at=_iso(stamp), last_attempt_epoch=stamp,
                        budget_next_at=stamp+spacing, next_run_at=stamp+spacing)
        # The attempt consumes quota BEFORE any compiler/recovery I/O. Keep
        # the existing fenced lease; a crash or failed final checkpoint cannot
        # refund the reservation or let another process repeat the paid call.
        if not STORE.checkpoint_job(self.connect, lease, status="RUNNING", cursor=state,
                                    result={"status": "RUNNING", "reason": "COMPILER_ATTEMPT_RESERVED",
                                            "next_run_at": compiler["next_run_at"]}, release=False):
            raise RuntimeError("COMPILER_BUDGET_RESERVATION_FAILED")
        try:
            self._recover_compiler_candidates(compiler.get("retry_ids") or [])
            imported, errors, audited = self.ns["compile_pending_candidates"](limit=1)
        except Exception as error:
            # Do not expose message text, which can contain credentials/body.
            imported, errors, audited = 0, [type(error).__name__], {}
        # Never expose provider response bodies/keys echoed in a legacy exception.
        code = None
        if errors:
            joined = " ".join(str(e) for e in errors).lower()
            code = ("COMPILER_RATE_LIMIT" if "429" in joined else "COMPILER_TIMEOUT"
                    if "timeout" in joined or "timed out" in joined else "COMPILER_TRANSPORT"
                    if re.search(r"\b50[0234]\b|connecterror|connectionerror", joined) else "COMPILER_ERROR")
            failures = compiler.get("failures", 0)+1
            retry_ids = [m.group(1) for error in errors[:1]
                         if (m := re.match(r"(OA_[0-9a-f]{24}):", str(error))) and code != "COMPILER_ERROR"]
            compiler.update(status="ERROR", reason=code, failures=failures, retry_ids=retry_ids,
                            next_run_at=max(compiler["budget_next_at"],
                                            self.clock()+min(3600, 300*2**min(failures-1, 4))))
        completed = int(audited.get("audited", 0))+int(audited.get("compiled", 0))
        if not errors and completed:
            compiler.update(status="OK", reason=None, failures=0, retry_ids=[],
                            next_run_at=compiler["budget_next_at"],
                            last_success_at=_iso(self.clock()))
        return {"status": "ERROR" if errors else "OK" if completed else "EMPTY",
                "reason": code or (None if completed else "NO_PENDING_COMPILATION"),
                "imported": int(imported), "audited": int(audited.get("audited", 0)),
                "rejected": int(audited.get("rejected", 0)), "compiled": int(audited.get("compiled", 0))}

    def _summary(self, state, result, stage):
        previous = dict(state.get("summary") or self._public)
        for total, delta in (("candidates_seen", "received"), ("candidates_new", "new"),
                             ("rules_imported", "imported"), ("screened_in", "screened_in"),
                             ("screened_out", "screened_out"), ("audited", "audited"), ("compiled", "compiled")):
            previous[total] = _integer(previous.get(total))+_integer(result.get(delta))
        providers = {name: {k: v for k, v in p.items() if k in
                     ("status", "reason", "attempts", "last_attempt_at", "last_success_at", "received", "raw_count", "next_run_at")}
                     for name, p in state.get("providers", {}).items()}
        errors = [{"provider": name, "code": p.get("reason"), "retry_at": _iso(p["next_run_at"])}
                  for name, p in providers.items() if p.get("status") in ("ERROR", "DEFERRED_PROVIDER")]
        compiler = {k: v for k, v in state.get("compiler", {}).items() if k != "retry_ids"}
        if compiler.get("status") == "ERROR":
            errors.append({"provider": "Compiler", "code": compiler["reason"],
                           "retry_at": _iso(compiler["next_run_at"])})
        previous.update(version=VERSION, status=result["status"], reason=result.get("reason"), stage=stage,
                        last_run=_iso(self.clock()), providers=providers, compiler=compiler, errors=errors,
                        last_batch=result, query_count=len(self.queries), compiler_batch_limit=1,
                        interval_seconds=TICK_SECONDS, restored=False)
        if result["status"] == "OK":
            previous["last_success_at"] = _iso(self.clock())
        state["summary"] = previous
        return previous

    def tick(self, reason="scheduled"):
        if not self.ns.get("KNOWLEDGE_AUTOMATION", True):
            return self._publish(dict(self._public, status="DISABLED", reason="AUTOMATION_DISABLED"))
        if self.clock() < self._next_tick:
            return dict(self.status(), tick_status="NOT_DUE")
        if not self._lock.acquire(blocking=False):
            return dict(self.status(), tick_status="DEFERRED_BUSY")
        lease = None
        try:
            if not self._ready:
                self._restore()
            lease = STORE.claim_job(self.connect, JOB, VERSION, lease_seconds=240)
            if not lease:
                return dict(self.status(), tick_status="DEFERRED_LEASE")
            state = deepcopy(lease.get("cursor") or self._state)
            if state.get("plan_hash") != self.plan_hash:
                state.update(plan_hash=self.plan_hash, provider_index=0)
                for progress in state.get("providers", {}).values():
                    progress.update(query_index=0, offset=0, broaden=False, pages={}, page_pass=0)
            stage_index = int(state.get("stage_index", 0)) % len(STAGES)
            stage = STAGES[stage_index]
            state["stage_index"] = (stage_index+1) % len(STAGES)
            # Advance the fair stage pointer on failure too; a broken provider
            # cannot keep an already stored source from its audit/compiler.
            try:
                result = self._discover(state) if stage == "discovery" else self._screen() if stage == "screening" else self._compile(state, lease)
            except Exception as error:
                result = {"status": "ERROR", "reason": type(error).__name__.upper()}
            summary = self._summary(state, result, stage)
            status = result["status"]
            if not STORE.checkpoint_job(self.connect, lease, status=status, cursor=state, result=summary,
                                        retry_after_seconds=TICK_SECONDS):
                raise RuntimeError("KNOWLEDGE_LEASE_EXPIRED")
            self._state = state
            self._next_tick = self.clock()+TICK_SECONDS
            self._publish(summary)
            emit = self.ns.get("emit")
            if callable(emit):
                emit("knowledge_discovery_complete", version=VERSION, status=summary["status"], stage=stage,
                     candidates_seen=summary["candidates_seen"], candidates_new=summary["candidates_new"],
                     rules_imported=summary["rules_imported"], last_batch=result, provider_errors=summary["errors"],
                     providers_tried=[name for name, p in summary["providers"].items() if p.get("attempts")])
            return self.status()
        except Exception as error:
            self._next_tick = self.clock()+TICK_SECONDS
            result = dict(self._public, status="ERROR", reason=type(error).__name__.upper(),
                          last_run=_iso(self.clock()), tick_status="DURABLE_STATE_UNAVAILABLE")
            return self._publish(result)
        finally:
            self._lock.release()


def install(ns):
    with _INSTALL_LOCK:
        if "_knowledge_discovery" not in ns:
            ns["_knowledge_discovery"] = KnowledgeDiscovery(ns)
    return ns["_knowledge_discovery"]
