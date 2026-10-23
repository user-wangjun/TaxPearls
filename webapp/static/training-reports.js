/* 高校管理报表（FR-U05）：院系 / 专业 / 班级 / 课程维度的教学成效聚合。
   自包含：自己取身份、自己渲染到 #collegeLearningReport，不改动其他控制器。
   数据口径由后端 training_reports.build_report 保证（与学情统计、成绩单同源）。 */
(function () {
  'use strict';
  const root = () => document.getElementById('collegeLearningReport');
  let state = { report: null, filters: { term_id: '', department_id: '' } };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const pct = value => (value === null || value === undefined ? '—' : `${value}%`);
  const num = value => (value === null || value === undefined ? '—' : String(value));
  const money = value => (value === null || value === undefined ? '—' : Number(value).toFixed(2));

  function table(headings, rows) {
    const wrap = el('div', 'table-scroll');
    const tbl = el('table');
    const head = el('tr');
    headings.forEach(h => head.append(el('th', null, h)));
    tbl.append(head);
    rows.forEach(cells => {
      const tr = el('tr');
      cells.forEach(cell => {
        const td = el('td');
        if (cell instanceof Node) td.append(cell);
        else td.textContent = cell === null || cell === undefined ? '—' : String(cell);
        tr.append(td);
      });
      tbl.append(tr);
    });
    wrap.append(tbl);
    return wrap;
  }

  function status(text, error) {
    const box = root()?.querySelector('#clrStatus');
    if (!box) return;
    box.textContent = text || '';
    box.className = error ? 'error' : 'muted';
  }

  async function fetchJSON(url, options) {
    const response = await fetch(url, Object.assign({credentials: 'same-origin'}, options));
    const text = await response.text();
    if (!response.ok) {
      let message = text;
      try { message = JSON.parse(text).detail || text; } catch (ignored) { /* 原样展示 */ }
      throw new Error(message || `HTTP ${response.status}`);
    }
    return text ? JSON.parse(text) : {};
  }

  function render(report) {
    const mount = root();
    const body = mount.querySelector('#clrBody');
    body.replaceChildren();
    const totals = report.totals;
    const kpis = el('div', 'kpi-grid');
    [['班级', totals.classes, '个'], ['学生', totals.students, '人'], ['任务', totals.tasks, '项'],
     ['提交率', pct(totals.submit_rate), ''], ['均分', money(totals.mean), '分'],
     ['待复核', totals.unreviewed, '份']].forEach(([label, value, unit]) => {
      const card = el('div', 'kpi');
      card.append(el('div', null, label));
      const v = el('div', 'value', value);
      if (unit) v.append(el('span', 'unit', unit));
      card.append(v);
      kpis.append(card);
    });
    body.append(kpis);
    body.append(el('p', 'muted', report.scope));
    body.append(el('p', 'muted', `生成时间 ${new Date(report.generated_at).toLocaleString()} · 报表口径会在换库/回滚后随数据变化`));

    // ---- 院系 → 专业 → 班级
    body.append(el('h3', null, '院系与专业'));
    if (!report.departments.length) {
      body.append(el('p', 'muted', '本院校还没有可统计的班级实体：先在「师生与班级」建院系/专业/班级，再把教学班绑定到班级实体。'));
    }
    const rows = [];
    for (const dept of report.departments) {
      if (!dept.majors.length) {
        rows.push([dept.name, '—', 0, 0, 0, 0, 0, '—', '—', 0]);
      }
      for (const major of dept.majors) {
        const t = major.totals;
        if (!major.classes.length) {
          rows.push([dept.name, major.name, 0, 0, 0, 0, 0, '—', '—', 0]);
        }
        for (const cls of major.classes) {
          const c = cls.totals;
          const names = cls.teaching_classes.map(tc => tc.name).join('、') || '未绑定教学班';
          rows.push([dept.name, `${major.name} · ${cls.name}`, c.classes, c.students, c.tasks,
                     c.expected, c.submitted, pct(c.submit_rate), money(c.mean), c.unreviewed, names]);
        }
      }
    }
    if (rows.length) {
      body.append(table(['院系', '专业 · 班级', '教学班', '学生', '任务', '应交', '已交', '提交率', '均分', '待复核', '教学班名称'],
                        rows));
    }

    // ---- 未归属教学班（附候选绑定，管理员确认后才生效）
    body.append(el('h3', null, '未归属的教学班'));
    if (!report.unbound.length) {
      body.append(el('p', 'muted', '所有教学班都已绑定到班级实体。'));
    } else {
      body.append(el('p', 'muted', '教学班未绑定班级实体时不计入院系/专业汇总——按名字猜会把成绩算到错的院系，所以这里只给候选，由管理员确认。'));
      const unboundRows = report.unbound.map(row => {
        const cell = el('div');
        const select = el('select');
        select.append(new Option('选择班级实体…', ''));
        row.suggestions.forEach(s => select.append(new Option(`${s.name}（${s.match}）`, s.id)));
        const button = el('button', 'small', '绑定');
        button.onclick = async () => {
          if (!select.value) { status('请先选择要绑定到的班级实体。', true); return; }
          button.disabled = true;
          try {
            await fetchJSON('/api/training/staff/class-bindings', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({college: state.report.college, teaching_class_id: row.id,
                                    college_class_id: select.value}),
            });
            status(`已把「${row.name}」绑定到班级实体，正在刷新报表…`);
            await load();
          } catch (error) {
            status('绑定失败：' + error.message, true);
            button.disabled = false;
          }
        };
        cell.append(select, button);
        return [row.name, row.owner, row.students, row.tasks, pct(row.submit_rate),
                money(row.mean), row.unreviewed, cell];
      });
      body.append(table(['教学班', '负责人', '学生', '任务', '提交率', '均分', '待复核', '绑定到班级实体'], unboundRows));
    }

    // ---- 课程维度
    body.append(el('h3', null, '课程维度'));
    if (!report.courses.length) body.append(el('p', 'muted', '本院校还没有启用中的课程。'));
    else {
      body.append(table(['课程', '学期', '教师', '班级', '任务', '应交', '已交', '提交率', '均分', '备注'],
        report.courses.map(c => [c.name, c.semester || '—', c.teacher || '—', c.classes, c.tasks,
                                 c.expected, c.submitted, pct(c.submit_rate), money(c.mean), c.note || ''])));
      body.append(el('p', 'muted', '课程区只算提交份数与均分，不把人头相加（一个学生可能同时修多门课）。'));
    }

    const notes = el('ul', 'muted');
    report.notes.forEach(note => notes.append(el('li', null, note)));
    body.append(notes);
  }

  async function load() {
    const mount = root();
    if (!mount) return;
    const query = new URLSearchParams();
    if (state.filters.term_id) query.set('term_id', state.filters.term_id);
    if (state.filters.department_id) query.set('department_id', state.filters.department_id);
    status('正在汇总教学报表…');
    try {
      const me = await fetchJSON('/api/training/staff/auth/me');
      const staff = me.staff || {};
      if (!['admin', 'school_admin'].includes(staff.role)) {
        status('管理报表仅限实训管理员或校级管理员查看；任课教师请用上方「班级学情统计」看本班。', true);
        mount.querySelector('#clrBody').replaceChildren();
        return;
      }
      query.set('college', staff.college);
      const report = await fetchJSON('/api/training/staff/reports/learning?' + query.toString());
      state.report = report;
      fillFilters(report);
      render(report);
      status(`已汇总：${report.departments.length} 个院系 · ${report.unbound.length} 个未归属教学班 · ${report.courses.length} 门课程`);
    } catch (error) {
      status('读取失败：' + error.message, true);
    }
  }

  function fillFilters(report) {
    const mount = root();
    const term = mount.querySelector('#clrTerm');
    const dept = mount.querySelector('#clrDepartment');
    const keepTerm = term.value;
    const keepDept = dept.value;
    term.replaceChildren(new Option('全部学期', ''));
    report.options.terms.forEach(t => term.append(new Option(t.name + (t.is_current ? '（当前）' : ''), t.id)));
    dept.replaceChildren(new Option('全部院系', ''));
    report.options.departments.forEach(d => dept.append(new Option(d.name, d.id)));
    term.value = keepTerm;
    dept.value = keepDept;
  }

  function mount() {
    const box = root();
    if (!box || box.dataset.bound === '1') return;
    box.dataset.bound = '1';
    box.querySelector('#clrRefresh').onclick = load;
    box.querySelector('#clrTerm').onchange = event => {
      state.filters.term_id = event.target.value;
      load();
    };
    box.querySelector('#clrDepartment').onchange = event => {
      state.filters.department_id = event.target.value;
      load();
    };
    load();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
