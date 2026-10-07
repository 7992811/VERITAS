"""One-time source transformation for the reviewed repair branch; not runtime."""
from pathlib import Path
import hashlib

EXPECTED = {
    'veritas_structural_breakout.py':'145878bb5e475ec54bcc115707906e8b52050625',
    'veritas_breakout_runtime.py':'7d4a873',
    'veritas_book_lock.py':'a1223c81b69e01952d77bd7820138ea4b2b7fbb6',
}
texts = {}
for name, digest in EXPECTED.items():
    raw = Path(name).read_bytes()
    actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if not actual.startswith(digest):
        raise SystemExit('Base changed: ' + name)
    texts[name] = raw.decode()

def replace(name, old, new):
    if texts[name].count(old) != 1:
        raise SystemExit('Anchor missing or ambiguous in ' + name + ': ' + old[:90])
    texts[name] = texts[name].replace(old,new)

sb = 'veritas_structural_breakout.py'
replace(sb, 'from veritas_quote_time import quote_gate\n',
        'from veritas_quote_time import quote_gate\nimport veritas_structural_validation_cache as SVC\n')
replace(sb, 'def validate_event(event, source_identity=None):\n', '''def validate_event(event, source_identity=None):
    # Static evidence only. Current quote/time, spent barriers, entry expiry and
    # full portfolio risk remain independently checked on every decision.
    return SVC.verify(event, source_identity, version=VERSION,
                      defaults=DEFAULT_POLICY, validator=_validate_event_uncached)


def _validate_event_uncached(event, source_identity=None):
''')
replace(sb, 'def _native_facts(raw, as_of, source, policy):\n', '''def _first_cross_times(rows_by_tf, levels):
    """Earliest available historical crossing, preserving delayed bar times."""
    result = {}
    for level in levels:
        crossed_at = None
        for bar in rows_by_tf.get(level['timeframe'], ()):
            if bar['ts'] < level['available_at']:
                continue
            crossed = (bar['high'] > level['price'] if level['kind'] == 'resistance'
                       else bar['low'] < level['price'])
            if crossed:
                available = bar['available_at']
                crossed_at = available if crossed_at is None else min(crossed_at, available)
        if crossed_at is not None:
            result[level['level_id']] = crossed_at
    return result


def _native_facts(raw, as_of, source, policy):
''')
replace(sb, '    facts = {"rows_by_tf": rows_by_tf, "levels": levels, "atr_by_tf": atr_by_tf}\n',
'''    facts = {"rows_by_tf": rows_by_tf, "levels": levels, "atr_by_tf": atr_by_tf,
             "first_cross_at": _first_cross_times(rows_by_tf, levels)}
''')
replace(sb, '''    for level in levels:
        for bar in rows_by_tf.get(level["timeframe"], []):
            if bar["ts"] < level["available_at"] or bar["available_at"] > known_before:
                continue
            crossed = (bar["high"] > level["price"] if level["kind"] == "resistance"
                       else bar["low"] < level["price"])
            if crossed:
                seen.add(level["level_id"])
                break
''', '''    # One historical scan per exact native bundle, not seven scans for seven
    # execution horizons. Compare its original availability to this lane's
    # known-before clock; a later bar never consumes a fresh quote crossing.
    seen.update(level_id for level_id, available in facts['first_cross_at'].items()
                if available <= known_before)
''')

br = 'veritas_breakout_runtime.py'
replace(br, 'INTERVAL_SECONDS = 5.0\n', 'INTERVAL_SECONDS = 5.0\nBOOK_RETRY_SECONDS = 0.5\n')
replace(br, '        self._pending, self._observations = {}, {}\n',
        '        self._pending, self._observations = {}, {}\n        self._pending_entry_rows = []\n')
replace(br, '''        public_context = {key: deepcopy(value) for key, value in context.items() if key != "quote_state"}
        public_context["levels"] = public_context.get("levels", [])[-40:]
''', '''        # Project before copying. The previous code cloned every historical
        # level and immediately discarded all but forty of them.
        public_context = {key: deepcopy(value[-40:] if key == "levels" else value)
                          for key, value in context.items() if key != "quote_state"}
        public_context.setdefault("levels", [])
''')
replace(br, '    def run_once(self, now=None, *, quotes=None):\n', '''    def _retry_entry(self, markets, quotes, clock, started):
        """Retry original events before history work can exhaust their queue lease.

        The callback remains the sole admission/accounting authority and obtains
        a current same-source quote at mutation. No event timestamp is renewed.
        Quote collection continues for every asset while the book is occupied.
        """
        pending, self._pending_entry_rows = self._pending_entry_rows, []
        for asset, market in markets.items():
            self._fresh_quote(asset, market['structure_source_identity'],
                              quotes.get(asset) or {}, clock)
        entry_started = time.monotonic()
        execution = self.entry_pass(pending, clock)
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
''')
replace(br, '''            context_started = time.monotonic()
            rows = []
''', '''            if self._pending_entry_rows:
                return self._retry_entry(markets, observed_quotes, clock, started)
            context_started = time.monotonic()
            rows = []
''')
replace(br, '''                if execution.get("status") == "BUSY":
                    # Book contention does not consume an otherwise fresh
                    # observation. Retry this exact quote on the next pass;
                    # the structural owner preserves the original event time.
                    for asset in {row["asset"] for row in rows}:
                        identity = markets[asset]["structure_source_identity"]
                        self._observations.pop((asset, *_source_key(identity)), None)
''', '''                if execution.get("status") == "BUSY" and execution.get("entry_turn_reserved") is not False:
                    # Reuse this bounded batch, rather than rebuilding all
                    # seven assets before trying the reserved book turn.
                    self._pending_entry_rows = rows
''')
replace(br, '''            self.state.update(status="OK", checked_at=clock.isoformat(), cycles=self.state["cycles"]+1,
''', '''            self.state.update(status="WAITING_FOR_BOOK" if execution.get("status") == "BUSY" else "OK",
                              pending_entry_rows=len(self._pending_entry_rows),
                              execution_reason=execution.get("reason"),
                              checked_at=clock.isoformat(), cycles=self.state["cycles"]+1,
''')
replace(br, '''    def snapshot(self):
        return dict(self.state)
''', '''    def snapshot(self):
        out = dict(self.state)
        describe = getattr(VPG._mutex, 'snapshot', None)
        if callable(describe):
            out['book_lock'] = describe()
        out['proof_cache'] = TFP.SB.SVC.snapshot()
        return out
''')
replace(br, '            self._stop.wait(max(0.05, INTERVAL_SECONDS-(time.monotonic()-started)))\n',
'''            interval = BOOK_RETRY_SECONDS if self._pending_entry_rows else INTERVAL_SECONDS
            self._stop.wait(max(0.05, interval-(time.monotonic()-started)))
''')

bl = 'veritas_book_lock.py'
replace(bl, '        self._entry_turns = {}\n',
'''        self._entry_turns = {}
        self._owner_name = None
        self._acquired_at = None
        self._last_hold_seconds = 0.0
''')
replace(bl, '                self._owner, self._depth = ident, 1\n',
'''                self._owner, self._depth = ident, 1
                self._owner_name = threading.current_thread().name[:96]
                self._acquired_at = time.monotonic()
''')
replace(bl, '''            if not self._depth:
                self._owner = None
                self._condition.notify_all()
''', '''            if not self._depth:
                self._last_hold_seconds = max(0.0, time.monotonic()-self._acquired_at)
                self._owner = self._owner_name = self._acquired_at = None
                self._condition.notify_all()
''')
replace(bl, '    def __enter__(self):\n', '''    def snapshot(self):
        """Bounded diagnostics only: no frames, local variables or account data."""
        with self._condition:
            now = time.monotonic()
            return {'owner':self._owner_name, 'depth':self._depth,
                    'held_seconds':max(0.0, now-self._acquired_at) if self._acquired_at is not None else 0.0,
                    'last_hold_seconds':self._last_hold_seconds,
                    'protection_waiters':self._priority_waiters,
                    'ordinary_waiters':self._ordinary_waiters,
                    'entry_reservations':sum(until > now for until in self._entry_turns.values())}

    def __enter__(self):
''')
for name, text in texts.items():
    Path(name).write_text(text)
print('Applied exact structural-latency transformation')
