/* AI 备课助手 / 学生任务卡。
   使用统一登录身份，挂载到“课程与作业”视图，不依赖 training-staff.js 或 training-portal.js 的内部状态。 */
"use strict";
(() => {
  const $ = (id, root = document) => root.querySelector("#" + CSS.escape(id));
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const button = (label, act, cls = "", key = "ac") => {
    const node = el("button", cls, label);
    node.type = "button";
    if (act) node.dataset[key] = act;
    return node;
  };

  function injectStyle() {
    if ($("ac-coach-style")) return;
    const style = el("style");
    style.id = "ac-coach-style";
    style.textContent = ".ac-opt{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0}"
      + ".ac-opt>input[type=text]{flex:1;min-width:160px}"
      + ".ac-opt>input[type=radio],.ac-opt>input[type=checkbox]{width:auto;margin:0}"
      + "#ai-coach-card .row,#ai-student-card .row{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}"
      + "#ai-coach-card .row>div{flex:1 1 160px;min-width:150px}"
      + "#ai-coach-card select,#ai-coach-card input,#ai-student-card select{width:100%;box-sizing:border-box}"
      + "#ai-coach-card .ac-opt>input[type=radio],#ai-coach-card .ac-opt>input[type=checkbox]"
      + ",#ai-student-card .ac-opt>input{width:auto;flex:0 0 auto}"
      + "#ai-coach-card .row>div[style*='0 0 auto']{flex:0 0 auto}"
      + "#ai-coach-card h3,#ai-student-card h3{margin:18px 0 6px}"
      + "#ai-coach-card .data-item>input,#ai-coach-card .data-item>textarea,#ai-coach-card .data-item>div>textarea{width:100%;box-sizing:border-box;margin-bottom:6px}"
      + ".ac-block{display:block;padding:10px 0;border-top:1px solid rgba(127,127,127,.18)}"
      + ".ac-block>.row{display:flex;gap:10px;align-items:flex-end}"
      + "#ai-coach-card textarea,#ai-student-card textarea{width:100%;box-sizing:border-box}";
    document.head.append(style);
  }
  let booted = false;
  let role = null;
  let staff = null;
  let student = null;

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
  const post = (path, body) => api(path, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });

  function host() {
    const panel = $("trainingPanel");
    if (panel) return {node: panel, classroom: true};
    const app = $("app-view");
    return app ? {node: app, classroom: false} : null;
  }

  function mount(card) {
    const box = host();
    if (!box) return false;
    if (box.classroom) {
      card.dataset.classroomView = "tasks";
      box.node.prepend(card);
      const panel = $("universityPanel");
      if (panel?.dataset.kind) {
        card.classList.toggle("university-view-hidden", panel.dataset.view !== "tasks");
      }
    } else {
      card.dataset.universityView = "tasks";
      box.node.append(card);
    }
    return true;
  }

  /* ---------------------------- 教师：AI 备课 ---------------------------- */
  function staffCard() {
    const card = el("section", "card");
    card.id = "ai-coach-card";
    const heading = el("h2", null, "AI 备课助手：把教学想法变成学生任务 ");
    heading.append(button("刷新任务", "refresh", "small"));
    card.append(
      heading,
      el("p", "muted", "把备课时写的想法或讲义贴进来（也可载入 .md / .txt），AI 会拆成知识点和客观题。"
        + "生成后必须由你逐题核对、修改，点“发布给学生”之后学生才能看到；AI 内容不会写入考纲知识点库。"),
    );
    const briefRow = el("div", "row");
    const briefCell = el("div");
    briefCell.style.flex = "1";
    const label = el("label", null, "教学想法 / 备课材料");
    label.htmlFor = "ac-brief";
    const area = el("textarea");
    area.id = "ac-brief";
    area.rows = 7;
    area.placeholder = "例如：这节课讲增值税一般计税方法，重点是销项税额与进项税额的确定。学生常把不得抵扣的进项税记错，要强调用于集体福利、个人消费的部分不能抵……";
    briefCell.append(label, area);
    briefRow.append(briefCell);
    card.append(briefRow);

    const controls = el("div", "row");
    const fileCell = el("div");
    const fileLabel = el("label", null, "载入本地文档");
    fileLabel.htmlFor = "ac-file";
    const file = el("input");
    file.type = "file";
    file.id = "ac-file";
    file.accept = ".txt,.md,.markdown,text/plain,text/markdown";
    fileCell.append(fileLabel, file);

    const classCell = el("div");
    const classLabel = el("label", null, "发布到班级");
    classLabel.htmlFor = "ac-class";
    const classes = el("select");
    classes.id = "ac-class";
    classes.append(new Option("全机构学生", ""));
    classCell.append(classLabel, classes);

    const countCell = el("div");
    const countLabel = el("label", null, "题目数量");
    countLabel.htmlFor = "ac-count";
    const count = el("input");
    count.type = "number";
    count.id = "ac-count";
    count.min = "3";
    count.max = "20";
    count.value = "5";
    countCell.append(countLabel, count);

    const kindCell = el("div");
    const kindLabel = el("label", null, "题型");
    kindLabel.htmlFor = "ac-kinds";
    const kinds = el("select");
    kinds.id = "ac-kinds";
    kinds.append(new Option("单选", "single"), new Option("单选 + 多选", "single,multiple"), new Option("多选", "multiple"));
    kindCell.append(kindLabel, kinds);

    const goCell = el("div");
    goCell.style.flex = "0 0 auto";
    goCell.append(button("生成草稿", "generate", "primary"));
    controls.append(fileCell, classCell, countCell, kindCell, goCell);
    card.append(controls);

    const status = el("div", "muted");
    status.id = "ac-status";
    status.setAttribute("role", "status");
    const draftBox = el("div");
    draftBox.id = "ac-draft";
    const listBox = el("div");
    listBox.id = "ac-tasks";
    card.append(status, draftBox, listBox);

    card.addEventListener("click", event => {
      const act = event.target.closest("button[data-ac]")?.dataset.ac;
      if (act) staffAction(act, event.target.closest("button"));
    });
    card.addEventListener("change", event => staffChange(event));
    card.addEventListener("input", event => staffInput(event));
    file.addEventListener("change", () => loadFile(file, area, status));
    return card;
  }

  function loadFile(input, area, status) {
    const picked = input.files?.[0];
    if (!picked) return;
    if (picked.size > 200 * 1024) {
      status.className = "error";
      status.textContent = "文档超过 200KB，请节选主要内容后再载入。";
      input.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "").trim();
      if (!text) {
        status.className = "error";
        status.textContent = "这个文件没有可读取的文字。";
        return;
      }
      area.value = area.value.trim() ? `${area.value.trim()}\n\n${text}` : text;
      status.className = "muted";
      status.textContent = `已载入 ${picked.name}（${area.value.length} 字）。`;
    };
    reader.readAsText(picked, "utf-8");
    input.value = "";
  }

  function setStatus(text, error = false) {
    const node = $("ac-status");
    if (!node) return;
    node.className = error ? "error" : "muted";
    node.textContent = text;
  }

  function staffChange(event) {
    const target = event.target;
    if (target.matches("#ac-draft [data-kp-field], #ac-draft [data-q-field]")) {
      syncDraft(target);
    } else if (target.matches("#ac-draft input[data-answer]")) {
      syncAnswer(target);
    }
  }

  function staffInput(event) {
    const target = event.target;
    if (target.matches("#ac-draft [data-kp-field], #ac-draft [data-q-field]")) syncDraft(target);
  }

  function syncDraft(node) {
    if (!staff?.draft) return;
    const value = node.type === "checkbox" ? node.checked : node.value;
    if (node.dataset.kpField && node.dataset.kp !== undefined) {
      staff.draft.knowledge_points[Number(node.dataset.kp)][node.dataset.kpField] = value;
    }
    if (node.dataset.qField && node.dataset.q !== undefined) {
      const q = staff.draft.questions[Number(node.dataset.q)];
      if (node.dataset.opt !== undefined) {
        q.options[Number(node.dataset.opt)].text = value;
      } else {
        q[node.dataset.qField] = value;
      }
    }
  }

  function syncAnswer(node) {
    const q = staff?.draft?.questions[Number(node.dataset.q)];
    if (!q) return;
    const key = node.dataset.answer;
    if (q.kind === "single") {
      q.answers = node.checked ? [key] : [];
    } else if (node.checked) {
      q.answers = [...new Set([...q.answers, key])].sort();
    } else {
      q.answers = q.answers.filter(item => item !== key);
    }
    paintDraft();
  }

  function paintDraft() {
    const box = $("ac-draft");
    if (!box) return;
    box.replaceChildren();
    if (!staff?.draft) return;
    const draft = staff.draft;
    const head = el("div", "row");
    const titleCell = el("div");
    titleCell.style.flex = "1";
    const titleLabel = el("label", null, "任务标题（学生看到的）");
    titleLabel.htmlFor = "ac-title";
    const title = el("input");
    title.id = "ac-title";
    title.value = draft.title;
    title.addEventListener("input", () => { draft.title = title.value; });
    titleCell.append(titleLabel, title);
    head.append(titleCell);
    box.append(head, el("h3", null, `知识点（${draft.knowledge_points.length} 条，随任务下发，不进考纲库）`));
    draft.knowledge_points.forEach((kp, index) => {
      const item = el("div", "ac-block");
      const name = el("input");
      name.value = kp.name;
      name.dataset.kp = index;
      name.dataset.kpField = "name";
      name.setAttribute("aria-label", `知识点 ${index + 1} 名称`);
      const summary = el("textarea");
      summary.rows = 2;
      summary.value = kp.summary;
      summary.dataset.kp = index;
      summary.dataset.kpField = "summary";
      summary.setAttribute("aria-label", `知识点 ${index + 1} 要点`);
      const line = el("div", "row");
      const diffCell = el("div");
      const diff = el("select");
      diff.dataset.kp = index;
      diff.dataset.kpField = "difficulty";
      diff.setAttribute("aria-label", `知识点 ${index + 1} 难度`);
      for (const level of ["基础", "进阶", "拓展"]) diff.append(new Option(level, level));
      diff.value = kp.difficulty || "基础";
      diffCell.append(diff);
      const remove = button("删除这条知识点", "remove-kp");
      remove.dataset.kp = index;
      line.append(diffCell, remove);
      item.append(name, summary, line);
      if (kp.source_quote) item.append(el("p", "muted", `材料原句：${kp.source_quote}`));
      box.append(item);
    });
    box.append(el("h3", null, `题目（${draft.questions.length} 道）`));
    draft.questions.forEach((q, index) => box.append(questionEditor(q, index)));
    const actions = el("div", "row");
    actions.append(button("保存草稿", "save", "primary"), button("发布给学生", "publish", "primary"),
                   button("丢弃这份草稿", "discard"));
    box.append(actions);
  }

  function questionEditor(q, index) {
    const item = el("div", "ac-block");
    const meta = el("p", "muted", `第 ${index + 1} 题 · ${q.kind === "single" ? "单选" : "多选"}`
      + (q.kp ? ` · ${q.kp}` : ""));
    const stem = el("textarea");
    stem.rows = 2;
    stem.value = q.stem;
    stem.dataset.q = index;
    stem.dataset.qField = "stem";
    stem.setAttribute("aria-label", `第 ${index + 1} 题题干`);
    item.append(meta, stem);
    q.options.forEach((option, optIndex) => {
      const line = el("div", "ac-opt");
      const pick = el("input");
      pick.type = q.kind === "single" ? "radio" : "checkbox";
      pick.name = `ac-answer-${index}`;
      pick.dataset.q = index;
      pick.dataset.answer = option.key;
      pick.value = option.key;
      pick.checked = q.answers.includes(option.key);
      const text = el("input");
      text.type = "text";
      text.value = option.text;
      text.dataset.q = index;
      text.dataset.opt = optIndex;
      text.dataset.qField = "option";
      text.setAttribute("aria-label", `第 ${index + 1} 题选项 ${option.key}`);
      const key = el("strong", null, `${option.key}. `);
      line.append(pick, key, text);
      item.append(line);
    });
    const why = el("textarea");
    why.rows = 2;
    why.value = q.explanation || "";
    why.placeholder = "解析（提交后学生可见）";
    why.dataset.q = index;
    why.dataset.qField = "explanation";
    why.setAttribute("aria-label", `第 ${index + 1} 题解析`);
    item.append(why);
    if (q.source_quote) item.append(el("p", "muted", `材料原句：${q.source_quote}`));
    const remove = button("删除这道题", "remove-q");
    remove.dataset.q = index;
    item.append(remove);
    return item;
  }

  function collectTask(publish) {
    const draft = staff.draft;
    if (!draft) return null;
    const brief = $("ac-brief")?.value.trim() || "";
    const payload = {
      title: (draft.title || "").trim(),
      brief,
      class_id: $("ac-class")?.value || null,
      knowledge_points: draft.knowledge_points.map(kp => ({
        name: kp.name.trim(), summary: (kp.summary || "").trim(),
        difficulty: kp.difficulty || "基础", source_quote: kp.source_quote || "",
      })).filter(kp => kp.name),
      questions: draft.questions.map(q => ({
        kp: q.kp || "", kind: q.kind, stem: q.stem.trim(),
        options: q.options.map(o => ({key: o.key, text: (o.text || "").trim()})),
        answers: q.answers, explanation: (q.explanation || "").trim(),
        source_quote: q.source_quote || "",
      })),
      publish,
    };
    if (!payload.title) throw new Error("请填写任务标题。");
    if (!payload.questions.length) throw new Error("至少保留一道题目。");
    return payload;
  }

  async function staffAction(act, node) {
    if (!staff) return;
    if (act === "refresh") return paintTasks();
    if (act === "generate") return generate(node);
    if (act === "remove-kp") {
      staff.draft.knowledge_points.splice(Number(node.dataset.kp), 1);
      return paintDraft();
    }
    if (act === "remove-q") {
      staff.draft.questions.splice(Number(node.dataset.q), 1);
      return paintDraft();
    }
    if (act === "discard") {
      staff.draft = null;
      $("ac-draft").replaceChildren();
      return setStatus("已丢弃未保存的草稿。");
    }
    if (act === "save" || act === "publish") return persist(act === "publish", node);
    if (act === "edit") return editTask(node.dataset.task);
    if (act === "delete") return deleteTask(node.dataset.task, node);
    if (act === "archive") return archiveTask(node.dataset.task, node);
  }

  async function generate(node) {
    const brief = $("ac-brief")?.value.trim() || "";
    if (brief.length < 20) return setStatus("教学想法至少写 20 字，AI 才有材料可以拆解。", true);
    const kindValue = $("ac-kinds")?.value || "single";
    node.disabled = true;
    setStatus("AI 正在拆解知识点与题目，通常需要十几秒…");
    try {
      const draft = await post("/api/teaching/ai-tasks/draft", {
        brief,
        question_count: Number($("ac-count")?.value) || 5,
        kinds: kindValue.split(","),
      });
      staff.draft = {
        id: null, title: draft.title || "", class_id: null, model: draft.model || "",
        knowledge_points: draft.knowledge_points || [], questions: draft.questions || [],
      };
      paintDraft();
      setStatus(`已生成 ${draft.knowledge_points.length} 条知识点、${draft.questions.length} 道题（模型：${draft.model || "未知"}）。请逐题核对后再发布。`);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      node.disabled = false;
    }
  }

  async function persist(publish, node) {
    let payload;
    try {
      payload = collectTask(publish);
    } catch (error) {
      return setStatus(error.message, true);
    }
    if (!payload) return;
    node.disabled = true;
    setStatus(publish ? "正在发布…" : "正在保存草稿…");
    try {
      const saved = staff.draft.id
        ? await api(`/api/teaching/ai-tasks/${staff.draft.id}`, {
          method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
        })
        : await post("/api/teaching/ai-tasks", payload);
      staff.draft.id = saved.id;
      staff.draft.title = saved.title;
      setStatus(publish ? "已发布，学生端“课程任务”里可以看到这份任务。" : "草稿已保存，学生看不到。");
      await paintTasks();
      if (publish) {
        staff.draft = null;
        $("ac-draft").replaceChildren();
      }
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      node.disabled = false;
    }
  }

  async function editTask(id) {
    try {
      const data = await api(`/api/teaching/ai-tasks/${id}`);
      staff.draft = {
        id: data.id, title: data.title, class_id: data.class_id, model: data.model,
        knowledge_points: data.knowledge_points, questions: data.questions,
      };
      const brief = $("ac-brief");
      if (brief) brief.value = data.brief || brief.value;
      const classSelect = $("ac-class");
      if (classSelect) classSelect.value = data.class_id || "";
      paintDraft();
      setStatus(`正在编辑「${data.title}」。`);
      $("ac-draft").scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function deleteTask(id, node) {
    if (!window.confirm("删除这份草稿？删除后无法恢复。")) return;
    node.disabled = true;
    try {
      await api(`/api/teaching/ai-tasks/${id}`, {method: "DELETE"});
      setStatus("草稿已删除。");
      await paintTasks();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      node.disabled = false;
    }
  }

  async function archiveTask(id, node) {
    if (!window.confirm("归档后学生将不再看到这份任务，已提交的记录保留。确认归档？")) return;
    node.disabled = true;
    try {
      await api(`/api/teaching/ai-tasks/${id}/publish`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({published: false}),
      });
      setStatus("任务已归档。");
      await paintTasks();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      node.disabled = false;
    }
  }

  async function paintTasks() {
    const box = $("ac-tasks");
    if (!box) return;
    box.replaceChildren();
    let items = [];
    try {
      items = (await api("/api/teaching/ai-tasks")).items || [];
    } catch (error) {
      box.append(el("p", "error", `任务列表读取失败：${error.message}`));
      return;
    }
    const head = el("h3", null, `我布置的任务（${items.length}）`);
    box.append(head);
    if (!items.length) {
      box.append(el("p", "muted", "还没有布置过任务。生成草稿并发布后，这里会显示完成情况。"));
      return;
    }
    const statusText = {draft: "草稿（学生不可见）", published: "已发布", archived: "已归档"};
    for (const item of items) {
      const row = el("div", "data-item");
      const info = el("div");
      info.append(el("strong", null, item.title),
        el("p", "muted", `${statusText[item.status] || item.status} · ${item.question_count} 题 · 已提交 `
          + `${item.submitted}/${item.expected} 人` + (item.mean_score === null ? "" : ` · 平均 ${item.mean_score} 分`)));
      const actions = el("div", "row");
      if (item.status === "draft") {
        const edit = button("编辑草稿", "edit");
        edit.dataset.task = item.id;
        actions.append(edit);
        const remove = button("删除", "delete");
        remove.dataset.task = item.id;
        actions.append(remove);
      }
      if (item.status === "published") {
        const archive = button("归档", "archive");
        archive.dataset.task = item.id;
        actions.append(archive);
      }
      row.append(info, actions);
      box.append(row);
    }
  }

  async function renderStaff(container) {
    injectStyle();
    const card = staffCard();
    if (!container.classroom) card.dataset.universityView = "tasks";
    staff = {draft: null};
    if (!mount(card)) return;
    try {
      const classes = await api("/api/classes");
      const select = $("ac-class");
      for (const item of classes) select.append(new Option(item.name, item.id));
    } catch (error) {
      setStatus(`班级列表读取失败：${error.message}`, true);
    }
    await paintTasks();
  }

  /* ---------------------------- 学生：任务作答 ---------------------------- */
  function studentCard() {
    const card = el("section", "card");
    card.id = "ai-student-card";
    card.append(
      el("h2", null, "老师布置的学习任务"),
      el("p", "muted", "老师用 AI 把课堂内容整理成知识点和练习，发布后出现在这里。作答提交后由系统判分并给出解析。"),
    );
    const list = el("div");
    list.id = "at-list";
    const detail = el("div");
    detail.id = "at-detail";
    card.append(list, detail);
    card.addEventListener("click", event => {
      const act = event.target.closest("button[data-at]")?.dataset.at;
      if (!act) return;
      const id = event.target.closest("button").dataset.task;
      if (act === "open") openTask(id);
      if (act === "submit") submitTask(id, event.target.closest("button"));
      if (act === "close") {
        $("at-detail").replaceChildren();
        student.current = null;
      }
    });
    return card;
  }

  async function paintStudentTasks() {
    const box = $("at-list");
    if (!box) return;
    box.replaceChildren();
    let items = [];
    try {
      items = (await api("/api/teaching/ai-tasks")).items || [];
    } catch (error) {
      box.append(el("p", "error", `任务读取失败：${error.message}`));
      return;
    }
    if (!items.length) {
      box.append(el("p", "muted", "暂时没有老师布置的任务。可以先做“税务案例训练与刷题”。"));
      return;
    }
    for (const item of items) {
      const row = el("div", "data-item");
      const info = el("div");
      const state = item.score === null ? "未作答" : `已提交 · ${item.score} 分`;
      info.append(el("strong", null, item.title),
        el("p", "muted", `${item.question_count} 题 · ${state}`));
      const open = button(item.score === null ? "开始作答" : "查看结果与解析", "open", "", "at");
      open.dataset.task = item.id;
      row.append(info, open);
      box.append(row);
    }
  }

  async function openTask(id) {
    const box = $("at-detail");
    box.replaceChildren(el("p", "muted", "正在读取任务…"));
    try {
      const data = await api(`/api/teaching/ai-tasks/${id}`);
      student.current = data;
      paintStudentDetail(data);
      box.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) {
      box.replaceChildren(el("p", "error", error.message));
    }
  }

  function paintStudentDetail(data) {
    const box = $("at-detail");
    box.replaceChildren();
    const done = data.submission;
    box.append(el("h3", null, data.title));
    if (done) {
      box.append(el("p", "ok", `你的得分：${done.score} / 100（${new Date(done.submitted_at).toLocaleString()}）`));
    }
    if (data.knowledge_points.length) {
      box.append(el("h4", null, "本次知识点"));
      const list = el("ul");
      for (const kp of data.knowledge_points) {
        const li = el("li", null, `${kp.name}（${kp.difficulty}）：${kp.summary || "—"}`);
        list.append(li);
      }
      box.append(list);
    }
    const detail = done ? Object.fromEntries(done.detail.map(row => [row.question_id, row])) : null;
    data.questions.forEach((q, index) => {
      const item = el("div", "ac-block");
      item.append(el("p", null, `第 ${index + 1} 题（${q.kind === "single" ? "单选" : "多选"}）${q.stem}`));
      for (const option of q.options) {
        const line = el("label", "ac-opt");
        const pick = el("input");
        pick.type = q.kind === "single" ? "radio" : "checkbox";
        pick.name = `at-${q.id}`;
        pick.value = option.key;
        pick.dataset.question = q.id;
        if (done) {
          const picked = detail[q.id]?.picked || [];
          pick.checked = picked.includes(option.key);
          pick.disabled = true;
        }
        line.append(pick, el("span", null, ` ${option.key}. ${option.text}`));
        item.append(line);
      }
      if (detail?.[q.id]) {
        const row = detail[q.id];
        item.append(el("p", row.correct ? "ok" : "error",
          `${row.correct ? "答对" : "答错"} · 正确答案：${row.answers.join("")}`));
        if (row.explanation) item.append(el("p", "muted", `解析：${row.explanation}`));
      }
      box.append(item);
    });
    const actions = el("div", "row");
    if (!done) {
      const submit = button("提交作答", "submit", "primary", "at");
      submit.dataset.task = data.id;
      actions.append(submit);
    }
    actions.append(button("收起", "close", "", "at"));
    box.append(actions);
  }

  async function submitTask(id, node) {
    const data = student.current;
    if (!data) return;
    const answers = {};
    for (const q of data.questions) {
      const picked = [...document.querySelectorAll(`#at-detail input[data-question="${CSS.escape(q.id)}"]:checked`)]
        .map(input => input.value);
      if (q.kind === "single" && picked.length > 1) {
        window.alert(`第 ${q.position} 题是单选题，只能选一个选项。`);
        return;
      }
      answers[q.id] = picked;
    }
    const blank = data.questions.filter(q => !answers[q.id].length).length;
    if (blank && !window.confirm(`还有 ${blank} 题没有作答，未作答按 0 分计。确定提交？`)) return;
    node.disabled = true;
    try {
      await post(`/api/teaching/ai-tasks/${id}/submit`, {answers});
      await openTask(id);
      await paintStudentTasks();
    } catch (error) {
      window.alert(error.message);
      node.disabled = false;
    }
  }

  async function renderStudent(container) {
    injectStyle();
    student = {current: null};
    const card = studentCard();
    if (!container.classroom) card.dataset.universityView = "tasks";
    if (!mount(card)) return;
    await paintStudentTasks();
  }

  async function init() {
    const box = host();
    if (!box) return false;
    const who = typeof currentUser === "undefined" ? null : currentUser;
    if (!who) return false;
    const studentRole = who.education_role === "student" || who.role === "student";
    const teacherRole = ["teacher", "org_admin"].includes(who.role) || who.education_role === "teacher";
    if (studentRole) {
      role = "student";
      await renderStudent(box);
    } else if (teacherRole) {
      role = "staff";
      await renderStaff(box);
    } else {
      return false;
    }
    return true;
  }

  let attempts = 0;
  function tryBoot() {
    if (booted) return;
    if (typeof currentUser === "undefined" || !currentUser) return;
    const box = host();
    if (!box) return;
    if (box.node.id === "app-view" && box.node.hidden) return;
    booted = true;
    init().then(ok => {
      if (!ok) booted = false;
    }).catch(() => {
      booted = false;
      // 仅在已登录后的数据加载失败时重试；登录前不发送请求。
      if (attempts++ < 20) window.setTimeout(tryBoot, 1500);
    });
  }

  new MutationObserver(tryBoot).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"],
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tryBoot);
  else tryBoot();
})();
