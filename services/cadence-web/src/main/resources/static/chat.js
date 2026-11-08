/* =========================================================================
   Cadence 前端 · 对话区（原生 JS，无构建，无外部依赖）
   依赖 api.js 的 window.cadence 与 app.js 的 window.CadenceApp。

   数据流：
     POST {agent}/chat -> 202 {streamId, traceId}
       -> EventSource GET {agent}/stream/{streamId}
       -> 7 个事件：token / tool_call / tool_result / clarify / confirm / done / error

   断线重连：
     EventSource 原生会带 Last-Event-ID 重连；这里另外保存最后事件 id，
     必要时用 ?lastEventId= 新建 EventSource（服务端同时接受 header 与 query）。
     渲染层用 eventId 去重，重连不会重复追加已有内容。
   ========================================================================= */
(function (global) {
  'use strict';

  const api = global.cadence;
  if (!api) return;

  const $ = (sel, root = document) => root.querySelector(sel);
  const params = new URLSearchParams(global.location.search);
  const META = $('meta[name="cadence-agent-url"]');

  function safeGet(storage, key) {
    try { return storage ? storage.getItem(key) : null; } catch { return null; }
  }

  function safeSet(storage, key, value) {
    try { if (storage) storage.setItem(key, value); } catch { /* 隐私模式/文件页可能禁用 */ }
  }

  function resolveAgentUrl() {
    const configured = params.get('agent')
      || safeGet(global.localStorage, 'cadence.agentUrl')
      || (META && META.content)
      || global.CADENCE_AGENT_URL
      || '';
    if (configured) return String(configured).replace(/\/+$/, '');
    // 页面由 Core 托管时 Agent 在另一个端口；端口是部署默认值，可用 ?agent= 覆盖。
    return `${global.location.protocol}//${global.location.hostname}:18000`.replace(/\/+$/, '');
  }

  let agentBaseUrl = resolveAgentUrl();
  const SESSION_KEY = 'cadence.chat.session';
  const sessionId = safeGet(global.sessionStorage, SESSION_KEY)
    || `web_${Date.now().toString(36)}_${Math.random().toString(16).slice(2, 8)}`;
  safeSet(global.sessionStorage, SESSION_KEY, sessionId);

  const state = {
    source: null,
    streamId: '',
    lastEventId: 0,
    rendered: new Set(),
    done: false,
    aborted: false,
    reconnectTimer: null,
    reconnectCount: 0,
    assistant: null,
    toolCards: [],
    pendingConfirm: null
  };

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

  function scroll() {
    const log = $('#chat-log');
    if (log) log.scrollTop = log.scrollHeight;
  }

  function hideEmpty() {
    const empty = $('#chat-empty');
    if (empty) empty.remove();
  }

  function addMessage(role, text) {
    hideEmpty();
    const bubble = el('div', { class: `chat-msg ${role}` }, [
      el('div', { class: 'chat-role', text: role === 'user' ? '你' : 'Cadence' }),
      el('div', { class: 'chat-bubble', text })
    ]);
    $('#chat-log').append(bubble);
    scroll();
    return bubble;
  }

  function addSystem(text, kind = '') {
    hideEmpty();
    const node = el('div', { class: `chat-system ${kind}`.trim(), text });
    $('#chat-log').append(node);
    scroll();
    return node;
  }

  function assistantBubble() {
    if (state.assistant && state.assistant.isConnected) return state.assistant.querySelector('.chat-bubble');
    hideEmpty();
    const wrap = el('div', { class: 'chat-msg assistant' }, [
      el('div', { class: 'chat-role', text: 'Cadence' }),
      el('div', { class: 'chat-bubble' })
    ]);
    $('#chat-log').append(wrap);
    state.assistant = wrap;
    scroll();
    return wrap.querySelector('.chat-bubble');
  }

  function appendToken(text) {
    if (!text) return;
    const bubble = assistantBubble();
    bubble.textContent += text;
    scroll();
  }

  function resetAssistant() {
    state.assistant = null;
  }

  function toolCall(data) {
    hideEmpty();
    const name = data.name || 'tool';
    const args = data.args || {};
    const card = el('div', { class: 'tool-card running', dataset: { name } }, [
      el('details', {}, [
        el('summary', {}, [
          el('span', { class: 'tool-dot' }),
          el('strong', { text: name }),
          el('span', { class: 'tool-status', text: '调用中…' })
        ]),
        el('pre', { class: 'tool-args', text: JSON.stringify(args, null, 2) })
      ])
    ]);
    toolCardState(card, { name, args, done: false });
    state.toolCards.push(card);
    $('#chat-log').append(card);
    scroll();
  }

  function toolCardState(card, data) {
    card._cadence = data;
  }

  function toolResult(data) {
    const name = data.name || '';
    const card = [...state.toolCards].reverse().find((node) => {
      const info = node._cadence || {};
      return info.name === name && !info.done;
    });
    if (!card) {
      addSystem(`工具 ${name} 返回：${data.summary || (data.ok ? '成功' : '失败')}`, data.ok ? 'ok' : 'err');
      return;
    }
    const info = card._cadence || {};
    info.done = true;
    card._cadence = info;
    card.classList.remove('running');
    card.classList.add(data.ok ? 'ok' : 'err');
    const status = card.querySelector('.tool-status');
    if (status) {
      status.textContent = data.ok
        ? `成功 · ${data.durationMs || 0}ms`
        : `失败${data.code ? ' · ' + data.code : ''}`;
    }
    const summary = data.summary || '';
    if (summary) {
      card.append(el('div', { class: 'tool-summary', text: summary }));
    }
    scroll();
  }

  function clarify(data) {
    hideEmpty();
    const box = el('div', { class: 'chat-msg assistant' }, [
      el('div', { class: 'chat-role', text: 'Cadence · 需要确认' }),
      el('div', { class: 'chat-bubble clarify' }, [
        el('div', { text: data.question || '还需要你补充一点信息。' }),
        data.missing && data.missing.length
          ? el('div', { class: 'muted small', text: `缺：${data.missing.join('、')}` })
          : null
      ])
    ]);
    const options = data.options || [];
    if (options.length) {
      const row = el('div', { class: 'clarify-options' });
      options.forEach((option) => {
        row.append(el('button', {
          class: 'btn small',
          text: option,
          onclick: () => sendMessage(option)
        }));
      });
      box.querySelector('.chat-bubble').append(row);
    }
    $('#chat-log').append(box);
    scroll();
  }

  function diffGroup(title, items, render) {
    return el('details', { class: 'diff-group', open: true }, [
      el('summary', {}, [el('strong', { text: title }), el('span', { class: 'muted small', text: ` ${items.length} 条` })]),
      items.length
        ? el('ul', { class: 'diff-list' }, items.map((item) => el('li', { text: render(item) })))
        : el('div', { class: 'muted small', text: '无' })
    ]);
  }

  function renderDiff(plan) {
    const moved = plan.moved || [];
    const kept = plan.kept || [];
    const cannot = plan.cannotFit || plan.cannot_fit || [];
    const fmt = (item) => {
      if (typeof item === 'string') return item;
      return item.title || item.message || JSON.stringify(item);
    };
    return el('div', { class: 'confirm-diff' }, [
      diffGroup('会移动 / 会执行', moved, fmt),
      diffGroup('保持不动', kept, fmt),
      diffGroup('放不下 / 被拒绝', cannot, fmt)
    ]);
  }

  function confirmPanel(data) {
    hideEmpty();
    const plan = data.plan || {};
    const box = el('div', { class: 'chat-msg assistant' }, [
      el('div', { class: 'chat-role', text: 'Cadence · 等待确认' }),
      el('div', { class: 'chat-bubble confirm-card' }, [
        el('div', { text: plan.message || `即将执行 ${data.tool || '操作'}，需要你确认。` }),
        renderDiff(plan),
        el('div', { class: 'muted small', text: `影响 ${data.affected || 0} 条 · 令牌 ${String(data.confirmToken || '').slice(0, 10)}…` })
      ])
    ]);
    const buttons = el('div', { class: 'confirm-actions' }, [
      el('button', {
        class: 'btn danger',
        text: '确认执行',
        onclick: () => resolveConfirm(data, true)
      }),
      el('button', {
        class: 'btn ghost',
        text: '放弃',
        onclick: () => resolveConfirm(data, false)
      })
    ]);
    box.querySelector('.chat-bubble').append(buttons);
    state.pendingConfirm = data;
    $('#chat-log').append(box);
    scroll();
  }

  async function resolveConfirm(data, approve) {
    if (!state.pendingConfirm) return;
    state.pendingConfirm = null;
    if (api.isDemo()) {
      addSystem(approve ? '演示模式：已确认执行（内存操作）' : '演示模式：已放弃', approve ? 'ok' : '');
      finishDemo(approve);
      return;
    }
    try {
      const response = await fetch(`${agentBaseUrl}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmToken: data.confirmToken, approve: !!approve })
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok || !payload || payload.code !== 0) {
        const message = (payload && payload.message) || `HTTP ${response.status}`;
        throw new Error(message);
      }
      const result = payload.data || {};
      addSystem(
        approve
          ? `已执行：${result.tool || data.tool || '操作'}${result.applied ? ` · ${result.applied} 条` : ''}`
          : '已放弃，未修改任何数据',
        approve ? 'ok' : ''
      );
      if (approve && global.CadenceApp) global.CadenceApp.loadTasks({ silent: true });
    } catch (err) {
      addSystem(`确认失败：${err.message || err}`, 'err');
    }
  }

  function suggestionTitle(s) {
    return s.hint || s.tool || s.field || '建议';
  }

  function suggestionSubtitle(s) {
    return s.reason || (s.args ? JSON.stringify(s.args) : '');
  }

  function renderDone(data) {
    const summary = el('div', { class: 'chat-msg assistant' }, [
      el('div', { class: 'chat-role', text: 'Cadence · 本轮结束' }),
      el('div', { class: 'chat-bubble done-card' }, [
        el('div', { class: 'done-badges' }, [
          el('span', { class: 'badge', text: `step ${data.steps || 0}` }),
          el('span', { class: 'badge', text: `¥${Number(data.costYuan || 0).toFixed(6)}` }),
          el('span', { class: 'badge', text: `endedBy ${data.endedBy || '—'}` }),
          data.degraded ? el('span', { class: 'badge warn', text: `degraded · ${data.degradeReason || ''}` }) : null,
          data.errorCode ? el('span', { class: 'badge err', text: data.errorCode }) : null
        ]),
        data.text ? el('div', { class: 'done-text', text: data.text }) : null
      ])
    ]);
    const suggestions = data.suggestions || [];
    if (suggestions.length) {
      const row = el('div', { class: 'suggestion-row' });
      suggestions.forEach((item) => {
        const title = suggestionTitle(item);
        const sub = suggestionSubtitle(item);
        row.append(el('button', {
          class: 'suggestion',
          title: sub,
          onclick: () => {
            const input = $('#message-input');
            input.value = item.hint
              || (item.field ? `补一下 ${item.field}` : '')
              || (item.tool ? `${item.tool} ${JSON.stringify(item.args || {})}` : '');
            input.focus();
          }
        }, [
          el('strong', { text: title }),
          sub ? el('small', { text: sub }) : null
        ]));
      });
      summary.querySelector('.done-card').append(row);
    }
    if (data.traceId) {
      summary.querySelector('.done-card').append(el('button', {
        class: 'btn ghost small trace-link',
        text: '查看 trace 时间线',
        onclick: () => global.CadenceTrace && global.CadenceTrace.open(data.traceId)
      }));
    }
    $('#chat-log').append(summary);
    scroll();
  }

  function showError(data) {
    hideEmpty();
    const text = data.message || data.code || '发生错误';
    const box = el('div', { class: 'chat-msg assistant' }, [
      el('div', { class: 'chat-role', text: 'Cadence · 错误' }),
      el('div', { class: 'chat-bubble error-card' }, [
        el('div', { class: 'error-code', text: data.code || 'ERROR' }),
        el('div', { text }),
        data.suggestions && data.suggestions.length
          ? el('ul', { class: 'error-suggestions' }, data.suggestions.map((s) => el('li', {
            text: `${suggestionTitle(s)}${suggestionSubtitle(s) ? ' — ' + suggestionSubtitle(s) : ''}`
          })))
          : null
      ])
    ]);
    $('#chat-log').append(box);
    scroll();
  }

  function onEvent(type, data, eventId) {
    if (eventId) {
      const key = String(eventId);
      if (state.rendered.has(key)) return;
      state.rendered.add(key);
      state.lastEventId = Number(key) || state.lastEventId;
    }
    if (type === 'token') appendToken(data.text || '');
    else if (type === 'tool_call') toolCall(data);
    else if (type === 'tool_result') toolResult(data);
    else if (type === 'clarify') clarify(data);
    else if (type === 'confirm') confirmPanel(data);
    else if (type === 'error') showError(data);
    else if (type === 'done') {
      state.done = true;
      renderDone(data);
      closeSource();
      setStatus('本轮结束');
      if (global.CadenceApp) global.CadenceApp.loadTasks({ silent: true });
    }
  }

  function setStatus(text) {
    const node = $('#chat-status');
    if (node) node.textContent = text || '';
  }

  function closeSource() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function attach(source) {
    ['token', 'tool_call', 'tool_result', 'clarify', 'confirm', 'done', 'error'].forEach((type) => {
      source.addEventListener(type, (event) => {
        let data = {};
        try { data = JSON.parse(event.data); } catch { data = { message: event.data }; }
        onEvent(type, data, event.lastEventId);
      });
    });
  }

  function openStream(streamId) {
    closeSource();
    state.streamId = streamId;
    state.done = false;
    state.aborted = false;
    state.reconnectCount = 0;
    connect(streamId);
  }

  function connect(streamId) {
    closeSource();
    const query = state.lastEventId > 0 ? `?lastEventId=${encodeURIComponent(state.lastEventId)}` : '';
    const source = new EventSource(`${agentBaseUrl}/stream/${encodeURIComponent(streamId)}${query}`);
    state.source = source;
    setStatus('连接中…');
    source.onopen = () => {
      setStatus('已连接');
      state.reconnectCount = 0;
    };
    source.onerror = () => {
      if (state.done || state.aborted) return;
      if (source.readyState === EventSource.CLOSED) {
        setStatus('连接断开，重连中…');
        scheduleReconnect(streamId);
      } else {
        setStatus('网络抖动，等待自动重连…');
      }
    };
    attach(source);
  }

  function scheduleReconnect(streamId) {
    if (state.reconnectTimer || state.done || state.aborted) return;
    if (state.reconnectCount >= 5) {
      setStatus('重连失败，请点击重试');
      return;
    }
    state.reconnectCount += 1;
    const delay = Math.min(5000, 500 * Math.pow(2, state.reconnectCount - 1));
    state.reconnectTimer = global.setTimeout(() => {
      state.reconnectTimer = null;
      if (!state.done && !state.aborted) connect(streamId);
    }, delay);
  }

  async function postChat(message) {
    const response = await fetch(`${agentBaseUrl}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, message })
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || payload.code !== 0) {
      const messageText = (payload && payload.message) || `HTTP ${response.status}`;
      throw new Error(messageText);
    }
    return payload.data || {};
  }

  async function sendMessage(rawText) {
    const text = String(rawText || '').trim();
    if (!text) return;
    hideEmpty();
    addMessage('user', text);
    resetAssistant();
    state.toolCards = [];
    state.pendingConfirm = null;
    state.rendered.clear();
    state.lastEventId = 0;
    setStatus('发送中…');
    $('#message-input').value = '';
    if (api.isDemo()) {
      simulate(text);
      return;
    }
    try {
      const data = await postChat(text);
      if (!data.streamId) throw new Error('后端没有返回 streamId');
      setStatus('等待响应…');
      openStream(data.streamId);
    } catch (err) {
      setStatus('发送失败');
      showError({ code: 'CHAT_FAILED', message: err.message || String(err) });
    }
  }

  function abort() {
    if (state.done) return;
    state.aborted = true;
    closeSource();
    setStatus('已停止接收');
    addSystem('已停止接收这一轮的事件；后台循环可能仍在执行，任务状态以看板为准。', 'warn');
  }

  /* ------------------------------------------------------------------
     演示模式：不连后端，按剧本走一遍 7 类事件
     ------------------------------------------------------------------ */

  function simulate(text) {
    state.rendered.clear();
    setStatus('演示模式');
    global.setTimeout(() => appendToken('收到，我先看看你的任务。'), 120);
    global.setTimeout(() => {
      toolCall({ name: 'task.search', args: { keyword: text.slice(0, 12), limit: 10 }, step: 1 });
    }, 380);
    global.setTimeout(() => {
      toolResult({ name: 'task.search', ok: true, summary: '找到 3 条，共 3 条', durationMs: 18 });
    }, 700);
    if (/删|清理|删除/.test(text)) {
      global.setTimeout(() => {
        confirmPanel({
          confirmToken: 'ct_demo_chat',
          tool: 'task.delete',
          affected: 1,
          plan: {
            kind: 'delete',
            moved: [],
            kept: [],
            cannotFit: [],
            message: '演示：即将删除「旧任务」，删除后可用恢复接口找回。'
          }
        });
      }, 1050);
      return;
    }
    if (/报告|那个|什么|怎么/.test(text)) {
      global.setTimeout(() => {
        clarify({
          question: '你指的是哪一条「报告」？是写周报，还是客户演示？',
          missing: ['task_id', 'action'],
          options: ['写周报', '客户演示']
        });
        setStatus('等待你补充');
      }, 1050);
      return;
    }
    global.setTimeout(() => finishDemo(true), 1050);
  }

  function finishDemo(ok) {
    state.done = true;
    onEvent('done', {
      traceId: 'tr_demo_chat',
      costYuan: 0,
      degraded: false,
      degradeReason: '',
      steps: 2,
      endedBy: 'finish',
      text: ok ? '演示完成：已查询任务。' : '演示：已取消。',
      actionsTaken: [],
      suggestions: [
        { hint: '继续拆解', reason: '把这件事拆成 2–3 步' },
        { tool: 'task.search', args: { status: 'TODO' } },
        { field: 'due_at', reason: '补一个截止时间' }
      ],
      promptVersion: 'demo'
    });
  }

  function init() {
    if (!$('#chat') || !$('#composer')) return;
    $('#composer').addEventListener('submit', (event) => {
      event.preventDefault();
      sendMessage($('#message-input').value);
    });
    $('#message-input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage($('#message-input').value);
      }
    });
    $('#btn-abort').addEventListener('click', abort);
    $('#btn-chat-clear').addEventListener('click', () => {
      const log = $('#chat-log');
      log.replaceChildren();
      const empty = el('div', { class: 'chat-empty', id: 'chat-empty' }, [
        el('p', { text: '直接说一句话，例如「今天还有什么没做完？」。' }),
        el('div', { class: 'chat-examples' }, [
          el('button', { class: 'example', text: '示例：今天还有什么没做完？', onclick: () => sendMessage('今天还有什么没做完？') }),
          el('button', { class: 'example', text: '示例：把写周报拆成三小步', onclick: () => sendMessage('把写周报拆成三小步') }),
          el('button', { class: 'example', text: '示例：旧任务删掉吧', onclick: () => sendMessage('旧任务删掉吧') })
        ])
      ]);
      log.append(empty);
      resetAssistant();
      state.toolCards = [];
      state.done = false;
      setStatus('');
    });
    document.querySelectorAll('.chat-examples .example').forEach((button) => {
      button.addEventListener('click', () => sendMessage(button.dataset.example || button.textContent));
    });
    setStatus(api.isDemo() ? '演示模式（不连后端）' : `Agent ${agentBaseUrl}`);
  }

  global.CadenceChat = {
    agentBaseUrl: () => agentBaseUrl,
    setAgentBaseUrl: (url) => {
      agentBaseUrl = String(url || '').replace(/\/+$/, '');
      if (agentBaseUrl) safeSet(global.localStorage, 'cadence.agentUrl', agentBaseUrl);
    },
    send: sendMessage,
    abort,
    sessionId: () => sessionId
  };

  document.addEventListener('DOMContentLoaded', init);
})(window);
