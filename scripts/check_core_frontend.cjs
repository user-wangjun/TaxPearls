"use strict";
// Exercise the shipped controller: failed saves retain edits, stale sessions cannot display evidence.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.value="";this.checked=false;this.disabled=false;this.listeners={};this.style={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(key,value){this[key]=value;}
  addEventListener(key,fn){this.listeners[key]=fn;}
  querySelectorAll(selector){const tags=selector.split(",");return all(this).filter(n=>tags.includes(n.tag));}
}
function all(root){return [root,...root.children.flatMap(all)];}
(async()=>{
  const ids=new Map(),$=id=>{if(!ids.has(id))ids.set(id,new Node("div"));return ids.get(id);};
  const errors=[],calls=[],continued=[];let actor={id:"one",role:"org_admin"},fail=false,failScope=false,delay=null;
  const sample={id:"task",revision:1,task:{company_name:"企业",tax_no:"91310000123456789A",start_date:"2026-01-01",end_date:"2026-01-31",applicable_forms:null},
    processing:{status:"needs_completion",issues:[],notices:[],records:[],boundary:"规则核验前"},documents:[],files:[{id:"excluded-file",deleted_at:null}],source_options:{"excluded-file":{excluded:true,original_name:"进项.xlsx"}},versions:[]};
  const api=async(url,options)=>{calls.push({url,options});if(delay)return await delay;
    if(failScope&&url.endsWith("/scope"))throw new Error("测试范围保存失败");
    if(fail&&url.endsWith("/prepare"))throw new Error("测试保存失败");return url==="/api/clients"?[{id:"existing",name:"历史企业",taxpayer_id:"91310000123456789A"}]:url==="/api/enterprise/input-tasks"&&!options?[]:structuredClone(sample);};
  const context={document:{createElement:tag=>new Node(tag)},URL,Blob,setTimeout,console,FormData,};vm.createContext(context);vm.runInContext(fs.readFileSync("webapp/static/core-inputs.js","utf8"),context);
  const controller=context.createCoreInputs({api,el:(tag,cls,text)=>{const n=new Node(tag);n.className=cls||"";n.textContent=text||"";return n;},$,user:()=>actor,showError:msg=>errors.push(msg),continueCheck:id=>continued.push(id)});
  await controller.refresh();assert.equal($("coreClient").children.length,2);
  $("coreClient").value="existing";$("coreClient").onchange();assert.equal($("coreCompanyName").value,"历史企业");assert.equal($("coreTaxNo").value,"91310000123456789A");
  $("coreClient").value="";$("coreClient").onchange();assert.equal($("coreTaxNo").value,"");assert.equal($("coreTaxNo")["aria-invalid"],"false");
  for(const [id,value] of [["coreCompanyName","企业"],["coreTaxNo","91310000123456789A"],["coreStart","2026-01-01"],["coreEnd","2026-01-31"]])$(id).value=value;
  await $("coreCreateTask").onclick();
  const find=text=>all($("coreTaskResult")).find(n=>n.textContent===text);
  const field=label=>all($("coreTaskResult")).find(n=>n["aria-label"]===label);
  for(const [kind,label] of [["account_balance","科目余额表"],["vat_declaration","增值税申报表"],["invoice_detail","发票明细"]]){
    const pick=field(label+"上传");pick.files=[new Blob(["synthetic material"])];await pick.onchange();
    const upload=calls.findLast(c=>c.url.endsWith("/files")).options.body;
    assert.equal(upload.get("material_type"),kind);assert.equal(upload.get("mapping_mode"),"ai");
    assert.ok(pick.accept.split(",").includes(".pdf"));assert.equal(upload.get("ocr"),"1");
  }
  const review=find("资料口径核对");const editor=all($("coreTaskResult")).find(n=>n.tag==="fieldset");assert.ok(review);
  const forms=field("本期适用附表名称（以分号分隔）");forms.value="附表一";editor.listeners.input();
  const before=calls.length;await find("重新读取任务").onclick();assert.equal(calls.length,before);assert.match(errors.at(-1),/未保存/);assert.equal(forms.value,"附表一");
  find("开始检查").onclick();assert.equal(continued.length,0);assert.match(errors.at(-1),/先保存/);
  field("已核实本期适用附表清单（如无适用附表，也须明确确认）").checked=true;
  fail=true;await find("保存核对并重新整理资料").onclick();assert.match(errors.at(-1),/保存失败/);assert.equal(forms.value,"附表一");assert.equal(forms.disabled,false);
  fail=false;await find("保存核对并重新整理资料").onclick();const write=calls.findLast(c=>c.url.endsWith("/prepare"));assert.deepEqual(JSON.parse(write.options.body).applicable_forms,["附表一"]);
  assert.equal(JSON.parse(write.options.body).mapping_mode,"ai");
  await find("恢复资料：进项.xlsx").onclick();const restored=calls.findLast(c=>c.url.endsWith("/prepare"));assert.deepEqual(JSON.parse(restored.options.body).source_options,{"excluded-file":{excluded:false}});
  assert.equal(JSON.parse(restored.options.body).mapping_mode,"ai");
  $("coreTaxNo").value="bad";$("coreTaxNo").listeners.input();assert.equal($("coreTaxNo")["aria-invalid"],"true");
  const beforeScope=calls.length;await find("开始检查").onclick();assert.equal(calls.length,beforeScope);assert.equal(continued.length,0);
  $("coreTaxNo").value="91310000123456789A";$("coreTaxNo").listeners.input();assert.equal($("coreTaxNo")["aria-invalid"],"false");
  failScope=true;await find("开始检查").onclick();assert.equal(continued.length,0);assert.match(errors.at(-1),/范围保存失败/);
  failScope=false;await find("开始检查").onclick();assert.deepEqual(continued,["task"]);
  assert.equal(JSON.parse(calls.findLast(c=>c.url.endsWith("/scope")).options.body).confirm,true);
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
  assert.equal(field("销项发票_每页一张.pdf 已对照原页核实提取候选的字段、金额、购销方和所属期").checked,false);
  assert.ok(find("发票明细 · 12 条候选 · 待核对"));
  find("下一页").onclick();assert.equal(rows().length,2);assert.ok(find("26260901000000000011"));assert.equal(find("下一页").disabled,true);
  find("上一页").onclick();assert.equal(field(firstLabel).value,"红字");
  await find("保存核对并重新整理资料").onclick();
  const statuses=JSON.parse(calls.findLast(c=>c.url.endsWith("/prepare")).options.body).source_options.pdf.invoice_statuses;
  assert.equal(Object.keys(statuses).length,12);assert.equal(statuses.r0.red_letter_status,"红字");assert.equal(statuses.r10.red_letter_status,"");assert.equal(statuses.r11.red_letter_status,"蓝字");
  assert.equal(find("上一页").disabled,true);
  sample.scope_errors={};sample.processing.records=[{...sample.documents[0].records[0],direction:"output"}];
  sample.processing.issues=[1,2].map(i=>({code:"invoice_status_missing",message:"票面未证明红蓝字或作废状态，请逐票核实。",source_file_id:"pdf",source_position:`PDF第${i}页/发票票面`}));
  sample.documents.push({id:"balance",name:"余额表.xlsx",material_type:"account_balance",sha256:"balance",parts:[],records:[{id:"balance-1",account_name:"库存现金",source_position:"余额表!A2",opening_balance:"0",current_debit:"1234.5600",current_credit:null,closing_balance:"-0.01"}]},{id:"vat",name:"申报表.xlsx",material_type:"vat_declaration",sha256:"vat",parts:[],records:[{id:"vat-1",item_label:"销售额",source_position:"主表!B2",declared_value:"1000.00"}]});
  sample.source_options.balance={};sample.source_options.vat={};
  await find("重新读取任务").onclick();
  for(const text of ["销项","库存现金","期初余额","1,234.5600","0","待识别","-0.01","销售额","1,000.00","标准记录与字段来源（1）"])assert.ok(find(text),text);
  assert.ok(find("票面未证明红蓝字或作废状态，请逐票核实。（2 处）"));
  let resolve;delay=new Promise(r=>resolve=r);const pending=find("重新读取任务").onclick();actor={id:"two",role:"org_admin"};controller.reset();resolve(structuredClone(sample));await pending;
  assert.equal($("coreTaskResult").children.length,0);assert.equal($("coreCompanyName").value,"");
  console.log("Core frontend: scope validation, upload dispatch, unsaved/session guards, candidate/standard record distinction, three material tables, exact decimal display, paginated invoice edits and full save passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
