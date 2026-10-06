"""Observed-bar fixtures shared by source and canonical entry regressions."""
from datetime import datetime, timezone
import veritas_price_source as VPS
import veritas_timeframe_structure as TS
import veritas_timeframe_policy as TFP
from test_veritas_timeframe_structure import example


def with_structural_breakout(row):
    row = dict(row)
    asset, tf = row['asset'], row['horizon']
    short = row['research_decision'] == 'SHORT'
    now = datetime.now(timezone.utc)
    bars, close_at = example(tf, short=short)
    scale = float(row['price']) / (98.8 if short else 101.2)
    shift = now.timestamp() - 2 - close_at
    for bar in bars:
        bar['ts'] += shift
        for key in ('open','high','low','close'):
            bar[key] *= scale
    source = {'NQ':'ProFinance NASD100_FUT','GOLD':'ProFinance Gold','BRENT':'ProFinance Brent oil',
              'MOEX':'MOEX ISS IMOEX','CNYRUBF':'MOEX ISS CNYRUBF',
              'BTC':'Binance spot','ETH':'Binance spot'}[asset]
    row.update(source_names={'primary':source}, observed_at=now.isoformat(),
               market_observed_at=now.isoformat())
    identity=VPS.identity(asset,row)
    row['timeframe_entry_context']=TS.build_context(bars,tf,now,asset=asset,source_identity=identity)
    return TFP.prepare_row(row,now=now)
