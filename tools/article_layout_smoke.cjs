// 只服务临时合成页面；外网请求一律拦下，不使用网站或模型。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright-core');

(async () => {
  const root = path.resolve(__dirname, '..');
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'research-article-layout-'));
  const generated = spawnSync(process.env.ARTICLE_PYTHON || 'python',
    ['tools/article_layout_fixture.py', directory], {cwd: root, env: process.env, encoding: 'utf8'});
  assert.equal(generated.status, 0, generated.stderr);
  const server = http.createServer(async (req, res) => {
    const requested = path.resolve(directory, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname));
    if (!requested.startsWith(directory + path.sep)) { res.writeHead(403).end(); return; }
    try {
      const type = {'.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml'};
      res.setHeader('content-type', (type[path.extname(requested)] || 'application/octet-stream') + '; charset=utf-8');
      res.end(await fs.readFile(requested));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({executablePath: process.env.ARTICLE_CHROMIUM || undefined, headless: true});
    const results = [];
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.continue() : route.abort());
      await page.goto(origin + '/complete.html');
      await page.locator('.article-toc').waitFor();
      assert.equal(await page.locator('h1').count(), 1);
      assert.equal(await page.locator('.bilingual-en img').count(), 1);
      assert.equal(await page.locator('.bilingual-zh img').count(), 0);
      assert((await page.locator('.bilingual-en pre').innerText()).includes('return 42 < 100'));
      assert((await page.locator('.source-missing').first().innerText()).includes('原导出未包含'));
      const metrics = await page.locator('.bilingual-en table').evaluate(table => {
        const cell = table.querySelector('td');
        const range = document.createRange(); range.selectNodeContents(cell);
        return {viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
          tableWidth: table.clientWidth, tableContent: table.scrollWidth, cellLines: range.getClientRects().length,
          tabIndex: table.tabIndex};
      });
      assert(metrics.tableContent > metrics.tableWidth, JSON.stringify(metrics));
      assert(metrics.documentWidth <= metrics.viewport + 1, JSON.stringify(metrics));
      assert.equal(metrics.cellLines, 1, JSON.stringify(metrics));
      assert.equal(metrics.tabIndex, 0);
      await page.screenshot({path: path.join(directory, `complete-${width}.png`)});
      await page.locator('.bilingual-en table').scrollIntoViewIfNeeded();
      await page.screenshot({path: path.join(directory, `table-${width}.png`)});
      await page.locator('.article-toc summary').click();
      const link = page.locator('.article-toc a').last();
      const target = await link.getAttribute('href'); await link.click();
      assert.equal(new URL(page.url()).hash, target);
      await page.goto(origin + '/misaligned.html');
      assert((await page.locator('.body-note').innerText()).includes('分别展示完整原文和译文'));
      assert.equal(await page.locator('.bilingual-en table').count(), 1);
      assert.equal(await page.locator('.bilingual-pair:has(.bilingual-en):has(.bilingual-zh)').count(), 0);
      for (const name of ['missing-middle', 'amount-swap']) {
        await page.goto(origin + '/' + name + '.html');
        assert((await page.locator('.body-note').innerText()).includes('分别展示完整原文和译文'));
        assert.equal(await page.locator('.bilingual-pair:has(.bilingual-en):has(.bilingual-zh)').count(), 0);
        assert((await page.locator('.bilingual-en').innerText()).includes('Beta spent 20'));
        await page.screenshot({path: path.join(directory, `${name}-${width}.png`)});
      }
      await page.goto(origin + '/alternate-wording.html');
      assert.equal(await page.locator('.bilingual-pair').count(), 2);
      const alternate = (await page.locator('.bilingual-zh').allTextContents()).join(' ');
      assert(alternate.includes('甲赚了10美元') && alternate.includes('新增订阅带来10美元'));
      await page.screenshot({path: path.join(directory, `alternate-wording-${width}.png`)});
      assert.equal(errors.length, 0, errors.join('\n'));
      results.push({width, ...metrics, tocNavigation: true, misalignmentNotice: true,
        missingMiddleFallback: true, swappedAmountFallback: true, alternateWordingPreserved: true,
        javascriptErrors: errors.length});
      await page.close();
    }
    console.log(JSON.stringify({directory, results}, null, 2));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
