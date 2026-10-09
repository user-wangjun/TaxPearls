/* 考证备考学生端（FR-K01/K02）：证书目录、学习目标与考试倒计时。 */
"use strict";

const state = { student: null, certificates: [], goals: [] };

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

function countdownBadge(cd) {
  if (!cd) return `<span class="badge warn">未设日期</span>`;
  if (cd.expired) return `<span class="badge bad">已过期（${esc(cd.target_date)}）</span>`;
  if (cd.days_left === 0) return `<span class="badge warn">今天考试</span>`;
  return `<span class="badge">距考试 ${cd.days_left} 天</span>`;
}

const SOURCE_LABEL = { official: "官方考试日", expected: "预计考试日", planned: "个人计划日" };

/* ---------- 渲染 ---------- */

function renderGoals() {
  const box = document.getElementById("goals");
  if (!state.goals.length) {
    box.innerHTML = `<p class="muted">还没有考证目标，从下方证书目录中选择一个开始。</p>`;
    return;
  }
  box.innerHTML = state.goals.map((g) => {
    const cert = g.certificate || {};
    const dateText = g.official_date
      ? `${esc(g.official_date.exam_date)}（${SOURCE_LABEL[g.official_date.date_type] || g.official_date.date_type}）`
      : (g.planned_date ? `${esc(g.planned_date)}（个人计划日）` : "未设日期");
    const statusText = { active: "进行中", paused: "已暂停", achieved: "已达成", archived: "已归档" }[g.status] || g.status;
    const actions = [];
    if (g.status === "active") {
      actions.push(`<button class="small" data-act="edit" data-id="${esc(g.id)}">修改日期</button>`);
      actions.push(`<button class="small" data-act="pause" data-id="${esc(g.id)}">暂停</button>`);
      actions.push(`<button class="small" data-act="archive" data-id="${esc(g.id)}">归档</button>`);
    } else if (g.status === "paused") {
      actions.push(`<button class="small" data-act="resume" data-id="${esc(g.id)}">恢复</button>`);
      actions.push(`<button class="small" data-act="archive" data-id="${esc(g.id)}">归档</button>`);
    }
    return `<div class="goal">
      <div class="goal-head">
        <div><strong>${esc(cert.name || "未知证书")}</strong>
          <span class="muted">${statusText}</span></div>
        ${countdownBadge(g.countdown)}
      </div>
      <div class="muted">目标日期：${dateText} · 来源：${SOURCE_LABEL[g.countdown_source] || "未设置"}</div>
      <div class="actions">${actions.join("")}</div>
      <div class="inline-form" id="edit-${esc(g.id)}" hidden>
        <label>改为官方/预计考试日</label>
        <select data-role="official" data-id="${esc(g.id)}">
          <option value="">（不指定）</option>
          ${officialOptions(cert.id)}
        </select>
        <label>或自定义个人计划日</label>
        <input type="date" data-role="planned" data-id="${esc(g.id)}" min="${todayISO()}"
               value="${esc(g.planned_date || "")}">
        <div class="actions">
          <button class="small primary" data-act="save-edit" data-id="${esc(g.id)}">保存</button>
          <button class="small" data-act="cancel-edit" data-id="${esc(g.id)}">取消</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

function officialOptions(certificateId) {
  const cert = state.certificates.find((c) => c.id === certificateId);
  if (!cert) return "";
  return cert.exam_dates
    .filter((d) => d.countdown && !d.countdown.expired)
    .map((d) => `<option value="${esc(d.id)}">${esc(d.exam_date)}${d.round_label ? " · " + esc(d.round_label) : ""}（${d.date_type === "official" ? "官方" : "预计"}）</option>`)
    .join("");
}

function renderCerts() {
  const box = document.getElementById("certs");
  if (!state.certificates.length) {
    box.innerHTML = `<p class="muted">证书目录暂未开放，请等待管理员上架。</p>`;
    return;
  }
  box.innerHTML = state.certificates.map((c) => {
    const dates = c.exam_dates.length
      ? c.exam_dates.map((d) => {
          const label = `${esc(d.exam_date)}${d.round_label ? " · " + esc(d.round_label) : ""}`;
          return `<li>${label} ${countdownBadge(d.countdown)}</li>`;
        }).join("")
      : `<li class="muted">暂未公布考试日期</li>`;
    const hasActiveGoal = state.goals.some((g) => g.certificate && g.certificate.id === c.id && g.status === "active");
    return `<div class="cert">
      <div class="cert-head">
        <div><strong>${esc(c.name)}</strong> <span class="muted">${esc(c.code)}</span></div>
        <button class="small primary" data-act="pick" data-id="${esc(c.id)}" ${hasActiveGoal ? "disabled" : ""}>
          ${hasActiveGoal ? "已是我的目标" : "设为我的目标"}
        </button>
      </div>
      <div>${(c.subjects || []).map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>
      ${c.description ? `<div class="muted" style="margin-top:6px;">${esc(c.description)}</div>` : ""}
      <ul class="muted" style="margin:8px 0 0; padding-left:18px;">${dates}</ul>
      <div class="inline-form" id="pick-${esc(c.id)}" hidden>
        <label>选择官方/预计考试日</label>
        <select data-role="pick-official" data-id="${esc(c.id)}">
          <option value="">（不指定）</option>
          ${officialOptions(c.id)}
        </select>
        <label>或填写个人计划日</label>
        <input type="date" data-role="pick-planned" data-id="${esc(c.id)}" min="${todayISO()}">
        <div class="actions">
          <button class="small primary" data-act="confirm-pick" data-id="${esc(c.id)}">确认目标</button>
          <button class="small" data-act="cancel-pick" data-id="${esc(c.id)}">取消</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

function todayISO() {
  const now = new Date();
  const cst = new Date(now.getTime() + (8 * 60 + now.getTimezoneOffset()) * 60000);
  return cst.toISOString().slice(0, 10);
}

/* ---------- 行为 ---------- */

function flash(message, isError = false) {
  const box = document.getElementById(isError ? "login-error" : "goal-msg");
  if (isError) box.textContent = message || "";
  else box.textContent = message || "";
  if (message && !isError) setTimeout(() => { box.textContent = ""; }, 4000);
}

async function refresh() {
  const [certs, goals] = await Promise.all([
    api("/api/training/certificates"),
    api("/api/training/my/goals?include_archived=1"),
  ]);
  state.certificates = certs.certificates;
  state.goals = goals.goals;
  renderGoals();
  renderCerts();
}

function showApp(loggedIn) {
  document.getElementById("login-view").hidden = loggedIn;
  document.getElementById("app-view").hidden = !loggedIn;
  document.getElementById("who").textContent = loggedIn
    ? `${state.student.name}（${state.student.student_no}${state.student.college ? " · " + state.student.college : ""}）` : "";
}

async function boot() {
  try {
    const me = await api("/api/training/auth/me");
    state.student = me.student;
    showApp(true);
    await refresh();
  } catch {
    showApp(false);
  }
}

document.getElementById("login-btn").addEventListener("click", async () => {
  const box = document.getElementById("login-error");
  box.textContent = "";
  try {
    const data = await api("/api/training/auth/login", {
      method: "POST",
      body: {
        student_no: document.getElementById("student-no").value.trim(),
        password: document.getElementById("password").value,
        college: document.getElementById("college").value.trim() || null,
        remember: document.getElementById("remember").checked,
      },
    });
    state.student = data.student;
    showApp(true);
    await refresh();
  } catch (err) {
    box.textContent = err.message;
  }
});

document.getElementById("app-view").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  try {
    if (act === "pause" || act === "resume" || act === "archive") {
      await api(`/api/training/my/goals/${id}/${act}`, { method: "POST" });
      flash(act === "archive" ? "目标已归档，历史记录保留。" : "已更新。");
      await refresh();
    } else if (act === "edit" || act === "pick") {
      document.getElementById(`${act}-${id}`).hidden = false;
    } else if (act === "cancel-edit" || act === "cancel-pick") {
      document.getElementById(`${act.replace("cancel-", "")}-${id}`).hidden = true;
    } else if (act === "save-edit") {
      const scope = document.getElementById(`edit-${id}`);
      await api(`/api/training/my/goals/${id}`, {
        method: "PATCH",
        body: {
          official_date_id: scope.querySelector("[data-role=official]").value || null,
          planned_date: scope.querySelector("[data-role=planned]").value || null,
        },
      });
      flash("目标日期已更新，历史创建时间保留。");
      await refresh();
    } else if (act === "confirm-pick") {
      const scope = document.getElementById(`pick-${id}`);
      await api("/api/training/my/goals", {
        method: "POST",
        body: {
          certificate_id: id,
          official_date_id: scope.querySelector("[data-role=pick-official]").value || null,
          planned_date: scope.querySelector("[data-role=pick-planned]").value || null,
        },
      });
      flash("目标已建立，倒计时开始。");
      await refresh();
    }
  } catch (err) {
    flash(err.message, true);
    setTimeout(() => flash("", true), 5000);
  }
});

boot();
