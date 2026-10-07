"""Frozen pre-batch protective pass for full PostgreSQL ledger parity.

Source: veritas_position_guard.py at 0e4ace837baaa9a7a5244897537de5051fbc93bc.
Only this function is frozen; price gates, fills and accounting are production
functions supplied by the importing test. It is never imported by runtime code.
Function SHA256: 81f76de482cfbce6659eeeff6c5e56c7cd198949d9f475718ac979e3c534fc16
"""

def run_protective_pass(vp, pg_connect, quotes, now=None, *, timing=None):
    measured=timing if timing is not None else {}
    changes = []
    with pg_connect() as c, book_transaction(c,lane='PROTECTIVE',timing=measured):
        # A queued pass must not validate an old quote against its pre-wait
        # clock. Explicit historical/replay clocks remain deterministic.
        now = now or datetime.now(timezone.utc)
        ts = now.isoformat()
        query_started=time.monotonic()
        try:
            positions = c.execute('SELECT * FROM paper_positions ORDER BY portfolio_name,asset FOR UPDATE').fetchall()
        finally:
            measured['positions_query_seconds']=time.monotonic()-query_started
        protection_started=time.monotonic()
        for item in positions:
            z = dict(item)
            selected = quote_for_position(z,quotes.get(z['asset']),now)
            # Validate the same strict exit tuple before path/protection writes;
            # a delayed research allowance cannot authorize protective mutation.
            q = exit_execution_quote(dict(z,_execution_quote=selected),now=now)
            if not q:
                continue
            z.update(_execution_quote=q,_execution_quote_frozen=True)

            # R55: persist lifetime excursion from the independent fresh
            # quote before any partial reduction / exit mutates the position.
            zp=payload_of(z)
            try:
                _entry=float(z.get('avg_entry_price') or zp.get('entry_price') or 0.0)
                _px=float((q or {}).get('price') or 0.0)
                if _entry>0 and _px>0:
                    _signed=100.0*((_px/_entry-1.0) if z.get('direction')=='LONG'
                                    else (_entry/_px-1.0))
                    _path={
                      'observation_path':VOP.observe(z,q,ts,lane='PROTECTIVE_GUARD'),
                      'price_source_lock':VPS.position_identity(z),
                      'price_source_status':'OK',
                      'source_locked_mark':{'identity':VPS.identity(z['asset'],q),
                                            'price':_px,'observed_at':q['observed_at']},
                      'mfe_pct':max(float(zp.get('mfe_pct') or 0.0),_signed,0.0),
                      'mae_pct':min(float(zp.get('mae_pct') or 0.0),_signed,0.0),
                      'r55_lifetime_mfe_pct':max(float(zp.get('r55_lifetime_mfe_pct') or 0.0),
                                                 float(zp.get('mfe_pct') or 0.0),_signed,0.0),
                      'r55_lifetime_mae_pct':min(float(zp.get('r55_lifetime_mae_pct') or 0.0),
                                                 float(zp.get('mae_pct') or 0.0),_signed,0.0),
                      'r55_last_path_mark_at':ts,
                      'r55_last_path_mark_price':_px,
                    }
                    # Optional telemetry must not poison the book transaction
                    # and suppress a protective exit if metadata cannot be saved.
                    with c.transaction():
                        c.execute(
                            "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                            "WHERE active_trade_id=%s",(json.dumps(_path,allow_nan=False),z.get('active_trade_id'))
                        )
                        if z.get('active_trade_id'):
                            c.execute(
                                "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                                "WHERE trade_id=%s",(json.dumps(_path,allow_nan=False),z.get('active_trade_id'))
                            )
                    zp.update(_path); z['payload']=zp
            except Exception:
                pass

            fees_paid=0.0
            _lock_trade={}
            if z.get('active_trade_id'):
                try:
                    _tr=c.execute("SELECT fees_rub,funding_rub,gross_pnl_rub FROM paper_trades WHERE trade_id=%s",
                                  (z.get('active_trade_id'),)).fetchone()
                    _lock_trade=dict(_tr or {})
                    fees_paid=float((_tr or {}).get('fees_rub') or 0.0)
                except Exception:
                    fees_paid=0.0

            # R63: leave real execution room around the soft profit lock.
            _rearm_after=float(zp.get('r63_profit_lock_rearm_after_pct') or 0.0)
            try:
                _entry_lock=float(z.get('avg_entry_price') or zp.get('entry_price') or 0.0)
                _px_lock=float((q or {}).get('price') or 0.0)
                _cur_lock_pct=100.0*((_px_lock/_entry_lock-1.0) if z.get('direction')=='LONG'
                                     else (_entry_lock/_px_lock-1.0)) if _entry_lock>0 and _px_lock>0 else 0.0
            except Exception:
                _cur_lock_pct=0.0
            # R65: BTC/ETH protective fills use a live top-of-book execution model.
            # A 10bp synthetic slippage + 10bp profit cushion delayed protection
            # until ~30bp and allowed 26-28bp MFE to round-trip into fee losses.
            # Keep the universal 25bp activation floor, but use a realistic 2.5bp
            # slippage allowance and 2.5bp positive-net cushion for crypto.
            _crypto_lock=str(z.get('asset') or '') in ('BTC','ETH')
            _lock_slippage=VC.SLIPPAGE_RATE
            _lock_min_net=.00025 if _crypto_lock else .0010
            if _crypto_lock:
                _nav=max(float(zp.get('entry_nav_rub') or 1_000_000.0),1.0)
                _fraction=abs(float(z.get('units') or 0.0)*float(q.get('price') or 0.0))/_nav
                _fill=exit_fill({**z,'_execution_quote':q},float(q['price']),_fraction,ts)
                # Includes actual spread, residual slippage and size impact.
                _lock_slippage=max(_lock_slippage,float(_fill['adverse_fill_bps'])/10000.0)
            lock = None if (_rearm_after and _cur_lock_pct<_rearm_after) else profit_lock_stop(
                z,q,getattr(vp,'COMMISSION',VC.COMMISSION_RATE),fees_paid_rub=fees_paid,
                slippage_pct=_lock_slippage,min_net_pct=_lock_min_net,
                funding_rub=_lock_trade.get('funding_rub',0.0),
                realized_gross_rub=_lock_trade.get('gross_pnl_rub',0.0)
            )
            # Legacy event prefixes cannot disable checked profit protection.
            # New same-TF positions use confirmed structural trailing, not synthetic locks.
            if lock:
                pl_patch = {
                    'trailing_stop': lock['stop_price'],
                    'r48_profit_lock_active': True,
                    'r48_profit_lock_at': ts,
                    'r48_profit_lock_activation_pct': lock['activation_profit_pct'],
                    'r48_profit_lock_locked_pct': lock['locked_profit_pct'],
                    'r48_profit_lock_seen_profit_pct': lock['current_profit_pct'],
                    'r48_profit_lock_policy': lock['policy'],
                    'r55_net_profit_lock_active': True,
                    'r55_fees_paid_rub': lock.get('fees_paid_rub'),
                    'r55_estimated_exit_fee_rub': lock.get('estimated_exit_fee_rub'),
                    'r55_estimated_slippage_rub': lock.get('estimated_slippage_rub'),
                    'r55_minimum_net_profit_rub': lock.get('minimum_net_profit_rub'),
                    'r55_projected_net_profit_at_stop_rub': lock.get('projected_net_profit_at_stop_rub'),
                }
                c.execute(
                    "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                    "WHERE active_trade_id=%s",
                    (json.dumps(pl_patch), z.get('active_trade_id'))
                )
                if z.get('active_trade_id'):
                    c.execute(
                        "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                        "WHERE trade_id=%s",
                        (json.dumps(pl_patch), z.get('active_trade_id'))
                    )
                zp=payload_of(z)
                zp.update(pl_patch)
                z['payload']=zp
                changes.append({
                    'portfolio': z.get('portfolio_name'),
                    'asset': z.get('asset'),
                    'trade_id': z.get('active_trade_id'),
                    'reason': 'PROFIT_LOCK_UPDATED',
                    'price': float((q or {}).get('price') or 0.0),
                    'new_stop_price': lock['stop_price'],
                    'activation_profit_pct': lock['activation_profit_pct'],
                    'locked_profit_pct': lock['locked_profit_pct'],
                    'seen_profit_pct': lock['current_profit_pct'],
                    'policy': lock['policy'],
                    'fees_paid_rub': lock.get('fees_paid_rub'),
                    'projected_net_profit_at_stop_rub': lock.get('projected_net_profit_at_stop_rub'),
                })

            reason = protective_reason(z, q, now)
            if not reason:
                continue
            name, tid, px = z['portfolio_name'], z['active_trade_id'], float(q['price'])
            p, pos = vp._portfolio_rows(c, name)
            # Accrue funding up to this exit exactly once under the same book lock.
            prices = {x['asset']: float(x['last_price']) for x in pos}
            prices[z['asset']] = px
            vp._apply_funding(c, p, pos, prices, p.get('last_ruonia'), ts)
            c.execute('UPDATE paper_portfolios SET last_mark_at=%s WHERE name=%s', (ts, name))
            p, pos = vp._portfolio_rows(c, name)
            nav, _, _, _ = vp._mark_nav(p, pos, prices)
            if reason=='STOP' and z.get('active_trade_id'):
                _tr_full=c.execute("SELECT gross_pnl_rub,fees_rub,funding_rub FROM paper_trades WHERE trade_id=%s",
                                   (z.get('active_trade_id'),)).fetchone()
                _soft=_r63_soft_profit_stop_assessment(
                    z,q,_tr_full,nav,getattr(vp,'COMMISSION',VC.COMMISSION_RATE))
                if _soft.get('suppress'):
                    _p=payload_of(z)
                    try:
                        _entry=float(z.get('avg_entry_price') or _p.get('entry_price') or 0.0)
                        _px=float(q.get('price') or 0.0)
                        _pct=100.0*((_px/_entry-1.0) if z.get('direction')=='LONG'
                                    else (_entry/_px-1.0)) if _entry>0 and _px>0 else 0.0
                    except Exception:
                        _pct=0.0
                    _rearm=max(_pct+0.10,float(_p.get('r63_profit_lock_rearm_after_pct') or 0.0))
                    _patch={'trailing_stop':None,'r48_profit_lock_active':False,
                            'r55_net_profit_lock_active':False,
                            'r63_profit_lock_rearm_after_pct':_rearm,
                            'r63_last_soft_stop_suppressed_at':ts,
                            'r63_last_soft_stop_projected_net_rub':_soft.get('net_pnl_rub'),
                            'r63_last_soft_stop_fill_price':_soft.get('fill_price')}
                    c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE active_trade_id=%s",
                              (json.dumps(_patch),z.get('active_trade_id')))
                    c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                              (json.dumps(_patch),z.get('active_trade_id')))
                    changes.append({'portfolio':name,'asset':z.get('asset'),'trade_id':z.get('active_trade_id'),
                                    'reason':'PROFIT_LOCK_SOFT_STOP_NET_NEGATIVE_REARMED',
                                    'price':px,'projected_net_pnl_rub':_soft.get('net_pnl_rub'),
                                    'modeled_fill_price':_soft.get('fill_price'),
                                    'rearm_after_profit_pct':_rearm})
                    continue
            vp._v90j_update_excursions(c, name, prices, ts)
            patch = {'last_guard_market_observed_at': q['observed_at'],
                     'last_guard_checked_at': ts, 'protective_exit_lane': 'INDEPENDENT_PAPER_GUARD'}
            c.execute("UPDATE paper_positions SET payload=payload||%s::jsonb WHERE active_trade_id=%s",
                      (json.dumps(patch), tid))
            c.execute("UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id=%s", (json.dumps(patch), tid))
            z['_execution_quote']=dict(q)
            result = vp._close_or_reduce(c, p, name, z, px, 0.0, nav, ts, reason)
            if not result:
                continue
            p, pos = vp._portfolio_rows(c, name)
            nav, unreal, gross, net = vp._mark_nav(p, pos, prices)
            hwm = max(float(p['high_water_nav_rub']), nav)
            c.execute('UPDATE paper_portfolios SET high_water_nav_rub=%s,updated_at=%s WHERE name=%s', (hwm, ts, name))
            c.execute('''INSERT INTO paper_nav_history(portfolio_name,observed_at,nav_rub,nav_usd,
                         benchmark_nav_rub,gross_leverage,net_exposure,drawdown,ruonia,usdrub,payload)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                         ON CONFLICT(portfolio_name,observed_at) DO UPDATE SET nav_rub=EXCLUDED.nav_rub,
                         nav_usd=EXCLUDED.nav_usd,gross_leverage=EXCLUDED.gross_leverage,
                         net_exposure=EXCLUDED.net_exposure,drawdown=EXCLUDED.drawdown,payload=EXCLUDED.payload''',
                      (name, ts, nav, nav / float(p['last_usdrub']) if p.get('last_usdrub') else None,
                       p['benchmark_nav_rub'], gross, net, max(0, 1-nav/max(hwm, 1)), p.get('last_ruonia'),
                       p.get('last_usdrub'), json.dumps({'unrealized_pnl_rub': unreal, 'protective_guard': True})))
            changes.append({'portfolio': name, 'asset': z['asset'], 'trade_id': tid,
                            'reason': reason, 'price': px, 'market_observed_at': q['observed_at']})
        if changes:
            VPP.refresh(c, commission=getattr(vp, 'COMMISSION', VC.COMMISSION_RATE))
        measured['protection_seconds']=time.monotonic()-protection_started
    return changes


# Pre-pass quote projection at the same baseline commit.
QUOTE_POSITION_SQL = """SELECT asset,active_trade_id,jsonb_build_object(
    'price_source_lock',payload->'price_source_lock',
    'contract_identity',payload->'contract_identity',
    'entry_primary_source',payload->'entry_primary_source',
    'entry_contract_secid',payload->'entry_contract_secid',
    'source_locked_mark',jsonb_build_object('observed_at',payload->'source_locked_mark'->'observed_at'),
    'entry_execution_observed_at',payload->'entry_execution_observed_at',
    'entry_market_observed_at',payload->'entry_market_observed_at') AS payload
    FROM paper_positions"""
