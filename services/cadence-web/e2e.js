/* 对真实后端做一遍页面级 e2e（不使用 ?demo），并收集浏览器控制台错误 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9340;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const OUT = path.join(__dirname, 'docs');
const PAGE = process.env.PAGE || 'http://localhost:18080/ui/index.html';

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${PORT}`,
    '--user-data-dir=' + path.join(OUT, '.p-e2e'),
    '--window-size=1440,1000', 'about:blank'
  ], { stdio: 'ignore' });
  let ep;
  for (let i = 0; i < 60; i++) {
    try { ep = (await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json()).webSocketDebuggerUrl; break; } catch { await sleep(200); }
  }
  const ws = new WebSocket(ep);
  await new Promise((r) => ws.addEventListener('open', r, { once: true }));
  let id = 0;
  const pend = new Map();
  const logs = [];
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      logs.push('EXC ' + (m.params.exceptionDetails.exception && m.params.exceptionDetails.exception.description || m.params.exceptionDetails.text));
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      logs.push('ERR ' + m.params.args.map((a) => a.value ?? a.description).join(' '));
    }
  });
  const send = (method, params = {}, sessionId) => new Promise((r) => {
    const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId }));
  });
  const t = await send('Target.createTarget', { url: 'about:blank' });
  const a = await send('Target.attachToTarget', { targetId: t.result.targetId, flatten: true });
  const sid = a.result.sessionId;
  const S = (m, p) => send(m, p, sid);
  await S('Page.enable');
  await S('Runtime.enable');
  const probe = async (expr) => {
    const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (!r.result || !r.result.result) {
      throw new Error('evaluate failed: ' + JSON.stringify(r.error));
    }
    return r.result.result.value;
  };
  const shot = async (name) => {
    await S('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    const r = await S('Page.captureScreenshot', { format: 'png' });
    if (!r.result || !r.result.data) { console.log('  (screenshot failed: ' + JSON.stringify(r.error) + ')'); return; }
    fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, 'base64'));
    console.log('  shot', name);
  };
  const check = (label, ok, extra) => console.log((ok ? '  PASS ' : '  FAIL ') + label + (extra ? '  ' + extra : ''));

  console.log('打开真实后端页面:', PAGE);
  await S('Page.navigate', { url: PAGE });
  await sleep(2000);

  const initial = await probe(`JSON.stringify({
    demo: CadenceApp.state.demo ? true : false,
    cards: document.querySelectorAll('.task').length,
    meta: document.querySelector('#list-meta').textContent,
    conn: Array.from(document.querySelectorAll('#conn-list li')).map(n => n.textContent.replace(/\\s+/g,' ').trim()),
    busy: document.querySelectorAll('.busy-item').length,
    banner: !document.querySelector('#banner').classList.contains('hidden')
  })`);
  console.log('初始状态:', initial);
  const st0 = JSON.parse(initial);
  check('不处于演示模式', st0.demo === false);
  check('看板渲染出任务卡片', st0.cards > 0, `${st0.cards} 张`);
  check('服务状态面板显示 UP', st0.conn.some((x) => x.includes('UP')), JSON.stringify(st0.conn));
  check('没有连接错误横幅', st0.banner === false);

  await shot('live-board.png');

  // 新建任务 → 出现在看板
  const title = '前端自测-' + new Date().toISOString().slice(11, 19);
  await probe(`CadenceApp.openTaskForm(null)`);
  await sleep(300);
  await probe(`(function(){ const f=document.querySelector('#task-form'); f.title.value=${JSON.stringify(title)}; f.priority.value='HIGH'; f.estimateMin.value=45; f.tags.value='e2e'; })()`);
  await probe(`document.querySelector('#task-form').requestSubmit()`);
  await sleep(1200);
  const afterCreate = await probe(`JSON.stringify({
    found: Array.from(document.querySelectorAll('.task h4')).some(h => h.textContent === ${JSON.stringify(title)}),
    dialogOpen: document.querySelector('#task-dialog').open,
    toast: document.querySelector('#toasts').textContent.includes('已创建')
  })`);
  console.log('新建后:', afterCreate);
  const ac = JSON.parse(afterCreate);
  check('新任务出现在看板', ac.found);
  check('新建弹窗已关闭', ac.dialogOpen === false);
  check('出现「已创建」提示', ac.toast);

  // 打开详情 → 抽屉可见
  const newId = await probe(`(function(){ const n = Array.from(document.querySelectorAll('.task')).find(n => n.querySelector('h4').textContent === ${JSON.stringify(title)}); return n ? n.dataset.id : null; })()`);
  await probe(`CadenceApp.openDetail(${JSON.stringify(newId)})`);
  await sleep(800);
  const detail = await probe(`JSON.stringify({open: document.querySelector('#detail-dialog').open, title: document.querySelector('#detail-title').textContent, version: document.querySelector('#detail-version').textContent})`);
  console.log('详情:', detail);
  const dt = JSON.parse(detail);
  check('详情抽屉已打开', dt.open === true);
  check('详情标题正确', dt.title === title, dt.title);

  // 历史标签
  await probe(`document.querySelector('.tabs .tab[data-tab="history"]').click()`);
  await sleep(800);
  const hist = await probe(`document.querySelectorAll('#history-list li').length`);
  check('历史标签有条目', hist >= 1, `${hist} 条`);

  // 重复实例标签（真实接口）
  await probe(`document.querySelector('.tabs .tab[data-tab="occur"]').click()`);
  await probe(`document.querySelector('#btn-occur').click()`);
  await sleep(900);
  const occ = await probe(`document.querySelector('#occur-result').textContent.slice(0, 60)`);
  console.log('重复实例:', occ);
  check('重复实例查询返回内容', !!occ && !occ.includes('失败'));
  await shot('live-detail.png');
  await probe(`document.querySelector('#detail-dialog').close()`);

  // 开始 → 完成 两个状态按钮
  await probe(`(function(){ const n=document.querySelector('.task[data-id="${newId}"]'); Array.from(n.querySelectorAll('button')).find(b=>b.textContent.includes('开始')).click(); })()`);
  await sleep(1000);
  const doing = await probe(`document.querySelectorAll('.column')[1].querySelectorAll('.task').length`);
  check('「开始」后进入进行中列', doing > 0, `${doing} 张`);

  // 两步删除
  await probe(`(function(){ const n=document.querySelector('.task[data-id="${newId}"]'); Array.from(n.querySelectorAll('button')).find(b=>b.textContent.includes('删除')).click(); })()`);
  await sleep(900);
  const confirmOpen = await probe(`JSON.stringify({open: document.querySelector('#confirm-dialog').open, token: (document.querySelector('.token-box')||{}).textContent})`);
  console.log('删除确认:', confirmOpen);
  const cf = JSON.parse(confirmOpen);
  check('两步删除弹出确认框', cf.open === true);
  check('确认框展示了确认令牌', typeof cf.token === 'string' && cf.token.startsWith('ct_'));
  await shot('live-delete-confirm.png');
  await probe(`document.querySelector('#confirm-ok').click()`);
  await sleep(1200);
  const gone = await probe(`!Array.from(document.querySelectorAll('.task h4')).some(h => h.textContent === ${JSON.stringify(title)})`);
  check('确认后任务从看板消失', gone === true);

  // 忙闲查询
  await probe(`document.querySelector('#btn-busy').click()`);
  await sleep(900);
  const busy = await probe(`document.querySelector('#busy-list').textContent.slice(0,80)`);
  console.log('忙闲:', busy);

  // 刷新 + 筛选
  await probe(`document.querySelector('#f-priority').value='URGENT'; document.querySelector('#f-priority').dispatchEvent(new Event('change'))`);
  await sleep(900);
  const urgent = await probe(`document.querySelectorAll('.task').length`);
  console.log('筛选 URGENT 后卡片数:', urgent);

  console.log('控制台错误:', logs.length ? JSON.stringify(logs, null, 1) : 'none');
  chrome.kill();
  setTimeout(() => process.exit(0), 300);
})().catch((e) => { console.error(e); process.exit(1); });
