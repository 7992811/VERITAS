from pathlib import Path
import sys, json, re as _re
root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
p=root/'veritas_intelligence.py'
_src=p.read_text(encoding='utf-8')

# Durable production source layer. All execution verification fails closed.
_helper=r'''
def _stooq_latest(symbol):
    """Independent futures quote with provider timestamp; never invent freshness."""
    import csv,io
    url='https://stooq.com/q/l/'
    with httpx.Client(timeout=15,headers={'User-Agent':'Mozilla/5.0 VERITAS paper-verification'}) as h:
        r=h.get(url,params={'s':symbol.lower(),'f':'sd2t2ohlcv','h':'','e':'csv'})
        r.raise_for_status(); rows=list(csv.DictReader(io.StringIO(r.text)))
    if not rows: raise RuntimeError(f'STOOQ_NO_QUOTE {symbol}')
    row=rows[-1]; price=None
    for k in ('Close','close','CLOSE'):
        if row.get(k) not in (None,'','N/D'):
            try: price=float(row[k]); break
            except Exception: pass
    if price is None or price<=0: raise RuntimeError(f'STOOQ_BAD_QUOTE {symbol}')
    observed=None
    ds=row.get('Date') or row.get('DATE') or row.get('date')
    ts=row.get('Time') or row.get('TIME') or row.get('time')
    if ds not in (None,'','N/D') and ts not in (None,'','N/D'):
        for fmt in ('%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M'):
            try:
                dt=datetime.strptime(str(ds)+' '+str(ts),fmt).replace(tzinfo=ZoneInfo('Europe/Warsaw'))
                observed=dt.astimezone(timezone.utc).isoformat(); break
            except Exception: pass
    return {'price':price,'observed_at':observed,'retrieved_at':now(),'row':row,'source':'Stooq'}

def _finam_imoex_quote():
    """Price-only independent check. Missing provider timestamp never grants execution."""
    url='https://www.finam.ru/quote/moex/imoex/'
    with httpx.Client(timeout=15,headers={'User-Agent':'Mozilla/5.0 VERITAS paper-verification'}) as h:
        r=h.get(url); r.raise_for_status(); text=r.text
    patterns=[
      r'(?is)Последн[^<]{0,40}(?:сделк|цена).*?([0-9][0-9\\s]{2,}(?:[.,][0-9]+)?)\\s*₽',
      r'(?is)"lastPrice"\\s*:\\s*"?([0-9]+(?:[.,][0-9]+)?)',
      r'(?is)"price"\\s*:\\s*"?([0-9]{4}(?:[.,][0-9]+)?)'
    ]
    price=None
    for pat in patterns:
        m=_re.search(pat,text)
        if m:
            try: price=float(m.group(1).replace(' ','').replace(',','.')); break
            except Exception: pass
    if price is None or price<=100: raise RuntimeError('FINAM_IMOEX_PARSE_FAIL')
    return {'price':price,'observed_at':None,'retrieved_at':now(),'source':'Finam IMOEX delayed'}
'''
_anchor="def _research_only_derivatives(asset):"
if 'def _stooq_latest(symbol):' not in _src:
    if _anchor not in _src: raise SystemExit('NONCRYPTO_HELPER_ANCHOR_NOT_FOUND')
    _src=_src.replace(_anchor,_helper+'\n'+_anchor,1)

# Brent / Gold: direct futures quote from Yahoo cross-checked to independent Stooq futures.
_pat=r"def _yahoo_research_futures_market\(asset,yahoo_symbol,proxy_symbol,policy_key,source_name\):\n.*?\n\ndef _moex_block"
_new=r'''def _yahoo_research_futures_market(asset,yahoo_symbol,proxy_symbol,policy_key,source_name):
    bars5,_=_yahoo_series(yahoo_symbol,'5d','5m',True)
    bars1h,_=_yahoo_series(yahoo_symbol,'3mo','1h',True)
    if len(bars1h)<200: raise RuntimeError(f'INSUFFICIENT_{asset}_HOURLY_BARS {len(bars1h)}')
    last=bars5[-1] if bars5 else bars1h[-1]
    price=float(last['close']); observed=datetime.fromtimestamp(last['ts'],tz=timezone.utc).isoformat()
    closes=[float(x['close']) for x in bars1h[-240:]]
    highs=[float(x['high']) for x in bars1h[-240:]]
    lows=[float(x['low']) for x in bars1h[-240:]]
    vols=[float(x.get('volume') or 0) for x in bars1h[-240:]]
    taker=[v*0.5 for v in vols]
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    market_open=_futures_market_open_from_age(observed)
    age=_age_seconds(observed); delay=DATA_SOURCE_POLICY[policy_key]['documented_delay_sec']
    stooq_symbol={'BRENT':'CB.F','GOLD':'GC.F'}.get(asset)
    secondary=None; sec_obs=None; sec_name=None; div=999.0; sec_age=None
    if stooq_symbol:
        try:
            sq=_stooq_latest(stooq_symbol)
            secondary=float(sq['price']); sec_obs=sq['observed_at']; sec_name=f'Stooq {stooq_symbol}'
            sec_age=_age_seconds(sec_obs) if sec_obs else None
            mid=(price+secondary)/2; div=abs(price-secondary)/mid if mid else 999.0
        except Exception: pass
    direct_ok=bool(secondary is not None and sec_age is not None and 0<=sec_age<=DELAYED_FUTURES_MAX_AGE_SECONDS and div<=0.015)
    gate=bool(market_open and age is not None and age<=DELAYED_FUTURES_MAX_AGE_SECONDS)
    quality=[
      _source_row(source_name,f'{asset} futures','primary research delayed',observed,delay,
                  'OK' if gate else 'STALE_OR_CLOSED',
                  DATA_SOURCE_POLICY[policy_key]['commercial_note'],'Yahoo'),
      _source_row(sec_name or f'Stooq {stooq_symbol}',f'{asset} futures','independent delayed verification',
                  sec_obs,0,'OK' if direct_ok else 'FAIL',
                  f'direct futures divergence={div:.4%}; age={sec_age}' if secondary is not None else 'secondary unavailable','Stooq')]
    _set_source_quality(quality)
    quote_event=f'{asset}:'+str(int(last['ts']))+':'+format(price,'.8f')+':'+(format(secondary,'.8f') if secondary is not None else 'NA')
    out=_v85_annotate_market({
      'asset':asset,'price':price,'secondary_price':secondary,'coinbase_price':secondary,
      'secondary_observed_at':sec_obs,'source_divergence':div if secondary is not None else 999.0,
      'closes':closes,'highs':highs,'lows':lows,'vols':vols,'intraday_bars':bars5,
      'taker_buy':taker,'returns':rets,'binance_close_time_ms':int(last['ts']*1000),
      'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
      'data_latency_class':'DELAYED_RESEARCH','verification_mode':'direct_independent' if direct_ok else 'single_direct_only',
      'source_names':{'primary':source_name,'secondary':sec_name or 'NOT_AVAILABLE'}},locals())
    out['v85_quote']={
      'event_id':quote_event,'primary_time':observed,'secondary_time':sec_obs,
      'primary_source':source_name,'secondary_source':sec_name,
      'source_verified':bool(gate and direct_ok),'max_age_seconds':float(DELAYED_FUTURES_MAX_AGE_SECONDS)}
    return out

def _moex_block'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'NONCRYPTO_FUTURES_PATCH_FAILED {n}')

# MOEX primary: try board and generic marketdata, then recent official 10-minute candle.
_pat=r"def _moex_current_quote\(\):\n.*?\n\ndef _moex_yahoo_klines"
_new=r'''def _moex_current_quote():
    urls=[
      'https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json',
      'https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX.json']
    errors=[]
    with httpx.Client(timeout=20,headers={'User-Agent':'VERITAS/86 MOEX primary'}) as h:
        for url in urls:
            try:
                r=h.get(url,params={'iss.meta':'off'}); r.raise_for_status(); rows=_moex_block(r.json(),'marketdata')
                for row in rows:
                    price=None
                    for k in ('CURRENTVALUE','LASTVALUE','LAST','MARKETPRICE'):
                        if row.get(k) not in (None,''):
                            try: price=float(row[k]); break
                            except Exception: pass
                    if price is None or price<=0: continue
                    dt=None
                    for k in ('SYSTIME','UPDATETIME','TIME'):
                        if row.get(k):
                            dt=_moex_parse_dt(row[k])
                            if dt: break
                    if dt is not None:
                        return {'price':price,'observed_at':dt.isoformat(),'row':row,'quote_mode':'marketdata'}
                errors.append('NO_USABLE_MARKETDATA')
            except Exception as ex: errors.append(type(ex).__name__)
        try:
            m=datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
            frm=(m.date()-timedelta(days=1)).isoformat(); till=(m.date()+timedelta(days=1)).isoformat()
            url='https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX/candles.json'
            r=h.get(url,params={'from':frm,'till':till,'interval':10,'start':0,'iss.meta':'off'})
            r.raise_for_status(); rows=_moex_block(r.json(),'candles'); usable=[]
            for row in rows:
                cl=row.get('close') if row.get('close') not in (None,'') else row.get('CLOSE')
                if cl in (None,''): continue
                dt=_moex_parse_dt(row.get('end') or row.get('END') or row.get('begin') or row.get('BEGIN'))
                if dt is None: continue
                try: usable.append((dt,float(cl),row))
                except Exception: pass
            if usable:
                dt,price,row=max(usable,key=lambda z:z[0])
                return {'price':price,'observed_at':dt.isoformat(),'row':row,'quote_mode':'official_10m_candle'}
            errors.append('NO_10M_CANDLE')
        except Exception as ex: errors.append('CANDLE_'+type(ex).__name__)
    raise RuntimeError('MOEX_ISS_NO_MARKETDATA:'+','.join(errors[-4:]))

def _moex_yahoo_klines'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'MOEX_CURRENT_QUOTE_PATCH_FAILED {n}')

# CNYRUBF: official future cross-checked against independent-vendor CNY/RUB underlying.
_pat=r"def _cnyrubf_market\(\):\n.*?\n\ndef _moex_market"
_new=r'''def _cnyrubf_market():
    end=time.time(); hist=_moex_futures_candles_between('CNYRUBF',end-120*86400,end+86400,60)
    intr5=_moex_futures_candles_between('CNYRUBF',end-5*86400,end+86400,5)
    if len(hist)<120: raise RuntimeError(f'INSUFFICIENT_CNYRUBF_HOURLY_BARS {len(hist)}')
    q=_moex_futures_current_quote('CNYRUBF'); price=float(q['price']); observed=q['observed_at']
    w=hist[-360:]; closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]
    vols=[float(x[5]) for x in w]; taker=[v*0.5 for v in vols]; closes[-1]=price
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    age=_age_seconds(observed); market_open=_futures_market_open_from_age(observed)
    secondary=None; sec_obs=None
    try:
        yr,_=_yahoo_series('CNYRUB%3DX','5d','5m',True)
        if yr:
            secondary=float(yr[-1]['close']); sec_obs=datetime.fromtimestamp(yr[-1]['ts'],tz=timezone.utc).isoformat()
    except Exception: pass
    mid=(price+secondary)/2 if secondary is not None else None
    div=abs(price-secondary)/mid if mid else 999.0
    sec_age=_age_seconds(sec_obs) if sec_obs else None
    paired_ok=bool(secondary is not None and sec_age is not None and 0<=sec_age<=1800 and div<=0.02)
    gate=bool(market_open and age is not None and age<=3600)
    quality=[
      _source_row('MOEX ISS CNYRUBF','CNY/RUB perpetual futures','primary delayed',observed,900,
                  'OK' if gate else 'STALE_OR_CLOSED',DATA_SOURCE_POLICY['moex_forts_cnyrubf']['commercial_note'],'Moscow Exchange'),
      _source_row('Yahoo CNYRUB=X','CNY/RUB interbank','independent underlying verification',sec_obs,0,
                  'OK' if paired_ok else 'FAIL',
                  f'underlying/futures divergence={div:.4%}; age={sec_age}' if secondary is not None else 'secondary unavailable','Yahoo')]
    _set_source_quality(quality)
    quote_event='CNYRUBF:'+str(int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()))+':'+format(price,'.6f')+':'+(format(secondary,'.6f') if secondary is not None else 'NA')
    out=_v85_annotate_market({
      'asset':'CNYRUBF','price':price,'secondary_price':secondary,'coinbase_price':secondary,'source_divergence':div,
      'secondary_observed_at':sec_obs,'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
      'binance_close_time_ms':int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000),
      'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
      'data_latency_class':'DELAYED_RESEARCH',
      'verification_mode':'paired_underlying_independent_vendor' if paired_ok else 'single_direct_official',
      'intraday_bars':[{'ts':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in intr5[-288:]],
      'intraday_5m':[{'ts':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in intr5[-288:]],
      'source_names':{'primary':'MOEX ISS CNYRUBF','secondary':'Yahoo CNYRUB=X' if secondary is not None else 'NOT_AVAILABLE'},
      'contract':{'secid':'CNYRUBF','lot':1000,'price_tick':0.001,'tick_value_rub':1.0,'settlement':'cash','roll':'automatic'}},locals())
    out['v85_quote']={
      'event_id':quote_event,'primary_time':observed,'secondary_time':sec_obs,
      'primary_source':'MOEX ISS CNYRUBF','secondary_source':'Yahoo CNYRUB=X' if secondary is not None else None,
      'source_verified':bool(gate and paired_ok),'max_age_seconds':3600.0}
    return out

def _moex_market'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'CNYRUBF_PATCH_FAILED {n}')

# MOEX index: official primary plus fresh independent Yahoo check; Finam remains price-only fallback.
_pat=r"def _moex_market\(\):\n.*?\n\ndef _ndx_derivatives_context"
_new=r'''def _moex_market():
    end=time.time(); hist=_moex_candles_between(end-90*86400,end+86400)
    if len(hist)<80: raise RuntimeError(f'INSUFFICIENT_MOEX_HOURLY_BARS {len(hist)}')
    q=_moex_current_quote(); price=float(q['price']); observed=q['observed_at']
    w=hist[-240:]; closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]
    vols=[float(x[5]) for x in w]; taker=[v*0.5 for v in vols]; closes[-1]=price
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    open_now=_moex_index_open_now(); age=_age_seconds(observed); gate=bool(open_now and age is not None and age<=MOEX_MAX_AGE_SECONDS)
    secondary=None; yobs=None; moex5m=[]; secondary_name='Yahoo IMOEX.ME'
    try:
        yr,_=_yahoo_series('IMOEX.ME','5d','5m',False)
        if yr:
            moex5m=yr[-240:]; secondary=float(yr[-1]['close'])
            yobs=datetime.fromtimestamp(yr[-1]['ts'],tz=timezone.utc).isoformat()
    except Exception: pass
    if secondary is None or yobs is None or (_age_seconds(yobs) or 999999)>MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS:
        try:
            fq=_finam_imoex_quote()
            if secondary is None: secondary=float(fq['price'])
            if yobs is None: yobs=fq.get('observed_at')
            secondary_name='Finam IMOEX delayed'
        except Exception: pass
    divergence=(abs(price-secondary)/((price+secondary)/2) if secondary and (price+secondary) else 999.0)
    sec_age=_age_seconds(yobs) if yobs else None
    secondary_fresh=bool(secondary is not None and sec_age is not None and 0<=sec_age<=MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS)
    exec_ok=bool(gate and secondary_fresh and divergence<=MOEX_EXEC_MAX_DIVERGENCE)
    quality=[
      _source_row('MOEX ISS IMOEX','MOEX index','primary delayed',observed,MOEX_FREE_ISS_DELAY_SECONDS,
                  'OK' if gate else ('SESSION_CLOSED' if not open_now else 'STALE'),
                  DATA_SOURCE_POLICY['moex_iss']['commercial_note'],'Moscow Exchange'),
      _source_row(secondary_name,'MOEX index','independent delayed verification',yobs,900,
                  'OK' if secondary_fresh else 'STALE_OR_UNAVAILABLE',
                  f'price divergence={divergence:.4%}; age={sec_age}' if secondary is not None else 'secondary unavailable',
                  'Yahoo' if secondary_name.startswith('Yahoo') else 'Finam')]
    _set_source_quality(quality)
    quote_event='MOEX:'+str(int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()))+':'+format(price,'.6f')+':'+(format(secondary,'.6f') if secondary is not None else 'NA')
    out=_v85_annotate_market({
      'asset':'MOEX','price':price,'secondary_price':secondary,'coinbase_price':secondary,
      'secondary_observed_at':yobs,'source_divergence':divergence,
      'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
      'binance_close_time_ms':int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000),
      'observed_at':observed,'source_gate_pass':gate,'market_open':open_now,'source_quality':quality,
      'data_latency_class':'DELAYED_RESEARCH','intraday_5m':moex5m,
      'verification_mode':'direct_independent' if exec_ok else 'single_direct_official',
      'source_names':{'primary':'MOEX ISS IMOEX','secondary':secondary_name}},locals())
    out['v85_quote']={
      'event_id':quote_event,'primary_time':observed,'secondary_time':yobs,
      'primary_source':'MOEX ISS IMOEX','secondary_source':secondary_name if yobs else None,
      'source_verified':bool(exec_ok),'max_age_seconds':float(MOEX_MAX_AGE_SECONDS)}
    return out

def _ndx_derivatives_context'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'MOEX_MARKET_PATCH_FAILED {n}')

# CNY execution may use independent-vendor underlying verification with a conservative basis gate.
_pat=r"    if asset=='CNYRUBF':\n.*?\n\n    if asset=='MOEX':"
_new=r'''    if asset=='CNYRUBF':
        sec=raw.get('secondary_price'); sec_ts=raw.get('secondary_observed_at')
        sec_age=_age_seconds(sec_ts) if sec_ts else None
        divergence=float(raw.get('source_divergence') or 999.0)
        mode=raw.get('verification_mode')
        ok=bool(research_ok and time_ok and mode=='paired_underlying_independent_vendor'
                and sec is not None and sec_age is not None and sec_age<=1800 and divergence<=0.02)
        return {'eligible':ok,'reason':'cnyrubf_independent_underlying_check' if ok else 'research_only_no_second_direct_cnyrubf_quote',
                'direct_sources':2 if ok else 1,'secondary_age_seconds':sec_age,'divergence':divergence,
                'research_ok':research_ok,'time_ok':time_ok,'verification_mode':mode}

    if asset=='MOEX':'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'CNY_EXEC_GATE_PATCH_FAILED {n}')

p.write_text(_src,encoding='utf-8')
print('V86_NONCRYPTO_EXECUTION_SOURCES_ACTIVE')
print('V86_RUNTIME_PATCH_OK')
