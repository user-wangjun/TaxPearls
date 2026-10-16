"use strict";
const graphKinds={company:["企业","#d49b38"],audit:["审查记录","#5f87c4"],entity:["关联主体","#a16fbd"],relation:["股权/控制关系","#5f87c4"],trade:["关联交易","#d48453"],rule:["核对规则","#4278e8"],metric:["业务指标","#2da99a"],risk:["核对结果","#e77277"],law:["法条依据","#9970d2"],source:["材料来源","#7090ad"]};
let graphData={nodes:[],edges:[]},graphSelected=null,graphVisible=new Set(),graphPositions=new Map(),graphTransform={x:0,y:0,k:1},graphFocused=false,graphRequest=0,graphBusy=false;
const graphCanvas=$("graphCanvas");
const graphRuleLevels={national:"国家级规则",provincial:"省级规则",municipal:"市级规则",unspecified:"未标注级别"};
let graphAllowed=new Set();
let graphScopes=new Map();
for(const [kind,[label,color]]of Object.entries(graphKinds)){const item=el("span",null,label);item.style.setProperty("--node-color",color);$("graphLegend").append(item);}
function graphCurrent(ticket,owner){return ticket===graphRequest&&currentUser?.id===owner&&$("knowledgePanel").classList.contains("active");}
function clearGraph(message=""){
  graphSelected=null;graphData={nodes:[],edges:[]};graphVisible=new Set();graphAllowed=new Set();graphPositions=new Map();graphFocused=false;graphTransform={x:0,y:0,k:1};graphDrag=null;updateGraphRegions();
  for(const id of ["knowledgeDetail","graphMessages","graphSummary"])$(id).replaceChildren();
  graphCanvas.replaceChildren();graphCanvas.style.height="";graphCanvas.setAttribute("viewBox","0 0 960 680");
  $("graphCount").textContent=message;$("graphCoverage").textContent="";$("graphCoverage").hidden=true;
  $("graphAIStatus").textContent="尚未载入证据";$("graphSend").disabled=true;$("graphQuestion").value="";updateAIContext();
}
function invalidateGraph(){graphRequest++;clearGraph();}
async function loadKnowledge(){
  const ticket=++graphRequest,owner=currentUser?.id,select=$("graphScope"),previous=select.value,previousAudit=$("graphAudit").value;
  clearGraph("正在载入可访问的企业…");select.disabled=true;
  graphScopes=new Map();select.replaceChildren();option(select,"","公共");updateGraphAudits();
  try{
    if(["org_admin","accountant","teacher"].includes(currentUser?.role)){
      const companies=await api("/api/knowledge/enterprises");if(!graphCurrent(ticket,owner))return;
      for(const company of companies)graphScopes.set(company.taxpayer_id,company);
      const names=new Map();for(const scope of graphScopes.values())names.set(scope.name,(names.get(scope.name)||0)+1);
      for(const [key,scope]of [...graphScopes].sort((a,b)=>a[1].name.localeCompare(b[1].name,"zh-CN")||a[0].localeCompare(b[0])))option(select,key,scope.name+(names.get(scope.name)>1?" · "+scope.taxpayer_id:""));
      if(graphScopes.has(previous))select.value=previous;
      updateGraphAudits(previousAudit);
    }
  }catch(e){if(graphCurrent(ticket,owner))$("graphCount").textContent=e.message;return;}
  finally{if(graphCurrent(ticket,owner))select.disabled=false;}
  if(graphCurrent(ticket,owner))await loadGraph();
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
async function loadGraph(){
  const ticket=++graphRequest,owner=currentUser?.id;clearGraph("正在载入关系网络…");
  try{
    const params=new URLSearchParams();if($("graphScope").value)params.set("taxpayer_id",$("graphScope").value);if($("graphAudit").value)params.set("audit_id",$("graphAudit").value);
    const data=await api("/api/knowledge/graph"+(params.size?"?"+params:""));if(!graphCurrent(ticket,owner))return;
    graphData=data;graphPositions=new Map();graphFocused=false;graphTransform={x:0,y:0,k:1};
    if(data.related_paths?.truncated){$("graphCoverage").hidden=false;$("graphCoverage").textContent=`关联方网络仅展示前 ${data.related_paths.shown} 条已复核关系候选路径，并非全部交易；G-001 的冻结结论仍基于全部路径，命中证据卡最多展示前 20 条。请结合原始台账核对其余路径。`;}
    $("graphAIStatus").textContent=data.ai.configured?"已配置模型 · "+data.ai.model:"AI 尚未连接 · 填写项目模型配置后启用";
    $("graphSend").disabled=!data.ai.configured||graphBusy;
    updateGraphRegions();filterGraph(true);focusGraph();if(data.empty)$("graphCount").textContent="该企业尚无审查记录，完成材料审查后可查看企业图谱。";
  }catch(e){if(graphCurrent(ticket,owner)){clearGraph(e.message);}}
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
for(const id of ["knowledgeSearch","knowledgeCategory","graphStatus","graphRuleLevel","graphRegion"])$(id).addEventListener(id==="knowledgeSearch"?"input":"change",()=>{if(id==="graphRuleLevel")updateGraphRegions();graphFocused=false;graphTransform={x:0,y:0,k:1};filterGraph();});
function neighbors(ids){const result=new Set(ids);for(const e of graphData.edges){if(ids.has(e.source))result.add(e.target);if(ids.has(e.target))result.add(e.source);}return new Set([...result].filter(id=>graphAllowed.has(id)));}
function filterGraph(historyFocus=false){
  const query=$("knowledgeSearch").value.trim().toLowerCase(),category=$("knowledgeCategory").value,status=$("graphStatus").value,level=$("graphRuleLevel").value,region=$("graphRegion").value;
  const matchesLevel=n=>!level||(level==="local"?["provincial","municipal"].includes(n.rule_level)&&(!region||n.region===region):(n.rule_level||"unspecified")===level);
  graphAllowed=new Set(graphData.nodes.filter(n=>!["rule","risk"].includes(n.kind)||matchesLevel(n)).map(n=>n.id));
  const candidates=graphData.nodes.filter(n=>graphAllowed.has(n.id)&&(!level||["rule","risk"].includes(n.kind)));
  let seeds=candidates.filter(n=>(!query||JSON.stringify(n).toLowerCase().includes(query))&&(!category||n.category===category)&&(!status||n.status===status));
  if(category&&status){const ruleIds=new Set(candidates.filter(n=>n.category===category).map(n=>n.id));seeds=candidates.filter(n=>n.status===status&&(n.category===category||ruleIds.has(n.rule_id))&&(!query||JSON.stringify(n).toLowerCase().includes(query)));}
  graphVisible=neighbors(new Set(seeds.map(n=>n.id)));
  if(!graphSelected||!graphVisible.has(graphSelected))graphSelected=(graphData.taxpayer_id?seeds.find(n=>n.kind==="risk"&&n.status==="hit")||seeds.find(n=>n.kind==="risk"):seeds.find(n=>n.kind==="rule"||n.kind==="risk"))?.id||seeds[0]?.id||null;
  if(historyFocus&&graphData.taxpayer_id&&!query&&!category&&!status&&!level){graphFocused=true;graphVisible=neighbors(new Set(graphSelected?[graphSelected]:[]));}
  layoutGraph();drawGraph();renderNode();
}
function layoutGraph(){
  const nodes=graphData.nodes.filter(n=>graphVisible.has(n.id));
  if(!nodes.length){graphCanvas.style.height="";graphCanvas.setAttribute("viewBox","0 0 960 680");return;}
  if(nodes.length>160){
    nodes.forEach((node,i)=>graphPositions.set(node.id,{x:80+i%6*160,y:65+Math.floor(i/6)*90}));
    const height=Math.ceil(nodes.length/6)*90+40;graphCanvas.setAttribute("viewBox",`0 0 960 ${height}`);graphCanvas.style.height=height+"px";return;
  }
  const groups=Object.keys(graphKinds).filter(k=>nodes.some(n=>n.kind===k));
  groups.forEach((kind,group)=>{const members=nodes.filter(n=>n.kind===kind),angle=group*2*Math.PI/groups.length-Math.PI/2,cx=480+Math.cos(angle)*220,cy=335+Math.sin(angle)*160;members.forEach((n,i)=>{const a=i*2.39996,r=22*Math.sqrt(i);graphPositions.set(n.id,{x:cx+Math.cos(a)*r,y:cy+Math.sin(a)*r});});});
  const links=graphData.edges.filter(e=>graphVisible.has(e.source)&&graphVisible.has(e.target));
  for(let step=0;step<140;step++){
    const force=new Map(nodes.map(n=>[n.id,{x:0,y:0}]));
    for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=graphPositions.get(nodes[i].id),b=graphPositions.get(nodes[j].id),dx=a.x-b.x,dy=a.y-b.y,d=Math.max(1,Math.hypot(dx,dy)),f=2600/(d*d);const fa=force.get(nodes[i].id),fb=force.get(nodes[j].id);fa.x+=dx/d*f;fa.y+=dy/d*f;fb.x-=dx/d*f;fb.y-=dy/d*f;}
    for(const e of links){const a=graphPositions.get(e.source),b=graphPositions.get(e.target),d=Math.max(1,Math.hypot(a.x-b.x,a.y-b.y)),f=(d-100)*.012;const fa=force.get(e.source),fb=force.get(e.target);fa.x-=(a.x-b.x)/d*f;fa.y-=(a.y-b.y)/d*f;fb.x+=(a.x-b.x)/d*f;fb.y+=(a.y-b.y)/d*f;}
    for(const n of nodes){const p=graphPositions.get(n.id),f=force.get(n.id);p.x=Math.max(45,Math.min(915,p.x+Math.max(-6,Math.min(6,f.x))));p.y=Math.max(45,Math.min(620,p.y+Math.max(-6,Math.min(6,f.y))));}
  }
  if(graphFocused&&graphSelected&&nodes.length<=9){
    graphPositions.set(graphSelected,{x:480,y:335});const others=nodes.filter(n=>n.id!==graphSelected);
    const left=others.filter(n=>n.kind==="metric"||n.kind==="company"||n.kind==="source"),right=others.filter(n=>!left.includes(n));
    for(const [list,x]of [[left,170],[right,790]])list.forEach((n,i)=>graphPositions.set(n.id,{x,y:335+(i-(list.length-1)/2)*138}));
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
      g.append(svgNode("rect",{x:-88,y:-27,width:25,height:25,rx:8,fill:color,opacity:.12}),svgNode("text",{x:-75.5,y:-10,"text-anchor":"middle",fill:color,"font-size":13}, {audit:"▤",rule:"◇",metric:"≋",law:"§",risk:"!",source:"▤",company:"▥",entity:"◉",relation:"↔",trade:"¥"}[n.kind]));
      g.append(svgNode("text",{x:-54,y:-11,fill:color,"font-size":11,"font-weight":600},graphKinds[n.kind][0]+(n.kind==="rule"?" · "+(n.rule_code||n.id):"")));
      const label=n.label.replace(/[《》]/g,"");
      g.append(svgNode("text",{x:-88,y:13,fill:"#263c58","font-size":13,"font-weight":600},label.length>13?label.slice(0,13)+"…":label));
      const sub=n.kind==="rule"?graphRuleLevels[n.rule_level||"unspecified"]+(n.region?" · "+n.region:""):n.value!=null?Number(n.value).toLocaleString("zh-CN"):n.kind==="metric"?"查看取数要求":n.kind==="law"?"查看条文与官方来源":n.audit_period||n.period||n.category||"查看关联证据";
      g.append(svgNode("text",{x:-88,y:31,fill:"#8a9ab0","font-size":10},sub.length>21?sub.slice(0,21)+"…":sub));
    }else{
      if(selected)g.append(svgNode("circle",{r:radius+9,fill:color,opacity:.14}));g.append(svgNode("circle",{r:radius,fill:selected?color:"white",stroke:color,"stroke-width":selected?3:1.5}));
      g.append(svgNode("text",{"text-anchor":"middle",y:4,fill:selected?"white":color,"font-size":12,"font-weight":600},{audit:"▤",company:"▥",entity:"◉",relation:"↔",trade:"¥",rule:"◇",metric:"≋",risk:n.status==="hit"?"!":n.status==="pass"?"✓":"?",law:"§",source:"▤"}[n.kind]));
      if(selected||graphFocused||n.kind==="rule"||n.kind==="company")g.append(svgNode("text",{"text-anchor":"middle",y:radius+18,class:"node-label"},!selected&&!graphFocused&&n.kind==="rule"?(n.rule_code||n.id):n.label.length>13?n.label.slice(0,13)+"…":n.label));
    }
    if(!cards&&n.kind==="rule")g.append(svgNode("text",{"text-anchor":"middle",y:radius+31,class:"node-label"},graphRuleLevels[n.rule_level||"unspecified"]));
    g.append(svgNode("title",{},graphKinds[n.kind][0]+"："+n.label));g.addEventListener("click",()=>{if(!graphDragged)selectGraphNode(n.id);});g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectGraphNode(n.id);}});layer.append(g);
  }
  $("graphMode").textContent=graphFocused?"关联视图":"全景视图";
  $("graphOverview").classList.toggle("selected",!graphFocused);$("graphFocus").classList.toggle("selected",graphFocused);
  $("graphSummary").replaceChildren();for(const [num,label]of [[graphData.nodes.length,"知识节点"],[graphData.edges.length,"证据关联"]]){const item=el("div");item.append(el("strong",null,num),el("span",null,label));$("graphSummary").append(item);}
  $("graphCount").textContent=graphVisible.size?graphVisible.size+" 个节点 · "+graphData.edges.filter(e=>graphVisible.has(e.source)&&graphVisible.has(e.target)).length+" 条关系":"没有匹配的节点，请调整筛选";
}
function selectGraphNode(id){graphSelected=id;drawGraph();renderNode();}
function renderNode(){
  const box=$("knowledgeDetail"),n=graphData.nodes.find(n=>n.id===graphSelected);box.replaceChildren();updateAIContext();if(!n){box.append(el("p","empty","选择节点查看来源和关联。"));return;}
  const badge=el("span","node-kind",graphKinds[n.kind][0]);badge.style.color=graphKinds[n.kind][1];box.append(badge,el("h3",null,n.label));
  if(["rule","risk"].includes(n.kind)){box.append(el("p","ev-title","规则级别"),el("p","kv",graphRuleLevels[n.rule_level||"unspecified"]));if(n.region)box.append(el("p","ev-title","适用地域"),el("p","kv",n.region));}
  for(const [key,label]of [["taxpayer_id","纳税人识别号"],["company_names","企业名称记录"],["audit_count","累计审查次数"],["audit_period","审查期间"],["audited_at","审查时间"],["audit_id","审查编号"],["rule_code","规则编号"],["value","指标值"],["period","所属期间"],["category","规则类型"],["tax_type","税种"],["status","核对结果"],["conclusion","结论"],["calculation","计算过程"],["reason","未执行原因"],["scope","适用范围"],["source","材料来源"],["requirement","取数要求"],["suggestion","排查建议"]]){
    if(n[key]===undefined||n[key]==="")continue;let value=n[key];if(key==="status")value={hit:"风险命中",pass:"检查通过",skipped:"未执行"}[value];box.append(el("p","ev-title",label),el("p","kv",value===null?"未提供":value));
  }
  if(n.audit_id||graphData.audit_id)box.append(action("打开审计证据",()=>openAudit(n.audit_id||graphData.audit_id)));
  for(const url of n.references||[]){if(!/^https?:\/\//i.test(url))continue;const a=el("a","kv","官方来源 ↗");a.href=url;a.target="_blank";a.rel="noopener noreferrer";box.append(a,el("br"));}
  box.append(el("p","ev-title","相邻节点"));for(const e of graphData.edges.filter(e=>e.source===n.id||e.target===n.id)){const other=graphData.nodes.find(v=>v.id===(e.source===n.id?e.target:e.source));if(!graphAllowed.has(other.id))continue;box.append(action(e.label+" · "+other.label,()=>{graphVisible.add(other.id);if(!graphPositions.has(other.id))layoutGraph();selectGraphNode(other.id);}));}
}
function updateAIContext(){const n=graphData.nodes.find(n=>n.id===graphSelected);$("graphAIContext").textContent=n?"当前上下文："+n.label+(n.audit_period?" · "+n.audit_period:""):"请先在图谱中选择一个节点";}
function focusGraph(){if(!graphSelected)return;graphFocused=true;graphVisible=neighbors(new Set([graphSelected]));layoutGraph();graphTransform={x:0,y:0,k:1};drawGraph();}
$("graphFocus").addEventListener("click",focusGraph);
$("graphExpand").addEventListener("click",()=>{graphVisible=neighbors(graphVisible);layoutGraph();drawGraph();});
function clearGraphFilters(){for(const id of ["knowledgeSearch","knowledgeCategory","graphStatus","graphRuleLevel","graphRegion"])$(id).value="";updateGraphRegions();}
$("graphOverview").addEventListener("click",()=>{graphFocused=false;clearGraphFilters();graphTransform={x:0,y:0,k:1};filterGraph();});
$("graphCenter").addEventListener("click",()=>{graphTransform={x:0,y:0,k:1};layoutGraph();drawGraph();});
$("graphReset").addEventListener("click",()=>{clearGraphFilters();graphSelected=null;graphFocused=false;graphDrag=null;graphDragged=false;graphPositions=new Map();graphTransform={x:0,y:0,k:1};filterGraph(true);focusGraph();renderNode();graphTab(false);if(graphData.empty)$("graphCount").textContent="该企业尚无审查记录，完成材料审查后可查看企业图谱。";});
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
$("graphQuestionForm").addEventListener("submit",async e=>{
  e.preventDefault();if(graphBusy)return;if(!graphSelected)return;const question=$("graphQuestion").value.trim();if(!question)return;
  const node=graphSelected,audit=graphData.audit_id,taxpayer=graphData.taxpayer_id,version=graphRequest,owner=currentUser?.id,box=$("graphMessages"),turn=el("div","ai-answer");turn.append(el("p","ai-question",question),el("p","muted","正在检索关联证据并生成解释…"));box.append(turn);graphBusy=true;$("graphSend").disabled=true;
  try{const result=await api("/api/knowledge/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({node_id:node,audit_id:audit,taxpayer_id:taxpayer,question})});if(!graphCurrent(version,owner))return;turn.replaceChildren(el("p","ai-question",question),el("p","ai-text",result.answer),el("p","muted","证据引用 · 点击定位"));for(const cited of result.citations)turn.append(action(cited.label,()=>{graphSelected=cited.id;focusGraph();renderNode();}));if($("graphQuestion").value.trim()===question)$("graphQuestion").value="";
  }catch(err){if(graphCurrent(version,owner))turn.replaceChildren(el("p","ai-question",question),el("p","ai-text",err.message));}finally{graphBusy=false;$("graphSend").disabled=!graphData.ai?.configured;}
});

let graphResizeTimer;window.addEventListener("resize",()=>{clearTimeout(graphResizeTimer);graphResizeTimer=setTimeout(()=>{if(graphData.nodes.length){layoutGraph();drawGraph();}},250);});
