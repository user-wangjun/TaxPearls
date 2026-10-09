/* 考证内容管理教师端：证书上架、官方日期录入、考纲知识点与三类标注。 */
"use strict";

const state = { staff: null, certificates: [], currentId: null, kps: [] };

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
      <div class="actions"><button class="small" data-act="show-mark-form" data-id="${esc(k.id)}">＋ 标注</button></div>
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

function renderParentOptions() {
  const select = document.getElementById("kf-parent");
  select.innerHTML = [`<option value="">（无，作为顶级）</option>`].concat(
    state.kps.filter((k) => k.active).map((k) =>
      `<option value="${esc(k.id)}">${esc(k.code)} ${esc(k.name)}</option>`)
  ).join("");
}

function renderAll() {
  renderCertSelect();
  renderCertForm();
  renderDates();
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
  if (state.currentId) {
    const kp = await api(`/api/training/staff/certificates/${state.currentId}/knowledge-points`);
    state.kps = kp.knowledge_points;
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
