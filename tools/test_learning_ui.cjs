// Exercise the actual dashboard script against its public scorecard contract.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('veritas_v90_ui.py', 'utf8');
const script = source.split('<script>')[1]?.split('</script>')[0];
assert.ok(script && /\}\)\(\);\s*$/.test(script), 'Canonical script is present');
const elements = {}, requests = [], timers = new Set();
let nextTimer = 0, responses = {}, intervals = 0;
const context = vm.createContext({
  document: {readyState:'loading', addEventListener(){}, querySelectorAll:()=>[],
    getElementById:id=>elements[id]||={}},
  AbortController,
  setTimeout(){const id=++nextTimer;timers.add(id);return id},
  clearTimeout(id){timers.delete(id)}, setInterval(){intervals++},
  fetch: async url => {requests.push(url);const value=responses[url];
    return {ok:value!==undefined, status:value===undefined?503:200, json:async()=>value}},
});
vm.runInContext(script.replace(/\}\)\(\);\s*$/,
  'globalThis.ui={st,deepNum,normalizeIntelligence,renderIntelligence,autonomousLearningHtml,learningWaitText,loadIntelligence};})();'),
  context, {timeout:2000});
const ui=context.ui;
const autonomous={version:'AUTONOMOUS_LEARNING_V1',status:'evaluating',
  updated_at:'2026-10-07T12:00:00Z',counts:{direction:37,trade:5},
  candidates:[{state:'evaluating',reasons:['INSUFFICIENT_TEMPORAL_COVERAGE']}],
  profiles:[
    {state:'promoted',evidence_valid:true,valid_until:'2999-10-08T00:00:00Z'},
    {state:'promoted',evidence_valid:true,valid_until:'2000-10-08T00:00:00Z'},
    {state:'promoted',evidence_valid:false,valid_until:'2999-10-08T00:00:00Z'},
    {state:'candidate',evidence_valid:true,valid_until:'2999-10-08T00:00:00Z'},
  ],
  continuous:{ready:true,last_success_at:'2026-10-07T13:04:00Z'},jobs:{},
};
const raw={status:'OK',autonomous_learning:autonomous,asset_management_intelligence:{
  status:'OK',version:'ami-v1.1',score:42.5,score_status:'PARTIAL_EVIDENCE',confidence:'LOW',
  components:{decision_intelligence:18,portfolio_outcome_quality:15,
    movement_risk_management:null,knowledge_application:4.5,experience_depth:5,
    self_learning_effectiveness:null},
  component_status:{self_learning_effectiveness:{status:'BUILDING'},
    movement_risk_management:{status:'BUILDING'},experience_depth:{status:'PARTIAL'}},
  coverage:{available_components:4,total_components:6,observed_max_points:60,total_max_points:100,score_renormalized:false},
  evidence:{learning_episodes:{n:0,avg_capture_ratio:null}},benchmarks:{},
}};
function render(value=raw){ui.st.intelligence=ui.normalizeIntelligence(value,null,null);ui.renderIntelligence();return elements.intelligence.innerHTML}
let html=render();
const component=(html,key)=>html.split('data-component="'+key+'"')[1]?.split('</div></div></div>')[0];
assert.match(component(html,'self_learning_effectiveness'),/Накапливается/);
assert.doesNotMatch(component(html,'self_learning_effectiveness'),/0\.0 \/ 15/);
assert.match(component(html,'movement_risk_management'),/Накапливается/);
assert.match(html,/>42\.5<\/div>/); // Missing evidence cannot inflate remaining weights.
assert.match(html,/из 100/);assert.match(html,/4 из 6 разделов · покрытие 60%/);
assert.match(html,/от запуска индекса — п\./);
assert.match(html,/Средний захват движения<b>—/);
assert.match(html,/42 наблюдений/);
assert.match(html,/Подтверждённые поправки<\/span><b>1<\/b>/);
assert.match(html,/07\.10, 16:04 МСК/);
assert.match(html,/больше разных дней/);
assert.match(html,/само по себе не доказывает рост доходности/);
assert.doesNotMatch(html,/NaN|undefined|\+—/);
assert.equal(ui.deepNum({index_vs_start:null},['index_vs_start']),null);
assert.equal(ui.deepNum({index_vs_start:0},['index_vs_start']),0);

const measured=structuredClone(raw);
measured.asset_management_intelligence.components.self_learning_effectiveness=0;
measured.asset_management_intelligence.component_status.self_learning_effectiveness={status:'PARTIAL'};
html=render(measured);
assert.match(component(html,'self_learning_effectiveness'),/0\.0 \/ 15/);
assert.doesNotMatch(component(html,'self_learning_effectiveness'),/Накапливается/);

const unavailable=structuredClone(raw);
unavailable.asset_management_intelligence.component_status.self_learning_effectiveness={status:'UNAVAILABLE'};
html=render(unavailable);
assert.match(component(html,'self_learning_effectiveness'),/Обновление задержано/);
unavailable.asset_management_intelligence.status='UNAVAILABLE';
html=render(unavailable);
assert.match(html,/Оценка интеллекта пока недоступна/);
assert.match(html,/42 наблюдений/); // Autonomous state survives an unavailable score.
assert.doesNotMatch(html,/>42\.5<\/div>/);
assert.equal(ui.normalizeIntelligence(null,null,null).score,null);
assert.equal(ui.normalizeIntelligence({status:'UNAVAILABLE'},null,null).score,null);

for(const [snapshot,expected] of [
  [{jobs:{learning:{status:'DEFERRED_MEMORY',last_result:{reason:'DEFERRED_MEMORY'}}}},/запас памяти/],
  [{jobs:{learning:{status:'RETRY',last_result:{reason:'DURABLE_JOB_LEASE_BUSY'}}}},/другая фоновая задача/],
  [{last_error:'QueryCanceled: SELECT secret FROM private_table'},/повтор запланирован/],
  [{jobs:{learning:{status:'RETRY',last_result:{reason:'LEARNING_BOOTSTRAP_PENDING'}}}},/Восстанавливаются/],
])assert.match(ui.learningWaitText(snapshot),expected);
const unsafe=ui.autonomousLearningHtml({...autonomous,last_error:'<script>alert("secret")</script>'});
assert.doesNotMatch(unsafe,/<script>|secret|SELECT/);
const epoch=Date.parse('2026-10-07T14:05:00Z')/1000;
assert.match(ui.autonomousLearningHtml({status:'collecting',jobs:{learning:{last_success_at:epoch}}}),/17:05 МСК/);

(async()=>{
  responses={'/api/v1/intelligence-scorecard':raw,
    '/api/v1/learning-progress':{status:'BUILDING',index_vs_start:null},
    '/api/v1/library-summary':{status:'OK'}};
  await ui.loadIntelligence();
  assert.deepEqual(requests,['/api/v1/intelligence-scorecard','/api/v1/learning-progress','/api/v1/library-summary']);
  assert.equal(ui.st.intelligence.autonomous_learning.counts.direction,37);
  assert.equal(intervals,0);assert.equal(timers.size,0);
  responses={};await ui.loadIntelligence();
  assert.equal(ui.st.intelligence.score,42.5);
  assert.match(elements.intelligence.innerHTML,/Показан последний полученный результат/);
  assert.match(elements.intelligence.innerHTML,/42 наблюдений/);
  assert.equal(requests.length,6);assert.equal(intervals,0);assert.equal(timers.size,0);
  console.log('Learning UI: missing versus measured zero, fixed score/coverage, autonomous proof/expiry, errors and existing polling passed');
})().catch(error=>{console.error(error);process.exitCode=1});
