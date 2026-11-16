// 从检索结果点进文章以后回得来（Zhilin 11-16：点进去就回不到全部结果）。
// 点结果里的文章链接时，记下这页检索的地址、滚到哪、哪几条片段展开着（sessionStorage，只在这个标签页里）；
// 文章页据此在顶上放「返回检索结果」（article.js），点它或浏览器后退回到这里时，把展开的片段和滚动位置还原。
// 存不了（隐私模式、禁用存储）就什么都不做，浏览器后退照旧能用。
(function () {
  var RETURN = 'research-search-return';
  var RETURNING = 'research-search-returning';
  var here = location.pathname + location.search;
  var stateKey = 'research-search-state:' + here;
  var MAX_AGE = 12 * 3600 * 1000;

  function save(key, value) {
    try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 存不了就算了 */ }
  }
  function read(key) {
    try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (e) { return null; }
  }
  function drop(key) {
    try { sessionStorage.removeItem(key); } catch (e) { /* 同上 */ }
  }
  function expandable() {
    return Array.prototype.slice.call(document.querySelectorAll('details.fragment-more'));
  }
  function snapshot() {
    var open = [];
    expandable().forEach(function (item, index) { if (item.open) open.push(index); });
    save(stateKey, {y: window.scrollY, open: open, at: Date.now()});
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest ? event.target.closest('a[href^="/articles/"]') : null;
    if (!link) return;
    var match = link.getAttribute('href').match(/^\/articles\/(\d+)/);
    if (!match) return;
    var box = document.querySelector('.search-bar input[name="q"]');
    snapshot();
    save(RETURN, {url: here, article: match[1], query: box ? box.value : '', at: Date.now()});
  });
  window.addEventListener('pagehide', snapshot);

  window.addEventListener('pageshow', function (event) {
    // 浏览器整页缓存着的（后退时原样拿回来）不用动。
    if (event.persisted) return;
    var entry = performance.getEntriesByType ? performance.getEntriesByType('navigation')[0] : null;
    var back = entry && entry.type === 'back_forward';
    var returning = read(RETURNING) === here;
    drop(RETURNING);
    if (!back && !returning) return;
    var saved = read(stateKey);
    if (!saved || Date.now() - saved.at > MAX_AGE) return;
    var items = expandable();
    (saved.open || []).forEach(function (index) { if (items[index]) items[index].open = true; });
    if (saved.y) window.scrollTo(0, saved.y);
  });
}());
