import json, os, sqlite3, threading, time, traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx

VERSION='veritas-intelligence-v1.2.0'
DB_PATH=os.getenv('VERITAS_LEDGER_PATH','/tmp/veritas_decisions.sqlite3')
INTERVAL=max(300,int(os.getenv('VERITAS_INTERVAL_SECONDS','900')))
MAX_SOURCE_DIVERGENCE=float(os.getenv('VERITAS_MAX_SOURCE_DIVERGENCE','0.01'))
ASSETS={'BTCUSDT':('BTC','BTC-USD'),'ETHUSDT':('ETH','ETH-USD')}
HORIZONS={'4h':4,'1d':24,'3d':72,'7d':168}
last_cycle={'status':'starting','version':VERSION}; lock=threading.Lock()

def now(): return datetime.now(timezone.utc).isoformat()
def emit(event,**fields): print(json.dumps({'ts':now(),'event':event,'version':VERSION,**fields},ensure_ascii=False),flush=True)
def db():
    c=sqlite3.connect(DB_PATH,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init_db():
    with db() as c: c.executescript("""
    CREATE TABLE IF NOT EXISTS market_states(id INTEGER PRIMARY KEY,ts TEXT NOT NULL,asset TEXT NOT NULL,horizon TEXT NOT NULL,features TEXT NOT NULL,source_times TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agent_views(id INTEGER PRIMARY KEY,state_id INTEGER NOT NULL,agent TEXT NOT NULL,direction TEXT NOT NULL,confidence REAL NOT NULL,rationale TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,state_id INTEGER NOT NULL,decision TEXT NOT NULL,confidence REAL NOT NULL,sizing REAL NOT NULL,synthesis TEXT NOT NULL,model_version TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY,decision_id INTEGER NOT NULL,horizon TEXT NOT NULL,evaluated_at TEXT NOT NULL,forward_return REAL,mfe REAL,mae REAL,realized TEXT NOT NULL,UNIQUE(decision_id,horizon));""")
def get_json(url,params=None):
    with httpx.Client(timeout=15,headers={'User-Agent':'VERITAS/1.2'}) as h:
        r=h.get(url,params=params); r.raise_for_status(); return r.json()
def market(symbol,coinbase_product):
    k=get_json('https://api.binance.com/api/v3/klines',{'symbol':symbol,'interval':'1h','limit':240})
    closes=[float(x[4]) for x in k]; vols=[float(x[5]) for x in k]; p=closes[-1]
    cb=float(get_json(f'https://api.exchange.coinbase.com/products/{coinbase_product}/ticker')['price'])
    mid=(p+cb)/2; divergence=abs(p-cb)/mid if mid else 999
    if divergence>MAX_SOURCE_DIVERGENCE: raise RuntimeError(f'SOURCE_DIVERGENCE {symbol}: {divergence:.4%}')
    rs=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    return {'price':p,'coinbase_price':cb,'source_divergence':divergence,'closes':closes,'vols':vols,'returns':rs,'observed_at':now()}
def features(raw,horizon):
    n=HORIZONS[horizon]; c=raw['closes']; v=raw['vols']; p=raw['price']
    fast=max(4,min(n,24)); slow=max(24,min(max(3*n,72),168))
    prior=v[-slow:-fast]
    return {'price':p,'coinbase_price':raw['coinbase_price'],'source_divergence':raw['source_divergence'],
      'ret_h':p/c[-1-n]-1,'trend':p/(sum(c[-slow:])/slow)-1,'momentum':p/c[-1-fast]-1,
      'rv':(sum(x*x for x in raw['returns'][-fast:])/fast)**.5*(fast**.5),
      'volume_ratio':(sum(v[-fast:])/fast)/(sum(prior)/len(prior)) if prior and sum(prior) else 1,'observed_at':raw['observed_at']}
def derivatives(symbol):
    try:
        base='https://fapi.binance.com'
        premium=get_json(base+'/fapi/v1/premiumIndex',{'symbol':symbol}); oi=get_json(base+'/fapi/v1/openInterest',{'symbol':symbol})
        return {'ok':True,'funding':float(premium['lastFundingRate']),'open_interest':float(oi['openInterest'])}
    except Exception as e: return {'ok':False,'error':str(e)}
def agent_views(f,horizon,deriv):
    scale={'4h':1.0,'1d':.9,'3d':.75,'7d':.65}[horizon]
    trend,mom,rv,vr=f['trend'],f['momentum'],f['rv'],f['volume_ratio']
    qs=(.55*trend+.45*mom)*scale; ts=(.65*mom+.35*trend)*(1.1 if vr>1 else .9)*scale
    sig=lambda s,t:'LONG' if s>t else 'SHORT' if s<-t else 'NO_TRADE'
    out=[('MACRO','NO_TRADE',.15,{'reason':'macro feed not yet wired'}),
         ('QUANT',sig(qs,.006),min(.85,.35+abs(qs)*10),{'score':qs}),
         ('TECH_FLOW',sig(ts,.005),min(.8,.3+abs(ts)*10),{'score':ts,'volume_ratio':vr})]
    if deriv.get('ok'):
        ds=-deriv['funding']*120
        out.append(('DERIV',sig(ds,.004),min(.7,.3+abs(ds)*8),{'funding':deriv['funding'],'open_interest':deriv['open_interest'],'score':ds}))
    else: out.append(('DERIV','NO_TRADE',.1,{'reason':'derivatives unavailable','error':deriv.get('error')}))
    out.append(('RISK','NO_TRADE',.8 if rv>.08 else .45,{'rv':rv,'veto':rv>.08}))
    return out
def committee(views):
    weights={'MACRO':1,'QUANT':1.2,'TECH_FLOW':1.1,'DERIV':1,'RISK':1.4}; score=den=0; veto=False
    for a,d,c,r in views:
        if a=='RISK' and r.get('veto'): veto=True
        score+=weights[a]*(1 if d=='LONG' else -1 if d=='SHORT' else 0)*c; den+=weights[a]
    x=score/den if den else 0; decision='NO_TRADE' if veto or abs(x)<.20 else ('LONG' if x>0 else 'SHORT')
    return decision,abs(x),0 if decision=='NO_TRADE' else min(.5,abs(x)),x
def evaluate_outcomes():
    written=0
    with db() as c:
        rows=c.execute("""SELECT d.id,d.created_at,d.decision,s.asset,s.horizon,json_extract(s.features,'$.price') entry
        FROM decisions d JOIN market_states s ON s.id=d.state_id LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon WHERE o.id IS NULL""").fetchall()
    for r in rows:
        created=datetime.fromisoformat(r['created_at']); hours=HORIZONS[r['horizon']]
        if time.time()<created.timestamp()+hours*3600: continue
        symbol='BTCUSDT' if r['asset']=='BTC' else 'ETHUSDT'
        try:
            k=get_json('https://api.binance.com/api/v3/klines',{'symbol':symbol,'interval':'1h','startTime':int(created.timestamp()*1000),'limit':min(hours+3,1000)})
            if len(k)<hours: continue
            entry=float(r['entry']); exitp=float(k[min(hours,len(k)-1)][4]); hs=[float(x[2]) for x in k[:hours+1]]; ls=[float(x[3]) for x in k[:hours+1]]
            fr=exitp/entry-1; mfe=max(hs)/entry-1; mae=min(ls)/entry-1; realized='UP' if fr>0 else 'DOWN' if fr<0 else 'FLAT'
            with db() as c: c.execute('INSERT OR IGNORE INTO outcomes(decision_id,horizon,evaluated_at,forward_return,mfe,mae,realized) VALUES(?,?,?,?,?,?,?)',(r['id'],r['horizon'],now(),fr,mfe,mae,realized))
            emit('outcome',decision_id=r['id'],asset=r['asset'],horizon=r['horizon'],decision=r['decision'],forward_return=round(fr,6),mfe=round(mfe,6),mae=round(mae,6)); written+=1
        except Exception as e: emit('outcome_error',decision_id=r['id'],error=str(e))
    return written
def latest(limit=16):
    with db() as c: return [dict(r) for r in c.execute("""SELECT d.id,d.created_at,s.asset,s.horizon,d.decision,d.confidence,d.sizing,d.model_version FROM decisions d JOIN market_states s ON s.id=d.state_id ORDER BY d.id DESC LIMIT ?""",(limit,))]
def stats():
    with db() as c: return [dict(r) for r in c.execute("""SELECT s.asset,s.horizon,d.decision,COUNT(o.id) n,AVG(o.forward_return) avg_return,
    AVG(CASE WHEN d.decision='LONG' AND o.forward_return>0 THEN 1.0 WHEN d.decision='SHORT' AND o.forward_return<0 THEN 1.0 WHEN d.decision='NO_TRADE' THEN NULL ELSE 0.0 END) hit_rate,
    AVG(o.mfe) avg_mfe,AVG(o.mae) avg_mae FROM decisions d JOIN market_states s ON s.id=d.state_id LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
    GROUP BY s.asset,s.horizon,d.decision ORDER BY s.asset,s.horizon,d.decision""")]
def cycle():
    init_db(); outcomes=evaluate_outcomes(); made=0; summary=[]; emit('cycle_start')
    for symbol,(asset,cb_product) in ASSETS.items():
        raw=market(symbol,cb_product); deriv=derivatives(symbol)
        emit('market_verified',asset=asset,binance=raw['price'],coinbase=raw['coinbase_price'],divergence=raw['source_divergence'],derivatives_ok=deriv.get('ok'))
        for horizon in HORIZONS:
            f=features(raw,horizon); agents=agent_views(f,horizon,deriv); dec,conf,size,score=committee(agents)
            with db() as c:
                cur=c.execute('INSERT INTO market_states(ts,asset,horizon,features,source_times) VALUES(?,?,?,?,?)',(now(),asset,horizon,json.dumps(f),json.dumps({'Binance':f['observed_at'],'Coinbase':f['observed_at']}))); sid=cur.lastrowid
                for a,d,cf,r in agents: c.execute('INSERT INTO agent_views(state_id,agent,direction,confidence,rationale) VALUES(?,?,?,?,?)',(sid,a,d,cf,json.dumps(r)))
                c.execute('INSERT INTO decisions(state_id,decision,confidence,sizing,synthesis,model_version,created_at) VALUES(?,?,?,?,?,?,?)',(sid,dec,conf,size,json.dumps({'committee_score':score,'gates':{'scope':True,'metric':True,'source':True,'time':True}}),VERSION,now()))
            made+=1; z={'asset':asset,'horizon':horizon,'decision':dec,'confidence':round(conf,4),'score':round(score,4)}; summary.append(z); emit('decision',**z)
    with lock: last_cycle.clear(); last_cycle.update({'status':'ok','at':now(),'version':VERSION,'decisions_written':made,'outcomes_written':outcomes,'summary':summary,'storage':'sqlite-ephemeral'})
    emit('cycle_complete',decisions_written=made,outcomes_written=outcomes)
def loop():
    while True:
        try: cycle()
        except Exception as e:
            with lock: last_cycle.clear(); last_cycle.update({'status':'error','at':now(),'version':VERSION,'error':str(e)})
            emit('cycle_error',error=str(e),trace=traceback.format_exc(limit=3))
        time.sleep(INTERVAL)
class H(BaseHTTPRequestHandler):
    def reply(self,obj,code=200):
        body=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        try:
            if self.path in ('/','/health'):
                with lock: x=dict(last_cycle)
                self.reply(x,200 if x.get('status')!='error' else 503)
            elif self.path.startswith('/decisions'): self.reply({'version':VERSION,'decisions':latest()})
            elif self.path.startswith('/stats'): self.reply({'version':VERSION,'stats':stats()})
            else: self.reply({'error':'not found'},404)
        except Exception as e: self.reply({'error':str(e)},503)
    def log_message(self,*args): pass
def main():
    init_db(); emit('service_start',db_path=DB_PATH,interval=INTERVAL); threading.Thread(target=loop,daemon=True).start(); HTTPServer(('0.0.0.0',int(os.getenv('PORT','10000'))),H).serve_forever()
if __name__=='__main__': main()
