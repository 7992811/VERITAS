"""Synthetic NAV/mark parity; inspect the real engine with AST, never import it."""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_book_marking as M
import veritas_position_guard as G
import veritas_price_source as S


# Deliberately artificial amounts, dates and identifiers, unrelated to any book.
NOW = datetime(2042, 1, 2, 10, tzinfo=timezone.utc)
PORTFOLIO = 'SYNTHETIC-NAV-PARITY'
TREE = ast.parse(Path(__file__).with_name('veritas_portfolio.py').read_text())


def helpers():
    names = {'_portfolio_rows', '_mark_nav', '_v90j_json', '_v90j_iso',
             '_v90j_mark_open_positions'}
    nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {'VBM': M, 'VPG': G, 'VPS': S, 'json': json, 'math': math}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'book-marking-helpers', 'exec'), ns)
    return SimpleNamespace(**{k: ns[k] for k in names})


H = helpers()


def quote(asset='BTC', price=137.):
    return {'asset': asset, 'price': price, 'source_gate_pass': True,
            'observed_at': (NOW-timedelta(seconds=7)).isoformat(),
            'market_open': True, 'source_names': {'primary': 'Binance spot'}}


def position(asset='BTC', *, payload=None):
    identity = S.identity(asset, quote(asset))
    original = {
        'price_source_lock': identity,
        'source_locked_mark': {'identity': identity, 'price': 131.,
                              'observed_at': (NOW-timedelta(seconds=20)).isoformat()},
        'entry_execution_observed_at': (NOW-timedelta(minutes=3)).isoformat(),
        'entry_execution_model': {'reference_price': 113.},
        'retained_history': {'proof': 'SYNTHETIC-UNUSED-EVIDENCE-'*6000},
        'explicit_unrelated_null': None,
    }
    return {'portfolio_name': PORTFOLIO, 'asset': asset, 'direction': 'LONG',
            'units': 3., 'avg_entry_price': 127., 'last_price': 9999.,
            'stop_price': 81., 'opened_at': NOW-timedelta(minutes=4),
            'updated_at': NOW-timedelta(minutes=2),
            'active_trade_id': 'SYNTHETIC-'+asset,
            'payload': original if payload is None else payload}


def project(row):
    out = deepcopy(row)
    value = out.get('payload')
    if isinstance(value, dict):
        out['payload'] = {k: v for k, v in value.items() if k in M.MARK_FIELDS}
    return out


def legacy_mark(c, name, prices, ts):
    """Frozen pre-change marker from b684c27e for differential SQL verification."""
    rows = c.execute('SELECT asset,payload FROM paper_positions WHERE portfolio_name=%s',
                     (name,)).fetchall()
    marked = 0
    for original in rows:
        row = dict(original)
        asset = str(row.get('asset') or '')
        if asset not in (prices or {}):
            continue
        try:
            q = G.quote_for_position(row, now=ts)
            if not q:
                continue
            price = float(q['price'])
            if not math.isfinite(price) or price <= 0:
                continue
        except Exception:
            continue
        payload = H._v90j_json(row.get('payload'))
        payload['last_mark_price'] = price
        payload['last_mark_at'] = H._v90j_iso(ts)
        payload['price_source_lock'] = S.position_identity(row)
        payload['price_source_status'] = 'OK'
        payload['source_locked_mark'] = {'identity': S.identity(asset, q),
                                         'price': price, 'observed_at': q['observed_at']}
        c.execute('UPDATE paper_positions SET last_price=%s,updated_at=%s,payload=%s::jsonb '
                  'WHERE portfolio_name=%s AND asset=%s',
                  (price, ts, json.dumps(payload, ensure_ascii=False, default=str), name, asset))
        marked += 1
    return marked


class MemoryConnection:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.statements = []
        self.account = {'name': PORTFOLIO, 'initial_nav_rub': 4103.,
                        'realized_pnl_rub': 19., 'fees_rub': 7., 'funding_rub': 3.}

    def execute(self, query, params=()):
        self.statements.append((query, params))
        if query.startswith('SELECT * FROM paper_portfolios'):
            return SimpleNamespace(fetchone=lambda: deepcopy(self.account))
        if query.startswith('SELECT'):
            rows = [deepcopy(r) for r in self.rows if r['portfolio_name'] == params[0]]
            if query.startswith(M.MARK_SQL):
                rows = [project(r) for r in rows]
            elif not query.startswith(('SELECT * FROM paper_positions',
                                       'SELECT asset,payload FROM paper_positions')):
                raise AssertionError(query)
            return SimpleNamespace(fetchall=lambda: rows)
        if query.startswith('WITH delta AS') and 'UPDATE paper_positions AS target' in query:
            batch=json.loads(params[0]); timestamp=params[1]
            for item in batch:
                row=next(r for r in self.rows if
                         (r['portfolio_name'],r['asset'])==(item['portfolio_name'],item['asset']))
                change=item['patch']
                row['payload']=(change if item['replace_payload']
                                else dict(row['payload'],**change))
                row.update(last_price=float(item['last_price']),updated_at=timestamp)
            return SimpleNamespace(rowcount=len(batch))
        if query.startswith('UPDATE paper_positions'):
            price, ts, encoded, name, asset = params
            row = next(r for r in self.rows if (r['portfolio_name'], r['asset']) == (name, asset))
            change = json.loads(encoded)
            row['payload'] = dict(row['payload'], **change) if 'payload ||' in query else change
            row.update(last_price=price, updated_at=ts)
            return SimpleNamespace(rowcount=1)
        raise AssertionError(query)


class BookMarkingTests(unittest.TestCase):
    def setUp(self):
        for cache in (G._quotes, G._source_quotes, G._market_state):
            self.enterContext(patch.dict(cache, {}, clear=True))
        clock = self.enterContext(patch.object(G, 'datetime', wraps=datetime))
        clock.now.return_value = NOW

    def result(self, row):
        try:
            account = MemoryConnection([]).account
            return ('OK', H._mark_nav(account, [row], {}))
        except Exception as exc:
            return ('ERROR', type(exc))

    def test_full_and_projected_nav_agree_for_sources_fallbacks_and_corrupt_shapes(self):
        base = position()['payload']
        cases = [base, {}, None, [], [1], 42, False, 'null', '[]', '7',
                 '{"entry_primary_source":"Binance spot"}']
        for key in ('price_source_lock', 'contract_identity', 'source_locked_mark',
                    'entry_execution_model'):
            cases.extend((dict(base, **{key: None}), {key: 'malformed'}))
        cases.extend([
            dict(base, entry_execution_observed_at=NOW.isoformat()),
            dict(base, entry_execution_observed_at=None, entry_market_observed_at=NOW.isoformat()),
            dict(base, source_locked_mark=dict(base['source_locked_mark'],
                                               price=151., observed_at=NOW.isoformat())),
            dict(base, price_source_lock=dict(base['price_source_lock'], key='FOREIGN:BTC')),
        ])
        for cached in ({}, {'BTC': quote()}, {'BTC': dict(quote(), source_gate_pass=False)}):
            with patch.dict(G._quotes, cached, clear=True):
                for index, value in enumerate(cases):
                    for direction in ('LONG', 'SHORT'):
                        with self.subTest(case=index, cached=bool(cached), direction=direction):
                            full = dict(position(), payload=deepcopy(value), direction=direction)
                            self.assertEqual(self.result(full), self.result(project(full)))
        with patch.dict(G._quotes, {}, clear=True):
            self.assertEqual(self.result(position())[1][1], 12.)  # pinned 131 versus entry 127
            no_mark = dict(position(), payload=dict(base, source_locked_mark=None))
            self.assertEqual(self.result(no_mark)[1][1], -42.)  # execution reference 113

    def test_brent_present_null_pin_is_not_repaired_into_legacy_absence(self):
        identity = dict(asset='BRENT', key=S.BRENT_SOURCE_KEY, contract_id=None,
                        primary_source='ProFinance Brent oil', **S.BRENT_PIN_FIELDS)
        frozen = {'identity': identity, 'price': 131.,
                  'observed_at': (NOW-timedelta(seconds=4)).isoformat()}
        legacy = {k: v for k, v in identity.items() if k not in S.BRENT_PIN_FIELDS}
        valid = dict(position('BRENT'), payload={'price_source_lock': legacy,
                     'source_locked_mark': frozen, 'entry_execution_model': {'reference_price': 113.}})
        bad = deepcopy(valid)
        bad['payload']['price_source_lock']['source_pin_version'] = None
        self.assertEqual(self.result(valid), self.result(project(valid)))
        self.assertEqual(self.result(bad), self.result(project(bad)))
        self.assertNotEqual(self.result(valid), self.result(bad))

    def test_nav_reads_reload_only_after_position_or_order_mutations(self):
        base = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == '_step_one')
        calls = sorted((n for n in ast.walk(base) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == '_portfolio_rows'),
                       key=lambda n: n.lineno)
        # Funding changes only ledger totals, so the initial locked snapshot is
        # advanced locally. Fresh projected reads are retained only after an
        # actual close/reduce or add mutation.
        self.assertEqual([[(k.arg, ast.literal_eval(k.value)) for k in n.keywords] for n in calls],
                         [[], [('mark_only', True)], [('mark_only', True)]])
        c = MemoryConnection([position()])
        before = H._portfolio_rows(c, PORTFOLIO, mark_only=True)
        c.account.update(realized_pnl_rub=37., fees_rub=17., funding_rub=11.)
        c.rows[0]['units'] = 1.
        after = H._portfolio_rows(c, PORTFOLIO, mark_only=True)
        self.assertEqual(after[0], c.account)
        self.assertEqual(after[1][0]['units'], 1.)
        self.assertNotEqual(H._mark_nav(*before, {}), H._mark_nav(*after, {}))
        self.assertNotIn('retained_history', after[1][0]['payload'])
        self.assertIn('retained_history', H._portfolio_rows(c, PORTFOLIO)[1][0]['payload'])

    def test_mark_delta_matches_full_baseline_and_does_not_send_retained_history(self):
        G._quotes['BTC'] = quote()
        before = position()
        old, new = MemoryConnection([before]), MemoryConnection([before])
        self.assertEqual(legacy_mark(old, PORTFOLIO, {'BTC': 9999.}, NOW),
                         H._v90j_mark_open_positions(new, PORTFOLIO, {'BTC': 9999.}, NOW))
        self.assertEqual(new.rows, old.rows)
        self.assertEqual(new.rows[0]['last_price'], 137.)  # the verified quote, not scalar prices
        writes = [(sql,params) for sql,params in new.statements
                  if sql.startswith('WITH delta AS')]
        self.assertEqual(len(writes), 1)
        batch=json.loads(writes[0][1][0])
        self.assertEqual(len(batch),1)
        self.assertLess(len(json.dumps(batch[0]['patch']).encode('utf-8')),1000)
        self.assertEqual(new.rows[0]['payload']['retained_history'], before['payload']['retained_history'])

    def test_exact_same_source_observation_is_a_noop(self):
        q=quote()
        identity=S.identity('BTC',q)
        row=position()
        row['last_price']=float(q['price'])
        row['payload'].update(
            last_mark_price=float(q['price']),
            last_mark_at=(NOW-timedelta(seconds=1)).isoformat(),
            price_source_lock=identity,
            price_source_status='OK',
            source_locked_mark={'identity':identity,'price':float(q['price']),
                                'observed_at':q['observed_at']})
        G._quotes['BTC']=q
        c=MemoryConnection([row])
        self.assertEqual(H._v90j_mark_open_positions(c,PORTFOLIO,{'BTC':9999.},NOW),0)
        self.assertFalse(any(sql.startswith('UPDATE') or sql.startswith('WITH delta AS')
                             for sql,_ in c.statements))
        self.assertEqual(c.rows[0],row)

    def test_marker_preserves_nonobject_behavior_and_skips_unusable_quotes(self):
        cases = [None, [], {}, [1], 7, False, 'null',
                 '{"entry_primary_source":"Binance spot","opaque":"synthetic"}']
        for cached in ({}, {'BTC': quote()}, {'BTC': dict(quote(), source_gate_pass=False)}):
            with patch.dict(G._quotes, cached, clear=True):
                for value in cases:
                    with self.subTest(shape=repr(value), cached=bool(cached)):
                        original = dict(position(), payload=value)
                        old, new = MemoryConnection([original]), MemoryConnection([original])
                        self.assertEqual(legacy_mark(old, PORTFOLIO, {'BTC': 1.}, NOW),
                                         H._v90j_mark_open_positions(new, PORTFOLIO, {'BTC': 1.}, NOW))
                        self.assertEqual(new.rows, old.rows)


if __name__ == '__main__':
    unittest.main()
