// Regression checks for asynchronous dashboard reads; no browser or network needed.
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync(0,'utf8');
let script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
script=script.replace("if(document.readyState==='loading')", "globalThis.testUI={st,start,loadBootstrap,loadPortfolios,loadTrades,applyBootstrap};if(document.readyState==='loading')");
const names=['Impulse','Aggressive','Champion','Challenger'];
const position={asset:'GOLD',direction:'SHORT',active_trade_id:'gold-1',avg_entry_price:4136,last_price:4129,target_fraction:.1,payload:{price_source_lock:{primary_source:'ProFinance GOLD'}}};
const portfolio=(open=true)=>({status:'OK',portfolios:names.map((name,i)=>({name,positions:open&&i===0?[position]:[],gross_leverage:open&&i===0?.1:0,closed_trades:2,wins:1}))});
const trades={status:'OK',trades:[{trade_id:'closed-1',portfolio_name:'Impulse',asset:'GOLD',direction:'LONG',status:'CLOSED',avg_entry_price:4000,avg_exit_price:4020,net_pnl_rub:10}]};
function harness(){
  let clock=0,id=0;const timers=new Map(),nodes=new Map(),responses=new Map(),calls=[];
  const node=key=>{if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',className:'',scrollIntoView(){}});return nodes.get(key)};
  const context=vm.createContext({console,AbortController,Date,Set,Map,Promise,
    document:{readyState:'loading',getElementById:node,querySelectorAll:()=>[],addEventListener(){}},
    window:{addEventListener(){}},localStorage:{getItem:()=>null,setItem(){}},
    setTimeout:(fn,ms)=>{const key=++id;timers.set(key,{at:clock+ms,fn});return key},
    clearTimeout:key=>timers.delete(key),setInterval:()=>0,
    fetch:(url,{signal})=>{calls.push(url);const r=responses.get(url);return new Promise((resolve,reject)=>{
      const finish=()=>r&&r.error?reject(new Error('temporary outage')):resolve({ok:true,json:async()=>r?.data});
      if(r?.delay){timers.set(++id,{at:clock+r.delay,fn:finish});}else finish();
      signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});
    })}
  });vm.runInContext(script,context);
  return {ui:context.testUI,nodes,node,responses,calls,async advance(ms){clock+=ms;for(const [key,t] of [...timers])if(t.at<=clock){timers.delete(key);t.fn()}for(let i=0;i<30;i++)await Promise.resolve()}};
}
(async()=>{
  const h=harness(),u=h.ui;
  h.responses.set('/api/v1/paper-portfolios',{data:portfolio(),delay:23000});
  h.responses.set('/api/v1/portfolio-trades',{data:trades,delay:20000});
  h.responses.set('/api/v1/dashboard-bootstrap?view=signals',{error:true});
  const pending=Promise.all([u.loadBootstrap(),u.loadPortfolios(),u.loadTrades()]);
  await h.advance(12000);assert.equal(u.st.trades,null);
  await h.advance(11000);await pending;
  assert.equal(h.node('openCount').textContent,1);
  assert.match(h.node('positions').innerHTML,/GOLD/);assert.match(h.node('trades').innerHTML,/GOLD/);
  assert.equal(u.st.positionBook.Impulse[0].last_price,4129);
  console.log('PASS: 20–23 second portfolio/trade reads render even when bootstrap fails');
  const positions=h.node('positions').innerHTML,closed=h.node('trades').innerHTML;
  h.responses.set('/api/v1/paper-portfolios',{data:{status:'ERROR',portfolios:[]}});
  h.responses.set('/api/v1/portfolio-trades',{data:{status:'UNAVAILABLE',trades:[]}});
  await Promise.all([u.loadPortfolios(),u.loadTrades()]);
  assert.equal(h.node('positions').innerHTML,positions);assert.equal(h.node('trades').innerHTML,closed);
  assert.match(h.node('positionSync').textContent,/задержано/);assert.match(h.node('tradeSync').textContent,/задержано/);
  console.log('PASS: errors preserve displayed positions/trades and disclose delayed refresh');
  u.applyBootstrap({status:'OK',health:{bootstrap_ready:true},signals:[],portfolios:[],trades:[]});
  assert.equal(h.node('positions').innerHTML,positions);assert.equal(h.node('trades').innerHTML,closed);
  console.log('PASS: partial/older bootstrap cannot erase independent ledger views');
  h.responses.set('/api/v1/paper-portfolios',{data:{status:'OK',portfolios:[{name:'Impulse',positions:[]}]}});
  await u.loadPortfolios();assert.equal(h.node('positions').innerHTML,positions);
  h.responses.set('/api/v1/paper-portfolios',{data:portfolio(false)});
  await u.loadPortfolios();assert.equal(h.node('openCount').textContent,0);assert.match(h.node('positions').innerHTML,/Открытых позиций нет/);
  h.responses.set('/api/v1/portfolio-trades',{data:{status:'OK',trades:[]}});
  await u.loadTrades();assert.match(h.node('trades').innerHTML,/Закрытых сделок пока нет/);
  console.log('PASS: complete authoritative empty books clear positions; incomplete books cannot');
  h.responses.set('/api/v1/portfolio-trades',{data:trades,delay:31000});
  const count=h.calls.length,slow=u.loadTrades();await u.loadTrades();assert.equal(h.calls.length,count+1);
  await h.advance(30000);await slow;assert.match(h.node('tradeSync').textContent,/задержано/);
  assert.equal(u.st.busy['portfolio-trades'],false);
  h.responses.set('/api/v1/portfolio-trades',{data:trades});await u.loadTrades();assert.equal(u.st.trades.trades.length,1);
  console.log('PASS: no overlapping reads; timed-out loader recovers on next refresh');
})().catch(e=>{console.error(e);process.exitCode=1});
