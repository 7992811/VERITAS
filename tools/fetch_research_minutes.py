"""Read-only public spot candles, with strict continuity and resumable pages."""
import argparse, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import httpx


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--output',required=True);p.add_argument('--assets',nargs='+',default=['BTC','ETH'])
    p.add_argument('--include-flow',action='store_true')
    a=p.parse_args();root=Path(a.output);root.mkdir(parents=True,exist_ok=True)
    start=int(datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc).timestamp())
    end=int(datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc).timestamp())
    assert start<end and start%60==end%60==0
    pages=root/'pages';pages.mkdir(exist_ok=True)
    jobs=[(asset,s) for asset in a.assets for s in range(start,end,60000)]
    with httpx.Client(timeout=25) as client:
        def fetch(job):
            asset,s=job;path=pages/f'{asset}_{s}.json';stop=min(end,s+60000)
            if path.exists():
                cached=json.loads(path.read_text())
                if not a.include_flow or all('taker_buy_volume' in b for b in cached):return asset,cached
            for attempt in range(4):
                try:
                    r=client.get('https://data-api.binance.vision/api/v3/klines',params={
                        'symbol':asset+'USDT','interval':'1m','startTime':s*1000,'endTime':stop*1000-1,'limit':1000})
                    r.raise_for_status()
                    raw=r.json()
                    bars=[dict(ts=int(b[0])//1000,open=float(b[1]),high=float(b[2]),low=float(b[3]),
                               close=float(b[4]),volume=float(b[5])) for b in raw]
                    if a.include_flow:
                        for bar,b in zip(bars,raw):
                            bar.update(quote_volume=float(b[7]),trade_count=int(b[8]),
                                taker_buy_volume=float(b[9]),taker_buy_quote_volume=float(b[10]))
                            assert 0<=bar['taker_buy_volume']<=bar['volume']+1e-8 and bar['trade_count']>=0
                    assert [b['ts'] for b in bars]==list(range(s,stop,60)),(asset,s,len(bars))
                    assert all(0<b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']
                               and b['volume']>=0 for b in bars)
                    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(bars,separators=(',',':')));tmp.replace(path)
                    return asset,bars
                except Exception:
                    if attempt==3:raise
                    time.sleep(1+attempt)
        data={asset:[] for asset in a.assets}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i,future in enumerate(as_completed([pool.submit(fetch,j) for j in jobs]),1):
                asset,bars=future.result();data[asset].extend(bars)
                if i%50==0:print('pages',i,'/',len(jobs),flush=True)
    for asset,bars in data.items():
        bars.sort(key=lambda b:b['ts'])
        assert [b['ts'] for b in bars]==list(range(start,end,60))
        path=root/f'{asset}_1m.json'
        path.write_text(json.dumps(dict(asset=asset,source='Binance public spot klines',start=start,end_exclusive=end,
            retrieved_at=datetime.now(timezone.utc).isoformat(),bars=bars),separators=(',',':')))
        print(asset,'complete',len(bars),flush=True)


if __name__=='__main__':main()
