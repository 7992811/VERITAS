from pathlib import Path
import hashlib
p=Path('veritas_breakout_runtime.py');raw=p.read_bytes()
if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!='cfaf3f0310195708ed96bd1736418c0151bc0598':
    raise SystemExit('retry source changed')
s=raw.decode()
def edit(old,new):
    global s
    if s.count(old)!=1:raise SystemExit('retry anchor: '+old[:90])
    s=s.replace(old,new)
edit('    def _fresh_quote(self, asset, identity, quote, clock):\n',
     '    def _fresh_quote(self, asset, identity, quote, clock, *, consume=True):\n')
edit('''        self._observations[key] = signature
        self.state["fresh_quotes"] += 1
        VPG.publish_quote(asset, quote)
''','''        # A retry may publish a fresher price for execution but has not yet
        # observed that quote against all native levels. Do not consume it as
        # structurally processed or a new breakout could be lost afterwards.
        if consume:
            self._observations[key] = signature
            self.state["fresh_quotes"] += 1
        else:
            self.state["retry_quote_refreshes"] = self.state.get("retry_quote_refreshes", 0)+1
        VPG.publish_quote(asset, quote)
''')
edit('''                new_at = TS.timestamp((new or {}).get("market_observed_at"))
                merged.append(new if new and (old_at is None or new_at >= old_at) else old)
''','''                new_at = TS.timestamp((new or {}).get("market_observed_at") or (new or {}).get("observed_at"))
                merged.append(new if new and (old_at is None or new_at is not None and new_at >= old_at) else old)
''')
edit('''        pending, self._pending_entry_rows = self._pending_entry_rows, []
        for asset, market in markets.items():
            self._fresh_quote(asset, market['structure_source_identity'],
                              quotes.get(asset) or {}, clock)
        entry_started = time.monotonic()
        execution = self.entry_pass(pending, clock)
''','''        pending, self._pending_entry_rows = self._pending_entry_rows, []
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
''')
p.write_text(s)
# Missing market_observed_at is intentional in context_row: the display must
# accept the same verified observed_at alias as the rest of the system.
p=Path('test_veritas_structural_latency.py');s=p.read_text()
needle="        self.ns={'lock':threading.RLock(),'last_cycle':{'summary':[]}}\n"
assert s.count(needle)==1
s=s.replace(needle,"        self.assertTrue(BR.publish_market(self.row))\n"+needle)
s=s.replace("self.assertEqual(result['status'],'WAITING_FOR_BOOK')", "self.assertEqual(result['status'],'WAITING_FOR_BOOK',result)")
p.write_text(s)
print('Retry and display refinements applied')
