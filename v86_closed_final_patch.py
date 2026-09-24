from pathlib import Path
import sys

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')

old="""        outcome={'net_pnl':str(episode_net),'gross_close_leg':str(gross),'funding_close_leg':str(fund),
                 'observed_mfe_fraction':str(mfe),'observed_mae_fraction':str(mae),
"""
new="""        outcome={'net_pnl':str(episode_net),'gross_close_leg':str(gross),'funding_close_leg':str(fund),
                 'entry_fee':str(entry_fee),'exit_fee':str(fee),'exit_price':str(quote.price),
                 'outcome_finalized':True,'finalization_contract':'CLOSED_FINAL_V1','learning_eligible':True,
                 'observed_mfe_fraction':str(mfe),'observed_mae_fraction':str(mae),
"""
if old not in s:
    raise SystemExit('CLOSED_FINAL_OUTCOME_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

anchor="        insert_lesson(c,lesson)\n"
guard="""        _required_final=('net_pnl','gross_close_leg','funding_close_leg','entry_fee','exit_fee','exit_price',
                         'observed_mfe_fraction','observed_mae_fraction','giveback_from_observed_peak',
                         'held_seconds','exit_reason','path_points')
        _missing_final=[k for k in _required_final if outcome.get(k) is None]
        if _missing_final or int(outcome.get('path_points') or 0)<2:
            raise RuntimeError('CLOSED_FINAL_INCOMPLETE:'+','.join(_missing_final or ['path_points<2']))
        insert_lesson(c,lesson)
"""
if anchor not in s:
    raise SystemExit('CLOSED_FINAL_LEARNING_ANCHOR_NOT_FOUND')
s=s.replace(anchor,guard,1)
p.write_text(s,encoding='utf-8')
print('V86_CLOSED_FINAL_PATCH_OK')
