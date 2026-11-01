'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createHash,createHmac} = require('node:crypto');
const {bridgeSettings,createBindingBridge} = require('./binding.cjs');
const {createDispatcher,readConfig} = require('./bot.cjs');

function fixture() {
  const requests=[], sends=[], logs=[];
  let response={state:'pending',url:'https://taxpearls.wxtech.site/connect/feishu#ticket=synthetic'};
  const config={appId:'cli_test',allowedOpenIds:new Set(),binding:{base:'http://127.0.0.1:8000',key:Buffer.from('ab'.repeat(32),'hex')}};
  const client={im:{message:{create:async request=>{sends.push(request);return {code:0,data:{message_id:'om_synthetic'}};}}}};
  const bridge=createBindingBridge(config,client,{log:s=>logs.push(s),fetcher:async(url,options)=>{
    requests.push({url,...options}); return {ok:true,json:async()=>response};
  }});
  return {bridge,config,client,requests,sends,logs,setResponse:value=>{response=value;}};
}

test('binding is disabled by default and rejects unsafe configured destinations',()=>{
  assert.equal(bridgeSettings({}),null);
  for(const url of ['http://external.test','https://user:secret@example.test','https://example.test/path']) {
    assert.throws(()=>bridgeSettings({TAXPEARLS_FEISHU_BINDING_ENABLED:'1',TAXPEARLS_FEISHU_BRIDGE_URL:url,TAXPEARLS_FEISHU_BRIDGE_KEY:'ab'.repeat(32)}));
  }
});
test('every bridge request signs method, fixed path, time, fresh nonce and exact body',async()=>{
  const f=fixture(); await f.bridge.menu('ou_test','oc_test','evt_test'); await f.bridge.menu('ou_test','oc_test','evt_test');
  const request=f.requests[0], headers=request.headers;
  const message=['POST','/api/internal/feishu-binding',headers['x-tp-time'],headers['x-tp-nonce'],createHash('sha256').update(request.body).digest('hex')].join('\n');
  assert.equal(headers['x-tp-signature'],createHmac('sha256',f.config.binding.key).update(message).digest('hex'));
  assert.notEqual(headers['x-tp-nonce'],f.requests[1].headers['x-tp-nonce']);
  assert.equal(request.redirect,'error');
  assert.ok(!request.body.includes('ab'.repeat(32)));
});
test('a pending entry uses a website link and never claims the user is bound',async()=>{
  const f=fixture();const card=await f.bridge.menu('ou_test','oc_test','evt_test');
  assert.equal(card.elements[1].actions[0].url,'https://taxpearls.wxtech.site/connect/feishu#ticket=synthetic');
  assert.match(card.header.title.content,/绑定账户/);
});
test('a website-approved request sends a final confirmation to its original chat',async()=>{
  const f=fixture(); f.setResponse({items:[{id:'a'.repeat(32),subject:'ou_test',chat:'oc_test',account_hint:'al***e',confirmation:'b'.repeat(64)}]});
  await f.bridge.poll();
  assert.equal(f.sends[0].data.receive_id,'oc_test');
  const card=JSON.parse(f.sends[0].data.content);
  assert.equal(card.elements[1].actions[0].value.binding_action,'confirm');
  assert.match(card.elements[0].text.content,/al\*\*\*e/);
});
test('final confirmation acknowledges promptly and only reports success after server confirmation',async()=>{
  const f=fixture();f.setResponse({state:'completed'});
  const toast=f.bridge.onAction({operator:{open_id:'ou_test'},context:{open_chat_id:'oc_test'},action:{value:{binding_action:'confirm',id:'a'.repeat(32),confirmation:'b'.repeat(64)}}});
  assert.equal(toast.toast.type,'info');assert.equal(f.sends.length,0);
  await f.bridge.drain();
  assert.equal(JSON.parse(f.sends[0].data.content).header.title.content,'TaxPearls · 绑定成功');
});
test('service rejection never produces a binding-success card',async()=>{
  const config={appId:'cli_test',allowedOpenIds:new Set(),binding:{base:'http://127.0.0.1:8000',key:Buffer.alloc(32)}};
  const cards=[];const client={im:{message:{create:async req=>{cards.push(JSON.parse(req.data.content));return {code:0,data:{message_id:'om_test'}};}}}};
  const bridge=createBindingBridge(config,client,{log(){},fetcher:async()=>({ok:false})});
  bridge.onAction({operator:{open_id:'ou_test'},context:{open_chat_id:'oc_test'},action:{value:{binding_action:'confirm',id:'a'.repeat(32),confirmation:'b'.repeat(64)}}});
  await bridge.drain();assert.equal(cards[0].header.title.content,'绑定未完成');
});
test('provider app and tester validation still precede binding callbacks',async()=>{
  const config=readConfig({TAXPEARLS_FEISHU_APP_ID:'cli_test',TAXPEARLS_FEISHU_APP_SECRET:'synthetic'});
  let invoked=0;const binding={onAction(){invoked++;return {toast:{type:'info'}};}};
  const {dispatcher}=createDispatcher(config,{}, {binding,log(){}});
  const event={schema:'2.0',header:{app_id:'cli_other',event_type:'card.action.trigger'},event:{operator:{open_id:'ou_test'},context:{open_chat_id:'oc_test'},action:{value:{binding_action:'confirm'}}}};
  await dispatcher.invoke(event,{needCheck:false});assert.equal(invoked,0);
});
