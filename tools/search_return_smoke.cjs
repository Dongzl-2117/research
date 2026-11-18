// 用真实 Chromium 和两条可换序的合成片段核对检索回跳；不访问线上或调用模型。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function exercise(browser, width) {
  const context = await browser.newContext({viewport: {width, height: 844}});
  const origin = 'http://reader.test';
  const article = '/articles/7';
  const initial = '/search?q=%E6%AF%9B%E5%88%A9&source=sample&sort=score&page=2';
  let ordinals = [2, 8];
  const script = name => '<script src="/static/' + name + '.js"></script>';
  const fragment = ordinal => '<article class="fragment"><h3 class="item-title"><a href="' + article +
    '?chunk=' + ordinal + '#p-' + ordinal + '">查看片段 ' + ordinal +
    '</a></h3><details class="fragment-more" id="f-' + ordinal + '"><summary>展开</summary>片段全文</details></article>';
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    if (['/static/search.js', '/static/article.js'].includes(url.pathname)) {
      const body = await fs.readFile(path.join(__dirname, '../src/researchlib/web', url.pathname));
      return route.fulfill({contentType: 'text/javascript', body});
    }
    let html;
    if (url.pathname === '/search') {
      html = '<form class="search-bar"><input name="q" value="毛利"></form>' + ordinals.map(fragment).join('') +
        '<a href="/articles/7?chunk=2" target="_blank" rel="noopener">在新标签阅读</a>' + script('search');
    } else if (url.pathname.startsWith(article)) {
      html = '<div class="reader-top"></div><div class="reader"><div class="prose">原文</div></div>' +
        '<a href="/articles/7/zh?chunk=8#p-8">中文</a> <a href="/articles/7?section=3">第三节</a> ' +
        '<a href="#p-8">句子</a> <p id="p-8">句子内容</p> <a href="/authors/sample">作者</a>' + script('article');
    } else {
      html = '<a href="/articles/7">独立打开原文</a>';
    }
    return route.fulfill({contentType: 'text/html; charset=utf-8', body: html});
  });
  const page = await context.newPage();
  const back = page.locator('.back-search');
  try {
    await page.goto(origin + initial);
    const [tab] = await Promise.all([context.waitForEvent('page'), page.getByText('在新标签阅读').click()]);
    assert.equal(await tab.locator('.back-search').getAttribute('href'), initial);
    await tab.close();
    await page.locator('.search-bar input').fill('尚未提交');
    await page.locator('#f-8 summary').click();
    await page.getByText('查看片段 8').click();
    assert.equal(await back.getAttribute('href'), initial);
    assert.equal(await back.getAttribute('title'), '回到「毛利」的结果');
    await page.getByText('中文', {exact: true}).click();
    await back.waitFor();
    await page.getByText('第三节').click();
    await back.waitFor();
    await page.getByText('句子', {exact: true}).click();
    await page.reload();
    assert.equal(await back.getAttribute('href'), initial);

    ordinals = [8, 2];
    await back.click();
    assert.equal(new URL(page.url()).pathname + new URL(page.url()).search, initial);
    await page.locator('#f-8[open]').waitFor();
    assert.equal(await page.locator('#f-2').evaluate(node => node.open), false);

    await page.getByText('查看片段 8').click();
    await page.getByText('作者', {exact: true}).click();
    await page.getByText('独立打开原文').click();
    await page.waitForLoadState();
    assert.equal(await back.count(), 0);
    await page.getByText('中文', {exact: true}).click();
    await page.waitForLoadState();
    assert.equal(await back.count(), 0);

    await page.goto(origin + initial);
    await page.getByText('查看片段 2').click();
    await page.goto(origin + '/search?q=margin');
    await page.getByText('查看片段 2').click();
    assert.equal(await back.getAttribute('href'), '/search?q=margin');
    await page.goBack();
    await page.goBack();
    assert.equal(await back.getAttribute('href'), initial);
    console.log(width + 'px 已通过：新标签页、实际检索词、语言/章节/锚点刷新、片段换序、独立作者入口、两次检索历史');
  } finally { await context.close(); }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.ARTICLE_CHROMIUM || undefined,
    args: ['--no-sandbox']});
  try {
    for (const width of [1440, 390]) await exercise(browser, width);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
