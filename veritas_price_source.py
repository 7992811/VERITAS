"""Immutable price-source identities for normalized paper positions."""
import json
import math
from copy import deepcopy

QUOTE_FIELDS = ('price', 'best_bid', 'best_ask', 'bid', 'ask', 'market_open',
                'source_gate_pass', 'data_latency_class', 'source_names', 'source',
                'primary_source', 'market_source_names', 'verification_mode',
                'contract', 'contract_id', 'raw_label', 'direct_sources',
                'secondary_price', 'coinbase_price', 'source_divergence',
                'spread_bps', 'orderbook_observed_at', 'book_observed_at',
                'orderbook_ts', 'quote_observed_at', 'instrument_id', 'raw_ticker',
                'provider_ticker_verified', 'exact_contract_verified',
                'contract_identity_status', 'price_series_type', 'price_field')


def payload(position):
    value=(position or {}).get('payload') or {}
    return json.loads(value) if isinstance(value,str) else dict(value)


def identity(asset, row):
    row=row or {}
    names=row.get('source_names') or row.get('market_source_names') or {}
    source=names.get('primary') or row.get('source') or row.get('primary_source')
    contract=row.get('contract') or {}
    contract=contract if isinstance(contract,dict) else {}
    cid=contract.get('secid') or contract.get('symbol') or row.get('contract_id')
    if not source:
        return None
    name=str(source).strip(); upper=name.upper()
    if upper.startswith('PROFINANCE'):
        channel={'GOLD':'Gold','NQ':'NASD100_FUT','BRENT':'Brent oil'}.get(asset)
        if not channel or row.get('raw_label',channel)!=channel:
            return None
        ticker={'GOLD':'gold','NQ':'NASD100_FUT','BRENT':'brent'}[asset]
        if row.get('raw_ticker') is not None and row['raw_ticker']!=ticker:
            return None
        key='PROFINANCE:'+channel
    elif upper.startswith('TBANK_GRPC'):
        cid=contract.get('instrument_uid')
        if not cid:
            return None
        key='TBANK_GRPC:'+str(asset)
    elif 'PROXY' in upper or 'BRIDGE' in upper:
        key='PROXY:'+upper
    elif upper.startswith('YAHOO'):
        key='YAHOO:'+{'GOLD':'GC=F','NQ':'NQ=F','BRENT':'BZ=F','MOEX':'IMOEX.ME'}.get(asset,asset)
    elif upper.startswith('STOOQ'):
        key='STOOQ:'+str(asset)
    elif upper.startswith('MOEX'):
        key='MOEX:'+str(asset)
    elif upper.startswith('BINANCE'):
        key='BINANCE:'+str(asset)+'USDT'
    elif upper.startswith('COINBASE'):
        key='COINBASE:'+str(asset)+'-USD'
    else:
        key=upper+':'+str(asset)
    return {'version':'R80_SOURCE_LOCK','asset':asset,'key':key,
            'primary_source':name,'contract_id':str(cid) if cid else None}


def position_identity(position):
    p=payload(position); asset=position.get('asset')
    if p.get('price_source_lock'):
        return dict(p['price_source_lock'])
    old=p.get('contract_identity') or {}
    source=old.get('primary_source') or p.get('entry_primary_source')
    # These legacy adapters have a fixed primary venue, even when old journal
    # versions omitted its name. Never infer GOLD/NQ/BRENT from today's feed.
    inferred=False
    if not source and asset in ('MOEX','CNYRUBF'):
        source='MOEX ISS '+('IMOEX' if asset=='MOEX' else 'CNYRUBF'); inferred=True
    if not source and asset in ('BTC','ETH'):
        source='Binance spot'; inferred=True
    result=identity(asset,{'primary_source':source,
                          'contract_id':p.get('entry_contract_secid') or old.get('contract_id')})
    if result and inferred:
        result['legacy_fixed_adapter']=True
    return result


def same(expected, actual):
    if not expected or not actual or expected.get('key')!=actual.get('key'):
        return False
    cid=expected.get('contract_id')
    return not cid or cid==actual.get('contract_id')


def quote_identity_fields(source_identity):
    """Rehydrate a stored canonical identity without relaxing feed validation.

    TBANK quote identity requires the broker instrument UID in ``contract``.
    Passing its flattened canonical ``contract_id`` through a plan used to lose
    that provenance and reject the same instrument at the final boundary. Only
    complete identities that round-trip to their original key are reconstructed;
    this helper supplies no quote price, timestamp, or permission to execute.
    """
    expected = source_identity if isinstance(source_identity, dict) else {}
    asset, source = expected.get('asset'), expected.get('primary_source')
    if (not isinstance(asset, str) or not asset or not isinstance(source, str)
            or not source or not isinstance(expected.get('key'), str) or not expected['key']):
        return {}
    fields = {'source': source}
    cid = expected.get('contract_id')
    if cid:
        fields['contract_id'] = str(cid)
    if str(source).strip().upper().startswith('TBANK_GRPC'):
        if not cid:
            return {}
        fields['contract'] = {'instrument_uid': str(cid)}
    rebuilt = identity(asset, fields)
    if not same(expected, rebuilt):
        return {}
    return fields


def matches(position, quote):
    return same(position_identity(position),identity(position.get('asset'),quote))


def valuation_basis(position, quote=None):
    """Describe the held price basis without guessing a contract or rebasing P&L.

    A public provider channel is not proof of an exchange delivery month.
    Current quote metadata is included only when it belongs to the held source.
    """
    expected=position_identity(position) or {}
    q=quote if quote and matches(position,quote) else {}
    cid=expected.get('contract_id')
    key=str(expected.get('key') or '')
    status=('PINNED_CONTRACT' if cid else 'UNVERIFIED_PROVIDER_SERIES'
            if key.startswith(('PROFINANCE:','YAHOO:','STOOQ:')) else 'UNRESOLVED')
    return {'version':'VALUATION_SOURCE_BASIS_V1','asset':position.get('asset'),
            'primary_source':expected.get('primary_source'),'source_key':key or None,
            'contract_id':cid,'contract_identity_status':status,
            'provider_label':key.split(':',1)[1] if key.startswith('PROFINANCE:') else None,
            'provider_ticker':q.get('raw_ticker'),'provider_instrument_id':q.get('instrument_id'),
            'provider_ticker_verified':q.get('provider_ticker_verified') is True,
            'price_field':q.get('price_field'),
            'quote_observed_at':q.get('observed_at'),
            'exact_contract_verified':bool(cid and q and q.get('exact_contract_verified') is True),
            'price_series_type':'UNVERIFIED' if status=='UNVERIFIED_PROVIDER_SERIES' else None,
            'valuation_mode':'NORMALIZED_PAPER'}


def quote_from_row(row):
    if '_execution_quote' in (row or {}):
        execution=row.get('_execution_quote')
        return deepcopy(execution) if isinstance(execution,dict) else {}
    return {**(row or {}),'observed_at':(row or {}).get('market_observed_at') or (row or {}).get('observed_at')}


def execution_row(row):
    """Replace every quote field together; never splice an old book into a new quote."""
    result=dict(row or {})
    if '_execution_quote' not in result:
        return result
    result.setdefault('_signal_reference_price',result.get('price'))
    quote=quote_from_row(result)
    for key in QUOTE_FIELDS:
        result.pop(key,None)
        if key in quote:
            result[key]=deepcopy(quote[key])
    result['observed_at']=quote.get('observed_at')
    result['market_observed_at']=quote.get('observed_at')
    result['_execution_quote']=quote
    return result


def positive(value):
    try:
        value=float(value)
        return value if math.isfinite(value) and value>0 else None
    except (TypeError,ValueError,OverflowError):
        return None


def frozen_price(position):
    p=payload(position); mark=p.get('source_locked_mark') or {}
    if same(position_identity(position),mark.get('identity')) and positive(mark.get('price')):
        return float(mark['price'])
    # Unlabelled historical marks might already contain the wrong provider.
    entry=p.get('entry_execution_model') or {}
    return positive(entry.get('reference_price')) or positive(position.get('avg_entry_price')) or positive(position.get('last_price')) or 0.0
