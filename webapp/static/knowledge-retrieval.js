"use strict";
(() => {
  const $kg=id=>document.getElementById(id);
  let generation=0,owner=null,tasks=[],config=null,busy=false,last=null,requested=null,detail=null,reports=[];
  const regions={"11":"北京市","12":"天津市","13":"河北省","14":"山西省","15":"内蒙古自治区","21":"辽宁省","22":"吉林省","23":"黑龙江省","31":"上海市","32":"江苏省","33":"浙江省","34":"安徽省","35":"福建省","36":"江西省","37":"山东省","41":"河南省","42":"湖北省","43":"湖南省","44":"广东省","45":"广西壮族自治区","46":"海南省","50":"重庆市","51":"四川省","52":"贵州省","53":"云南省","54":"西藏自治区","61":"陕西省","62":"甘肃省","63":"青海省","64":"宁夏回族自治区","65":"新疆维吾尔自治区"};
  for(const [code,name]of Object.entries(regions))option($kg("kgRegion"),"CN-"+code,name);
  function current(ticket,identity){return generation===ticket&&currentUser?.id===identity&&$kg("knowledgePanel").classList.contains("active");}
  function message(text){$kg("kgRetrievalStatus").textContent=text;}
  function reset(){generation++;owner=null;tasks=[];config=null;last=null;requested=null;detail=null;reports=[];busy=false;$kg("kgEvidenceFields").disabled=false;for(const id of ["kgEvidenceResult","kgTaskChecks","kgReportVersions"])$kg(id).replaceChildren();$kg("kgReportStatus").textContent="";$kg("kgTask").replaceChildren();option($kg("kgTask"),"","仅检索政策，不读取企业资料");$kg("kgRule").replaceChildren();$kg("kgStart").value="";$kg("kgEnd").value="";$kg("kgTopic").value="";$kg("kgRegion").value="";message("");buttons();}
  function buttons(){const task=$kg("kgTask").value;$kg("kgAnalyze").disabled=busy||!task||!detail||!config?.acceptance?.passed||!config?.ai_configured;for(const id of ["kgArchiveReport","kgRecheck"])$kg(id).disabled=busy||!detail||!Object.keys(detail.tax_checks||{}).length;$kg("kgBackToInputs").hidden=!task;$kg("kgBackToInputs").disabled=busy;}
  function reportList(){const root=$kg("kgReportVersions");root.replaceChildren();for(const row of reports){const line=el("p","kv",`报告 V${row.version} · 输入任务版本 ${row.input_revision} · ${row.created_at} `);for(const [format,label]of [["html","查看报告"],["pdf","下载 PDF"],["json","下载报告数据"]]){const a=el("a","kv",label);a.href=`/api/enterprise/input-tasks/${encodeURIComponent(detail.id)}/reports/${row.version}?format=${format}`;a.target="_blank";a.rel="noopener";line.append(a,document.createTextNode("　"));}root.append(line);}}
  function savedChecks(){const root=$kg("kgTaskChecks");root.replaceChildren();for(const [rid,slot]of Object.entries(detail?.tax_checks||{})){const b=el("button",null,slot.bundle.check.topic+(slot.stale?" · 资料更新待重检":" · 已保存"));b.type="button";b.onclick=()=>{if(busy)return;$kg("kgRule").value=rid;restore();};root.append(b);}buttons();}
  function restore(){last=null;$kg("kgEvidenceResult").replaceChildren();const slot=detail?.tax_checks?.[$kg("kgRule").value];if(slot){$kg("kgRegion").value=slot.bundle.task.region_code||"";$kg("kgTopic").value=slot.bundle.check.topic;show({...slot,revision:detail.revision,saved:true,cached:true});if(slot.stale)message("资料已更新：以下为历史核查结果，请重新核查后生成新报告。");}else{$kg("kgTopic").value="";message(detail?"已带入企业任务，请选择项目与地区后核查。":"请选择政策检查项目。");}}
  async function selectTask(ticket=++generation,identity=currentUser?.id){
    const id=$kg("kgTask").value;detail=null;reports=[];last=null;$kg("kgEvidenceResult").replaceChildren();$kg("kgTaskChecks").replaceChildren();$kg("kgReportVersions").replaceChildren();$kg("kgReportStatus").textContent="";dates();buttons();if(!id)return;
    try{const [task,versions]=await Promise.all([api("/api/enterprise/input-tasks/"+encodeURIComponent(id)),api("/api/enterprise/input-tasks/"+encodeURIComponent(id)+"/reports")]);if(!current(ticket,identity)||$kg("kgTask").value!==id)return;detail=task;reports=versions;const listed=tasks.find(t=>t.id===id);if(listed)listed.revision=task.revision;savedChecks();reportList();if(!task.tax_checks?.[$kg("kgRule").value]){const first=Object.keys(task.tax_checks||{})[0];if(first)$kg("kgRule").value=first;}restore();}catch(error){if(current(ticket,identity))message(error.message);}finally{if(current(ticket,identity))buttons();}
  }
  function openTask(id){requested={id,owner:currentUser?.id};$kg("kgEvidencePanel").open=true;if($kg("knowledgePanel").classList.contains("active"))activate();else switchPanel("knowledgePanel");}
  async function activate(){
    const identity=currentUser?.id;if(!identity)return;
    const ticket=++generation;owner=identity;message("正在载入本地检索状态…");
    const selected=$kg("kgRule").value,taskId=requested?.owner===identity?requested.id:$kg("kgTask").value;requested=null;
    try{
      const values=await Promise.all([api("/api/knowledge/checks"),api("/api/knowledge/retrieval/status"),["org_admin","accountant"].includes(currentUser.role)?api("/api/enterprise/input-tasks"):Promise.resolve([])]);
      if(!current(ticket,identity))return;
      const [rules,status,list]=values;tasks=list;config=status;
      $kg("kgRule").replaceChildren();for(const rule of rules)option($kg("kgRule"),rule.rule_id,rule.name+(rule.review_status==="approved"?"":" · 待审核"));
      if(rules.some(r=>r.rule_id===selected))$kg("kgRule").value=selected;
      else if(rules.some(r=>r.review_status==="approved"))$kg("kgRule").value=rules.find(r=>r.review_status==="approved").rule_id;
      $kg("kgTask").replaceChildren();option($kg("kgTask"),"","仅检索政策，不读取企业资料");for(const item of tasks)option($kg("kgTask"),item.id,item.task.company_name+" · "+item.task.start_date+" 至 "+item.task.end_date);
      if(tasks.some(t=>t.id===taskId))$kg("kgTask").value=taskId;
      message((status.semantic_status==="ready"?"本地语义检索已就绪":"本地语义检索未就绪，结果会明确标记")+"；"+(status.acceptance.passed?"离线检索验收已通过":"离线验收尚未通过，暂不开放 AI 联调"));
      dates();buttons();await selectTask(ticket,identity);
      if(taskId&&!tasks.some(t=>t.id===taskId))message("该任务不存在或当前账号已无权访问，请重新选择。");
    }catch(error){if(current(ticket,identity))message(error.message);}
  }
  function dates(){const task=tasks.find(t=>t.id===$kg("kgTask").value);for(const [id,key]of [["kgStart","start_date"],["kgEnd","end_date"]]){$kg(id).readOnly=!!task;if(task)$kg(id).value=task.task[key];}buttons();}
  function section(title){const box=el("details","kg-evidence-section");box.append(el("summary",null,title));$kg("kgEvidenceResult").append(box);return box;}
  function show(result){
    last=result;const data=result.bundle,root=$kg("kgEvidenceResult");root.replaceChildren();
    const states={ready:"材料完整，可辅助核查",needs_review:"存在待复核事项",insufficient:"证据不足"};
    root.append(el("h3",null,data.check.topic),el("p","material-note",result.stale?"资料已更新，以下为历史结果，须重新核查":states[data.bundle_status]||data.bundle_status),el("p","muted",data.task.data_scope==="synthetic"?"纯合成工程样本，不是真实政策或企业结论。":"结果保留数据和政策缺口，AI 分析作为待复核线索。"));
    const route=data.retrieval_trace;root.append(el("p","kv",`关系命中 ${route.relation_hits.length} 条 · 关键词命中 ${route.keyword_hits.length} 条 · 语义命中 ${route.semantic_hits.length} 条（${route.semantic_status==="ready"?"本地就绪":"未就绪"}）`));
    const evaluation={TRUE:"满足本规则条件",FALSE:"不满足本规则条件",UNKNOWN:"条件尚不能确定"};root.append(el("p","kv","程序核对："+(evaluation[data.check.evaluation]||"待核对")+(data.check.disposition==="excluded"?"；触发排除例外":"")));
    const facts=section("企业事实与取数来源（"+data.facts.length+"）");for(const fact of data.facts){facts.append(el("p","kv",fact.name+"："+(fact.state==="known"?fact.value+" "+(fact.unit||""):"未取得或待核实")),el("p","muted",fact.period_start+" 至 "+fact.period_end+" · 来源 "+(fact.source_ids.join("、")||"缺失")));}
    const policies=section("政策条款与检索原因（"+data.policy_evidence.length+"）");for(const item of data.policy_evidence){const article=el("article","kg-clause");article.append(el("h4",null,item.title+" · "+item.clause_id),el("p","muted",item.applicability==="context_only"?"历史原文，仅供理解修改关系":"已核对本次时间和地区范围"),el("p","kg-original",item.text),el("p","muted",item.source_location+" · "+(item.effective_from||"日期待查")+" 至 "+(item.effective_to||"终止日期待查")));if(/^https?:\/\//i.test(item.source_url)){const a=el("a","kv","查看原文来源 ↗");a.href=item.source_url;a.target="_blank";a.rel="noopener noreferrer";article.append(a);}for(const match of item.retrieval_matches||[])article.append(el("p","muted",({relation:"图关系",keyword:"关键词",semantic:"本地语义"})[match.route]+"命中"));policies.append(article);}
    const exceptions=section("必要例外（"+data.exceptions.length+"）");for(const item of data.exceptions)exceptions.append(el("p","kv",item.name+" · "+({TRUE:"已触发",FALSE:"未触发",UNKNOWN:"是否触发待核实"})[item.trigger_state]),el("p","kg-original",item.description||"详见引用原文："+item.source_clause_ids.join("、")));
    const gaps=section("待补充与复核（"+data.gaps.length+"）");for(const gap of data.gaps)gaps.append(el("p","material-note",gap.message||gap.code));if(!data.gaps.length)gaps.append(el("p","muted","本次材料包没有已识别的缺口；不代表已经作出税务法律结论。"));
    const filtered=section("过滤与待核实线索");for(const item of route.excluded)filtered.append(el("p","muted",item.clause_id+"："+item.reasons.join("；")));for(const item of data.policy_leads)filtered.append(el("p","material-note",item.clause_id+"："+item.reasons.join("；")),el("p","kg-original",item.text));
    if(data.batching.requires_joint_review||data.batching.unsplittable)root.append(el("p","material-note",`完整原文保留为 ${data.batching.part_count} 份材料，需要联合复核。`));
    if(result.ai){const ai=section("AI 辅助分析 · "+(result.ai.validated?"引用已回查":"仍需复核"));ai.open=true;if(result.ai.reason)ai.append(el("p","material-note",result.ai.reason));for(const claim of result.ai.assessments||[])ai.append(el("p","kv",claim.text),el("p","muted","条款："+claim.clause_ids.join("、")+"；企业来源："+claim.source_ids.join("、")),el("p","muted","需补充："+(claim.required_materials.join("、")||"请复核原始材料")));}
    const raw=section("完整材料包 JSON");raw.append(el("pre","kg-json",JSON.stringify(data,null,2)));
    const download=el("button",null,"导出本次材料包");download.type="button";download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json;charset=utf-8"})),a=el("a");a.href=url;a.download="evidence-"+data.bundle_id.slice(0,12)+".json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};root.append(download);
    message(result.saved?"已保存检查版本 "+result.revision+(result.cached?"，复用未变化的检查结果":""):"仅查看本地政策检索，没有读取企业材料");
  }
  async function run(useAI){
    if(busy||!currentUser?.id)return;if(!$kg("kgEvidenceForm").reportValidity())return;
    const ticket=++generation,identity=currentUser.id,task=tasks.find(t=>t.id===$kg("kgTask").value);
    busy=true;$kg("kgEvidenceFields").disabled=true;message(useAI?"正在基于本次材料包辅助分析…":"正在本地检索并核对材料…");
    try{const result=await api("/api/knowledge/retrieve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rule_id:$kg("kgRule").value,topic:$kg("kgTopic").value.trim(),task_id:task?.id||null,expected_revision:task?.revision??null,region_code:$kg("kgRegion").value,period_start:$kg("kgStart").value||null,period_end:$kg("kgEnd").value||null,use_ai:useAI})});if(!current(ticket,identity))return;if(task){task.revision=result.revision;if(detail){detail.revision=result.revision;(detail.tax_checks??={})[result.bundle.check.rule_id]=result;savedChecks();}}show(result);if(task)await archiveReport(ticket,identity);}catch(error){if(current(ticket,identity))message(error.message);}finally{if(current(ticket,identity)){busy=false;$kg("kgEvidenceFields").disabled=false;buttons();}}
  }
  async function archiveReport(ticket=++generation,identity=currentUser?.id){
    if(!detail)return;const id=detail.id,manual=!busy;busy=true;buttons();$kg("kgReportStatus").textContent="正在冻结本次检查报告…";
    try{const row=await api(`/api/enterprise/input-tasks/${encodeURIComponent(id)}/reports`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_revision:detail.revision})});if(!current(ticket,identity))return;reports=[row,...reports.filter(r=>r.version!==row.version)];reportList();$kg("kgReportStatus").textContent=`已${row.cached?"复用":"生成"}报告 V${row.version}；包含本任务已保存的核查项目，未运行项目不作结论。`;}catch(error){if(current(ticket,identity))$kg("kgReportStatus").textContent="核查结果已保留，报告尚未更新："+error.message;}finally{if(manual&&current(ticket,identity)){busy=false;buttons();}}
  }
  async function recheck(){
    if(busy||!detail)return;const ticket=++generation,identity=currentUser.id,id=detail.id;busy=true;$kg("kgEvidenceFields").disabled=true;buttons();message("正在核对变化，只更新受影响项目，不调用 AI…");
    try{const result=await api(`/api/enterprise/input-tasks/${encodeURIComponent(id)}/recheck`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_revision:detail.revision})});if(!current(ticket,identity))return;detail.revision=result.revision;detail.tax_checks=result.tax_checks;const task=tasks.find(t=>t.id===id);if(task)task.revision=result.revision;savedChecks();restore();message(`已更新 ${result.changed_rule_ids.length} 项，复用 ${result.reused_rule_ids.length} 项；本次未调用 AI。`);await archiveReport(ticket,identity);}catch(error){if(current(ticket,identity))message(error.message);}finally{if(current(ticket,identity)){busy=false;$kg("kgEvidenceFields").disabled=false;buttons();}}
  }
  $kg("kgTask").addEventListener("change",()=>{if(busy)return;$kg("kgRegion").value="";selectTask();});$kg("kgRule").addEventListener("change",()=>{if(!busy)restore();});$kg("kgReload").addEventListener("click",()=>{if(!busy)activate();});$kg("kgAnalyze").addEventListener("click",()=>run(true));$kg("kgEvidenceForm").addEventListener("submit",event=>{event.preventDefault();run(false);});
  $kg("kgArchiveReport").addEventListener("click",()=>{if(!busy)archiveReport();});
  $kg("kgRecheck").addEventListener("click",recheck);
  $kg("kgBackToInputs").addEventListener("click",()=>{if(busy||!detail)return;const id=detail.id;switchPanel("uploadPanel");coreInputs.openTask(id).catch(error=>showError(error.message));});
  window.KnowledgeRetrieval={activate,reset,openTask};
})();
