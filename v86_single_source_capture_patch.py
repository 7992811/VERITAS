from pathlib import Path
import sys

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()

# A) One fresh primary source is sufficient for paper execution.
p=root/'veritas_v85/domain.py'
_d=p.read_text(encoding='utf-8')
_old="""        if self.source_verified is not True:
            return "SOURCE_NOT_VERIFIED"
        if not self.secondary_source or self.secondary_source == self.primary_source or self.secondary_time is None:
            return "INDEPENDENT_VERIFICATION_MISSING"
        for name, ts in (("PRIMARY", self.primary_time), ("SECONDARY", self.secondary_time)):
            age = (at - ts).total_seconds()
            if age < 0:
                return name + "_FROM_FUTURE"
            if age > self.max_age_seconds:
                return name + "_STALE"
"""
_new="""        if self.source_verified is not True:
            return "SOURCE_NOT_VERIFIED"
        # One-source paper policy: only the primary timestamp is a hard execution requirement.
        # Secondary data is a quality diagnostic and may be absent or stale without blocking.
        age = (at - self.primary_time).total_seconds()
        if age < 0:
            return "PRIMARY_FROM_FUTURE"
        if age > self.max_age_seconds:
            return "PRIMARY_STALE"
"""
if _old not in _d: raise SystemExit('SINGLE_SOURCE_QUOTE_PROBLEM_ANCHOR_NOT_FOUND')
p.write_text(_d.replace(_old,_new,1),encoding='utf-8')
print('SINGLE_SOURCE_QUOTE_PROBLEM_PATCH_OK')

p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
old="    if row.get('source_gate_pass') is not True: return 'SOURCE_GATE'\n"
new="    if row.get('source_gate_pass') is not True and row.get('execution_eligible') is not True: return 'SOURCE_GATE'\n"
if old not in s: raise SystemExit('SINGLE_SOURCE_HARD_REASON_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""    return Quote(spec.asset,decimal(raw.get('price'),positive=True),event_id or str(meta['event_id']),
                 utc(meta['primary_time']),utc(meta['secondary_time']) if meta.get('secondary_time') else None,
                 str(meta['primary_source']),meta.get('secondary_source'),meta.get('source_verified') is True,
                 raw.get('market_open') is True,float(meta['max_age_seconds']),quote_currency=spec.quote_currency)
"""
new="""    single_source_verified=bool(meta.get('primary_source') and meta.get('primary_time')
                                and raw.get('market_open') is True)
    return Quote(spec.asset,decimal(raw.get('price'),positive=True),event_id or str(meta['event_id']),
                 utc(meta['primary_time']),utc(meta['secondary_time']) if meta.get('secondary_time') else None,
                 str(meta['primary_source']),meta.get('secondary_source'),
                 bool(meta.get('source_verified') is True or single_source_verified),
                 raw.get('market_open') is True,float(meta['max_age_seconds']),quote_currency=spec.quote_currency)
"""
if old not in s: raise SystemExit('SINGLE_SOURCE_QUOTE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# B) Final execution gate: one fresh primary source is enough.
p=root/'veritas_intelligence.py'
s=p.read_text(encoding='utf-8')
override=r'''
def execution_eligibility(asset, raw, clock_info=None):
    meta=raw.get('v85_quote') or {}
    primary_time=meta.get('primary_time') or raw.get('observed_at')
    primary_source=meta.get('primary_source') or (raw.get('source_names') or {}).get('primary')
    age=_age_seconds(primary_time) if primary_time else None
    max_age=float(meta.get('max_age_seconds') or (
        120.0 if asset in CRYPTO_ASSETS else
        3600.0 if asset=='CNYRUBF' else
        MOEX_MAX_AGE_SECONDS if asset=='MOEX' else
        DELAYED_FUTURES_MAX_AGE_SECONDS))
    market_ok=bool(raw.get('market_open',True) or asset in CRYPTO_ASSETS)
    clock_ok=bool((clock_info or {}).get('ok',True))
    try: price_ok=float(raw.get('price') or 0)>0
    except Exception: price_ok=False
    fresh=bool(age is not None and age>=0 and age<=max_age)
    primary_ok=bool(primary_source and price_ok and market_ok and clock_ok and fresh)
    sec=raw.get('secondary_price',raw.get('coinbase_price'))
    sec_present=sec is not None
    return {
      'eligible':primary_ok,
      'reason':'single_primary_source_verified' if primary_ok else
               'primary_source_stale' if age is not None and not fresh else
               'market_closed' if not market_ok else
               'clock_gate_failed' if not clock_ok else
               'primary_source_unavailable',
      'direct_sources':1+(1 if sec_present else 0),
      'primary_source':primary_source,
      'primary_age_seconds':age,
      'max_age_seconds':max_age,
      'secondary_available':sec_present,
      'single_source_policy':True,
      'research_ok':primary_ok,
      'time_ok':bool(market_ok and fresh)
    }
'''
s += '\n\n'+override+'\n'
p.write_text(s,encoding='utf-8')

# C) Protect already-profitable trades after meaningful MFE and subsequent giveback.
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')
anchor="""                intent = manage(existing,signal,quote,self.instruments[asset],at,
                                portfolio_hard_stop=portfolio_hard_stop,thesis_break=thesis_break,
                                pending_exit_reason=pending["reason"] if pending else None)
                action_reason = intent.reason
"""
replacement="""                intent = manage(existing,signal,quote,self.instruments[asset],at,
                                portfolio_hard_stop=portfolio_hard_stop,thesis_break=thesis_break,
                                pending_exit_reason=pending["reason"] if pending else None)
                if intent.action == Action.HOLD and quote is not None:
                    pts=c.execute('SELECT price FROM v85_path WHERE episode_id=? ORDER BY at,event_id',
                                  (existing.episode_id,)).fetchall()
                    path_returns=[existing.direction.sign*(decimal(x['price'])/existing.entry_price-ONE) for x in pts]
                    current_ret=existing.direction.sign*(quote.price/existing.entry_price-ONE)
                    mfe=max(path_returns+[current_ret,ZERO])
                    all_in_floor=(self.instruments[asset].commission_rate*D('2'))+D('.0002')
                    mfe_trigger=max(D('.0040'),all_in_floor*D('2.5'))
                    giveback=max(ZERO,mfe-current_ret)
                    giveback_trigger=max(D('.0015'),mfe*D('.40'))
                    posrow=c.execute('SELECT entry_fee FROM v85_positions WHERE account_id=? AND asset=?',
                                     (existing.account_id,existing.asset)).fetchone()
                    eprow=c.execute('SELECT payload FROM v85_episodes WHERE episode_id=?',(existing.episode_id,)).fetchone()
                    spec=self.instruments[asset]
                    gross_if_exit=existing.direction.sign*existing.quantity*spec.multiplier*(quote.price-existing.entry_price)*quote.fx_to_nav
                    exit_fee_if_exit=existing.quantity*spec.multiplier*quote.price*quote.fx_to_nav*spec.commission_rate
                    entry_fee_if_exit=D(posrow['entry_fee']) if posrow else ZERO
                    fund_if_exit=accrued(existing,json.loads(eprow['payload']),at) if eprow else ZERO
                    legacy_carry=decimal((json.loads(eprow['payload']) if eprow else {}).get('legacy_realized_gross','0'))
                    net_if_exit=gross_if_exit-entry_fee_if_exit-exit_fee_if_exit-fund_if_exit+legacy_carry
                    if mfe>=mfe_trigger and current_ret>=all_in_floor*D('1.10') and giveback>=giveback_trigger and net_if_exit>ZERO:
                        reason='PROFIT_PROTECT_GIVEBACK'
                        iid=stable_id('INT_',existing.episode_id,existing.revision,Action.EXIT.value,
                                      reason,quote.event_id,existing.policy_version)
                        intent=replace(intent,intent_id=iid,action=Action.EXIT,reason=reason)
                action_reason = intent.reason
"""
if anchor not in s: raise SystemExit('PROFIT_CAPTURE_MANAGE_ANCHOR_NOT_FOUND')
s=s.replace(anchor,replacement,1)
p.write_text(s,encoding='utf-8')

print('V86_SINGLE_SOURCE_AND_PROFIT_CAPTURE_ACTIVE')
