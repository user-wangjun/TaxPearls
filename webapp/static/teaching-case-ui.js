/* 实操案例任务：教师的「发布 → 看交付 → 按量规评分」入口，学生的「五步实操」表单。

   自包含：自行判定身份、自行挂到教学面板；学生侧监听 tp:exercise-open，
   这道作业没挂案例（404）就静默退出，不干扰原有作答页。

   展示口径（与后端一致，前端不得改写）：
   - 量规分与客观题成绩分开展示，不合并成一个课程总分；
   - 未训练的维度是「不适用」，既不按 0 分也不按满分算，页面显示真实分母；
   - 案例的标准结论与量规结果都按任务的答案开放策略下发，前端不提前显示。 */
"use strict";
(() => {
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
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
  // 后端这几个列表接口返回的是裸数组（/api/classes、/api/audits），不是 {items:[…]}
  const asList = (data, ...keys) => {
    if (Array.isArray(data)) return data;
    for (const key of keys) if (Array.isArray(data?.[key])) return data[key];
    return [];
  };
  const when = value => value ? new Date(value).toLocaleString("zh-CN") : "未设置";
  const pad = n => String(n).padStart(2, "0");
  const local = value => {
    if (!value) return "";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "";
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
      + `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  // ---------------------------------------------------------------- 样式

  function injectStyle() {
    if (document.getElementById("case-task-style")) return;
    const style = el("style");
    style.id = "case-task-style";
    style.textContent =
      "#case-task-card .ct-row{border-top:1px solid rgba(127,127,127,.18);padding:10px 0}"
      + "#case-task-card .ct-head{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}"
      + "#case-task-card .ct-acts{display:flex;gap:6px;flex-wrap:wrap}"
      + "#case-task-card .ct-badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid rgba(127,127,127,.45);margin-right:6px}"
      + "#case-task-card .ct-detail{margin-top:8px;padding:8px 10px;border-left:3px solid rgba(127,127,127,.35)}"
      + "#case-task-card .ct-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:8px}"
      + "#case-task-card .ct-form>label{flex:0 1 210px;display:flex;flex-direction:column;gap:3px;font-size:13px}"
      + "#case-task-card .ct-checks{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}"
      + "#case-task-card .ct-checks label{display:flex;gap:4px;align-items:center;font-size:13px}"
      + "#case-task-card select,#case-task-card input[type=text],#case-task-card input[type=datetime-local]{width:100%;box-sizing:border-box}"
      + "#case-task-card .ct-note{margin-top:6px;font-size:12px;opacity:.75}"
      + "#tp-case-panel{margin:12px 0;padding:10px 12px;border:1px solid rgba(127,127,127,.35);border-radius:8px}"
      + "#tp-case-panel .ct-step{margin:10px 0}"
      + "#tp-case-panel textarea{width:100%;box-sizing:border-box;min-height:68px}"
      + "#tp-case-panel .ct-hint{font-size:12px;opacity:.75;margin:2px 0 4px}"
      + "#tp-case-panel .ct-table{width:100%;border-collapse:collapse;font-size:13px}"
      + "#tp-case-panel .ct-table th,#tp-case-panel .ct-table td{border-top:1px solid rgba(127,127,127,.2);padding:4px 6px;text-align:left}";
    document.head.append(style);
  }

  // ---------------------------------------------------------------- 挂载

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
      card.dataset.classroomView = "case-tasks";
      box.node.append(card);
    } else {
      card.dataset.universityView = "case-tasks";
      box.node.append(card);
    }
    return true;
  }

  // ---------------------------------------------------------------- 常量

  const STEPS = ["receive", "identify", "reconcile", "judge", "evidence"];
  const STEP_TEXT = {"receive": "① 接收材料", "identify": "② 识别要素", "reconcile": "③ 勾稽分析",
    "judge": "④ 作出判断", "evidence": "⑤ 提交证据与建议"};
  const DIMS = ["material", "reconcile", "basis", "trace"];
  const DIM_TEXT = {"material": "材料与要素", "reconcile": "勾稽与口径",
    "basis": "判定与依据", "trace": "留痕与建议"};

  function checks(name, entries, labels, selected) {
    const wrap = el("div", "ct-checks");
    for (const key of entries) {
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.value = key;
      box.name = name;
      box.checked = selected.includes(key);
      label.append(box, document.createTextNode(labels[key] || key));
      wrap.append(label);
    }
    return wrap;
  }

  function pickedChecks(scope, name) {
    return Array.from(scope.querySelectorAll(`input[name=${name}]:checked`)).map(node => node.value);
  }

  // ---------------------------------------------------------------- 教师：发布表单

  async function buildPublishForm(card, reload) {
    let certificates = [], classes = [], audits = [];
    try {
      [certificates, classes, audits] = await Promise.all([
        api("/api/training/teaching/certificates").then(d => asList(d, "items")),
        api("/api/classes").then(d => asList(d, "classes", "items")),
        api("/api/audits").then(d => asList(d, "audits", "items")),
      ]);
    } catch (error) {
      card.append(el("p", "muted", "读取证书 / 班级 / 仿真案例失败：" + error.message));
      return;
    }
    if (!certificates.length) {
      card.append(el("p", "muted", "还没有启用的证书，先在教师工作台里维护证书与实操案例。"));
      return;
    }
    if (!classes.length) {
      card.append(el("p", "muted", "你名下还没有班级，先在班级管理里建班并加学生。"));
      return;
    }

    const form = el("div", "ct-detail");
    const certSelect = el("select");
    for (const cert of certificates) certSelect.append(new Option(cert.name, cert.id));
    const caseSelect = el("select");
    const classSelect = el("select");
    for (const cls of classes) classSelect.append(new Option(cls.name, cls.id));
    const auditSelect = el("select");
    for (const audit of audits) {
      auditSelect.append(new Option(
        `${audit.company_name || audit.company || "仿真案例"} · ${audit.period || ""}`
        + `（${String(audit.id).slice(0, 8)}）`, audit.id));
    }
    const titleInput = el("input");
    titleInput.type = "text";
    const dueInput = el("input");
    dueInput.type = "datetime-local";

    async function loadCases() {
      caseSelect.textContent = "";
      let cases = [];
      try {
        const data = await api("/api/training/teaching/publish-cases?certificate_id="
          + encodeURIComponent(certSelect.value));
        cases = asList(data, "items");
      } catch { cases = []; }
      if (!cases.length) {
        caseSelect.append(new Option("（该证书下没有已发布案例）", ""));
        return;
      }
      for (const item of cases) caseSelect.append(new Option(item.name, item.id));
    }
    certSelect.onchange = loadCases;
    await loadCases();

    const field = (text, node) => {
      const label = el("label");
      label.append(document.createTextNode(text), node);
      return label;
    };
    const row = el("div", "ct-form");
    row.append(field("证书", certSelect), field("实操案例（须已发布）", caseSelect),
      field("班级", classSelect), field("数据底座（仿真案例）", auditSelect),
      field("作业标题（留空用案例名）", titleInput), field("截止时间", dueInput));
    const stepChecks = checks("ct-step", STEPS, STEP_TEXT, STEPS);
    const dimChecks = checks("ct-dim", DIMS, DIM_TEXT, DIMS);
    const submit = el("button", "primary", "发布成学生任务");
    form.append(el("div", null, "本次训练哪些步骤"), stepChecks,
      el("div", null, "本次评价哪些维度（未勾选 = 不适用，不计入分母）"), dimChecks,
      row, submit,
      el("p", "ct-note", "发布后会生成一道作业：班级、截止、提交与复核、答案开放都沿用课程任务主链；"
        + "数据底座是学生要勾稽分析的仿真案例，案例本体只提供情境、材料清单、标准结论与评价维度。"));
    card.append(form);

    submit.onclick = async () => {
      if (!caseSelect.value) { window.alert("请先选一个已发布的实操案例。"); return; }
      if (!classSelect.value) { window.alert("请选择班级。"); return; }
      if (!auditSelect.value) { window.alert("请选择数据底座（仿真案例）。"); return; }
      const steps = pickedChecks(form, "ct-step");
      const dimensions = pickedChecks(form, "ct-dim");
      if (!steps.length) { window.alert("至少选一个训练步骤。"); return; }
      if (!dimensions.length) { window.alert("至少选一个评价维度。"); return; }
      submit.disabled = true;
      try {
        await send(`/api/training/teaching/cases/${encodeURIComponent(caseSelect.value)}/publish`,
          "POST", {
            class_id: classSelect.value, audit_id: auditSelect.value,
            title: titleInput.value.trim() || null,
            deadline_at: dueInput.value ? new Date(dueInput.value).toISOString() : null,
            steps, dimensions, published: true,
          });
        window.alert("已发布成作业，可在「课程任务清单」里调整排期与答案开放。");
        await reload();
      } catch (error) {
        window.alert(error.message);
        submit.disabled = false;
      }
    };
  }

  // ---------------------------------------------------------------- 教师：评分

  function scoreRow(dimension, current) {
    const row = el("tr");
    const weight = current?.weight;
    row.append(el("td", null, weight ? `${DIM_TEXT[dimension] || dimension}（权重 ${weight}）`
      : (DIM_TEXT[dimension] || dimension)));
    const cell = el("td");
    const select = el("select");
    select.append(new Option("不适用", ""));
    for (const score of [0, 1, 2]) select.append(new Option(String(score), String(score)));
    select.value = current && current.score !== null && current.score !== undefined
      ? String(current.score) : "";
    const comment = el("input");
    comment.type = "text";
    comment.placeholder = "维度意见（可选）";
    comment.value = (current && current.comment) || "";
    cell.append(select, comment);
    row.append(cell);
    row._dimension = dimension;
    row._select = select;
    row._comment = comment;
    return row;
  }

  function paintTotal(node, value) {
    if (!value) { node.textContent = ""; return; }
    const parts = [`量规总分 ${value.raw} / ${value.max}`];
    if (value.percent !== null && value.percent !== undefined) parts.push(`折合 ${value.percent}%`);
    if (value.not_applicable?.length) {
      parts.push("不适用：" + value.not_applicable.map(key => DIM_TEXT[key] || key).join("、"));
    }
    if (value.scale) parts.push(value.scale);
    node.textContent = parts.join(" · ");
  }

  async function buildScoring(task, holder, students) {
    holder.textContent = "";
    if (!students.length) {
      holder.append(el("p", "ct-note", "这个班级还没有学生，无法评分。"));
      return;
    }
    const picker = el("div", "ct-form");
    const studentSelect = el("select");
    for (const person of students) {
      studentSelect.append(new Option(person.display_name || person.username || person.id,
        person.id));
    }
    const label = el("label");
    label.append(document.createTextNode("学生"), studentSelect);
    picker.append(label);
    const board = el("div", "ct-detail");
    const total = el("p", "ct-note");
    holder.append(picker, board, total);

    async function loadStudent() {
      board.textContent = "";
      const studentId = studentSelect.value;
      if (!studentId) return;
      let rubric, scores, deliveries;
      try {
        [rubric, scores, deliveries] = await Promise.all([
          api(`/api/training/teaching/case-tasks/${task.id}/rubric`),
          api(`/api/training/teaching/case-tasks/${task.id}/rubric-scores`
            + `?student_id=${encodeURIComponent(studentId)}`),
          api(`/api/training/teaching/case-tasks/${task.id}/deliveries`
            + `?student_id=${encodeURIComponent(studentId)}`),
        ]);
      } catch (error) {
        board.append(el("p", "muted", error.message));
        return;
      }
      const table = el("table", "ct-table");
      const head = el("tr");
      head.append(el("th", null, "维度"), el("th", null, "得分 / 意见"));
      table.append(head);
      const rows = [];
      for (const dimension of rubric.dimensions) {
        const current = (scores.scores || []).find(item => item.dimension === dimension.key);
        const row = scoreRow(dimension.key, current);
        rows.push(row);
        table.append(row);
      }
      const save = el("button", "primary", "保存量规评分");
      save.onclick = async () => {
        save.disabled = true;
        try {
          const updated = await send(
            `/api/training/teaching/case-tasks/${task.id}/rubric-scores`, "PUT", {
              student_id: studentId,
              scores: rows.map(row => ({
                dimension: row._dimension,
                score: row._select.value === "" ? null : Number(row._select.value),
                comment: row._comment.value.trim() || null,
              })),
            });
          paintTotal(total, updated.total);
          window.alert("已保存。量规分不写入客观题复核分，两项成绩分开显示。");
        } catch (error) { window.alert(error.message); }
        save.disabled = false;
      };
      board.append(el("p", "ct-note",
        `量规 v${rubric.version} · 满分 ${rubric.max_score}${rubric.note ? " · " + rubric.note : ""}`),
      table, save);
      paintTotal(total, scores.total);

      const filled = (deliveries.items || []).flatMap(item => item.steps || []);
      if (filled.length) {
        board.append(el("div", null, "该学生的交付（五步）"));
        for (const step of filled) {
          board.append(el("div", "ct-step",
            el("div", null, STEP_TEXT[step.step] || step.step),
            el("div", "muted", step.content || "（未填写）")));
        }
      } else {
        board.append(el("p", "ct-note", "该学生还没有填写任何步骤。"));
      }
    }
    studentSelect.onchange = loadStudent;
    await loadStudent();

    // 权重：改了会升版本，已有评分不会被覆盖
    const weights = el("details", "ct-detail");
    weights.append(el("summary", null, "调整各维度权重"));
    const weightForm = el("div", "ct-form");
    const inputs = new Map();
    const rubric = await api(`/api/training/teaching/case-tasks/${task.id}/rubric`).catch(() => null);
    if (rubric) {
      for (const dimension of rubric.dimensions) {
        const input = el("input");
        input.type = "text";
        input.value = String(dimension.weight ?? 1);
        const label = el("label");
        label.append(document.createTextNode(DIM_TEXT[dimension.key] || dimension.key), input);
        weightForm.append(label);
        inputs.set(dimension.key, input);
      }
      const apply = el("button", "small", "保存权重");
      const note = el("p", "ct-note");
      apply.onclick = async () => {
        const dimensions = [...inputs].map(([key, input]) => ({
          key, weight: Number(input.value),
        }));
        if (dimensions.some(item => !Number.isFinite(item.weight) || item.weight <= 0 || item.weight > 10)) {
          window.alert("权重要是大于 0 且不超过 10 的数。"); return;
        }
        apply.disabled = true;
        try {
          const saved = await send(`/api/training/teaching/case-tasks/${task.id}/rubric`, "PUT",
            {dimensions, note: ""});
          note.textContent = saved.rotated_from
            ? `已升到 v${saved.version}：旧评分按旧口径保留，不受影响。`
            : `已保存 v${saved.version}（满分 ${saved.max_score}）。`;
          await loadStudent();
        } catch (error) { window.alert(error.message); }
        apply.disabled = false;
      };
      weights.append(weightForm, apply, note);
      holder.append(weights);
    }
  }

  // ---------------------------------------------------------------- 教师：任务列表

  let classCache = null;
  async function classes() {
    if (!classCache) classCache = await api("/api/classes").then(d => asList(d, "classes", "items"));
    return classCache;
  }

  async function roster(task) {
    // 评分名单只取本班：后端也按同一张名册校验，前端不从全机构捞人
    const list = await classes();
    const found = list.find(item => item.id === task.class_id);
    return found?.students || [];
  }

  async function buildTeacherCard() {
    const card = el("div", "card");
    card.id = "case-task-card";
    const heading = el("h2", null, "实操案例任务");
    const refresh = el("button", "small", "刷新");
    heading.append(refresh);
    card.append(heading, el("p", "muted",
      "把已发布的实操案例发布成班级作业：学生按五步提交过程性交付物，教师按四维量规评价。"
      + "量规分与客观题成绩分开展示，不合并计算。"));
    const list = el("div", "ct-list");
    card.append(list);

    async function reload() {
      list.textContent = "";
      let tasks = [];
      try {
        tasks = asList(await api("/api/training/teaching/case-tasks"), "items");
      } catch (error) {
        list.append(el("p", "muted", error.message));
        return;
      }
      if (!tasks.length) list.append(el("p", "muted", "还没有发布过案例任务。"));
      for (const task of tasks) {
        const row = el("div", "ct-row");
        const head = el("div", "ct-head");
        const main = el("div");
        const bits = [task.assignment_title || "作业"];
        if (task.case_name) bits.push(task.case_name);
        if (task.archived) bits.push("已归档");
        main.append(el("div", null, bits.join(" · ")));
        main.append(el("div", "muted",
          `步骤：${(task.step_labels || []).join("、") || "—"}　维度：${(task.dimension_labels || []).join("、") || "—"}`));
        head.append(main);
        const acts = el("div", "ct-acts");
        const toggle = el("button", "small", task.archived ? "取消归档" : "归档");
        toggle.onclick = async () => {
          toggle.disabled = true;
          try {
            await send(`/api/training/teaching/case-tasks/${task.id}/archive`, "POST",
              {archived: !task.archived});
            await reload();
          } catch (error) { window.alert(error.message); toggle.disabled = false; }
        };
        const score = el("button", "small", "量规评分");
        const holder = el("div", "ct-detail");
        score.onclick = async () => {
          score.disabled = true;
          try {
            await buildScoring(task, holder, await roster(task));
          } catch (error) { window.alert(error.message); }
          score.disabled = false;
        };
        acts.append(score, toggle);
        head.append(acts);
        row.append(head, holder);
        list.append(row);
      }
    }
    refresh.onclick = reload;
    await buildPublishForm(card, reload);
    await reload();
    return card;
  }

  // ---------------------------------------------------------------- 学生：五步表单

  function pairText(item) {
    if (item === null || item === undefined) return "";
    if (typeof item !== "object") return String(item);
    return Object.entries(item)
      .filter(([, value]) => value !== null && value !== "" && value !== undefined)
      .map(([key, value]) => `${key}：${value}`).join("　");
  }

  function studentPanel(view, rubric) {
    const panel = el("div");
    panel.id = "tp-case-panel";
    panel.append(el("div", null, `实操案例：${view.case.name}`));
    if (view.case.scenario) panel.append(el("p", "muted", view.case.scenario));
    if (view.case.desensitization_note) panel.append(el("p", "ct-note", view.case.desensitization_note));
    if (view.case.materials?.length) {
      panel.append(el("div", null, "材料清单"));
      const table = el("table", "ct-table");
      for (const item of view.case.materials) {
        const row = el("tr");
        row.append(el("td", null, item.name || pairText(item)),
          el("td", null, item.note || ""));
        table.append(row);
      }
      panel.append(table);
    }
    if (view.deadline_at) panel.append(el("p", "ct-note", "截止时间：" + when(view.deadline_at)));
    panel.append(el("div", null, "本次要完成的步骤"));
    for (const step of view.steps_detail) {
      const box = el("div", "ct-step");
      box.append(el("div", null, step.label || step.step));
      box.append(el("div", "ct-hint", step.hint || ""));
      const area = el("textarea");
      area.value = step.content || "";
      area.disabled = !view.editable;
      box.append(area);
      if (view.editable) {
        const save = el("button", "small", "保存这一步");
        const state = el("span", "ct-note");
        save.onclick = async () => {
          save.disabled = true;
          try {
            const saved = await send(
              `/api/assignments/${encodeURIComponent(view.task.assignment_id)}/case-delivery`,
              "PUT", {step: step.step, content: area.value});
            state.textContent = "已保存 " + when(saved.updated_at);
          } catch (error) { window.alert(error.message); }
          save.disabled = false;
        };
        box.append(save, state);
      } else if (view.edit_note) {
        box.append(el("div", "ct-note", view.edit_note));
      }
      panel.append(box);
    }
    if (view.standard_conclusion?.length) {
      panel.append(el("div", null, "标准结论（教师参考答案）"));
      for (const item of view.standard_conclusion) {
        panel.append(el("div", "muted", pairText(item)));
      }
    } else if (view.conclusion_note) {
      panel.append(el("p", "ct-note", view.conclusion_note));
    }
    if (rubric && rubric.released) {
      panel.append(el("div", null, "量规评价"));
      const table = el("table", "ct-table");
      const head = el("tr");
      head.append(el("th", null, "维度"), el("th", null, "得分"), el("th", null, "教师意见"));
      table.append(head);
      for (const item of rubric.scores) {
        const row = el("tr");
        row.append(el("td", null, item.label || item.dimension),
          el("td", null, item.score === null ? "不适用" : String(item.score)),
          el("td", null, item.comment || ""));
        table.append(row);
      }
      panel.append(table);
      const value = rubric.total;
      if (value) {
        panel.append(el("p", "ct-note",
          `量规 ${value.raw} / ${value.max}`
          + (value.percent !== null && value.percent !== undefined ? `（${value.percent}%）` : "")
          + (value.not_applicable?.length
            ? " · 不适用：" + value.not_applicable.map(key => DIM_TEXT[key] || key).join("、")
            : "")));
      }
      if (rubric.note) panel.append(el("p", "ct-note", rubric.note));
    } else if (rubric && rubric.note) {
      panel.append(el("p", "ct-note", rubric.note));
    }
    return panel;
  }

  let studentRefreshRevision = 0;
  async function refreshStudent(id) {
    const user = typeof currentUser === "undefined" ? null : currentUser;
    if (user?.role !== "student") return;
    const host = document.getElementById("exerciseBox");
    if (!host || String(host.dataset.assignment) !== String(id)) return;
    const ticket = ++studentRefreshRevision, uid = user.id;
    const current = () => ticket === studentRefreshRevision && currentUser?.id === uid
      && String(host.dataset.assignment) === String(id);
    document.getElementById("tp-case-panel")?.remove();
    let view;
    try {
      view = await api(`/api/assignments/${encodeURIComponent(id)}/case`);
    } catch {
      return;   // 不是实操案例任务：静默，不干扰原页面
    }
    if (!current()) return;
    const rubric = await api(`/api/assignments/${encodeURIComponent(id)}/rubric`).catch(() => null);
    if (!current()) return;
    const panel = studentPanel(view, rubric);
    const button = document.getElementById("btnSubmitExercise");
    if (button) button.before(panel);
    else host.append(panel);
  }

  // ---------------------------------------------------------------- 启动

  let booted = false;
  async function init() {
    const box = host();
    if (!box) return false;
    const who = typeof currentUser === "undefined" ? null : currentUser;
    if (!who) return false;
    const studentRole = who.education_role === "student" || who.role === "student";
    const teacherRole = ["teacher", "org_admin"].includes(who.role) || who.education_role === "teacher";
    if (teacherRole) return mount(await buildTeacherCard());
    if (studentRole) {
      window.addEventListener("tp:exercise-open", event => {
        const id = event.detail?.assignment;
        if (id) refreshStudent(id);
      });
      // 提交后交付物会被锁住：跟着服务器刷新一次
      document.addEventListener("click", event => {
        if (event.target?.id !== "btnSubmitExercise") return;
        const id = document.getElementById("exerciseBox")?.dataset.assignment;
        if (id) window.setTimeout(() => refreshStudent(id), 1200);
      });
      return true;
    }
    return false;
  }

  let attempts = 0;
  function tryBoot() {
    if (booted) return;
    if (typeof currentUser === "undefined" || !currentUser) return;
    if (!host()) return;
    booted = true;
    init().then(ok => { if (!ok) booted = false; }).catch(() => {
      booted = false;
      if (attempts++ < 20) window.setTimeout(tryBoot, 1500);
    });
  }

  injectStyle();
  new MutationObserver(tryBoot).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"],
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tryBoot);
  else tryBoot();
})();
