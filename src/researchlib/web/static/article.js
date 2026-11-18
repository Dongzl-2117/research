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

// 从检索结果点进来的：顶上加「返回检索结果」，回到原来那页结果（search.js 记的地址，同一个标签页、同一篇里有效，
// 在这篇里翻节、看全文都还在）。长文里翻几节以后浏览器后退只是退回上一节，以前回不到结果页。
(function () {
  var saved;
  try { saved = JSON.parse(sessionStorage.getItem('research-search-return') || 'null'); } catch (_) { return; }
  var match = location.pathname.match(/^\/articles\/(\d+)/);
  if (!saved || !match || saved.article !== match[1] || typeof saved.url !== 'string') return;
  if (saved.url.indexOf('/search?') !== 0) return;
  var top = document.querySelector('.reader-top');
  if (!top) return;
  var link = document.createElement('a');
  link.className = 'back back-search';
  link.href = saved.url;
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
  if (saved.query) link.title = '回到「' + saved.query + '」的结果';
  link.addEventListener('click', function () {
    try { sessionStorage.setItem('research-search-returning', JSON.stringify(saved.url)); } catch (_) { /* 照常跳转 */ }
  });
  top.prepend(link);
}());

// 出处与选中文字只经过片段标识符，在本机手记中读取。
(function () {
  const link = document.getElementById('journal-article');
  if (!link) return;
  const ref = location.pathname + location.search + location.hash;
  const quoted = document.querySelector('.is-quoted, .quoted > p:last-child')?.textContent.trim() || '';
  function update(excerpt) {
    link.hash = new URLSearchParams({ref, excerpt: excerpt.slice(0, 4000)}).toString();
  }
  update(quoted);
  document.addEventListener('selectionchange', function () {
    const selection = document.getSelection();
    const anchor = selection?.anchorNode?.parentElement;
    if (selection?.toString().trim() && anchor?.closest('.reader .prose')) update(selection.toString().trim());
  });
}());
