"""Public page quote channel. Reference verification only, never broker fills."""
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import math
import re
import httpx

SYMBOLS={'NQ':'NASD100_FUT','GOLD':'Gold','BRENT':'Brent oil'}
BASE='https://jq.profinance.ru/html/htmlquotes/'


def parse_quotes(text,now=None):
    now=now or datetime.now(timezone.utc)
    local=now.astimezone(ZoneInfo('Europe/Moscow'))
    out={}
    for line in text.splitlines():
        fields=dict(part.split('=',1) for part in line.split(';') if '=' in part)
        symbol=fields.get('S')
        asset=next((a for a,s in SYMBOLS.items() if s==symbol),None)
        if not asset:continue
        try:
            # The public widget hides one leading LP sign before displaying
            # price (q_show.js: setValueToTdById(..., hide_sign=true)). It is
            # quote-direction formatting, not a negative instrument price.
            raw_price=fields['LP'].strip()
            magnitude=raw_price[1:] if raw_price[:1] in ('+','-') else raw_price
            if not re.fullmatch(r'\d+(?:\.\d+)?',magnitude):continue
            price=float(magnitude)
            clock=datetime.strptime(fields['T'],'%H:%M:%S').time()
            observed=datetime.combine(local.date(),clock,tzinfo=local.tzinfo)
            if observed>local+timedelta(seconds=5):observed-=timedelta(days=1)
            age=(local-observed).total_seconds()
            if not math.isfinite(price) or price<=0 or not -5<=age<=180:continue
        except (ValueError,KeyError):continue
        out[asset]={'price':price,'observed_at':observed.astimezone(timezone.utc).isoformat(),
                    'source':'ProFinance','source_role':'public_freshness_verification',
                    'raw_label':symbol,'instrument_id':fields.get('I'),
                    'raw_price':raw_price,'quote_direction_sign':raw_price[:1] if raw_price[:1] in ('+','-') else None,
                    'time_basis':'Europe/Moscow quote clock; date inferred',
                    'date_verified':False,'exact_contract_verified':False,
                    'execution_eligible':False,'reference_only':True}
    return out


def fetch_quotes():
    with httpx.Client(timeout=httpx.Timeout(3.,connect=1.5),follow_redirects=True,
                      headers={'User-Agent':'Mozilla/5.0 VERITAS public quote verification'}) as client:
        response=client.get(BASE+'site.jsp');response.raise_for_status()
        match=re.search(r'SID=([A-Za-z0-9_-]+)',response.text)
        if not match:return {}
        query='1;SID='+match.group(1)+';LP=;T=;'+';'.join('S='+s for s in SYMBOLS.values())+'\n'
        response=client.post(BASE+'q',content=query);response.raise_for_status()
        return parse_quotes(response.text)
