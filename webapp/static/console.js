"use strict";
/* 安全约定：所有来自上传数据的文本一律通过 textContent / DOM API 写入，
   绝不 innerHTML 拼接（杜绝原型期遗留的 XSS 路径）。 */

let auditId = null;
let lastResult = null;
let snapshotReturn = null;
let selectedClientId = "";
let openAssignmentId = null;
let exerciseOpenRequest = 0, sandboxFeedbackRequest = 0, sandboxRuleId = null;
let sandboxEvidence = new Map();
let trainingLoadRequest = 0, exerciseDeadlineTimer = null;
let latestFindings = [];
let graphRuleCatalog = [], ruleCatalogRequest = 0;
let editingRule = null;
let ruleDraftLogic = null;
let noticeRequest = 0, noticeBusy = false, noticeReady = false, noticeHasEmail = false;
let ruleEditorRequest = 0, ruleHistoryRequest = 0, ruleBusy = false, adminRequest = 0;

let aiKeyProbes = null;
let universityMembersUI=null,teachingViewOwner=null;
let teachingWorkspace = null;
function isCampusUser(user=currentUser){return user?.org_type==="university";}
function campusStudent(user=currentUser){return isCampusUser(user)&&(user.education_role==="student"||user.role==="student");}
function accountRoleName(user){return isCampusUser(user)&&["admin","school_admin"].includes(user.education_role)?"高校管理员 / 教师":({teacher:"教师",student:"学生",org_admin:isCampusUser(user)?"高校管理员":"机构管理员",accountant:"会计",platform_admin:"平台管理员"}[user.role]||user.role);}
async function teachingRequest(path,options={}){
  const owner=currentUser?.id,request={...options};if(request.body&&typeof request.body!=="string"){request.headers={"Content-Type":"application/json",...request.headers};request.body=JSON.stringify(request.body);}
  try{return await api(path,request);}catch(error){
    if(error.status!==401||currentUser?.id!==owner)throw error;
    await api('/api/training/membership/session',{method:'POST'});
    if(currentUser?.id!==owner)throw new Error('登录信息已变化，请重新打开。');
    return api(path,request);
  }
}
function loadCampusMembers(){
  universityMembersUI?.destroy();const owner=currentUser.id;
  universityMembersUI=TPMemberLinks.school($('universityMembers'),{embedded:true,request:teachingRequest,valid:()=>currentUser?.id===owner});
}
function openTeachingWorkspace(){
  if(teachingViewOwner===currentUser.id&&$('examWorkspace').children.length)return;
  teachingViewOwner=currentUser.id;
  teachingWorkspace.mount($('examWorkspace'),!campusStudent());
}
async function loadCampusDashboard(){
  const owner=currentUser.id,body=$('dashboardBody');body.replaceChildren(el('p','muted','正在读取高校教学信息…'));
  try{
    const data=await teachingRequest('/api/training/membership');if(currentUser?.id!==owner)return;
    const grid=el('div','kpi-grid');
    const stats=data.administrator?[["教职工（含管理员）",data.quota.teachers_used+" / "+data.quota.teacher_quota],["学生席位",data.quota.students_used+" / "+data.quota.student_quota],["学生额度待分配",data.quota.unallocated],["我的学生",data.mine.used]]:[["我的学生额度",data.mine.seats],["已使用",data.mine.used],["剩余可用",data.mine.remaining]];
    for(const [label,value]of stats){const card=el('div','kpi');card.append(el('div','label',label),el('div','value',value));grid.append(card);}
    const actions=el('div','sheet row');for(const [label,panel]of [["成员与席位","adminPanel"],["考证教学","examPanel"]]){const button=el('button','btn re',label);button.onclick=()=>switchPanel(panel);actions.append(button);}
    body.replaceChildren(grid,actions);
  }catch(error){if(currentUser?.id===owner)body.replaceChildren(el('p','error-box',error.message));}
}
function defaultWorkspacePanel() {
  if(campusStudent())return "examPanel";
  if(currentUser?.legacy_teaching)return "examPanel";
  return currentUser?.role === "student" ? "trainingPanel" : currentUser?.role === "platform_admin" ? "adminPanel" : "dashboardPanel";
}
function canOpenWorkspacePanel(id) {
  if (!currentUser || !$(id)?.classList.contains("panel")) return false;
  return Array.from(document.querySelectorAll("#nav [data-panel]")).some(button =>
    button.dataset.panel === id && !button.hidden && button.style.display !== "none");
}
function initialWorkspacePanel() {
  const requested = new URLSearchParams(location.hash.slice(1)).get("panel");
  return canOpenWorkspacePanel(requested) ? requested : defaultWorkspacePanel();
}
function rememberWorkspacePanel(id) {
  const hash = "#panel=" + encodeURIComponent(id);
  if (location.hash === hash) return;
  try { history.replaceState(history.state, "", location.pathname + location.search + hash); } catch (_) {}
}

// Module preparation is shared by navigation and the embedded university workspace.
const preparedPanels = new Set();
const panelPreparations = new Map();
let panelLoadRequest = 0;
let pendingPanelMessage = null;
const panelLoadTickets = new WeakMap();
function prepareWorkspacePanel(id) {
  if (preparedPanels.has(id)) return Promise.resolve();
  if (panelPreparations.has(id)) return panelPreparations.get(id);
  const preparation = (async () => {
    const load = name => loadClientScript("/" + name + "?v=" + (name==="core-inputs.js"?"20261028-review-status-2":"20261029-teaching-names-12"));
    if (["uploadPanel", "auditPanel", "historyPanel", "trainingPanel", "knowledgePanel"].includes(id)) {
      await Promise.all([load("core-inputs.js"), load("enterprise.js")]);
      if (!enterpriseMaterials) {
        enterpriseMaterials = createEnterpriseMaterials({api, el, $, user:()=>currentUser,
          clear:clearMaterials, showError, showResult:result=>{renderResult(result);switchPanel("auditPanel");},
          showReview:()=>switchPanel("uploadPanel")});
        coreInputs = createCoreInputs({api, el, $, user:()=>currentUser, showError, policyUI:()=>enterpriseMaterials.policyUI,openTaskPanel:()=>switchPanel("uploadPanel"),
          continueCheck:async id=>{
            const owner=currentUser?.id, request=panelLoadRequest;
            try {
              await prepareWorkspacePanel("knowledgePanel");
              if(currentUser?.id===owner && panelLoadRequest===request) window.KnowledgeRetrieval.openTask(id);
            } catch(error) { if(currentUser?.id===owner && panelLoadRequest===request) showError(error.message); }
          }});
        if (lastResult) enterpriseMaterials.renderReference(lastResult);
      }
    }
    if (id === "aiKeysPanel") {
      await load("ai-keys.js");
      if (!aiKeyProbes) aiKeyProbes = createAIKeyProbes({api, el, $, user:()=>currentUser});
    }
    if (id === "retrievalChecksPanel") {
      await load("retrieval-checks.js");
      if (!retrievalChecks) retrievalChecks = createRetrievalChecks({api,el,$,user:()=>currentUser,active:()=>$("retrievalChecksPanel").classList.contains("active")});
    }
    if (id === "knowledgePanel") {
      await Promise.all([load("policy-source.js"), load("graph.js"), load("rule-bindings.js"), load("knowledge-retrieval.js")]);
    }
    if (id === "trainingPanel") {
      await load("mistake-book.js");
      await load("classroom.js");
      if (!classroomUI) classroomUI = window.createTrainingClassroom({api,el,getUser:()=>currentUser,openExercise,refresh:()=>loadAssignments(),showError});
      await Promise.all([load("training-ai-coach.js"), load("training-tasks.js"), load("training-submission-ui.js"), load("teaching-case-ui.js")]);
      window.tpTrainingTasks?.setUser(currentUser);
    }
    if (id === "adminPanel" || id === "examPanel") await load("member-links.js");
    if (id === "examPanel") {
      await Promise.all([load("training-workspace.js"), load("policy-source.js"), load(campusStudent() ? "training-portal.js" : "training-staff.js")]);
      if (!teachingWorkspace) teachingWorkspace=window.createTrainingWorkspace({api,valid:()=>!!currentUser&&currentUser.id===teachingViewOwner});
    }
    preparedPanels.add(id);
  })().finally(() => panelPreparations.delete(id));
  panelPreparations.set(id, preparation);
  return preparation;
}
function workspacePanelReady(id) { return preparedPanels.has(id); }
function deferWorkspacePanel(id, request, owner) {
  const panel = $(id);
  const message = el("p", "muted", "正在加载页面组件…");
  pendingPanelMessage = message;
  panelLoadTickets.set(panel, request);
  message.setAttribute("role", "status");
  panel.before(message);
  panel.inert = true;
  panel.setAttribute("aria-busy", "true");
  prepareWorkspacePanel(id).then(() => {
    if (request === panelLoadRequest && currentUser?.id === owner) switchPanel(id);
  }).catch(error => {
    if (request !== panelLoadRequest || currentUser?.id !== owner) return;
    showError(error.message);
    const retry=el("button", "btn re", "重试加载");
    retry.type="button";
    retry.onclick=()=>{message.remove();switchPanel(id);};
    message.replaceChildren(document.createTextNode(error.message+" "),retry);
  }).finally(() => {
    if (panelLoadTickets.get(panel) === request) {
      panel.inert=!preparedPanels.has(id);
      panel.removeAttribute("aria-busy");
    }
    if (preparedPanels.has(id) || request !== panelLoadRequest || currentUser?.id !== owner) message.remove();
  });
}

function switchPanel(id) {
  if (!currentUser) return;
  if (currentUser.workspace === "university") { universityWorkspace.choose(({userSettingsPanel:"settings",trainingPanel:"tasks",knowledgePanel:"knowledge"})[id] || "home"); return; }
  if (!canOpenWorkspacePanel(id)) id = defaultWorkspacePanel();
  if(currentUser?.role==="platform_admin" && !["adminPanel","retrievalChecksPanel","aiKeysPanel","knowledgePanel","userSettingsPanel"].includes(id)) id="adminPanel";
  if (id !== "notificationPanel") noticeRequest++;
  if (id !== "userSettingsPanel") settingsRequest++;
  if (id !== "knowledgePanel" && typeof invalidateGraph === "function") invalidateGraph();
  if (id !== "adminPanel") adminRequest++;
  if (id !== "knowledgePanel") { ruleEditorRequest++; ruleCatalogRequest++; }
  const request = ++panelLoadRequest;
  pendingPanelMessage?.remove(); pendingPanelMessage = null;
  if (!workspacePanelReady(id)) {
    document.querySelectorAll(".panel").forEach(n => n.classList.toggle("active", n.id === id));
    document.querySelectorAll("#nav [data-panel]").forEach(n => n.classList.toggle("active", n.dataset.panel === id));
    rememberWorkspacePanel(id);
    deferWorkspacePanel(id, request, currentUser.id);
    return;
  }
  if (id === "uploadPanel") { refreshAIStatus(); refreshAuditClients(); enterpriseMaterials.refresh(); coreInputs.refresh().catch(err=>showError(err.message)); }
  document.querySelectorAll(".panel").forEach((n) => n.classList.toggle("active", n.id === id));
  document.querySelectorAll("#nav [data-panel]").forEach((n) => n.classList.toggle("active", n.dataset.panel === id));
  rememberWorkspacePanel(id);
  if (id === "historyPanel") loadHistory();
  if (id === "notificationPanel") loadNotifications();
  if (id === "userSettingsPanel") loadUserSettings();
  if (id === "trainingPanel") loadAssignments();
  if (id === "examPanel") openTeachingWorkspace();
  if (id === "adminPanel") loadAdmin();
  if (id === "retrievalChecksPanel") retrievalChecks.load();
  else retrievalChecks?.reset();
  if (id === "aiKeysPanel") aiKeyProbes.load();
  else aiKeyProbes?.reset();
  if (id === "dashboardPanel") loadDashboard();
  if (id === "orgReportPanel") loadOrgOverview();
  if (id === "knowledgePanel") { loadKnowledge(); loadGraphRules(); }
}

/* ---------- 多材料上传与 PDF 核对 ---------- */
const dz = $("dropzone"), fi = $("fileInput");
let materialDraft = null, materialCards = [], originalUrls = [];
let materialRequest = 0;
let coreInputs = null, retrievalChecks = null, enterpriseMaterials = null;
function materialCurrent(request,owner){return request===materialRequest&&currentUser?.id===owner;}
async function refreshAIStatus() {
  try { const status = await api("/api/materials/config"); $("aiStatus").textContent = status.message; }
  catch(err) { $("aiStatus").textContent = "AI 配置状态不可用：" + err.message; }
}
async function refreshAuditClients() {
  const field=$("auditClientField"),select=$("uploadCompanyMode");
  if(!currentUser || !["org_admin","accountant"].includes(currentUser.role)){field.style.display="none";return;}
  field.style.display="block";
  try {
    const owner=currentUser.id,clients=await api("/api/clients"),preferred=selectedClientId||select.value;
    if(currentUser?.id!==owner)return;
    const group=$("uploadClientGroup");
    group.replaceChildren();
    for(const client of clients){const o=el("option",null,client.name+" · "+client.taxpayer_id);o.value=client.id;group.append(o);}
    group.hidden=!clients.length;
    if(clients.some(client=>client.id===preferred))select.value=preferred;else select.value="new";
    selectedClientId=select.value==="new"?"":select.value;
    await enterpriseMaterials.selectClient(selectedClientId);
  } catch(err){showError(err.message);}
}
$("uploadCompanyMode").addEventListener("change",()=>{selectedClientId=$("uploadCompanyMode").value==="new"?"":$("uploadCompanyMode").value;});
function clearMaterials() {
  materialRequest++;
  enterpriseMaterials?.reset();
  coreInputs?.reset();
  $("busy").style.display="none";$("busy").textContent="正在处理材料，请稍候……";
  originalUrls.forEach(URL.revokeObjectURL); originalUrls = [];
  materialDraft = null; materialCards = [];
  $("materialReview").replaceChildren(); $("materialReview").hidden = true;
  $("batchResults").replaceChildren(); $("batchResults").hidden = true;
  $("materialReview").after($("batchResults"));
}
dz.addEventListener("click", () => fi.click());
fi.addEventListener("click", e => e.stopPropagation());
fi.addEventListener("change", () => { if (fi.files.length) upload(Array.from(fi.files)); fi.value = ""; });
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", e => { e.preventDefault(); dz.classList.remove("drag"); if (e.dataTransfer.files.length) upload(Array.from(e.dataTransfer.files)); });
async function upload(files) {
  hideError();
  if (files.length > 20) return showError("每次最多选择 20 个文件。");
  if (files.some(f => !/\.(xlsx|xls|csv|tsv|xml|pdf|png|jpg|jpeg|tif|tiff|zip)$/i.test(f.name))) return showError("支持 XLSX、受控旧版 XLS、CSV/TSV、数电票据 XML、PDF、PNG/JPEG/TIFF 和 ZIP；实际内容由服务器检查，旧 XLS 的公式、宏和对象仍拒绝。");
  if (files.some(f => !f.size || f.size > 10 * 1024 * 1024)) return showError("单个文件须非空且不超过 10MB。");
  if (files.reduce((n,f) => n+f.size,0) > 50 * 1024 * 1024) return showError("上传总大小不能超过 50MB。");
  if (enterpriseMaterials.enabled()) return enterpriseMaterials.upload(files);
  clearMaterials();
  const request=materialRequest,owner=currentUser?.id;
  $("coBar").style.display = $("statRow").style.display = $("actions").style.display = $("filterBar").style.display = "none";
  $("hitSection").style.display = $("skipSection").style.display = $("passSection").style.display = "none";
  dz.style.display = "none"; $("busy").style.display = "block";
  try {
    const fd = new FormData(); files.forEach(f => fd.append("files", f));
    fd.append("extraction", $("extractionMode").value);
    let draft = await api("/api/materials/preview", {method:"POST",body:fd});
    if(!materialCurrent(request,owner))return;
    if (draft.job_id) {
      $("busy").textContent = "AI 正在提取并校验材料，请保持页面打开……";
      const jobId = draft.job_id, started = Date.now();
      for (;;) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        if(!materialCurrent(request,owner))return;
        const job = await api("/api/materials/jobs/" + encodeURIComponent(jobId));
        if(!materialCurrent(request,owner))return;
        if (job.state === "failed") throw new Error(job.detail);
        if (job.state === "done") { draft = job.result; break; }
        if (Date.now() - started > 15 * 60 * 1000) throw new Error("提取任务等待超时，请稍后重新上传。");
      }
    }
    materialDraft=draft;renderMaterialReview(files);
  } catch(err) { if(materialCurrent(request,owner)){if($("uploadPanel").classList.contains("active"))showError(err.message);dz.style.display = "block";} }
  finally { if(materialCurrent(request,owner)){$("busy").style.display = "none"; $("busy").textContent = "正在处理材料，请稍候……";} }
}
function companyInputs(parent, values, locked = false) {
  const grid = el("div", "material-company"), inputs = {};
  [["name","企业名称"],["taxpayer_id","纳税人识别号"],["industry","所属行业"],["period","核对所属期（与账表保持一致）"]].forEach(([key,title]) => {
    const label = el("label", "", title), input = el("input");
    input.value = values[key] || ""; input.maxLength = 200; input.setAttribute("aria-label",title);
    input.readOnly = locked && Boolean(values[key]); inputs[key] = input; label.append(input); grid.append(label);
  });
  parent.append(grid); return () => Object.fromEntries(Object.entries(inputs).map(([k,v]) => [k,v.value.trim()]));
}
function checkLabel(parent, text) {
  const label = el("label","material-check"), input = el("input"); input.type = "checkbox";
  label.append(input, document.createTextNode(" " + text)); parent.append(label); return input;
}
function renderMaterialReview(files) {
  const request=materialRequest,owner=currentUser?.id,draft=materialDraft;
  const root = $("materialReview"); root.hidden = false;
  root.append(el("h2","","材料核对"),el("p","muted","先确认材料分组，再开始审计。核对草稿保留 10 分钟；材料不完整时，相应规则显示“未执行”。"));
  const mode = el("select"); mode.id = "materialMode"; mode.setAttribute("aria-label","审计方式");
  [["merge","同一企业、同一期间：合并为一次审计"],["separate","不同企业或不同期间：每个文件分别审计"]].forEach(([v,t]) => { const o=el("option","",t); o.value=v; mode.append(o); });
  root.append(mode);
  const scope = el("div"); const first = materialDraft.documents.find(d => !d.error);
  scope.append(el("p","muted","以下信息用于补全缺少企业信息的材料。已有信息不一致时，系统会阻止合并。"));
  const company = companyInputs(scope, first ? first.company : {});
  const sameScope = checkLabel(scope,"我已确认选中的材料属于同一企业、同一核对期间；历史指标已注明实际期间。不同企业材料请改用分别审计。");
  root.append(scope); mode.addEventListener("change",()=>scope.hidden=mode.value!=="merge");
  materialCards = [];
  materialDraft.documents.forEach(doc => {
    const card = el("div","material-card"); root.append(card);
    const selected = checkLabel(card,doc.name); selected.checked = !doc.error; selected.disabled = Boolean(doc.error);
    card.append(el("p",doc.error ? "material-note" : "muted",doc.error || doc.summary));
    if(doc.error) return;
    const editable = doc.kind === "pdf" || doc.review_required;
    const getCompany = companyInputs(card,doc.company,!editable);
    const state = {doc, selected, getCompany, rows:[]}; materialCards.push(state);
    doc.warnings.forEach(w => card.append(el("p","material-note",w)));
    if(editable) {
      const original = files.find(f=>f.name===doc.name);
      if(original && doc.kind === "pdf") { const url=URL.createObjectURL(original); originalUrls.push(url); const a=el("a","","打开原始 PDF 对照核对"); a.href=url; a.target="_blank"; a.rel="noopener"; card.append(a); }
      else card.append(el("p","muted","请打开本地原件对照核对；下方可查看提取文字及来源编号。"));
      const textDetail=el("details"), summary=el("summary","","查看提取的原文（按页）"); textDetail.append(summary);
      doc.pages.forEach(page => textDetail.append(el("h3","",page.label || "第 "+page.page+" 页"),el("pre","",page.text || "本页没有可提取文字，请查看原件。"))); card.append(textDetail);
      const wrap=el("div","material-table-wrap"), table=el("table","material-table"), head=el("thead"), tr=el("tr");
      ["标准指标 / 口径","数值（元；比率用小数）",doc.kind === "pdf" ? "页码" : "表编号","来源栏次及口径说明","操作"].forEach(t=>tr.append(el("th","",t))); head.append(tr); table.append(head);
      const tbody=el("tbody"); table.append(tbody); wrap.append(table); card.append(wrap);
      function addRow(row={}) {
        const tr=el("tr"), name=el("select"), value=el("input"), page=el("input"), detail=el("input"), remove=el("button","","移除");
        name.setAttribute("aria-label","标准指标"); value.setAttribute("aria-label","指标数值"); page.setAttribute("aria-label","来源页码"); detail.setAttribute("aria-label","口径说明");
        Object.entries(materialDraft.fields).sort(([a],[b])=>a.localeCompare(b,"zh")).forEach(([k,d])=>{const o=el("option","",k);o.value=k;o.title=d;name.append(o);});
        if(row.name) name.value=row.name;
        value.value=row.value ?? ""; value.placeholder="空白表示缺失"; value.inputMode="decimal";
        page.type="number"; page.min=1; page.max=doc.page_count; page.value=row.page || 1;
        detail.value=row.detail || ""; detail.maxLength=2000; detail.placeholder="例如：第1栏本期金额，不含税";
        name.title=materialDraft.fields[name.value]; name.addEventListener("change",()=>name.title=materialDraft.fields[name.value]);
        const entry={name,value,page,detail}; state.rows.push(entry);
        remove.addEventListener("click",()=>{tr.remove();state.rows=state.rows.filter(r=>r!==entry);});
        [name,value,page,detail,remove].forEach(n=>{const td=el("td");td.append(n);tr.append(td);});tbody.append(tr);
        if(row.ai_issues?.length) { const hint=el("p","material-note",row.ai_issues.join("；")); tr.cells[0].append(hint); }
        if(row.ai_raw_value !== undefined) tr.cells[1].append(el("p","muted",`AI 候选原值（待核对）：${row.ai_raw_value ?? "缺失"} ${row.ai_unit || ""}`));
      }
      doc.rows.forEach(addRow);
      const add=el("button","","＋ 手工添加未识别指标"); add.addEventListener("click",()=>addRow()); card.append(add);
      state.reviewed=checkLabel(card,"已确认提取结果及标注疑点：企业、期间、金额列和口径正确；金额统一为元，未确认的数值已清空或移除。");
    } else {
      const details=el("details"); details.append(el("summary","","查看已读取的报表及补充指标"));
      doc.rows.forEach(r=>details.append(el("p","muted",r.name+"："+r.value+" · "+r.source)));card.append(details);
      if(doc.invoices?.length){const inv=el("details");inv.append(el("summary","",`查看已读取的 ${doc.invoices.length} 张发票`));doc.invoices.forEach(r=>inv.append(el("p","muted",`${r.number} · ${r.issued_on} · ${r.kind||"待按企业税号判定"}${r.status} · 不含税 ${r.amount} · 税额 ${r.tax}`)));card.append(inv);}
      if(doc.bank_transactions?.length){const bank=el("details");bank.append(el("summary","",`查看已读取的 ${doc.bank_transactions.length} 笔银行流水（原始入账不直接作为收入）`));doc.bank_transactions.forEach(r=>bank.append(el("p","muted",`${r.transaction_id} · ${r.transacted_on} · ${Number(r.income)>0?"收入 "+r.income:"支出 "+r.expense} · ${r.category||"待调节底稿分类"}${r.summary?" · "+r.summary:""}`)));card.append(bank);}
      if(doc.bank_adjustments?.length){const bridge=el("details");bridge.append(el("summary","",`查看已读取的 ${doc.bank_adjustments.length} 条银行调节底稿`));doc.bank_adjustments.forEach(r=>bridge.append(el("p","muted",`${r.number} · ${r.transaction_id||"独立调节"} · ${r.category} · 权责期 ${r.period} · ${r.transaction_id?"本期不含税收入 "+(r.recognized_amount??"未填"):(r.direction||"未定方向")+" "+(r.adjustment_amount??"未填")} · 底稿 ${r.workpaper} · ${r.reviewed?"已复核":"待复核"}`)));card.append(bridge);}
      if(doc.human_records?.length){const human=el("details");human.append(el("summary","",`查看已读取的 ${doc.human_records.length} 条人力记录（不展示姓名及证件号码）`));doc.human_records.forEach(r=>human.append(el("p","muted",`${r.kind} · ${r.month} · ${r.active?"有效":"停保/作废等剔除"} · 金额 ${r.amount??"未提供"} · ${r.source}`)));card.append(human);}
      if(doc.contracts?.length){const contracts=el("details");contracts.append(el("summary","",`查看已读取的 ${doc.contracts.length} 份合同`));doc.contracts.forEach(r=>contracts.append(el("p","muted",`${r.number} · ${r.kind} · ${r.counterparty} · 含税 ${r.amount} · 履约期 ${r.performance_start} 至 ${r.performance_end} · ${r.reviewed?"已复核":"待复核"}${r.workpaper?" · 底稿 "+r.workpaper:""}`)));card.append(contracts);}
      if(doc.fulfillments?.length){const fulfillments=el("details");fulfillments.append(el("summary","",`查看已读取的 ${doc.fulfillments.length} 条履约记录`));doc.fulfillments.forEach(r=>fulfillments.append(el("p","muted",`${r.number} · 合同 ${r.contract_number} · ${r.fulfilled_on} · ${r.kind} · 含税 ${r.amount} · ${r.reviewed?"已复核":"待复核"}${r.workpaper?" · 底稿 "+r.workpaper:""}`)));card.append(fulfillments);}
      if(doc.contract_links?.length){const links=el("details");links.append(el("summary","",`查看已读取的 ${doc.contract_links.length} 条四流勾稽`));doc.contract_links.forEach(r=>links.append(el("p","muted",`${r.number} · 合同 ${r.contract_number} · 发票 ${r.invoice_number||"待补"} · 流水 ${r.transaction_id||"待补"} · 履约 ${r.fulfillment_number||"待补"} · 分摊含税 ${r.amount} · ${r.reviewed?"已复核":"待复核"}${r.workpaper?" · 底稿 "+r.workpaper:""}`)));card.append(links);}
      if(doc.related_graph){const graph=doc.related_graph,details=el("details");details.append(el("summary","",`查看关联方图：${graph.subjects.length} 个主体、${graph.relations.length} 条关系、${graph.trades.length} 笔交易`));graph.relations.forEach(r=>details.append(el("p","muted",`${r.key} · ${r.owner_key} → ${r.company_key} · ${r.kind} · ${r.start_on} 至 ${r.end_on||"持续"} · ${r.reviewed?"已复核":"待复核"} · ${r.basis||"无证据说明"} · ${r.source}`)));graph.trades.forEach(t=>details.append(el("p","muted",`${t.key} · ${t.seller_key} → ${t.buyer_key} · ${t.traded_on} · 含税 ${t.amount} · ${t.reviewed?"已复核":"待复核"} · ${t.anomaly_basis||"无异常依据"} · ${t.source}`)));card.append(details);}
    }
  });
  const submit=el("button","primary","确认材料并开始审计"), reset=el("button","","重新选择材料");
  reset.addEventListener("click",()=>{clearMaterials();hideError();dz.style.display="block";});
  submit.addEventListener("click",async()=>{
    if(submit.disabled||!materialCurrent(request,owner))return;
    hideError();
    const selected=materialCards.filter(c=>c.selected.checked);
    if(!selected.length) return showError("请至少选择一份有效材料。");
    if(mode.value==="merge" && !sameScope.checked) return showError("合并前请确认所有材料属于同一企业、同一核对期间。");
    if(selected.some(c=>(c.doc.kind==="pdf"||c.doc.review_required)&&!c.reviewed.checked)) return showError("请完成所选材料提取结果的核对确认。");
    const selections=selected.map(c=>({id:c.doc.id,company:c.getCompany(),reviewed:c.reviewed?.checked,
      rows:(c.doc.kind==="pdf"||c.doc.review_required)?c.rows.map(r=>({name:r.name.value,value:r.value.value.trim(),page:Number(r.page.value),detail:r.detail.value.trim()})):undefined}));
    submit.disabled=true; reset.disabled=true; $("busy").style.display="block";
    try {
      const result=await api("/api/materials/audit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:draft.token,mode:mode.value,same_scope:sameScope.checked,company:company(),client_id:selectedClientId||null,selections})});
      if(!materialCurrent(request,owner)||!$("uploadPanel").classList.contains("active"))return;
      const box=$("batchResults"); box.replaceChildren(); box.hidden=false;
      box.append(el("h2","",`完成 ${result.results.length} 份审计，${result.errors.length} 组材料未通过`));
      if(result.errors.length)box.append(el("p","muted","成功组已独立保存；重传前请逐组核查历史档案，避免重复建档。"));
      result.errors.forEach(e=>box.append(el("p","material-note",e.files.join("、")+"："+e.detail)));
      result.results.forEach(item=>{const b=el("button","",item.audit.company.name+" · 查看报告");b.addEventListener("click",()=>renderResult(item.audit));box.append(b);});
      if(result.results.length) {
        root.hidden=true;
        $("auditPanel").insertBefore(box,$("coBar"));
        const again=el("button","","上传下一组材料");again.addEventListener("click",()=>$("btnNew").click());box.append(again);
        renderResult(result.results[0].audit);
      } else box.scrollIntoView({behavior:"smooth",block:"center"});
    } catch(err){if(materialCurrent(request,owner)&&$("uploadPanel").classList.contains("active"))showError(err.message);}
    finally{submit.disabled=false;reset.disabled=false;if(materialCurrent(request,owner))$("busy").style.display="none";}
  });
  root.append(submit,reset);
}

/* ---------- 渲染 ---------- */
function renderResult(j) {
  switchPanel("auditPanel");
  $("auditEmpty").style.display = "none";
  auditId = j.audit_id;
  lastResult = j;
  enterpriseMaterials?.renderReference(j);
  snapshotReturn = null;
  $("snapshotBack").hidden = true;
  loadRiskChanges(j.audit_id);
  $("coName").textContent = j.company.name;
  $("coTaxId").textContent = j.company.taxpayer_id;
  $("coIndustry").textContent = j.company.industry;
  $("coPeriod").textContent = j.company.period;
  $("coMetrics").textContent = j.metrics.length + " 项";
  renderMaterialEvidence(j.metrics);
  $("auditTime").textContent = "审计时间 " + j.audited_at;
  $("coBar").style.display = "block";

  $("nHit").textContent = j.summary.hit;
  $("nHigh").textContent = j.summary.high;
  $("nPass").textContent = j.summary.pass;
  $("nSkip").textContent = j.summary.skipped;
  $("statRow").style.display = "grid";
  $("filterBar").style.display = "block";
  $("actions").style.display = "flex";
  $("auditNarrativeSection").hidden = false;
  renderAuditNarrative(j.narrative || null);

  latestFindings = j.findings;
  const category = $("filterCategory"); category.textContent = ""; const allCategory = el("option",null,"全部"); allCategory.value=""; category.appendChild(allCategory);
  for (const value of Array.from(new Set(j.findings.map((f)=>f.category))).sort()) { const o=el("option",null,value); o.value=value; category.appendChild(o); }
  const tax = $("filterTax"); tax.textContent = ""; const allTax = el("option",null,"全部"); allTax.value=""; tax.appendChild(allTax);
  for (const value of Array.from(new Set(j.findings.map((f)=>f.tax_type))).sort()) { const o=el("option",null,value); o.value=value; tax.appendChild(o); }
  applyFilters();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMaterialEvidence(metrics) {
  const box=$("materialEvidence");box.replaceChildren();
  const selected=(metrics||[]).filter(m=>/^(人力|个税|社保|公积金|合同)\./.test(m.name));
  box.hidden=!selected.length;
  if(!selected.length)return;
  box.append(el("summary","","人力与合同归集证据（"+selected.length+" 项）"),el("p","muted","以下为本次已保存的归集指标。四流完整仅表示已复核材料关联及金额勾稽完整，不等于税务合规；待完善不算通过。人力人数按证据所示核对月统计，不是期间人数累加。"));
  for(const metric of selected){const item=el("div","material-card");item.append(el("h3","",metric.name+"："+metric.value),el("p","muted",metric.detail||"未记录口径说明"),el("p","muted","来源："+(metric.source||"未记录")));box.append(item);}
}

function renderAuditNarrative(narrative) {
  const panel = $("auditNarrativePanel"), status = $("auditNarrativeStatus"), button = $("btnAuditNarrative");
  panel.replaceChildren();
  status.textContent = "";
  button.textContent = narrative ? "重新读取已保存内容" : "生成总体评价与建议";
  if (!narrative) {
    panel.appendChild(el("p", "muted", "尚未生成。确定性统计和逐条证据不受影响，生成后会同步进入 HTML/PDF 报告。"));
    return;
  }
  panel.appendChild(el("div", "audit-narrative-locked", `规则统计已锁定：命中 ${narrative.summary.hit} 项、通过 ${narrative.summary.pass} 项、未执行 ${narrative.summary.skipped} 项`));
  const addGroup = (title, items) => {
    panel.appendChild(el("h4", null, title));
    for (const item of items) {
      const block = el("div", "audit-narrative-paragraph");
      block.append(el("p", null, item.text), el("p", "audit-narrative-citations", "来源规则：" + item.citations.join("、")));
      panel.appendChild(block);
    }
  };
  addGroup("总体评价", narrative.overall_assessment);
  addGroup("处理建议", narrative.recommendations);
  panel.appendChild(el("p", "finding-ai-meta", `仅汇总，不改变规则结论 · ${narrative.model} · 证据版本 ${narrative.evidence_hash.slice(0,12)} · ${narrative.cached ? "已保存内容" : "本次生成"}`));
}

$("btnAuditNarrative").addEventListener("click", async () => {
  if (!auditId) return;
  const button = $("btnAuditNarrative"), status = $("auditNarrativeStatus");
  button.disabled = true;
  button.textContent = "正在基于 Finding 汇总……";
  status.textContent = "模型只负责报告文字，规则结论不会改变。";
  try {
    const narrative = await api("/api/audits/" + encodeURIComponent(auditId) + "/narrative", {method:"POST"});
    renderAuditNarrative(narrative);
    status.textContent = "总体评价已保存，并同步用于本次审计的 HTML/PDF 报告。";
  } catch (err) {
    status.textContent = err.message || String(err);
    button.textContent = "重试生成总体评价与建议";
  } finally {
    button.disabled = false;
  }
});

function applyFilters() {
  const filtered = latestFindings.filter((f) => (!$("filterStatus").value || f.status === $("filterStatus").value) && (!$("filterSeverity").value || f.severity === $("filterSeverity").value) && (!$("filterCategory").value || f.category === $("filterCategory").value) && (!$("filterTax").value || f.tax_type === $("filterTax").value));
  const hits = filtered.filter((f) => f.status === "hit");
  const skips = filtered.filter((f) => f.status === "skipped");
  const passes = filtered.filter((f) => f.status === "pass");

  $("hitCnt").textContent = "命中 " + hits.length + " 项，点击卡片展开完整证据链";
  renderCards($("hitList"), hits);
  renderCards($("skipList"), skips);
  renderCards($("passList"), passes);

  $("hitSection").style.display = hits.length ? "block" : "none";
  $("skipSection").style.display = skips.length ? "block" : "none";
  $("passSection").style.display = passes.length ? "block" : "none";
  $("passFoldText").textContent = "检查通过事项（" + passes.length + " 项）—— 已执行，未发现异常";
  $("passList").classList.remove("open");

}

for (const id of ["filterStatus","filterSeverity","filterCategory","filterTax"]) $(id).addEventListener("change",applyFilters);

/* 一张规则卡：head（可点击展开/收起）+ body（证据链） */
function renderCards(container, findings) {
  container.textContent = "";
  for (const f of findings) container.appendChild(findingCard(f));
}

function findingCard(f) {
  const card = el("div", "card");

  const head = el("div", "card-head");
  const sev = f.status === "hit" ? f.severity : (f.status === "skipped" ? "skip" : "pass");
  const sevText = f.status === "hit" ? f.severity_label : (f.status === "skipped" ? "未执行" : "通过");
  head.appendChild(el("span", "tag " + sev, sevText));
  head.appendChild(el("span", "rid", f.id));
  head.appendChild(el("span", "rname", f.name));
  if (f.status === "hit") head.appendChild(el("span", "concl", f.conclusion));
  head.appendChild(el("span", "arrow", "▶"));
  head.addEventListener("click", () => card.classList.toggle("open"));
  card.appendChild(head);

  const body = el("div", "card-body");

  /* 基本属性 */
  const kv = el("div", "kv");
  kv.appendChild(el("div", null, "风险类型：" + f.category + "　·　税种：" + f.tax_type + "　·　规则版本 v" + f.version + (f.effective_from ? "（" + f.effective_from + " 至 " + (f.effective_to || "持续") + "）" : "")));
  if (f.scope) kv.appendChild(el("div", null, "适用范围：" + f.scope));
  body.appendChild(kv);

  /* 未执行：原因置顶 */
  if (f.status === "skipped") {
    body.appendChild(el("div", "ev-title", "未执行原因（请按实际原因核对后复检）"));
    body.appendChild(el("div", "calc-box", f.skip_reason || f.conclusion));
  }

  /* 触发明细 */
  if (f.evidence && f.evidence.length) {
    body.appendChild(el("div", "ev-title", "触发明细（每项数值均标注取数来源）"));
    const tbl = el("table", "ev finding-evidence");
    const columns = el("colgroup");
    for (const name of ["item", "value", "source"]) columns.appendChild(el("col", "evidence-" + name));
    tbl.appendChild(columns);
    const trh = el("tr");
    for (const [index, h] of ["项目", "数值", "取数来源"].entries()) {
      const th = el("th", index === 1 ? "v" : null, h);
      th.scope = "col";
      trh.appendChild(th);
    }
    const thead = el("thead"); thead.appendChild(trh); tbl.appendChild(thead);
    const tb = el("tbody");
    for (const e of f.evidence) {
      if (e.label === "阈值依据") continue;
      const tr = el("tr", e.emphasis ? "emph" : null);
      tr.appendChild(el("td", null, e.label));
      tr.appendChild(el("td", "v num", e.value));
      tr.appendChild(el("td", "src", e.source));
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    if (tb.children.length) {
      const scroll = el("div", "finding-evidence-scroll");
      scroll.tabIndex = 0;
      scroll.setAttribute("role", "region");
      scroll.setAttribute("aria-label", "触发明细，窄屏可横向滚动查看取数来源");
      scroll.appendChild(tbl);
      body.appendChild(scroll);
    }
    for (const e of f.evidence.filter(item => item.label === "阈值依据")) {
      const note = el("div", "finding-evidence-note");
      note.append(el("strong", null, e.label), el("p", null, e.value), el("p", "muted", e.source));
      body.appendChild(note);
    }
  }

  /* 计算过程 + 阈值 */
  if (f.calculation) {
    body.appendChild(el("div", "ev-title", "计算过程"));
    body.appendChild(el("div", "calc-box num", f.calculation));
  }
  if (f.threshold_desc) {
    body.appendChild(el("div", "ev-title", "判定阈值"));
    body.appendChild(el("div", "kv", f.threshold_desc + (f.threshold_basis ? "（" + f.threshold_basis + "）" : "")));
  }

  /* 法条依据 */
  if (f.legal_basis && f.legal_basis.length) {
    body.appendChild(el("div", "ev-title", "法律依据"));
    const ul = el("ul", "plain-list");
    for (const item of f.legal_basis) ul.appendChild(el("li", null, item));
    body.appendChild(ul);
  }

  /* 建议 */
  if (f.status === "hit" && f.suggestion) {
    body.appendChild(el("div", "ev-title", "核查建议"));
    body.appendChild(el("div", "calc-box", f.suggestion));
  }

  /* 命中 Finding 的受约束模型解读：规则结论始终由上方确定性证据卡负责。 */
  if (f.status === "hit") {
    const tools = el("div", "finding-ai-tools");
    const explain = el("button", "btn finding-ai-btn", "✦ AI 白话解读");
    explain.type = "button";
    const panel = el("div", "finding-ai-panel");
    panel.hidden = true;
    explain.addEventListener("click", async () => {
      if (!auditId) return;
      explain.disabled = true;
      explain.textContent = "正在基于证据解读……";
      panel.hidden = false;
      panel.replaceChildren(el("p", "muted", "模型只翻译当前 Finding，不参与风险判定。"));
      try {
        const result = await api("/api/audits/" + encodeURIComponent(auditId) + "/findings/" + encodeURIComponent(f.id) + "/interpretation", {method:"POST"});
        panel.replaceChildren();
        const locked = el("div", "finding-ai-locked", "规则引擎结论已锁定：命中（" + result.citation + "）");
        panel.appendChild(locked);
        panel.appendChild(el("div", "ev-title", "业务白话"));
        panel.appendChild(el("p", null, result.plain_language));
        panel.appendChild(el("div", "ev-title", "为什么触发"));
        panel.appendChild(el("p", null, result.why_flagged));
        panel.appendChild(el("div", "ev-title", "建议核对顺序"));
        const ol = el("ol", "finding-ai-steps");
        for (const step of result.review_steps) ol.appendChild(el("li", null, step));
        panel.appendChild(ol);
        panel.appendChild(el("p", "finding-ai-meta", "仅解释，不改变规则结论 · " + result.model + " · " + (result.cached ? "已保存解读" : "本次生成")));
        explain.textContent = "已生成 AI 白话解读";
      } catch (err) {
        panel.replaceChildren(el("div", "finding-ai-error", err.message || String(err)));
        explain.textContent = "重试 AI 白话解读";
      } finally {
        explain.disabled = false;
      }
    });
    tools.appendChild(explain);
    tools.appendChild(el("span", "finding-ai-note", "仅解释，不改变规则结论"));
    body.appendChild(tools);
    body.appendChild(panel);
  }

  /* 参考链接 */
  if (f.references && f.references.length) {
    body.appendChild(el("div", "ev-title", "官方参考"));
    const ul = el("ul", "plain-list");
    for (const url of f.references) {
      const li = el("li");
      const a = el("a", null, url); a.href = url; a.target = "_blank"; a.rel = "noopener";
      li.appendChild(a); ul.appendChild(li);
    }
    body.appendChild(ul);
  }

  card.appendChild(body);
  return card;
}

/* ---------- 折叠 / 导出 / 重来 ---------- */
$("passFold").addEventListener("click", () => {
  $("passFold").classList.toggle("open");
  $("passList").classList.toggle("open");
});

$("btnPdf").addEventListener("click", async () => {
  if (!auditId) return;
  const btn = $("btnPdf"); btn.disabled = true; btn.textContent = "正在生成 PDF……";
  try {
    const confirmed = currentUser && currentUser.role === "accountant" ? window.confirm("确认导出该客户报告并记录本次导出行为？") : true;
    if (!confirmed) return;
    const r = await fetch("/api/report/" + auditId + (currentUser && currentUser.role === "accountant" ? "?confirm=true" : ""));
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || "PDF 生成失败");
    }
    const blob = await r.blob();
    const a = el("a"); a.href = URL.createObjectURL(blob);
    a.download = "税务风险审计报告.pdf";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) {
    showError(err.message || String(err));
  } finally {
    btn.disabled = false; btn.textContent = "导出 A4 PDF 报告";
  }
});

$("btnNew").addEventListener("click", () => {
  switchPanel("uploadPanel");
  $("auditEmpty").style.display = "block";
  clearMaterials();
  auditId = null;
  lastResult = null;
  snapshotReturn = null;
  $("snapshotBack").hidden = true;
  latestFindings = [];
  $("coBar").style.display = $("statRow").style.display = $("actions").style.display = $("filterBar").style.display = "none";
  $("auditNarrativeSection").hidden = true;
  $("auditNarrativePanel").replaceChildren();
  $("auditNarrativeStatus").textContent = "";
  $("hitSection").style.display = $("skipSection").style.display = $("passSection").style.display = "none";
  $("hitList").textContent = $("skipList").textContent = $("passList").textContent = "";
  hideError();
  dz.style.display = "block";
  window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ---------- P1 登录、档案、实训与管理 ---------- */
function enterWorkspace(user) {
  universityMembersUI?.destroy();universityMembersUI=null;teachingWorkspace?.destroy();teachingViewOwner=null;$('examWorkspace').replaceChildren();
  $("loginRemember").checked = false;
  authViewVersion++;pendingEmailToken="";emailRegistrationProof=null;authResetProof="";
  for(const id of ["signupPass","signupPass2","newPass","newPass2","signupEmailCode","loginPass"])$(id).value="";
  trainingLoadRequest++; exerciseOpenRequest++; sandboxFeedbackRequest++; openAssignmentId=null;
  clearTimeout(exerciseDeadlineTimer); classroomUI?.reset(); $("exerciseBox").style.display="none";
  $("exerciseGenerationResult").textContent=""; $("exerciseGenerationResult").hidden=true;
  const campusIdentity=universityWorkspace.resolveIdentity(user);
  currentUser=campusIdentity?{...user,workspace:"university",role:campusIdentity.role==="admin"?"teacher":user.role}:user;
  window.tpTrainingTasks?.setUser(currentUser);
  window.tpSubmissionUI?.reset();
  $("tp-case-panel")?.remove();
  $("loginCollege").value="";delete $("loginCollege").dataset.needed;$("loginCollegeField").hidden=true;$("loginCollege").disabled=true;
  if(campusIdentity){universityWorkspace.enter(campusIdentity);return;}
  settingsRequest++; settingsReady = false;
  memberRequest++; memberSnapshot=null; $("memberOrg").textContent=""; $("userList").textContent="";
  clearMemberInvitations(); $("signupCode").value=""; delete $("authForm").dataset.inviteCode;
  $("memberQuotaStatus").textContent="尚未加载成员信息。"; $("memberQuotaEditor").hidden=true;
  resetMemberQuotaEditor();$("universityCapacityPanel").hidden=true;$("universityCapacityStatus").textContent="";
  $("btnSaveUniversityCapacity").disabled=true;$("universityTeacherQuota").value="";$("universityStudentQuota").value="";
  $("authScreen").style.display = "none";
  document.body.classList.add("workspace-ready");
  $("appHeader").style.display = "block"; $("appMain").style.display = "block"; $("appFooter").style.display = "block";
  $("nav").style.display = "flex";
  $("currentUser").textContent = user.display_name + " · " + accountRoleName(user);
  const campus=isCampusUser(user),staff=campus&&!campusStudent(user);
  for(const [panel,text]of [["uploadPanel","上传材料"],["auditPanel","审计详情"],["historyPanel","历史档案"]])document.querySelector('[data-panel="'+panel+'"] .nav-label').textContent=text;
  const caption=campus?"高校工作空间":user.role==="platform_admin"?"平台管理":user.org_type==="enterprise"?"企业工作空间":"机构工作空间";
  $('nav').setAttribute('aria-label',caption);document.querySelector('#nav .nav-caption').textContent=caption;
  aiKeyProbes?.reset();
  retrievalChecks?.reset();
  document.querySelector('[data-panel="aiKeysPanel"]').style.display = user.role === "platform_admin" ? "" : "none";
  document.querySelector('[data-panel="retrievalChecksPanel"]').style.display = user.role === "platform_admin" ? "" : "none";
  document.querySelectorAll(".training-nav").forEach((n) => n.style.display = ["teacher","student"].includes(user.role) ? "" : "none");
  document.querySelectorAll(".admin-nav").forEach((n) => n.style.display = ["platform_admin","org_admin","teacher","accountant"].includes(user.role) ? "" : "none");
  $("teacherBox").style.display = user.role === "teacher" ? "block" : "none";
  $("orgSettingsAdmin").style.display = user.role === "org_admin" ? "block" : "none";
  $("inviteAdmin").style.display = user.role === "platform_admin" ? "block" : "none";
  $("createUserRow").style.display = user.role === "org_admin" ? "flex" : "none";
  $("userAdmin").style.display = ["platform_admin","org_admin"].includes(user.role) ? "block" : "none";
  $("clientAdmin").style.display = user.role === "org_admin" ? "block" : "none";
  const ruleAccess = ["platform_admin","org_admin","teacher","accountant"].includes(user.role) && !isCampusUser();
  $("ruleAdmin").hidden = !ruleAccess;
  $("ruleAdminLink").hidden = !ruleAccess;
  ruleCatalogRequest++; ruleEditorRequest++; ruleHistoryRequest++;
  graphRuleCatalog=[]; editingRule=null; $("ruleList").replaceChildren(); $("ruleEditor").hidden=true;

  $("logAdmin").style.display = ["platform_admin","org_admin","teacher"].includes(user.role) ? "block" : "none";
  $("auditClientField").style.display = ["org_admin","accountant"].includes(user.role) ? "block" : "none";
  const businessUser = !["student","platform_admin"].includes(user.role);
  document.querySelector('[data-panel="auditPanel"]').style.display = businessUser ? "" : "none";
  document.querySelectorAll(".business-nav, [data-panel='historyPanel'], [data-panel='notificationPanel']").forEach(n => n.style.display = businessUser ? "" : "none");
  document.querySelector('[data-panel="orgReportPanel"]').style.display = user.role === "org_admin" ? "" : "none";
  document.querySelector('[data-panel="examPanel"]').style.display=campus&&user.education_role?"":"none";
  document.querySelector('[data-panel="examPanel"] .nav-label').textContent=campusStudent(user)?"考证备考":"考证教学";
  document.querySelector('#examPanel h2').textContent=campusStudent(user)?"考证备考":"考证教学";
  if(campus){
    $('clientAdmin').style.display='none';$('logAdmin').style.display='none';
    $('userAdmin').style.display=staff?'block':'none';$('createUserRow').style.display='none';
    document.querySelector('[data-panel="adminPanel"]').style.display=staff?'':'none';
    document.querySelector('[data-panel="orgReportPanel"]').style.display='none';
    if(user.legacy_teaching)for(const panel of ['trainingPanel','uploadPanel','auditPanel','historyPanel','notificationPanel','knowledgePanel','dashboardPanel'])document.querySelector('[data-panel="'+panel+'"]').style.display='none';
    for(const [panel,text]of [['uploadPanel','案例材料'],['auditPanel','案例详情'],['historyPanel','案例档案']])document.querySelector('[data-panel="'+panel+'"] .nav-label').textContent=text;
  }
  for(const child of $('userAdmin').children)child.classList.toggle('campus-hidden',campus&&!['universityMembers','userAdminHint'].includes(child.id)&&child.tagName!=='H2');
  $('universityMembers').hidden=!staff;
  if(!staff)$('universityMembers').replaceChildren();
  $('userAdminHint').textContent=campus?(staff?'高校管理员邀请教师并分配学生额度；每位教师邀请自己的学生，管理员也可给自己分配。学校总额度由平台顶级管理员设置。':'学生可在教学面板查看自己的学习任务。'):'机构管理员管理本机构员工；加入时核销席位，停用释放席位并撤销会话，恢复后须重新登录。';
  $('dashboardFilters').hidden=campus;
  document.querySelector('#dashboardPanel .page-head h2').textContent=campus?'教学工作台':'工作台';
  document.querySelector('#dashboardPanel .page-head .muted').textContent=campus?'汇总教学与学生席位，管理成员邀请和学习任务。':'从企业业务数据出发，了解经营表现与税务风险。';
  $('dashboardUpload').textContent=campus?'进入教学 ↗':'上传材料 ↗';
  switchPanel(initialWorkspacePanel());
}

$("btnLogout").addEventListener("click", async () => { if (currentUser?.workspace === "university") { try { await universityWorkspace.logout(); } catch (err) { showToast(err.message); } return; } await api("/api/logout", {method:"POST"}); location.reload(); });
document.querySelectorAll("#nav [data-panel]").forEach((b) => b.addEventListener("click", () => switchPanel(b.dataset.panel)));

/* ---------- 用户设置：复用本人通知偏好与现有报告入口 ---------- */
let settingsRequest = 0, settingsReady = false, settingsHasEmail = false, settingsWrite = null;
function settingsCurrent(request, owner) {
  return request === settingsRequest && currentUser?.id === owner && $("userSettingsPanel").classList.contains("active");
}
function settingsNotificationAccess() {
  if(currentUser?.legacy_teaching)return false;
  return ["org_admin", "accountant", "teacher"].includes(currentUser?.role);
}
function settingsControls() {
  const busy = !!settingsWrite;
  $("settingsPreferences").disabled = !settingsReady || busy || !settingsNotificationAccess();
  $("settingsEmail").disabled = !settingsReady || busy || !settingsHasEmail;
  $("settingsSave").disabled = !settingsReady || busy || !settingsNotificationAccess();
  $("settingsRefresh").disabled = !!busy;
  $("settingsSave").textContent = busy ? "正在保存……" : "保存通知偏好";
}
function renderSettingsAccount() {
  const user = currentUser;
  const role = accountRoleName(user);
  $("currentUser").textContent = (user.display_name || user.username) + " · " + role;
  $("settingsUsername").textContent = user.username || "—";
  $("settingsDisplayName").textContent = user.display_name || "—";
  $("settingsRole").textContent = role;
  const created = user.created_at ? new Date(user.created_at) : null;
  $("settingsCreatedAt").textContent = created && !Number.isNaN(created.getTime()) ? created.toLocaleString("zh-CN", {hour12:false}) : "—";
  const history = canOpenWorkspacePanel("historyPanel"), org = canOpenWorkspacePanel("orgReportPanel");
  $("settingsHistoryExport").hidden = !history;
  $("settingsOrgExport").hidden = !org;
  $("settingsExportStatus").textContent = history || org ? "导出范围沿用当前账号的访问权限。" : "当前角色暂无审计报告导出入口。";
  $("settingsNoticeOpen").hidden = !canOpenWorkspacePanel("notificationPanel");
}
async function loadUserSettings(message = "") {
  if (!currentUser || !$("userSettingsPanel").classList.contains("active")) return false;
  const request = ++settingsRequest, owner = currentUser.id;
  renderSettingsAccount();
  settingsReady = false; settingsHasEmail = false;
  for (const id of ["settingsCompleted", "settingsHigh", "settingsEmail"]) $(id).checked = false;
  settingsControls();
  if (!settingsNotificationAccess()) {
    $("settingsRefresh").disabled = true;
    $("settingsStatus").textContent = "当前角色尚未开放审计通知订阅。";
    return false;
  }
  $("settingsStatus").textContent = "正在读取通知偏好……";
  try {
    // Re-entering while a save is pending must wait for that write before reading.
    if (settingsWrite) {
      const writeOwner = settingsWrite.owner;
      const result = await settingsWrite.promise;
      if (!settingsCurrent(request, owner)) return false;
      if (!result.ok && writeOwner === owner) message = "上次保存结果未确认，请核对当前偏好。";
    }
    if (!settingsCurrent(request, owner)) return false;
    const prefs = await api("/api/notifications/preferences");
    if (!settingsCurrent(request, owner)) return false;
    settingsHasEmail = !!prefs.has_email;
    $("settingsCompleted").checked = !!prefs.audit_completed;
    $("settingsHigh").checked = !!prefs.high_risk;
    $("settingsEmail").checked = !!prefs.email_enabled;
    const delivery = !prefs.has_email ? "账号未绑定邮箱，仅可接收站内通知。" :
      prefs.delivery_enabled ? "邮件发送服务已启用。" : "邮件发送服务未启用，邮件将留在队列中。";
    $("settingsStatus").textContent = (message ? message + " " : "") + delivery + " 关闭邮件通知不撤回已受理的邮件。";
    settingsReady = true;
    return true;
  } catch (error) {
    if (settingsCurrent(request, owner)) $("settingsStatus").textContent = "读取失败，请重新读取后再保存：" + error.message;
    return false;
  } finally { if (settingsCurrent(request, owner)) settingsControls(); }
}
async function saveUserSettings() {
  if (!currentUser || !settingsReady || !settingsNotificationAccess() || settingsWrite || !$("userSettingsPanel").classList.contains("active")) return false;
  const request = ++settingsRequest, owner = currentUser.id;
  const body = {audit_completed:$("settingsCompleted").checked, high_risk:$("settingsHigh").checked,
    email_enabled:settingsHasEmail && $("settingsEmail").checked};
  const operation = {owner, promise:api("/api/notifications/preferences", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})
    .then(() => ({ok:true}), error => ({ok:false, error}))};
  settingsWrite = operation; settingsControls();
  $("settingsStatus").textContent = "正在保存通知偏好……";
  const result = await operation.promise;
  if (settingsWrite === operation) settingsWrite = null;
  if (!settingsCurrent(request, owner)) return false;
  if (result.ok) return loadUserSettings("通知偏好已保存。");
  settingsReady = false; settingsControls();
  $("settingsStatus").textContent = "保存结果未确认，请重新读取后核对：" + result.error.message;
  return false;
}
$("settingsSave").onclick = saveUserSettings;
$("settingsRefresh").onclick = () => loadUserSettings();
$("settingsNoticeOpen").onclick = () => switchPanel("notificationPanel");
$("settingsHistoryOpen").onclick = () => switchPanel("historyPanel");
$("settingsOrgOpen").onclick = () => switchPanel("orgReportPanel");

function noticeCurrent(request, owner) {
  return request === noticeRequest && currentUser?.id === owner && $("notificationPanel").classList.contains("active");
}
function noticeControls(loading = false) {
  const disabled = loading || noticeBusy || !noticeReady;
  for (const id of ["noticeCompleted", "noticeHigh", "noticeSave"]) $(id).disabled = disabled;
  $("noticeEmail").disabled = disabled || !noticeHasEmail;
  $("noticeRefresh").disabled = noticeBusy;
  document.querySelectorAll("#noticeList button, #noticeRecipients button, #noticeRecipients input").forEach(n => { n.disabled = disabled; });
}
async function noticeAction(action, message = "", openAudit = false) {
  if (noticeBusy || !noticeReady || !$("notificationPanel").classList.contains("active")) return;
  noticeBusy = true;
  noticeReady = false;
  const request = ++noticeRequest, owner = currentUser?.id;
  let interrupted = false;
  noticeControls();
  try {
    const result = await action();
    if (!noticeCurrent(request, owner)) { interrupted = true; return; }
    if (openAudit) { renderResult(result); switchPanel("auditPanel"); return; }
    noticeBusy = false;
    if (await loadNotifications()) $("noticeStatus").textContent += " " + message;
  } catch (error) {
    interrupted = !noticeCurrent(request, owner);
    if (noticeCurrent(request, owner)) {
      noticeReady = false;
      $("noticeStatus").textContent = "操作未确认，请刷新核对后再试：" + error.message;
    }
  } finally {
    noticeBusy = false;
    noticeControls();
    if (interrupted && currentUser?.id === owner && $("notificationPanel").classList.contains("active")) await loadNotifications();
  }
}
async function loadNotifications() {
  if (!currentUser || noticeBusy || !$("notificationPanel").classList.contains("active")) return false;
  const request = ++noticeRequest, owner = currentUser.id, admin = currentUser.role === "org_admin";
  noticeReady = false;
  noticeControls(true);
  $("noticeList").replaceChildren(); $("noticeRecipients").replaceChildren(); $("noticeRecipientBox").hidden = true;
  $("noticeStatus").textContent = "正在读取通知与订阅……";
  try {
    const [prefs, notices, members] = await Promise.all([api("/api/notifications/preferences"),
      api("/api/notifications"), admin ? api("/api/notifications/recipients") : Promise.resolve([])]);
    if (!noticeCurrent(request, owner)) return false;
    noticeHasEmail = prefs.has_email;
    $("noticeCompleted").checked = prefs.audit_completed;
    $("noticeHigh").checked = prefs.high_risk;
    $("noticeEmail").checked = prefs.email_enabled;
    $("noticeStatus").textContent = !prefs.has_email ? "账号未绑定邮箱，仅可接收站内通知。" :
      (prefs.delivery_enabled ? "邮件发送服务已启用。" : "邮件发送服务未启用：订阅仍会保存，邮件留在队列中。") + " 退订不撤回已受理邮件。";
    const box = $("noticeList"); box.textContent = "";
    const labels = {pending:"待发送",claimed:"发送中",accepted:"邮件已受理（非送达）",failed:"发送失败",uncertain:"发送结果未知",suppressed:"邮件已取消"};
    for (const item of notices) {
      const card = el("div", "sheet");
      card.append(el("h3", null, (item.event === "high_risk" ? "高风险提醒" : "审计完成") + (item.read_at ? " · 已读" : " · 未读")),
        el("p", "muted", item.created_at + " · " + item.audit_id),
        el("p", null, `风险 ${item.summary.hit_count} 项，高风险 ${item.summary.high_count} 项 · ${labels[item.email_status] || "仅站内通知"}`));
      for (const risk of item.summary.risks) card.append(el("p", null, risk.id + "：" + risk.name));
      const open = el("button", "btn pdf", "查看审计");
      open.onclick = () => noticeAction(async () => { const result = await api("/api/audits/" + item.audit_id); await api("/api/notifications/" + item.id + "/read", {method:"PUT"}); return result; }, "", true);
      card.append(open);
      if (!item.read_at) { const read = el("button", "btn re", "标为已读"); read.onclick = () => noticeAction(() => api("/api/notifications/" + item.id + "/read", {method:"PUT"})); card.append(read); }
      if (["failed", "uncertain"].includes(item.email_status)) { const retry = el("button", "btn re", "申请邮件重试"); retry.onclick = () => noticeAction(() => api("/api/notifications/" + item.id + "/retry", {method:"POST"}), "已申请重试，发送结果请刷新查看。"); card.append(retry); }
      box.append(card);
    }
    if (!box.childElementCount) box.append(el("p", "muted", "暂无通知。订阅只影响之后的审计。"));
    $("noticeRecipientBox").hidden = !admin;
    if (admin) {
      const recipients = $("noticeRecipients"); recipients.textContent = "";
      for (const member of members) {
        const row = el("div", "notice-recipient"), events = el("div", "notice-recipient-events");
        row.append(el("span", "notice-recipient-name", member.display_name + " · " + member.org_id));
        const inputs = {};
        for (const [key, title] of [["audit_completed","审计完成"],["high_risk","高风险"]]) {
          const label = el("label"); const input = el("input"); input.type = "checkbox"; input.checked = member[key]; inputs[key] = input; label.append(input, document.createTextNode(title)); events.append(label);
        }
        row.append(events, el("span", "muted notice-recipient-email", member.email_enabled ? "本人已同意邮件" : "本人未开邮件"));
        const save = el("button", "btn re", "保存接收事件"); save.onclick = () => noticeAction(() => api("/api/notifications/recipients/" + member.id, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({audit_completed:inputs.audit_completed.checked, high_risk:inputs.high_risk.checked, email_enabled:member.email_enabled})}), "接收事件已保存。"); row.append(save); recipients.append(row);
      }
    }
    noticeReady = true;
    return true;
  } catch(e) { if (noticeCurrent(request, owner)) $("noticeStatus").textContent = "读取失败，请刷新重试：" + e.message; return false; }
  finally { if (noticeCurrent(request, owner)) noticeControls(); }
}
$("noticeRefresh").onclick = loadNotifications;
$("noticeSave").onclick = () => noticeAction(() => api("/api/notifications/preferences", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({audit_completed:$("noticeCompleted").checked, high_risk:$("noticeHigh").checked, email_enabled:$("noticeEmail").checked})}), "订阅已保存。");

let currentRiskChanges = null;
let riskChangesRequest = 0;
async function loadRiskChanges(id, baseline = "") {
  const request = ++riskChangesRequest;
  currentRiskChanges = null;
  $("riskChangesSection").hidden = false;
  $("riskBaseline").disabled = true;
  $("riskChangesStatus").textContent = "正在比较已冻结的审计快照……";
  $("riskChangesList").textContent = "";
  $("riskBaselineOpen").hidden = true;
  try {
    const result = await api("/api/audits/" + encodeURIComponent(id) + "/changes" + (baseline ? "?baseline_id=" + encodeURIComponent(baseline) : ""));
    if (auditId !== id || request !== riskChangesRequest) return;
    currentRiskChanges = result;
    const select = $("riskBaseline"); select.textContent = "";
    for (const candidate of result.baselines) { const option = el("option", null, candidate.period + " · " + candidate.audited_at + " · " + candidate.id.slice(0,8)); option.value = candidate.id; select.append(option); }
    select.disabled = result.status !== "ready";
    if (result.status !== "ready") { $("riskChangesStatus").textContent = result.message; return; }
    select.value = result.baseline.id;
    $("riskBaselineOpen").hidden = false;
    $("riskChangesStatus").textContent = `基期 ${result.baseline.period} → 本期 ${result.current.period}；新增 ${result.counts.new}、消失 ${result.counts.resolved}、偏离恶化 ${result.counts.worsened}、偏离改善 ${result.counts.improved}、无法比较 ${result.counts.incomparable}、口径变化 ${result.counts.rule_changed}。` + (result.gap ? " 两期不连续，中间变化未知。" : "");
    renderRiskChanges();
  } catch (error) { if (auditId === id && request === riskChangesRequest) { currentRiskChanges = null; $("riskChangesStatus").textContent = "跨期比较失败：" + error.message; } }
}
function renderRiskChanges() {
  const box = $("riskChangesList"); box.textContent = "";
  if (!currentRiskChanges || currentRiskChanges.status !== "ready") return;
  for (const item of currentRiskChanges.items) {
    const filter = $("riskChangeFilter").value;
    if (filter === "attention" && ["clear", "persistent", "incomparable"].includes(item.change)) continue;
    if (!["attention", "all"].includes(filter) && item.change !== filter) continue;
    const card = el("div", "sheet");
    card.append(el("h3", null, item.label + " · " + item.rule_id + " " + item.name),
      el("p", "muted", `基期 ${item.before_status} / v${item.before_version || "无"} → 本期 ${item.after_status} / v${item.after_version || "无"}`), el("p", null, item.reason));
    if (item.before_distance !== null && item.after_distance !== null) card.append(el("p", null, "偏离值 " + item.before_distance + " → " + item.after_distance));
    box.append(card);
  }
  if (!box.childElementCount) box.append(el("p", "muted", "所选类型无变化；可切换全部规则或无法比较查看完整状态。"));
}
$("riskBaseline").onchange = () => loadRiskChanges(auditId, $("riskBaseline").value);
$("riskChangeFilter").onchange = renderRiskChanges;
$("riskBaselineOpen").onclick = async () => {
  try {
    const origin = snapshotReturn || lastResult;
    renderResult(await api("/api/audits/" + encodeURIComponent(currentRiskChanges.baseline.id)));
    snapshotReturn = origin;
    $("snapshotBack").hidden = false;
  } catch(e) { showError(e.message); }
};
$("snapshotBack").onclick = () => { if (snapshotReturn) renderResult(snapshotReturn); };

let orgOverview = null, orgOverviewRequest = 0, orgSelectedClients = new Set();
// Use a dedicated entry, leaving the existing single-client workspace intact.
const orgReportNav = el("button","org-report-nav"); orgReportNav.dataset.panel = "orgReportPanel";
orgReportNav.setAttribute("aria-label","机构总览"); orgReportNav.title = "机构总览";
const orgReportIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
orgReportIcon.setAttribute("class", "nav-icon");
orgReportIcon.setAttribute("viewBox", "0 0 24 24");
orgReportIcon.setAttribute("aria-hidden", "true");
orgReportIcon.setAttribute("focusable", "false");
const orgReportIconPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
orgReportIconPath.setAttribute("fill-rule", "evenodd");
orgReportIconPath.setAttribute("d", "M6 2h12a1 1 0 0 1 1 1v19h-5v-5h-4v5H5V3a1 1 0 0 1 1-1Zm1 3v3h3V5Zm7 0v3h3V5ZM7 11v3h3v-3Zm7 0v3h3v-3Z");
orgReportIcon.append(orgReportIconPath);
orgReportNav.append(orgReportIcon,el("span","nav-label","机构总览"));
orgReportNav.style.display = "none";
document.querySelector('[data-panel="historyPanel"]').after(orgReportNav);
orgReportNav.onclick = () => switchPanel("orgReportPanel");
function orgSummary(data) {
  const t = data.totals;
  return `所选范围 ${t.clients} 家；有审计 ${t.audited} 家，无匹配审计 ${t.no_audit} 家；有高等级命中 ${t.high} 家、中等级命中 ${t.medium} 家、低等级命中 ${t.low} 家；全部检查通过 ${t.clear} 家，未命中但仍有检查未执行 ${t.incomplete} 家；命中 ${t.hit} 条 / 通过 ${t.pass} 条 / 未执行 ${t.skipped} 条。`;
}
function orgWarnings(data) {
  const notes = [];
  if (data.mixed_periods) notes.push("客户业务期间不同，仅汇总线索，不作同期财务比较。");
  if (data.mixed_rule_sets) notes.push("规则集合或定义不同，按冻结口径分别统计，不比较命中率。");
  const e=data.excluded_audits;
  notes.push(`机构历史未纳入候选：未关联当前客户档案 ${e.unlinked} 份、主体不匹配 ${e.identity_mismatch} 份、无法识别期间 ${e.invalid_period} 份。`);
  return notes.join(" ");
}
function updateOrgSelection() {
  $("orgReportStatus").textContent = `已选择 ${orgSelectedClients.size} 家客户；生成时再次读取当前快照，以生成后的清单为准。`;
  $("orgReportCreate").disabled = !orgSelectedClients.size;
  if (!orgOverview) return;
  const rows=orgOverview.rows.filter(row=>orgSelectedClients.has(row.client_id)),totals=Object.fromEntries(Object.keys(orgOverview.totals).map(key=>[key,0]));
  totals.clients=rows.length;
  for(const row of rows) { totals[row.state]++;if(row.audit_id)totals.audited++;for(const key of ["hit","pass","skipped"])totals[key]+=row.counts[key]; }
  const selected={...orgOverview,rows,totals,
    mixed_periods:new Set(rows.filter(row=>row.audit_id).map(row=>row.period_start+"/"+row.period_end)).size>1,
    mixed_rule_sets:new Set(rows.filter(row=>row.audit_id).map(row=>JSON.stringify([...row.rules].sort((a,b)=>a.id.localeCompare(b.id))))).size>1};
  $("orgReportSummary").textContent=orgSummary(selected);$("orgReportWarnings").textContent=orgWarnings(selected);
}
async function loadOrgOverview() {
  const request=++orgOverviewRequest;
  orgOverview=null; orgSelectedClients.clear(); $("orgReportClients").textContent="";
  $("orgReportCreate").disabled=true; $("orgReportStatus").textContent="正在读取机构总览……";
  try {
    const data=await api("/api/org/overview?period="+encodeURIComponent($("orgReportPeriod").value));
    if (request!==orgOverviewRequest) return;
    orgOverview=data; orgSelectedClients=new Set(data.rows.map(row=>row.client_id));
    const select=$("orgReportPeriod"), current=select.value; select.replaceChildren();
    const latest=el("option",null,"各客户最新业务期间"); latest.value=""; select.append(latest);
    for(const p of data.available_periods) { const option=el("option",null,p.label); option.value=p.value; select.append(option); }
    if(current && !data.available_periods.some(p=>p.value===current)) { const option=el("option",null,current);option.value=current;select.append(option); }
    select.value=current;
    $("orgReportSummary").textContent=orgSummary(data); $("orgReportWarnings").textContent=orgWarnings(data);
    for(const row of data.rows) {
      const card=el("div","data-item"), label=el("label"), check=el("input"); check.type="checkbox"; check.checked=true;
      check.onchange=()=>{ if(check.checked) orgSelectedClients.add(row.client_id);else orgSelectedClients.delete(row.client_id);updateOrgSelection(); };
      label.append(check,document.createTextNode(` ${row.name} · ${row.taxpayer_id} · ${row.period || "无匹配审计"} · ${row.state_label}`));
      const text=el("div"); text.append(label,el("p","muted",`负责会计 ${row.accountant} · 命中 ${row.counts.hit} / 通过 ${row.counts.pass} / 未执行 ${row.counts.skipped}`)); card.append(text);
      if(row.audit_id) { const open=el("button",null,"查看客户证据");open.onclick=async()=>{try{renderResult(await api("/api/audits/"+encodeURIComponent(row.audit_id)));switchPanel("auditPanel");}catch(e){showError(e.message);}};card.append(open); }
      $("orgReportClients").append(card);
    }
    if(!data.rows.length) $("orgReportClients").textContent="暂无客户档案，请在管理中建立客户。";
    updateOrgSelection(); await loadOrgReports();
  } catch(e) { if(request===orgOverviewRequest) { $("orgReportSummary").textContent="";$("orgReportWarnings").textContent="";$("orgReportStatus").textContent="读取失败："+e.message; } }
}
async function loadOrgReports() {
  const rows=await api("/api/org/reports"), box=$("orgReportSaved"); box.textContent="";
  for(const row of rows) { const card=el("div","data-item"), open=el("button",null,"查看汇总快照");
    card.append(el("div",null,`TP-ORG-${row.id.slice(0,12).toUpperCase()} · ${row.created_at}`),open);
    open.onclick=async()=>{try{showOrgReport(await api("/api/org/reports/"+encodeURIComponent(row.id)));}catch(e){showError(e.message);}};box.append(card);
  }
  if(!rows.length) box.textContent="尚未生成机构报告。";
}
function showOrgReport(record) {
  const box=$("orgReportDetail"), data=record.snapshot; box.hidden=false; box.textContent="";
  box.append(el("h3",null,data.report_no+" · "+data.branding.display_name),el("p",null,orgSummary(data)),el("p","muted",orgWarnings(data)));
  const hash=el("p","muted",`快照 SHA-256 ${record.snapshot_sha256} · HTML SHA-256 ${record.html_sha256}`); hash.style.overflowWrap="anywhere";box.append(hash);
  const html=el("a",null,"打开机构报告 HTML");html.href="/api/org/reports/"+encodeURIComponent(record.id)+"/html";html.target="_blank";html.rel="noopener";
  const pdf=el("button","btn pdf","导出机构 PDF");
  pdf.onclick=async()=>{pdf.disabled=true;pdf.textContent="正在导出……";try{
    const response=await fetch("/api/org/reports/"+encodeURIComponent(record.id)+"/pdf");
    if(!response.ok) { const error=await response.json();throw new Error(error.detail || "导出失败"); }
    const url=URL.createObjectURL(await response.blob()), download=el("a");download.href=url;download.download=data.report_no+".pdf";download.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }catch(e){showError(e.message);}finally{pdf.disabled=false;pdf.textContent="导出机构 PDF";}};
  const actions=el("div","row");actions.append(html,pdf,htmlOriginalButton(html.href,data.report_no+".html"));box.append(actions);
  appendProtectionAction(box,data.protection);
  for(const row of data.rows) box.append(el("p",null,`${row.name} · ${row.period || "无匹配审计"} · ${row.state_label} · ${row.audit_id || "无审计编号"}`));
}
$("orgReportPeriod").onchange=loadOrgOverview;
$("orgReportRefresh").onclick=loadOrgOverview;
$("orgReportAll").onclick=()=>{if(orgOverview){orgSelectedClients=new Set(orgOverview.rows.map(row=>row.client_id));$("orgReportClients").querySelectorAll('input[type="checkbox"]').forEach(input=>input.checked=true);updateOrgSelection();}};
$("orgReportNone").onclick=()=>{orgSelectedClients.clear();$("orgReportClients").querySelectorAll('input[type="checkbox"]').forEach(input=>input.checked=false);updateOrgSelection();};
$("orgReportCreate").onclick=async()=>{const button=$("orgReportCreate");button.disabled=true;try{
  const result=await api("/api/org/reports",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({period:$("orgReportPeriod").value,client_ids:[...orgSelectedClients]})});
  showOrgReport(result);await loadOrgReports();$("orgReportStatus").textContent="已生成并冻结 "+result.snapshot.report_no+"，共 "+result.snapshot.totals.clients+" 家客户。";
}catch(e){showError(e.message);}finally{button.disabled=!orgSelectedClients.size;}};

let historyPage = 1, historyRequest = 0;
let verificationRequest=0;
function htmlOriginalButton(url,filename) {
  const button=el("button","btn re","下载 HTML 原件");
  button.onclick=async()=>{button.disabled=true;try{
    const response=await fetch(url);
    if(!response.ok){const error=await response.json();throw new Error(error.detail || "原件下载失败");}
    const objectUrl=URL.createObjectURL(await response.blob()),download=el("a");
    download.href=objectUrl;download.download=filename;download.click();setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
  }catch(e){showError(e.message);}finally{button.disabled=false;}};
  return button;
}
function appendProtectionAction(box,protection) {
  if(!protection) {box.append(el("p","muted","旧归档：未登记 D08 水印/追溯标识，不改写原文件。"));return;}
  const label=el("p","muted","追溯标识 "+protection.id);label.style.overflowWrap="anywhere";
  const open=el("button","btn re","核验报告原件");open.onclick=()=>{
    invalidateVerification();$("verificationId").value=protection.id;$("verificationFile").value="";
    switchPanel("historyPanel");$("reportVerification").scrollIntoView({block:"start"});lookupVerification();
  };box.append(label,open);
}
function invalidateVerification() {
  verificationRequest++;$("verificationStatus").textContent="";$("verificationDetails").textContent="";
  $("verificationLookup").disabled=$("verificationCheck").disabled=false;
}
function verificationDetails(data) {
  $("verificationDetails").textContent=`${data.report_no} · ${data.kind==="audit"?"审计 v"+data.version:"机构报告"}\n${data.customer_name} · 报告日期 ${data.report_date} · 归档 ${data.archived_at}\n标识 ${data.id}\nHTML SHA-256 ${data.html_sha256}\nPDF SHA-256 ${data.pdf_sha256 || "未导出存档；不能核验 PDF"}\n${data.notice}`;
}
async function performVerification(checkFile) {
  const request=++verificationRequest,id=$("verificationId").value.trim(),format=$("verificationFormat").value,file=$("verificationFile").files[0];
  $("verificationLookup").disabled=$("verificationCheck").disabled=true;
  $("verificationDetails").textContent="";$("verificationStatus").textContent=checkFile?"正在本机计算文件指纹……":"正在读取登记……";
  try {
    if(!/^TPV-[0-9a-f]{40}$/.test(id))throw new Error("请输入完整的 TPV 追溯标识；TPL 本地标识没有服务器登记。");
    let options;
    if(checkFile) {
      if(!file)throw new Error("请先选择待核验文件。");
      if(file.size>64*1024*1024)throw new Error("本机核验限制 64MB，请选择较小报告文件。");
      if(!crypto.subtle)throw new Error("文件核验需要 HTTPS 或 localhost 安全上下文。");
      const hash=await crypto.subtle.digest("SHA-256",await file.arrayBuffer());
      const sha256=Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,"0")).join("");
      if(request!==verificationRequest)return;
      options={method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({format,sha256})};
    }
    const data=await api("/api/report-verification/"+encodeURIComponent(id),options);
    if(request!==verificationRequest)return;
    verificationDetails(data);
    $("verificationStatus").textContent=checkFile?(data.sha256_matches?"核验通过：所选文件与归档原件逐字节一致。":"核验不通过：所选文件与归档原件不一致，可能改动或选错版本。"):
      "此标识已登记；尚未核验任何文件，请选择文件后核验。";
  } catch(e) {if(request===verificationRequest)$("verificationStatus").textContent="核验失败："+e.message;}
  finally {if(request===verificationRequest)$("verificationLookup").disabled=$("verificationCheck").disabled=false;}
}
function lookupVerification(){return performVerification(false);}
$("verificationLookup").onclick=lookupVerification;$("verificationCheck").onclick=()=>performVerification(true);
$("verificationId").oninput=invalidateVerification;$("verificationFormat").onchange=invalidateVerification;$("verificationFile").onchange=invalidateVerification;
async function loadHistory(page = 1) {
  void coreInputs.renderArchived();
  const request = ++historyRequest;
  $("historyPrev").disabled = $("historyNext").disabled = true;
  $("historyStatus").textContent = "正在检索……";
  try {
    const query = new URLSearchParams({q:$("historyQuery").value, period:$("historyPeriod").value,
      risk:$("historyRisk").value, page:String(page), page_size:"20"});
    if ($("historyDateFrom").value) query.set("date_from",$("historyDateFrom").value);
    if ($("historyDateTo").value) query.set("date_to",$("historyDateTo").value);
    const result = await api("/api/archive?" + query);
    if (request !== historyRequest) return;
    historyPage = result.page;
    const rows = result.items, box = $("historyList"); box.textContent = "";
    $("historyStatus").textContent = `共 ${result.total} 条，第 ${historyPage} / ${Math.max(1,Math.ceil(result.total/result.page_size))} 页`;
    $("historyPrev").disabled = historyPage <= 1;
    $("historyNext").disabled = historyPage * result.page_size >= result.total;
    if (!rows.length) box.appendChild(el("div","data-item","暂无历史审计。"));
    for (const row of rows) {
      const item = el("div","sheet"); const txt = el("div");
      txt.appendChild(el("div",null,row.company_name + " · " + row.period));
      txt.appendChild(el("div","muted",row.audited_at + "　命中 " + row.summary.hit + " / 通过 " + row.summary.pass + " / 未执行 " + row.summary.skipped));
      const btn = el("button","btn re","打开证据快照"); btn.addEventListener("click", async () => { try { renderResult(await api("/api/audits/" + encodeURIComponent(row.id))); switchPanel("auditPanel"); } catch(e) { showError(e.message); } });
      const versions = el("button","btn re",`报告版本（${row.report_versions}）`), versionBox = el("div"); versionBox.hidden = true;
      versions.onclick = async () => { versionBox.hidden = !versionBox.hidden; if (!versionBox.hidden) await loadReportVersions(row.id, versionBox, versions); };
      const actions = el("div","row"); actions.append(btn,versions);
      item.append(txt,actions,versionBox); box.appendChild(item);
    }
  } catch (err) { if (request === historyRequest) { $("historyList").textContent = ""; $("historyStatus").textContent = "检索失败：" + err.message; } }
}
async function loadReportVersions(id, box, toggle) {
  box.textContent = "正在读取报告版本……";
  try {
    const rows = await api("/api/audits/" + encodeURIComponent(id) + "/report-versions");
    if (!box.isConnected) return;
    box.textContent = ""; toggle.textContent = `报告版本（${rows.length}）`;
    const publish = el("button","btn re","归档当前报告");
    publish.onclick = async () => { publish.disabled = true; try { await api("/api/audits/" + encodeURIComponent(id) + "/report-versions",{method:"POST"}); await loadReportVersions(id,box,toggle); } catch(e) { publish.disabled = false; showError(e.message); } };
    box.append(publish,el("p","muted","相同内容复用已有版本；归档不重跑规则。校验和用于完整性核对，不代表第三方认证或防伪签名。"));
    if (!rows.length) box.append(el("p","muted","尚无归档报告；可归档当前报告，记录实际生成时间。"));
    for (const row of rows) {
      const card = el("div","sheet"), m = row.manifest;
      card.append(el("h3",null,`v${row.version} · ${m.report_title}`),el("p","muted",`归档 ${row.created_at} · 操作人 ${row.created_by} · ${m.org_name}`),
        el("p","muted",`报告编号 ${m.report_no} · 规则 ${m.rules.map(r=>r.id+" v"+r.version).join("、")}`));
      const hashes = el("p","muted",`HTML SHA-256 ${row.html_sha256}\n模板 SHA-256 ${m.template_sha256}\n审计 SHA-256 ${m.audit_sha256}\nPDF ${row.pdf_sha256 || "尚未导出；首次导出后冻结"}${m.narrative_model ? "\n叙述模型 "+m.narrative_model : "\n无 AI 叙述"}`);
      hashes.style.overflowWrap = "anywhere"; hashes.style.whiteSpace = "pre-line"; card.append(hashes);
      const html = el("a",null,"查看此版 HTML"); html.href = "/api/report/" + encodeURIComponent(id) + "/html?version=" + row.version; html.target = "_blank"; html.rel = "noopener";
      const pdf = el("a",null,"下载此版 PDF"); pdf.href = "/api/report/" + encodeURIComponent(id) + "?version=" + row.version + "&confirm=true";
      pdf.onclick = async (event) => {
        event.preventDefault();
        if (pdf.dataset.busy === "1") return;
        if (currentUser.role === "accountant" && !window.confirm("确认已核对报告内容，并明确报告用途后导出此版本？")) return;
        pdf.dataset.busy = "1"; pdf.textContent = "正在导出……";
        try {
          const response = await fetch(pdf.href);
          if (!response.ok) { const error = await response.json(); throw new Error(error.detail || error.error || "导出失败"); }
          const url = URL.createObjectURL(await response.blob()), download = el("a");
          const name = response.headers.get("Content-Disposition")?.match(/filename\*=UTF-8''(.+)$/);
          download.href = url; download.download = name ? decodeURIComponent(name[1]) : `审计报告-v${row.version}.pdf`;
          download.click(); setTimeout(()=>URL.revokeObjectURL(url),60000);
          if (box.isConnected) await loadReportVersions(id,box,toggle);
        } catch(e) { showError(e.message); }
        finally { pdf.dataset.busy = "0"; pdf.textContent = "下载此版 PDF"; }
      };
      const links = el("div","row"); links.append(html,pdf,htmlOriginalButton(html.href,`${m.report_no}-v${row.version}.html`)); card.append(links); box.append(card);
      appendProtectionAction(card,m.protection);
    }
  } catch(e) { if (box.isConnected) box.textContent = "版本读取失败：" + e.message; }
}
$("historySearch").onsubmit = event => { event.preventDefault(); loadHistory(1); };
$("historyReset").onclick = () => { $("historySearch").reset(); loadHistory(1); };
$("historyPrev").onclick = () => loadHistory(historyPage-1);
$("historyNext").onclick = () => loadHistory(historyPage+1);

let exerciseRuleRequest=0,exerciseRulesYear=null;
async function refreshExerciseRules() {
  const request=++exerciseRuleRequest,year=Number($("exerciseYear").value),select=$("exerciseTargetRule");
  select.textContent="";exerciseRulesYear=null;$("btnGenerateExercise").disabled=true;
  try {
    if(!Number.isInteger(year)||year<2000||year>2099)throw new Error("教学年度须为 2000–2099 的整数。");
    const data=await api("/api/exercises/rules?year="+year);
    if(request!==exerciseRuleRequest)return;
    for(const rule of data.rules){const option=el("option",null,rule.id+" "+rule.name+" · v"+rule.version);option.value=rule.id;option.dataset.version=rule.version;select.append(option);}
    exerciseRulesYear=year;$("btnGenerateExercise").disabled=!data.rules.length;
    $("exerciseGenerationStatus").textContent=data.rules.length?"本期规则已加载；生成后请核对完整答案再发布。":"本期没有已启用规则。";
  }catch(e){if(request===exerciseRuleRequest)$("exerciseGenerationStatus").textContent=e.message;}
}
function exerciseDownloadButton(url) {
  const button=el("button",null,"下载仿真材料（不含答案）");button.type="button";
  button.onclick=async()=>{
    button.disabled=true;
    try {
      const response=await fetch(url,{credentials:"same-origin"});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.detail||"材料下载失败。");}
      const objectUrl=URL.createObjectURL(await response.blob()),link=el("a");
      link.href=objectUrl;link.download="exercise-material.xlsx";link.click();
      setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
    }catch(e){showError(e.message);}finally{button.disabled=false;}
  };
  return button;
}
function showGeneratedExercise(data) {
  const box=$("exerciseGenerationResult"),meta=data.metadata;box.hidden=false;box.textContent="";
  box.append(el("p",null,data.company_name+" · "+meta.year+" 年 · 目标 "+meta.requested_rule_id+" · v"+meta.rule_versions[meta.requested_rule_id]+" · 偏离幅度："+({near:"接近阈值",normal:"普通偏离",obvious:"明显偏离"}[meta.level]||"未记录")));
  box.append(el("p","muted",meta.notice));
  box.append(exerciseDownloadButton("/api/exercises/"+encodeURIComponent(data.audit_id)+"/materials"));
  box.append(el("p",null,"完整标准答案："+meta.standard_answer.join("、")));
  const extra=meta.standard_answer.filter(id=>id!==meta.requested_rule_id);if(extra.length)box.append(el("p","muted","关联命中也须评分："+extra.join("、")+"。发布前请复核全部答案。"));
  const details=el("details"),summary=el("summary",null,"核对全部规则答案与计算过程");details.append(summary);box.append(details);
  for(const answer of data.answers){const item=el("div","data-item");item.append(el("p",null,answer.rule_id+" "+answer.name+" · "+({hit:"命中",pass:"通过",skipped:"未执行"}[answer.status])));item.append(el("div","calc-box",answer.calculation||answer.skip_reason));details.append(item);}
}
$("btnRefreshExerciseRules").onclick=refreshExerciseRules;
$("exerciseYear").oninput=()=>{exerciseRuleRequest++;exerciseRulesYear=null;$("exerciseTargetRule").textContent="";$("btnGenerateExercise").disabled=true;$("exerciseGenerationStatus").textContent="年度已变化，请刷新本期规则。";};
$("btnGenerateExercise").onclick=async()=>{
  const button=$("btnGenerateExercise"),option=$("exerciseTargetRule").selectedOptions[0];button.disabled=true;
  $("exerciseGenerationResult").hidden=true;$("exerciseGenerationStatus").textContent="正在生成并由审计引擎复核……";
  try{
    const year=Number($("exerciseYear").value);
    if(!option||exerciseRulesYear!==year)throw new Error("请先刷新本期规则。");
    const data=await api("/api/exercises",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rule_id:option.value,expected_version:option.dataset.version,year,level:$("exerciseLevel").value})});
    await loadAssignments();$("assignmentAudit").value=data.audit_id;showGeneratedExercise(data);
    $("exerciseGenerationStatus").textContent="案例已保存并选入发布栏；尚未发布给学生。";
  }catch(e){$("exerciseGenerationStatus").textContent="生成失败："+e.message;}
  finally{button.disabled=exerciseRulesYear!==Number($("exerciseYear").value)||!$("exerciseTargetRule").options.length;}
};
$("btnExerciseRecord").onclick=async()=>{try{showGeneratedExercise(await api("/api/exercises/"+encodeURIComponent($("assignmentAudit").value)));}catch(e){$("exerciseGenerationStatus").textContent=e.message;}};

let classroomUI = null;
function updateExerciseDeadline(a) {
  clearTimeout(exerciseDeadlineTimer);
  const expired=a.deadline_passed || (a.deadline_at && Date.now()>=Date.parse(a.deadline_at));
  $("exerciseDeadlineStatus").textContent=(expired?"已截止，仅可查看材料与已有成绩":"截止："+classroomUI.dueText(a.deadline_at))+"；提交以服务器校验为准。";
  $("btnSubmitExercise").disabled=!a.can_submit || !!expired || !!window.tpSubmissionUI?.blocksSubmit?.(a.id);
  if(a.deadline_at && !expired)exerciseDeadlineTimer=setTimeout(()=>{if(openAssignmentId===a.id)updateExerciseDeadline(a);},Math.min(60000,Math.max(100,Date.parse(a.deadline_at)-Date.now())));
}
async function loadAssignments({throwOnError=false}={}) {
  const request=++trainingLoadRequest,uid=currentUser?.id,isTeacher=currentUser?.role==="teacher";
  classroomUI.invalidatePending();
  try {
    const [rows,classes,papers,students,audits,submissions,courses]=await Promise.all([
      api("/api/assignments"),api("/api/classes"),api("/api/papers"),
      isTeacher?api("/api/classes/students"):[],isTeacher?api(currentUser.org_type==="university"?"/api/teaching/cases":"/api/audits"):[],isTeacher?api("/api/submissions"):[],isTeacher&&currentUser.org_type==="university"?api("/api/teaching/courses"):[],
      window.tpTrainingTasks?.refresh({throwOnError})
    ]);
    if(request!==trainingLoadRequest||uid!==currentUser?.id)return;
    classroomUI.render({classes,papers,students,audits});
    for(const [id, classId]of [["assignmentCourse","assignmentClass"],["paperCourse","paperClass"]]){
      const select=$(id);if(!select)continue;const previous=select.value;select.replaceChildren(new Option("独立作业 / 不关联课程",""));
      for(const course of courses)select.append(new Option(course.name,course.id));select.value=courses.some(c=>c.id===previous)?previous:"";
      select.onchange=()=>{const course=courses.find(c=>c.id===select.value),classSelect=$(classId);for(const option of classSelect.options)option.disabled=!!course&&!course.class_ids.includes(option.value);if(course&&!course.class_ids.includes(classSelect.value))classSelect.value="";};select.onchange();
    }

    if(openAssignmentId){const current=rows.find(a=>a.id===openAssignmentId);if(current)updateExerciseDeadline(current);else{openAssignmentId=null;exerciseOpenRequest++;sandboxFeedbackRequest++;clearTimeout(exerciseDeadlineTimer);$("exerciseBox").style.display="none";$("exerciseData").textContent="";$("scoreResult").textContent="";}}
    const box = $("assignmentList"); box.textContent = "";
    const singles=rows.filter(row=>!row.paper_id);
    for (const row of singles) {
      const item = el("div","training-card"); item.appendChild(el("h3",null,row.title));
      if(row.course_name)item.append(el("p","muted","课程："+row.course_name));
      item.append(el("p","muted",classroomUI.status(row)+" · 截止："+classroomUI.dueText(row.deadline_at)));
      const btn = el("button",null,currentUser.role === "student" ? "开始 / 查看评分" : "查看");
      btn.addEventListener("click",() => openExercise(row.id)); item.appendChild(btn);
      const controls=classroomUI.singleControls(row);if(controls)item.append(controls);box.appendChild(item);
    }
    if (!singles.length) box.appendChild(el("div","data-item","暂无单案例作业；组卷题目请从上方试卷进入。"));
    $("reviewBox").style.display=isTeacher?"block":"none";const sb=$("submissionList");sb.textContent="";
    if (isTeacher) {
      const select = $("assignmentAudit"), previous=select.value; select.textContent = "";
      for (const row of audits) { const o = el("option",null,row.company_name + " · " + row.period); o.value=row.id; select.appendChild(o); }
      if(audits.some(a=>a.id===previous))select.value=previous;
      for(const s of submissions){const item=el("div","data-item");const shown=s.adjusted_score===null?s.score:s.adjusted_score;item.appendChild(el("span",null,s.display_name+" · "+s.title+" · "+shown+" 分"));const extra=window.tpSubmissionUI?.reviewActions?.(s);if(extra)item.appendChild(extra);
        const b=el("button",null,"复核调整");b.addEventListener("click",async()=>{try{const value=window.prompt("调整后分数（0-100）",String(shown));if(value===null)return;const feedback=window.prompt("教师评语",s.feedback||"")||"";await api("/api/submissions/"+s.id+"/review",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({adjusted_score:Number(value),feedback})});await loadAssignments();}catch(error){showError(error.message);}});item.appendChild(b);sb.appendChild(item);}if(!submissions.length)sb.appendChild(el("div","data-item","暂无学生提交。"));
      if(exerciseRulesYear===null)await refreshExerciseRules();
    }
  } catch (err) { if(request===trainingLoadRequest&&uid===currentUser?.id){showError(err.message);if(throwOnError)throw err;} }
}

$("btnAssignment").addEventListener("click", async () => {
  const button=$("btnAssignment");if(button.disabled)return;button.disabled=true;
  try {
    await api("/api/assignments", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:$("assignmentTitle").value,audit_id:$("assignmentAudit").value,false_positive_penalty:Number($("assignmentPenalty").value),published:$("assignmentPublished").checked,class_id:$("assignmentClass").value||null,course_id:$("assignmentCourse")?.value||null,deadline_at:classroomUI.deadlineInput($("assignmentDeadline"))})});
    await loadAssignments();
  } catch (err) { showError(err.message); } finally {button.disabled=false;}
});

function paintSandboxFeedback(result) {
  const box=$("sandboxMessages");box.textContent="";
  for(const message of result.messages)box.append(el("p",message.level==="warning"?"calc-box":"muted",message.text));
  if(result.calculation) {
    const c=result.calculation;
    box.append(el("p","calc-box",c.left+"（"+c.left_value+"）"+(c.operation==="ratio"?" ÷ ":" − ")+c.right+"（"+c.right_value+"） = "+c.value+" "+c.unit+(c.percent!==undefined?"（"+c.percent+"%）":"")));
  }
  if(result.guide) {
    const guide=$("sandboxGuide");guide.textContent="";
    guide.append(el("h3",null,result.guide.rule_id+" 证据定位"),el("p","muted",result.guide.method));
    const located=sandboxEvidence.get(result.guide.rule_id)||new Set();
    for(const input of result.guide.inputs) {
      const label=el("label","sandbox-evidence"),check=el("input");check.type="checkbox";check.checked=located.has(input.name);check.disabled=!input.present;
      check.onchange=()=>{if(check.checked)located.add(input.name);else located.delete(input.name);sandboxEvidence.set(result.guide.rule_id,located);requestSandboxFeedback({action:"mark_risk",rule_id:result.guide.rule_id,evidence_metrics:[...located]});};
      label.append(check,document.createTextNode(" 已核对 "+input.name+"（"+input.unit+"）"),el("p","muted",input.source||"当前案例缺少此项原始指标"),el("p","muted",input.detail||input.description));guide.append(label);
    }
  }
}
async function requestSandboxFeedback(body) {
  const request=++sandboxFeedbackRequest,assignment=openAssignmentId;
  if(!assignment)return;
  $("sandboxMessages").textContent="正在核对操作与证据口径……";
  try {
    const result=await api("/api/assignments/"+encodeURIComponent(assignment)+"/feedback",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(request===sandboxFeedbackRequest&&assignment===openAssignmentId)paintSandboxFeedback(result);
  }catch(e){if(request===sandboxFeedbackRequest&&assignment===openAssignmentId)$("sandboxMessages").textContent=e.message;}
}
function buildSandboxTools(a) {
  $("sandboxTools")?.remove();
  const tools=el("section");tools.id="sandboxTools";
  tools.append(el("h3",null,"即时教学反馈"),el("p","muted","勾选风险后定位证据，或用原始指标试算。提示不判定答案正确性；定位和试算仅在本页使用，不改变材料或正式评分。"));
  const row=el("div","sandbox-controls");
  for(const [id,title] of [["sandboxLeft","左侧指标"],["sandboxRight","右侧指标"]]) {
    const field=el("label","field"),select=el("select");select.id=id;select.setAttribute("aria-label",title);select.append(el("option",null,"请选择"));select.options[0].value="";
    field.append(el("span",null,title),select);row.append(field);
    for(const metric of [...(a.metrics||[])].sort((x,y)=>x.name.localeCompare(y.name,"zh-CN"))){const o=el("option",null,metric.name);o.value=metric.name;select.append(o);}
  }
  tools.append(row);
  const options=el("div","sandbox-controls");
  for(const [id,title,values] of [["sandboxOperation","试算方式",[["difference","左侧 − 右侧"],["ratio","左侧 ÷ 右侧"]]],["sandboxPeriodMode","期间口径",[["same_period","同期间核对"],["year_on_year","同比：本期 / 上年同期"]]]]) {
    const field=el("label","field"),select=el("select");select.id=id;select.setAttribute("aria-label",title);
    for(const [value,text] of values){const o=el("option",null,text);o.value=value;select.append(o);}field.append(el("span",null,title),select);options.append(field);
  }
  const calc=el("button","btn pdf","核对并试算");calc.type="button";
  const calculate=()=>requestSandboxFeedback({action:"calculate",left:$("sandboxLeft").value,right:$("sandboxRight").value,operation:$("sandboxOperation").value,period_mode:$("sandboxPeriodMode").value});
  calc.onclick=calculate;options.append(calc);tools.append(options);
  for(const id of ["sandboxLeft","sandboxRight","sandboxOperation","sandboxPeriodMode"])tools.querySelector("#"+id).onchange=calculate;
  const messages=el("div");messages.id="sandboxMessages";messages.setAttribute("role","status");messages.setAttribute("aria-live","polite");messages.textContent="选择风险选项或原始指标后，在这里查看操作提示。";
  const guide=el("div");guide.id="sandboxGuide";tools.append(messages,guide);
  $("exerciseRules").after(tools);
}
async function openExercise(id) {
  const request=++exerciseOpenRequest,loadVersion=trainingLoadRequest;sandboxFeedbackRequest++;clearTimeout(exerciseDeadlineTimer);openAssignmentId=null;$("exerciseBox").style.display="none";
  try {
    const a = await api("/api/assignments/" + id);
    if(request!==exerciseOpenRequest||loadVersion!==trainingLoadRequest)return;
    openAssignmentId=id;sandboxRuleId=null;sandboxEvidence=new Map();$("exerciseBox").style.display="block";$("exerciseBox").dataset.assignment=id;
    $("exerciseTitle").textContent=a.title; $("exerciseCompany").textContent=a.company.name + " · " + a.company.period;
    const data=$("exerciseData"); data.textContent="";
    if(a.generated_material_available)data.append(exerciseDownloadButton("/api/assignments/"+encodeURIComponent(id)+"/materials"));
    data.appendChild(el("div","ev-title","科目余额表"));
    const at=el("table","ev"), ah=el("tr"); for(const h of ["科目编码","科目名称","期初","借方","贷方","期末"])ah.appendChild(el("th",null,h));const ahr=el("thead");ahr.appendChild(ah);at.appendChild(ahr);const ab=el("tbody");for(const row of a.accounts){const tr=el("tr");for(const v of [row.code,row.name,row.opening,row.debit,row.credit,row.closing])tr.appendChild(el("td",null,v));ab.appendChild(tr);}at.appendChild(ab);data.appendChild(at);
    data.appendChild(el("div","ev-title","增值税申报")); const dt=el("table","ev"),db=el("tbody");for(const [k,v] of Object.entries(a.declarations)){const tr=el("tr");tr.append(el("td",null,k),el("td","v num",v));db.appendChild(tr);}dt.appendChild(db);data.appendChild(dt);
    data.append(el("div","ev-title","报表与补充底稿指标"),el("p","muted","金额单位元，比例用小数，人数为整数；历史实际期间见口径说明。仿真参考值不代表真实行业标准。"));
    const mt=el("table","ev"),mh=el("tr");for(const h of ["指标","数值","来源 / 口径"])mh.append(el("th",null,h));const mhead=el("thead");mhead.append(mh);mt.append(mhead);const mb=el("tbody");
    for(const metric of a.metrics||[]){const row=el("tr");row.append(el("td",null,metric.name),el("td","v num",metric.value),el("td",null,metric.source+"；"+metric.detail));mb.append(row);}mt.append(mb);data.append(mt);
    const box=$("exerciseRules"); box.textContent="";
    for (const r of a.rules) { const label=el("label"); const input=el("input"); input.type="checkbox"; input.value=r.id;
      input.onchange=()=>{if(input.checked){sandboxRuleId=r.id;requestSandboxFeedback({action:"mark_risk",rule_id:r.id,evidence_metrics:[...(sandboxEvidence.get(r.id)||[])]});}else if(sandboxRuleId===r.id){sandboxFeedbackRequest++;sandboxRuleId=null;$("sandboxGuide").textContent="";$("sandboxMessages").textContent="已取消该风险标注；正式答案以提交时勾选的项目为准。";}};
      label.append(input,document.createTextNode(" "+r.id+" "+r.name)); box.appendChild(label); }
    buildSandboxTools(a);
    $("btnSubmitExercise").style.display=currentUser.role === "student" ? "inline-block" : "none";
    updateExerciseDeadline(a);
    $("scoreResult").textContent="";
    if(a.submission){
      if(a.submission.result_released===false) showResultNote(a.submission.result_note);
      else{
        showScore(a.submission.details);
        if(a.submission.adjusted_score!==null&&a.submission.adjusted_score!==undefined)$("scoreResult").append(el("p","muted","教师复核："+a.submission.adjusted_score+" 分 · "+(a.submission.feedback||"")));
      }
    }
    $("exerciseBox").scrollIntoView({block:"start"});
    window.dispatchEvent(new CustomEvent("tp:exercise-open",{detail:{assignment:id}}));
  } catch (err) { if(request===exerciseOpenRequest&&loadVersion===trainingLoadRequest)showError(err.message); }
}

$("btnSubmitExercise").addEventListener("click", async () => {
  const assignment=openAssignmentId,button=$("btnSubmitExercise");if(!assignment||button.disabled)return;button.disabled=true;
  const ids=Array.from($("exerciseRules").querySelectorAll("input:checked")).map((n)=>n.value);
  try { const result=await api("/api/assignments/"+assignment+"/submit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({selected_rule_ids:ids})});if(openAssignmentId===assignment){if(result.result_released===false)showResultNote(result.result_note);else showScore(result);}await loadAssignments(); }
  catch(err){if(openAssignmentId===assignment){showError(err.message);if(err.status===404||err.status===409)await loadAssignments();else button.disabled=false;}}
});

function showResultNote(note){
  const box=$("scoreResult"); box.textContent="";
  box.appendChild(el("div","score","已提交"));
  box.appendChild(el("p","muted",note||"成绩与解析暂未开放。"));
}

function showScore(result) {
  const box=$("scoreResult"); box.textContent=""; box.appendChild(el("div","score",result.score+" 分"));
  box.appendChild(el("div","muted","命中 "+result.correct.length+" 项，漏检 "+result.missed.length+" 项，误报 "+result.false_positives.length+" 项；误报扣 "+result.false_positive_deduction+" 分。"));
  for (const group of [["漏检明细",result.missed],["误报明细",result.false_positives]]) { if (!group[1].length) continue; box.appendChild(el("div","ev-title",group[0])); for (const d of group[1]) box.appendChild(el("div","calc-box",d.rule_id+" "+d.name+"："+d.explanation)); }
}

function showOrgSettings(settings,message="") {
  $("orgDisplayName").value=settings.display_name||"";
  $("orgReportTitle").value=settings.report_title||"";
  $("orgFooterText").value=settings.footer_text||"";
  const preview=$("orgLogoPreview");preview.hidden=!settings.has_logo;
  if(settings.has_logo)preview.src="/api/org/logo?v="+encodeURIComponent(settings.logo_updated_at||Date.now());
  else preview.removeAttribute("src");
  $("btnRemoveOrgLogo").disabled=!settings.has_logo;
  $("orgSettingsStatus").textContent=message||(settings.has_logo?"已配置机构 Logo。":"当前使用无 Logo 的文字抬头。");
}

async function loadOrgSettings(){showOrgSettings(await api("/api/org/settings"));}

function nextRuleVersion(version){const parts=version.split(".").map(Number);if(parts.some(n=>!Number.isInteger(n)))return version+".1";parts[parts.length-1]+=1;return parts.join(".");}
async function loadGraphRules(){
  if($("ruleAdmin").hidden||!$("ruleAdmin").open)return;
  const request=++ruleCatalogRequest,owner=currentUser?.id;
  const current=()=>request===ruleCatalogRequest&&currentUser?.id===owner&&$("knowledgePanel").classList.contains("active")&&$("ruleAdmin").open;
  graphRuleCatalog=[];$("ruleList").replaceChildren();$("ruleCatalogStatus").textContent="正在读取图谱规则…";
  try{
    const rows=await api("/api/knowledge/rule-catalog");if(!current())return;
    const audits=currentUser.role==="platform_admin"?[]:await api("/api/audits");if(!current())return;
    const select=$("ruleTrialAudit"),previous=select.value;select.replaceChildren();
    for(const row of audits){const item=el("option",null,row.company_name+" · "+row.period);item.value=row.id;select.append(item);}
    if(audits.some(row=>row.id===previous))select.value=previous;
    if(!audits.length){const item=el("option",null,"暂无可试跑的历史审计");item.value="";select.append(item);}
    graphRuleCatalog=rows;renderGraphRules();
    if(editingRule)loadRuleVersionHistory(editingRule.id);
  }catch(error){if(current())$("ruleCatalogStatus").textContent="图谱规则读取失败："+error.message;}
}
function renderGraphRules(){
  const query=$("ruleCatalogSearch").value.trim().toLowerCase(),box=$("ruleList");box.replaceChildren();
  const rows=graphRuleCatalog.filter(r=>[r.rule_code,r.name,r.version,r.rule_id].join(" ").toLowerCase().includes(query));
  $("ruleCatalogStatus").textContent=`共 ${graphRuleCatalog.length} 个图谱规则版本，匹配 ${rows.length} 个`+(rows.length>80?"；显示前 80 个，请搜索缩小范围。":"。");
  for(const row of rows.slice(0,80)){
    const item=el("div","data-item"),text=el("div"),actions=el("div","row");
    const status=({candidate:"候选 · 待审核",approved:"已审核",rejected:"已驳回",retired:"已退役"})[row.review_status]||"审核状态待核实";
    text.append(el("div",null,(row.rule_code||row.rule_id)+" "+row.name+" · v"+row.version),
      el("p","muted",row.source==="converted"?"原有规则已转换为图谱计算条件 · 阈值仍为内部筛查参数":"知识图谱政策规则"));
    actions.append(el("span","role-chip",status));
    const view=el("button",null,"查看依据与条件");view.addEventListener("click",()=>viewGraphRule(row));actions.append(view);
    if(row.legacy){
      const edit=el("button",null,currentUser.role==="platform_admin"?"配置参数版本":"参数草稿 / 历史试跑");
      edit.addEventListener("click",()=>openRuleEditor(row.legacy));actions.append(edit);
      const compatibility=el("details"),summary=el("summary",null,"历史审计兼容设置");
      compatibility.append(summary,el("p","muted",(row.legacy.enabled?"历史审计筛查开启。":"历史审计筛查关闭。")+"此开关仅用于旧审计流程，不改变图谱审核状态或企业材料检查，也不重算历史报告。"));
      if(currentUser.role==="platform_admin"){
        const toggle=el("button",null,row.legacy.enabled?"关闭历史审计筛查":"开启历史审计筛查");
        toggle.addEventListener("click",async()=>{
          const owner=currentUser?.id,request=ruleCatalogRequest;toggle.disabled=true;
          try{await api("/api/rules/"+row.legacy.id+"/state",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:!row.legacy.enabled})});
            if(currentUser?.id===owner&&ruleCatalogRequest===request&&$("knowledgePanel").classList.contains("active"))await loadGraphRules();
          }catch(error){if(currentUser?.id===owner&&ruleCatalogRequest===request)showError(error.message);}
          finally{toggle.disabled=false;}
        });compatibility.append(toggle);
      }
      text.append(compatibility);
    }
    item.append(text,actions);box.append(item);
  }
  if(!rows.length)box.append(el("p","muted","没有匹配的图谱规则。"));
}
async function viewGraphRule(row){
  const owner=currentUser?.id,request=ruleCatalogRequest;
  $("graphScope").value="";updateGraphAudits();
  const loading=loadGraph(),ticket=graphRequest;await loading;
  if(currentUser?.id!==owner||request!==ruleCatalogRequest||ticket!==graphRequest||!$("knowledgePanel").classList.contains("active"))return;
  const node=graphData.nodes.find(n=>n.kind==="rule"&&n.record_id===row.rule_id);
  if(!node){$("ruleCatalogStatus").textContent="该图谱版本已变化或未能载入，请刷新规则后重试。";return;}
  for(const id of ["knowledgeCategory","graphStatus","graphRuleLevel","graphRegion"])$(id).value="";
  $("knowledgeSearch").value=row.rule_id;graphSelected=node.id;filterGraph(true);focusGraph();graphTab(false);
  $("knowledgeDetail").scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("btnOpenGraphRules").addEventListener("click",()=>{switchPanel("knowledgePanel");$("ruleAdmin").open=true;$("ruleAdmin").scrollIntoView({behavior:"smooth",block:"start"});});
$("btnReloadRules").addEventListener("click",loadGraphRules);
$("ruleCatalogSearch").addEventListener("input",renderGraphRules);
$("ruleAdmin").addEventListener("toggle",()=>{if($("ruleAdmin").open)loadGraphRules();else ruleCatalogRequest++;});

function setRuleDraft(path,value){let node=ruleDraftLogic;for(let i=0;i<path.length-1;i++)node=node[path[i]];node[path[path.length-1]]=value;}
function ruleFieldName(path){const key=path[path.length-1],names={threshold:"相对偏离阈值",tolerance:"差额容差",min:"区间下限",max:"区间上限",left:"左侧指标 / 基准",right:"右侧指标 / 基准",numerator:"分子指标",denominator:"分母指标",direction:"比较方向"};return names[key]||("参数 "+(Number.isInteger(key)?key+1:key));}

function renderRuleParamFields(){
  const box=$("ruleParamFields");box.textContent="";const metrics=Object.keys(editingRule.inputs);
  function walk(value,path=[]){
    if(Array.isArray(value)){value.forEach((item,index)=>walk(item,path.concat(index)));return;}
    if(value&&typeof value==="object"){for(const [key,item] of Object.entries(value)){if(key!=="type")walk(item,path.concat(key));}return;}
    const key=path[path.length-1],field=el("div","field"),label=el("label",null,ruleFieldName(path));let input;
    if(typeof value==="number"){
      input=el("input");input.type="number";input.step="any";input.value=String(value);input.addEventListener("input",()=>{if(input.value!==""&&Number.isFinite(Number(input.value)))setRuleDraft(path,Number(input.value));});
    }else if(key==="direction"){
      input=el("select");for(const [v,t] of [["absolute","绝对差"],["above","左侧高于右侧"],["below","左侧低于右侧"]]){const option=el("option",null,t);option.value=v;input.append(option);}input.value=value;input.addEventListener("change",()=>setRuleDraft(path,input.value));
    }else{
      input=el("select");for(const metric of metrics){const option=el("option",null,metric);option.value=metric;input.append(option);}input.value=value;input.addEventListener("change",()=>setRuleDraft(path,input.value));
    }
    input.setAttribute("aria-label",ruleFieldName(path));const hint=el("span","rule-param-path",path.join("."));field.append(label,input,hint);box.append(field);
  }
  walk(ruleDraftLogic);
}

function openRuleEditor(rule){
  ruleEditorRequest++;
  editingRule=rule;ruleDraftLogic=JSON.parse(JSON.stringify(rule.logic));$("ruleEditor").hidden=false;
  $("ruleEditorTitle").textContent=rule.id+" "+rule.name;$("ruleEditorMeta").textContent=rule.category+" · "+({high:"高",medium:"中",low:"低"}[rule.severity]||rule.severity)+"风险";
  $("ruleEditorState").textContent="参数配置不代表图谱已审核";$("ruleCurrentVersion").value=rule.version;$("ruleNewVersion").value=nextRuleVersion(rule.version);$("ruleThresholdBasis").value=rule.threshold_basis;
  $("ruleEffectiveFrom").value="";$("ruleEffectiveTo").value="";
  $("ruleMetricHint").textContent="允许引用的指标："+Object.keys(rule.inputs).join("、");$("btnRuleSave").hidden=currentUser.role!=="platform_admin";
  $("btnRuleTrial").hidden=currentUser.role==="platform_admin";$("ruleTrialAudit").closest(".field").hidden=currentUser.role==="platform_admin";
  $("ruleEditorStatus").textContent=currentUser.role==="platform_admin"?"平台仅维护规则，不读取客户材料或试跑客户审计；请依据已核对的变更发布新版本。":"可调整并试跑草稿；正式保存由平台管理员完成。";$("ruleTrialResult").hidden=true;renderRuleParamFields();
  loadRuleVersionHistory(rule.id);
  ruleEditorControls();
  $("ruleEditor").scrollIntoView({behavior:"smooth",block:"nearest"});
}

async function loadRuleVersionHistory(ruleId){
  const request=++ruleHistoryRequest,editor=ruleEditorRequest,owner=currentUser?.id;
  const current=()=>request===ruleHistoryRequest&&ruleEditorCurrent(editor,owner)&&editingRule.id===ruleId;
  const box=$("ruleVersionHistory");box.textContent="";
  if(currentUser.role!=="platform_admin")return;
  try{
    const versions=await api("/api/rules/"+ruleId+"/versions");
    if(!current())return;
    box.append(el("h3",null,"版本历史"));
    if(!versions.length){box.append(el("p","muted","尚无自定义版本。"));return;}
    for(const item of versions){const interval=item.effective_from?item.effective_from+" 至 "+(item.effective_to||"持续"):"全期间兼容版本";
      box.append(el("div","data-item","v"+item.version+" · "+interval+" · 保存于 "+item.updated_at));}
  }catch(err){if(current())box.append(el("p","muted","版本历史加载失败："+err.message));}
}

function ruleEditorCurrent(request,owner){return request===ruleEditorRequest&&currentUser?.id===owner&&!!editingRule&&$("knowledgePanel").classList.contains("active");}
function ruleEditorControls(){
  $("ruleEditor").querySelectorAll("input:not([readonly]),select,textarea,button").forEach(n=>{n.disabled=ruleBusy;});
}
async function submitRuleDraft(publish){
  if(ruleBusy||!editingRule||!$("knowledgePanel").classList.contains("active"))return;
  const request=ruleEditorRequest,owner=currentUser.id,id=editingRule.id;
  try{
    const body=ruleDraftBody(),audit=$("ruleTrialAudit").value;
    if(!publish&&!audit)throw new Error("请先完成一份可访问的审计再试跑。");
    ruleBusy=true;ruleEditorControls();$("ruleTrialResult").hidden=true;
    const result=await api("/api/rules/"+id+(publish?"/parameters":"/trial"),{method:publish?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(publish?body:{...body,audit_id:audit})});
    if(!ruleEditorCurrent(request,owner))return;
    if(publish){
      openRuleEditor(result);
      $("ruleEditorStatus").textContent="已保存 "+result.id+" v"+result.version+"；图谱新版本仍须审核，历史报告保持原版本。";
      await loadGraphRules();
    }else{showRuleTrial(result);$("ruleEditorStatus").textContent="试跑完成，历史审计未被修改。";}
  }catch(err){if(ruleEditorCurrent(request,owner)){
    $("ruleEditorStatus").textContent=(publish?"发布未确认，请刷新核对版本后再操作：":"试跑失败：")+err.message;
    showError(err.message);
  }}finally{ruleBusy=false;ruleEditorControls();}
}

function ruleDraftBody(){
  if(!editingRule)throw new Error("请先选择规则。");const version=$("ruleNewVersion").value.trim(),basis=$("ruleThresholdBasis").value.trim();
  if(!version)throw new Error("请填写新版本号。");if(!basis)throw new Error("请填写阈值依据。");
  if(Array.from($("ruleParamFields").querySelectorAll('input[type="number"]')).some(input=>input.value===""))throw new Error("数值参数不能为空。");
  const from=$("ruleEffectiveFrom").value||null,to=$("ruleEffectiveTo").value||null;
  if(to&&!from)throw new Error("请填写生效起始日。");if(from&&to&&to<from)throw new Error("终止日不能早于起始日。");
  return {expected_version:editingRule.version,new_version:version,logic:ruleDraftLogic,threshold_basis:basis,effective_from:from,effective_to:to};
}

function showRuleTrial(result){
  const box=$("ruleTrialResult");box.className="rule-trial-result "+result.status;box.textContent="";const labels={hit:"风险命中",pass:"检查通过",skipped:"未执行"};
  box.append(el("h3",null,"试跑结果："+(labels[result.status]||result.status)+" · v"+result.version),el("p",null,result.conclusion));
  if(result.calculation)box.append(el("div","calc-box",result.calculation));if(result.skip_reason)box.append(el("p","muted",result.skip_reason));box.hidden=false;
}

let memberRequest=0,memberSnapshot=null;
function memberCurrent(uid,org){return currentUser?.id===uid&&$("memberOrg").value===org;}
function clearMemberInvitations(){
  $("memberInvitationPanel").hidden=true;$("memberInvitationList").textContent="";
  $("memberInvitationReveal").hidden=true;$("memberInvitationURL").textContent="";
  $("memberDomains").value="";$("memberDomainEnabled").checked=false;
}
function renderMemberInvitations(result,uid,org){
  $("memberInvitationPanel").hidden=memberSnapshot?.org_type==="university";
  $("btnIssueMemberInvite").disabled=false;$("btnSaveInvitationPolicy").disabled=false;
  $("memberDomainEnabled").checked=result.policy.enabled;$("memberDomains").value=result.policy.domains.join("\n");
  TPMemberLinks.links($("memberInvitationList"),{links:result.links.filter(link=>link.role==="accountant"),valid:()=>memberCurrent(uid,org),
    refresh:link=>mutateMemberInvitation("/invitations/"+encodeURIComponent(link.id)+"/refresh","POST",{revision:link.revision}),
    toggle:link=>mutateMemberInvitation("/invitations/"+encodeURIComponent(link.id)+"/state","PUT",{revision:link.revision,active:!link.active})});
}

async function mutateMemberInvitation(suffix,method,body){
  const uid=currentUser?.id,org=$("memberOrg").value,request=memberRequest;
  if(!memberSnapshot||memberSnapshot.org_id!==org)return;
  $("memberInvitationPanel").querySelectorAll("button").forEach(button=>button.disabled=true);
  $("memberInvitationURL").textContent="";$("memberInvitationReveal").hidden=true;
  let result;
  try{result=await api("/api/members/"+encodeURIComponent(org)+suffix,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}
  catch(err){if(memberCurrent(uid,org)&&request===memberRequest)showError(err.message);}
  if(!memberCurrent(uid,org)||request!==memberRequest)return;
  const reloadRequest=memberRequest+1;await loadMembers();
  if(result?.path&&memberCurrent(uid,org)&&memberRequest===reloadRequest&&memberSnapshot){
    $("memberInvitationURL").textContent=new URL(result.path,location.origin).href;
    $("memberInvitationReveal").hidden=false;$("btnCopyMemberInvite").disabled=false;$("btnHideMemberInvite").disabled=false;
  }
}
$("btnIssueMemberInvite").addEventListener("click",()=>mutateMemberInvitation("/invitations","POST",{role:$("memberInviteRole").value}));
let memberQuotaEdit=null,memberQuotaConfirmation=null;
function memberQuotaControls(context,saving=false){
  for(const [kind,inputs,edit,save,cancel] of [
    ["university",["universityTeacherQuota","universityStudentQuota"],"btnEditUniversityCapacity","btnSaveUniversityCapacity","btnCancelUniversityCapacity"],
    ["enterprise",["memberSeats"],"btnEditMemberQuota","btnSaveMemberQuota","btnCancelMemberQuota"]]){
    const editing=context?.kind===kind;
    for(const id of inputs)$(id).disabled=!editing||saving;
    $(edit).hidden=editing;$(save).hidden=$(cancel).hidden=!editing;
    $(save).disabled=$(cancel).disabled=saving||!editing;
  }
}
function resetMemberQuotaEditor(){
  memberQuotaEdit=null;memberQuotaConfirmation=null;
  if($("quotaConfirmDialog").open)$("quotaConfirmDialog").close();
  memberQuotaControls(null);$("universityCapacityStatus").textContent="";$("memberQuotaSaveStatus").textContent="";
}
function memberQuotaContext(kind){
  const data=memberSnapshot,org=$("memberOrg").value;
  if(currentUser?.role!=="platform_admin"||!data||data.org_id!==org||
    (kind==="university"?data.org_type!=="university"||!data.university_quota:data.org_type==="university"))return null;
  return {kind,org,uid:currentUser.id,request:memberRequest,snapshot:data};
}
function memberQuotaValid(context){
  return !!context&&currentUser?.role==="platform_admin"&&memberCurrent(context.uid,context.org)&&
    memberRequest===context.request&&memberSnapshot===context.snapshot;
}
function memberQuotaStatus(context){return $(context.kind==="university"?"universityCapacityStatus":"memberQuotaSaveStatus");}
function openMemberQuotaConfirmation(kind,stage){
  const context=stage==="unlock"?memberQuotaContext(kind):memberQuotaEdit;
  if(!memberQuotaValid(context)||context.kind!==kind)return;
  const school=kind==="university",q=school?context.snapshot.university_quota:context.snapshot.quota;
  const fields=school?[["教职工总额度","universityTeacherQuota",q.teacher_quota],["学生总额度","universityStudentQuota",q.student_quota]]:[["机构总席位","memberSeats",q.seats]];
  if(stage==="save"&&!fields.every(([,id])=>$(id).reportValidity()))return;
  const values=fields.map(([,id])=>Number($(id).value));
  if(stage==="save"&&fields.every(([, ,old],i)=>old===values[i])){memberQuotaStatus(context).textContent="额度未变化，无需保存。";return;}
  const body=school?{teacher_quota:values[0],student_quota:values[1],revision:q.revision}:{seats:values[0],revision:q.revision};
  memberQuotaConfirmation={...context,stage,body};
  $("quotaConfirmTitle").textContent=stage==="unlock"?"确认修改席位":"确认保存席位修改";
  $("quotaConfirmOrganization").textContent="管理机构："+($("memberOrg").selectedOptions?.[0]?.textContent||context.org);
  $("quotaConfirmDescription").textContent=stage==="unlock"?"确认后可编辑席位额度；当前额度不会立即改变。":"请核对以下修改，确认后提交保存。";
  $("quotaConfirmChanges").replaceChildren(...fields.map(([label,,old],i)=>el("p",null,label+"："+(old??"未配置")+(stage==="save"?" → "+values[i]:""))));
  $("quotaConfirmAccept").textContent=stage==="unlock"?"确认修改":"确认保存";
  $("quotaConfirmDialog").showModal();
}
async function saveMemberQuota(context,body){
  if(!memberQuotaValid(context))return;
  const school=context.kind==="university",q=context.snapshot.university_quota,status=memberQuotaStatus(context);
  memberQuotaControls(context,true);status.textContent="正在保存席位修改…";
  try{
    await api(school?"/api/universities/"+encodeURIComponent(q.college)+"/capacity":"/api/members/"+encodeURIComponent(context.org)+"/quota",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!memberQuotaValid(context))return;
    await loadMembers();
    const refreshed=()=>memberCurrent(context.uid,context.org)&&memberRequest===context.request+1&&memberSnapshot&&currentUser?.role==="platform_admin";
    if(!refreshed())return;
    status.textContent="席位修改已保存。";
    try{const logs=await api("/api/audit-log");if(refreshed())renderAdminLogs(logs,refreshed);}
    catch{if(refreshed())status.textContent="席位修改已保存，操作记录加载失败，请刷新管理页查看。";}
  }catch(err){if(memberQuotaValid(context)){status.textContent=err.message;memberQuotaControls(context);}}
}
$("quotaConfirmCancel").addEventListener("click",()=>$("quotaConfirmDialog").close());
$("quotaConfirmDialog").addEventListener("close",()=>{memberQuotaConfirmation=null;});
$("quotaConfirmAccept").addEventListener("click",async()=>{
  const context=memberQuotaConfirmation;
  if(!memberQuotaValid(context)){resetMemberQuotaEditor();return;}
  $("quotaConfirmDialog").close();
  if(context.stage==="unlock"){
    memberQuotaEdit=context;memberQuotaControls(context);memberQuotaStatus(context).textContent="修改后请保存；取消将保留原额度。";
    $(context.kind==="university"?"universityTeacherQuota":"memberSeats").focus();
  }else await saveMemberQuota(context,context.body);
});
for(const [kind,edit,save,cancel] of [["university","btnEditUniversityCapacity","btnSaveUniversityCapacity","btnCancelUniversityCapacity"],["enterprise","btnEditMemberQuota","btnSaveMemberQuota","btnCancelMemberQuota"]]){
  $(edit).addEventListener("click",()=>openMemberQuotaConfirmation(kind,"unlock"));
  $(save).addEventListener("click",()=>openMemberQuotaConfirmation(kind,"save"));
  $(cancel).addEventListener("click",()=>{if(memberQuotaValid(memberQuotaEdit)){resetMemberQuotaEditor();renderMemberQuota(memberSnapshot);}});
}
function renderMemberQuota(result){
  const school=result.org_type==="university",q=school?result.university_quota:result.quota;
  $("memberQuotaEditor").hidden=currentUser?.role!=="platform_admin"||school;
  $("universityCapacityPanel").hidden=currentUser?.role!=="platform_admin"||!school||!q;
  if(school){
    $("userAdminHint").textContent="高校教职工与学生分别核销席位；平台管理员调整学校总额度，高校管理员在额度内分配。停用释放对应席位并撤销会话，恢复后须重新登录。";
    $("memberQuotaStatus").textContent=q?"教职工 "+q.teachers_used+" / "+q.teacher_quota+"，剩余 "+q.teacher_remaining+"；学生 "+q.students_used+" / "+q.student_quota+"，剩余 "+q.student_remaining+"。学生额度已分配 "+q.allocated+"，未分配 "+q.unallocated+"。":"高校席位信息尚未关联，请先核对学校关联后再配置额度。";
    if(q){$("universityTeacherQuota").value=q.teacher_quota;$("universityStudentQuota").value=q.student_quota;}
  }else{
    $("userAdminHint").textContent="机构管理员管理本机构员工；加入时核销席位，停用释放席位并撤销会话，恢复后须重新登录。";
    $("memberQuotaStatus").textContent=q.seats===null?"在岗 "+q.used+" 人；席位尚未配置，创建或恢复成员前须由平台配置。":"席位 "+q.used+" / "+q.seats+"，剩余 "+q.remaining+(q.over_quota?"；现有成员超额，须先处理或调整配额。":"。");
    $("memberSeats").value=q.seats??"";
  }
  memberQuotaControls(null);
}
$("btnSaveInvitationPolicy").addEventListener("click",()=>{
  if(!memberSnapshot?.invitations)return;
  mutateMemberInvitation("/invitation-policy","PUT",{enabled:$("memberDomainEnabled").checked,domains:$("memberDomains").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),revision:memberSnapshot.invitations.policy.revision});
});
$("btnHideMemberInvite").addEventListener("click",()=>{$("memberInvitationURL").textContent="";$("memberInvitationReveal").hidden=true;});
$("btnCopyMemberInvite").addEventListener("click",async()=>{
  try{if($("memberInvitationURL").textContent)await navigator.clipboard.writeText($("memberInvitationURL").textContent);}
  catch{showError("复制失败，请手动选中并复制链接。");}
});
async function loadMembers(refreshOrganizations=false){
  const uid=currentUser?.id,role=currentUser?.role,request=++memberRequest;
  resetMemberQuotaEditor();
  if(!["platform_admin","org_admin"].includes(role))return;
  memberSnapshot=null;$("btnSaveMemberQuota").disabled=true;$("memberQuotaStatus").textContent="正在读取成员与席位…";
  $("universityCapacityPanel").hidden=true;$("btnSaveUniversityCapacity").disabled=true;$("universityCapacityStatus").textContent="";
  $("universityTeacherQuota").value="";$("universityStudentQuota").value="";
  clearMemberInvitations();
  $("userList").textContent="";$("memberQuotaEditor").hidden=true;
  try{
    if(refreshOrganizations){
      const organizations=await api("/api/members/organizations");
      if(request!==memberRequest||currentUser?.id!==uid)return;
      const selected=$("memberOrg").value;$("memberOrg").textContent="";
      for(const org of organizations){const option=el("option",null,org.name+" · "+org.org_id);option.value=org.org_id;$("memberOrg").append(option);}
      if(organizations.some(org=>org.org_id===selected))$("memberOrg").value=selected;
      $("memberOrg").disabled=role!=="platform_admin";
    }
    const org=$("memberOrg").value;
    if(!org){$("memberQuotaStatus").textContent="暂无已开户机构；请先签发创始码，由持码人完成邮箱验证与开户。";return;}
    const [result,invites]=await Promise.all([api("/api/members/"+encodeURIComponent(org)),api("/api/members/"+encodeURIComponent(org)+"/invitations")]);
    if(request!==memberRequest||!memberCurrent(uid,org))return;
    memberSnapshot={...result,invitations:invites};renderMemberInvitations(invites,uid,org);
    renderMemberQuota(result);
    const names={platform_admin:"平台管理员",org_admin:"机构管理员",accountant:"会计",teacher:"教师",student:"学生"};
    const sources={founder:"创始码开户",admin_created:"机构管理员创建",member_invitation:"机构邀请链接",legacy:"现有账号（来源未登记）"};
    for(const person of result.members){
      const item=el("div","data-item"),text=el("div"),actions=el("div","row");
      text.append(el("div",null,person.display_name+" · "+person.username),el("p","muted",(names[person.role]||person.role)+" · 注册："+person.created_at+" · "+(sources[person.source_kind]||person.source_kind)));
      if(person.source_kind==="member_invitation")text.append(el("p","muted","来源凭证："+person.credential_id+" · "+(person.invitation_active?"当前可用":"当前停用")+" · 签发："+(person.invitation_created_at||"—")));
      actions.append(el("span","role-chip",person.active?"在岗":"已停用"));
      if(person.id!==uid&&(role==="platform_admin"||person.role!=="org_admin")){
        const button=el("button",null,person.active?"停用成员":"恢复成员");
        button.addEventListener("click",async()=>{
          if(!memberCurrent(uid,org))return;
          if(!window.confirm((person.active?"停用将撤销旧会话并释放席位，不删除历史业务。确认停用":"恢复需要可用席位，旧会话不会恢复。确认恢复")+"「"+person.display_name+"」？"))return;
          button.disabled=true;
          try{await api("/api/members/"+encodeURIComponent(org)+"/"+encodeURIComponent(person.id)+"/active",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({active:!person.active,expected_active:!!person.active})});}
          catch(err){if(memberCurrent(uid,org))showError(err.message);}
          finally{if(memberCurrent(uid,org))await loadMembers();}
        });actions.append(button);
      }
      item.append(text,actions);$("userList").append(item);
    }
  }catch(err){if(request===memberRequest&&currentUser?.id===uid){$("memberQuotaStatus").textContent="成员信息加载失败，请刷新重试。";showError(err.message);}}
}
$("memberOrg").addEventListener("change",()=>loadMembers());
$("btnRefreshMembers").addEventListener("click",()=>loadMembers(true));

function renderAdminLogs(logs,current){
  const root=$("logList");root.replaceChildren();
  const localTime=value=>{const time=new Date(value);return Number.isNaN(time.getTime())?"时间未记录":new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(time);};
  const kinds={analysis:"资料分析快照",edit:"保存资料核对",confirmation:"确认检查结果",material_change:"材料变更"};
  for(const log of logs.slice(0,80)){
    const row=el("div","data-item"),content=el("div");
    content.append(el("p","",`${localTime(log.created_at)}（北京时间） · ${log.action} · ${log.target_type} ${log.target_id}`));row.append(content);root.append(row);
    if(["set_member_quota","set_university_capacity"].includes(log.action)){
      let detail;try{detail=JSON.parse(log.detail);}catch{}
      if(detail?.before&&detail?.after){
        content.append(el("p","",`管理机构：${detail.organization_name||detail.organization_id} · 操作人：${detail.actor?.display_name||"未记录"}（${detail.actor?.username||detail.actor?.id||"未记录"}） · 来源 IP：${detail.source_ip||"未记录"}`));
        const labels={seats:"机构总席位",teacher_quota:"教职工总额度",student_quota:"学生总额度"};
        for(const [key,label]of Object.entries(labels))if(Object.hasOwn(detail.after,key))content.append(el("p","muted",`${label}：${detail.before[key]??"未配置"} → ${detail.after[key]}`));
        content.append(el("p","muted",`配置版本：${detail.old_revision} → ${detail.new_revision} · 操作人 ID：${detail.actor?.id||"未记录"} · 机构 ID：${detail.organization_id}`));
      }else content.append(el("p","muted","历史记录未保存完整前后额度及来源 IP。"));
    }
    if(currentUser?.role!=="org_admin"||log.target_type!=="material_batch")continue;
    const trace=el("div");trace.hidden=true;
    const button=el("button","btn re","查看材料追溯");button.type="button";
    const valid=()=>current()&&currentUser?.role==="org_admin";
    button.onclick=async()=>{
      if(!valid())return;
      if(!trace.hidden){trace.hidden=true;button.textContent="查看材料追溯";return;}
      trace.hidden=false;trace.replaceChildren(el("p","muted","正在查询材料追溯…"));button.disabled=true;
      try{
        const result=await api("/api/enterprise/materials/"+encodeURIComponent(log.target_id)+"/trace");
        if(!valid())return;
        trace.replaceChildren(el("p","muted",`材料任务 ${result.id} · 当前版本 ${result.revision}`));
        for(const version of [...(result.versions||[])].reverse()){
          trace.append(el("p","",`第 ${version.revision} 版 · ${kinds[version.kind]||version.kind} · ${localTime(version.created_at)}（北京时间） · 操作人 ${version.created_by||"未记录"}`));
        }
        const events=el("details");events.append(el("summary","",`上传、核对与访问记录（${result.events?.length||0} 条）`));
        for(const event of [...(result.events||[])].reverse())events.append(el("p","muted",`${localTime(event.created_at)}（北京时间） · ${event.action} · 操作人 ${event.actor_id||"系统"} · 版本 ${event.revision??"未记录"}`));
        trace.append(events);button.textContent="收起材料追溯";
      }catch(error){if(valid()){trace.replaceChildren(el("p","material-note","查询失败："+error.message));button.textContent="收起材料追溯";}}
      finally{if(valid())button.disabled=false;}
    };
    content.append(button,trace);
  }
}

async function loadAdmin() {
  const request=++adminRequest,owner=currentUser?.id;
  const current=()=>request===adminRequest&&currentUser?.id===owner&&$("adminPanel").classList.contains("active");
  try {
    if(isCampusUser()&&!campusStudent()){
      if(currentUser.role==='org_admin'&&!currentUser.legacy_teaching)await loadOrgSettings();
      if(current())loadCampusMembers();
      return;
    }
    let users=[];
    if (["platform_admin","org_admin"].includes(currentUser.role)) {
      if(currentUser.role==="org_admin") await loadOrgSettings();
      users=await api("/api/users"); await loadMembers(true);
      if(currentUser.role==="org_admin"){$("newRole").textContent="";const o=el("option",null,"会计");o.value="accountant";$("newRole").appendChild(o);}
      const accountants=users.filter(u=>u.role==="accountant"&&u.active&&u.org_id===currentUser.org_id),assigned=$("clientAccountant").value;
      $("clientAccountant").textContent="";const none=el("option",null,"暂不指派");none.value="";$("clientAccountant").append(none);
      for(const u of accountants){const o=el("option",null,u.display_name+" · "+u.username);o.value=u.id;$("clientAccountant").append(o);}
      if(accountants.some(u=>u.id===assigned))$("clientAccountant").value=assigned;
      const clients=currentUser.role==="org_admin"?await api("/api/clients"):[],cb=$("clientList");cb.textContent="";
      for(const client of clients){const item=el("div","data-item"),text=el("div");text.append(el("div",null,client.name),el("p","muted",client.taxpayer_id+" · 负责会计："+(client.accountant_name||client.accountant_username||"未指派")));item.append(text,el("span","role-chip","客户档案"));cb.append(item);}
      if(!clients.length)cb.append(el("div","data-item","暂无客户档案。"));
    }
    if(currentUser.role==="platform_admin"){
      const invites=await api("/api/invites"),ib=$("inviteList");ib.textContent="";
      const nowMs=Date.now();
      for(const inv of invites){
        const state=inv.revoked?"已吊销":inv.redeemed_by?"已注册":(new Date(inv.expires_at).getTime()<nowMs?"已过期":"未使用");
        const item=el("div","data-item"),text=el("div");
        const detail=["机构类别："+inviteOrgTypeLabel(inv.org_type),"机构总席位："+inv.seats,"签发："+(inv.created_at||"—"),"有效期至："+inv.expires_at];
        if(inv.redeemed_email)detail.push("注册人："+inv.redeemed_email+(inv.redeemed_at?" · 注册时间："+inv.redeemed_at:""));
        text.append(el("div",null,inv.org_name),el("p","muted",detail.join(" · ")));
        item.append(text,el("span","role-chip",state));ib.append(item);
      }
      if(!invites.length)ib.append(el("div","data-item","暂无签发记录。"));
    }
    if(["platform_admin","org_admin","teacher"].includes(currentUser.role)){const logs=await api("/api/audit-log");if(current())renderAdminLogs(logs,current);}
  } catch(err){if(current())showError(err.message);}
}

$("btnCreateUser").addEventListener("click",async()=>{const uid=currentUser?.id;$("btnCreateUser").disabled=true;try{await api("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:$("newUsername").value,password:$("newPassword").value,display_name:$("newDisplayName").value,role:$("newRole").value})});if(currentUser?.id===uid){$("newPassword").value="";await loadAdmin();}}catch(err){if(currentUser?.id===uid)showError(err.message);}finally{$("btnCreateUser").disabled=false;}});
function inviteOrgTypeLabel(value){return value==="enterprise"?"企业":value==="university"?"高校":"未标注";}
function setInviteOrgType(value){
  if(!["enterprise","university"].includes(value))return;
  $("inviteOrgType").value=value;$("inviteOrgTypeOptions").dataset.value=value;
  $("inviteTypeEnterprise").setAttribute("aria-pressed",String(value==="enterprise"));
  $("inviteTypeUniversity").setAttribute("aria-pressed",String(value==="university"));
  const university=value==="university";
  $("inviteStudentSeatsField").hidden=!university;
  $("inviteSeatsLabel").textContent=university?"教职工总席位（含高校管理员）":"机构总席位（含管理员）";
  $("inviteOrgName").placeholder=university?"例如：某某财经大学":"例如：某某税务师事务所";
}
$("inviteTypeEnterprise").addEventListener("click",()=>setInviteOrgType("enterprise"));
$("inviteTypeUniversity").addEventListener("click",()=>setInviteOrgType("university"));
$("inviteSeats").addEventListener("change",()=>{
  const custom=$("inviteSeats").value==="custom";
  $("inviteCustomSeatsField").hidden=!custom;
  $("inviteCustomSeats").disabled=!custom;
  if(custom)$("inviteCustomSeats").focus();
});
$("btnCreateInvite").addEventListener("click",async()=>{
  try{
    const org=$("inviteOrgName").value.trim(),orgType=$("inviteOrgType").value,seats=Number($("inviteSeats").value==="custom"?$("inviteCustomSeats").value:$("inviteSeats").value);
    if(!Number.isInteger(seats)||seats<1||seats>200)throw new Error("席位须为 1–200 的整数（含管理员）。");
    if(!["enterprise","university"].includes(orgType))throw new Error("请选择机构类别：企业或高校。");
    if(!org)throw new Error("请填写机构名称。");
    const inv=await api("/api/invites",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({org_name:org,org_type:orgType,seats,student_seats:orgType==="university"?Number($("inviteStudentSeats").value):0})});
    $("inviteOrgName").value="";
    $("inviteRevealCode").textContent=inv.code;
    $("inviteRevealOrg").textContent=inviteOrgTypeLabel(inv.org_type)+" · "+inv.org_name;
    $("inviteRevealSeats").textContent=(inv.org_type==="university"?"教职工 "+inv.seats+" 席（含管理员） · 学生 "+inv.student_seats+" 席":"机构总席位："+inv.seats+" 席（含管理员）");
    $("btnCopyInvite").disabled=false;
    $("inviteReveal").hidden=false;
    await loadAdmin();
  }catch(err){showError(err.message);}
});
$("btnCopyInvite").addEventListener("click",async()=>{
  const code=$("inviteRevealCode").textContent.trim();
  if($("inviteReveal").hidden||!code)return showError("请先签发邀请码。");
  try{await navigator.clipboard.writeText(code);showToast("邀请码已复制");}
  catch(err){showError("复制失败，请手动选中复制。");}
});
$("btnHideInvite").addEventListener("click",()=>{
  $("inviteReveal").hidden=true;$("inviteRevealCode").textContent="";
  $("inviteRevealOrg").textContent="";$("inviteRevealSeats").textContent="";
  $("btnCopyInvite").disabled=true;
});
$("btnCreateClient").addEventListener("click",async()=>{try{const client=await api("/api/clients",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("clientName").value,taxpayer_id:$("clientTaxpayerId").value,accountant_id:$("clientAccountant").value||null})});selectedClientId=client.id;$("clientName").value="";$("clientTaxpayerId").value="";await loadAdmin();}catch(err){showError(err.message);}});
$("btnSaveOrgSettings").addEventListener("click",async()=>{try{const saved=await api("/api/org/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({display_name:$("orgDisplayName").value,report_title:$("orgReportTitle").value,footer_text:$("orgFooterText").value})});showOrgSettings(saved,"报告抬头与页脚已保存。");}catch(err){showError(err.message);}});
$("btnUploadOrgLogo").addEventListener("click",async()=>{const file=$("orgLogoFile").files[0];if(!file)return showError("请选择 PNG 或 JPEG Logo。");try{const form=new FormData();form.append("file",file);const saved=await api("/api/org/logo",{method:"POST",body:form});$("orgLogoFile").value="";showOrgSettings(saved,"Logo 已上传并同步到报告模板。");}catch(err){showError(err.message);}});
$("btnRemoveOrgLogo").addEventListener("click",async()=>{if(!window.confirm("确认移除本机构报告 Logo？机构名称、标题和页脚不会改变。"))return;try{showOrgSettings(await api("/api/org/logo",{method:"DELETE"}),"Logo 已移除。");}catch(err){showError(err.message);}});
$("btnRuleTrial").addEventListener("click",()=>submitRuleDraft(false));
$("btnRuleSave").addEventListener("click",()=>submitRuleDraft(true));
for(const event of ["input","change"])$("ruleEditor").addEventListener(event,()=>{
  $("ruleTrialResult").hidden=true;
  $("ruleEditorStatus").textContent="草稿已修改；请重新试跑或核对后发布，旧试跑结果不再适用。";
});
