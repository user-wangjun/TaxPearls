"use strict";
// Exercise the shipped controller: failed saves retain edits, stale sessions cannot display evidence.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.value="";this.checked=false;this.disabled=false;this.listeners={};this.style={};this.dataset={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(key,value){this[key]=value;}
  removeAttribute(key){delete this[key];}
  addEventListener(key,fn){this.listeners[key]=fn;}
  click(){this.clicked=true;}
  focus(){this.focused=true;}
  showModal(){this.open=true;}
  close(value=""){this.returnValue=value;this.open=false;this.listeners.close?.();}
  querySelectorAll(selector){const tags=selector.split(",");return all(this).filter(n=>tags.includes(n.tag));}
}
function all(root){return [root,...root.children.flatMap(all)];}
(async()=>{
  const ids=new Map(),$=id=>{if(!ids.has(id))ids.set(id,new Node("div"));return ids.get(id);};
  const errors=[],calls=[],continued=[];let actor={id:"one",role:"org_admin"},fail=false,failScope=false,delay=null,failDelete=false,identityFailure=null,failAI=false,failedBatch=false,aiReady=true,acceptance=true,holdHistory=null,aiDelay=null,failAIRule=null,timeoutAI=false,expectedRegion="CN-33";
  let checkRules=[{rule_id:"VAT-R01",name:"销项与申报核对"},{rule_id:"R-001",name:"收入核对"}];
  const sample={id:"task",revision:1,scope_period_candidates:[{start_date:"2026-01-01",end_date:"2026-01-31",sources:[{file_id:"f",position:"Sheet1"}]}],task:{company_name:"企业",tax_no:"91310000123456789A",start_date:"2026-01-01",end_date:"2026-01-31",range_kind:"month",
      scope_origins:{},scope_candidates:{start_date:[{value:"2026-01-01",sources:[{file_id:"f",position:"Sheet1"}]}],end_date:[{value:"2026-01-31",sources:[{file_id:"f",position:"Sheet1"}]}]},applicable_forms:null},
    processing:{status:"needs_completion",issues:[],notices:[],records:[],scope:{kind:"month",start_date:"2026-01-01",end_date:"2026-01-31",confirmed:false,outside_records:0,unknown_date_records:0},boundary:"规则核验前"},
    documents:[{id:"review-pdf",name:"进项.pdf",material_type:"invoice_detail",records:[],parts:[],sha256:"synthetic-pdf",pdf_candidate:true},
      {id:"review-sheet",name:"余额.xlsx",material_type:"account_balance",records:[],parts:[],sha256:"synthetic-sheet"}],
    files:[{id:"excluded-file",deleted_at:null}],source_options:{"excluded-file":{excluded:true,original_name:"进项.xlsx"},
      "review-pdf":{original_name:"进项.pdf",ocr:false,source_reviewed:false,confirmed_metadata:{direction:"output"}},"review-sheet":{original_name:"余额.xlsx",ocr:false}},versions:[]};
  let reports=[],reportFailure=false,reportListFailure=false,reportHold=null;
  const api=async(url,options)=>{calls.push({url,options});if(url.endsWith("/reports")){if(reportHold)return await reportHold;if(options?.method==="POST"){if(reportFailure)throw new Error("测试报告生成失败");const data=JSON.parse(options.body);assert.equal(data.expected_revision,sample.revision);const row={version:reports.length+1,input_revision:sample.revision,created_at:"2026-10-19T00:00:00+08:00"};reports=[row,...reports];return structuredClone(row);}if(reportListFailure)throw new Error("测试报告读取失败");return structuredClone(reports);}if(url==="/api/enterprise/input-tasks"&&!options&&holdHistory)return await holdHistory;if(delay)return await delay;
    if(url.endsWith("/identity-validation")){if(identityFailure instanceof Error)throw identityFailure;return identityFailure||{valid:true,errors:{}};}
    if(url==="/api/knowledge/checks?enterprise=true")return checkRules;
    if(url==="/api/knowledge/retrieval/status")return {ai_configured:aiReady,acceptance:{passed:acceptance}};
    if(url==="/api/knowledge/retrieve"){
      if(failAI)throw new Error("测试AI连接失败");
      if(timeoutAI)return await new Promise((resolve,reject)=>options.signal.addEventListener("abort",()=>reject(new Error("aborted"))));
      const body=JSON.parse(options.body);if(aiDelay)await aiDelay;if(failAIRule===body.rule_id)throw Object.assign(new Error("测试任务版本已变化，请重新读取"),{status:409});assert.equal(body.expected_revision,sample.revision);assert.equal(body.use_ai,true);assert.equal(body.region_code,expectedRegion);
      assert.equal(body.token_budget,undefined,"enterprise checks use the server's selected model capacity");
      const slot={revision:++sample.revision,bundle:{batching:{strategy:"category_and_source_v1",part_count:1,model_context:{context_window:1048576,input_budget:128000,output_reserve:8192}},task:{region_code:body.region_code},check:{rule_id:body.rule_id,topic:body.rule_id},facts:[],policy_evidence:[],policy_leads:[],gaps:[]},ai:{validated:true,status:"needs_review",batches:[{part_number:1,category_name:"申报资料",group:"本期申报",status:"completed"}],assessments:[{text:"证据待核实",required_materials:["原始申报表"]}]}};
      if(failedBatch){slot.ai.validated=false;slot.ai.reason="测试分类批次失败";slot.ai.batches[0].status="failed";}
      (sample.tax_checks??={})[body.rule_id]=slot;return structuredClone(slot);
    }
    if(options?.method==="DELETE"){if(failDelete)throw new Error("测试删除失败");return {id:"task",deleted:true};}
    if(failScope&&url.endsWith("/scope"))throw new Error("测试范围保存失败");
    if(fail&&url.endsWith("/prepare"))throw new Error("测试保存失败");return url==="/api/clients"?[{id:"existing",name:"历史企业",taxpayer_id:"91310000123456789A"}]:url==="/api/enterprise/input-tasks"&&!options?[]:structuredClone(sample);};
  let holdUploads=false;const uploadRequests=[];
  class UploadRequest {
    constructor(){this.upload={};uploadRequests.push(this);}
    open(method,url){this.method=method;this.url=url;}
    send(body){calls.push({url:this.url,options:{method:this.method,body}});if(!holdUploads)queueMicrotask(()=>this.succeed());}
    succeed(revision=sample.revision){this.upload.onload?.();this.status=200;this.responseText=JSON.stringify({...sample,revision});this.onload();}
    abort(){this.aborted=true;this.onabort?.();}
  }
  const pageEvents={};
  const context={document:{createElement:tag=>new Node(tag)},Blob,setTimeout:(fn,ms)=>timeoutAI&&ms===300000?(queueMicrotask(fn),0):setTimeout(fn,ms),clearTimeout,AbortController,console,FormData,XMLHttpRequest:UploadRequest,addEventListener:(event,fn)=>pageEvents[event]=fn};vm.createContext(context);vm.runInContext(fs.readFileSync("webapp/static/core-inputs.js","utf8"),context);
  const controller=context.createCoreInputs({api,el:(tag,cls,text)=>{const n=new Node(tag);n.className=cls||"";n.textContent=text||"";return n;},$,user:()=>actor,showError:msg=>errors.push(msg),continueCheck:id=>continued.push(id)});
  await controller.refresh();assert.equal($("coreClient").children.length,2);
  $("coreClient").value="existing";$("coreClient").onchange();assert.equal($("coreCompanyName").value,"历史企业");assert.equal($("coreTaxNo").value,"91310000123456789A");
  $("coreClient").value="";$("coreClient").onchange();assert.equal($("coreTaxNo").value,"");assert.equal($("coreTaxNo")["aria-invalid"],"false");
  const chooseKind=kind=>{$("coreRangeKind").value=kind;$("coreRangeKind").listeners.change();};
  chooseKind("month");$("coreRangeMonth").value="2028-02";$("coreRangeMonth").listeners.change?.();
  assert.equal($("coreStart").value,"2028-02-01");assert.equal($("coreEnd").value,"2028-02-29");
  assert.equal($("coreRangeMonthField").hidden,false);assert.equal($("coreRangeYearField").hidden,true);
  assert.equal($("coreStart").readOnly,true);
  chooseKind("quarter");$("coreRangeYear").value="2026";$("coreRangeQuarter").value="4";$("coreRangeQuarter").listeners.change?.();
  assert.equal($("coreStart").value,"2026-10-01");assert.equal($("coreEnd").value,"2026-12-31");
  assert.equal($("coreRangeMonthField").hidden,true);assert.equal($("coreRangeQuarterField").hidden,false);
  chooseKind("year");$("coreRangeYear").value="2027";$("coreRangeYear").listeners.change?.();
  assert.equal($("coreStart").value,"2027-01-01");assert.equal($("coreEnd").value,"2027-12-31");
  assert.equal($("coreRangeQuarterField").hidden,true);
  $("coreRangeYear").value="";$("coreRangeYear").listeners.change?.();
  assert.equal($("coreStart").value,"");assert.equal($("coreEnd").value,"");
  $("coreRangeKind").value="custom";$("coreRangeKind").listeners.change();
  assert.equal($("coreStart").readOnly,false);assert.equal($("coreRangeYearField").hidden,true);
  assert.equal(typeof $("coreDateRange").onclick,"function","custom range opens one shared calendar");
  const day=value=>all($("coreCalendarDays")).find(n=>n["data-date"]===value);
  const viewMonth=value=>{$("coreCalendarMonth").value=value;$("coreCalendarMonth").listeners.change();};
  $("coreDateRange").onclick();assert.equal($("coreRangeCalendar").hidden,false);
  viewMonth("2026-12");day("2026-12-28").onclick();
  assert.equal($("coreStart").value,""); // One click remains an uncommitted draft.
  viewMonth("2027-01");day("2027-01-04").onclick();
  assert.equal($("coreStart").value,"2026-12-28");assert.equal($("coreEnd").value,"2027-01-04");
  assert.equal($("coreRangeCalendar").hidden,false,"completed range stays open until dismissed");assert.equal($("coreDateRange")["aria-expanded"],"true");assert.match($("coreDateRangeText").textContent,/2026-12-28.*2027-01-04/);
  assert.equal(day("2027-01-04")["aria-pressed"],"true");
  viewMonth("2026-12");day("2026-12-20").onclick();$("coreCalendarCancel").onclick();
  assert.equal($("coreStart").value,"2026-12-28");assert.equal($("coreEnd").value,"2027-01-04");
  $("coreDateRange").onclick();viewMonth("2027-01");day("2027-01-20").onclick();day("2027-01-15").onclick();
  assert.equal($("coreStart").value,"2027-01-15");assert.equal($("coreEnd").value,"2027-01-20");
  $("coreCalendarPrev").onclick();assert.equal($("coreCalendarMonth").value,"2026-12");
  $("coreCalendarNext").onclick();assert.equal($("coreCalendarMonth").value,"2027-01");
  day("2027-01-15").onclick();day("2027-01-15").onclick();
  assert.equal($("coreStart").value,"2027-01-15");assert.equal($("coreEnd").value,"2027-01-15");
  assert.equal($("coreRangeCalendar").hidden,false);
  $("coreRangeCalendar").listeners.keydown({key:"Escape",preventDefault(){}});assert.equal($("coreRangeCalendar").hidden,true);
  $("coreDateRange").onclick();$("coreCalendarClear").onclick();
  assert.equal($("coreStart").value,"");assert.equal($("coreEnd").value,"");
  for(const [id,value] of [["coreCompanyName","企业"],["coreTaxNo","91310000123456789A"],["coreStart","2026-01-01"],["coreEnd","2026-01-31"]])$(id).value=value;
  await $("coreCreateTask").onclick();
  assert.equal($("coreRangeMonth").value,"2026-01");
  assert.match($("coreDateRangeText").textContent,/2026-01-01.*2026-01-31/);
  const find=text=>all($("coreTaskResult")).find(n=>n.textContent===text);
  const field=label=>all($("coreTaskResult")).find(n=>n["aria-label"]===label);
  assert.equal(field("进项.pdf 允许未识别PDF页交给配置模型OCR（发送页图）"),undefined,"OCR fallback does not require a per-file checkbox");
  assert.equal(field("进项.pdf 已对照原页核实提取候选的字段、金额、购销方和所属期").checked,false);
  assert.equal(field("进项.pdf 确认发票方向"),undefined,"invoice direction is automatic, with no file-wide selector");
  assert.equal(find("下载标准数据 JSON"),undefined);
  assert.equal(all($("coreTaskResult")).some(n=>n.tag==="pre"),false,"internal record JSON is not rendered");
  sample.versions=[{revision:1,kind:"analysis",created_at:"2026-10-17T10:50:18+00:00"},{revision:2,kind:"edit",created_at:"2026-10-17T13:43:30+00:00"}];
  await controller.openTask("task");
  assert.equal(find("版本与操作记录"),undefined,"upload workflow does not display administrative history");
  assert.ok(!all($("coreTaskResult")).some(n=>/版本 \d|analysis|2026-10-17T10:50/.test(n.textContent||"")),"internal versions and raw operations are absent even for an old response");
  assert.equal(find("采用此期间"),undefined);assert.ok(all($("corePeriodCandidates")).find(n=>n.textContent==="查看期间候选"));assert.equal($("coreStart").value,"2026-01-01");assert.equal($("coreEnd").value,"2026-01-31");
  for(const [kind,label] of [["account_balance","科目余额表"],["vat_declaration","增值税申报表"],["invoice_detail","发票明细"]]){
    const pick=field(label+"上传");pick.files=[new Blob(["synthetic material"])];await pick.onchange();
    const upload=calls.findLast(c=>c.url.endsWith("/files")).options.body;
    assert.equal(upload.get("material_type"),kind);assert.equal(upload.get("mapping_mode"),"ai");
    assert.ok(pick.accept.split(",").includes(".pdf"));assert.equal(upload.get("ocr"),"1");
    assert.equal(field(label+"文件传输进度").value,100);
  }
  // Real transfer events drive the percentage; parsing never pretends to have a percentage.
  holdUploads=true;
  const uploadPick=field("科目余额表上传");uploadPick.files=[new Blob(["progress fixture"])];
  const uploading=uploadPick.onchange(),request=uploadRequests.at(-1),progress=field("科目余额表文件传输进度");
  request.upload.onprogress({lengthComputable:true,loaded:42,total:100});assert.equal(progress.value,42);
  request.upload.onload();assert.equal(Object.hasOwn(progress,"value"),false);assert.ok(find("文件已传输，正在保存和解析，请稍候……"));
  assert.equal(uploadPick.disabled,false,"file selection stays available during parsing");
  assert.equal(field("增值税申报表上传").disabled,false);assert.equal($("coreTaskForm").disabled,true);
  const requestCount=uploadRequests.length,queuedPick=field("增值税申报表上传");queuedPick.files=[new Blob(["next batch"])];
  const queued=queuedPick.onchange();uploadPick.files=[new Blob(["another balance"])];const sameKind=uploadPick.onchange();
  assert.equal(uploadRequests.length,requestCount,"new selections wait without concurrent revision writes");
  assert.ok(find("等待上传 1 份（尚未发送）"));
  const unloadEvent={preventDefault(){this.prevented=true;}};pageEvents.beforeunload(unloadEvent);assert.equal(unloadEvent.prevented,true);
  request.succeed(2);await uploading;
  const secondRequest=uploadRequests.at(-1);assert.notEqual(secondRequest,request);
  assert.equal(calls.at(-1).options.body.get("expected_revision"),"2");assert.equal(calls.at(-1).options.body.get("material_type"),"vat_declaration");
  secondRequest.succeed(3);await queued;
  assert.equal(calls.at(-1).options.body.get("expected_revision"),"3");assert.equal(calls.at(-1).options.body.get("material_type"),"account_balance");
  uploadRequests.at(-1).succeed(4);await sameKind;
  assert.equal(field("科目余额表文件传输进度").value,100);assert.equal(field("科目余额表上传").disabled,false);
  const completedUnload={preventDefault(){this.prevented=true;}};pageEvents.beforeunload(completedUnload);assert.equal(completedUnload.prevented,undefined);
  const failedPick=field("增值税申报表上传");failedPick.files=[new Blob(["failed fixture"])];
  const failing=failedPick.onchange(),failingRequest=uploadRequests.at(-1),waitingPick=field("发票明细上传");waitingPick.files=[new Blob(["not sent yet"])];
  const waiting=waitingPick.onchange(),beforeFailure=uploadRequests.length;failingRequest.onerror();await failing;
  assert.match(errors.at(-1),/结果未确认/);assert.equal(failedPick.disabled,false);assert.equal(failedPick.value,"");
  assert.equal(uploadRequests.length,beforeFailure);assert.ok(find("已暂停，等待上传 1 份（尚未发送）"));
  const beforeSwitch=calls.length;await controller.openTask("other-task");assert.equal(calls.length,beforeSwitch,"waiting files cannot follow a task switch");
  const resuming=find("读取任务并继续上传").onclick();await new Promise(setImmediate);
  assert.equal(uploadRequests.length,beforeFailure+1,"resume sends only the never-sent batch");assert.equal(calls.at(-1).options.body.get("expected_revision"),"1","resume refreshes the authoritative task revision");
  uploadRequests.at(-1).succeed();await waiting;await resuming;
  const retryPick=field("增值税申报表上传");retryPick.files=[new Blob(["retry fixture"])];
  const retry=retryPick.onchange(),httpFailure=uploadRequests.at(-1);httpFailure.status=422;httpFailure.responseText=JSON.stringify({detail:"文件格式不支持"});httpFailure.onload();await retry;
  assert.equal(errors.at(-1),"文件格式不支持");assert.equal(retryPick.disabled,false);
  // Removing waiting files never cancels or resends the active batch.
  const keepPick=field("科目余额表上传");keepPick.files=[new Blob(["keep active"])];const keepActive=keepPick.onchange(),keepRequest=uploadRequests.at(-1);
  const removePick=field("发票明细上传");removePick.files=[new Blob(["remove waiting"])];const removed=removePick.onchange();
  const queueBox=all($("coreTaskResult")).find(n=>n.className==="core-upload-status"&&all(n).includes(field("发票明细文件传输进度")));
  all(queueBox).find(n=>n.textContent==="清除待上传").onclick();assert.equal(await removed,false);assert.equal(keepRequest.aborted,undefined);
  keepRequest.succeed();await keepActive;
  const limitPick=field("科目余额表上传"),beforeLimit=uploadRequests.length;limitPick.files=Array.from({length:20},()=>new Blob(["limit"]));await limitPick.onchange();assert.match(errors.at(-1),/合计不能超过 20/);
  limitPick.files=[{size:10*1024*1024+1}];await limitPick.onchange();assert.match(errors.at(-1),/10MB/);assert.equal(uploadRequests.length,beforeLimit);
  const cancelPick=field("发票明细上传");cancelPick.files=[new Blob(["cancel fixture"])];
  const cancelled=cancelPick.onchange(),cancelRequest=uploadRequests.at(-1);const stalePick=field("增值税申报表上传");stalePick.files=[new Blob(["stale queued fixture"])];const staleQueued=stalePick.onchange();
  controller.reset();await cancelled;assert.equal(await staleQueued,false);
  assert.equal(cancelRequest.aborted,true);assert.equal($("coreTaskResult").children.length,0,"session reset discards stale progress");
  cancelRequest.upload.onprogress({lengthComputable:true,loaded:100,total:100});assert.equal($("coreTaskResult").children.length,0);
  holdUploads=false;await controller.openTask("task");
  const review=find("资料口径核对");const editor=all($("coreTaskResult")).find(n=>n.tag==="fieldset");assert.ok(review);
  const forms=field("本期适用附表名称（以分号分隔）");forms.value="附表一";editor.listeners.input();

  const before=calls.length;await find("重新读取任务").onclick();assert.equal(calls.length,before);assert.match(errors.at(-1),/未保存/);assert.equal(forms.value,"附表一");
  find("开始检查").onclick();assert.equal(continued.length,0);assert.match(errors.at(-1),/先保存/);
  field("已核实本期适用附表清单（如无适用附表，也须明确确认）").checked=true;
  fail=true;await find("保存核对并重新整理资料").onclick();assert.match(errors.at(-1),/保存失败/);assert.equal(forms.value,"附表一");assert.equal(forms.disabled,false);
  fail=false;await find("保存核对并重新整理资料").onclick();const write=calls.findLast(c=>c.url.endsWith("/prepare"));assert.deepEqual(JSON.parse(write.options.body).applicable_forms,["附表一"]);
  assert.equal(JSON.parse(write.options.body).mapping_mode,"ai");
  assert.equal(JSON.parse(write.options.body).source_options["review-pdf"].ocr,true,"review save keeps PDF OCR fallback enabled for older tasks");
  assert.equal(JSON.parse(write.options.body).source_options["review-pdf"].source_reviewed,false,"automatic OCR does not confirm source review");
  for(const options of Object.values(JSON.parse(write.options.body).source_options))assert.equal(Object.hasOwn(options.confirmed_metadata,"direction"),false,"review save does not resend old file-wide directions");
  await find("恢复资料：进项.xlsx").onclick();const restored=calls.findLast(c=>c.url.endsWith("/prepare"));assert.deepEqual(JSON.parse(restored.options.body).source_options,{"excluded-file":{excluded:false,ocr:true}});
  assert.equal(JSON.parse(restored.options.body).mapping_mode,"ai");
  $("coreTaxNo").value="bad";$("coreTaxNo").listeners.input();assert.equal($("coreTaxNo")["aria-invalid"],"true");
  const beforeScope=calls.length;await find("开始检查").onclick();assert.equal(calls.length,beforeScope);assert.equal(continued.length,0);
  $("coreTaxNo").value="91310000123456789A";$("coreTaxNo").listeners.input();assert.equal($("coreTaxNo")["aria-invalid"],"false");
  sample.scope_identity_candidates={company_name:[{value:"企业",sources:[{file_id:"review-sheet",file_name:"余额.xlsx",position:"Sheet1"}]}],tax_no:[{value:"91310000123456789A",sources:[{file_id:"review-sheet",file_name:"余额.xlsx",position:"Sheet1",needs_review:true}]}]};
  await find("保存企业与检查区间").onclick();
  $("coreCompanyName").value="1111";$("coreCompanyName").listeners.input();
  $("coreTaxNo").value="12312321312";$("coreTaxNo").listeners.input();
  assert.equal($("coreCompanyName")["aria-invalid"],"true");assert.equal($("coreTaxNo")["aria-invalid"],"true");
  const blockedIdentity=calls.length;await find("开始检查").onclick();assert.equal(calls.length,blockedIdentity);assert.equal(continued.length,0);
  const adoptAll=all($("coreIdentityActions")).find(n=>n.textContent==="一键采用文件信息");assert.ok(adoptAll,"identity candidates offer an explicit apply action");
  const beforeAdopt=calls.length,savedDates=[$("coreStart").value,$("coreEnd").value],cancelAdopt=adoptAll.onclick();
  assert.equal($("coreIdentityDialog").open,true,"overwriting filled fields requires an in-page confirmation");
  assert.equal($("coreCompanyName").value,"1111");assert.equal($("coreTaxNo").value,"12312321312");
  const conflictText=all($("coreIdentityChanges")).map(n=>n.textContent).join(" ");assert.match(conflictText,/1111/);assert.match(conflictText,/12312321312/);assert.match(conflictText,/余额.xlsx/);assert.equal(all($("coreCompanyNameCandidates")).some(n=>n.textContent==="企业"),false,"candidate values stay inside the dialog");
  $("coreIdentityCancel").onclick();await cancelAdopt;assert.equal($("coreCompanyName").value,"1111");assert.equal(calls.length,beforeAdopt);
  const escapeAdopt=adoptAll.onclick();$("coreIdentityDialog").close();await escapeAdopt;assert.equal($("coreCompanyName").value,"1111","Escape dismisses without overwriting");
  const changedFormAdopt=adoptAll.onclick();$("coreTaxNo").value="99999999999";$("coreIdentityConfirm").onclick();await changedFormAdopt;
  assert.equal($("coreCompanyName").value,"1111");assert.equal($("coreTaxNo").value,"99999999999");assert.match(errors.at(-1),/表单已变化/);$("coreTaxNo").value="12312321312";
  const resetAdopt=adoptAll.onclick();controller.reset();await resetAdopt;assert.equal($("coreIdentityDialog").open,false);assert.equal($("coreCompanyName").value,"1111","reset cancels a pending overwrite");
  await controller.refresh();await controller.openTask("task");$("coreCompanyName").value="1111";$("coreTaxNo").value="12312321312";
  const freshAdopt=all($("coreIdentityActions")).find(n=>n.textContent==="一键采用文件信息");
  const beforeApproved=calls.length,approvedAdopt=freshAdopt.onclick();$("coreIdentityConfirm").onclick();await approvedAdopt;
  assert.equal($("coreCompanyName").value,"企业");assert.equal($("coreTaxNo").value,"91310000123456789A");
  assert.deepEqual([$("coreStart").value,$("coreEnd").value],savedDates,"identity adoption preserves the chosen period");
  assert.equal(calls.length,beforeApproved+1,"adoption performs a read-only validation without saving or starting AI");
  assert.ok(calls.at(-1).url.endsWith("/identity-validation"));
  assert.equal($("coreCompanyName")["aria-invalid"],"false");assert.equal($("coreTaxNo")["aria-invalid"],"false");
  assert.equal(sample.source_options["review-pdf"].source_reviewed,false,"adopting metadata does not attest source review");
  $("coreCompanyName").value=$("coreTaxNo").value="";const blankAdopt=freshAdopt.onclick();
  assert.equal($("coreIdentityDialog").open,true,"explicit adoption is reviewed in the dialog even for empty fields");$("coreIdentityConfirm").onclick();await blankAdopt;assert.equal($("coreCompanyName").value,"企业");
  $("coreCompanyName").value="企 业";$("coreCompanyName").listeners.input();
  $("coreTaxNo").value="91310000123456789a";$("coreTaxNo").listeners.input();
  assert.equal($("coreCompanyName")["aria-invalid"],"false");assert.equal($("coreTaxNo")["aria-invalid"],"false");
  $("coreRegion").value="";expectedRegion="CN";await find("开始检查").onclick();
  assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,2,"no region defaults to national checks");
  assert.ok(all($("coreCheckResult")).some(n=>n.textContent==="检查结果"));
  assert.equal($("coreRegion").value,"","default national scope does not invent an enterprise province");
  expectedRegion="CN-33";
  $("coreRegion").value="CN-33";$("coreRegion").listeners.change();assert.equal($("coreRegion")["aria-invalid"],"false");
  failScope=true;await find("开始检查").onclick();assert.equal(continued.length,0);assert.match(errors.at(-1),/范围保存失败/);
  failScope=false;await find("开始检查").onclick();assert.deepEqual(continued,[],"start check must not navigate to the graph");
  assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,4);
  assert.ok(all($("coreCheckResult")).some(n=>n.textContent==="检查结果"));
  assert.ok(all($("coreCheckResult")).some(n=>n.textContent==="需补充或核实：原始申报表"));
  assert.equal($("coreTaskForm").disabled,false);
  assert.ok(all($("coreCheckResult")).some(n=>n.textContent==="分析批次：已完成 1/1 批"));
  assert.ok(all($("coreCheckResult")).some(n=>n.textContent?.includes("申报资料 · 本期申报 · 已完成")));
  assert.equal(calls.filter(c=>c.url.endsWith("/reports")&&c.options?.method==="POST").length,2,"each completed check run freezes its report without navigating away");
  assert.ok(all($("coreCheckResult")).some(n=>n.tag==="a"&&n.textContent==="查看报告"&&n.href.endsWith("/reports/2?format=html")));
  assert.ok(all($("coreCheckResult")).some(n=>n.tag==="a"&&n.textContent==="下载 PDF"&&n.href.endsWith("/reports/2?format=pdf")));
  const reportedRevision=sample.revision;sample.revision++;await controller.openTask("task");await Promise.resolve();
  assert.ok(all($("coreCheckResult")).some(n=>n.tag==="a"&&n.textContent==="查看上次报告"),"an older report must not be presented as current");
  sample.revision=reportedRevision;await controller.openTask("task");
  const savedChecks=structuredClone(sample.tax_checks),noRequests=calls.filter(c=>c.url==="/api/knowledge/retrieve").length;
  const oldSlot=sample.tax_checks["R-001"],oldPack=oldSlot.bundle;
  oldPack.check.topic="收入核对";oldPack.batching={unsplittable:true};oldSlot.ai.reason="完整必要上下文超出预算，保留原文并转人工复核。";
  oldPack.facts=[{metric_id:"revenue",name:"账面收入",state:"unknown",source_ids:[]},{metric_id:"vat_sales",name:"增值税销售额",state:"unknown",source_ids:[]}];
  oldPack.policy_leads=[{clause_id:"c1",title:"政策甲",source_location:"第二条"},{clause_id:"c2",title:"政策乙",source_location:"第三条"}];
  oldPack.gaps=[{code:"metric_missing",metric_id:"revenue",message:"指标值缺失或类型无效。"},{code:"metric_missing",metric_id:"vat_sales",message:"指标值缺失或类型无效。"},
    ...Array.from({length:40},(_,i)=>({code:"policy_review",clause_id:i%2?"c1":"c2",message:"政策效力尚未复核。"})),
    {code:"budget_unsplittable",message:"单个完整条款或必要上下文超出预算，保留原文并转复核。"},
    {code:"unrecognized_issue",message:"保留未识别事项。"}];
  await controller.openTask("task");
  const reviewNodes=()=>all($("coreCheckResult"));
  assert.ok(reviewNodes().some(n=>n.textContent==="收入核对 · 旧版结果，需重新检查"));
  assert.ok(reviewNodes().some(n=>n.textContent==="指标与材料：1 类问题 · 2 项"));
  assert.ok(reviewNodes().some(n=>n.textContent==="政策与规则：1 类问题 · 2 项"));
  assert.equal(reviewNodes().filter(n=>n.textContent==="政策效力尚未复核。").length,1,"equal policy issues are grouped without losing distinct clauses");
  assert.ok(reviewNodes().some(n=>n.tag==="li"&&n.textContent==="账面收入"));
  assert.ok(reviewNodes().some(n=>n.tag==="li"&&n.textContent==="政策甲 · 第二条 · c1"));
  assert.ok(reviewNodes().some(n=>n.textContent==="其他核实事项：1 类问题 · 1 项"));
  const archive=reviewNodes().find(n=>n.tag==="details"&&n.children[0]?.textContent==="旧版处理记录（仅供追溯）");
  assert.ok(archive&&!archive.open);assert.ok(all(archive).some(n=>n.textContent===oldSlot.ai.reason));
  for(const n of reviewNodes().filter(n=>n.tag==="details"&&/类问题/.test(n.children[0]?.textContent||"")))assert.ok(!n.open,"gap categories start collapsed");
  const incomeCard=()=>reviewNodes().find(n=>n.tag==="details"&&n.children[0]?.textContent==="收入核对 · 旧版结果，需重新检查");
  const metricGroup=()=>all(incomeCard()).find(n=>n.tag==="details"&&n.children[0]?.textContent==="指标与材料：1 类问题 · 2 项");
  const toggleDetails=details=>{let prevented=false;details.children[0].onclick({preventDefault(){prevented=true;}});assert.ok(prevented,"avoid toggling twice via the browser default action");assert.equal(details.children[0]["aria-expanded"],String(details.open));assert.equal(details.children[0].children[0].textContent,details.open?"收起":"展开");};
  for(let i=0;i<3;i++){toggleDetails(incomeCard());assert.equal(incomeCard().open,true);toggleDetails(incomeCard());assert.equal(incomeCard().open,false);}
  toggleDetails(incomeCard());toggleDetails(metricGroup());assert.equal(incomeCard().open,true,"nested toggles leave the outer item open");
  await controller.openTask("task");assert.equal(incomeCard().open,true,"rendering saved results preserves the outer fold state");assert.equal(metricGroup().open,true,"nested fold state survives a result refresh");
  toggleDetails(metricGroup());assert.equal(incomeCard().open,true);
  incomeCard().children.find(n=>n.tag==="button"&&n.textContent==="收起本项").onclick();assert.equal(incomeCard().open,false);
  await controller.openTask("task");assert.equal(incomeCard().open,false,"a closed item stays closed after refresh");
  toggleDetails(incomeCard());sample.id="other-fold-task";await controller.openTask(sample.id);assert.equal(incomeCard().open,false,"fold state is isolated by task");
  sample.id="task";await controller.openTask("task");assert.equal(incomeCard().open,false);
  oldPack.batching=structuredClone(savedChecks["R-001"].bundle.batching);oldSlot.ai.reason="当前结构化记录仍超出容量。";
  await controller.openTask("task");
  assert.ok(!reviewNodes().some(n=>n.textContent==="收入核对 · 旧版结果，需重新检查"));
  assert.ok(reviewNodes().some(n=>n.textContent==="系统处理：1 类问题 · 1 项"),"current budget failures remain visible");
  assert.ok(reviewNodes().some(n=>n.textContent==="当前结构化记录仍超出容量。"));
  sample.tax_checks=savedChecks;await controller.openTask("task");
  assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,noRequests,"viewing and grouping saved results never triggers AI or rewrites history");
  sample.tax_checks=Object.fromEntries(Array.from({length:25},(_,i)=>{const slot=structuredClone(savedChecks["R-001"]);slot.bundle.check={rule_id:`page-rule-${i+1}`,topic:`分页检查${i+1}`};return [slot.bundle.check.rule_id,slot];}));
  const checkCards=()=>$("coreCheckResult").children.filter(n=>n.tag==="details"&&n.className.includes("material-card"));
  const checkPager=()=>$("coreCheckResult").children.find(n=>n.tag==="nav"),pageButton=label=>checkPager().children.find(n=>n.tag==="button"&&n.textContent===label);
  await controller.openTask("task");assert.equal(checkCards().length,5);assert.match(checkCards()[0].children[0].textContent,/分页检查1 ·/);
  assert.equal(pageButton("上一页").disabled,true);assert.equal(pageButton("下一页").disabled,false);
  assert.ok(all(checkPager()).some(n=>n.textContent==="共 25 项 · 第 1/5 页 · 1–5 项"));
  toggleDetails(checkCards()[0]);pageButton("下一页").onclick();assert.match(checkCards()[0].children[0].textContent,/分页检查6 ·/);
  pageButton("上一页").onclick();assert.equal(checkCards()[0].open,true,"expansion state survives page changes");
  for(let i=0;i<4;i++)pageButton("下一页").onclick();assert.equal(checkCards().length,5);assert.match(checkCards()[0].children[0].textContent,/分页检查21 ·/);assert.equal(pageButton("下一页").disabled,true);
  await controller.openTask("task");assert.match(checkCards()[0].children[0].textContent,/分页检查21 ·/,"refresh preserves the selected page");
  sample.id="another-page-task";await controller.openTask(sample.id);assert.match(checkCards()[0].children[0].textContent,/分页检查1 ·/,"changing task resets the page");
  sample.id="task";await controller.openTask("task");for(let i=0;i<4;i++)pageButton("下一页").onclick();
  sample.tax_checks=savedChecks;await controller.openTask("task");assert.equal(checkCards().length,2);assert.equal(checkPager(),undefined,"shrinking results clamps the page and hides unnecessary controls");
  assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,noRequests,"pagination is local and does not run AI");
  reportFailure=true;await find("开始检查").onclick();
  assert.ok(reviewNodes().some(n=>n.textContent?.includes("报告生成失败：测试报告生成失败")),"a report failure preserves the completed checks and has a local retry");
  const beforeReportRetry=calls.filter(c=>c.url==="/api/knowledge/retrieve").length;reportFailure=false;
  await reviewNodes().find(n=>n.tag==="button"&&n.textContent==="更新检查报告").onclick();
  assert.ok(reviewNodes().some(n=>n.tag==="a"&&n.textContent==="查看报告"));
  assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,beforeReportRetry,"retrying report generation does not run AI again");
  sample.id="report-load-error";reportListFailure=true;await controller.openTask(sample.id);await Promise.resolve();
  assert.ok(reviewNodes().some(n=>n.textContent==="报告读取失败：测试报告读取失败"));
  reportListFailure=false;reviewNodes().find(n=>n.textContent==="重试读取报告").onclick();await Promise.resolve();
  assert.ok(!reviewNodes().some(n=>n.textContent==="报告读取失败：测试报告读取失败"));
  let releaseReports;reportHold=new Promise(resolve=>releaseReports=resolve);sample.id="pending-report-task";await controller.openTask(sample.id);
  reportHold=null;sample.id="task";await controller.openTask("task");releaseReports([{version:999,input_revision:sample.revision}]);await Promise.resolve();
  assert.ok(!reviewNodes().some(n=>n.tag==="a"&&n.href.includes("/reports/999?")),"late report lists cannot cross task boundaries");
  failedBatch=true;const beforeFailedBatch=calls.filter(c=>c.url==="/api/knowledge/retrieve").length;await find("开始检查").onclick();
  assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,beforeFailedBatch+1,"stop before sending other rules after a batch request failed");
  assert.match(errors.at(-1),/分类批次失败/);failedBatch=false;
  const aiCount=calls.filter(c=>c.url==="/api/knowledge/retrieve").length;
  aiReady=false;await find("开始检查").onclick();assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,aiCount);assert.match(errors.at(-1),/尚未配置/);assert.equal($("coreTaskForm").disabled,false);
  aiReady=true;acceptance=false;await find("开始检查").onclick();assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,aiCount);assert.match(errors.at(-1),/检索校验/);
  acceptance=true;failAI=true;await find("开始检查").onclick();assert.match(errors.at(-1),/AI连接失败/);assert.ok(all($("coreCheckResult")).some(n=>n.textContent==="检查未完成"));assert.equal($("coreTaskForm").disabled,false);
  const originalRules=checkRules;checkRules=Array.from({length:1233},(_,i)=>({rule_id:`rule-${i}`,name:`尚未完成的检查第${i}项`}));
  await find("开始检查").onclick();
  const pendingList=all($("coreCheckResult")).find(n=>n.tag==="details"&&n.children[0]?.textContent==="尚未完成 1233 项（展开查看）");
  assert.ok(pendingList);assert.ok(!pendingList.open);assert.equal(all(pendingList).filter(n=>n.tag==="li").length,1233);
  assert.ok(!all($("coreCheckResult")).some(n=>n.tag==="p"&&n.textContent?.includes("尚未完成的检查第0项")));
  checkRules=originalRules;
  failAI=false;timeoutAI=true;await find("开始检查").onclick();assert.match(errors.at(-1),/等待超时/);assert.equal($("coreTaskForm").disabled,false);timeoutAI=false;
  // A slow history list must never keep a completed task locked.
  let finishHistory;holdHistory=new Promise(r=>finishHistory=r);await find("重新读取任务").onclick();assert.equal($("coreTaskForm").disabled,false);holdHistory=null;finishHistory([]);await Promise.resolve();
  assert.equal(JSON.parse(calls.findLast(c=>c.url.endsWith("/scope")).options.body).confirm,true);
  assert.equal(JSON.parse(calls.findLast(c=>c.url.endsWith("/scope")).options.body).range_kind,"month","starting checks preserves the selected monthly range");
  const identitySnapshot=structuredClone(sample.scope_identity_candidates);
  sample.scope_identity_candidates.company_name.push({value:"另一企业",sources:[{file_id:"other",file_name:"另一来源.pdf",position:"PDF第2页"}]});
  sample.scope_identity_candidates.tax_no=[{value:"91310000123456789A / 91310000987654321B",alternatives:["91310000123456789A","91310000987654321B"],sources:[{file_id:"invoice",file_name:"发票.pdf",position:"PDF第1页",needs_review:true}]}];
  await find("重新读取任务").onclick();
  assert.equal($("coreIdentityActions").hidden,true,"conflicting names and ambiguous invoice parties never offer bulk adoption");
  const nameLink=all($("coreCompanyNameCandidates")).find(n=>n.textContent==="查看识别结果"||n.textContent==="查看差异");assert.ok(nameLink);
  const beforeSingle=calls.length,otherTax=$("coreTaxNo").value,singleAdopt=nameLink.onclick();
  assert.equal($("coreCompanyName").value,"企业");assert.equal($("coreIdentityConfirm").disabled,true,"multiple distinct names require a selection");
  const nameRadio=all($("coreIdentityChanges")).find(n=>n["aria-label"]==="企业名称采用另一企业");nameRadio.checked=true;nameRadio.listeners.change();
  await $("coreIdentityConfirm").onclick();assert.equal($("coreIdentityDialog").open,true,"unresolved source conflicts block adoption before confirmation");
  assert.match($("coreIdentityValidation").textContent,/材料识别结果不同/);$("coreIdentityCancel").onclick();await singleAdopt;
  assert.equal($("coreCompanyName").value,"企业");assert.equal($("coreTaxNo").value,otherTax);assert.equal(calls.length,beforeSingle);
  $("coreClient").value="existing";
  const taxLink=all($("coreTaxNoCandidates")).find(n=>n.textContent==="查看识别结果"||n.textContent==="查看差异");assert.ok(taxLink);
  const taxAdopt=taxLink.onclick();
  const taxRadios=all($("coreIdentityChanges")).filter(n=>n.tag==="input");assert.equal(taxRadios.length,2);taxRadios[1].checked=true;taxRadios[1].listeners.change();
  $("coreIdentityConfirm").onclick();await taxAdopt;
  assert.equal($("coreTaxNo").value,"91310000987654321B");assert.equal($("coreClient").value,"","a differing tax candidate clears the selected history binding");
  const staleAdoption=taxLink.onclick;controller.reset();staleAdoption();assert.equal($("coreTaxNo").value,"91310000987654321B","a detached candidate cannot change a reset form");
  assert.equal($("coreIdentityActions").hidden,true);assert.equal($("coreCompanyNameCandidates").hidden,true);
  sample.scope_identity_candidates=identitySnapshot;await controller.openTask("task");
  let validateLink=all($("coreTaxNoCandidates")).find(n=>n.tag!=="div"&&typeof n.onclick==="function");
  identityFailure={valid:false,errors:{tax_no:"服务端发现材料税号冲突，请重新读取。"}};
  let validationAttempt=validateLink.onclick();await $("coreIdentityConfirm").onclick();
  assert.equal($("coreIdentityDialog").open,true);assert.match($("coreIdentityValidation").textContent,/服务端发现/);
  $("coreIdentityCancel").onclick();await validationAttempt;
  identityFailure=new Error("校验服务暂不可用");validationAttempt=validateLink.onclick();await $("coreIdentityConfirm").onclick();
  assert.equal($("coreIdentityDialog").open,true);assert.match($("coreIdentityValidation").textContent,/暂不可用/);
  $("coreIdentityCancel").onclick();await validationAttempt;identityFailure=null;
  const savedIdentityCandidates=structuredClone(sample.scope_identity_candidates);
  sample.scope_identity_candidates.tax_no=[{value:"bad!",sources:[]}];await controller.openTask("task");
  validateLink=all($("coreTaxNoCandidates")).find(n=>typeof n.onclick==="function");
  validationAttempt=validateLink.onclick();const invalidCalls=calls.length;await $("coreIdentityConfirm").onclick();
  assert.equal($("coreIdentityConfirm").disabled,true);assert.match($("coreIdentityValidation").textContent,/8至20/);assert.equal(calls.length,invalidCalls);
  $("coreIdentityCancel").onclick();await validationAttempt;
  sample.scope_identity_candidates=savedIdentityCandidates;await controller.openTask("task");
  validateLink=all($("coreTaxNoCandidates")).find(n=>typeof n.onclick==="function");
  validationAttempt=validateLink.onclick();let finishValidation;delay=new Promise(resolve=>finishValidation=resolve);
  const pendingValidation=$("coreIdentityConfirm").onclick();const once=calls.length;await $("coreIdentityConfirm").onclick();assert.equal(calls.length,once,"double confirmation validates once");
  $("coreIdentityCancel").onclick();await validationAttempt;finishValidation({valid:true,errors:{}});await pendingValidation;delay=null;
  assert.equal($("coreIdentityDialog").open,false,"late validation cannot apply a canceled choice");
  const savedPeriodCandidates=structuredClone(sample.scope_period_candidates);
  sample.scope_period_candidates.push({start_date:"2026-03-01",end_date:"2026-03-31",sources:[{file_id:"march",position:"主表"}]});
  await controller.openTask("task");
  const periodBefore=[$("coreStart").value,$("coreEnd").value];
  const bulk=()=>all($("coreIdentityActions")).find(n=>n.textContent==="一键采用文件信息").onclick();
  let periodAdopt=bulk();assert.equal($("coreIdentityConfirm").disabled,true,"multiple periods require explicit selection");
  const march=()=>all($("coreIdentityChanges")).find(n=>n["aria-label"]==="检查区间采用2026-03-01 至 2026-03-31");
  march().checked=true;march().listeners.change();assert.equal($("coreIdentityConfirm").disabled,false);
  $("coreIdentityCancel").onclick();await periodAdopt;
  assert.deepEqual([$("coreStart").value,$("coreEnd").value],periodBefore,"cancel leaves the selected period intact");
  periodAdopt=bulk();march().checked=true;march().listeners.change();await $("coreIdentityConfirm").onclick();await periodAdopt;
  assert.deepEqual([$("coreStart").value,$("coreEnd").value],["2026-03-01","2026-03-31"]);
  assert.equal($("coreRangeKind").value,"custom");assert.equal($("coreRangeMonth").value,"2026-03");
  assert.equal($("coreDateRangeText").textContent,"2026-03-01 至 2026-03-31");
  const periodRequest=JSON.parse(calls.at(-1).options.body);
  assert.equal(periodRequest.start_date,"2026-03-01");assert.equal(periodRequest.end_date,"2026-03-31");
  assert.deepEqual(periodRequest.fields,["company_name","tax_no","start_date","end_date"]);
  controller.reset();sample.scope_period_candidates=savedPeriodCandidates;await controller.openTask("task");
  periodAdopt=all($("corePeriodCandidates")).find(n=>typeof n.onclick==="function").onclick();
  assert.equal(all($("coreIdentityChanges")).filter(n=>n.tag==="input").length,1,"period-only entry uses the same confirmation dialog");
  $("coreIdentityCancel").onclick();await periodAdopt;
  // Parsed candidates remain visible before the enterprise/check period is complete.
  sample.scope_errors={start_date:"请填写检查起始日"};
  sample.documents=[{id:"pdf",name:"销项发票_每页一张.pdf",material_type:"invoice_detail",sha256:"synthetic",pdf_candidate:true,parts:[],records:Array.from({length:12},(_,i)=>({id:"r"+i,invoice_no:"262609010000000000"+String(i).padStart(2,"0"),issue_date:"2026-01-15",source_position:`PDF第${i+1}页/发票票面`,amount_excluding_tax:i?"100.00":"12345678901234567890.0123",tax_amount:"13.00",seller_tax_no:sample.task.tax_no,buyer_tax_no:"OTHER"}))}];
  sample.documents[0].records[11].parsed_invoice_statuses={red_letter_status:"蓝字",void_status:"正常"};
  sample.source_options.pdf={source_reviewed:true,ocr:true,invoice_statuses:{r11:{red_letter_status:"",void_status:""}}};
  await find("重新读取任务").onclick();
  assert.ok(find("已解析候选 12 条"));assert.ok(find("资料缺口与冲突（待核对检查范围）"));
  assert.equal(all($("coreTaskResult")).some(n=>n.tag==="pre"),false);
  assert.equal(field("本期适用附表名称（以分号分隔）").tag,"input");
  assert.ok(all($("coreTaskResult")).some(n=>n.className==="field"));
  assert.ok(find("12,345,678,901,234,567,890.0123"));
  const documentCard=()=>all($("coreTaskResult")).find(n=>n.className==="core-document");
  assert.equal(documentCard().open,false,"material files are collapsed by default, including the first file");
  documentCard().children[0].onclick({preventDefault(){}});assert.equal(documentCard().open,true);
  await find("重新读取任务").onclick();assert.equal(documentCard().open,true,"reloading preserves a manually expanded file");
  documentCard().children[0].onclick({preventDefault(){}});assert.equal(documentCard().open,false);
  await find("重新读取任务").onclick();assert.equal(documentCard().open,false,"reloading must not reopen a manually collapsed file");
  const rows=()=>all($("coreTaskResult")).filter(n=>n.tag==="tbody").flatMap(n=>n.children);
  assert.equal(rows().length,10);assert.ok(find("26260901000000000000"));assert.equal(find("上一页").disabled,true);
  const firstLabel="销项发票_每页一张.pdf · PDF第1页/发票票面 · 26260901000000000000 红蓝字状态";
  field(firstLabel).value="红字";field(firstLabel).listeners.change();
  const firstDirection=firstLabel.replace(" 红蓝字状态"," 购销方向");
  assert.equal(field(firstDirection).value,"");
  field(firstDirection).value="input";field(firstDirection).listeners.change();
  assert.equal(field("销项发票_每页一张.pdf 已对照原页核实提取候选的字段、金额、购销方和所属期").checked,false);
  assert.ok(find("发票明细 · 12 条候选 · 待核对"));
  find("下一页").onclick();assert.equal(rows().length,2);assert.ok(find("26260901000000000011"));assert.equal(find("下一页").disabled,true);
  const lastDirection="销项发票_每页一张.pdf · PDF第12页/发票票面 · 26260901000000000011 购销方向";
  field(lastDirection).value="output";field(lastDirection).listeners.change();
  find("上一页").onclick();assert.equal(field(firstLabel).value,"红字");
  assert.equal(field(firstDirection).value,"input","paging retains per-invoice direction edits");
  await find("保存核对并重新整理资料").onclick();
  const statuses=JSON.parse(calls.findLast(c=>c.url.endsWith("/prepare")).options.body).source_options.pdf.invoice_statuses;
  assert.equal(documentCard().open,false,"saving review results does not reopen the first file");
  assert.equal(Object.keys(statuses).length,12);assert.equal(statuses.r0.red_letter_status,"红字");assert.equal(statuses.r10.red_letter_status,"");assert.equal(statuses.r11.red_letter_status,"蓝字");
  assert.deepEqual(JSON.parse(calls.findLast(c=>c.url.endsWith("/prepare")).options.body).source_options.pdf.invoice_directions,{r0:"input",r11:"output"});
  assert.equal(find("上一页").disabled,true);
  sample.scope_errors={};sample.processing.records=[{...sample.documents[0].records[0],direction:"output"}];
  sample.processing.issues=[1,2].map(i=>({code:"invoice_status_missing",message:"票面未证明红蓝字或作废状态，请逐票核实。",source_file_id:"pdf",source_position:`PDF第${i}页/发票票面`}));
  sample.documents.push({id:"balance",name:"余额表.xlsx",material_type:"account_balance",sha256:"balance",parts:[],records:[{id:"balance-1",account_name:"库存现金",source_position:"余额表!A2",opening_balance:"0",current_debit:"1234.5600",current_credit:null,closing_balance:"-0.01"}]},{id:"vat",name:"申报表.xlsx",material_type:"vat_declaration",sha256:"vat",parts:[],records:[{id:"vat-1",item_label:"销售额",source_position:"主表!B2",declared_value:"1000.00"}]});
  sample.source_options.balance={};sample.source_options.vat={};
  await find("重新读取任务").onclick();
  for(const text of ["销项","库存现金","期初余额","1,234.5600","0","待识别","-0.01","销售额","1,000.00"])assert.ok(find(text),text);
  assert.ok(find("票面未证明红蓝字或作废状态，请逐票核实。（2 处）"));
  let resolve;actor={id:"two",role:"org_admin"};controller.reset();
  delay=null;await controller.openTask("task");
  delay=new Promise(r=>resolve=r);const pending=find("重新读取任务").onclick();actor={id:"three",role:"org_admin"};controller.reset();resolve(structuredClone(sample));await pending;
  assert.equal($("coreTaskResult").children.length,0);assert.equal($("coreCompanyName").value,"");
  delay=null;await controller.openTask("task");
  assert.equal(typeof find("删除本次任务")?.onclick,"function","task has an explicit deletion action");
  const beforeDelete=calls.length,cancelDelete=find("删除本次任务").onclick();assert.equal($("coreDeleteDialog").open,true,"confirmation is an in-page modal");assert.equal($("coreDeleteTaskName").textContent,"企业");
  assert.equal(typeof $("coreDeleteDialog").listeners.keydown,"function");$("coreDeleteDialog").listeners.keydown({key:"Tab",target:$("coreDeleteConfirm"),preventDefault(){}});assert.equal($("coreDeleteCancel").focused,true);
  $("coreDeleteCancel").onclick();await cancelDelete;assert.equal(calls.length,beforeDelete,"cancel does not send DELETE");
  failDelete=true;const failedDelete=find("删除本次任务").onclick();$("coreDeleteConfirm").onclick();await failedDelete;assert.match(errors.at(-1),/删除失败/);assert.ok(find("删除本次任务"));assert.equal($("coreCompanyName").value,"企业");
  failDelete=false;const confirmedDelete=find("删除本次任务").onclick();$("coreDeleteConfirm").onclick();await confirmedDelete;
  const deletion=calls.findLast(c=>c.options?.method==="DELETE");assert.equal(deletion.url,"/api/enterprise/input-tasks/task");assert.deepEqual(JSON.parse(deletion.options.body),{expected_revision:sample.revision,confirm:true});
  assert.equal($("coreTaskResult").children.length,0);assert.equal($("coreCompanyName").value,"");assert.equal($("coreStart").value,"");
  await controller.openTask("task");let finishDelete;delay=new Promise(resolve=>finishDelete=resolve);
  const pendingDelete=find("删除本次任务").onclick();$("coreDeleteConfirm").onclick();await Promise.resolve();
  actor={id:"four",role:"org_admin"};controller.reset();delay=null;await controller.openTask("task");
  finishDelete({id:"task",deleted:true});await pendingDelete;
  assert.ok(find("删除本次任务"),"late deletion result cannot clear a new session's task");assert.equal($("coreCompanyName").value,"企业");
  const beforeReset=calls.length,resetDialog=find("删除本次任务").onclick();controller.reset();await resetDialog;assert.equal($("coreDeleteDialog").open,false);assert.equal(calls.length,beforeReset,"session reset cancels an open confirmation");
  actor={id:"four",role:"accountant"};controller.reset();await controller.openTask("task");assert.equal(find("删除本次任务"),undefined,"task deletion follows administrator-only material deletion permissions");
  assert.equal($("coreRegion").value,"CN-33","reopening restores region from saved checks");
  const savedRegionChecks=sample.tax_checks;sample.id="other-task";sample.tax_checks={};await controller.openTask("other-task");
  assert.equal($("coreRegion").value,"","a new task must not inherit the previous enterprise's region");
  sample.id="task";sample.tax_checks=savedRegionChecks;await controller.openTask("task");assert.equal($("coreRegion").value,"CN-33");
  failAIRule="R-001";const beforePartial=calls.filter(c=>c.url==="/api/knowledge/retrieve").length;
  await find("开始检查").onclick();assert.equal(calls.filter(c=>c.url==="/api/knowledge/retrieve").length,beforePartial+2);
  assert.ok(all($("coreCheckResult")).some(n=>n.textContent==="本轮已完成 1/2 项。未完成不等于通过。"));
  assert.ok(all($("coreCheckResult")).some(n=>n.tag==="summary"&&n.textContent==="尚未完成 1 项（展开查看）"));
  assert.ok(all($("coreCheckResult")).some(n=>n.tag==="li"&&n.textContent==="收入核对"));
  assert.match($("coreActionStatus").textContent,/版本已变化/);assert.equal($("coreTaskForm").disabled,false);failAIRule=null;
  let releaseAI;aiDelay=new Promise(resolve=>releaseAI=resolve);const aiPending=find("开始检查").onclick();
  // Wait until scope and configuration have resolved and the first paid request is in flight.
  for(let i=0;i<12&&!calls.at(-1).url.endsWith("/retrieve");i++)await Promise.resolve();
  assert.ok(calls.at(-1).url.endsWith("/retrieve"));const beforeSessionChange=calls.length;
  actor={id:"changed-during-ai",role:"org_admin"};controller.reset();releaseAI();await aiPending;aiDelay=null;
  assert.equal(calls.length,beforeSessionChange,"a changed identity stops the remaining AI requests");
  assert.equal($("coreCheckResult").children.length,0);assert.equal($("coreTaskResult").children.length,0);assert.equal($("coreRegion").value,"");
  assert.equal($("coreTaskForm").disabled,false);
  await controller.refresh();await controller.openTask("task");await Promise.resolve();
  let releaseReport;reportHold=new Promise(resolve=>releaseReport=resolve);
  const reportPending=all($("coreCheckResult")).find(n=>n.tag==="button"&&n.textContent==="更新检查报告").onclick();
  actor={id:"changed-during-report",role:"org_admin"};controller.reset();reportHold=null;
  releaseReport({version:1000,input_revision:sample.revision});await reportPending;
  assert.equal($("coreCheckResult").children.length,0,"late report generation cannot repopulate a reset session");
  assert.equal($("coreTaskForm").disabled,false);
  console.log("Core frontend: scope validation, continuous upload/progress, serial revisions, pause/resume/cancel, automatic AI workflow, revision chaining, partial failures, session isolation, unsaved/session guards, no internal JSON, material tables, exact decimals, paginated invoice edits and review saves passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
