"""One-time reviewed transformation; no runtime patching or trading changes."""
from pathlib import Path
modules=('veritas_structural_breakout.py','veritas_breakout_runtime.py',
         'veritas_timeframe_policy.py','veritas_timeframe_data.py','veritas_structural_lifecycle.py')
texts={name:Path(name).read_text() for name in modules}
for name in modules:
    old='from copy import deepcopy\n'
    if texts[name].count(old)!=1:raise SystemExit('copy import mismatch: '+name)
    texts[name]=texts[name].replace(old,'from veritas_data_copy import deepcopy\n')
def edit(name,old,new):
    if texts[name].count(old)!=1:raise SystemExit('anchor mismatch: '+name+' '+old[:80])
    texts[name]=texts[name].replace(old,new)
name='veritas_structural_breakout.py'
old='def build_context(raw, horizon, now=None, base_context=None, *, state=None, config=None):\n'
new='''def build_context(raw, horizon, now=None, base_context=None, *, state=None, config=None):
    result = _build_context_impl(raw, horizon, now, base_context,
                                 state=state, config=config)
    # Invalid returns preserve the supplied diagnostic state. A successful
    # observation already owns a new detached state, so do not first clone a
    # complete old proof graph that would immediately be discarded.
    if result.get('quote_state') is None and state is not None:
        result['quote_state'] = deepcopy(state)
    return result


def _build_context_impl(raw, horizon, now=None, base_context=None, *, state=None, config=None):
'''
edit(name,old,new)
edit(name,'"quote_state": deepcopy(state)}','"quote_state": None}')
edit(name,'    event = deepcopy(next_state.get("active_event"))\n    leg = deepcopy(next_state.get("protected_leg"))\n',
'''    # next_state is already a private deep copy of the caller's state.
    event = next_state.get("active_event")
    leg = next_state.get("protected_leg")
''')
edit(name,'                      active_event=deepcopy(event), protected_leg=deepcopy(leg))\n',
'                      active_event=event, protected_leg=leg)\n')
name='veritas_timeframe_data.py'
edit(name,'        witness = deepcopy(active)\n',
'''        # _spend writes only its top-level spent fields; historical proof
        # fields are read-only. A full graph clone here is discarded at return.
        witness = dict(active)
''')
name='veritas_breakout_runtime.py'
edit(name,'        self.state = {"status": "READY", "version": VERSION, "paper_only": True,\n',
'''        self.state = {"status": "READY", "version": VERSION, "paper_only": True,
                      "latency_policy_version": "NATIVE_COPY_AND_RESERVED_RETRY_V2",
''')
for name,text in texts.items():Path(name).write_text(text)
print('Native snapshot copy optimizations applied; financial rules unchanged')
