// 合成页面与问库响应；外网一律拦截，不启动应用或调用模型。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright-core');

(async () => {
  const root = path.resolve(__dirname, '..');
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'research-sidebar-ask-'));
  const generated = spawnSync(process.env.ARTICLE_PYTHON || 'python',
    ['tools/article_layout_fixture.py', directory], {cwd: root, env: process.env, encoding: 'utf8'});
  assert.equal(generated.status, 0, generated.stderr);
  const script = process.env.SIDEBAR_JS_REF
    ? spawnSync('git', ['show', `${process.env.SIDEBAR_JS_REF}:src/researchlib/web/static/article-chat.js`],
      {cwd: root, encoding: 'utf8'})
    : {status: 0, stdout: await fs.readFile(path.join(root, 'src/researchlib/web/static/article-chat.js'), 'utf8')};
  assert.equal(script.status, 0, script.stderr);
  await fs.writeFile(path.join(directory, 'static/article-chat.js'), script.stdout);
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (req.headers['x-test-hang-body'] === '1' && url.pathname.startsWith('/api/v1/answers/')) {
      res.writeHead(200, {'content-type': 'application/json'});
      res.write('{"status":');
      setTimeout(() => { if (!res.destroyed) res.end('null}'); }, 600);
      return;
    }
    const target = path.resolve(directory, '.' + decodeURIComponent(url.pathname));
    if (!target.startsWith(directory + path.sep)) { res.writeHead(403).end(); return; }
    try {
      res.setHeader('content-type', ({'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
        '.svg': 'image/svg+xml'}[path.extname(target)] || 'application/octet-stream') + '; charset=utf-8');
      res.end(await fs.readFile(target));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({executablePath: process.env.ARTICLE_CHROMIUM || undefined, headless: true});
  const evidence = [{n: 1000, article_id: 9, ord: 0, title: '合成来源',
    text: '合成原文证据，数字是10。', url: '/articles/9?chunk=0', cited: true}];
  const done = {id: 7, status: 'done', answer: '合成回答[1000]。', citations: evidence,
    checks: [{sentence: '<script>window.injection=1</script>', problems: ['数字99不在原文']}],
    stats: {batches: 4, failed_batches: [2]}};
  const results = [];

  async function casePage(options = {}) {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    page.setDefaultTimeout(2500);
    page.setDefaultNavigationTimeout(10000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    // 加快轮询与请求截止时间；不改变原脚本的次数上限和状态转移。
    await page.addInitScript(() => {
      const native = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => native(fn,
        delay >= 10000 ? 100 : (delay >= 800 && delay <= 5000 ? 1 : delay), ...args);
    });
    const state = {posts: [], gets: 0, retries: 0, mode: options.mode || 'done'};
    if (state.mode === 'body-timeout') await page.setExtraHTTPHeaders({'x-test-hang-body': '1'});
    await page.route('**/*', async route => {
      const request = route.request();
      if (!request.url().startsWith(origin + '/')) { await route.abort(); return; }
      const pathname = new URL(request.url()).pathname;
      const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
      if (pathname === '/api/v1/ask') {
        state.posts.push(request.postDataJSON());
        if (state.mode === 'post-failure') { await route.abort('failed'); return; }
        if (state.mode === 'raw') {
          await json({id: null, status: '', answer: '', error: '没有模型凭证，只给出召回原文', citations: evidence});
          return;
        }
        await json(state.mode === 'done' ? done : {id: 7, status: 'running', citations: evidence});
        return;
      }
      if (pathname === '/ask/7/retry') {
        state.retries += 1;
        state.mode = 'done';
        await route.fulfill({status: 200, contentType: 'text/html', body: '<p>合成重试确认</p>'});
        return;
      }
      if (pathname === '/api/v1/answers/7') {
        state.gets += 1;
        if (state.mode === 'network') { await route.abort('failed'); return; }
        if (state.mode === 'server') { await json({error: {message: '合成读取故障'}}, 503); return; }
        if (state.mode === 'missing') { await json({error: {message: '合成404'}}, 404); return; }
        if (state.mode === 'invalid') { await route.fulfill({status: 200, body: '{'}); return; }
        if (state.mode === 'body-timeout') { await route.continue(); return; }
        if (state.mode === 'error') { await json({id: 7, status: 'error', error: '合成模型故障', citations: evidence}); return; }
        if (state.mode === 'running') { await json({id: 7, status: 'running'}); return; }
        if (state.mode === 'delayed') {
          await new Promise(resolve => setTimeout(resolve, 250));
          try { await json(done); } catch {}
          return;
        }
        await json(done); return;
      }
      await route.continue();
    });
    await page.goto(origin + '/complete.html');
    await page.waitForFunction(() => document.querySelector('.chat-toggle').getAttribute('aria-expanded') === 'true');
    return {page, state, errors};
  }

  async function send(page, text = '合成问题') {
    await page.locator('#article-chat-q').fill(text);
    await page.locator('.chat-form button[type="submit"]').click();
  }

  async function enabled(page) {
    await page.waitForFunction(() => !document.querySelector('.chat-form button[type="submit"]').disabled,
      null, {timeout: 3500});
  }

  async function check(name, options, run) {
    let fixture;
    try {
      fixture = await casePage(options);
      await run(fixture);
      assert.deepEqual(fixture.errors, []);
      results.push({name, passed: true});
    } catch (error) {
      results.push({name, passed: false, error: error.message.split('\n')[0]});
    } finally { if (fixture) await fixture.page.close(); }
  }

  try {
    await check('原问库请求、显式范围、独立轮次及核对提示', {}, async ({page, state}) => {
      assert.equal(await page.locator('#article-chat-scope').inputValue(), '');
      await send(page);
      await enabled(page);
      assert.equal(state.posts.length, 1);
      assert.equal(state.posts[0].article, undefined);
      assert.equal(state.posts[0].chat, undefined);
      assert.equal(state.posts[0].history, undefined);
      assert((await page.locator('.chat-warnings').innerText()).includes('数字99'));
      assert((await page.locator('.chat-partial').innerText()).includes('2'));
      assert.equal(await page.locator('.chat-content a.cite').getAttribute('href'), '/articles/9?chunk=0');
      assert.equal(await page.evaluate(() => window.injection), undefined);
      await page.locator('#article-chat-scope').selectOption('1');
      assert.equal(await page.locator('.chat-chip').count(), 0);
      await send(page, '另一轮独立问题'); await enabled(page);
      assert.equal(state.posts[1].article, '1');
      assert.equal(state.posts[1].history, undefined);
      assert.equal(await page.locator('.chat-bubble-user').count(), 2);
      assert.equal(await page.locator('.chat-hint').count(), 0);
      assert.equal(await page.locator('#article-chat-q').getAttribute('placeholder'), null);
      assert.equal(await page.locator('#article-chat-q').getAttribute('aria-describedby'), 'article-chat-mode');
      assert((await page.locator('#article-chat-mode').textContent()).includes('每轮独立提问'));
      assert(await page.locator('#article-chat-mode').evaluate(node => node.getBoundingClientRect().width <= 1));
      assert((await page.locator('#article-chat-scope option:checked').textContent()).includes('单轮'));
    });
    await check('输入法组合与Shift换行不误发送', {}, async ({page, state}) => {
      const input = page.locator('#article-chat-q');
      await input.fill('中文输入');
      await input.dispatchEvent('compositionstart');
      await input.dispatchEvent('keydown', {key: 'Enter', isComposing: true});
      await page.waitForTimeout(30); assert.equal(state.posts.length, 0);
      await input.dispatchEvent('compositionend');
      await input.dispatchEvent('keydown', {key: 'Enter', keyCode: 229});
      await input.dispatchEvent('keydown', {key: 'Enter', shiftKey: true});
      await page.waitForTimeout(30); assert.equal(state.posts.length, 0);
      await input.press('Enter'); await enabled(page); assert.equal(state.posts.length, 1);
    });
    await check('没有模型仍展示原文和问库链接', {mode: 'raw'}, async ({page}) => {
      await send(page); await enabled(page);
      await page.locator('.chat-evidence summary').click();
      assert((await page.locator('.chat-evidence').innerText()).includes('合成原文证据'));
      assert.equal(await page.locator('.chat-open-answer').getAttribute('href'), '/ask');
    });
    for (const mode of ['network', 'server', 'invalid', 'body-timeout']) {
      await check(`有限恢复：${mode}`, {mode}, async ({page, state}) => {
        await send(page); await enabled(page);
        assert(state.gets <= 3, `${state.gets} reads`);
        assert.equal(await page.locator('.chat-open-answer').getAttribute('href'), '/ask/7');
        assert.equal(await page.locator('.chat-resume').count(), 1);
        state.mode = 'done';
        await page.locator('.chat-resume').click(); await enabled(page);
        assert((await page.locator('.chat-content').innerText()).includes('合成回答'));
        assert.equal(state.posts.length, 1);
      });
    }
    await check('404立即停止并保留回答ID', {mode: 'missing'}, async ({page, state}) => {
      await send(page); await enabled(page);
      assert.equal(state.gets, 1);
      assert.equal(await page.locator('.chat-open-answer').getAttribute('href'), '/ask/7');
    });
    await check('一直running有截止并可继续读取原ID', {mode: 'running'}, async ({page, state}) => {
      await send(page); await enabled(page);
      assert(state.gets <= 300);
      assert.equal(await page.locator('.chat-resume').count(), 1);
      state.mode = 'done'; await page.locator('.chat-resume').click(); await enabled(page);
      assert.equal(state.posts.length, 1);
    });
    await check('错误重试调用原动作和原ID', {mode: 'error'}, async ({page, state}) => {
      await send(page); await enabled(page);
      await page.locator('.chat-retry').click(); await enabled(page);
      assert.equal(state.retries, 1); assert.equal(state.posts.length, 1);
      assert((await page.locator('.chat-content').innerText()).includes('合成回答'));
    });
    await check('提交结果未知时恢复入口且不自动重提', {mode: 'post-failure'}, async ({page, state}) => {
      await send(page); await enabled(page);
      assert.equal(state.posts.length, 1);
      assert.equal(await page.locator('.chat-open-answer').getAttribute('href'), '/ask');
    });
    await check('主线宽栏、拖动和键盘调宽持久保存且不改变问库范围', {}, async ({page, state}) => {
      await page.setViewportSize({width: 1920, height: 900});
      const split = page.locator('.chat-split');
      const panel = page.locator('.article-chat');
      const width = async () => (await panel.boundingBox()).width;
      const original = await width();
      assert(original > 320);
      assert(await split.isVisible());
      await split.focus(); await page.keyboard.press('ArrowLeft');
      assert(await width() > original + 10);
      const stored = await page.evaluate(() => localStorage.getItem('research-article-chat-ratio'));
      assert(Number(stored) > 0.12 && Number(stored) < 0.8);
      const adjusted = await width();
      await page.reload();
      assert(Math.abs(await width() - adjusted) <= 2);
      const bounds = await split.boundingBox();
      await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + 40);
      await page.mouse.down();
      await page.mouse.move(bounds.x - 70, bounds.y + 40, {steps: 4});
      await page.mouse.up();
      assert(await width() > adjusted + 20);
      await split.dblclick();
      assert.equal(await page.evaluate(() => localStorage.getItem('research-article-chat-ratio')), null);
      assert(Math.abs(await width() - original) <= 2);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await send(page); await enabled(page);
      assert.equal(state.posts[0].article, undefined);
      assert.equal(state.posts[0].chat, undefined);
      assert.equal(state.posts[0].history, undefined);
      await page.setViewportSize({width: 390, height: 844});
      assert.equal(await split.isVisible(), false);
      await page.locator('.chat-toggle').click();
      assert(await panel.isVisible());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    });
    await check('清空丢弃迟到响应并保留原侧栏布局', {mode: 'delayed'}, async ({page}) => {
      await send(page); await page.locator('[data-chat-clear]').click();
      await page.waitForTimeout(350);
      assert.equal(await page.locator('.chat-content').count(), 0);
      assert.equal(await page.locator('.chat-log').evaluate(node => node.childElementCount), 0);
      await page.screenshot({path: path.join(directory, 'sidebar-wide.png'), animations: 'disabled'});
      await page.locator('[data-chat-close]').click();
      await page.reload();
      assert.equal(await page.locator('.read-with-chat.is-chat-closed').count(), 1);
      await page.setViewportSize({width: 390, height: 844});
      await page.locator('.chat-toggle').click();
      assert(await page.locator('.article-chat.is-open').isVisible());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await page.screenshot({path: path.join(directory, 'sidebar-mobile.png'), animations: 'disabled'});
      assert(await page.locator('#article-chat-q').evaluate(input => input.getBoundingClientRect().bottom <= innerHeight));
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.article-chat.is-open').count(), 0);
    });
    console.log(JSON.stringify({directory, results}, null, 2));
    assert(results.every(result => result.passed), '浏览器验收有失败项');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
