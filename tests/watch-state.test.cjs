const {test} = require('node:test');
const assert = require('node:assert/strict');
const {signature, watchState, saveWatchlist} = require('../static/watch-state.js');

const request = {query:'Liverpool jersey 97/98', source_mode:'live', marketplaces:['carousell','shopee'], match_mode:'exact', pricing_mode:'auto', max_purchase_price:null};
const response = {decisions:[{price:300,is_profitable:false},{price:250,is_profitable:true}]};
function storage() {
  return {calls:0, value:null, setItem(key,value) { this.calls++; this.value=JSON.parse(value); }};
}

test('a live text board is ready to save, while missing, demo and Lens boards explain why not', () => {
  assert.equal(watchState(request,response).kind,'ready');
  assert.equal(watchState(null,null).kind,'unavailable');
  assert.match(watchState({...request,source_mode:'demo'},response).message,/Demo/);
  assert.equal(watchState({...request,lens_reference_id:'temporary'},response).kind,'unsupported');
  assert.equal(watchState(request,{...response,lens_context:{}}).kind,'unsupported');
});
test('successful save creates a persistent saved state with an explicit manual-scan message', () => {
  const store=storage();
  const result=saveWatchlist(store,'watch',request,response);
  assert.equal(result.ok,true);
  assert.equal(result.alreadySaved,false);
  assert.equal(store.calls,1);
  const state=watchState(request,response,store.value);
  assert.equal(state.kind,'saved');
  assert.equal(state.id,result.id);
  assert.match(state.message,/this browser.*manually/);
  assert.equal(store.value[0].best_price,250);
  assert.equal(store.value[0].buy_count,1);
});
test('repeated saves preserve the existing baseline and do not write duplicate records', () => {
  const store=storage();
  saveWatchlist(store,'watch',request,response,[],'2026-11-08T00:00:00Z');
  const existing=store.value;
  const result=saveWatchlist(store,'watch',request,{decisions:[{price:20}]},existing);
  assert.equal(result.alreadySaved,true);
  assert.equal(store.calls,1);
  assert.equal(store.value.length,1);
  assert.equal(store.value[0].best_price,250);
  assert.equal(store.value[0].created_at,'2026-11-08T00:00:00Z');
});
test('old watchlists are recognised from their request, regardless of legacy signature or source order', () => {
  const old={id:'old',signature:'legacy',request:{...request,query:' LIVERPOOL  jersey 97/98 ',marketplaces:['shopee','carousell']}};
  assert.equal(watchState(request,response,[old]).id,'old');
  for (const change of [{max_purchase_price:300},{match_mode:'related'},{pricing_mode:'manual',resale_estimate:400},{marketplaces:['carousell']},{query:'LEGO 75192'}]) {
    assert.equal(watchState({...request,...change},response,[old]).kind,'ready');
  }
});
test('blocked storage never claims a successful save and leaves the button unsaved', () => {
  const result=saveWatchlist({setItem(){throw new Error('QuotaExceededError');}},'watch',request,response);
  assert.equal(result.ok,false);
  assert.match(result.message,/full or blocked/);
  assert.equal(watchState(request,response,[]).kind,'ready');
  assert.equal(saveWatchlist(null,'watch',request,response).ok,false);
});
test('saving snapshots the request; a new query or removal clears the saved state', () => {
  const store=storage(); const active={...request,marketplaces:[...request.marketplaces]};
  saveWatchlist(store,'watch',active,response);
  const saved=store.value;
  active.marketplaces.push('mudah');
  assert.equal(saved[0].request.marketplaces.length,2);
  assert.equal(watchState(active,response,saved).kind,'ready');
  assert.equal(watchState(request,response,[]).kind,'ready');
});
test('unsupported saves do not touch storage and missing offer prices stay unknown', () => {
  const store=storage();
  assert.equal(saveWatchlist(store,'watch',{...request,source_mode:'demo'},response).ok,false);
  assert.equal(store.calls,0);
  saveWatchlist(store,'watch',request,{decisions:[{price:null},{price:'bad'}]});
  assert.equal(store.value[0].best_price,null);
});
