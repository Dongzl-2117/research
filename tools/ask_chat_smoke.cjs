// 完整问库与侧栏的合成浏览器验收。只服务模板和替身响应，外网全部拦截。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright-core');

(async () => {
  const root = path.resolve(__dirname, '..');
  const directory = process.env.ASK_EVIDENCE_DIR || await fs.mkdtemp(path.join(os.tmpdir(), 'ask-chat-'));
  await fs.mkdir(directory, {recursive: true});
  for (const fixture of ['ask_layout_fixture.py', 'article_layout_fixture.py']) {
    const made = spawnSync(process.env.ARTICLE_PYTHON || 'python', ['tools/' + fixture, directory],
      {cwd: root, env: process.env, encoding: 'utf8'});
    assert.equal(made.status, 0, made.stderr);
  }
  const done = JSON.parse(await fs.readFile(path.join(directory, 'response.json'), 'utf8'));
  const server = http.createServer(async (req, res) => {
    let pathname = new URL(req.url, 'http://localhost').pathname;
    if (/^\/ask\/\d+$/.test(pathname)) pathname = '/ask-answer.html';
    if (pathname === '/ask') pathname = '/ask-empty.html';
    const target = path.resolve(directory, '.' + decodeURIComponent(pathname));
    if (!target.startsWith(directory + path.sep)) { res.writeHead(403).end(); return; }
    try {
      res.setHeader('content-type', ({'.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
        '.svg': 'image/svg+xml'}[path.extname(target)] || 'application/octet-stream') + '; charset=utf-8');
      res.end(await fs.readFile(target));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.ARTICLE_CHROMIUM || undefined});
  const results = [];

  async function fixture(options = {}) {
    const context = await browser.newContext({viewport: options.viewport || {width: 1440, height: 960},
      javaScriptEnabled: options.scripts !== false, colorScheme: options.dark ? 'dark' : 'light'});
    const page = await context.newPage();
    page.setDefaultTimeout(4000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const native = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => native(fn,
        delay >= 10000 ? 120 : (delay >= 800 && delay <= 5000 ? 10 : delay), ...args);
    });
    const state = {posts: [], gets: 0, retries: 0, mode: options.mode || 'done'};
    await page.route('**/*', async route => {
      const request = route.request();
      if (!request.url().startsWith(origin + '/')) { await route.abort(); return; }
      const pathname = new URL(request.url()).pathname;
      const json = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
      if (pathname === '/api/v1/ask') {
        state.posts.push(request.postDataJSON());
        if (state.mode === 'unknown') { await route.abort(); return; }
        await json(state.mode === 'done' ? done : {...done, answer: '', answer_html: '', status: 'running'});
        return;
      }
      if (pathname === '/api/v1/answers/7') {
        state.gets += 1;
        if (state.mode === 'network') { await json({detail: '合成503'}, 503); return; }
        if (state.mode === 'missing') { await json({detail: '合成404'}, 404); return; }
        if (state.mode === 'error') { await json({...done, status: 'error', answer: '', answer_html: '', error: '合成整合失败'}); return; }
        if (state.mode === 'hold') { await json({...done, status: 'running', answer: '', answer_html: ''}); return; }
        await json(done); return;
      }
      if (pathname === '/ask/7/retry') {
        state.retries += 1; state.mode = 'done';
        await route.fulfill({contentType: 'text/html', body: '<p>合成重试确认</p>'}); return;
      }
      await route.continue();
    });
    await page.goto(origin + '/' + (options.file || 'ask-empty.html'));
    return {page, context, errors, state};
  }

  async function send(page, text = '合成问题') {
    await page.locator('.chat-form textarea').fill(text);
    await page.locator('.chat-form button[type="submit"]').click();
  }
  async function finished(page) {
    await page.waitForFunction(() => !document.querySelector('.chat-form button[type="submit"]').disabled);
  }
  async function fits(page) {
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert(await page.locator('.chat-form').evaluate(node => node.getBoundingClientRect().bottom <= innerHeight + 1));
  }
  async function accessibility(page, name) {
    if (process.env.ASK_AXE !== '1') return;
    const {AxeBuilder} = require('@axe-core/playwright');
    const result = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
    await fs.writeFile(path.join(directory, `axe-${name}.json`), JSON.stringify(result, null, 2));
    assert.deepEqual(result.violations.map(item => ({id:item.id, nodes:item.nodes.map(node=>node.target)})), []);
  }
  async function check(name, options, run) {
    let view;
    try {
      view = await fixture(options);
      await run(view);
      assert.deepEqual(view.errors, []);
      results.push({name, passed: true});
    } catch (error) { results.push({name, passed: false, error: error.message}); }
    finally { if (view) await view.context.close(); }
  }

  try {
    await check('桌面空态、完整Markdown、两次独立提问和刷新地址', {}, async ({page, state}) => {
      assert((await page.locator('#ask-chat').ariaSnapshot()).includes('每次独立提问'));
      await page.screenshot({path: path.join(directory, '01-empty-desktop.png')});
      await accessibility(page, 'desktop-empty');
      await send(page); await finished(page);
      assert.equal(await page.locator('.ask-welcome').count(), 0);
      for (const tag of ['h2', 'ol', 'table', 'blockquote', 'pre']) assert(await page.locator('.chat-markdown ' + tag).count());
      assert.equal(await page.locator('.chat-markdown pre a').count(), 0);
      assert.equal(await page.locator('.chat-markdown a.cite').first().getAttribute('href'), '/articles/9?chunk=0');
      assert.equal(page.url(), origin + '/ask/7');
      assert.equal(state.posts[0].history, undefined);
      assert.equal(state.posts[0].wait, false);
      await fits(page);
      await page.screenshot({path: path.join(directory, '02-answer-desktop.png')});
      await accessibility(page, 'desktop-answer');
      await send(page, '另一条独立问题'); await finished(page);
      assert.equal(state.posts.length, 2);
      assert.equal(state.posts[1].history, undefined);
      assert.equal(await page.locator('.chat-bubble-user').count(), 2);
    });
    await check('范围快照保留作者日期来源公司文章目录，不传历史', {}, async ({page, state}) => {
      await page.locator('.ask-filters summary').click();
      await page.locator('[name="author"]').selectOption('合成作者甲');
      await page.locator('[name="from"]').fill('2026-10-01');
      await page.locator('[name="to"]').fill('2026-11-01');
      await page.locator('.chat-form').evaluate(form => {
        for (const [name, value] of Object.entries({source:'synthetic', company:'12', tree:'13'})) {
          const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; form.append(input);
        }
        form.elements.article.value = '14';
      });
      await send(page); await finished(page);
      assert.deepEqual(state.posts[0], {question:'合成问题', author:'合成作者甲', source:'synthetic', company:'12',
        from:'2026-10-01', to:'2026-11-01', article:'14', tree:'13', wait:false});
      assert((await page.locator('.chat-scope').innerText()).includes('2026-10-01'));
    });
    await check('示例库提示不挤走桌面和手机的输入区', {file:'ask-sample.html'}, async ({page}) => {
      assert((await page.locator('.banner').innerText()).includes('示例数据'));
      await fits(page);
      await page.setViewportSize({width:390,height:844}); await fits(page);
    });
    await check('历史、引用和原文反馈可用键盘到达', {file:'ask-answer.html'}, async ({page}) => {
      await page.locator('.ask-history summary').focus(); await page.keyboard.press('Enter');
      assert(await page.locator('.ask-history nav').isVisible());
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.ask-history').getAttribute('open'), null);
      assert(await page.locator('.ask-history summary').evaluate(node => node === document.activeElement));
      await page.locator('.chat-markdown a.cite').first().click();
      assert.equal(await page.evaluate(() => document.activeElement.id), 'c-1');
      await page.locator('#c-1 .passage-full summary').click();
      assert((await page.locator('#c-1 .passage-full').innerText()).includes('80%'));
      assert.equal(await page.locator('#c-1 .mark-form').getAttribute('action'), '/ask/7/feedback');
      await page.locator('.answer-feedback summary').click();
      assert(await page.locator('#missing-ref').isVisible());
      await fits(page);
    });
    await check('手机长回答、输入区、窄视口和软键盘空间', {viewport:{width:390,height:844}}, async ({page}) => {
      await send(page); await finished(page); await fits(page);
      assert(await page.locator('.chat-markdown h2').first().isVisible());
      await page.screenshot({path: path.join(directory, '03-answer-mobile.png')});
      await accessibility(page, 'mobile-answer');
      await page.setViewportSize({width:320,height:568}); await fits(page);
      await page.setViewportSize({width:390,height:500}); await fits(page);
      await page.locator('.chat-form textarea').fill('长草稿'.repeat(140));
      await fits(page);
      assert((await page.locator('.chat-count').innerText()).includes('500'));
    });
    await check('断网暂停后读取原ID，不重发提问', {mode:'network'}, async ({page, state}) => {
      await send(page); await finished(page);
      assert.equal(state.gets, 3); assert.equal(state.posts.length, 1);
      assert(await page.locator('.chat-resume').isVisible());
      state.mode = 'done'; await page.locator('.chat-resume').click(); await finished(page);
      assert.equal(state.posts.length, 1);
      assert(await page.locator('.chat-markdown table').isVisible());
    });
    await check('未知提交回填保留当时范围，不自动重提', {mode:'unknown'}, async ({page, state}) => {
      await page.locator('.ask-filters summary').click();
      await page.locator('[name="author"]').selectOption('合成作者甲');
      await send(page, '待恢复的问题'); await finished(page);
      assert.equal(state.posts.length, 1);
      await page.locator('[name="author"]').selectOption('合成作者乙');
      await page.locator('.chat-refill').click();
      assert.equal(await page.locator('[name="author"]').inputValue(), '合成作者甲');
      assert.equal(await page.locator('.chat-form textarea').inputValue(), '待恢复的问题');
      assert.equal(state.posts.length, 1);
      await page.screenshot({path:path.join(directory, '04-unknown-submit.png')});
    });
    await check('全库问题回填时清掉之后选择的作者和日期', {mode:'unknown'}, async ({page, state}) => {
      await send(page); await finished(page);
      await page.locator('.ask-filters summary').click();
      await page.locator('[name="author"]').selectOption('合成作者乙');
      await page.locator('[name="from"]').fill('2026-11-01');
      await page.locator('.chat-refill').click();
      assert.equal(await page.locator('[name="author"]').inputValue(), '');
      assert.equal(await page.locator('[name="from"]').inputValue(), '');
      assert.equal(state.posts.length, 1);
    });
    await check('错误重试继续同一回答任务', {mode:'error'}, async ({page, state}) => {
      await send(page); await finished(page);
      await page.locator('.chat-retry').click(); await finished(page);
      assert.equal(state.retries, 1); assert.equal(state.posts.length, 1);
      assert(await page.locator('.chat-markdown table').isVisible());
    });
    await check('打开整合中回答恢复轮询，保留新输入草稿', {file:'ask-running.html', mode:'hold'}, async ({page, state}) => {
      assert((await page.locator('.chat-status').innerText()).includes('整合'));
      await page.locator('.chat-form textarea').fill('保留这条尚未提交的草稿');
      state.mode = 'done'; await finished(page);
      assert.equal(await page.locator('.chat-form textarea').inputValue(), '保留这条尚未提交的草稿');
      assert.equal(state.posts.length, 0);
    });
    await check('无脚本保留完整回答、引用和原生提问反馈', {file:'ask-answer.html', scripts:false}, async ({page}) => {
      assert.equal(await page.locator('.chat-form').getAttribute('action'), '/ask');
      assert(await page.locator('.chat-markdown table').isVisible());
      assert.equal(await page.locator('#c-1').count(), 1);
      await page.locator('.answer-feedback summary').click();
      assert(await page.locator('#missing-ref').isVisible());
      await fits(page);
    });
    await check('无脚本手机侧栏保留文末原生问库表单', {file:'complete.html', scripts:false, viewport:{width:390,height:844}}, async ({page}) => {
      assert(await page.locator('#article-chat').isVisible());
      assert.equal(await page.locator('.chat-form').getAttribute('action'), '/ask');
      await page.locator('.chat-form textarea').scrollIntoViewIfNeeded();
      assert(await page.locator('.chat-form textarea').isVisible());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    });
    await check('深色主题与错误状态', {file:'ask-error.html', dark:true}, async ({page}) => {
      assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
      assert((await page.locator('.chat-error').innerText()).includes('合成整合失败'));
      assert.equal(await page.locator('.retry-form').getAttribute('action'), '/ask/7/retry');
      await page.screenshot({path:path.join(directory, '05-error-dark.png')});
    });
    await check('侧栏同样显示完整Markdown并保留空白初始界面', {file:'complete.html'}, async ({page}) => {
      assert.equal(await page.locator('.chat-log').innerText(), '');
      await send(page); await finished(page);
      assert(await page.locator('.chat-markdown table').isVisible());
      assert.equal(await page.locator('.chat-markdown pre a').count(), 0);
      await fits(page);
      await page.screenshot({path:path.join(directory, '06-sidebar-desktop.png')});
    });
    await check('手机抽屉焦点循环、Escape返回入口和背景隔离', {file:'complete.html', viewport:{width:390,height:844}}, async ({page}) => {
      await page.locator('.chat-toggle').click();
      assert.equal(await page.locator('#article-chat').getAttribute('role'), 'dialog');
      assert(await page.locator('.reader-grid').evaluate(node => node.inert));
      await send(page); await finished(page); await fits(page);
      await page.locator('.chat-form button[type="submit"]').focus(); await page.keyboard.press('Tab');
      assert(await page.locator('[data-chat-clear]').evaluate(node => node === document.activeElement));
      await page.keyboard.press('Shift+Tab');
      assert(await page.locator('.chat-form button[type="submit"]').evaluate(node => node === document.activeElement));
      await page.screenshot({path:path.join(directory, '07-sidebar-mobile.png')});
      await accessibility(page, 'mobile-dialog');
      await page.keyboard.press('Escape');
      assert(await page.locator('.chat-toggle').evaluate(node => node === document.activeElement));
      assert.equal(await page.locator('.reader-grid').evaluate(node => node.inert), false);
      assert.equal(await page.locator('#article-chat').getAttribute('aria-modal'), null);
    });
    await fs.writeFile(path.join(directory, 'browser-results.json'), JSON.stringify({directory, results}, null, 2));
    console.log(JSON.stringify({directory, results}, null, 2));
    assert(results.every(result => result.passed), '问库浏览器验收存在失败');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => {console.error(error); process.exitCode = 1;});
