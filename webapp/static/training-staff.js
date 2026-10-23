"use strict";
window.createTrainingStaff = function(document = window.document, {embedded=false} = {}) {
let disposed=false;const requests=new AbortController();
/* controller body */
/* 考证内容管理教师端：证书上架、官方日期录入、考纲知识点与三类标注。 */
"use strict";

const state = { staff: null, certificates: [], currentId: null, kps: [], versions: [], importPlan: null, markStats: null, rules: [], students: [], classes: [], classHistory: [], courses: [], teachers: [], departments: [], terms: [], orgClasses: [], quota: null, auths: [], bank: [], outline: null, trialFeedback: [], trialStats: null };

const SOURCE_ZH = { simulated: "仿真题", real: "真题", recall: "回忆题", mock: "模拟题" };
const BANK_STATUS_ZH = { draft: "草稿", pending: "待审", approved: "已上线", rejected: "已驳回" };
const REVIEW_ZH = { pending: "待核对", confirmed: "沿用", updated: "已按新纲修改", removed: "新版删除", added: "新版新增" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
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

function todayISO() {
  const now = new Date();
  const cst = new Date(now.getTime() + (8 * 60 + now.getTimezoneOffset()) * 60000);
  return cst.toISOString().slice(0, 10);
}

/* ---------- 渲染 ---------- */

function renderCertSelect() {
  const select = document.getElementById("cert-select");
  select.innerHTML = state.certificates.map((c) =>
    `<option value="${esc(c.id)}">${esc(c.name)}（${esc(c.code)}）${c.active ? "" : " · 已下架"}</option>`
  ).join("");
  if (state.currentId) select.value = state.currentId;
}

function renderCertForm() {
  const cert = current();
  const form = document.getElementById("cert-form");
  const empty = document.getElementById("cert-empty");
  const stateBadge = document.getElementById("cert-state");
  form.hidden = !cert;
  empty.hidden = !!cert;
  if (!cert) { stateBadge.innerHTML = ""; return; }
  stateBadge.innerHTML = cert.active
    ? `<span class="badge">已上架</span>` : `<span class="badge gray">已下架</span>`;
  document.getElementById("cf-name").value = cert.name;
  document.getElementById("cf-code").value = cert.code;
  document.getElementById("cf-subjects").value = (cert.subjects || []).join(", ");
  document.getElementById("cf-description").value = cert.description || "";
  document.getElementById("cf-source").value = cert.source_ref || "";
  const toggle = form.querySelector('[data-act="toggle-active"]');
  toggle.textContent = cert.active ? "下架" : "重新上架";
}

function renderDates() {
  const cert = current();
  const box = document.getElementById("dates");
  if (!cert) { box.innerHTML = ""; return; }
  if (!cert.exam_dates.length) {
    box.innerHTML = `<p class="muted">尚未录入考试日期。学生建目标需要至少一个未来的官方/预计日期。</p>`;
    return;
  }
  box.innerHTML = cert.exam_dates.map((d) => {
    const type = d.date_type === "official" ? "官方" : "预计";
    return `<div class="item" id="date-${esc(d.id)}">
      <div class="item-head">
        <div><strong>${esc(d.exam_date)}</strong> <span class="badge gray">${type}</span>
          ${d.round_label ? esc(d.round_label) : ""} ${countdownBadge(d.countdown)}</div>
        <div class="actions">
          <button class="small" data-act="edit-date" data-id="${esc(d.id)}">改期</button>
          <button class="small danger" data-act="delete-date" data-id="${esc(d.id)}">删除</button>
        </div>
      </div>
      <div class="inline-form" data-role="date-edit" hidden>
        <div class="row">
          <div><label>日期</label><input type="date" data-role="de-date" min="${todayISO()}" value="${esc(d.exam_date)}"></div>
          <div><label>批次标识</label><input data-role="de-label" value="${esc(d.round_label || "")}"></div>
        </div>
        <div class="actions">
          <button class="small primary" data-act="save-date" data-id="${esc(d.id)}">保存</button>
          <button class="small" data-act="cancel-date-edit" data-id="${esc(d.id)}">取消</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

function kpDepth(kp, byId) {
  let depth = 0, cur = kp.parent_id;
  while (cur && depth < 16) { depth += 1; cur = byId[cur] ? byId[cur].parent_id : null; }
  return depth;
}

function renderRelations(k) {
  const rows = (k.relations || {});
  const TYPE = { prerequisite: "前置", concept: "概念", confusable: "易混淆" };
  const chips = [];
  (rows.prerequisite || []).forEach((t) => chips.push({ label: `前置：${t.name}`, id: t.relation_id }));
  (rows.concept || []).forEach((t) => chips.push({ label: `概念关联：${t.name}`, id: t.relation_id }));
  (rows.confusable || []).forEach((t) => chips.push({ label: `易混淆：${t.name}`, id: t.relation_id }));
  (k.incoming_prerequisite || []).forEach((r) =>
    chips.push({ label: `被前置依赖：${r.name}` }));
  if (!chips.length) return "";
  return `<div style="margin-top:6px;">${chips.map((c) =>
    `<span class="chip">${esc(c.label)}${c.id ? ` <button class="small danger" style="padding:0 6px;" data-act="delete-relation" data-id="${esc(c.id)}">删</button>` : ""}</span>`
  ).join(" ")}</div>`;
}

function renderRuleLinks(k) {
  const rules = (k.links || []).filter((l) => l.target_type === "rule");
  if (!rules.length) return "";
  return `<div style="margin-top:6px;">${rules.map((l) => {
    const r = state.rules.find((x) => x.id === l.target_id);
    return `<span class="chip">练习规则：${r ? esc(r.name) : esc(l.target_id)}
      <button class="small danger" style="padding:0 6px;" data-act="delete-link" data-id="${esc(l.id)}">删</button></span>`;
  }).join(" ")}</div>`;
}

function renderKps() {
  const box = document.getElementById("kps");
  if (!state.kps.length) {
    box.innerHTML = `<p class="muted">尚未建考纲知识点。学生刷题与“常考考点”展示将以此为基础。</p>`;
    return;
  }
  const byId = Object.fromEntries(state.kps.map((k) => [k.id, k]));
  const TYPE = { high_freq: "考证高频", risk_context: "企业风险", error_prone: "学生易错" };
  box.innerHTML = state.kps.map((k) => {
    const indent = `style="margin-left:${kpDepth(k, byId) * 22}px;"`;
    const marks = (k.marks || []).map((m) => `
      <div class="mark">${TYPE[m.mark_type] || m.mark_type} · ${esc(m.level)}
        ${m.basis_ref ? `— 依据：${esc(m.basis_ref)}${m.basis_version ? "（" + esc(m.basis_version) + "）" : ""}` : `<span class="muted">（无文本依据）</span>`}
        <button class="small danger" data-act="delete-mark" data-id="${esc(m.id)}" data-kp="${esc(k.id)}">删</button>
      </div>`).join("");
    const parentOptions = [`<option value="">（无）</option>`].concat(
      state.kps.filter((p) => p.id !== k.id && p.active).map((p) =>
        `<option value="${esc(p.id)}" ${p.id === k.parent_id ? "selected" : ""}>${esc(p.code)} ${esc(p.name)}</option>`)
    ).join("");
    const reviewStatus = k.review_status || "";
    const reviewBox = (reviewStatus || (state.outline && state.outline.current))
      ? `<div class="inline-form">
          <div class="row">
            <div><label>考纲升版核对</label>
              <select data-role="kp-review">${Object.entries(REVIEW_ZH).map(([v, t]) =>
                `<option value="${v}"${reviewStatus === v ? " selected" : ""}>${t}</option>`).join("")}</select></div>
            <div class="actions" style="align-self:flex-end;">
              <button class="small primary" data-act="set-review" data-id="${esc(k.id)}">保存核对</button></div>
          </div>
          <p class="muted" style="margin:4px 0 0;">核对口径：沿用=与新版一致；已按新纲修改=内容按要求改过；新版删除=激活后停用；新增项由系统自动标记，无需手工选。</p>
        </div>` : "";
    return `<div class="item" ${indent} id="kp-${esc(k.id)}">
      <div class="item-head">
        <div><strong>${esc(k.code)}</strong> ${esc(k.name)}
          ${k.active ? "" : `<span class="badge gray">已停用</span>`}
          ${reviewStatus ? `<span class="badge warn">${REVIEW_ZH[reviewStatus] || esc(reviewStatus)}</span>` : ""}
          ${k.subject ? `<span class="chip">${esc(k.subject)}</span>` : ""}
          ${k.source_ref ? `<span class="muted">依据：${esc(k.source_ref)}${k.outline_version ? "（" + esc(k.outline_version) + "）" : ""}</span>` : ""}
        </div>
        <div class="actions">
          <button class="small" data-act="edit-kp" data-id="${esc(k.id)}">编辑</button>
        </div>
      </div>
      ${reviewBox}
      ${marks}
      ${renderRelations(k)}
      ${renderRuleLinks(k)}
      <div class="actions">
        <button class="small" data-act="edit-kp" data-id="${esc(k.id)}">编辑</button>
        <button class="small" data-act="show-mark-form" data-id="${esc(k.id)}">＋ 标注</button>
        <button class="small" data-act="show-relation-form" data-id="${esc(k.id)}">＋ 关系</button>
        <button class="small" data-act="show-link-form" data-id="${esc(k.id)}">＋ 规则</button>
      </div>
      <div class="inline-form" data-role="link-form" hidden>
        <div class="row">
          <div><label>练习规则（题目来源，当前为规则仿真题）</label>
            <select data-role="lf-rule">${state.rules.map((r) =>
              `<option value="${esc(r.id)}">${esc(r.id)} ${esc(r.name)}</option>`).join("")}</select></div>
        </div>
        <div class="actions"><button class="small primary" data-act="create-link" data-id="${esc(k.id)}">关联规则</button></div>
      </div>
      <div class="inline-form" data-role="relation-form" hidden>
        <div class="row">
          <div><label>关系类型</label>
            <select data-role="rf-type">
              <option value="prerequisite">前置知识（学本点前先学对方）</option>
              <option value="concept">概念关联</option>
              <option value="confusable">易混淆</option>
            </select></div>
          <div><label>对侧知识点</label>
            <select data-role="rf-target">${state.kps.filter((p) => p.id !== k.id && p.active)
              .map((p) => `<option value="${esc(p.id)}">${esc(p.code)} ${esc(p.name)}</option>`).join("")}</select></div>
        </div>
        <div class="row">
          <div><label>依据（必填，考纲章节/教材说明）</label><input data-role="rf-basis" placeholder="如 考纲第三章第2节"></div>
          <div><label>依据版本</label><input data-role="rf-version" placeholder="如 2026大纲"></div>
        </div>
        <div class="actions"><button class="small primary" data-act="create-relation" data-id="${esc(k.id)}">建立关系</button></div>
      </div>
      <div class="inline-form" data-role="kp-edit" hidden>
        <div class="row">
          <div><label>名称</label><input data-role="ke-name" value="${esc(k.name)}"></div>
          <div><label>科目</label><input data-role="ke-subject" value="${esc(k.subject || "")}"></div>
          <div><label>上级</label><select data-role="ke-parent">${parentOptions}</select></div>
        </div>
        <div class="row">
          <div><label>考纲依据</label><input data-role="ke-source" value="${esc(k.source_ref || "")}"></div>
          <div><label>考纲版本</label><input data-role="ke-version" value="${esc(k.outline_version || "")}"></div>
        </div>
        <div class="actions">
          <button class="small primary" data-act="save-kp" data-id="${esc(k.id)}">保存</button>
          <button class="small" data-act="cancel-kp-edit" data-id="${esc(k.id)}">取消</button>
          <button class="small ${k.active ? "danger" : "primary"}" data-act="toggle-kp" data-id="${esc(k.id)}">${k.active ? "停用" : "启用"}</button>
        </div>
      </div>
      <div class="inline-form" data-role="mark-form" hidden>
        <div class="row">
          <div><label>类型</label>
            <select data-role="mf-type">
              <option value="high_freq">考证高频</option>
              <option value="risk_context">企业风险情境</option>
              <option value="error_prone">学生易错</option>
            </select></div>
          <div><label>程度</label>
            <select data-role="mf-level"><option>high</option><option>medium</option><option>low</option></select></div>
          <div><label>依据（考证高频必填）</label><input data-role="mf-basis" placeholder="如 考纲第三章第2节"></div>
          <div><label>依据版本</label><input data-role="mf-version" placeholder="如 2027版"></div>
        </div>
        <div class="actions"><button class="small primary" data-act="create-mark" data-id="${esc(k.id)}">添加标注</button></div>
      </div>
    </div>`;
  }).join("");
}

function renderImportResult(data) {
  const box = document.getElementById("import-result");
  if (!data) { box.innerHTML = ""; return; }
  if (data.error) {
    box.innerHTML = `<div class="error" style="white-space:pre-wrap;">${esc(data.error)}</div>`;
    state.importPlan = null;
    return;
  }
  if (data.done) {
    box.innerHTML = `<div class="ok">导入完成：新建 ${data.created} 条，更新 ${data.updated} 条（共 ${data.total}）。</div>`;
    state.importPlan = null;
    return;
  }
  const s = data.summary;
  state.importPlan = data;
  const rows = data.plan.map((p) => `<tr>
    <td style="font-family:ui-monospace,monospace;">${esc(p.code)}</td>
    <td>${esc(p.name)}</td>
    <td>${p.action === "insert"
      ? `<span class="badge">新增</span>`
      : `<span class="badge gray">更新${p.existing_name && p.existing_name !== p.name ? `（原：${esc(p.existing_name)}）` : ""}</span>`}</td>
    <td class="muted">${esc(p.parent_code || "—")}</td>
    <td>${p.warnings.map((w) => `<span class="badge warn">${esc(w)}</span>`).join(" ")}</td>
  </tr>`).join("");
  box.innerHTML = `
    <div style="margin-bottom:6px;">
      <span class="badge">共 ${s.total} 行</span>
      <span class="badge">新增 ${s.insert}</span>
      <span class="badge gray">更新 ${s.update}</span>
      ${s.warning ? `<span class="badge warn">警告 ${s.warning}</span>` : ""}
    </div>
    ${s.total ? `<div style="max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead><tr>
          <th style="padding:6px 8px;">编码</th><th style="padding:6px 8px;">名称</th>
          <th style="padding:6px 8px;">动作</th><th style="padding:6px 8px;">上级编码</th>
          <th style="padding:6px 8px;">提示</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <div class="actions" style="margin-top:10px;">
        <button class="primary small" data-act="confirm-import">确认导入（${s.insert} 新增 / ${s.update} 更新）</button>
        <button class="small" data-act="cancel-import-preview">放弃</button>
      </div>` : `<p class="muted">没有可导入的行。</p>`}
  `;
}

async function importPayload() {
  return {
    text: document.getElementById("if-text").value,
    format: document.getElementById("if-format").value,
    default_outline_version: document.getElementById("if-version").value.trim(),
  };
}

function renderMarkStats() {
  const box = document.getElementById("mark-stats");
  const s = state.markStats;
  if (!s) { box.innerHTML = `<p class="muted">暂无统计数据。</p>`; return; }
  const TYPE = { high_freq: "考证高频", risk_context: "企业风险", error_prone: "学生易错" };
  const kp = s.knowledge_points;
  const rows = Object.entries(s.marks).map(([type, m]) => {
    const versionDetail = m.by_basis_version && Object.keys(m.by_basis_version).length
      ? `<span class="muted">依据版本：${Object.entries(m.by_basis_version)
          .map(([v, n]) => `${esc(v)} × ${n}`).join("、")}</span>` : "";
    return `<div class="item">
      <div class="item-head">
        <div><strong>${TYPE[type]}</strong>
          <span class="badge">${m.count} 条标注 · 覆盖 ${m.points} 个知识点</span>
          <span class="badge gray">未标注 ${m.unmarked_points} 个</span></div>
      </div>${versionDetail ? `<div style="margin-top:4px;">${versionDetail}</div>` : ""}
    </div>`;
  }).join("");
  box.innerHTML = `
    <div style="margin-bottom:10px;">
      <span class="badge">知识点 ${kp.total} 个</span>
      <span class="badge">有题目 ${kp.with_questions}</span>
      ${kp.without_questions ? `<span class="badge warn">无题目 ${kp.without_questions}（刷题前需关联规则）</span>` : `<span class="badge">题目全覆盖</span>`}
      ${(Object.entries(kp.by_subject) || []).map(([sub, n]) => `<span class="chip">${esc(sub)} ${n}</span>`).join(" ")}
    </div>${rows}`;
}

async function loadMarkStats() {
  if (!state.currentId) { state.markStats = null; renderMarkStats(); return; }
  try {
    state.markStats = (await api(
      `/api/training/staff/certificates/${state.currentId}/mark-stats`)).mark_stats;
  } catch (err) {
    state.markStats = null;
  }
  renderMarkStats();
}

function renderParentOptions() {
  const select = document.getElementById("kf-parent");
  select.innerHTML = [`<option value="">（无，作为顶级）</option>`].concat(
    state.kps.filter((k) => k.active).map((k) =>
      `<option value="${esc(k.id)}">${esc(k.code)} ${esc(k.name)}</option>`)
  ).join("");
}

function renderVersions() {
  const box = document.getElementById("versions");
  const cert = current();
  if (!cert) { box.innerHTML = ""; return; }
  if (!state.versions.length) {
    box.innerHTML = `<p class="muted">尚无内容版本：学生首次练习时会自动建立“仿真基线”版本，也可在上方手动发布。</p>`;
    return;
  }
  const TYPE = { simulated: "仿真题", real: "真题", recall: "回忆题", mock: "模拟题" };
  box.innerHTML = state.versions.map((v) => `<div class="item">
    <div class="item-head">
      <div><strong>${esc(v.label)}</strong>
        <span class="badge ${v.status === "active" ? "" : "gray"}">${v.status === "active" ? "启用中" : "已退役"}</span>
        <span class="chip">${TYPE[v.source_type] || v.source_type} · ${esc(v.year)} 年度</span>
        <span class="muted">覆盖规则 ${(v.rule_ids || []).length} 条</span></div>
      <div class="actions">
        ${v.status === "active" ? `<button class="small danger" data-act="retire-version" data-id="${esc(v.id)}">退役</button>` : ""}
      </div>
    </div>
    ${v.knowledge_update?.pending ? `<p class="notice">${esc(v.knowledge_update.notice)}
      ${v.knowledge_update.changed_rule_ids.length ? `变更规则：${esc(v.knowledge_update.changed_rule_ids.join("、"))}。` : ""}
      请使用上方“发布新版本”表单采用更新。</p>` : ""}
    ${v.source_ref ? `<div class="muted">依据：${esc(v.source_ref)}</div>` : ""}
    ${v.note ? `<div class="muted">备注：${esc(v.note)}</div>` : ""}
  </div>`).join("");
}

function renderAll() {
  renderCertSelect();
  renderCertForm();
  renderDates();
  renderVersions();
  renderKps();
  renderParentOptions();
  renderAuths();
  renderBank();
  renderOutline();
  renderTrial();
}

/* ---------- 题源授权 / 题库 / 考纲升版 / 试用反馈 ---------- */

async function loadSourceAuths() {
  if (!state.currentId) { state.auths = []; renderAuths(); return; }
  state.auths = (await api(
    `/api/training/staff/certificates/${state.currentId}/source-authorizations`)).authorizations;
  renderAuths();
}

function renderAuths() {
  const box = document.getElementById("authorizations");
  const rows = state.auths || [];
  if (!rows.length) {
    box.innerHTML = `<p class="muted">该证书还没有登记任何授权：真题/回忆题/模拟题当前不可发布与导入，题库只能保持规则仿真题。</p>`;
    return;
  }
  box.innerHTML = `<div class="table-scroll"><table style="width:100%;border-collapse:collapse;">
    <tr><th style="text-align:left;">授权方</th><th style="text-align:left;">文号</th>
      <th style="text-align:left;">覆盖题源</th><th style="text-align:left;">有效期</th>
      <th style="text-align:left;">状态</th><th style="text-align:left;"></th></tr>
    ${rows.map((a) => `<tr>
      <td>${esc(a.licensor)}</td>
      <td>${esc(a.auth_no)}</td>
      <td>${(a.question_type_list || []).map((t) => SOURCE_ZH[t] || t).join("、")}</td>
      <td>${esc(a.valid_from || "")} ~ ${esc(a.valid_until || "长期")}</td>
      <td><span class="badge ${a.status === "active" ? (a.covering_today ? "" : "warn") : "gray"}">${
        a.status === "active" ? (a.covering_today ? "生效中" : "未生效/已过期") : "已撤销"}</span></td>
      <td>${a.status === "active" ? `<button class="small danger" data-act="revoke-auth" data-id="${esc(a.id)}">撤销</button>` : ""}</td>
    </tr>`).join("")}
  </table></div>
  ${rows.some((a) => a.status === "active" && !a.covering_today)
    ? `<p class="muted">「未生效/已过期」的授权不能用于发布或导入；如需继续使用请重新登记有效期内的授权。</p>` : ""}`;
}

async function loadBank() {
  if (!state.currentId) { state.bank = []; renderBank(); return; }
  const filter = document.getElementById("bank-status-filter");
  const status = filter && filter.value ? `?status=${encodeURIComponent(filter.value)}` : "";
  state.bank = (await api(
    `/api/training/staff/certificates/${state.currentId}/bank-questions${status}`)).questions;
  renderBank();
}

function renderBank() {
  const box = document.getElementById("bank-questions");
  const rows = state.bank || [];
  if (!rows.length) {
    box.innerHTML = `<p class="muted">题库暂无题目：先在上方登记授权，再粘贴人工核对过的 JSON 导入。</p>`;
    return;
  }
  box.innerHTML = rows.map((q) => {
    const options = (q.options || []).map((o) => `${esc(o.key)}. ${esc(o.text)}`).join("　");
    const actions = q.status === "draft" || q.status === "rejected"
      ? `<button class="small primary" data-act="bank-submit" data-id="${esc(q.id)}">提交审核</button>`
      : q.status === "pending"
        ? `<button class="small primary" data-act="bank-approve" data-id="${esc(q.id)}">通过</button>
           <button class="small danger" data-act="bank-reject" data-id="${esc(q.id)}">驳回</button>`
        : `<button class="small" data-act="bank-offline" data-id="${esc(q.id)}">下架</button>`;
    return `<div class="item">
      <div class="item-head">
        <div><span class="chip">${SOURCE_ZH[q.source_type] || esc(q.source_type)} · ${esc(q.year)} 年度</span>
          <span class="badge ${q.status === "approved" ? "" : "gray"}">${BANK_STATUS_ZH[q.status] || esc(q.status)}</span>
          ${q.question_no ? `<span class="muted">${esc(q.question_no)}</span>` : ""}</div>
        <div class="actions">${actions}</div>
      </div>
      <div style="margin-top:4px;">${esc(q.stem)}</div>
      <div class="muted">${options}</div>
      <div class="muted">标准答案：${esc((q.answer_keys || []).join("、"))}</div>
      <details><summary>解析与依据</summary>
        <div class="detail">${esc(q.explanation || "（缺解析）")}</div>
        <div class="muted">依据：${esc(q.source_ref || "")}</div>
        ${q.review_note ? `<div class="muted">审核意见：${esc(q.review_note)}</div>` : ""}
      </details>
    </div>`;
  }).join("");
}

function renderBankImport(data) {
  const rows = (data.results || []).map((r) => r.ok
    ? `<div class="detail ok">第 ${r.line} 行：已入库草稿</div>`
    : `<div class="detail miss">第 ${r.line} 行：${esc(r.error)}</div>`).join("");
  document.getElementById("bank-import-result").innerHTML =
    `<p class="${data.failed ? "warn" : "ok"}">导入完成：成功 ${data.imported} 题，失败 ${data.failed} 题（非法行不中断整批）。</p>${rows}`;
}

async function loadOutline() {
  if (!state.currentId) { state.outline = null; renderOutline(); return; }
  state.outline = await api(`/api/training/staff/certificates/${state.currentId}/outline-upgrade`);
  renderOutline();
}

function renderOutline() {
  const box = document.getElementById("outline-body");
  const data = state.outline;
  if (!data) { box.innerHTML = ""; return; }
  const cur = data.current;
  const counts = data.review_counts || {};
  const progress = `<span class="chip">待核对 ${counts.pending || 0}</span>
    <span class="chip">沿用 ${counts.confirmed || 0}</span>
    <span class="chip">已修改 ${counts.updated || 0}</span>
    <span class="chip">新增 ${counts.added || 0}</span>
    <span class="chip">删除 ${counts.removed || 0}</span>`;
  const current = cur
    ? `<div class="item">
        <div class="item-head"><div><strong>核对中：${esc(cur.from_version)} → ${esc(cur.to_version)}</strong>
          <span class="badge warn">核对中</span></div>
          <span class="muted">发起于 ${esc((cur.started_at || "").slice(0, 16).replace("T", " "))}</span></div>
        <div style="margin-top:4px;">${progress}</div>
        <div class="muted">在「考纲知识点」卡片逐条把核对状态改为沿用/已修改/删除；核对期新增的知识点自动记为新增、暂不对学生显示。待核对清零后可点上方「激活切换」。</div>
        ${cur.note ? `<div class="muted">备注：${esc(cur.note)}</div>` : ""}
      </div>`
    : `<p class="muted">当前没有进行中的升版核对；现行主导版本：${esc(data.dominant_version || "未登记")}。新版大纲发布后填目标版本再发起。</p>`;
  const history = (data.history || []).map((h) => `<div class="item">
    <div class="item-head"><div>${esc(h.from_version)} → <strong>${esc(h.to_version)}</strong>
      <span class="badge gray">已激活</span></div>
      <span class="muted">${esc((h.activated_at || "").slice(0, 16).replace("T", " "))}</span></div>
    ${h.note ? `<div class="muted">备注：${esc(h.note)}</div>` : ""}
  </div>`).join("");
  box.innerHTML = current + (history ? `<h3 style="margin-top:14px;">升版历史</h3>${history}` : "");
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
  const rows = state.units || [];
  if (!rows.length) {
    box.innerHTML = '<p class="muted">还没有教学单元。可新建单元，把知识点与规则拼成一次课的能力单元。</p>';
    return;
  }
  box.innerHTML = rows.map((u) => `<div class="data-item">
    <div><b>${esc(u.name)}</b>
      <div class="muted">${esc(u.stage_label)} · 规则 ${u.rule_codes.length} 条 · 知识点 ${u.knowledge_point_ids.length} 个</div>
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
  const rows = state.cases || [];
  if (!rows.length) {
    box.innerHTML = '<p class="muted">还没有实操案例。案例为脱敏仿真材料包，绑定规则与标准结论。</p>';
    return;
  }
  box.innerHTML = rows.map((c) => `<div class="data-item">
    <div><b>${esc(c.name)}</b>
      <div class="muted">规则 ${c.rule_codes.length} 条 · 材料 ${c.materials.length} 份${c.unit_id ? " · 已挂单元" : ""}</div>
      ${c.scenario ? `<div>${esc(c.scenario)}</div>` : ""}
      ${c.desensitization_note ? `<div class="muted">${esc(c.desensitization_note)}</div>` : ""}
    </div>
    <span class="chip">${esc(c.status === "published" ? "已发布" : c.status === "archived" ? "已归档" : "草稿")}</span>
  </div>`).join("");
}

function fillUnitSelects() {
  const rules = document.getElementById("uf-rules");
  if (rules && (state.rules || []).length) {
    rules.innerHTML = state.rules.map((r) =>
      `<option value="${esc(r.id || r.rule_code || r.code)}">${esc(r.id || r.rule_code || r.code)} ${esc(r.name || "")}</option>`).join("");
  }
  const kps = document.getElementById("uf-kps");
  if (kps) {
    kps.innerHTML = (state.kps || []).map((k) =>
      `<option value="${esc(k.id)}">${esc(k.code)} ${esc(k.name)}</option>`).join("");
  }
}

function fillCaseSelects() {
  const rules = document.getElementById("cf-case-rules");
  if (rules && (state.rules || []).length) {
    rules.innerHTML = state.rules.map((r) =>
      `<option value="${esc(r.id || r.rule_code || r.code)}">${esc(r.id || r.rule_code || r.code)} ${esc(r.name || "")}</option>`).join("");
  }
  const unit = document.getElementById("cf-case-unit");
  if (unit) {
    unit.innerHTML = '<option value="">（不挂单元）</option>' +
      (state.units || []).map((u) => `<option value="${esc(u.id)}">${esc(u.name)}</option>`).join("");
  }
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
  if(!membershipUI&&state.staff&&state.staff.role!=="school_admin"&&document.getElementById("school-membership")){const id=state.staff.id;membershipUI=TPMemberLinks.school(document.getElementById("school-membership"),{request:api,valid:()=>!disposed&&state.staff?.id===id,afterChange:async()=>{await loadStudents();}});}
  const data = await api("/api/training/staff/certificates?include_inactive=true");
  state.certificates = data.certificates;
  if (!state.certificates.find((c) => c.id === state.currentId)) {
    state.currentId = state.certificates.length ? state.certificates[0].id : null;
  }
  state.kps = [];
  state.versions = [];
  state.auths = [];
  state.bank = [];
  state.outline = null;
  if (state.currentId) {
    const kp = await api(`/api/training/staff/certificates/${state.currentId}/knowledge-points`);
    state.kps = kp.knowledge_points;
    const vs = await api(`/api/training/staff/certificates/${state.currentId}/content-versions`);
    state.versions = vs.versions;
    await loadSourceAuths().catch(() => { state.auths = []; renderAuths(); });
    await loadBank().catch(() => { state.bank = []; renderBank(); });
    await loadOutline().catch(() => { state.outline = null; renderOutline(); });
    await loadTeachingUnits().catch(() => { state.units = []; renderTeachingUnits(); });
    await loadTeachingCases().catch(() => { state.cases = []; renderTeachingCases(); });
  }
  state.rules = (await api("/api/training/staff/rules")).rules;
  await loadStudents();
  await loadCourses();
  if (state.staff && ["admin", "school_admin"].includes(state.staff.role)) {
    document.getElementById("overview-card").hidden = false;
    await loadOverview();
  }
  renderAll();
  await loadMarkStats().catch(() => {});
  await loadRoster().catch(() => { state.classes = []; state.classHistory = []; renderRoster(); });
  await loadOrg().catch(() => { state.departments = []; state.terms = []; state.orgClasses = []; renderOrg(); });
  await loadTrial().catch(() => { state.trialFeedback = []; state.trialStats = null; renderTrial(); });
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
        <div><strong>${esc(s.student_no)}</strong> ${esc(s.name)}
          <span class="chip">${esc(s.college)}</span>
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
  const college = (state.staff && state.staff.college) || "";
  state.terms = college
    ? (await api(`/api/training/staff/org/terms?college=${encodeURIComponent(college)}&include_archived=false`)).terms
    : [];
  renderCourses();
  renderTermOptions();
}

function teacherOptions(selectedId) {
  return state.teachers.map((t) =>
    `<option value="${esc(t.id)}" ${t.id === selectedId ? "selected" : ""}>${esc(t.display_name)}（${esc(t.username)}）</option>`).join("");
}

/** 学期下拉：只列未归档学期；没有任何学期时给出提示而非静默空列表。 */
function termOptions(selectedId, { keepUnset = false } = {}) {
  const head = keepUnset ? `<option value="" ${selectedId ? "" : "selected"}>（未挂学期）</option>` : "";
  return head + state.terms.map((t) =>
    `<option value="${esc(t.id)}" ${t.id === selectedId ? "selected" : ""}>${esc(t.name)}${t.is_current ? "（当前学期）" : ""}</option>`).join("");
}

function renderTermOptions() {
  const select = document.getElementById("cf2-term");
  if (!select) return;
  if (!state.terms.length) {
    select.innerHTML = `<option value="">（院校还没有学期，请先到「组织与学期」建立）</option>`;
    return;
  }
  select.innerHTML = termOptions(null, { keepUnset: true });
  const current = state.terms.find((t) => t.is_current);
  if (current) select.value = current.id;
}

function courseTermLabel(c) {
  return c.term_name || c.semester || "";
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
          ${courseTermLabel(c) ? `<span class="chip">${esc(courseTermLabel(c))}</span>` : `<span class="badge gray">未挂学期</span>`}
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
          <div><label>学年学期</label><select data-role="ce-term">${termOptions(c.term_id, { keepUnset: true })}</select></div>
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
    document.getElementById(id).value = loggedIn ? state.staff.college : "";
  }
  document.getElementById("teacher-import-btn").hidden = !(loggedIn && state.staff && state.staff.role === "admin");
  document.getElementById("who").innerHTML = loggedIn
    ? `${esc(state.staff.display_name)}（${esc(state.staff.role)} · ${esc(state.staff.college)}）
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

/* 四个内容治理面板的轻量刷新：只重取授权/题库/升版/试用，不重跑整页 load。 */
async function refreshPanels() {
  await Promise.all([
    loadSourceAuths().catch(() => {}),
    loadBank().catch(() => {}),
    loadOutline().catch(() => {}),
    loadTrial().catch(() => {}),
  ]);
}

document.getElementById("app-view").addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const id = btn.dataset.id;
  const cert = current();
  const q = (sel, root) => (root || document).querySelector(sel);

  const handlers = {
    /* ---- 题源授权登记 ---- */
    "refresh-auth": () => refreshPanels(),
    "refresh-bank": () => refreshPanels(),
    "refresh-outline": () => refreshPanels(),
    "refresh-trial": () => loadTrial().catch((err) => flash(err.message, true)),
    "create-auth": () => {
      const types = [...document.querySelectorAll(".au-type:checked")].map((c) => c.value);
      if (!q("#au-licensor").value.trim() || !q("#au-no").value.trim()) {
        flash("授权方与授权文号必填。", true); return;
      }
      if (!types.length) { flash("请至少勾选一个覆盖题源。", true); return; }
      if (!q("#au-from").value) { flash("请填生效日。", true); return; }
      withCert(async () => {
        await api(`/api/training/staff/certificates/${cert.id}/source-authorizations`, {
          method: "POST",
          body: {
            licensor: q("#au-licensor").value.trim(),
            auth_no: q("#au-no").value.trim(),
            question_types: types,
            valid_from: q("#au-from").value,
            valid_until: q("#au-until").value || null,
            scope_note: q("#au-scope").value.trim(),
          },
        });
        q("#au-no").value = "";
        q("#au-scope").value = "";
        document.querySelectorAll(".au-type:checked").forEach((c) => { c.checked = false; });
        flash("授权已登记：发布对应题源版本或导入题库时会自动写入该文号留痕。");
      });
    },
    "revoke-auth": () => {
      if (!window.confirm("撤销后该授权不能再用于发布或导入（记录保留可审计）。确认撤销？")) return;
      withCert(async () => {
        await api(`/api/training/staff/source-authorizations/${id}/revoke`, { method: "POST" });
        flash("授权已撤销；已发布版本与存量作答不受影响。");
      });
    },
    /* ---- 题库导入与审核 ---- */
    "import-bank": () => {
      const text = q("#bank-json").value.trim();
      if (!text) { flash("请先粘贴导入 JSON。", true); return; }
      let questions;
      try { questions = JSON.parse(text); } catch (err) {
        flash(`JSON 解析失败：${err.message}`, true); return;
      }
      if (!Array.isArray(questions)) { flash("导入内容必须是 JSON 数组。", true); return; }
      withCert(async () => {
        const data = await api(`/api/training/staff/certificates/${cert.id}/bank-questions`, {
          method: "POST", body: { questions } });
        renderBankImport(data);
        flash(`导入完成：成功 ${data.imported} 题，失败 ${data.failed} 题。`, data.failed > 0);
      });
    },
    "filter-bank": () => { loadBank().catch((err) => flash(err.message, true)); },
    "bank-submit": () => withCert(async () => {
      await api(`/api/training/staff/bank-questions/${id}/submit`, { method: "POST" });
      flash("已提交审核；待审通过后学生才能抽到。");
    }),
    "bank-approve": () => withCert(async () => {
      await api(`/api/training/staff/bank-questions/${id}/review`, {
        method: "POST", body: { decision: "approve" } });
      flash("已审核上线。");
    }),
    "bank-reject": () => {
      const note = window.prompt("驳回原因（会显示在审核意见里）：", "");
      if (note === null) return;
      withCert(async () => {
        await api(`/api/training/staff/bank-questions/${id}/review`, {
          method: "POST", body: { decision: "reject", note } });
        flash("已驳回；该题可修改后重新提交审核。");
      });
    },
    "bank-offline": () => {
      if (!window.confirm("下架后学生不再抽到本题；已产生的历史作答仍按当时题目与解析回看。继续？")) return;
      withCert(async () => {
        await api(`/api/training/staff/bank-questions/${id}/offline`, { method: "POST" });
        flash("已下架回草稿。");
      });
    },
    /* ---- 考纲升版 ---- */
    "set-review": () => withCert(async () => {
      const value = q("[data-role=kp-review]", q(`#kp-${id}`)).value;
      await api(`/api/training/staff/knowledge-points/${id}`, {
        method: "PATCH", body: { review_status: value } });
      flash(`核对状态已保存：${REVIEW_ZH[value] || value}。`);
    }),
    "outline-start": () => {
      const to = q("#ou-to").value.trim();
      if (!to) { flash("请填目标考纲版本（如 2027）。", true); return; }
      if (!window.confirm(`发起后现行知识点全部标记「待核对」，核对完成前不能再次发起。确认发起 ${to} 升版核对？`)) return;
      withCert(async () => {
        await api(`/api/training/staff/certificates/${cert.id}/outline-upgrade/start`, {
          method: "POST", body: { to_version: to, note: q("#ou-note").value.trim() } });
        flash(`已发起 ${to} 升版核对：请逐条核对知识点，待核对清零后再激活。`);
      });
    },
    "outline-activate": () => {
      const cur = state.outline && state.outline.current;
      if (!cur) { flash("当前没有进行中的升版核对。", true); return; }
      const pending = (state.outline.review_counts || {}).pending || 0;
      if (pending) { flash(`还有 ${pending} 条知识点待核对，不能激活切换。`, true); return; }
      if (!window.confirm(`确认激活 ${cur.to_version}：知识点版本号统一切换、删除项停用留痕、新增项上线。`)) return;
      withCert(async () => {
        const data = await api(
          `/api/training/staff/certificates/${cert.id}/outline-upgrade/activate`, { method: "POST" });
        flash(`已切换到 ${data.upgrade.to_version}：上线 ${data.upgrade.activated} 条，停用 ${data.upgrade.removed} 条。`);
      });
    },
    /* ---- 试用反馈处理 ---- */
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
    "new-cert-btn": () => { document.getElementById("new-cert-form").hidden = false; },
    "cancel-new-cert": () => { document.getElementById("new-cert-form").hidden = true; },
    "create-cert": () => withCert(async () => {
      const data = await api("/api/training/staff/certificates", {
        method: "POST",
        body: {
          code: q("#nc-code").value.trim(),
          name: q("#nc-name").value.trim(),
          description: q("#nc-description").value,
          subjects: q("#nc-subjects").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          source_ref: q("#nc-source").value,
        },
      });
      document.getElementById("new-cert-form").hidden = true;
      state.currentId = data.certificate.id;
      flash("证书已创建并上架。");
    }),
    "save-cert": () => withCert(async () => {
      await api(`/api/training/staff/certificates/${cert.id}`, {
        method: "PATCH",
        body: {
          name: q("#cf-name").value.trim(),
          description: q("#cf-description").value,
          subjects: q("#cf-subjects").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          source_ref: q("#cf-source").value,
        },
      });
      flash("证书信息已保存。");
    }),
    "toggle-active": () => withCert(async () => {
      await api(`/api/training/staff/certificates/${cert.id}`, {
        method: "PATCH", body: { active: !cert.active } });
      flash(cert.active ? "已下架，学生目录不再显示。" : "已重新上架。");
    }),
    "show-date-form": () => { const f = q("#date-form"); f.hidden = !f.hidden; q("#df-date").min = todayISO(); },
    "create-date": () => withCert(async () => {
      await api(`/api/training/staff/certificates/${cert.id}/dates`, {
        method: "POST",
        body: { date_type: q("#df-type").value, exam_date: q("#df-date").value, round_label: q("#df-label").value },
      });
      q("#df-date").value = ""; q("#df-label").value = "";
      flash("考试日期已录入。");
    }),
    "edit-date": () => { q(`#date-${id} [data-role=date-edit]`).hidden = false; },
    "cancel-date-edit": () => { q(`#date-${id} [data-role=date-edit]`).hidden = true; },
    "save-date": () => withCert(async () => {
      const scope = q(`#date-${id}`);
      await api(`/api/training/staff/dates/${id}`, {
        method: "PATCH",
        body: { exam_date: q("[data-role=de-date]", scope).value, round_label: q("[data-role=de-label]", scope).value },
      });
      flash("考试日期已更新，引用它的学生目标自动跟随新日期。");
    }),
    "delete-date": () => {
      if (!window.confirm("确认删除该考试日期？（被学生目标引用时会被拒绝）")) return;
      withCert(async () => { await api(`/api/training/staff/dates/${id}`, { method: "DELETE" }); flash("已删除。"); });
    },
    "show-kp-form": () => { const f = q("#kp-form"); f.hidden = !f.hidden; },
    "show-import-form": () => { const f = q("#import-form"); f.hidden = !f.hidden; },
    "refresh-mark-stats": () => loadMarkStats(),
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
      const college = q("#cm-college").value.trim();
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
      const college = q("#ti-college").value.trim();
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
          college: q("#sf-college").value.trim(),
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
      const college = q("#si-college").value.trim();
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
      const college = q("#si-college").value.trim();
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
        renderTermOptions();
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
          college: q("#cf2-college").value.trim(),
          term_id: q("#cf2-term").value || null,
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
          term_id: form.querySelector("[data-role=ce-term]").value || null,
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
    "cancel-import": () => {
      q("#import-form").hidden = true;
      renderImportResult(null);
    },
    "cancel-import-preview": () => renderImportResult(null),
    "preview-import": async () => {
      const body = await importPayload();
      if (!body.text.trim()) { flash("请先粘贴导入内容。", true); return; }
      try {
        renderImportResult(await api(
          `/api/training/staff/certificates/${cert.id}/knowledge-points/import/preview`,
          { method: "POST", body }));
      } catch (err) {
        renderImportResult({ error: err.message });
      }
    },
    "confirm-import": async () => {
      if (!state.importPlan) return;
      const body = await importPayload();
      try {
        const r = await api(`/api/training/staff/certificates/${cert.id}/knowledge-points/import`,
          { method: "POST", body });
        await load();
        renderImportResult({ done: true, created: r.created, updated: r.updated, total: r.total });
        flash("知识点批量导入完成。");
      } catch (err) {
        renderImportResult({ error: err.message });
      }
    },
    "create-kp": () => withCert(async () => {
      await api(`/api/training/staff/certificates/${cert.id}/knowledge-points`, {
        method: "POST",
        body: {
          code: q("#kf-code").value.trim(), name: q("#kf-name").value.trim(),
          subject: q("#kf-subject").value.trim(), parent_id: q("#kf-parent").value || null,
          source_ref: q("#kf-source").value, outline_version: q("#kf-version").value,
        },
      });
      q("#kf-code").value = ""; q("#kf-name").value = "";
      flash("知识点已创建。");
    }),
    "edit-kp": () => { q(`#kp-${id} [data-role=kp-edit]`).hidden = false; },
    "cancel-kp-edit": () => { q(`#kp-${id} [data-role=kp-edit]`).hidden = true; },
    "save-kp": () => withCert(async () => {
      const scope = q(`#kp-${id}`);
      await api(`/api/training/staff/knowledge-points/${id}`, {
        method: "PATCH",
        body: {
          name: q("[data-role=ke-name]", scope).value.trim(),
          subject: q("[data-role=ke-subject]", scope).value.trim(),
          parent_id: q("[data-role=ke-parent]", scope).value || null,
          source_ref: q("[data-role=ke-source]", scope).value,
          outline_version: q("[data-role=ke-version]", scope).value,
        },
      });
      flash("知识点已更新。");
    }),
    "toggle-kp": () => withCert(async () => {
      const kp = state.kps.find((k) => k.id === id);
      await api(`/api/training/staff/knowledge-points/${id}`, {
        method: "PATCH", body: { active: !kp.active } });
      flash(kp.active ? "知识点已停用。" : "知识点已启用。");
    }),
    "show-mark-form": () => { const f = q(`#kp-${id} [data-role=mark-form]`); f.hidden = !f.hidden; },
    "create-mark": () => withCert(async () => {
      const scope = q(`#kp-${id}`);
      await api(`/api/training/staff/knowledge-points/${id}/marks`, {
        method: "POST",
        body: {
          mark_type: q("[data-role=mf-type]", scope).value,
          level: q("[data-role=mf-level]", scope).value,
          basis_ref: q("[data-role=mf-basis]", scope).value,
          basis_version: q("[data-role=mf-version]", scope).value,
        },
      });
      flash("标注已添加。");
    }),
    "show-version-form": () => { const f = q("#version-form"); f.hidden = !f.hidden; },
    "create-version": () => withCert(async () => {
      await api(`/api/training/staff/certificates/${cert.id}/content-versions`, {
        method: "POST",
        body: {
          label: q("#vf-label").value.trim(),
          source_type: q("#vf-type").value,
          year: q("#vf-year").value ? parseInt(q("#vf-year").value, 10) : null,
          source_ref: q("#vf-source").value,
        },
      });
      q("#vf-label").value = ""; q("#vf-year").value = ""; q("#vf-source").value = "";
      q("#version-form").hidden = true;
      flash("内容版本已发布并启用；旧版本自动退役，旧作答仍按当时版本回溯。");
    }),
    "retire-version": () => {
      if (!window.confirm("确认退役该内容版本？记录保留，旧作答仍按当时版本回溯。")) return;
      withCert(async () => {
        await api(`/api/training/staff/content-versions/${id}/retire`, { method: "POST" });
        flash("版本已退役。");
      });
    },
    "show-relation-form": () => { const f = q(`#kp-${id} [data-role=relation-form]`); f.hidden = !f.hidden; },
    "create-relation": () => withCert(async () => {
      const scope = q(`#kp-${id}`);
      await api(`/api/training/staff/knowledge-points/${id}/relations`, {
        method: "POST",
        body: {
          to_kp_id: q("[data-role=rf-target]", scope).value,
          relation_type: q("[data-role=rf-type]", scope).value,
          basis_ref: q("[data-role=rf-basis]", scope).value,
          basis_version: q("[data-role=rf-version]", scope).value,
        },
      });
      q("[data-role=rf-basis]", scope).value = "";
      flash("关系已建立。");
    }),
    "delete-relation": () => {
      if (!window.confirm("确认删除该知识点关系？")) return;
      withCert(async () => { await api(`/api/training/staff/relations/${id}`, { method: "DELETE" }); flash("关系已删除。"); });
    },
    "delete-mark": () => {
      if (!window.confirm("确认删除该标注？")) return;
      withCert(async () => { await api(`/api/training/staff/marks/${id}`, { method: "DELETE" }); flash("标注已删除。"); });
    },
    "show-link-form": () => { const f = q(`#kp-${id} [data-role=link-form]`); f.hidden = !f.hidden; },
    "create-link": () => withCert(async () => {
      const scope = q(`#kp-${id}`);
      await api(`/api/training/staff/knowledge-points/${id}/links`, {
        method: "POST",
        body: { target_type: "rule", target_id: q("[data-role=lf-rule]", scope).value },
      });
      flash("规则已关联；学生可从该知识点进入规则仿真练习。");
    }),
    "delete-link": () => {
      if (!window.confirm("确认删除该规则关联？")) return;
      withCert(async () => { await api(`/api/training/staff/links/${id}`, { method: "DELETE" }); flash("规则关联已删除。"); });
    },

    /* ---- 教学单元与实操案例（B2，接口 /api/training/teaching/*） ---- */
    "refresh-teaching-units": () => loadTeachingUnits().catch((err) => flash(err.message, true)),
    "refresh-teaching-cases": () => loadTeachingCases().catch((err) => flash(err.message, true)),
    "show-unit-form": () => {
      const form = q("#unit-form");
      if (form) { form.hidden = !form.hidden; if (!form.hidden) { fillUnitSelects(); form.scrollIntoView({ behavior: "smooth" }); } }
    },
    "cancel-unit": () => { const form = q("#unit-form"); if (form) form.hidden = true; },
    "create-unit": () => withCert(async () => {
      const name = q("#uf-name").value.trim();
      if (!name) { flash("请填单元名称。", true); return; }
      const rules = [...q("#uf-rules").selectedOptions].map((o) => o.value);
      const kps = [...q("#uf-kps").selectedOptions].map((o) => o.value);
      if (!rules.length) { flash("请至少关联一条规则编号。", true); return; }
      const created = await api("/api/training/teaching/units", { method: "POST", body: {
        certificate_id: cert.id, name, stage: q("#uf-stage").value,
        standard: q("#uf-standard").value, knowledge_point_ids: kps, rule_codes: rules,
        deliverables: q("#uf-deliverables").value } });
      await api(`/api/training/teaching/units/${created.unit.id}`, {
        method: "PUT", body: { status: "published" } });
      ["#uf-name", "#uf-standard", "#uf-deliverables"].forEach((sel) => { q(sel).value = ""; });
      q("#unit-form").hidden = true;
      flash("教学单元已创建并发布。");
    }),
    "show-case-form": () => {
      const form = q("#case-form");
      if (form) { form.hidden = !form.hidden; if (!form.hidden) { fillCaseSelects(); form.scrollIntoView({ behavior: "smooth" }); } }
    },
    "cancel-case": () => { const form = q("#case-form"); if (form) form.hidden = true; },
    "create-case": () => withCert(async () => {
      const name = q("#cf-case-name").value.trim();
      if (!name) { flash("请填案例名称。", true); return; }
      const rules = [...q("#cf-case-rules").selectedOptions].map((o) => o.value);
      if (!rules.length) { flash("请至少关联一条规则编号。", true); return; }
      const materials = q("#cf-case-materials").value.split("\n").map((l) => l.trim()).filter(Boolean)
        .map((line) => { const [mname, ...rest] = line.split("|"); return { name: mname.trim(), note: rest.join("|").trim() }; });
      const created = await api("/api/training/teaching/cases", { method: "POST", body: {
        certificate_id: cert.id, name, unit_id: q("#cf-case-unit").value || null,
        scenario: q("#cf-case-scenario").value, rule_codes: rules, materials,
        desensitization_note: q("#cf-case-note").value } });
      await api(`/api/training/teaching/cases/${created.case.id}`, {
        method: "PUT", body: { status: "published" } });
      ["#cf-case-name", "#cf-case-scenario", "#cf-case-materials", "#cf-case-note"]
        .forEach((sel) => { q(sel).value = ""; });
      q("#case-form").hidden = true;
      flash("实操案例已创建并发布。");
    }),
  };

  if (handlers[act]) handlers[act](btn);
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
  box.innerHTML = `<p class="muted">${esc(state.staff.college)} · ${esc(state.staff.display_name)}，从这里继续教学与管理。</p>
    <div class="kpi-grid">${count("在读学生", state.students.filter(s => s.active).length, "人")}${count("名册班级", state.classes.length, "个")}${count("课程", state.courses.length, "门")}${count("上架证书", state.certificates.filter(c => c.active).length, "项")}</div>
    <p class="muted">师生、班级和课程按本院校统计；考证内容为共享教学资源。这里不将资源数量当作学习成绩。</p>
    <div class="university-home-actions"><button data-workspace-view="students">查看师生与班级</button><button data-workspace-view="courses">查看课程</button><button data-workspace-view="organization">查看组织与学期</button></div>`;
  box.querySelectorAll("[data-workspace-view]").forEach(button => button.addEventListener("click", () => window.universityWorkspace.choose(button.dataset.workspaceView)));
}

/* controller end */
return {ready,refresh:async()=>{await load();await membershipUI?.load();},destroy(){disposed=true;requests.abort();membershipUI?.destroy();}};
};
