"use strict";
window.createTrainingStaff = function(document = window.document, {embedded=false} = {}) {
let disposed=false;const requests=new AbortController();
/* controller body */
/* 教师工作台：选用公共教学资源、管理课程与班级。 */
"use strict";

const state = {staff:null, certificates:[], currentId:null, kps:[], students:[], classes:[], classHistory:[], courses:[], teachers:[], departments:[], terms:[], orgClasses:[], quota:null, units:[], cases:[], trialFeedback:[], trialStats:null};


function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function schoolName(college = state.staff?.college) {
  return college === state.staff?.college ? (state.staff?.college_name || college || "") : (college || "");
}

async function api(path, options = {}) {
  if(disposed)throw new DOMException("视图已关闭","AbortError");
  const form = options.form instanceof FormData ? options.form : null;
  const res = await fetch(path, {
    headers: form ? {} : { "Content-Type": "application/json" },
    ...options,
    signal:requests.signal,
    body: form ? form : (options.body ? JSON.stringify(options.body) : undefined),
  });
  const data = await res.json().catch(() => ({}));
  if(disposed)throw new DOMException("视图已关闭","AbortError");
  if (!res.ok) {const error=new Error(data.detail || `请求失败（${res.status}）`);error.status=res.status;throw error;}
  return data;
}

function flash(message, isError = false) {
  if(disposed)return;
  if(embedded){window.universityWorkspace?.notify(message,isError);return;}
  const box = document.getElementById(isError ? "app-error" : "app-error");
  box.textContent = message || "";
  if (message) setTimeout(() => { if (box.textContent === message) box.textContent = ""; }, 5000);
}

function renderStudentImport(data) {
  const skippedNote = data.skipped.length
    ? `；跳过 ${data.skipped.length} 行：` + data.skipped.map((s) => `${s.student_no || `第${s.line}行`}（${s.reason}）`).join("；")
    : "";
  document.getElementById("student-import-result").innerHTML =
    `<p class="ok">导入完成：新增 ${data.created.length} 人${esc(skippedNote)}</p>`;
}

function current() {
  return state.certificates.find((c) => c.id === state.currentId) || null;
}

function countdownBadge(cd) {
  if (!cd) return "";
  if (cd.expired) return `<span class="badge bad">已过期</span>`;
  if (cd.days_left === 0) return `<span class="badge warn">今天</span>`;
  return `<span class="badge">${cd.days_left} 天</span>`;
}

/* ---------- 渲染 ---------- */

function renderCertSelect() {
  const select = document.getElementById("cert-select");
  select.innerHTML = state.certificates.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("") || '<option value="">暂无可用资源</option>';
  if (state.currentId) select.value = state.currentId;
}

function renderCertForm() {
  const cert = current();
  const box = document.getElementById("cert-summary");
  if (!cert) { box.innerHTML = '<p class="muted">暂无可用教学资源。</p>'; return; }
  let source = "";
  try {
    const url = new URL(cert.source_ref);
    if (["https:", "http:"].includes(url.protocol)) source = `<p><a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">查看发布来源</a></p>`;
  } catch { /* Unstructured maintenance notes stay out of the resource summary. */ }
  box.innerHTML = `<h3>${esc(cert.name)}</h3><p>${esc((cert.subjects || []).join("、"))}</p>${cert.description ? `<p class="muted">${esc(cert.description)}</p>` : ""}${source}`;
}

function renderDates() {
  const dates = current()?.exam_dates || [];
  document.getElementById("dates").innerHTML = dates.length ? dates.map(d =>
    `<div class="item"><strong>${esc(d.exam_date)}</strong> <span class="badge">${d.date_type === "official" ? "官方考试日" : "预计考试日"}</span> ${esc(d.round_label || "")} ${countdownBadge(d.countdown)}</div>`
  ).join("") : '<p class="muted">暂无已发布的考试安排。</p>';
}

function kpDepth(kp, byId) {
  let depth = 0, cur = kp.parent_id;
  while (cur && depth < 16) { depth += 1; cur = byId[cur] ? byId[cur].parent_id : null; }
  return depth;
}

function renderKps() {
  const box = document.getElementById("kps");
  if (!state.kps.length) { box.innerHTML = '<p class="muted">当前资源暂无实训知识点。</p>'; return; }
  const byId = Object.fromEntries(state.kps.map(k => [k.id, k]));
  box.innerHTML = state.kps.map(k => `<div class="item" style="margin-left:${kpDepth(k, byId) * 22}px">
    <div><strong>${esc(k.name)}</strong> ${k.subject ? `<span class="chip">${esc(k.subject)}</span>` : ""}</div>
    ${k.description ? `<p class="muted" style="white-space:pre-line">${esc(k.description)}</p>` : ""}
  </div>`).join("");
}

function renderAll() {
  renderCertSelect(); renderCertForm(); renderDates(); renderKps();
}

/* ---- B2：教学单元与实操案例的加载与渲染 ---- */

async function loadTeachingUnits() {
  if (!state.currentId) { state.units = []; renderTeachingUnits(); return; }
  const data = await api(`/api/training/teaching/units?certificate_id=${encodeURIComponent(state.currentId)}`);
  state.units = data.units;
  renderTeachingUnits();
}

function renderTeachingUnits() {
  const box = document.getElementById("teaching-units");
  if (!box) return;
  const rows = (state.units || []).filter(u => u.status === "published");
  if (!rows.length) {
    box.innerHTML = '<p class="muted">当前资源暂无已发布的教学单元。</p>';
    return;
  }
  box.innerHTML = rows.map((u) => `<div class="data-item">
    <div><b>${esc(u.name)}</b>
      <div class="muted">${esc(u.stage_label)} · 知识点 ${u.knowledge_point_ids.length} 个</div>
      ${u.standard ? `<div>${esc(u.standard)}</div>` : ""}
      ${u.deliverables ? `<div class="muted">要交的证据：${esc(u.deliverables)}</div>` : ""}
    </div>
    <span class="chip">${esc(u.status === "published" ? "已发布" : u.status === "archived" ? "已归档" : "草稿")}</span>
  </div>`).join("");
}

async function loadTeachingCases() {
  if (!state.currentId) { state.cases = []; renderTeachingCases(); return; }
  const data = await api(`/api/training/teaching/cases?certificate_id=${encodeURIComponent(state.currentId)}`);
  state.cases = data.cases;
  renderTeachingCases();
}

function renderTeachingCases() {
  const box = document.getElementById("teaching-cases");
  if (!box) return;
  const rows = (state.cases || []).filter(c => c.status === "published");
  if (!rows.length) {
    box.innerHTML = '<p class="muted">当前资源暂无已发布的实操案例。</p>';
    return;
  }
  box.innerHTML = rows.map((c) => `<div class="data-item">
    <div><b>${esc(c.name)}</b>
      <div class="muted">材料 ${c.materials.length} 份</div>
      ${c.scenario ? `<div>${esc(c.scenario)}</div>` : ""}
      ${c.desensitization_note ? `<div class="muted">${esc(c.desensitization_note)}</div>` : ""}
    </div>
    <span class="chip">${esc(c.status === "published" ? "已发布" : c.status === "archived" ? "已归档" : "草稿")}</span>
  </div>`).join("");
}

async function loadTrial() {
  const [fb, stats] = await Promise.all([
    api("/api/training/staff/trial-feedback"),
    api("/api/training/staff/trial-stats"),
  ]);
  state.trialFeedback = fb.feedback;
  state.trialStats = stats.stats;
  renderTrial();
}

function renderTrial() {
  const box = document.getElementById("trial-stats");
  const list = document.getElementById("trial-feedback");
  const s = state.trialStats;
  box.innerHTML = s ? `<div class="row" style="gap:10px;flex-wrap:wrap;">
    <div class="chip">建档学生 ${s.students_total}</div>
    <div class="chip">登录过 ${s.students_logged_in}</div>
    <div class="chip">近 7 日活跃 ${s.students_active_7d}</div>
    <div class="chip">作答 ${s.attempts_total} 次（满分 ${s.attempts_perfect}）</div>
    <div class="chip">题库题作答 ${s.attempts_bank} 次</div>
    <div class="chip">覆盖知识点 ${s.kps_covered}</div>
    <div class="chip">反馈 ${s.feedback.total}（新 ${s.feedback.new} / 处理中 ${s.feedback.processing} / 已处理 ${s.feedback.resolved}）</div>
  </div>` : `<p class="muted">暂无盘面数据。</p>`;
  const rows = state.trialFeedback || [];
  const STATUS = { new: "新反馈", processing: "处理中", resolved: "已处理" };
  const CATEGORY = { bug: "缺陷", suggestion: "建议", content: "内容纠错", other: "其他" };
  list.innerHTML = rows.length ? rows.map((f) => `<div class="item">
    <div class="item-head">
      <div><span class="badge ${f.status === "resolved" ? "" : "warn"}">${STATUS[f.status] || esc(f.status)}</span>
        <span class="chip">${CATEGORY[f.category] || esc(f.category)}</span>
        <span class="muted">${esc(f.student ? `${f.student.student_no} ${f.student.name}` : "")}</span>
        <span class="muted">${esc((f.created_at || "").slice(0, 16).replace("T", " "))}</span></div>
    </div>
    <div style="margin-top:4px;">${esc(f.content)}</div>
    <div class="row" style="margin-top:6px;">
      <div><label>处理状态</label>
        <select id="fb-status-${esc(f.id)}">
          ${Object.entries(STATUS).map(([k, v]) => `<option value="${k}"${f.status === k ? " selected" : ""}>${v}</option>`).join("")}
        </select></div>
      <div><label>处理备注</label><input id="fb-note-${esc(f.id)}" value="${esc(f.staff_note || "")}" placeholder="如 已定位，下个版本修复"></div>
      <div class="actions" style="align-self:flex-end;">
        <button class="small primary" data-act="feedback-save" data-id="${esc(f.id)}">保存处理</button></div>
    </div>
    ${f.handled_at ? `<div class="muted">最近处理：${esc((f.handled_at || "").slice(0, 16).replace("T", " "))}</div>` : ""}
  </div>`).join("") : `<p class="muted">暂无学生反馈。</p>`;
}

/* ---------- 数据 ---------- */

let membershipUI=null;
async function load() {
  if (!membershipUI && state.staff && state.staff.role !== "school_admin" && document.getElementById("school-membership")) {
    const id = state.staff.id;
    membershipUI = TPMemberLinks.school(document.getElementById("school-membership"), {request:api, valid:()=>!disposed && state.staff?.id===id, afterChange:loadStudents});
  }
  state.certificates = (await api("/api/training/staff/certificates?include_inactive=false")).certificates;
  if (!state.certificates.some(c => c.id === state.currentId)) state.currentId = state.certificates[0]?.id || null;
  state.kps = state.currentId ? (await api(`/api/training/staff/certificates/${state.currentId}/knowledge-points?include_inactive=false`)).knowledge_points : [];
  renderAll();
  await loadTeachingUnits();
  await loadTeachingCases();
  await loadStudents();
  await loadCourses();
  if (state.staff && ["admin", "school_admin"].includes(state.staff.role)) {
    document.getElementById("overview-card").hidden = false;
    await loadOverview();
  }
  await loadRoster();
  await loadOrg();
  await loadTrial();
  renderStaffWorkbench();
}

/* ---------- 班级与教师名册（FR-U03） ---------- */

async function loadRoster() {
  const college = state.staff ? state.staff.college : (document.getElementById("cm-college")?.value.trim() || "");
  if (!college) { renderRoster(); return; }
  state.classes = (await api(`/api/training/staff/classes?college=${encodeURIComponent(college)}`)).classes;
  state.classHistory = (await api(`/api/training/staff/class-history?college=${encodeURIComponent(college)}&limit=20`)).history;
  renderRoster();
}

function renderRoster() {
  const box = document.getElementById("classes");
  const hist = document.getElementById("class-history");
  const names = document.getElementById("class-names");
  const classes = state.classes || [];
  names.innerHTML = classes.map((c) => `<option value="${esc(c.class_name)}">`).join("");
  if (!classes.length) {
    box.innerHTML = `<p class="muted">该院校暂无班级数据（学生名册「班级」字段聚合）。</p>`;
  } else {
    box.innerHTML = `<table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;">班级</th><th style="text-align:left;">在读</th><th style="text-align:left;">含停用</th></tr>
      ${classes.map((c) => `<tr><td>${esc(c.class_name)}</td><td>${c.active_students}</td><td>${c.students}</td></tr>`).join("")}
    </table>`;
  }
  const h = state.classHistory || [];
  if (!h.length) {
    hist.innerHTML = `<p class="muted">暂无班级变更记录。</p>`;
  } else {
    hist.innerHTML = h.map((r) => `<div class="item"><div class="item-head">
      <div>${esc(r.student_no)} ${esc(r.name)}：${esc(r.from_class || "（未分班）")} → <strong>${esc(r.to_class || "（未分班）")}</strong></div>
      <span class="muted">${esc((r.created_at || "").slice(0, 16).replace("T", " "))}${r.changed_by_name ? ` · 操作人 ${esc(r.changed_by_name)}` : ""}</span>
    </div></div>`).join("");
  }
}

function parseTeacherRows(text) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split(/[，,]/).map((p) => p.trim());
    return { username: parts[0] || "", display_name: parts[1] || "",
             email: parts[2] || null, phone: null };
  }).filter((r) => r.username);
}

/* ---------- 组织架构（FR-U01）：院系 / 学期 / 班级实体 ---------- */

async function loadOrg() {
  const college = state.staff ? state.staff.college : "";
  if (!college) { renderOrg(); return; }
  const q = encodeURIComponent(college);
  state.departments = (await api(`/api/training/staff/org/departments?college=${q}`)).departments;
  state.majors = (await api(`/api/training/staff/org/majors?college=${q}`)).majors;
  state.terms = (await api(`/api/training/staff/org/terms?college=${q}`)).terms;
  state.orgClasses = (await api(`/api/training/staff/org/classes?college=${q}`)).classes;
  state.quota = (await api(`/api/training/staff/org/quota?college=${q}`)).quota;
  renderOrg();
}

function orgBadge(archived) {
  return archived ? ` <span class="badge warn">已归档</span>` : "";
}

function renderOrg() {
  const deps = state.departments || [];
  const majors = state.majors || [];
  const terms = state.terms || [];
  const classes = state.orgClasses || [];
  const isAdmin = state.staff && state.staff.role === "admin";
  document.getElementById("org-dep-form").hidden = !isAdmin;
  document.getElementById("org-major-form").hidden = !isAdmin;
  document.getElementById("org-term-form").hidden = !isAdmin;
  document.getElementById("org-class-form").hidden = !isAdmin;
  document.getElementById("org-departments").innerHTML = deps.length
    ? `<table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;">院系</th><th style="text-align:left;">班级数</th><th style="text-align:left;"></th></tr>
      ${deps.map((d) => `<tr><td>${esc(d.name)}${orgBadge(d.archived)}</td><td>${d.classes}</td>
        <td>${isAdmin ? `<button class="small" data-act="org-dep-toggle" data-id="${esc(d.id)}" data-v="${d.archived ? 0 : 1}">${d.archived ? "恢复" : "归档"}</button>
        <button class="small" data-act="org-dep-delete" data-id="${esc(d.id)}">删除</button>` : ""}</td></tr>`).join("")}
    </table>`
    : `<p class="muted">暂无院系${isAdmin ? "，可在上方创建" : ""}。</p>`;
  document.getElementById("org-majors").innerHTML = majors.length
    ? `<table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;">专业</th><th style="text-align:left;">所属院系</th><th style="text-align:left;">班级数</th><th style="text-align:left;"></th></tr>
      ${majors.map((m) => `<tr><td>${esc(m.name)}${orgBadge(m.archived)}</td>
        <td>${esc(m.department_name || "—")}</td><td>${m.classes}</td>
        <td>${isAdmin ? `<button class="small" data-act="org-major-toggle" data-id="${esc(m.id)}" data-v="${m.archived ? 0 : 1}">${m.archived ? "恢复" : "归档"}</button>
        <button class="small" data-act="org-major-delete" data-id="${esc(m.id)}">删除</button>` : ""}</td></tr>`).join("")}
    </table>`
    : `<p class="muted">暂无专业${isAdmin ? "，可在上方创建（专业可选挂院系，班级可再挂专业）" : ""}。</p>`;
  document.getElementById("org-terms").innerHTML = terms.length
    ? `<table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;">学期</th><th style="text-align:left;">起止</th><th style="text-align:left;">班级数</th><th style="text-align:left;"></th></tr>
      ${terms.map((t) => `<tr><td>${esc(t.name)}${t.is_current ? ` <span class="badge">当前</span>` : ""}${orgBadge(t.archived)}</td>
        <td class="muted">${esc(t.start_date || "—")} ~ ${esc(t.end_date || "—")}</td><td>${t.classes}</td>
        <td>${isAdmin ? `<button class="small" data-act="org-term-current" data-id="${esc(t.id)}" ${t.is_current ? "disabled" : ""}>设为当前</button>
        <button class="small" data-act="org-term-snapshot" data-id="${esc(t.id)}">生成快照</button>
        <button class="small" data-act="org-term-snapshot-view" data-id="${esc(t.id)}">查看快照</button>
        <button class="small" data-act="org-term-toggle" data-id="${esc(t.id)}" data-v="${t.archived ? 0 : 1}">${t.archived ? "恢复" : "归档"}</button>
        <button class="small" data-act="org-term-delete" data-id="${esc(t.id)}">删除</button>` : `<button class="small" data-act="org-term-snapshot-view" data-id="${esc(t.id)}">查看快照</button>`}</td></tr>`).join("")}
    </table>`
    : `<p class="muted">暂无学期${isAdmin ? "，可在上方创建" : ""}。</p>`;
  document.getElementById("org-classes").innerHTML = classes.length
    ? `<table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;">班级</th><th style="text-align:left;">院系</th><th style="text-align:left;">专业</th><th style="text-align:left;">学期</th><th style="text-align:left;">在读</th><th style="text-align:left;"></th></tr>
      ${classes.map((c) => `<tr><td>${esc(c.name)}${orgBadge(c.archived)}</td>
        <td>${esc(c.department_name || "—")}</td><td>${esc(c.major_name || "—")}</td><td>${esc(c.term_name || "—")}</td><td>${c.active_students}</td>
        <td>${isAdmin ? `<button class="small" data-act="org-class-clone" data-id="${esc(c.id)}">按学期重建</button>
        <button class="small" data-act="org-class-toggle" data-id="${esc(c.id)}" data-v="${c.archived ? 0 : 1}">${c.archived ? "恢复" : "归档"}</button>
        <button class="small" data-act="org-class-delete" data-id="${esc(c.id)}">删除</button>` : ""}</td></tr>`).join("")}
    </table>`
    : `<p class="muted">暂无班级实体。学生名册「班级」字段照常聚合展示于上方「班级与教师名册」，不受影响。</p>`;
  document.getElementById("oc-department").innerHTML = `<option value="">（未绑定院系）</option>` +
    deps.filter((d) => !d.archived).map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
  document.getElementById("om-department").innerHTML = `<option value="">（未挂院系）</option>` +
    deps.filter((d) => !d.archived).map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
  document.getElementById("oc-major").innerHTML = `<option value="">（未绑定专业）</option>` +
    majors.filter((m) => !m.archived).map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("");
  document.getElementById("oc-term").innerHTML = `<option value="">（未绑定学期）</option>` +
    terms.filter((t) => !t.archived).map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  const quota = state.quota;
  if (quota) {
    document.getElementById("org-quota").innerHTML =
      `<p>教师 <strong>${quota.teachers_used}</strong>/${quota.teacher_quota} · 学生 <strong>${quota.students_used}</strong>/${quota.student_quota}${quota.configured ? "" : "（默认档，未单独配置）"}</p>`;

    const hist = quota.history || [];
    document.getElementById("org-quota-history").innerHTML = hist.length
      ? `<h3 style="margin-top:12px;">调整留痕（最近 ${hist.length} 条）</h3>` + hist.map((h) =>
        `<div class="item"><div class="item-head"><div>教师 ${esc(h.old_teacher_quota ?? "—")}→${h.new_teacher_quota} · 学生 ${esc(h.old_student_quota ?? "—")}→${h.new_student_quota}</div>
        <span class="muted">${esc((h.created_at || "").slice(0, 16).replace("T", " "))}${h.changed_by_name ? ` · ${esc(h.changed_by_name)}` : ""}</span></div></div>`).join("")
      : "";
  }
}

/* ---------- 学生管理 ---------- */

async function loadStudents() {
  state.students = (await api("/api/training/staff/students")).students;
  renderStudents();
}

function renderStudents() {
  const box = document.getElementById("students");
  if (!state.students.length) {
    box.innerHTML = `<p class="muted">还没有学生。点上方「添加学生」或「批量导入」。</p>`;
    return;
  }
  box.innerHTML = state.students.map((s) => `
    <div class="item">
      <div class="item-head">
        <div><strong>${esc(s.name)}</strong> <span class="muted">学号 ${esc(s.student_no)}</span>
          <span class="chip">${esc(schoolName(s.college))}</span>
          ${s.class_name ? `<span class="chip">${esc(s.class_name)}</span>` : ""}
          ${s.active ? `<span class="badge">启用中</span>` : `<span class="badge gray">已停用</span>`}
        </div>
        <div class="actions">
          <button class="small" data-act="reset-student-pass" data-id="${esc(s.id)}" data-no="${esc(s.student_no)}">重置密码</button>
          <button class="small ${s.active ? "danger" : ""}" data-act="toggle-student" data-id="${esc(s.id)}">${s.active ? "停用" : "启用"}</button>
        </div>
      </div>
    </div>`).join("");
}

function parseStudentRows(text) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split(/[，,]/).map((p) => p.trim());
    return { student_no: parts[0] || "", name: parts[1] || "", class_name: parts[2] || null };
  }).filter((r) => r.student_no && r.name);
}

/* ---------- 课程管理（FR-U04） ---------- */

async function loadCourses() {
  state.courses = (await api("/api/training/staff/courses")).courses;
  state.teachers = (await api("/api/training/staff/teachers")).teachers;
  renderCourses();
}

function teacherOptions(selectedId) {
  return state.teachers.map(t => {
    const name = (t.display_name || t.username || "").trim();
    const duplicate = state.teachers.some(other => other.id !== t.id
      && (other.display_name || other.username || "").trim() === name);
    const label = duplicate ? `${name}（${t.username}）` : name;
    return `<option value="${esc(t.id)}" ${t.id === selectedId ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
}

function renderCourses() {
  const box = document.getElementById("courses");
  if (!state.courses.length) {
    box.innerHTML = `<p class="muted">还没有课程。点上方「新建课程」。</p>`;
    return;
  }
  box.innerHTML = state.courses.map((c) => `
    <div class="item" id="course-${esc(c.id)}">
      <div class="item-head">
        <div><strong>${esc(c.name)}</strong>
          ${c.semester ? `<span class="chip">${esc(c.semester)}</span>` : ""}
          ${c.teacher_name ? `<span class="chip">授课：${esc(c.teacher_name)}</span>` : `<span class="badge gray">未指派教师</span>`}
          ${c.active ? `<span class="badge">开设中</span>` : `<span class="badge gray">已停用</span>`}
        </div>
        <div class="actions">
          <button class="small" data-act="course-roster" data-id="${esc(c.id)}">花名册（${c.student_count}）</button>
          <button class="small" data-act="edit-course" data-id="${esc(c.id)}">编辑</button>
          <button class="small ${c.active ? "danger" : ""}" data-act="toggle-course" data-id="${esc(c.id)}">${c.active ? "停用" : "启用"}</button>
        </div>
      </div>
      <div class="muted">绑定班级：${c.classes.length ? c.classes.map((n) => `<span class="chip">${esc(n)}</span>`).join("") : "（无）"}</div>
      ${c.description ? `<div class="muted">${esc(c.description)}</div>` : ""}
      <div class="inline-form" data-role="course-roster" hidden></div>
      <div class="inline-form" data-role="course-edit" hidden>
        <div class="row">
          <div><label>课程名称</label><input data-role="ce-name" value="${esc(c.name)}"></div>
          <div><label>学年学期</label><input data-role="ce-semester" value="${esc(c.semester || "")}"></div>
          <div><label>授课教师</label><select data-role="ce-teacher">${teacherOptions(c.teacher_id)}</select></div>
        </div>
        <div class="row">
          <div><label>绑定班级（逗号分隔）</label><input data-role="ce-classes" value="${esc(c.classes.join(","))}"></div>
          <div><label>课程说明</label><input data-role="ce-desc" value="${esc(c.description || "")}"></div>
        </div>
        <div class="actions"><button class="small primary" data-act="save-course" data-id="${esc(c.id)}">保存</button>
          <button class="small" data-act="cancel-course-edit" data-id="${esc(c.id)}">取消</button></div>
      </div>
    </div>`).join("");
}

function splitClassNames(text) {
  return text.split(/[，,]/).map((p) => p.trim()).filter(Boolean);
}

async function toggleCourseRoster(btn, scope, courseId) {
  const box = scope.querySelector("[data-role=course-roster]");
  if (!box.hidden) { box.hidden = true; btn.textContent = btn.dataset.label || "花名册"; return; }
  btn.dataset.label = btn.textContent;
  box.innerHTML = `<p class="muted">加载中…</p>`;
  box.hidden = false;
  try {
    const data = await api(`/api/training/staff/courses/${courseId}/roster`);
    box.innerHTML = data.students.length
      ? `<div class="muted" style="margin-bottom:4px;">共 ${data.count} 名在读学生：</div>` +
        data.students.map((s) => `<span class="chip">${esc(s.student_no)} ${esc(s.name)}（${esc(s.class_name)}）</span>`).join(" ")
      : `<p class="muted">绑定班级暂无在读学生。</p>`;
    btn.textContent = "收起花名册";
  } catch (err) {
    box.hidden = true;
    flash(err.message, true);
  }
}

/* ---------- 院校教学概况（FR-U05 简化版，管理员） ---------- */

async function loadOverview() {
  const college = state.staff.college;
  if (!college) { document.getElementById("overview-body").innerHTML = `<p class="muted">账号未绑定院校，无法聚合。</p>`; return; }
  const o = (await api(`/api/training/staff/overview?college=${encodeURIComponent(college)}`)).overview;
  const cls = o.classes.length
    ? o.classes.map((c) => `<tr><td>${esc(c.class_name)}</td><td>${c.students}</td><td>${c.active_students}</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">暂无班级</td></tr>`;
  const courses = o.courses.length
    ? o.courses.map((c) => `<div class="item"><div class="item-head">
        <div><strong>${esc(c.name)}</strong>${c.semester ? ` <span class="chip">${esc(c.semester)}</span>` : ""}
          ${c.active ? "" : `<span class="badge gray">已停用</span>`}</div>
        <div class="muted">班级 ${c.class_count} · 学生 ${c.student_count}${c.teacher_name ? ` · ${esc(c.teacher_name)}` : ""}</div>
      </div></div>`).join("")
    : `<p class="muted">暂无课程。</p>`;
  const depts = (o.departments || []).length
    ? `<div class="muted" style="margin:8px 0 4px;">按院系（FR-U01 实体聚合）：</div>` +
      o.departments.map((d) => `<span class="chip">${esc(d.name)}：班级 ${d.classes} · 在读 ${d.students} · 覆盖课程 ${d.courses_covered}</span>`).join(" ")
    : "";
  document.getElementById("overview-body").innerHTML = `
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
      <span class="badge">在读学生 ${o.students.active}/${o.students.total}</span>
      <span class="badge">教职账号 ${o.teachers}</span>
      <span class="badge">班级 ${o.classes.length}</span>
      <span class="badge">课程 ${o.courses.length}</span>
      <span class="badge gray">考证知识点 ${o.content.knowledge_points}（已挂规则 ${o.content.linked_points} · 高频标注 ${o.content.high_freq_marks}）</span>
    </div>
    ${depts}
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin:10px 0;">
      <thead><tr><th style="text-align:left;">班级</th><th>名册</th><th>在读</th></tr></thead>
      <tbody>${cls}</tbody>
    </table>
    ${courses}
    <p class="muted">${esc(o.scope_note.replace(/（FR-U\d+）/g, ""))}</p>`;
}

/* ---------- 事件 ---------- */

function showApp(loggedIn) {
  if(disposed)return;
  if(!loggedIn){membershipUI?.destroy();membershipUI=null;state.staff=null;location.replace("/");return;}
  document.getElementById("app-view").hidden = !loggedIn;
  for (const id of ["sf-college", "si-college", "cf2-college", "cm-college", "ti-college"]) {
    document.getElementById(id).value = loggedIn ? schoolName() : "";
  }
  document.getElementById("teacher-import-btn").hidden = !(loggedIn && state.staff && state.staff.role === "admin");
  document.getElementById("who").innerHTML = loggedIn
    ? `${esc(state.staff.display_name)}（${esc(({admin:"高校管理员",school_admin:"高校管理员",teacher:"教师"})[state.staff.role] || "教学人员")} · ${esc(schoolName())}）
       <button class="small" id="pass-btn">改密码</button>
       <button class="small" id="logout-btn">退出</button>` : "";
  if (loggedIn) {
    document.getElementById("logout-btn").addEventListener("click", async () => {
      await api("/api/training/staff/auth/logout", { method: "POST" });
      showApp(false);
    });
    document.getElementById("pass-btn").addEventListener("click", () => {
      const f = document.getElementById("pass-form");
      f.hidden = !f.hidden;
      if (!f.hidden) f.scrollIntoView({ behavior: "smooth" });
    });
  }
}

async function withCert(mutator) {
  try {
    await mutator();
    await load();
  } catch (err) {
    flash(err.message, true);
  }
}

document.getElementById("app-view").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const id = btn.dataset.id;
  const q = (sel, root) => (root || document).querySelector(sel);

  const handlers = {
    "refresh-trial": () => loadTrial(),
    "feedback-save": async () => {
      try {
        await api(`/api/training/staff/trial-feedback/${id}`, {
          method: "PATCH",
          body: { status: q(`#fb-status-${id}`).value, staff_note: q(`#fb-note-${id}`).value },
        });
        flash("反馈处理已保存。");
        await loadTrial();
      } catch (err) {
        flash(err.message, true);
      }
    },

    "hide-pass-form": () => { q("#pass-form").hidden = true; },
    "change-pass": () => {
      const oldPw = q("#pw-old").value, newPw = q("#pw-new").value, newPw2 = q("#pw-new2").value;
      if (!oldPw || !newPw) { flash("请填写当前密码和新密码。", true); return; }
      if (newPw !== newPw2) { flash("两次输入的新密码不一致。", true); return; }
      (async () => {
        try {
          await api("/api/training/staff/auth/change-password", {
            method: "POST", body: { old_password: oldPw, new_password: newPw } });
          q("#pw-old").value = ""; q("#pw-new").value = ""; q("#pw-new2").value = "";
          q("#pass-form").hidden = true;
          flash("密码已修改，请用新密码重新登录。");
          if (window.universityWorkspace) { window.universityWorkspace.expired("密码已修改，请用新密码重新登录。"); return; }
          showApp(false);
        } catch (err) {
          flash(err.message, true);
        }
      })();
    },
    "show-student-form": () => { q("#student-form").hidden = !q("#student-form").hidden; },
    "show-student-import": () => { q("#student-import-form").hidden = !q("#student-import-form").hidden; },
    "refresh-students": () => withCert(loadStudents),
    "refresh-roster": () => loadRoster().catch((err) => flash(err.message, true)),
    "show-teacher-import": () => { q("#teacher-import-form").hidden = !q("#teacher-import-form").hidden; },
    "migrate-class": async () => {
      const college = state.staff.college;
      const fromClass = q("#cm-from").value.trim();
      const toClass = q("#cm-to").value.trim();
      if (!college || !fromClass || !toClass) { flash("院校、原班级、新班级均必填。", true); return; }
      try {
        const data = (await api("/api/training/staff/classes/migrate", {
          method: "POST",
          body: { college, from_class: fromClass, to_class: toClass },
        })).migration;
        document.getElementById("class-migrate-result").innerHTML =
          `<p class="ok">迁移完成：${data.migrated} 名在读学生已从「${esc(fromClass)}」改入「${esc(toClass)}」，归属变更已写入追溯。</p>`;
        q("#cm-from").value = ""; q("#cm-to").value = "";
        await loadRoster();
      } catch (err) {
        flash(err.message, true);
      }
    },
    "import-teachers": async () => {
      const rows = parseTeacherRows(q("#ti-text").value);
      if (!rows.length) { flash("没有可导入的行。格式：登录名,姓名,邮箱可选", true); return; }
      const college = state.staff.college;
      const defaultPassword = q("#ti-pass").value;
      if (!college || !defaultPassword) { flash("院校和统一初始密码必填。", true); return; }
      try {
        const data = (await api("/api/training/staff/teachers/import", {
          method: "POST",
          body: { college, default_password: defaultPassword, rows },
        })).import;
        const skippedNote = data.skipped.length
          ? `；跳过 ${data.skipped.length} 行：` + data.skipped.map((s) => `${s.username || `第${s.line}行`}（${s.reason}）`).join("；")
          : "";
        document.getElementById("teacher-import-result").innerHTML =
          `<p class="ok">导入完成：新增 ${data.created.length} 个教师账号${esc(skippedNote)}</p>`;
        q("#ti-text").value = ""; q("#ti-pass").value = "";
      } catch (err) {
        flash(err.message, true);
      }
    },
    "create-student": () => withCert(async () => {
      await api("/api/training/staff/students", {
        method: "POST",
        body: {
          college: state.staff.college,
          student_no: q("#sf-no").value.trim(),
          name: q("#sf-name").value.trim(),
          class_name: q("#sf-class").value.trim() || null,
          initial_password: q("#sf-pass").value || null,
          email: q("#sf-email").value.trim() || null,
        },
      });
      q("#sf-no").value = ""; q("#sf-name").value = ""; q("#sf-pass").value = ""; q("#sf-email").value = "";
      q("#student-form").hidden = true;
      await loadStudents();
      flash("学生已添加。");
    }),
    "import-students": () => withCert(async () => {
      const rows = parseStudentRows(q("#si-text").value);
      if (!rows.length) { flash("没有可导入的行。格式：学号,姓名,班级", true); return; }
      const college = state.staff.college;
      const defaultPassword = q("#si-pass").value;
      if (!college || !defaultPassword) { flash("院校和统一初始密码必填。", true); return; }
      const result = await api("/api/training/staff/students/import", {
        method: "POST",
        body: { college, default_password: defaultPassword,
                default_class_name: q("#si-class").value.trim() || null, rows },
      });
      renderStudentImport(result.import);
      await loadStudents();
    }),
    "import-students-xlsx": () => withCert(async () => {
      const file = q("#si-file").files && q("#si-file").files[0];
      if (!file) { flash("请选择 .xlsx 名册文件。", true); return; }
      const college = state.staff.college;
      const defaultPassword = q("#si-pass").value;
      if (!college || !defaultPassword) { flash("院校和统一初始密码必填。", true); return; }
      const fd = new FormData();
      fd.append("college", college);
      fd.append("default_password", defaultPassword);
      fd.append("default_class_name", q("#si-class").value.trim());
      fd.append("file", file);
      const result = await api("/api/training/staff/students/import-xlsx", { method: "POST", form: fd });
      renderStudentImport(result.import);
      await loadStudents();
    }),
    "org-dep-create": async () => {
      const name = q("#od-name").value.trim();
      if (!name) { flash("院系名称必填。", true); return; }
      await api("/api/training/staff/org/departments", {
        method: "POST", body: { college: state.staff.college, name } });
      q("#od-name").value = "";
      flash("院系已创建。");
      await loadOrg();
    },
    "org-dep-toggle": async () => {
      await api(`/api/training/staff/org/departments/${id}`, {
        method: "PATCH", body: { archived: id && q(`button[data-act="org-dep-toggle"][data-id="${id}"]`).dataset.v === "1" } });
      await loadOrg();
    },
    "org-dep-delete": async () => {
      try {
        await api(`/api/training/staff/org/departments/${id}`, { method: "DELETE" });
        flash("院系已删除。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-major-create": async () => {
      const name = q("#om-name").value.trim();
      if (!name) { flash("专业名称必填。", true); return; }
      try {
        await api("/api/training/staff/org/majors", {
          method: "POST", body: { college: state.staff.college, name,
                                  department_id: q("#om-department").value || null } });
        q("#om-name").value = "";
        flash("专业已创建。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-major-toggle": async () => {
      try {
        await api(`/api/training/staff/org/majors/${id}`, {
          method: "PATCH", body: { archived: q(`button[data-act="org-major-toggle"][data-id="${id}"]`).dataset.v === "1" } });
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-major-delete": async () => {
      try {
        await api(`/api/training/staff/org/majors/${id}`, { method: "DELETE" });
        flash("专业已删除。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-term-create": async () => {
      const name = q("#ot-name").value.trim();
      if (!name) { flash("学期名称必填（如 2026-2027-1）。", true); return; }
      await api("/api/training/staff/org/terms", {
        method: "POST",
        body: { college: state.staff.college, name,
                start_date: q("#ot-start").value.trim() || null,
                end_date: q("#ot-end").value.trim() || null,
                is_current: q("#ot-current").checked } });
      q("#ot-name").value = ""; q("#ot-start").value = ""; q("#ot-end").value = ""; q("#ot-current").checked = false;
      flash("学期已创建。");
      await loadOrg();
    },
    "org-term-current": async () => {
      await api(`/api/training/staff/org/terms/${id}`, { method: "PATCH", body: { is_current: true } });
      flash("已设为当前学期。");
      await loadOrg();
    },
    "org-term-snapshot": async () => {
      try {
        const data = (await api(`/api/training/staff/org/terms/${id}/roster-snapshot`, { method: "POST" })).snapshot;
        flash(`名册快照已生成：在读 ${data.snapshotted} 人（新增 ${data.inserted} · 更新 ${data.updated} · 移出 ${data.removed}）。`);
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-term-snapshot-view": async () => {
      try {
        const data = (await api(`/api/training/staff/org/terms/${id}/roster-snapshot`)).snapshot;
        const box = document.getElementById("org-snapshot-view");
        if (!data.total) {
          box.innerHTML = `<p class="muted">学期「${esc(data.term_name)}」暂无名册快照，请先点「生成快照」。</p>`;
          return;
        }
        box.innerHTML = `<h3 style="margin-top:12px;">名册快照 · ${esc(data.term_name)}（${data.total} 人，快照时间 ${esc((data.taken_at || "").slice(0, 16).replace("T", " "))}）</h3>` +
          data.classes.map((c) => `<p style="margin:6px 0 2px;"><strong>${esc(c.class_name)}</strong>（${c.students.length} 人）：${c.students.map((s) => esc(s.student_no + " " + s.name)).join("、")}</p>`).join("");
      } catch (err) { flash(err.message, true); }
    },
    "org-term-toggle": async () => {
      await api(`/api/training/staff/org/terms/${id}`, {
        method: "PATCH", body: { archived: q(`button[data-act="org-term-toggle"][data-id="${id}"]`).dataset.v === "1" } });
      await loadOrg();
    },
    "org-term-delete": async () => {
      try {
        await api(`/api/training/staff/org/terms/${id}`, { method: "DELETE" });
        flash("学期已删除。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-class-create": async () => {
      const name = q("#oc-name").value.trim();
      if (!name) { flash("班级名称必填。", true); return; }
      try {
        await api("/api/training/staff/org/classes", {
          method: "POST",
          body: { college: state.staff.college, name,
                  department_id: q("#oc-department").value || null,
                  major_id: q("#oc-major").value || null,
                  term_id: q("#oc-term").value || null } });
        q("#oc-name").value = "";
        flash("班级实体已创建（在读人数按学生名册「班级」字段对齐）。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-class-toggle": async () => {
      await api(`/api/training/staff/org/classes/${id}`, {
        method: "PATCH", body: { archived: q(`button[data-act="org-class-toggle"][data-id="${id}"]`).dataset.v === "1" } });
      await loadOrg();
    },
    "org-class-delete": async () => {
      try {
        await api(`/api/training/staff/org/classes/${id}`, { method: "DELETE" });
        flash("班级实体已删除（学生名册不受影响）。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "org-class-clone": async () => {
      const termName = window.prompt("按学期重建班级：输入目标学期名称（须已存在，如 2026-2027-2）。学生名册不移动，归属调整仍用「班级整建制迁移」。");
      if (!termName) return;
      const term = (state.terms || []).find((t) => t.name === termName.trim());
      if (!term) { flash("未找到该学期，请先在学期区创建。", true); return; }
      try {
        await api(`/api/training/staff/org/classes/${id}/clone`, {
          method: "POST", body: { term_id: term.id } });
        flash("已按学期重建班级实体。");
      } catch (err) { flash(err.message, true); }
      await loadOrg();
    },
    "reset-student-pass": () => {
      const stu = state.students.find((s) => s.id === id);
      const input = window.prompt(`为 ${stu ? stu.student_no + " " + stu.name : "该学生"} 设置新密码（10–128 位，大小写字母、数字、符号至少三类，不能使用常见弱口令或包含邮箱前缀；重置后其登录立即失效）：`);
      if (input === null) return;
      withCert(async () => {
        await api(`/api/training/staff/students/${id}/reset-password`, {
          method: "POST", body: { new_password: input } });
        flash("密码已重置，该学生的旧登录已全部失效。");
      });
    },
    "toggle-student": () => {
      const stu = state.students.find((s) => s.id === id);
      if (!stu) return;
      if (stu.active && !window.confirm(`确认停用 ${stu.student_no} ${stu.name}？停用后立即无法登录。`)) return;
      withCert(async () => {
        await api(`/api/training/staff/students/${id}/toggle`, {
          method: "POST", body: { active: !stu.active } });
        flash(stu.active ? "已停用。" : "已启用。");
      });
    },
    "show-course-form": () => {
      const f = q("#course-form");
      f.hidden = !f.hidden;
      if (!f.hidden) {
        const sel = q("#cf2-teacher");
        sel.innerHTML = `<option value="">（未指派）</option>` + teacherOptions(null);
      }
    },
    "cancel-course-form": () => { q("#course-form").hidden = true; },
    "refresh-courses": () => withCert(loadCourses),
    "refresh-overview": () => withCert(loadOverview),
    "create-course": () => withCert(async () => {
      await api("/api/training/staff/courses", {
        method: "POST",
        body: {
          name: q("#cf2-name").value.trim(),
          college: state.staff.college,
          semester: q("#cf2-semester").value.trim() || null,
          teacher_id: q("#cf2-teacher").value || null,
          class_names: splitClassNames(q("#cf2-classes").value),
          description: q("#cf2-desc").value.trim() || null,
        },
      });
      q("#cf2-name").value = ""; q("#cf2-classes").value = ""; q("#cf2-desc").value = "";
      q("#course-form").hidden = true;
      await loadCourses();
      flash("课程已创建。");
    }),
    "course-roster": (btn) => {
      const scope = document.getElementById(`course-${id}`);
      if (!scope) return;
      toggleCourseRoster(btn, scope, id);
    },
    "edit-course": () => {
      const scope = document.getElementById(`course-${id}`);
      if (!scope) return;
      const form = scope.querySelector("[data-role=course-edit]");
      form.hidden = !form.hidden;
      if (!form.hidden) {
        const sel = form.querySelector("[data-role=ce-teacher]");
        const current = state.courses.find((c) => c.id === id);
        sel.innerHTML = `<option value="">（未指派）</option>` + teacherOptions(current ? current.teacher_id : null);
      }
    },
    "cancel-course-edit": () => {
      const scope = document.getElementById(`course-${id}`);
      if (scope) scope.querySelector("[data-role=course-edit]").hidden = true;
    },
    "save-course": () => withCert(async () => {
      const scope = document.getElementById(`course-${id}`);
      if (!scope) return;
      const form = scope.querySelector("[data-role=course-edit]");
      await api(`/api/training/staff/courses/${id}`, {
        method: "PATCH",
        body: {
          name: form.querySelector("[data-role=ce-name]").value.trim(),
          semester: form.querySelector("[data-role=ce-semester]").value.trim() || null,
          teacher_id: form.querySelector("[data-role=ce-teacher]").value || null,
          class_names: splitClassNames(form.querySelector("[data-role=ce-classes]").value),
          description: form.querySelector("[data-role=ce-desc]").value.trim() || null,
        },
      });
      await loadCourses();
      flash("课程已保存。");
    }),
    "toggle-course": () => {
      const c = state.courses.find((x) => x.id === id);
      if (!c) return;
      if (c.active && !window.confirm(`确认停用课程「${c.name}」？停用不删数据，可随时启用。`)) return;
      withCert(async () => {
        await api(`/api/training/staff/courses/${id}`, {
          method: "PATCH", body: { active: !c.active } });
        flash(c.active ? "课程已停用。" : "课程已启用。");
      });
    },
  };

  try {
    if (handlers[act]) await handlers[act](btn);
  } catch (err) {
    flash(err.message, true);
  }
});

document.getElementById("cert-select").addEventListener("change", async (event) => {
  state.currentId = event.target.value || null;
  await load().catch((err) => flash(err.message, true));
});

async function boot() {
  try {
    let me;
    try{me=await api("/api/training/staff/auth/me");}catch{await api("/api/training/membership/session",{method:"POST"});me=await api("/api/training/staff/auth/me");}
    state.staff = me.staff;
    showApp(true);
    document.getElementById("pass-form").hidden = false;
    await load();
  } catch (error) {
    if(disposed)return;
    if(embedded)throw error;
    if(error.status===401||error.status===403)showApp(false);else flash(error.message,true);
  }
}

const ready=boot();

function renderStaffWorkbench() {
  const box = document.getElementById("staff-workbench-body");
  if (!box) return;
  const count = (label, value, unit) => `<div class="kpi"><div>${esc(label)}</div><div class="value">${esc(value)}<span class="unit">${esc(unit)}</span></div></div>`;
  box.innerHTML = `<p class="muted">${esc(schoolName())} · ${esc(state.staff.display_name)}，从这里继续教学与管理。</p>
    <div class="kpi-grid">${count("在读学生", state.students.filter(s => s.active).length, "人")}${count("名册班级", state.classes.length, "个")}${count("课程", state.courses.length, "门")}${count("上架证书", state.certificates.filter(c => c.active).length, "项")}</div>
    <p class="muted">师生、班级和课程按本院校统计；考证内容为共享教学资源。这里不将资源数量当作学习成绩。</p>
    <div class="university-home-actions"><button data-workspace-view="students">查看师生与班级</button><button data-workspace-view="courses">查看课程</button><button data-workspace-view="organization">查看组织与学期</button></div>`;
  box.querySelectorAll("[data-workspace-view]").forEach(button => button.addEventListener("click", () => window.universityWorkspace.choose(button.dataset.workspaceView)));
}

/* controller end */
return {ready,refresh:async()=>{await load();await membershipUI?.load();},destroy(){disposed=true;requests.abort();membershipUI?.destroy();}};
};
