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
  const errors=[],calls=[],continued=[],downloads=[];let actor={id:"one",role:"org_admin"},fail=false,failScope=false,delay=null,failDelete=false;
  const exported={schema_version:"taxpearls.material-data.v1",task:{},records:[{id:"clean-record",quality:{requires_source_review:true}}],sources:[]};
  const sample={id:"task",revision:1,task:{company_name:"企业",tax_no:"91310000123456789A",start_date:"2026-01-01",end_date:"2026-01-31",range_kind:"month",
      scope_origins:{},scope_candidates:{start_date:[{value:"2026-01-01",sources:[{file_id:"f",position:"Sheet1"}]}],end_date:[{value:"2026-01-31",sources:[{file_id:"f",position:"Sheet1"}]}]},applicable_forms:null},
    processing:{status:"needs_completion",issues:[],notices:[],records:[],scope:{kind:"month",start_date:"2026-01-01",end_date:"2026-01-31",confirmed:false,outside_records:0,unknown_date_records:0},boundary:"规则核验前"},
    documents:[{id:"review-pdf",name:"进项.pdf",material_type:"invoice_detail",records:[],parts:[],sha256:"synthetic-pdf",pdf_candidate:true},
      {id:"review-sheet",name:"余额.xlsx",material_type:"account_balance",records:[],parts:[],sha256:"synthetic-sheet"}],
    files:[{id:"excluded-file",deleted_at:null}],source_options:{"excluded-file":{excluded:true,original_name:"进项.xlsx"},
      "review-pdf":{original_name:"进项.pdf",ocr:false,source_reviewed:false,confirmed_metadata:{direction:"output"}},"review-sheet":{original_name:"余额.xlsx",ocr:false}},versions:[]};
  const api=async(url,options)=>{calls.push({url,options});if(delay)return await delay;
    if(options?.method==="DELETE"){if(failDelete)throw new Error("测试删除失败");return {id:"task",deleted:true};}
    if(failScope&&url.endsWith("/scope"))throw new Error("测试范围保存失败");
    if(fail&&url.endsWith("/prepare"))throw new Error("测试保存失败");if(url.includes("/data?"))return structuredClone(exported);return url==="/api/clients"?[{id:"existing",name:"历史企业",taxpayer_id:"91310000123456789A"}]:url==="/api/enterprise/input-tasks"&&!options?[]:structuredClone(sample);};
  let holdUploads=false;const uploadRequests=[];
  class UploadRequest {
    constructor(){this.upload={};uploadRequests.push(this);}
    open(method,url){this.method=method;this.url=url;}
    send(body){calls.push({url:this.url,options:{method:this.method,body}});if(!holdUploads)queueMicrotask(()=>this.succeed());}
    succeed(revision=sample.revision){this.upload.onload?.();this.status=200;this.responseText=JSON.stringify({...sample,revision});this.onload();}
    abort(){this.aborted=true;this.onabort?.();}
  }
  const pageEvents={};
  const context={document:{createElement:tag=>new Node(tag)},URL:{createObjectURL(blob){downloads.push(blob);return "blob:export";},revokeObjectURL(){}},Blob,setTimeout,console,FormData,XMLHttpRequest:UploadRequest,addEventListener:(event,fn)=>pageEvents[event]=fn};vm.createContext(context);vm.runInContext(fs.readFileSync("webapp/static/core-inputs.js","utf8"),context);
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
  await find("下载标准数据 JSON").onclick();
  assert.equal(calls.at(-1).url,"/api/enterprise/input-tasks/task/data?expected_revision=1");
  assert.deepEqual(JSON.parse(await downloads.at(-1).text()),exported);
  assert.ok(find("采用此期间"));assert.equal($("coreStart").value,"2026-01-01");assert.equal($("coreEnd").value,"2026-01-31");
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
  const downloadsBefore=downloads.length;await find("下载标准数据 JSON").onclick();assert.equal(downloads.length,downloadsBefore);assert.match(errors.at(-1),/先保存/);
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
  failScope=true;await find("开始检查").onclick();assert.equal(continued.length,0);assert.match(errors.at(-1),/范围保存失败/);
  failScope=false;await find("开始检查").onclick();assert.deepEqual(continued,["task"]);
  assert.equal(JSON.parse(calls.findLast(c=>c.url.endsWith("/scope")).options.body).confirm,true);
  assert.equal(JSON.parse(calls.findLast(c=>c.url.endsWith("/scope")).options.body).range_kind,"month");
  // Parsed candidates remain visible before the enterprise/check period is complete.
  sample.scope_errors={start_date:"请填写检查起始日"};
  sample.documents=[{id:"pdf",name:"销项发票_每页一张.pdf",material_type:"invoice_detail",sha256:"synthetic",pdf_candidate:true,parts:[],records:Array.from({length:12},(_,i)=>({id:"r"+i,invoice_no:"262609010000000000"+String(i).padStart(2,"0"),issue_date:"2026-01-15",source_position:`PDF第${i+1}页/发票票面`,amount_excluding_tax:i?"100.00":"12345678901234567890.0123",tax_amount:"13.00",seller_tax_no:sample.task.tax_no,buyer_tax_no:"OTHER"}))}];
  sample.documents[0].records[11].parsed_invoice_statuses={red_letter_status:"蓝字",void_status:"正常"};
  sample.source_options.pdf={source_reviewed:true,ocr:true,invoice_statuses:{r11:{red_letter_status:"",void_status:""}}};
  await find("重新读取任务").onclick();
  assert.ok(find("已解析候选 12 条"));assert.ok(find("资料缺口与冲突（待核对检查范围）"));
  assert.ok(find("标准记录与字段来源（待整理；已解析候选 12 条）"));
  assert.equal(field("本期适用附表名称（以分号分隔）").tag,"input");
  assert.ok(all($("coreTaskResult")).some(n=>n.className==="field"));
  assert.ok(find("12,345,678,901,234,567,890.0123"));
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
  assert.equal(Object.keys(statuses).length,12);assert.equal(statuses.r0.red_letter_status,"红字");assert.equal(statuses.r10.red_letter_status,"");assert.equal(statuses.r11.red_letter_status,"蓝字");
  assert.deepEqual(JSON.parse(calls.findLast(c=>c.url.endsWith("/prepare")).options.body).source_options.pdf.invoice_directions,{r0:"input",r11:"output"});
  assert.equal(find("上一页").disabled,true);
  sample.scope_errors={};sample.processing.records=[{...sample.documents[0].records[0],direction:"output"}];
  sample.processing.issues=[1,2].map(i=>({code:"invoice_status_missing",message:"票面未证明红蓝字或作废状态，请逐票核实。",source_file_id:"pdf",source_position:`PDF第${i}页/发票票面`}));
  sample.documents.push({id:"balance",name:"余额表.xlsx",material_type:"account_balance",sha256:"balance",parts:[],records:[{id:"balance-1",account_name:"库存现金",source_position:"余额表!A2",opening_balance:"0",current_debit:"1234.5600",current_credit:null,closing_balance:"-0.01"}]},{id:"vat",name:"申报表.xlsx",material_type:"vat_declaration",sha256:"vat",parts:[],records:[{id:"vat-1",item_label:"销售额",source_position:"主表!B2",declared_value:"1000.00"}]});
  sample.source_options.balance={};sample.source_options.vat={};
  await find("重新读取任务").onclick();
  for(const text of ["销项","库存现金","期初余额","1,234.5600","0","待识别","-0.01","销售额","1,000.00","标准记录与字段来源（1）"])assert.ok(find(text),text);
  assert.ok(find("票面未证明红蓝字或作废状态，请逐票核实。（2 处）"));
  let resolve;delay=new Promise(r=>resolve=r);const exportPending=find("下载标准数据 JSON").onclick();actor={id:"two",role:"org_admin"};controller.reset();resolve(structuredClone(exported));await exportPending;
  assert.equal(downloads.length,downloadsBefore,"a changed session must not download previous enterprise data");
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
  const deletion=calls.findLast(c=>c.options?.method==="DELETE");assert.equal(deletion.url,"/api/enterprise/input-tasks/task");assert.deepEqual(JSON.parse(deletion.options.body),{expected_revision:1,confirm:true});
  assert.equal($("coreTaskResult").children.length,0);assert.equal($("coreCompanyName").value,"");assert.equal($("coreStart").value,"");
  await controller.openTask("task");let finishDelete;delay=new Promise(resolve=>finishDelete=resolve);
  const pendingDelete=find("删除本次任务").onclick();$("coreDeleteConfirm").onclick();await Promise.resolve();
  actor={id:"four",role:"org_admin"};controller.reset();delay=null;await controller.openTask("task");
  finishDelete({id:"task",deleted:true});await pendingDelete;
  assert.ok(find("删除本次任务"),"late deletion result cannot clear a new session's task");assert.equal($("coreCompanyName").value,"企业");
  const beforeReset=calls.length,resetDialog=find("删除本次任务").onclick();controller.reset();await resetDialog;assert.equal($("coreDeleteDialog").open,false);assert.equal(calls.length,beforeReset,"session reset cancels an open confirmation");
  actor={id:"four",role:"accountant"};controller.reset();await controller.openTask("task");assert.equal(find("删除本次任务"),undefined,"task deletion follows administrator-only material deletion permissions");
  console.log("Core frontend: scope validation, continuous upload/progress, serial revisions, pause/resume/cancel, unsaved/session guards, candidate/standard records, three material tables, exact decimals, paginated invoice edits and full save passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
