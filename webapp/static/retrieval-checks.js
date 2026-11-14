"use strict";
function createRetrievalChecks({api,el,$,user,active}) {
  const base="/api/platform/retrieval-checks",root=$("retrievalChecksAdmin");
  let epoch=0,timer=null,snapshot=null,busy=false,dirty=false,scheduleRevision=null;
  const labels={queued:"排队中",running:"检测中",passed:"通过",failed:"未通过",error:"执行失败",interrupted:"已中断"};
  const names={K01:"按规则关系找条款",K02:"关键词检索",K03:"换一种说法检索",K04:"保留例外",K05:"政策局部修订",K06:"历史期间适用",K07:"排除已过期政策",K08:"地区适用范围",K09:"未核实来源",K10:"多路检索去重",K11:"缺数据不影响无关项目",K12:"长原文和例外完整性",K13:"跨材料统计口径"};
  const date=value=>value?new Date(value).toLocaleString("zh-CN",{hour12:false}):"—";
  const valid=(ticket,owner)=>ticket===epoch&&owner===user()?.id&&user()?.role==="platform_admin"&&active();
  function reset(){epoch++;clearTimeout(timer);timer=null;snapshot=null;busy=dirty=false;root.hidden=true;$("retrievalCheckHistory").replaceChildren();$("retrievalCheckResults").replaceChildren();}
  function button(text,action){const node=el("button","btn re",text);node.type="button";node.onclick=action;return node;}
  function renderReport(value){
    const target=$("retrievalCheckResults");target.replaceChildren();
    target.append(el("h3","",`检测详情 · ${labels[value.status]||value.status}`),el("p","muted",`完成时间：${date(value.finished_at)} · 检测版本：${value.source_version?.slice(0,12)||"—"}`));
    if(value.error)target.append(el("p","material-note",value.error));
    const report=value.report;if(!report)return;
    target.append(el("p","",`通过 ${report.passed_count}/${report.case_count} 项 · 使用独立合成案例，不代表真实企业或政策审核通过。`));
    const metrics=report.metrics||{};
    const percentages=[["应找条款召回率",metrics.recall],["相关条款准确率",metrics.precision],["例外覆盖率",metrics.exception_coverage],["来源可追溯率",metrics.source_traceability]];
    target.append(el("p","muted",percentages.map(([name,value])=>`${name}：${typeof value==="number"?(value*100).toFixed(1)+"%":"—"}`).join(" · ")));
    for(const item of report.cases||[]){
      const detail=el("details","retrieval-check-case"),summary=el("summary","",`${item.passed?"通过":"未通过"} · ${item.id} ${names[item.id]||"检索案例"}`);
      detail.append(summary,el("p","muted",`预期条款：${item.expected?.join("、")||"不应形成适用依据"}`),el("p","muted",`实际条款：${item.actual?.join("、")||"无"}`));
      for(const failure of item.failures||[])detail.append(el("p","material-note",failure));
      target.append(detail);
    }
  }
  async function details(id){
    const ticket=epoch,owner=user()?.id;
    try{const row=await api(base+"/"+encodeURIComponent(id));if(valid(ticket,owner))renderReport(row);}
    catch(error){if(valid(ticket,owner))$("retrievalCheckStatus").textContent="检测详情读取失败："+error.message;}
  }
  function render(data){
    snapshot=data;root.hidden=false;
    $("retrievalCheckStatus").textContent=data.active?(data.active.status==="queued"?"检索检测已排队，完成后自动显示结果。":"正在检测检索功能，完成后自动显示结果。"):data.gate.reason||"尚未检测。";
    $("retrievalCheckVersion").textContent=`当前代码版本：${data.current_version?.slice(0,12)||"文件不完整"} · 检索模型：${data.model||"未就绪"}`;
    $("retrievalCheckRun").disabled=busy||!!data.active;
    $("retrievalCheckSave").disabled=busy;
    if(!dirty){scheduleRevision=data.schedule.revision;$("retrievalCheckEnabled").checked=data.schedule.enabled;$("retrievalCheckHours").value=String(data.schedule.interval_hours);}
    $("retrievalCheckNext").textContent=data.schedule.enabled?`下次定时检测：${date(data.schedule.next_due)}`:"定时检测未开启。";
    const history=$("retrievalCheckHistory");history.replaceChildren();
    for(const row of data.history){
      const line=el("div","data-item"),text=el("div");text.append(el("p","",`${labels[row.status]||row.status} · ${row.trigger==="manual"?"手动":"定时"} · ${date(row.created_at)}`),el("p","muted",`检测版本 ${row.source_version?.slice(0,12)||"待运行"}${row.case_count?` · 通过 ${row.passed_count}/${row.case_count} 项`:""}`));
      if(row.error)text.append(el("p","material-note",row.error));
      line.append(text,button("查看结果",()=>details(row.id)));history.append(line);
    }
    if(!data.history.length)history.append(el("p","muted","还没有后台检测记录，点击「立即重新检测」生成当前版本的报告。"));
  }
  async function load(){
    if(busy)return;
    clearTimeout(timer);
    if(user()?.role!=="platform_admin"||!active()){reset();return;}
    const ticket=++epoch,owner=user().id;root.hidden=false;
    try{
      const data=await api(base);if(!valid(ticket,owner))return;
      const previous=snapshot?.history?.find(row=>!["queued","running"].includes(row.status))?.id;
      render(data);
      const latest=data.history.find(row=>!["queued","running"].includes(row.status));
      if(latest&&latest.id!==previous)await details(latest.id);
      if(valid(ticket,owner))timer=setTimeout(load,data.active?2000:30000);
    }catch(error){if(valid(ticket,owner))$("retrievalCheckStatus").textContent="检测状态读取失败："+error.message;}
  }
  async function mutate(path,options){
    if(busy||user()?.role!=="platform_admin"||!active())return;
    const ticket=epoch,owner=user().id;busy=true;clearTimeout(timer);
    $("retrievalCheckRun").disabled=$("retrievalCheckSave").disabled=true;
    try{await api(path,options);if(!valid(ticket,owner))return;dirty=false;busy=false;await load();}
    catch(error){if(valid(ticket,owner)){$("retrievalCheckStatus").textContent=error.message;busy=false;$("retrievalCheckRun").disabled=!!snapshot?.active;$("retrievalCheckSave").disabled=false;}}
  }
  $("retrievalCheckRun").onclick=()=>mutate(base,{method:"POST"});
  $("retrievalCheckRefresh").onclick=()=>{dirty=false;return load();};
  $("retrievalCheckEnabled").onchange=$("retrievalCheckHours").oninput=()=>{dirty=true;};
  $("retrievalCheckSave").onclick=()=>{
    if(!snapshot)return;
    const hours=Number($("retrievalCheckHours").value);
    if(!Number.isInteger(hours)||hours<1||hours>720){$("retrievalCheckStatus").textContent="检测间隔须为 1–720 小时的整数。";return;}
    return mutate(base+"/schedule",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:$("retrievalCheckEnabled").checked,interval_hours:hours,revision:scheduleRevision})});
  };
  return {load,reset};
}
