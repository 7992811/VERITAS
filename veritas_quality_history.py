"""Exact bounded quality history and request-local pure-analysis reuse.

The selected trade population and order sums are unchanged. Evidence projection
runs only after the original CLOSED/order/5001 boundary; memo entries never
retain a second copy of source rows or proof graphs.
"""
import veritas_learning_integrity as LI
from contextlib import nullcontext
import time
import traceback
from veritas_maintenance import MaintenanceDeferred

SELECT_TRADES='''SELECT t.trade_id,t.portfolio_name,t.asset,t.direction,t.status,t.horizon,
 t.opened_at,t.closed_at,t.avg_entry_price,t.avg_exit_price,t.max_fraction,
 t.gross_pnl_rub,t.fees_rub,t.funding_rub,t.net_pnl_rub,
 ('''+LI.payload_sql('t',root_field=lambda key:'quality_payload.'+key)+''' || jsonb_build_object(
 'strategy_epoch',quality_payload.strategy_epoch,'strategy_entry_sha',quality_payload.strategy_entry_sha,
 'strategy_policy_hash',quality_payload.strategy_policy_hash,
 'strategy_policy_hash_version',quality_payload.strategy_policy_hash_version,
 'strategy_role',quality_payload.strategy_role,'idea_id',quality_payload.idea_id,
 'idea_id_verified',quality_payload.idea_id_verified,
 'posttrade_review',jsonb_build_object('input_hash',quality_payload.posttrade_review->'input_hash'))) AS payload,
 o.entry_notional_rub,o.entry_order_count
 FROM paper_trades t CROSS JOIN LATERAL jsonb_to_record(
 CASE WHEN jsonb_typeof(t.payload)='object' THEN t.payload ELSE '{}'::jsonb END) AS quality_payload(
 data_integrity_status jsonb,entry_primary_source jsonb,entry_data_latency_class jsonb,recovered jsonb,learning_eligible jsonb,
 exit_reason jsonb,close_reason jsonb,idea_event_id jsonb,r66_event_id jsonb,mfe_pct jsonb,mae_pct jsonb,
 r55_lifetime_mfe_pct jsonb,r55_lifetime_mae_pct jsonb,initial_stop_price jsonb,entry_atr jsonb,execution_timeframe jsonb,
 execution_horizon jsonb,atr_timeframe jsonb,stop_timeframe jsonb,target_timeframe jsonb,
 structural_timeframe jsonb,trigger_timeframe jsonb,price_source_lock jsonb,
 entry_execution_source_identity jsonb,last_exit_source_identity jsonb,contract_identity jsonb,entry_source_names jsonb,
 source_locked_mark jsonb,entry_event_snapshot jsonb,entry_execution_model jsonb,last_exit_execution_model jsonb,
 observation_path jsonb,strategy_epoch jsonb,strategy_entry_sha jsonb,strategy_policy_hash jsonb,
 strategy_policy_hash_version jsonb,strategy_role jsonb,idea_id jsonb,idea_id_verified jsonb,posttrade_review jsonb) LEFT JOIN (
 SELECT trade_id,SUM(notional_rub) AS entry_notional_rub,COUNT(*) AS entry_order_count
 FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') GROUP BY trade_id
 ) o ON o.trade_id=t.trade_id'''


def history_query(select_trades=SELECT_TRADES):
    # Materialize only IDs/clocks, never entire trade payloads. Aggregate every
    # entry/add order of those trades, including orders outside the trade window.
    selected = """WITH quality_selected AS MATERIALIZED (
        SELECT trade_id,closed_at FROM paper_trades
        WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 5001
    ) """
    query = select_trades.replace('FROM paper_trades t',
        'FROM quality_selected selected JOIN paper_trades t ON t.trade_id=selected.trade_id')
    query = query.replace("FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') GROUP BY trade_id",
        "FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') "
        "AND trade_id IN (SELECT trade_id FROM quality_selected) GROUP BY trade_id")
    return selected + query + " ORDER BY t.closed_at DESC"


class AnalysisContext:
    """One portfolio/report pass only; cache values, not mutable input graphs."""
    def __init__(self, review, idea_key):
        self._review, self._idea_key = review, idea_key
        self._reviews, self._ideas = {}, {}

    def idea(self, trade):
        key = id(trade)
        if key not in self._ideas:
            self._ideas[key] = self._idea_key(trade)
        return self._ideas[key]

    def review(self, trade):
        key = id(trade)
        if key not in self._reviews:
            self._reviews[key] = self._review(trade)
        return self._reviews[key]


class ReviewCollector:
    """First ten dirty reviews in original SQL order, including legacy books."""
    def __init__(self, trades, review, payload):
        self._order = {id(trade): index for index, trade in enumerate(trades)}
        self._review, self._payload = review, payload
        self._visited, self._dirty = set(), []

    def accept(self, trade, record):
        key = id(trade)
        self._visited.add(key)
        saved = (self._payload(trade.get('payload')).get('posttrade_review') or {}).get('input_hash')
        if saved != record['input_hash']:
            self._dirty.append((self._order[key], trade, record))
            self._dirty.sort(key=lambda item:item[0])
            del self._dirty[10:]

    def pending(self, trades):
        for trade in trades:
            if id(trade) not in self._visited:
                self.accept(trade, self._review(trade))
        return [(trade, record) for _, trade, record in self._dirty]


def emit_review(emit, event, **values):
    """Observability failure cannot discard a completed review or stop its worker."""
    if emit:
        try:
            emit(event, **values)
        except Exception:
            pass


def guarded_refresh(cache, lock, refresh, emit=None, resource_guard=None):
    """Run one review without overlapping other admitted history work.

    Catch/clear failed refresh frames *inside* the permit, so an exception's
    traceback cannot retain selected rows after another history reader starts.
    Deferral never changes the last completed result, its age or prior error.
    """
    state = {'status':'RUNNING', 'attempted_at_monotonic':time.monotonic()}
    with lock:
        cache['refresh_state'] = dict(state)
    try:
        with resource_guard('strategy_quality_review') if resource_guard else nullcontext():
            result = None
            try:
                result = refresh()
                status = result.get('status') if isinstance(result,dict) else None
                state.update(status=status if status in ('OK','PARTIAL_HISTORY') else 'ERROR')
                if state['status']=='ERROR':
                    state['error_code']='INVALID_REVIEW_RESULT'
            except MaintenanceDeferred as error:
                state.update(error.result(), reason=error.status)
                traceback.clear_frames(error.__traceback__)
            except Exception as error:
                state.update(status='ERROR', error_code=type(error).__name__)
                traceback.clear_frames(error.__traceback__)
            finally:
                result = None
    except MaintenanceDeferred as error:
        state.update(error.result(), reason=error.status)
    except Exception as error:
        state.update(status='ERROR', error_code=type(error).__name__)
    state['finished_at_monotonic'] = time.monotonic()
    with lock:
        cache['refresh_state'] = state
        if state['status']=='ERROR':
            cache['last_error'] = state['error_code']
    if state['status']=='ERROR':
        emit_review(emit, 'strategy_quality_review_error', error_code=state['error_code'], shadow_only=True)
    elif state['status'].startswith('DEFERRED'):
        emit_review(emit, 'strategy_quality_review_deferred', **state, shadow_only=True)
