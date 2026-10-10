/* 考证备考学生端（FR-K01/K02）：证书目录、学习目标与考试倒计时。 */
"use strict";

const state = { student: null, certificates: [], goals: [], kps: [], attempt: null,
                profile: null, pfCert: null };

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
  await refreshPractice().catch((err) => flash(err.message, true));
  await refreshProfile().catch(() => {});
}

/* ---------- 自主练习（FR-K05/K06） ---------- */

function fmtNum(v) { return v === null || v === undefined || v === "" ? "—" : esc(v); }

function renderStats() {
  const box = document.getElementById("pr-stats");
  const s = state.prStats;
  if (!s) { box.textContent = ""; return; }
  const fmt = (b) => b.attempts ? `${b.attempts} 次（满分 ${b.perfect}，正确率 ${(b.accuracy * 100).toFixed(0)}%）` : "0 次";
  box.textContent = `细分统计 —— 新题：${fmt(s.new)} · 原题重做：${fmt(s.redo_same)} · 未作答即交：${fmt(s.unanswered)} · 跳过：${s.skipped} 次（不计入正确率）`;
}

async function refreshPractice() {
  const certSel = document.getElementById("pr-cert");
  certSel.innerHTML = state.certificates.map((c) =>
    `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  if (!state.prCert || !state.certificates.some((c) => c.id === state.prCert)) {
    state.prCert = state.certificates.length ? state.certificates[0].id : null;
  }
  if (state.prCert) certSel.value = state.prCert;
  const [kpData, wrong, open, dash, stats] = await Promise.all([
    state.prCert ? api(`/api/training/my/knowledge-points?certificate_id=${state.prCert}`)
                 : Promise.resolve({ knowledge_points: [] }),
    api("/api/training/my/practice/wrong"),
    api("/api/training/my/practice?status=open"),
    api("/api/training/my/dashboard"),
    state.prCert ? api(`/api/training/my/practice/stats?certificate_id=${state.prCert}`)
                 : Promise.resolve(null),
  ]);
  state.kps = kpData.knowledge_points;
  state.wrong = wrong.attempts;
  state.open = open.attempts;
  state.dashboard = dash;
  state.prStats = stats ? stats.stats : null;
  renderWorkbench();
  renderStats();
  const kpSel = document.getElementById("pr-kp");
  kpSel.innerHTML = `<option value="">（整证随机）</option>` + state.kps.map((k) =>
    `<option value="${esc(k.id)}">${esc(k.code)} ${esc(k.name)}${k.rule_links ? "" : "（暂无题目）"}</option>`).join("");
  document.getElementById("pr-hint").textContent = state.kps.length
    ? "题目来自教师关联的规则仿真题（纯仿真，非真题）；提交后自动判分并开放解析。"
    : "该证书下教师尚未配置知识点与题目，等待教师上架后即可练习。";
  renderKpRows();
  renderWrong();
  renderOpen();
  if (!state.attempt) renderAttempt();
}

function renderKpRows() {
  const box = document.getElementById("pr-kps");
  box.innerHTML = state.kps.map((k) => {
    const rel = k.relations || {};
    const relChips = [
      ...(rel.prerequisite || []).map((t) => `<span class="chip" title="学本知识点前建议先学">前置：${esc(t.name)}</span>`),
      ...(rel.confusable || []).map((t) => `<span class="chip" title="与本知识点易混淆">易混淆：${esc(t.name)}</span>`),
      ...(rel.concept || []).map((t) => `<span class="chip" title="概念关联">关联：${esc(t.name)}</span>`),
    ].join("");
    return `<div class="item"><div class="item-head">
      <div><strong>${esc(k.code)}</strong> ${esc(k.name)}
        ${k.high_freq.map((m) => `<span class="badge warn">常考${m.basis_ref ? " · " + esc(m.basis_ref) : ""}</span>`).join("")}
        ${k.rule_links ? "" : `<span class="badge gray">暂无题目</span>`}
      </div>
      <div class="muted">已练 ${k.attempts} 次 · 满分 ${k.perfect} 次</div>
    </div>${relChips ? `<div style="margin-top:4px;">${relChips}</div>` : ""}</div>`;
  }).join("");
}

function renderWrong() {
  const box = document.getElementById("pr-wrong");
  box.innerHTML = state.wrong.length ? state.wrong.map((a) => `
    <div class="item"><div class="item-head">
      <div><span class="badge bad">${a.score} 分</span>
        <span class="muted">${esc(a.scored_at || "")}</span></div>
      <div class="actions">
        <button class="small" data-act="redo-new" data-rule="${esc(a.rule_id)}" data-kp="${esc(a.knowledge_point_id || "")}">再练新题</button>
        <button class="small" data-act="redo-same" data-rule="${esc(a.rule_id)}" data-seed="${esc(a.seed)}" data-kp="${esc(a.knowledge_point_id || "")}">原题重做</button>
      </div>
    </div></div>`).join("") : `<p class="muted">暂无错题，保持！</p>`;
}

function renderOpen() {
  const box = document.getElementById("pr-open");
  box.innerHTML = state.open.length ? `<h2 style="margin-top:18px;">未完成的练习</h2>` + state.open.map((a) => `
    <div class="item"><div class="item-head">
      <div class="muted">开始于 ${esc(a.created_at)}</div>
      <button class="small" data-act="resume" data-id="${esc(a.id)}">继续作答</button>
    </div></div>`).join("") : "";
}

const MASTERY_ZH = { proficient: "较熟练", consolidating: "巩固中", acquainted: "初识", unstarted: "未开始" };

function renderWorkbench() {
  const box = document.getElementById("wb-body");
  const dash = state.dashboard;
  if (!dash) { box.innerHTML = ""; return; }
  if (!dash.goals.length) {
    box.innerHTML = `<p class="muted">${esc(dash.hint)}</p>`;
    return;
  }
  box.innerHTML = dash.goals.map((g) => {
    const source = g.countdown_source === "planned" ? "个人计划日"
      : (g.official_date ? (g.official_date.date_type === "official" ? "官方考试日" : "预计考试日") : "未设日期");
    const targetDate = g.countdown ? g.countdown.target_date : "未设";
    const hot = g.high_freq.map((h) =>
      `<span class="chip" title="${esc(h.basis_ref || "")}">常考 ${esc(h.name)}</span>`).join("");
    const weak = g.weak.length
      ? g.weak.map((w) => `<span class="chip">${esc(w.name)}（${w.attempts} 次未满分）</span>`).join("")
      : `<span class="muted">暂无薄弱知识点</span>`;
    const masteryChips = [["proficient", "badge"], ["consolidating", "badge warn"],
      ["acquainted", "badge gray"], ["unstarted", "badge gray"]]
      .map(([k, cls]) => `<span class="${cls}" title="口径：≥3 次且正确率≥80% 且最近一次满分为较熟练">${MASTERY_ZH[k]} ${g.mastery[k]}</span>`).join(" ");
    const planRows = (g.plan || []).map((t) => {
      const btn = t.kind === "resume"
        ? `<button class="small primary" data-act="wb-resume" data-id="${esc(t.attempt_id)}">继续作答</button>`
        : `<button class="small" data-act="wb-practice" data-cert="${esc(t.certificate_id)}" data-kp="${esc(t.knowledge_point_id || "")}" data-rule="${esc(t.rule_id || "")}">${t.kind === "review" ? "开始复习" : "开始练习"}</button>`;
      return `<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px dashed var(--line);padding:6px 0;">
        <div><span style="font-weight:500;">${esc(t.title)}</span>
          <span class="muted">— ${esc(t.reason)}</span></div>${btn}</div>`;
    }).join("");
    const pacingRow = g.pacing
      ? `<div class="muted" style="margin-top:6px;">节奏提示：${esc(g.pacing.note)}</div>` : "";
    const gapRow = g.gaps && g.gaps.kps_without_questions
      ? `<div class="muted" style="margin-top:4px;">题量缺口：${g.gaps.kps_without_questions} 个知识点暂无关联题目，等教师配置后即可练习。</div>` : "";
    return `<div class="item">
      <div class="item-head">
        <div><strong>${esc(g.certificate_name)}</strong>
          <span class="muted">目标日期 ${esc(targetDate)}（${source}）</span></div>
        ${countdownBadge(g.countdown)}
      </div>
      <div class="muted">覆盖进度：${g.coverage.covered}/${g.coverage.total} 个知识点已练 · 练习 ${g.coverage.attempts} 次 · 满分 ${g.coverage.perfect} 次${g.coverage.skipped ? ` · 跳过 ${g.coverage.skipped}（不计入）` : ""}</div>
      <div style="margin-top:6px;"><span class="muted">掌握程度：</span>${masteryChips}</div>
      <div style="margin-top:6px;">${hot || `<span class="muted">暂无常考标注</span>`}</div>
      <div style="margin-top:6px;"><span class="muted">薄弱点：</span>${weak}</div>
      <div class="inline-form" style="margin-top:10px;">
        <div class="muted" style="margin-bottom:4px;">今日学习安排（按优先级排列，可自主选择）：</div>
        ${planRows || `<p class="muted" style="margin:4px 0;">暂无可安排的任务；教师配置知识点与题目后，这里会给出可解释的建议。</p>`}
      </div>
      ${pacingRow}${gapRow}
    </div>`;
  }).join("");
}

function renderAttempt() {
  const box = document.getElementById("pr-attempt");
  const a = state.attempt;
  if (!a) { box.innerHTML = ""; return; }
  if (a.status === "open") {
    const m = a.materials;
    const accounts = (m.accounts || []).map((acc) => `<tr><td>${esc(acc.code)}</td><td>${esc(acc.name)}</td>
      <td class="num">${fmtNum(acc.opening)}</td><td class="num">${fmtNum(acc.debit)}</td>
      <td class="num">${fmtNum(acc.credit)}</td><td class="num">${fmtNum(acc.closing)}</td></tr>`).join("");
    const decls = Object.entries(m.declarations || {}).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td class="num">${fmtNum(v)}</td></tr>`).join("");
    const metrics = Object.entries(m.metrics || {}).map(([k, mt]) =>
      `<tr><td>${esc(mt.name)}</td><td class="num">${fmtNum(mt.value)}</td><td>${esc(mt.source)}</td></tr>`).join("");
    const catalog = a.rule_catalog.map((r) =>
      `<label style="display:inline-flex;gap:6px;align-items:center;font-size:13px;margin:2px 12px 2px 0;">
        <input type="checkbox" class="risk-pick" value="${esc(r.id)}"> ${esc(r.name)}</label>`).join("");
    box.innerHTML = `
      <div class="item"><strong>仿真材料（纯仿真教学企业，非真题）</strong>
        <div class="muted">${esc(m.company.name)} · 税号 ${esc(m.company.taxpayer_id)} · ${esc(m.company.industry)} · ${esc(m.company.period)}</div>
        <div><strong style="font-size:13px;">科目余额表</strong>
          <table><tr><th>编码</th><th>科目</th><th>期初</th><th>借方</th><th>贷方</th><th>期末</th></tr>${accounts}</table></div>
        <div><strong style="font-size:13px;">申报数据</strong>
          <table><tr><th>项目</th><th>金额</th></tr>${decls}</table></div>
        <div><strong style="font-size:13px;">标准化指标</strong>
          <table><tr><th>指标</th><th>数值</th><th>取数来源</th></tr>${metrics}</table></div>
      </div>
      <div class="item"><strong>识别风险点</strong>
        <div class="muted">勾选你认为构成风险的规则（可多选）；选错扣分，漏选不得分，提交后开放逐项解析。</div>
        <div style="margin:6px 0;">${catalog}</div>
        <div class="actions">
          <button class="primary" data-act="submit">提交判分</button>
          <button data-act="abandon">跳过本题（留痕，不计入统计）</button>
        </div>
        <div class="error" id="pr-error"></div>
      </div>`;
  } else {
    const r = a.result || {};
    const kindLabel = { correct: "命中", missed: "漏检", false_positives: "误报" };
    const cls = { correct: "ok", missed: "miss", false_positives: "false" };
    const rows = ["correct", "missed", "false_positives"].flatMap((key) =>
      (r[key] || []).map((d) => `<div class="detail ${cls[key]}">
        [${kindLabel[key]}] ${esc(d.name)}（${d.points > 0 ? "+" : ""}${esc(d.points)} 分）${d.explanation ? " — " + esc(d.explanation) : ""}</div>`));
    box.innerHTML = `
      <div class="item">
        <div class="item-head"><strong>判分结果</strong>
          <span class="badge ${a.perfect ? "" : "warn"}">${a.score} 分${a.perfect ? " · 满分" : ""}</span></div>
        ${rows.join("") || `<p class="muted">无明细。</p>`}
        <div class="muted">标准答案（判分后开放）：${esc((r.standard_answer || []).join("、"))}</div>
        <div class="actions">
          <button class="small" data-act="redo-new" data-rule="${esc(a.rule_id)}" data-kp="${esc(a.knowledge_point_id || "")}">再练新题</button>
          <button class="small" data-act="redo-same" data-rule="${esc(a.rule_id)}" data-seed="${esc(a.seed)}" data-kp="${esc(a.knowledge_point_id || "")}">原题重做</button>
        </div>
      </div>
      <div class="item">
        <strong>AI 答疑（基于本题解析与知识点依据）</strong>
        <div class="muted">回答仅作解读参考；标准答案与判分以系统为准，答疑记录仅本人可见。</div>
        <div class="row" style="margin-top:6px;">
          <input id="tutor-q" placeholder="如：为什么这项是漏检？判定依据是什么？">
          <button class="small primary" data-act="tutor-ask" style="flex:0 0 auto;">问 AI</button>
        </div>
        <div id="tutor-out"></div>
      </div>`;
  }
}

async function startPractice(body) {
  const data = await api("/api/training/my/practice/start", { method: "POST", body });
  state.attempt = data.attempt;
  renderAttempt();
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

document.getElementById("pr-cert").addEventListener("change", async (event) => {
  state.prCert = event.target.value || null;
  state.attempt = null;
  renderAttempt();
  await refreshPractice().catch((err) => flash(err.message, true));
});

document.getElementById("start-practice").addEventListener("click", () => {
  if (!state.prCert) { flash("请先选择证书范围。", true); return; }
  startPractice({
    certificate_id: state.prCert,
    knowledge_point_id: document.getElementById("pr-kp").value || null,
  }).catch((err) => flash(err.message, true));
});

document.getElementById("pr-attempt").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === "submit") {
    const picked = [...document.querySelectorAll("#pr-attempt .risk-pick:checked")].map((c) => c.value);
    const attemptId = state.attempt.id;
    try {
      const data = await api(`/api/training/my/practice/${attemptId}/submit`, {
        method: "POST", body: { selected_rule_ids: picked } });
      state.attempt = data.attempt;
      renderAttempt();
      flash("已判分，解析已开放。");
      await refreshPractice();
    } catch (err) {
      document.getElementById("pr-error").textContent = err.message + "（你的选择已保留，可修正后重新提交）";
    }
  } else if (act === "abandon") {
    try {
      await api(`/api/training/my/practice/${state.attempt.id}/skip`, { method: "POST" });
      flash("已跳过：留痕统计，不计入正确率。");
    } catch (err) { flash(err.message, true); }
    state.attempt = null;
    renderAttempt();
    await refreshPractice().catch(() => {});
  } else if (act === "tutor-ask") {
    const q = document.getElementById("tutor-q").value.trim();
    const out = document.getElementById("tutor-out");
    if (!q) { out.innerHTML = `<p class="error">请输入问题。</p>`; return; }
    try {
      const data = await api("/api/training/my/tutor", { method: "POST", body: {
        certificate_id: state.attempt.certificate_id || state.prCert,
        attempt_id: state.attempt.id, question: q } });
      const m = data.message;
      out.innerHTML = `<div class="detail ${m.degraded ? "miss" : "ok"}">${esc(m.answer).replace(/\n/g, "<br>")}</div>
        <div class="muted">引用：${m.citations.map(esc).join("、") || "（无）"}${m.model ? " · 模型：" + esc(m.model) : ""}</div>`;
    } catch (err) {
      out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  } else if (act === "redo-new") {
    startPractice({ certificate_id: state.prCert, knowledge_point_id: btn.dataset.kp || null,
                    rule_id: btn.dataset.rule || null, mode: "new" }).catch((err) => flash(err.message, true));
  } else if (act === "redo-same") {
    startPractice({ certificate_id: state.prCert, knowledge_point_id: btn.dataset.kp || null,
                    rule_id: btn.dataset.rule, seed: parseInt(btn.dataset.seed, 10),
                    mode: "redo_same" }).catch((err) => flash(err.message, true));
  } else if (act === "resume") {
    try {
      const data = await api(`/api/training/my/practice/${btn.dataset.id}`);
      state.attempt = data.attempt;
      renderAttempt();
    } catch (err) { flash(err.message, true); }
  }
});

document.getElementById("wb-body").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === "wb-resume") {
    try {
      const data = await api(`/api/training/my/practice/${btn.dataset.id}`);
      state.attempt = data.attempt;
      state.prCert = state.attempt.certificate_id;
      renderAttempt();
      document.getElementById("pr-attempt").scrollIntoView({ behavior: "smooth" });
    } catch (err) { flash(err.message, true); }
  } else if (act === "wb-practice") {
    state.prCert = btn.dataset.cert;
    state.attempt = null;
    document.getElementById("pr-cert").value = state.prCert;
    await refreshPractice().catch(() => {});
    startPractice({ certificate_id: btn.dataset.cert,
                    knowledge_point_id: btn.dataset.kp || null,
                    rule_id: btn.dataset.rule || null }).catch((err) => flash(err.message, true));
    document.getElementById("pr-attempt").scrollIntoView({ behavior: "smooth" });
  }
});

/* ---------- 学习档案与知识库 ---------- */

function renderProfile() {
  const p = state.profile;
  const sum = document.getElementById("pf-summary");
  const list = document.getElementById("pf-list");
  if (!p) {
    sum.innerHTML = `<p class="muted">选择证书后加载档案。</p>`;
    list.innerHTML = "";
    return;
  }
  const s = p.summary;
  const pct = (v) => (v === null || v === undefined) ? "—" : `${(v * 100).toFixed(0)}%`;
  sum.innerHTML = `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">
    <span class="badge">覆盖 ${s.covered_kps}/${s.total_kps} 知识点（${pct(s.coverage_rate)}）</span>
    <span class="badge">较熟练 ${s.proficient_kps} 个</span>
    <span class="badge gray">作答 ${s.attempts} 次 · 正确率 ${pct(s.accuracy)}</span>
    <span class="badge gray">跳过 ${s.skipped}（不计入正确率）</span>
    <span class="badge gray">笔记 ${s.notes} · 提问 ${s.tutor_questions}</span>
    ${p.goal ? countdownBadge(p.goal.countdown) : ""}
  </div>`;
  if (!p.knowledge_points.length) {
    list.innerHTML = `<p class="muted">该证书下暂无知识点，等待教师按考纲配置后即可记录。</p>`;
    return;
  }
  const MASTERY_BADGE = { proficient: "badge", consolidating: "badge warn",
                          acquainted: "badge gray", unstarted: "badge gray" };
  list.innerHTML = p.knowledge_points.map((k) => {
    const m = k.mastery;
    const noteBlock = k.note
      ? `<div class="detail ok" style="white-space:pre-wrap;">${esc(k.note.content)}</div>
         <div class="muted">笔记更新于 ${esc((k.note.updated_at || "").slice(0, 16).replace("T", " "))}</div>`
      : `<div class="muted">暂无笔记</div>`;
    const last = m.last_scored_at ? ` · 最近作答 ${esc(m.last_scored_at.slice(0, 10))}` : "";
    const flags = [];
    if (m.attempts && m.last_perfect === false) flags.push("最近一次未满分（需复核）");
    if (k.wrong) flags.push(`错题 ${k.wrong}`);
    if (k.tutor_questions) flags.push(`提问 ${k.tutor_questions}`);
    return `<div class="item" id="pf-${esc(k.id)}">
      <div class="item-head">
        <div><strong>${esc(k.code)}</strong> ${esc(k.name)}
          ${k.subject ? `<span class="chip">${esc(k.subject)}</span>` : ""}</div>
        <span class="${MASTERY_BADGE[m.level]}" title="作答 ${m.attempts} 次 · 正确率 ${pct(m.accuracy)} · 最近${m.last_perfect === true ? "满分" : m.last_perfect === false ? "未满分" : "无作答"}">${esc(m.label)}</span>
      </div>
      <div class="muted">作答 ${m.attempts} 次 · 正确率 ${pct(m.accuracy)} · 满分 ${m.perfect}${last}${flags.length ? ` · ${flags.map(esc).join(" · ")}` : ""}</div>
      ${noteBlock}
      <div class="actions">
        <button class="small" data-act="edit-note" data-id="${esc(k.id)}">${k.note ? "改笔记" : "写笔记"}</button>
      </div>
      <div class="inline-form" data-role="note-edit" hidden>
        <textarea data-role="note-text" style="min-height:90px;">${esc(k.note ? k.note.content : "")}</textarea>
        <div class="actions">
          <button class="small primary" data-act="save-note" data-id="${esc(k.id)}">保存笔记</button>
          <button class="small" data-act="cancel-note" data-id="${esc(k.id)}">取消</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

async function refreshProfile() {
  const sel = document.getElementById("pf-cert");
  sel.innerHTML = state.certificates.map((c) =>
    `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  if (!state.pfCert || !state.certificates.some((c) => c.id === state.pfCert)) {
    const goal = state.goals.find((g) => g.status === "active" && g.certificate);
    state.pfCert = goal ? goal.certificate.id
      : (state.certificates[0] ? state.certificates[0].id : null);
  }
  if (state.pfCert) sel.value = state.pfCert;
  if (!state.pfCert) { state.profile = null; renderProfile(); return; }
  try {
    state.profile = (await api(`/api/training/my/profile?certificate_id=${state.pfCert}`)).profile;
  } catch (err) {
    flash(err.message, true);
    state.profile = null;
  }
  renderProfile();
}

document.getElementById("load-profile").addEventListener("click", () => refreshProfile());

document.getElementById("pf-cert").addEventListener("change", (event) => {
  state.pfCert = event.target.value || null;
  refreshProfile();
});

document.getElementById("pf-list").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const scope = document.getElementById(`pf-${btn.dataset.id}`);
  if (!scope) return;
  const act = btn.dataset.act;
  if (act === "edit-note") {
    scope.querySelector("[data-role=note-edit]").hidden = false;
  } else if (act === "cancel-note") {
    scope.querySelector("[data-role=note-edit]").hidden = true;
  } else if (act === "save-note") {
    const content = scope.querySelector("[data-role=note-text]").value;
    try {
      await api(`/api/training/my/knowledge-points/${btn.dataset.id}/note`,
                { method: "PUT", body: { content } });
      flash("笔记已保存（仅本人可见，教师无访问入口）。");
      await refreshProfile();
    } catch (err) { flash(err.message, true); }
  }
});

boot();
