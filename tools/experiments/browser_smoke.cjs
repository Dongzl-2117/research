/* 临时合成实验台验收，不连接用户库或任何模型。 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright-core');
const root = path.resolve(__dirname, '../..');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'algorithm-browser-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.EXPERIMENT_PYTHON || 'python', ['-m', 'uvicorn',
    'tools.experiments.browser_fixture:app', '--host', '127.0.0.1', '--port', String(port), '--no-proxy-headers',
  ], {cwd: root, env: {...process.env, PYTHONPATH: path.join(root, 'src'), EXPERIMENT_SMOKE_DIR: dir}, stdio:['ignore','pipe','pipe']});
  let logs = '', browser;
  server.stderr.on('data', chunk => logs += chunk);
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (server.exitCode !== null) throw Error(logs);
      try { ready = (await fetch(url + '/experiments')).ok; } catch {}
      if (ready) break;
      await pause(100);
    }
    assert(ready, logs);
    browser = await chromium.launch({executablePath: process.env.EXPERIMENT_CHROMIUM || '/usr/bin/chromium', headless:true});
    const page = await browser.newPage({viewport:{width:1440, height:1100}});
    const errors = [], external = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      if (!route.request().url().startsWith(url)) {external.push(route.request().url()); return route.abort();}
      return route.continue();
    });
    await page.goto(url + '/experiments');
    await page.locator('#questions input').first().waitFor();
    assert.equal(await page.locator('#questions input:checked').count(), 3);
    await page.locator('[data-preset="title"]').click();
    assert.equal(await page.locator('#candidate-fields [data-key="title_tf"]').inputValue(), '1');
    assert.equal(await page.locator('#baseline-fields [data-key="title_tf"]').inputValue(), '0.5');
    await page.locator('#compare').click();
    await page.waitForFunction(() => document.querySelectorAll('.case').length === 3);
    const firstRun = new URL(page.url()).hash;
    assert(await page.locator('#status').textContent(), '检索对照已保存');
    // 双击仅一个评价事件；修改、撤销保留最新修订。
    const first = page.locator('.case').first();
    await first.getByRole('button', {name:'B 更好', exact:true}).evaluate(button => {button.click(); button.click();});
    await page.waitForFunction(() => document.querySelector('.rating').textContent.includes('修订 1'));
    const exportState = () => page.evaluate(async () => {
      const key = location.hash.slice(5);
      return (await fetch('/api/experiments/runs/' + key)).json();
    });
    assert.equal((await exportState()).rating_events.length, 1);
    await first.getByRole('button', {name:'相当', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('.rating').textContent.includes('修订 2'));
    await first.getByRole('button', {name:'撤销评价', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('.rating').textContent.includes('修订 3'));
    assert.equal((await exportState()).summary.search.directional_n, 0);
    await page.locator('.source-link').first().click();
    await page.locator('#source-dialog').waitFor({state:'visible'});
    assert((await page.locator('#source-text').textContent()).includes('<img'));
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => window.compromised), undefined);
    await page.reload(); await page.locator('.case').first().waitFor();
    assert.equal(new URL(page.url()).hash, firstRun);
    assert((await page.locator('.rating').first().textContent()).includes('修订 3'));
    await page.getByRole('tab', {name:'关键词搜索'}).focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.getByRole('tab', {name:'问库检索'}).getAttribute('aria-selected'), 'true');
    await page.getByRole('tab', {name:'问库回答'}).click();
    const gen = page.locator('.generation').first();
    assert(await gen.getByRole('button').isDisabled());
    await gen.locator('input').check(); await gen.getByRole('button').click();
    await page.waitForFunction(() => document.querySelector('.generation').textContent.includes('两侧答案已保存'));
    assert.equal((await exportState()).generations['demo-1'].attempted_sides.length, 2);
    assert((await page.locator('.answer-text').first().textContent()).includes('合成模型替身'));
    assert.equal(await page.evaluate(() => window.compromised), undefined);
    await page.locator('.case').first().getByRole('button', {name:'A 更好', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('.rating').textContent.includes('修订 1'));
    const downloadWait = page.waitForEvent('download'); await page.locator('#export-results').click();
    const download = await downloadWait;
    assert.equal(download.suggestedFilename(), 'algorithm-comparison.json');
    // 桌面与手机结果均无整页横向溢出。
    await page.getByRole('tab', {name:'关键词搜索'}).click();
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({width, height:900});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), String(width));
      if (process.env.EXPERIMENT_SCREENSHOT_DIR && [1440,390].includes(width)) {
        await fs.mkdir(process.env.EXPERIMENT_SCREENSHOT_DIR, {recursive:true});
        await page.screenshot({path:path.join(process.env.EXPERIMENT_SCREENSHOT_DIR, `experiments-${width}.png`),fullPage:true});
      }
    }
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log('浏览器验收通过：三题双方案、参数隔离、评价连点/改判/撤销、原文、刷新恢复、键盘标签、显式替身生成、回答评价、导出、HTML安全、1440/1024/390布局；真实模型调用0。');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const stopped = new Promise(resolve => server.once('exit', resolve)); server.kill('SIGTERM'); await stopped;
    }
    await fs.rm(dir, {recursive:true, force:true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
