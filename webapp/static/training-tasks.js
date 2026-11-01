/* 课程任务清单：教师统一查看案例作业、试卷和 AI 课堂练习。
   由统一登录传入身份，随课堂数据刷新；进入作答仍走原入口。 */
"use strict";
(() => {
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const STATE = {draft: "草稿", scheduled: "未到开放时间", published: "进行中", closed: "已截止", archived: "已归档"};
  const MINE = {not_started: "未开始", in_progress: "进行中", submitted: "待复核",
    pending_review: "待复核", returned: "待订正", done: "已完成"};
  const RELEASE = {after_submit: "交卷后开放", after_due: "截止后开放", manual: "教师统一开放"};
  const FILTER_INFO = {
    all: "全部任务，包含草稿、进行中、已截止和已归档的任务。",
    draft: "草稿尚未发布，学生不可见。",
    published: "进行中的任务已发布，且尚未截止。",
    closed: "已截止任务已超过提交期限；历史提交和成绩保留。",
    archived: "已归档任务对学生隐藏；历史提交和成绩保留。",
  };

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
  const send = (path, method, body) => api(path, {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}),
  });
  const when = value => value ? new Date(value).toLocaleString("zh-CN") : "未设置";
  const local = value => {
    if (!value) return "";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "";
    const pad = n => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  let role = null;
  let cache = [];
  let filter = "all";
  let loadError = null;
  let identityRevision = 0, loadRevision = 0;
  const expanded = new Set();

  function injectStyle() {
    if (document.getElementById("task-board-style")) return;
    const style = el("style");
    style.id = "task-board-style";
    style.textContent = "#task-board-card .tb-row{border-top:1px solid rgba(127,127,127,.18);padding:10px 0}"
      + "#task-board-card .tb-row:first-of-type{border-top:0}"
      + "#task-board-card .tb-list{max-height:min(500px,60dvh);overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--line,#aab8c6) transparent}"
      + "#task-board-card .tb-list>.tb-row{min-height:100px;box-sizing:border-box}"
      + "#task-board-card .tb-list::-webkit-scrollbar{width:7px}#task-board-card .tb-list::-webkit-scrollbar-thumb{background:var(--line,#aab8c6);border-radius:8px}"
      + "#task-board-card .tb-head{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}"
      + "#task-board-card .tb-main{flex:1 1 260px;min-width:220px}"
      + "#task-board-card .tb-acts{display:flex;gap:6px;flex-wrap:wrap}"
      + "#task-board-card .tb-badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid rgba(127,127,127,.45);margin-right:6px}"
      + "#task-board-card .tb-detail{margin-top:8px;padding:8px 10px;border-left:3px solid rgba(127,127,127,.35)}"
      + "#task-board-card .tb-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:8px}"
      + "#task-board-card .tb-form>div{flex:0 1 190px}"
      + "#task-board-card input,#task-board-card select{width:100%;box-sizing:border-box}"
      + "#task-board-card .tb-filters{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 4px}"
      + "@media(max-width:600px){#appMain #task-board-card .tb-filters{grid-auto-flow:row;grid-auto-columns:auto;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;border-radius:18px}#appMain #task-board-card .tb-filters>button:last-child:nth-child(odd){grid-column:1/-1}}";
    document.head.append(style);
  }

  function host() {
    const panel = document.getElementById("trainingPanel");
    if (panel) return {node: panel, classroom: true};
    const app = document.getElementById("app-view");
    return app ? {node: app, classroom: false} : null;
  }

  function mount(card) {
    const box = host();
    if (!box) return false;
    if (box.classroom) {
      card.dataset.classroomView = "tasks";
      box.node.prepend(card);
    } else {
      card.dataset.universityView = "tasks";
      box.node.append(card);
    }
    return true;
  }

  function badge(text) { return el("span", "tb-badge", text); }

  function summary(task) {
    const bits = [STATE[task.state] || task.state];
    if (task.kind === "paper") bits.push(`${task.question_count} 题 / ${task.total_points} 分`);
    if (task.course_name) bits.push(task.course_name);
    bits.push(task.class_name || "全机构学生");
    bits.push("截止 " + when(task.due_at));
    return bits.join(" · ");
  }

  function teacherRow(task) {
    if (task.kind === "ai") return window.tpAICoach.createTaskRow(task);
    const row = el("div", "tb-row");
    const head = el("div", "tb-head");
    const main = el("div", "tb-main");
    main.append(el("strong", null, task.title),
      el("p", "muted", summary(task)),
      el("p", "muted", `已交 ${task.submitted_total || 0}/${task.expected} 人 · 已复核 ${task.reviewed_total || 0} 人 · 答案${RELEASE[task.answer_release]}`));
    const acts = el("div", "tb-acts");
    const open = el("button", "btn re", "预览学生视角");
    open.onclick = () => preview(task, row);
    const tune = el("button", null, "任务设置");
    tune.onclick = () => settingsForm(task, row);
    acts.append(open, tune);
    if (task.answer_release === "manual") {
      const release = el("button", null, task.answer_released_at ? "已开放答案" : "开放答案");
      release.disabled = Boolean(task.answer_released_at) || task.state === "archived";
      release.onclick = async () => {
        release.disabled = true;
        try { await send(`/api/training/tasks/${task.kind}/${task.id}/answers/release`, "POST"); await load(); }
        catch (error) { window.alert(error.message); release.disabled = false; }
      };
      acts.append(release);
    }
    const archive = el("button", null, task.state === "archived" ? "恢复任务" : "归档");
    archive.onclick = async () => {
      const confirmText = task.state === "archived"
        ? "恢复后学生重新可见该任务；已提交记录不变。"
        : "归档后学生看不到该任务，已提交记录与成绩保留。";
      if (!window.confirm(confirmText)) return;
      archive.disabled = true;
      try {
        await send(`/api/training/tasks/${task.kind}/${task.id}/archive`, "POST", {archived: task.state !== "archived"});
        await load();
      } catch (error) { window.alert(error.message); archive.disabled = false; }
    };
    acts.append(archive);
    head.append(main, acts);
    row.append(head);
    return row;
  }

  async function preview(task, row) {
    row.querySelector(".tb-detail")?.remove();
    const box = el("div", "tb-detail");
    box.append(el("p", "muted", "正在读取预览…"));
    row.append(box);
    try {
      const data = await api(`/api/training/tasks/${task.kind}/${task.id}/preview`);
      box.replaceChildren(el("p", null, "学生看到的样子（预览，仅教师可见）"),
        el("p", "muted", summary(data.task)),
        el("p", "muted", `学生可提交：${data.task.can_submit ? "可以" : "不可以"} · 答案：${data.task.answers_visible ? "已可查看" : "尚未开放"}`),
        el("p", "muted", data.note));
      // 单案例作业本身就是一道实训题，只列试卷的题目顺序
      if (task.kind === "paper") {
        const list = el("ol");
        for (const question of data.task.questions || []) {
          list.append(el("li", null, `第 ${question.position} 题 · ${question.points} 分 · ${question.title}`));
        }
        if (list.children.length) box.append(el("p", null, "题目顺序"), list);
      }
    } catch (error) {
      box.replaceChildren(el("p", "muted", "预览失败：" + error.message));
    }
  }

  function settingsForm(task, row) {
    row.querySelector(".tb-detail")?.remove();
    const box = el("div", "tb-detail");
    const form = el("div", "tb-form");
    const openCell = el("div");
    const openLabel = el("label", null, "开放时间（留空=发布即可用）");
    const open = el("input");
    open.type = "datetime-local";
    open.value = local(task.open_at);
    openCell.append(openLabel, open);
    const releaseCell = el("div");
    const releaseLabel = el("label", null, "答案开放方式");
    const release = el("select");
    for (const [key, text] of Object.entries(RELEASE)) {
      const option = el("option", null, text);
      option.value = key;
      release.append(option);
    }
    release.value = task.answer_release;
    releaseCell.append(releaseLabel, release);
    const save = el("button", "btn pdf", "保存设置");
    const hint = el("p", "muted", "提交次数当前固定为 1 次；多次提交要等提交版本（订正）流程一起开放。已有学生作答后排期不能再改，只能归档后新建。");
    save.onclick = async () => {
      save.disabled = true;
      try {
        await send(`/api/training/tasks/${task.kind}/${task.id}/settings`, "PUT", {
          open_at: open.value ? new Date(open.value).toISOString() : null,
          submit_limit: 1,
          answer_release: release.value,
        });
        await load();
      } catch (error) { window.alert(error.message); save.disabled = false; }
    };
    form.append(openCell, releaseCell, save);
    box.append(form, hint);
    row.append(box);
  }

  function studentRow(task) {
    const row = el("div", "tb-row");
    const head = el("div", "tb-head");
    const main = el("div", "tb-main");
    const limits = [`限交 ${task.submit_limit} 次`, `已交 ${task.my_attempts || 0} 次`];
    if (task.kind === "paper") limits.push(`已交 ${task.submitted_mine}/${task.question_count} 题`);
    const mine = MINE[task.my_status] || "";
    if (mine) limits.unshift("我的进度：" + mine);
    main.append(el("strong", null, task.title),
      el("p", "muted", summary(task)),
      el("p", "muted", `${limits.join(" · ")} · 答案${RELEASE[task.answer_release]}${task.answers_visible ? "（已可看）" : ""}`));
    if (task.my_status === "returned" && task.my_return_reason) {
      main.append(el("p", "muted", "教师退回原因：" + task.my_return_reason));
    }
    const acts = el("div", "tb-acts");
    const label = task.my_status === "returned" ? "去订正"
      : task.can_submit ? "进入作答"
      : (task.my_status === "done" ? "查看结果" : "查看任务");
    const open = el("button", "btn re", label);
    open.onclick = () => enterTask(task);
    const more = el("button", null, "任务说明");
    more.onclick = () => toggleDetail(task, row);
    acts.append(open, more);
    head.append(main, acts);
    row.append(head);
    if (expanded.has(task.kind + task.id)) row.append(studentDetail(task));
    return row;
  }

  function studentDetail(task) {
    const box = el("div", "tb-detail");
    const lines = [
      `课程：${task.course_name || "未关联课程"}`,
      `班级：${task.class_name || "全机构学生"}`,
      `开放时间：${when(task.open_at)}`,
      `截止时间：${when(task.due_at)}`,
      `提交情况：已提交 ${task.my_attempts || 0} 次 / 限交 ${task.submit_limit} 次，订正 ${task.my_corrections || 0}/${task.correction_limit} 次`,
      `答案开放：${RELEASE[task.answer_release]}${task.answers_visible ? "，当前已可查看" : "，当前不可查看"}`,
    ];
    if (task.kind === "paper") lines.push(`本次试卷 ${task.question_count} 题，合计 ${task.total_points} 分，按题评分。`);
    for (const line of lines) box.append(el("p", "muted", line));
    box.append(el("p", "muted", "任务进度与成绩以服务器记录为准；提交后的复核状态可在学习档案查看。"));
    return box;
  }

  function toggleDetail(task, row) {
    const key = task.kind + task.id;
    if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
    row.querySelector(".tb-detail")?.remove();
    if (expanded.has(key)) row.append(studentDetail(task));
  }

  function enterTask(task) {
    try {
      if (task.kind === "paper") {
        if (typeof classroomUI !== "undefined" && classroomUI.openPaper) classroomUI.openPaper(task.id);
        else window.alert("试卷详情尚未就绪，请在下方「组卷作业」列表打开。");
        return;
      }
      if (typeof window.openExercise === "function") window.openExercise(task.id);
      else window.alert("作答页尚未就绪，请刷新后重试。");
    } catch (error) { window.alert(error.message); }
  }

  function paintList(card) {
    const box = card.querySelector(".tb-list");
    box.replaceChildren();
    card.querySelector(".tb-filters").querySelectorAll("button").forEach(button => {
      const key = button.dataset.filter;
      const count = key === "all" ? cache.length : cache.filter(task => task.state === key).length;
      button.textContent = `${button.dataset.label}（${count}）`;
    });
    const hint = card.querySelector(".tb-filter-hint");
    if (hint) hint.textContent = FILTER_INFO[filter] || "";
    if (loadError) {
      box.append(el("p", "muted", "读取失败：" + loadError));
      return;
    }
    const rows = cache.filter(task => filter === "all" || task.state === filter);
    if (!rows.length) {
      box.append(el("p", "muted", role === "staff"
        ? (filter === "all" ? "还没有课程任务。可在下方创建课堂练习、实训作业或试卷。" : `没有${STATE[filter]}任务。`)
        : "暂无分配给你的课程任务；如果刚入班，请联系任课教师确认名册。"));
      return;
    }
    for (const task of rows) box.append(role === "staff" ? teacherRow(task) : studentRow(task));
  }

  async function load({throwOnError = false} = {}) {
    const card = document.getElementById("task-board-card");
    if (!role || !card) return;
    const identity = identityRevision, ticket = ++loadRevision;
    const current = () => identity === identityRevision && ticket === loadRevision
      && document.getElementById("task-board-card") === card;
    const box = card.querySelector(".tb-list");
    loadError = null;
    box.replaceChildren(el("p", "muted", "正在读取任务…"));
    try {
      const [data, ai] = await Promise.all([api("/api/training/tasks"),
        role === "staff" ? api("/api/teaching/ai-tasks") : Promise.resolve({items: []})]);
      if (!current()) return;
      cache = [...(data.items || []), ...(ai.items || []).map(item => ({...item, kind: "ai", state: item.status}))];
      window.tpAICoach?.useTaskBoard?.();
      paintList(card);
    } catch (error) {
      if (!current()) return;
      cache = [];
      loadError = error.message;
      paintList(card);
      if (throwOnError) throw error;
    }
  }

  function buildCard() {
    injectStyle();
    const card = el("section", "card");
    card.id = "task-board-card";
    const heading = el("h2", null, role === "staff" ? "课程任务清单" : "课程任务（作业与试卷）");
    const refresh = el("button", "small", "刷新任务");
    refresh.onclick = load;
    heading.append(refresh);
    card.append(heading, el("p", "muted", role === "staff"
      ? "集中查看课堂练习、案例作业和试卷，按任务状态筛选。创建与编辑仍在下方对应入口完成。"
      : "这里按截止时间列出要完成的任务。答案是否可看由教师设置决定，不是提交就一定开放。"));
    const filters = el("div", "tb-filters");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "任务状态筛选");
    const options = role === "staff"
      ? [["all", "全部"], ["draft", "草稿"], ["published", "进行中"], ["closed", "已截止"], ["archived", "已归档"]]
      : [["all", "全部"], ["scheduled", "未开放"], ["published", "进行中"], ["closed", "已截止"]];
    for (const [key, text] of options) {
      const button = el("button", "small", text);
      button.onclick = () => {
        filter = key;
        filters.querySelectorAll("button").forEach(node => node.setAttribute("aria-pressed", String(node === button)));
        paintList(card);
      };
      button.type = "button";
      button.dataset.filter = key;
      button.dataset.label = text;
      button.setAttribute("aria-pressed", String(key === filter));
      filters.append(button);
    }
    card.append(filters);
    if (role === "staff") card.append(el("p", "muted tb-filter-hint", FILTER_INFO.all));
    const list = el("div", "tb-list");
    list.tabIndex = 0;
    list.setAttribute("role", "region");
    list.setAttribute("aria-label", "课程任务列表");
    card.append(list);
    return card;
  }

  function setUser(who) {
    identityRevision++;
    cache = []; filter = "all"; loadError = null; expanded.clear();
    document.getElementById("task-board-card")?.remove();
    const studentRole = who?.role === "student" || who?.education_role === "student";
    const teacherRole = who?.role === "teacher" || who?.education_role === "teacher"
      || who?.education_role === "admin";
    role = studentRole ? "student" : teacherRole ? "staff" : null;
    if (role && host()) mount(buildCard());
  }

  window.tpTrainingTasks = {setUser, refresh: load};
})();
