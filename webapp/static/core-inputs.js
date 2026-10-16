"use strict";
function createCoreInputs({api, el, $, user, showError, continueCheck=()=>{}}) {
  const base = "/api/enterprise/input-tasks";
  let current = null, busy = false, dirty = false, generation = 0;
  let formOwner = user()?.id;
  const enabled = () => ["org_admin", "accountant"].includes(user()?.role);
  const states = {needs_completion:"待补充或核对",ready_for_analysis:"资料已就绪，可继续核查",parsing:"正在解析"};
  const kinds = {account_balance:"科目余额表",vat_declaration:"增值税申报表",invoice_detail:"发票明细"};
  const json = value => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
  const button = (text, fn) => {const n=el("button","btn re",text);n.type="button";n.onclick=fn;return n;};
  function clearScope(){for(const id of ["coreCompanyName","coreTaxNo","coreStart","coreEnd"])$(id).value="";}
  function reset() {generation++;current=null;busy=dirty=false;$("coreTaskResult").replaceChildren();$("coreTaskHistory").replaceChildren();$("coreTaskForm").disabled=false;if(formOwner!==user()?.id||!enabled())clearScope();formOwner=user()?.id;}
  async function action(fn, saving=false) {
    if(busy||!enabled())return;
    if(dirty&&!saving){showError("存在未保存的资料口径核对，请先保存后再补传、创建或读取任务。");return;}
    busy=true;$("coreTaskForm").disabled=true;$("coreTaskResult").querySelectorAll("button,input,select").forEach(n=>n.disabled=true);
    const owner=user()?.id, ticket=generation;
    try {const result=await fn();if(owner===user()?.id&&ticket===generation&&enabled()){current=result;dirty=false;render();await history();}}
    catch(error){if(owner===user()?.id&&ticket===generation)showError(error.message);}
    finally{if(ticket===generation){busy=false;$("coreTaskForm").disabled=false;$("coreTaskResult").querySelectorAll("fieldset,button,input,select").forEach(n=>n.disabled=false);}}
  }
  $("coreCreateTask").onclick=()=>action(()=>api(base,json({company_name:$("coreCompanyName").value,tax_no:$("coreTaxNo").value,
    start_date:$("coreStart").value,end_date:$("coreEnd").value})));
  function select(label, choices, value) {
    const wrap=el("label","",label),input=el("select");input.setAttribute("aria-label",label);
    for(const [v,name] of choices){const o=el("option","",name);o.value=v;input.append(o);}input.value=value||"";wrap.append(input);return {wrap,input};
  }
  function field(label, type, value) {const wrap=el("label","",label),input=el("input");input.type=type;input.value=value||"";input.setAttribute("aria-label",label);wrap.append(input);return {wrap,input};}
  function render() {
    const root=$("coreTaskResult");root.replaceChildren();if(!current)return;
    const task=current.task,p=current.processing;
    root.append(el("h3","",`${task.company_name} · ${task.tax_no}`),el("p","muted",`${task.start_date} 至 ${task.end_date} · 版本 ${current.revision}`),
      el("p",p.status==="ready_for_analysis"?"":"material-note",states[p.status]||p.status),el("p","muted",p.boundary));
    root.append(button("重新读取任务",()=>action(()=>api(base+"/"+current.id))));
    const next=button("继续核查",()=>{if(busy)return;if(dirty){showError("请先保存资料口径核对，再继续核查。");return;}continueCheck(current.id);});
    root.append(next,el("p","muted","带入本任务企业、期间及已保存资料；缺口会在相应核查项目中保留。"));
    const saved=Object.values(current.tax_checks||{});
    if(saved.length)root.append(el("p","material-note",`已保存 ${saved.length} 项核查；${saved.filter(s=>s.stale).length} 项因资料变化待重检。点击继续核查可查看结果和报告。`));
    const slots=el("div","core-slots");
    for(const [kind,name] of Object.entries(kinds)) {
      const card=el("section","material-card");card.append(el("h4","",name+"（必传）"));
      card.append(el("p","muted",kind==="account_balance"?"Excel 科目汇总余额；不是逐笔明细账。":kind==="vat_declaration"?"Excel / PDF 主表及适用附表，可多文件合并期间。":"Excel / 数电 XML，须覆盖进销项；按开具日期统计。"));
      const pick=el("input");pick.type="file";pick.multiple=true;pick.accept=kind==="account_balance"?".xlsx,.xls":kind==="vat_declaration"?".xlsx,.xls,.pdf":".xlsx,.xls,.xml";pick.setAttribute("aria-label",name+"上传");
      pick.onchange=()=>{const files=Array.from(pick.files);if(!files.length)return;return action(()=>{const form=new FormData();form.append("material_type",kind);form.append("expected_revision",current.revision);form.append("mapping_mode","ai");form.append("ocr",kind==="vat_declaration"?"1":"0");files.forEach(f=>form.append("files",f));return api(base+"/"+current.id+"/files",{method:"POST",body:form});});};
      pick.disabled=busy;card.append(pick,el("p","muted",`已保存 ${current.documents.filter(d=>d.material_type===kind).length} 份。`));slots.append(card);
    }
    root.append(slots);
    const excluded=(current.files||[]).filter(file=>current.source_options[file.id]?.excluded&&!file.deleted_at);
    if(excluded.length){const box=el("section","material-card");box.append(el("h4","","已排除的资料（原件保留，可恢复）"));for(const file of excluded){const options=current.source_options[file.id];box.append(el("p","muted",options.original_name),button("恢复资料："+options.original_name,()=>action(()=>api(base+"/"+current.id+"/prepare",json({expected_revision:current.revision,source_options:{[file.id]:{excluded:false}},mapping_mode:"ai"})))));}root.append(box);}
    const review=el("fieldset");review.disabled=busy;
    review.addEventListener("input",()=>{dirty=true;});review.addEventListener("change",()=>{dirty=true;});
    review.append(el("legend","","资料口径核对"),el("p","muted","仅填写已核实的信息，留空沿用文件识别值。填写的是人工确认信息，不会改写原件或声称文件已经证明。一个文件的统一口径会应用到其所有工作表；混合期间请保留原识别值。"));
    const applicable=field("本期适用附表名称（以分号分隔）","text",(task.applicable_forms||[]).join("；"));
    const scopeKnown=field("已核实本期适用附表清单（如无适用附表，也须明确确认）","checkbox","");scopeKnown.input.checked=task.applicable_forms!==null;
    review.append(applicable.wrap,scopeKnown.wrap);
    const editors=[];
    for(const doc of current.documents){
      const card=el("section","material-card"),options=current.source_options[doc.id],meta=options.confirmed_metadata||{};
      card.append(el("h4","",doc.name),el("p","muted",`${kinds[doc.material_type]} · ${doc.records.length} 条记录 · SHA-256 ${doc.sha256}`));
      const download=el("a","","下载原件");download.href=base+"/"+current.id+"/originals/"+doc.id;card.append(download);
      const grid=el("div","material-company");
      const unit=select(doc.name+" 确认单位",[["","沿用识别值"],["元","元"],["千元","千元"],["万元","万元"],["亿元","亿元"]],meta.unit);
      const start=field(doc.name+" 确认覆盖起始日","date",meta.period_start),end=field(doc.name+" 确认覆盖终止日","date",meta.period_end);
      const form=select(doc.name+" 确认表单类型",[["","沿用识别值"],["main","主表"],["supplement","附表"]],meta.form_type);
      const direction=select(doc.name+" 确认发票方向",[["","沿用识别值"],["input","进项"],["output","销项"]],meta.direction);
      grid.append(unit.wrap,start.wrap,end.wrap);if(doc.material_type==="vat_declaration")grid.append(form.wrap);if(doc.material_type==="invoice_detail")grid.append(direction.wrap);
      const exclude=field(doc.name+" 排除此资料（原件与历史版本保留）","checkbox","");exclude.input.checked=!!options.excluded;grid.append(exclude.wrap);card.append(grid);
      const reviewed=field(doc.name+" 已对照原页核实OCR候选的栏次、金额、税号和所属期","checkbox","");reviewed.input.checked=!!options.source_reviewed;
      const ocr=field(doc.name+" 允许未识别PDF页交给配置模型OCR（发送页图）","checkbox","");ocr.input.checked=!!options.ocr;
      if(doc.ocr_candidate)card.append(reviewed.wrap);if(doc.material_type==="vat_declaration"&&doc.name.toLowerCase().endsWith(".pdf"))card.append(ocr.wrap);
      for(const f of [unit,start,end,form,direction])f.input.addEventListener("change",()=>{reviewed.input.checked=false;});
      const parts=el("details"),summary=el("summary","","识别到的来源口径");parts.append(summary);
      for(const part of doc.parts)parts.append(el("p","muted",`${part.name} · ${part.form_type} · ${part.period?.join(" 至 ")||"期间未识别"} · ${part.unit||"单位未识别"}`));card.append(parts);
      review.append(card);editors.push({id:doc.id,unit,start,end,form,direction,exclude,reviewed,ocr});
    }
    const save=button("保存核对并重新整理资料",()=>action(()=>{const options={};for(const e of editors)options[e.id]={excluded:e.exclude.input.checked,source_reviewed:e.reviewed.input.checked,ocr:e.ocr.input.checked,confirmed_metadata:{unit:e.unit.input.value,period_start:e.start.input.value,period_end:e.end.input.value,form_type:e.form.input.value,direction:e.direction.input.value}};
      return api(base+"/"+current.id+"/prepare",json({expected_revision:current.revision,applicable_forms:scopeKnown.input.checked?applicable.input.value.split(/[;；]/).map(s=>s.trim()).filter(Boolean):null,source_options:options,mapping_mode:"ai"}));},true));review.append(save);root.append(review);
    const problems=el("section","enterprise-feedback"+(p.issues.length?" blocking":""));problems.append(el("h4","",`资料缺口与冲突（${p.issues.length}）`));
    for(const item of p.issues)problems.append(el("p","",item.message+(item.source_position?" · "+item.source_position:"")));root.append(problems);
    if(p.notices.length){const notes=el("details");notes.append(el("summary","",`来源说明（${p.notices.length}）`));for(const n of p.notices)notes.append(el("p","muted",n.message+(n.source_position?" · "+n.source_position:"")));root.append(notes);}
    const evidence=el("details");evidence.append(el("summary","",`标准记录与字段来源（${p.records.length}）`));
    for(const r of p.records.slice(0,100)){const card=el("section","material-card");card.append(el("p","",`${kinds[r.material_type]} · ${r.source_position} · ${r.in_scope?"检查区间内":"区间外保留"}${r.duplicate_of?" · 重复来源":""}`));const raw=el("pre","",JSON.stringify(r,null,2));raw.style.whiteSpace="pre-wrap";raw.style.overflowWrap="anywhere";card.append(raw);evidence.append(card);}if(p.records.length>100)evidence.append(el("p","muted","页面预览前100条；下载整理结果包含全部记录。"));root.append(evidence);
    const exportButton=button("下载整理结果 JSON",()=>{const blob=new Blob([JSON.stringify(current,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download="基础资料整理-"+current.id+".json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});root.append(exportButton);
    const trace=el("details");trace.append(el("summary","","版本与操作记录"));for(const v of current.versions)trace.append(el("p","muted",`版本 ${v.revision} · ${v.kind} · ${v.created_at}`));root.append(trace);
  }
  async function history(){const owner=user()?.id,ticket=generation,rows=await api(base);if(owner!==user()?.id||ticket!==generation||!enabled())return;const root=$("coreTaskHistory");root.replaceChildren(el("h3","","继续资料处理任务"));for(const row of rows)root.append(button(`${row.task.company_name} · ${row.task.start_date} 至 ${row.task.end_date} · ${states[row.status]||row.status}`,()=>action(()=>api(base+"/"+row.id))));}
  async function refresh(){const active=enabled();if(formOwner!==user()?.id){reset();clearScope();formOwner=user()?.id;}$("coreInputs").hidden=!active;$("coreLegacyToggle").hidden=!active;if(active)await history();}
  async function openTask(id){return action(()=>api(base+"/"+encodeURIComponent(id)));}
  return {refresh,reset,openTask};
}
