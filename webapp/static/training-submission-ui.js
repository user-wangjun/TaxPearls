/* 提交状态与订正面板（学生端作业页 + 教师端复核列表）。
   自包含：只依赖 /api/assignments/{id}/progress|draft|return 与页面已有的 #exerciseRules、
   #btnSubmitExercise、#submissionList，不改动原有作答与判分逻辑。 */
"use strict";
(() => {
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const $ = id => document.getElementById(id);

  const STATUS = {
    not_started: ["未开始", ""],
    in_progress: ["进行中（有草稿）", ""],
    submitted: ["已提交，等待教师复核", "warn"],
    pending_review: ["已提交，等待教师复核", "warn"],
    done: ["已完成（已复核）", "ok"],
    returned: ["已退回，需要订正", "bad"],
  };

  const cache = new Map();
  let refreshRevision = 0;

  async function api(path, options = {}) {
    const response = await fetch(path, {credentials: "same-origin", ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }
  const put = (path, body) => api(path, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});

  function injectStyle() {
    if ($("tp-sub-style")) return;
    const style = el("style");
    style.id = "tp-sub-style";
    style.textContent = ".tp-sub{margin:10px 0;padding:10px 12px;border:1px solid rgba(127,127,127,.28);border-radius:8px}"
      + ".tp-sub>p{margin:4px 0}"
      + ".tp-sub .tp-acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}"
      + ".tp-sub .tp-reason{border-left:3px solid #c0392b;padding-left:10px;margin:8px 0}"
      + ".tp-sub .tp-ok{border-left:3px solid #2d7a46;padding-left:10px;margin:8px 0}"
      + ".tp-sub .tp-warn{border-left:3px solid #b8860b;padding-left:10px;margin:8px 0}"
      + ".tp-sub-msg{font-size:12px;margin-top:6px}"
      + ".tp-sub-badge{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid rgba(127,127,127,.45);font-size:12px}";
    document.head.append(style);
  }

  function answersFromPage() {
    const box = $("exerciseRules");
    if (!box) return [];
    return Array.from(box.querySelectorAll("input:checked")).map(node => node.value);
  }

  function applyAnswers(answers) {
    const box = $("exerciseRules");
    if (!box) return;
    const wanted = new Set(answers || []);
    for (const input of box.querySelectorAll("input[type=checkbox]")) {
      input.checked = wanted.has(input.value);
    }
  }

  function blocksSubmit(id) {
    return cache.get(id)?.can_submit === false;
  }

  function panel(id, progress) {
    const box = el("div", "tp-sub");
    box.id = "tp-submission-panel";
    const [label, tone] = STATUS[progress.status] || [progress.status, ""];
    const line = el("p");
    line.append(el("span", "tp-sub-badge", label),
      document.createTextNode(` 已提交 ${progress.attempts || 0} 次 · 订正 ${progress.corrections || 0}/${progress.correction_limit} 次`));
    box.append(line);

    if (progress.status === "returned" && progress.return_reason) {
      const reason = el("div", "tp-reason");
      reason.append(el("p", null, "教师退回原因"), el("p", "muted", progress.return_reason));
      if (progress.correct_due_at) reason.append(el("p", "muted", "订正期限：" + new Date(progress.correct_due_at).toLocaleString("zh-CN")));
      box.append(reason);
    } else if (progress.status === "pending_review" || progress.status === "submitted") {
      const note = el("div", "tp-warn");
      note.append(el("p", "muted", progress.retake_policy === "free"
        ? "已提交；当前任务允许自由重交，历史提交按版本保留。"
        : "已提交，等待教师复核。复核前不能重复提交；教师退回后才能订正。"));
      box.append(note);
    } else if (progress.status === "done") {
      const note = el("div", "tp-ok");
      note.append(el("p", "muted", progress.retake_policy === "free"
        ? "教师已复核；当前任务允许自由重交，历史提交按版本保留。"
        : "教师已复核。如需重新提交，请联系任课教师退回。"));
      box.append(note);
    }

    const allowDraft = !blocksSubmit(id);
    const acts = el("div", "tp-acts");
    const message = el("p", "muted tp-sub-msg", "");
    if (allowDraft) {
      const save = el("button", "small", progress.draft_saved_at ? "更新草稿" : "保存草稿");
      const load = el("button", "small", "载入草稿");
      load.disabled = !progress.draft_saved_at;
      const clear = el("button", "small", "清除草稿");
      clear.disabled = !progress.draft_saved_at;
      const reload = async () => {
        try {
          const next = await api(`/api/assignments/${id}/progress`);
          cache.set(id, next);
          box.replaceWith(panel(id, next));
        } catch (error) { message.textContent = error.message; }
      };
      save.onclick = async () => {
        save.disabled = true;
        try {
          const saved = await put(`/api/assignments/${id}/draft`, {answers: answersFromPage()});
          message.textContent = "草稿已保存（服务器时间 " + new Date(saved.saved_at).toLocaleTimeString("zh-CN") + "）";
          await reload();
        } catch (error) { message.textContent = error.message; save.disabled = false; }
      };
      load.onclick = async () => {
        load.disabled = true;
        try {
          const data = await api(`/api/assignments/${id}/draft`);
          if (!data.draft) { message.textContent = "没有可恢复的草稿。"; return; }
          applyAnswers(data.draft.answers);
          message.textContent = "已恢复草稿（" + new Date(data.draft.saved_at).toLocaleString("zh-CN") + "），核对后再提交。";
        } catch (error) { message.textContent = error.message; }
        finally { load.disabled = false; }
      };
      clear.onclick = async () => {
        if (!window.confirm("清除后草稿不再恢复，确定吗？")) return;
        clear.disabled = true;
        try {
          await api(`/api/assignments/${id}/draft`, {method: "DELETE"});
          await reload();
        } catch (error) { message.textContent = error.message; clear.disabled = false; }
      };
      acts.append(save, load, clear);
    }
    if (progress.draft_saved_at) {
      box.append(el("p", "muted", "上次保存草稿：" + new Date(progress.draft_saved_at).toLocaleString("zh-CN")));
    }
    if (acts.children.length) box.append(acts);
    box.append(message);
    return box;
  }

  async function refresh(id) {
    const user = typeof currentUser === "undefined" ? null : currentUser;
    if (user?.role !== "student") return;
    const button = $("btnSubmitExercise");
    const host = $("exerciseBox");
    if (!host || String(host.dataset.assignment) !== String(id)) return;
    const ticket = ++refreshRevision, uid = user.id;
    let progress;
    try {
      progress = await api(`/api/assignments/${id}/progress`);
    } catch {
      return;   // 老师或非本人作业：静默跳过，不干扰原页面
    }
    if (ticket !== refreshRevision || currentUser?.id !== uid
        || String(host.dataset.assignment) !== String(id) || !STATUS[progress.status]) return;
    cache.set(id, progress);
    $("tp-submission-panel")?.remove();
    if (button) {
      button.before(panel(id, progress));
      const blocked = blocksSubmit(id);
      if (blocked) {
        button.disabled = true;
        button.textContent = progress.status === "done" ? "已完成（已复核）"
          : ["submitted", "pending_review"].includes(progress.status) ? "已提交，等待教师复核"
          : progress.status === "returned" && progress.corrections >= progress.correction_limit
            ? "订正次数已用尽" : "当前任务不可提交";
      } else {
        button.disabled = false;
        button.textContent = "提交答案并评分";
      }
    }
  }

  function reviewActions(submission) {
    if (!submission || !submission.assignment_id || !submission.student_id) return null;
    const wrap = el("span", "tp-acts");
    const back = el("button", null, "退回要求订正");
    back.onclick = async () => {
      const reason = window.prompt(
        `退回给 ${submission.display_name || "该学生"} 的原因（学生可见，必填）`, "");
      if (reason === null) return;
      if (!reason.trim()) { window.alert("退回必须写明原因。"); return; }
      const days = window.prompt("订正期限（天，留空表示不设期限）", "");
      let due = null;
      if (days && days.trim()) {
        const count = Number(days);
        if (!Number.isInteger(count) || count <= 0) { window.alert("期限要填正整数天数。"); return; }
        due = new Date(Date.now() + count * 86400000).toISOString();
      }
      back.disabled = true;
      try {
        await post(`/api/assignments/${submission.assignment_id}/return`,
          {student_id: submission.student_id, reason: reason.trim(), correct_due_at: due});
        if (typeof window.loadAssignments === "function") await window.loadAssignments();
      } catch (error) {
        window.alert(error.message);
        back.disabled = false;
      }
    };
    wrap.append(back);
    return wrap;
  }

  function reset() {
    refreshRevision++; cache.clear(); $("tp-submission-panel")?.remove();
  }
  window.tpSubmissionUI = {blocksSubmit, reviewActions, refresh, reset};

  injectStyle();
  window.addEventListener("tp:exercise-open", event => {
    const id = event.detail?.assignment;
    if (id) refresh(id);
  });
  // 提交后按钮态与退回原因要跟着服务器走：点完等一下再刷新一次
  document.addEventListener("click", event => {
    if (event.target?.id !== "btnSubmitExercise") return;
    const host = $("exerciseBox");
    const id = host?.dataset.assignment;
    if (id) window.setTimeout(() => refresh(id), 1200);
  });
})();
