"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const readConsole = () => fs.readFileSync("webapp/static/auth.js", "utf8") + "\n" + fs.readFileSync("webapp/static/console.js", "utf8");

// Sending consumes the question immediately; delayed answers cannot erase a new draft.
(async()=>{
  const source=fs.readFileSync("webapp/static/graph.js","utf8");
  const code=source.slice(source.indexOf("async function readNodeStream("),source.indexOf("function ragSourceCard("))
    +source.slice(source.indexOf('$("graphQuestionForm").addEventListener("submit"'),source.indexOf("let graphResizeTimer;"));
  function setup(mode="rag"){
    const nodes=new Map();let submit,resolve,reject,stream;
    const element=(tag,cls,text="")=>({tag,className:cls,textContent:text,value:"",children:[],dataset:{},
      append(...items){this.children.push(...items);},insertBefore(item,before){this.children.splice(this.children.indexOf(before),0,item);},
      querySelectorAll(){return [];},addEventListener(type,handler){if(type==="submit")submit=handler;}});
    const get=id=>{if(!nodes.has(id))nodes.set(id,element("div"));return nodes.get(id);};
    const request=new Promise((yes,no)=>{resolve=yes;reject=no;});
    const calls=[];
    const send=(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return request;};
    const ctx={$:get,el:element,AbortController,TextDecoder,Uint8Array,currentUser:{id:"owner"},
      graphSelected:mode==="rag"?null:"rule",graphData:{ai:{configured:true},...(mode==="enterprise"?{audit_id:"audit",taxpayer_id:"company"}:{})},
      graphRequest:1,graphBusy:false,nodeQuestionKey:"scope",nodeQuestionHistory:[],nodeQuestionController:null,
      nodeGateLabels:{},graphRagMode:()=>mode==="rag",graphCurrent:(version,owner)=>version===ctx.graphRequest&&owner===ctx.currentUser.id,
      fetch:send,api:send};
    vm.createContext(ctx);vm.runInContext(code,ctx);
    const raw="  增值税进项税额抵扣有哪些规定？  ";get("graphQuestion").value=raw;
    return {ctx,get,raw,calls,submit:()=>submit({preventDefault(){}}),reject,
      async context(){
        resolve({ok:true,headers:new Headers({"content-type":"text/event-stream"}),body:new ReadableStream({start(controller){stream=controller;}})});
        stream.enqueue(new TextEncoder().encode('data: '+JSON.stringify({type:"context",message:"已检索并读取 13 条公共原文。",manifest:{node_ids:[]}})+'\n\n'));
        await new Promise(setImmediate);
      },
      complete(){if(mode==="enterprise")resolve({answer:"核对原文",citations:[]});else{
        stream.enqueue(new TextEncoder().encode('data: '+JSON.stringify({type:"done",answer:"核对原文",status:"answerable",citations:[]})+'\n\n'));stream.close();
      }},fail(){stream.error(new Error("连接中断"));}};
  }
  for(const mode of ["rag","node","enterprise"]){
    const test=setup(mode),pending=test.submit();
    assert.equal(test.get("graphQuestion").value,"",mode+": clear as soon as the question is sent");
    assert.equal(test.get("graphMessages").children[0].children[0].textContent,"增值税进项税额抵扣有哪些规定？");
    assert.equal(test.calls[0].body.question,"增值税进项税额抵扣有哪些规定？");
    assert.equal(test.calls[0].path,mode==="rag"?"/api/knowledge/rag-ask":mode==="node"?"/api/knowledge/node-ask":"/api/knowledge/ask");
    if(mode!=="enterprise"){
      await test.context();assert.equal(test.get("graphQuestion").value,"","Context events must not restore submitted text");
    }
    // Even an intentionally retyped identical question is a new draft.
    test.get("graphQuestion").value="增值税进项税额抵扣有哪些规定？";
    test.complete();await pending;
    assert.equal(test.get("graphQuestion").value,"增值税进项税额抵扣有哪些规定？","Completion preserves the next draft");
    assert.equal(test.ctx.graphBusy,false);assert.equal(test.get("graphStop").hidden,true);
  }
  for(const failure of ["request","stream","stop","new-draft","stale"]){
    const test=setup(),pending=test.submit();
    if(failure==="request")test.reject(new Error("提问失败"));
    else{
      await test.context();
      if(failure==="new-draft")test.get("graphQuestion").value="下一条问题";
      if(failure==="stale")test.ctx.currentUser={id:"other"};
      if(failure==="stop")test.ctx.nodeQuestionController.abort();
      test.fail();
    }
    await pending;
    assert.equal(test.get("graphQuestion").value,failure==="new-draft"?"下一条问题":failure==="stale"?"":test.raw,
      failure+": restore failed submissions only in the original, empty composer");
  }
  for(const guard of ["empty","busy","missing-node"]){
    const test=setup(guard==="missing-node"?"node":"rag");
    if(guard==="empty")test.get("graphQuestion").value="  ";
    if(guard==="busy")test.ctx.graphBusy=true;
    if(guard==="missing-node")test.ctx.graphSelected=null;
    const before=test.get("graphQuestion").value;await test.submit();
    assert.equal(test.calls.length,0);assert.equal(test.get("graphQuestion").value,before);
  }
  console.log("Graph composer: immediate clearing, streamed context, draft preservation, failure recovery and submit guards passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// Roster edits expose current placement and allow explicit repair of legacy duplicates.
{
  const source = fs.readFileSync("webapp/static/classroom.js", "utf8");
  const code = source.slice(source.indexOf("  function personName("), source.indexOf('  $("classEditor").onchange'));
  const nodes = new Map();
  const element = (tag, cls, text="") => ({tag,children:[],value:"",checked:false,disabled:false,
    _text:text, get textContent(){return this._text;}, set textContent(value){this._text=value;this.children=[];},
    append(...items){this.children.push(...items);},
    querySelectorAll(selector){return this.children.flatMap(label=>label.children||[])
      .filter(node=>node.tag==="input" && (selector!=="input:checked" || node.checked));}});
  const get = id => {if(!nodes.has(id))nodes.set(id,element("div"));return nodes.get(id);};
  const student = {id:"student",display_name:"测试学生",username:"student@example.com",
    classes:[{id:"old",name:"会计5班"},{id:"keep",name:"创新1班"}]};
  const context = {data:{students:[student,{id:"new",display_name:"未分班学生",username:"new@example.com",classes:[]}],
    classes:[{id:"old",name:"会计5班",revision:1,students:[student]},
             {id:"keep",name:"创新1班",revision:1,students:[student]}]},
    editingClass:null,$:get,el:element,document:{createTextNode:text=>({textContent:text})}};
  vm.createContext(context);vm.runInContext(code,context);
  context.roster();
  let checks=get("classRoster").querySelectorAll("input");
  assert.equal(checks[0].disabled,true,"Already assigned students cannot be checked for a new class");
  assert.equal(checks[1].disabled,false,"Unassigned students remain selectable");
  assert.match(get("classRoster").children[0].children[1].textContent,/已在：会计5班、创新1班.*请先从原班级移出/);
  checks[0].checked=true;context.roster(true);
  assert.equal(get("classRoster").querySelectorAll("input")[0].checked,false,"Preserved drafts cannot silently add a conflicting member");
  get("classEditor").value="old";context.editClass();
  checks=get("classRoster").querySelectorAll("input");
  assert.equal(checks[0].disabled,false,"Legacy duplicates must remain removable");
  assert.equal(checks[0].checked,true,"Opening an old class must not silently drop members");
  assert.match(get("classRoster").children[0].children[1].textContent,/存在重复绑定/);
  checks[0].checked=false;context.roster(true);
  assert.equal(get("classRoster").querySelectorAll("input")[0].checked,false,"Explicit member removal survives a roster refresh");
  context.data.students[0].classes=[{id:"keep",name:"创新1班"}];
  context.data.classes[0].students=[];
  get("classEditor").value="keep";context.editClass();
  checks=get("classRoster").querySelectorAll("input");
  assert.equal(checks[0].disabled,false);assert.equal(checks[0].checked,true);
  assert.doesNotMatch(get("classRoster").children[0].children[1].textContent,/重复绑定/);
  console.log("Class rosters: existing placement, duplicate prevention, legacy repair and preserved selections passed.");
}

// Student labels hide login accounts unless names need disambiguation.
{
  const source=fs.readFileSync("webapp/static/classroom.js","utf8");
  const ctx={};vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("  function personName("),source.indexOf("  function roster(")),ctx);
  const first={id:"one",display_name:"同学",username:"first@example.com"};
  const second={id:"two",display_name:"同学",username:"second@example.com"};
  assert.equal(ctx.personName(first,[first]),"同学");
  assert.equal(ctx.personName(first,[first,second]),"同学（first@example.com）");
  assert.equal(ctx.personName(second,[first,second]),"同学（second@example.com）");
}

// Display names stay separate from stored school keys and disambiguate same-name teachers.
{
  const source=fs.readFileSync("webapp/static/training-staff.js","utf8");
  const ctx={state:{staff:{college:"school-key",college_name:"示例高校"},
    teachers:[{id:"one",display_name:"王老师",username:"teacher-one"}]}};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("function esc("),source.indexOf("async function api("))
    +source.slice(source.indexOf("function teacherOptions("),source.indexOf("function renderCourses(")),ctx);
  assert.equal(ctx.schoolName(),"示例高校");assert.equal(ctx.state.staff.college,"school-key");
  assert.ok(ctx.teacherOptions().includes("王老师"));assert.ok(!ctx.teacherOptions().includes("teacher-one"));
  ctx.state.teachers.push({id:"two",display_name:"王老师",username:"teacher-two"});
  assert.ok(ctx.teacherOptions().includes("teacher-one"));assert.ok(ctx.teacherOptions().includes("teacher-two"));
  console.log("Teaching names: school display names, stable school keys and same-name teacher disambiguation passed.");
}

// The task board uses the signed-in identity and refreshes progress without polling /api/me.
(async()=>{
  const all = node => [node, ...node.children.flatMap(all)];
  function node(tag, cls="", text="") {
    return {tag,className:cls,textContent:text,children:[],dataset:{},attributes:{},
      setAttribute(name,value){this.attributes[name]=String(value);},
      getAttribute(name){return this.attributes[name];},
      classList:{add(){},toggle(){}},
      append(...items){for(const item of items){item.parent=this;this.children.push(item);}},
      prepend(item){item.parent=this;this.children.unshift(item);},
      replaceChildren(...items){this.children=[];this.append(...items);},
      remove(){if(this.parent)this.parent.children=this.parent.children.filter(item=>item!==this);},
      querySelector(selector){return this.querySelectorAll(selector)[0];},
      querySelectorAll(selector){return all(this).slice(1).filter(item=>selector==="button"?item.tag==="button":item.className.split(" ").includes(selector.slice(1)));}};
  }
  const root=node("body"),head=node("head"),panel=node("section");panel.id="trainingPanel";root.append(head,panel);
  const calls=[];let pending=null,fail=false;
  const task={id:"task-1",kind:"assignment",title:"实测作业",state:"published",expected:1,
    submitted_total:1,reviewed_total:0,answer_release:"after_due",my_status:"not_started",submit_limit:1};
  const aiTasks=[{id:"ai-live",status:"published",title:"课堂练习已发布"},
    {id:"ai-draft",status:"draft",title:"课堂练习草稿"},{id:"ai-archive",status:"archived",title:"课堂练习已归档"}];
  const ctx={window:{tpAICoach:{createTaskRow:item=>node("div","tb-row",item.title)}},document:{head,createElement:tag=>node(tag),getElementById:id=>all(root).find(item=>item.id===id)},
    fetch:async path=>{calls.push(path);if(pending)return await pending;return {ok:!fail,status:503,json:async()=>fail?{detail:"任务暂不可用"}:{items:path==="/api/teaching/ai-tasks"?aiTasks.map(item=>({...item})):[{...task}]}};}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync("webapp/static/training-tasks.js","utf8"),ctx);
  const board=ctx.window.tpTrainingTasks,text=()=>all(root).map(item=>item.textContent).join("\n");
  assert.equal(calls.length,0,"Loading the login page must not request identity or task data");
  board.setUser(null);await board.refresh();assert.equal(calls.length,0);
  board.setUser({id:"enterprise",role:"org_admin"});await board.refresh();assert.equal(calls.length,0);
  board.setUser({id:"teacher",role:"teacher"});await board.refresh();
  assert.match(text(),/已交 1\/1 人 · 已复核 0 人/);
  const filters=panel.querySelector('.tb-filters'),buttons=filters.querySelectorAll('button');
  assert.equal(filters.getAttribute('aria-label'),'任务状态筛选');
  assert.equal(buttons[0].getAttribute('aria-pressed'),'true');
  assert.equal(buttons[0].textContent,"全部（4）","all task types must be counted together");
  buttons[1].onclick();assert.ok(!text().includes('实测作业'));
  assert.match(text(),/课堂练习草稿/);assert.ok(!text().includes("课堂练习已发布"));
  assert.deepEqual(buttons.map(button=>button.getAttribute('aria-pressed')),['false','true','false','false','false']);
  buttons[2].onclick();assert.match(text(),/实测作业/);
  assert.match(text(),/课堂练习已发布/);assert.equal(buttons[2].textContent,"进行中（2）");
  buttons[4].onclick();assert.match(text(),/课堂练习已归档/);assert.ok(!text().includes("实测作业"));
  buttons[3].onclick();assert.match(text(),/没有已截止任务/);assert.ok(!text().includes("课堂练习已发布"));
  buttons[2].onclick();
  task.reviewed_total=1;await board.refresh();assert.match(text(),/已复核 1 人/);
  // A superseded refresh cannot undo a newer review count.
  let resolve;pending=new Promise(done=>{resolve=done;});const old=board.refresh();pending=null;
  task.reviewed_total=2;await board.refresh();
  resolve({ok:true,json:async()=>({items:[{...task,reviewed_total:0}]})});await old;
  assert.match(text(),/已复核 2 人/);
  // Account switching must not paint a previous teacher's delayed response.
  pending=new Promise(done=>{resolve=done;});const previousUser=board.refresh();pending=null;
  board.setUser({id:"student",role:"student"});await board.refresh();
  resolve({ok:true,json:async()=>({items:[{...task,title:"private teacher task"}]})});await previousUser;
  assert.ok(!text().includes("private teacher task"));assert.ok(!text().includes("已复核 2 人"));
  assert.equal(all(root).filter(item=>item.id==="task-board-card").length,1);
  fail=true;await assert.rejects(board.refresh({throwOnError:true}),/任务暂不可用/);
  assert.match(text(),/读取失败：任务暂不可用/);
  board.setUser(null);assert.ok(!all(root).some(item=>item.id==="task-board-card"));
  assert.ok(calls.every(path=>["/api/training/tasks","/api/teaching/ai-tasks"].includes(path)));
  const consoleSource=readConsole();
  assert.ok(consoleSource.includes("window.tpTrainingTasks?.setUser(currentUser)"));
  const load=consoleSource.slice(consoleSource.indexOf("async function loadAssignments("),consoleSource.indexOf('$("btnAssignment").addEventListener'));
  assert.ok(load.includes("window.tpTrainingTasks?.refresh({throwOnError})"),"Classroom refresh must include the task board");
  console.log("Task board: no anonymous identity polling, current review counts, failure and stale-response guards passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// Auxiliary teaching cards must also stay idle while the shared login screen is visible.
for(const file of ["training-ai-coach.js","teaching-case-ui.js"]){
  const observers=[],timers=[],calls=[];
  const nodes=new Map(),node=id=>{
    if(!nodes.has(id))nodes.set(id,{id,children:[],append(...items){this.children.push(...items);}});
    return nodes.get(id);
  };
  const ctx={currentUser:null,CSS:{escape:value=>value},window:{setTimeout:fn=>timers.push(fn)},
    document:{head:node("head"),documentElement:node("html"),readyState:"complete",
      createElement:()=>({}),getElementById:node,querySelector:selector=>node(selector.slice(1))},
    MutationObserver:class{constructor(callback){observers.push(callback);}observe(){}},
    fetch:async path=>{calls.push(path);throw Error("anonymous request");}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync("webapp/static/"+file,"utf8"),ctx);
  for(let n=0;n<10;n++)for(const callback of observers)callback([]);
  assert.equal(calls.length,0,file+" must not request identity or teaching data before login");
  assert.equal(timers.length,0,file+" must not schedule anonymous login retries");
}

// Closing answer details invalidates pending reads; reopening cannot show an older task response.
(async()=>{
  const nodes=[],pending=[],writes=[];
  const node=(tag="div")=>{
    const item={tag,children:[],dataset:{},style:{},events:{},isConnected:true,open:false,_text:"",
      classList:{toggle(){}},setAttribute(){},addEventListener(name,fn){this.events[name]=fn;},
      append(...items){this.children.push(...items);},prepend(...items){this.children.unshift(...items);},
      replaceChildren(...items){this._text="";this.children=items;},scrollIntoView(){},
      closest(){return this.tag==="button"?this:null;},
      showModal(){this.open=true;},close(){this.open=false;this.events.close?.();},
      get textContent(){return this._text+this.children.map(child=>child.textContent||"").join("");},
      set textContent(value){this._text=value;this.children=[];}};
    nodes.push(item);return item;
  };
  const app=node();app.id="app-view";
  const get=id=>nodes.find(item=>item.id===id);
  const ctx={currentUser:{id:"teacher",role:"teacher"},CSS:{escape:value=>value},
    window:{setTimeout(){},confirm:()=>true},Option:function(label,value){const item=node("option");item.textContent=label;item.value=value;return item;},
    document:{head:node(),documentElement:node(),readyState:"complete",createElement:node,getElementById:id=>get(id)||null,
      querySelector:selector=>get(selector.slice(1))||null},
    MutationObserver:class{observe(){}},
    fetch:async (path,options={})=>{
      if(options.method==="POST") {writes.push({path,body:JSON.parse(options.body)});return {ok:true,json:async()=>({})};}
      if(path.endsWith("/submissions"))return await new Promise(resolve=>pending.push(resolve));
      return {ok:true,json:async()=>path==="/api/classes"?[]:{items:[{id:"task",title:"任务",status:"published",question_count:1,submitted:1,expected:1,mean_score:100}]}};
    }};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync("webapp/static/training-ai-coach.js","utf8"),ctx);
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();
  const card=get("ai-coach-card"),dialog=get("ac-results");
  assert.equal(dialog.tag,"dialog","answer details must open in a separate modal frame");
  const open=nodes.find(item=>item.dataset.ac==="results");
  card.events.click({target:open});
  assert.equal(dialog.open,true);
  dialog.events.cancel({preventDefault(){}});
  assert.equal(dialog.open,false);
  card.events.click({target:open});
  pending[0]({ok:true,json:async()=>({task:{title:"旧结果",questions:[]},items:[]})});
  await flush();
  assert.ok(!dialog.textContent.includes("旧结果"),"a closed read must not overwrite the reopened dialog");
  pending[1]({ok:true,json:async()=>({task:{title:"新结果",questions:[]},items:[]})});
  await flush();
  assert.ok(dialog.textContent.includes("新结果"));
  assert.ok(dialog.textContent.includes("暂无学生提交"));
  card.events.click({target:nodes.find(item=>item.dataset.ac==="close-results")});
  assert.equal(dialog.open,false);
  assert.equal(get("ac-results-body").textContent,"");
  card.events.click({target:open});
  const question=id=>({id,kind:"single",stem:"测试题目",options:[{key:"A",text:"选项一"}],answers:["A"],explanation:"题目解析"});
  pending[2]({ok:true,json:async()=>({task:{title:"按题分析",questions:[question("q1"),question("q2")]},items:[
    {student_name:"学生甲",detail:[{question_id:"q1",correct:true},{question_id:"q2",correct:false}]},
    {student_name:"学生乙",detail:[{question_id:"q1",correct:false},{question_id:"q2",correct:true}]},
    {student_name:"学生丙",detail:[]},
  ]})});
  await flush();
  assert.equal(nodes.filter(item=>item.className==="ac-result-question").length,2,"render each question once, not once per student");
  const group=outcome=>nodes.find(item=>item.dataset.question==="q1"&&item.dataset.outcome===outcome);
  assert.ok(group("correct").textContent.includes("答对 1 人"));
  assert.ok(group("incorrect").textContent.includes("答错 1 人"));
  assert.ok(group("unknown").textContent.includes("待核对 1 人"),"missing results must not count as incorrect");
  const right=get("ac-roster-q1-correct"),wrong=get("ac-roster-q1-incorrect");
  assert.equal(group("correct").tag,"details","student rosters use an arrow disclosure");
  assert.equal(group("incorrect").tag,"details");
  assert.equal(group("correct").open,false,"student rosters start collapsed");
  assert.equal(group("incorrect").open,false);
  assert.ok(right.textContent.includes("学生甲"));
  assert.ok(!right.textContent.includes("学生乙"));
  assert.ok(wrong.textContent.includes("学生乙"));
  assert.ok(!wrong.textContent.includes("学生甲"));
  assert.ok(get("ac-roster-q1-unknown").textContent.includes("学生丙"));
  assert.ok(!get("ac-results-body").textContent.includes("学生作答："),"do not repeat individual answer sheets");
  card.events.click({target:nodes.find(item=>item.dataset.ac==="close-results")});
  const taskBoard=node();taskBoard.id="task-board-card";
  let refreshes=0;ctx.window.tpTrainingTasks={refresh:async()=>{refreshes++;}};
  const tree=item=>[item,...item.children.flatMap(tree)];
  for(const status of ["archived","published"]) {
    const row=ctx.window.tpAICoach.createTaskRow({id:"ai-task",title:"课堂练习",status,question_count:2,submitted:1,expected:2,mean_score:50});
    const action=tree(row).find(item=>item.dataset.ac===(status==="archived"?"restore":"archive"));
    row.events.click({target:action,stopPropagation(){}});await flush();
    assert.equal(writes.at(-1).path,"/api/teaching/ai-tasks/ai-task/publish");
    assert.equal(writes.at(-1).body.published,status==="archived");
  }
  assert.equal(refreshes,2,"AI archive and restore must refresh the unified task counts");
  console.log("Answer details: modal lifecycle, stale reads, question counts and matching student rosters passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// The submission button follows pending, reviewed and returned states, including zero scores.
(async()=>{
  const nodes=new Map();
  const node=(tag="div")=>({tag,children:[],dataset:{},textContent:"",append(...items){this.children.push(...items);},
    before(item){nodes.set(item.id,item);},remove(){nodes.delete(this.id);}});
  const button=node("button"),host=node();host.dataset.assignment="task";
  nodes.set("btnSubmitExercise",button);nodes.set("exerciseBox",host);
  let progress={status:"pending_review",attempts:1,corrections:0,correction_limit:1,can_submit:false},delayed=null;
  const ctx={currentUser:{id:"student",role:"student"},window:{addEventListener(){}},document:{head:node(),createElement:node,createTextNode:text=>({textContent:text}),
    getElementById:id=>nodes.get(id),addEventListener(){}},
    fetch:async()=>delayed?await delayed:({ok:true,json:async()=>({...progress})})};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync("webapp/static/training-submission-ui.js","utf8"),ctx);
  const ui=ctx.window.tpSubmissionUI;
  await ui.refresh("task");assert.equal(button.textContent,"已提交，等待教师复核");assert.equal(button.disabled,true);
  progress.status="done";await ui.refresh("task");assert.equal(button.textContent,"已完成（已复核）");assert.equal(button.disabled,true);
  progress.status="returned";progress.can_submit=true;await ui.refresh("task");assert.equal(button.textContent,"提交答案并评分");assert.equal(button.disabled,false);
  progress.corrections=1;progress.can_submit=false;await ui.refresh("task");assert.equal(button.textContent,"订正次数已用尽");assert.equal(button.disabled,true);
  progress={...progress,status:"returned",corrections:0,can_submit:false};
  await ui.refresh("task");assert.equal(button.disabled,true,"Progress refresh cannot reopen a closed task");
  progress={...progress,status:"pending_review",can_submit:true,retake_policy:"free"};
  await ui.refresh("task");assert.equal(button.disabled,false,"Explicit free retakes must remain usable");
  let resolve;delayed=new Promise(done=>{resolve=done;});const old=ui.refresh("task");delayed=null;
  host.dataset.assignment="next";progress.status="done";progress.retake_policy="teacher_return";progress.can_submit=false;await ui.refresh("next");
  resolve({ok:true,json:async()=>({status:"returned",can_submit:true,corrections:0,correction_limit:1})});await old;
  assert.equal(button.textContent,"已完成（已复核）","A previous exercise response cannot overwrite current controls");
  delayed=new Promise(done=>{resolve=done;});const previousUser=ui.refresh("next");delayed=null;
  ctx.currentUser={id:"other-student",role:"student"};ui.reset();
  resolve({ok:true,json:async()=>({status:"returned",return_reason:"previous student private feedback"})});await previousUser;
  assert.equal(nodes.get("tp-submission-panel"),undefined,"A switched account cannot receive previous private progress");
  ctx.currentUser.role="teacher";await ui.refresh("next");assert.equal(nodes.get("tp-submission-panel"),undefined);
  console.log("Submission controls: pending review, completed review and correction states passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// Late case materials and rubric results cannot attach to a different exercise.
(async()=>{
  const source=fs.readFileSync("webapp/static/teaching-case-ui.js","utf8");
  const host={dataset:{assignment:"first"}},panels=[];let resolve;
  const delayed=new Promise(done=>{resolve=done;});
  const ctx={currentUser:{id:"student",role:"student"},studentRefreshRevision:0,
    document:{getElementById:id=>id==="exerciseBox"?host:id==="btnSubmitExercise"?{before:panel=>panels.push(panel)}:null},
    api:async path=>path.includes("/first/")?await delayed:{case_id:"second"},studentPanel:data=>data};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf("  async function refreshStudent("),source.indexOf("  // ---------------------------------------------------------------- 启动")),ctx);
  const first=ctx.refreshStudent("first");host.dataset.assignment="second";await ctx.refreshStudent("second");
  resolve({case_id:"first-private-case"});await first;
  assert.deepEqual(panels,[{case_id:"second"}]);
  console.log("Case materials: late responses stay within the active exercise.");
})().catch(error=>{console.error(error);process.exitCode=1;});
// Raw JSON coordinates are kept for evidence, while all teaching views show readable locations.
{
  const ctx={};vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('webapp/static/policy-source.js','utf8'),ctx);
  const format=ctx.formatPolicySourceLocation;
  assert.equal(format(JSON.stringify({text_path:'增值税/source.txt',start_line:88,end_line:92})), 'source.txt · 第 88–92 行');
  assert.equal(format(JSON.stringify({retained_text_path:'C:\\private\\source.txt',start_char:0,end_char_exclusive:8})), 'source.txt · 第 1–8 字符');
  assert.equal(format(JSON.stringify({provenance_kind:'catalog_annotation',catalog_field:'policies[id=T]',raw_path:'catalog/政策.yaml'})), '政策.yaml · 政策目录说明 · T');
  assert.equal(format('原件第2页'), '原件第2页');
  assert.equal(format('{bad-json'), '{bad-json');
  assert.equal(format('[]'), '原文位置待核对');
  assert.ok(readConsole().includes('load("policy-source.js")'));
  for(const file of ['training-portal.html','training-staff.html'])assert.ok(fs.readFileSync('webapp/static/'+file,'utf8').includes('/policy-source.js?'));
}
// Registration gives feedback before submission and rechecks both fields after edits.
{
  const source = readConsole();
  const start = source.indexOf('function updateSignupPasswordValidation(');
  assert.ok(start >= 0, 'Registration needs live password validation');
  const nodes = Object.fromEntries(['signupPass', 'signupPass2', 'signupPasswordHint', 'signupPasswordMatch', 'authForm'].map(id => [id, {
    value:'', dataset:{view:'signup'}, attributes:{}, textContent:'',
    setCustomValidity(message){this.validationMessage=message;},
    setAttribute(name,value){this.attributes[name]=String(value);}
  }]));
  const ctx = {$:id=>nodes[id]};
  vm.createContext(ctx);
  vm.runInContext(source.slice(start, source.indexOf('\nfunction showAuthView(', start)), ctx);
  const check = (password, confirm=password) => {
    nodes.signupPass.value=password;nodes.signupPass2.value=confirm;
    ctx.updateSignupPasswordValidation();
    return nodes.signupPass.validationMessage;
  };
  assert.match(check('short12!'), /10–128/);
  assert.match(check('abcdef1234'), /三类/);
  assert.match(check('abcdef123 '), /三类/, 'Whitespace is not a symbol');
  assert.equal(check('ZebraForest92'), '');
  assert.equal(check('éééééééé9!'), '', 'Unicode letters follow the server policy');
  assert.equal(check('ａｂｃｄｅｆ１２３！'), '', 'Check character classes after NFKC normalization');
  assert.equal(check('Ab9'+'😀'.repeat(7)), '');
  assert.match(check('Ab9'+'😀'.repeat(6)), /10–128/, 'Length counts Unicode code points');
  assert.equal(check('Ab9!'+'x'.repeat(124)), '');
  assert.match(check('Ab9!'+'x'.repeat(125)), /10–128/);
  check('ZebraForest92', 'ZebraForest93');
  assert.match(nodes.signupPass2.validationMessage, /不一致/);
  assert.equal(nodes.signupPass2.attributes['aria-invalid'], 'true');
  check('ZebraForest93');
  assert.equal(nodes.signupPass2.validationMessage, '');
  assert.match(nodes.signupPasswordMatch.textContent, /一致/);
  check('ZebraForest94', 'ZebraForest93');
  assert.match(nodes.signupPass2.validationMessage, /不一致/, 'Changing the first field rechecks confirmation');
  check('', '');
  assert.equal(nodes.signupPass.validationMessage, '');
  assert.equal(nodes.signupPasswordMatch.textContent, '');
  check('short12!', 'different');
  nodes.authForm.dataset.view='login';ctx.updateSignupPasswordValidation();
  assert.equal(nodes.signupPass.validationMessage, '', 'Hidden registration fields must not block login');
  assert.equal(nodes.signupPass2.validationMessage, '');
}
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

// Retrieval checks preserve drafts and discard stale responses across sessions.
(async()=>{
  const nodes=new Map(),timers=new Map();let next=0,actor={id:"platform",role:"platform_admin"},visible=true;
  const node=()=>({children:[],value:"",checked:false,disabled:false,hidden:false,textContent:"",append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}});
  const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  const el=(tag,className,text)=>Object.assign(node(),{tag,className,textContent:text||""});
  const calls=[];let hold=null;
  let state={schedule:{enabled:false,interval_hours:24,revision:0,next_due:null},gate:{passed:false,reason:"尚未检测"},current_version:"current-version",model:"synthetic-model",history:[],active:null};
  const api=async(url,options)=>{calls.push({url,options});if(hold)return await hold;if(options?.method==="PUT"){const body=JSON.parse(options.body);assert.equal(body.revision,0,"polls must not silently rebase a dirty schedule draft");state={...state,schedule:{...state.schedule,...body,revision:2}};return {saved:true};}if(options?.method==="POST")return {status:"queued"};return structuredClone(state);};
  const ctx={setTimeout(fn){const id=++next;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}};vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("webapp/static/retrieval-checks.js","utf8"),ctx);
  const ui=ctx.createRetrievalChecks({api,el,$,user:()=>actor,active:()=>visible});
  await ui.load();assert.equal($("retrievalChecksAdmin").hidden,false);assert.equal($("retrievalCheckHours").value,"24");
  $("retrievalCheckHours").value="12";$("retrievalCheckHours").oninput();state.schedule.revision=1;
  await ui.load();assert.equal($("retrievalCheckHours").value,"12","polling retains unsaved input");
  await $("retrievalCheckSave").onclick();assert.equal(JSON.parse(calls.find(c=>c.options?.method==="PUT").options.body).interval_hours,12);
  let release;hold=new Promise(resolve=>{release=resolve;});
  const pending=$("retrievalCheckRun").onclick();$("retrievalCheckRun").onclick();
  assert.equal(calls.filter(c=>c.options?.method==="POST").length,1,"double clicks do not enqueue duplicate jobs");
  ui.reset();actor={id:"business",role:"org_admin"};release({status:"queued"});await pending;hold=null;
  assert.equal($("retrievalChecksAdmin").hidden,true);assert.equal(timers.size,0);
  const before=calls.length;await ui.load();assert.equal(calls.length,before,"non-platform users do not read check history");
  actor={id:"platform",role:"platform_admin"};let resolveStatus;hold=new Promise(resolve=>{resolveStatus=resolve;});
  const loading=ui.load();actor={id:"new-platform",role:"platform_admin"};resolveStatus(state);await loading;hold=null;
  assert.equal(timers.size,0,"old-session responses do not start polling for a new session");
  visible=false;ui.reset();
  console.log("Retrieval checks: platform scope, dirty schedule revisions, single submission, timers and stale-session guards passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// Refresh restores the actual selected workspace view, while role-hidden views stay unavailable.
{
  const source = readConsole();
  const code = source.slice(source.indexOf("function isCampusUser("), source.indexOf("// Module preparation"));
  const panels = ["dashboardPanel", "uploadPanel", "auditPanel", "historyPanel", "knowledgePanel", "trainingPanel", "adminPanel", "notificationPanel", "orgReportPanel", "userSettingsPanel", "aiKeysPanel", "retrievalChecksPanel"];
  const permitted = {
    org_admin: panels.filter(id => !["trainingPanel", "aiKeysPanel", "retrievalChecksPanel"].includes(id)),
    accountant: panels.filter(id => !["trainingPanel", "orgReportPanel", "aiKeysPanel", "retrievalChecksPanel"].includes(id)),
    teacher: panels.filter(id => !["orgReportPanel", "aiKeysPanel", "retrievalChecksPanel"].includes(id)),
    student: ["trainingPanel", "knowledgePanel", "userSettingsPanel"],
    platform_admin: ["adminPanel", "knowledgePanel", "userSettingsPanel", "aiKeysPanel", "retrievalChecksPanel"],
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
      noticeRequest:0, settingsRequest:0, ruleEditorRequest:0, adminRequest:0, ruleCatalogRequest:0,
      panelLoadRequest:0,pendingPanelMessage:null,workspacePanelReady:()=>true,
      aiKeyProbes:{load(){calls.push("loadAIKeys");},reset(){}},
      retrievalChecks:{load(){calls.push("loadRetrievalChecks");},reset(){}},
      refreshAIStatus(){calls.push("ai");},refreshAuditClients(){calls.push("clients");},enterpriseMaterials:{refresh(){calls.push("materials");}},coreInputs:{refresh(){return Promise.resolve();}}};
    for(const name of ["loadHistory","loadNotifications","loadUserSettings","loadAssignments","loadAdmin","loadDashboard","loadOrgOverview","loadKnowledge","loadGraphRules"])ctx[name]=()=>calls.push(name);
    vm.createContext(ctx);vm.runInContext(code+source.slice(source.indexOf("function switchPanel("),source.indexOf("/* ---------- 多材料上传")),ctx);
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
    ["org_admin","aiKeysPanel","dashboardPanel"],
    ["student","aiKeysPanel","trainingPanel"],
    ["org_admin","retrievalChecksPanel","dashboardPanel"],
    ["accountant","retrievalChecksPanel","dashboardPanel"],
    ["teacher","retrievalChecksPanel","dashboardPanel"],
    ["student","retrievalChecksPanel","trainingPanel"],
    ["org_admin","unknownPanel","dashboardPanel"],
  ]) {
    const view=navigation(role,"http://localhost/#panel="+requested);
    view.ctx.switchPanel(view.ctx.initialWorkspacePanel());
    assert.equal(view.views.find(panel=>panel.classList.contains("active")).id,expected);
    assert.equal(view.location.hash,"#panel="+expected);
  }
  const graph=navigation("org_admin","http://localhost/#panel=knowledgePanel");
  graph.ctx.switchPanel(graph.ctx.initialWorkspacePanel());assert.deepEqual(graph.calls,["loadKnowledge","loadGraphRules"]);
  const retrieval=navigation("platform_admin","http://localhost/#panel=retrievalChecksPanel");
  retrieval.ctx.switchPanel(retrieval.ctx.initialWorkspacePanel());assert.deepEqual(retrieval.calls,["loadRetrievalChecks"]);
  const admin=navigation("platform_admin");admin.ctx.switchPanel("adminPanel");assert.deepEqual(admin.calls,["loadAdmin"]);
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
const selection = graph.slice(graph.indexOf("async function loadKnowledge("), graph.indexOf("async function loadGraph("));
(async () => {
  // Empty/hidden founder results must never overwrite the clipboard or claim success.
  {
    const source = readConsole();
    const code = source.slice(source.indexOf("function inviteOrgTypeLabel("), source.indexOf('$("btnCreateClient").addEventListener'));
    const nodes = new Map(), handlers = new Map(), writes = [], errors = [], toasts = [];
    const node = id => {
      if(!nodes.has(id))nodes.set(id,{value:"",textContent:"",hidden:false,disabled:false,dataset:{},attributes:{},setAttribute(name,value){this.attributes[name]=value;},
        addEventListener:(_event,handler)=>handlers.set(id,handler)});
      return nodes.get(id);
    };
    const context = {$:node, showError:message=>errors.push(message),showToast:message=>toasts.push(message),
      navigator:{clipboard:{writeText:async value=>{writes.push(value);}}},
      api:async()=>({code:"ABCD2345EFGH",org_name:"测试高校",org_type:"university",seats:20}),loadAdmin:async()=>{}};
    vm.createContext(context);vm.runInContext(code,context);
    for(const [hidden,value] of [[false,""],[false,"   "],[true,"ABCD2345EFGH"]]){
      node("inviteReveal").hidden=hidden;node("inviteRevealCode").textContent=value;
      await handlers.get("btnCopyInvite")();
      assert.equal(writes.length,0);assert.equal(toasts.length,0);
    }
    node("inviteOrgName").value="测试高校";node("inviteOrgType").value="university";node("inviteSeats").value="20";
    node("btnCopyInvite").disabled=true;
    await handlers.get("btnCreateInvite")();
    assert.equal(node("btnCopyInvite").disabled,false);
    await handlers.get("btnCopyInvite")();
    assert.deepEqual(writes,["ABCD2345EFGH"]);assert.equal(toasts.length,1);
    context.navigator.clipboard.writeText=async()=>{throw new Error("denied");};
    await handlers.get("btnCopyInvite")();
    assert.equal(toasts.length,1);assert.ok(errors.length>=4);
    handlers.get("btnHideInvite")();
    assert.equal(node("inviteReveal").hidden,true);assert.equal(node("btnCopyInvite").disabled,true);
    assert.equal(node("inviteRevealCode").textContent,"");
    await handlers.get("btnCopyInvite")();
    assert.deepEqual(writes,["ABCD2345EFGH"]);assert.equal(toasts.length,1);
  }
  for (const role of ["platform_admin", "student", "org_admin", "accountant", "teacher"]) {
    const calls = [], errors = [], choices = [], nodes = new Map();
    const context = {currentUser:{id:'user',role}, graphRequest:0, graphScopes:new Map(), graphCurrent:()=>true, clearGraph(){},
      $:id=>{if(!nodes.has(id))nodes.set(id,{value:id==='graphScope'?'TESTA':id==='graphAudit'?'audit-1':'',replaceChildren(){this.value='';}});return nodes.get(id);},
      option:(_select, value)=>choices.push(value), showError:error=>errors.push(error),
      api:async url=>{calls.push(url); return [{taxpayer_id:'TESTA',name:'合成企业',audits:[{id:'audit-1',period:'2026-01',audited_at:'2026-02-01T00:00:00Z'}]}];}, loadGraph:async()=>{}};
    vm.createContext(context);
    vm.runInContext(selection, context);
    await context.loadKnowledge();
    const enterpriseRole=["org_admin", "accountant", "teacher"].includes(role);
    assert.deepEqual(calls, enterpriseRole ? ["/api/knowledge/enterprises"] : []);
    assert.equal(context.$('graphScope').value,enterpriseRole?'TESTA':'');
    assert.equal(context.$('graphAuditField').hidden,!enterpriseRole);
    if(enterpriseRole){assert.ok(choices.includes('TESTA'));assert.equal(context.$('graphAudit').value,'audit-1');}
    assert.equal(errors.length, 0);
    assert.equal(choices[0], "");
  }
  // Exercise the actual notification functions, including out-of-order reads
  // and navigation while a write is in flight. No browser globals are replaced
  // in application code; this tiny boundary provides only the needed DOM/API.
  const source = readConsole();
  // One graph catalog replaces the old admin list; compatibility never implies approval.
  {
    const nodes=new Map(),calls=[],errors=[],editors=[];let active=true;
    function element(tag,cls,text){return {tag,textContent:text||"",value:"",hidden:false,open:true,children:[],handlers:{},
      classList:{contains:()=>active},append(...items){this.children.push(...items);},
      replaceChildren(...items){this.children=items;},addEventListener(name,fn){this.handlers[name]=fn;}};}
    const ctx={currentUser:{id:"admin",role:"platform_admin"},editingRule:null,
      $:id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},el:element,
      api:(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject})),
      showError:error=>errors.push(error),openRuleEditor:rule=>editors.push(rule),loadRuleVersionHistory(){}};
    vm.createContext(ctx);
    vm.runInContext('let graphRuleCatalog=[],ruleCatalogRequest=0;'+source.slice(source.indexOf('async function loadGraphRules('),source.indexOf('function setRuleDraft(')),ctx);
    const row={rule_id:'R-001@2.0:converted:test',rule_code:'R-001',version:'2.0',name:'收入差异',source:'converted',review_status:'candidate',legacy:{id:'R-001',enabled:true}};
    ctx.$('ruleAdmin').open=false;await ctx.loadGraphRules();assert.equal(calls.length,0,'Closed maintenance does not fetch a second catalog');
    ctx.$('ruleAdmin').open=true;
    const stale=ctx.loadGraphRules(),first=calls.shift(),latest=ctx.loadGraphRules();
    assert.equal(calls[0].url,'/api/knowledge/rule-catalog');calls.shift().resolve([row]);await latest;
    first.resolve([{...row,name:'stale'}]);await stale;
    const text=node=>[node.textContent,...node.children.map(text)].join(' ');
    assert.ok(text(ctx.$('ruleList')).includes('候选 · 待审核'));assert.ok(!text(ctx.$('ruleList')).includes('已启用'));
    const buttons=[];function walk(node){if(node.tag==='button')buttons.push(node);node.children.forEach(walk);}walk(ctx.$('ruleList'));
    buttons.find(b=>b.textContent==='配置参数版本').handlers.click();assert.equal(editors[0].id,'R-001');
    const toggle=buttons.find(b=>b.textContent==='关闭历史审计筛查'),writing=toggle.handlers.click();
    assert.equal(calls[0].url,'/api/rules/R-001/state');assert.deepEqual(JSON.parse(calls[0].options.body),{enabled:false});
    active=false;calls.shift().resolve({enabled:false});await writing;assert.equal(calls.length,0,'Leaving while a write finishes must not reload another panel');
    active=true;ctx.$('ruleCatalogSearch').value='not-found';ctx.renderGraphRules();assert.ok(text(ctx.$('ruleList')).includes('没有匹配'));
    ctx.$('ruleCatalogSearch').value='';const changedUser=ctx.loadGraphRules();ctx.currentUser={id:'other',role:'accountant'};
    calls.shift().reject(new Error('old-user failure'));await changedUser;assert.equal(errors.length,0);
    ctx.currentUser={id:'admin',role:'platform_admin'};
    const failed=ctx.loadGraphRules();calls.shift().reject(new Error('catalog unavailable'));await failed;
    assert.ok(ctx.$('ruleCatalogStatus').textContent.includes('catalog unavailable'));
    assert.equal(ctx.$('ruleList').children.length,0,'Failed refresh must not leave stale enabled controls');
    const markup=fs.readFileSync('webapp/static/index.html','utf8');
    assert.ok(markup.indexOf('id="ruleAdmin"')>markup.indexOf('id="knowledgePanel"'));
    assert.ok(markup.indexOf('id="ruleAdmin"')<markup.indexOf('id="uploadPanel"'));
    assert.ok(!source.slice(source.indexOf('async function loadAdmin()')).includes('api("/api/rules")'));
  }
  // Settings reuses self-only preferences; stale reads/writes may not alter another view/account.
  const settingsCode = source.slice(source.indexOf("function isCampusUser("), source.indexOf("async function teachingRequest("))+source.slice(source.indexOf("let settingsRequest ="), source.indexOf("function noticeCurrent("));
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
  const graphNodes=new Map(),graphCalls=[],graphDraws=[],graphTimers=[];
  const graphCtx={graphKinds:{},currentUser:{id:'owner',role:'org_admin'},graphCanvas:{...node(),style:{},setAttribute(){}},
    URLSearchParams,
    $:id=>{if(!graphNodes.has(id))graphNodes.set(id,{...node(),value:'',style:{},addEventListener(){}});return graphNodes.get(id);},
    option:(select,value)=>select.append(value),updateAIContext(){},stopNodeQuestion(){},renderNode(){},
    el:(_tag,_cls,text)=>({...node(),textContent:text,style:{setProperty(){}}}),
    setTimeout:fn=>graphTimers.push(fn),
    api:url=>new Promise((resolve,reject)=>graphCalls.push({url,resolve,reject})),
    filterGraph:()=>graphDraws.push(vm.runInContext('graphData.audit_id',graphCtx)),focusGraph(){}};
  vm.createContext(graphCtx);
  vm.runInContext('let graphFullLoad=null,graphFilterRequest=0,nodeQuestionHistory=[],nodeQuestionKey=null,graphMatchedCount=0,graphRequest=0,graphScopes=new Map(),graphAllowed=new Set(),graphData={nodes:[],edges:[]},graphSelected=null,graphVisible=new Set(),graphPositions=new Map(),graphFocused=false,graphTransform={},graphDrag=null,graphBusy=false;'+
    graph.slice(graph.indexOf('function graphCurrent('),graph.indexOf('$("graphAudit").addEventListener'))+
    graph.slice(graph.indexOf('function updateGraphRegions('),graph.indexOf('for(const id of ["knowledgeSearch"')),graphCtx);
  const listOld=graphCtx.loadKnowledge(),oldList=graphCalls.shift();
  const listNew=graphCtx.loadKnowledge();graphCalls.shift().resolve([{taxpayer_id:'new',name:'新企业',audits:[]}]);
  await new Promise(resolve=>setImmediate(resolve));
  graphCalls.shift().resolve({nodes:[],edges:[],audit_id:null,ai:{configured:false}});await listNew;
  oldList.resolve([{taxpayer_id:'old',name:'旧企业',audits:[]}]);await listOld;
  assert.deepEqual(graphCtx.$('graphScope').children,['','new']);assert.equal(graphCalls.length,0);
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
  ownerCall.resolve([{taxpayer_id:'private',name:'不可显示的企业',audits:[]}]);await changedOwner;assert.deepEqual(graphCtx.$('graphScope').children,['']);
  graphCtx.currentUser={id:'campus',role:'teacher',org_type:'university'};
  const firstPaint=graphCtx.loadGraph();
  const initialCall=graphCalls.shift();
  assert.equal(initialCall.url,'/api/training/knowledge/graph?initial=1');
  initialCall.resolve({nodes:[],edges:[],partial:true,audit_id:'initial',ai:{configured:false}});
  await firstPaint;
  assert.equal(graphCalls.length,0,'Full graph must not block the first draw');
  assert.equal(graphDraws.at(-1),'initial');
  graphTimers.shift()();
  const completing=graphCtx.ensureGraphComplete();
  assert.equal(graphCalls.length,1,'Background and user actions share the same full request');
  graphCalls.shift().resolve({nodes:[{id:'complete',kind:'clause'}],edges:[],ai:{configured:false}});
  await completing;
  assert.equal(vm.runInContext('graphData.nodes[0].id',graphCtx),'complete');
  const failureFirst=graphCtx.loadGraph();graphCalls.shift().resolve({nodes:[],edges:[],partial:true,ai:{configured:false}});await failureFirst;
  graphTimers.shift()();const incomplete=graphCtx.ensureGraphComplete();graphCalls.shift().reject(new Error('background unavailable'));await incomplete;
  assert.equal(vm.runInContext('graphData.partial',graphCtx),true,'Keep the usable initial graph when background loading fails');
  assert.match(graphCtx.$('graphCoverage').textContent,/background unavailable/);
  const retryFull=graphCtx.ensureGraphComplete();graphCalls.shift().resolve({nodes:[],edges:[],ai:{configured:false}});assert.equal(await retryFull,true);
  const again=graphCtx.loadGraph();graphCalls.shift().resolve({nodes:[],edges:[],partial:true,ai:{configured:false}});await again;
  graphTimers.shift()();const abandoned=graphCtx.ensureGraphComplete(),abandonedCall=graphCalls.shift();
  graphCtx.invalidateGraph();abandonedCall.resolve({nodes:[{id:'stale'}],edges:[],ai:{configured:false}});await abandoned;
  assert.equal(vm.runInContext('graphData.nodes.length',graphCtx),0,'Late full graphs must not restore a closed view');
  vm.runInContext(graph.slice(graph.indexOf('function neighbors('),graph.indexOf('function layoutGraph(')),graphCtx);
  graphCtx.layoutGraph=()=>{};graphCtx.drawGraph=()=>{};graphCtx.renderNode=()=>{};
  vm.runInContext('graphData={nodes:[{id:"trade",category:"关联方图",status:"skipped",kind:"trade"}],edges:[]};',graphCtx);
  graphCtx.$('knowledgeCategory').value='关联方图';graphCtx.$('graphStatus').value='skipped';
  graphCtx.filterGraph();assert.equal(vm.runInContext('graphVisible.has("trade")',graphCtx),true);
  vm.runInContext('graphData={nodes:[{id:"national",kind:"rule",rule_level:"national"},{id:"local",kind:"rule",rule_level:"provincial",region:"浙江省"},{id:"metric",kind:"metric"}],edges:[{source:"national",target:"metric"},{source:"local",target:"metric"}]};',graphCtx);
  graphCtx.$('knowledgeCategory').value='';graphCtx.$('graphStatus').value='';graphCtx.$('graphRuleLevel').value='national';
  graphCtx.filterGraph();assert.equal(vm.runInContext('graphVisible.has("national")&&graphVisible.has("metric")&&!graphVisible.has("local")',graphCtx),true);
  assert.equal(vm.runInContext('neighbors(graphVisible).has("local")',graphCtx),false);
  // The full corpus stays searchable while the SVG view has a bounded size.
  for(const id of ['knowledgeSearch','knowledgeCategory','graphStatus','graphRuleLevel','graphRegion'])graphCtx.$(id).value='';
  vm.runInContext('graphData={nodes:Array.from({length:1000},(_,i)=>({id:"policy-"+i,kind:"clause",label:"条款"+i})),edges:[]};graphSelected=null;',graphCtx);
  graphCtx.filterGraph();assert.equal(vm.runInContext('graphVisible.size',graphCtx),240);
  graphCtx.$('knowledgeSearch').value='policy-999';graphCtx.filterGraph();
  assert.equal(vm.runInContext('graphVisible.has("policy-999")',graphCtx),true);
  graphCtx.$('knowledgeSearch').value='';
  const initialLayouts=[],initialDraws=[];
  graphCtx.layoutGraph=()=>initialLayouts.push(vm.runInContext('graphVisible.size',graphCtx));
  graphCtx.drawGraph=()=>initialDraws.push(vm.runInContext('graphVisible.size',graphCtx));
  vm.runInContext('graphData={view:"public_teaching",nodes:Array.from({length:150},(_,i)=>({id:"kp-"+i,kind:"knowledge_point",label:"知识点"+i})),edges:Array.from({length:6},(_,i)=>({source:"kp-0",target:"kp-"+(i+1)}))};graphSelected=null;',graphCtx);
  graphCtx.filterGraph(true);
  assert.deepEqual(initialLayouts,[7],'Initial load must lay out only the selected neighborhood');
  assert.deepEqual(initialDraws,[7],'Initial load must draw the focused view once');
  assert.equal(vm.runInContext('graphFocused',graphCtx),true);
  vm.runInContext(graph.slice(graph.indexOf('function layoutGraph('),graph.indexOf('function drawGraph(')),graphCtx);
  graphCtx.innerWidth=1306;
  graphCtx.graphKinds={knowledge_point:[]};
  graphCtx.Math=Object.create(Math);
  graphCtx.Math.hypot=()=>assert.fail('Small focused cards must not run a discarded force simulation');
  graphCtx.layoutGraph();
  assert.equal(vm.runInContext('graphPositions.get("kp-0").x',graphCtx),480);
  assert.equal(vm.runInContext('graphPositions.get("kp-6").y',graphCtx),680);
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
    openRuleEditor:r=>shown.push(r),loadGraphRules:async()=>{}};
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

// Business controllers are prepared on demand, outside the authentication entry.
assert.ok(indexMarkup.includes('/auth.js?'));
assert.ok(!indexMarkup.includes('/console.js?'));
assert.ok(!indexMarkup.includes('/core-inputs.js?'));
assert.ok(indexMarkup.includes('id="coreInputs"'));

// Material history stays in management and is fetched only after an explicit query.
(async()=>{
  const source=readConsole(),start=source.indexOf("function renderAdminLogs("),end=source.indexOf("\nasync function loadAdmin()",start);
  const node=(tag,cls="",text="")=>({tag,className:cls,textContent:text,children:[],hidden:false,disabled:false,append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}});
  const root=node("div"),calls=[];let pending=null,fail=false,current=true;
  const ctx={$:()=>root,el:node,currentUser:{id:"admin",role:"org_admin"},Intl,Date,
    api:async url=>{calls.push(url);if(pending)return await pending;if(fail)throw Error("权限已变化");return {id:"task/id",revision:2,versions:[{revision:2,kind:"edit",created_at:"2026-10-17T13:43:30+00:00",created_by:"admin"}],events:[{action:"view",created_at:"2026-10-17T13:43:30+00:00",actor_id:"admin",revision:2}]};}};
  vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
  const rows=[{target_type:"material_batch",target_id:"task/id",action:"material_edit",created_at:"2026-10-17T13:43:30+00:00"}];
  const all=n=>[n,...n.children.flatMap(all)],button=()=>all(root).find(n=>n.tag==="button");
  ctx.renderAdminLogs(rows,()=>current);assert.equal(calls.length,0,"management only requests history on demand");
  await button().onclick();assert.deepEqual(calls,["/api/enterprise/materials/task%2Fid/trace"]);
  assert.ok(all(root).some(n=>n.textContent.includes("保存资料核对")&&n.textContent.includes("21:43:30")),"timestamps use Asia/Shanghai");
  const trace=all(root).find(n=>n.tag==="div"&&n.children.some(c=>c.textContent.includes("材料任务 task/id")));
  await button().onclick();assert.equal(trace.hidden,true);assert.equal(calls.length,1);
  await button().onclick();assert.equal(calls.length,2,"reopening reauthorizes the trace on the server");
  await button().onclick();fail=true;await button().onclick();assert.ok(all(root).some(n=>n.textContent==="查询失败：权限已变化"));
  fail=false;ctx.renderAdminLogs(rows,()=>current);let resolve;pending=new Promise(done=>{resolve=done;});const request=button().onclick();current=false;
  resolve({id:"stale-private-task",revision:3,versions:[],events:[]});await request;
  assert.ok(!all(root).some(n=>n.textContent.includes("stale-private-task")),"late responses cannot repopulate an inactive session or panel");
  for(const role of ["accountant","teacher","platform_admin"]){ctx.currentUser.role=role;ctx.renderAdminLogs(rows,()=>true);assert.equal(button(),undefined);}
  ctx.renderAdminLogs([{action:"set_university_capacity",target_type:"org",target_id:"school",created_at:"2026-10-21T01:00:00Z",
    detail:JSON.stringify({actor:{id:"actor-1",username:"root",display_name:"平台管理员"},organization_id:"school",organization_name:"模拟高校",source_ip:"2001:db8::21",before:{teacher_quota:5,student_quota:1},after:{teacher_quota:6,student_quota:3},old_revision:1,new_revision:2})},
    {action:"set_member_quota",target_type:"org",target_id:"company",detail:"seats=5;revision=1"}],()=>true);
  const quotaText=all(root).map(n=>n.textContent).join("\n");
  assert.match(quotaText,/来源 IP：2001:db8::21/);assert.match(quotaText,/教职工总额度：5 → 6/);
  assert.match(quotaText,/学生总额度：1 → 3/);assert.match(quotaText,/配置版本：1 → 2/);
  assert.match(quotaText,/历史记录未保存完整前后额度及来源 IP/);
  console.log("Admin material trace: on-demand query, close/reopen authorization, failure and stale-session guards passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// Identity and workspace selection come from the authenticated server record.
{
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id,{value:"",dataset:{view:"login",setup:"0"},hidden:false,disabled:false,addEventListener(){}});
    return nodes.get(id);
  };
  const ctx={$:node,window:{addEventListener(){}},document:{getElementById:node}};vm.createContext(ctx);
  const auth=fs.readFileSync("webapp/static/auth.js","utf8");
  vm.runInContext(auth.slice(auth.indexOf("function updateAuthCollege("),auth.indexOf('$("loginUser").addEventListener')),ctx);
  vm.runInContext(fs.readFileSync("webapp/static/university-workspace.js","utf8"),ctx);
  const workspace=ctx.window.universityWorkspace;
  assert.ok(workspace.viewsFor("student","student").some(v=>v[0]==="tasks"));
  assert.ok(!workspace.viewsFor("student","student").some(v=>v[0]==="qa"),"Public Q&A stays in the knowledge graph instead of a duplicate menu");
  assert.ok(!workspace.viewsFor("student","student").some(v=>v[0]==="goals"),"Goals and countdown belong to the learning homepage");
  assert.ok(!workspace.viewsFor("student","unlinked").some(v=>v[0]==="qa"));
  for(const workspaceValue of ["/training-staff","university",null]){
    const user=workspace.resolveIdentity({id:"campus",org_type:"university",workspace:workspaceValue,role:"org_admin",education_role:"admin"});
    assert.equal(user.workspace,"university");assert.equal(user.role,"admin");
  }
  const unlinked=workspace.resolveIdentity({org_type:"university",role:"org_admin"});
  assert.equal(unlinked.role,"unlinked","Unlinked university accounts must never fall through to enterprise menus");
  assert.equal(workspace.resolveIdentity({org_type:"enterprise",role:"org_admin"}),null);
  const teacher=workspace.viewsFor("staff","admin").map(v=>v[0]);
  for(const key of ["home","tasks","statistics","knowledge","archives"])assert.ok(teacher.includes(key));
  const school=workspace.viewsFor("staff","school_admin").map(v=>v[0]);
  assert.ok(school.includes("home")&&!school.includes("content")&&!school.includes("judge"));
  workspace.updateLogin();
  assert.equal(node("loginCollegeField").hidden,true);
  assert.equal(node("loginCollege").disabled,true);
  node("loginCollege").dataset.needed="1";workspace.updateLogin();
  assert.equal(node("loginCollegeField").hidden,false);
  assert.equal(node("loginCollege").required,true);
  node("authForm").dataset.view="signup";workspace.updateLogin();
  assert.equal(node("loginCollege").disabled,true,"disambiguation must not block registration or reset");
  console.log("University navigation and conditional ownership disambiguation passed.");
}

// Student countdown distinguishes real remaining days, exam day, expired and missing dates.
{
  const source=fs.readFileSync("webapp/static/training-portal.js","utf8");
  const start=source.indexOf("function countdownPanel("),end=source.indexOf("const SOURCE_LABEL",start);
  const ctx={};vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
  assert.match(ctx.countdownPanel({days_left:72,expired:false}),/72<\/strong>/);
  assert.match(ctx.countdownPanel({days_left:0,expired:false}),/今天/);
  assert.match(ctx.countdownPanel({days_left:-3,expired:true}),/已到期/);
  for(const value of [null,{}, {days_left:"72"}]){
    const html=ctx.countdownPanel(value);assert.match(html,/尚未设置考试日期/);assert.ok(!html.includes("NaN"));
  }
  console.log("Student countdown: remaining days, exam day, expired and unset states passed.");
}

// Selected-organization capacity must survive refreshes without leaking stale school data.
(async()=>{
  const source=readConsole(),nodes=new Map(),calls=[];
  const node=(tag,cls,text="")=>({tag,value:"",textContent:text,hidden:true,disabled:true,children:[],style:{},open:false,
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},reportValidity(){return this.valid!==false;},focus(){},
    showModal(){this.open=true;},close(){this.open=false;this.onclose?.();},
    addEventListener(event,handler){if(event==="close")this.onclose=handler;else this[event]=handler;}});
  const ctx={currentUser:{id:"root",role:"platform_admin"},memberRequest:0,memberSnapshot:null,
    $:id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},el:node,
    clearMemberInvitations(){},renderMemberInvitations(){},renderAdminLogs(){},showError(){},
    api:(url,options)=>url==="/api/audit-log"?Promise.resolve([]):new Promise((resolve,reject)=>calls.push({url,options,resolve,reject}))};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("function memberCurrent("),source.indexOf("function clearMemberInvitations("))+
    source.slice(source.indexOf("let memberQuotaEdit="),source.indexOf('$("btnSaveInvitationPolicy").addEventListener'))+
    source.slice(source.indexOf("async function loadMembers("),source.indexOf('$("memberOrg").addEventListener')),ctx);
  const school={org_id:"school",org_type:"university",members:[],quota:{seats:5,used:2,remaining:3},
    university_quota:{college:"school/college",teacher_quota:5,student_quota:1,teachers_used:1,students_used:1,teacher_remaining:4,student_remaining:0,allocated:1,unallocated:0,revision:7}};
  const enterprise={org_id:"company",org_type:"enterprise",members:[],quota:{seats:5,used:2,remaining:3,revision:1}};
  const respond=(result)=>{calls.shift().resolve(result);calls.shift().resolve({links:[]});};
  const unlock=async(kind)=>{ctx.$(kind==="university"?"btnEditUniversityCapacity":"btnEditMemberQuota").click();await ctx.$("quotaConfirmAccept").click();};
  const save=(kind)=>{ctx.$(kind==="university"?"btnSaveUniversityCapacity":"btnSaveMemberQuota").click();return ctx.$("quotaConfirmAccept").click();};
  ctx.$("memberOrg").value="school";
  let pending=ctx.loadMembers();respond(school);await pending;
  assert.equal(ctx.$("universityCapacityPanel").hidden,false);
  assert.equal(ctx.$("memberQuotaEditor").hidden,true);
  assert.match(ctx.$("memberQuotaStatus").textContent,/教职工 1 \/ 5.*学生 1 \/ 1/);
  assert.ok(!ctx.$("memberQuotaStatus").textContent.includes("2 / 5"));
  assert.equal(ctx.$("universityTeacherQuota").disabled,true);
  assert.equal(ctx.$("btnSaveUniversityCapacity").hidden,true);
  ctx.$("btnEditUniversityCapacity").click();assert.equal(ctx.$("quotaConfirmDialog").open,true);
  ctx.$("quotaConfirmCancel").click();assert.equal(ctx.$("universityTeacherQuota").disabled,true);assert.equal(calls.length,0);
  await unlock("university");assert.equal(ctx.$("universityTeacherQuota").disabled,false);
  ctx.$("universityTeacherQuota").value="6";ctx.$("universityStudentQuota").value="3";
  ctx.$("btnSaveUniversityCapacity").click();assert.equal(calls.length,0);
  assert.ok(ctx.$("quotaConfirmChanges").children.some(n=>n.textContent.includes("5 → 6")));
  ctx.$("quotaConfirmCancel").click();assert.equal(calls.length,0);assert.equal(ctx.$("universityTeacherQuota").value,"6");
  pending=save("university");
  const write=calls.shift();assert.equal(write.url,"/api/universities/school%2Fcollege/capacity");
  assert.deepEqual(JSON.parse(write.options.body),{teacher_quota:6,student_quota:3,revision:7});
  write.resolve({});await new Promise(resolve=>setImmediate(resolve));
  respond({...school,university_quota:{...school.university_quota,teacher_quota:6,student_quota:3,teacher_remaining:5,student_remaining:2,revision:8}});
  await pending;assert.equal(ctx.$("universityCapacityStatus").textContent,"席位修改已保存。");
  assert.equal(ctx.$("universityTeacherQuota").disabled,true);
  assert.match(ctx.$("memberQuotaStatus").textContent,/教职工 1 \/ 6.*学生 1 \/ 3/);
  const stale=ctx.loadMembers(),old=calls.splice(0);
  assert.equal(ctx.$("universityCapacityPanel").hidden,true);
  ctx.$("memberOrg").value="company";pending=ctx.loadMembers();respond(enterprise);await pending;
  old[0].resolve(school);old[1].resolve({links:[]});await stale;
  assert.equal(ctx.$("universityCapacityPanel").hidden,true);
  assert.equal(ctx.$("memberQuotaEditor").hidden,false);
  assert.equal(ctx.$("memberQuotaStatus").textContent,"席位 2 / 5，剩余 3。");
  assert.equal(ctx.$("memberSeats").disabled,true);
  await unlock("enterprise");ctx.$("memberSeats").value="9";
  ctx.$("btnCancelMemberQuota").click();assert.equal(ctx.$("memberSeats").value,5);assert.equal(ctx.$("memberSeats").disabled,true);
  await unlock("enterprise");ctx.$("memberSeats").value="9";ctx.$("memberSeats").valid=false;
  ctx.$("btnSaveMemberQuota").click();assert.equal(ctx.$("quotaConfirmDialog").open,false);assert.equal(calls.length,0);
  ctx.$("memberSeats").valid=true;pending=save("enterprise");
  const enterpriseWrite=calls.shift();assert.equal(enterpriseWrite.url,"/api/members/company/quota");
  assert.deepEqual(JSON.parse(enterpriseWrite.options.body),{seats:9,revision:1});
  enterpriseWrite.reject(new Error("版本冲突，请刷新"));await pending;
  assert.match(ctx.$("memberQuotaSaveStatus").textContent,/版本冲突/);assert.equal(ctx.$("memberSeats").value,"9");
  ctx.$("btnCancelMemberQuota").click();
  ctx.$("memberOrg").value="school";pending=ctx.loadMembers();respond({...school,university_quota:null});await pending;
  assert.equal(ctx.$("universityCapacityPanel").hidden,true);assert.match(ctx.$("memberQuotaStatus").textContent,/尚未关联/);
  pending=ctx.loadMembers();respond(school);await pending;
  await unlock("university");ctx.$("universityTeacherQuota").value="7";
  const saving=save("university"),lateWrite=calls.shift();
  ctx.$("memberOrg").value="company";pending=ctx.loadMembers();respond(enterprise);await pending;
  lateWrite.resolve({});await saving;assert.equal(calls.length,0);
  assert.equal(ctx.$("universityCapacityStatus").textContent,"");
  ctx.$("memberOrg").value="school";pending=ctx.loadMembers();respond(school);await pending;
  ctx.$("btnEditUniversityCapacity").click();ctx.currentUser={id:"other",role:"platform_admin"};
  await ctx.$("quotaConfirmAccept").click();assert.equal(calls.length,0);assert.equal(ctx.$("universityTeacherQuota").disabled,true);
  ctx.currentUser={id:"owner",role:"org_admin"};ctx.renderMemberQuota(school);
  assert.equal(ctx.$("universityCapacityPanel").hidden,true);
  console.log("Member capacity: locked defaults, unlock/save confirmations, cancellations, failed saves, school/enterprise accounting and stale read/write/session guards passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});

// A late profile refresh must preserve open note drafts, scoped to the same point.
{
  const source=fs.readFileSync("webapp/static/training-portal.js","utf8");
  const ctx={};vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("function noteDrafts("),source.indexOf("function renderProfile(")),ctx);
  const item=(id,text,hidden=false)=>{const textarea={value:text},editor={hidden,querySelector:()=>textarea};return {id,querySelector:()=>editor,editor,textarea};};
  const old=[item("point-a","unsaved draft"),item("point-b","cancelled",true)];
  const drafts=ctx.noteDrafts({querySelectorAll:()=>old});
  assert.equal(drafts.length,1);
  const fresh=[item("point-a","server note",true),item("point-c","other point",true)];
  ctx.restoreNoteDrafts({querySelectorAll:()=>fresh},drafts);
  assert.equal(fresh[0].textarea.value,"unsaved draft");assert.equal(fresh[0].editor.hidden,false);
  assert.equal(fresh[1].textarea.value,"other point");assert.equal(fresh[1].editor.hidden,true);
  console.log("Profile refresh preserves open drafts and does not transfer drafts across knowledge points.");
}

// Startup must coalesce downloads, retry failed assets, and ignore superseded navigation.
(async()=>{
  const auth=fs.readFileSync('webapp/static/auth.js','utf8');
  const appended=[];
  const ctx={document:{createElement:()=>({remove(){this.removed=true;}}),head:{append:node=>appended.push(node)}}};
  vm.createContext(ctx);
  vm.runInContext(auth.slice(auth.indexOf('const clientScriptLoads'),auth.indexOf('let workspaceScripts')),ctx);
  const first=ctx.loadClientScript('/one.js'),same=ctx.loadClientScript('/one.js');
  assert.equal(first,same);assert.equal(appended.length,1);
  appended[0].onload();await first;
  assert.equal(ctx.loadClientScript('/one.js'),first,'Successful scripts must not bind their handlers twice');
  const failed=ctx.loadClientScript('/retry.js');
  const rejected=assert.rejects(failed,/加载失败/);
  appended[1].onerror();await rejected;assert.equal(appended[1].removed,true);
  const retry=ctx.loadClientScript('/retry.js');assert.notEqual(retry,failed);
  appended[2].onload();await retry;

  const source=fs.readFileSync('webapp/static/console.js','utf8');
  const pending=[],switched=[],messages=[],errors=[],panel={inert:false,attrs:{},before(node){messages.push(node);},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];}};
  const navigation={currentUser:{id:'owner'},panelLoadRequest:1,pendingPanelMessage:null,panelLoadTickets:new WeakMap(),preparedPanels:new Set(),
    $:()=>panel,el:()=>({setAttribute(){},remove(){this.removed=true;},replaceChildren(...nodes){this.children=nodes;}}),
    document:{createTextNode:text=>text},prepareWorkspacePanel:()=>new Promise((resolve,reject)=>pending.push({resolve,reject})),
    switchPanel:id=>switched.push(id),showError:message=>errors.push(message)};
  vm.createContext(navigation);
  vm.runInContext(source.slice(source.indexOf('function deferWorkspacePanel('),source.indexOf('function switchPanel(')),navigation);
  navigation.deferWorkspacePanel('knowledgePanel',1,'owner');
  navigation.panelLoadRequest=2;navigation.deferWorkspacePanel('knowledgePanel',2,'owner');
  pending[0].resolve();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(switched.length,0);assert.equal(panel.inert,true,'Older loads cannot unlock the newer loading view');
  navigation.preparedPanels.add('knowledgePanel');pending[1].resolve();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(switched,['knowledgePanel']);assert.equal(panel.inert,false);
  navigation.panelLoadRequest=3;navigation.deferWorkspacePanel('historyPanel',3,'owner');
  navigation.currentUser={id:'other'};pending[2].reject(new Error('private failure'));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(errors.length,0);assert.equal(switched.length,1,'Previous identities cannot reopen a page');

  const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},value:''});return nodes.get(id);};
  const entries=[],views=[],calls=[];
  const bootstrap={URLSearchParams,location:{search:'',hash:'',pathname:'/'},history:{replaceState(){}},$:node,
    api:async path=>{calls.push(path);return {needs_setup:false,user:null};},enterApp:user=>entries.push(user),
    showAuthView:view=>views.push(view),showToast(){}};
  vm.createContext(bootstrap);
  vm.runInContext('let pendingEmailToken="";'+auth.slice(auth.indexOf('async function bootstrap('),auth.indexOf('const authViews')),bootstrap);
  await bootstrap.bootstrap();assert.deepEqual(calls,['/api/status']);assert.deepEqual(views,['login']);assert.equal(entries.length,0);
  bootstrap.api=async()=>({needs_setup:false,user:{id:'remembered'}});
  await bootstrap.bootstrap();assert.equal(entries[0].id,'remembered');
  console.log('Startup: anonymous authentication stays independent, remembered sessions enter directly, script downloads coalesce/retry, stale loads and identities cannot override navigation.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Switching authentication views while workspace assets download cancels that entry.
(async()=>{
 const auth=fs.readFileSync('webapp/static/auth.js','utf8');
 const form={dataset:{ready:'1'}},submit={},entered=[];let release;
 const ctx={authViewVersion:1,workspaceScripts:new Promise(resolve=>{release=resolve;}),
   $:id=>id==='authForm'?form:submit,enterWorkspace:user=>entered.push(user),showAuthView(){throw new Error('Unexpected view reset');}};
 vm.createContext(ctx);
 vm.runInContext(auth.slice(auth.indexOf('async function enterApp('),auth.indexOf('async function startAuthentication(')),ctx);
 const pending=ctx.enterApp({id:'old-entry'});ctx.authViewVersion++;release();await pending;
 assert.equal(entered.length,0);assert.equal(form.dataset.entering,undefined);
 await ctx.enterApp({id:'current-entry'});assert.equal(entered[0].id,'current-entry');
 console.log('Authentication view changes cancel pending workspace entry.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// The faster cellular-noise traversal must preserve every source texel.
(async()=>{
  const source=fs.readFileSync('webapp/static/auth-ocean.mjs','utf8');
  const block=source.slice(source.indexOf('const noiseVoxels='),source.indexOf('const cloudNoise='));
  let yields=0,clock=0;
  const create=new Function('state','yieldTask','performance',block+'return {run:prepareNoiseSource,bytes:noiseVoxels};');
  const noise=create({startup:{}},async()=>{yields++;},{now:()=>clock++});
  await noise.run();
  assert.equal(require('node:crypto').createHash('sha256').update(noise.bytes).digest('hex'),
    '06a67de706898665af771e7aae163df91b3a7606d690907a17f5384231112ac3',
    'Cloud noise must match the original voxel traversal byte for byte');
  assert.ok(yields>0,'Noise generation must yield periodically for input');
  console.log('Ocean noise: original texture checksum and cooperative CPU slices passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// A lean position-only grid retains PlaneGeometry's exact drawing topology.
(async()=>{
  const {pathToFileURL}=require('node:url');
  const THREE=await import(pathToFileURL(require('node:path').resolve('webapp/static/vendor/three.module.mjs')).href);
  const source=fs.readFileSync('webapp/static/auth-ocean.mjs','utf8');
  const block=source.slice(source.indexOf('const gridX='),source.indexOf('const water=new THREE.Mesh'));
  const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
  const create=new AsyncFunction('THREE','camera','yieldTask',block+'updateOceanGrid();return waterGeometry;');
  for(const aspect of [16/9,390/844]){
    const camera=new THREE.PerspectiveCamera(44,aspect,.1,500);
    camera.position.set(0,3.4,12.8);camera.lookAt(0,4.65,-6.5);
    const actual=await create(THREE,camera,async()=>{}),reference=new THREE.PlaneGeometry(2,2,360,240);
    assert.equal(actual.attributes.position.count,reference.attributes.position.count);
    assert.deepEqual(actual.index.array,reference.index.array,'Water triangle order must remain identical');
    assert.ok(actual.attributes.position.array.every(Number.isFinite));
    actual.dispose();reference.dispose();
  }
  console.log('Ocean mesh: original vertex count, triangle order and finite desktop/mobile projection passed.');
  const {oceanVendor}=require('./build_auth_ocean.cjs');
  const vendor=fs.readFileSync('webapp/static/vendor/three.module.mjs','utf8');
  const scoped=oceanVendor(vendor,source);
  const slim=await import('data:text/javascript;base64,'+Buffer.from(scoped).toString('base64'));
  const retained=['colorspace_fragment','colorspace_pars_fragment','common','tonemapping_pars_fragment'];
  assert.deepEqual(Object.keys(slim.ShaderChunk).sort(),retained);
  for(const chunk of retained)assert.equal(slim.ShaderChunk[chunk],THREE.ShaderChunk[chunk],`Retained ${chunk} GLSL must remain exact`);
  assert.equal(Object.keys(slim.ShaderLib).length,0);
  assert.ok(THREE.ShaderLib.basic.vertexShader,'The public vendored module must retain standard shaders');
  for(const unsupported of [source.replace('new THREE.ShaderMaterial','new THREE.MeshStandardMaterial'),
    source.replace('THREE.NoToneMapping','THREE.ACESFilmicToneMapping'),
    source+'\nscene.background = texture;',source+'\n#include <fog_fragment>']){
    assert.throws(()=>oceanVendor(vendor,unsupported),/shader features changed/);
  }
  assert.throws(()=>oceanVendor(vendor.replace('const ShaderLib = {','const ChangedShaderLib = {'),source),/Unexpected vendored/);
  console.log('Ocean bundle: exact retained GLSL, intact public Three module and unsupported-feature build guards passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Hidden sky preparation must not redraw the entire ocean for every strip.
{
  const source=fs.readFileSync('webapp/static/auth-ocean.mjs','utf8');
  const animate=source.slice(source.indexOf('function prepareOpeningSky()'),source.indexOf('function resume()'));
  let strips=0,draws=0,scheduled=0;
  const ctx={frameId:0,previous:0,fpsTime:0,fpsFrames:0,motionRampStart:NaN,holdOpening:true,
    state:{active:true,lost:false,ready:true,paused:false,openingHeld:true,skyReady:false,time:0,speed:1,startup:{}},
    document:{hidden:false},performance:{now:()=>0},shared:{uTime:{value:0}},composite:{uniforms:{uGlassStrength:{value:0}}},
    updateSkyCache(){strips++;if(strips===48){ctx.state.skyReady=true;ctx.state.startup.skyReadyMs=1;}},
    draw(){draws++;},requestAnimationFrame(){return ++scheduled;}};
  vm.createContext(ctx);vm.runInContext(animate,ctx);
  ctx.animate(100);assert.equal(strips,32);assert.equal(draws,0);
  ctx.animate(120);assert.equal(strips,48);assert.equal(draws,1,'Full-quality sky must be drawn before reveal');
  assert.equal(ctx.state.time,0,'Preparing the sky must preserve the poster opening pose');
  ctx.animate(140);assert.equal(scheduled,2,'Held ready scenes must stop scheduling frames');
  ctx.state.openingHeld=false;ctx.animate(160);assert.equal(draws,2);assert.ok(ctx.state.time>0);
  ctx.state.lost=true;ctx.animate(180);assert.equal(draws,2);assert.equal(strips,48);
  ctx.state.lost=false;ctx.document.hidden=true;ctx.animate(200);assert.equal(draws,2);
  let clock=0;ctx.performance.now=()=>++clock;ctx.document.hidden=false;
  ctx.state.skyReady=false;ctx.state.openingHeld=true;strips=0;
  ctx.animate(220);assert.equal(strips,4,'Slow sky submissions must stop at the CPU time budget');assert.equal(draws,2);
  console.log('Ocean startup: bounded sky-only preparation, full-quality first draw, held pose, active motion, context-loss and hidden-page guards passed.');
}

// Start a bounded sky slice before waiting for a display refresh, with the
// same hidden-page and context-loss boundaries as the later animation loop.
(async()=>{
  const source=fs.readFileSync('webapp/static/auth-ocean.mjs','utf8');
  const code=source.slice(source.indexOf('async function prepareFirstFrame()'),source.indexOf('function coverFrame()'));
  let events=[];
  const ctx={state:{lost:false,openingHeld:true,skyReady:false,active:true},document:{hidden:false},
    updateMaterialFlow(){},updateWaveCache(){},updateSheetPose(){},updateSkyCache(){},
    async yieldTask(){events.push('yield');},draw(){events.push('draw');},prepareOpeningSky(){events.push('sky');}};
  vm.createContext(ctx);vm.runInContext(code,ctx);
  await ctx.prepareFirstFrame();assert.deepEqual(events.slice(-3),['draw','yield','sky']);
  for(const suppressed of ['hidden','inactive','unheld']){
    events=[];ctx.document.hidden=suppressed==='hidden';ctx.state.active=suppressed!=='inactive';ctx.state.openingHeld=suppressed!=='unheld';
    await ctx.prepareFirstFrame();assert.ok(!events.includes('sky'),suppressed+' scenes must not run opening work');
  }
  ctx.document.hidden=false;ctx.state.active=true;ctx.state.openingHeld=true;events=[];
  ctx.prepareOpeningSky=()=>{events.push('sky');ctx.state.skyReady=true;};
  await ctx.prepareFirstFrame();assert.deepEqual(events.slice(-2),['sky','draw'],'An early completed sky must be drawn before the API resolves');
  events=[];ctx.state.skyReady=false;
  ctx.yieldTask=async()=>{events.push('yield');if(events.filter(e=>e==='yield').length===4)ctx.state.lost=true;};
  await assert.rejects(()=>ctx.prepareFirstFrame(),/context lost/);assert.ok(!events.includes('sky'));
  console.log('Ocean first-frame task: early bounded work, hidden/inactive/unheld guards, full draw and context-loss cancellation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Prefer real task yields while retaining a working fallback on older browsers.
(async()=>{
  const source=fs.readFileSync('webapp/static/auth-ocean.mjs','utf8');
  const code=source.slice(source.indexOf('const yieldTask='),source.indexOf('const startupStart='));
  let yields=0,timers=0;
  const modern={scheduler:{yield(){yields++;return Promise.resolve();}},setTimeout(){throw new Error('Modern browsers must not accumulate timer delays');}};
  vm.createContext(modern);vm.runInContext(code+'globalThis.yieldForTest=yieldTask;',modern);
  await modern.yieldForTest();assert.equal(yields,1);
  const legacy={setTimeout(resolve,delay){timers++;assert.equal(delay,0);resolve();}};
  vm.createContext(legacy);vm.runInContext(code+'globalThis.yieldForTest=yieldTask;',legacy);
  await legacy.yieldForTest();assert.equal(timers,1);
  console.log('Ocean task yielding: scheduler API and older-browser timer fallback passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Anonymous idle pages start promptly; typing and navigation still cancel preparation.
{
  const source=fs.readFileSync('webapp/static/auth-glass.js','utf8');
  const code=source.slice(source.indexOf('  function prepareWhenIdle()'),source.indexOf('  function unavailable()'));
  let now=100,mounts=0,eligible=true;const timers=[],idle=[];
  const ctx={ocean:null,loading:false,failed:false,startTimer:null,idleId:null,lastInteraction:0,
    performance:{now:()=>now},canPrepare:()=>eligible,
    setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},
    window:{requestIdleCallback(fn){idle.push(fn);return idle.length;}},
    mountWhenReady(){mounts++;}};
  vm.createContext(ctx);vm.runInContext(code,ctx);
  ctx.prepareWhenIdle();assert.equal(timers.length,0);assert.equal(idle.length,1,'Register the current idle slot without an intervening timer');
  now=100;idle[0]();assert.equal(mounts,1,'Eligible painted forms must not wait for an arbitrary startup timer');
  now=300;ctx.lastInteraction=300;ctx.prepareWhenIdle();assert.equal(timers[0].delay,1200);
  now=1500;timers[0].fn();eligible=false;idle[1]();assert.equal(mounts,1,'Entering the workspace cancels queued GPU preparation');
  const before=timers.length;ctx.prepareWhenIdle();assert.equal(timers.length,before,'Remembered sessions must not schedule a background');
  eligible=true;ctx.lastInteraction=0;ctx.prepareWhenIdle();ctx.prepareWhenIdle();
  assert.equal(idle.length,3,'Repeated updates must share the pending idle callback');eligible=false;idle[2]();
  assert.equal(mounts,1,'Entering the workspace also cancels a directly queued idle start');
  eligible=true;ctx.lastInteraction=0;ctx.window.requestIdleCallback=undefined;
  ctx.prepareWhenIdle();assert.equal(timers.at(-1).delay,0,'Older browsers retain a task boundary');timers.at(-1).fn();assert.equal(mounts,2);
  console.log('Ocean scheduling: prompt first idle start, input quiet interval and workspace-entry cancellation passed.');
}
