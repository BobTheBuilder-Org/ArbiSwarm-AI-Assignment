const {test}=require('node:test');
const assert=require('node:assert/strict');
const {requestJSON}=require('../static/chat-state.js');

test('a hung fetch is aborted without sending an automatic retry',async()=>{
  let signal,calls=0;
  const fetcher=(_,options)=>{calls++;signal=options.signal;return new Promise(()=>{});};
  await assert.rejects(requestJSON('/x',{},15,fetcher),{name:'TimeoutError'});
  assert.equal(calls,1);assert.equal(signal.aborted,true);
});
test('a response body that never finishes also reaches the deadline',async()=>{
  const fetcher=async()=>({ok:true,json:()=>new Promise(()=>{})});
  await assert.rejects(requestJSON('/x',{},15,fetcher),{name:'TimeoutError'});
});
test('successful JSON is returned and the timer is cleared',async()=>{
  let signal;
  const fetcher=async(_,options)=>{signal=options.signal;return {ok:true,json:async()=>({ok:1})};};
  const {response,payload}=await requestJSON('/x',{},20,fetcher);
  assert.equal(response.ok,true);assert.deepEqual(payload,{ok:1});
  await new Promise(r=>setTimeout(r,30));assert.equal(signal.aborted,false);
});
test('closing Lens cancels a non-cooperative transport',async()=>{
  const parent=new AbortController();let signal;
  const task=requestJSON('/x',{signal:parent.signal},1000,(_,o)=>{signal=o.signal;return new Promise(()=>{});});
  parent.abort();await assert.rejects(task,{name:'AbortError'});assert.equal(signal.aborted,true);
});
test('an already cancelled request sends nothing',async()=>{
  const parent=new AbortController();parent.abort();let calls=0;
  await assert.rejects(requestJSON('/x',{signal:parent.signal},100,()=>{calls++;}),{name:'AbortError'});
  assert.equal(calls,0);
});
