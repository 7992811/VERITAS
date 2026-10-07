"""Immutable price-source identities for normalized paper positions."""
import json
import math
from copy import deepcopy
from veritas_profinance import (BRENT_FEED_PIN_VERSION, BRENT_PROVIDER_INSTRUMENT_ID,
                               brent_quote_metadata_verified)

BRENT_SOURCE_KEY='PROFINANCE:Brent oil'
BRENT_PIN_FIELDS={'source_pin_version':BRENT_FEED_PIN_VERSION,
                  'provider_ticker':'brent',
                  'provider_instrument_id':BRENT_PROVIDER_INSTRUMENT_ID}

QUOTE_FIELDS = ('price', 'best_bid', 'best_ask', 'bid', 'ask', 'market_open',
                'source_gate_pass', 'data_latency_class', 'source_names', 'source',
                'primary_source', 'market_source_names', 'verification_mode',
                'contract', 'contract_id', 'raw_label', 'direct_sources',
                'secondary_price', 'coinbase_price', 'source_divergence',
                'spread_bps', 'orderbook_observed_at', 'book_observed_at',
                'orderbook_ts', 'quote_observed_at', 'instrument_id', 'raw_ticker',
                'provider_ticker_verified', 'exact_contract_verified',
                'contract_identity_status', 'price_series_type', 'price_field',
                'provider_series_verified', 'source_pin_version',
                'provider_ticker', 'provider_instrument_id', 'source_pin_status')


def brent_feed_pin_identity():
    """Configured source selection; numeric provider ID is never contract_id."""
    return {'version':'R80_SOURCE_LOCK','asset':'BRENT','key':BRENT_SOURCE_KEY,
            'primary_source':'ProFinance','contract_id':None,**BRENT_PIN_FIELDS}


def _brent_pin_fields_valid(value):
    return all(key not in value or value[key]==expected
               for key,expected in BRENT_PIN_FIELDS.items())


def is_pinned_brent_identity(value):
    value=value if isinstance(value,dict) else {}
    return (value.get('asset')=='BRENT' and value.get('key')==BRENT_SOURCE_KEY
            and str(value.get('primary_source') or '').strip().upper().startswith('PROFINANCE')
            and value.get('contract_id') is None
            and all(value.get(key)==expected for key,expected in BRENT_PIN_FIELDS.items()))


def brent_quote_verified(row):
    """Recompute feed proof from observed fields instead of trusting a flag."""
    return bool(brent_quote_metadata_verified(row)
                and is_pinned_brent_identity(identity('BRENT',row)))


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
    pin={}
    if upper.startswith('PROFINANCE'):
        channel={'GOLD':'Gold','NQ':'NASD100_FUT','BRENT':'Brent oil'}.get(asset)
        if not channel or row.get('raw_label',channel)!=channel:
            return None
        ticker={'GOLD':'gold','NQ':'NASD100_FUT','BRENT':'brent'}[asset]
        if row.get('raw_ticker') is not None and row['raw_ticker']!=ticker:
            return None
        key='PROFINANCE:'+channel
        if asset=='BRENT':
            if (row.get('instrument_id') is not None
                    and str(row['instrument_id'])!=BRENT_PROVIDER_INSTRUMENT_ID):
                return None
            if not _brent_pin_fields_valid(row):
                return None
            # Legacy source-only identities keep their historical scope. They
            # are never relabelled as a verified provider row without evidence.
            if brent_quote_metadata_verified(row):
                pin=dict(BRENT_PIN_FIELDS)
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
            'primary_source':name,'contract_id':str(cid) if cid else None,**pin}


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
    if expected.get('key')==BRENT_SOURCE_KEY:
        # Unknown exchange month is a value, never a wildcard that can adopt
        # any newly supplied expiry. Legacy saved contexts omit optional feed
        # metadata; abstract source compatibility does not verify a quote.
        # Actual quote admission separately requires the full observed tuple.
        return (cid==actual.get('contract_id')
                and _brent_pin_fields_valid(expected) and _brent_pin_fields_valid(actual))
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
    if expected.get('key')==BRENT_SOURCE_KEY:
        if any(key in expected for key in BRENT_PIN_FIELDS):
            if not is_pinned_brent_identity(expected):
                return {}
            fields.update(raw_label='Brent oil',raw_ticker=expected['provider_ticker'],
                          instrument_id=expected['provider_instrument_id'],
                          source_pin_version=expected['source_pin_version'])
    rebuilt = identity(asset, fields)
    if not same(expected, rebuilt):
        return {}
    return fields


def matches(position, quote):
    expected=position_identity(position)
    if (expected or {}).get('key')==BRENT_SOURCE_KEY and not brent_quote_verified(quote):
        return False
    return same(expected,identity(position.get('asset'),quote))


def valuation_basis(position, quote=None):
    """Describe the held price basis without guessing a contract or rebasing P&L.

    A public provider channel is not proof of an exchange delivery month.
    Current quote metadata is included only when it belongs to the held source.
    """
    expected=position_identity(position) or {}
    q=quote if quote and matches(position,quote) else {}
    cid=expected.get('contract_id')
    key=str(expected.get('key') or '')
    brent=position.get('asset')=='BRENT' and key==BRENT_SOURCE_KEY
    provider_verified=brent and brent_quote_verified(q)
    saved_pin=brent and is_pinned_brent_identity(expected)
    status=('PINNED_CONTRACT' if cid else 'UNVERIFIED_PROVIDER_SERIES'
            if key.startswith(('PROFINANCE:','YAHOO:','STOOQ:')) else 'UNRESOLVED')
    result={'version':'VALUATION_SOURCE_BASIS_V1','asset':position.get('asset'),
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
    if brent:
        # A saved pin describes the selected feed even during an outage. Only
        # an actually matching quote verifies the currently observed provider.
        result.update(source_pin_status=('PINNED_PROVIDER_FEED' if saved_pin or provider_verified
                                         else 'AWAITING_PROVIDER_VERIFICATION'),
                      source_pin_version=BRENT_FEED_PIN_VERSION if saved_pin or provider_verified else None,
                      provider_series_verified=bool(provider_verified),
                      provider_ticker=q.get('raw_ticker') or (expected.get('provider_ticker') if saved_pin else None),
                      provider_instrument_id=q.get('instrument_id') or (expected.get('provider_instrument_id') if saved_pin else None))
    return result


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
