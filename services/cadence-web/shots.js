/* =========================================================================
   页面截图脚本（Node 18+，零依赖）
   用 Chrome DevTools Protocol 打开页面、执行动作、截图。

   用途：
     1. 给 README 出图
     2. 快速发现页面上的 JS 报错（--errors 只看错误）

   用法（先让后端跑起来，见 README）：
     node shots.js                              # 演示模式：看板 / 新建 / 详情 / 历史 / 删除确认
     PAGE=http://localhost:18080/ui/index.html node shots.js
                                                # 换成联真实后端（会往库里写一条任务再删掉）
   ========================================================================= */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CHROME = process.env.CHROME_PATH
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PAGE = process.env.PAGE || 'http://localhost:18080/ui/index.html?demo';
const OUT = path.join(__dirname, 'docs');
const PORT = 9333;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForEndpoint() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      return (await res.json()).webSocketDebuggerUrl;
    } catch { await sleep(200); }
  }
  throw new Error('Chrome 调试端口未就绪');
}

/** 极简 CDP 客户端 */
function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let seq = 0;
  const pending = new Map();
  const onMessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve } = pending.get(msg.id);
      pending.delete(msg.id);
      resolve(msg);
    }
  };
  ws.addEventListener('message', onMessage);
  return {
    ws,
    opened() { return new Promise((r) => ws.addEventListener('open', r, { once: true })); },
    send(method, params = {}, sessionId) {
      const id = ++seq;
      return new Promise((resolve) => {
        pending.set(id, { resolve });
        ws.send(JSON.stringify({ id, method, params, sessionId }));
      });
    }
  };
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const profile = path.join(OUT, '.chrome-profile');
  fs.rmSync(profile, { recursive: true, force: true });

  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    '--window-size=1440,1200', 'about:blank'
  ], { stdio: 'ignore' });

  try {
    const c = connect(await waitForEndpoint());
    await c.opened();

    const target = await c.send('Target.createTarget', { url: 'about:blank' });
    const attached = await c.send('Target.attachToTarget', { targetId: target.result.targetId, flatten: true });
    const sid = attached.result.sessionId;
    const S = (m, p) => c.send(m, p, sid);

    const errors = [];
    c.ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails;
        errors.push(d.text + ' ' + ((d.exception && d.exception.description) || ''));
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        errors.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
      }
    });

    await S('Page.enable');
    await S('Runtime.enable');
    await S('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1200, deviceScaleFactor: 1, mobile: false });

    const evaluate = async (expression) => {
      const r = await S('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (!r.result || !r.result.result) throw new Error('evaluate failed: ' + JSON.stringify(r.error));
      return r.result.result.value;
    };
    const shot = async (name) => {
      const r = await S('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, fromSurface: true });
      if (!r.result || !r.result.data) {
        console.log('skip', name, JSON.stringify(r.error));
        return;
      }
      fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, 'base64'));
      console.log('saved', name);
    };
    const open = async () => {
      await S('Page.navigate', { url: PAGE });
      await sleep(1800);
    };

    if (process.argv.includes('--errors')) {
      await open();
      console.log('console errors:', errors.length ? JSON.stringify(errors, null, 1) : 'none');
    } else {
      await open();
      await shot('demo-board.png');

      // 新建任务弹窗
      await evaluate("document.querySelector('#btn-new').click()");
      await sleep(400);
      await shot('demo-new-task.png');
      await evaluate(`
        (function () {
          const f = document.querySelector('#task-form');
          f.title.value = '整理面试题库';
          f.priority.value = 'HIGH';
          f.estimateMin.value = 90;
          f.startAt.value = '${new Date(Date.now() + 86400000).toISOString().slice(0, 10)}T20:00';
          f.tags.value = 'career, review';
          f.description.value = '把高频八股整理成一页，面试前一晚过一遍。';
        })()
      `);
      await sleep(250);
      await shot('demo-new-task-filled.png');
      await evaluate("document.querySelector('#task-dialog').close()");

      // 详情抽屉 + 历史
      await evaluate("CadenceApp.openDetail('d1')");
      await sleep(800);
      await shot('demo-detail.png');
      await evaluate("document.querySelector('.tabs .tab[data-tab=\"history\"]').click()");
      await sleep(700);
      await shot('demo-history.png');
      await evaluate("document.querySelector('#detail-dialog').close()");

      // 两步删除确认
      await evaluate(`
        (function () {
          const card = Array.from(document.querySelectorAll('.task')).find((n) => n.dataset.id === 'd2');
          Array.from(card.querySelectorAll('button')).find((b) => b.textContent.includes('删除')).click();
        })()
      `);
      await sleep(900);
      await shot('demo-delete-confirm.png');
      await evaluate("document.querySelector('#confirm-dialog').close()");

      console.log('console errors:', errors.length ? JSON.stringify(errors, null, 1) : 'none');
    }

    c.ws.close();
  } finally {
    chrome.kill();
    // Chrome 退出时会自己清理 profile；这里延迟删除，删不掉就忽略
    setTimeout(() => {
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* 无妨 */ }
      process.exit(0);
    }, 1500);
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
