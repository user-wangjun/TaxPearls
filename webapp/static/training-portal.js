"use strict";
window.createTrainingStudent = function(document = window.document, {embedded=false} = {}) {
let disposed=false;const requests=new AbortController();
/* controller body */
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
  if(disposed)throw new DOMException("视图已关闭","AbortError");
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    signal:requests.signal,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if(disposed)throw new DOMException("视图已关闭","AbortError");
  if (!res.ok) {const error=new Error(data.detail || `请求失败（${res.status}）`);error.status=res.status;throw error;}
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
  if(disposed)return;
  if(embedded){window.universityWorkspace?.notify(message,isError);return;}
  const box = document.getElementById(isError ? "app-error" : "goal-msg");
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
  await refreshProfile({ strict: true }).catch(() => {});
  await loadMyFeedback().catch(() => {});
}

/* ---------- 试用反馈（本人隔离） ---------- */

const FB_STATUS_ZH = { new: "已提交", processing: "处理中", resolved: "已处理" };
const FB_CATEGORY_ZH = { bug: "缺陷", suggestion: "建议", content: "内容纠错", other: "其他" };

async function loadMyFeedback() {
  const box = document.getElementById("fb-mine");
  const rows = (await api("/api/training/my/feedback")).feedback;
  box.innerHTML = rows.length ? `<p class="muted">我提交的反馈（仅本人可见）：</p>` + rows.map((f) => `
    <div class="item">
      <div class="item-head">
        <div><span class="badge ${f.status === "resolved" ? "" : "warn"}">${FB_STATUS_ZH[f.status] || esc(f.status)}</span>
          <span class="chip">${FB_CATEGORY_ZH[f.category] || esc(f.category)}</span></div>
        <span class="muted">${esc((f.created_at || "").slice(0, 16).replace("T", " "))}</span>
      </div>
      <div>${esc(f.content)}</div>
      ${f.staff_note ? `<div class="muted">教师回复：${esc(f.staff_note)}</div>` : ""}
    </div>`).join("") : `<p class="muted">还没有提交过反馈。</p>`;
}

document.getElementById("fb-submit").addEventListener("click", async () => {
  const content = document.getElementById("fb-content").value.trim();
  if (!content) { flash("请先写清楚遇到的问题或建议。", true); return; }
  try {
    await api("/api/training/my/feedback", {
      method: "POST",
      body: { category: document.getElementById("fb-category").value, content,
              certificate_id: state.prCert || null },
    });
    document.getElementById("fb-content").value = "";
    flash("反馈已提交；教师处理后可在本卡片看到回复。");
    await loadMyFeedback();
  } catch (err) {
    flash(err.message, true);
  }
});

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
    state.prCert ? api(`/api/training/my/practice/wrong?certificate_id=${encodeURIComponent(state.prCert)}`)
                 : Promise.resolve({ attempts: [] }),
    state.prCert ? api(`/api/training/my/practice?status=open&certificate_id=${encodeURIComponent(state.prCert)}`)
                 : Promise.resolve({ attempts: [] }),
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
    ? "题目来自教师关联的规则仿真题（仿真，非真题）；提交后自动判分并开放解析。"
    : "该证书下教师尚未配置知识点与题目，等待教师上架后即可练习。";
  renderPracticeHint();
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
    const knowledge = (k.knowledge || []).map((r) => {
      const sources = r.sources.map((s) => {
        const title = `${s.title} · ${s.clause_label}`;
        const link = /^https:\/\//.test(s.source_url || "")
          ? `<a href="${esc(s.source_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent);">${esc(title)}</a>` : esc(title);
        const validity = s.current_validity_status || s.validity_status;
        const extraction = s.current_extraction_status || s.extraction_status;
        return `<li>${link}（${validity !== "in_force" || extraction !== "verified" ? "待适用复核" : "来源已复核，适用范围另核"}）
          <div class="muted">地区：${esc(s.region_code || "未注明")} · 有效期：${esc(s.effective_from || "待核对")} 至 ${esc(s.effective_to || "终止日期未注明")}</div>
          ${s.content ? `<details><summary>查看条款原文${s.snapshot_status === "published" ? "（教学版本留存）" : ""}</summary><div style="white-space:pre-wrap;overflow-wrap:anywhere;">${esc(s.content)}</div>
            ${s.source_location ? `<div class="muted">原文位置：${esc(s.source_location)}</div>` : ""}</details>` : ""}</li>`;
      }).join("");
      return `<div class="detail"><strong>${esc(r.rule_code)} · v${esc(r.rule_version)}</strong>
        <div class="muted">规则仿真参考；政策适用仍须复核。</div>
        <div class="muted">${esc(r.threshold_basis)}</div>
        ${r.inputs ? `<details><summary>查看取数口径</summary><ul>${Object.entries(r.inputs).map(([name, meaning]) => `<li>${esc(name)}：${esc(meaning)}</li>`).join("")}</ul></details>` : ""}
        ${sources ? `<ul>${sources}</ul>` : `<p class="muted">尚未关联原文依据，待完善；不据此确认政策适用。</p>`}</div>`;
    }).join("");
    return `<div class="item"><div class="item-head">
      <div><strong>${esc(k.code)}</strong> ${esc(k.name)}
        ${k.high_freq.map((m) => `<span class="badge warn">常考${m.basis_ref ? " · " + esc(m.basis_ref) : ""}</span>`).join("")}
        ${k.rule_links ? "" : `<span class="badge gray">暂无题目</span>`}
      </div>
      <div class="muted">已练 ${k.attempts} 次 · 满分 ${k.perfect} 次</div>
    </div>${k.subject ? `<div class="muted">${esc(k.subject)} · ${esc(k.outline_version || "未标注内容版本")}</div>` : ""}
    ${k.content_version ? `<div class="muted">教学版本：${esc(k.content_version.label)}</div>` : ""}
    ${k.knowledge_update?.pending ? `<p class="muted">公共知识已有更新，练习仍按当前教学版本进行，等待教师核对发布。</p>` : ""}
    ${relChips ? `<div style="margin-top:4px;">${relChips}</div>` : ""}
    ${knowledge ? `<details><summary>规则与政策依据</summary>${knowledge}</details>` : ""}</div>`;
  }).join("");
}

function renderWrong() {
  const box = document.getElementById("pr-wrong");
  box.innerHTML = state.wrong.length ? state.wrong.map((a) => {
    const bank = !!a.bank_question_id;
    const label = bank ? `<span class="chip">${esc(SOURCE_ZH[a.source_type] || a.source_type || "题库题")}</span>` : "";
    const actions = bank
      ? `<button class="small" data-act="bank-new" data-source="${esc(a.source_type || "real")}" data-kp="${esc(a.knowledge_point_id || "")}">再练一道</button>
         <button class="small" data-act="bank-redo" data-q="${esc(a.bank_question_id)}" data-source="${esc(a.source_type || "real")}" data-kp="${esc(a.knowledge_point_id || "")}">原题重做</button>`
      : `<button class="small" data-act="redo-new" data-rule="${esc(a.rule_id)}" data-kp="${esc(a.knowledge_point_id || "")}">再练新题</button>
         <button class="small" data-act="redo-same" data-rule="${esc(a.rule_id)}" data-seed="${esc(a.seed)}" data-kp="${esc(a.knowledge_point_id || "")}">原题重做</button>`;
    return `<div class="item"><div class="item-head">
      <div><span class="badge bad">${a.score} 分</span>${label}
        <span class="muted">${esc(a.scored_at || "")}</span></div>
      <div class="actions">${actions}</div>
    </div></div>`;
  }).join("") : `<p class="muted">暂无错题，保持！</p>`;
}

function renderOpen() {
  const box = document.getElementById("pr-open");
  box.innerHTML = state.open.length ? `<h2 style="margin-top:18px;">未完成的练习</h2>` + state.open.map((a) => `
    <div class="item"><div class="item-head">
      <div class="muted">开始于 ${esc(a.created_at)}</div>
      <button class="small" data-act="practice-resume" data-id="${esc(a.id)}">继续作答</button>
    </div></div>`).join("") : "";
}

const MASTERY_ZH = { proficient: "较熟练", consolidating: "巩固中", acquainted: "初识", unstarted: "未开始" };

function renderWorkbench() {
  const box = document.getElementById("wb-body");
  const dash = state.dashboard;
  if (!dash) { box.innerHTML = ""; return; }
  if (!dash.goals.length) {
    const hosted = typeof window !== "undefined" && window.universityWorkspace;
    const hint = hosted ? dash.hint.replace("下方证书目录", "侧栏的考证目标") : dash.hint;
    box.innerHTML = `<p class="muted">${esc(hint)}</p>${hosted ? '<button class="btn pdf" data-act="workspace-goals">设置考证目标</button>' : ""}`;
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

const SOURCE_ZH = { simulated: "仿真题", real: "真题", recall: "回忆题", mock: "模拟题" };

function bankBadge(a) {
  const label = a.source_type_label || SOURCE_ZH[a.source_type] || a.source_type || "题库题";
  return `<span class="chip">${esc(label)}${a.year ? " · " + esc(a.year) + " 年度" : ""}</span>`;
}

function renderBankAttempt(box, a) {
  if (a.status === "open") {
    const options = (a.options || []).map((o) => `<label style="display:flex;gap:8px;align-items:flex-start;margin:6px 0;">
      <input type="checkbox" class="bank-pick" value="${esc(o.key)}" style="width:auto;margin-top:4px;">
      <span>${esc(o.key)}. ${esc(o.text)}</span></label>`).join("");
    box.innerHTML = `<div class="item">
      <div class="item-head">
        <div>${bankBadge(a)}${a.question_no ? `<span class="muted">${esc(a.question_no)}</span>` : ""}</div>
        <span class="muted">答案与解析在提交判分后开放</span>
      </div>
      <div style="margin:8px 0;white-space:pre-wrap;">${esc(a.stem || "")}</div>
      ${options || `<p class="muted">本题没有选项数据，请联系教师核对。</p>`}
      <div class="muted" style="margin-top:6px;">多选题须完全选对才得分：漏选、错选均不得分（与初级会计客观题评分口径一致）。</div>
      <div class="actions">
        <button class="primary" data-act="submit-bank">提交判分</button>
        <button data-act="abandon">跳过本题（留痕，不计入统计）</button>
      </div>
      <div class="error" id="pr-error"></div>
    </div>`;
    return;
  }
  const r = a.result || {};
  const missed = (r.missed || []).join("、");
  const extra = (r.false_positives || []).join("、");
  box.innerHTML = `<div class="item">
    <div class="item-head">
      <div>${bankBadge(a)}</div>
      <span class="badge ${a.perfect ? "" : "warn"}">${a.score} 分${a.perfect ? " · 满分" : ""}</span>
    </div>
    <div style="margin:6px 0;white-space:pre-wrap;">${esc(a.stem || "")}</div>
    <div class="muted">你的选择：${esc((a.answers || []).join("、") || "（未选）")}</div>
    <div class="muted">标准答案（判分后开放）：${esc((r.answer_keys || []).join("、"))}</div>
    ${missed ? `<div class="detail miss">漏选：${esc(missed)}</div>` : ""}
    ${extra ? `<div class="detail false">错选：${esc(extra)}</div>` : ""}
    <details open><summary>解析</summary><div class="detail">${esc(r.explanation || "（无解析）")}</div></details>
    <div class="actions">
      <button class="small" data-act="bank-new" data-source="${esc(a.source_type || "real")}" data-kp="${esc(a.knowledge_point_id || "")}">再练一道</button>
      <button class="small" data-act="bank-redo" data-q="${esc(a.bank_question_id || "")}" data-source="${esc(a.source_type || "real")}" data-kp="${esc(a.knowledge_point_id || "")}">原题重做</button>
    </div>
    <div class="item" style="margin-top:10px;">
      <strong>AI 答疑（基于本题解析与知识点依据）</strong>
      <div class="muted">回答仅作解读参考；标准答案与判分以系统为准，答疑记录仅本人可见。</div>
      <div class="row" style="margin-top:6px;">
        <input id="tutor-q" placeholder="如：为什么我选的这项不对？依据是什么？">
        <button class="small primary" data-act="tutor-ask" style="flex:0 0 auto;">问 AI</button>
      </div>
      <div id="tutor-out"></div>
    </div>
  </div>`;
}

function renderAttempt() {
  const box = document.getElementById("pr-attempt");
  const a = state.attempt;
  if (!a) { box.innerHTML = ""; return; }
  if (a.bank) {
    renderBankAttempt(box, a);
  } else if (a.status === "open") {
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
          <div class="table-scroll"><table><tr><th>编码</th><th>科目</th><th>期初</th><th>借方</th><th>贷方</th><th>期末</th></tr>${accounts}</table></div></div>
        <div><strong style="font-size:13px;">申报数据</strong>
          <div class="table-scroll"><table><tr><th>项目</th><th>金额</th></tr>${decls}</table></div></div>
        <div><strong style="font-size:13px;">标准化指标</strong>
          <div class="table-scroll"><table><tr><th>指标</th><th>数值</th><th>取数来源</th></tr>${metrics}</table></div></div>
      </div>
      <div class="item"><strong>训练任务：识别涉税风险</strong>
        <div class="muted">先核对上方账务、申报数据与指标，再勾选需要进一步核查的涉税风险（可多选）。选错扣分，漏选不得分；提交后查看判定依据与逐项解析。风险线索不等同于税务违法结论。</div>
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
  if (a.applicability?.notice) {
    box.insertAdjacentHTML("afterbegin", `<div class="detail miss">${esc(a.applicability.notice)}</div>`);
  }
}

async function startPractice(body) {
  const data = await api("/api/training/my/practice/start", { method: "POST", body });
  state.attempt = data.attempt;
  renderAttempt();
  document.getElementById("pr-attempt").scrollIntoView({ behavior: "smooth", block: "start" });
}

function showApp(loggedIn) {
  if(disposed)return;
  if(!loggedIn){state.student=null;location.replace("/");return;}
  document.getElementById("app-view").hidden = !loggedIn;
  document.getElementById("who").textContent = loggedIn
    ? `${state.student.name}（${state.student.student_no}${state.student.college ? " · " + state.student.college : ""}）` : "";
  document.getElementById("header-actions").hidden = !loggedIn;
}

function closePassModal() {
  document.getElementById("pass-modal").hidden = true;
  for (const id of ["old-pass", "new-pass", "new-pass2"]) document.getElementById(id).value = "";
  document.getElementById("pass-error").textContent = "";
}

async function boot() {
  try {
    let me;
    try{me=await api("/api/training/auth/me");}catch{await api("/api/training/membership/session",{method:"POST"});me=await api("/api/training/auth/me");}
    state.student = me.student;
    showApp(true);
    await refresh();
  } catch (error) {
    if(disposed)return;
    if(embedded)throw error;
    if(error.status===401||error.status===403)showApp(false);else flash(error.message,true);
  }
}

document.getElementById("change-pass-btn").addEventListener("click", () => {
  document.getElementById("pass-modal").hidden = false;
  document.getElementById("pass-error").textContent = "";
  document.getElementById("old-pass").focus();
});

document.getElementById("cancel-pass").addEventListener("click", closePassModal);

document.getElementById("save-pass").addEventListener("click", async () => {
  const box = document.getElementById("pass-error");
  box.textContent = "";
  const oldPassword = document.getElementById("old-pass").value;
  const newPassword = document.getElementById("new-pass").value;
  const confirm2 = document.getElementById("new-pass2").value;
  if (!oldPassword || !newPassword) { box.textContent = "当前密码与新密码均必填。"; return; }
  if (newPassword !== confirm2) { box.textContent = "两次输入的新密码不一致。"; return; }
  try {
    await api("/api/training/auth/change-password", {
      method: "POST",
      body: { old_password: oldPassword, new_password: newPassword },
    });
    closePassModal();
    state.student = null;
    if (typeof window !== "undefined" && window.universityWorkspace) { window.universityWorkspace.expired("密码已修改，请用新密码重新登录。"); return; }
    showApp(false);

  } catch (err) {
    box.textContent = err.message;
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  try { await api("/api/training/auth/logout", { method: "POST" }); } catch { /* 会话已失效即可 */ }
  state.student = null;
  showApp(false);
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
    source_type: document.getElementById("pr-source").value || "simulated",
  }).catch((err) => flash(err.message, true));
});

document.getElementById("pr-source").addEventListener("change", renderPracticeHint);

function renderPracticeHint() {
  const source = document.getElementById("pr-source").value;
  const box = document.getElementById("pr-hint");
  if (source === "simulated") {
    box.textContent = state.kps.length
      ? "题目来自教师关联的规则仿真题（纯仿真，非真题）；提交后自动判分并开放解析。"
      : "该证书下教师尚未配置知识点与题目，等待教师上架后即可练习。";
  } else {
    const label = { real: "真题", recall: "回忆题", mock: "模拟题" }[source] || "题库题";
    box.textContent = `${label}来自教师取得书面授权后导入并通过审核的题库，题目标签会写明题源；`
      + "选项作答、完全选对才得分，解析判分后开放。若提示暂无已审核上线的题目，说明教师还没导入或尚未审核通过。";
  }
}

document.getElementById("pr-card").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === "submit-bank") {
    const picked = [...document.querySelectorAll("#pr-attempt .bank-pick:checked")].map((c) => c.value);
    if (!picked.length) {
      document.getElementById("pr-error").textContent = "请先勾选答案（多选题可选多项）。";
      return;
    }
    try {
      const data = await api(`/api/training/my/practice/${state.attempt.id}/submit`, {
        method: "POST", body: { selected_keys: picked } });
      state.attempt = data.attempt;
      renderAttempt();
      flash("已判分，解析已开放。");
      await refreshPractice();
      await refreshProfile().catch(() => {});
    } catch (err) {
      document.getElementById("pr-error").textContent = err.message;
    }
  } else if (act === "submit") {
    const picked = [...document.querySelectorAll("#pr-attempt .risk-pick:checked")].map((c) => c.value);
    const attemptId = state.attempt.id;
    try {
      const data = await api(`/api/training/my/practice/${attemptId}/submit`, {
        method: "POST", body: { selected_rule_ids: picked } });
      state.attempt = data.attempt;
      renderAttempt();
      flash("已判分，解析已开放。");
      await refreshPractice();
      await refreshProfile().catch(() => {});
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
  } else if (act === "bank-new") {
    startPractice({ certificate_id: state.prCert, knowledge_point_id: btn.dataset.kp || null,
                    source_type: btn.dataset.source || "real", mode: "new" })
      .catch((err) => flash(err.message, true));
  } else if (act === "bank-redo") {
    startPractice({ certificate_id: state.prCert, knowledge_point_id: btn.dataset.kp || null,
                    source_type: btn.dataset.source || "real", mode: "redo_same",
                    bank_question_id: btn.dataset.q })
      .catch((err) => flash(err.message, true));
  } else if (act === "practice-resume") {
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

function noteDrafts(list) {
  return Array.from(list.querySelectorAll(".item[id]"), item => {
    const editor = item.querySelector('[data-role="note-edit"]');
    return editor && !editor.hidden ? [item.id, editor.querySelector("textarea").value] : null;
  }).filter(Boolean);
}

function restoreNoteDrafts(list, drafts) {
  for (const [id, text] of drafts) {
    const item = Array.from(list.querySelectorAll(".item[id]")).find(item => item.id === id);
    const editor = item?.querySelector('[data-role="note-edit"]');
    if (editor) { editor.hidden = false; editor.querySelector("textarea").value = text; }
  }
}

function renderProfile() {
  const p = state.profile;
  const sum = document.getElementById("pf-summary");
  const list = document.getElementById("pf-list");
  const drafts = noteDrafts(list);
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
    const tutorBlock = k.tutor_questions
      ? `<div class="actions"><button class="small" data-act="toggle-tutor" data-id="${esc(k.id)}">展开答疑记录（${k.tutor_questions}）</button></div>
         <div data-role="tutor-log" hidden></div>`
      : "";
    return `<div class="item" id="pf-${esc(k.id)}">
      <div class="item-head">
        <div><strong>${esc(k.code)}</strong> ${esc(k.name)}
          ${k.subject ? `<span class="chip">${esc(k.subject)}</span>` : ""}</div>
        <span class="${MASTERY_BADGE[m.level]}" title="作答 ${m.attempts} 次 · 正确率 ${pct(m.accuracy)} · 最近${m.last_perfect === true ? "满分" : m.last_perfect === false ? "未满分" : "无作答"}">${esc(m.label)}</span>
      </div>
      <div class="muted">作答 ${m.attempts} 次 · 正确率 ${pct(m.accuracy)} · 满分 ${m.perfect}${last}${flags.length ? ` · ${flags.map(esc).join(" · ")}` : ""}</div>
      ${noteBlock}
      <div class="actions">
        <button class="small" data-act="show-history" data-id="${esc(k.id)}">作答明细</button>
        <button class="small" data-act="edit-note" data-id="${esc(k.id)}">${k.note ? "改笔记" : "写笔记"}</button>
        ${k.note ? `<button class="small" data-act="delete-note" data-id="${esc(k.id)}">删笔记</button>` : ""}
      </div>
      <div class="inline-form" data-role="kp-history" hidden></div>
      <div class="inline-form" data-role="note-edit" hidden>
        <textarea data-role="note-text" style="min-height:90px;">${esc(k.note ? k.note.content : "")}</textarea>
        <div class="actions">
          <button class="small primary" data-act="save-note" data-id="${esc(k.id)}">保存笔记</button>
          <button class="small" data-act="cancel-note" data-id="${esc(k.id)}">取消</button>
        </div>
      </div>
      ${tutorBlock}
    </div>`;
  }).join("");
  restoreNoteDrafts(list, drafts);
}

async function refreshProfile({strict = false} = {}) {
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
    if (strict) throw err;
  }
  renderProfile();
}

document.getElementById("load-profile").addEventListener("click", () => refreshProfile());

document.getElementById("export-profile").addEventListener("click", async () => {
  if (!state.pfCert) { flash("请先选择证书。", true); return; }
  try {
    const res = await fetch(`/api/training/my/profile/export?certificate_id=${encodeURIComponent(state.pfCert)}`,
      { credentials: "same-origin" });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `导出失败（${res.status}）`);
    }
    const blob = await res.blob();
    const m = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : "profile-export.md";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    flash("档案已导出为 Markdown 文件（导出时刻的快照副本）。");
  } catch (err) {
    flash(err.message, true);
  }
});

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
      scope.querySelector('[data-role="note-edit"]').hidden = true;
      await refreshProfile();
    } catch (err) { flash(err.message, true); }
  } else if (act === "delete-note") {
    if (!confirm("删除这条笔记？删除后不可恢复。")) return;
    try {
      await api(`/api/training/my/knowledge-points/${btn.dataset.id}/note`, { method: "DELETE" });
      flash("笔记已删除。");
      await refreshProfile();
    } catch (err) { flash(err.message, true); }
  } else if (act === "toggle-tutor") {
    const box = scope.querySelector("[data-role=tutor-log]");
    if (!box.hidden) { box.hidden = true; btn.textContent = btn.textContent.replace("收起", "展开"); return; }
    try {
      const rows = (await api(`/api/training/my/tutor/history?knowledge_point_id=${btn.dataset.id}&limit=20`)).messages;
      box.innerHTML = rows.length
        ? rows.map((r) => `<div class="detail" style="margin-top:8px;">
            <div class="muted">${esc((r.created_at || "").slice(0, 16).replace("T", " "))}${r.degraded ? " · 降级回答" : ""}</div>
            <div><strong>问：</strong>${esc(r.question)}</div>
            <div style="white-space:pre-wrap;"><strong>答：</strong>${esc(r.answer)}</div>
          </div>`).join("")
        : `<p class="muted">暂无答疑记录。</p>`;
      box.hidden = false;
      btn.textContent = btn.textContent.replace(/（\d+）/, `（${rows.length}）`).replace("展开", "收起");
    } catch (err) { flash(err.message, true); }
  } else if (act === "show-history") {
    await toggleKpHistory(btn, scope, btn.dataset.id);
  } else if (act === "history-redo") {
    const seed = Number(btn.dataset.seed), ruleId = btn.dataset.rule;
    try {
      await startPractice({ certificate_id: state.pfCert, knowledge_point_id: btn.dataset.id,
                            rule_id: ruleId, seed, mode: "redo_same" });
      flash("已载入原题重做（错题订正），完成后回到本知识点可查看新结果。");
      document.getElementById("pr-card")?.scrollIntoView({ behavior: "smooth" });
    } catch (err) { flash(err.message, true); }
  } else if (act === "history-new") {
    try {
      await startPractice({ certificate_id: state.pfCert, knowledge_point_id: btn.dataset.id, mode: "new" });
      flash("已生成一道新题，去练习区作答。");
      document.getElementById("pr-card")?.scrollIntoView({ behavior: "smooth" });
    } catch (err) { flash(err.message, true); }
  }
});

async function toggleKpHistory(btn, scope, kpId) {
  const box = scope.querySelector("[data-role=kp-history]");
  if (!box.hidden) { box.hidden = true; btn.textContent = "作答明细"; return; }
  box.innerHTML = `<p class="muted">加载中…</p>`;
  box.hidden = false;
  try {
    const rows = (await api(`/api/training/my/practice?certificate_id=${state.pfCert}&kp_id=${kpId}`)).attempts;
    if (!rows.length) {
      box.innerHTML = `<p class="muted">该知识点还没有作答记录。</p>`;
      return;
    }
    const MODE = { new: "新题", redo_same: "原题重做" };
    box.innerHTML = `<div class="muted" style="margin-bottom:6px;">最近 ${rows.length} 次（新到旧）：</div>` +
      rows.map((a) => {
        const scored = a.status === "scored";
        const score = scored && a.result ? `${a.result.score} 分${a.result.score >= 100 ? " · 满分" : ""}`
          : (a.status === "open" ? (a.skipped ? "已跳过" : "未完成") : esc(a.status));
        const mode = MODE[a.mode] || esc(a.mode || "new");
        return `<div class="detail" style="margin-top:6px;">
          <div class="item-head">
            <div class="muted">${esc((a.created_at || "").slice(0, 16).replace("T", " "))} · ${mode}
              ${a.rule_id ? ` · 规则 ${esc(a.rule_id)}` : ""}</div>
            <div><strong>${esc(score)}</strong></div>
          </div>
          <div class="actions">
            ${scored && a.rule_id && a.seed ? `<button class="small" data-act="history-redo" data-id="${esc(kpId)}"
              data-rule="${esc(a.rule_id)}" data-seed="${esc(a.seed)}">原题重做</button>` : ""}
            <button class="small" data-act="history-new" data-id="${esc(kpId)}">再练一道</button>
          </div>
        </div>`;
      }).join("");
    btn.textContent = "收起明细";
  } catch (err) {
    box.hidden = true;
    flash(err.message, true);
  }
}

const ready=boot();

/* controller end */
return {ready,refresh,destroy(){disposed=true;requests.abort();}};
};
