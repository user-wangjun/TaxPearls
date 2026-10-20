"use strict";
function createCoreInputs({api, el, $, user, showError, continueCheck=()=>{}, policyUI=()=>null, openTaskPanel=()=>{}}) {
  const base = "/api/enterprise/input-tasks";
  let current = null, busy = false, dirty = false, generation = 0, policyCatalog=null;
  let checkFoldScope=null,checkPage=0;const checkFolds=new Map(),CHECK_PAGE_SIZE=5;
  let reportState=null;
  let formOwner = user()?.id, clients=[], scopeDirty=false, attempted=false;
  const uploadStates=new Map(),uploadViews=new Map(),waitingUploads=[];
  let activeUpload=null,uploadRun=null,currentUpload=null,uploadsPaused=false;
  function paintUpload(kind){
    const view=uploadViews.get(kind);if(!view)return;
    const state=uploadStates.get(kind),count=waitingUploads.filter(item=>item.kind===kind).reduce((n,item)=>n+item.files.length,0);
    view.root.hidden=!state&&!count;view.root.dataset.phase=state?.phase||"waiting";
    view.text.textContent=state?.text||"等待前一批资料处理完成。";
    if(state?.percent===null)view.bar.removeAttribute("value");else view.bar.value=state?.percent||0;
    view.waiting.hidden=!count;view.waiting.textContent=count?`${uploadsPaused?"已暂停，等待上传":"等待上传"} ${count} 份（尚未发送）`:"";
    view.controls.hidden=!count;view.resume.hidden=!uploadsPaused;
  }
  function paintUploads(){for(const kind of uploadViews.keys())paintUpload(kind);}
  function uploadStatus(kind,state){
    uploadStates.set(kind,state);paintUpload(kind);
  }
  function taskControls(locked){
    $("coreTaskForm").disabled=locked;
    $("coreTaskForm").querySelectorAll("button,input,select").forEach(n=>{n.disabled=locked||!!current?.completion&&n.id!=="coreCreateTask";});
    $("coreTaskResult").querySelectorAll("fieldset,button,input,select").forEach(n=>{
      n.disabled=locked?!(n.coreUploadPicker&&uploadRun||n.coreUploadQueueControl):!!n.corePageDisabled;
    });
  }
  function clearWaiting(kind){
    for(let i=waitingUploads.length-1;i>=0;i--)if(waitingUploads[i].kind===kind)waitingUploads.splice(i,1)[0].finish(false);
    if(!waitingUploads.length)uploadsPaused=false;paintUploads();
  }
  async function resumeUploads(){
    if(busy||uploadRun||!waitingUploads.length)return;
    // The failed batch may already have been saved. Refresh its revision first;
    // resume only the files that have never been sent, not the failed batch.
    if(await action(()=>api(base+"/"+current.id),true,true)){uploadsPaused=false;paintUploads();return drainUploads();}
  }
  async function drainUploads(){
    if(uploadRun||busy||uploadsPaused||!waitingUploads.length||!enabled())return;
    const run={owner:user()?.id,ticket:generation,id:current.id};uploadRun=run;
    const valid=()=>uploadRun===run&&run.owner===user()?.id&&run.ticket===generation&&current?.id===run.id&&enabled();
    try{
      while(waitingUploads.length&&valid()&&!uploadsPaused){
        const item=waitingUploads.shift();currentUpload=item;paintUploads();
        const ok=await action(async()=>{
          if(scopeDirty)await saveScope();
          const form=new FormData();form.append("material_type",item.kind);form.append("expected_revision",current.revision);
          form.append("mapping_mode","ai");form.append("ocr","1");item.files.forEach(file=>form.append("files",file));
          const result=await sendUpload(base+"/"+run.id+"/files",form,item.kind,item.files.length);item.received=true;return result;
        },true,true);
        if(!valid())return;
        item.finish(!!ok);currentUpload=null;
        if(!ok)uploadsPaused=waitingUploads.length>0;
        paintUploads();if(!ok)break;
      }
    }finally{if(uploadRun===run){uploadRun=null;currentUpload=null;taskControls(busy);paintUploads();}}
  }
  function queueUpload(kind,files){
    if(!files.length||!current||!enabled())return;
    if(busy&&!uploadRun){showError("正在保存任务，请稍后再添加文件。");return;}
    if(dirty){showError("请先保存资料口径核对，再补传资料。");return;}
    const pending=[...waitingUploads.flatMap(item=>item.files),...(currentUpload&&!currentUpload.received?currentUpload.files:[])];
    if(files.some(file=>!file.size||file.size>10*1024*1024)){showError("单份文件须非空且不超过 10MB。");return;}
    if((current.files||[]).length+pending.length+files.length>20){showError("已保存、正在上传和等待上传的文件合计不能超过 20 份。");return;}
    if([...pending,...files].reduce((n,file)=>n+file.size,0)>50*1024*1024){showError("正在上传和等待上传的文件合计不能超过 50MB。");return;}
    const result=new Promise(finish=>waitingUploads.push({kind,files,finish,received:false}));
    paintUploads();void drainUploads();return result;
  }
  globalThis.addEventListener?.("beforeunload",event=>{if(currentUpload||waitingUploads.length){event.preventDefault();event.returnValue="";}});
  function sendUpload(url,form,kind,count){
    const owner=user()?.id,ticket=generation,valid=()=>owner===user()?.id&&ticket===generation&&enabled();
    const update=state=>{if(valid())uploadStatus(kind,state);};
    update({phase:"uploading",percent:0,text:`正在上传 ${count} 个文件 · 0%`});
    return new Promise((resolve,reject)=>{
      const request=new XMLHttpRequest();activeUpload=request;request.open("POST",url);
      const failed=message=>{if(activeUpload===request)activeUpload=null;update({phase:"error",percent:0,text:message});reject(new Error(message));};
      request.upload.onprogress=event=>{
        const percent=event.lengthComputable&&event.total>0?Math.min(100,Math.floor(event.loaded/event.total*100)):null;
        update({phase:"uploading",percent,text:`正在上传 ${count} 个文件${percent===null?"":` · ${percent}%`}`});
      };
      request.upload.onload=()=>update({phase:"processing",percent:null,text:"文件已传输，正在保存和解析，请稍候……"});
      request.onload=()=>{
        let result;try{result=JSON.parse(request.responseText);}catch{failed("未收到有效处理结果，请重新读取任务核对。");return;}
        if(request.status<200||request.status>=300){
          const detail=result?.detail,message=Array.isArray(detail)?detail.map(item=>item.msg||"字段校验失败").join("；"):detail;
          failed(typeof message==="string"?message:`请求失败（${request.status}），请重新读取任务核对。`);return;
        }
        if(!result||typeof result.id!=="string"||!result.task||!result.processing){failed("处理结果不完整，请重新读取任务核对。");return;}
        if(activeUpload===request)activeUpload=null;
        update({phase:"done",percent:100,text:`${count} 个文件已上传，解析结果已返回，请核对下方资料。`});resolve(result);
      };
      request.onerror=()=>failed("连接中断，上传结果未确认，请重新读取任务核对后再重试。");
      request.onabort=()=>failed("上传已中断，请重新读取任务核对。");
      request.send(form);
    });
  }
  let checkProgress=null;
  let regionTaskId=null;
  const regions={"CN-11":"北京市","CN-12":"天津市","CN-13":"河北省","CN-14":"山西省","CN-15":"内蒙古自治区","CN-21":"辽宁省","CN-22":"吉林省","CN-23":"黑龙江省","CN-31":"上海市","CN-32":"江苏省","CN-33":"浙江省","CN-34":"安徽省","CN-35":"福建省","CN-36":"江西省","CN-37":"山东省","CN-41":"河南省","CN-42":"湖北省","CN-43":"湖南省","CN-44":"广东省","CN-45":"广西壮族自治区","CN-46":"海南省","CN-50":"重庆市","CN-51":"四川省","CN-52":"贵州省","CN-53":"云南省","CN-54":"西藏自治区","CN-61":"陕西省","CN-62":"甘肃省","CN-63":"青海省","CN-64":"宁夏回族自治区","CN-65":"新疆维吾尔自治区"};
  for(const [value,name] of Object.entries(regions)){const option=el("option","",name);option.value=value;$("coreRegion").append(option);}
  $("coreRegion").addEventListener("change",()=>{$("coreRegion").setAttribute("aria-invalid","false");});
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
  const scopeWrite=()=>{for(const [key,id] of Object.entries(scopeIds))$(id).value=current.task[key]||"";const kind=current.task.range_kind||current.range_kind;rangeKind.value=["month","quarter","year","custom"].includes(kind)?kind:"custom";writePeriodInputs(current.task.start_date||"");syncRange();$("coreClient").value=clients.some(c=>c.id===current.task.company_id)?current.task.company_id:"";scopeDirty=false;
    if(regionTaskId!==current.id){const saved=[...new Set(Object.values(current.tax_checks||{}).map(s=>s.bundle?.task?.region_code).filter(code=>code==="CN"||regions[code]))];$("coreRegion").value=saved.length===1&&saved[0]!=="CN"?saved[0]:"";regionTaskId=current.id;}
  };
  const deleteDialog=$("coreDeleteDialog");let deleteDecision=null;
  deleteDialog.addEventListener("close",()=>{const resolve=deleteDecision;deleteDecision=null;resolve?.(deleteDialog.returnValue==="delete");});
  $("coreDeleteCancel").onclick=()=>deleteDialog.close("cancel");
  $("coreDeleteConfirm").onclick=()=>deleteDialog.close("delete");
  deleteDialog.addEventListener("keydown",event=>{
    if(event.key!=="Tab")return;
    const cancel=$("coreDeleteCancel"),confirm=$("coreDeleteConfirm");
    if(event.shiftKey&&event.target===cancel){event.preventDefault();confirm.focus();}
    else if(!event.shiftKey&&event.target===confirm){event.preventDefault();cancel.focus();}
  });
  function confirmDelete(name){
    if(deleteDecision)return Promise.resolve(false);
    $("coreDeleteTaskName").textContent=name;deleteDialog.returnValue="";
    return new Promise(resolve=>{deleteDecision=resolve;deleteDialog.showModal();});
  }
  const identityDialog=$("coreIdentityDialog");let identityDecision=null,identityRead=null,identityValidate=null;
  identityDialog.addEventListener("close",()=>{const resolve=identityDecision;identityDecision=null;const read=identityRead;identityRead=identityValidate=null;resolve?.(identityDialog.returnValue==="apply"?read?.():null);});
  $("coreIdentityCancel").onclick=()=>identityDialog.close("cancel");
  $("coreIdentityConfirm").onclick=()=>identityValidate?.();
  identityDialog.addEventListener("keydown",event=>{
    if(event.key!=="Tab")return;
    const focusable=[...identityDialog.querySelectorAll("input,button")].filter(n=>!n.disabled);
    const first=focusable[0]||$("coreIdentityCancel"),last=focusable.at(-1)||$("coreIdentityConfirm");
    if(event.shiftKey&&event.target===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&event.target===last){event.preventDefault();first.focus();}
  });
  const identityToken=(key,value)=>{const text=String(value||"").normalize("NFKC").replace(/\s/g,"");return key==="tax_no"?text.toUpperCase():text;};
  function identityChoices(key){
    if(key==="period")return (current?.scope_period_candidates||[]).map(p=>({...p,value:`${p.start_date} 至 ${p.end_date}`,sources:(p.sources||[]).map(s=>({...s,file_name:current.documents.find(d=>d.id===s.file_id)?.name||s.file_id}))}));
    const groups=new Map();
    for(const choice of current?.scope_identity_candidates?.[key]||[])if(choice.selectable!==false)
      for(const value of choice.alternatives||[choice.value]){
        const token=identityToken(key,value);if(!token)continue;
        if(!groups.has(token))groups.set(token,{value,sources:[]});
        const sources=groups.get(token).sources;
        for(const source of choice.sources||[])if(!sources.some(s=>s.file_id===source.file_id&&s.position===source.position))sources.push(source);
      }
    return [...groups.values()];
  }
  function identitySources(sources){
    const files=new Map();
    for(const source of sources){
      const id=source.file_id||source.file_name;
      if(!files.has(id))files.set(id,{name:source.file_name||id,positions:new Set(),review:false});
      const file=files.get(id);file.positions.add(source.position||"来源待核对");file.review ||= !!source.needs_review;
    }
    const details=el("details","core-identity-sources"),list=el("ul");
    details.append(el("summary","",`查看来源（${files.size} 个文件 · ${[...files.values()].reduce((n,f)=>n+f.positions.size,0)} 处）`));
    for(const file of files.values()){
      const pages=[],other=[];
      for(const pos of file.positions){const match=pos.match(/第(\d+)页/);if(match)pages.push(Number(match[1]));else other.push(pos);}
      const sorted=[...new Set(pages)].sort((a,b)=>a-b),ranges=[];
      for(let i=0;i<sorted.length;i++){const first=sorted[i];let last=first;while(sorted[i+1]===last+1)last=sorted[++i];ranges.push(first===last?String(first):`${first}–${last}`);}
      const positions=[...(ranges.length?[`第 ${ranges.slice(0,4).join("、")}${ranges.length>4?"等":""} 页`]:[]),...other.slice(0,3)];
      if(other.length>3)positions.push(`等 ${other.length} 处`);
      list.append(el("li","",`${file.name} · ${positions.join("；")}${file.review?"（待核对候选）":""}`));
    }
    details.append(list);return details;
  }
  function identityDiff(value,other){
    const node=el("span"),text=String(value||"未填写"),reference=String(other||"");
    let first=0,last=text.length,end=reference.length;
    while(first<last&&first<end&&text[first]===reference[first])first++;
    while(last>first&&end>first&&text[last-1]===reference[end-1]){last--;end--;}
    node.append(el("span","",text.slice(0,first)),el("mark","core-identity-different",text.slice(first,last)),el("span","",text.slice(last)));return node;
  }
  function identityFieldErrors(value){
    const errors={};
    if(!value.company_name?.trim())errors.company_name="请填写企业名称，或从材料读取后核对。";
    else if(value.company_name.trim().length>200)errors.company_name="企业名称不得超过200字。";
    if(!value.tax_no?.replace(/\s/g,""))errors.tax_no="请填写税号，或从材料读取后核对。";
    else if(!/^[A-Za-z0-9]{8,20}$/.test(value.tax_no.replace(/\s/g,"")))errors.tax_no="税号须为8至20位字母或数字。";
    return errors;
  }
  function confirmIdentity(keys,before,preferred={}){
    if(identityDecision)return Promise.resolve(null);
    const list=$("coreIdentityChanges"),readers=[],notice=$("coreIdentityValidation"),id=current.id,revision=current.revision;
    let checking=false;list.replaceChildren();notice.textContent="";notice.hidden=true;
    const fields=keys.flatMap(key=>key==="period"?["start_date","end_date"]:[key]);
    const read=()=>Object.assign({},...readers.map(r=>{
      if(r.key!=="period")return {[r.key]:r.read()};
      const choice=identityChoices("period").find(p=>p.value===r.read());
      return {start_date:choice?.start_date||"",end_date:choice?.end_date||"",range_kind:"custom"};
    }));
    const errors=()=>{
      const value={...before,...read()},all={...identityErrors(value),...identityFieldErrors(value)};
      if(keys.includes("period"))for(const key of ["start_date","end_date"]){
        const date=new Date(value[key]+"T00:00:00Z");
        if(!/^\d{4}-\d{2}-\d{2}$/.test(value[key])||!Number.isFinite(date.getTime())||date.toISOString().slice(0,10)!==value[key])all[key]="请选择材料中的完整检查区间。";
      }
      if(keys.includes("period")&&value.start_date>value.end_date)all.end_date="检查终止日不能早于起始日。";
      return Object.fromEntries(Object.entries(all).filter(([key])=>fields.includes(key)));
    };
    const show=messages=>{notice.textContent=Object.values(messages).join("；");notice.hidden=!notice.textContent;};
    const update=()=>{const messages=errors();show(messages);$("coreIdentityConfirm").disabled=checking||!!Object.keys(messages).length;return messages;};
    for(const key of keys){
      const label={company_name:"企业名称",tax_no:"本企业税号",period:"检查区间"}[key],choices=identityChoices(key),row=el("section","core-identity-change");
      const previousValue=key==="period"?(before.start_date||before.end_date?`${before.start_date||"待选择起始日"} 至 ${before.end_date||"待选择终止日"}`:""):before[key];
      const previous=el("p","core-identity-current");previous.append(el("span","","当前填写："),identityDiff(previousValue,choices.length===1?choices[0].value:previousValue));row.append(el("h3","",label),previous);
      const radios=[];
      for(const choice of choices){
        const option=el("label","core-identity-option"),radio=el("input"),info=el("span");radio.type="radio";radio.name="coreIdentityChoice_"+key;radio.value=choice.value;
        radio.setAttribute("aria-label",`${label}采用${choice.value}`);
        radio.checked=preferred[key]?identityToken(key,preferred[key])===identityToken(key,choice.value):choices.length===1;
        info.append(identityDiff(choice.value,previousValue));
        if(choice.sources.some(s=>s.needs_review))info.append(el("small","muted","含待核对的识别候选"));
        option.append(radio,info);row.append(option,identitySources(choice.sources));radios.push(radio);
        radio.addEventListener("change",()=>{for(const r of radios)r.checked=r===radio;update();});
      }
      if(!choices.length){
        row.append(el("p","material-note","当前来源尚不能确定本企业信息。发票购销双方不作为企业候选，请先核对购销方向或补充余额表、申报表。"));
        row.append(identitySources((current?.scope_identity_candidates?.[key]||[]).flatMap(c=>c.sources||[])));
      }
      readers.push({key,read:()=>radios.find(r=>r.checked)?.value});list.append(row);
    }
    update();identityRead=read;identityDialog.returnValue="";
    identityValidate=async()=>{
      if(checking||Object.keys(update()).length)return;
      const pending=identityDecision,values=read();checking=true;$("coreIdentityConfirm").disabled=true;
      const radios=[...identityDialog.querySelectorAll("input")];radios.forEach(n=>n.disabled=true);
      notice.textContent="正在校验所选企业信息与检查区间…";notice.hidden=false;
      try{
        const result=await api(base+"/"+encodeURIComponent(id)+"/identity-validation",json({company_name:values.company_name??before.company_name,tax_no:values.tax_no??before.tax_no,...(keys.includes("period")?{start_date:values.start_date,end_date:values.end_date,range_kind:values.range_kind}:{}),fields,expected_revision:revision}));
        if(identityDecision!==pending)return;
        if(result.valid!==true||Object.keys(result.errors||{}).length){show(Object.keys(result.errors||{}).length?result.errors:{validation:"所选字段校验未通过，请重新核对。"});return;}
        identityDialog.close("apply");
      }catch(error){if(identityDecision===pending)show({validation:error.message});}
      finally{if(identityDecision===pending){checking=false;radios.forEach(n=>n.disabled=false);$("coreIdentityConfirm").disabled=!!Object.keys(errors()).length;}}
    };
    return new Promise(resolve=>{identityDecision=resolve;identityDialog.showModal();});
  }
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
  function validateScope(){const value=scopeRead(),errors=identityFieldErrors(value);for(const [key,label] of Object.entries({start_date:"检查起始日",end_date:"检查终止日"}))if(!value[key])errors[key]="请填写"+label+"，或从材料读取后核对。";
    const selected=clients.find(c=>c.id===$("coreClient").value);if(selected&&value.tax_no.replace(/\s/g,"").toUpperCase()!==selected.taxpayer_id.replace(/\s/g,"").toUpperCase())errors.tax_no="所选企业与填写税号不一致，请核对或切换为新企业。";
    if(value.start_date&&value.end_date&&value.start_date>value.end_date)errors.end_date="检查终止日不能早于起始日。";
    Object.assign(errors,validateRange(value),identityErrors(value));return errors;}
  function identityErrors(value){
    const errors={},candidates=current?.scope_identity_candidates||{};
    for(const key of ["company_name","tax_no"]){
      const choices=candidates[key]||[];
      if(value[key]&&choices.some(c=>!(c.alternatives||[c.value]).some(v=>identityToken(key,v)===identityToken(key,value[key]))))
        errors[key]=`${key==="company_name"?"企业名称":"税号"}与材料识别结果不同，请查看差异并对照原件确认。`;
    }return errors;
  }
  const identityLinks={};
  function renderIdentityCandidates(){
    const actions=$("coreIdentityActions");actions.replaceChildren();actions.hidden=true;
    const candidates=current?.scope_identity_candidates||{},unique={};
    const owner=user()?.id,ticket=generation,id=current?.id,revision=current?.revision;
    const apply=async (keys,preferred={})=>{
      if(busy||!enabled()||owner!==user()?.id||ticket!==generation||current?.id!==id||current?.revision!==revision)return;
      const before=scopeRead(),selectedClient=$("coreClient").value,values=await confirmIdentity(keys,before,preferred);
      if(!values||Object.values(values).some(v=>!v))return;
      if(busy||!enabled()||owner!==user()?.id||ticket!==generation||current?.id!==id||current?.revision!==revision)return;
      if(JSON.stringify(scopeRead())!==JSON.stringify(before)||$("coreClient").value!==selectedClient){showError("表单已变化，请重新选择文件识别值并确认。");return;}
      const changes=Object.entries(values).filter(([key,value])=>identityToken(key,before[key])!==identityToken(key,value));
      if(!changes.length)return;
      for(const [key,value] of changes)if(key==="range_kind")rangeKind.value=value;else $(scopeIds[key]).value=value;
      if(keys.includes("period")){writePeriodInputs(values.start_date);syncRange();closeCalendar();}
      const selected=clients.find(c=>c.id===$("coreClient").value);
      if(selected&&identityToken("tax_no",selected.taxpayer_id)!==identityToken("tax_no",$("coreTaxNo").value))$("coreClient").value="";
      scopeDirty=true;const errors=validateScope();displayErrors(attempted?errors:Object.fromEntries(Object.entries(errors).filter(([key])=>scopeRead()[key])));
    };
    for(const key of ["company_name","tax_no"]){
      const box=$(scopeIds[key]+"Candidates"),checks=candidates[key]||[],choices=identityChoices(key);box.replaceChildren();box.hidden=!checks.length;identityLinks[key]=null;
      if(!checks.length)continue;
      const agreed=choices.filter(choice=>checks.every(c=>(c.alternatives||[c.value]).some(v=>identityToken(key,v)===identityToken(key,choice.value))));
      if(agreed.length===1)unique[key]=agreed[0].value;
      const link=button("查看差异",()=>apply([key]));link.className="core-identity-link";box.append(link);identityLinks[key]=link;
    }
    const periods=identityChoices("period"),periodBox=$("corePeriodCandidates");periodBox.replaceChildren();periodBox.hidden=!periods.length;
    if(periods.length){const link=button("查看期间候选",()=>apply(["period"]));link.className="core-identity-link";periodBox.append(link);}
    if(unique.company_name&&unique.tax_no){
      actions.hidden=false;actions.append(button("一键采用文件信息",()=>apply(["company_name","tax_no",...(periods.length?["period"]:[])],unique)));
    }
  }
  function inputFor(key){return key==="range_kind"?rangeKind:["start_date","end_date"].includes(key)?rangeTrigger:$(scopeIds[key]);}
  function displayErrors(errors,keys=Object.keys(scopeIds)){for(const key of [...new Set([...keys,"range_kind"])]){if(!fieldErrors[key])continue;const message=errors[key]||"";fieldErrors[key].textContent=message;fieldErrors[key].hidden=!message;inputFor(key)?.setAttribute?.("aria-invalid",String(!!message));}rangeTrigger.setAttribute("aria-invalid",String(!fieldErrors.start_date.hidden||!fieldErrors.end_date.hidden));for(const key of ["company_name","tax_no"])if(identityLinks[key])identityLinks[key].textContent=identityErrors(scopeRead())[key]?"查看差异":"查看识别结果";}
  rangeKind.addEventListener("change",()=>{scopeDirty=true;writePeriodInputs($("coreStart").value);syncRange();displayErrors(attempted?validateScope():{});});
  for(const input of [rangeMonth,rangeYear,rangeQuarter])input.addEventListener("change",()=>{scopeDirty=true;syncRange();displayErrors(attempted?validateScope():{});});
  syncRange();
  $("coreClient").onchange=()=>{const company=clients.find(c=>c.id===$("coreClient").value);$("coreCompanyName").value=company?.name||"";$("coreTaxNo").value=company?.taxpayer_id||"";scopeDirty=true;displayErrors(attempted?validateScope():identityErrors(scopeRead()));};
  async function saveScope(confirm=false){const owner=user()?.id,ticket=generation;const result=await api(base+"/"+current.id+"/scope",json({...scopeRead(),client_id:$("coreClient").value||null,expected_revision:current.revision,confirm}));if(owner!==user()?.id||ticket!==generation)throw new Error("会话已变化，请重新读取任务。");current=result;scopeDirty=false;return result;}
  async function startCheck(){
    if(busy)return;if(dirty){showError("请先保存资料口径核对，再开始检查。");return;}
    attempted=true;const errors=validateScope();displayErrors(errors);
    if(Object.keys(errors).length){inputFor(Object.keys(errors)[0]).focus?.();return;}
    const region=$("coreRegion").value||"CN";
    if(region!=="CN"&&!regions[region]){const message="企业所在地选项无效，请重新选择；也可留空，先匹配国家级政策。";$("coreRegion").setAttribute("aria-invalid","true");$("coreActionStatus").textContent=message;showError(message);$("coreRegion").focus?.();return;}
    const owner=user()?.id,ticket=generation,id=current.id;
    const valid=()=>owner===user()?.id&&ticket===generation&&enabled()&&current?.id===id;
    const stage=message=>{checkProgress.message=message;renderChecks();};
    checkProgress={id,message:"正在核对企业与检查区间…",pending:[],done:0,total:0};checkPage=0;renderChecks();
    $("coreCheckResult").scrollIntoView?.({block:"start",behavior:"smooth"});
    await action(async()=>{
      await saveScope(true);if(!valid())return current;
      if(Object.keys(current.scope_errors||{}).length||current.task.scope_confirmed===false)
        throw new Error("企业与检查区间尚未核对通过，请补全页面提示后重试。");
      stage("正在读取检查项目与 AI 配置…");
      const [rules,status]=await Promise.all([api("/api/knowledge/checks?enterprise=true"),api("/api/knowledge/retrieval/status")]);
      if(!valid())return current;
      if(!Array.isArray(rules)||!rules.length)throw new Error("尚无可用检查项目，请先配置核查规则。");
      if(!status.ai_configured)throw new Error("尚未配置 AI 服务，请先完成 AI 服务配置后重新开始检查。");
      if(!status.acceptance?.passed)throw new Error(status.acceptance?.reason||"知识检索校验尚未通过，暂不能进行 AI 检查。");
      checkProgress.pending=rules.map(r=>r.name);checkProgress.total=rules.length;
      for(const [index,rule] of rules.entries()){
        if(!valid())return current;
        stage(`正在检索依据、组装上下文并进行 AI 分析（${index+1}/${rules.length}）：${rule.name}`);
        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),300000);
        let result;
        try{result=await api("/api/knowledge/retrieve",{...json({task_id:id,expected_revision:current.revision,
          rule_id:rule.rule_id,region_code:region,use_ai:true}),signal:controller.signal});}
        catch(error){if(controller.signal.aborted)throw new Error("AI 分析等待超时，服务端可能仍在处理。请重新读取最新任务确认结果，避免重复发起检查。");throw error;}
        finally{clearTimeout(timer);}
        if(!valid())return current;
        current.revision=result.revision;(current.tax_checks??={})[rule.rule_id]=result;
        if(result.ai?.batches?.some(b=>b.status==="failed"))throw new Error(result.ai.reason||"本批 AI 请求未完成，已保存完成批次，请稍后重新开始检查。");
        checkProgress.done++;checkProgress.pending.shift();renderChecks();
      }
      stage("正在保存并核对检查结果…");
      const fresh=await api(base+"/"+encodeURIComponent(id));if(!valid())return current;
      current=fresh;stage("检查已完成，正在生成检查报告…");
      const report=reportForTask();report.creating=true;
      try{await saveReport(fresh,report);if(!valid())return current;checkProgress.message="检查流程已完成，报告已生成，请查看报告和待复核事项。";}
      catch(error){if(valid()){report.createError=error.message;checkProgress.message="检查结果已保存，报告尚未生成，请点击生成检查报告重试。";}}
      finally{report.creating=false;}
      if(!valid())return current;
      checkProgress.complete=true;checkProgress.revision=fresh.revision;return fresh;
    },true);
    if(valid()){renderChecks();$("coreCheckResult").scrollIntoView?.({block:"start",behavior:"smooth"});}
  }
  const archiveTime=value=>new Date(value).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});
  function reportForTask(){
    if(!reportState||reportState.taskId!==current?.id||reportState.owner!==user()?.id)
      reportState={taskId:current?.id,owner:user()?.id,rows:[],loadedRevision:null,loading:false,creating:false,loadError:"",createError:""};
    return reportState;
  }
  function reportCurrent(state,ticket){return reportState===state&&generation===ticket&&current?.id===state.taskId&&user()?.id===state.owner;}
  async function loadReports(state){
    const ticket=generation,revision=current.revision,request=state.listRequest=(state.listRequest||0)+1;state.loading=true;state.loadError="";
    const valid=()=>reportCurrent(state,ticket)&&state.listRequest===request;
    try{
      const rows=await api(base+"/"+encodeURIComponent(state.taskId)+"/reports");
      if(valid()){if(!Array.isArray(rows))throw new Error("报告列表格式无效，请重试。");state.rows=rows;}
    }catch(error){if(valid())state.loadError=error.message;}
    finally{if(valid()){state.loading=false;state.loadedRevision=revision;renderChecks();}}
  }
  async function saveReport(task,state){
    const ticket=generation,row=await api(base+"/"+encodeURIComponent(task.id)+"/reports",json({expected_revision:task.revision}));
    if(!reportCurrent(state,ticket))return;
    state.listRequest=(state.listRequest||0)+1;state.loading=false;
    state.rows=[row,...state.rows.filter(r=>r.version!==row.version)];state.loadedRevision=task.revision;
    state.confirmedRevision=task.revision;state.confirmedVersion=row.version;state.createError="";state.loadError="";
  }
  async function generateReport(){
    if(busy||!current||current.completion)return;
    if(dirty||scopeDirty){showError("请先保存资料与检查区间，再生成检查报告。");return;}
    const state=reportForTask(),ticket=generation;if(state.creating||state.loading)return;
    busy=true;state.creating=true;state.createError="";taskControls(true);renderChecks();
    try{await saveReport(current,state);}
    catch(error){if(reportCurrent(state,ticket))state.createError=error.message;}
    finally{state.creating=false;if(reportCurrent(state,ticket)){busy=false;taskControls(false);renderChecks();}}
  }
  function renderReportActions(root){
    const state=reportForTask();
    if(!busy&&!state.loading&&state.loadedRevision!==current.revision)void loadReports(state);
    const box=el("section","core-report-actions");box.setAttribute("aria-label","检查报告");
    box.append(el("h4","","检查报告"));
    const completed=current.completion,latest=completed?state.rows.find(r=>r.version===completed.report_version):state.rows[0];
    const checkFailed=checkProgress?.id===current.id&&Boolean(checkProgress.error);
    const matches=latest&&(completed?latest.content_hash===completed.content_hash:!state.createError&&!checkFailed&&(latest.input_revision===current.revision||state.confirmedRevision===current.revision&&state.confirmedVersion===latest.version));
    const actions=el("div","core-report-buttons");
    const create=button(state.creating?"正在生成报告…":latest?"更新检查报告":"生成检查报告",generateReport);
    create.disabled=busy||state.loading||dirty||scopeDirty;if(!completed)actions.append(create);
    if(latest){
      const reportMessage=completed?"任务已完成并归档，以下为归档时的检查报告；待复核事项仍保留。":matches?"报告已就绪，包含已保存的检查结果与待复核事项。":checkFailed?"本次检查未完成，以下为上次保存的报告。":state.createError?"本次报告更新失败，以下为上次保存的报告。":"资料或检查结果已更新，以下为上次报告；请更新报告获取当前结果。";
      box.append(el("p",matches?"muted":"material-note",reportMessage));
      for(const [format,label] of [["html",matches?"查看报告":"查看上次报告"],["pdf",matches?"下载 PDF":"下载上次报告 PDF"]]){
        const link=el("a","btn re",label);link.href=base+"/"+encodeURIComponent(state.taskId)+"/reports/"+latest.version+"?format="+format;link.target="_blank";link.rel="noopener noreferrer";actions.append(link);
      }
    }else box.append(el("p","muted",state.creating?"正在整理并保存检查报告…":busy?"本轮检查结束后会自动生成报告。":"可将已保存的检查结果生成报告；未完成的检查不作结论。"));
    if(!completed){
      const complete=button("完成任务并归档",()=>{
        if(busy||dirty||scopeDirty||!matches||currentUpload||waitingUploads.length){showError("请先保存全部修改、完成上传并更新检查报告，再归档任务。");return;}
        return action(()=>api(base+"/"+encodeURIComponent(current.id)+"/complete",json({expected_revision:current.revision,report_version:latest.version})));
      });
      complete.disabled=busy||state.loading||dirty||scopeDirty||!matches||!!currentUpload||!!waitingUploads.length;actions.append(complete);
      box.append(el("p","muted","归档将结束本次任务并保留当前报告及待复核事项，不代表检查通过。后续补传或重检可重新打开任务。"));
    }
    box.append(actions);
    if(state.loading)box.append(el("p","muted","正在读取报告…"));
    if(state.loadError){box.append(el("p","material-note","报告读取失败："+state.loadError));const retry=button("重试读取报告",()=>{state.loadedRevision=null;renderChecks();});retry.disabled=busy;box.append(retry);}
    if(state.createError)box.append(el("p","material-note","检查结果已保留，报告生成失败："+state.createError));
    root.append(box);
  }
  function checkDisclosure(key,label,className=""){
    const details=el("details",("core-check-disclosure "+className).trim()),heading=el("summary","",label);
    const toggle=el("span","core-check-toggle");heading.append(toggle);details.append(heading);
    details.open=checkFolds.get(key)===true;
    const sync=()=>{checkFolds.set(key,details.open);heading.setAttribute("aria-expanded",String(details.open));toggle.textContent=details.open?"收起":"展开";};
    // Handle the title once, including native keyboard activation, without a second default toggle.
    heading.onclick=event=>{event.preventDefault();details.open=!details.open;sync();};
    details.addEventListener("toggle",event=>{if(event.target===details&&details.isConnected)sync();});sync();
    return {details,heading,close:()=>{details.open=false;sync();heading.focus?.({preventScroll:true});heading.scrollIntoView?.({block:"nearest"});}};
  }
  function legacyCheck(slot){
    return slot.bundle?.batching?.strategy!=="category_and_source_v1"||!slot.bundle?.batching?.model_context;
  }
  function renderReviewGaps(card,pack,legacy,foldKey){
    const categories=new Map([
      ["facts",{name:"指标与材料",hint:"展开查看具体指标，并在下方核对数据与材料来源；未取得有效值不一定是少上传了文件。",groups:new Map()}],
      ["policy",{name:"政策与规则",hint:"这些是政策效力、适用范围或关联关系的核实事项，需要核实依据，不能仅靠补传企业材料解决。",groups:new Map()}],
      ["system",{name:"系统处理",hint:"这些是处理过程中留下的限制或错误，请结合本次处理状态核对。",groups:new Map()}],
      ["other",{name:"其他核实事项",hint:"请核对以下事项及其对应对象。",groups:new Map()}]
    ]);
    const facts=new Map((pack.facts||[]).map(f=>[f.metric_id,f.name||f.metric_id]));
    const policies=new Map([...(pack.policy_evidence||[]),...(pack.policy_leads||[])].map(p=>[p.clause_id,p]));
    const conditions=new Map((pack.conditions||[]).map(c=>[c.condition_id,c]));
    const exceptions=new Map((pack.exceptions||[]).map(e=>[e.exception_id,e]));
    const relationships=new Map((pack.relationships||[]).map(r=>[r.relationship_id,r]));
    const archived=new Set();
    const policyName=id=>{const p=policies.get(id);return p?[p.title,p.source_location,id].filter(Boolean).join(" · "):id;};
    const metricName=id=>facts.get(id)||pack.metric_definitions?.[id]?.name||id;
    for(const gap of pack.gaps||[]){
      if(!gap.message)continue;
      if(legacy&&gap.code?.startsWith("budget_")){archived.add(gap.message);continue;}
      const kind=/^(metric_|comparability_)/.test(gap.code||"")?"facts":/^(budget_|semantic_|retrieval_)/.test(gap.code||"")?"system":/^(policy_|rule_|relationship_|change_|required_clause_|entity_|exception_)/.test(gap.code||"")?"policy":"other";
      const category=categories.get(kind),key=JSON.stringify([gap.code||"",gap.message.trim()]);
      if(!category.groups.has(key))category.groups.set(key,{message:gap.message.trim(),objects:new Map()});
      let object="本项检查";
      if(gap.metric_id)object=metricName(gap.metric_id);
      else if(gap.clause_id)object=policyName(gap.clause_id)+(gap.relationship_id?" · 关联记录 "+gap.relationship_id:"");
      else if(gap.condition_id){const c=conditions.get(gap.condition_id);object=c?[metricName(c.left_metric_id),metricName(c.right_metric_id)].filter(Boolean).join(" 与 "):gap.condition_id;}
      else if(gap.relationship_id){const r=relationships.get(gap.relationship_id);object=r?[policyName(r.from_id),policyName(r.to_id)].filter(Boolean).join(" → "):"关联记录 "+gap.relationship_id;}
      else if(gap.exception_id)object=exceptions.get(gap.exception_id)?.name||"例外记录 "+gap.exception_id;
      else if(gap.entity_id)object="适用主体 "+gap.entity_id;
      else if(gap.rule_id)object=pack.check.topic||gap.rule_id;
      // Deduplicate the same issue and source while retaining separate source records with equal labels.
      const identity=JSON.stringify([gap.metric_id,gap.clause_id,gap.condition_id,gap.relationship_id,gap.exception_id,gap.entity_id,gap.rule_id]);
      category.groups.get(key).objects.set(identity,object);
    }
    const active=[...categories.values()].filter(c=>c.groups.size);
    const count=c=>[...c.groups.values()].reduce((sum,g)=>sum+g.objects.size,0);
    if(active.length)card.append(el("p","material-note","待核实："+active.map(c=>`${c.name} ${count(c)} 项`).join("；")+"。同类原因已合并，可展开查看具体对象。"));
    for(const [kind,category] of categories){
      if(!category.groups.size)continue;
      const {details}=checkDisclosure(`${foldKey}:gaps:${kind}`,`${category.name}：${category.groups.size} 类问题 · ${count(category)} 项`,"material-card");
      details.append(el("p","muted",category.hint));
      for(const group of category.groups.values()){
        details.append(el("p","material-note",group.message));
        const list=el("ul","muted");for(const object of group.objects.values())list.append(el("li","",object));
        details.append(list);
      }
      card.append(details);
    }
    if(legacy&&pack.batching?.unsplittable&&!archived.size)archived.add("旧版分批记录：当时未能在预算内组织完整上下文。");
    return archived;
  }
  function renderChecks(){
    const root=$("coreCheckResult");root.replaceChildren();
    const scope=JSON.stringify([user()?.id,current?.id]);if(scope!==checkFoldScope){checkFolds.clear();checkFoldScope=scope;checkPage=0;}
    const progress=checkProgress?.id===current?.id&&(!checkProgress.complete||checkProgress.revision===current?.revision)?checkProgress:null,checks=Object.values(current?.tax_checks||{}).filter(slot=>slot.bundle);
    const pages=Math.max(1,Math.ceil(checks.length/CHECK_PAGE_SIZE));checkPage=Math.max(0,Math.min(checkPage,pages-1));
    root.hidden=!progress&&!checks.length;if(root.hidden)return;
    root.append(el("h3","",progress?.error?"检查未完成":progress&&!progress.complete?"正在检查":"检查结果"));
    const status=el("p",progress?.error?"material-note":"muted",progress?.error||progress?.message||"以下为本任务已保存的检查结果。");
    status.setAttribute("role","status");root.append(status);
    if(progress?.total)root.append(el("p","muted",`本轮已完成 ${progress.done}/${progress.total} 项。未完成不等于通过。`));
    if(progress?.error&&progress.pending.length){
      const {details:pending}=checkDisclosure("pending",`尚未完成 ${progress.pending.length} 项（展开查看）`);
      const list=el("ul","muted");for(const name of progress.pending)list.append(el("li","",name));
      pending.append(list);root.append(pending);
    }
    if(progress?.error)root.append(button("重新读取最新任务",()=>{checkProgress=null;return action(()=>api(base+"/"+encodeURIComponent(current.id)));}));
    if(checks.length)renderReportActions(root);
    const legacyCount=checks.filter(slot=>slot.bundle&&legacyCheck(slot)).length;
    if(legacyCount)root.append(el("p","material-note",`已保存的检查中有 ${legacyCount} 项旧版结果，需点击「开始检查」重新生成。重新读取任务不会自动重算。`));
    const pager=position=>{
      const nav=el("nav","core-pagination core-check-pagination");nav.setAttribute("aria-label",`检查结果分页（${position}）`);
      const move=delta=>{checkPage+=delta;renderChecks();root.setAttribute("tabindex","-1");root.focus?.({preventScroll:true});root.scrollIntoView?.({block:"start"});};
      const previous=button("上一页",()=>move(-1)),next=button("下一页",()=>move(1));previous.disabled=checkPage===0;next.disabled=checkPage===pages-1;
      const description=el("span","muted",`共 ${checks.length} 项 · 第 ${checkPage+1}/${pages} 页 · ${checkPage*CHECK_PAGE_SIZE+1}–${Math.min((checkPage+1)*CHECK_PAGE_SIZE,checks.length)} 项`);
      nav.append(previous,description,next);return nav;
    };
    if(pages>1)root.append(pager("顶部"));
    for(const slot of checks.slice(checkPage*CHECK_PAGE_SIZE,(checkPage+1)*CHECK_PAGE_SIZE)){
      const pack=slot.bundle,ai=slot.ai;if(!pack)continue;
      const legacy=legacyCheck(slot);
      const label=slot.stale?"资料有变化，待重检":legacy?"旧版结果，需重新检查":!ai?"尚未完成 AI 分析":!ai.validated||ai.status==="needs_review"?"待复核":"AI 分析已完成";
      const foldKey=`rule:${pack.check.rule_id}`,{details:card,close}=checkDisclosure(foldKey,`${pack.check.topic} · ${label}`,"material-card");
      if(slot.stale)card.append(el("p","material-note","以下是历史结果，不能代表当前资料。请重新开始检查。"));
      if(legacy)card.append(el("p","material-note","这份结果生成于旧版检查流程，尚未按当前容量和分类批次重检。请点击「开始检查」重新生成；以下缺口为当时记录。"));
      if(ai?.reason&&!legacy&&!(pack.gaps||[]).some(g=>g.message===ai.reason))card.append(el("p","material-note",ai.reason));
      const capacity=pack.batching?.model_context;
      if(capacity)card.append(el("p","muted",(capacity.context_window?`模型上下文：${capacity.context_window.toLocaleString()} Token。`:"上游未提供模型上下文容量，使用保守容量。")+`本次单批输入估算上限 ${capacity.input_budget.toLocaleString()}（含缓存输入），输出预留 ${capacity.output_reserve.toLocaleString()} Token。`));
      if(ai?.batches?.length){
        const {details:batches}=checkDisclosure(`${foldKey}:batches`,`分析批次：已完成 ${ai.batches.filter(b=>b.status==="completed").length}/${pack.batching.part_count} 批`);
        for(const batch of ai.batches)batches.append(el("p","muted",`${batch.part_number}/${pack.batching.part_count} · ${batch.category_name}${batch.group?" · "+batch.group:""} · ${batch.status==="completed"?"已完成":"待复核"}`));
        card.append(batches);
      }
      const assessments=legacy?checkDisclosure(`${foldKey}:assessments`,"历史分析说明（仅供追溯）").details:card;
      if(legacy&&ai?.assessments?.length)card.append(assessments);
      for(const item of ai?.assessments||[]){
        assessments.append(el("p","",item.text));
        if(item.required_materials?.length)assessments.append(el("p","material-note","需补充或核实："+item.required_materials.join("；")));
      }
      const archived=renderReviewGaps(card,pack,legacy,foldKey);
      if(legacy&&ai?.reason)archived.add(ai.reason);
      if(archived.size){const {details:history}=checkDisclosure(`${foldKey}:history`,"旧版处理记录（仅供追溯）");for(const message of archived)history.append(el("p","muted",message));card.append(history);}
      const {details:facts}=checkDisclosure(`${foldKey}:facts`,"核对数据与材料来源");
      for(const fact of pack.facts||[]){
        facts.append(el("p","",`${fact.name}：${fact.state==="known"?amount(fact.value)+" "+(fact.unit||""):"待补充或核实"}`));
        for(const source of fact.source_ids||[]){
          const row=current.processing.records.find(r=>r.id===source),doc=current.documents.find(d=>d.id===(row?.source_file_id||source.replace(/^file:/,"")));
          if(doc)facts.append(el("p","muted",[doc.name,row?.source_position].filter(Boolean).join(" · ")));
        }
      }
      card.append(facts);
      const {details:basis}=checkDisclosure(`${foldKey}:basis`,"参考依据与核实事项");
      for(const item of [...(pack.policy_evidence||[]),...(pack.policy_leads||[])]){
        basis.append(el("p","",[item.title,item.clause_id].filter(Boolean).join(" · ")),el("p","muted",item.text));
        if(/^https?:\/\//i.test(item.source_url||"")){const link=el("a","","查看原文");link.href=item.source_url;link.target="_blank";link.rel="noopener noreferrer";basis.append(link);}
      }
      card.append(basis,button("收起本项",close));root.append(card);
    }
    if(pages>1)root.append(pager("底部"));
    root.append(el("p","muted","AI 分析为待复核线索；资料或政策依据不足的项目保留待核实状态。未选择企业所在地时先匹配国家级政策；地方政策须确认地区后再核查。"));
    if(checks.length){const evidence=button("查看知识依据图谱",()=>continueCheck(current.id));evidence.disabled=busy;root.append(evidence);}
  }

  const enabled = () => ["org_admin", "accountant"].includes(user()?.role);
  const states = {needs_completion:"待补充或核对",ready_for_analysis:"资料已就绪，可继续核查",parsing:"正在解析"};
  const kinds = {account_balance:"科目余额表",vat_declaration:"增值税申报表",invoice_detail:"发票明细"};
  const json = value => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
  const button = (text, fn) => {const n=el("button","btn re",text);n.type="button";n.onclick=fn;return n;};
  function clearScope(){regionTaskId=null;$("coreRegion").value="";$("coreRegion").setAttribute("aria-invalid","false");for(const id of Object.values(scopeIds))$(id).value="";rangeKind.value="custom";writePeriodInputs("");syncRange();$("coreClient").value="";scopeDirty=attempted=false;displayErrors({});}
  function reset() {reportState=null;checkFolds.clear();checkFoldScope=null;checkPage=0;checkProgress=null;$("coreCheckResult").replaceChildren();$("coreCheckResult").hidden=true;$("coreActionStatus").replaceChildren();generation++;activeUpload?.abort();activeUpload=null;currentUpload?.finish(false);waitingUploads.splice(0).forEach(item=>item.finish(false));uploadRun=currentUpload=null;uploadsPaused=false;uploadStates.clear();uploadViews.clear();current=null;busy=dirty=scopeDirty=attempted=false;closeCalendar();if(deleteDialog.open)deleteDialog.close("cancel");if(identityDialog.open)identityDialog.close("cancel");displayErrors({});renderIdentityCandidates();clients=[];$("coreClient").replaceChildren();$("coreTaskResult").replaceChildren();$("coreTaskHistory").replaceChildren();$("coreArchivedTasks").replaceChildren();$("coreArchivedTasks").hidden=true;taskControls(false);if(formOwner!==user()?.id||!enabled())clearScope();formOwner=user()?.id;}
  async function action(fn, saving=false, uploading=false) {
    if(busy||!enabled())return;
    if(waitingUploads.length&&!uploading){showError("还有等待上传的文件，请先继续上传或清除待上传文件。");return;}
    if((dirty||scopeDirty&&current)&&!saving){showError("存在未保存的资料口径核对，请先保存后再补传、创建或读取任务。");return;}
    busy=true;taskControls(true);$("coreActionStatus").textContent="正在处理，请稍候…";
    const owner=user()?.id, ticket=generation, previousId=current?.id;
    try {const result=await fn();if(owner===user()?.id&&ticket===generation&&enabled()){if(previousId!==result.id){attempted=false;uploadStates.clear();}current=result;dirty=false;scopeWrite();render();$("coreActionStatus").textContent="";taskControls(true);history().catch(error=>{if(owner===user()?.id&&ticket===generation)$("coreActionStatus").textContent="任务已更新，但历史列表未能刷新："+error.message;});return true;}}
    catch(error){if(owner===user()?.id&&ticket===generation){$("coreActionStatus").textContent=error.message;if(checkProgress?.id===current?.id&&!checkProgress.complete){checkProgress.error=error.message;}showError(error.message);}}
    finally{if(ticket===generation){busy=false;taskControls(false);renderChecks();}}
  }
  async function removeCurrent(){
    if(busy||!current||user()?.role!=="org_admin")return;
    if(waitingUploads.length){showError("请先处理或清除等待上传的文件，再删除任务。");return;}
    const owner=user()?.id,ticket=generation,id=current.id,revision=current.revision;
    const approved=await confirmDelete(current.task.company_name||"待识别企业");
    if(!approved||busy||ticket!==generation||owner!==user()?.id||user()?.role!=="org_admin"||current?.id!==id)return;
    busy=true;$("coreTaskForm").disabled=true;$("coreTaskResult").querySelectorAll("button,input,select").forEach(n=>n.disabled=true);
    try{
      const result=await api(base+"/"+encodeURIComponent(id),{...json({expected_revision:revision,confirm:true}),method:"DELETE"});
      if(ticket!==generation||owner!==user()?.id||current?.id!==id)return;
      if(result.deleted!==true||result.id!==id)throw new Error("任务删除未完成，请重新读取后核对。");
      current=null;dirty=false;clearScope();render();$("coreTaskHistory").replaceChildren();await history();
    }catch(error){if(ticket===generation&&owner===user()?.id)showError(error.message);}
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
  function recordTable(doc, options, reviewed, statuses, directions, prepared) {
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
        const value=record[key],cell=el("td",numeric.has(key)?"core-number":"");cell.setAttribute("data-label",name);
        if(key==="direction"){
          const automatic=Object.hasOwn(record,"automatic_direction")?record.automatic_direction:prepared.get(record.id)?.direction??record.direction;
          const label=({input:"进项",output:"销项","进项":"进项","销项":"销项"})[automatic]||"待核对";
          const choice=select(doc.name+" · "+record.source_position+" · "+(record.invoice_no||"票号待核对")+" 购销方向",[["","自动 · "+label],["input","进项"],["output","销项"]],options.invoice_directions?.[record.id],"");
          choice.input.className="core-direction-select";choice.input.title="默认自动判断；可仅核对本条发票，保存后生效。";
          choice.input.addEventListener("change",()=>{dirty=true;reviewed.input.checked=false;reviewed.updateState();});
          cell.append(choice.wrap);directions.push({id:record.id,input:choice.input});
        }else cell.append(el("span","",numeric.has(key)?amount(value):value===null||value===undefined||value===""?"待识别":String(value)));
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
    uploadViews.clear();
    const root=$("coreTaskResult");root.replaceChildren();renderIdentityCandidates();renderChecks();if(!current)return;
    const task=current.task,p=current.processing,prepared=new Map(p.records.map(r=>[r.id,r])),parsedCount=current.documents.reduce((sum,d)=>sum+d.records.length,0),scopePending=Object.keys(current.scope_errors||{}).length>0;
    const rangeLabels={month:"按月",quarter:"按季度",year:"按年度",custom:"自定义起止日"};
    root.append(el("h3","",`${task.company_name||"待识别企业"} · ${task.tax_no||"税号待填写"}`),el("p","muted",`${task.start_date||"起始日待确认"} 至 ${task.end_date||"终止日待确认"} · ${rangeLabels[task.range_kind||current.range_kind]||""}`),
      el("p",p.status==="ready_for_analysis"?"":"material-note",states[p.status]||p.status),el("p","muted",Object.keys(current.tax_checks||{}).length?"检查结果见上方；资料更新后需重新检查。":"资料已保存。核对后点击开始检查，自动检索依据并进行 AI 辅助分析。"));
    if(current.completion){
      const archived=current.completion;
      root.append(el("p","material-note",`已完成并归档 · 报告 V${archived.report_version} · ${archiveTime(archived.completed_at)}`),
        el("p","muted","本次任务已结束，材料与报告已保留。归档不代表检查通过；需要补传或重检时请先重新打开任务。"),
        button("重新打开任务",()=>action(()=>api(base+"/"+encodeURIComponent(current.id)+"/reopen",json({expected_revision:current.revision})))),
        button("重新读取任务",()=>action(()=>api(base+"/"+encodeURIComponent(current.id)))));
      for(const doc of current.documents){const link=el("a","btn re","下载原件 · "+doc.name);link.href=base+"/"+encodeURIComponent(current.id)+"/originals/"+encodeURIComponent(doc.id);root.append(link);}
      return;
    }
    const actions=el("div","core-actions");actions.append(button("重新读取任务",()=>action(()=>api(base+"/"+current.id))));
    actions.append(button("保存企业与检查区间",()=>{if(dirty){showError("请先保存资料口径核对，再保存企业与检查区间。");return;}return action(()=>saveScope(),true);}));
    const next=button("开始检查",startCheck);actions.append(next);root.append(actions,el("p","muted","点击开始检查后，自动检索知识依据、整理材料上下文并调用已配置的 AI，在本页展示结果。"));
    if(user()?.role==="org_admin"){const remove=button("删除本次任务",removeCurrent);remove.style.color="var(--red,#b42318)";actions.append(remove);}
    const origins=task.scope_origins||{};for(const [key,origin] of Object.entries(origins))if(origin.kind==="file"&&["company_name","tax_no"].includes(key))root.append(el("p","muted",`${{company_name:"企业名称",tax_no:"税号"}[key]}已从文件读取：${(origin.sources||[]).map(s=>`${current.documents.find(d=>d.id===s.file_id)?.name||s.file_id} · ${s.position}`).join("；")}，请核对后开始检查。`));
    displayErrors(attempted?current.scope_errors||{}:Object.fromEntries(Object.entries(current.scope_errors||{}).filter(([key])=>scopeRead()[key])));
    const saved=Object.values(current.tax_checks||{});
    if(saved.length)root.append(el("p","material-note",`已保存 ${saved.length} 项核查；${saved.filter(s=>s.stale).length} 项因资料变化待重检。结果已展示在上方，可展开查看依据。`));
    const slots=el("div","core-slots");
    for(const [kind,name] of Object.entries(kinds)) {
      const card=el("section","material-card");card.append(el("h4","",name+"（必传）"));
      card.append(el("p","muted",kind==="account_balance"?"Excel / PDF 科目汇总余额；不是逐笔明细账。":kind==="vat_declaration"?"Excel / PDF 主表及适用附表，可多文件合并期间。":"Excel / PDF 发票 / 数电 XML，须覆盖进销项；按开具日期统计。"));
      const pick=el("input");pick.type="file";pick.multiple=true;pick.accept=kind==="invoice_detail"?".xlsx,.xls,.xml,.pdf":".xlsx,.xls,.pdf";pick.setAttribute("aria-label",name+"上传");
      pick.coreUploadPicker=true;pick.onchange=()=>{const files=Array.from(pick.files);pick.value="";return queueUpload(kind,files);};
      const status=el("div","core-upload-status"),bar=el("progress"),statusText=el("p");status.hidden=true;status.dataset.phase="idle";
      bar.max=100;bar.value=0;bar.setAttribute("aria-label",name+"文件传输进度");statusText.setAttribute("role","status");statusText.setAttribute("aria-live","polite");
      const waiting=el("p","core-upload-waiting"),controls=el("div","core-upload-queue-actions"),resume=button("读取任务并继续上传",resumeUploads),remove=button("清除待上传",()=>clearWaiting(kind));
      waiting.setAttribute("role","status");resume.coreUploadQueueControl=remove.coreUploadQueueControl=true;controls.append(resume,remove);
      status.append(bar,statusText,waiting,controls);uploadViews.set(kind,{root:status,bar,text:statusText,waiting,controls,resume});paintUpload(kind);
      const upload=el("div","core-upload-control");pick.disabled=busy&&!uploadRun;upload.append(pick,status);
      card.append(upload,el("p","muted",`已保存 ${current.documents.filter(d=>d.material_type===kind).length} 份。`));slots.append(card);
    }
    root.append(slots);
    const excluded=(current.files||[]).filter(file=>current.source_options[file.id]?.excluded&&!file.deleted_at);
    if(excluded.length){const box=el("section","material-card");box.append(el("h4","","已排除的资料（原件保留，可恢复）"));for(const file of excluded){const options=current.source_options[file.id];box.append(el("p","muted",options.original_name),button("恢复资料："+options.original_name,()=>action(()=>api(base+"/"+current.id+"/prepare",json({expected_revision:current.revision,source_options:{[file.id]:{excluded:false,ocr:true}},mapping_mode:"ai"})))));}root.append(box);}
    const review=el("fieldset");review.disabled=busy;
    review.addEventListener("input",()=>{dirty=true;});review.addEventListener("change",()=>{dirty=true;});
    review.append(el("legend","","资料口径核对"),el("p","muted","仅填写已核实的信息，留空沿用文件识别值。填写的是人工确认信息，不会改写原件或声称文件已经证明。一个文件的统一口径会应用到其所有工作表。所有材料须在所选检查区间内；区间外或日期无法识别的记录会保留原值与来源位置，但不纳入本次检查。"));
    const applicable=field("本期适用附表名称（以分号分隔）","text",(task.applicable_forms||[]).join("；"));
    const scopeKnown=field("已核实本期适用附表清单（如无适用附表，也须明确确认）","checkbox","");scopeKnown.input.checked=task.applicable_forms!==null;
    const formScope=el("details","core-metadata");formScope.append(el("summary","","申报附表适用范围（核对申报表时填写）"),applicable.wrap,scopeKnown.wrap);
    review.append(el("p","core-candidate-count",`已解析候选 ${parsedCount} 条`),el("p","muted",scopePending?"先核对下方文件候选，并补全企业与检查区间，再整理标准记录；候选条数不代表资料已齐全。":"下方按文件展示解析候选，尚需核对原件；标准记录、重复来源和检查区间以整理结果为准。"));
    const editors=[];
    for(const doc of current.documents){
      const card=el("details","core-document"),options=current.source_options[doc.id]||{},meta=options.confirmed_metadata||{};
      const foldKey="document:"+doc.id;card.open=checkFolds.get(foldKey)===true;
      const heading=el("summary","core-document-heading");heading.append(el("span","core-file-name",doc.name),el("span","core-file-state",`${kinds[doc.material_type]} · ${doc.records.length} 条候选 · ${options.source_reviewed?"已人工核对":"待核对"}`));card.append(heading);
      const syncFold=()=>{checkFolds.set(foldKey,card.open);heading.setAttribute("aria-expanded",String(card.open));};
      heading.onclick=event=>{event.preventDefault();card.open=!card.open;syncFold();};
      card.addEventListener("toggle",event=>{if(event.target===card&&card.isConnected)syncFold();});syncFold();
      const content=el("div","core-document-body"),download=el("a","btn re","下载原件");download.href=base+"/"+current.id+"/originals/"+doc.id;content.append(download);card.append(content);
      const metadata=el("details","core-metadata");metadata.append(el("summary","","文件口径与排除设置（选填）"),el("p","muted","留空沿用识别值；统一口径应用到本文件所有记录，混合期间请保留原识别值。"));
      const grid=el("div","material-company"+(doc.material_type==="invoice_detail"?" core-invoice-metadata":""));
      const unit=select(doc.name+" 确认单位",[["","沿用识别值"],["元","元"],["千元","千元"],["万元","万元"],["亿元","亿元"]],meta.unit,"金额单位");
      const start=field(doc.name+" 确认覆盖起始日","date",meta.period_start,"覆盖起始日"),end=field(doc.name+" 确认覆盖终止日","date",meta.period_end,"覆盖终止日");
      const form=select(doc.name+" 确认表单类型",[["","沿用识别值"],["main","主表"],["supplement","附表"]],meta.form_type,"表单类型");
      grid.append(unit.wrap,start.wrap,end.wrap);if(doc.material_type==="vat_declaration")grid.append(form.wrap);
      const exclude=field(doc.name+" 排除此资料（原件与历史版本保留）","checkbox","","排除此资料（原件与历史版本保留）");exclude.input.checked=!!options.excluded;metadata.append(grid,exclude.wrap);content.append(metadata);
      const reviewed=field(doc.name+" 已对照原页核实提取候选的字段、金额、购销方和所属期","checkbox","","已对照原页核实字段、金额、购销方和所属期");reviewed.input.checked=!!options.source_reviewed;
      reviewed.updateState=()=>{heading.children[1].textContent=`${kinds[doc.material_type]} · ${doc.records.length} 条候选 · ${reviewed.input.checked?"已人工核对":"待核对"}`;};reviewed.input.addEventListener("change",reviewed.updateState);
      const statuses=[],directions=[];
      if(doc.material_type==="invoice_detail"&&doc.name.toLowerCase().endsWith(".pdf")){
        content.append(el("p","material-note","票面未记载的红蓝字、作废状态须逐票核实；留空会保留资料缺口。单张发票不能证明整期进销项齐全。"));
      }
      content.append(el("p","muted","金额按解析候选的原单位展示，不在此处换算或汇总；单位及来源口径见下方详情。"),recordTable(doc,options,reviewed,statuses,directions,prepared));
      if(doc.ocr_candidate||doc.pdf_candidate)content.append(reviewed.wrap);
      for(const f of [unit,start,end,form])f.input.addEventListener("change",()=>{reviewed.input.checked=false;reviewed.updateState();});
      const parts=el("details"),summary=el("summary","","识别到的来源口径");parts.append(summary);
      parts.append(el("p","core-checksum",`SHA-256 ${doc.sha256}`));
      for(const part of doc.parts)parts.append(el("p","muted",`${part.name} · ${{main:"主表",supplement:"附表"}[part.form_type]||"表单类型待核对"} · ${part.period?.join(" 至 ")||"期间未识别"} · ${part.unit||"单位未识别"}`));content.append(parts);
      review.append(card);editors.push({id:doc.id,unit,start,end,form,exclude,reviewed,statuses,directions});
    }
    review.append(formScope);
    let readPolicy=null;
    if(policyCatalog&&policyUI()){
      const policy=el("details","core-policy"),panel=el("div");policy.append(el("summary","","政策条件（选填）"),panel);review.append(policy);
      readPolicy=policyUI().editor(panel,policyCatalog,current.policy_input,current.policy_assessment,()=>{dirty=true;},()=>{dirty=true;},()=>{});
      panel.append(el("p","muted",`目录来源核对截至 ${policyCatalog.checked_as_of}；政策条件与本任务资料版本绑定，单独复核，不替代知识图谱的规则审核。`));
      if(current.policy_error)panel.append(el("p","material-note",current.policy_error));
    }
    const save=button("保存核对并重新整理资料",()=>{const policyInput=readPolicy?.();return action(async()=>{if(scopeDirty)await saveScope();const options={};for(const e of editors)options[e.id]={excluded:e.exclude.input.checked,source_reviewed:e.reviewed.input.checked,ocr:true,invoice_directions:Object.fromEntries(e.directions.filter(d=>d.input.value).map(d=>[d.id,d.input.value])),invoice_statuses:Object.fromEntries(e.statuses.map(s=>[s.id,{red_letter_status:s.red.input.value,void_status:s.voided.input.value}])),confirmed_metadata:{unit:e.unit.input.value,period_start:e.start.input.value,period_end:e.end.input.value,form_type:e.form.input.value}};
      return api(base+"/"+current.id+"/prepare",json({expected_revision:current.revision,applicable_forms:scopeKnown.input.checked?applicable.input.value.split(/[;；]/).map(s=>s.trim()).filter(Boolean):null,source_options:options,mapping_mode:"ai",...(readPolicy?{policy_input:policyInput}:{})}));},true);});review.append(save);root.append(review);
    const problems=el("section","enterprise-feedback"+(p.issues.length?" blocking":""));problems.append(el("h4","",scopePending?"资料缺口与冲突（待核对检查范围）":`资料缺口与冲突（${p.issues.length}）`));if(scopePending)problems.append(el("p","","企业或检查区间尚未完整，资料完整性与区间冲突检查尚未完成；请先补全并保存范围。"));
    const issueGroups=new Map();for(const item of p.issues){const key=JSON.stringify([item.code,item.message]);if(!issueGroups.has(key))issueGroups.set(key,[]);issueGroups.get(key).push(item);}
    for(const items of issueGroups.values()){
      const locations=items.map(item=>[current.documents.find(d=>d.id===item.source_file_id)?.name,item.source_position].filter(Boolean).join(" · "));
      if(items.length===1)problems.append(el("p","",[items[0].message,locations[0]].filter(Boolean).join(" · ")));
      else{const group=el("details");group.append(el("summary","",`${items[0].message}（${items.length} 处）`));for(const location of locations)group.append(el("p","muted",location||"任务整体口径"));problems.append(group);}
    }root.append(problems);
    if(p.notices.length){const notes=el("details");notes.append(el("summary","",`来源说明（${p.notices.length}）`));for(const n of p.notices)notes.append(el("p","muted",n.message+(n.source_position?" · "+n.source_position:"")));root.append(notes);}
  }
  async function history(){const owner=user()?.id,ticket=generation,rows=await api(base);if(owner!==user()?.id||ticket!==generation||!enabled())return;const root=$("coreTaskHistory");root.replaceChildren();for(const archived of [false,true]){const group=rows.filter(row=>(row.status==="archived")===archived);if(archived&&!group.length)continue;root.append(el("h3","",archived?"已完成并归档的任务":"继续资料处理任务"));for(const row of group)root.append(button(`${row.task.company_name||"待识别企业"} · ${row.task.start_date||"待确认"} 至 ${row.task.end_date||"待确认"} · ${archived?"已完成并归档":states[row.status]||row.status}`,()=>action(()=>api(base+"/"+row.id))));}}
  async function refresh(){const active=enabled();if(formOwner!==user()?.id){reset();clearScope();formOwner=user()?.id;}$("coreInputs").hidden=!active;$("coreLegacyToggle").hidden=!active;if(active){const owner=user()?.id,ticket=generation;if(policyUI()){const config=await api("/api/enterprise/materials/config");if(owner!==user()?.id||ticket!==generation)return;policyCatalog=config.policy_catalog;}const rows=await api("/api/clients");if(owner!==user()?.id||ticket!==generation||!enabled())return;clients=rows;const selected=$("coreClient").value;$("coreClient").replaceChildren();const empty=el("option","","新企业 / 从文件读取");empty.value="";$("coreClient").append(empty);for(const c of clients){const option=el("option","",`${c.name} · ${c.taxpayer_id}`);option.value=c.id;$("coreClient").append(option);}$("coreClient").value=selected;await history();}}
  async function openTask(id){return action(()=>api(base+"/"+encodeURIComponent(id)));}
  let archiveRequest=0;
  async function renderArchived(){
    const root=$("coreArchivedTasks"),owner=user()?.id,ticket=generation,request=++archiveRequest;
    root.replaceChildren();root.hidden=!enabled();if(root.hidden)return;
    const valid=()=>owner===user()?.id&&ticket===generation&&request===archiveRequest&&enabled();
    root.append(el("h2","","最近已归档检查任务"),el("p","muted","保留归档时的报告和待复核事项；可打开任务查看材料，重新打开后补传或重检。"));
    try{
      const rows=await api(base);if(!valid())return;
      const archived=rows.filter(row=>row.status==="archived"&&row.completion);
      if(!archived.length)root.append(el("p","muted","暂无已归档检查任务。"));
      for(const row of archived){
        const item=el("section","data-item"),actions=el("div","row");
        item.append(el("h3","",row.task.company_name||"待识别企业"),el("p","muted",`${row.task.start_date} 至 ${row.task.end_date} · 已完成并归档 · ${archiveTime(row.completion.completed_at)}`));
        actions.append(button("查看归档任务",async()=>{if(await openTask(row.id))openTaskPanel();}));
        for(const [format,label] of [["html","查看归档报告"],["pdf","下载归档 PDF"]]){const link=el("a","btn re",label);link.href=base+"/"+encodeURIComponent(row.id)+"/reports/"+row.completion.report_version+"?format="+format;link.target="_blank";link.rel="noopener noreferrer";actions.append(link);}
        item.append(actions);root.append(item);
      }
    }catch(error){if(valid())root.append(el("p","material-note","归档任务读取失败："+error.message));}
  }
  return {refresh,reset,openTask,renderArchived};
}
