"use strict";

// All case/roster text is rendered as text, never interpreted as HTML.
window.createTrainingClassroom = function ({api, el, getUser, openExercise, refresh, showError}) {
  const $ = id => document.getElementById(id);
  let data = {classes: [], students: [], audits: [], papers: []};
  let editingClass = null, draft = [], selectedPaper = null, paperRequest = 0;
  const teacher = () => getUser()?.role === "teacher";
  const dueText = value => value ? new Date(value).toLocaleString() : "无截止时间";
  const status = item => !item.published ? "草稿 / 已撤回" : item.deadline_passed ? "已截止" : "已发布";
  const localInput = value => {
    if (!value) return "";
    const date = new Date(value);
    const pad = n => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };
  function deadlineInput(input) {
    if (!input.value) return null;
    const date = new Date(input.value);
    if (!Number.isFinite(date.getTime())) throw new Error("请填写有效截止时间，或留空表示无截止。");
    return date.toISOString();
  }
  function fillSelect(id, entries, fallback) {
    const select = $(id), saved = select.value;
    select.textContent = "";
    for (const [value, text] of entries) {
      const option = el("option", null, text); option.value = value; select.append(option);
    }
    select.value = entries.some(([value]) => value === saved) ? saved : fallback;
  }
  async function mutation(button, action, fieldset = null) {
    const uid = getUser()?.id;
    if (button.disabled) return;
    button.disabled = true; if (fieldset) fieldset.disabled = true;
    try { await action(uid); }
    catch (error) { if (getUser()?.id === uid) showError(error.message); }
    finally { if (getUser()?.id === uid) { button.disabled = false; if (fieldset) fieldset.disabled = false; } }
  }
  function roster(preserve = false) {
    const box = $("classRoster");
    const included = new Set(preserve ? [...box.querySelectorAll("input:checked")].map(n=>n.value) : editingClass?.students.map(s => s.id) || []);
    box.textContent = "";
    // Include existing inactive members so saving does not silently remove them.
    const people = new Map(data.students.map(s => [s.id, s]));
    for (const person of editingClass?.students || []) if (!people.has(person.id)) people.set(person.id, person);
    for (const person of people.values()) {
      const label = el("label"), check = el("input"); check.type = "checkbox"; check.value = person.id;
      check.checked = included.has(person.id);
      label.append(check, document.createTextNode(` ${person.display_name}（${person.username}）`)); box.append(label);
    }
    if (!people.size) box.append(el("p", "muted", "暂无有效学生账号；需先由管理员创建账号。空班级不会自动公开作业。"));
  }
  function editClass() {
    const selected = data.classes.find(c => c.id === $("classEditor").value);
    editingClass = selected ? {...selected} : null;
    $("className").value = selected?.name || "";
    $("classEditStatus").textContent = selected ? `编辑名册 v${selected.revision}；移出成员会撤回其班级作业访问，历史提交仍保留。` : "新建班级：可先保存空班级，再编辑名册。";
    roster();
  }
  $("classEditor").onchange = editClass;
  $("btnReloadRoster").onclick = async () => { await refresh(); editClass(); };
  $("btnResetClass").onclick = () => { $("classEditor").value = ""; editClass(); };
  $("btnSaveClass").onclick = () => mutation($("btnSaveClass"), async uid => {
    const body = {name: $("className").value.trim(), student_ids: [...$("classRoster").querySelectorAll("input:checked")].map(n => n.value)};
    if (!body.name) throw new Error("请填写班级名称。");
    const existing = editingClass;
    if (existing) body.revision = existing.revision;
    const result = await api(existing ? "/api/classes/" + existing.id : "/api/classes", {
      method: existing ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    });
    if (getUser()?.id !== uid) return;
    await refresh(); $("classEditor").value = result.id; editClass();
  }, $("classEditorControls"));

  function draftRows() {
    const box = $("paperDraft"); box.textContent = "";
    draft.forEach((item, index) => {
      const card = el("div", "training-card");
      card.append(el("p", null, `第 ${index+1} 题 · ${item.label} · ${item.points} 分 · 每项误报扣 ${item.false_positive_penalty} 分`));
      const row = el("div", "training-actions");
      for (const [label, move] of [["上移", -1], ["下移", 1]]) {
        const button = el("button", null, label); button.type = "button";
        button.disabled = index + move < 0 || index + move >= draft.length;
        button.onclick = () => { [draft[index], draft[index+move]] = [draft[index+move], draft[index]]; draftRows(); }; row.append(button);
      }
      const remove = el("button", null, "移除本题"); remove.type = "button";
      remove.onclick = () => { draft.splice(index, 1); draftRows(); }; row.append(remove); card.append(row); box.append(card);
    });
    if (!draft.length) box.append(el("p", "muted", "尚未选择案例。请先核对案例完整答案，再添加到试卷。"));
    $("paperComposeStatus").textContent = `已选 ${draft.length}/30 题，总分值 ${draft.reduce((n, item) => n+item.points, 0)}；保存后题目与分值冻结，改变组成须另建新卷。`;
  }
  $("btnAddPaperCase").onclick = () => {
    try {
      const audit = data.audits.find(a => a.id === $("paperAudit").value);
      if (!audit) throw new Error("请选择仿真案例。");
      if (draft.length >= 30) throw new Error("每卷最多 30 个案例。");
      if (draft.some(i => i.audit_id === audit.id)) throw new Error("同一案例不能重复加入试卷。");
      const points = Number($("paperPoints").value), penalty = Number($("paperPenalty").value);
      if (!Number.isFinite(points) || points <= 0 || points > 1000000) throw new Error("题目分值须大于 0 且不超过 1000000。");
      if (!Number.isFinite(penalty) || penalty < 0 || penalty > 100) throw new Error("每项误报扣分须为 0–100。");
      draft.push({audit_id: audit.id, points, false_positive_penalty: penalty, label: `${audit.company_name} · ${audit.period} · ${audit.id.slice(-6)}`}); draftRows();
    } catch (error) { showError(error.message); }
  };
  $("btnClearPaper").onclick = () => { draft = []; draftRows(); };
  function savePaper(button, published) {
    return mutation(button, async uid => {
      const title = $("paperTitle").value.trim(), scope = $("paperClass").value;
      if (!title || !draft.length) throw new Error("请填写试卷标题并至少添加一个案例。");
      if (scope === "__choose__") throw new Error("请明确选择发布班级或全机构学生。");
      const body = {title, class_id: scope || null, deadline_at: deadlineInput($("paperDeadline")), published,
        items: draft.map(({label, ...item}) => item)};
      await api("/api/papers", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      if (getUser()?.id !== uid) return;
      draft = []; draftRows(); await refresh();
      $("paperComposeStatus").textContent = published ? "试卷已发布；所选范围的学生可见。" : "试卷草稿已保存，学生不可见；可在下方预览后发布。";
    }, $("paperComposerControls"));
  }
  $("btnSavePaperDraft").onclick = () => savePaper($("btnSavePaperDraft"), false);
  $("btnPublishPaper").onclick = () => savePaper($("btnPublishPaper"), true);
  $("btnRefreshTraining").onclick = () => refresh();

  function settingsControls(item, kind) {
    const details = el("details"), summary = el("summary", null, "发布与截止设置"); details.append(summary);
    const row = el("div", "training-actions"), label = el("label", "training-deadline", "截止时间（本机时区，留空为不限）");
    const input = el("input"); input.type = "datetime-local"; input.step = "1"; input.value = localInput(item.deadline_at);
    input.setAttribute("aria-label", item.title + " 截止时间"); label.append(input); row.append(label);
    const update = (button, published) => mutation(button, async uid => {
      await api(`/api/${kind}/${item.id}${kind === "assignments" ? "/settings" : ""}`, {
        method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({revision: item.revision, published, deadline_at: deadlineInput(input)})
      });
      if (getUser()?.id === uid) await refresh();
    });
    const publish = el("button", null, item.published ? "撤回发布" : "发布"), save = el("button", null, "保存截止时间");
    publish.type = save.type = "button"; publish.onclick = () => update(publish, !item.published); save.onclick = () => update(save, item.published);
    row.append(publish, save); details.append(row); return details;
  }
  function paintPaper(item) {
    const box = $("paperDetail"); box.hidden = false; box.textContent = "";
    box.append(el("h2", null, item.title), el("p", "muted", `${status(item)} · 截止：${dueText(item.deadline_at)} · 截止以服务器提交校验为准`));
    if (item.progress) {
      const p = item.progress;
      box.append(el("p", "training-progress", `已完成 ${p.completed}/${p.case_count} 题 · 已得 ${p.earned_points}/${p.total_points} 分值 · 总评分：${p.score === null ? "待全部题目完成" : p.score + " 分"}`));
    }
    for (const question of item.items) {
      const card = el("div", "training-card");
      card.append(el("p", null, `第 ${question.position} 题 · ${question.points} 分值 · ${question.title}`));
      if (item.progress) card.append(el("p", "muted", question.score === null ? "未提交" : `本题评分：${question.score} 分`));
      const button = el("button", null, "打开本题 / 查看评分"); button.type = "button";
      button.onclick = () => openExercise(question.id); card.append(button); box.append(card);
    }
  }
  async function openPaper(id) {
    const request = ++paperRequest, uid = getUser()?.id;
    selectedPaper = null; $("paperDetail").hidden = true; $("paperDetail").textContent = "";
    try {
      const item = await api("/api/papers/" + id);
      if (request !== paperRequest || uid !== getUser()?.id) return;
      selectedPaper = id; paintPaper(item); $("paperDetail").scrollIntoView({block: "start"});
    } catch (error) { if (request === paperRequest && uid === getUser()?.id) showError(error.message); }
  }
  function render(next) {
    data = next;
    $("classManager").hidden = $("paperComposer").hidden = !teacher();
    const list = $("classList"); list.textContent = "";
    for (const item of data.classes) list.append(el("p", null, `${item.name} · ${item.students?.length ?? item.member_count} 人`));
    if (!data.classes.length) list.append(el("p", "muted", teacher() ? "暂无自己管理的班级。" : "尚未加入班级；全机构公开作业仍可访问。"));
    if (teacher()) {
      const choices = data.classes.map(c => [c.id, c.name]);
      fillSelect("classEditor", [["", "新建班级"], ...choices], "");
      fillSelect("assignmentClass", [["", "全机构学生（不限制班级）"], ...choices], "");
      fillSelect("paperClass", [["__choose__", "请选择发布范围"], ["", "全机构学生（不限制班级）"], ...choices], "__choose__");
      const synthetic = data.audits.filter(a => a.company_name.includes("仿真") || a.company_name.includes("纯合成测试") || (a.taxpayer_id || "").toUpperCase().includes("TEST"));
      fillSelect("paperAudit", [["", "请选择仿真案例"], ...synthetic.map(a => [a.id, `${a.company_name} · ${a.period} · ${a.id.slice(-6)}`])], "");
      if (!editingClass) roster(true);
      else if (data.classes.find(c => c.id === editingClass.id)?.revision !== editingClass.revision)
        $("classEditStatus").textContent = "名册版本已变化；保留当前编辑内容。请刷新名册后重新编辑，保存不会覆盖新版本。";
    }
    const papers = $("paperList"); papers.textContent = "";
    for (const item of data.papers) {
      const card = el("div", "training-card");
      const scope = item.class_id ? data.classes.find(c => c.id === item.class_id)?.name || "所属班级" : "全机构学生";
      card.append(el("h3", null, item.title), el("p", "muted", `${status(item)} · ${scope} · ${item.items.length} 题 · 截止：${dueText(item.deadline_at)}`));
      if (item.progress) card.append(el("p", "training-progress", `已完成 ${item.progress.completed}/${item.progress.case_count} 题 · 总评分：${item.progress.score === null ? "未完成" : item.progress.score + " 分"}`));
      const view = el("button", null, "打开试卷 / 查看进度"); view.type = "button"; view.onclick = () => openPaper(item.id); card.append(view);
      if (teacher()) card.append(settingsControls(item, "papers")); papers.append(card);
    }
    if (!data.papers.length) papers.append(el("p", "muted", "暂无可访问的组卷作业。"));
    if (selectedPaper) {
      const item = data.papers.find(p => p.id === selectedPaper);
      if (item) paintPaper(item);
      else { selectedPaper = null; paperRequest++; $("paperDetail").hidden = true; $("paperDetail").textContent = ""; }
    }
  }
  function reset() {
    paperRequest++; selectedPaper = null; editingClass = null; draft = [];
    data = {classes: [], students: [], audits: [], papers: []};
    for (const id of ["classRoster", "classList", "paperList", "paperDetail"]) $(id).textContent = "";
    $("paperDetail").hidden = true; $("classManager").hidden = $("paperComposer").hidden = true;
    $("classEditorControls").disabled = $("paperComposerControls").disabled = false;
    $("className").value = ""; $("paperClass").value = "__choose__"; $("paperDeadline").value = "";
    draftRows();
  }
  return {render, reset, deadlineInput, dueText, status, invalidatePending:()=>{paperRequest++;},
    singleControls: item => teacher() && item.created_by === getUser()?.id ? settingsControls(item, "assignments") : null};
};
