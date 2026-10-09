"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
// Skipped can mean evidence, period, denominator or applicability limits.
// The shared UI must not translate every skipped result into missing files.
const indexMarkup = fs.readFileSync("webapp/static/index.html", "utf8");
assert.ok(indexMarkup.includes('未执行不等于通过，请按各项原因核对后复检'));
assert.ok(!indexMarkup.includes('value="skipped">材料不足'));
for (const file of ['console.js', 'graph.js']) {
  const code = fs.readFileSync('webapp/static/' + file, 'utf8');
  assert.ok(!code.includes('skipped:"材料不足"'));
  assert.ok(!code.includes('未命中但材料不足'));
}
for (const name of fs.readdirSync("webapp/static").filter(name => name.endsWith(".js"))) {
  new vm.Script(fs.readFileSync("webapp/static/" + name, "utf8"), {filename:name});
}
for (const match of fs.readFileSync("webapp/static/index.html", "utf8").matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  if (match[1].trim()) new vm.Script(match[1]);
}
// Trend area gaps remain gaps; isolated/zero samples and reduced-motion modes stay valid.
{
  const source = fs.readFileSync("webapp/static/workspace.js", "utf8");
  function node(tag='div',className='',text='') {
    return {tag,children:[],attributes:{class:className},style:{setProperty(){}},textContent:text,
      classList:{add(){},remove(){}},setAttribute(name,value){this.attributes[name]=String(value);},
      append(...children){this.children.push(...children);},addEventListener(){}};
  }
  function all(parent) { return [parent,...parent.children.flatMap(all)]; }
  for(const reduced of [true,false]) {
    const ctx={REDUCED_MOTION:reduced, $:()=>({value:'2026-05'}),
      metricOf:(row,key)=>row.metrics[key],amount:metric=>String(metric.value),
      document:{createElementNS:(_ns,tag)=>node(tag)},el:node,requestAnimationFrame:fn=>fn()};
    vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function svgNode(')),ctx);
    const box=node();
    const rows=[1,2,3,4,5].map(month=>({period:'2026-0'+month,metrics:{'营业成本':{value:month*5}}}));
    for(const index of [0,1,3,4]) rows[index].metrics['营业收入']={value:(index+1)*10};
    ctx.renderTrend(box,rows);
    const elements=all(box),areas=elements.filter(n=>n.tag==='path');
    const income=areas.filter(n=>n.attributes.fill.includes('Income'));
    assert.equal(income.length,2,'Missing income must split the filled area into two runs');
    assert.equal(areas.length,3,'Continuous cost data should retain one filled area');
    for(const area of income) assert.ok(!area.attributes.d.includes('L 325 '),'A missing metric must not become a filled vertex');
    assert.ok(areas.every(n=>/ Z$/.test(n.attributes.d)&&!n.attributes.d.includes('NaN')));
    const reveals=elements.filter(n=>n.tag==='rect');
    assert.equal(reveals.length,2);
    assert.ok(reveals.every(n=>reduced?n.attributes.width==='520':n.style.width==='520px'));
    const sparse=node();
    ctx.renderTrend(sparse,rows.map((row,i)=>({...row,metrics:i===2?{'营业收入':{value:0}}:{}})));
    assert.equal(all(sparse).filter(n=>n.tag==='path').length,0,'Isolated and zero-valued samples must not create a bridge');
  }
}

// Appearance persists independently of account data and reacts to system changes only in system mode.
{
  const appearance = indexMarkup.match(/<script id="workspaceAppearance">([\s\S]*?)<\/script>/)[1];
  function appearanceBoundary(saved, dark = false, blocked = false) {
    const button = {dataset:{},attributes:{},setAttribute(name,value){this.attributes[name]=value;},addEventListener(_name,fn){this.click=fn;}};
    const options = ["light", "dark", "system"].map(value => ({dataset:{themeSelect:value},attributes:{},setAttribute(name,value){this.attributes[name]=value;},addEventListener(_name,fn){this.click=fn;}}));
    const label = {textContent:""};
    const icons = ["light", "dark", "system"].map(value => ({dataset:{themeIcon:value},hidden:false,toggleAttribute(name,on){assert.equal(name,"hidden");this.hidden=on;}}));
    const dataset = {}, events = {}, writes = [];let mounted, changed, isMounted=false;
    const media = {matches:dark,addEventListener(_name,fn){changed=fn;}};
    const ctx = {document:{documentElement:{dataset},readyState:"loading",getElementById:id=>!isMounted?null:id==="themeCycle"?button:id==="themeLabel"?label:null,querySelectorAll:selector=>!isMounted?[]:selector.includes("themeOptions")?options:icons,addEventListener(_name,fn){mounted=()=>{isMounted=true;fn();};}},
      localStorage:{getItem(){if(blocked)throw new Error("storage blocked");return saved;},setItem(key,value){if(blocked)throw new Error("storage blocked");writes.push({key,value});saved=value;}},
      window:{matchMedia:()=>media,addEventListener(name,fn){events[name]=fn;}}};
    vm.createContext(ctx);vm.runInContext(appearance,ctx);
    const initial = {...dataset};mounted();
    return {dataset,initial,button,label,icons,options,writes,events,select(value){options.find(option=>option.dataset.themeSelect===value).click();},system(value){media.matches=value;changed();},click(){button.click();}};
  }
  const light=appearanceBoundary(null);assert.equal(light.initial.theme,"light");
  assert.equal(light.label.textContent,"亮色");assert.equal(light.icons.filter(i=>!i.hidden).length,1);
  assert.match(light.button.attributes["aria-label"],/点击切换为暗色/);
  light.click();assert.equal(light.dataset.themePreference,"dark");assert.equal(light.label.textContent,"暗色");
  light.click();assert.equal(light.dataset.themePreference,"system");assert.equal(light.label.textContent,"跟随系统");
  light.click();assert.equal(light.dataset.themePreference,"light");assert.equal(light.writes.at(-1).value,"light");
  const direct=appearanceBoundary("light");
  for (const mode of ["system", "dark", "light"]) {
    direct.select(mode);assert.equal(direct.dataset.themePreference,mode);
    assert.equal(direct.options.filter(option=>option.attributes["aria-pressed"]==="true").length,1);
    assert.equal(direct.options.find(option=>option.attributes["aria-pressed"]==="true").dataset.themeSelect,mode);
    assert.equal(direct.icons.find(icon=>!icon.hidden).dataset.themeIcon,mode);
    assert.equal(direct.writes.at(-1).value,mode);
  }
  direct.select("dark");direct.click();assert.equal(direct.dataset.themePreference,"system");
  assert.equal(direct.options.find(option=>option.attributes["aria-pressed"]==="true").dataset.themeSelect,"system");
  assert.equal(appearanceBoundary(direct.writes.at(-1).value,true).initial.theme,"dark");
  const savedDark=appearanceBoundary("dark");assert.equal(savedDark.initial.theme,"dark");
  assert.equal(savedDark.icons.find(i=>!i.hidden).dataset.themeIcon,"dark");
  savedDark.system(false);assert.equal(savedDark.dataset.theme,"dark");
  savedDark.click();assert.equal(savedDark.dataset.themePreference,"system");
  const system=appearanceBoundary("system",true);assert.equal(system.initial.theme,"dark");
  system.system(false);assert.equal(system.dataset.theme,"light");assert.equal(system.dataset.themePreference,"system");
  system.click();system.click();system.system(false);assert.equal(system.dataset.theme,"dark");
  system.click();assert.equal(system.dataset.theme,"light");
  system.events.storage({key:"unrelated",newValue:"dark"});assert.equal(system.dataset.themePreference,"system");
  system.events.storage({key:"taxpearls.theme",newValue:"light"});assert.equal(system.dataset.themePreference,"light");
  assert.equal(system.icons.find(i=>!i.hidden).dataset.themeIcon,"light");
  system.events.storage({key:null,newValue:null});assert.equal(system.dataset.theme,"light");
  assert.equal(appearanceBoundary("invalid",true).initial.theme,"light");
  const blocked=appearanceBoundary("dark",true,true);assert.equal(blocked.initial.theme,"light");
  blocked.click();assert.equal(blocked.dataset.themePreference,"dark");
  blocked.click();assert.equal(blocked.dataset.themePreference,"system");
  blocked.click();assert.equal(blocked.dataset.themePreference,"light");
  assert.equal(appearanceBoundary(light.dataset.themePreference).initial.theme,"light");
}

// Refresh restores the actual selected workspace view, while role-hidden views stay unavailable.
{
  const source = fs.readFileSync("webapp/static/console.js", "utf8");
  const code = source.slice(source.indexOf("function defaultWorkspacePanel()"), source.indexOf("/* ---------- 多材料上传"));
  const panels = ["dashboardPanel", "uploadPanel", "auditPanel", "historyPanel", "knowledgePanel", "trainingPanel", "adminPanel", "notificationPanel", "orgReportPanel", "userSettingsPanel"];
  const permitted = {
    org_admin: panels.filter(id => id !== "trainingPanel"),
    accountant: panels.filter(id => !["trainingPanel", "orgReportPanel"].includes(id)),
    teacher: panels.filter(id => id !== "orgReportPanel"),
    student: ["trainingPanel", "knowledgePanel", "userSettingsPanel"],
    platform_admin: ["adminPanel", "knowledgePanel", "userSettingsPanel"],
  };
  function navigation(role, href = "http://localhost/?view=test") {
    const calls = [], url = new URL(href);
    const location = {href:url.href, pathname:url.pathname, search:url.search, hash:url.hash, reload(){calls.push("reload");}};
    const classes = (...initial) => {
      const values = new Set(initial);
      return {contains:name=>values.has(name), toggle(name, enabled){if(enabled) values.add(name);else values.delete(name);}};
    };
    const views = panels.map(id => ({id, classList:classes("panel")}));
    const buttons = panels.map(id => ({dataset:{panel:id}, hidden:false, style:{display:permitted[role].includes(id)?"block":"none"}, classList:classes()}));
    const ctx = {currentUser:{id:"user",role}, location, URLSearchParams,
      history:{state:{retained:true}, replaceState(_state,_title,path){const next=new URL(path,location.href);Object.assign(location,{href:next.href,pathname:next.pathname,search:next.search,hash:next.hash});}},
      $:id=>views.find(view=>view.id===id),
      document:{querySelectorAll:selector=>selector===".panel"?views:buttons},
      noticeRequest:0, settingsRequest:0, ruleEditorRequest:0, adminRequest:0,
      refreshAIStatus(){calls.push("ai");},refreshAuditClients(){calls.push("clients");},enterpriseMaterials:{refresh(){calls.push("materials");}}};
    for(const name of ["loadHistory","loadNotifications","loadUserSettings","loadAssignments","loadAdmin","loadDashboard","loadOrgOverview","loadKnowledge"])ctx[name]=()=>calls.push(name);
    vm.createContext(ctx);vm.runInContext(code,ctx);
    return {ctx,calls,location,views,buttons};
  }
  for (const [role, ids] of Object.entries(permitted)) {
    for(const id of ids) {
      const before=navigation(role);before.ctx.switchPanel(id);
      assert.equal(before.location.hash,"#panel="+id);
      assert.equal(before.location.search,"?view=test");
      const refreshed=navigation(role,before.location.href);
      refreshed.ctx.switchPanel(refreshed.ctx.initialWorkspacePanel());
      assert.equal(refreshed.views.find(view=>view.classList.contains("active")).id,id);
      assert.equal(refreshed.buttons.find(button=>button.classList.contains("active")).dataset.panel,id);
    }
  }
  for(const [role, requested, expected] of [
    ["student","adminPanel","trainingPanel"],
    ["platform_admin","dashboardPanel","adminPanel"],
    ["accountant","orgReportPanel","dashboardPanel"],
    ["org_admin","unknownPanel","dashboardPanel"],
  ]) {
    const view=navigation(role,"http://localhost/#panel="+requested);
    view.ctx.switchPanel(view.ctx.initialWorkspacePanel());
    assert.equal(view.views.find(panel=>panel.classList.contains("active")).id,expected);
    assert.equal(view.location.hash,"#panel="+expected);
  }
  const graph=navigation("org_admin","http://localhost/#panel=knowledgePanel");
  graph.ctx.switchPanel(graph.ctx.initialWorkspacePanel());assert.deepEqual(graph.calls,["loadKnowledge"]);
  const guest=navigation("org_admin");guest.ctx.currentUser=null;guest.ctx.switchPanel("adminPanel");
  assert.equal(guest.calls.length,0);assert.equal(guest.location.hash,"");
  const hashStart=source.indexOf('window.addEventListener("hashchange"');
  const hashCode=source.slice(hashStart,source.indexOf("async function bootstrap()",hashStart));
  const mail=navigation("org_admin");let hashChanged;
  mail.ctx.window={addEventListener(_name,handler){hashChanged=handler;}};
  vm.runInContext(hashCode,mail.ctx);
  mail.location.hash="#email=confirmation-token";hashChanged();assert.deepEqual(mail.calls,["reload"]);
  mail.calls.length=0;mail.location.hash="#panel=historyPanel";hashChanged();assert.deepEqual(mail.calls,["loadHistory"]);
  assert.ok(source.includes("switchPanel(initialWorkspacePanel());"));
}

// Run the actual role-selection function with a small API/DOM boundary.
const graph = fs.readFileSync("webapp/static/graph.js", "utf8");
const selection = graph.slice(graph.indexOf("async function loadKnowledge()"), graph.indexOf("async function loadGraph()"));
(async () => {
  for (const role of ["platform_admin", "student", "org_admin", "accountant", "teacher"]) {
    const calls = [], errors = [], choices = [];
    const context = {currentUser:{id:'user',role}, graphRequest:0, graphCurrent:()=>true, clearGraph(){}, $:()=>({value:"", replaceChildren(){}}),
      option:(_select, value)=>choices.push(value), showError:error=>errors.push(error),
      api:async url=>{calls.push(url); return [{id:"audit-1"}];}, loadGraph:async()=>{}};
    vm.createContext(context);
    vm.runInContext(selection, context);
    await context.loadKnowledge();
    assert.deepEqual(calls, ["org_admin", "accountant", "teacher"].includes(role) ? ["/api/audits"] : []);
    assert.equal(errors.length, 0);
    assert.equal(choices[0], "");
  }
  // Exercise the actual notification functions, including out-of-order reads
  // and navigation while a write is in flight. No browser globals are replaced
  // in application code; this tiny boundary provides only the needed DOM/API.
  const source = fs.readFileSync("webapp/static/console.js", "utf8");
  // Settings reuses self-only preferences; stale reads/writes may not alter another view/account.
  const settingsCode = source.slice(source.indexOf("let settingsRequest ="), source.indexOf("function noticeCurrent("));
  function settingsBoundary(role = "accountant") {
    const nodes = new Map(), calls = [], navigated = []; let active = true;
    const user = {id:"settings-user", role, username:"<account>", display_name:"<name>", created_at:"2026-10-01T00:00:00+00:00"};
    const ctx = {currentUser:user, Number, Date,
      $:id => {if(!nodes.has(id)) nodes.set(id, {checked:false, disabled:false, hidden:false, textContent:"", classList:{contains:()=>active}});return nodes.get(id);},
      canOpenWorkspacePanel:id=>["org_admin","accountant","teacher"].includes(ctx.currentUser.role) && (id!=="orgReportPanel" || ctx.currentUser.role==="org_admin"),
      switchPanel:id=>navigated.push(id),
      api:(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject}))};
    vm.createContext(ctx); vm.runInContext(settingsCode,ctx);
    return {ctx,calls,navigated,leave(){active=false;vm.runInContext("settingsRequest++;",ctx);},enter(){active=true;}};
  }
  const settingsPrefs = {audit_completed:true, high_risk:false, email_enabled:true, has_email:true, delivery_enabled:false};
  for(const role of ["student","platform_admin"]) {
    const {ctx,calls}=settingsBoundary(role);await ctx.loadUserSettings();await ctx.saveUserSettings();
    assert.equal(calls.length,0);assert.equal(ctx.$("settingsSave").disabled,true);
    assert.equal(ctx.$("settingsHistoryExport").hidden,true);assert.equal(ctx.$("settingsOrgExport").hidden,true);
    assert.equal(ctx.$("settingsUsername").textContent,"<account>");
  }
  for(const role of ["org_admin","accountant","teacher"]) {
    const {ctx,calls,navigated}=settingsBoundary(role);const loading=ctx.loadUserSettings();
    assert.equal(ctx.$("settingsSave").disabled,true);
    assert.equal(calls.length,1);assert.equal(calls[0].url,"/api/notifications/preferences");
    calls.shift().resolve(settingsPrefs);await loading;
    assert.equal(ctx.$("settingsCompleted").checked,true);assert.equal(ctx.$("settingsEmail").checked,true);
    assert.equal(ctx.$("settingsPreferences").disabled,false);assert.equal(ctx.$("settingsSave").disabled,false);
    assert.equal(ctx.$("settingsOrgExport").hidden,role!=="org_admin");
    ctx.$("settingsHistoryOpen").onclick();assert.deepEqual(navigated,["historyPanel"]);
  }
  {
    const boundary=settingsBoundary(),{ctx,calls}=boundary;
    const old=ctx.loadUserSettings(),oldRead=calls.shift();
    const latest=ctx.loadUserSettings();calls.shift().resolve({...settingsPrefs,has_email:false,email_enabled:false});await latest;
    oldRead.resolve(settingsPrefs);await old;
    assert.equal(ctx.$("settingsEmail").disabled,true);assert.equal(ctx.$("settingsEmail").checked,false);
    ctx.$("settingsEmail").checked=true; // Disabled channels cannot be enabled by a stale/programmatic value.
    const saving=ctx.saveUserSettings(),write=calls.shift();
    assert.equal(write.options.method,"PUT");assert.deepEqual(JSON.parse(write.options.body),{audit_completed:true,high_risk:false,email_enabled:false});
    assert.equal(ctx.$("settingsSave").disabled,true);await ctx.saveUserSettings();assert.equal(calls.length,0);
    write.resolve({});await new Promise(resolve=>setImmediate(resolve));
    calls.shift().resolve({...settingsPrefs,has_email:false,email_enabled:false});await saving;
    assert.ok(ctx.$("settingsStatus").textContent.includes("已保存"));
    const leaving=ctx.loadUserSettings(),leaveRead=calls.shift();boundary.leave();
    leaveRead.reject(new Error("stale settings error"));await leaving;
    assert.ok(!ctx.$("settingsStatus").textContent.includes("stale settings error"));
    boundary.enter();const recover=ctx.loadUserSettings();calls.shift().resolve(settingsPrefs);await recover;
    const pendingSave=ctx.saveUserSettings(),pendingWrite=calls.shift();boundary.leave();boundary.enter();
    const reentered=ctx.loadUserSettings();assert.equal(calls.length,0); // No pre-write GET on re-entry.
    pendingWrite.resolve({});await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.length,1);calls.shift().resolve({...settingsPrefs,high_risk:true});await Promise.all([pendingSave,reentered]);
    assert.equal(ctx.$("settingsHigh").checked,true);assert.equal(ctx.$("settingsSave").disabled,false);
    const failedSave=ctx.saveUserSettings();calls.shift().reject(new Error("uncertain save"));await failedSave;
    assert.equal(ctx.$("settingsSave").disabled,true);assert.equal(ctx.$("settingsRefresh").disabled,false);
    assert.ok(ctx.$("settingsStatus").textContent.includes("未确认"));
    const reload=ctx.loadUserSettings();calls.shift().reject(new Error("settings read failure"));await reload;
    assert.equal(ctx.$("settingsSave").disabled,true);
    const changedOwner=ctx.loadUserSettings(),privateRead=calls.shift();ctx.currentUser={id:"other",role:"accountant",username:"other"};
    privateRead.resolve(settingsPrefs);await changedOwner;assert.equal(ctx.$("settingsCompleted").checked,false);
    const nextUser=ctx.loadUserSettings();calls.shift().resolve(settingsPrefs);await nextUser;
    const otherSave=ctx.saveUserSettings(),otherWrite=calls.shift();ctx.currentUser={id:"third",role:"accountant",username:"third"};
    const thirdLoad=ctx.loadUserSettings();assert.equal(calls.length,0);
    otherWrite.reject(new Error("other user's save"));await new Promise(resolve=>setImmediate(resolve));
    calls.shift().resolve(settingsPrefs);await Promise.all([otherSave,thirdLoad]);
    assert.equal(ctx.$("settingsUsername").textContent,"third");assert.ok(!ctx.$("settingsStatus").textContent.includes("other user's save"));
    assert.equal(ctx.$("settingsSave").disabled,false);
  }
  assert.ok(source.includes('AI 候选原值（待核对）'));
  assert.ok(!source.includes('`原件：${row.ai_raw_value'));
  const notificationCode = source.slice(source.indexOf("function noticeCurrent("), source.indexOf("let currentRiskChanges"));
  const nodes = new Map();
  const node = () => ({children:[], textContent:"", disabled:false, checked:false,
    classList:{contains:()=>true}, replaceChildren(){this.children=[];},
    append(...items){this.children.push(...items);}, get childElementCount(){return this.children.length;}});
  const graphNodes=new Map(),graphCalls=[],graphDraws=[];
  const graphCtx={currentUser:{id:'owner',role:'org_admin'},graphCanvas:{...node(),style:{},setAttribute(){}},
    $:id=>{if(!graphNodes.has(id))graphNodes.set(id,{...node(),value:'',style:{}});return graphNodes.get(id);},
    option:(select,value)=>select.append(value),updateAIContext(){},
    api:url=>new Promise((resolve,reject)=>graphCalls.push({url,resolve,reject})),
    filterGraph:()=>graphDraws.push(vm.runInContext('graphData.audit_id',graphCtx)),focusGraph(){}};
  vm.createContext(graphCtx);
  vm.runInContext('let graphRequest=0,graphData={nodes:[],edges:[]},graphSelected=null,graphVisible=new Set(),graphPositions=new Map(),graphFocused=false,graphTransform={},graphDrag=null,graphBusy=false;'+
    graph.slice(graph.indexOf('function graphCurrent('),graph.indexOf('$("graphAudit").addEventListener')),graphCtx);
  const listOld=graphCtx.loadKnowledge(),oldList=graphCalls.shift();
  const listNew=graphCtx.loadKnowledge();graphCalls.shift().resolve([{id:'new'}]);
  await new Promise(resolve=>setImmediate(resolve));
  graphCalls.shift().resolve({nodes:[],edges:[],audit_id:null,ai:{configured:false}});await listNew;
  oldList.resolve([{id:'old'}]);await listOld;
  assert.deepEqual(graphCtx.$('graphAudit').children,['','new']);assert.equal(graphCalls.length,0);
  const graphOld=graphCtx.loadGraph(),oldGraph=graphCalls.shift();
  const graphNew=graphCtx.loadGraph();graphCalls.shift().resolve({nodes:[],edges:[],audit_id:'new',ai:{configured:false}});await graphNew;
  oldGraph.resolve({nodes:[],edges:[],audit_id:'old',ai:{configured:true}});await graphOld;
  assert.equal(vm.runInContext('graphData.audit_id',graphCtx),'new');
  const leaving=graphCtx.loadGraph(),leaveCall=graphCalls.shift();graphCtx.invalidateGraph();
  leaveCall.resolve({nodes:[{id:'secret'}],edges:[],audit_id:'old',ai:{configured:true}});await leaving;
  assert.equal(vm.runInContext('graphData.nodes.length',graphCtx),0);assert.equal(graphCtx.$('graphSend').disabled,true);
  const failure=graphCtx.loadGraph();graphCalls.shift().reject(new Error('graph failure'));await failure;
  assert.equal(graphCtx.$('graphCount').textContent,'graph failure');assert.equal(graphCtx.$('graphSummary').children.length,0);
  const changedOwner=graphCtx.loadKnowledge(),ownerCall=graphCalls.shift();graphCtx.currentUser={id:'other',role:'org_admin'};
  ownerCall.resolve([{id:'private'}]);await changedOwner;assert.deepEqual(graphCtx.$('graphAudit').children,['']);
  vm.runInContext(graph.slice(graph.indexOf('function neighbors('),graph.indexOf('function layoutGraph(')),graphCtx);
  graphCtx.layoutGraph=()=>{};graphCtx.drawGraph=()=>{};graphCtx.renderNode=()=>{};
  vm.runInContext('graphData={nodes:[{id:"trade",category:"关联方图",status:"skipped",kind:"trade"}],edges:[]};',graphCtx);
  graphCtx.$('knowledgeCategory').value='关联方图';graphCtx.$('graphStatus').value='skipped';
  graphCtx.filterGraph();assert.equal(vm.runInContext('graphVisible.has("trade")',graphCtx),true);
  vm.runInContext(graph.slice(graph.indexOf('function layoutGraph('),graph.indexOf('function drawGraph(')),graphCtx);
  vm.runInContext('graphData={nodes:[],edges:[]};graphFocused=true;graphVisible=new Set();',graphCtx);
  graphCtx.innerWidth=390;graphCtx.layoutGraph(); // empty focused mobile view must not dereference undefined
  const pending = [];
  const ctx = {currentUser:{id:"user-1",role:"accountant"},
    $:id=>{if(!nodes.has(id)) nodes.set(id,node());return nodes.get(id);}, el:node,
    document:{querySelectorAll:()=>[]},
    api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
    renderResult(){}, switchPanel(){}};
  vm.createContext(ctx);
  vm.runInContext('let noticeRequest=0,noticeBusy=false,noticeReady=false,noticeHasEmail=false;'+notificationCode,ctx);
  const preferences = {has_email:true,audit_completed:true,high_risk:false,email_enabled:false,delivery_enabled:false};
  const resolveLoad = items => {items[0].resolve(preferences);items[1].resolve([]);};
  const old = ctx.loadNotifications(), oldCalls = pending.splice(0);
  const newer = ctx.loadNotifications(); resolveLoad(pending.splice(0)); await newer;
  oldCalls[0].reject(new Error("stale failure")); oldCalls[1].resolve([]); await old;
  assert.ok(!ctx.$("noticeStatus").textContent.includes("stale failure"));
  const older = ctx.loadNotifications(), olderCalls = pending.splice(0);
  const latest = ctx.loadNotifications(), latestCalls = pending.splice(0);
  latestCalls[0].reject(new Error("current failure"));latestCalls[1].resolve([]);await latest;
  resolveLoad(olderCalls);await older;
  assert.ok(ctx.$("noticeStatus").textContent.includes("current failure"));
  assert.equal(ctx.$("noticeSave").disabled,true);
  const recover = ctx.loadNotifications();resolveLoad(pending.splice(0));await recover;
  let finish, writes=0;
  const write = ctx.noticeAction(()=>{writes++;return new Promise(resolve=>{finish=resolve;});});
  await ctx.noticeAction(()=>{writes++;});
  assert.equal(writes,1);
  vm.runInContext('noticeRequest++;',ctx); // leave and return before save responds
  finish();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(pending.length,2); // return to the panel refreshes persisted state
  resolveLoad(pending.splice(0));await write;
  assert.equal(ctx.$("noticeSave").disabled,false);
  const failedWrite = ctx.noticeAction(()=>Promise.reject(new Error("write failed")));
  await failedWrite;
  assert.equal(ctx.$("noticeSave").disabled,true);
  assert.ok(ctx.$("noticeStatus").textContent.includes("write failed"));
  const ruleNodes = new Map(), ruleCalls = [], shown = [], ruleErrors = [];
  const ruleCtx = {currentUser:{id:"manager",role:"platform_admin"},editingRule:{id:"R-001"},
    $:id=>{if(!ruleNodes.has(id))ruleNodes.set(id,{...node(),value:"audit-1",querySelectorAll:()=>[]});return ruleNodes.get(id);},
    el:node,api:url=>new Promise((resolve,reject)=>ruleCalls.push({url,resolve,reject})),
    ruleDraftBody:()=>({new_version:"2.1"}),showRuleTrial:r=>shown.push(r),showError:e=>ruleErrors.push(e),
    openRuleEditor:r=>shown.push(r),loadAdmin:async()=>{}};
  vm.createContext(ruleCtx);
  vm.runInContext('let ruleEditorRequest=0,ruleHistoryRequest=0,ruleBusy=false;'+
    source.slice(source.indexOf("async function loadRuleVersionHistory("),source.indexOf("function ruleDraftBody(")),ruleCtx);
  const history1=ruleCtx.loadRuleVersionHistory("R-001"),firstHistory=ruleCalls.shift();
  const history2=ruleCtx.loadRuleVersionHistory("R-001");ruleCalls.shift().resolve([]);await history2;
  const latestChildren=ruleCtx.$("ruleVersionHistory").children.length;
  firstHistory.resolve([{version:"stale"}]);await history1;
  assert.equal(ruleCtx.$("ruleVersionHistory").children.length,latestChildren);
  const trial=ruleCtx.submitRuleDraft(false);
  await ruleCtx.submitRuleDraft(false);assert.equal(ruleCalls.length,1);
  vm.runInContext('ruleEditorRequest++;editingRule={id:"R-002"};',ruleCtx);
  ruleCalls.shift().resolve({version:"2.1"});await trial;assert.equal(shown.length,0);
  const staleSave=ruleCtx.submitRuleDraft(true);
  vm.runInContext('ruleEditorRequest++;',ruleCtx);
  ruleCalls.shift().reject(new Error("stale publish error"));await staleSave;
  assert.equal(ruleErrors.length,0);
  const save=ruleCtx.submitRuleDraft(true);ruleCalls.shift().resolve({id:"R-002",version:"2.1"});await save;
  assert.equal(shown.length,1);assert.ok(ruleCtx.$("ruleEditorStatus").textContent.includes("已保存"));
  const uploadNodes=new Map(),uploadCalls=[],reviews=[],uploadErrors=[];
  const uploadCtx={currentUser:{id:'u1'},dz:{style:{}},enterpriseMaterials:{enabled:()=>false},
    $:id=>{if(!uploadNodes.has(id))uploadNodes.set(id,{...node(),style:{},value:'local'});return uploadNodes.get(id);},
    FormData:class{append(){}},hideError(){},showError:e=>uploadErrors.push(e),
    api:()=>new Promise((resolve,reject)=>uploadCalls.push({resolve,reject})),
    renderMaterialReview:()=>reviews.push(vm.runInContext('materialDraft.token',uploadCtx)),
    clearMaterials:()=>vm.runInContext('materialRequest++;materialDraft=null;',uploadCtx)};
  vm.createContext(uploadCtx);
  vm.runInContext('let materialRequest=0,materialDraft=null;'+
    source.slice(source.indexOf('function materialCurrent('),source.indexOf('async function refreshAIStatus('))+
    source.slice(source.indexOf('async function upload(files)'),source.indexOf('function companyInputs(')),uploadCtx);
  const upload1=uploadCtx.upload([{name:'old.xlsx',size:10}]),oldUpload=uploadCalls.shift();
  const upload2=uploadCtx.upload([{name:'new.xlsx',size:10}]);uploadCalls.shift().resolve({token:'new'});await upload2;
  oldUpload.resolve({token:'old'});await upload1;assert.deepEqual(reviews,['new']);
  const upload3=uploadCtx.upload([{name:'failed.xlsx',size:10}]),oldFailure=uploadCalls.shift();
  const upload4=uploadCtx.upload([{name:'last.xlsx',size:10}]);uploadCalls.shift().resolve({token:'last'});await upload4;
  oldFailure.reject(new Error('old upload error'));await upload3;
  assert.deepEqual(reviews,['new','last']);assert.equal(uploadErrors.length,0);
  let routed=0;uploadCtx.enterpriseMaterials={enabled:()=>true,upload:async()=>routed++};
  await uploadCtx.upload([{name:'enterprise.xlsx',size:10}]);assert.equal(routed,1);assert.equal(uploadCalls.length,0);
  const evidenceBox=node();
  const evidenceCtx={$:()=>evidenceBox,el:(tag,cls,text)=>({...node(),tag,textContent:text||""})};
  vm.createContext(evidenceCtx);
  vm.runInContext(source.slice(source.indexOf('function renderMaterialEvidence('),source.indexOf('function renderAuditNarrative(')),evidenceCtx);
  evidenceCtx.renderMaterialEvidence([{name:'合同.四流完整合同数量',value:'0.00',source:'原始来源',detail:'待完善 1 份'},
    {name:'人力.社保参保人数',value:'1.00',source:'人力来源',detail:'核对月 2026-05'},
    {name:'其他.指标',value:'99'}]);
  assert.equal(evidenceBox.hidden,false);assert.equal(evidenceBox.children.length,4);
  assert.ok(evidenceBox.children[1].textContent.includes('不等于税务合规'));
  assert.equal(evidenceBox.children[2].children[1].textContent,'待完善 1 份');
  evidenceCtx.renderMaterialEvidence([]);
  assert.equal(evidenceBox.hidden,true);assert.equal(evidenceBox.children.length,0);
  // Run the mistake controller against reordered API replies and revoked UI ownership.
  const mistakeNodes = new Map(), mistakeCalls = [];
  const mistakeNode = (tag, cls, text) => ({...node(), tag, textContent:text || "", value:"", hidden:false,
    setAttribute(){}, scrollIntoView(){}});
  let mistakeUser = {id:"student-a", role:"student"};
  const mistakeCtx = {window:{}, document:{getElementById:id=>{
    if (!mistakeNodes.has(id)) mistakeNodes.set(id,mistakeNode()); return mistakeNodes.get(id);
  }}};
  vm.createContext(mistakeCtx);
  vm.runInContext(fs.readFileSync("webapp/static/mistake-book.js", "utf8"), mistakeCtx);
  const controller = mistakeCtx.window.createMistakeBook({el:mistakeNode, getUser:()=>mistakeUser,
    api:url=>new Promise((resolve,reject)=>mistakeCalls.push({url,resolve,reject}))});
  const staleMistakes = controller.load(), staleMistakeCall = mistakeCalls.shift();
  const freshMistakes = controller.load();
  mistakeCalls.shift().resolve({cases:[{id:"current", title:"Current student case", available:false, status:"unavailable", errors:[]}], method:"current"});
  await freshMistakes;
  staleMistakeCall.resolve({cases:[{id:"private", title:"Stale private case", available:false, status:"unavailable", errors:[]}], method:"stale"});
  await staleMistakes;
  assert.equal(mistakeNodes.get("mistakeList").children[0].children[0].textContent,"Current student case");
  const revokedMistakes = controller.load(), revokedCall = mistakeCalls.shift();
  controller.reset(); mistakeUser = {id:"student-b", role:"student"};
  revokedCall.resolve({cases:[],method:"private"}); await revokedMistakes;
  assert.equal(mistakeNodes.get("mistakeBook").hidden,true);
  assert.equal(mistakeNodes.get("mistakeStatus").textContent,"");
  // A cancelled sync must not start a follow-up list read or display an old failure.
  const staleSync = controller.load(true), syncCall = mistakeCalls.shift();
  controller.invalidate(); syncCall.resolve({added:1,updated:0,unchanged:0,invalid:0}); await staleSync;
  assert.equal(mistakeCalls.length,0);
  const lostRole = controller.load(), lostRoleCall = mistakeCalls.shift();
  mistakeUser = {id:"student-b",role:"teacher"};
  lostRoleCall.reject(new Error("private failure")); await lostRole;
  assert.ok(!mistakeNodes.get("mistakeStatus").textContent.includes("private failure"));
  console.log("Frontend syntax, role, evidence and notification/rule/material/mistake race and trend area contracts passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
