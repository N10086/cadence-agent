/* =========================================================================
   Cadence 前端 · 页面逻辑（原生 JS，无构建，无外部依赖）
   依赖 api.js 暴露的 window.cadence

   分区：
     1. 状态与工具函数
     2. 提示 / 加载 / 确认框
     3. 看板渲染与任务卡片
     4. 任务新建 / 编辑
     5. 状态流转与两步删除
     6. 详情抽屉（详情 / 依赖 / 历史 / 重复实例）
     7. 批量操作
     8. 忙闲日历与连通性
     9. 事件绑定与初始化
   ========================================================================= */
(function () {
  'use strict';

  const api = window.cadence;

  /* ======================================================================
     1. 状态与工具
     ====================================================================== */

  const COLUMNS = [
    { key: 'TODO', title: '待办', color: '#2563eb' },
    { key: 'DOING', title: '进行中', color: '#f59e0b' },
    { key: 'DONE', title: '已完成', color: '#059669' }
  ];

  const PRIORITY_LABEL = { URGENT: '紧急', HIGH: '高', NORMAL: '普通', LOW: '低' };
  const ACTION_LABEL = {
    CREATE: '创建', UPDATE: '修改', COMPLETE: '完成', START: '开始',
    REOPEN: '恢复', CANCEL: '取消', DELETE: '删除', BATCH_UPDATE: '批量修改'
  };

  const state = {
    tasks: [],          // 当前筛选结果
    hasMore: false,
    total: 0,
    cancelled: [],      // 已取消（单独查询，仅展示）
    selected: new Set(),
    detailId: null,
    busyTimer: null
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([k, v]) => {
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'html') node.innerHTML = v;   // 只用于本文件内写死的模板，不放用户输入
        else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else if (k === 'dataset') Object.assign(node.dataset, v);
        else node.setAttribute(k, v === true ? '' : v);
      });
    }
    (Array.isArray(children) ? children : children ? [children] : []).forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    });
    return node;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtTimeShort(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso || '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtDuration(min) {
    if (min === null || min === undefined || min === '') return '';
    const h = Math.floor(min / 60);
    const m = min % 60;
    if (h && m) return `${h}h${m}m`;
    if (h) return `${h}h`;
    return `${m}m`;
  }

  /** 截止时间的紧急程度：已逾期 / 24h 内 / 3 天内 */
  function dueLevel(dueAt, status) {
    if (!dueAt || status === 'DONE' || status === 'CANCELLED') return null;
    const diff = new Date(dueAt).getTime() - Date.now();
    if (diff < 0) return 'overdue';
    if (diff < 24 * 3600 * 1000) return 'soon';
    if (diff < 3 * 24 * 3600 * 1000) return 'warn';
    return null;
  }

  /** 忙闲查询的默认区间：今天 00:00 → 明天 24:00（本地时间） */
  function todayRange() {
    const from = new Date();
    from.setHours(0, 0, 0, 0);
    const to = new Date(from);
    to.setDate(to.getDate() + 1);
    return { from, to };
  }

  function debounce(fn, ms) {
    return function (...args) {
      clearTimeout(state.busyTimer);
      state.busyTimer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  /* ======================================================================
     2. 提示 / 加载 / 确认
     ====================================================================== */

  function toast(message, kind = '', detail = '') {
    const node = el('div', { class: `toast ${kind}` }, [
      el('div', { text: message }),
      detail ? el('small', { text: detail }) : null
    ]);
    $('#toasts').append(node);
    setTimeout(() => {
      node.style.transition = 'opacity .2s';
      node.style.opacity = '0';
      setTimeout(() => node.remove(), 220);
    }, kind === 'err' ? 5200 : 2600);
  }

  let loadingCount = 0;
  function busy(on) {
    loadingCount += on ? 1 : -1;
    if (loadingCount < 0) loadingCount = 0;
    $('#loading').classList.toggle('hidden', loadingCount === 0);
  }

  async function withBusy(fn) {
    busy(true);
    try { return await fn(); } finally { busy(false); }
  }

  function showError(err) {
    if (err instanceof api.ApiError) {
      const hint = err.status === 0 ? '' : `HTTP ${err.status} · code ${err.code}`;
      toast(err.message, 'err', hint);
    } else {
      console.error(err);
      toast(String(err && err.message || err), 'err');
    }
  }

  /** 通用确认框，返回 Promise<boolean> */
  function confirmDialog(title, bodyBuilder, okText = '确认') {
    return new Promise((resolve) => {
      const dlg = $('#confirm-dialog');
      $('#confirm-title').textContent = title;
      $('#confirm-ok').textContent = okText;
      const body = $('#confirm-body');
      body.replaceChildren();
      const extra = bodyBuilder(body);
      dlg._resolve = resolve;
      dlg.showModal();
      if (extra) extra();
    });
  }

  /* ---------------------------------------------------------------
     弹窗统一调度
     Chrome 有个必须绕开的行为：同一个 task 里，第一个 Promise 微任务执行时，
     刚 showModal() 打开的模态层会被同步移除；若此时另一个 dialog 还处于 open 状态，
     连新开的那一个也会一起失去 open 属性。
     对策：showModal 一律推到下一个宏任务，并且同一时刻只允许一个模态弹窗。
     --------------------------------------------------------------- */
  const DIALOG_IDS = ['task-dialog', 'detail-dialog', 'confirm-dialog'];

  /** 打开弹窗：返回 Promise，resolve 时对话框已真正打开 */
  function openModal(dialog) {
    return new Promise((resolve) => {
      setTimeout(() => {
        if (dialog.open) {
          resolve();
          return;
        }
        DIALOG_IDS.forEach((id) => {
          const other = document.getElementById(id);
          if (other && other !== dialog && other.open) other.close();
        });
        dialog.showModal();
        resolve();
      }, 0);
    });
  }

  function closeConfirm(result) {
    const dlg = $('#confirm-dialog');
    if (dlg.open) dlg.close();
    if (dlg._resolve) {
      dlg._resolve(result);
      dlg._resolve = null;
    }
  }

  function setBanner(html) {
    const b = $('#banner');
    if (!html) {
      b.classList.add('hidden');
      b.innerHTML = '';
      return;
    }
    b.innerHTML = html;
    b.classList.remove('hidden');
  }

  /* ======================================================================
     3. 看板渲染
     ====================================================================== */

  function currentFilters() {
    const filters = {
      keyword: $('#f-keyword').value.trim(),
      priority: $('#f-priority').value,
      tag: $('#f-tag').value.trim(),
      limit: $('#f-limit').value
    };
    const due = $('#f-due').value;
    if (due) {
      const now = new Date();
      if (due === 'today') {
        const end = new Date(now); end.setHours(23, 59, 59, 999);
        filters.dueBefore = api.toIso(end);
      } else if (due === 'week') {
        const end = new Date(now.getTime() + 7 * 24 * 3600 * 1000);
        filters.dueBefore = api.toIso(end);
        filters.dueAfter = api.toIso(now);
      } else if (due === 'overdue') {
        filters.dueBefore = api.toIso(now);
      }
    }
    return filters;
  }

  /** 演示模式：URL 带 ?demo 时用内存数据，保证每个按钮都能点通 */
  function demoFlag() {
    return new URLSearchParams(window.location.search).has('demo');
  }

  /** 演示模式的默认时间：以今天为基准，不写死日期 */
  function atDay(offsetDays, hour, minute = 0) {
    const d = new Date();
    d.setDate(d.getDate() + offsetDays);
    d.setHours(hour, minute, 0, 0);
    return api.toIso(d);
  }

  /** 演示模式用的假任务对象 */
  function demoTask(id, title, priority, extra) {
    const now = api.toIso(new Date());
    return Object.assign({
      id, title, priority, status: 'TODO', pinned: false, tags: [],
      version: 0, createdAt: now, updatedAt: now
    }, extra);
  }

  function renderDemoBoard() {
    renderBoard();
    renderSelectionBar();
  }

  async function loadTasks(opts = {}) {
    const filters = currentFilters();
    if (demoFlag()) {
      renderDemoBoard();
      return;
    }
    try {
      const [list, cancelled] = await Promise.all([
        api.listTasks(filters),
        api.listTasks({ status: 'CANCELLED', limit: 50 })
      ]);
      state.hasMore = list.hasMore;
      state.total = list.total;
      state.tasks = list.items || [];
      state.cancelled = cancelled.items || [];

      // 已选中的任务如果已不在列表里（被删/改状态），从选择集移除
      const ids = new Set(state.tasks.map((t) => t.id));
      state.selected.forEach((id) => { if (!ids.has(id)) state.selected.delete(id); });

      renderBoard();
      renderSelectionBar();
      setBanner(null);
      if (!opts.silent) {
        $('#list-meta').textContent = `共 ${state.total} 条${state.hasMore ? `（仅显示前 ${state.tasks.length} 条，可把条数调大）` : ''}`;
      }
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 0) {
        setBanner(
          `无法连接后端 <code>${api.baseUrl()}</code>。请先启动 cadence-core：<br>`
          + `在 <code>services/cadence-core</code> 执行 <code>mvn spring-boot:run</code>，并确保 Postgres 已启动（<code>docker compose up -d postgres</code>）。`
        );
      }
      state.tasks = [];
      renderBoard();
      renderSelectionBar();
      showError(e);
    }
  }

  function renderBoard() {
    const board = $('#board');
    board.replaceChildren();

    const groups = {
      TODO: state.tasks.filter((t) => t.status === 'TODO'),
      DOING: state.tasks.filter((t) => t.status === 'DOING'),
      DONE: state.tasks.filter((t) => t.status === 'DONE'),
      CANCELLED: state.cancelled
    };

    COLUMNS.forEach((col) => {
      const tasks = groups[col.key];
      const filteredNote = state.tasks.length;
      const body = el('div', { class: 'column-body' });
      if (tasks.length === 0) {
        body.append(el('div', { class: 'empty', text: state.total === 0 ? '没有任务' : '无符合条件的任务' }));
      } else {
        tasks.forEach((task) => body.append(renderTask(task)));
      }
      board.append(el('section', { class: 'column' }, [
        el('div', { class: 'column-head' }, [
          el('span', { class: 'dot', style: `background:${col.color}` }),
          el('h2', { text: col.title }),
          el('span', { class: 'count', text: String(tasks.length) })
        ]),
        body
      ]));
      void filteredNote;
    });

    // 已取消单独一列放不下三列布局，这里以折叠区附加在最后
    if (groups.CANCELLED.length) {
      const list = el('div', { class: 'column-body' });
      groups.CANCELLED.forEach((t) => list.append(renderTask(t)));
      const section = el('section', { class: 'column cancelled' }, [
        el('div', { class: 'column-head' }, [
          el('span', { class: 'dot', style: 'background:#94a3b8' }),
          el('h2', { text: '已取消' }),
          el('span', { class: 'count', text: String(groups.CANCELLED.length) })
        ]),
        list
      ]);
      section.dataset.extra = 'cancelled';
      board.append(section);
    }
  }

  function renderTask(task) {
    const selected = state.selected.has(task.id);
    const checkbox = el('input', {
      type: 'checkbox',
      'aria-label': `选择任务 ${task.title}`,
      onchange: (e) => {
        if (e.target.checked) state.selected.add(task.id);
        else state.selected.delete(task.id);
        renderSelectionBar();
      }
    });
    checkbox.checked = selected;

    const meta = el('div', { class: 'task-meta' });
    meta.append(el('span', { class: `badge ${task.priority}`, text: PRIORITY_LABEL[task.priority] || task.priority }));

    if (task.dueAt) {
      const level = dueLevel(task.dueAt, task.status);
      const cls = level === 'overdue' ? 'due-overdue' : '';
      meta.append(el('span', { class: cls, text: `截止 ${fmtTimeShort(task.dueAt)}` }));
      if (level === 'overdue') meta.append(el('span', { class: 'badge due-soon', text: '已逾期' }));
      else if (level === 'soon') meta.append(el('span', { class: 'badge due-soon', text: '24h内' }));
      else if (level === 'warn') meta.append(el('span', { class: 'badge due-warn', text: '3天内' }));
    } else if (task.startAt) {
      meta.append(el('span', { text: `开始 ${fmtTimeShort(task.startAt)}` }));
    }

    if (task.estimateMin) meta.append(el('span', { text: `⏱ ${fmtDuration(task.estimateMin)}` }));
    if (task.pinned) meta.append(el('span', { class: 'badge pin', text: '📌 固定' }));
    if (task.rrule) meta.append(el('span', { class: 'badge rec', text: '🔁 重复' }));
    (task.tags || []).forEach((tag) => meta.append(el('span', { class: 'tag', text: `#${tag}` })));

    const actions = el('div', { class: 'task-actions' });
    const btn = (text, cls, handler, title) => el('button', {
      class: `btn ${cls || ''}`, text, title: title || '', onclick: (e) => { e.stopPropagation(); handler(); }
    });

    if (task.status === 'TODO') actions.append(btn('▶ 开始', '', () => transition(task, 'start')));
    if (task.status === 'TODO' || task.status === 'DOING') {
      actions.append(btn('✓ 完成', '', () => transition(task, 'complete')));
      actions.append(btn('取消', 'ghost', () => transition(task, 'cancel')));
    }
    if (task.status === 'DONE' || task.status === 'CANCELLED') {
      actions.append(btn('↺ 恢复', '', () => transition(task, 'reopen')));
    }
    actions.append(btn('详情', 'ghost', () => openDetail(task.id)));
    actions.append(btn('编辑', 'ghost', () => openTaskForm(task)));
    actions.append(btn('删除', 'ghost', () => deleteTask(task), '需要两步确认'));

    return el('article', {
      class: `task p-${task.priority}${selected ? ' selected' : ''}`,
      dataset: { id: task.id },
      ondblclick: () => openDetail(task.id)
    }, [
      el('div', { class: 'task-top' }, [checkbox, el('h4', { text: task.title })]),
      meta,
      actions
    ]);
  }

  function renderSelectionBar() {
    const n = state.selected.size;
    $('#selection-bar').classList.toggle('hidden', n === 0);
    $('#sel-count').textContent = String(n);
    const all = $('#sel-all');
    const selectable = state.tasks.length;
    all.checked = n > 0 && n === selectable;
    all.indeterminate = n > 0 && n < selectable;
  }

  /* ======================================================================
     4. 新建 / 编辑
     ====================================================================== */

  let editing = null;   // 正在编辑的任务（null 表示新建）

  function openTaskForm(task) {
    editing = task || null;
    const dlg = $('#task-dialog');
    const form = $('#task-form');
    form.reset();
    $('#task-form-error').classList.add('hidden');
    $('#task-dialog-title').textContent = editing ? '编辑任务' : '新建任务';
    $('#task-submit').textContent = editing ? '保存修改' : '创建任务';

    if (editing) {
      form.title.value = editing.title || '';
      form.description.value = editing.description || '';
      form.priority.value = editing.priority || 'NORMAL';
      form.estimateMin.value = editing.estimateMin ?? '';
      form.startAt.value = api.isoToLocalInput(editing.startAt);
      form.dueAt.value = api.isoToLocalInput(editing.dueAt);
      form.rrule.value = editing.rrule || '';
      form.tags.value = (editing.tags || []).join(', ');
      form.pinned.checked = !!editing.pinned;
    }
    openModal(dlg).then(() => {
      form.title.focus();
    });
  }

  function readForm() {
    const form = $('#task-form');
    const tags = form.tags.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    return {
      title: form.title.value.trim(),
      description: form.description.value.trim(),
      priority: form.priority.value,
      estimateMin: form.estimateMin.value ? Number(form.estimateMin.value) : undefined,
      startAt: api.localToIso(form.startAt.value) || undefined,
      dueAt: api.localToIso(form.dueAt.value) || undefined,
      rrule: form.rrule.value.trim() || undefined,
      tags: tags.length ? tags : undefined,
      pinned: form.pinned.checked
    };
  }

  async function submitTaskForm(ev) {
    ev.preventDefault();
    const body = readForm();
    const errorBox = $('#task-form-error');
    errorBox.classList.add('hidden');

    if (!body.title) {
      errorBox.textContent = '标题不能为空';
      errorBox.classList.remove('hidden');
      return;
    }

    try {
      if (editing) {
        // PATCH 需要带 version；字段为 undefined 表示不修改
        const payload = Object.assign({ version: editing.version }, body);
        const result = await withBusy(() => api.patchTask(editing.id, payload));
        const warnings = result && result.warnings ? result.warnings : [];
        toast('修改成功', 'ok');
        warnings.forEach((w) => toast(`⚠ ${w.message}`, 'warn'));
        closeTaskDialog();
        await loadTasks({ silent: true });
        if (state.detailId === editing.id) refreshDetail();
      } else {
        const created = await withBusy(() => api.createTask(body));
        toast(`已创建「${created.title}」`, 'ok');
        closeTaskDialog();
        await loadTasks({ silent: true });
      }
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 409) {
        errorBox.textContent = '任务已被其他人修改（409 版本冲突），请关闭后刷新再改。';
        errorBox.classList.remove('hidden');
      } else if (e instanceof api.ApiError && e.status === 400) {
        errorBox.textContent = `参数不合法：${e.message}`;
        errorBox.classList.remove('hidden');
      } else {
        showError(e);
      }
    }
  }

  /* ======================================================================
     5. 状态流转 / 删除
     ====================================================================== */

  async function transition(task, action) {
    try {
      const updated = await withBusy(() => api.transition(task.id, action, task.version));
      const label = { start: '已开始', complete: '已完成', reopen: '已恢复', cancel: '已取消' }[action] || '操作成功';
      toast(`${label}：${updated.title}`, 'ok');
      await loadTasks({ silent: true });
      if (state.detailId === task.id) refreshDetail();
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 409) {
        toast('版本冲突：任务刚被别人改过，已为你刷新', 'warn');
        await loadTasks({ silent: true });
        return;
      }
      showError(e);
    }
  }

  async function deleteTask(task) {
    let tokenInfo;
    try {
      tokenInfo = await withBusy(() => api.requestDeleteToken(task.id));
    } catch (e) {
      showError(e);
      return;
    }

    if (tokenInfo.deleted) {
      toast('已删除', 'ok');
      await loadTasks({ silent: true });
      return;
    }

    const ok = await confirmDialog(`确认删除「${task.title}」？`, (body, extraState = {}) => {
      body.append(el('div', { class: 'confirm-steps' }, [
        el('div', { class: 'step' }, [
          el('span', { class: 'num', text: '1' }),
          el('span', { text: '第一步已完成：后端返回了确认令牌（5 分钟内有效，只能用一次）' })
        ]),
        el('div', { class: 'step' }, [
          el('span', { class: 'num', text: '2' }),
          el('span', { text: '点击「删除」后，再带令牌请求一次 DELETE' })
        ]),
        el('div', { class: 'token-box', text: tokenInfo.confirmToken }),
        el('p', { class: 'muted small', text: '删除是软删除：之后查询一律 404，数据库仍保留审计记录。' })
      ]));
      return () => { extraState.done = true; };
    }, '删除');

    if (!ok) {
      toast('已取消，任务未删除');
      return;
    }

    try {
      await withBusy(() => api.deleteTaskWithToken(task.id, tokenInfo.confirmToken));
      toast(`已删除「${task.title}」`, 'ok');
      if (state.detailId === task.id) closeDetail();
      await loadTasks({ silent: true });
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 404) {
        toast('任务已不存在（可能已删除），已刷新列表', 'warn');
        await loadTasks({ silent: true });
        return;
      }
      showError(e);
    }
  }

  /* ======================================================================
     6. 详情抽屉
     ====================================================================== */

  let detailData = null;
  let detailReq = 0;   // 请求序号：只处理最后一次打开的详情，避免旧响应覆盖新状态

  async function openDetail(id) {
    state.detailId = id;
    switchTab('info');
    await openModal($('#detail-dialog'));
    await refreshDetail();
  }

  function closeDetail() {
    state.detailId = null;
    detailData = null;
    detailReq += 1;          // 让在途的详情请求作废
    const dlg = $('#detail-dialog');
    if (dlg.open) dlg.close();
  }

  function closeTaskDialog() {
    const dlg = $('#task-dialog');
    if (dlg.open) dlg.close();
  }

  function switchTab(name) {
    $$('.tabs .tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    $$('#detail-body .panel').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== name));
  }

  async function refreshDetail() {
    const id = state.detailId;
    if (!id) return;
    const seq = ++detailReq;
    try {
      const detail = await withBusy(() => api.getTask(id));
      if (seq !== detailReq || state.detailId !== id) return;   // 已被更新的请求取代
      detailData = detail;
      renderDetail(detail);
    } catch (e) {
      if (seq !== detailReq || state.detailId !== id) return;
      if (e instanceof api.ApiError && e.status === 404) {
        toast('任务已不存在', 'warn');
        closeDetail();
        await loadTasks({ silent: true });
      } else {
        showError(e);
      }
    }
  }

  function renderDetail(detail) {
    const t = detail.task;
    $('#detail-title').textContent = t.title;
    $('#detail-sub').textContent = `${t.id}`;
    $('#detail-version').textContent = String(t.version);

    const rows = [
      ['状态', statusLabel(t.status)],
      ['优先级', PRIORITY_LABEL[t.priority] || t.priority],
      ['开始时间', t.startAt ? fmtTime(t.startAt) : '—'],
      ['截止时间', t.dueAt ? fmtTime(t.dueAt) : '—'],
      ['预估耗时', t.estimateMin ? fmtDuration(t.estimateMin) : '—'],
      ['固定', t.pinned ? '是（重排期不动）' : '否'],
      ['重复规则', t.rrule || '—'],
      ['标签', (t.tags || []).length ? t.tags.map((x) => `#${x}`).join(' ') : '—'],
      ['父任务', t.parentId || '—'],
      ['创建时间', fmtTime(t.createdAt)],
      ['更新时间', fmtTime(t.updatedAt)]
    ];
    if (t.description) rows.push(['描述', t.description]);

    const info = el('dl', { class: 'kv' });
    rows.forEach(([k, v]) => {
      info.append(el('dt', { text: k }));
      info.append(el('dd', { text: v }));
    });

    const panel = $('[data-panel="info"]');
    panel.replaceChildren(info);

    if (detail.conflicts && detail.conflicts.length) {
      const box = el('div', { class: 'warn-box' }, [
        el('strong', { text: `⚠ 时间冲突 ${detail.conflicts.length} 处` }),
        el('ul', {}, detail.conflicts.map((c) => el('li', {
          text: `与「${c.title}」重叠 ${fmtDuration(c.overlapMinutes)}（${fmtTimeShort(c.overlapStart)} → ${fmtTimeShort(c.overlapEnd)}）`
        })))
      ]);
      panel.prepend(box);
    }

    renderDeps(detail);
  }

  function statusLabel(s) {
    return { TODO: '待办', DOING: '进行中', DONE: '已完成', CANCELLED: '已取消' }[s] || s;
  }

  function renderDeps(detail) {
    const task = detail.task;

    // 前置列表：带「移除」
    const depList = $('#dep-list');
    depList.replaceChildren();
    if (!detail.dependsOn.length) {
      depList.append(el('li', { class: 'muted', text: '没有前置任务' }));
    } else {
      detail.dependsOn.forEach((id) => {
        const known = state.tasks.find((x) => x.id === id) || state.cancelled.find((x) => x.id === id);
        depList.append(el('li', {}, [
          el('span', { class: 'grow', text: known ? known.title : id }),
          el('span', { class: `badge ${known ? known.status : ''}`, text: known ? statusLabel(known.status) : '—' }),
          el('button', {
            class: 'btn ghost small', text: '移除',
            onclick: () => removeDep(task.id, id)
          })
        ]));
      });
    }

    const blockList = $('#block-list');
    blockList.replaceChildren();
    if (!detail.blocks.length) {
      blockList.append(el('li', { class: 'muted', text: '没有后继任务' }));
    } else {
      detail.blocks.forEach((id) => {
        const known = state.tasks.find((x) => x.id === id);
        blockList.append(el('li', {}, [
          el('span', { class: 'grow', text: known ? known.title : id }),
          el('span', { class: `badge ${known ? known.status : ''}`, text: known ? statusLabel(known.status) : '—' })
        ]));
      });
    }

    // 候选前置：当前列表里除自己以外的任务
    const pick = $('#dep-pick');
    pick.replaceChildren(el('option', { value: '', text: '选择一个任务作为前置…' }));
    state.tasks
      .filter((x) => x.id !== task.id && !detail.dependsOn.includes(x.id))
      .forEach((x) => pick.append(el('option', { value: x.id, text: `${x.title}（${statusLabel(x.status)}）` })));
  }

  async function addDep() {
    const id = state.detailId;
    const dependsOnId = $('#dep-pick').value;
    const errBox = $('#dep-error');
    errBox.classList.add('hidden');
    if (!dependsOnId) {
      errBox.textContent = '先选择一个任务';
      errBox.classList.remove('hidden');
      return;
    }
    try {
      await withBusy(() => api.addDependency(id, dependsOnId));
      toast('已添加前置依赖', 'ok');
      await refreshDetail();
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 422) {
        errBox.textContent = `不能添加：${e.message}`;
        errBox.classList.remove('hidden');
      } else {
        showError(e);
      }
    }
  }

  async function removeDep(id, dependsOnId) {
    try {
      await withBusy(() => api.removeDependency(id, dependsOnId));
      toast('已移除依赖', 'ok');
      await refreshDetail();
    } catch (e) {
      showError(e);
    }
  }

  async function loadHistory() {
    const id = state.detailId;
    if (!id) return;
    const list = $('#history-list');
    list.replaceChildren(el('li', { class: 'muted', text: '加载中…' }));
    try {
      const entries = await withBusy(() => api.history(id));
      list.replaceChildren();
      if (!entries.length) {
        list.append(el('li', { class: 'muted', text: '暂无历史记录' }));
        return;
      }
      entries.forEach((e) => {
        list.append(el('li', {}, [
          el('div', {}, [
            el('strong', { text: ACTION_LABEL[e.action] || e.action }),
            el('span', { class: 'muted', text: ` · ${e.actor || 'anonymous'} · ${fmtTime(e.at)}` })
          ]),
          e.traceId ? el('div', { class: 'muted small', text: `trace ${e.traceId}` }) : null,
          el('details', {}, [
            el('summary', { text: '查看快照' }),
            el('pre', { text: `before: ${prettify(e.before)}\nafter:  ${prettify(e.after)}` })
          ])
        ]));
      });
    } catch (e) {
      list.replaceChildren(el('li', { class: 'muted', text: '历史加载失败' }));
      showError(e);
    }
  }

  function prettify(jsonText) {
    if (!jsonText) return '（无）';
    try { return JSON.stringify(JSON.parse(jsonText), null, 2); } catch { return jsonText; }
  }

  async function loadOccurrences() {
    const id = state.detailId;
    if (!id) return;
    const from = api.localToIso($('#occur-from').value);
    const to = api.localToIso($('#occur-to').value);
    if (!from || !to) {
      $('#occur-result').textContent = '请选择开始和结束时间';
      return;
    }
    try {
      const r = await withBusy(() => api.occurrences(id, from, to, 200));
      const items = r.occurrences || [];
      const box = $('#occur-result');
      box.replaceChildren();
      if (!items.length) {
        box.append(el('span', { text: '区间内没有实例' }));
        return;
      }
      box.append(el('div', { class: 'muted small', text: `共 ${items.length} 个实例${r.truncated ? '（已截断）' : ''}` }));
      const ul = el('ul', { class: 'list' });
      items.forEach((iso) => ul.append(el('li', {}, [el('span', { class: 'grow', text: fmtTime(iso) })])));
      box.append(ul);
    } catch (e) {
      showError(e);
    }
  }

  /* ======================================================================
     7. 批量操作
     ====================================================================== */

  function selectedTasks() {
    return state.tasks.filter((t) => state.selected.has(t.id));
  }

  async function batchPatch() {
    const tasks = selectedTasks();
    if (!tasks.length) return;
    const startAt = api.localToIso($('#batch-start').value);
    const estimateMin = $('#batch-estimate').value ? Number($('#batch-estimate').value) : undefined;
    if (!startAt && !estimateMin) {
      toast('请先填写开始时间或时长', 'warn');
      return;
    }
    await applyBatch(tasks.map((t) => ({ task: t, patch: { startAt: startAt || undefined, estimateMin } })));
  }

  /** 按依赖顺序往后排：每件事排在上一件结束之后，跳过已有固定时间且 pinned 的任务 */
  async function batchScheduleByDeps() {
    const tasks = selectedTasks();
    if (!tasks.length) return;
    const base = api.isoToLocalInput(new Date().toISOString());
    const startInput = $('#batch-start').value || base;
    let cursor = new Date(startInput);
    const ops = tasks.map((t) => {
      const estimate = t.estimateMin || 30;
      const start = new Date(cursor);
      cursor = new Date(cursor.getTime() + estimate * 60 * 1000);
      return { task: t, patch: { startAt: api.toIso(start), estimateMin: estimate } };
    });

    const ok = await confirmDialog(`按顺序排期 ${ops.length} 个任务？`, (body) => {
      const ul = el('ul', { class: 'list' });
      ops.forEach(({ task, patch }) => {
        ul.append(el('li', {}, [
          el('span', { class: 'grow', text: task.title }),
          el('span', { class: 'muted small', text: `${fmtTime(patch.startAt)} · ${fmtDuration(patch.estimateMin)}` })
        ]));
      });
      body.append(el('p', { class: 'muted small', text: '从所选开始时间起，按勾选顺序依次排列（每件事紧接上一件）。' }));
      body.append(ul);
    }, '执行排期');
    if (!ok) return;
    await applyBatch(ops);
  }

  async function applyBatch(items) {
    // 一条 op 对应一个任务：id + version + patch
    const ops = items.map(({ task, patch }) => ({
      id: task.id,
      version: task.version,
      patch: api.compact(patch)
    }));
    try {
      const r = await withBusy(() => api.batch(ops));
      toast(`批量修改成功：${r.applied} 条`, 'ok');
      state.selected.clear();
      await loadTasks({ silent: true });
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 409) {
        toast('批量修改失败：有条目版本冲突，整批已回滚（一条都没改）', 'err', e.message);
        await loadTasks({ silent: true });
        return;
      }
      showError(e);
    }
  }

  function exportSelected() {
    const tasks = selectedTasks();
    if (!tasks.length) return;
    const blob = new Blob([JSON.stringify(tasks, null, 2)], { type: 'application/json' });
    const a = el('a', { href: URL.createObjectURL(blob), download: 'cadence-tasks.json' });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  /* ======================================================================
     8. 忙闲日历 / 连通性
     ====================================================================== */

  function initBusyRange() {
    const r = todayRange();
    $('#busy-from').value = api.isoToLocalInput(api.toIso(r.from));
    $('#busy-to').value = api.isoToLocalInput(api.toIso(r.to));
    $('#occur-from').value = api.isoToLocalInput(api.toIso(r.from));
    const monthLater = new Date(r.from.getTime() + 30 * 24 * 3600 * 1000);
    $('#occur-to').value = api.isoToLocalInput(api.toIso(monthLater));
  }

  /** 演示模式下用内存里的任务计算忙闲，不请求后端 */
  function loadDemoBusy() {
    const from = $('#busy-from').value;
    const to = $('#busy-to').value;
    const all = state.tasks.concat(state.cancelled);
    const box = $('#busy-list');
    box.replaceChildren();
    const windows = all
      .filter((t) => t.startAt && t.estimateMin && t.status !== 'DONE' && t.status !== 'CANCELLED')
      .filter((t) => api.isoToLocalInput(t.startAt) >= from && api.isoToLocalInput(t.startAt) <= to)
      .map((t) => ({
        start: t.startAt,
        end: api.toIso(new Date(new Date(t.startAt).getTime() + t.estimateMin * 60000)),
        minutes: t.estimateMin
      }));
    if (!windows.length) {
      box.append(el('div', { class: 'empty', text: '这段时间完全空闲' }));
      return;
    }
    windows.forEach((w) => box.append(el('div', { class: 'busy-item' }, [
      el('span', { text: `${fmtTimeShort(w.start)} → ${fmtTimeShort(w.end)}` }),
      el('b', { text: fmtDuration(w.minutes) })
    ])));
    box.append(el('div', { class: 'muted small', text: `共 ${windows.length} 段，占用 ${fmtDuration(windows.reduce((s, w) => s + w.minutes, 0))}` }));
  }

  async function loadBusy() {
    const from = api.localToIso($('#busy-from').value);
    const to = api.localToIso($('#busy-to').value);
    const box = $('#busy-list');
    if (!from || !to) {
      box.replaceChildren(el('div', { class: 'muted small', text: '请选择时间区间' }));
      return;
    }
    try {
      const r = await withBusy(() => api.busy(from, to));
      const windows = r.windows || [];
      box.replaceChildren();
      if (!windows.length) {
        box.append(el('div', { class: 'empty', text: '这段时间完全空闲' }));
        return;
      }
      windows.forEach((w) => box.append(el('div', { class: 'busy-item' }, [
        el('span', { text: `${fmtTimeShort(w.start)} → ${fmtTimeShort(w.end)}` }),
        el('b', { text: fmtDuration(w.minutes) })
      ])));
      const total = windows.reduce((s, w) => s + w.minutes, 0);
      box.append(el('div', { class: 'muted small', text: `共 ${windows.length} 段，占用 ${fmtDuration(total)}` }));
    } catch (e) {
      box.replaceChildren();
      showError(e);
    }
  }

  async function checkConnection() {
    const list = $('#conn-list');
    list.replaceChildren(el('li', { class: 'muted small', text: '检测中…' }));
    const rows = [];
    try {
      const t0 = performance.now();
      const r = await api.listTasks({ limit: 1 });
      const ms = Math.round(performance.now() - t0);
      rows.push(connRow('后端 GET /api/v1/tasks', true, `${ms}ms · 共 ${r.total} 条`));
    } catch (e) {
      rows.push(connRow('后端 GET /api/v1/tasks', false, e instanceof api.ApiError ? e.message : String(e)));
    }
    rows.push(connRow('基础地址', true, api.baseUrl()));
    try {
      const t0 = performance.now();
      await api.busy(api.toIso(todayRange().from), api.toIso(todayRange().to));
      rows.push(connRow('忙闲 GET /api/v1/calendar/busy', true, `${Math.round(performance.now() - t0)}ms`));
    } catch (e) {
      rows.push(connRow('忙闲 GET /api/v1/calendar/busy', false, e instanceof api.ApiError ? e.message : String(e)));
    }
    if (api.isDemo()) {
      rows.push(connRow('数据来源', true, '演示数据（内存）'));
    }
    list.replaceChildren(...rows);
  }

  function connRow(name, ok, detail) {
    return el('li', {}, [
      el('span', { text: name }),
      el('span', { class: ok ? 'conn-ok' : 'conn-bad', text: ok ? '✔ UP' : '✘ DOWN', title: detail })
    ]);
  }

  /* ======================================================================
     9. 事件绑定与初始化
     ====================================================================== */

  function bind() {
    $('#btn-refresh').addEventListener('click', () => loadTasks());
    $('#btn-new').addEventListener('click', () => openTaskForm(null));
    $('#f-limit').addEventListener('change', () => loadTasks());

    const reload = debounce(() => loadTasks(), 350);
    ['#f-keyword', '#f-tag'].forEach((s) => $(s).addEventListener('input', reload));
    ['#f-priority', '#f-due'].forEach((s) => $(s).addEventListener('change', () => loadTasks()));

    $('#task-form').addEventListener('submit', submitTaskForm);
    $$('[data-close]').forEach((b) => b.addEventListener('click', closeTaskDialog));
    $('#detail-dialog').querySelector('[data-close-detail]')
      .addEventListener('click', closeDetail);
    $$('[data-close-confirm]').forEach((b) => b.addEventListener('click', () => closeConfirm(false)));
    $('#confirm-ok').addEventListener('click', () => closeConfirm(true));
    $('#confirm-dialog').addEventListener('cancel', () => closeConfirm(false));

    // 抽屉底部
    $('#btn-detail-edit').addEventListener('click', () => {
      if (detailData) {
        closeDetail();
        openTaskForm(detailData.task);
      }
    });
    $('#btn-detail-delete').addEventListener('click', () => {
      if (detailData) {
        closeDetail();
        deleteTask(detailData.task);
      }
    });

    // 标签页
    $$('.tabs .tab').forEach((tab) => tab.addEventListener('click', () => {
      switchTab(tab.dataset.tab);
      if (tab.dataset.tab === 'history') loadHistory();
      if (tab.dataset.tab === 'occur' && !$('#occur-result').textContent) loadOccurrences();
    }));

    $('#btn-add-dep').addEventListener('click', addDep);
    $('#btn-occur').addEventListener('click', loadOccurrences);

    // 批量
    $('#sel-all').addEventListener('change', (e) => {
      if (e.target.checked) state.tasks.forEach((t) => state.selected.add(t.id));
      else state.selected.clear();
      renderBoard();
      renderSelectionBar();
    });
    $('#btn-batch').addEventListener('click', batchPatch);
    $('#btn-batch-deps').addEventListener('click', batchScheduleByDeps);
    $('#btn-export').addEventListener('click', exportSelected);

    // 侧栏
    $('#btn-busy').addEventListener('click', () => {
      if (api.isDemo()) loadDemoBusy();
      else loadBusy();
    });
    $('#btn-conn').addEventListener('click', checkConnection);

    $$('#api-list [data-jump]').forEach((s) => s.addEventListener('click', () => {
      const jump = s.dataset.jump;
      if (jump === 'refresh') loadTasks().then(loadBusy);
      if (jump === 'new') openTaskForm(null);
      if (jump === 'busy') loadBusy();
      if (jump === 'batch') {
        if (!state.tasks.length) toast('先创建一些任务');
        else if (!state.selected.size) toast('先在卡片左侧勾选任务');
        else $('#batch-start').focus();
      }
      if (jump === 'drawer' || jump === 'edit' || jump === 'delete' || jump === 'state') {
        toast('在任务卡片上操作（卡片底部按钮 / 双击卡片看详情）');
      }
    }));

    // 快捷键：n 新建，/ 聚焦搜索，Esc 关闭抽屉
    document.addEventListener('keydown', (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (typing) return;
      if (e.key === 'n') { e.preventDefault(); openTaskForm(null); }
      if (e.key === '/') { e.preventDefault(); $('#f-keyword').focus(); }
    });
  }

  async function init() {
    // 事件绑定必须在两条分支之前：演示模式也要能点按钮
    bind();

    if (api.isDemo()) {
      document.body.classList.remove('demo-empty');
      const tip = el('div', { class: 'demo-tip' }, [
        el('strong', { text: '演示模式' }),
        ' 数据只存在内存里，刷新即重置。去掉地址栏的 ',
        el('code', { text: '?demo' }),
        ' 即连接真实后端 ',
        el('code', { text: api.baseUrl() }),
        '。'
      ]);
      $('.topbar').after(tip);
      state.demo = {
        tasks: [
          demoTask('d1', '交季度报告', 'URGENT', { dueAt: atDay(1, 18), estimateMin: 360, tags: ['work'] }),
          demoTask('d2', '写周报', 'HIGH', { startAt: atDay(0, 10), estimateMin: 60, tags: ['work'] }),
          demoTask('d3', '每周站会', 'NORMAL', { startAt: atDay(0, 14), estimateMin: 30, rrule: 'FREQ=WEEKLY;BYDAY=WE;COUNT=3' }),
          demoTask('d4', '准备下周的面试', 'HIGH', { dueAt: atDay(3, 20), estimateMin: 180, status: 'DOING', startAt: atDay(0, 9) }),
          demoTask('d5', '看论文', 'LOW', { startAt: atDay(2, 15), estimateMin: 90, tags: ['study'] }),
          demoTask('d6', '客户演示', 'URGENT', { startAt: atDay(1, 15), estimateMin: 120, pinned: true, tags: ['work'] }),
          demoTask('d7', '整理报销单', 'NORMAL', { dueAt: atDay(-1, 18), estimateMin: 30, status: 'DONE' }),
          demoTask('d8', '预约体检', 'LOW', { estimateMin: 45, status: 'CANCELLED' })
        ],
        selected: new Set(['d2', 'd5'])
      };
      const r = state.demo;
      state.tasks = r.tasks.filter((t) => t.status !== 'CANCELLED');
      state.cancelled = r.tasks.filter((t) => t.status === 'CANCELLED');
      state.total = state.tasks.length;
      r.selected.forEach((id) => state.selected.add(id));
      renderBoard();
      renderSelectionBar();
      $('#list-meta').textContent = `演示数据：共 ${state.total} 条`;
      initBusyRange();
      loadDemoBusy();
      checkConnection();
      return;
    }

    initBusyRange();
    await loadTasks();
    await loadBusy();
    checkConnection();
  }
  document.addEventListener('DOMContentLoaded', init);

  /* 供调试/截图脚本调用（控制台里可以直接  CadenceApp.openDetail(id)） */
  window.CadenceApp = {
    openDetail,
    openTaskForm,
    loadTasks,
    loadBusy,
    state
  };
})();
