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
    requests.push({url,...options}); return {ok:true,json:async()=>typeof response==='function'?response(JSON.parse(options.body)):response};
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

test('help needs no binding request and status reads never issue a binding ticket',async()=>{
  const f=fixture();
  assert.match((await f.bridge.menu('ou_test','oc_test','help','help')).header.title.content,/使用帮助/);
  assert.equal(f.requests.length,0);
  for(const state of ['unbound','bound']) {
    f.setResponse(body=>body.operation==='lookup'?{state}:{binding_tag:'c'.repeat(64)});
    const card=await f.bridge.menu('ou_test','oc_test','status','status');
    assert.match(card.header.title.content,/绑定状态/);
    assert.match(card.elements[0].text.content,state==='bound'?/已连接/:/尚未建立/);
    assert.ok(f.requests.some(r=>JSON.parse(r.body).operation==='lookup'));
    assert.ok(!f.requests.some(r=>JSON.parse(r.body).operation==='issue'));
  }
  f.setResponse({state:'pending'});
  await assert.rejects(f.bridge.menu('ou_test','oc_test','status','status'));
});

test('bound menu queries real tasks, separates account status, and requires create confirmation',async()=>{
  const f=fixture(),tag='c'.repeat(64);
  f.setResponse(body=>body.operation==='issue'?{state:'bound'}:body.operation==='lookup'?{state:'bound'}:body.operation==='task_list'
    ?{items:[{id:'a'.repeat(32),display_name:'示例企业 · 2026-01-01 至 2026-01-31',status:'needs_completion',files:0,url:'https://example.test/?input_task='+ 'a'.repeat(32)+'#panel=uploadPanel'}],count:1,page:0,has_more:false,binding_tag:tag}
    :body.operation==='task_create'?{id:'a'.repeat(32),display_name:'新建任务 · 2026-11-16 10:29',url:'https://example.test/?input_task='+ 'a'.repeat(32)+'#panel=uploadPanel'}:{binding_tag:tag});
  const app=createDispatcher(f.config,f.client,{binding:f.bridge,log(){}});
  const message=(text,id)=>({schema:'2.0',header:{app_id:'cli_test',event_type:'im.message.receive_v1'},event:{
    sender:{sender_type:'user',sender_id:{open_id:'ou_test'}},
    message:{message_id:id,chat_type:'p2p',message_type:'text',chat_id:'oc_test',content:JSON.stringify({text})},
  }});
  await app.dispatcher.invoke(message('唤出菜单','om_menu'),{needCheck:false});await app.drain();
  const menu=JSON.parse(f.sends[0].data.content);
  assert.deepEqual(menu.elements[1].actions.map(item=>item.text.content),['创建任务','进行中的任务','已完成的任务']);
  assert.match(menu.header.title.content,/任务菜单/);
  const click=async(value,id='evt_click')=>app.dispatcher.invoke({schema:'2.0',header:{app_id:'cli_test',event_type:'card.action.trigger',event_id:id},event:{
    operator:{open_id:'ou_test'},context:{open_chat_id:'oc_test'},action:{value},
  }},{needCheck:false});
  assert.equal((await click(menu.elements[1].actions[1].value)).toast.type,'info');await f.bridge.drain();
  assert.match(JSON.parse(f.sends.at(-1).data.content).elements[0].text.content,/共 1 项/);
  const taskLabel=JSON.parse(f.sends.at(-1).data.content).elements[1].text.content;
  assert.match(taskLabel,/示例企业 · 2026-01-01 至 2026-01-31/);
  assert.ok(!taskLabel.includes('aaaaaaaa'));
  const before=f.requests.length;
  await click(menu.elements[1].actions[0].value,'evt_prompt');await f.bridge.drain();
  assert.ok(!f.requests.slice(before).some(r=>JSON.parse(r.body).operation==='task_create'));
  const confirmation=JSON.parse(f.sends.at(-1).data.content).elements[1].actions[0].value;
  await click(confirmation,'evt_confirm');await f.bridge.drain();
  assert.match(JSON.parse(f.sends.at(-1).data.content).header.title.content,/任务已创建/);
  assert.match(JSON.parse(f.sends.at(-1).data.content).elements[0].text.content,/新建任务 · 2026-11-16 10:29/);
  assert.ok(f.requests.some(r=>JSON.parse(r.body).operation==='task_create'));
  const requests=f.requests.length;
  const tampered={...confirmation,task_payload:confirmation.task_payload+'x'};
  assert.equal((await click(tampered)).toast.type,'warning');assert.equal(f.requests.length,requests);
  await app.dispatcher.invoke(message('账户状态','om_status'),{needCheck:false});await app.drain();
  const status=JSON.parse(f.sends.at(-1).data.content);assert.match(status.header.title.content,/绑定状态/);
  assert.deepEqual(status.elements[1].actions.map(item=>item.text.content),['解除绑定']);
  f.setResponse({state:'pending',url:'https://example.test/connect/feishu#ticket=synthetic'});
  await app.dispatcher.invoke(message('唤出菜单','om_unbound'),{needCheck:false});await app.drain();
  assert.equal(JSON.parse(f.sends.at(-1).data.content).elements[1].actions[0].text.content,'绑定 TaxPearls 账户');
});

test('account unlink requires confirmation, supports keeping the binding, and rejects forwarded cards',async()=>{
  const f=fixture(),tag='c'.repeat(64);let bound=true;
  f.setResponse(body=>{
    if(body.operation==='lookup')return {state:bound?'bound':'unbound'};
    if(body.operation==='binding_unlink'){bound=false;return {state:'unbound'};}
    return {binding_tag:tag};
  });
  const read=()=>JSON.parse(f.sends.at(-1).data.content);
  const click=async(value,operator='ou_test',chat='oc_test')=>{
    const result=f.bridge.onTaskAction({operator:{open_id:operator},context:{open_chat_id:chat},action:{value}});
    await f.bridge.drain();return result;
  };
  const status=await f.bridge.menu('ou_test','oc_test','status','status');
  const unlink=status.elements[1].actions[0].value;
  await click(unlink);
  assert.match(read().header.title.content,/确认解除绑定/);
  assert.deepEqual(read().elements[1].actions.map(a=>a.text.content),['确认解除绑定','保留绑定']);
  assert.ok(!f.requests.some(r=>JSON.parse(r.body).operation==='binding_unlink'));
  await click(read().elements[1].actions[1].value);
  assert.match(read().elements[0].text.content,/已连接/);assert.equal(bound,true);
  await click(unlink);
  const confirm=read().elements[1].actions[0].value,before=f.requests.length;
  assert.equal((await click(confirm,'ou_other')).toast.type,'warning');
  assert.equal((await click(confirm,'ou_test','oc_other')).toast.type,'warning');
  assert.equal((await click({...confirm,task_payload:confirm.task_payload+'x'})).toast.type,'warning');
  assert.equal(f.requests.length,before);
  await click(confirm);
  assert.match(read().header.title.content,/已解除绑定/);
  assert.match(read().elements[0].text.content,/任务、材料和报告保留/);
  const request=JSON.parse(f.requests.at(-1).body);
  assert.deepEqual(request,{app_id:'cli_test',operation:'binding_unlink',subject:'ou_test',binding_tag:tag});
  const after=await f.bridge.menu('ou_test','oc_test','status_after','status');
  assert.match(after.elements[0].text.content,/尚未建立/);
  assert.ok(!after.elements.some(item=>item.tag==='action'));
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
