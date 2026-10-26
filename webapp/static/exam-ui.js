/* 独立在线考试（FR-U07）：教师端「出卷 → 发布 → 监考」，学生端「进入 → 作答 → 交卷 → 看分」。

   自包含：自行判定身份、自行建卡挂到教学面板（data-university-view="exams"），
   不改动既有页面结构，也不写 hidden（applyView 不会复位 hidden，写了就再也显示不出来）。

   四条口径前端不得改写（与后端一致）：
   1. 剩余时间一律以服务端 remaining_seconds 为准，前端只做本地倒计时显示，到点由服务端收卷；
   2. 取卷接口不带标准答案与解析，成绩与解析按发布策略下发，未发布时前端不提前显示；
   3. 防作弊只上报事件（切页/复制/粘贴/右键），不计分、不自动判定，由教师在监考台看；
   4. 客观题是"选择集合完全一致才得分"，前端不做任何本地判分提示。 */
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
    method,
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  const query = (path, params) => api(path + "?" + new URLSearchParams(params).toString());
  const when = value => (value ? new Date(value).toLocaleString("zh-CN") : "未设置");
  const pad = n => String(n).padStart(2, "0");
  const clock = seconds => {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
  };

  const PAPER_TEXT = {draft: "草稿", published: "已发布", closed: "已结束", archived: "已归档"};
  const PHASE_TEXT = {draft: "草稿", scheduled: "未开始", open: "进行中", ended: "已结束",
    closed: "已结束", archived: "已归档"};
  const SESSION_TEXT = {in_progress: "作答中", submitted: "已交卷", forced: "已收卷", not_started: "未进入"};
  const RELEASE_TEXT = {manual: "教师发布后可见", after_submit: "交卷后即可查看", after_close: "考试结束后查看"};

  // ---------------------------------------------------------------- 样式

  function injectStyle() {
    if (document.getElementById("exam-ui-style")) return;
    const style = el("style");
    style.id = "exam-ui-style";
    style.textContent =
      "#exam-ui-card .ex-row{border-top:1px solid rgba(127,127,127,.18);padding:10px 0}"
      + "#exam-ui-card .ex-head{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}"
      + "#exam-ui-card .ex-acts{display:flex;gap:6px;flex-wrap:wrap}"
      + "#exam-ui-card .ex-badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;"
      + "border:1px solid rgba(127,127,127,.45);margin-right:6px}"
      + "#exam-ui-card .ex-panel{margin-top:8px;padding:8px 10px;border-left:3px solid rgba(127,127,127,.35)}"
      + "#exam-ui-card .ex-grid{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}"
      + "#exam-ui-card .ex-grid>div{display:flex;flex-direction:column;gap:2px;min-width:150px;flex:1}"
      + "#exam-ui-card .ex-note{font-size:13px;opacity:.85}"
      + "#exam-ui-card .ex-timer{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}"
      + "#exam-ui-card .ex-timer.urgent{color:var(--red,#c0392b)}"
      + "#exam-ui-card .ex-question{padding:10px 0;border-top:1px solid rgba(127,127,127,.18)}"
      + "#exam-ui-card .ex-options{display:flex;flex-direction:column;gap:4px;margin-top:6px}"
      + "#exam-ui-card .ex-options label{display:flex;gap:6px;align-items:flex-start;cursor:pointer}"
      + "#exam-ui-card .ex-answer{margin-top:4px;font-size:13px}"
      + "#exam-ui-card .ex-answer.ok{color:var(--green,#1e7e34)}"
      + "#exam-ui-card .ex-answer.no{color:var(--red,#c0392b)}"
      + "#exam-ui-card table.ex-table{width:100%;border-collapse:collapse}"
      + "#exam-ui-card table.ex-table th,#exam-ui-card table.ex-table td{text-align:left;padding:4px 8px;"
      + "border-bottom:1px solid rgba(127,127,127,.18);font-size:13px}";
    document.head.append(style);
  }

  // ---------------------------------------------------------------- 教师端

  function teacherCard(college) {
    const card = el("div", "card sheet");
    card.id = "exam-ui-card";
    card.dataset.universityView = "exams";
    card.append(el("h2", null, "在线考试"));
    card.append(el("p", "muted", "在线考试独立成卷、独立判分，不进作业主链：题目进卷即快照，发布后卷面冻结；"
      + "名单以平台账号为准，导入时查不到的学号会逐条列出。成绩与解析按发布策略下发。"));
    const status = el("p", "muted");
    const list = el("div", "muted", "加载中…");
    const actions = el("div", "ex-acts");
    const refresh = el("button", "small", "刷新");
    const create = el("button", "small primary", "＋ 新建考试");
    actions.append(refresh, create);
    card.append(actions, status, el("div", "ex-row"), list);

    const form = el("div", "ex-panel");
    form.hidden = true;
    const field = (label, input) => {
      const box = el("div");
      const text = el("label", null, label);
      box.append(text, input);
      if (!input.id) input.id = "ex-" + Math.random().toString(36).slice(2, 8);
      text.htmlFor = input.id;
      return box;
    };
    const titleInput = el("input");
    const durationInput = el("input");
    durationInput.type = "number";
    durationInput.value = "60";
    durationInput.min = "1";
    durationInput.max = "600";
    const openInput = el("input");
    openInput.type = "datetime-local";
    const closeInput = el("input");
    closeInput.type = "datetime-local";
    const lateInput = el("input");
    lateInput.type = "number";
    lateInput.value = "0";
    lateInput.min = "0";
    const passInput = el("input");
    passInput.type = "number";
    passInput.min = "0";
    const releaseSelect = el("select");
    for (const [value, text] of [["manual", "教师发布后可见"], ["after_submit", "交卷后即可查看"],
      ["after_close", "考试结束后查看"]]) {
      const option = el("option", null, text);
      option.value = value;
      releaseSelect.append(option);
    }
    const shuffleQuestions = el("input");
    shuffleQuestions.type = "checkbox";
    const shuffleOptions = el("input");
    shuffleOptions.type = "checkbox";
    const descInput = el("input");
    const grid = el("div", "ex-grid");
    grid.append(field("考试名称（必填）", titleInput), field("时长（分钟）", durationInput),
      field("开放时间（可空）", openInput), field("结束时间（可空）", closeInput),
      field("迟到宽限（分钟）", lateInput), field("及格线（可空）", passInput),
      field("成绩开放策略", releaseSelect));
    const toggles = el("div", "ex-grid");
    const qBox = el("label", null, " "), oBox = el("label", null, " ");
    qBox.prepend(shuffleQuestions, document.createTextNode(" 题目乱序"));
    oBox.prepend(shuffleOptions, document.createTextNode(" 选项乱序"));
    toggles.append(qBox, oBox);
    const submit = el("button", "small primary", "创建");
    const cancel = el("button", "small", "收起");
    submit.onclick = async () => {
      submit.disabled = true;
      try {
        await send("/api/training/exams/papers", "POST", {
          title: titleInput.value.trim(), college,
          description: descInput.value.trim() || null,
          duration_minutes: Number(durationInput.value || 60),
          open_at: openInput.value ? new Date(openInput.value).toISOString() : null,
          close_at: closeInput.value ? new Date(closeInput.value).toISOString() : null,
          late_entry_minutes: Number(lateInput.value || 0),
          pass_score: passInput.value === "" ? null : Number(passInput.value),
          answer_release: releaseSelect.value,
          shuffle_questions: shuffleQuestions.checked,
          shuffle_options: shuffleOptions.checked,
        });
        form.hidden = true;
        status.textContent = "考试已创建，接下来加题与导入名单。";
        await reload();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        submit.disabled = false;
      }
    };
    cancel.onclick = () => { form.hidden = true; };
    const acts = el("div", "ex-acts");
    acts.append(submit, cancel);
    form.append(grid, field("考试说明（选填）", descInput), toggles, acts);
    card.append(form);

    create.onclick = () => { form.hidden = !form.hidden; if (!form.hidden) titleInput.focus(); };

    async function reload() {
      status.textContent = "正在读取…";
      try {
        const {papers} = await query("/api/training/exams/papers", {college});
        renderList(papers);
        status.textContent = papers.length ? `共 ${papers.length} 场考试。` : "还没有考试，先新建一场。";
      } catch (error) {
        status.textContent = error.message;
      }
    }
    refresh.onclick = reload;

    function renderList(papers) {
      list.replaceChildren();
      if (!papers.length) { list.append(el("p", "muted", "暂无考试。")); return; }
      for (const paper of papers) list.append(paperBlock(paper, college, reload, status));
    }
    card.reload = reload;
    reload();
    return card;
  }

  function paperBlock(paper, college, reload, status) {
    const box = el("div", "ex-row");
    const head = el("div", "ex-head");
    const left = el("div");
    const badges = el("div");
    badges.append(el("span", "ex-badge", PAPER_TEXT[paper.status] || paper.status),
      el("span", "ex-badge", PHASE_TEXT[paper.phase] || paper.phase),
      el("span", "ex-badge", RELEASE_TEXT[paper.answer_release] || paper.answer_release));
    left.append(el("strong", null, paper.title), badges,
      el("p", "ex-note", `${paper.question_count} 题 · ${paper.total_points} 分 · 名单 ${paper.roster_count} 人`
        + ` · 时长 ${paper.duration_minutes} 分钟 · 已交 ${paper.submitted_count}/${paper.roster_count}`
        + ` · 开放 ${when(paper.open_at)} / 结束 ${when(paper.close_at)}`));
    if (paper.description) left.append(el("p", "ex-note", paper.description));
    if (paper.fingerprint) {
      left.append(el("p", "ex-note", "卷面指纹 " + paper.fingerprint.slice(0, 12) + "…（发布后题目不可改）"));
    }
    head.append(left);
    const acts = el("div", "ex-acts");
    head.append(acts);
    box.append(head);

    const holder = el("div", "ex-panel");
    box.append(holder);
    const note = el("p", "ex-note");
    let reopen = async () => {};
    const open = async builder => {
      reopen = async () => { holder.replaceChildren(await builder()); };
      await reopen();
    };

    const questions = el("button", "small", "题目");
    questions.onclick = () => open(() => questionPanel(paper, college, note, reload, reopen));
    const roster = el("button", "small", "名单");
    roster.onclick = () => open(() => rosterPanel(paper, college, note, reload));
    const proctor = el("button", "small", "监考台");
    proctor.onclick = async () => open(() => proctorPanel(paper, college));
    acts.append(questions, roster, proctor);
    if (paper.status !== "draft") {
      const scores = el("button", "small", "成绩");
      scores.onclick = async () => open(() => scoresPanel(paper, college));
      acts.append(scores);
    }

    if (paper.status === "draft") {
      const publish = el("button", "small primary", "发布");
      publish.onclick = async () => {
        publish.disabled = true;
        try {
          const {paper: fresh} = await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/publish`, {college});
          status.textContent = `已发布：${fresh.frozen.question_count} 题 / ${fresh.frozen.total_points} 分，卷面已冻结。`;
          await reload();
        } catch (error) {
          status.textContent = error.message;
        } finally {
          publish.disabled = false;
        }
      };
      acts.append(publish);
    }
    if (paper.status === "published") {
      const close = el("button", "small", "提前结束");
      close.onclick = async () => {
        close.disabled = true;
        try {
          await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/close`, {college});
          status.textContent = "考试已结束，未交卷的考生由系统收卷。";
          await reload();
        } catch (error) {
          status.textContent = error.message;
        } finally {
          close.disabled = false;
        }
      };
      acts.append(close);
    }
    box.append(note);
    return box;
  }

  async function questionPanel(paper, college, note, reload, reopen) {
    const {paper: detail} = await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}`, {college});
    const box = el("div");
    box.append(el("p", "ex-note", "题目在发布后冻结；发布前可整份替换。分值按题给，判分口径是"
      + "「选择集合与标准答案完全一致才得分」。" ));
    const table = el("table", "ex-table");
    const head = el("tr");
    for (const text of ["#", "题型", "题干", "选项", "答案", "分值"]) head.append(el("th", null, text));
    table.append(head);
    for (const question of detail.questions) {
      const row = el("tr");
      row.append(el("td", null, String(question.position)),
        el("td", null, question.kind === "single" ? "单选" : "多选"),
        el("td", null, question.stem),
        el("td", null, (question.options || []).map(o => `${o.key}. ${o.text}`).join(" / ")),
        el("td", null, (question.answer_keys || []).join("、")),
        el("td", null, String(question.points)));
      table.append(row);
    }
    if (!detail.questions.length) box.append(el("p", "muted", "还没有题目。"));
    else box.append(table);

    if (paper.status === "draft") {
      const form = el("div");
      const stem = el("input");
      stem.placeholder = "题干";
      const kind = el("select");
      for (const [value, text] of [["single", "单选"], ["multiple", "多选"]]) {
        const option = el("option", null, text);
        option.value = value;
        kind.append(option);
      }
      const options = el("textarea");
      options.placeholder = "每行一个选项，形如：A. 13%";
      options.rows = 3;
      const keys = el("input");
      keys.placeholder = "标准答案，如 A 或 A,C";
      const points = el("input");
      points.type = "number";
      points.value = "2";
      points.min = "0";
      const explanation = el("input");
      explanation.placeholder = "解析（选填，成绩发布后学生可见）";
      const grid = el("div", "ex-grid");
      const wrap = (label, input) => {
        const cell = el("div");
        cell.append(el("label", null, label), input);
        return cell;
      };
      grid.append(wrap("题型", kind), wrap("答案", keys), wrap("分值", points));
      const add = el("button", "small primary", "加入卷尾");
      add.onclick = async () => {
        add.disabled = true;
        try {
          const rows = options.value.split("\n").map(line => line.trim()).filter(Boolean);
          const parsed = rows.map(line => {
            const match = line.match(/^([A-Za-z0-9]+)[.、:：\s]\s*(.*)$/);
            return match ? {key: match[1], text: match[2] || match[1]} : {key: line, text: line};
          });
          await send(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/questions`, "PUT", {
            college,
            questions: [...detail.questions.map(q => ({kind: q.kind, stem: q.stem, options: q.options,
              answer_keys: q.answer_keys, points: q.points, explanation: q.explanation,
              source: q.source, source_question_id: q.source_question_id})),
              {kind: kind.value, stem: stem.value.trim(), options: parsed,
                answer_keys: keys.value.split(",").map(k => k.trim()).filter(Boolean),
                points: Number(points.value || 0), explanation: explanation.value.trim()}],
          });
          note.textContent = "题目已更新。";
          stem.value = ""; options.value = ""; keys.value = ""; explanation.value = "";
          await reload();
          await reopen?.();
        } catch (error) {
          note.textContent = error.message;
        } finally {
          add.disabled = false;
        }
      };
      form.append(el("label", null, "新增题目"), stem, grid, options, explanation, add);
      box.append(form);

      const bank = el("div");
      const pick = el("button", "small", "从题库选题");
      pick.onclick = async () => {
        pick.disabled = true;
        try {
          const {questions: bankQuestions} = await query("/api/training/exams/papers/bank-questions",
            {college, limit: 50});
          bank.replaceChildren();
          if (!bankQuestions.length) { bank.append(el("p", "muted", "题库里还没有已审核的客观题。")); return; }
          const checks = el("div");
          for (const item of bankQuestions) {
            const label = el("label");
            const box2 = el("input");
            box2.type = "checkbox";
            box2.value = item.id;
            label.append(box2, document.createTextNode(" " + item.stem.slice(0, 60)));
            checks.append(label, el("br"));
          }
          const confirm = el("button", "small primary", "加入选中题目");
          confirm.onclick = async () => {
            confirm.disabled = true;
            try {
              const ids = [...checks.querySelectorAll("input:checked")].map(n => n.value);
              const {paper: fresh} = await send(
                `/api/training/exams/papers/${encodeURIComponent(paper.id)}/questions/from-bank`, "POST",
                {college, question_ids: ids});
              note.textContent = `已加入 ${fresh.added} 题` + (fresh.missing.length
                ? `；${fresh.missing.length} 题已下架或状态不是已审核。` : "。");
              await reload();
            } catch (error) {
              note.textContent = error.message;
            } finally {
              confirm.disabled = false;
            }
          };
          bank.append(checks, confirm);
        } catch (error) {
          bank.append(el("p", "muted", error.message));
        } finally {
          pick.disabled = false;
        }
      };
      box.append(pick, bank);
    }
    return box;
  }

  async function rosterPanel(paper, college, note, reload) {
    const {paper: detail} = await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}`, {college});
    const box = el("div");
    box.append(el("p", "ex-note", "名单以平台账号为准：学生用学号登录的平台账号。学籍没关联平台账号的，"
      + "导入时会逐条列出，不会静默丢弃。整份替换。"));
    const text = el("textarea");
    text.rows = 4;
    text.placeholder = "每行一个学号，如：\n20260101\n20260102";
    text.value = (detail.roster || []).map(r => r.student_no).filter(Boolean).join("\n");
    const save = el("button", "small primary", "导入名单");
    save.onclick = async () => {
      save.disabled = true;
      try {
        const {roster} = await send(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/roster`, "PUT", {
          college,
          student_nos: text.value.split("\n").map(line => line.trim()).filter(Boolean),
        });
        note.textContent = `名单已保存：成功 ${roster.enrolled} 人。`
          + (roster.unresolved.length
            ? " 未导入：" + roster.unresolved.map(u => `${u.student_no}（${u.reason}）`).join("；")
            : "");
        await reload();
      } catch (error) {
        note.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    };
    box.append(text, save);
    return box;
  }

  async function proctorPanel(paper, college) {
    const box = el("div");
    const render = async () => {
      const data = await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/sessions`, {college});
      box.replaceChildren();
      const summary = data.summary;
      box.append(el("p", "ex-note", `名单 ${summary.roster} 人 · 作答中 ${summary.in_progress} · 已交 ${summary.submitted}`
        + ` · 未进入 ${summary.not_started} · 异常事件 ${summary.violations} 次（只记录，不自动判定）`));
      const table = el("table", "ex-table");
      const head = el("tr");
      for (const text of ["学号", "姓名", "状态", "已答", "剩余", "异常", "操作"]) head.append(el("th", null, text));
      table.append(head);
      for (const person of data.people) {
        const row = el("tr");
        const acts = el("div", "ex-acts");
        if (person.status === "in_progress") {
          const force = el("button", "small", "强制收卷");
          force.onclick = async () => {
            force.disabled = true;
            try {
              await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/sessions/`
                + `${encodeURIComponent(person.student_id)}/force-submit`, {college});
              await render();
            } catch (error) {
              box.append(el("p", "muted", error.message));
            } finally {
              force.disabled = false;
            }
          };
          acts.append(force);
        }
        row.append(el("td", null, person.student_no || "—"),
          el("td", null, person.name || "—"),
          el("td", null, SESSION_TEXT[person.status] || person.status),
          el("td", null, `${person.answered}/${person.question_count}`),
          el("td", null, person.remaining_seconds === null ? "—" : clock(person.remaining_seconds)),
          el("td", null, String(person.violations)),
          (() => { const cell = el("td"); cell.append(acts); return cell; })());
        table.append(row);
      }
      box.append(table);
      const refresh = el("button", "small", "刷新监考台");
      refresh.onclick = render;
      box.append(refresh);
    };
    await render();
    return box;
  }

  // ---------------------------------------------------------------- 判分复核与成绩（E3）

  function scoresPanel(paper, college) {
    const box = el("div");
    const note = el("p", "ex-note");
    const render = async () => {
      const data = await query(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/sessions`, {college});
      box.replaceChildren();
      const released = Boolean(paper.released_at);
      const closed = paper.status === "closed" || paper.status === "archived";
      note.textContent = released
        ? `成绩已发布（${when(paper.released_at)}），考生交卷后即可看到分数与解析。`
        : (closed ? "成绩尚未发布，考生还看不到分数与解析。"
          : "考试还没结束：发布成绩前请先「提前结束」，否则考生交卷即见分。");
      box.append(el("p", "ex-note", `名单 ${data.summary.roster} 人 · 已交 ${data.summary.submitted}`
        + ` · 满分 ${paper.total_points} 分 · 及格线 ${paper.pass_score === null || paper.pass_score === undefined
          ? "未设" : paper.pass_score + " 分"} · 口径：复核分优先，未复核用自动判分`), note);

      const acts = el("div", "ex-acts");
      const release = el("button", "small" + (released ? "" : " primary"), released ? "撤回发布" : "发布成绩");
      release.onclick = async () => {
        release.disabled = true;
        try {
          await send(`/api/training/exams/papers/${encodeURIComponent(paper.id)}/release`,
            "POST", {college, release: !released});
          note.textContent = released ? "已撤回：考生立刻看不到成绩。" : "已发布：考生可查看分数与解析。";
          const {papers} = await query("/api/training/exams/papers", {college});
          const fresh = papers.find(item => item.id === paper.id);
          if (fresh) Object.assign(paper, fresh);
          await render();
        } catch (error) {
          release.disabled = false;
          note.textContent = error.message;
        }
      };
      const exportButton = el("button", "small", "导出成绩单 CSV");
      exportButton.onclick = () => {
        const link = el("a");
        link.href = `/api/training/exams/papers/${encodeURIComponent(paper.id)}/scores.csv`
          + "?" + new URLSearchParams({college}).toString();
        link.download = "";
        link.click();
      };
      const refresh = el("button", "small", "刷新");
      refresh.onclick = render;
      acts.append(release, exportButton, refresh);
      box.append(acts);

      const table = el("table", "ex-table");
      const head = el("tr");
      for (const text of ["学号", "姓名", "状态", "自动分", "复核后", "最终", "操作"]) head.append(el("th", null, text));
      table.append(head);
      for (const person of data.people) {
        const row = el("tr");
        const cell = el("td");
        const sheet = el("button", "small", "答卷");
        sheet.onclick = () => openSheet(person);
        cell.append(sheet);
        const show = value => (value === null || value === undefined ? "—" : String(value));
        row.append(el("td", null, person.student_no || "—"),
          el("td", null, person.name || "—"),
          el("td", null, SESSION_TEXT[person.status] || person.status),
          el("td", null, show(person.auto_score)),
          el("td", null, show(person.adjusted_score)),
          el("td", null, show(person.score)),
          cell);
        table.append(row);
      }
      if (!data.people.length) box.append(el("p", "muted", "名单是空的。"));
      else box.append(table);
      box.append(note);
    };

    const holder = el("div");
    const openSheet = async person => {
      holder.replaceChildren(el("p", "ex-note", "正在读取答卷…"));
      try {
        holder.replaceChildren(await answerSheet(paper, college, person));
      } catch (error) {
        holder.replaceChildren(el("p", "muted", error.message));
      }
    };
    box.append(holder);
    render().catch(error => box.append(el("p", "muted", error.message)));
    return box;
  }

  async function answerSheet(paper, college, person) {
    const base = `/api/training/exams/papers/${encodeURIComponent(paper.id)}/sessions/`
      + encodeURIComponent(person.student_id);
    const data = await query(`${base}/answers`, {college});
    const box = el("div");
    const note = el("p", "ex-note");
    const head = () => {
      const session = data.session;
      note.textContent = `${data.student.name || "—"}（${data.student.student_no || "—"}）`
        + ` · 自动判分 ${session.auto_score === null ? "—" : session.auto_score}`
        + ` · 复核后 ${session.adjusted_score === null ? "未复核" : session.adjusted_score}`
        + ` · 最终 ${session.score === null ? "—" : session.score}`
        + ` · 交卷 ${when(session.submitted_at)}`;
    };
    head();
    box.append(note);

    const reload = async () => {
      const fresh = await query(`${base}/answers`, {college});
      data.session = fresh.session;
      data.questions = fresh.questions;
      box.replaceChildren(note, table);
      head();
    };

    const table = el("table", "ex-table");
    const header = el("tr");
    for (const text of ["#", "题干", "标准答案", "考生选项", "自动得分", "满分", "复核分", "复核说明", "操作"])
      header.append(el("th", null, text));
    table.append(header);
    for (const question of data.questions) {
      const row = el("tr");
      const scoreInput = el("input");
      scoreInput.type = "number";
      scoreInput.min = "0";
      scoreInput.max = String(question.points);
      scoreInput.step = "0.5";
      scoreInput.style.width = "70px";
      scoreInput.value = question.manual_score === null ? "" : String(question.manual_score);
      scoreInput.placeholder = "0–" + question.points;
      const noteInput = el("input");
      noteInput.maxLength = 60;
      noteInput.placeholder = "可选";
      noteInput.value = question.manual_note || "";
      noteInput.style.width = "110px";
      const save = el("button", "small", "保存");
      const reset = el("button", "small", "撤销");
      const ops = el("div", "ex-acts");
      const submit = async value => {
        save.disabled = reset.disabled = true;
        try {
          await send(`${base}/answers/${encodeURIComponent(question.id)}/review`, "POST",
            {college, score: value, note: value === null ? null : noteInput.value.trim() || null});
          await reload();
        } catch (error) {
          note.textContent = error.message;
          save.disabled = reset.disabled = false;
        }
      };
      save.onclick = () => {
        const raw = scoreInput.value.trim();
        if (raw === "") return submit(null);
        const value = Number(raw);
        if (!Number.isFinite(value) || value < 0 || value > Number(question.points)) {
          note.textContent = `复核分须在 0–${question.points} 之间。`;
          return;
        }
        return submit(value);
      };
      reset.onclick = () => submit(null);
      ops.append(save, reset);
      row.append(el("td", null, String(question.position)),
        el("td", null, question.stem),
        el("td", null, (question.answer_keys || []).join("、")),
        el("td", null, (question.picked || []).join("、") || "未作答"),
        el("td", null, question.points_gained === null ? "—" : String(question.points_gained)),
        el("td", null, String(question.points)),
        (() => { const cell = el("td"); cell.append(scoreInput); return cell; })(),
        (() => { const cell = el("td"); cell.append(noteInput); return cell; })(),
        (() => { const cell = el("td"); cell.append(ops); return cell; })());
      table.append(row);
    }
    box.append(table);
    box.append(el("p", "ex-note", "复核只改这一题的得分并重算整卷，自动判分原样保留；"
      + "清空复核分即撤销，回到自动判分。"));
    return box;
  }

  // ---------------------------------------------------------------- 学生端

  function studentCard() {
    const card = el("div", "card sheet");
    card.id = "exam-ui-card";
    card.dataset.universityView = "exams";
    card.append(el("h2", null, "在线考试"));
    card.append(el("p", "muted", "进入考试即开始计时，时间由服务端计算；切页与复制等操作会被记录供教师参考，"
      + "不据此判定成绩。到点未交卷由系统自动收卷。"));
    const status = el("p", "muted");
    const body = el("div", "muted", "加载中…");
    const refresh = el("button", "small", "刷新");
    refresh.onclick = () => reload();
    card.append(refresh, status, el("div", "ex-row"), body);

    // 一场考试一套运行时：倒计时、心跳、待上报事件、监听解绑
    const runtime = {timer: null, beat: null, events: [], flushing: false, detach: null};

    function stopRuntime() {
      if (runtime.timer) window.clearInterval(runtime.timer);
      if (runtime.beat) window.clearInterval(runtime.beat);
      runtime.timer = null;
      runtime.beat = null;
      runtime.detach?.();
      runtime.detach = null;
    }

    async function reload() {
      stopRuntime();
      status.textContent = "正在读取…";
      try {
        const {exams} = await api("/api/training/exams/my");
        body.replaceChildren();
        if (!exams.length) { body.append(el("p", "muted", "当前没有安排给你的在线考试。")); status.textContent = ""; return; }
        for (const exam of exams) body.append(examBlock(exam, status, body, reload, runtime, stopRuntime));
        status.textContent = `共 ${exams.length} 场考试。`;
      } catch (error) {
        status.textContent = error.message;
      }
    }

    card.reload = reload;
    reload();
    return card;
  }

  function examBlock(exam, status, body, reload, runtime, stopRuntime) {
    const box = el("div", "ex-row");
    const head = el("div", "ex-head");
    const left = el("div");
    left.append(el("strong", null, exam.title),
      el("div", null, (() => {
        const badges = el("div");
        badges.append(el("span", "ex-badge", PHASE_TEXT[exam.phase] || exam.phase),
          el("span", "ex-badge", SESSION_TEXT[exam.session_status] || "未进入"),
          el("span", "ex-badge", RELEASE_TEXT[exam.answer_release] || exam.answer_release));
        return badges;
      })()),
      el("p", "ex-note", `时长 ${exam.duration_minutes} 分钟 · 开放 ${when(exam.open_at)} / 结束 ${when(exam.close_at)}`));
    if (exam.description) left.append(el("p", "ex-note", exam.description));
    head.append(left);
    const acts = el("div", "ex-acts");
    head.append(acts);
    box.append(head);

    const holder = el("div", "ex-panel");
    box.append(holder);

    const enter = el("button", "small primary", exam.session_status ? "继续作答" : "进入考试");
    enter.disabled = exam.status !== "published" || exam.phase === "scheduled" || exam.phase === "ended";
    if (exam.status !== "published") enter.title = "考试未发布或已结束";
    enter.onclick = async () => {
      enter.disabled = true;
      try {
        if (!exam.session_status) {
          await api(`/api/training/exams/${encodeURIComponent(exam.id)}/start`, {method: "POST"});
        }
        holder.replaceChildren(await takePanel(exam, status, reload, runtime, stopRuntime));
        status.textContent = "";
      } catch (error) {
        status.textContent = error.message;
      } finally {
        enter.disabled = false;
      }
    };
    const result = el("button", "small", "成绩与解析");
    result.onclick = async () => {
      result.disabled = true;
      try {
        holder.replaceChildren(await resultPanel(exam));
      } catch (error) {
        status.textContent = error.message;
      } finally {
        result.disabled = false;
      }
    };
    acts.append(enter, result);
    return box;
  }

  function flushEvents(exam, runtime) {
    if (runtime.flushing || !runtime.events.length) return Promise.resolve();
    runtime.flushing = true;
    const batch = runtime.events.splice(0, runtime.events.length);
    return api(`/api/training/exams/${encodeURIComponent(exam.id)}/events`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({events: batch}),
    }).catch(() => {}).finally(() => { runtime.flushing = false; });
  }

  async function takePanel(exam, status, reload, runtime, stopRuntime) {
    const data = await api(`/api/training/exams/${encodeURIComponent(exam.id)}/paper`);
    const box = el("div");
    const timer = el("div", "ex-timer", clock(data.session.remaining_seconds));
    let deadline = Date.now() + Number(data.session.remaining_seconds || 0) * 1000;
    const tick = () => {
      const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
      timer.textContent = clock(left);
      timer.classList.toggle("urgent", left <= 60);
      if (left <= 0) {
        stopRuntime();
        status.textContent = "考试时间已到，系统正在收卷…";
        window.setTimeout(() => reload(), 1200);
      }
    };
    const beat = async () => {
      try {
        const beatData = await api(`/api/training/exams/${encodeURIComponent(exam.id)}/heartbeat`, {method: "POST"});
        deadline = Date.now() + Number(beatData.remaining_seconds || 0) * 1000;
      } catch { /* 心跳失败不打断作答，本地倒计时继续 */ }
    };
    stopRuntime();
    runtime.timer = window.setInterval(() => { tick(); flushEvents(exam, runtime); }, 1000);
    runtime.beat = window.setInterval(beat, 60000);
    // 防作弊只上报不判定：切页与复制类操作入队，交卷或每秒定时上报，心跳不计入异常
    const onBlur = () => runtime.events.push({kind: "blur"});
    const onVisibility = () => { if (document.hidden) runtime.events.push({kind: "visibility"}); };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    runtime.detach = () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
    };

    const meta = el("p", "ex-note", `${data.paper.title} · 共 ${data.paper.question_count} 题 / `
      + `${data.paper.total_points} 分 · 已答 ${data.session.answered} 题`);
    if (data.paper.description) meta.append(el("span", null, " · " + data.paper.description));
    const saveState = el("span", "ex-note", "");
    const head = el("div", "ex-head");
    head.append(el("div", null, timer, meta), saveState);
    box.append(head);

    const picked = new Map();
    const list = el("div");
    for (const question of data.questions) {
      picked.set(question.id, [...(question.picked || [])]);
      list.append(questionNode(question, picked, () => autosave()));
    }
    box.append(list);

    let pending = null;
    async function autosave() {
      const answers = [...picked.entries()].map(([question_id, keys]) => ({question_id, picked: keys}));
      saveState.textContent = "保存中…";
      if (pending) window.clearTimeout(pending);
      pending = window.setTimeout(async () => {
        try {
          const saved = await send(`/api/training/exams/${encodeURIComponent(exam.id)}/answers`, "POST", {answers});
          saveState.textContent = `已保存 ${saved.saved} 题 · 剩余 ${clock(saved.session.remaining_seconds)}`;
          deadline = Date.now() + Number(saved.session.remaining_seconds || 0) * 1000;
        } catch (error) {
          saveState.textContent = error.message;
        }
      }, 600);
    }

    const submit = el("button", "small primary", "交卷");
    submit.onclick = async () => {
      if (!window.confirm("交卷后将不能再修改答案，确认交卷？")) return;
      submit.disabled = true;
      try {
        await send(`/api/training/exams/${encodeURIComponent(exam.id)}/answers`, "POST",
          {answers: [...picked.entries()].map(([question_id, keys]) => ({question_id, picked: keys}))});
        await flushEvents(exam, runtime);
        const done = await api(`/api/training/exams/${encodeURIComponent(exam.id)}/submit`, {method: "POST"});
        stopRuntime();
        status.textContent = done.score_visible
          ? `已交卷，得分 ${done.score} 分。`
          : "已交卷，成绩待教师发布。";
        await reload();
      } catch (error) {
        status.textContent = error.message;
        submit.disabled = false;
      }
    };
    const guard = event => {
      if (!["copy", "paste", "contextmenu"].includes(event.type)) return;
      runtime.events.push({kind: event.type});
    };
    list.addEventListener("copy", guard);
    list.addEventListener("paste", guard);
    list.addEventListener("contextmenu", guard);
    box.append(submit);
    return box;
  }

  function questionNode(question, picked, onchange) {
    const box = el("div", "ex-question");
    box.append(el("strong", null, `${question.kind === "single" ? "单选" : "多选"} · ${question.points} 分`));
    box.append(el("p", null, question.stem));
    const options = el("div", "ex-options");
    for (const option of question.options || []) {
      const label = el("label");
      const input = el("input");
      input.type = question.kind === "single" ? "radio" : "checkbox";
      input.name = "exq-" + question.id;
      input.value = option.key;
      input.checked = (picked.get(question.id) || []).includes(option.key);
      input.onchange = () => {
        if (question.kind === "single") picked.set(question.id, [option.key]);
        else {
          const keys = new Set(picked.get(question.id) || []);
          if (input.checked) keys.add(option.key); else keys.delete(option.key);
          picked.set(question.id, [...keys]);
        }
        onchange();
      };
      label.append(input, el("span", null, `${option.key}. ${option.text}`));
      options.append(label);
    }
    box.append(options);
    return box;
  }

  async function resultPanel(exam) {
    const data = await api(`/api/training/exams/${encodeURIComponent(exam.id)}/result`);
    const box = el("div");
    if (!data.released) {
      box.append(el("p", "ex-note", data.reason || "成绩尚未发布。"));
      return box;
    }
    box.append(el("p", null, `得分 ${data.score} 分`
      + (data.reviewed ? `（含教师复核；自动判分 ${data.auto_score} 分）` : "")
      + (data.pass_score === null || data.pass_score === undefined
        ? "" : ` · 及格线 ${data.pass_score} 分 · ${data.passed ? "及格" : "未及格"}`)));
    for (const question of data.questions) {
      const item = el("div", "ex-question");
      item.append(el("strong", null, `${question.kind === "single" ? "单选" : "多选"} · ${question.points} 分`));
      item.append(el("p", null, question.stem));
      const options = el("div", "ex-options");
      for (const option of question.options || []) {
        const mark = (question.answer_keys || []).includes(option.key) ? " ✓" : "";
        options.append(el("div", null, `${option.key}. ${option.text}${mark}`));
      }
      item.append(options);
      const mine = (question.picked || []).join("、") || "未作答";
      const gained = question.gain ?? question.points_gained ?? 0;
      const answer = el("p", "ex-answer " + (question.correct ? "ok" : "no"),
        `你的答案：${mine} · 标准答案：${(question.answer_keys || []).join("、")}`
        + ` · 得分 ${gained}`
        + (question.manual_score === null || question.manual_score === undefined ? "" : "（教师复核）"));
      item.append(answer);
      if (question.explanation) item.append(el("p", "ex-note", "解析：" + question.explanation));
      box.append(item);
    }
    return box;
  }

  // ---------------------------------------------------------------- 启动

  let booted = false;
  let stop = false;

  // 宿主卡片是异步塞进 #universityContent 的：教师端停在 home 视图时模板还没加载，
  // 而且 mount() 会整块 replaceChildren，所以每次都检查"卡片还在不在、内容就位没有"。
  async function init() {
    const host = document.getElementById("universityContent");
    if (!host || !host.querySelector("#app-view")) return;   // 内容没就位：等下一次变更
    let who;
    try { who = await api("/api/me"); } catch { return; }     // 还没登录：继续等
    const isStudent = who.education_role === "student" || who.role === "student";
    const isTeacher = ["teacher", "admin", "org_admin"].includes(who.role)
      || ["teacher", "admin"].includes(who.education_role);
    if (!isStudent && !isTeacher) { stop = true; return; }    // 不是教学身份：不再尝试
    // /api/me 不带院校，教师侧院校从教师会话身份里取（考试接口按院校隔离）
    let college = who.college || "";
    if (!isStudent) {
      try {
        const {staff} = await api("/api/training/staff/auth/me");
        college = (staff && staff.college) || "";
      } catch { return; }                                     // 教师会话还没建：继续等
      if (!college) return;
    }
    document.getElementById("exam-ui-card")?.remove();
    host.append(isStudent ? studentCard() : teacherCard(college));
    // 切到「在线考试」视图时刷新一次（视图切换由工作空间自己管，这里只跟随）
    document.querySelectorAll('#universityNav button[data-university-view="exams"]').forEach(button => {
      if (button.dataset.examBound) return;
      button.dataset.examBound = "1";
      button.addEventListener("click", () => document.getElementById("exam-ui-card")?.reload?.());
    });
  }

  function tryBoot() {
    if (booted || stop) return;
    if (document.getElementById("exam-ui-card")) return;
    const host = document.getElementById("universityContent");
    if (!host || !host.querySelector("#app-view")) return;
    booted = true;
    init().catch(() => {}).finally(() => { booted = false; });
  }

  injectStyle();
  new MutationObserver(tryBoot).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"],
  });
  // 轮询只作为兜底：DOM 变更观察者已经覆盖绝大多数情况，这里保证"卡片被整块替换"后能补回来
  const poller = window.setInterval(() => {
    if (stop || document.getElementById("exam-ui-card")) { window.clearInterval(poller); return; }
    tryBoot();
  }, 2000);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tryBoot);
  else tryBoot();
})();
