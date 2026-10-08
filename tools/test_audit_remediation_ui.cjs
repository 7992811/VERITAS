const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const script=fs.readFileSync('veritas_v90_ui.py','utf8').split('<script>')[1].split('</script>')[0];
const elements={},responses={},requests=[];
const context=vm.createContext({document:{readyState:'loading',visibilityState:'visible',addEventListener(){},querySelectorAll:()=>[],getElementById:id=>elements[id]||={}},
 AbortController,setTimeout(){return 1},clearTimeout(){},
 fetch:async url=>{requests.push(url);return{ok:responses[url]!==undefined,json:async()=>responses[url]}}});
vm.runInContext(script.replace(/\}\)\(\);\s*$/,'globalThis.ui={st,signalEstimate,renderSignals,renderPortfolios,renderIntelligence,loadAutonomousLearning,loadStrategyQuality,autonomousLearningHtml};})();'),context);
const ui=context.ui;
assert.equal(ui.signalEstimate(.5,'MODEL_QUALITY_SCORE_UNCALIBRATED').text,'50.0 / 100');
assert.equal(ui.signalEstimate(.5,'MODEL_QUALITY_SCORE_UNCALIBRATED').label,'Оценка сигнала');
assert.equal(ui.signalEstimate(.67,'EMPIRICAL_CALIBRATION').text,'67.0% · эмпир.');
for(const value of [null,'',true,NaN,2,-1])assert.equal(ui.signalEstimate(value,'EMPIRICAL_CALIBRATION').text,'—');
assert.equal(ui.signalEstimate(.5,'UNKNOWN').text,'—');
ui.st.signals={signals:[{asset:'ETH',horizon:'1m',research_decision:'NO_TRADE',source_gate_pass:true,snapshot_stale:true}]};
ui.renderSignals();assert.match(elements.cells.textContent,/проверено 0 · устарело 1/);assert.doesNotMatch(elements.cells.className,/ok/);
assert.match(ui.autonomousLearningHtml({status:'collecting',last_error:null,operational:{status:'DEGRADED'},jobs:{score:{status:'ERROR'}}}),/class="warn">Частичный сбой/);
(async()=>{
 responses['/api/v1/autonomous-learning']={status:'collecting',counts:{processed:426},continuous:{ready:true},operational:{status:'WAITING_RESOURCES'}};
 ui.st.intelligence={score:null};await ui.loadAutonomousLearning();
 assert.match(elements.intelligence.innerHTML,/426 наблюдений/);
 assert.match(elements.intelligence.innerHTML,/Ожидает ресурсы/);
 assert.deepEqual(requests,['/api/v1/autonomous-learning']);
 ui.st.strategyQuality={status:'OK',portfolios:[{name:'Champion',cohorts:{}}]};
 responses['/api/v1/strategy-quality']={status:'WARMING_UP',portfolios:[],refresh:{processed:16}};
 await ui.loadStrategyQuality();assert.equal(ui.st.strategyQuality.status,'STALE');assert.equal(ui.st.strategyQuality.portfolios[0].name,'Champion');
 context.document.visibilityState='hidden';const prior=requests.length;await ui.loadAutonomousLearning();assert.equal(requests.length,prior);
 console.log('Audit UI: source-aware probability, stale data, independent learning and last-good quality passed');
})().catch(error=>{console.error(error);process.exitCode=1});
