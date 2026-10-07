"""Bounded quote-driven structural decisions for the existing PAPER engine.

The market loop publishes already acquired native history once. This lane
observes fresh, source-pinned quotes without running features, MA discovery,
research, history downloads or the full portfolio cycle. A supplied callback
owns canonical admission and atomic paper-book accounting; no broker API is
called here. Five seconds is a polling target, not a promise of market ticks.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import math
import threading
import time

import veritas_canonical_constitution as CTC
import veritas_execution as VX
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_timeframe_data as TFD
import veritas_timeframe_policy as TFP
import veritas_timeframe_structure as TS


VERSION = "STRUCTURAL_QUOTE_RUNTIME_V1"
INTERVAL_SECONDS = 5.0
BOOK_RETRY_SECONDS = 0.5
HORIZONS = tuple(TS.TIMEFRAMES)
MAX_BARS_PER_TIMEFRAME = 500
_cache_lock = threading.RLock()
_markets = {}
_latest_rows = {}
_runtime = None
_start_lock = threading.Lock()


def _clock(value=None):
    stamp = TS.timestamp(datetime.now(timezone.utc) if value is None else value)
    return datetime.fromtimestamp(stamp, timezone.utc) if stamp is not None else None


def _source_key(identity):
    identity = identity or {}
    return identity.get("key"), identity.get("contract_id")


def _same_source(expected, actual):
    return bool(VPS.same(expected, actual) and VPS.same(actual, expected))


def _clean_quote(raw):
    q = VPS.quote_from_row(raw or {})
    fields = (*VPS.QUOTE_FIELDS, "asset", "observed_at", "paper_eligible",
              "production_eligible", "orders_enabled", "book_observed_at")
    return {key: deepcopy(q[key]) for key in fields if key in q}


def publish_market(raw):
    """Capture bounded, already source-labelled native bars; never fetch data."""
    raw = raw or {}
    asset = raw.get("asset")
    identity = raw.get("structure_source_identity")
    if not asset or not identity or not _same_source(identity, VPS.identity(asset, raw)):
        return False
    if asset == "CNYRUBF":
        import veritas_direct_cny as CNY
        if CNY.enabled() and not str(identity.get("key", "")).startswith("TBANK_GRPC:"):
            # Research fallback remains available to analytics, but cannot evict
            # the selected broker context and strand the direct recovery lane.
            return False
    mapping = {}
    for tf, values in (raw.get("structure_bars_by_timeframe") or {}).items():
        if tf not in TS.TIMEFRAMES:
            continue
        rows = []
        for bar in values[-MAX_BARS_PER_TIMEFRAME:]:
            if not isinstance(bar, dict):
                continue
            if bar.get("source_identity") and not _same_source(identity, bar["source_identity"]):
                return False
            rows.append(deepcopy(dict(bar, timeframe=tf, source_identity=identity)))
        mapping[tf] = rows
    if not any(mapping.values()):
        return False
    value = {"asset": asset, "structure_source_identity": deepcopy(identity),
             "structure_bars_by_timeframe": mapping, "quote": _clean_quote(raw),
             "captured_at": datetime.now(timezone.utc).isoformat()}
    with _cache_lock:
        old = _markets.get(asset)
        # A slow asset fetch must not replace a newer complete bar with an old
        # one. Source/contract changes intentionally start a separate context.
        if old and _same_source(identity, old["structure_source_identity"]):
            for tf, previous in old["structure_bars_by_timeframe"].items():
                current = mapping.get(tf) or []
                last = lambda rows: max((TS.timestamp(b.get("ts", b.get("time"))) or 0
                                         for b in rows), default=0)
                if previous and last(previous) > last(current):
                    mapping[tf] = deepcopy(previous)
        _markets[asset] = value
    return True


def publish_summary(summary):
    """Merge newer fast observations into a finishing slow scan atomically.

    Callers use the returned list. This prevents a scan begun before a fast
    trigger from immediately overwriting that trigger's actual execution audit.
    """
    with _cache_lock:
        fast = {key: deepcopy(row) for key, row in _latest_rows.items()}
    out = []
    for original in summary or []:
        row = dict(original)
        newer = fast.pop((row.get("asset"), row.get("horizon")), None)
        if newer:
            old_at = TS.timestamp(row.get("market_observed_at") or row.get("observed_at"))
            new_at = TS.timestamp(newer.get("market_observed_at") or newer.get("observed_at"))
            identity = VPS.identity(row.get("asset"), row)
            if (new_at is not None and (old_at is None or new_at > old_at)
                    and _same_source(identity, VPS.identity(newer.get("asset"), newer))):
                row = newer
        out.append(row)
    out.extend(fast.values())
    return out


def _namespace_summary(ns):
    with ns.get("lock", nullcontext()):
        return [dict(row) for row in (ns.get("last_cycle", {}).get("summary") or [])]


def _report_scalars(raw, fields):
    """Copy a bounded display projection, never an archived execution graph."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in fields:
        if key not in raw:
            continue
        value = raw[key]
        if value is None or isinstance(value, (bool, int)):
            out[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:400]
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _row_event_key(row):
    return (row.get("asset"), row.get("horizon"),
            (TFP.context_of(row).get("event") or {}).get("event_id"))


def _execution_report_index(rows, execution):
    """Match actual selected outcomes; other horizons did not receive that fill.

    The canonical portfolio report owns complete admission and book evidence.
    Duplicating its whole graph into each of up to 49 signals multiplies both
    retained memory and every serialized dashboard snapshot. Only scalar facts
    for this exact asset, horizon and immutable event belong on a signal row.
    """
    wanted = {_row_event_key(row) for row in rows}
    indexed = {}
    for portfolio in execution.get("portfolios") or []:
        if not isinstance(portfolio, dict) or portfolio.get("name") not in CTC.PORTFOLIO_ORDER:
            continue
        for trace in portfolio.get("admission_trace") or []:
            if not isinstance(trace, dict):
                continue
            key = (trace.get("asset"), trace.get("horizon"), trace.get("event_id"))
            if key not in wanted or not key[2]:
                continue
            item = _report_scalars(trace, ("asset", "horizon", "event_id", "reason",
                                           "checked_at", "target_fraction"))
            item.update(_report_scalars(trace.get("execution"), (
                "status", "reason", "checked_at", "execution_action", "fill_price",
                "order_id", "current_fraction", "requested_fraction")))
            item["name"] = portfolio["name"]
            indexed.setdefault(key, {})[portfolio["name"]] = item
    return indexed


def _default_quote_fetch(ns, asset, identity):
    """Use only an exact-source quote adapter; history fallbacks are excluded."""
    key = str(identity.get("key") or "")
    if key.startswith("TBANK_GRPC:") and asset == "CNYRUBF":
        import veritas_direct_cny as CNY
        return CNY.quote(connection=(getattr(ns.get("VTB"), "connection", None)))
    if key.startswith(("PROFINANCE:", "BINANCE:", "MOEX:")):
        position = {"asset": asset, "payload": {"price_source_lock": identity}}
        return VPG.fetch_guard_quote(ns, asset, [position], identity.get("contract_id"))
    # Unsupported providers may still publish a source-pinned quote through
    # the ordinary loop; this lane never downloads Yahoo/STOOQ history for it.
    return VPG.quote_for_position({"asset": asset, "payload": {"price_source_lock": identity}})


class BreakoutRuntime:
    def __init__(self, ns, entry_pass, *, quote_fetcher=None, context_builder=None):
        if not callable(entry_pass):
            raise ValueError("a canonical paper entry callback is required")
        self.ns, self.entry_pass = ns, entry_pass
        self.quote_fetcher = quote_fetcher or _default_quote_fetch
        self.context_builder = context_builder or TFD.structural_context
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="veritas-breakout-quote")
        self._pending, self._observations = {}, {}
        self._pending_entry_rows = []
        self._history_versions = {}
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self.state = {"status": "READY", "version": VERSION, "paper_only": True,
                      "interval_seconds": INTERVAL_SECONDS, "cycles": 0,
                      "fresh_quotes": 0, "duplicate_quotes": 0,
                      "source_rejections": 0, "stale_quotes": 0}

    def _recover_direct_cny(self, clock):
        """Bootstrap the selected source from verified reader memory, not a scan."""
        import veritas_direct_cny as CNY
        connection = getattr(self.ns.get("VTB"), "connection", None)
        if connection is None or not CNY.enabled():
            return
        with _cache_lock:
            old = _markets.get("CNYRUBF")
            if old and str(old["structure_source_identity"].get("key", "")).startswith("TBANK_GRPC:"):
                return
        try:
            raw = CNY.validate_snapshot(CNY._snapshot(connection), now=clock)
            identity = VPS.identity("CNYRUBF", raw)
            mapping = {}
            for tf, field in (("1m", "structure_minute_bars"),
                              ("5m", "canonical_five_minute_bars"),
                              ("1h", "canonical_hourly_bars")):
                mapping[tf] = [dict(bar, timeframe=tf, source_identity=deepcopy(identity))
                               for bar in raw[field][-MAX_BARS_PER_TIMEFRAME:]]
            raw.update(structure_source_identity=identity, structure_bars_by_timeframe=mapping)
            if publish_market(raw):
                self.state["direct_cny_recoveries"] = self.state.get("direct_cny_recoveries", 0)+1
                self.state["direct_cny_recovery_reason"] = None
                CNY._record_state(status='DIRECT_READY', reason=None,
                    checked_at=clock.isoformat(), instrument_uid=raw['broker_instrument_uid'],
                    source='TBANK_GRPC', quote_observed_at=raw['observed_at'], history_source='TBANK_GRPC')
        except Exception as error:
            # No freshness promotion, source substitution or forged candle on
            # failure. The same exact quote/book/session/history gates still apply.
            self.state["direct_cny_recovery_reason"] = (
                str(error) if isinstance(error, CNY.TB.TBankError) else "CNY_RECOVERY_UNAVAILABLE")

    def _native_broker_history(self, market):
        """Refresh from the existing broker reader's memory, never its network."""
        identity = market["structure_source_identity"]
        if not str(identity.get("key") or "").startswith("TBANK_GRPC:"):
            return market
        connection = getattr(self.ns.get("VTB"), "connection", None)
        if connection is None:
            return market
        asset = market["asset"]
        with connection.lock:
            snapshots = {}
            for tf in HORIZONS:
                value = connection.candles.get((asset, tf)) or {}
                version = (value.get("loaded_at"), value.get("status"), value.get("instrument_uid"))
                if (version != self._history_versions.get((asset, tf))
                        and value.get("status") == "OK"
                        and value.get("instrument_uid") == identity.get("contract_id")):
                    snapshots[tf] = deepcopy(value)
        factor = (market.get("quote", {}).get("contract") or {}).get("normalization_factor")
        if asset == "CNYRUBF" and factor is None:
            # The direct adapter certifies the unit before publish_market.
            # Missing that certificate is not permission to infer a factor.
            return market
        factor = VPS.positive(1.0 if factor is None else factor)
        if factor is None:
            return market
        mapping = market["structure_bars_by_timeframe"]
        for tf, value in snapshots.items():
            bars = []
            for b in value.get("candles", [])[-MAX_BARS_PER_TIMEFRAME:]:
                at = TS.timestamp(b.get("time"))
                if at is None or b.get("candle_source") == "CANDLE_SOURCE_DEALER_WEEKEND":
                    continue
                try:
                    row = {key: float(b[key])*factor for key in ("open", "high", "low", "close")}
                    row.update(ts=at, volume=float(b.get("volume_lots", 0)), timeframe=tf,
                               source_identity=deepcopy(identity))
                    bars.append(row)
                except (KeyError, TypeError, ValueError):
                    continue
            if bars:
                last_at = max(bar["ts"] for bar in bars)
                previous_at = max((TS.timestamp(bar.get("ts")) or 0 for bar in mapping.get(tf, [])), default=0)
                if last_at >= previous_at:
                    mapping[tf] = bars
                # Only successfully validated data consumes a cache version.
                # A missing normalization certificate can be supplied by the
                # next ordinary publish without waiting for another download.
                self._history_versions[(asset, tf)] = (
                    value.get("loaded_at"), value.get("status"), value.get("instrument_uid"))
        if snapshots:
            with _cache_lock:
                current = _markets.get(asset)
                if current and _same_source(identity, current["structure_source_identity"]):
                    current["structure_bars_by_timeframe"] = deepcopy(mapping)
        return market

    def _quotes(self, markets, supplied):
        if supplied is not None:
            return dict(supplied)
        result = {}
        for asset, market in markets.items():
            identity = market["structure_source_identity"]
            job = self._pending.get(asset)
            if job and job.done():
                try:
                    result[asset] = job.result()
                except Exception as error:
                    self.state["last_quote_error"] = type(error).__name__
                del self._pending[asset]
            if asset == "CNYRUBF" and str(identity.get("key", "")).startswith("TBANK_GRPC:"):
                # A stream quote is a local locked read and can be used in this
                # pass rather than paying another five-second future cycle.
                try:
                    result[asset] = self.quote_fetcher(self.ns, asset, identity)
                except Exception as error:
                    self.state["last_quote_error"] = type(error).__name__
                continue
            if asset not in self._pending:
                self._pending[asset] = self._executor.submit(self.quote_fetcher, self.ns, asset, identity)
            if asset not in result:
                cached = VPG.quote_for_position({"asset": asset, "payload": {"price_source_lock": identity}})
                if cached:
                    result[asset] = cached
        return result

    def _fresh_quote(self, asset, identity, quote, clock, *, consume=True):
        if not quote:
            return None
        quote = _clean_quote(quote)
        quote["asset"] = asset
        actual = VPS.identity(asset, quote)
        if not _same_source(identity, actual):
            self.state["source_rejections"] += 1
            return None
        if (quote.get("source_gate_pass") is not True or quote.get("market_open") is not True
                or not VX.paper_quote_time_gate(quote, now=clock).get("eligible")):
            self.state["stale_quotes"] += 1
            return None
        observed = TS.timestamp(quote.get("observed_at"))
        price = TS._number(quote.get("price"))
        if observed is None or price is None or price <= 0:
            return None
        key = (asset, *_source_key(identity))
        signature = (observed, price, quote.get("best_bid", quote.get("bid")),
                     quote.get("best_ask", quote.get("ask")), quote.get("book_observed_at"))
        previous = self._observations.get(key)
        if previous == signature:
            self.state["duplicate_quotes"] += 1
            return None
        if previous and (observed < previous[0] or observed == previous[0] and price != previous[1]):
            self.state["stale_quotes"] += 1
            return None
        # A retry may publish a fresher price for execution but has not yet
        # observed that quote against all native levels. Do not consume it as
        # structurally processed or a new breakout could be lost afterwards.
        if consume:
            self._observations[key] = signature
            self.state["fresh_quotes"] += 1
        else:
            self.state["retry_quote_refreshes"] = self.state.get("retry_quote_refreshes", 0)+1
        VPG.publish_quote(asset, quote)
        return quote

    def _row(self, market, quote, tf, context, template, clock):
        event = context.get("event") or {}
        direction = event.get("direction")
        if direction not in ("LONG", "SHORT"):
            return None
        same_direction = (template.get("research_decision") or template.get("decision")) == direction
        # Keep observed analytics, but never copy the previous event's trade
        # plan, hard vetoes, reverse setup, arbitrary confidence or permission.
        keep = ("regime", "realized_vol", "sma18", "sma50", "sma200",
                "research_history_status", "research_feature_availability",
                "research_hourly_bar_count", "source_policy", "minimum_sources")
        row = {key: deepcopy(template[key]) for key in keep if key in template}
        if same_direction:
            for key in ("confidence", "model_quality_score", "signal_quality",
                        "independent_evidence_families", "horizon_structure",
                        "horizon_structure_direction", "horizon_structure_score",
                        "horizon_structure_state", "effective_evidence"):
                if key in template:
                    row[key] = deepcopy(template[key])
        row.update(asset=market["asset"], horizon=tf, decision=direction, research_decision=direction,
                   signal_tier=direction, execution_signal_tier=direction,
                   confidence=row.get("confidence"), pwin=None,
                   probability_source="STRUCTURAL_EVENT_UNCALIBRATED",
                   price=quote["price"], observed_at=quote["observed_at"],
                   market_observed_at=quote["observed_at"], _execution_quote=deepcopy(quote),
                   source_gate_pass=True, paper_eligible=quote.get("paper_eligible", True),
                   execution_eligible=True, production_eligible=False,
                   _breakout_checked_at=clock.isoformat(), _breakout_runtime=VERSION,
                   snapshot_stale=False, trade_plan={})
        row.update({key: deepcopy(quote[key]) for key in VPS.QUOTE_FIELDS if key in quote})
        # Project before copying. The previous code cloned every historical
        # level and immediately discarded all but forty of them.
        public_context = {key: deepcopy(value[-40:] if key == "levels" else value)
                          for key, value in context.items() if key != "quote_state"}
        public_context.setdefault("levels", [])
        row["timeframe_entry_context"] = public_context
        row["trend_entry_context"] = public_context
        row = TFP.prepare_row(row, price=quote["price"], now=clock)
        plan = row["trade_plan"]
        plan.update(market_observed_at=quote["observed_at"], best_bid=quote.get("best_bid", quote.get("bid")),
                    best_ask=quote.get("best_ask", quote.get("ask")), spread_bps=quote.get("spread_bps"))
        row["trade_plan"] = TFP.final_plan(market["asset"], direction, plan, now=clock)
        row["entry_quality"] = row["trade_plan"].get("entry_quality")
        row["decision_stage"] = "ENTRY_READY" if row["trade_plan"].get("eligible") else "WAIT_LOCAL_ENTRY"
        row["expected_move_pct"] = row["trade_plan"].get("expected_move_pct")
        return row

    def _publish_rows(self, rows, execution, clock):
        summary = _report_scalars(execution, ("status", "reason", "version", "checked_at", "paper_only"))
        indexed = _execution_report_index(rows, execution)
        for row in rows:
            matching = indexed.get(_row_event_key(row), {})
            result = dict(summary, portfolios=[matching[name] for name in CTC.PORTFOLIO_ORDER if name in matching])
            row["_execution_audit"] = {"lane": VERSION, "checked_at": clock.isoformat(),
                                       "paper_only": True, "result": result}
        with _cache_lock:
            for row in rows:
                _latest_rows[(row["asset"], row["horizon"])] = deepcopy(row)
        with self.ns.get("lock", nullcontext()):
            last = self.ns.setdefault("last_cycle", {})
            summary = list(last.get("summary") or [])
            updated = {(r["asset"], r["horizon"]): r for r in rows}
            merged = []
            for old in summary:
                key = (old.get("asset"), old.get("horizon"))
                new = updated.pop(key, None)
                old_at = TS.timestamp(old.get("market_observed_at") or old.get("observed_at"))
                new_at = TS.timestamp((new or {}).get("market_observed_at") or (new or {}).get("observed_at"))
                merged.append(new if new and (old_at is None or new_at is not None and new_at >= old_at) else old)
            merged.extend(updated.values())
            last["summary"] = merged
            last["breakout_runtime"] = self.snapshot()

    def _retry_entry(self, markets, quotes, clock, started):
        """Retry original events before history work can exhaust their queue lease.

        The callback remains the sole admission/accounting authority and obtains
        a current same-source quote at mutation. No event timestamp is renewed.
        Quote collection continues for every asset while the book is occupied.
        """
        pending, self._pending_entry_rows = self._pending_entry_rows, []
        for asset, market in markets.items():
            self._fresh_quote(asset, market['structure_source_identity'],
                              quotes.get(asset) or {}, clock, consume=False)
        # A source/contract rollover since enqueue cannot execute an old event
        # even if its former cached quote is still inside a freshness window.
        current, rejected = [], []
        with _cache_lock:
            for row in pending:
                market = _markets.get(row.get('asset')) or {}
                expected = market.get('structure_source_identity')
                actual = VPS.identity(row.get('asset'), VPS.quote_from_row(row))
                (current if expected and _same_source(expected, actual) else rejected).append(row)
        if rejected:
            self._publish_rows(rejected, {'status':'BLOCKED',
                'reason':'STRUCTURAL_RETRY_SOURCE_CHANGED','paper_only':True}, clock)
        pending = current
        entry_started = time.monotonic()
        execution = (self.entry_pass(pending, clock) if pending else
                     {'status':'BLOCKED','reason':'STRUCTURAL_RETRY_SOURCE_CHANGED','paper_only':True})
        if not pending:
            VPG._mutex.cancel_entry_turn()
        entry_seconds = time.monotonic()-entry_started
        if execution.get('status') == 'BUSY' and execution.get('entry_turn_reserved') is not False:
            self._pending_entry_rows = pending
        self._publish_rows(pending, execution, clock)
        self.state.update(status='WAITING_FOR_BOOK' if execution.get('status') == 'BUSY' else 'OK',
                          checked_at=clock.isoformat(), cycles=self.state['cycles']+1,
                          rows=len(pending), assets=len(markets),
                          context_seconds=0.0, entry_seconds=entry_seconds,
                          retry_passes=self.state.get('retry_passes',0)+1,
                          pending_entry_rows=len(self._pending_entry_rows),
                          execution_status=execution.get('status'),
                          execution_reason=execution.get('reason'),
                          duration_seconds=time.monotonic()-started)
        return {**self.snapshot(), 'execution':execution}

    def run_once(self, now=None, *, quotes=None):
        """Observe one bounded pass; supplied quotes make deterministic tests."""
        if not self._run_lock.acquire(blocking=False):
            return {"status": "BUSY", "paper_only": True}
        started = time.monotonic()
        try:
            clock = _clock(now)
            if clock is None:
                return {"status": "INVALID_CLOCK", "paper_only": True}
            self._recover_direct_cny(clock)
            # Copy only the lightweight quote/source descriptors up front.
            # Full native histories are copied one asset at a time below, so the
            # fast lane cannot retain a second complete multi-asset market graph
            # while the ordinary cycle is building its own current asset.
            with _cache_lock:
                markets = {
                    asset: {
                        "asset": market["asset"],
                        "structure_source_identity": deepcopy(
                            market["structure_source_identity"]),
                        "quote": deepcopy(market.get("quote") or {}),
                    }
                    for asset, market in _markets.items()
                }
            summary = _namespace_summary(self.ns)
            templates = {(row.get("asset"), row.get("horizon")): row for row in summary}
            observed_quotes = self._quotes(markets, quotes)
            # Network futures may complete long after their original scheduling
            # time. Never evaluate a quote with a clock from before retrieval.
            if now is None:
                clock = _clock()
            if self._pending_entry_rows:
                return self._retry_entry(markets, observed_quotes, clock, started)
            context_started = time.monotonic()
            rows = []
            for asset, descriptor in markets.items():
                identity = descriptor["structure_source_identity"]
                quote = self._fresh_quote(asset, identity, observed_quotes.get(asset) or {}, clock)
                if not quote:
                    continue
                # Take an isolated snapshot only for the asset being evaluated.
                # Revalidate identity because publish_market may replace this
                # asset between quote collection and the per-asset snapshot.
                with _cache_lock:
                    current = _markets.get(asset)
                    if (not current or not _same_source(
                            identity, current["structure_source_identity"])):
                        continue
                    market = deepcopy(current)
                market = self._native_broker_history(market)
                raw = dict(market, **quote, _execution_quote=quote)
                for tf in HORIZONS:
                    if not market["structure_bars_by_timeframe"].get(tf):
                        continue
                    # The ordinary and fast loops share TFD's single state
                    # owner; two private quote-state caches would diverge.
                    context = self.context_builder(raw, tf, clock)
                    row = self._row(market, quote, tf, context, templates.get((asset, tf), {}), clock)
                    if row:
                        rows.append(row)
            context_seconds = time.monotonic()-context_started
            execution = {"status": "NO_STRUCTURAL_EVENTS", "paper_only": True}
            entry_seconds, publish_seconds = 0.0, 0.0
            if rows:
                # The callback owns the shared RLock + PostgreSQL advisory
                # transaction and every existing canonical admission/risk gate.
                entry_started = time.monotonic()
                execution = self.entry_pass(rows, clock)
                entry_seconds = time.monotonic()-entry_started
                if execution.get("status") == "BUSY" and execution.get("entry_turn_reserved") is not False:
                    # Reuse this bounded batch, rather than rebuilding all
                    # seven assets before trying the reserved book turn.
                    self._pending_entry_rows = rows
                publish_started = time.monotonic()
                self._publish_rows(rows, execution, clock)
                publish_seconds = time.monotonic()-publish_started
            self.state.update(status="WAITING_FOR_BOOK" if execution.get("status") == "BUSY" else "OK",
                              pending_entry_rows=len(self._pending_entry_rows),
                              execution_reason=execution.get("reason"),
                              checked_at=clock.isoformat(), cycles=self.state["cycles"]+1,
                              rows=len(rows), assets=len(markets), pending_quote_fetches=len(self._pending),
                              context_seconds=context_seconds, entry_seconds=entry_seconds,
                              publish_seconds=publish_seconds, execution_status=execution.get("status"),
                              duration_seconds=time.monotonic()-started)
            return {**self.snapshot(), "execution": execution}
        except Exception as error:
            self.state.update(status="ERROR", error=type(error).__name__+": "+str(error),
                              duration_seconds=time.monotonic()-started)
            emit = self.ns.get("emit")
            if callable(emit):
                emit("structural_quote_runtime_error", error=self.state["error"], paper_only=True)
            return self.snapshot()
        finally:
            self._run_lock.release()

    def snapshot(self):
        out = dict(self.state)
        describe = getattr(VPG._mutex, 'snapshot', None)
        if callable(describe):
            out['book_lock'] = describe()
        out['proof_cache'] = TFP.SB.SVC.snapshot()
        return out

    def close(self):
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def loop(self):
        last_log = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            self.run_once()
            emit = self.ns.get("emit")
            if callable(emit) and time.monotonic()-last_log >= 60:
                emit("structural_quote_runtime", **self.snapshot())
                last_log = time.monotonic()
            interval = BOOK_RETRY_SECONDS if self._pending_entry_rows else INTERVAL_SECONDS
            self._stop.wait(max(0.05, interval-(time.monotonic()-started)))


def start(ns, entry_pass):
    """Start once after database/bootstrap readiness; callback is PAPER only."""
    global _runtime
    with _start_lock:
        if _runtime is not None:
            return _runtime
        _runtime = BreakoutRuntime(ns, entry_pass)
        threading.Thread(target=_runtime.loop, daemon=True, name="veritas-structural-quotes").start()
        return _runtime


def snapshot():
    return _runtime.snapshot() if _runtime else {"status": "NOT_STARTED", "version": VERSION,
                                                "paper_only": True, "interval_seconds": INTERVAL_SECONDS}


def restore_snapshot(ns):
    """Restore the durable display and available quote state without executing."""
    emit=ns['emit']
    try:
        cold=ns['latest_signal_summary_pg']() if ns['pg_enabled']() else []
        if not cold:
            return
        indexed={(x.get('asset'),x.get('horizon')):dict(x) for x in cold
                 if x.get('asset') and x.get('horizon')}
        rows=[]
        for asset in ns['DISPLAY_ASSETS']:
            for horizon in HORIZONS:
                row=indexed.get((asset,horizon))
                if row:
                    row['snapshot_stale']=True
                    TFD.restore_context_state(row)
                    rows.append(row)
        with ns['lock']:
            ns['last_cycle'].update(status='warming',at=ns['now'](),version=ns['VERSION'],
                                    summary=rows,summary_source='postgres_cold_start',
                                    signal_cells=len(rows),cycle_mode='COLD_START')
        emit('v90_cold_start_snapshot',signal_cells=len(rows),status='READY')
    except Exception as error:
        emit('v90_cold_start_snapshot',signal_cells=0,status='ERROR',
             error=f'{type(error).__name__}: {error}')
