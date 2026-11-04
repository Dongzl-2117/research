// 阅读页右侧对话。提问走 /api/v1/ask（chat=true），回答轮询 /api/v1/answers/{id}。
// 多轮只留在这一页的内存里：清空、收起后换页，或整页刷新，上文就没了。
// 宽屏是否收起记在 localStorage（research-article-chat）。窄屏抽屉仍每次从关上开始。
(() => {
  const panel = document.getElementById('article-chat');
  const stage = document.querySelector('.read-with-chat');
  const toggle = document.querySelector('.chat-toggle');
  const backdrop = document.querySelector('.chat-backdrop');
  if (!panel || !stage || !toggle || !backdrop) return;
  const form = panel.querySelector('.chat-form');
  const input = panel.querySelector('textarea');
  const submit = form && form.querySelector('button[type="submit"]');
  const log = panel.querySelector('.chat-log');
  const articleId = panel.getAttribute('data-article');
  if (!form || !input || !submit || !log || !articleId) return;

  const wideQuery = window.matchMedia('(min-width: 1320px)');
  const storageKey = 'research-article-chat';
  const emptyText = '先看这篇。\n别处有关的，也会引用。';
  let turns = [];
  let pending = false;
  let session = 0;
  let statusNode = null;

  function isWide() {
    return wideQuery.matches;
  }

  function preferClosed() {
    return document.documentElement.getAttribute('data-article-chat') === 'closed';
  }

  function rememberDesktop(open) {
    if (!isWide()) return;
    try {
      localStorage.setItem(storageKey, open ? 'open' : 'closed');
    } catch (e) {}
    if (open) document.documentElement.removeAttribute('data-article-chat');
    else document.documentElement.setAttribute('data-article-chat', 'closed');
  }

  function setOpen(open, remember) {
    if (isWide()) {
      if (remember) rememberDesktop(open);
      stage.classList.toggle('is-chat-closed', !open);
      panel.classList.remove('is-open');
      backdrop.hidden = true;
    } else {
      stage.classList.remove('is-chat-closed');
      panel.classList.toggle('is-open', open);
      backdrop.hidden = !open;
    }
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && remember) input.focus();
  }

  function syncLayout() {
    if (isWide()) {
      backdrop.hidden = true;
      panel.classList.remove('is-open');
      stage.classList.toggle('is-chat-closed', preferClosed());
      toggle.setAttribute('aria-expanded', preferClosed() ? 'false' : 'true');
      return;
    }
    stage.classList.remove('is-chat-closed');
    const open = panel.classList.contains('is-open');
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function fitInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }

  function scrollLog() {
    log.scrollTop = log.scrollHeight;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function safeArticleUrl(url) {
    if (typeof url !== 'string' || !url.startsWith('/articles/')) return '';
    if (/[\s<>"']/.test(url)) return '';
    return url;
  }

  function renderAnswerHtml(text, citations) {
    const byN = {};
    (citations || []).forEach((item) => { byN[item.n] = item; });
    return String(text || '').split(/\n{2,}/).map((block) => {
      const lines = block.split('\n').map((line) => {
        const escaped = escapeHtml(line).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        return escaped.replace(/\[(\d{1,3})\]/g, (match, num) => {
          const item = byN[Number(num)];
          const href = item && safeArticleUrl(item.url);
          if (!href) return match;
          return `<a class="cite" href="${href}">[${num}]</a>`;
        });
      });
      return `<p>${lines.join('<br>')}</p>`;
    }).join('');
  }

  function appendSources(parent, citations) {
    const seen = {};
    const items = [];
    (citations || []).forEach((item) => {
      if (!item || !item.cited || seen[item.article_id]) return;
      if (!safeArticleUrl(item.url)) return;
      seen[item.article_id] = true;
      items.push(item);
    });
    if (!items.length) return;
    const list = document.createElement('ul');
    list.className = 'chat-sources';
    items.forEach((item) => {
      const li = document.createElement('li');
      const link = document.createElement('a');
      link.href = safeArticleUrl(item.url);
      const same = String(item.article_id) === String(articleId);
      link.textContent = `${same ? '这篇 · ' : ''}${item.title || '文章'}`;
      li.appendChild(link);
      list.appendChild(li);
    });
    parent.appendChild(list);
  }

  function appendUser(question) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-user';
    const paragraph = document.createElement('p');
    paragraph.textContent = question;
    bubble.appendChild(paragraph);
    log.appendChild(bubble);
  }

  function appendAnswer(data) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-assistant';
    bubble.innerHTML = renderAnswerHtml(data.answer, data.citations);
    appendSources(bubble, data.citations);
    log.appendChild(bubble);
  }

  function clearStatus() {
    if (statusNode && statusNode.parentNode) statusNode.remove();
    statusNode = null;
  }

  function showError(message) {
    clearStatus();
    const note = document.createElement('p');
    note.className = 'chat-error';
    note.textContent = message || '没有答出来';
    log.appendChild(note);
    scrollLog();
  }

  function finish(data) {
    pending = false;
    submit.disabled = false;
    clearStatus();
    turns.push({ role: 'assistant', content: data.answer || '' });
    appendAnswer(data);
    scrollLog();
  }

  function poll(id, token) {
    let tries = 0;
    function tick() {
      if (token !== session) return;
      fetch(`/api/v1/answers/${id}`, { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (token !== session) return;
          if (!data || data.status === 'running') {
            tries += 1;
            if (tries > 80) {
              pending = false;
              submit.disabled = false;
              showError('这一轮还没写完。可以再到问这篇看，或重新问一次。');
              return;
            }
            window.setTimeout(tick, tries < 8 ? 1500 : 4000);
            return;
          }
          if (data.status === 'error' || !data.answer) {
            pending = false;
            submit.disabled = false;
            showError(data.error || '没有答出来');
            return;
          }
          finish(data);
        })
        .catch(() => {
          if (token === session) window.setTimeout(tick, 4000);
        });
    }
    window.setTimeout(tick, 800);
  }

  function resetLog() {
    log.replaceChildren();
    const empty = document.createElement('p');
    empty.className = 'chat-empty';
    emptyText.split('\n').forEach((line, index) => {
      if (index) empty.appendChild(document.createElement('br'));
      empty.appendChild(document.createTextNode(line));
    });
    log.appendChild(empty);
  }

  toggle.addEventListener('click', () => {
    if (isWide()) setOpen(true, true);
    else setOpen(!panel.classList.contains('is-open'), true);
  });
  backdrop.addEventListener('click', () => setOpen(false, true));
  panel.querySelector('[data-chat-close]').addEventListener('click', () => setOpen(false, true));
  panel.querySelector('[data-chat-clear]').addEventListener('click', () => {
    session += 1;
    pending = false;
    turns = [];
    submit.disabled = false;
    statusNode = null;
    resetLog();
    input.focus();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (isWide()) {
      if (!stage.classList.contains('is-chat-closed')) setOpen(false, true);
      return;
    }
    if (panel.classList.contains('is-open')) setOpen(false, true);
  });
  if (wideQuery.addEventListener) wideQuery.addEventListener('change', syncLayout);
  else if (wideQuery.addListener) wideQuery.addListener(syncLayout);

  input.addEventListener('input', fitInput);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (form.requestSubmit) form.requestSubmit();
      else submit.click();
    }
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || pending) return;
    pending = true;
    submit.disabled = true;
    const empty = log.querySelector('.chat-empty');
    if (empty) empty.remove();
    appendUser(question);
    const history = turns.slice();
    turns.push({ role: 'user', content: question });
    input.value = '';
    fitInput();
    clearStatus();
    statusNode = document.createElement('p');
    statusNode.className = 'chat-status';
    statusNode.textContent = '正在根据这篇文章查找…';
    log.appendChild(statusNode);
    scrollLog();
    const token = session;
    fetch('/api/v1/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        question,
        article: String(articleId),
        chat: true,
        history,
        wait: false,
      }),
    })
      .then((response) => response.json().then((body) => ({ ok: response.ok, body })))
      .then(({ ok, body }) => {
        if (token !== session) return;
        if (!ok || !body || body.id == null) {
          pending = false;
          submit.disabled = false;
          const message = body && body.error && (body.error.message || body.error);
          showError(typeof message === 'string' ? message : '没有答出来');
          return;
        }
        if (body.status === 'done' && body.answer) {
          finish(body);
          return;
        }
        if (body.status === 'error') {
          pending = false;
          submit.disabled = false;
          showError(body.error || '没有答出来');
          return;
        }
        poll(body.id, token);
      })
      .catch(() => {
        if (token !== session) return;
        pending = false;
        submit.disabled = false;
        showError('没有连上。可以再试一次。');
      });
  });

  syncLayout();
})();
