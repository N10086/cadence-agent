/* L5 手工验收脚本（无 Playwright，直接走 Chrome DevTools Protocol）
 *
 * 覆盖：
 *   1) live 页面：发一句话 -> 工具卡片 -> done -> 打开 trace 时间线
 *   2) demo 页面：发“旧任务删掉吧” -> confirm diff 三组 -> 点确认 -> done
 *   3) 任务板仍有卡片（看板/筛选/批量未被对话区挤掉）
 *
 * 用法（先 docker compose up -d --build）：
 *   node services/cadence-web/l5_manual.js
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9341;
const OUT = path.join(__dirname, 'docs');
const LIVE = process.env.LIVE_PAGE || 'http://127.0.0.1:18080/ui/index.html';
const DEMO = process.env.DEMO_PAGE || 'http://127.0.0.1:18080/ui/index.html?demo';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${PORT}`,
    '--user-data-dir=' + path.join(OUT, '.p-l5'),
    '--window-size=1600,1100', 'about:blank'
  ], { stdio: 'ignore' });

  let endpoint;
  for (let i = 0; i < 60; i++) {
    try {
      const info = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
      endpoint = info.webSocketDebuggerUrl;
      break;
    } catch {
      await sleep(200);
    }
  }
  if (!endpoint) throw new Error('Chrome CDP 没有起来');

  const ws = new WebSocket(endpoint);
  await new Promise((r) => ws.addEventListener('open', r, { once: true }));
  let id = 0;
  const pending = new Map();
  const logs = [];
  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
      return;
    }
    if (msg.method === 'Runtime.exceptionThrown') {
      logs.push('EXC ' + (msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text));
    }
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      logs.push('ERR ' + msg.params.args.map((a) => a.value ?? a.description).join(' '));
    }
  });
  const send = (method, params = {}, sessionId) => new Promise((resolve) => {
    const i = ++id;
    pending.set(i, resolve);
    ws.send(JSON.stringify({ id: i, method, params, sessionId }));
  });

  const target = await send('Target.createTarget', { url: 'about:blank' });
  const attached = await send('Target.attachToTarget', { targetId: target.result.targetId, flatten: true });
  const sid = attached.result.sessionId;
  const S = (method, params) => send(method, params, sid);
  await S('Page.enable');
  await S('Runtime.enable');

  const evaluate = async (expression) => {
    const result = await S('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true
    });
    if (!result.result || !result.result.result) {
      throw new Error('evaluate failed: ' + JSON.stringify(result.error));
    }
    return result.result.result.value;
  };
  const shot = async (name) => {
    await S('Emulation.setDeviceMetricsOverride', {
      width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false
    });
    const result = await S('Page.captureScreenshot', { format: 'png' });
    if (result.result?.data) fs.writeFileSync(path.join(OUT, name), Buffer.from(result.result.data, 'base64'));
  };
  const check = (label, ok, extra = '') => {
    console.log((ok ? '  PASS ' : '  FAIL ') + label + (extra ? '  ' + extra : ''));
    if (!ok) failures.push(label);
  };
  const failures = [];
  const nav = async (url, wait = 1600) => {
    await S('Page.navigate', { url });
    await sleep(wait);
  };
  const waitFor = async (expression, timeout = 20000) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      if (await evaluate(expression)) return true;
      await sleep(400);
    }
    return false;
  };

  console.log('== live 页面 ==', LIVE);
  await nav(LIVE, 2000);
  const initial = JSON.parse(await evaluate(`JSON.stringify({
    demo: !!CadenceApp.state.demo,
    cards: document.querySelectorAll('.task').length,
    chat: !!document.querySelector('#chat'),
    composer: !!document.querySelector('#composer')
  })`));
  console.log('初始:', JSON.stringify(initial));
  check('live 页面不是 demo', initial.demo === false);
  check('任务板有卡片', initial.cards > 0, `${initial.cards} 张`);
  check('对话区/输入框存在', initial.chat && initial.composer);

  await evaluate(`(function(){
    const input = document.querySelector('#message-input');
    input.value = '今天还有什么没做完？';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('#btn-send').click();
  })()`);
  await waitFor("document.querySelectorAll('.done-card').length > 0", 20000);
  await sleep(300);
  const liveChat = JSON.parse(await evaluate(`JSON.stringify({
    toolCards: document.querySelectorAll('.tool-card').length,
    okCards: document.querySelectorAll('.tool-card.ok').length,
    doneCards: document.querySelectorAll('.done-card').length,
    traceLink: !!document.querySelector('.trace-link'),
    status: document.querySelector('#chat-status').textContent
  })`));
  console.log('对话:', JSON.stringify(liveChat));
  check('出现工具卡片', liveChat.toolCards >= 1, `${liveChat.toolCards} 张`);
  check('工具结果成功', liveChat.okCards >= 1);
  check('出现 done 卡', liveChat.doneCards >= 1);
  check('done 带 trace 入口', liveChat.traceLink);
  await shot('l5-chat.png');

  await evaluate(`document.querySelector('.trace-link').click()`);
  await sleep(900);
  const trace = JSON.parse(await evaluate(`JSON.stringify({
    open: document.querySelector('#trace').open,
    steps: document.querySelectorAll('.trace-step').length,
    kinds: Array.from(document.querySelectorAll('.trace-kind')).map(n => n.textContent.trim())
  })`));
  console.log('trace:', JSON.stringify(trace));
  check('Trace 抽屉打开', trace.open === true);
  check('Trace 有步骤', trace.steps >= 1, `${trace.steps} 步`);
  check('Trace 含 LLM 与 TOOL', trace.kinds.includes('LLM') && trace.kinds.includes('TOOL'));
  await shot('l5-trace.png');
  await evaluate(`document.querySelector('#trace').close()`);

  console.log('== demo 页面 ==', DEMO);
  await nav(DEMO, 1600);
  const demoInit = JSON.parse(await evaluate(`JSON.stringify({
    demo: !!CadenceApp.state.demo,
    cards: document.querySelectorAll('.task').length
  })`));
  check('demo 页面是内存模式', demoInit.demo === true);
  check('demo 看板有卡片', demoInit.cards > 0, `${demoInit.cards} 张`);

  await evaluate(`CadenceChat.send('旧任务删掉吧')`);
  await sleep(1500);
  const confirm = JSON.parse(await evaluate(`JSON.stringify({
    confirm: !!document.querySelector('.confirm-card'),
    diffGroups: document.querySelectorAll('.confirm-card .diff-group').length,
    buttons: document.querySelectorAll('.confirm-actions button').length
  })`));
  console.log('confirm:', JSON.stringify(confirm));
  check('弹出 confirm 卡', confirm.confirm === true);
  check('diff 三组齐全', confirm.diffGroups === 3, `${confirm.diffGroups} 组`);
  check('确认/放弃按钮存在', confirm.buttons === 2);
  await evaluate(`document.querySelector('.confirm-actions .btn.danger').click()`);
  await sleep(1200);
  const demoDone = JSON.parse(await evaluate(`JSON.stringify({
    done: document.querySelectorAll('.done-card').length,
    cards: document.querySelectorAll('.task').length,
    system: document.querySelector('#chat-log').textContent.includes('演示模式：已确认')
  })`));
  console.log('demo done:', JSON.stringify(demoDone));
  check('确认后进入 done', demoDone.done >= 1);
  check('确认后有演示执行提示', demoDone.system === true);
  check('看板仍然可用', demoDone.cards > 0, `${demoDone.cards} 张`);
  await shot('l5-demo-confirm.png');

  console.log('控制台错误:', logs.length ? JSON.stringify(logs, null, 1) : 'none');
  if (logs.length) failures.push('console errors');
  chrome.kill();
  setTimeout(() => {
    process.exit(failures.length ? 1 : 0);
  }, 300);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
