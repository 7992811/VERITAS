"""Immutable execution evidence shared by the trade, position and order journals."""
import math
import veritas_trend_entry as VTE


def geometry_audit(row, fill, gate):
    plan=row.get('trade_plan') or {}
    event=(row.get('timeframe_entry_context') or plan.get('timeframe_entry_context') or {}).get('event') or {}
    price=fill.get('fill_price')
    stop=(gate.get('entry_geometry') or {}).get('stop_price')
    target=gate.get('target_price')
    atr=plan.get('atr')
    sign=1 if row.get('research_decision')=='LONG' else -1
    def divide(a,b):
        try:
            value=float(a)/float(b)
            return value if math.isfinite(value) else None
        except (TypeError,ValueError,ZeroDivisionError):
            return None
    risk=sign*(price-stop) if price is not None and stop is not None else None
    reward=sign*(target-price) if price is not None and target is not None else None
    timing=gate.get('trend_event') or {}
    return {'version':'EXECUTED_GEOMETRY_AUDIT_V1','event_id':event.get('event_id'),
            'timeframe':row.get('horizon'),'atr':atr,'atr_timeframe':plan.get('atr_timeframe'),
            'initial_stop_price':stop,'target_price':target,'actual_entry_fill':price,
            'stop_distance_atr':divide(risk,atr),'gross_reward_risk':divide(reward,risk),
            'net_reward_risk':gate.get('net_reward_risk'),
            'net_risk_pct':gate.get('net_risk_pct'),'net_reward_pct':gate.get('net_reward_pct'),
            'target_ladder':gate.get('target_ladder'),
            'target_fill_basis':gate.get('target_fill_basis'),
            'modeled_weighted_target_fill':gate.get('modeled_weighted_target_fill'),
            'trigger_timeframe':plan.get('trigger_timeframe'),
            'structural_timeframe':plan.get('structural_timeframe'),
            'signal_at':event.get('signal_at'),'entry_timing':timing,
            'quote_time_gate':gate.get('quote_time_gate'),
            'stop_anchor_price':event.get('stop_anchor'),
            'stop_level_available_at':event.get('stop_level_available_at')}


def entry_payload(row, asset, direction, nav, ts, source_lock, quote, fill, final_gate,
                  intent, teaching_trace, canonical_setup_id, cohort, model_version, units):
    plan=row.get('trade_plan') or {}
    price=float(quote['price'])
    stop=(final_gate.get('execution_snapshot') or {}).get('stop_price')
    target=final_gate.get('target_price')
    entry_integrity=row.get('data_integrity_status')
    if entry_integrity is None or (isinstance(entry_integrity,str) and not entry_integrity.strip()):
        entry_integrity='OK'
    import veritas_structural_lifecycle as VSL
    return {'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],
                 **VSL.entry_metadata(row,units),
                 'price_source_lock':source_lock,'price_source_status':'OK',
                 'data_integrity_status':entry_integrity,
                 'entry_execution_source_identity':source_lock,
                 'structural_policy_version':plan.get('structural_policy_version'),
                 'execution_snapshot':final_gate.get('execution_snapshot'),
                 'entry_stop_risk_budget':row.get('_stop_risk_budget'),
                 'entry_geometry_audit':geometry_audit(row,fill,final_gate),
                 'user_teaching_id':plan.get('user_teaching_id'),'user_teaching_trace':teaching_trace,
                 'entry_event_snapshot':plan.get('entry_event_snapshot'),
                 'timeframe_entry_context':plan.get('timeframe_entry_context'),
                 'entry_canonical_admission':row.get('_canonical_admission'),
                 'fill_economics_gate':final_gate,
                 'initial_stop_price':stop,'take_price':target,
                 'target_price':target,'initial_take_price':target,
                 'stop_timeframe':plan.get('stop_timeframe',row.get('horizon')),
                 'target_timeframe':plan.get('target_timeframe',row.get('horizon')),
                 'atr_timeframe':plan.get('atr_timeframe',row.get('horizon')),'entry_atr':plan.get('atr'),
                 'entry_decision_snapshot':{'asset':asset,'horizon':row.get('horizon'),
                     'research_decision':direction,'signal_tier':row.get('signal_tier'),
                     'confidence':row.get('confidence'),'market_observed_at':quote['observed_at'],
                     'trade_plan':dict(plan)},
                 'entry_primary_source':source_lock['primary_source'],
                 'entry_execution_observed_at':quote['observed_at'],
                 'source_locked_mark':{'identity':source_lock,'price':price,'observed_at':quote['observed_at']},
                 'entry_rule_revision':cohort,'execution_cohort':cohort,
                 'production_evidence_epoch':str(ts),
                 'canonical_setup_id':canonical_setup_id,
                 'experience_decision':plan.get('experience_decision'),
                 'setup_memory':plan.get('setup_memory'),
                 'adaptive_regime_policy':plan.get('adaptive_regime_policy'),
                 'execution_policy':plan.get('execution_policy'),
                 'independent':((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'),
                 'model_version':model_version,'setup_id':plan.get('setup_id'),'execution_horizon':row.get('horizon'),
                 'structural_stop_enforced':bool(plan.get('structural_stop_enforced')),
                 'soft_invalidation_count':0,'entry_permission':(plan.get('trade_integrity') or {}).get('entry_permission'),
                 'entry_execution_model':fill,'client_order_id':intent.client_order_id,
                 'entry_timing':row.get('_r65_entry_timing'),
                 'r66_event_id':(VTE.context_of(row).get('event') or {}).get('event_id'),
                 'r66_entry_geometry':plan.get('r66_geometry'),
                 'normalized_paper_notional':True,
                 'quantity_semantics':'NORMALIZED_PAPER_RETURN_UNITS',
                 'normalized_units':units,
                 'broker_quantity':None,
                 'broker_quantity_source':None}
