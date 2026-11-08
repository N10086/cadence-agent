/* =========================================================================
   Cadence 前端 · API 封装
   对应 docs/接口文档.md v0.2.0，所有接口统一返回 { code, message, data }。

   约定：
     - 成功：code === 0，业务数据在 data
     - 失败：抛出 ApiError，带 status / code / message，由调用方提示用户
     - 204 无响应体：data 为 null

   演示模式：地址栏带 ?demo 时，用内存数据代替真实接口，
   后端没起来也能把所有按钮点通（数据只存在内存，刷新即重置）。
   ========================================================================= */
(function (global) {
  'use strict';

  const query = new URLSearchParams(global.location.search);
  const DEMO_FLAG = query.has('demo') && !query.get('api');

  /* ------------------------------------------------------------------
     基础工具：时间格式 / 空值处理 / 错误类型 / 幂等键
     ------------------------------------------------------------------ */

  class ApiError extends Error {
    constructor(status, code, message, payload) {
      super(message || `HTTP ${status}`);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.payload = payload;
    }
  }

  /** 本地时间 → 带偏移的 ISO-8601，如 2026-11-05T09:00:00+08:00 */
  function toIso(date) {
    if (!(date instanceof Date) || isNaN(date.getTime())) return null;
    const pad = (n) => String(Math.abs(n)).padStart(2, '0');
    const off = -date.getTimezoneOffset();
    const sign = off >= 0 ? '+' : '-';
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
      + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
      + `${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
  }

  /** datetime-local 的值（2026-11-05T09:00）→ 带偏移的 ISO-8601 */
  function localToIso(value) {
    if (!value) return null;
    return toIso(new Date(value));
  }

  /** ISO-8601（带偏移）→ datetime-local 输入框的值（本地时间） */
  function isoToLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  /** 去掉空串 / null / undefined，避免把空值写进 PATCH 请求体 */
  function compact(obj) {
    const out = {};
    Object.entries(obj).forEach(([k, v]) => {
      if (v === undefined || v === null || v === '') return;
      out[k] = v;
    });
    return out;
  }

  /** 幂等键：重复点击 / 网络重试不会重复建任务 */
  function idemKey() {
    const rnd = (global.crypto && crypto.randomUUID)
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
    return `web_${Date.now().toString(36)}_${rnd}`;
  }

  /* ------------------------------------------------------------------
     演示数据（懒加载：只有 ?demo 时才创建）
     ------------------------------------------------------------------ */
  let demoStore = null;

  function demo() {
    if (!DEMO_FLAG) return null;
    if (!demoStore) demoStore = createDemo();
    return demoStore;
  }

  function createDemo() {
    const now = Date.now();
    const H = 3600 * 1000;
    const at = (offsetH, hour = 9, minute = 0) => {
      const d = new Date(now + offsetH * H);
      d.setHours(hour, minute, 0, 0);
      return toIso(d);
    };

    const store = new Map();
    [
      { title: '交季度报告', priority: 'URGENT', dueAt: at(1, 18), estimateMin: 360, tags: ['work'] },
      { title: '写周报', priority: 'HIGH', startAt: at(0, 10), estimateMin: 60, tags: ['work'] },
      { title: '每周站会', priority: 'NORMAL', startAt: at(0, 14), estimateMin: 30, rrule: 'FREQ=WEEKLY;BYDAY=WE;COUNT=3' },
      { title: '准备下周的面试', priority: 'HIGH', dueAt: at(3, 20), estimateMin: 180 },
      { title: '看论文', priority: 'LOW', startAt: at(2, 15), estimateMin: 90, tags: ['study'] },
      { title: '客户演示', priority: 'URGENT', startAt: at(1, 15), estimateMin: 120, pinned: true, tags: ['work'] },
      { title: '整理报销单', priority: 'NORMAL', dueAt: at(-1, 18), estimateMin: 30, status: 'DONE' },
      { title: '预约体检', priority: 'LOW', estimateMin: 45, status: 'CANCELLED' }
    ].forEach((seed, i) => {
      // id 必须是 'd1'..'d8'：页面演示数据（app.js 的 demoTask）与截图脚本都用这个约定
      const id = 'd' + (i + 1);
      store.set(id, Object.assign({
        id, status: 'TODO', priority: 'NORMAL', pinned: false, tags: [],
        version: 0, createdAt: toIso(new Date()), updatedAt: toIso(new Date())
      }, seed));
    });

    const deps = new Map();
    const hist = new Map();
    const all = () => Array.from(store.values());
    const clone = (t) => Object.assign({}, t);
    const fail = (status, message) => { throw new ApiError(status, status, message, null); };
    const bump = (t) => { t.version += 1; t.updatedAt = toIso(new Date()); return clone(t); };

    function ensureHistory(id) {
      if (!hist.size) {
        hist.set(id, [{
          action: 'CREATE', actor: 'web', traceId: 'tr_demo', before: null,
          after: JSON.stringify(store.get(id) || {}), at: toIso(new Date())
        }]);
      }
      return hist.get(id) || [];
    }

    return {
      list(params) {
        const items = all().filter((t) =>
          (!params.status || t.status === params.status)
          && (!params.priority || t.priority === params.priority)
          && (!params.keyword || t.title.toLowerCase().includes(String(params.keyword).toLowerCase())));
        const limit = Number(params.limit || 20);
        return { items: items.slice(0, limit).map(clone), hasMore: items.length > limit, total: items.length };
      },

      get(id) {
        const t = store.get(id);
        if (!t) fail(404, '任务不存在: ' + id);
        return { task: clone(t), dependsOn: (deps.get(id) || []).slice(), blocks: [], conflicts: [] };
      },

      create(body) {
        const id = 'd' + (store.size + 1) + '-' + Date.now().toString(36);
        const t = Object.assign({ status: 'TODO', priority: 'NORMAL', pinned: false, tags: [] }, body,
          { id, version: 0, createdAt: toIso(new Date()), updatedAt: toIso(new Date()) });
        store.set(id, t);
        return clone(t);
      },

      patch(id, body) {
        const t = store.get(id);
        if (!t) fail(404, '任务不存在');
        if (body.version !== t.version) fail(409, '任务 ' + id + ' 的 version 不匹配');
        Object.entries(body).forEach(([k, v]) => { if (k !== 'version' && v !== undefined) t[k] = v; });
        return { task: bump(t), warnings: [] };
      },

      transition(id, action, version) {
        const t = store.get(id);
        if (!t) fail(404, '任务不存在');
        if (action === 'start' && t.status !== 'TODO' && t.status !== 'DOING') {
          fail(422, '只有 TODO 状态可以开始；当前是 ' + t.status);
        }
        if (action === 'cancel' && t.status === 'DONE') {
          fail(422, '已完成的任务不能取消。若要撤销请先 reopen 再取消');
        }
        if (action !== 'complete' && action !== 'start' && action !== 'reopen' && version !== t.version) {
          fail(409, 'version 不匹配');
        }
        t.status = { start: 'DOING', complete: 'DONE', reopen: 'TODO', cancel: 'CANCELLED' }[action];
        return bump(t);
      },

      deleteToken(id) {
        const t = store.get(id);
        if (!t) fail(404, '任务不存在');
        return { deleted: false, confirmToken: 'ct_demo_' + id, expiresInSeconds: 300, snapshot: clone(t) };
      },

      deleteWithToken(id) {
        store.delete(id);
        deps.delete(id);
        return null;
      },

      history(id) { return ensureHistory(id); },

      dependencies(id) { return (deps.get(id) || []).slice(); },

      addDependency(id, dep) {
        if (id === dep) fail(422, '任务不能依赖自己');
        const arr = deps.get(id) || [];
        if (!arr.includes(dep)) arr.push(dep);
        deps.set(id, arr);
        return arr.slice();
      },

      removeDependency(id, dep) {
        deps.set(id, (deps.get(id) || []).filter((x) => x !== dep));
        return null;
      },

      batch(ops) {
        ops.forEach((op) => {
          const t = store.get(op.id);
          if (!t) fail(404, '任务不存在: ' + op.id);
          if (t.version !== op.version) fail(409, '批量更新中有条目版本冲突，整批已回滚');
        });
        ops.forEach((op) => { Object.assign(store.get(op.id), op.patch || {}); bump(store.get(op.id)); });
        return { applied: ops.length, ids: ops.map((o) => o.id) };
      },

      busy(fromIso, toIsoStr) {
        const f = new Date(fromIso).getTime();
        const t = new Date(toIsoStr).getTime();
        const windows = all()
          .filter((x) => x.startAt && x.estimateMin && x.status !== 'DONE' && x.status !== 'CANCELLED')
          .map((x) => {
            const s = Math.max(new Date(x.startAt).getTime(), f);
            const e = Math.min(new Date(x.startAt).getTime() + x.estimateMin * 60000, t);
            return e > s
              ? { start: toIso(new Date(s)), end: toIso(new Date(e)), minutes: Math.round((e - s) / 60000) }
              : null;
          })
          .filter(Boolean);
        return { windows };
      },

      occurrences(id, fromIso, toIsoStr) {
        const t = store.get(id);
        if (!t) fail(404, '任务不存在');
        const anchor = t.startAt || t.dueAt;
        const inRange = anchor
          && new Date(anchor) >= new Date(fromIso)
          && new Date(anchor) <= new Date(toIsoStr);
        return { taskId: id, rrule: t.rrule || null, from: fromIso, to: toIsoStr,
          occurrences: inRange ? [anchor] : [], truncated: false };
      }
    };
  }

  /* ------------------------------------------------------------------
     真实接口
     ------------------------------------------------------------------ */

  /* 基础地址：
     - 页面由 Core 托管时（/ui/...），走同源，避免任何跨域
     - 单独打开文件时，默认指向本机后端，可用 ?api=http://host:port 覆盖
     - 也可以在控制台执行  cadence.setBaseUrl('http://192.168.1.5:18080')
  */
  const DEFAULT_BASE = query.get('api')
    || (global.location.origin && global.location.origin.startsWith('http')
          ? global.location.origin
          : 'http://localhost:18080');

  let baseUrl = DEFAULT_BASE.replace(/\/+$/, '');

  /**
   * 调度器：演示模式走内存实现，否则走真实 HTTP。
   *
   * 签名是 http(演示方法名, 演示参数, 真实请求参数)：
   *   - 演示模式：调用 demo[method](...demoArgs)
   *   - 真实模式：调用 fetchJson(httpMethod, path, opts)
   * 演示实现同步抛的错必须包成 rejected promise —— 否则会跳出调用方的
   * try/catch 时序，让"失败处理"落到和真实请求不同的位置。
   */
  function http(demoMethod, demoArgs = [], request = null) {
    const d = demo();
    if (d) {
      try {
        return Promise.resolve(d[demoMethod](...demoArgs));
      } catch (e) {
        return Promise.reject(e);
      }
    }
    const [httpMethod, path, opts] = request;
    return fetchJson(httpMethod, path, opts || {});
  }

  /**
   * 底层请求（解包 {code,message,data}，失败抛 ApiError）
   * @returns {Promise<any>} data（204 时为 null）
   */
  async function fetchJson(method, path, opts = {}) {
    const headers = Object.assign({
      'X-Actor': 'web',
      'X-Trace-Id': `web_${Date.now().toString(36)}`
    }, opts.headers || {});

    const init = { method, headers };
    if (opts.body !== undefined && opts.body !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    let res;
    try {
      res = await fetch(baseUrl + path, init);
    } catch (e) {
      throw new ApiError(0, 0, '无法连接后端，请确认 cadence-core 已启动', { cause: String(e) });
    }

    if (res.status === 204) return null;

    const text = await res.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = null; }
    }

    const code = payload && typeof payload.code === 'number' ? payload.code : res.status;
    const message = payload && payload.message ? payload.message : '';
    if (!(res.ok && code === 0)) {
      throw new ApiError(res.status, code, message || defaultMessage(res.status), payload);
    }
    return payload ? payload.data : null;
  }

  function defaultMessage(status) {
    switch (status) {
      case 400: return '请求参数不合法';
      case 403: return '需要确认';
      case 404: return '目标不存在';
      case 405: return '请求方法不允许';
      case 409: return '数据已被修改，请刷新后重试';
      case 422: return '当前状态不允许该操作';
      case 500: return '服务内部错误';
      default: return `请求失败（HTTP ${status}）`;
    }
  }

  const api = {
    baseUrl: () => baseUrl,
    isDemo: () => DEMO_FLAG,
    setBaseUrl: (url) => { baseUrl = String(url).replace(/\/+$/, ''); },
    ApiError,
    idemKey,
    toIso,
    localToIso,
    isoToLocalInput,
    compact,

    /* ------------------------------------------------ 任务 */
    listTasks(params = {}) {
      const qs = new URLSearchParams();
      Object.entries(compact(params)).forEach(([k, v]) => qs.set(k, v));
      const s = qs.toString();
      return http('list', [compact(params)], ['GET', `/api/v1/tasks${s ? '?' + s : ''}`]);
    },

    getTask(id) {
      return http('get', [id], ['GET', `/api/v1/tasks/${encodeURIComponent(id)}`]);
    },

    createTask(body, opts = {}) {
      return http('create', [body], [
        'POST',
        '/api/v1/tasks',
        {
          body: compact(body),
          headers: { 'X-Idempotency-Key': opts.idemKey || idemKey() }
        }
      ]);
    },

    patchTask(id, body) {
      return http('patch', [id, body], ['PATCH', `/api/v1/tasks/${encodeURIComponent(id)}`, { body: compact(body) }]);
    },

    /**
     * 删除第一步：不带令牌。403 时返回 data 里的 confirmToken。
     * 这一步两种模式都走同一段逻辑，避免演示数据在 403 时被误判为失败。
     */
    async requestDeleteToken(id) {
      if (demo()) return demo().deleteToken(id);
      try {
        await fetchJson('DELETE', `/api/v1/tasks/${encodeURIComponent(id)}`);
        return { deleted: true };
      } catch (e) {
        if (e.status === 403 && e.payload && e.payload.data && e.payload.data.confirmToken) {
          return Object.assign({ deleted: false }, e.payload.data);
        }
        throw e;
      }
    },

    /** 删除第二步：带确认令牌 */
    deleteTaskWithToken(id, token) {
      return http('deleteWithToken', [id, token], [
        'DELETE',
        `/api/v1/tasks/${encodeURIComponent(id)}`,
        { headers: { 'X-Confirm-Token': token } }
      ]);
    },

    transition(id, action, version) {
      return http('transition', [id, action, version], [
        'POST',
        `/api/v1/tasks/${encodeURIComponent(id)}:${action}`,
        { body: { version } }
      ]);
    },

    history(id) {
      return http('history', [id], ['GET', `/api/v1/tasks/${encodeURIComponent(id)}/history`]);
    },

    /* ------------------------------------------------ 依赖 */
    dependencies(id) {
      return http('dependencies', [id], ['GET', `/api/v1/tasks/${encodeURIComponent(id)}/dependencies`]);
    },

    addDependency(id, dependsOnId) {
      return http('addDependency', [id, dependsOnId], [
        'POST',
        `/api/v1/tasks/${encodeURIComponent(id)}/dependencies`,
        { body: { dependsOnId } }
      ]);
    },

    removeDependency(id, dependsOnId) {
      return http('removeDependency', [id, dependsOnId], [
        'DELETE',
        `/api/v1/tasks/${encodeURIComponent(id)}/dependencies/${encodeURIComponent(dependsOnId)}`
      ]);
    },

    /* ------------------------------------------------ 批量 */
    batch(ops) {
      return http('batch', [ops], ['POST', '/api/v1/tasks:batch', { body: { ops } }]);
    },

    /* ------------------------------------------------ 日历 */
    busy(fromIso, toIsoStr) {
      const qs = new URLSearchParams({ from: fromIso, to: toIsoStr });
      return http('busy', [fromIso, toIsoStr], ['GET', `/api/v1/calendar/busy?${qs}`]);
    },

    occurrences(id, fromIso, toIsoStr, cap = 200) {
      const qs = new URLSearchParams({ from: fromIso, to: toIsoStr, cap });
      return http('occurrences', [id, fromIso, toIsoStr], [
        'GET',
        `/api/v1/tasks/${encodeURIComponent(id)}/occurrences?${qs}`
      ]);
    }
  };

  global.cadence = api;
})(window);
