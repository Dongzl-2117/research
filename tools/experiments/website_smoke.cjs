/* 主站挂载和站内模型界面验收：仅回环 HTTP、合成认证/模型；用请求头模拟 HTTPS 上游。 */
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
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'algorithm-site-browser-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`, origin = `https://127.0.0.1:${port}`;
  const server = spawn(process.env.EXPERIMENT_PYTHON || 'python', ['-m', 'uvicorn',
    'tools.experiments.website_fixture:app', '--host', '127.0.0.1', '--port', String(port), '--no-proxy-headers'],
    {cwd:root, env:{...process.env, PYTHONPATH:path.join(root,'src'), EXPERIMENT_SMOKE_DIR:dir,
      EXPERIMENT_SYNTHETIC_ORIGIN:origin}, stdio:['ignore','pipe','pipe']});
  let logs = '', browser;
  server.stderr.on('data', chunk => logs += chunk);
  try {
    let ready = false;
    for (let i=0; i<100; i++) {
      if(server.exitCode !== null) throw Error(logs);
      try {ready=(await fetch(url+'/synthetic-call-count')).ok;} catch {}
      if(ready) break; await pause(100);
    }
    assert(ready, logs);
    browser = await chromium.launch({executablePath:process.env.EXPERIMENT_CHROMIUM || '/usr/bin/chromium',headless:true});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [], external = [], failed = [], consoleErrors = [], requests = [];
    page.on('console', message => {if(message.type()==='error') consoleErrors.push(message.text());});
    page.on('request', request => requests.push(request.url()));
    page.setDefaultTimeout(10000);
    page.on('response', response => {if(response.status()>=400) failed.push([response.url(),response.status()]);});
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      if(!route.request().url().startsWith(url)){external.push(route.request().url());return route.abort();}
      if(route.request().method()==='POST') {
        const response=await route.fetch({headers:{...route.request().headers(),origin}});
        return route.fulfill({response});
      }
      return route.continue();
    });
    const count = async () => (await (await fetch(url+'/synthetic-call-count')).json()).calls;
    await page.goto(url+'/fixture-page');
    assert.equal(await page.locator('a[href="/admin/algorithms/experiments"]').count(),0);
    assert.equal((await page.goto(url+'/admin/algorithms/experiments')).status(),403);
    await page.setExtraHTTPHeaders({'x-test-session':'synthetic-browser-only'});
    await page.goto(url+'/fixture-page');
    await page.locator('a[href="/admin/algorithms/experiments"]').first().click();
    await page.locator('#questions input').first().waitFor().catch(async error => {
      console.error({url:page.url(),failed,external,consoleErrors,requests,head:await page.locator('head').innerHTML(),body:(await page.locator('body').innerText()).slice(0,1200)});throw error;
    });
    assert.equal(await count(),0);
    await page.locator('#compare').click();
    await page.waitForFunction(()=>document.querySelectorAll('.case').length===3);
    assert.equal(await count(),0);
    await page.getByRole('tab',{name:'问库回答'}).click();
    const first=page.locator('.case').first(), box=first.locator('.generation');
    assert((await box.textContent()).includes('实际费用未知，不等于免费'));
    assert((await box.textContent()).includes('每侧输出预算 1500'));
    assert((await box.textContent()).includes('每日最多 5 组'));
    assert(await box.getByRole('button').isDisabled());
    await box.locator('input').check(); await box.getByRole('button').click();
    await page.waitForFunction(()=>document.querySelector('.generation').textContent.includes('两侧答案已保存'));
    assert.equal(await count(),2);
    assert((await first.locator('.answer-text').first().textContent()).includes('合成替身回答'));
    await page.reload(); await page.locator('.case').first().waitFor();
    await page.getByRole('tab',{name:'问库回答'}).click();
    assert.equal(await count(),2);
    // 另一个参数方案：B 侧失败，保留 A、提示可能计费且不重试。
    await page.locator('#candidate-fields textarea').fill('SIMULATE-FAILURE');
    await page.locator('#compare').click();
    await page.waitForFunction(()=>document.querySelector('.generation input'));
    const failure=page.locator('.generation').first();
    await failure.locator('input').check(); await failure.getByRole('button').click();
    await page.waitForFunction(()=>document.querySelector('.generation').textContent.includes('可能已计费'));
    assert.equal(await count(),4);
    assert(await page.locator('.case').first().getByRole('button',{name:'A 更好',exact:true}).isDisabled());
    await page.reload(); await page.locator('.case').first().waitFor();
    await page.getByRole('tab',{name:'问库回答'}).click(); assert.equal(await count(),4);
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    console.log('网站浏览器验收通过：默认访客拒绝、所有者管理入口、挂载路径、双方案、费用/额度/预算、手动确认、成功与部分失败、刷新不重发、手机布局；真实模型调用0。');
  } finally {
    if(browser) await browser.close();
    if(server.exitCode===null){const done=new Promise(resolve=>server.once('exit',resolve));server.kill('SIGTERM');await done;}
    await fs.rm(dir,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
