"use strict";
(() => {
  const $kg=id=>document.getElementById(id);
  let generation=0,owner=null,tasks=[],config=null,busy=false,last=null;
  const regions={"11":"北京市","12":"天津市","13":"河北省","14":"山西省","15":"内蒙古自治区","21":"辽宁省","22":"吉林省","23":"黑龙江省","31":"上海市","32":"江苏省","33":"浙江省","34":"安徽省","35":"福建省","36":"江西省","37":"山东省","41":"河南省","42":"湖北省","43":"湖南省","44":"广东省","45":"广西壮族自治区","46":"海南省","50":"重庆市","51":"四川省","52":"贵州省","53":"云南省","54":"西藏自治区","61":"陕西省","62":"甘肃省","63":"青海省","64":"宁夏回族自治区","65":"新疆维吾尔自治区"};
  for(const [code,name]of Object.entries(regions))option($kg("kgRegion"),"CN-"+code,name);
  function current(ticket,identity){return generation===ticket&&currentUser?.id===identity;}
  function message(text){$kg("kgRetrievalStatus").textContent=text;}
  function reset(){generation++;owner=null;tasks=[];config=null;last=null;busy=false;$kg("kgEvidenceFields").disabled=false;$kg("kgEvidenceResult").replaceChildren();$kg("kgTask").replaceChildren();option($kg("kgTask"),"","仅检索政策，不读取企业资料");$kg("kgRule").replaceChildren();$kg("kgStart").value="";$kg("kgEnd").value="";$kg("kgTopic").value="";$kg("kgRegion").value="";message("");}
  function buttons(){const task=$kg("kgTask").value;$kg("kgAnalyze").disabled=busy||!task||!config?.acceptance?.passed||!config?.ai_configured;}
  async function activate(){
    const identity=currentUser?.id;if(!identity)return;
    const ticket=++generation;owner=identity;message("正在载入本地检索状态…");
    const selected=$kg("kgRule").value,taskId=$kg("kgTask").value;
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
      dates();buttons();
    }catch(error){if(current(ticket,identity))message(error.message);}
  }
  function dates(){const task=tasks.find(t=>t.id===$kg("kgTask").value);for(const [id,key]of [["kgStart","start_date"],["kgEnd","end_date"]]){$kg(id).readOnly=!!task;if(task)$kg(id).value=task.task[key];}buttons();}
  function section(title){const box=el("details","kg-evidence-section");box.append(el("summary",null,title));$kg("kgEvidenceResult").append(box);return box;}
  function show(result){
    last=result;const data=result.bundle,root=$kg("kgEvidenceResult");root.replaceChildren();
    const states={ready:"材料完整，可辅助核查",needs_review:"存在待复核事项",insufficient:"证据不足"};
    root.append(el("h3",null,data.check.topic),el("p","material-note",states[data.bundle_status]||data.bundle_status),el("p","muted",data.task.data_scope==="synthetic"?"纯合成工程样本，不是真实政策或企业结论。":"结果保留数据和政策缺口，AI 分析作为待复核线索。"));
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
    try{const result=await api("/api/knowledge/retrieve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rule_id:$kg("kgRule").value,topic:$kg("kgTopic").value.trim(),task_id:task?.id||null,expected_revision:task?.revision??null,region_code:$kg("kgRegion").value,period_start:$kg("kgStart").value||null,period_end:$kg("kgEnd").value||null,use_ai:useAI})});if(!current(ticket,identity))return;if(task)task.revision=result.revision;show(result);}catch(error){if(current(ticket,identity))message(error.message);}finally{if(current(ticket,identity)){busy=false;$kg("kgEvidenceFields").disabled=false;buttons();}}
  }
  $kg("kgTask").addEventListener("change",dates);$kg("kgReload").addEventListener("click",activate);$kg("kgAnalyze").addEventListener("click",()=>run(true));$kg("kgEvidenceForm").addEventListener("submit",event=>{event.preventDefault();run(false);});
  window.KnowledgeRetrieval={activate,reset};
})();
