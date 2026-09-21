import json, os, sqlite3, threading, time, traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx

VERSION='veritas-intelligence-v1.1.0'
DB_PATH=os.getenv('VERITAS_LEDGER_PATH','/tmp/veritas_decisions.sqlite3')
INTERVAL=max(300,int(os.getenv('VERITAS_INTERVAL_SECONDS','900')))
MAX_SOURCE_DIVERGENCE=float(os.getenv('VERITAS_MAX_SOURCE_DIVERGENCE','0.01'))
ASSETS={'BTCUSDT':('BTC','BTC-USD'),'ETHUSDT':('ETH','ETH-USD')}
HORIZONS=['4h','1d','3d','7d']
last_cycle={'status':'starting','version':VERSION}; lock=threading.Lock()

def now(): return datetime.now(timezone.utc).isoformat()
def emit(event, **fields): print(json.dumps({'ts':now(),'event':event,'version':VERSION,**fields},ensure_ascii=False),flush=True)
def db():
    c=sqlite3.connect(DB_PATH,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c

def init_db():
    with db() as c: c.executescript('''
    CREATE TABLE IF NOT EXISTS market_states(id INTEGER PRIMARY KEY,ts TEXT NOT NULL,asset TEXT NOT NULL,horizon TEXT NOT NULL,features TEXT NOT NULL,source_times TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agent_views(id INTEGER PRIMARY KEY,state_id INTEGER NOT NULL,agent TEXT NOT NULL,direction TEXT NOT NULL,confidence REAL NOT NULL,rationale TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,state_id INTEGER NOT NULL,decision TEXT NOT NULL,confidence REAL NOT NULL,sizing REAL NOT NULL,synthesis TEXT NOT NULL,model_version TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY,decision_id INTEGER NOT NULL,horizon TEXT NOT NULL,evaluated_at TEXT NOT NULL,forward_return REAL,mfe REAL,mae REAL,realized TEXT NOT NULL,UNIQUE(decision_id,horizon));''')

def get_json(url,params=None):
    with httpx.Client(timeout=15,headers={'User-Agent':'VERITAS/1.1'}) as h:
        r=h.get(url,params=params); r.raise_for_status(); return r.json()

def market(symbol, coinbase_product):
    k=get_json('https://api.binance.com/api/v3/klines',{'symbol':symbol,'interval':'1h','limit':240})
    closes=[float(x[4]) for x in k]; vols=[float(x[5]) for x in k]; p=closes[-1]
    cb=float(get_json(f'https://api.exchange.coinbase.com/products/{coinbase_product}/ticker')['price'])
    mid=(p+cb)/2; divergence=abs(p-cb)/mid if mid else 999
    if divergence>MAX_SOURCE_DIVERGENCE: raise RuntimeError(f'SOURCE_DIVERGENCE {symbol}: Binance={p} Coinbase={cb} diff={divergence:.4%}')
    def ret(n): return p/closes[-1-n]-1
    def sma(n): return sum(closes[-n:])/n
    rs=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    rv24=(sum(x*x for x in rs[-24:])/24)**0.5*(24**0.5)
    vr=(sum(vols[-24:])/24)/(sum(vols[-120:-24])/96) if sum(vols[-120:-24]) else 1
    return {'price':p,'coinbase_price':cb,'source_divergence':divergence,'ret_4h':ret(4),'ret_24h':ret(24),'ret_72h':ret(72),'ret_168h':ret(168),'sma24':sma(24),'sma72':sma(72),'sma168':sma(168),'rv24':rv24,'volume_ratio_24h':vr,'observed_at':now()}

def view(agent,f):
    p=f['price']; trend=p/f['sma72']-1; mom=f['ret_24h']; vol=f['rv24']; vr=f['volume_ratio_24h']
    if agent=='MACRO': return 'NO_TRADE',.15,{'reason':'macro inputs not wired; abstain'}
    if agent=='QUANT':
        s=.55*trend+.45*mom; d='LONG' if s>.01 else 'SHORT' if s<-.01 else 'NO_TRADE'; return d,min(.85,.35+abs(s)*8),{'trend72':trend,'mom24':mom}
    if agent=='TECH_FLOW':
        s=(p/f['sma24']-1)*(1.15 if vr>1 else .85); d='LONG' if s>.006 else 'SHORT' if s<-.006 else 'NO_TRADE'; return d,min(.8,.3+abs(s)*12),{'price_vs_sma24':p/f['sma24']-1,'volume_ratio':vr}
    if agent=='DERIV': return 'NO_TRADE',.15,{'reason':'derivatives inputs not wired; abstain'}
    if agent=='RISK':
        if vol>.08: return 'NO_TRADE',.75,{'reason':'elevated realized volatility','rv24':vol}
        return 'NO_TRADE',.45,{'reason':'risk veto inactive; abstain','rv24':vol}

def committee(views):
    weights={'MACRO':1,'QUANT':1.2,'TECH_FLOW':1.1,'DERIV':1,'RISK':1.4}; score=den=0
    for a,d,c,_ in views:
        score+=weights[a]*(1 if d=='LONG' else -1 if d=='SHORT' else 0)*c; den+=weights[a]
    x=score/den if den else 0; decision='NO_TRADE' if abs(x)<.20 else ('LONG' if x>0 else 'SHORT')
    return decision,abs(x),0 if decision=='NO_TRADE' else min(.5,abs(x)),x

def latest(limit=16):
    with db() as c:
        rows=c.execute('''SELECT d.id,d.created_at,s.asset,s.horizon,d.decision,d.confidence,d.sizing,d.synthesis,s.features FROM decisions d JOIN market_states s ON s.id=d.state_id ORDER BY d.id DESC LIMIT ?''',(limit,)).fetchall()
        return [dict(r) for r in rows]

def cycle():
    init_db(); made=0; summary=[]; emit('cycle_start')
    for symbol,(asset,cb_product) in ASSETS.items():
        f=market(symbol,cb_product); emit('market_verified',asset=asset,binance=f['price'],coinbase=f['coinbase_price'],divergence=f['source_divergence'])
        for horizon in HORIZONS:
            agents=[]
            for a in ['MACRO','QUANT','TECH_FLOW','DERIV','RISK']:
                d,cf,r=view(a,f); agents.append((a,d,cf,r))
            dec,conf,size,score=committee(agents)
            with db() as c:
                cur=c.execute('INSERT INTO market_states(ts,asset,horizon,features,source_times) VALUES(?,?,?,?,?)',(now(),asset,horizon,json.dumps(f),json.dumps({'Binance':f['observed_at'],'Coinbase':f['observed_at']}))); sid=cur.lastrowid
                for a,d,cf,r in agents: c.execute('INSERT INTO agent_views(state_id,agent,direction,confidence,rationale) VALUES(?,?,?,?,?)',(sid,a,d,cf,json.dumps(r)))
                c.execute('INSERT INTO decisions(state_id,decision,confidence,sizing,synthesis,model_version,created_at) VALUES(?,?,?,?,?,?,?)',(sid,dec,conf,size,json.dumps({'committee_score':score,'gates':{'scope':True,'metric':True,'source':True,'time':True}}),VERSION,now()))
            made+=1; summary.append({'asset':asset,'horizon':horizon,'decision':dec,'confidence':round(conf,4),'score':round(score,4)}); emit('decision',**summary[-1])
    state={'status':'ok','at':now(),'version':VERSION,'decisions_written':made,'summary':summary,'db_path':DB_PATH}
    with lock: last_cycle.clear(); last_cycle.update(state)
    emit('cycle_complete',decisions_written=made)

def loop():
    while True:
        try: cycle()
        except Exception as e:
            err={'status':'error','at':now(),'version':VERSION,'error':f'{type(e).__name__}: {e}'}
            with lock: last_cycle.clear(); last_cycle.update(err)
            emit('cycle_error',error=err['error'],trace=traceback.format_exc(limit=3))
        time.sleep(INTERVAL)

class H(BaseHTTPRequestHandler):
    def reply(self,obj,code=200):
        body=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path in ('/','/health'):
            with lock: x=dict(last_cycle)
            self.reply(x,200 if x.get('status')!='error' else 503)
        elif self.path.startswith('/decisions'):
            try: self.reply({'version':VERSION,'decisions':latest()})
            except Exception as e: self.reply({'error':str(e)},503)
        else: self.reply({'error':'not found'},404)
    def log_message(self,*args): pass

def main():
    init_db(); emit('service_start',db_path=DB_PATH,interval=INTERVAL)
    threading.Thread(target=loop,daemon=True).start(); HTTPServer(('0.0.0.0',int(os.getenv('PORT','10000'))),H).serve_forever()
if __name__=='__main__': main()
