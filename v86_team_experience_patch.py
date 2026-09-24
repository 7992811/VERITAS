from __future__ import annotations
from pathlib import Path
import json, sys

root=Path(sys.argv[1]).resolve()
seed_src=root.parent/'veritas_team_experience_seed_v1.json'
if not seed_src.is_file():
    raise SystemExit('TEAM_EXPERIENCE_SEED_MISSING')
seed_text=seed_src.read_text(encoding='utf-8')
(root/'veritas_v86/team_experience_seed_v1.json').write_text(seed_text,encoding='utf-8')

module = r'''from __future__ import annotations
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,math

CONTRACT='V86_TEAM_EXPERIENCE_V1'
SEED=Path(__file__).resolve().parent/'team_experience_seed_v1.json'

def _now():
    return datetime.now(timezone.utc)

def _iso(x=None):
    return (x or _now()).isoformat()

def _obj(x):
    if isinstance(x,dict): return x
    if isinstance(x,str):
        try:
            y=json.loads(x); return y if isinstance(y,dict) else {}
        except Exception:return {}
    return {}

def _list(x):
    if isinstance(x,list): return x
    if isinstance(x,str):
        try:
            y=json.loads(x); return y if isinstance(y,list) else []
        except Exception:return []
    return []

def _f(x,default=None):
    try:return float(x)
    except Exception:return default

def _i(x,default=0):
    try:return int(x)
    except Exception:return default

def _hash(*parts):
    return hashlib.sha256('|'.join(str(x) for x in parts).encode()).hexdigest()[:32]

def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS v86_team_experience_cards(
      card_id TEXT PRIMARY KEY,
      source_kind TEXT NOT NULL,
      author_role TEXT NOT NULL,
      category TEXT NOT NULL,
      asset TEXT NOT NULL,
      horizon TEXT NOT NULL,
      regime TEXT NOT NULL,
      direction TEXT NOT NULL,
      setup_family TEXT NOT NULL,
      source_text TEXT NOT NULL,
      conditions TEXT NOT NULL,
      rule_payload TEXT NOT NULL,
      exceptions TEXT NOT NULL,
      error_criterion TEXT NOT NULL,
      direct_authority INTEGER NOT NULL,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      payload TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS v86_team_experience_validation(
      card_id TEXT PRIMARY KEY,
      unique_market_ideas INTEGER NOT NULL,
      wins INTEGER NOT NULL,
      losses INTEGER NOT NULL,
      posterior_mean TEXT NOT NULL,
      evidence_status TEXT NOT NULL,
      last_refreshed_at TEXT NOT NULL,
      payload TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS v86_team_experience_applications(
      application_id TEXT PRIMARY KEY,
      decision_id TEXT NOT NULL,
      card_id TEXT NOT NULL,
      asset TEXT NOT NULL,
      horizon TEXT NOT NULL,
      direction TEXT NOT NULL,
      setup_family TEXT NOT NULL,
      routed INTEGER NOT NULL,
      applied_at TEXT NOT NULL,
      payload TEXT NOT NULL
    )""")

def seed_team_experience(ledger):
    raw=json.loads(SEED.read_text(encoding='utf-8'))
    cards=raw.get('cards') or []; inserted=existing=0; stamp=_iso()
    with ledger.transaction() as c:
        ensure_schema(c)
        for card in cards:
            cid=str(card.get('card_id') or '')
            if not cid: continue
            prior=c.execute('SELECT 1 FROM v86_team_experience_cards WHERE card_id=?',(cid,)).fetchone()
            if prior:
                existing+=1; continue
            category=str(card.get('category') or 'MARKET_HYPOTHESIS')
            status='ACTIVE_POLICY' if category=='MANAGEMENT_CONSTRAINT' and bool(card.get('direct_authority')) else 'SHADOW_PRIOR'
            c.execute("""INSERT INTO v86_team_experience_cards(
              card_id,source_kind,author_role,category,asset,horizon,regime,direction,setup_family,
              source_text,conditions,rule_payload,exceptions,error_criterion,direct_authority,status,
              created_at,updated_at,payload)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (cid,str(card.get('source_kind') or 'PROJECT_TEAM_FEEDBACK'),
               str(card.get('author_role') or 'TEAM'),category,
               str(card.get('asset') or '*'),str(card.get('horizon') or '*'),
               str(card.get('regime') or '*'),str(card.get('direction') or '*'),
               str(card.get('setup_family') or '*'),str(card.get('source_text') or ''),
               json.dumps(card.get('conditions') or {},ensure_ascii=False,separators=(',',':')),
               json.dumps(card.get('rule') or {},ensure_ascii=False,separators=(',',':')),
               json.dumps(card.get('exceptions') or [],ensure_ascii=False,separators=(',',':')),
               str(card.get('error_criterion') or ''),1 if card.get('direct_authority') else 0,
               status,stamp,stamp,json.dumps(card,ensure_ascii=False,separators=(',',':'))))
            inserted+=1
    return {'status':'OK','contract':CONTRACT,'inserted':inserted,'existing':existing,'cards_total':len(cards)}

def _pattern(value,pattern):
    p=str(pattern or '*'); v=str(value or '')
    if p in ('','*'): return True
    if p.endswith('*'): return v.startswith(p[:-1])
    return v==p

def _posterior(wins,n):
    a=2.0+wins; b=2.0+n-wins
    return a/(a+b)

def _lesson_matches(card,lesson):
    if not _pattern(lesson.get('asset'),card.get('asset')): return False
    if not _pattern(lesson.get('horizon'),card.get('horizon')): return False
    if not _pattern(lesson.get('direction'),card.get('direction')): return False
    if not _pattern(lesson.get('setup_family'),card.get('setup_family')): return False
    if not _pattern(lesson.get('regime'),card.get('regime')): return False
    return True

def refresh_team_validation(ledger,at=None):
    stamp=_iso(at)
    with ledger.transaction() as c:
        ensure_schema(c)
        cards=[dict(r) for r in c.execute('SELECT * FROM v86_team_experience_cards ORDER BY card_id').fetchall()]
        try:
            lessons=[dict(r) for r in c.execute("""SELECT idea_id,asset,horizon,setup_family,regime,direction,win
                 FROM v86_closed_loop_lessons""").fetchall()]
        except Exception:
            lessons=[]
        for card in cards:
            grouped={}
            for l in lessons:
                if _lesson_matches(card,l):
                    grouped.setdefault(str(l.get('idea_id') or ''),[]).append(l)
            wins=0
            for grp in grouped.values():
                wins += 1 if sum(_i(x.get('win')) for x in grp)*2>=len(grp) else 0
            n=len(grouped); losses=n-wins; mean=_posterior(wins,n)
            if card.get('category')=='MANAGEMENT_CONSTRAINT' and bool(_i(card.get('direct_authority'))):
                ev='ACTIVE_POLICY'
            elif n<20:
                ev='EVIDENCE_BUILDING'
            elif mean>=0.56:
                ev='OOS_CANDIDATE_POSITIVE'
            elif mean<=0.44:
                ev='OOS_CANDIDATE_NEGATIVE'
            else:
                ev='INCONCLUSIVE'
            payload={'contract':CONTRACT,'card_id':card['card_id'],'unique_market_ideas':n,
                     'wins':wins,'losses':losses,'posterior_mean':mean,'evidence_status':ev,
                     'promotion_rule':'market hypotheses remain non-authoritative until independent OOS/VAULT validation'}
            c.execute("""INSERT INTO v86_team_experience_validation(
              card_id,unique_market_ideas,wins,losses,posterior_mean,evidence_status,last_refreshed_at,payload)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(card_id) DO UPDATE SET
              unique_market_ideas=excluded.unique_market_ideas,wins=excluded.wins,losses=excluded.losses,
              posterior_mean=excluded.posterior_mean,evidence_status=excluded.evidence_status,
              last_refreshed_at=excluded.last_refreshed_at,payload=excluded.payload""",
              (card['card_id'],n,wins,losses,str(mean),ev,stamp,
               json.dumps(payload,ensure_ascii=False,separators=(',',':'))))
        return {'status':'OK','cards':len(cards),'lessons_seen':len(lessons),'refreshed_at':stamp}

def _infer_setup(row,direction):
    pp=row.get('trade_plan') if isinstance(row.get('trade_plan'),dict) else {}
    existing=str(pp.get('setup') or row.get('setup_family') or '')
    if existing:return existing
    mg=row.get('movement_genesis') if isinstance(row.get('movement_genesis'),dict) else {}
    if mg.get('eligible') and mg.get('direction')==direction:return 'MOVEMENT_GENESIS'
    tr=row.get('tactical_reversal') if isinstance(row.get('tactical_reversal'),dict) else {}
    if tr.get('active') and tr.get('direction')==direction:return str(tr.get('setup') or 'TACTICAL_REVERSAL')
    rr=row.get('range_retest_breakout') if isinstance(row.get('range_retest_breakout'),dict) else {}
    if rr.get('active') and rr.get('direction')==direction:return str(rr.get('setup') or 'RANGE_RETEST_BREAKOUT')
    hs=row.get('horizon_structure') if isinstance(row.get('horizon_structure'),dict) else {}
    st=str(hs.get('state') or '').upper()
    if hs.get('direction')==direction and ('TREND' in st or st in ('CONTINUATION','BREAKOUT')):
        return 'TREND'
    inst=row.get('institutional_signal') if isinstance(row.get('institutional_signal'),dict) else {}
    bq=inst.get('breakout_quality') if isinstance(inst.get('breakout_quality'),dict) else {}
    if bq.get('is_breakout') and bq.get('direction')==direction:
        return 'BREAKOUT'
    return 'UNCLASSIFIED'

def _late(row):
    pp=row.get('trade_plan') if isinstance(row.get('trade_plan'),dict) else {}
    st=row.get('intraday_structure') if isinstance(row.get('intraday_structure'),dict) else {}
    eq=str(pp.get('entry_quality') or row.get('entry_quality') or st.get('entry_quality') or '').upper()
    return bool(pp.get('late_entry') or st.get('late_entry') or 'LATE' in eq or 'EXTENDED' in eq)

def _card_matches_live(card,row,direction,setup):
    if not _pattern(row.get('asset'),card.get('asset')): return False
    if not _pattern(row.get('horizon'),card.get('horizon')): return False
    if not _pattern(direction,card.get('direction')): return False
    if not _pattern(row.get('regime'),card.get('regime')): return False
    if not _pattern(setup,card.get('setup_family')): return False
    cond=_obj(card.get('conditions'))
    if cond.get('late_entry') is True and not _late(row): return False
    if cond.get('directional_signal_required') is True and direction not in ('LONG','SHORT'): return False
    if cond.get('volume_confirmation') is True:
        st=row.get('intraday_structure') if isinstance(row.get('intraday_structure'),dict) else {}
        inst=row.get('institutional_signal') if isinstance(row.get('institutional_signal'),dict) else {}
        bq=inst.get('breakout_quality') if isinstance(inst.get('breakout_quality'),dict) else {}
        if not (st.get('volume_confirmed') or _f(bq.get('volume_ratio'),0.0)>=1.0): return False
    if cond.get('trend_day') is True:
        hs=row.get('horizon_structure') if isinstance(row.get('horizon_structure'),dict) else {}
        if not ('TREND' in str(hs.get('state') or '').upper() or str(row.get('trend_phase') or '').upper()=='TREND_DAY'): return False
    if cond.get('range_regime') is True and not str(row.get('regime') or '').startswith('RANGE'): return False
    if cond.get('recent_stopouts_same_asset_min'):
        # Only act when upstream explicitly supplies the count; never infer a hard pause from incomplete state.
        if _i(row.get('recent_stopouts_same_asset'),0)<_i(cond.get('recent_stopouts_same_asset_min')): return False
    return True

def apply_team_experience(ledger,summary,at=None):
    seed_team_experience(ledger)
    refresh_team_validation(ledger,at)
    with ledger.read() as c:
        cards=[dict(r) for r in c.execute("""SELECT card_id,source_kind,author_role,category,asset,horizon,regime,direction,
              setup_family,source_text,conditions,rule_payload,exceptions,error_criterion,direct_authority,status
              FROM v86_team_experience_cards ORDER BY card_id""").fetchall()]
        vals={str(r['card_id']):dict(r) for r in c.execute('SELECT * FROM v86_team_experience_validation').fetchall()}
    matched_rows=matched_cards=active_constraints=0
    for row in summary:
        d=str(row.get('research_decision') or row.get('decision') or 'NO_TRADE')
        if d not in ('LONG','SHORT'):
            row['team_experience']={'contract':CONTRACT,'matches':[],'decision_influence':False}
            continue
        setup=_infer_setup(row,d); matches=[]; constraints={}
        for card in cards:
            if not _card_matches_live(card,row,d,setup): continue
            val=vals.get(str(card['card_id'])) or {}
            rule=_obj(card.get('rule_payload'))
            item={'card_id':card['card_id'],'category':card['category'],'author_role':card['author_role'],
                  'setup_family':card['setup_family'],'source_text':card['source_text'],
                  'evidence_status':val.get('evidence_status') or card.get('status'),
                  'unique_market_ideas':_i(val.get('unique_market_ideas')),
                  'wins':_i(val.get('wins')),'losses':_i(val.get('losses')),
                  'posterior_mean':_f(val.get('posterior_mean')),'direct_authority':bool(_i(card.get('direct_authority')))}
            matches.append(item); matched_cards+=1
            if bool(_i(card.get('direct_authority'))) and card.get('category')=='MANAGEMENT_CONSTRAINT':
                if rule.get('max_fraction') is not None:
                    cap=_f(rule.get('max_fraction'))
                    if cap is not None: constraints['max_fraction']=min(_f(constraints.get('max_fraction'),1.0),cap)
                if rule.get('no_scale'): constraints['no_scale']=True
                if rule.get('probe_fraction') is not None: constraints['safe_probe_fraction']=_f(rule.get('probe_fraction'))
                if rule.get('action'): constraints.setdefault('active_policies',[]).append(str(rule.get('action')))
        if matches: matched_rows+=1
        if constraints: active_constraints+=1
        row['team_experience_setup_hint']=setup
        row['team_experience']={'contract':CONTRACT,'matches':matches,'constraints':constraints,
          'decision_influence':bool(constraints),
          'principle':'team market hypotheses are priors; only explicit management constraints may act before OOS/VAULT validation'}
    return summary,{'status':'OK','contract':CONTRACT,'directional_rows':sum(1 for r in summary if str(r.get('research_decision') or r.get('decision')) in ('LONG','SHORT')),
                    'matched_rows':matched_rows,'matched_cards':matched_cards,'active_constraint_rows':active_constraints,
                    'cards_total':len(cards)}

def record_team_experience_applications(ledger,summary,routing,at=None):
    stamp=_iso(at); routed=set()
    for tr in routing or []:
        if str(tr.get('status') or '').startswith('ROUTED'):
            routed.add((str(tr.get('asset') or ''),str(tr.get('horizon') or '')))
    inserted=0
    with ledger.transaction() as c:
        ensure_schema(c)
        for row in summary:
            a=str(row.get('asset') or ''); h=str(row.get('horizon') or '')
            d=str(row.get('research_decision') or row.get('decision') or 'NO_TRADE')
            tx=row.get('team_experience') if isinstance(row.get('team_experience'),dict) else {}
            setup=str(row.get('team_experience_setup_hint') or 'UNCLASSIFIED')
            for m in tx.get('matches') or []:
                cid=str(m.get('card_id') or '')
                if not cid: continue
                did=str(row.get('decision_id') or row.get('observed_at') or row.get('at') or stamp)
                appid='TAPP_'+_hash(cid,a,h,d,did)
                payload={'contract':CONTRACT,'card':m,'asset':a,'horizon':h,'direction':d,'setup_family':setup,
                         'routed':(a,h) in routed,'constraints':tx.get('constraints') or {}}
                before=c.execute('SELECT 1 FROM v86_team_experience_applications WHERE application_id=?',(appid,)).fetchone()
                if before: continue
                c.execute("""INSERT INTO v86_team_experience_applications(
                  application_id,decision_id,card_id,asset,horizon,direction,setup_family,routed,applied_at,payload)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (appid,did,cid,a,h,d,setup,1 if (a,h) in routed else 0,stamp,
                   json.dumps(payload,ensure_ascii=False,separators=(',',':'))))
                inserted+=1
        total=_i(c.execute('SELECT COUNT(*) n FROM v86_team_experience_applications').fetchone()['n'])
        routed_n=_i(c.execute('SELECT COUNT(*) n FROM v86_team_experience_applications WHERE routed=1').fetchone()['n'])
    return {'status':'OK','new_applications':inserted,'applications':total,'routed_applications':routed_n}

def team_experience_status(ledger):
    seed=seed_team_experience(ledger); refresh_team_validation(ledger)
    with ledger.read() as c:
        cards=[]
        rows=c.execute("""SELECT c.card_id,c.source_kind,c.author_role,c.category,c.asset,c.horizon,c.regime,c.direction,
             c.setup_family,c.source_text,c.error_criterion,c.direct_authority,c.status,
             v.unique_market_ideas,v.wins,v.losses,v.posterior_mean,v.evidence_status,v.last_refreshed_at
             FROM v86_team_experience_cards c
             LEFT JOIN v86_team_experience_validation v ON v.card_id=c.card_id
             ORDER BY c.category,c.card_id""").fetchall()
        for r in rows:
            d=dict(r); d['direct_authority']=bool(_i(d.get('direct_authority'))); d['posterior_mean']=_f(d.get('posterior_mean'))
            cards.append(d)
        apps=_i(c.execute('SELECT COUNT(*) n FROM v86_team_experience_applications').fetchone()['n'])
        routed=_i(c.execute('SELECT COUNT(*) n FROM v86_team_experience_applications WHERE routed=1').fetchone()['n'])
    return {'status':'OK','contract':CONTRACT,'cards_total':len(cards),
            'management_constraints':sum(1 for x in cards if x.get('category')=='MANAGEMENT_CONSTRAINT'),
            'market_hypotheses':sum(1 for x in cards if x.get('category')=='MARKET_HYPOTHESIS'),
            'applications':apps,'routed_applications':routed,'seed':seed,'validation':refresh,'cards':cards}
'''
(root/'veritas_v86/team_experience.py').write_text(module,encoding='utf-8')

# Integrate before closed-loop matching so team taxonomy can enrich setup classification.
p=root/'veritas_v86/application.py'
s=p.read_text(encoding='utf-8')
imp="from .closed_loop_learning import apply_learning_to_summary,refresh_learning_state,record_learning_applications,learning_status\n"
if "from .team_experience import " not in s:
    if imp not in s: raise SystemExit('TEAM_APP_IMPORT_ANCHOR_NOT_FOUND')
    s=s.replace(imp,imp+"from .team_experience import apply_team_experience,record_team_experience_applications,team_experience_status,seed_team_experience\n",1)

old="""        learning_pre={'status':'UNAVAILABLE'}
        try:
            summary,learning_pre=apply_learning_to_summary(self.ledger,summary,now())
"""
new="""        team_pre={'status':'UNAVAILABLE'}
        try:
            summary,team_pre=apply_team_experience(self.ledger,summary,now())
            if not getattr(self,'_team_experience_reported',False):
                print(canonical_json({'event':'V86_TEAM_EXPERIENCE','phase':'pre',**team_pre}),flush=True)
                self._team_experience_reported=True
        except Exception as exc:
            team_pre={'status':'ERROR','error':type(exc).__name__+': '+str(exc)[:180]}
            print(canonical_json({'event':'V86_TEAM_EXPERIENCE','phase':'pre',**team_pre}),flush=True)
        learning_pre={'status':'UNAVAILABLE'}
        try:
            summary,learning_pre=apply_learning_to_summary(self.ledger,summary,now())
"""
if old not in s: raise SystemExit('TEAM_PRE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
        try:
            app_result=record_learning_applications(self.ledger,summary,out.get('routing') or [],now())
"""
new="""        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
        try:
            team_apps=record_team_experience_applications(self.ledger,summary,out.get('routing') or [],now())
            out['team_experience']={'pre':team_pre,'applications':team_apps}
            if team_apps.get('new_applications'):
                print(canonical_json({'event':'V86_TEAM_EXPERIENCE','phase':'post',**team_apps}),flush=True)
        except Exception as exc:
            out['team_experience']={'pre':team_pre,'applications':{'status':'ERROR','error':type(exc).__name__+': '+str(exc)[:180]}}
        try:
            app_result=record_learning_applications(self.ledger,summary,out.get('routing') or [],now())
"""
if old not in s: raise SystemExit('TEAM_POST_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Team setup hint participates in matching, but never owns direction.
p=root/'veritas_v86/closed_loop_learning.py'
s=p.read_text(encoding='utf-8')
old="    s=str(pp.get('setup') or row.get('setup_family') or '')\n"
new="    s=str(pp.get('setup') or row.get('setup_family') or row.get('team_experience_setup_hint') or '')\n"
if old not in s: raise SystemExit('TEAM_CLOSED_LOOP_SETUP_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Bounded management constraints: team experience can only reduce size / suppress scale.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
old="""        learning_profile=r.get('closed_loop_learning') or {}
        learning_cap=fnum(learning_profile.get('scale_cap'),.25)
        if learning_cap>0:
            desired=min(desired,D(str(learning_cap)))
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION')
                           and not learning_profile.get('no_scale') and learning_cap>=.10)
"""
new="""        learning_profile=r.get('closed_loop_learning') or {}
        learning_cap=fnum(learning_profile.get('scale_cap'),.25)
        if learning_cap>0:
            desired=min(desired,D(str(learning_cap)))
        team_profile=r.get('team_experience') or {}
        team_constraints=team_profile.get('constraints') or {}
        team_cap=fnum(team_constraints.get('max_fraction'),1.0)
        if team_cap>0 and team_cap<1.0:
            desired=min(desired,D(str(team_cap)))
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION')
                           and not learning_profile.get('no_scale') and learning_cap>=.10
                           and not team_constraints.get('no_scale'))
"""
if old not in s: raise SystemExit('TEAM_ROUTING_SCALE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# Read-only audit/status endpoint. No public write endpoint: prevents experience poisoning.
p=root/'veritas_v85/application.py'
s=p.read_text(encoding='utf-8')
anchor="""            if path=='/api/v1/learning-status':
"""
route="""            if path=='/api/v1/team-experience-status':
                try:
                    from veritas_v86.team_experience import team_experience_status
                    return self.reply(team_experience_status(app.ledger),200)
                except Exception as exc:
                    return self.reply({'status':'UNAVAILABLE','error':type(exc).__name__+': '+str(exc)[:160]},503)
            if path=='/api/v1/learning-status':
"""
if "/api/v1/team-experience-status" not in s:
    if anchor not in s: raise SystemExit('TEAM_STATUS_ROUTE_ANCHOR_NOT_FOUND')
    s=s.replace(anchor,route,1)
p.write_text(s,encoding='utf-8')

print('V86_TEAM_EXPERIENCE_PATCH_ACTIVE')
