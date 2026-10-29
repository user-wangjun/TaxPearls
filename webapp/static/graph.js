"use strict";
const graphKinds={knowledge_point:["学习知识点","#41968b"],certificate:["教学证书","#5584b6"],company:["企业","#d49b38"],policy:["政策文件","#9970d2"],clause:["原文条款","#8562b1"],concept:["适用概念","#a16fbd"],condition:["判断条件","#d48453"],exception:["例外情形","#cf6a91"],audit:["审查记录","#5f87c4"],entity:["关联主体","#a16fbd"],relation:["股权/控制关系","#5f87c4"],trade:["关联交易","#d48453"],rule:["核对规则","#4278e8"],metric:["业务指标","#2da99a"],risk:["核对结果","#e77277"],law:["法条依据","#9970d2"],source:["材料来源","#7090ad"]};
let graphData={nodes:[],edges:[]},graphSelected=null,graphVisible=new Set(),graphPositions=new Map(),graphTransform={x:0,y:0,k:1},graphFocused=false,graphRequest=0,graphBusy=false;
const graphCanvas=$("graphCanvas");
const graphRuleLevels={national:"国家级规则",provincial:"省级规则",municipal:"市级规则",unspecified:"未标注级别"};
let graphAllowed=new Set();
let graphMatchedCount=0;
let graphScopes=new Map();
let graphFullLoad=null,graphFilterRequest=0;
for(const [kind,[label,color]]of Object.entries(graphKinds)){const item=el("span",null,label);item.style.setProperty("--node-color",color);$("graphLegend").append(item);}
function graphCurrent(ticket,owner){return ticket===graphRequest&&currentUser?.id===owner&&$("knowledgePanel").classList.contains("active");}
function updateGraphLegend(){
  $("graphLegend").replaceChildren();for(const [kind,[label,color]]of Object.entries(graphKinds)){if(!graphData.nodes.some(n=>n.kind===kind))continue;const item=el("span",null,label);item.style.setProperty("--node-color",color);$("graphLegend").append(item);}
}
function updateGraphStats(){
  $("graphSummary").replaceChildren();for(const [num,label]of [[graphData.nodes.length,graphData.partial?"已载入节点":"知识节点"],[graphData.edges.length,graphData.partial?"已载入关联":"证据关联"]]){const item=el("div");item.append(el("strong",null,num),el("span",null,label));$("graphSummary").append(item);}
  $("graphCount").textContent=graphVisible.size?graphVisible.size+" 个节点 · "+graphData.edges.filter(e=>graphVisible.has(e.source)&&graphVisible.has(e.target)).length+" 条关系"+(graphMatchedCount>graphVisible.size?`；匹配 ${graphMatchedCount} 个节点，当前展示前 ${graphVisible.size} 个，可搜索全部知识或按地区缩小范围。`:""):graphData.partial?"正在载入政策关联…":"没有匹配的节点，请调整筛选";
}
function clearGraph(message=""){
  graphFullLoad=null;graphFilterRequest++;
  stopNodeQuestion();nodeQuestionHistory=[];nodeQuestionKey=null;
  graphSelected=null;graphData={nodes:[],edges:[]};graphVisible=new Set();graphAllowed=new Set();graphPositions=new Map();graphFocused=false;graphTransform={x:0,y:0,k:1};graphDrag=null;updateGraphRegions();
  for(const id of ["knowledgeDetail","graphMessages","graphSummary"])$(id).replaceChildren();
  graphCanvas.replaceChildren();graphCanvas.style.height="";graphCanvas.setAttribute("viewBox","0 0 960 680");
  $("graphCount").textContent=message;$("graphCoverage").textContent="";$("graphCoverage").hidden=true;
  $("graphAIStatus").textContent="尚未载入证据";$("graphSend").disabled=true;$("graphQuestion").value="";updateAIContext();
}
function invalidateGraph(){graphRequest++;clearGraph();if(typeof window!=="undefined")window.KnowledgeRetrieval?.reset();}
async function loadKnowledge({throwOnError=false}={}){
  const ticket=++graphRequest,owner=currentUser?.id,select=$("graphScope"),previous=select.value,previousAudit=$("graphAudit").value;
  clearGraph("正在载入可访问的企业…");select.disabled=true;
  graphScopes=new Map();select.replaceChildren();option(select,"","公共");updateGraphAudits();
  try{
    if(currentUser?.org_type!=="university"&&["org_admin","accountant","teacher"].includes(currentUser?.role)){
      const companies=await api("/api/knowledge/enterprises");if(!graphCurrent(ticket,owner))return;
      for(const company of companies)graphScopes.set(company.taxpayer_id,company);
      const names=new Map();for(const scope of graphScopes.values())names.set(scope.name,(names.get(scope.name)||0)+1);
      for(const [key,scope]of [...graphScopes].sort((a,b)=>a[1].name.localeCompare(b[1].name,"zh-CN")||a[0].localeCompare(b[0])))option(select,key,scope.name+(names.get(scope.name)>1?" · "+scope.taxpayer_id:""));
      if(graphScopes.has(previous))select.value=previous;
      updateGraphAudits(previousAudit);
    }
  }catch(e){if(graphCurrent(ticket,owner)){$("graphCount").textContent=e.message;if(throwOnError)throw e;}return;}
  finally{if(graphCurrent(ticket,owner))select.disabled=false;}
  if(graphCurrent(ticket,owner)){await loadGraph({throwOnError});if(typeof window!=="undefined"&&window.KnowledgeRetrieval&&currentUser?.org_type!=="university"&&currentUser?.id===owner&&$("knowledgePanel").classList.contains("active"))window.KnowledgeRetrieval.activate();}
}
function updateGraphAudits(previous=""){
  const scope=graphScopes.get($("graphScope").value),select=$("graphAudit");
  $("graphIdentity").hidden=!scope;$("graphIdentity").textContent=scope?"纳税人识别号："+scope.taxpayer_id:"";
  $("graphAuditField").hidden=!scope;select.replaceChildren();select.disabled=!scope?.audits.length;
  if(!scope)return;
  if(!scope.audits.length){option(select,"","暂无审查记录");return;}
  option(select,"","全部历史 · "+scope.audits.length+" 次审查");
  scope.audits.forEach((row,i)=>{
    const date=new Date(row.audited_at),reviewedAt=Number.isNaN(date.getTime())?row.audited_at:date.toLocaleString("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
    option(select,row.id,row.period+" · "+reviewedAt+(i===0?" · 最近一次":""));
  });
  if(scope.audits.some(row=>row.id===previous))select.value=previous;
}
async function loadGraph({throwOnError=false}={}){
  const ticket=++graphRequest,owner=currentUser?.id;clearGraph("正在载入关系网络…");
  try{
    const params=new URLSearchParams();if($("graphScope").value)params.set("taxpayer_id",$("graphScope").value);if($("graphAudit").value)params.set("audit_id",$("graphAudit").value);
    const initial=!params.size&&["knowledgeSearch","knowledgeCategory","graphStatus","graphRuleLevel","graphRegion"].every(id=>!$(id).value);
    if(initial)params.set("initial","1");
    const data=await api(currentUser?.org_type==="university"?"/api/training/knowledge/graph"+(initial?"?initial=1":""):"/api/knowledge/graph"+(params.size?"?"+params:""));if(!graphCurrent(ticket,owner))return;
    graphData=data;graphPositions=new Map();graphFocused=false;graphTransform={x:0,y:0,k:1};
    updateGraphLegend();
    if(data.boundary){$("graphCoverage").hidden=false;$("graphCoverage").textContent=data.boundary;}
    if(data.related_paths?.truncated){$("graphCoverage").hidden=false;$("graphCoverage").textContent=`关联方网络仅展示前 ${data.related_paths.shown} 条已复核关系候选路径，并非全部交易；G-001 的冻结结论仍基于全部路径，命中证据卡最多展示前 20 条。请结合原始台账核对其余路径。`;}
    $("graphAIStatus").textContent=data.ai.configured?"":"AI 尚未连接，请先配置模型。";$("graphAIStatus").hidden=data.ai.configured;
    $("graphSend").disabled=!data.ai.configured||graphBusy;
    updateGraphRegions();filterGraph(true);if(data.empty)$("graphCount").textContent="该企业尚无审查记录，完成材料审查后可查看企业图谱。";
    if(data.partial)setTimeout(()=>{if(graphCurrent(ticket,owner)&&graphData===data)ensureGraphComplete();},0);
  }catch(e){if(graphCurrent(ticket,owner)){clearGraph(e.message);if(throwOnError)throw e;}}
}
async function ensureGraphComplete(){
  if(!graphData.partial)return true;
  if(graphFullLoad)return graphFullLoad;
  const ticket=graphRequest,owner=currentUser?.id,initial=graphData;
  const loading=(async()=>{
    try{
      const data=await api(currentUser?.org_type==="university"?"/api/training/knowledge/graph":"/api/knowledge/graph");
      if(!graphCurrent(ticket,owner)||graphData!==initial)return false;
      graphData=data;graphAllowed=new Set(data.nodes.map(n=>n.id));updateGraphLegend();updateGraphRegions();
      $("graphCoverage").hidden=!data.boundary;$("graphCoverage").textContent=data.boundary||"";
      if(!graphVisible.size||[...graphVisible].some(id=>!graphAllowed.has(id)))filterGraph(true);
      else{updateGraphStats();renderNode();}
      return true;
    }catch(e){
      if(graphCurrent(ticket,owner)){$("graphCoverage").hidden=false;$("graphCoverage").textContent=(initial.boundary||"")+" 完整政策关联暂未载入，可刷新或重试搜索："+e.message;}
      return false;
    }finally{if(graphFullLoad===loading)graphFullLoad=null;}
  })();
  graphFullLoad=loading;return loading;
}
$("graphScope").addEventListener("change",()=>{updateGraphAudits();loadGraph();});
$("graphAudit").addEventListener("change",loadGraph);
function updateGraphRegions(){
  const select=$("graphRegion"),previous=select.value;select.replaceChildren();option(select,"","全部地域");
  const regions=[...new Set(graphData.nodes.filter(n=>n.kind==="rule"&&["provincial","municipal"].includes(n.rule_level)&&n.region).map(n=>n.region))].sort((a,b)=>a.localeCompare(b,"zh-CN"));
  for(const region of regions)option(select,region,region);
  select.disabled=$("graphRuleLevel").value!=="local";
  if(!select.disabled&&regions.includes(previous))select.value=previous;
}
for(const id of ["knowledgeSearch","knowledgeCategory","graphStatus","graphRuleLevel","graphRegion"])$(id).addEventListener(id==="knowledgeSearch"?"input":"change",async()=>{const request=++graphFilterRequest;if(!await ensureGraphComplete()||request!==graphFilterRequest)return;if(id==="graphRuleLevel")updateGraphRegions();graphFocused=false;graphTransform={x:0,y:0,k:1};filterGraph();});
function neighbors(ids){const result=new Set(ids);for(const e of graphData.edges){if(ids.has(e.source))result.add(e.target);if(ids.has(e.target))result.add(e.source);}return new Set([...result].filter(id=>graphAllowed.has(id)));}
function limitGraphView(){
  graphMatchedCount=graphVisible.size;
  if(graphData.taxpayer_id||graphVisible.size<=240)return;
  const ordered=graphData.nodes.filter(n=>graphVisible.has(n.id));
  const importance=n=>({certificate:0,knowledge_point:1,rule:2,policy:3,metric:4}[n.kind]??5);
  ordered.sort((a,b)=>(a.id===graphSelected?-1:b.id===graphSelected?1:graphData.view==="public_teaching"?importance(a)-importance(b):0));
  graphVisible=new Set(ordered.slice(0,240).map(n=>n.id));
}
function filterGraph(initialFocus=false){
  const query=$("knowledgeSearch").value.trim().toLowerCase(),category=$("knowledgeCategory").value,status=$("graphStatus").value,level=$("graphRuleLevel").value,region=$("graphRegion").value;
  const matchesLevel=n=>!level||(level==="local"?["provincial","municipal"].includes(n.rule_level)&&(!region||n.region===region):(n.rule_level||"unspecified")===level);
  graphAllowed=new Set(graphData.nodes.filter(n=>!["rule","risk"].includes(n.kind)||matchesLevel(n)).map(n=>n.id));
  const candidates=graphData.nodes.filter(n=>graphAllowed.has(n.id)&&(!level||["rule","risk"].includes(n.kind)));
  let seeds=candidates.filter(n=>(!query||JSON.stringify(n).toLowerCase().includes(query))&&(!category||n.category===category)&&(!status||n.status===status));
  if(category&&status){const ruleIds=new Set(candidates.filter(n=>n.category===category).map(n=>n.id));seeds=candidates.filter(n=>n.status===status&&(n.category===category||ruleIds.has(n.rule_id))&&(!query||JSON.stringify(n).toLowerCase().includes(query)));}
  graphVisible=neighbors(new Set(seeds.map(n=>n.id)));
  if(!graphSelected||!graphVisible.has(graphSelected))graphSelected=(graphData.view==="public_teaching"?seeds.find(n=>n.kind==="knowledge_point"):graphData.taxpayer_id?seeds.find(n=>n.kind==="risk"&&n.status==="hit")||seeds.find(n=>n.kind==="risk"):seeds.find(n=>n.kind==="rule"||n.kind==="risk"))?.id||seeds[0]?.id||null;
  if(initialFocus&&graphSelected){graphFocused=true;graphVisible=neighbors(new Set([graphSelected]));}
  limitGraphView();
  layoutGraph();drawGraph();renderNode();
}
function layoutGraph(){
  const nodes=graphData.nodes.filter(n=>graphVisible.has(n.id));
  if(!nodes.length){graphCanvas.style.height="";graphCanvas.setAttribute("viewBox","0 0 960 680");return;}
  if(nodes.length>160){
    nodes.forEach((node,i)=>graphPositions.set(node.id,{x:80+i%6*160,y:65+Math.floor(i/6)*90}));
    const height=Math.ceil(nodes.length/6)*90+40;graphCanvas.setAttribute("viewBox",`0 0 960 ${height}`);graphCanvas.style.height=height+"px";return;
  }
  if(graphFocused&&graphSelected&&nodes.length<=9){
    graphPositions.set(graphSelected,{x:480,y:335});const others=nodes.filter(n=>n.id!==graphSelected);
    const left=others.filter(n=>n.kind==="metric"||n.kind==="company"||n.kind==="source"),right=others.filter(n=>!left.includes(n));
    for(const [list,x]of [[left,170],[right,790]])list.forEach((n,i)=>graphPositions.set(n.id,{x,y:335+(i-(list.length-1)/2)*138}));
  }else{
    const groups=Object.keys(graphKinds).filter(k=>nodes.some(n=>n.kind===k));
    groups.forEach((kind,group)=>{const members=nodes.filter(n=>n.kind===kind),angle=group*2*Math.PI/groups.length-Math.PI/2,cx=480+Math.cos(angle)*220,cy=335+Math.sin(angle)*160;members.forEach((n,i)=>{const a=i*2.39996,r=22*Math.sqrt(i);graphPositions.set(n.id,{x:cx+Math.cos(a)*r,y:cy+Math.sin(a)*r});});});
    const links=graphData.edges.filter(e=>graphVisible.has(e.source)&&graphVisible.has(e.target));
    for(let step=0;step<140;step++){
      const force=new Map(nodes.map(n=>[n.id,{x:0,y:0}]));
      for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=graphPositions.get(nodes[i].id),b=graphPositions.get(nodes[j].id),dx=a.x-b.x,dy=a.y-b.y,d=Math.max(1,Math.hypot(dx,dy)),f=2600/(d*d);const fa=force.get(nodes[i].id),fb=force.get(nodes[j].id);fa.x+=dx/d*f;fa.y+=dy/d*f;fb.x-=dx/d*f;fb.y-=dy/d*f;}
      for(const e of links){const a=graphPositions.get(e.source),b=graphPositions.get(e.target),d=Math.max(1,Math.hypot(a.x-b.x,a.y-b.y)),f=(d-100)*.012;const fa=force.get(e.source),fb=force.get(e.target);fa.x-=(a.x-b.x)/d*f;fa.y-=(a.y-b.y)/d*f;fb.x+=(a.x-b.x)/d*f;fb.y+=(a.y-b.y)/d*f;}
      for(const n of nodes){const p=graphPositions.get(n.id),f=force.get(n.id);p.x=Math.max(45,Math.min(915,p.x+Math.max(-6,Math.min(6,f.x))));p.y=Math.max(45,Math.min(620,p.y+Math.max(-6,Math.min(6,f.y))));}
    }
  }
  if(innerWidth<=600&&graphFocused&&nodes.length<=9){
    const ordered=nodes.filter(n=>n.id!==graphSelected);ordered.splice(Math.floor(ordered.length/2),0,nodes.find(n=>n.id===graphSelected));
    ordered.forEach((n,i)=>graphPositions.set(n.id,{x:480,y:75+i*120}));
    graphCanvas.setAttribute("viewBox",`320 0 320 ${nodes.length*120+30}`);graphCanvas.style.height=(nodes.length*112+30)+"px";return;
  }
  graphCanvas.style.height="";
  if(graphFocused&&graphVisible.size<=9&&graphVisible.size){const points=[...graphVisible].map(id=>graphPositions.get(id));const minY=Math.min(...points.map(p=>p.y)),maxY=Math.max(...points.map(p=>p.y));graphCanvas.setAttribute("viewBox",`0 ${minY-110} 960 ${Math.max(350,maxY-minY+220)}`);}else graphCanvas.setAttribute("viewBox","0 0 960 680");
}
function drawGraph(){
  graphCanvas.replaceChildren();
  const defs=svgNode("defs",{}),shadow=svgNode("filter",{id:"nodeShadow",x:"-30%",y:"-40%",width:"160%",height:"190%"});shadow.append(svgNode("feDropShadow",{dx:0,dy:5,stdDeviation:7,"flood-color":"#233f68","flood-opacity":.09}));defs.append(shadow);graphCanvas.append(defs);
  const cards=graphFocused&&graphVisible.size<=9;

  const layer=svgNode("g",{transform:`translate(${graphTransform.x} ${graphTransform.y}) scale(${graphTransform.k})`});graphCanvas.append(layer);
  const near=graphSelected?neighbors(new Set([graphSelected])):new Set();
  for(const e of graphData.edges){if(!graphVisible.has(e.source)||!graphVisible.has(e.target))continue;const a=graphPositions.get(e.source),b=graphPositions.get(e.target),active=e.source===graphSelected||e.target===graphSelected;
    if(cards&&a.x===b.x){const down=b.y>a.y,sy=a.y+(down?44:-44),ty=b.y+(down?-44:44);layer.append(svgNode("path",{d:`M ${a.x+104} ${sy} C 630 ${sy}, 630 ${ty}, ${b.x+104} ${ty}`,fill:"none",stroke:"#9bb5da","stroke-width":1.5}));}else if(cards){const fromRight=b.x>a.x,sx=a.x+(fromRight?104:-104),tx=b.x+(fromRight?-104:104),mx=(sx+tx)/2;
      layer.append(svgNode("path",{d:`M ${sx} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${tx} ${b.y}`,fill:"none",stroke:active?"#8eadd9":"#d8e3f1","stroke-width":2}));
      const lx=mx,ly=(a.y+b.y)/2;layer.append(svgNode("rect",{x:lx-29,y:ly-10,width:58,height:20,rx:10,fill:"#f2f6fc"}),svgNode("text",{x:lx,y:ly+4,"text-anchor":"middle",class:"edge-label"},e.label));
    }else layer.append(svgNode("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:active?"#779bcf":"#d7e2ef","stroke-width":active?2:1,opacity:active?1:.55}));
  }

  for(const n of graphData.nodes){if(!graphVisible.has(n.id))continue;const p=graphPositions.get(n.id),selected=n.id===graphSelected,muted=graphSelected&&!near.has(n.id),color=graphKinds[n.kind][1],radius=n.kind==="rule"||n.kind==="company"?20:12;
    const g=svgNode("g",{transform:`translate(${p.x} ${p.y})`,class:"network-node",tabindex:0,role:"button","aria-label":graphKinds[n.kind][0]+"："+n.label,"data-node":n.id,opacity:muted?.45:1});
    if(cards){
      if(selected)g.append(svgNode("rect",{x:-111,y:-51,width:222,height:102,rx:21,fill:color,opacity:.09}));
      g.append(svgNode("rect",{x:-104,y:-44,width:208,height:88,rx:16,fill:selected?"#f4f8ff":"#fff",stroke:selected?color:"#dee7f2","stroke-width":selected?1.6:1,filter:"url(#nodeShadow)"}));
      g.append(svgNode("rect",{x:-88,y:-27,width:25,height:25,rx:8,fill:color,opacity:.12}),svgNode("text",{x:-75.5,y:-10,"text-anchor":"middle",fill:color,"font-size":13}, {policy:"§",clause:"¶",concept:"◉",condition:"?",exception:"!",audit:"▤",rule:"◇",metric:"≋",law:"§",risk:"!",source:"▤",company:"▥",entity:"◉",relation:"↔",trade:"¥"}[n.kind]));
      g.append(svgNode("text",{x:-54,y:-11,fill:color,"font-size":11,"font-weight":600},graphKinds[n.kind][0]+(n.kind==="rule"?" · "+(n.rule_code||n.id):"")));
      const label=n.label.replace(/[《》]/g,"");
      g.append(svgNode("text",{x:-88,y:13,fill:"#263c58","font-size":13,"font-weight":600},label.length>13?label.slice(0,13)+"…":label));
      const sub=n.kind==="rule"?graphRuleLevels[n.rule_level||"unspecified"]+(n.region?" · "+n.region:""):n.value!=null?Number(n.value).toLocaleString("zh-CN"):n.kind==="metric"?"查看取数要求":n.kind==="law"?"查看条文与官方来源":n.audit_period||n.period||n.category||"查看关联证据";
      g.append(svgNode("text",{x:-88,y:31,fill:"#8a9ab0","font-size":10},sub.length>21?sub.slice(0,21)+"…":sub));
    }else{
      if(selected)g.append(svgNode("circle",{r:radius+9,fill:color,opacity:.14}));g.append(svgNode("circle",{r:radius,fill:selected?color:"white",stroke:color,"stroke-width":selected?3:1.5}));
      g.append(svgNode("text",{"text-anchor":"middle",y:4,fill:selected?"white":color,"font-size":12,"font-weight":600},{policy:"§",clause:"¶",concept:"◉",condition:"?",exception:"!",audit:"▤",company:"▥",entity:"◉",relation:"↔",trade:"¥",rule:"◇",metric:"≋",risk:n.status==="hit"?"!":n.status==="pass"?"✓":"?",law:"§",source:"▤"}[n.kind]));
      if(selected||graphFocused||n.kind==="rule"||n.kind==="company")g.append(svgNode("text",{"text-anchor":"middle",y:radius+18,class:"node-label"},!selected&&!graphFocused&&n.kind==="rule"?(n.rule_code||n.id):n.label.length>13?n.label.slice(0,13)+"…":n.label));
    }
    if(!cards&&n.kind==="rule")g.append(svgNode("text",{"text-anchor":"middle",y:radius+31,class:"node-label"},graphRuleLevels[n.rule_level||"unspecified"]));
    g.append(svgNode("title",{},graphKinds[n.kind][0]+"："+n.label));g.addEventListener("click",()=>{if(!graphDragged)selectGraphNode(n.id);});g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectGraphNode(n.id);}});layer.append(g);
  }
  $("graphMode").textContent=graphFocused?"关联视图":"全景视图";
  $("graphOverview").classList.toggle("selected",!graphFocused);$("graphFocus").classList.toggle("selected",graphFocused);
  updateGraphStats();
}
function selectGraphNode(id){graphSelected=id;drawGraph();renderNode();}
function renderNode(){
  const box=$("knowledgeDetail"),n=graphData.nodes.find(n=>n.id===graphSelected);box.replaceChildren();updateAIContext();if(!n){box.append(el("p","empty","选择节点查看来源和关联。"));return;}
  const badge=el("span","node-kind",graphKinds[n.kind][0]);badge.style.color=graphKinds[n.kind][1];box.append(badge,el("h3",null,n.label));
  if(["rule","risk"].includes(n.kind)){box.append(el("p","ev-title","规则级别"),el("p","kv",graphRuleLevels[n.rule_level||"unspecified"]));if(n.region)box.append(el("p","ev-title","适用地域"),el("p","kv",n.region));}
  for(const [key,label]of [["taxpayer_id","纳税人识别号"],["company_names","企业名称记录"],["audit_count","累计审查次数"],["audit_period","审查期间"],["audited_at","审查时间"],["audit_id","审查编号"],["rule_code","规则编号"],["document_no","文号"],["issuer","发布机关"],["version","版本"],["unit","指标单位"],["extraction_status","原文复核"],["reviewed_by","审核人"],["reviewed_at","审核时间"],["review_status","复核状态"],["validity_status","政策效力"],["condition_source","条件来源"],["effective_from","生效日期"],["effective_to","终止日期"],["valid_from","规则起始日期"],["valid_to","规则终止日期"],["source_schema","指标来源表"],["source_field","指标来源字段"],["period_basis","期间口径"],["source_location","原文位置"],["notes","说明"],["value","指标值"],["period","所属期间"],["category","规则类型"],["tax_type","税种"],["status","核对结果"],["conclusion","结论"],["calculation","计算过程"],["reason","未执行原因"],["scope","适用范围"],["source","材料来源"],["requirement","取数要求"],["suggestion","排查建议"]]){
    if(n[key]===undefined||n[key]===""||(key==="version"&&currentUser?.org_type==="university"&&/[a-f0-9]{12,}/i.test(String(n[key]))))continue;let value=n[key];if(key==="source_location"||(key==="source"&&n.schema_type==="Clause"))value=formatPolicySourceLocation(value);if(key==="review_status")value=({teaching_reference:"教学参考，按来源核对",candidate:"候选，尚未审核",approved:"已审核",unverified:"待核实",source_recorded:"来源已记录",legacy_unverified:"历史执行版本，政策关联待核实",rejected:"已驳回",retired:"已退役"})[value]||value;if(key==="validity_status")value=({unverified:"效力待核实",in_force:"有效",partially_modified:"部分修订",repealed:"已废止",expired:"已失效"})[value]||value;if(key==="extraction_status")value=({pending:"待解析",parsed:"已解析，待复核",needs_review:"待人工核对",verified:"原文已复核"})[value]||value;if(key==="condition_source")value=({pending:"来源待核实",statutory:"法定条件",internal_screening:"内部筛查参数"})[value]||value;if(key==="status")value={hit:"风险命中",pass:"检查通过",skipped:"未执行"}[value];box.append(el("p","ev-title",label),el("p","kv",value===null?"未提供":value));
  }
  if(n.audit_id||graphData.audit_id)box.append(action("打开审计证据",()=>openAudit(n.audit_id||graphData.audit_id)));
  for(const url of n.references||[]){if(!/^https?:\/\//i.test(url))continue;const a=el("a","kv","官方来源 ↗");a.href=url;a.target="_blank";a.rel="noopener noreferrer";box.append(a,el("br"));}
  box.append(el("p","ev-title","相邻节点"));for(const e of graphData.edges.filter(e=>e.source===n.id||e.target===n.id)){const other=graphData.nodes.find(v=>v.id===(e.source===n.id?e.target:e.source));if(!graphAllowed.has(other.id))continue;box.append(action(e.label+" · "+other.label,()=>{graphVisible.add(other.id);if(!graphPositions.has(other.id))layoutGraph();selectGraphNode(other.id);}));}
}
function graphRagMode(){return graphQuestionMode==="rag"&&!graphData.audit_id&&!graphData.taxpayer_id;}
function updateAIContext(){
  const n=graphData.nodes.find(n=>n.id===graphSelected),isPublic=!graphData.audit_id&&!graphData.taxpayer_id,rag=graphRagMode();
  const key=[graphRequest,currentUser?.id,rag?"rag":graphSelected].join(":");
  if(key!==nodeQuestionKey){stopNodeQuestion();nodeQuestionKey=key;nodeQuestionHistory=[];$("graphMessages").replaceChildren();}
  $("graphQuestionModes").hidden=!isPublic;
  $("graphAskPublic").setAttribute("aria-pressed",String(rag));$("graphAskNode").setAttribute("aria-pressed",String(!rag));
  $("graphAIContext").textContent=rag?"公共税务知识 · 无需选择节点":n?"围绕："+n.label+(n.audit_period?" · "+n.audit_period:""):"请先在图谱中选择一个节点";
  $("graphQuestion").placeholder=rag?"直接输入税务问题，例如：进项税额抵扣有哪些规定？":"围绕当前节点提问…";
  $("graphSend").disabled=(!rag&&!n)||(!rag&&!graphData.ai?.configured)||graphBusy;
  const publicPrompts=["增值税进项税额抵扣有哪些规定？","小规模纳税人有哪些相关税务规定？","研发费用加计扣除有哪些规定？"];
  document.querySelectorAll(".ai-prompts [data-question]").forEach((button,i)=>{
    if(!button.dataset.nodeQuestion){button.dataset.nodeQuestion=button.dataset.question;button.dataset.nodeLabel=button.textContent;}
    button.dataset.question=rag?publicPrompts[i]:button.dataset.nodeQuestion;
    button.textContent=rag?["进项抵扣","小规模纳税人","研发费用"][i]:button.dataset.nodeLabel;
  });
}

function focusGraph(){if(!graphSelected)return;graphFocused=true;graphVisible=neighbors(new Set([graphSelected]));limitGraphView();layoutGraph();graphTransform={x:0,y:0,k:1};drawGraph();}
$("graphFocus").addEventListener("click",async()=>{if(await ensureGraphComplete())focusGraph();});
$("graphExpand").addEventListener("click",async()=>{if(!await ensureGraphComplete())return;graphVisible=neighbors(graphVisible);layoutGraph();drawGraph();});
function clearGraphFilters(){for(const id of ["knowledgeSearch","knowledgeCategory","graphStatus","graphRuleLevel","graphRegion"])$(id).value="";updateGraphRegions();}
$("graphOverview").addEventListener("click",async()=>{if(!await ensureGraphComplete())return;graphFocused=false;clearGraphFilters();graphTransform={x:0,y:0,k:1};filterGraph();});
$("graphCenter").addEventListener("click",()=>{graphTransform={x:0,y:0,k:1};layoutGraph();drawGraph();});
$("graphReset").addEventListener("click",()=>{clearGraphFilters();graphSelected=null;graphFocused=false;graphDrag=null;graphDragged=false;graphPositions=new Map();graphTransform={x:0,y:0,k:1};filterGraph(true);graphTab(false);if(graphData.empty)$("graphCount").textContent="该企业尚无审查记录，完成材料审查后可查看企业图谱。";});
function zoomGraph(factor,p={x:480,y:340}){const old=graphTransform.k,k=Math.max(.35,Math.min(3,old*factor));graphTransform.x=p.x-(p.x-graphTransform.x)*k/old;graphTransform.y=p.y-(p.y-graphTransform.y)*k/old;graphTransform.k=k;drawGraph();}
$("graphZoomIn").addEventListener("click",()=>zoomGraph(1.2));$("graphZoomOut").addEventListener("click",()=>zoomGraph(1/1.2));
function svgPoint(e){return new DOMPoint(e.clientX,e.clientY).matrixTransform(graphCanvas.getScreenCTM().inverse());}
graphCanvas.addEventListener("wheel",e=>{e.preventDefault();zoomGraph(e.deltaY<0?1.1:1/1.1,svgPoint(e));},{passive:false});
let graphDrag=null,graphDragged=false;
graphCanvas.addEventListener("pointerdown",e=>{if(e.button!==0)return;const p=svgPoint(e);graphDragged=false;graphDrag={id:e.target.closest("[data-node]")?.getAttribute("data-node"),p};});
window.addEventListener("pointermove",e=>{if(!graphDrag)return;const p=svgPoint(e),dx=p.x-graphDrag.p.x,dy=p.y-graphDrag.p.y;if(Math.abs(dx)+Math.abs(dy)>2)graphDragged=true;if(!graphDragged)return;if(graphDrag.id){const n=graphPositions.get(graphDrag.id);n.x+=dx/graphTransform.k;n.y+=dy/graphTransform.k;}else{graphTransform.x+=dx;graphTransform.y+=dy;}graphDrag.p=p;drawGraph();});
window.addEventListener("pointerup",()=>{graphDrag=null;});window.addEventListener("pointercancel",()=>{graphDrag=null;});
function graphTab(ai){$("graphAssistant").hidden=!ai;$("knowledgeDetail").hidden=ai;$("nodeTab").classList.toggle("active",!ai);$("aiTab").classList.toggle("active",ai);}
$("nodeTab").addEventListener("click",()=>graphTab(false));$("aiTab").addEventListener("click",()=>graphTab(true));
document.querySelectorAll("[data-question]").forEach(b=>b.addEventListener("click",()=>{$("graphQuestion").value=b.dataset.question;$("graphQuestion").focus();}));
let nodeQuestionController=null,nodeQuestionHistory=[],nodeQuestionKey=null,graphQuestionMode="rag";
$("graphAskPublic").addEventListener("click",()=>{graphQuestionMode="rag";updateAIContext();});
$("graphAskNode").addEventListener("click",()=>{graphQuestionMode="node";updateAIContext();});
function stopNodeQuestion(){
  if(nodeQuestionController){nodeQuestionController.abort();nodeQuestionController=null;graphBusy=false;}
  $("graphStop").hidden=true;
}
$("graphStop").addEventListener("click",()=>{if(nodeQuestionController)nodeQuestionController.abort();});
const nodeGateLabels={answerable:"正在生成 · 引用待核对",clarify:"需要澄清",out_of_scope:"超出当前节点范围",insufficient:"当前依据不足"};
async function readNodeStream(response,onEvent){
  if(!response.ok){let body;try{body=await response.json();}catch{}throw new Error(typeof body?.detail==="string"?body.detail:"提问失败，请检查输入后重试。");}
  if(!response.body||!response.headers.get("content-type")?.includes("text/event-stream"))throw new Error("服务未返回问答流。");
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="",done=false;
  try{
    while(true){
      const part=await reader.read();buffer+=decoder.decode(part.value||new Uint8Array(),{stream:!part.done});
      let lineEnd;while((lineEnd=buffer.indexOf("\n"))>=0){
        const line=buffer.slice(0,lineEnd).trimEnd();buffer=buffer.slice(lineEnd+1);
        if(!line.startsWith("data:"))continue;
        const event=JSON.parse(line.slice(5));onEvent(event);if(event.type==="done")done=true;
        if(event.type==="error")throw new Error(event.message);
      }
      if(buffer.length>1048576)throw new Error("回答过大，已停止。");
      if(part.done)break;
    }
    if(!done)throw new Error("连接中断，回答未完成，请重试。");
  }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
}
function ragSourceCard(source){
  const details=el("details","ai-rag-source"),heading=el("summary",null,source.id+" · "+source.label);
  const version=source.version&&!/[a-f0-9]{12,}/i.test(source.version)?"版本 "+source.version:"";
  details.append(heading,el("p","muted",[source.issuer,source.document_no,version].filter(Boolean).join(" · ")));
  details.append(el("p","muted","适用时间："+(source.effective_from||"起始日期待核实")+" 至 "+(source.effective_to||"终止日期待核实")));
  const validity={unverified:"政策效力待核实",in_force:"库内记载有效，仍须核对时点",partially_modified:"部分修改",repealed:"已废止",expired:"已失效"};
  details.append(el("p","muted",(validity[source.validity_status]||"政策效力待核实")+" · "+(source.extraction_status==="verified"?"原文已复核":"原文待复核")));
  details.append(el("p","ai-text",source.content),el("p","muted",formatPolicySourceLocation(source.source_location)));
  if(/^https?:\/\//i.test(source.source_url||"")){const link=el("a",null,"查看来源网页 ↗");link.href=source.source_url;link.target="_blank";link.rel="noopener noreferrer";details.append(link);}
  return details;
}
$("graphQuestionForm").addEventListener("submit",async e=>{
  e.preventDefault();if(graphBusy||(!graphRagMode()&&!graphSelected))return;
  const draft=$("graphQuestion").value,question=draft.trim();if(!question)return;
  const node=graphSelected,audit=graphData.audit_id,taxpayer=graphData.taxpayer_id,version=graphRequest,owner=currentUser?.id;
  const publicMode=!audit&&!taxpayer,rag=graphRagMode(),controller=new AbortController(),key=nodeQuestionKey;
  nodeQuestionController=controller;
  const box=$("graphMessages"),turn=el("div","ai-answer"),state=el("p","muted",rag?"正在检索公共原文…":"正在读取节点及关联依据…"),text=el("p","ai-text","");
  turn.append(el("p","ai-question",question),state,text);box.append(turn);box.scrollTop=box.scrollHeight;
  graphBusy=true;$("graphSend").disabled=true;$("graphStop").hidden=false;
  $("graphQuestion").value="";
  const current=()=>graphCurrent(version,owner)&&nodeQuestionKey===key&&!controller.signal.aborted;
  try{
    if(publicMode){
      const response=await fetch(rag?"/api/knowledge/rag-ask":"/api/knowledge/node-ask",{method:"POST",credentials:"same-origin",signal:controller.signal,
        headers:{"Content-Type":"application/json"},body:JSON.stringify(rag?{question,previous_questions:nodeQuestionHistory}:{node_id:node,question,previous_questions:nodeQuestionHistory})});
      await readNodeStream(response,event=>{
        if(!current())return;
        if(event.type==="context"){
          state.textContent=event.message;
          turn.querySelectorAll(".ai-reference-scope").forEach(item=>item.remove());
          const details=el("details","ai-reference-scope");details.append(el("summary",null,"本次参考范围"));
          for(const id of event.manifest.node_ids){const n=event.manifest.sources?.find(n=>n.id===id)||graphData.nodes.find(n=>n.id===id);details.append(el("p","muted",n?.label||id));}
          if(event.manifest.message)details.append(el("p","muted",event.manifest.message));
          if(rag&&event.manifest.reference_date)details.append(el("p","muted","检索参考日期："+event.manifest.reference_date));
          if(event.manifest.coverage?.omitted_candidates)details.append(el("p","muted","本次发送部分相关原文，尚未穷尽全部候选。"));
          if(event.manifest.capacity_source==="conservative_fallback")details.append(el("p","muted","模型未提供容量，使用保守容量限制。"));
          turn.insertBefore(details,text);
        }else if(event.type==="progress"&&event.message)state.textContent=event.message;
        else if(event.type==="gate")state.textContent=event.status==="out_of_scope"&&rag?"超出税务知识范围":nodeGateLabels[event.status]||"正在生成…";
        else if(event.type==="summary"&&event.text){const details=el("details","ai-reason-summary");details.append(el("summary",null,"回答依据说明"),el("p","ai-text",event.text));turn.insertBefore(details,text);}
        else if(event.type==="delta"){const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<80;text.textContent+=event.text;if(nearBottom)box.scrollTop=box.scrollHeight;}
        else if(event.type==="done"){
          text.textContent=event.answer;state.textContent=event.status==="answerable"?(rag?"已完成 · 请结合引用原文核实":"已完成 · 引用节点已核对，原文状态请见来源"):event.status==="out_of_scope"&&rag?"超出税务知识范围":nodeGateLabels[event.status];
          if(event.citations.length)turn.append(el("p","muted",rag?"原文依据 · 展开查看":"参考节点 · 点击查看"));
          for(const cited of event.citations){if(rag)turn.append(ragSourceCard(cited));else turn.append(action(cited.label,()=>{selectGraphNode(cited.id);focusGraph();renderNode();}));}
          if(event.status==="answerable"||(rag&&["clarify","insufficient"].includes(event.status)))nodeQuestionHistory=[...nodeQuestionHistory,question.slice(0,500)].slice(-3);
        }
      });
    }else{
      // Existing enterprise answers stay on their original, non-streaming endpoint.
      const result=await api("/api/knowledge/ask",{method:"POST",signal:controller.signal,headers:{"Content-Type":"application/json"},body:JSON.stringify({node_id:node,audit_id:audit,taxpayer_id:taxpayer,question})});
      if(!current())return;text.textContent=result.answer;state.textContent="已完成";
      for(const cited of result.citations)turn.append(action(cited.label,()=>{selectGraphNode(cited.id);focusGraph();renderNode();}));
    }
  }catch(err){
    if(graphCurrent(version,owner)&&nodeQuestionKey===key){
      state.textContent=controller.signal.aborted?"已停止 · 回答未完成":err.message;turn.dataset.incomplete="true";
      if($("graphQuestion").value==="")$("graphQuestion").value=draft;
    }
  }finally{
    if(nodeQuestionController===controller){nodeQuestionController=null;graphBusy=false;$("graphStop").hidden=true;$("graphSend").disabled=(!graphRagMode()&&!graphSelected)||(!graphRagMode()&&!graphData.ai?.configured);}
  }
});

let graphResizeTimer;window.addEventListener("resize",()=>{clearTimeout(graphResizeTimer);graphResizeTimer=setTimeout(()=>{if(graphData.nodes.length){layoutGraph();drawGraph();}},250);});
