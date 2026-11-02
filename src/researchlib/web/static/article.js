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
