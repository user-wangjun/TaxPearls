"use strict";
// Execute the real enterprise controller with a minimal DOM/network boundary.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
function node(tag="div", className="", text="") {
  return {tag, className, textContent:text, children:[], style:{}, value:"", hidden:false, disabled:false, listeners:{},
    classList:{add(){}}, scrollIntoView(){}, setAttribute(key,value){this[key]=value;},
    append(...items){this.children.push(...items);}, replaceChildren(...items){this.children=items;},
    addEventListener(name,fn){this.listeners[name]=fn;}, remove(){}, click(){return this.onclick?.();}};
}
async function main() {
  const nodes=new Map(), calls=[], errors=[], results=[];
  let actor={id:"owner",role:"org_admin"}, controller;
  const timers=new Map();let timerId=0;
  const context={URL:{revokeObjectURL(){}},setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},window:{confirm:()=>true},
    FormData:class{append(){}},fetch:()=>{throw new Error("unexpected download");}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("webapp/static/enterprise.js","utf8"),context);
  const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  controller=context.createEnterpriseMaterials({api:(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject})),
    el:node,$,user:()=>actor,clear:()=>controller.reset(),showError:error=>errors.push(error),showResult:r=>results.push(r)});
  const sample=()=>({id:"batch",revision:1,analysis_revision:1,company:{name:"原文企业",taxpayer_id:"ID",period_start:"2026-01-01",period_end:"2026-01-31"},
    selections:{},documents:[],files:[],fields:{},metrics:[],executions:[],analysis:{can_confirm:true,checks:[],edits:[],feedback:{blocking:[],limited:[],suggested:[]}}});
  const all=(root=$("materialReview"))=>[root,...root.children.flatMap(child=>all(child))];
  const button=text=>{const found=all().find(n=>n.tag==="button"&&n.textContent===text);assert.ok(found,text);return found;};
  const field=title=>all().find(n=>n["aria-label"]===title);
  const settleLists=()=>{for(const call of calls.splice(0)){assert.ok(["/api/enterprise/materials","/api/enterprise/materials/config"].includes(call.url),call.url);call.resolve(call.url.endsWith("/config")?{retention_ready:true,message:"configured"}:[]);}};
  // Late responses from a previous account/reset must not paint private data.
  const stale=controller.open("old"), pending=calls.shift();controller.reset();
  pending.resolve(sample());await stale;assert.equal($("materialReview").children.length,0);
  const opening=controller.open("batch");calls.shift().resolve(sample());await opening;
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Each acknowledgement is explicit; changing scope clears and disables stale acknowledgements.
  const securityReview=sample();securityReview.documents=[{id:"proof",name:"扫描.pdf",kind:"pdf",error:"candidate",
    company:{},evidence_reviews:[{id:"a".repeat(64),key:"scope",label:"企业与期间",reason:"对照原件",reviewed:false}]}];
  securityReview.analysis.can_confirm=false;
  securityReview.documents[0].extraction={method:"ai",period_observations:[{value:"2026-01",adopted_as_period:false}]};
  const proofOpen=controller.open("proof");calls.shift().resolve(securityReview);await proofOpen;
  assert.ok(all().some(n=>n.textContent.includes("AI 企业与期间候选（待核对）")));
  const acknowledgement=field("扫描.pdf 复核 scope");assert.equal(acknowledgement.checked,false);
  acknowledgement.checked=true;acknowledgement.listeners.change();
  const proofSave=button("保存修改并重新分析").click(),proofWrite=calls.shift();
  assert.deepEqual(JSON.parse(proofWrite.options.body).selections.proof.evidence_reviews,["a".repeat(64)]);
  proofWrite.resolve(securityReview);await proofSave;settleLists();await Promise.resolve();
  field("扫描.pdf 复核 scope").checked=true;
  field("企业名称（必填）").value="其他企业";field("企业名称（必填）").listeners.input();
  assert.equal(field("扫描.pdf 复核 scope").checked,false);assert.equal(field("扫描.pdf 复核 scope").disabled,true);
  const restoreReview=controller.open("batch");calls.shift().resolve(sample());await restoreReview;
  // Mapping controls send only source-column choices and clear stale value edits/reviews.
  const mapped=sample();mapped.selections.f={standard_edits:{"利润表!C3":"12"}};
  mapped.documents=[{id:"f",name:"导出.xlsx",kind:"xlsx",company:{},
    standard_fields:[{id:"利润表!C3",table:"利润表",label:"营业收入",value:"10"}],
    import_mapping:{sheets:[{sheet:"利润表",kind:"利润表",name:"其他企业",source_unit:"",source_period:"",unit_origin:"missing",
      column_choices:{"2:A":[{value:"C",label:"本月数"},{value:"D",label:"本年累计数"}]}}]},
    evidence_reviews:[{id:"b".repeat(64),key:"mapping",label:"映射",reason:"核对",reviewed:true}]}];
  const mappingOpen=controller.open("batch");calls.shift().resolve(mapped);await mappingOpen;
  const unitChoice=field("导出.xlsx 利润表 原金额单位");unitChoice.value="万元";unitChoice.listeners.change();
  const mappingPeriod=field("导出.xlsx 利润表 实际期间");mappingPeriod.value="2026-01";mappingPeriod.listeners.input();
  const columnChoice=field("导出.xlsx 利润表 金额栏次 2:A");columnChoice.value="C";columnChoice.listeners.change();
  assert.equal(field("导出.xlsx 复核 mapping").checked,false);
  const mappingSave=button("保存修改并重新分析").click(),mappingWrite=calls.shift();
  const mappingBody=JSON.parse(mappingWrite.options.body).selections.f;
  assert.deepEqual(mappingBody.import_options,{"利润表":{unit:"万元",period:"2026-01",columns:{"2:A":"C"}}});
  assert.deepEqual(mappingBody.standard_edits,{});assert.deepEqual(mappingBody.evidence_reviews,[]);
  mappingWrite.resolve(sample());await mappingSave;settleLists();await Promise.resolve();
  // Legacy sources use the same explicit unit/period controls, never an
  // implicit scale or an auto-approved legacy-only path.
  const legacyMapped=structuredClone(mapped);legacyMapped.documents[0].kind="xls";
  legacyMapped.documents[0].name="旧报表.xls";legacyMapped.analysis.can_confirm=false;
  const legacyOpen=controller.open("legacy");calls.shift().resolve(legacyMapped);await legacyOpen;
  assert.equal(field("旧报表.xls 利润表 原金额单位").value,"");
  assert.equal(button("确认材料并开始检测").disabled,true);
  assert.equal(field("旧报表.xls 利润表!C3 用于检测的值").value,"12");
  // A cached old adapter result has no editable amounts or acknowledgements;
  // saving may retain explicit column choices, but never sends old value edits.
  const oldAdapter=structuredClone(mapped);oldAdapter.analysis.can_confirm=false;
  oldAdapter.analysis.feedback.blocking=[{code:"financial_reanalysis_required",file_id:"f",message:"须重解析"}];
  oldAdapter.metrics=[{name:"营业收入",value:"120000",source:"旧缓存",detail:"未重解析"}];
  oldAdapter.analysis.edits=[{field:"利润表!C3",new:"12"}];
  oldAdapter.analysis.checks=[{rule_id:"R-007",name:"收入勾稽",version:"1",ready:true,reasons:[]}];
  oldAdapter.documents[0].financial_reanalysis_required=true;
  oldAdapter.selections.f.evidence_reviews=["b".repeat(64)];
  const adapterOpen=controller.open("batch");calls.shift().resolve(oldAdapter);await adapterOpen;
  assert.ok(all().some(n=>n.textContent.includes("财务适配已更新")));
  assert.ok(all().some(n=>n.textContent.includes("旧版缓存指标与来源")));
  assert.ok(all().some(n=>n.textContent.includes("旧版缓存数值修正记录（重解析时清除）")));
  assert.ok(all().some(n=>n.textContent.includes("旧版缓存检查范围")));
  assert.ok(all().some(n=>n.textContent.includes("须重解析后重新判断")));
  assert.equal(all().some(n=>n.textContent.includes("本次检测输入指标与来源")),false);
  assert.equal(button("确认材料并开始检测").disabled,true);
  assert.equal(field("导出.xlsx 利润表!C3 用于检测的值"),undefined);
  assert.equal(field("导出.xlsx 复核 mapping"),undefined);
  const adapterSave=button("保存修改并重新分析").click(),adapterWrite=calls.shift();
  const adapterBody=JSON.parse(adapterWrite.options.body).selections.f;
  assert.deepEqual(adapterBody.standard_edits,{});assert.deepEqual(adapterBody.evidence_reviews,[]);
  adapterWrite.resolve(sample());await adapterSave;settleLists();await Promise.resolve();
  // Old PDF caches have the same no-edit/no-acknowledgement boundary, but
  // saving preserves the chosen original pages and clears old amount edits.
  const oldPdf=sample();oldPdf.analysis.can_confirm=false;
  oldPdf.analysis.feedback.blocking=[{code:"pdf_reanalysis_required",file_id:"p",message:"须重解析原件"}];
  oldPdf.metrics=[{name:"利润表.营业收入",value:"777",source:"旧缓存"}];
  oldPdf.ai={ready:true,queue_enabled:true,max_pages:5};
  oldPdf.selections.p={pdf_range:{first:1,last:2},rows:[{name:"利润表.营业收入",value:"777",page:2}],evidence_reviews:["c".repeat(64)]};
  oldPdf.documents=[{id:"p",name:"旧原件.pdf",kind:"pdf",company:{},page_count:2,pdf_reanalysis_required:true,
    rows:oldPdf.selections.p.rows,pdf_selection:{first:1,last:2,total_pages:2,max_segment_pages:50,pending:false},
    evidence_reviews:[{id:"c".repeat(64),key:"scope",label:"归属",reason:"原件",reviewed:true}]}];
  const oldPdfOpen=controller.open("batch");calls.shift().resolve(oldPdf);await oldPdfOpen;
  assert.ok(all().some(n=>n.textContent.includes("PDF 适配已更新或旧解析状态不明")));
  assert.ok(all().some(n=>n.textContent.includes("旧版缓存指标与来源")));
  assert.equal(field("指标数值"),undefined);assert.equal(field("旧原件.pdf 复核 scope"),undefined);
  assert.equal(button("确认材料并开始检测").disabled,true);
  field("旧原件.pdf 同意发送片段至模型").checked=true;
  field("旧原件.pdf 同意发送片段至模型").listeners.change();
  assert.equal(button("AI 提取已保存的 PDF 片段 · 旧原件.pdf").disabled,true);
  const oldPdfSave=button("保存修改并重新分析").click(),oldPdfWrite=calls.shift();
  const oldPdfBody=JSON.parse(oldPdfWrite.options.body).selections.p;
  assert.deepEqual(oldPdfBody.pdf_range,{first:1,last:2});
  assert.deepEqual(oldPdfBody.rows,[]);assert.deepEqual(oldPdfBody.evidence_reviews,[]);
  oldPdfWrite.resolve(sample());await oldPdfSave;settleLists();await Promise.resolve();
  const excludeOpen=controller.open("batch");calls.shift().resolve(mapped);await excludeOpen;
  assert.ok(all().some(n=>n.textContent.includes("原文企业：其他企业；识别表类：利润表")));
  const excludedSheet=field("导出.xlsx 排除工作表 利润表");
  excludedSheet.checked=true;excludedSheet.listeners.change();
  assert.equal(button("确认材料并开始检测").disabled,true);
  assert.equal(field("导出.xlsx 复核 mapping").checked,false);
  const exclusionSave=button("保存修改并重新分析").click(),exclusionWrite=calls.shift();
  const exclusionBody=JSON.parse(exclusionWrite.options.body).selections.f;
  assert.deepEqual(exclusionBody.import_options,{"利润表":{excluded:true}});
  assert.deepEqual(exclusionBody.standard_edits,{});assert.deepEqual(exclusionBody.evidence_reviews,[]);
  exclusionWrite.resolve(sample());await exclusionSave;settleLists();await Promise.resolve();
  const foreignMapping=sample();foreignMapping.analysis.can_confirm=false;
  foreignMapping.documents=[{id:"fx",name:"外币导出.xlsx",kind:"xlsx",company:{},
    import_mapping:{sheets:[{sheet:"利润表",kind:"利润表",source_unit:"万元",source_period:"2026Q1",
      currency:"USD",currency_supported:false,as_of:"2026-03-31"}]}}];
  const foreignOpen=controller.open("batch");calls.shift().resolve(foreignMapping);await foreignOpen;
  assert.ok(all().some(n=>n.textContent.includes("原文币种：USD")&&n.textContent.includes("补填单位不能转换币种")));
  assert.ok(all().some(n=>n.textContent==="原文期间：2026Q1"));
  assert.ok(all().some(n=>n.textContent==="原文报表时点：2026-03-31"));
  assert.equal(field("外币导出.xlsx 利润表 原金额单位"),undefined);
  assert.equal(field("外币导出.xlsx 利润表 实际期间"),undefined);
  assert.equal(button("确认材料并开始检测").disabled,true);
  const excludeForeign=field("外币导出.xlsx 排除工作表 利润表");excludeForeign.checked=true;excludeForeign.listeners.change();
  const saveForeign=button("保存修改并重新分析").click(),foreignWrite=calls.shift();
  assert.deepEqual(JSON.parse(foreignWrite.options.body).selections.fx.import_options,{"利润表":{excluded:true}});
  foreignWrite.resolve(sample());await saveForeign;settleLists();await Promise.resolve();
  const unresolvedPeriod=sample();unresolvedPeriod.analysis.can_confirm=false;
  unresolvedPeriod.documents=[{id:"period",name:"期间导出.xlsx",kind:"xlsx",company:{},
    import_mapping:{sheets:[{sheet:"利润表",kind:"利润表",source_unit:"万元",source_period:"",
      period_unresolved:true,period_unresolved_text:"2026年1至9月"}]}}];
  const periodOpen=controller.open("batch");calls.shift().resolve(unresolvedPeriod);await periodOpen;
  assert.ok(all().some(n=>n.textContent.includes("2026年1至9月")&&n.textContent.includes("补填其他期间不能覆盖")));
  assert.equal(field("期间导出.xlsx 利润表 实际期间"),undefined);
  assert.equal(button("确认材料并开始检测").disabled,true);
  const formula=sample();formula.analysis.can_confirm=false;
  formula.documents=[{id:"formula",name:"公式.xlsx",kind:"xlsx",company:{},
    standard_fields:[{id:"利润表!C3",table:"利润表",label:"营业收入",value:null,formula:"=1+1",cached_raw:"0",cached_value:"0"}],
    import_mapping:{sheets:[{sheet:"利润表",kind:"利润表",source_unit:"元",source_period:"2026-01",unit_origin:"template"}]},
    evidence_reviews:[{id:"d".repeat(64),key:"formula:利润表!C3",label:"公式金额",reason:"核对",reviewed:true}]}];
  const formulaOpen=controller.open("batch");calls.shift().resolve(formula);await formulaOpen;
  const formulaValue=field("公式.xlsx 利润表!C3 用于检测的值");
  assert.equal(formulaValue.value,"");
  assert.ok(all().some(n=>n.textContent.includes("缓存可能过期")));
  assert.ok(all().some(n=>n.textContent==="标准模板单位约定：元"));
  assert.equal(field("公式.xlsx 利润表 原金额单位"),undefined);
  await button("核对后采用缓存候选 · 利润表!C3").click();
  assert.equal(formulaValue.value,"0");
  assert.equal(field("公式.xlsx 复核 formula:利润表!C3").checked,false);
  const formulaSave=button("保存修改并重新分析").click(),formulaWrite=calls.shift();
  const formulaBody=JSON.parse(formulaWrite.options.body).selections.formula;
  assert.deepEqual(formulaBody.standard_edits,{"利润表!C3":"0"});
  assert.deepEqual(formulaBody.evidence_reviews,[]);
  formulaWrite.resolve(formula);await formulaSave;settleLists();await Promise.resolve();
  await button("恢复原文 · 利润表!C3").click();
  assert.equal(field("公式.xlsx 利润表!C3 用于检测的值").value,"");
  const formulaRestore=button("保存修改并重新分析").click(),restoreFormulaWrite=calls.shift();
  assert.deepEqual(JSON.parse(restoreFormulaWrite.options.body).selections.formula.standard_edits,{});
  restoreFormulaWrite.resolve(sample());await formulaRestore;settleLists();await Promise.resolve();
  // Source-page ranges keep original numbers and never carry old edits or approvals.
  const longPDF=sample();longPDF.analysis.can_confirm=false;
  longPDF.documents=[{id:"p",name:"长原件.pdf",kind:"pdf",company:{},rows:[],pages:[],
    pdf_selection:{total_pages:158,max_segment_pages:50,pending:true,first:null,last:null,
      segments:[{first:1,last:50}],unprocessed_ranges:[{first:1,last:158}]},
    evidence_reviews:[{id:"c".repeat(64),key:"pdf_range",label:"页码范围",reason:"核实原件",reviewed:false}]}];
  const rangeOpen=controller.open("batch");calls.shift().resolve(longPDF);await rangeOpen;
  const firstPage=field("长原件.pdf 原件起始页"),lastPage=field("长原件.pdf 原件结束页");
  firstPage.value="51";firstPage.listeners.input();lastPage.value="52";lastPage.listeners.input();
  assert.equal(field("长原件.pdf 复核 pdf_range").disabled,true);
  const rangeSave=button("保存修改并重新分析").click(),rangeWrite=calls.shift();
  const rangeBody=JSON.parse(rangeWrite.options.body).selections.p;
  assert.deepEqual(rangeBody.pdf_range,{first:51,last:52});
  assert.deepEqual(rangeBody.rows,[]);assert.deepEqual(rangeBody.evidence_reviews,[]);
  rangeWrite.resolve(sample());await rangeSave;settleLists();await Promise.resolve();
  // Short PDFs expose explicit selection without silently rewriting local data.
  const shortPDF=sample();shortPDF.analysis.can_confirm=false;
  shortPDF.ai={ready:true,queue_enabled:true,max_pages:2,effective_model:"synthetic",vision:true};
  shortPDF.documents=[{id:"short",name:"短扫描.pdf",kind:"pdf",page_count:20,company:{},pages:[],
    rows:[{name:"利润表.营业收入",value:"12",page:4,detail:"人工核对"}]}];
  const shortTitle="AI 提取已保存的 PDF 片段 · 短扫描.pdf";
  const shortOpen=controller.open("batch");calls.shift().resolve(shortPDF);await shortOpen;
  assert.equal(field("短扫描.pdf 原件起始页").value,1);
  assert.equal(field("短扫描.pdf 原件结束页").value,20);
  const shortConsent=field("短扫描.pdf 同意发送片段至模型");
  shortConsent.checked=true;shortConsent.listeners.change();
  assert.equal(button(shortTitle).disabled,true);
  await button(shortTitle).click();assert.equal(calls.length,0);
  field("指标数值").value="13";field("指标数值").listeners.input();
  const ordinarySave=button("保存修改并重新分析").click(),ordinaryWrite=calls.shift();
  const ordinaryBody=JSON.parse(ordinaryWrite.options.body).selections.short;
  assert.ok(!("pdf_range" in ordinaryBody));assert.equal(ordinaryBody.rows[0].value,"13");
  ordinaryWrite.resolve(shortPDF);await ordinarySave;settleLists();await Promise.resolve();
  field("短扫描.pdf 原件起始页").value="4";field("短扫描.pdf 原件起始页").listeners.input();
  field("短扫描.pdf 原件结束页").value="4";field("短扫描.pdf 原件结束页").listeners.input();
  assert.equal(button(shortTitle).disabled,true);
  const shortSave=button("保存修改并重新分析").click(),shortWrite=calls.shift();
  const shortBody=JSON.parse(shortWrite.options.body).selections.short;
  assert.deepEqual(shortBody.pdf_range,{first:4,last:4});assert.deepEqual(shortBody.rows,[]);
  shortWrite.resolve({...shortPDF,documents:[{...shortPDF.documents[0],pdf_selection:{total_pages:20,
    max_segment_pages:50,pending:false,first:4,last:4,segments:[],unprocessed_ranges:[{first:1,last:3},{first:5,last:20}]}}]});
  await shortSave;settleLists();await Promise.resolve();
  assert.ok(!field("短扫描.pdf 同意发送片段至模型").checked);
  assert.equal(button(shortTitle).disabled,true);
  field("短扫描.pdf 同意发送片段至模型").checked=true;
  field("短扫描.pdf 同意发送片段至模型").listeners.change();assert.equal(button(shortTitle).disabled,false);
  const wholeOpen=controller.open("batch");calls.shift().resolve(shortPDF);await wholeOpen;
  await button("选用所填页码片段 · 短扫描.pdf").click();assert.equal(calls.length,0);
  const wholeSave=button("保存修改并重新分析").click(),wholeWrite=calls.shift();
  const wholeBody=JSON.parse(wholeWrite.options.body).selections.short;
  assert.deepEqual(wholeBody.pdf_range,{first:1,last:20});assert.deepEqual(wholeBody.rows,[]);
  wholeWrite.resolve(shortPDF);await wholeSave;settleLists();await Promise.resolve();
  // Reopened user edits must not hide or replace the immutable AI evidence.
  const correctedPDF={...shortPDF,documents:[{...shortPDF.documents[0],extraction:{method:"ai"},
    rows:[{...shortPDF.documents[0].rows[0],ai_raw_value:"12",ai_unit:"元",ai_quote:"收入 12"}]}],
    selections:{short:{rows:[{name:"利润表.营业收入",value:"13",page:4,detail:"用户说明"}]}}};
  const correctedOpen=controller.open("batch");calls.shift().resolve(correctedPDF);await correctedOpen;
  assert.equal(field("指标数值").value,"13");
  assert.ok(all().some(n=>n.textContent==="原始 AI 候选与证据（只读，人工修改不覆盖）"));
  assert.ok(all().some(n=>n.tag==="pre"&&n.textContent.includes('"ai_raw_value": "12"')&&n.textContent.includes("收入 12")));
  const correctedSave=button("保存修改并重新分析").click(),correctedWrite=calls.shift();
  const correctedRow=JSON.parse(correctedWrite.options.body).selections.short.rows[0];
  assert.equal(correctedRow.value,"13");assert.ok(!("ai_raw_value" in correctedRow));
  correctedWrite.resolve(correctedPDF);await correctedSave;settleLists();await Promise.resolve();
  // Sending a saved PDF is separate from local range saving and requires fresh consent.
  const selectedPDF=sample();selectedPDF.analysis.can_confirm=false;
  selectedPDF.ai={ready:true,queue_enabled:true,max_pages:2,effective_model:"synthetic",vision:false};
  selectedPDF.documents=[{...longPDF.documents[0],pdf_selection:{total_pages:54,max_segment_pages:50,
    pending:false,first:51,last:52,segments:[],unprocessed_ranges:[{first:1,last:50},{first:53,last:54}]}}];
  const aiOpen=controller.open("batch");calls.shift().resolve(selectedPDF);await aiOpen;
  const extractTitle="AI 提取已保存的 PDF 片段 · 长原件.pdf";
  assert.equal(button(extractTitle).disabled,true);
  await button(extractTitle).click();assert.equal(calls.length,0);
  let sendConsent=field("长原件.pdf 同意发送片段至模型");
  sendConsent.checked=true;sendConsent.listeners.change();assert.equal(button(extractTitle).disabled,false);
  const queryStart=field("长原件.pdf AI 核对期起始日");queryStart.listeners.input();
  assert.equal(sendConsent.checked,false);assert.equal(button(extractTitle).disabled,true);
  sendConsent.checked=true;sendConsent.listeners.change();
  field("企业名称（必填）").listeners.input();
  assert.equal(sendConsent.checked,false);assert.equal(button(extractTitle).disabled,true);
  const scopeSave=button("保存修改并重新分析").click();calls.shift().resolve(selectedPDF);await scopeSave;settleLists();await Promise.resolve();
  sendConsent=field("长原件.pdf 同意发送片段至模型");sendConsent.checked=true;sendConsent.listeners.change();
  const extracting=button(extractTitle).click(),extractCall=calls.shift();
  assert.equal(extractCall.url,"/api/enterprise/materials/batch/extract");
  assert.deepEqual(JSON.parse(extractCall.options.body),{expected_revision:1,document_id:"p",consent:true,
    period_start:"2026-01-01",period_end:"2026-01-31"});
  await button(extractTitle).click();assert.equal(calls.length,0);
  extractCall.resolve({...selectedPDF,revision:2,jobs:[{id:"pdf-job",state:"queued",input_revision:2,attempts:0,created_at:"today"}]});
  await extracting;settleLists();await Promise.resolve();
  assert.equal(button(extractTitle).disabled,true);assert.equal(button("确认材料并开始检测").disabled,true);
  const afterAI=controller.open("batch");calls.shift().resolve(selectedPDF);await afterAI;
  sendConsent=field("长原件.pdf 同意发送片段至模型");sendConsent.checked=true;sendConsent.listeners.change();
  const staleExtraction=button(extractTitle).click(),staleExtractCall=calls.shift();controller.reset();
  // The host clears rendered private data after resetting the controller.
  $("materialReview").replaceChildren();
  staleExtractCall.resolve(selectedPDF);await staleExtraction;assert.equal($("materialReview").children.length,0);
  for(const overrides of [{ready:false},{queue_enabled:false}]) {
    const disabledAI=controller.open("batch");calls.shift().resolve({...selectedPDF,ai:{...selectedPDF.ai,...overrides}});await disabledAI;
    sendConsent=field("长原件.pdf 同意发送片段至模型");sendConsent.checked=true;sendConsent.listeners.change();
    assert.equal(button(extractTitle).disabled,true);
    await button(extractTitle).click();assert.equal(calls.length,0);
  }
  const afterDisabled=controller.open("batch");calls.shift().resolve(sample());await afterDisabled;
  field("企业名称（必填）").value="手工企业";field("企业名称（必填）").listeners.input();
  assert.equal(button("确认材料并开始检测").disabled,true);
  assert.equal(field("补传材料").disabled,true);
  // Failed edits preserve the typed value. A retry sends only editable data.
  const failed=button("保存修改并重新分析").click();calls.shift().reject(new Error("save failed"));await failed;
  assert.equal(field("企业名称（必填）").value,"手工企业");assert.equal(errors.at(-1),"save failed");
  const saving=button("保存修改并重新分析").click(),write=calls.shift();
  const body=JSON.parse(write.options.body);assert.equal(body.company.name,"手工企业");
  assert.deepEqual(Object.keys(body).sort(),["company","expected_revision","selections"]);
  const modified={...sample(),revision:2,analysis_revision:2,company:{...sample().company,name:"手工企业"}};
  write.resolve(modified);await saving;settleLists();await Promise.resolve();
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Concurrent clicks cannot dispatch two confirmations; refresh after success.
  const confirming=button("确认材料并开始检测").click(),confirmation=calls.shift();
  await button("确认材料并开始检测").click();assert.equal(calls.length,0);
  confirmation.resolve({audit_id:"audit1"});await new Promise(r=>setImmediate(r));
  calls.shift().resolve({...modified,revision:3,executions:[{analysis_revision:2,audit_id:"audit1"}]});
  await confirming;settleLists();await Promise.resolve();
  assert.equal(results.length,1);assert.equal(button("确认材料并开始检测").disabled,true);
  // A 409 protects against stale confirmation; local fields remain for review.
  const reload=controller.open("batch");calls.shift().resolve(modified);await reload;
  const conflict=button("确认材料并开始检测").click();calls.shift().reject(Object.assign(new Error("stale"),{status:409}));await conflict;
  assert.equal(button("确认材料并开始检测").disabled,true);assert.equal(button("保存修改并重新分析").disabled,true);
  const recover=controller.open("batch");calls.shift().resolve(modified);await recover;
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Standard-cell edits are document-scoped, survive paging/filtering and
  // failures, and never send source grids or client-derived metrics.
  const standard=sample();
  const standardFields=Array.from({length:42},(_,i)=>({id:`利润表!B${i+2}`,table:"利润表",label:`项目${i}`,value:i===0?null:String(i)}));
  standard.documents=[{id:"d1",name:"本期.xlsx",kind:"xlsx",company:{},standard_fields:standardFields},
    {id:"d2",name:"另一表.xlsx",kind:"xlsx",company:{},standard_fields:[standardFields[0]]},
    {id:"legacy",name:"旧版.xlsx",kind:"xlsx",company:{},standard_fields:null}];
  const standardOpen=controller.open("batch");calls.shift().resolve(standard);await standardOpen;
  assert.ok(all().some(n=>n.textContent.includes("这是旧版解析材料")));
  const amount=title=>field(title+" 用于检测的值");
  amount("本期.xlsx 利润表!B2").value="0";amount("本期.xlsx 利润表!B2").listeners.input();
  assert.ok(all().some(n=>n.textContent.includes("匹配 42 个字段 · 本文件修正 1 项")));
  assert.equal(button("确认材料并开始检测").disabled,true);
  await button("下一页字段").click();
  amount("本期.xlsx 利润表!B43").value="900";amount("本期.xlsx 利润表!B43").listeners.input();
  const search=field("本期.xlsx 筛选账表字段");search.value="项目0";search.listeners.input();
  assert.equal(amount("本期.xlsx 利润表!B2").value,"0");
  const stdFailed=button("保存修改并重新分析").click(),stdWrite=calls.shift();
  const stdBody=JSON.parse(stdWrite.options.body);
  assert.deepEqual(stdBody.selections.d1.standard_edits,{"利润表!B2":"0","利润表!B43":"900"});
  assert.deepEqual(stdBody.selections.d2.standard_edits,{});
  assert.deepEqual(Object.keys(stdBody.selections.d1).sort(),["purpose","standard_edits"]);
  stdWrite.reject(new Error("standard save failed"));await stdFailed;
  assert.equal(amount("本期.xlsx 利润表!B2").value,"0");
  await button("恢复原文 · 利润表!B2").click();
  assert.equal(amount("本期.xlsx 利润表!B2").value,"");
  assert.ok(all().some(n=>n.textContent.includes("匹配 1 个字段 · 本文件修正 1 项")));
  const stdRetry=button("保存修改并重新分析").click(),stdRetryWrite=calls.shift();
  assert.deepEqual(JSON.parse(stdRetryWrite.options.body).selections.d1.standard_edits,{"利润表!B43":"900"});
  standard.selections.d1={standard_edits:{"利润表!B43":"900"}};
  standard.revision=standard.analysis_revision=2;stdRetryWrite.resolve(standard);await stdRetry;settleLists();await Promise.resolve();
  await button("下一页字段").click();assert.equal(amount("本期.xlsx 利润表!B43").value,"900");
  assert.equal(button("确认材料并开始检测").disabled,false);
  // Revoked material access clears displayed sensitive fields.
  const reference={batch_id:"batch",analysis_revision:1,confirmation_revision:2};
  const resultA={audit_id:"old-audit",material_reference:reference};
  const confirmed={...reference,audit_id:"old-audit",confirmed_by:"owner",confirmed_at:"then",current_revision:8,
    analysis_sha256:"digest",parser_version:"v2",company:{name:"old-company"},metrics:[{value:"100000"}],edits:[],checks:[],feedback:{},
    documents:[{name:"original.xlsx",fingerprint:"hash",purpose:"current",original_id:"file",material_company:{name:"old-company"}}],
    files:[{id:"file",name:"original.xlsx",deleted_at:"deleted-later"}]};
  const referenceButton=title=>all($("auditMaterials")).find(n=>n.tag==="button"&&n.textContent===title);
  controller.renderReference(resultA);
  const oldRead=referenceButton("查看当时确认的材料").click(),oldReadCall=calls.shift();
  controller.renderReference({...resultA,audit_id:"new-audit"});
  oldReadCall.resolve({confirmation:confirmed});await oldRead;
  assert.ok(!all($("auditMaterials")).some(n=>n.textContent.includes("old-company")));
  controller.renderReference(resultA);
  const readSnapshot=referenceButton("查看当时确认的材料").click();calls.shift().resolve({confirmation:confirmed});await readSnapshot;
  const historicalNodes=all($("auditMaterials"));
  assert.ok(historicalNodes.some(n=>n.textContent.includes("当前批次已到版本 8")));
  assert.ok(historicalNodes.some(n=>n.textContent.includes("原件当前已删除")));
  assert.ok(!historicalNodes.some(n=>n.tag==="input"||n.tag==="select"));
  assert.ok(!historicalNodes.some(n=>n.tag==="button"&&n.textContent.startsWith("下载确认原件")));
  const refused=referenceButton("查看当时确认的材料").click();calls.shift().reject(Object.assign(new Error("permission revoked"),{status:403}));await refused;
  assert.ok(!all($("auditMaterials")).some(n=>n.textContent.includes("old-company")));
  assert.ok(referenceButton("重试读取确认材料"));
  controller.renderReference({audit_id:"legacy"});
  assert.ok(all($("auditMaterials")).some(n=>n.textContent.includes("未保存材料确认版本关联")));
  assert.equal(calls.length,0);
  controller.renderReference(resultA);
  const resetRead=referenceButton("查看当时确认的材料").click(),resetCall=calls.shift();controller.reset();
  resetCall.resolve({confirmation:confirmed});await resetRead;
  assert.equal($("auditMaterials").children.length,0);
  const resume=controller.open("batch");calls.shift().resolve(modified);await resume;
  const revoked=button("保存修改并重新分析").click();calls.shift().reject(Object.assign(new Error("denied"),{status:403}));await revoked;
  assert.equal(field("企业名称（必填）"),undefined);
  assert.equal($("materialReview").hidden,false);assert.equal($("busy").style.display,"none");
  // Operation journal distinguishes rollback, committed-response failure and
  // interruption, never paints a late prior-session response, and can retry.
  const historyButton=text=>all($("enterpriseHistory")).find(n=>n.tag==="button"&&n.textContent===text);
  const journalList=controller.refresh();settleLists();await journalList;
  const journal=historyButton("查看材料操作与失败记录").click();
  const journalCall=calls.shift();assert.equal(journalCall.url,"/api/enterprise/material-operations");
  journalCall.resolve([{id:"failed",action:"upload",state:"failed",failure_code:"invalid_input",started_at:"now",actor_id:"owner"},
    {id:"committed",action:"upload",state:"committed",http_status:500,observed_at:"then",batch_id:"batch",started_at:"now",actor_id:"owner"},
    {id:"interrupted",action:"confirm",state:"started",started_at:"now",actor_id:"owner"}]);await journal;
  const journalNodes=all($("enterpriseHistory"));
  for(const text of ["本次操作未提交","业务已提交","未确认结果（处理中或中断）","不要据此重复上传"])
    assert.ok(journalNodes.some(n=>n.textContent.includes(text)),text);
  const journalFail=historyButton("查看材料操作与失败记录").click();calls.shift().reject(Object.assign(new Error("access denied"),{status:403}));await journalFail;
  assert.ok(!all($("enterpriseHistory")).some(n=>n.textContent.includes("请求 committed")));
  const retryJournal=historyButton("重试读取操作记录").click(),lateJournal=calls.shift();controller.reset();
  lateJournal.resolve([{id:"private-operation",action:"upload",state:"committed",started_at:"now"}]);await retryJournal;
  assert.ok(!all($("enterpriseHistory")).some(n=>n.textContent.includes("private-operation")));
  // Queued material UI observes small statuses and only reloads changed versions.
  const queued={...sample(),analysis_revision:null,jobs:[{id:"job",state:"queued",input_revision:1,attempts:0,created_at:"today"}]};
  const openQueue=controller.open("batch");calls.shift().resolve(queued);await openQueue;
  assert.equal(button("保存修改并重新分析").disabled,true);
  assert.equal(button("确认材料并开始检测").disabled,true);
  const tick=()=>{const [id,fn]=timers.entries().next().value;timers.delete(id);return fn();};
  const runningPoll=tick(),runningCall=calls.shift();assert.equal(runningCall.url,"/api/enterprise/materials/batch/status");
  runningCall.resolve({id:"batch",revision:1,jobs:[{...queued.jobs[0],state:"running",attempts:1}]});await runningPoll;
  assert.ok(all().some(n=>n.textContent.includes("分析中")));
  const donePoll=tick();calls.shift().resolve({id:"batch",revision:2,jobs:[{...queued.jobs[0],state:"done"}]});
  await new Promise(resolve=>setImmediate(resolve));
  const completed={...sample(),revision:2,analysis_revision:2,jobs:[{...queued.jobs[0],state:"done"}]};
  const completionRead=calls.shift();assert.equal(completionRead.url,"/api/enterprise/materials/batch");completionRead.resolve(completed);await donePoll;
  assert.equal(button("确认材料并开始检测").disabled,false);assert.equal(timers.size,0);
  const failedSupplement={...completed,revision:3,analysis_revision:2,
    jobs:[{id:"failed-supplement",state:"failed",input_revision:3,attempts:2,created_at:"today"},...completed.jobs]};
  const openFailure=controller.open("batch");calls.shift().resolve(failedSupplement);await openFailure;
  assert.equal(button("保存修改并重新分析").disabled,true);
  assert.equal(button("确认材料并开始检测").disabled,true);
  assert.equal(all().find(n=>n.tag==="fieldset").disabled,true);
  assert.ok(all().some(n=>n.textContent.includes("当前结果不可确认")));
  assert.ok(button("重试材料分析"));
  const reopenQueue=controller.open("batch");calls.shift().resolve(queued);await reopenQueue;
  const latePoll=tick(),lateStatus=calls.shift();controller.reset();lateStatus.resolve({id:"batch",revision:2,jobs:completed.jobs});await latePoll;
  assert.equal(calls.length,0);assert.equal(timers.size,0);
  // Client prefills preserve manual values, clear prior archive values and never reuse old dates.
  $("uploadCompanyName").value="手填企业";$("uploadCompanyName").listeners.input();
  const profileA=controller.selectClient("client-a"),responseA=calls.shift();
  assert.equal($("uploadScope").disabled,true);
  const profileB=controller.selectClient("client-b"),responseB=calls.shift();
  responseB.resolve({company:{name:"档案B",taxpayer_id:"TAX-B",region:"广东省",period_start:"2025-01-01"},analysis_count:1,history:{period:"2025"}});
  await profileB;
  responseA.resolve({company:{name:"旧档案A",taxpayer_id:"TAX-A"},analysis_count:0});await profileA;
  assert.equal($("uploadCompanyName").value,"手填企业");assert.equal($("uploadCompanyTaxId").value,"TAX-B");
  assert.equal($("uploadCompanyStart").value,"");assert.equal($("uploadScope").disabled,false);
  $("uploadCompanyMode").value="new";await controller.selectClient("");
  assert.equal($("uploadCompanyTaxId").value,"");assert.equal($("uploadCompanyName").value,"手填企业");
  assert.equal($("auditClient").disabled,true);
  const profileDenied=controller.selectClient("revoked");calls.shift().reject(Object.assign(new Error("access denied"),{status:403}));await profileDenied;
  assert.equal($("uploadCompanyTaxId").value,"");assert.ok($("uploadProfileHint").textContent.includes("未读取"));
  // A prior tenant's material-list response cannot leak after a role switch.
  const listing=controller.refresh();actor={id:"other",role:"teacher"};
  calls.shift().resolve([{id:"private",revision:1,created_at:"today"}]);calls.shift().resolve({retention_ready:true,message:"ready"});await listing;
  await controller.refresh();assert.equal($("enterpriseHistory").hidden,true);assert.equal($("enterpriseHistory").children.length,0);
  assert.equal(controller.enabled(),false);
  controller.reset();assert.equal($("uploadCompanyName").value,"");assert.equal($("auditClient").value,"");
  console.log("Enterprise frontend: routing, standard-cell review/paging/restore, dirty guard, failed save, retry, idempotent dispatch, stale response, version conflict and revoked access passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
