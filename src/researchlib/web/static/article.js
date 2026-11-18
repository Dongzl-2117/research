// 只读 DOM：长文用已有章节建目录；表格可用键盘横向滚动。
(() => {
  const reader = document.querySelector('.reader');
  if (!reader) return;
  const body = reader.querySelector('.bilingual, .prose');
  if (!body) return;
  body.querySelectorAll('table').forEach(table => {
    table.tabIndex = 0;
    if (!table.getAttribute('aria-label') && !table.querySelector('caption')) {
      table.setAttribute('aria-label', '数据表，可横向滚动');
    }
  });
  if ((body.textContent || '').length < 12000) return;
  const headings = [...body.querySelectorAll('h2, h3, h4, h5, h6')]
    .filter(heading => !heading.closest('.bilingual-zh') && heading.textContent.trim());
  if (headings.length < 3) return;
  const toc = document.createElement('details');
  toc.className = 'article-toc';
  const summary = document.createElement('summary');
  summary.textContent = `文章目录（${headings.length}节）`;
  toc.append(summary);
  const list = document.createElement('ol');
  headings.forEach((heading, index) => {
    if (!heading.id) {
      let id = `article-section-${index + 1}`;
      while (document.getElementById(id)) id += '-section';
      heading.id = id;
    }
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = `#${encodeURIComponent(heading.id)}`;
    link.textContent = heading.textContent.trim();
    item.append(link);
    list.append(item);
  });
  toc.append(list);
  body.before(toc);
})();

// 重复整对只是折叠，引用仍应打开对应的出现位置。
(function () {
  function openBilingualReference() {
    var targetId = '';
    try { targetId = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
    var item = targetId ? document.getElementById(targetId) : null;
    item = item || document.querySelector('.is-quoted');
    var opened = false;
    for (var node = item; node; node = node.parentElement) {
      if (node.tagName === 'DETAILS' && !node.open) { node.open = true; opened = true; }
    }
    if (opened && item) item.scrollIntoView({block: 'center'});
  }
  openBilingualReference();
  window.addEventListener('hashchange', openBilingualReference);
}());

// 回链属于这一次阅读。由检索页进来时取它的实际地址，翻节或切换语言时再传给同一篇的下一页。
(function () {
  const readerPath = /^\/articles\/(\d+)(?:\/(?:zh|original))?$/;
  const article = location.pathname.match(readerPath);
  const top = document.querySelector('.reader-top');
  if (!article || !top) return;

  const previous = document.referrer ? new URL(document.referrer) : null;
  const key = 'research-search-reader';
  const here = location.pathname + location.search;
  let recent;
  try { recent = JSON.parse(sessionStorage.getItem(key)); } catch (_) { /* 浏览器允许不存储 */ }
  let url = history.state && history.state.researchSearchUrl;
  if (!url && previous && previous.origin === location.origin) {
    const from = previous.pathname + previous.search;
    if (previous.pathname === '/search' && previous.search) url = from;
    else if (recent && recent.from === from && previous.pathname.match(readerPath)?.[1] === article[1]) url = recent.url;
  }
  const navigation = performance.getEntriesByType?.('navigation')[0];
  if (!url && navigation?.type === 'reload' && recent && recent.from === here) url = recent.url;

  if (typeof url !== 'string' || !url.startsWith('/search?')) url = null;
  function markActiveReader() {
    try {
      if (url) sessionStorage.setItem(key, JSON.stringify({from: here, url}));
      else sessionStorage.removeItem(key);
    } catch (_) { /* 仍可从当前浏览历史项或检索页恢复 */ }
  }
  // 从作者或公司页打开同一篇，也要清掉前一轮阅读的来路；原页面从浏览器缓存恢复时则重新标记。
  markActiveReader();
  window.addEventListener('pageshow', markActiveReader);
  if (!url) return;
  function remember() {
    try { history.replaceState({...history.state, researchSearchUrl: url}, ''); } catch (_) { /* 回链仍可用 */ }
  }
  remember();
  window.addEventListener('hashchange', remember);

  var link = document.createElement('a');
  link.className = 'back back-search';
  link.href = url;
  var ns = 'http://www.w3.org/2000/svg';
  var icon = document.createElementNS(ns, 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('aria-hidden', 'true');
  var path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M14.5 6.5 8.5 12l6 5.5');
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.8');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  icon.append(path);
  link.append(icon, document.createTextNode('返回检索结果'));
  const query = new URLSearchParams(url.slice(url.indexOf('?') + 1)).get('q');
  if (query) link.title = '回到「' + query + '」的结果';
  link.addEventListener('click', function () {
    try { sessionStorage.setItem('research-search-returning', JSON.stringify(url)); } catch (_) { /* 照常跳转 */ }
  });
  top.prepend(link);
}());
