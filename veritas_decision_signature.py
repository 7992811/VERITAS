"""Stable signatures for compact VERITAS durable events."""
import hashlib
import json


def decision_signature(event_type, payload):
    p=payload or {}
    if event_type=='decision':
        plan=p.get('trade_plan') or {}
        inst=p.get('institutional_signal') or {}
        bq=inst.get('breakout_quality') or {}
        raw={
          'research_decision':p.get('research_decision'),'decision':p.get('decision'),
          'signal_tier':p.get('signal_tier'),'decision_stage':p.get('decision_stage'),
          'regime':p.get('regime'),'eligible':plan.get('eligible'),
          'plan_reason':plan.get('reason'),'entry_quality':plan.get('entry_quality'),
          'breakout_state':bq.get('state'),
          'paper_eligible':(p.get('execution_eligibility') or {}).get('paper_eligible'),
          'source_gate':(p.get('gates') or {}).get('source'),
          'market_open':(p.get('gates') or {}).get('time'),
        }
    else:
        gate=p.get('gate') or {}
        raw={
          'setup':p.get('setup'),'direction':p.get('direction'),
          'research_direction':p.get('research_direction'),
          'candidate_direction':p.get('candidate_direction'),
          'active':p.get('active'),'state':p.get('state'),
          'status':p.get('status'),'label':p.get('label'),
          'old_plan_reason':p.get('old_plan_reason'),
          'gate_status':gate.get('status') if isinstance(gate,dict) else None,
        }
    return hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:20]
