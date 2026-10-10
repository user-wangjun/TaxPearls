/* 考证内容管理教师端：证书上架、官方日期录入、考纲知识点与三类标注。 */
"use strict";

const state = { staff: null, certificates: [], currentId: null, kps: [], versions: [], importPlan: null };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`);
  return data;
}

function flash(message, isError = false) {
  const box = document.getElementById(isError ? "app-error" : "app-error");
  box.textContent = message || "";
  if (message) setTimeout(() => { if (box.textContent === message) box.textContent = ""; }, 5000);
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
    return `<div class="item" ${indent} id="kp-${esc(k.id)}">
      <div class="item-head">
        <div><strong>${esc(k.code)}</strong> ${esc(k.name)}
          ${k.active ? "" : `<span class="badge gray">已停用</span>`}
          ${k.subject ? `<span class="chip">${esc(k.subject)}</span>` : ""}
          ${k.source_ref ? `<span class="muted">依据：${esc(k.source_ref)}${k.outline_version ? "（" + esc(k.outline_version) + "）" : ""}</span>` : ""}
        </div>
        <div class="actions">
          <button class="small" data-act="edit-kp" data-id="${esc(k.id)}">编辑</button>
        </div>
      </div>
      ${marks}
      ${renderRelations(k)}
      <div class="actions">
        <button class="small" data-act="edit-kp" data-id="${esc(k.id)}">编辑</button>
        <button class="small" data-act="show-mark-form" data-id="${esc(k.id)}">＋ 标注</button>
        <button class="small" data-act="show-relation-form" data-id="${esc(k.id)}">＋ 关系</button>
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
        <thead><tr style="text-align:left;background:#f5f6f4;">
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
}

/* ---------- 数据 ---------- */

async function load() {
  const data = await api("/api/training/staff/certificates?include_inactive=true");
  state.certificates = data.certificates;
  if (!state.certificates.find((c) => c.id === state.currentId)) {
    state.currentId = state.certificates.length ? state.certificates[0].id : null;
  }
  state.kps = [];
  state.versions = [];
  if (state.currentId) {
    const kp = await api(`/api/training/staff/certificates/${state.currentId}/knowledge-points`);
    state.kps = kp.knowledge_points;
    const vs = await api(`/api/training/staff/certificates/${state.currentId}/content-versions`);
    state.versions = vs.versions;
  }
  renderAll();
}

/* ---------- 事件 ---------- */

document.getElementById("login-btn").addEventListener("click", async () => {
  const box = document.getElementById("login-error");
  box.textContent = "";
  try {
    const data = await api("/api/training/staff/auth/login", {
      method: "POST",
      body: {
        username: document.getElementById("username").value.trim(),
        password: document.getElementById("password").value,
        remember: document.getElementById("remember").checked,
      },
    });
    state.staff = data.staff;
    showApp(true);
    await load();
  } catch (err) {
    box.textContent = err.message;
  }
});

document.getElementById("logout-btn")?.addEventListener("click", async () => {
  await api("/api/training/staff/auth/logout", { method: "POST" });
  showApp(false);
});

function showApp(loggedIn) {
  document.getElementById("login-view").hidden = loggedIn;
  document.getElementById("app-view").hidden = !loggedIn;
  document.getElementById("who").innerHTML = loggedIn
    ? `${esc(state.staff.display_name)}（${esc(state.staff.role)} · ${esc(state.staff.college)}）
       <button class="small" id="logout-btn">退出</button>` : "";
  if (loggedIn) document.getElementById("logout-btn").addEventListener("click", async () => {
    await api("/api/training/staff/auth/logout", { method: "POST" });
    showApp(false);
  });
}

async function withCert(mutator) {
  try {
    await mutator();
    await load();
  } catch (err) {
    flash(err.message, true);
  }
}

document.getElementById("app-view").addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const id = btn.dataset.id;
  const cert = current();
  const q = (sel, root) => (root || document).querySelector(sel);

  const handlers = {
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
  };

  if (handlers[act]) handlers[act]();
});

document.getElementById("cert-select").addEventListener("change", async (event) => {
  state.currentId = event.target.value || null;
  await load().catch((err) => flash(err.message, true));
});

async function boot() {
  try {
    const me = await api("/api/training/staff/auth/me");
    state.staff = me.staff;
    showApp(true);
    await load();
  } catch {
    showApp(false);
  }
}

boot();
