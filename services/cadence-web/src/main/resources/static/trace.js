/* =========================================================================
   Cadence 前端 · Trace 抽屉（原生 JS，无构建）
   GET {agent}/trace/{id} -> 时间线：LLM / TOOL / GUARD / DEGRADE
   每步显示 duration_ms、tokens、ok；点工具名展开 detail。
   ========================================================================= */
(function (global) {
  'use strict';

  const api = global.cadence;
  const $ = (sel, root = document) => root.querySelector(sel);

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([key, value]) => {
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? '' : value);
      });
    }
    (Array.isArray(children) ? children : children ? [children] : []).forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function agentBase() {
    return global.CadenceChat ? global.CadenceChat.agentBaseUrl() : '';
  }

  function open(traceId) {
    const dialog = $('#trace');
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    const body = $('#trace-body');
    body.replaceChildren(el('div', { class: 'muted', text: `加载 trace ${traceId}…` }));
    if (api && api.isDemo()) {
      render(demoTrace(traceId));
      return;
    }
    fetch(`${agentBase()}/trace/${encodeURIComponent(traceId)}`)
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (!response.ok || !payload || payload.code !== 0) {
          throw new Error((payload && payload.message) || `HTTP ${response.status}`);
        }
        return payload.data || {};
      })
      .then(render)
      .catch((err) => {
        body.replaceChildren(el('div', { class: 'error-card', text: `trace 加载失败：${err.message || err}` }));
      });
  }

  function render(trace) {
    const body = $('#trace-body');
    if (!body) return;
    const steps = trace.steps || [];
    const head = el('div', { class: 'trace-head' }, [
      el('div', {}, [
        el('strong', { text: trace.traceId || 'trace' }),
        el('span', { class: 'muted small', text: `  ${trace.totalMs || 0}ms  ¥${Number(trace.costYuan || 0).toFixed(6)}` })
      ]),
      el('div', { class: 'trace-badges' }, [
        el('span', { class: 'badge', text: `prompt ${trace.promptVersion || '—'}` }),
        el('span', { class: 'badge', text: `session ${trace.sessionId || '—'}` }),
        trace.degraded ? el('span', { class: 'badge warn', text: 'DEGRADED' }) : null
      ])
    ]);
    const list = el('ol', { class: 'trace-timeline' });
    steps.forEach((step) => list.append(renderStep(step)));
    body.replaceChildren(head, list);
  }

  function renderStep(step) {
    const kind = String(step.kind || 'STEP');
    const detail = step.detail || {};
    const item = el('li', { class: `trace-step kind-${kind.toLowerCase()}${step.ok === false ? ' err' : ''}` }, [
      el('div', { class: 'trace-line' }, [
        el('span', { class: `trace-kind kind-${kind.toLowerCase()}`, text: kind }),
        el('span', { class: 'trace-name', text: step.name || '—' }),
        el('span', { class: 'trace-meta', text: `${step.durationMs || 0}ms` }),
        step.tokensIn || step.tokensOut
          ? el('span', { class: 'trace-meta', text: `in ${step.tokensIn || 0} / out ${step.tokensOut || 0}` })
          : null,
        el('span', { class: step.ok === false ? 'trace-ok err' : 'trace-ok', text: step.ok === false ? '✘' : '✔' })
      ])
    ]);
    const name = item.querySelector('.trace-name');
    name.setAttribute('role', 'button');
    name.setAttribute('tabindex', '0');
    name.title = '点击展开 detail';
    const pre = el('pre', { class: 'trace-detail hidden', text: JSON.stringify(detail, null, 2) });
    const toggle = () => pre.classList.toggle('hidden');
    name.addEventListener('click', toggle);
    name.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        toggle();
      }
    });
    item.append(pre);
    if (detail.error || detail.code) {
      item.append(el('div', { class: 'trace-error', text: detail.error || detail.code }));
    }
    return item;
  }

  function demoTrace(traceId) {
    return {
      traceId,
      sessionId: 'demo',
      promptVersion: 'demo',
      totalMs: 342,
      costYuan: 0,
      degraded: false,
      steps: [
        { seq: 1, kind: 'LLM', name: 'route.classify', durationMs: 0, ok: true, detail: { tier: 'fast', rule: 'pure-query' } },
        { seq: 2, kind: 'LLM', name: 'llm.step1', durationMs: 210, tokensIn: 920, tokensOut: 48, ok: true, detail: { model: 'fake-model', tool_calls: ['task.search'] } },
        { seq: 3, kind: 'TOOL', name: 'task.search', durationMs: 18, ok: true, detail: { summary: '找到 3 条，共 3 条', args: { limit: 10 } } },
        { seq: 4, kind: 'GUARD', name: 'guard.classify', durationMs: 0, ok: true, detail: { intent: 'QUERY', ambiguous: false } },
        { seq: 5, kind: 'DEGRADE', name: 'llm.effective_tier', durationMs: 0, ok: false, detail: { requested: 'reason', used: 'fast' } },
        { seq: 6, kind: 'LLM', name: 'llm.step2', durationMs: 114, tokensIn: 1010, tokensOut: 22, ok: true, detail: { model: 'fake-model' } }
      ]
    };
  }

  function bind() {
    const dialog = $('#trace');
    if (!dialog) return;
    dialog.querySelectorAll('[data-close-trace]').forEach((button) => {
      button.addEventListener('click', () => dialog.close());
    });
    dialog.addEventListener('cancel', () => dialog.close());
  }

  global.CadenceTrace = { open };
  document.addEventListener('DOMContentLoaded', bind);
})(window);
