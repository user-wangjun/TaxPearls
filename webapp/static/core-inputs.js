"use strict";
function createCoreInputs({api, el, $, user, showError, continueCheck=()=>{}, policyUI=()=>null}) {
  const base = "/api/enterprise/input-tasks";
  let current = null, busy = false, dirty = false, generation = 0, policyCatalog=null;
  let formOwner = user()?.id, clients=[], scopeDirty=false, attempted=false;
  const scopeIds={company_name:"coreCompanyName",tax_no:"coreTaxNo",start_date:"coreStart",end_date:"coreEnd"};
  const rangeKind=$("coreRangeKind"),rangeMonth=$("coreRangeMonth"),rangeYear=$("coreRangeYear"),rangeQuarter=$("coreRangeQuarter");
  const rangeTrigger=$("coreDateRange"),calendar=$("coreRangeCalendar"),calendarMonth=$("coreCalendarMonth");
  let calendarYear=0,calendarMonthNumber=0,draftStart="";
  if(!rangeKind.value)rangeKind.value="custom";
  const pad=n=>String(n).padStart(2,"0"),lastDay=(y,m)=>new Date(Date.UTC(y,m,0)).getUTCDate();
  function updateRangeDisplay(){
    const start=$("coreStart").value,end=$("coreEnd").value,custom=rangeKind.value==="custom";
    $("coreDateRangeText").textContent=start||end?`${start||"待选择起始日"} 至 ${end||"待选择终止日"}`:custom?"选择起始和终止日期":"选择上方期间后显示区间";
    rangeTrigger.setAttribute("aria-readonly",String(!custom));}
  function closeCalendar(focus=false){calendar.hidden=true;rangeTrigger.setAttribute("aria-expanded","false");draftStart="";if(focus)rangeTrigger.focus?.();}
  function renderCalendar(){
    const year=String(calendarYear).padStart(4,"0"),month=pad(calendarMonthNumber),days=$("coreCalendarDays");
    calendarMonth.value=`${year}-${month}`;days.replaceChildren();
    const savedStart=$("coreStart").value,savedEnd=$("coreEnd").value;
    $("coreCalendarHint").textContent=draftStart?`已选 ${draftStart}，请选择终止日期`:savedStart&&savedEnd?`已选 ${savedStart} 至 ${savedEnd}，可重新选择`:"请选择起始日期，再选择终止日期";
    const first=new Date(0);first.setUTCFullYear(calendarYear,calendarMonthNumber-1,1);
    for(let i=0;i<(first.getUTCDay()+6)%7;i++){const blank=el("span");blank.setAttribute("aria-hidden","true");days.append(blank);}
    const start=draftStart||$("coreStart").value,end=draftStart?"":$("coreEnd").value;
    for(let d=1;d<=lastDay(calendarYear,calendarMonthNumber);d++){
      const value=`${year}-${month}-${pad(d)}`,selected=value===start||value===end;
      const day=el("button",selected?"selected":start&&end&&value>start&&value<end?"in-range":"",String(d));day.type="button";
      day.setAttribute("data-date",value);day.setAttribute("aria-label",value);day.setAttribute("aria-pressed",String(selected));
      day.onclick=()=>{if(!draftStart){draftStart=value;renderCalendar();days.querySelector?.(`button[data-date="${value}"]`)?.focus();return;}
        const [from,to]=[draftStart,value].sort();$("coreStart").value=from;$("coreEnd").value=to;writePeriodInputs(from);scopeDirty=true;draftStart="";updateRangeDisplay();renderCalendar();days.querySelector?.(`button[data-date="${value}"]`)?.focus();displayErrors(attempted?validateScope():{});};
      days.append(day);}}
  rangeTrigger.onclick=()=>{if(rangeKind.value!=="custom")return;if(!calendar.hidden){closeCalendar();return;}
    const start=$("coreStart").value,today=new Date();calendarYear=start?Number(start.slice(0,4)):today.getFullYear();calendarMonthNumber=start?Number(start.slice(5,7)):today.getMonth()+1;
    draftStart="";renderCalendar();calendar.hidden=false;rangeTrigger.setAttribute("aria-expanded","true");calendarMonth.focus?.();};
  calendarMonth.addEventListener("change",()=>{const [y,m]=calendarMonth.value.split("-").map(Number);if(y>=1&&y<=9999&&m>=1&&m<=12){calendarYear=y;calendarMonthNumber=m;renderCalendar();}});
  function moveCalendar(offset){const next=(calendarYear-1)*12+calendarMonthNumber-1+offset;if(next<0||next>=9999*12)return;calendarYear=Math.floor(next/12)+1;calendarMonthNumber=next%12+1;renderCalendar();}
  $("coreCalendarPrev").onclick=()=>moveCalendar(-1);$("coreCalendarNext").onclick=()=>moveCalendar(1);
  $("coreCalendarCancel").onclick=()=>closeCalendar(true);
  $("coreCalendarClear").onclick=()=>{$("coreStart").value=$("coreEnd").value="";writePeriodInputs("");scopeDirty=true;updateRangeDisplay();closeCalendar(true);displayErrors(attempted?validateScope():{});};
  calendar.addEventListener("keydown",event=>{if(event.key==="Escape"){event.preventDefault();closeCalendar(true);}});
  document.addEventListener?.("pointerdown",event=>{if(!calendar.hidden&&!$("coreDateRangeField").contains(event.target))closeCalendar();});
  function writePeriodInputs(start){
    rangeMonth.value=start?start.slice(0,7):"";rangeYear.value=start?start.slice(0,4):"";
    rangeQuarter.value=start?String(Math.floor((Number(start.slice(5,7))-1)/3)+1):"1";}
  function selectedRange(kind){
    const [monthYear,month]=rangeMonth.value.split("-").map(Number),year=kind==="month"?monthYear:Number(rangeYear.value),quarter=Number(rangeQuarter.value);
    if(!Number.isInteger(year)||year<1||year>9999)return null;
    const y=String(year).padStart(4,"0");
    if(kind==="month"&&month>=1&&month<=12)return [`${y}-${pad(month)}-01`,`${y}-${pad(month)}-${pad(lastDay(year,month))}`];
    if(kind==="quarter"&&quarter>=1&&quarter<=4){const sm=(quarter-1)*3+1,em=sm+2;return [`${y}-${pad(sm)}-01`,`${y}-${pad(em)}-${pad(lastDay(year,em))}`];}
    if(kind==="year")return [`${y}-01-01`,`${y}-12-31`];return null;}
  function syncRange(){
    const kind=rangeKind.value||"custom",custom=kind==="custom";
    $("coreRangeMonthField").hidden=kind!=="month";$("coreRangeYearField").hidden=!["quarter","year"].includes(kind);$("coreRangeQuarterField").hidden=kind!=="quarter";
    $("coreStart").readOnly=!custom;$("coreEnd").readOnly=!custom;
    closeCalendar();if(custom){updateRangeDisplay();return;}
    const range=selectedRange(kind);
    $("coreStart").value=range?.[0]||"";$("coreEnd").value=range?.[1]||"";updateRangeDisplay();}
  const scopeRead=()=>({...Object.fromEntries(Object.entries(scopeIds).map(([key,id])=>[key,$(id).value.trim()])),range_kind:rangeKind.value||"custom"});
  const scopeWrite=()=>{for(const [key,id] of Object.entries(scopeIds))$(id).value=current.task[key]||"";const kind=current.task.range_kind||current.range_kind;rangeKind.value=["month","quarter","year","custom"].includes(kind)?kind:"custom";writePeriodInputs(current.task.start_date||"");syncRange();$("coreClient").value=clients.some(c=>c.id===current.task.company_id)?current.task.company_id:"";scopeDirty=false;};
  const fieldErrors={};
  for(const [key,id] of Object.entries(scopeIds)){
    const error=el("small","core-field-error");error.id=id+"Error";error.hidden=true;$(id).setAttribute("aria-describedby",error.id);if(["start_date","end_date"].includes(key))$("coreDateRangeField").append(error);else $(id).after?.(error);fieldErrors[key]=error;
    $(id).addEventListener("input",()=>{scopeDirty=true;const errors=validateScope();displayErrors(attempted?errors:Object.fromEntries(Object.entries(errors).filter(([field])=>scopeRead()[field])),attempted?Object.keys(scopeIds):key==="start_date"?[key,"end_date"]:[key]);});
  }
  rangeTrigger.setAttribute("aria-describedby","coreStartError coreEndError");
  const rangeError=el("small","core-field-error");rangeError.id="coreRangeKindError";rangeError.hidden=true;rangeKind.setAttribute("aria-describedby",rangeError.id);rangeKind.after?.(rangeError);fieldErrors.range_kind=rangeError;
  function validateRange(value){
    const kind=value.range_kind||"custom",errors={};
    if(!["month","quarter","year","custom"].includes(kind)){errors.range_kind="检查范围方式无效。";return errors;}
    if(kind==="custom"||!value.start_date||!value.end_date||value.start_date>value.end_date)return errors;
    const [ys,ms,ds]=value.start_date.split("-").map(Number),[ye,me,de]=value.end_date.split("-").map(Number);
    const aligned=kind==="month"?(ds===1&&ys===ye&&ms===me&&de===lastDay(ye,me)):
      kind==="quarter"?(ds===1&&[1,4,7,10].includes(ms)&&ye===ys&&me===ms+2&&de===lastDay(ye,me)):
      (ys===ye&&ms===1&&ds===1&&me===12&&de===31);
    if(!aligned)errors.range_kind=`按${{month:"月份",quarter:"季度",year:"年度"}[kind]}检查时，起止日期须覆盖完整自然${{month:"月",quarter:"季度",year:"年度"}[kind]}。`;
    return errors;}
  function validateScope(){const value=scopeRead(),errors={};for(const [key,label] of Object.entries({company_name:"企业名称",tax_no:"税号",start_date:"检查起始日",end_date:"检查终止日"}))if(!value[key])errors[key]="请填写"+label+"，或从材料读取后核对。";
    if(value.tax_no&&!/^[A-Za-z0-9]{8,20}$/.test(value.tax_no.replace(/\s/g,"")))errors.tax_no="税号须为8至20位字母或数字。";
    const selected=clients.find(c=>c.id===$("coreClient").value);if(selected&&value.tax_no.replace(/\s/g,"").toUpperCase()!==selected.taxpayer_id.replace(/\s/g,"").toUpperCase())errors.tax_no="所选企业与填写税号不一致，请核对或切换为新企业。";
    if(value.start_date&&value.end_date&&value.start_date>value.end_date)errors.end_date="检查终止日不能早于起始日。";
    Object.assign(errors,validateRange(value));return errors;}
  function inputFor(key){return key==="range_kind"?rangeKind:["start_date","end_date"].includes(key)?rangeTrigger:$(scopeIds[key]);}
  function displayErrors(errors,keys=Object.keys(scopeIds)){for(const key of [...new Set([...keys,"range_kind"])]){if(!fieldErrors[key])continue;const message=errors[key]||"";fieldErrors[key].textContent=message;fieldErrors[key].hidden=!message;inputFor(key)?.setAttribute?.("aria-invalid",String(!!message));}rangeTrigger.setAttribute("aria-invalid",String(!fieldErrors.start_date.hidden||!fieldErrors.end_date.hidden));}
  rangeKind.addEventListener("change",()=>{scopeDirty=true;writePeriodInputs($("coreStart").value);syncRange();displayErrors(attempted?validateScope():{});});
  for(const input of [rangeMonth,rangeYear,rangeQuarter])input.addEventListener("change",()=>{scopeDirty=true;syncRange();displayErrors(attempted?validateScope():{});});
  syncRange();
  $("coreClient").onchange=()=>{const company=clients.find(c=>c.id===$("coreClient").value);$("coreCompanyName").value=company?.name||"";$("coreTaxNo").value=company?.taxpayer_id||"";scopeDirty=true;displayErrors(attempted?validateScope():{});};
  async function saveScope(confirm=false){const owner=user()?.id,ticket=generation;const result=await api(base+"/"+current.id+"/scope",json({...scopeRead(),client_id:$("coreClient").value||null,expected_revision:current.revision,confirm}));if(owner!==user()?.id||ticket!==generation)throw new Error("会话已变化，请重新读取任务。");current=result;scopeDirty=false;return result;}
  async function startCheck(){if(busy)return;if(dirty){showError("请先保存资料口径核对，再开始检查。");return;}attempted=true;const errors=validateScope();displayErrors(errors);if(Object.keys(errors).length){inputFor(Object.keys(errors)[0]).focus?.();return;}const owner=user()?.id,ticket=generation;const saved=await action(()=>saveScope(true),true);if(!saved||owner!==user()?.id||ticket!==generation)return;displayErrors(current.scope_errors||{});if(!Object.keys(current.scope_errors||{}).length&&current.task.scope_confirmed!==false)continueCheck(current.id);}

  const enabled = () => ["org_admin", "accountant"].includes(user()?.role);
  const states = {needs_completion:"待补充或核对",ready_for_analysis:"资料已就绪，可继续核查",parsing:"正在解析"};
  const kinds = {account_balance:"科目余额表",vat_declaration:"增值税申报表",invoice_detail:"发票明细"};
  const json = value => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
  const button = (text, fn) => {const n=el("button","btn re",text);n.type="button";n.onclick=fn;return n;};
  function clearScope(){for(const id of Object.values(scopeIds))$(id).value="";rangeKind.value="custom";writePeriodInputs("");syncRange();$("coreClient").value="";scopeDirty=attempted=false;displayErrors({});}
  function reset() {generation++;current=null;busy=dirty=scopeDirty=attempted=false;closeCalendar();displayErrors({});clients=[];$("coreClient").replaceChildren();$("coreTaskResult").replaceChildren();$("coreTaskHistory").replaceChildren();$("coreTaskForm").disabled=false;if(formOwner!==user()?.id||!enabled())clearScope();formOwner=user()?.id;}
  async function action(fn, saving=false) {
    if(busy||!enabled())return;
    if((dirty||scopeDirty&&current)&&!saving){showError("存在未保存的资料口径核对，请先保存后再补传、创建或读取任务。");return;}
    busy=true;$("coreTaskForm").disabled=true;$("coreTaskResult").querySelectorAll("button,input,select").forEach(n=>n.disabled=true);
    const owner=user()?.id, ticket=generation, previousId=current?.id;
    try {const result=await fn();if(owner===user()?.id&&ticket===generation&&enabled()){if(previousId!==result.id)attempted=false;current=result;dirty=false;scopeWrite();render();await history();return true;}}
    catch(error){if(owner===user()?.id&&ticket===generation)showError(error.message);}
    finally{if(ticket===generation){busy=false;$("coreTaskForm").disabled=false;$("coreTaskResult").querySelectorAll("fieldset,button,input,select").forEach(n=>n.disabled=false);}}
  }
  $("coreCreateTask").onclick=()=>{if(dirty){showError("请先保存资料口径核对。");return;}attempted=false;displayErrors({});return action(()=>api(base,json({...scopeRead(),client_id:$("coreClient").value||null})),true);};
  function select(label, choices, value) {
    const wrap=el("label","",label),input=el("select");input.setAttribute("aria-label",label);
    for(const [v,name] of choices){const o=el("option","",name);o.value=v;input.append(o);}input.value=value||"";wrap.append(input);return {wrap,input};
  }
  function field(label, type, value) {const wrap=el("label","",label),input=el("input");input.type=type;input.value=value||"";input.setAttribute("aria-label",label);wrap.append(input);return {wrap,input};}
  function render() {
    const root=$("coreTaskResult");root.replaceChildren();if(!current)return;
    const task=current.task,p=current.processing;
    const rangeLabels={month:"按月",quarter:"按季度",year:"按年度",custom:"自定义起止日"};
    root.append(el("h3","",`${task.company_name||"待识别企业"} · ${task.tax_no||"税号待填写"}`),el("p","muted",`${task.start_date||"起始日待确认"} 至 ${task.end_date||"终止日待确认"} · ${rangeLabels[task.range_kind||current.range_kind]||""} · 版本 ${current.revision}`),
      el("p",p.status==="ready_for_analysis"?"":"material-note",states[p.status]||p.status),el("p","muted",p.boundary));
    root.append(button("重新读取任务",()=>action(()=>api(base+"/"+current.id))));
    root.append(button("保存企业与检查区间",()=>{if(dirty){showError("请先保存资料口径核对，再保存企业与检查区间。");return;}return action(()=>saveScope(),true);}));
    const next=button("开始检查",startCheck);root.append(next,el("p","muted","先核对企业与检查区间；字段错误会阻止开始检查和 AI 审查。"));
    const origins=task.scope_origins||{};for(const [key,origin] of Object.entries(origins))if(origin.kind==="file"&&["company_name","tax_no"].includes(key))root.append(el("p","muted",`${{company_name:"企业名称",tax_no:"税号"}[key]}已从文件读取：${(origin.sources||[]).map(s=>`${current.documents.find(d=>d.id===s.file_id)?.name||s.file_id} · ${s.position}`).join("；")}，请核对后开始检查。`));
    const candidates=task.scope_candidates||{},candidateStarts=candidates.start_date||[],candidateEnds=candidates.end_date||[];
    if(candidateStarts.length){const box=el("section","material-card");box.append(el("h4","","材料读取的期间候选（不会自动更改检查区间）"));
      for(const start of candidateStarts){const end=candidateEnds.find(e=>JSON.stringify(e.sources)===JSON.stringify(start.sources));
        const source=(start.sources||[]).map(s=>`${current.documents.find(d=>d.id===s.file_id)?.name||s.file_id} · ${s.position}`).join("；")||"来源未记录";
        box.append(el("p","muted",`${start.value}${end?" 至 "+end.value:""} · ${source}`));
        if(end)box.append(button("采用此期间",()=>{rangeKind.value="custom";$("coreStart").value=start.value;$("coreEnd").value=end.value;writePeriodInputs(start.value);syncRange();scopeDirty=true;displayErrors(attempted?validateScope():{});}));}
      root.append(box);}
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
    review.append(el("legend","","资料口径核对"),el("p","muted","仅填写已核实的信息，留空沿用文件识别值。填写的是人工确认信息，不会改写原件或声称文件已经证明。一个文件的统一口径会应用到其所有工作表。所有材料须在所选检查区间内；区间外或日期无法识别的记录会保留原值与来源位置，但不纳入本次检查。"));
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
      const reviewed=field(doc.name+" 已对照原页核实提取候选的字段、金额、购销方和所属期","checkbox","");reviewed.input.checked=!!options.source_reviewed;
      const ocr=field(doc.name+" 允许未识别PDF页交给配置模型OCR（发送页图）","checkbox","");ocr.input.checked=!!options.ocr;
      if(doc.ocr_candidate||doc.pdf_candidate)card.append(reviewed.wrap);if(doc.name.toLowerCase().endsWith(".pdf"))card.append(ocr.wrap);
      const statuses=[];
      if(doc.material_type==="invoice_detail"&&doc.name.toLowerCase().endsWith(".pdf")){
        card.append(el("p","material-note","票面没有记载的红蓝字、作废状态须逐票核实后填写；留空会保留资料缺口。单张发票不能证明整期进销项齐全。"));
        for(const record of doc.records){
          const status=el("div","material-company"),prefix=doc.name+" · "+record.source_position+" · "+(record.invoice_no||"票号待核对");
          status.append(el("p","",prefix));
          const confirmed=options.invoice_statuses?.[record.id]||{},parsed=record.parsed_invoice_statuses||record;
          const red=select(prefix+" 红蓝字状态",[["","待核实"],["蓝字","蓝字"],["红字","红字"]],confirmed.red_letter_status||parsed.red_letter_status);
          const voided=select(prefix+" 作废状态",[["","待核实"],["正常","正常"],["未作废","未作废"],["作废","作废"],["已作废","已作废"]],confirmed.void_status||parsed.void_status);
          for(const f of [red,voided]){status.append(f.wrap);f.input.addEventListener("change",()=>{reviewed.input.checked=false;});}
          card.append(status);statuses.push({id:record.id,red,voided});
        }
      }
      for(const f of [unit,start,end,form,direction])f.input.addEventListener("change",()=>{reviewed.input.checked=false;});
      const parts=el("details"),summary=el("summary","","识别到的来源口径");parts.append(summary);
      for(const part of doc.parts)parts.append(el("p","muted",`${part.name} · ${part.form_type} · ${part.period?.join(" 至 ")||"期间未识别"} · ${part.unit||"单位未识别"}`));card.append(parts);
      review.append(card);editors.push({id:doc.id,unit,start,end,form,direction,exclude,reviewed,ocr,statuses});
    }
    let readPolicy=null;
    if(policyCatalog&&policyUI()){
      const panel=el("div");review.append(panel);
      readPolicy=policyUI().editor(panel,policyCatalog,current.policy_input,current.policy_assessment,()=>{dirty=true;},()=>{dirty=true;},()=>{});
      panel.append(el("p","muted",`目录来源核对截至 ${policyCatalog.checked_as_of}；政策条件与本任务资料版本绑定，单独复核，不替代知识图谱的规则审核。`));
      if(current.policy_error)panel.append(el("p","material-note",current.policy_error));
    }
    const save=button("保存核对并重新整理资料",()=>{const policyInput=readPolicy?.();return action(async()=>{if(scopeDirty)await saveScope();const options={};for(const e of editors)options[e.id]={excluded:e.exclude.input.checked,source_reviewed:e.reviewed.input.checked,ocr:e.ocr.input.checked,invoice_statuses:Object.fromEntries(e.statuses.map(s=>[s.id,{red_letter_status:s.red.input.value,void_status:s.voided.input.value}])),confirmed_metadata:{unit:e.unit.input.value,period_start:e.start.input.value,period_end:e.end.input.value,form_type:e.form.input.value,direction:e.direction.input.value}};
      return api(base+"/"+current.id+"/prepare",json({expected_revision:current.revision,applicable_forms:scopeKnown.input.checked?applicable.input.value.split(/[;；]/).map(s=>s.trim()).filter(Boolean):null,source_options:options,mapping_mode:"ai",...(readPolicy?{policy_input:policyInput}:{})}));},true);});review.append(save);root.append(review);
    const problems=el("section","enterprise-feedback"+(p.issues.length?" blocking":""));problems.append(el("h4","",`资料缺口与冲突（${p.issues.length}）`));
    for(const item of p.issues)problems.append(el("p","",item.message+(item.source_position?" · "+item.source_position:"")));root.append(problems);
    if(p.notices.length){const notes=el("details");notes.append(el("summary","",`来源说明（${p.notices.length}）`));for(const n of p.notices)notes.append(el("p","muted",n.message+(n.source_position?" · "+n.source_position:"")));root.append(notes);}
    const evidence=el("details");evidence.append(el("summary","",`标准记录与字段来源（${p.records.length}）`));
    for(const r of p.records.slice(0,100)){const card=el("section","material-card");card.append(el("p","",`${kinds[r.material_type]} · ${r.source_position} · ${r.in_scope?"检查区间内":"区间外保留"}${r.duplicate_of?" · 重复来源":""}`));const raw=el("pre","",JSON.stringify(r,null,2));raw.style.whiteSpace="pre-wrap";raw.style.overflowWrap="anywhere";card.append(raw);evidence.append(card);}if(p.records.length>100)evidence.append(el("p","muted","页面预览前100条；下载整理结果包含全部记录。"));root.append(evidence);
    const exportButton=button("下载整理结果 JSON",()=>{const blob=new Blob([JSON.stringify(current,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download="基础资料整理-"+current.id+".json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});root.append(exportButton);
    const trace=el("details");trace.append(el("summary","","版本与操作记录"));for(const v of current.versions)trace.append(el("p","muted",`版本 ${v.revision} · ${v.kind} · ${v.created_at}`));root.append(trace);
  }
  async function history(){const owner=user()?.id,ticket=generation,rows=await api(base);if(owner!==user()?.id||ticket!==generation||!enabled())return;const root=$("coreTaskHistory");root.replaceChildren(el("h3","","继续资料处理任务"));for(const row of rows)root.append(button(`${row.task.company_name||"待识别企业"} · ${row.task.start_date||"待确认"} 至 ${row.task.end_date||"待确认"} · ${states[row.status]||row.status}`,()=>action(()=>api(base+"/"+row.id))));}
  async function refresh(){const active=enabled();if(formOwner!==user()?.id){reset();clearScope();formOwner=user()?.id;}$("coreInputs").hidden=!active;$("coreLegacyToggle").hidden=!active;if(active){const owner=user()?.id,ticket=generation;if(policyUI()){const config=await api("/api/enterprise/materials/config");if(owner!==user()?.id||ticket!==generation)return;policyCatalog=config.policy_catalog;}const rows=await api("/api/clients");if(owner!==user()?.id||ticket!==generation||!enabled())return;clients=rows;const selected=$("coreClient").value;$("coreClient").replaceChildren();const empty=el("option","","新企业 / 从文件读取");empty.value="";$("coreClient").append(empty);for(const c of clients){const option=el("option","",`${c.name} · ${c.taxpayer_id}`);option.value=c.id;$("coreClient").append(option);}$("coreClient").value=selected;await history();}}
  async function openTask(id){return action(()=>api(base+"/"+encodeURIComponent(id)));}
  return {refresh,reset,openTask};
}
