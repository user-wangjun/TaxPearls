/* University modules use the main workspace shell and their existing scoped APIs.
   Native module imports isolate legacy state/functions without an iframe or eval. */
"use strict";
window.universityWorkspace = (() => {
  const $ = id => document.getElementById(id);
  let identity = null, controller = null, selected = "home", loading = null;
  const studentViews = [["home", "学习总览"], ["tasks", "课程任务"], ["practice", "练习与考证"], ["wrongs", "错题复习"], ["goals", "考证目标"], ["catalog", "学习资源"], ["knowledge", "政策与学习图谱"], ["profile", "学习档案"], ["settings", "账号设置"]];
  const staffViews = [["home", "教学总览"], ["students", "师生与班级"], ["courses", "课程管理"], ["tasks", "课程与作业"], ["statistics", "学情统计"], ["knowledge", "政策与学习图谱"], ["content", "教学资源"], ["archives", "教学档案"], ["settings", "账号设置"]];
  const labels = {student:"学生", teacher:"教师", admin:"高校管理员 / 教师", school_admin:"校级管理员", unlinked:"待关联教学身份"};
  const viewsFor = (kind, role) => role === "unlinked" ? [["home", "教学工作台"]] : kind === "student" ? studentViews : staffViews.filter(([key]) => role !== "school_admin" || ["home", "students", "courses", "knowledge", "settings"].includes(key));
  let overview = null, viewRevision = 0;
  function resolveIdentity(user) {
    if (!user || !(user.org_type === "university" || user.workspace === "university" || ["/training-staff", "/training-certificates"].includes(user.workspace))) return null;
    const role = user.education_role || (user.workspace === "university" ? user.role : null);
    const kind = role === "student" ? "student" : "staff";
    return {...user, workspace:"university", universityKind:kind, role:role || "unlinked", platformRole:user.role};
  }
  const element = (tag, cls, text) => { const node = document.createElement(tag); if(cls)node.className=cls; if(text !== undefined)node.textContent=String(text); return node; };
  const classroomViews = new Set(["students", "tasks", "statistics", "wrongs", "profile"]);
  const nativeViews = new Set(["students", "courses", "statistics", "content", "settings", "goals", "practice", "wrongs", "catalog", "profile"]);
  function usesNative(view=selected) { return nativeViews.has(view)||(view==="home"&&identity.universityKind==="student"); }
  function classroomCapable() { return !identity.legacy_teaching && ["admin", "teacher", "student"].includes(identity.role); }
  function setupPanels() {
    const panel = $("universityPanel");
    overview = element("div", "university-overview"); overview.id = "universityOverview";
    panel.insertBefore(overview, $("universityContent"));
    for (const id of ["trainingPanel", "knowledgePanel"]) panel.append($(id));
    const sections = {teacherBox:"tasks", classManager:"students", paperComposer:"tasks", classStatistics:"statistics", studentProfile:"statistics profile", paperDetail:"tasks", mistakeBook:"wrongs"};
    for(const [id, view] of Object.entries(sections))$(id).dataset.classroomView=view;
    for(const [id, view] of [["classList","students"],["paperList","tasks"],["assignmentList","tasks"],["reviewBox","tasks"]]) {
      const node=$(id);if(node)(node.classList.contains("sheet")?node:node.closest(".sheet")).dataset.classroomView=view;
    }
    $("exerciseBox").dataset.classroomView="tasks";
    for(const [id, parent]of [["assignmentCourse","teacherBox"],["paperCourse","paperComposerControls"]]){
      const field=element("div","field university-course-field"),label=element("label",null,"所属课程（可选）"),select=element("select");
      label.htmlFor=id;select.id=id;select.append(new Option("独立作业 / 不关联课程",""));field.append(label,select);$(parent).prepend(field);
    }

    const heading=$("knowledgePanel").querySelector(".graph-page-head");
    heading.querySelector("h2").textContent="政策、规则与知识点";
    heading.querySelector(".eyebrow").textContent="公共政策 / LEARNING GRAPH";
    heading.querySelector(".muted").textContent="从课程知识点查阅关联规则与政策原文，保留来源、版本和审核状态。";
    $("graphScope").closest(".graph-scope").hidden=true;
    $("graphStatus").closest(".field").hidden=true;
    $("kgEvidencePanel").hidden=true;
    const prompts=[["学习依据","请解释当前知识点或规则的学习依据，区分已核实来源与待审核内容。"],["知识点关联","请解释当前节点与关联知识点、规则和政策原文的关系，不将学习关联当作法律推导。"],["复习建议","请根据当前节点提供复习顺序；没有学生作答记录时，不推断其掌握程度。"]];
    $("graphAssistant").querySelectorAll("[data-question]").forEach((button,i)=>{button.textContent=prompts[i][0];button.dataset.question=prompts[i][1];});
  }
  function renderOverview(data, archive=false) {
    overview.replaceChildren();
    if(archive) {
      const card=element("section","sheet");card.append(element("h2",null,identity.role==="student"?"课程学习记录":"教学作答与复核记录"));
      if(!data.records.length)card.append(element("p","muted","暂无课程作答记录。完成课堂任务后，在这里查看成绩与复核记录。"));
      for(const row of data.records){const item=element("div","data-item");item.append(element("strong",null,row.title),element("p","muted",(row.student?row.student+" · ":"")+row.score+" 分 · "+(row.reviewed?"已人工复核":"自动评分")+" · "+new Date(row.submitted_at).toLocaleString()));card.append(item);}
      overview.append(card);return;
    }
    const c=data.counts, grid=element("div","kpi-grid");
    const metrics=identity.role==="student"?[["所在班级",c.classrooms,"个"],["课程任务",c.assignments,"项"],["已提交",c.submitted,"项"],["在学课程",c.courses,"门"]]:[["教学班级",c.classrooms,"个"],["在校学生",c.students,"人"],["课程",c.courses,"门"],["作业",c.assignments,"项"]];
    for(const [label,value,unit]of metrics){const card=element("div","kpi");card.append(element("div",null,label));const number=element("div","value",value);number.append(element("span","unit",unit));card.append(number);grid.append(card);}overview.append(grid);
    const actions=element("section","sheet university-home-shortcuts");actions.append(element("h2",null,identity.role==="student"?"继续学习":"继续教学"),element("p","muted",data.school+" · "+(identity.role==="student"?"查看课程任务，安排考证练习，积累学习记录。":"查看教学安排和课程进度，继续备课与复核。")));
    const row=element("div","university-home-actions");
    for(const [view,label]of (identity.role==="student"?[["tasks","查看课程任务"],["practice","开始练习"],["knowledge","查阅政策与图谱"]]:[["students","管理师生与班级"],["tasks","备课与发布作业"],["statistics","查看学情"],["knowledge","查阅政策与图谱"]])){
      if(!viewsFor(identity.universityKind,identity.role).some(v=>v[0]===view))continue;
      const button=element("button","btn re",label);button.onclick=()=>choose(view);row.append(button);
    }actions.append(row);overview.append(actions);
    if(data.courses.length){const courseList=element("section","sheet");courseList.append(element("h2",null,identity.role==="student"?"我的课程":"课程安排"));for(const course of data.courses)courseList.append(element("p",null,course.name+(course.semester?" · "+course.semester:"")));overview.append(courseList);}
    const tasks=element("section","sheet");tasks.append(element("h2",null,identity.role==="student"?"课程任务与截止时间":"作业进度与复核"));
    if(!data.assignments.length)tasks.append(element("p","muted",identity.role==="student"?"暂无分配给你的课程任务，可先进入练习与考证。":"尚未发布课堂作业。可先建立教学班级，再生成仿真案例或组卷。"));
    for(const task of data.assignments.slice(0,8)){
      const item=element("div","data-item university-task-row"),details=element("div");
      details.append(element("strong",null,(task.course_name?task.course_name+" · ":"")+task.title),element("p","muted",(task.published?(task.deadline_passed?"已截止":"已发布"):"草稿")+" · "+(task.deadline_at?new Date(task.deadline_at).toLocaleString():"未设置截止时间")));
      const open=element("button","btn re",identity.role==="student"?"进入作业":"查看作业");
      open.onclick=async()=>{if(await choose("tasks"))await openExercise(task.id);};
      item.append(details,open);tasks.append(item);
    }
    if(identity.role!=="student")tasks.append(element("p","muted","已提交作答 "+c.submitted+" 份；尚未人工复核 "+c.unreviewed+" 份。自动评分和人工复核分别保留。"));overview.append(tasks);
    if(identity.role!=="student"&&c.unreviewed){const review=element("button","btn re","复核学生作答（"+c.unreviewed+"）");review.onclick=async()=>{if(await choose("tasks"))$("reviewBox").scrollIntoView({behavior:"smooth",block:"start"});};tasks.append(review);}
    const knowledge=element("section","sheet");knowledge.append(element("h2",null,"公共政策与教学依据"),element("p","muted","政策、条款和规则来自统一公共知识目录；课程知识点以明确关联接入同一图谱。候选或未核实内容保留来源状态，历史作答沿用当时发布的版本。"));overview.append(knowledge);
  }

  async function loadCourseBindings() {
    if(!classroomCapable()||identity.role==="student")return;
    const [courses,classes]=await Promise.all([request("/api/teaching/courses"),request("/api/classes")]);
    if(selected!=="courses")return;
    $("universityCourseBindings")?.remove();
    const card=element("section","sheet");card.id="universityCourseBindings";card.dataset.universityView="courses";
    card.append(element("h2",null,"课程与教学班级"),element("p","muted","按明确的课程和教学班级关联课堂任务；不按同名班级自动合并。课程作业只能发布给已关联的班级。此处维护你管理的教学班级，其他教师已有的关联会保留。"));
    if(!courses.length)card.append(element("p","muted","尚无课程。新建课程后，可在这里选择教学班级。"));
    for(const course of courses){
      const item=element("div","data-item"),checks=element("div","training-checks");item.append(element("h3",null,course.name));
      for(const classroom of classes){const label=element("label"),check=element("input");check.type="checkbox";check.value=classroom.id;check.checked=course.class_ids.includes(classroom.id);label.append(check,document.createTextNode(" "+classroom.name));checks.append(label);}
      if(!classes.length)checks.append(element("p","muted","请先在“师生与班级”建立自己的教学班级。"));
      const message=element("p","muted"),save=element("button","btn re","保存我管理班级的关联");save.onclick=async()=>{save.disabled=true;try{await request("/api/teaching/courses/"+encodeURIComponent(course.id)+"/classrooms",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({class_ids:[...checks.querySelectorAll("input:checked")].map(n=>n.value)})});message.textContent="教学班级关联已保存。";}catch(error){message.textContent=error.message;}finally{save.disabled=false;}};
      item.append(checks,save,message);card.append(item);
    }
    $("universityContent").prepend(card);
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {credentials:"same-origin", ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.detail || `请求失败（${response.status}）`); error.status = response.status; throw error; }
    return data;
  }
  function updateLogin() {
    const setup = $("authForm").dataset.setup === "1";
    const login = $("authForm").dataset.view === "login" && !setup;
    const needsCollege = login && $("loginCollege").dataset.needed === "1";
    $("loginCollegeField").hidden = !needsCollege;
    $("loginCollege").required = needsCollege;
    $("loginCollege").disabled = !needsCollege;
    if (login) {
      $("loginUserLabel").textContent = "邮箱、用户名或学号";
      $("loginUser").placeholder = "请输入邮箱、用户名或学号";
      $("loginUser").inputMode = "text";
    }
  }
  function status(message, error = false) {
    $("universityStatus").textContent = message;
    $("universityStatus").className = error ? "university-status error" : "university-status muted";
    $("universityRetry").hidden = !error;
  }
  function choose(view = "home") {
    if (!identity) return;
    const allowed = viewsFor(identity.universityKind, identity.role);
    selected = allowed.some(([id]) => id === view) ? view : "home";
    $("universityTitle").textContent = selected === "settings" ? "个人信息" : allowed.find(([id]) => id === selected)[1];
    const account = document.querySelector('#nav [data-panel="userSettingsPanel"]');
    account.classList.toggle("active", selected === "settings");
    if (selected === "settings") account.setAttribute("aria-current", "page"); else account.removeAttribute("aria-current");
    document.querySelectorAll("#universityNav button").forEach(b => {
      b.classList.toggle("active", b.dataset.universityView === selected);
      if (b.dataset.universityView === selected) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    applyView();
    history.replaceState(null, "", `/?workspace=${identity.universityKind}#panel=universityPanel&view=${selected}`);
    $("universityPanel").dataset.view = selected;
    window.scrollTo({top:0, behavior:"instant"});
    return refresh();
  }

  function applyView() {
    if(!identity)return;
    const native=usesNative();
    $("universityContent").hidden=!native;
    if(overview)overview.hidden=!["home","archives"].includes(selected);
    $("trainingPanel").classList.toggle("active",classroomViews.has(selected)&&classroomCapable());
    $("knowledgePanel").classList.toggle("active",selected==="knowledge");
    const group=selected==="wrongs"?"practice":selected;
    document.querySelectorAll("#universityContent [data-university-view]").forEach(card=>{
      const views=card.dataset.universityView.split(" ");
      const visible=views.includes(group)||(group==="courses"&&views.includes("organization"))||(group==="content"&&views.includes("judge"));
      card.classList.toggle("university-view-hidden",!visible);
    });
    document.querySelectorAll("#trainingPanel > [data-classroom-view]").forEach(card=>card.classList.toggle("university-view-hidden",!card.dataset.classroomView.split(" ").includes(selected)));
    const tools=$("universityContent").querySelector(".toolbar");if(tools)tools.hidden=selected!=="content";
    if(selected==="settings"&&identity.universityKind==="staff"){
      const form=$("universityContent").querySelector("#pass-form");if(form)form.hidden=false;
    }
  }
  function decorate() {
    applyView();
    $("universityContent").querySelectorAll(".card").forEach(card => card.classList.add("sheet"));
    $("universityContent").querySelectorAll("button.primary").forEach(b => b.classList.add("btn", "pdf"));
    $("universityContent").querySelectorAll("label:not([for])").forEach(label=>{
      const field=label.nextElementSibling;
      if(!field?.matches("input,select,textarea"))return;
      if(field.id)label.htmlFor=field.id;
      else if(!field.hasAttribute("aria-label"))field.setAttribute("aria-label",label.textContent.trim());
    });
    $("universityPanel").querySelectorAll("table").forEach(table => {
      if (!table.parentElement.classList.contains("table-scroll")) {
        const wrap = document.createElement("div"); wrap.className = "table-scroll";
        table.before(wrap); wrap.append(table);
      }
    });
    if (identity?.role === "school_admin") {
      $("universityContent").querySelectorAll("[data-university-view]:not([data-university-view=settings]) button").forEach(button => {
        const action = button.dataset.act || "";
        if (action.startsWith("refresh-") || button.dataset.workspaceView) return;
        // Read-only school overview: do not offer write controls to this role.
        button.disabled = true;
        button.title = "校级管理员为只读查看；请由实训管理员维护。";
      });
    }
  }
  async function mount() {
    const kind = identity.universityKind;
    const initial = !controller;
    if (!controller) {
      const path = "/api/training/workspace/template/" + (kind === "student" ? "student" : "staff");
      const response = await fetch(path, {credentials:"same-origin"});
      if (!response.ok) throw new Error(`页面加载失败（${response.status}）`);
      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      // Fixed local templates use the shared controllers and their request lifecycle.
      const main = parsed.querySelector("main"), app = main.querySelector("#app-view");
      const helpers = document.createElement("div"); helpers.hidden = true;
      helpers.append(parsed.querySelector("header"));
      $("universityContent").replaceChildren(helpers, app);
      const information = element("section", "sheet");
      information.dataset.universityView = "settings";
      information.append(element("h2", null, "个人信息"));
      const fields = element("dl", "settings-account-fields");
      for (const [label, value, id] of [["登录账号", identity.username || identity.email], ["姓名", identity.display_name], ["身份", labels[identity.role]], ["所属高校", "待读取", "universityPersonalSchool"], [identity.universityKind === "student" ? "所在班级" : "任教班级", "待读取", "universityPersonalClasses"]]) {
        const field = element("div");
        const valueNode = element("dd", null, value || "—");
        if (id) valueNode.id = id;
        field.append(element("dt", null, label), valueNode);
        fields.append(field);
      }
      information.append(fields);
      app.prepend(information);
      const modal = parsed.getElementById("pass-modal");
      if (modal) $("universityContent").append(modal);
      const root = $("universityContent");
      const scoped = {
        getElementById:id=>root.querySelector("#"+CSS.escape(id)),
        querySelector:selector=>root.querySelector(selector),
        querySelectorAll:selector=>root.querySelectorAll(selector),
        createElement:tag=>document.createElement(tag), body:document.body
      };
      controller = (kind === "student" ? window.createTrainingStudent : window.createTrainingStaff)(scoped, {embedded:true});
      if (kind === "student") {
        $("workspace-change-password")?.addEventListener("click", () => scoped.getElementById("change-pass-btn").click());
        for (const id of ["wb-body", "pf-list", "pr-wrong"]) scoped.getElementById(id)?.addEventListener("click", event => {
          const action = event.target.closest("button[data-act]")?.dataset.act;
          if (action === "workspace-goals") choose("goals");
          if (["wb-resume", "wb-practice", "history-redo", "history-new", "redo-new", "redo-same"].includes(action)) choose("practice");
        });
      }
    }
    if (initial) {
      try { await controller.ready; }
      catch (error) { controller.destroy(); controller = null; throw error; }
    } else if (controller.refresh) await controller.refresh();
    if (!identity.legacy_teaching) {
      $("universityContent").querySelectorAll("[data-legacy-members], #teacher-import-btn").forEach(node => node.hidden = true);
    }
    const members = $("universityContent").querySelector("#school-membership");
    if (members && identity.role === "school_admin") members.hidden = true;
    decorate(); applyView();
  }
  async function ensureNative() {
    if(loading)return loading;
    loading=mount();
    try { await loading; } finally { loading=null; }
  }
  async function refresh() {
    if(!identity)return;
    const ticket=++viewRevision,view=selected;
    $("universityRefresh").disabled=true;status("正在读取教学数据…");
    try {
      if(identity.role==="unlinked")throw new Error("高校账号尚未关联教学身份，请联系平台管理员核对。当前不会进入企业业务面板。");
      if(["home","archives"].includes(view)){
        const data=await request("/api/training/workspace/dashboard");if(ticket!==viewRevision)return false;renderOverview(data,view==="archives");
        if(view==="home"&&usesNative(view))await ensureNative();
      }
      else if(view==="knowledge"){await loadKnowledge({throwOnError:true});}
      else {
        if(nativeViews.has(view)){
          if(view === "settings") {
            const [data] = await Promise.all([request("/api/training/workspace/dashboard"), ensureNative()]);
            if(ticket!==viewRevision)return false;
            $("universityPersonalSchool").textContent = data.school || "—";
            $("universityPersonalClasses").textContent = data.classes.map(classroom => classroom.name).join("、") || (identity.universityKind === "student" ? "尚未加入教学班级" : "暂无负责的教学班级");
          } else await ensureNative();
          if(ticket!==viewRevision)return false;
          if(view==="courses")await loadCourseBindings();
        }
        if(classroomViews.has(view)&&classroomCapable()){
          $("teacherBox").style.display=identity.role==="student"?"none":"block";
          await loadAssignments({throwOnError:true});
        } else if(view==="tasks"&&!classroomCapable())throw new Error("该教学档案尚未绑定课堂账号，请由管理员关联后使用课堂任务。已有考证练习仍可正常使用。");
      }
      if(ticket!==viewRevision)return false;applyView();
      status("数据已更新 · "+new Date().toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"}));
      return true;
    }catch(error){if(ticket===viewRevision)status("加载失败："+error.message+" 请重新加载。",true);return false;}
    finally{if(ticket===viewRevision)$("universityRefresh").disabled=false;}
  }
  function enter(user) {
    identity = user;
    $("universityPanel").dataset.kind=user.universityKind;
    if(!overview)setupPanels();
    document.body.classList.add("workspace-ready", "university-ready");
    $("authScreen").style.display = "none";
    for (const id of ["appMain", "appHeader", "appFooter"]) $(id).style.display = "block";
    $("nav").style.display = "flex"; $("nav").setAttribute("aria-label", "高校工作空间");
    document.querySelectorAll("#appMain > .wrap > .panel").forEach(p => p.classList.toggle("active", p.id === "universityPanel"));
    document.querySelectorAll("#nav [data-panel]").forEach(b => b.style.display = "none");
    const account = document.querySelector('#nav [data-panel="userSettingsPanel"]');
    account.style.display = "";
    account.setAttribute("aria-label", "个人信息"); account.title = "个人信息";
    account.querySelector(".account-label").textContent = "个人信息";
    document.querySelector("#nav .nav-caption").textContent = "高校工作空间";
    document.title = "税海拾珠 · 高校工作空间";
    $("currentUser").textContent = user.display_name + " · " + labels[user.role];
    $("appFooter").querySelector(".wrap").textContent = "教学内容仅供学习与训练，仿真案例不含真实企业材料。学习建议与 AI 解读不替代标准答案和教师复核。";
    $("universityEyebrow").textContent = user.college || "高校工作空间";
    $("universitySubtitle").textContent = user.display_name + " · " + labels[user.role] + "｜" + (user.universityKind === "student" ? "围绕目标安排学习，在练习与复习中积累自己的记录。" : "从班级、课程到教学内容，在同一工作空间开展教学。");
    const nav = $("universityNav"); nav.hidden = false; nav.replaceChildren();
    for (const [key, title] of viewsFor(user.universityKind, user.role).filter(([key]) => key !== "settings")) {
      const b = document.createElement("button"); b.type = "button"; b.dataset.universityView = key; b.title = title; b.setAttribute("aria-label", title);
      // Reuse the main workspace's icon sizing and label behavior.
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("class", "nav-icon"); icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("aria-hidden", "true");
      const path = document.createElementNS(icon.namespaceURI, "path");
      const shapes = {home:"M3 11 12 3l9 8v10h-7v-7h-4v7H3z", goals:"M4 3h2v2h14l-3 5 3 5H6v7H4z", practice:"M4 3h16v19H4zm4 4v2h8V7zm0 5v2h8v-2zm0 5v2h5v-2z", profile:"M3 3h18v18H3zm4 13v2h2v-2zm4-5v7h2v-7zm4-5v12h2V6z", students:"M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM4 21v-2a8 6 0 0 1 16 0v2z", courses:"M3 3h8v18H3zm10 0h8v18h-8zM5 6v2h4V6zm10 0v2h4V6z", tasks:"M5 3h14v19H5zm3 4v2h8V7zm0 5v2h8v-2zm0 5v2h5v-2z", statistics:"M3 20h18v2H3zm2-9h3v7H5zm5-5h3v12h-3zm5-4h3v16h-3z", knowledge:"M11 2h2v5h-2zM2 16h5v2H2zm15 0h5v2h-5zM8 7h8v8H8zM4 19h4v4H4zm12 0h4v4h-4z", content:"M2 4h8l2 2 2-2h8v17h-8l-2 2-2-2H2zm3 3v2h4V7zm10 0v2h4V7z", catalog:"M2 4h8l2 2 2-2h8v17h-8l-2 2-2-2H2zm3 3v2h4V7zm10 0v2h4V7z", archives:"M2 3h20v5H2zm2 7h16v11H4zm5 3v2h6v-2z", wrongs:"M5 3h14v19H5zm3 5 3 3-3 3 2 2 3-3 3 3 2-2-3-3 3-3-2-2-3 3-3-3z", settings:"M3 5h18v2H3zm0 6h18v2H3zm0 6h18v2H3zM7 3h3v6H7zm7 6h3v6h-3zm-7 6h3v6H7z"};
      path.setAttribute("d", shapes[key] || "M3 4h8v7H3zm10 0h8v7h-8zM3 13h8v7H3zm10 0h8v7h-8z"); icon.append(path);
      const label = document.createElement("span"); label.className = "nav-label"; label.textContent = title;
      b.append(icon, label); b.addEventListener("click", () => choose(key)); nav.append(b);
    }
    selected = new URLSearchParams(location.hash.slice(1)).get("view") || "home";
    new MutationObserver(decorate).observe($("universityPanel"), {childList:true, subtree:true});
    choose(selected);
  }
  function register(kind, value) { if (identity?.universityKind === kind) controller = value; }
  function expired(message = "登录状态已失效，请重新登录。") {
    location.replace(`/?workspace=${identity.universityKind}&notice=${encodeURIComponent(message)}`);
  }
  async function logout() {
    await request("/api/logout", {method:"POST"});
    location.replace(`/?workspace=${identity.universityKind}`);
  }
  $("loginUser").addEventListener("input", () => {
    delete $("loginCollege").dataset.needed;
    $("loginCollege").value = "";
    updateLogin();
  });
  $("universityRefresh").addEventListener("click", refresh);
  $("universityRetry").addEventListener("click", refresh);
  function notify(message, error = false) {
    status(message, error);
    $("universityRetry").hidden = true;
    decorate();
    if(selected==="courses"&&message.includes("课程"))loadCourseBindings().catch(error=>status(error.message,true));
  }
  return {resolveIdentity, viewsFor, updateLogin, enter, choose, register, expired, logout, notify};
})();
