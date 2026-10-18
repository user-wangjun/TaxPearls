"use strict";
function createCoreInputs({api, el, $, user, showError, continueCheck=()=>{}, policyUI=()=>null}) {
  const base = "/api/enterprise/input-tasks";
  let current = null, busy = false, dirty = false, generation = 0, policyCatalog=null;
  let formOwner = user()?.id, clients=[], scopeDirty=false, attempted=false;
  const scopeIds={company_name:"coreCompanyName",tax_no:"coreTaxNo",start_date:"coreStart",end_date:"coreEnd"};
  const scopeRead=()=>Object.fromEntries(Object.entries(scopeIds).map(([key,id])=>[key,$(id).value.trim()]));
  const scopeWrite=()=>{for(const [key,id] of Object.entries(scopeIds))$(id).value=current.task[key]||"";$("coreClient").value=clients.some(c=>c.id===current.task.company_id)?current.task.company_id:"";scopeDirty=false;};
  const fieldErrors={};
  for(const [key,id] of Object.entries(scopeIds)){
    const error=el("small","core-field-error");error.id=id+"Error";error.hidden=true;$(id).setAttribute("aria-describedby",error.id);$(id).after?.(error);fieldErrors[key]=error;
    $(id).addEventListener("input",()=>{scopeDirty=true;const errors=validateScope();displayErrors(attempted?errors:Object.fromEntries(Object.entries(errors).filter(([field])=>scopeRead()[field])),attempted?Object.keys(scopeIds):key==="start_date"?[key,"end_date"]:[key]);});
  }
  function validateScope(){const value=scopeRead(),errors={};for(const [key,label] of Object.entries({company_name:"企业名称",tax_no:"税号",start_date:"检查起始日",end_date:"检查终止日"}))if(!value[key])errors[key]="请填写"+label+"，或从材料读取后核对。";
    if(value.tax_no&&!/^[A-Za-z0-9]{8,20}$/.test(value.tax_no.replace(/\s/g,"")))errors.tax_no="税号须为8至20位字母或数字。";
    const selected=clients.find(c=>c.id===$("coreClient").value);if(selected&&value.tax_no.replace(/\s/g,"").toUpperCase()!==selected.taxpayer_id.replace(/\s/g,"").toUpperCase())errors.tax_no="所选企业与填写税号不一致，请核对或切换为新企业。";
    if(value.start_date&&value.end_date&&value.start_date>value.end_date)errors.end_date="检查终止日不能早于起始日。";return errors;}
  function displayErrors(errors,keys=Object.keys(scopeIds)){for(const key of keys){const message=errors[key]||"";fieldErrors[key].textContent=message;fieldErrors[key].hidden=!message;$(scopeIds[key]).setAttribute("aria-invalid",String(!!message));}}
  $("coreClient").onchange=()=>{const company=clients.find(c=>c.id===$("coreClient").value);$("coreCompanyName").value=company?.name||"";$("coreTaxNo").value=company?.taxpayer_id||"";scopeDirty=true;displayErrors(attempted?validateScope():{});};
  async function saveScope(confirm=false){const owner=user()?.id,ticket=generation;const result=await api(base+"/"+current.id+"/scope",json({...scopeRead(),client_id:$("coreClient").value||null,expected_revision:current.revision,confirm}));if(owner!==user()?.id||ticket!==generation)throw new Error("会话已变化，请重新读取任务。");current=result;scopeDirty=false;return result;}
  async function startCheck(){if(busy)return;if(dirty){showError("请先保存资料口径核对，再开始检查。");return;}attempted=true;const errors=validateScope();displayErrors(errors);if(Object.keys(errors).length){$(Object.entries(scopeIds).find(([key])=>errors[key])[1]).focus?.();return;}const owner=user()?.id,ticket=generation;const saved=await action(()=>saveScope(true),true);if(!saved||owner!==user()?.id||ticket!==generation)return;displayErrors(current.scope_errors||{});if(!Object.keys(current.scope_errors||{}).length&&current.task.scope_confirmed!==false)continueCheck(current.id);}

  const enabled = () => ["org_admin", "accountant"].includes(user()?.role);
  const states = {needs_completion:"待补充或核对",ready_for_analysis:"资料已就绪，可继续核查",parsing:"正在解析"};
  const kinds = {account_balance:"科目余额表",vat_declaration:"增值税申报表",invoice_detail:"发票明细"};
  const json = value => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
  const button = (text, fn) => {const n=el("button","btn re",text);n.type="button";n.onclick=fn;return n;};
  function clearScope(){for(const id of Object.values(scopeIds))$(id).value="";$("coreClient").value="";scopeDirty=attempted=false;displayErrors({});}
  function reset() {generation++;current=null;busy=dirty=scopeDirty=attempted=false;displayErrors({});clients=[];$("coreClient").replaceChildren();$("coreTaskResult").replaceChildren();$("coreTaskHistory").replaceChildren();$("coreTaskForm").disabled=false;if(formOwner!==user()?.id||!enabled())clearScope();formOwner=user()?.id;}
  async function action(fn, saving=false) {
    if(busy||!enabled())return;
    if((dirty||scopeDirty&&current)&&!saving){showError("存在未保存的资料口径核对，请先保存后再补传、创建或读取任务。");return;}
    busy=true;$("coreTaskForm").disabled=true;$("coreTaskResult").querySelectorAll("button,input,select").forEach(n=>n.disabled=true);
    const owner=user()?.id, ticket=generation, previousId=current?.id;
    try {const result=await fn();if(owner===user()?.id&&ticket===generation&&enabled()){if(previousId!==result.id)attempted=false;current=result;dirty=false;scopeWrite();render();await history();return true;}}
    catch(error){if(owner===user()?.id&&ticket===generation)showError(error.message);}
    finally{if(ticket===generation){busy=false;$("coreTaskForm").disabled=false;$("coreTaskResult").querySelectorAll("fieldset,button,input,select").forEach(n=>n.disabled=!!n.corePageDisabled);}}
  }
  $("coreCreateTask").onclick=()=>{if(dirty){showError("请先保存资料口径核对。");return;}attempted=false;displayErrors({});return action(()=>api(base,json({...scopeRead(),client_id:$("coreClient").value||null})),true);};
  function select(label, choices, value, caption=label) {
    const wrap=el("label","field",caption),input=el("select");input.setAttribute("aria-label",label);
    for(const [v,name] of choices){const o=el("option","",name);o.value=v;input.append(o);}input.value=value||"";wrap.append(input);return {wrap,input};
  }
  function field(label, type, value, caption=label) {const wrap=el("label",type==="checkbox"?"core-check":"field"),input=el("input");input.type=type;input.value=value||"";input.setAttribute("aria-label",label);if(type==="checkbox")wrap.append(input,el("span","",caption));else{wrap.append(el("span","",caption),input);}return {wrap,input};}
  // Format decimal strings without passing through Number (invoice identifiers and amounts can be long).
  function amount(value) {if(value===null||value===undefined||value==="")return "待识别";return String(value).replace(/^([-+]?\d+)(\.\d+)?$/,(_,whole,fraction)=>whole.replace(/\B(?=(\d{3})+(?!\d))/g,",")+(fraction||""));}
  function recordTable(doc, options, reviewed, statuses, prepared) {
    const wrap=el("div","core-records"),scroll=el("div","core-table-scroll"),table=el("table","core-record-table"),head=el("thead"),header=el("tr"),body=el("tbody");
    const invoice=doc.material_type==="invoice_detail",editable=invoice&&doc.name.toLowerCase().endsWith(".pdf");
    const columns=invoice?[["票号 / 来源","invoice_no"],["开票日期","issue_date"],["购销方向","direction"],["不含税金额","amount_excluding_tax"],["税额","tax_amount"],["价税合计","amount_including_tax"]]:doc.material_type==="account_balance"?[["科目 / 来源","account_name"],["期初余额","opening_balance"],["本期借方","current_debit"],["本期贷方","current_credit"],["期末余额","closing_balance"]]:[["项目 / 来源","item_label"],["申报金额","declared_value"]];
    if(invoice&&!editable)columns.push(["红蓝字状态","red_letter_status"],["作废状态","void_status"]);
    const numeric=new Set(["amount_excluding_tax","tax_amount","amount_including_tax","opening_balance","current_debit","current_credit","closing_balance","declared_value"]);
    const labels=[...columns.map(([name])=>name),...(editable?["红蓝字状态","作废状态"]:[])];
    for(const [index,name] of labels.entries()){const th=el("th",numeric.has(columns[index]?.[1])?"core-number":"",name);th.setAttribute("scope","col");header.append(th);}head.append(header);table.append(head,body);table.setAttribute("aria-label",doc.name+" 解析候选记录");scroll.append(table);wrap.append(scroll);
    const rows=doc.records.map(record=>{
      const row=el("tr");
      for(const [index,[name,key]] of columns.entries()){
        let value=key==="direction"?(prepared.get(record.id)?.direction??record.direction):record[key];
        if(key==="direction")value=({input:"进项",output:"销项","进项":"进项","销项":"销项"})[value]||"待核对";
        const cell=el("td",numeric.has(key)?"core-number":"");cell.setAttribute("data-label",name);cell.append(el("span","",numeric.has(key)?amount(value):value===null||value===undefined||value===""?"待识别":String(value)));
        if(index===0){const source=el("details","core-record-source");source.append(el("summary","","查看来源"),el("p","muted",record.source_position||"来源位置待识别"));if(invoice)source.append(el("p","muted",`销售方税号：${record.seller_tax_no||"待识别"}`),el("p","muted",`购买方税号：${record.buyer_tax_no||"待识别"}`));cell.append(source);}row.append(cell);
      }
      if(editable){
        const prefix=doc.name+" · "+record.source_position+" · "+(record.invoice_no||"票号待核对"),confirmed=options.invoice_statuses?.[record.id]||{},parsed=record.parsed_invoice_statuses||record;
        const red=select(prefix+" 红蓝字状态",[["","待核实"],["蓝字","蓝字"],["红字","红字"]],confirmed.red_letter_status||parsed.red_letter_status,"");
        const voided=select(prefix+" 作废状态",[["","待核实"],["正常","正常"],["未作废","未作废"],["作废","作废"],["已作废","已作废"]],confirmed.void_status||parsed.void_status,"");
        for(const [i,f] of [red,voided].entries()){const cell=el("td");cell.setAttribute("data-label",labels[columns.length+i]);cell.append(f.wrap);row.append(cell);f.input.addEventListener("change",()=>{reviewed.input.checked=false;reviewed.updateState();});}statuses.push({id:record.id,red,voided});
      }
      return row;
    });
    let page=0;const size=10,pages=Math.max(1,Math.ceil(rows.length/size)),pager=el("div","core-pagination"),description=el("span","muted"),previous=button("上一页",()=>{page--;draw();}),next=button("下一页",()=>{page++;draw();});description.setAttribute("aria-live","polite");
    function draw(){body.replaceChildren(...rows.slice(page*size,(page+1)*size));description.textContent=rows.length?`${page*size+1}–${Math.min((page+1)*size,rows.length)} / ${rows.length} 条 · 第 ${page+1} / ${pages} 页`:"尚未识别到记录，请查看来源说明并核对原件。";previous.disabled=previous.corePageDisabled=page===0;next.disabled=next.corePageDisabled=page>=pages-1;}
    pager.append(description);if(pages>1)pager.append(previous,next);wrap.append(pager);draw();return wrap;
  }
  function render() {
    const root=$("coreTaskResult");root.replaceChildren();if(!current)return;
    const task=current.task,p=current.processing,prepared=new Map(p.records.map(r=>[r.id,r])),parsedCount=current.documents.reduce((sum,d)=>sum+d.records.length,0),scopePending=Object.keys(current.scope_errors||{}).length>0,recordsPending=scopePending&&p.records.length===0;
    root.append(el("h3","",`${task.company_name||"待识别企业"} · ${task.tax_no||"税号待填写"}`),el("p","muted",`${task.start_date||"起始日待确认"} 至 ${task.end_date||"终止日待确认"} · 版本 ${current.revision}`),
      el("p",p.status==="ready_for_analysis"?"":"material-note",states[p.status]||p.status),el("p","muted",p.boundary));
    const actions=el("div","core-actions");actions.append(button("重新读取任务",()=>action(()=>api(base+"/"+current.id))));
    actions.append(button("保存企业与检查区间",()=>{if(dirty){showError("请先保存资料口径核对，再保存企业与检查区间。");return;}return action(()=>saveScope(),true);}));
    const next=button("开始检查",startCheck);actions.append(next);root.append(actions,el("p","muted","先核对企业与检查区间；字段错误会阻止开始检查和 AI 审查。"));
    const origins=task.scope_origins||{};for(const [key,origin] of Object.entries(origins))if(origin.kind==="file")root.append(el("p","muted",`${{company_name:"企业名称",tax_no:"税号",start_date:"起始日",end_date:"终止日"}[key]}已从文件读取：${(origin.sources||[]).map(s=>`${current.documents.find(d=>d.id===s.file_id)?.name||s.file_id} · ${s.position}`).join("；")}，请核对后开始检查。`));
    displayErrors(attempted?current.scope_errors||{}:Object.fromEntries(Object.entries(current.scope_errors||{}).filter(([key])=>scopeRead()[key])));
    const saved=Object.values(current.tax_checks||{});
    if(saved.length)root.append(el("p","material-note",`已保存 ${saved.length} 项核查；${saved.filter(s=>s.stale).length} 项因资料变化待重检。点击继续核查可查看结果和报告。`));
    const slots=el("div","core-slots");
    for(const [kind,name] of Object.entries(kinds)) {
      const card=el("section","material-card");card.append(el("h4","",name+"（必传）"));
      card.append(el("p","muted",kind==="account_balance"?"Excel / PDF 科目汇总余额；不是逐笔明细账。":kind==="vat_declaration"?"Excel / PDF 主表及适用附表，可多文件合并期间。":"Excel / PDF 发票 / 数电 XML，须覆盖进销项；按开具日期统计。"));
      const pick=el("input");pick.type="file";pick.multiple=true;pick.accept=kind==="invoice_detail"?".xlsx,.xls,.xml,.pdf":".xlsx,.xls,.pdf";pick.setAttribute("aria-label",name+"上传");
      pick.onchange=()=>{const files=Array.from(pick.files);if(!files.length)return;if(dirty){showError("请先保存资料口径核对，再补传资料。");return;}return action(async()=>{if(scopeDirty)await saveScope();const form=new FormData();form.append("material_type",kind);form.append("expected_revision",current.revision);form.append("mapping_mode","ai");form.append("ocr","1");files.forEach(f=>form.append("files",f));return api(base+"/"+current.id+"/files",{method:"POST",body:form});},true);};
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
    const formScope=el("details","core-metadata");formScope.append(el("summary","","申报附表适用范围（核对申报表时填写）"),applicable.wrap,scopeKnown.wrap);
    review.append(el("p","core-candidate-count",`已解析候选 ${parsedCount} 条`),el("p","muted",scopePending?"先核对下方文件候选，并补全企业与检查区间，再整理标准记录；候选条数不代表资料已齐全。":"下方按文件展示解析候选，尚需核对原件；标准记录、重复来源和检查区间以整理结果为准。"));
    const editors=[];
    for(const doc of current.documents){
      const card=el("details","core-document"),options=current.source_options[doc.id]||{},meta=options.confirmed_metadata||{};
      card.open=editors.length===0;const heading=el("summary","core-document-heading");heading.append(el("span","core-file-name",doc.name),el("span","core-file-state",`${kinds[doc.material_type]} · ${doc.records.length} 条候选 · ${options.source_reviewed?"已人工核对":"待核对"}`));card.append(heading);
      const content=el("div","core-document-body"),download=el("a","btn re","下载原件");download.href=base+"/"+current.id+"/originals/"+doc.id;content.append(download);card.append(content);
      const metadata=el("details","core-metadata");metadata.append(el("summary","","文件口径与排除设置（选填）"),el("p","muted","留空沿用识别值；统一口径应用到本文件所有记录，混合期间请保留原识别值。"));
      const grid=el("div","material-company");
      const unit=select(doc.name+" 确认单位",[["","沿用识别值"],["元","元"],["千元","千元"],["万元","万元"],["亿元","亿元"]],meta.unit,"金额单位");
      const start=field(doc.name+" 确认覆盖起始日","date",meta.period_start,"覆盖起始日"),end=field(doc.name+" 确认覆盖终止日","date",meta.period_end,"覆盖终止日");
      const form=select(doc.name+" 确认表单类型",[["","沿用识别值"],["main","主表"],["supplement","附表"]],meta.form_type,"表单类型");
      const direction=select(doc.name+" 确认发票方向",[["","沿用识别值"],["input","进项"],["output","销项"]],meta.direction,"发票方向");
      grid.append(unit.wrap,start.wrap,end.wrap);if(doc.material_type==="vat_declaration")grid.append(form.wrap);if(doc.material_type==="invoice_detail")grid.append(direction.wrap);
      const exclude=field(doc.name+" 排除此资料（原件与历史版本保留）","checkbox","","排除此资料（原件与历史版本保留）");exclude.input.checked=!!options.excluded;metadata.append(grid,exclude.wrap);content.append(metadata);
      const reviewed=field(doc.name+" 已对照原页核实提取候选的字段、金额、购销方和所属期","checkbox","","已对照原页核实字段、金额、购销方和所属期");reviewed.input.checked=!!options.source_reviewed;
      reviewed.updateState=()=>{heading.children[1].textContent=`${kinds[doc.material_type]} · ${doc.records.length} 条候选 · ${reviewed.input.checked?"已人工核对":"待核对"}`;};reviewed.input.addEventListener("change",reviewed.updateState);
      const ocr=field(doc.name+" 允许未识别PDF页交给配置模型OCR（发送页图）","checkbox","","允许未识别的 PDF 页交给已配置模型 OCR（发送页图）");ocr.input.checked=!!options.ocr;
      const statuses=[];
      if(doc.material_type==="invoice_detail"&&doc.name.toLowerCase().endsWith(".pdf")){
        content.append(el("p","material-note","票面未记载的红蓝字、作废状态须逐票核实；留空会保留资料缺口。单张发票不能证明整期进销项齐全。"));
      }
      content.append(el("p","muted","金额按解析候选的原单位展示，不在此处换算或汇总；单位及来源口径见下方详情。"),recordTable(doc,options,reviewed,statuses,prepared));
      if(doc.ocr_candidate||doc.pdf_candidate)content.append(reviewed.wrap);if(doc.name.toLowerCase().endsWith(".pdf"))content.append(ocr.wrap);
      for(const f of [unit,start,end,form,direction])f.input.addEventListener("change",()=>{reviewed.input.checked=false;reviewed.updateState();});
      const parts=el("details"),summary=el("summary","","识别到的来源口径");parts.append(summary);
      parts.append(el("p","core-checksum",`SHA-256 ${doc.sha256}`));
      for(const part of doc.parts)parts.append(el("p","muted",`${part.name} · ${{main:"主表",supplement:"附表"}[part.form_type]||"表单类型待核对"} · ${part.period?.join(" 至 ")||"期间未识别"} · ${part.unit||"单位未识别"}`));content.append(parts);
      review.append(card);editors.push({id:doc.id,unit,start,end,form,direction,exclude,reviewed,ocr,statuses});
    }
    review.append(formScope);
    let readPolicy=null;
    if(policyCatalog&&policyUI()){
      const policy=el("details","core-policy"),panel=el("div");policy.append(el("summary","","政策条件（选填）"),panel);review.append(policy);
      readPolicy=policyUI().editor(panel,policyCatalog,current.policy_input,current.policy_assessment,()=>{dirty=true;},()=>{dirty=true;},()=>{});
      panel.append(el("p","muted",`目录来源核对截至 ${policyCatalog.checked_as_of}；政策条件与本任务资料版本绑定，单独复核，不替代知识图谱的规则审核。`));
      if(current.policy_error)panel.append(el("p","material-note",current.policy_error));
    }
    const save=button("保存核对并重新整理资料",()=>{const policyInput=readPolicy?.();return action(async()=>{if(scopeDirty)await saveScope();const options={};for(const e of editors)options[e.id]={excluded:e.exclude.input.checked,source_reviewed:e.reviewed.input.checked,ocr:e.ocr.input.checked,invoice_statuses:Object.fromEntries(e.statuses.map(s=>[s.id,{red_letter_status:s.red.input.value,void_status:s.voided.input.value}])),confirmed_metadata:{unit:e.unit.input.value,period_start:e.start.input.value,period_end:e.end.input.value,form_type:e.form.input.value,direction:e.direction.input.value}};
      return api(base+"/"+current.id+"/prepare",json({expected_revision:current.revision,applicable_forms:scopeKnown.input.checked?applicable.input.value.split(/[;；]/).map(s=>s.trim()).filter(Boolean):null,source_options:options,mapping_mode:"ai",...(readPolicy?{policy_input:policyInput}:{})}));},true);});review.append(save);root.append(review);
    const problems=el("section","enterprise-feedback"+(p.issues.length?" blocking":""));problems.append(el("h4","",scopePending?"资料缺口与冲突（待核对检查范围）":`资料缺口与冲突（${p.issues.length}）`));if(scopePending)problems.append(el("p","","企业或检查区间尚未完整，资料完整性与区间冲突检查尚未完成；请先补全并保存范围。"));
    const issueGroups=new Map();for(const item of p.issues){const key=JSON.stringify([item.code,item.message]);if(!issueGroups.has(key))issueGroups.set(key,[]);issueGroups.get(key).push(item);}
    for(const items of issueGroups.values()){
      const locations=items.map(item=>[current.documents.find(d=>d.id===item.source_file_id)?.name,item.source_position].filter(Boolean).join(" · "));
      if(items.length===1)problems.append(el("p","",[items[0].message,locations[0]].filter(Boolean).join(" · ")));
      else{const group=el("details");group.append(el("summary","",`${items[0].message}（${items.length} 处）`));for(const location of locations)group.append(el("p","muted",location||"任务整体口径"));problems.append(group);}
    }root.append(problems);
    if(p.notices.length){const notes=el("details");notes.append(el("summary","",`来源说明（${p.notices.length}）`));for(const n of p.notices)notes.append(el("p","muted",n.message+(n.source_position?" · "+n.source_position:"")));root.append(notes);}
    const evidence=el("details");evidence.append(el("summary","",recordsPending?`标准记录与字段来源（待整理；已解析候选 ${parsedCount} 条）`:`标准记录与字段来源（${p.records.length}）`));if(recordsPending)evidence.append(el("p","muted","解析候选已按文件展示在上方；补全范围并重新整理后，此处显示标准记录及字段来源。"));
    for(const r of p.records.slice(0,100)){const card=el("section","material-card");card.append(el("p","",`${kinds[r.material_type]} · ${r.source_position} · ${r.in_scope?"检查区间内":"区间外保留"}${r.duplicate_of?" · 重复来源":""}`));const raw=el("pre","",JSON.stringify(r,null,2));raw.style.whiteSpace="pre-wrap";raw.style.overflowWrap="anywhere";card.append(raw);evidence.append(card);}if(p.records.length>100)evidence.append(el("p","muted","页面预览前100条；下载整理结果包含全部记录。"));root.append(evidence);
    const exportButton=button("下载标准数据 JSON",async()=>{
      if(busy||!enabled())return;
      if(dirty||scopeDirty){showError("请先保存资料与检查范围，再下载标准数据。");return;}
      const owner=user()?.id,ticket=generation,id=current.id,revision=current.revision;
      busy=true;exportButton.disabled=true;
      try{
        const data=await api(base+"/"+encodeURIComponent(id)+"/data?expected_revision="+revision);
        if(owner!==user()?.id||ticket!==generation||!enabled()||current?.id!==id||current.revision!==revision)return;
        const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),link=document.createElement("a");
        link.href=url;link.download="标准材料数据-"+id+".json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      }catch(error){if(owner===user()?.id&&ticket===generation)showError(error.message);}
      finally{if(ticket===generation){busy=false;exportButton.disabled=false;}}
    });root.append(exportButton);
    const trace=el("details");trace.append(el("summary","","版本与操作记录"));for(const v of current.versions)trace.append(el("p","muted",`版本 ${v.revision} · ${v.kind} · ${v.created_at}`));root.append(trace);
  }
  async function history(){const owner=user()?.id,ticket=generation,rows=await api(base);if(owner!==user()?.id||ticket!==generation||!enabled())return;const root=$("coreTaskHistory");root.replaceChildren(el("h3","","继续资料处理任务"));for(const row of rows)root.append(button(`${row.task.company_name||"待识别企业"} · ${row.task.start_date||"待确认"} 至 ${row.task.end_date||"待确认"} · ${states[row.status]||row.status}`,()=>action(()=>api(base+"/"+row.id))));}
  async function refresh(){const active=enabled();if(formOwner!==user()?.id){reset();clearScope();formOwner=user()?.id;}$("coreInputs").hidden=!active;$("coreLegacyToggle").hidden=!active;if(active){const owner=user()?.id,ticket=generation;if(policyUI()){const config=await api("/api/enterprise/materials/config");if(owner!==user()?.id||ticket!==generation)return;policyCatalog=config.policy_catalog;}const rows=await api("/api/clients");if(owner!==user()?.id||ticket!==generation||!enabled())return;clients=rows;const selected=$("coreClient").value;$("coreClient").replaceChildren();const empty=el("option","","新企业 / 从文件读取");empty.value="";$("coreClient").append(empty);for(const c of clients){const option=el("option","",`${c.name} · ${c.taxpayer_id}`);option.value=c.id;$("coreClient").append(option);}$("coreClient").value=selected;await history();}}
  async function openTask(id){return action(()=>api(base+"/"+encodeURIComponent(id)));}
  return {refresh,reset,openTask};
}
