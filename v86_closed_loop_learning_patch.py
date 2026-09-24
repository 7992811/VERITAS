from pathlib import Path
import sys

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()

# Closed-Loop Learning V1
# - every eligible CLOSED_FINAL becomes an immutable lesson
# - aggregates are deduplicated by market idea, so eight portfolio copies do not fake sample size
# - learning never flips direction and never overrides hard safety/source gates
# - poor contexts keep the 5% signal-first probe but cannot scale
# - execution-loss contexts tighten profit protection on subsequent trades

module = r'''from __future__ import annotations
from datetime import datetime, timezone
import json, math, hashlib

CONTRACT='V86_CLOSED_LOOP_LEARNING_V1'
PRIOR_A=3.0
PRIOR_B=3.0

def _now():
    return datetime.now(timezone.utc)

def _iso(at=None):
    return (at or _now()).isoformat()

def _obj(x):
    if isinstance(x,dict): return dict(x)
    if isinstance(x,str):
        try:
            y=json.loads(x)
            return y if isinstance(y,dict) else {}
        except Exception:
            return {}
    return {}

def _f(x,default=None):
    try:
        if x is None:return default
        y=float(x)
        return y if math.isfinite(y) else default
    except Exception:
        return default

def _i(x,default=0):
    try:return int(x)
    except Exception:return default

def _hash(*parts):
    return hashlib.sha256('|'.join(str(x) for x in parts).encode()).hexdigest()[:32]

def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS v86_closed_loop_lessons(
      episode_id TEXT PRIMARY KEY,
      idea_id TEXT NOT NULL,
      account_id TEXT NOT NULL,
      asset TEXT NOT NULL,
      horizon TEXT NOT NULL,
      setup_family TEXT NOT NULL,
      regime TEXT NOT NULL,
      direction TEXT NOT NULL,
      closed_at TEXT NOT NULL,
      net_pnl TEXT NOT NULL,
      outcome_class TEXT NOT NULL,
      win INTEGER NOT NULL,
      direction_failure INTEGER NOT NULL,
      execution_failure INTEGER NOT NULL,
      high_capture_win INTEGER NOT NULL,
      low_capture_win INTEGER NOT NULL,
      mfe_fraction TEXT,
      mae_fraction TEXT,
      giveback_fraction TEXT,
      path_points INTEGER NOT NULL,
      payload TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS v86_learning_contexts(
      context_key TEXT PRIMARY KEY,
      scope TEXT NOT NULL,
      asset TEXT NOT NULL,
      horizon TEXT NOT NULL,
      setup_family TEXT NOT NULL,
      regime TEXT NOT NULL,
      direction TEXT NOT NULL,
      unique_ideas INTEGER NOT NULL,
      wins INTEGER NOT NULL,
      losses INTEGER NOT NULL,
      direction_failures INTEGER NOT NULL,
      execution_failures INTEGER NOT NULL,
      high_capture_wins INTEGER NOT NULL,
      low_capture_wins INTEGER NOT NULL,
      posterior_mean TEXT NOT NULL,
      posterior_low TEXT NOT NULL,
      posterior_high TEXT NOT NULL,
      direction_failure_rate TEXT NOT NULL,
      execution_failure_rate TEXT NOT NULL,
      scale_cap TEXT NOT NULL,
      no_scale INTEGER NOT NULL,
      entry_policy TEXT NOT NULL,
      management_policy TEXT NOT NULL,
      authority TEXT NOT NULL,
      validated_rule INTEGER NOT NULL,
      updated_at TEXT NOT NULL,
      payload TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS v86_learning_applications(
      application_id TEXT PRIMARY KEY,
      decision_id TEXT NOT NULL,
      context_key TEXT NOT NULL,
      asset TEXT NOT NULL,
      horizon TEXT NOT NULL,
      direction TEXT NOT NULL,
      authority TEXT NOT NULL,
      applied_at TEXT NOT NULL,
      payload TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS v86_learning_refresh(
      singleton INTEGER PRIMARY KEY,
      refreshed_at TEXT NOT NULL,
      eligible_closed INTEGER NOT NULL,
      lessons_written INTEGER NOT NULL,
      unique_market_ideas INTEGER NOT NULL,
      contexts_total INTEGER NOT NULL,
      actionable_contexts INTEGER NOT NULL,
      validated_rules INTEGER NOT NULL,
      applications INTEGER NOT NULL,
      payload TEXT NOT NULL
    )""")

def _lesson_from_row(row):
    pay=_obj(row.get('payload')); sig=_obj(pay.get('signal'))
    mfe=_f(row.get('mfe_fraction')); mae=_f(row.get('mae_fraction')); give=_f(row.get('giveback_fraction'))
    net=_f(row.get('net_pnl'),0.0) or 0.0
    capture=(max(0.0,min(1.0,1.0-give/mfe)) if mfe is not None and give is not None and mfe>1e-12 else None)
    win=1 if net>0 else 0
    direction_failure=1 if (net<=0 and (mfe is None or mfe<0.004)) else 0
    execution_failure=1 if (net<=0 and mfe is not None and mfe>=0.004) else 0
    high_capture=1 if (net>0 and capture is not None and capture>=0.60) else 0
    low_capture=1 if (net>0 and capture is not None and capture<0.35) else 0
    if high_capture: cls='RIGHT_DIRECTION_HIGH_CAPTURE'
    elif low_capture: cls='RIGHT_DIRECTION_LOW_CAPTURE'
    elif execution_failure: cls='FAVORABLE_PATH_NOT_MONETIZED'
    elif direction_failure: cls='DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH'
    else: cls='MIXED_EXECUTION'
    return {
      'episode_id':str(row.get('episode_id') or ''),
      'idea_id':str(row.get('idea_id') or row.get('episode_id') or ''),
      'account_id':str(row.get('account_id') or ''),
      'asset':str(row.get('asset') or ''),
      'horizon':str(row.get('horizon') or sig.get('horizon') or ''),
      'setup_family':str(row.get('setup_family') or sig.get('setup_family') or 'UNCLASSIFIED'),
      'regime':str(sig.get('regime') or pay.get('regime') or 'UNKNOWN'),
      'direction':str(row.get('direction') or sig.get('direction') or ''),
      'closed_at':str(row.get('closed_at') or ''),
      'net_pnl':net,'outcome_class':cls,'win':win,
      'direction_failure':direction_failure,'execution_failure':execution_failure,
      'high_capture_win':high_capture,'low_capture_win':low_capture,
      'mfe_fraction':mfe,'mae_fraction':mae,'giveback_fraction':give,
      'path_points':_i(row.get('path_points')),
      'capture':capture
    }

def _context_specs(x):
    a=x['asset'];h=x['horizon'];s=x['setup_family'];r=x['regime'];d=x['direction']
    return [
      ('EXACT',a,h,s,r,d),
      ('SETUP_REGIME','*',h,s,r,d),
      ('ASSET_HORIZON',a,h,'*','*',d),
      # Conservative transfer layer: same asset + same direction across horizons/setups.
      # It may control scale/management only; signal direction remains immutable upstream.
      ('ASSET_DIRECTION',a,'*','*','*',d),
      ('SETUP_FAMILY','*','*',s,'*',d),
    ]

def _context_key(scope,a,h,s,r,d):
    return scope+'|'+a+'|'+h+'|'+s+'|'+r+'|'+d

def _posterior(wins,n):
    a=PRIOR_A+wins;b=PRIOR_B+n-wins
    mean=a/(a+b)
    var=(a*b)/(((a+b)**2)*(a+b+1.0))
    sd=math.sqrt(max(0.0,var))
    return mean,max(0.0,mean-1.64*sd),min(1.0,mean+1.64*sd)

def _policy(n,wins,df,ef,hi,lo):
    mean,low,high=_posterior(wins,n)
    dfr=df/n if n else 0.0; efr=ef/n if n else 0.0; hir=hi/n if n else 0.0
    validated=False
    authority='OBSERVE_ONLY';cap=.25;no_scale=False;entry='SIGNAL_FIRST_PROBE';mgmt='BASELINE'
    if n>=8 and (high<0.50 or dfr>=0.60):
        authority='VALIDATED_NEGATIVE';cap=.05;no_scale=True;entry='PROBE_ONLY_NO_SCALE';validated=True
    elif n>=6 and (mean<0.47 or dfr>=0.50):
        authority='ACTIONABLE_NEGATIVE';cap=.05;no_scale=True;entry='PROBE_ONLY_NO_SCALE'
    elif n>=6 and efr>=0.45 and dfr<0.45:
        authority='ACTIONABLE_EXECUTION';cap=.10;no_scale=False;entry='PROBE_THEN_CONFIRM';mgmt='PROFIT_PROTECT_EARLIER'
    elif n>=8 and low>0.50 and hir>=0.35:
        authority='VALIDATED_POSITIVE';cap=.25;no_scale=False;entry='PROBE_THEN_SCALE';validated=True
    elif n>=5 and mean>=0.56 and dfr<=0.35:
        authority='ACTIONABLE_POSITIVE';cap=.10;no_scale=False;entry='PROBE_THEN_CONFIRM'
    elif n>=3:
        authority='SOFT_LEARNING';cap=.10;no_scale=False;entry='PROBE_THEN_CONFIRM'
    return {'posterior_mean':mean,'posterior_low':low,'posterior_high':high,
            'direction_failure_rate':dfr,'execution_failure_rate':efr,
            'scale_cap':cap,'no_scale':no_scale,'entry_policy':entry,
            'management_policy':mgmt,'authority':authority,'validated_rule':validated}

def refresh_learning_state(ledger,at=None):
    at=at or _now();stamp=_iso(at)
    with ledger.transaction() as c:
        ensure_schema(c)
        try:
            rows=c.execute("""SELECT episode_id,idea_id,account_id,asset,horizon,setup_family,opened_at,closed_at,
                              direction,net_pnl,mfe_fraction,mae_fraction,giveback_fraction,path_points,
                              learning_eligible,payload
                              FROM v86_closed_trade_ledger
                              WHERE learning_eligible=1
                              ORDER BY closed_at,episode_id""").fetchall()
        except Exception:
            rows=[]
        inserted=0
        for raw in rows:
            x=_lesson_from_row(dict(raw))
            if not x['episode_id'] or not x['asset'] or x['direction'] not in ('LONG','SHORT'): continue
            before=c.execute('SELECT 1 FROM v86_closed_loop_lessons WHERE episode_id=?',(x['episode_id'],)).fetchone()
            if before: continue
            payload=json.dumps(x,ensure_ascii=False,separators=(',',':'))
            c.execute("""INSERT INTO v86_closed_loop_lessons
              (episode_id,idea_id,account_id,asset,horizon,setup_family,regime,direction,closed_at,net_pnl,
               outcome_class,win,direction_failure,execution_failure,high_capture_win,low_capture_win,
               mfe_fraction,mae_fraction,giveback_fraction,path_points,payload,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(episode_id) DO NOTHING""",
              (x['episode_id'],x['idea_id'],x['account_id'],x['asset'],x['horizon'],x['setup_family'],x['regime'],
               x['direction'],x['closed_at'],str(x['net_pnl']),x['outcome_class'],x['win'],x['direction_failure'],
               x['execution_failure'],x['high_capture_win'],x['low_capture_win'],
               None if x['mfe_fraction'] is None else str(x['mfe_fraction']),
               None if x['mae_fraction'] is None else str(x['mae_fraction']),
               None if x['giveback_fraction'] is None else str(x['giveback_fraction']),
               x['path_points'],payload,stamp))
            inserted+=1

        lessons=[dict(r) for r in c.execute("""SELECT episode_id,idea_id,account_id,asset,horizon,setup_family,regime,direction,
                    net_pnl,outcome_class,win,direction_failure,execution_failure,high_capture_win,low_capture_win,
                    mfe_fraction,mae_fraction,giveback_fraction,path_points,payload
                    FROM v86_closed_loop_lessons""").fetchall()]
        # Deduplicate by market idea + context. Multiple portfolio copies are one market observation.
        ideas={}
        for l in lessons:
            ik=(str(l['idea_id']),str(l['asset']),str(l['horizon']),str(l['setup_family']),str(l['regime']),str(l['direction']))
            g=ideas.setdefault(ik,[])
            g.append(l)
        idea_rows=[]
        for (idea,a,h,s,r,d),grp in ideas.items():
            n=len(grp)
            votes=lambda k:sum(_i(x.get(k)) for x in grp)
            net=sum((_f(x.get('net_pnl'),0.0) or 0.0) for x in grp)
            idea_rows.append({'idea_id':idea,'asset':a,'horizon':h,'setup_family':s,'regime':r,'direction':d,
                              'win':1 if votes('win')*2>=n else 0,
                              'direction_failure':1 if votes('direction_failure')*2>=n else 0,
                              'execution_failure':1 if votes('execution_failure')*2>=n else 0,
                              'high_capture_win':1 if votes('high_capture_win')*2>=n else 0,
                              'low_capture_win':1 if votes('low_capture_win')*2>=n else 0,
                              'net_sum':net})
        groups={}
        for x in idea_rows:
            for scope,a,h,s,r,d in _context_specs(x):
                key=_context_key(scope,a,h,s,r,d)
                g=groups.setdefault(key,{'scope':scope,'asset':a,'horizon':h,'setup_family':s,'regime':r,'direction':d,'rows':[]})
                g['rows'].append(x)
        actionable=validated=0
        for key,g in groups.items():
            rr=g['rows'];n=len(rr);wins=sum(x['win'] for x in rr);losses=n-wins
            df=sum(x['direction_failure'] for x in rr);ef=sum(x['execution_failure'] for x in rr)
            hi=sum(x['high_capture_win'] for x in rr);lo=sum(x['low_capture_win'] for x in rr)
            p=_policy(n,wins,df,ef,hi,lo)
            if p['authority']!='OBSERVE_ONLY': actionable+=1
            if p['validated_rule']: validated+=1
            payload={'contract':CONTRACT,'context_key':key,'scope':g['scope'],'unique_ideas':n,
                     'wins':wins,'losses':losses,'direction_failures':df,'execution_failures':ef,
                     'high_capture_wins':hi,'low_capture_wins':lo,**p}
            c.execute("""INSERT INTO v86_learning_contexts
              (context_key,scope,asset,horizon,setup_family,regime,direction,unique_ideas,wins,losses,
               direction_failures,execution_failures,high_capture_wins,low_capture_wins,
               posterior_mean,posterior_low,posterior_high,direction_failure_rate,execution_failure_rate,
               scale_cap,no_scale,entry_policy,management_policy,authority,validated_rule,updated_at,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(context_key) DO UPDATE SET
                 unique_ideas=excluded.unique_ideas,wins=excluded.wins,losses=excluded.losses,
                 direction_failures=excluded.direction_failures,execution_failures=excluded.execution_failures,
                 high_capture_wins=excluded.high_capture_wins,low_capture_wins=excluded.low_capture_wins,
                 posterior_mean=excluded.posterior_mean,posterior_low=excluded.posterior_low,posterior_high=excluded.posterior_high,
                 direction_failure_rate=excluded.direction_failure_rate,execution_failure_rate=excluded.execution_failure_rate,
                 scale_cap=excluded.scale_cap,no_scale=excluded.no_scale,entry_policy=excluded.entry_policy,
                 management_policy=excluded.management_policy,authority=excluded.authority,
                 validated_rule=excluded.validated_rule,updated_at=excluded.updated_at,payload=excluded.payload""",
              (key,g['scope'],g['asset'],g['horizon'],g['setup_family'],g['regime'],g['direction'],n,wins,losses,df,ef,hi,lo,
               str(p['posterior_mean']),str(p['posterior_low']),str(p['posterior_high']),
               str(p['direction_failure_rate']),str(p['execution_failure_rate']),str(p['scale_cap']),
               1 if p['no_scale'] else 0,p['entry_policy'],p['management_policy'],p['authority'],
               1 if p['validated_rule'] else 0,stamp,json.dumps(payload,ensure_ascii=False,separators=(',',':'))))
        apps=_i(c.execute('SELECT COUNT(*) AS n FROM v86_learning_applications').fetchone()['n'])
        eligible=len(rows);lessons_n=len(lessons);ideas_n=len(ideas);contexts_n=len(groups)
        status={'status':'OK','contract':CONTRACT,'refreshed_at':stamp,'eligible_closed':eligible,
                'new_lessons':inserted,'lessons_written':lessons_n,'unique_market_episodes':ideas_n,
                'contexts_total':contexts_n,'actionable_contexts':actionable,'validated_rules':validated,
                'applications':apps}
        c.execute("""INSERT INTO v86_learning_refresh
           (singleton,refreshed_at,eligible_closed,lessons_written,unique_market_ideas,contexts_total,
            actionable_contexts,validated_rules,applications,payload)
           VALUES(1,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(singleton) DO UPDATE SET
             refreshed_at=excluded.refreshed_at,eligible_closed=excluded.eligible_closed,
             lessons_written=excluded.lessons_written,unique_market_ideas=excluded.unique_market_ideas,
             contexts_total=excluded.contexts_total,actionable_contexts=excluded.actionable_contexts,
             validated_rules=excluded.validated_rules,applications=excluded.applications,payload=excluded.payload""",
          (stamp,eligible,lessons_n,ideas_n,contexts_n,actionable,validated,apps,
           json.dumps(status,ensure_ascii=False,separators=(',',':'))))
        return status

def _profile_from_row(row):
    return {
      'context_key':str(row['context_key']),'scope':str(row['scope']),
      'unique_ideas':_i(row['unique_ideas']),'wins':_i(row['wins']),'losses':_i(row['losses']),
      'posterior_mean':_f(row['posterior_mean'],0.5),'posterior_low':_f(row['posterior_low'],0.0),
      'posterior_high':_f(row['posterior_high'],1.0),
      'direction_failure_rate':_f(row['direction_failure_rate'],0.0),
      'execution_failure_rate':_f(row['execution_failure_rate'],0.0),
      'scale_cap':_f(row['scale_cap'],0.25),'no_scale':bool(_i(row['no_scale'])),
      'entry_policy':str(row['entry_policy']),'management_policy':str(row['management_policy']),
      'authority':str(row['authority']),'validated_rule':bool(_i(row['validated_rule']))
    }

def _choose_profile(contexts,row,direction):
    a=str(row.get('asset') or '');h=str(row.get('horizon') or '')
    pp=row.get('trade_plan') or {}
    s=str(pp.get('setup') or '')
    if not s:
        mg=row.get('movement_genesis') or {}; tr=row.get('tactical_reversal') or {}; rr=row.get('range_retest_breakout') or {}
        if mg.get('eligible') and mg.get('direction')==direction:s='MOVEMENT_GENESIS'
        elif tr.get('active') and tr.get('direction')==direction:s=str(tr.get('setup') or 'TACTICAL_REVERSAL')
        elif rr.get('active') and rr.get('direction')==direction:s=str(rr.get('setup') or 'RANGE_RETEST_BREAKOUT')
    s=s or 'UNCLASSIFIED';r=str(row.get('regime') or 'UNKNOWN')
    candidates=[
      _context_key('EXACT',a,h,s,r,direction),
      _context_key('SETUP_REGIME','*',h,s,r,direction),
      _context_key('ASSET_HORIZON',a,h,'*','*',direction),
      _context_key('ASSET_DIRECTION',a,'*','*','*',direction),
      _context_key('SETUP_FAMILY','*','*',s,'*',direction),
    ]
    mins={'EXACT':3,'SETUP_REGIME':4,'ASSET_HORIZON':4,'ASSET_DIRECTION':3,'SETUP_FAMILY':5}
    for key in candidates:
        x=contexts.get(key)
        if x and x['unique_ideas']>=mins.get(x['scope'],99):
            return {**x,'match_candidates':candidates}
    return {'context_key':candidates[0],'scope':'EXACT','unique_ideas':0,'wins':0,'losses':0,
            'posterior_mean':0.5,'posterior_low':0.0,'posterior_high':1.0,
            'direction_failure_rate':0.0,'execution_failure_rate':0.0,
            'scale_cap':0.25,'no_scale':False,'entry_policy':'SIGNAL_FIRST_PROBE',
            'management_policy':'BASELINE','authority':'OBSERVE_ONLY','validated_rule':False,
            'match_candidates':candidates}

def apply_learning_to_summary(ledger,summary,at=None):
    refresh=refresh_learning_state(ledger,at)
    with ledger.read() as c:
        rows=c.execute("""SELECT context_key,scope,unique_ideas,wins,losses,posterior_mean,posterior_low,posterior_high,
                         direction_failure_rate,execution_failure_rate,scale_cap,no_scale,entry_policy,
                         management_policy,authority,validated_rule FROM v86_learning_contexts""").fetchall()
    contexts={str(r['context_key']):_profile_from_row(dict(r)) for r in rows}
    applied=0;directional=0;asset_direction_applied=0;directional_debug=[]
    actionable_debug=[
      {'context_key':k,'scope':v.get('scope'),'authority':v.get('authority'),'unique_ideas':v.get('unique_ideas'),
       'wins':v.get('wins'),'losses':v.get('losses')}
      for k,v in contexts.items() if v.get('authority')!='OBSERVE_ONLY'
    ][:12]
    for row in summary:
        d=str(row.get('research_decision') or row.get('decision') or 'NO_TRADE')
        if d not in ('LONG','SHORT'):
            row['closed_loop_learning']={'authority':'OBSERVE_ONLY','reason':'NO_DIRECTION'}
            continue
        directional+=1
        p=_choose_profile(contexts,row,d)
        row['closed_loop_learning']={**p,'contract':CONTRACT,
          'decision_influence':p['authority']!='OBSERVE_ONLY',
          'principle':'direction remains owned by signal model; learning controls horizon preference, scale and profit protection'}
        directional_debug.append({
          'asset':str(row.get('asset') or ''),'horizon':str(row.get('horizon') or ''),'direction':d,
          'setup':str((row.get('trade_plan') or {}).get('setup') or ''),
          'regime':str(row.get('regime') or ''),
          'authority':p.get('authority'),'chosen_context':p.get('context_key'),
          'candidate_contexts':p.get('match_candidates') or []
        })
        if p['authority']!='OBSERVE_ONLY':
            applied+=1
            if p.get('scope')=='ASSET_DIRECTION':asset_direction_applied+=1
    return summary,{**refresh,'directional_rows':directional,'directional_rows_with_learning':applied,
                    'asset_direction_fallback_rows':asset_direction_applied,
                    'learning_match_rate':(applied/directional if directional else 0.0),
                    'actionable_context_detail':actionable_debug,
                    'directional_learning_debug':directional_debug[:12]}

def record_learning_applications(ledger,summary,routing,at=None):
    at=at or _now();stamp=_iso(at); routed=set()
    for tr in routing or []:
        if str(tr.get('status') or '').startswith('ROUTED'):
            routed.add((str(tr.get('asset') or ''),str(tr.get('horizon') or '')))
    inserted=0
    with ledger.transaction() as c:
        ensure_schema(c)
        for row in summary:
            key=(str(row.get('asset') or ''),str(row.get('horizon') or ''))
            lp=row.get('closed_loop_learning') or {}
            if key not in routed or lp.get('authority') in (None,'OBSERVE_ONLY'): continue
            did=str(row.get('decision_id') or (key[0]+':'+key[1]+':'+str(row.get('observed_at') or row.get('at') or stamp)))
            appid='LAPP_'+_hash(did,lp.get('context_key'))
            before=c.execute('SELECT 1 FROM v86_learning_applications WHERE application_id=?',(appid,)).fetchone()
            if before: continue
            payload={'contract':CONTRACT,'decision_id':did,'context':lp,
                     'asset':key[0],'horizon':key[1],
                     'direction':row.get('research_decision') or row.get('decision')}
            c.execute("""INSERT INTO v86_learning_applications
              (application_id,decision_id,context_key,asset,horizon,direction,authority,applied_at,payload)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(application_id) DO NOTHING""",
              (appid,did,str(lp.get('context_key') or ''),key[0],key[1],str(payload['direction']),
               str(lp.get('authority')),stamp,json.dumps(payload,ensure_ascii=False,separators=(',',':'))))
            inserted+=1
        apps=_i(c.execute('SELECT COUNT(*) AS n FROM v86_learning_applications').fetchone()['n'])
        c.execute('UPDATE v86_learning_refresh SET applications=? WHERE singleton=1',(apps,))
    return {'status':'OK','new_applications':inserted,'applications':apps}

def learning_status(ledger):
    with ledger.transaction() as c:
        ensure_schema(c)
        row=c.execute('SELECT * FROM v86_learning_refresh WHERE singleton=1').fetchone()
        if row:
            out=dict(row);out['payload']=_obj(out.get('payload'))
        else:
            out={'status':'BUILDING','contract':CONTRACT,'eligible_closed':0,'lessons_written':0,
                 'unique_market_ideas':0,'contexts_total':0,'actionable_contexts':0,'validated_rules':0,'applications':0}
        try: out['legacy_v85_lessons']=_i(c.execute('SELECT COUNT(*) AS n FROM v85_lessons').fetchone()['n'])
        except Exception: out['legacy_v85_lessons']=None
        top=[dict(r) for r in c.execute("""SELECT context_key,scope,unique_ideas,wins,losses,posterior_mean,
                  direction_failure_rate,execution_failure_rate,scale_cap,entry_policy,management_policy,authority,validated_rule
                  FROM v86_learning_contexts
                  WHERE authority<>'OBSERVE_ONLY'
                  ORDER BY validated_rule DESC,unique_ideas DESC,updated_at DESC LIMIT 12""").fetchall()]
        out['top_contexts']=top
        out['status']='OK';out['contract']=CONTRACT
        return out
'''
(root/'veritas_v86/closed_loop_learning.py').write_text(module,encoding='utf-8')

# Integrate closed-loop annotation around the final movement-memory commit_cycle.
p=root/'veritas_v86/application.py'
s=p.read_text(encoding='utf-8')
imp="from .performance import compare,persist_gate\n"
imp_new=imp+"from .closed_loop_learning import apply_learning_to_summary,refresh_learning_state,record_learning_applications,learning_status\n"
if 'from .closed_loop_learning import ' not in s:
    if imp not in s: raise SystemExit('CLOSED_LOOP_APP_IMPORT_ANCHOR_NOT_FOUND')
    s=s.replace(imp,imp_new,1)

old="""    def commit_cycle(self,summary,bundles,cycle_id,clock_ok=True):
        summary=apply_movement_state_memory(self.ledger,summary,now())
        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
"""
new="""    def commit_cycle(self,summary,bundles,cycle_id,clock_ok=True):
        summary=apply_movement_state_memory(self.ledger,summary,now())
        learning_pre={'status':'UNAVAILABLE'}
        try:
            summary,learning_pre=apply_learning_to_summary(self.ledger,summary,now())
            if learning_pre.get('new_lessons') or (learning_pre.get('lessons_written') and not getattr(self,'_closed_loop_reported',False)):
                print(canonical_json({'event':'V86_CLOSED_LOOP_LEARNING','phase':'pre','status':'OK',**learning_pre}),flush=True)
                self._closed_loop_reported=True
        except Exception as exc:
            learning_pre={'status':'ERROR','error':type(exc).__name__+': '+str(exc)[:180]}
            print(canonical_json({'event':'V86_CLOSED_LOOP_LEARNING','phase':'pre',**learning_pre}),flush=True)
        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
        try:
            app_result=record_learning_applications(self.ledger,summary,out.get('routing') or [],now())
            learning_post=refresh_learning_state(self.ledger,now())
            out['closed_loop_learning']={'pre':learning_pre,'applications':app_result,'post':learning_post}
            if learning_post.get('new_lessons') or app_result.get('new_applications'):
                print(canonical_json({'event':'V86_CLOSED_LOOP_LEARNING','phase':'post',
                                      'new_lessons':learning_post.get('new_lessons',0),
                                      'lessons_written':learning_post.get('lessons_written',0),
                                      'unique_market_episodes':learning_post.get('unique_market_episodes',0),
                                      'actionable_contexts':learning_post.get('actionable_contexts',0),
                                      'validated_rules':learning_post.get('validated_rules',0),
                                      'new_applications':app_result.get('new_applications',0),
                                      'applications':app_result.get('applications',0)}),flush=True)
        except Exception as exc:
            out['closed_loop_learning']={'pre':learning_pre,'post':{'status':'ERROR','error':type(exc).__name__+': '+str(exc)[:180]}}
            print(canonical_json({'event':'V86_CLOSED_LOOP_LEARNING','phase':'post','status':'ERROR',
                                  'error':type(exc).__name__+': '+str(exc)[:180]}),flush=True)
"""
if old not in s: raise SystemExit('CLOSED_LOOP_COMMIT_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Routing: learning can never reverse the signal; it chooses among already-executable horizons,
# caps validated scale-in, and carries an execution-management policy into the Signal.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
old="""def winrate_repair_gate(row,direction):
    pp=row.get('trade_plan') or {}
"""
new="""def winrate_repair_gate(row,direction):
    learn=row.get('closed_loop_learning') or {}
    pp=row.get('trade_plan') or {}
"""
if old not in s: raise SystemExit('CLOSED_LOOP_GATE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""        max_fraction=D(str(mg.get('target_fraction') or '.05'))
        return {'eligible':True,'reason':'MOVEMENT_GENESIS_'+str(mg.get('state')),
                'max_fraction':max_fraction,'movement':True,'movement_state':mg.get('state'),
                'movement_score':mg.get('score'),'net_rr':mg.get('net_rr'),
                'independent':mg.get('confirmation_count'),'historical_edge_gate':'FAIL'}
"""
new="""        max_fraction=D(str(mg.get('target_fraction') or '.05'))
        learn_cap=learn.get('scale_cap')
        if learn_cap is not None:
            try:max_fraction=min(max_fraction,D(str(learn_cap)))
            except Exception:pass
        return {'eligible':True,'reason':'MOVEMENT_GENESIS_'+str(mg.get('state')),
                'max_fraction':max_fraction,'movement':True,'movement_state':mg.get('state'),
                'movement_score':mg.get('score'),'net_rr':mg.get('net_rr'),
                'independent':mg.get('confirmation_count'),'historical_edge_gate':'FAIL',
                'learning_authority':learn.get('authority'),'learning_context_key':learn.get('context_key'),
                'learning_scale_cap':learn.get('scale_cap'),'learning_no_scale':bool(learn.get('no_scale'))}
"""
if old not in s: raise SystemExit('CLOSED_LOOP_MOVEMENT_GATE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="        r,w,p,gate=executable[0]\n"
new="""        # Closed-loop learning may prefer a better observed horizon, but only among horizons
        # that have already passed source, hard-risk and net-economics gates.
        chosen=0;best=-999.0
        for ii,item in enumerate(executable):
            lp=item[0].get('closed_loop_learning') or {}
            if lp.get('authority') in (None,'OBSERVE_ONLY'): continue
            score=(float(lp.get('posterior_mean') or .5)-.5
                   -0.30*float(lp.get('direction_failure_rate') or 0.0)
                   -0.12*float(lp.get('execution_failure_rate') or 0.0)
                   -0.03*ii)
            if score>best: best=score;chosen=ii
        if best>-0.20:
            r,w,p,gate=executable[chosen]
        else:
            r,w,p,gate=executable[0]
"""
if old not in s: raise SystemExit('CLOSED_LOOP_HORIZON_SELECTION_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""        movement_state=str((r.get('movement_genesis') or {}).get('state') or 'IDLE')
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION'))
        tp_raw=p.get('tactical_target_price') or p.get('target_price')
"""
new="""        movement_state=str((r.get('movement_genesis') or {}).get('state') or 'IDLE')
        learning_profile=r.get('closed_loop_learning') or {}
        learning_cap=fnum(learning_profile.get('scale_cap'),.25)
        if learning_cap>0:
            desired=min(desired,D(str(learning_cap)))
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION')
                           and not learning_profile.get('no_scale') and learning_cap>=.10)
        tp_raw=p.get('tactical_target_price') or p.get('target_price')
"""
if old not in s: raise SystemExit('CLOSED_LOOP_SIGNAL_SCALE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""                          entry_probability=D(str(prob)),probability_source=prob_source,
                          movement_state=movement_state,take_profit_price=take_profit)
"""
new="""                          entry_probability=D(str(prob)),probability_source=prob_source,
                          movement_state=movement_state,take_profit_price=take_profit,
                          learning_management_policy=str(learning_profile.get('management_policy') or 'BASELINE'),
                          learning_authority=str(learning_profile.get('authority') or 'OBSERVE_ONLY'),
                          learning_context_key=str(learning_profile.get('context_key') or ''))
"""
if old not in s: raise SystemExit('CLOSED_LOOP_SIGNAL_ARGS_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Signal carries only bounded learning metadata; direction remains immutable upstream.
p=root/'veritas_v85/domain.py'
s=p.read_text(encoding='utf-8')
old="""    take_profit_price: Decimal | None = None

    def __post_init__(self) -> None:
"""
new="""    take_profit_price: Decimal | None = None
    learning_management_policy: str | None = None
    learning_authority: str | None = None
    learning_context_key: str | None = None

    def __post_init__(self) -> None:
"""
if old not in s: raise SystemExit('CLOSED_LOOP_SIGNAL_DOMAIN_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Apply the execution lesson: when prior trades moved favorably but were not monetized,
# lock profit earlier. Hard stops/thesis exits remain senior and unchanged.
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')
old="""                    stage=(signal.movement_state if signal is not None else None)
                    harvest_ratio=(D('.60') if stage=='ACCELERATION' else D('.45') if stage=='CONFIRMED' else D('.40'))
                    giveback_trigger=max(D('.0015'),mfe*harvest_ratio)
"""
new="""                    stage=(signal.movement_state if signal is not None else None)
                    learning_policy=(getattr(signal,'learning_management_policy',None) if signal is not None else None)
                    if learning_policy=='PROFIT_PROTECT_EARLIER':
                        harvest_ratio=(D('.42') if stage=='ACCELERATION' else D('.34') if stage=='CONFIRMED' else D('.30'))
                    else:
                        harvest_ratio=(D('.60') if stage=='ACCELERATION' else D('.45') if stage=='CONFIRMED' else D('.40'))
                    giveback_trigger=max(D('.0015'),mfe*harvest_ratio)
"""
if old not in s: raise SystemExit('CLOSED_LOOP_PROFIT_PROTECT_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Read-only audit endpoint for the product UI and verification.
p=root/'veritas_v85/application.py'
s=p.read_text(encoding='utf-8')
anchor="""            if path=='/api/v1/closed-trade-ledger':
"""
route="""            if path=='/api/v1/learning-status':
                try:
                    from veritas_v86.closed_loop_learning import learning_status
                    return self.reply(learning_status(app.ledger),200)
                except Exception as exc:
                    return self.reply({'status':'UNAVAILABLE','error':type(exc).__name__+': '+str(exc)[:160]},503)
            if path=='/api/v1/closed-trade-ledger':
"""
if "/api/v1/learning-status" not in s:
    if anchor not in s: raise SystemExit('CLOSED_LOOP_ENDPOINT_ANCHOR_NOT_FOUND')
    s=s.replace(anchor,route,1)
p.write_text(s,encoding='utf-8')

print('V86_CLOSED_LOOP_LEARNING_PATCH_ACTIVE')
