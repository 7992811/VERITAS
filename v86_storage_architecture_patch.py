from __future__ import annotations
from pathlib import Path
import sys

root=Path(sys.argv[1]).resolve()

module=r'''from __future__ import annotations
from datetime import datetime, timezone
import json, os, time

REPAIR_ID='V86_STORAGE_REPAIR_V1'
CRITICAL_EVENT_TYPES={'case_lesson','outcome','setup_learning','admission_learning'}
RAW_DECISION_TYPES={'decision','meta_signal'}

def _now():
    return datetime.now(timezone.utc)

def _table_exists(cur,name):
    return bool(cur.execute("SELECT to_regclass(%s) AS r",(f'public.{name}',)).fetchone()['r'])

def _sizes(cur):
    rows=cur.execute("""SELECT relname,
        pg_total_relation_size(relid) AS bytes
        FROM pg_catalog.pg_statio_user_tables
        ORDER BY pg_total_relation_size(relid) DESC LIMIT 20""").fetchall()
    return {str(r['relname']):int(r['bytes'] or 0) for r in rows}

def _rollup_decisions_raw(cur):
    if not _table_exists(cur,'ledger_events'):
        return 0
    cur.execute("""CREATE TABLE IF NOT EXISTS v86_decision_memory(
      memory_id TEXT PRIMARY KEY,
      source_event_key TEXT NOT NULL,
      event_ts TIMESTAMPTZ NOT NULL,
      asset TEXT,
      horizon TEXT,
      decision TEXT,
      regime TEXT,
      signal_tier TEXT,
      eligible TEXT,
      reason TEXT,
      confidence TEXT,
      probability TEXT,
      payload JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )""")
    sql="""WITH src AS (
      SELECT event_key,event_ts,asset,horizon,payload,
        COALESCE(payload->>'research_decision',payload->>'decision',payload->>'direction','NO_TRADE') decision,
        COALESCE(payload->>'regime','') regime,
        COALESCE(payload->>'signal_tier','') signal_tier,
        COALESCE(payload#>>'{trade_plan,eligible}',payload->>'execution_eligible','') eligible,
        COALESCE(payload#>>'{trade_plan,reason}',payload->>'execution_reason','') reason,
        COALESCE(payload->>'confidence','') confidence,
        COALESCE(payload->>'calibrated_probability',payload->>'positive_trade_probability','') probability
      FROM ledger_events
      WHERE event_type IN ('decision','meta_signal')
    ), fp AS (
      SELECT *,
        md5(concat_ws('|',COALESCE(asset,''),COALESCE(horizon,''),decision,regime,signal_tier,eligible,reason)) fingerprint,
        lag(md5(concat_ws('|',COALESCE(asset,''),COALESCE(horizon,''),decision,regime,signal_tier,eligible,reason)))
          OVER (PARTITION BY COALESCE(asset,''),COALESCE(horizon,'') ORDER BY event_ts,event_key) prev_fp,
        row_number() OVER (
          PARTITION BY COALESCE(asset,''),COALESCE(horizon,''),
                       date_bin(interval '4 hours',event_ts,timestamptz '2000-01-01')
          ORDER BY event_ts DESC,event_key DESC) bucket_rank
      FROM src
    )
    INSERT INTO v86_decision_memory
      (memory_id,source_event_key,event_ts,asset,horizon,decision,regime,signal_tier,eligible,reason,confidence,probability,payload)
    SELECT md5(event_key),event_key,event_ts,asset,horizon,decision,regime,signal_tier,eligible,reason,confidence,probability,
      jsonb_build_object(
        'decision',decision,'regime',regime,'signal_tier',signal_tier,'eligible',eligible,'reason',reason,
        'confidence',confidence,'probability',probability,'storage_contract','DECISION_MEMORY_V1')
    FROM fp
    WHERE fingerprint IS DISTINCT FROM prev_fp OR bucket_rank=1
    ON CONFLICT(memory_id) DO NOTHING"""
    before=cur.execute("SELECT COUNT(*) AS n FROM v86_decision_memory").fetchone()['n']
    cur.execute(sql)
    after=cur.execute("SELECT COUNT(*) AS n FROM v86_decision_memory").fetchone()['n']
    return int(after)-int(before)

def emergency_cleanup(dsn):
    if not dsn:
        print('V86_STORAGE_REPAIR_SKIP no_dsn',flush=True); return {'status':'SKIP'}
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row,connect_timeout=8,
                         application_name='veritas-storage-repair') as c:
        c.execute("SET statement_timeout='120s'")
        c.execute("SET lock_timeout='5s'")
        c.execute("SELECT pg_advisory_lock(86000301)")
        try:
            marker=False
            if _table_exists(c,'v85_cutovers'):
                marker=bool(c.execute("SELECT 1 FROM v85_cutovers WHERE cutover_id=%s",(REPAIR_ID,)).fetchone())
            before=_sizes(c)
            if marker:
                print(json.dumps({'event':'V86_STORAGE_REPAIR','status':'ALREADY_DONE','sizes':before},
                                 separators=(',',':')),flush=True)
                return {'status':'ALREADY_DONE','before':before}

            # Raw full-cycle observations are a duplicate of current snapshots + ledger event stream.
            if _table_exists(c,'v85_observations'):
                c.execute("TRUNCATE TABLE v85_observations")

            # Receipt rows are only an idempotency cache. Runtime has an equal-timestamp guard before
            # this cleanup is activated, so stale receipts can be safely reset once.
            if _table_exists(c,'v85_receipts'):
                c.execute("TRUNCATE TABLE v85_receipts")
                c.execute("""ALTER TABLE v85_receipts
                             ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()""")

            # Preserve long-term decision changes before trimming raw decision/meta-signal events.
            rolled=_rollup_decisions_raw(c)
            if _table_exists(c,'ledger_events'):
                c.execute("""DELETE FROM ledger_events
                             WHERE event_type IN ('decision','meta_signal')
                               AND event_ts < clock_timestamp()-interval '24 hours'""")

            # Closed episodes already contain MFE/MAE/giveback/path_points; raw path is no longer needed.
            if _table_exists(c,'v85_path') and _table_exists(c,'v85_episodes'):
                c.execute("""DELETE FROM v85_path p USING v85_episodes e
                             WHERE p.episode_id=e.episode_id
                               AND e.status='CLOSED'
                               AND COALESCE(e.closed_at,e.opened_at)::timestamptz
                                   < clock_timestamp()-interval '1 hour'""")

            # Closed bars are useful only as recent structure context.
            if _table_exists(c,'v85_confirmations'):
                c.execute("""DELETE FROM v85_confirmations
                             WHERE completed_at::timestamptz < clock_timestamp()-interval '30 days'""")

            if _table_exists(c,'v85_cutovers'):
                payload=json.dumps({'contract':'STORAGE_REPAIR_V1','rolled_decisions':rolled,
                                    'preserved':['v85_episodes','v85_orders','v85_lessons',
                                                 'v86_closed_trade_ledger','v86_closed_loop_lessons',
                                                 'v86_episode_attribution','v86_team_experience_cards']},
                                   ensure_ascii=False,separators=(',',':'))
                c.execute("""INSERT INTO v85_cutovers(cutover_id,known_at,payload)
                             VALUES(%s,%s,%s)
                             ON CONFLICT(cutover_id) DO NOTHING""",
                          (REPAIR_ID,_now().isoformat(),payload))

            for table in ('v85_observations','v85_receipts','v85_path','ledger_events'):
                if _table_exists(c,table):
                    try:c.execute(f'VACUUM (ANALYZE) {table}')
                    except Exception:pass
            after=_sizes(c)
            result={'event':'V86_STORAGE_REPAIR','status':'OK','rolled_decisions':rolled,
                    'before':before,'after':after}
            print(json.dumps(result,ensure_ascii=False,separators=(',',':')),flush=True)
            return result
        finally:
            try:c.execute("SELECT pg_advisory_unlock(86000301)")
            except Exception:pass

def retention_cleanup(ledger):
    if getattr(ledger,'dialect',None)!='postgres':
        return {'status':'SKIP_NON_POSTGRES'}
    deleted={}
    with ledger.transaction() as c:
        # Old raw observations: only recent diagnostic window.
        try:
            r=c.execute("""DELETE FROM v85_observations
                           WHERE known_at::timestamptz < clock_timestamp()-interval '48 hours'
                           RETURNING observation_id""").fetchall()
            deleted['observations']=len(r)
        except Exception: deleted['observations']=None

        # Idempotency receipts only need a bounded replay window.
        try:
            r=c.execute("""DELETE FROM v85_receipts
                           WHERE created_at < clock_timestamp()-interval '48 hours'
                           RETURNING event_key""").fetchall()
            deleted['receipts']=len(r)
        except Exception: deleted['receipts']=None

        # Raw path only while position is open plus a short audit window after close.
        try:
            r=c.execute("""DELETE FROM v85_path p
                           USING v85_episodes e
                           WHERE p.episode_id=e.episode_id AND e.status='CLOSED'
                             AND e.closed_at::timestamptz < clock_timestamp()-interval '24 hours'
                           RETURNING p.event_id""").fetchall()
            deleted['closed_path']=len(r)
        except Exception: deleted['closed_path']=None

        try:
            r=c.execute("""DELETE FROM v85_confirmations
                           WHERE completed_at::timestamptz < clock_timestamp()-interval '30 days'
                           RETURNING information_id""").fetchall()
            deleted['confirmations']=len(r)
        except Exception: deleted['confirmations']=None

        if getattr(c,'raw',None) is not None:
            try:
                _rollup_decisions_raw(c.raw)
                r=c.raw.execute("""DELETE FROM ledger_events
                                   WHERE event_type IN ('decision','meta_signal')
                                     AND event_ts < clock_timestamp()-interval '24 hours'
                                   RETURNING event_key""").fetchall()
                deleted['raw_decisions']=len(r)
            except Exception: deleted['raw_decisions']=None
    return {'status':'OK','deleted':deleted}
'''
(root/'veritas_v86/storage_maintenance.py').write_text(module,encoding='utf-8')

# Compact evidence persistence: long-term memory stores decision changes, not every poll.
p=root/'veritas_v85/application.py'
s=p.read_text(encoding='utf-8')
old="""    def _flush_evidence(self,summary,cycle_id):
        rows=self.batch.take();at=now().isoformat();payload=canonical_json(clean({'summary':summary,'events':rows}))
        with self.ledger.transaction() as c:
            c.execute('INSERT INTO v85_observations VALUES(?,?,?) ON CONFLICT(observation_id) DO NOTHING',(cycle_id,at,payload))
            # Preserve the legacy research event schema for read-only analysis/backtest APIs.
            if getattr(self.ledger,'dialect',None)=='postgres':
                args=[]
                for typ,key,value,a,h,ts in rows:
                    args.append((typ+':'+key,key,typ,ts or at,a,h,canonical_json(clean(value)),self.model.VERSION))
                if args:
                    with c.raw.cursor() as cur:
                        cur.executemany('''INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
                             VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(event_key) DO NOTHING''',args)
        self.events_written+=len(rows)
"""
new="""    def _flush_evidence(self,summary,cycle_id):
        rows=self.batch.take();at=now().isoformat()
        compact_all=[];changed=[];changed_cells=set()
        with self.ledger.transaction() as c:
            for row in summary:
                plan=row.get('trade_plan') or {}
                cell=clean({
                  'asset':row.get('asset'),'horizon':row.get('horizon'),
                  'decision':row.get('research_decision') or row.get('decision') or 'NO_TRADE',
                  'regime':row.get('regime'),'signal_tier':row.get('signal_tier'),
                  'decision_stage':row.get('decision_stage'),
                  'confidence':row.get('confidence'),
                  'probability':row.get('calibrated_probability') if row.get('calibrated_probability') is not None else row.get('positive_trade_probability'),
                  'setup_family':row.get('setup_family') or row.get('team_experience_setup_hint') or plan.get('setup'),
                  'trade_plan':{'eligible':plan.get('eligible'),'stop_price':plan.get('stop_price'),
                                'target_price':plan.get('target_price'),'expected_to_stop_ratio':plan.get('expected_to_stop_ratio'),
                                'reason':plan.get('reason')}
                })
                encoded=canonical_json(cell);compact_all.append(cell)
                kind='decision_state:'+str(cell.get('asset'))+':'+str(cell.get('horizon'))
                prior=c.execute('SELECT payload FROM v85_snapshots WHERE kind=?',(kind,)).fetchone()
                if not prior or prior['payload']!=encoded:
                    changed.append(cell);changed_cells.add((cell.get('asset'),cell.get('horizon')))
                    c.execute('''INSERT INTO v85_snapshots VALUES(?,?,?)
                                 ON CONFLICT(kind) DO UPDATE SET published_at=excluded.published_at,payload=excluded.payload''',
                              (kind,at,encoded))
            latest=canonical_json({'at':at,'summary':compact_all,'storage_contract':'LATEST_COMPACT_CYCLE_V1'})
            c.execute('''INSERT INTO v85_snapshots VALUES(?,?,?)
                         ON CONFLICT(kind) DO UPDATE SET published_at=excluded.published_at,payload=excluded.payload''',
                      ('latest_compact_cycle',at,latest))
            if changed:
                payload=canonical_json({'kind':'DECISION_CHANGE','changes':changed,'storage_contract':'DECISION_CHANGE_V1'})
                c.execute('INSERT INTO v85_observations VALUES(?,?,?) ON CONFLICT(observation_id) DO NOTHING',
                          (cycle_id,at,payload))
            if getattr(self.ledger,'dialect',None)=='postgres':
                args=[]
                for typ,key,value,a,h,ts in rows:
                    semantic=(typ in ('case_lesson','outcome','setup_learning','admission_learning')
                              or any(tok in str(typ).lower() for tok in ('trade','order','close','exit','risk','thesis','lesson','error')))
                    changed_decision=(typ in ('decision','meta_signal') and (a,h) in changed_cells)
                    if not (semantic or changed_decision): continue
                    args.append((typ+':'+key,key,typ,ts or at,a,h,canonical_json(clean(value)),self.model.VERSION))
                if args:
                    with c.raw.cursor() as cur:
                        cur.executemany('''INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
                             VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(event_key) DO NOTHING''',args)
        self.events_written+=len(rows)
"""
if old not in s: raise SystemExit('STORAGE_FLUSH_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# Periodic retention runs outside the critical execution path and is fail-soft.
imp="from .validation import verify_release\n"
if "from veritas_v86.storage_maintenance import retention_cleanup" not in s:
    if imp not in s: raise SystemExit('STORAGE_IMPORT_ANCHOR_NOT_FOUND')
    s=s.replace(imp,imp+"from veritas_v86.storage_maintenance import retention_cleanup\n",1)
old2="""        self._flush_evidence(summary,cycle_id)
        with self.state_lock:
"""
new2="""        self._flush_evidence(summary,cycle_id)
        if time.monotonic() >= getattr(self,'_storage_cleanup_due',0.0):
            try:
                print('V86_STORAGE_RETENTION '+canonical_json(retention_cleanup(self.ledger)),flush=True)
            except Exception as exc:
                print('V86_STORAGE_RETENTION_ERROR '+type(exc).__name__+': '+str(exc)[:180],flush=True)
            self._storage_cleanup_due=time.monotonic()+21600
        with self.state_lock:
"""
if old2 not in s: raise SystemExit('STORAGE_PERIODIC_ANCHOR_NOT_FOUND')
s=s.replace(old2,new2,1)
p.write_text(s,encoding='utf-8')

# Paper-book receipt TTL support + duplicate guard + compact representative path.
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')
s=s.replace("points=c.execute('SELECT price FROM v85_path WHERE episode_id=? ORDER BY at,event_id',(p.episode_id,)).fetchall()",
            "points=c.execute('SELECT at,price FROM v85_path WHERE episode_id=? ORDER BY at,event_id',(p.episode_id,)).fetchall()")
anchor="        returns.append(p.direction.sign*(quote.price/p.entry_price-ONE))\n"
if anchor not in s: raise SystemExit('STORAGE_PATH_SAMPLE_ANCHOR_NOT_FOUND')
sample="""        _n=len(points)
        if _n<=16:
            _sample_idx=list(range(_n))
        elif _n:
            _sample_idx=sorted(set(round(i*(_n-1)/15) for i in range(16)))
        else:
            _sample_idx=[]
        path_sample=[{'at':points[i]['at'],'price':points[i]['price']} for i in _sample_idx]
"""
s=s.replace(anchor,anchor+sample,1)
s=s.replace("'path_points':len(points),","'path_points':len(points),'path_sample':path_sample,",1)

clock="""            if previous_clock and at<utc(previous_clock["last_at"]):
                raise InputConflict("OUT_OF_ORDER_EVENT: cannot trade a past market state")
"""
guard="""            if previous_clock and at<utc(previous_clock["last_at"]):
                raise InputConflict("OUT_OF_ORDER_EVENT: cannot trade a past market state")
            if previous_clock and at==utc(previous_clock["last_at"]) and not portfolio_hard_stop and thesis_break is None:
                existing = next((p for p in self._positions(c,account_id) if p.asset==asset),None)
                nav,gross,nav_verified,_ = self._valuation(c,account_id,context,at)
                result={"event_key":event_key,"events":[],"management_reason":"DUPLICATE_EVENT_GUARD",
                        "nav":str(nav),"gross":str(gross),"nav_verified":nav_verified,
                        "experience_effect":{"size_multiplier":"1","reason":"DUPLICATE_EVENT_GUARD","direction_prior_not_applied":True},
                        "replayed":True,"scope":"SYNTHETIC_PAPER"}
                c.execute("INSERT INTO v85_receipts(account_id,event_key,input_hash,result) VALUES(?,?,?,?) ON CONFLICT(account_id,event_key) DO NOTHING",
                          (account_id,event_key,input_hash,canonical_json(result)))
                return result
"""
if clock not in s: raise SystemExit('STORAGE_RECEIPT_GUARD_ANCHOR_NOT_FOUND')
s=s.replace(clock,guard,1)
old_insert='c.execute("INSERT INTO v85_receipts VALUES(?,?,?,?)",(account_id,event_key,input_hash,canonical_json(result)))'
new_insert='c.execute("INSERT INTO v85_receipts(account_id,event_key,input_hash,result) VALUES(?,?,?,?)",(account_id,event_key,input_hash,canonical_json(result)))'
if old_insert not in s: raise SystemExit('STORAGE_RECEIPT_INSERT_ANCHOR_NOT_FOUND')
s=s.replace(old_insert,new_insert,1)
p.write_text(s,encoding='utf-8')

# Emergency cleanup runs BEFORE PostgresLedger migration/application startup.
p=root/'veritas_v86_start.py'
start=p.read_text(encoding='utf-8')
hook="""import os as _storage_os
if _storage_os.getenv('VERITAS_V86_STORAGE_EMERGENCY_CLEANUP','0')=='1':
    from veritas_v86.storage_maintenance import emergency_cleanup as _v86_emergency_cleanup
    _storage_dsn=_storage_os.getenv('VERITAS_V85_TEST_DATABASE_URL') or _storage_os.getenv('DATABASE_URL')
    _v86_emergency_cleanup(_storage_dsn)
"""
if 'VERITAS_V86_STORAGE_EMERGENCY_CLEANUP' not in start:
    p.write_text(hook+'\n'+start,encoding='utf-8')

print('V86_STORAGE_ARCHITECTURE_PATCH_ACTIVE')
