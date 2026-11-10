// 完整问库和阅读侧栏共用提问、恢复和展示逻辑；每轮独立提交。
// 「新对话」只清界面，不取消后台任务，也不删除问库记录。
// 宽屏是否收起记在 localStorage（research-article-chat）。窄屏抽屉仍每次从关上开始。
// 输入框里选的模型和思考强度记在 localStorage（research-ask-model），问库页和阅读侧栏共用；没选过就是页面上的默认。
(() => {
  const fullPage = !!document.getElementById('ask-chat');
  const panel = document.getElementById(fullPage ? 'ask-chat' : 'article-chat');
  const stage = document.querySelector('.read-with-chat');
  const toggle = document.querySelector('.chat-toggle');
  const backdrop = document.querySelector('.chat-backdrop');
  if (!panel || (!fullPage && (!stage || !toggle || !backdrop))) return;
  const form = panel.querySelector('.chat-form');
  const input = panel.querySelector('textarea');
  const submit = form && form.querySelector('button[type="submit"]');
  const log = panel.querySelector('.chat-log');
  const scopeSelect = form && form.querySelector('[name="article"]');
  if (!form || !input || !submit || !log || !scopeSelect) return;
  const clearButton = panel.querySelector('[data-chat-clear]');
  const rangeText = form.querySelector('.chat-range-text');
  const modelMenu = form.querySelector('.ask-model');
  const modelKey = 'research-ask-model';
  // 阅读侧栏「新对话」以后把问候和可以问的题放回去。
  const welcomeCopy = !fullPage && log.querySelector('.ask-welcome') ? log.querySelector('.ask-welcome').cloneNode(true) : null;

  const wideQuery = window.matchMedia('(min-width: 1320px)');
  const storageKey = 'research-article-chat';
  const ratioKey = 'research-article-chat-ratio';
  const split = stage && stage.querySelector('.chat-split');
  const minRatio = 0.18;
  const maxRatio = 0.72;
  let pending = false;
  let session = 0;
  let composing = false;
  const timers = new Set();
  const controllers = new Set();
  const maxPolls = 300;
  const maxNetworkFailures = 3;
  const requestTimeout = 15000;
  let modal = false;
  let previousOverflow = '';
  const inertNodes = new Map();

  function syncModal(open) {
    if (fullPage) return;
    const active = open && !isWide();
    if (active === modal) return;
    modal = active;
    if (active) {
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      document.querySelectorAll('.reader-grid, .sidebar, .mobile-bar, .tabbar').forEach((node) => {
        inertNodes.set(node, node.inert);
        node.inert = true;
      });
    } else {
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
      document.body.style.overflow = previousOverflow;
      inertNodes.forEach((value, node) => { node.inert = value; });
      inertNodes.clear();
    }
  }

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
    syncModal(open);
    if (open && isWide()) window.requestAnimationFrame(paintSplit);
    if (open && remember) input.focus();
    if (!open && panel.contains(document.activeElement)) toggle.focus();
  }

  function syncLayout() {
    if (isWide()) {
      backdrop.hidden = true;
      panel.classList.remove('is-open');
      stage.classList.toggle('is-chat-closed', preferClosed());
      toggle.setAttribute('aria-expanded', preferClosed() ? 'false' : 'true');
      syncModal(false);
      if (preferClosed() && panel.contains(document.activeElement)) toggle.focus();
      if (!document.documentElement.hasAttribute('data-chat-ratio')) {
        const stored = readRatio();
        if (stored != null) showRatio(stored);
      }
      paintSplit();
      return;
    }
    stage.classList.remove('is-chat-closed');
    const open = panel.classList.contains('is-open');
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    syncModal(open);
    if (!open && panel.contains(document.activeElement)) toggle.focus();
  }

  function readRatio() {
    try {
      const value = parseFloat(localStorage.getItem(ratioKey));
      if (value > 0.12 && value < 0.8) return value;
    } catch (e) {}
    return null;
  }

  function writeRatio(value) {
    try {
      if (value == null) localStorage.removeItem(ratioKey);
      else localStorage.setItem(ratioKey, String(value));
    } catch (e) {}
  }

  function showRatio(value) {
    if (value == null) {
      document.documentElement.style.removeProperty('--chat-ratio');
      document.documentElement.removeAttribute('data-chat-ratio');
      return;
    }
    document.documentElement.style.setProperty('--chat-ratio', String(value));
    document.documentElement.setAttribute('data-chat-ratio', '1');
  }

  function paintSplit() {
    if (!split || !isWide() || preferClosed()) return;
    const total = stage.getBoundingClientRect().width;
    const chat = panel.getBoundingClientRect().width;
    if (total <= 0 || chat <= 0) return;
    const value = Math.round((chat / total) * 100);
    // 默认宽栏或旧保存比例可能超出调节边界；仍如实报告实际宽度，且不落在声明范围之外。
    split.setAttribute('aria-valuemin', String(Math.min(minRatio * 100, value)));
    split.setAttribute('aria-valuemax', String(Math.max(maxRatio * 100, value)));
    split.setAttribute('aria-valuenow', String(value));
  }

  function setRatio(value, remember) {
    showRatio(value);
    if (remember) writeRatio(value);
    if (isWide()) window.requestAnimationFrame(paintSplit);
  }

  function resetRatio() {
    setRatio(null, true);
  }

  if (split) {
    let drag = null;
    split.addEventListener('pointerdown', (event) => {
      if (!isWide() || preferClosed() || event.button !== 0) return;
      event.preventDefault();
      split.setPointerCapture(event.pointerId);
      drag = {
        x: event.clientX,
        width: panel.getBoundingClientRect().width,
        total: stage.getBoundingClientRect().width,
      };
    });
    split.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const next = drag.width + (drag.x - event.clientX);
      const ratio = Math.min(maxRatio, Math.max(minRatio, next / drag.total));
      setRatio(ratio, false);
    });
    function endDrag(event) {
      if (!drag) return;
      drag = null;
      if (event && split.hasPointerCapture && split.hasPointerCapture(event.pointerId)) {
        split.releasePointerCapture(event.pointerId);
      }
      const total = stage.getBoundingClientRect().width;
      const chat = panel.getBoundingClientRect().width;
      if (total > 0) setRatio(chat / total, true);
    }
    split.addEventListener('pointerup', endDrag);
    split.addEventListener('pointercancel', endDrag);
    split.addEventListener('dblclick', () => {
      if (!isWide() || preferClosed()) return;
      resetRatio();
    });
    split.addEventListener('keydown', (event) => {
      if (!isWide() || preferClosed()) return;
      const total = stage.getBoundingClientRect().width || 1;
      const current = panel.getBoundingClientRect().width / total;
      const step = event.shiftKey ? 0.08 : 0.02;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        setRatio(Math.min(maxRatio, current + step), true);
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        setRatio(Math.max(minRatio, current - step), true);
      } else if (event.key === 'Home') {
        event.preventDefault();
        setRatio(maxRatio, true);
      } else if (event.key === 'End') {
        event.preventDefault();
        setRatio(minRatio, true);
      }
    });
    window.addEventListener('resize', () => {
      if (isWide()) paintSplit();
    });
  }

  function fitInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
    const count = form.querySelector('.chat-count');
    if (count) count.textContent = input.value.length >= 400 ? `${input.value.length} / 500` : '';
  }

  function progress(turn, message) {
    turn.status.className = 'chat-status';
    turn.status.textContent = message + '…';
  }

  // 问库页整页滚动（.chat-log 不自己滚，输入框贴在窗口底部）；阅读侧栏照旧在面板里滚。
  function scrollLog() {
    if (fullPage) window.scrollTo(0, document.documentElement.scrollHeight);
    else log.scrollTop = log.scrollHeight;
  }

  function nearEnd() {
    if (!fullPage) return log.scrollHeight - log.scrollTop - log.clientHeight < 96;
    return document.documentElement.scrollHeight - window.scrollY - window.innerHeight < 160;
  }

  // 问库页：新问题挪到标题栏下面，回答从它下面往下写（和 Claude 一样）。页面没那么长时挪不上去，
  // 所以先给这一轮的回答留出一屏的高度；上一轮留的高度收回，免得回答短时下面空一大块。
  function reserve(turn) {
    if (!fullPage) return;
    log.querySelectorAll('.chat-bubble-assistant').forEach((node) => { node.style.minHeight = ''; });
    const head = panel.querySelector('.ask-head');
    const formBox = form.getBoundingClientRect();
    const covered = Math.max(0, window.innerHeight - formBox.top);
    const room = window.innerHeight - (head ? head.offsetHeight : 0) - covered
      - turn.user.offsetHeight - (parseFloat(getComputedStyle(log).rowGap) || 0) - 16;
    turn.bubble.style.minHeight = `${Math.max(0, Math.round(room))}px`;
  }

  // 把这一轮的提问挪到看得见的最上面；问库页扣掉粘在顶上的标题栏。
  function bringToTop(node) {
    if (!fullPage) {
      log.scrollTop += node.getBoundingClientRect().top - log.getBoundingClientRect().top - 16;
      return;
    }
    // 标题栏粘在窗口顶上：按它的高度扣，不按它眼下的位置（手机上它下面还有一条顶栏，滚上去就没了）。
    const head = panel.querySelector('.ask-head');
    window.scrollBy(0, node.getBoundingClientRect().top - (head ? head.offsetHeight : 0) - 16);
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function safeArticleUrl(url) {
    if (typeof url !== 'string' || !/^\/articles\/\d+(?:[?#]|$)/.test(url)) return '';
    if (/[\s<>"']/.test(url)) return '';
    return url;
  }

  function renderAnswerHtml(text, citations) {
    const byN = Object.create(null);
    (citations || []).forEach((item) => { if (item) byN[item.n] = item; });
    return String(text || '').split(/\n{2,}/).map((block) => {
      const lines = block.split('\n').map((line) => {
        const escaped = escapeHtml(line).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        return escaped.replace(/\[(\d+)\]/g, (match, num) => {
          const href = byN[Number(num)] && safeArticleUrl(byN[Number(num)].url);
          return href ? `<a class="cite" href="${escapeHtml(href)}"><span class="cite-mark">[</span>${num}<span class="cite-mark">]</span></a>` : match;
        });
      });
      return `<p>${lines.join('<br>')}</p>`;
    }).join('');
  }

  function setPending(value) {
    pending = value;
    submit.disabled = value;
    submit.setAttribute('aria-label', value ? '正在等待回答' : '发送问题');
    log.querySelectorAll('button.chat-action').forEach((button) => { button.disabled = value; });
  }

  function cancelRequests() {
    timers.forEach((timer) => window.clearTimeout(timer));
    timers.clear();
    controllers.forEach((controller) => controller.abort());
    controllers.clear();
  }

  function later(callback, delay) {
    const timer = window.setTimeout(() => { timers.delete(timer); callback(); }, delay);
    timers.add(timer);
  }

  async function fetchTimed(url, options = {}, read = async (response) => response) {
    const controller = new AbortController();
    controllers.add(controller);
    const timer = window.setTimeout(() => controller.abort(), requestTimeout);
    try {
      const response = await fetch(url, { credentials: 'same-origin', ...options, signal: controller.signal });
      return await read(response);
    } finally {
      window.clearTimeout(timer);
      controllers.delete(controller);
    }
  }

  function errorText(data, fallback) {
    const error = data && (data.error || data.detail);
    if (typeof error === 'string') return error;
    if (error && typeof error.message === 'string') return error.message;
    return fallback;
  }

  async function jsonRequest(url, options) {
    return fetchTimed(url, options, async (response) => {
      let data;
      try { data = await response.json(); } catch (error) {
        const invalid = new Error('没有读到有效的问库响应');
        invalid.status = response.status;
        throw invalid;
      }
      // 全站问库次数满了（429）：error 里写着多久后能再问，召回的原文照样给，按没登记的回答显示。
      const limited = response.status === 429 && data && typeof data === 'object' && !Array.isArray(data);
      if (!response.ok && !limited) {
        const error = new Error(errorText(data, '暂时无法读取问库'));
        error.status = response.status;
        throw error;
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('问库响应格式不正确');
      return data;
    });
  }

  function createTurn(question, article) {
    const welcome = log.querySelector('.ask-welcome');
    if (welcome) welcome.remove();
    const suggest = panel.querySelector('.ask-suggest');
    if (suggest) suggest.remove();
    if (clearButton) clearButton.hidden = false;
    const user = document.createElement('div');
    user.className = 'chat-bubble chat-bubble-user';
    const label = document.createElement('p');
    label.className = 'chat-scope';
    label.textContent = fullPage ? scopeText() : (article ? '范围：只问这篇' : '范围：整个资料库');
    const text = document.createElement('p');
    text.textContent = question;
    user.append(label, text);
    log.appendChild(user);
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-assistant';
    const status = document.createElement('p');
    status.className = 'chat-status';
    status.setAttribute('role', 'status');
    const content = document.createElement('div');
    content.className = 'chat-content';
    const links = document.createElement('div');
    links.className = 'chat-recovery';
    bubble.append(status, content, links);
    log.appendChild(bubble);
    return { question, article, status, content, links, user, bubble, id: null };
  }

  function action(turn, label, kind) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `chat-action chat-${kind}`;
    button.textContent = label;
    button.disabled = pending;
    button.addEventListener('click', () => {
      if (pending) return;
      if (kind === 'refill') {
        input.value = turn.question;
        scopeSelect.value = turn.article;
        ['author', 'source', 'company', 'from', 'to', 'article', 'tree'].forEach((key) => {
          if (form.elements.namedItem(key)) form.elements.namedItem(key).value = (turn.params || {})[key] || '';
        });
        pickModel((turn.params || {}).model, (turn.params || {}).effort);
        syncRange();
        updateScope();
        fitInput(); input.focus();
      } else {
        beginRecovery(turn, kind);
      }
    });
    turn.links.appendChild(button);
  }

  function recovery(turn, kind) {
    turn.links.replaceChildren();
    const link = document.createElement('a');
    link.className = 'chat-open-answer';
    link.href = turn.id ? `/ask/${turn.id}` : '/ask';
    if (!fullPage) { link.target = '_blank'; link.rel = 'noopener'; }
    link.textContent = turn.id ? '打开完整回答' : '打开问库';
    turn.links.appendChild(link);
    if (kind === 'resume') action(turn, '继续查看', 'resume');
    if (kind === 'retry') action(turn, '重新整合', 'retry');
    if (kind === 'refill') action(turn, '回填问题', 'refill');
  }

  function renderData(turn, data) {
    turn.content.replaceChildren();
    if (data.answer) {
      const body = document.createElement('div');
      body.className = 'prose chat-markdown';
      // answer_html 由同源接口的共用 Markdown 渲染器清理；旧服务响应仍可安全显示文字。
      body.innerHTML = typeof data.answer_html === 'string' ? data.answer_html : renderAnswerHtml(data.answer, data.citations);
      body.querySelectorAll('a.cite').forEach((link) => {
        const number = (link.getAttribute('href') || '').match(/^#c-(\d+)$/);
        if (!number && safeArticleUrl(link.getAttribute('href'))) return;
        const item = number && (data.citations || []).find((entry) => String(entry.n) === number[1]);
        const url = item && safeArticleUrl(item.url);
        if (url) link.href = url;
        else link.replaceWith(document.createTextNode(link.textContent));
      });
      turn.content.appendChild(body);
    }
    const missed = (data.stats && data.stats.failed_batches) || data.failed_batches || [];
    if (missed.length) {
      const note = document.createElement('p');
      note.className = 'chat-warning chat-partial';
      note.textContent = `第 ${missed.join('、')} 批资料未读到，回答可能不全。可打开完整回答，或重新提问。`;
      turn.content.appendChild(note);
    }
    if (Array.isArray(data.checks) && data.checks.length) {
      const warnings = document.createElement('section');
      warnings.className = 'chat-warnings';
      warnings.setAttribute('aria-label', '需要回原文核对');
      const title = document.createElement('p');
      title.textContent = '以下句子需要回原文核对：';
      warnings.appendChild(title);
      const list = document.createElement('ul');
      data.checks.forEach((flag) => {
        const item = document.createElement('li');
        item.textContent = `${flag.sentence || ''}：${(flag.problems || []).join('；')}`;
        list.appendChild(item);
      });
      warnings.appendChild(list);
      turn.content.appendChild(warnings);
    }
    const citations = Array.isArray(data.citations) ? data.citations : [];
    if (citations.length) {
      const cited = citations.filter((item) => item.cited && safeArticleUrl(item.url));
      if (cited.length) {
        const sources = document.createElement('ul');
        sources.className = 'chat-source-links';
        sources.setAttribute('aria-label', '引用来源');
        cited.forEach((item) => {
          const entry = document.createElement('li');
          const link = document.createElement('a');
          link.href = safeArticleUrl(item.url);
          link.textContent = `[${item.n}] ${item.title || '文章'}`;
          entry.appendChild(link); sources.appendChild(entry);
        });
        turn.content.appendChild(sources);
      }
      const evidence = document.createElement('details');
      evidence.className = 'chat-evidence';
      const summary = document.createElement('summary');
      summary.textContent = `检索原文 ${citations.length} 段`;
      const list = document.createElement('ol');
      citations.forEach((item) => {
        const entry = document.createElement('li');
        const url = safeArticleUrl(item.url);
        const title = document.createElement(url ? 'a' : 'span');
        if (url) title.href = url;
        title.textContent = `[${item.n}] ${item.title || '文章'}`;
        const text = document.createElement('p');
        text.textContent = item.text || '';
        entry.append(title, text);
        list.appendChild(entry);
      });
      evidence.append(summary, list);
      turn.content.appendChild(evidence);
    }
    if (data.model) {
      const meta = document.createElement('p');
      meta.className = 'answer-meta';
      const effort = data.llm && typeof data.llm.effort_label === 'string' ? data.llm.effort_label : '';
      meta.textContent = effort ? `${data.model} · 思考${effort}` : data.model;
      turn.content.appendChild(meta);
    }
  }

  function pause(turn, message, kind = 'resume') {
    turn.status.className = 'chat-error';
    turn.status.textContent = message;
    setPending(false);
    recovery(turn, kind);
    scrollLog();
  }

  function accept(turn, data, token) {
    if (token !== session) return;
    if (/^[1-9]\d*$/.test(String(data.id))) turn.id = String(data.id);
    if (fullPage && turn.id) window.history.replaceState(null, '', `/ask/${turn.id}`);
    if (turn.restored && !input.value.trim() && (data.status === 'done' || data.status === 'error')) {
      window.location.reload();
      return;
    }
    const following = nearEnd();
    renderData(turn, data);
    recovery(turn);
    if (data.status === 'done' && data.answer) {
      turn.status.className = 'sr';
      turn.status.textContent = '回答已完成';
      setPending(false);
    } else if (data.status === 'error') {
      pause(turn, data.error || '问库整合失败', turn.id ? 'retry' : 'refill');
    } else if (!turn.id) {
      pause(turn, data.error || '没有召回到可引用的原文。', 'refill');
    } else if (data.status === 'running') {
      progress(turn, '已找到原文，正在由问库整合');
      poll(turn, token);
    } else {
      pause(turn, '回答状态异常，可打开完整回答查看。');
    }
    if (following && turn.user) bringToTop(turn.user);
  }

  // 整合中：读到第几批写在状态行，已经写出来的半截回答先显示（stats.progress、answer，见 ask._Progress）。
  function showRunning(turn, data) {
    const stage = (data.stats && data.stats.progress) || {};
    if (data.answer && data.answer !== turn.partial) {
      turn.partial = data.answer;
      renderData(turn, { ...data, checks: [], stats: {}, failed_batches: [], model: '' });
    }
    if (stage.stage === 'reading' && stage.total) {
      progress(turn, `正在读资料（${stage.done || 0}/${stage.total} 批）`);
    } else if (stage.stage === 'writing') {
      progress(turn, data.answer ? '正在写回答' : '资料读完了，正在组织回答');
    } else {
      progress(turn, '已找到原文，正在由问库整合');
    }
    return Boolean(data.answer);
  }

  function poll(turn, token) {
    let attempts = 0;
    let failures = 0;
    let writing = false;
    const deadline = Date.now() + 30 * 60 * 1000;
    async function tick() {
      if (token !== session) return;
      attempts += 1;
      try {
        const data = await jsonRequest(`/api/v1/answers/${turn.id}`, { headers: { Accept: 'application/json' } });
        if (token !== session) return;
        failures = 0;
        if (data.status !== 'running') {
          accept(turn, data, token);
          return;
        }
        writing = showRunning(turn, data);
        if (attempts >= maxPolls || Date.now() >= deadline) {
          pause(turn, '回答仍在整合，可稍后继续查看。');
          return;
        }
      } catch (error) {
        if (token !== session) return;
        failures += 1;
        if (error.status === 403 || error.status === 404) {
          pause(turn, '无法读取这条回答，可打开完整回答核对。', '');
          return;
        }
        if (failures >= maxNetworkFailures || attempts >= maxPolls || Date.now() >= deadline) {
          pause(turn, '暂时无法读取进度，回答可能仍在整合。可稍后继续查看。');
          return;
        }
      }
      later(tick, attempts < 8 ? 1500 : (writing ? 2000 : 4000));
    }
    later(tick, 800);
  }

  async function beginRecovery(turn, kind) {
    const token = ++session;
    cancelRequests();
    setPending(true);
    progress(turn, kind === 'retry' ? '正在重新整合' : '正在读取回答');
    if (kind === 'retry') {
      try {
        // 与完整回答页使用同一重试动作；仅 error 状态会领取原任务，不创建另一条回答。
        await fetchTimed(`/ask/${turn.id}/retry`, { method: 'POST', headers: { Accept: 'text/html' } }, async (response) => {
          if (!response.ok) throw new Error('未确认重新整合');
          await response.text();
        });
      } catch (error) {
        if (token === session) pause(turn, '未确认重新整合结果，请先继续查看这条回答。');
        return;
      }
    }
    if (token === session) poll(turn, token);
  }

  function resetLog() {
    log.replaceChildren();
    if (welcomeCopy) log.appendChild(welcomeCopy.cloneNode(true));
    if (clearButton) clearButton.hidden = true;
  }

  if (!fullPage) {
  toggle.addEventListener('click', () => {
    if (isWide()) setOpen(true, true);
    else setOpen(!panel.classList.contains('is-open'), true);
  });
  backdrop.addEventListener('click', () => setOpen(false, true));
  panel.querySelector('[data-chat-close]').addEventListener('click', () => setOpen(false, true));
  panel.querySelector('[data-chat-clear]').addEventListener('click', () => {
    session += 1;
    cancelRequests();
    setPending(false);
    resetLog();
    input.focus();
  });
  // 捕获阶段先看：输入框里的菜单开着时，这一下 Esc 只收起菜单（base.html 管），不收起整个面板。
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (panel.querySelector('details[data-popover][open]')) return;
    if (isWide()) {
      if (!stage.classList.contains('is-chat-closed')) setOpen(false, true);
      return;
    }
    if (panel.classList.contains('is-open')) setOpen(false, true);
  }, true);
  if (wideQuery.addEventListener) wideQuery.addEventListener('change', syncLayout);
  else if (wideQuery.addListener) wideQuery.addListener(syncLayout);
  }
  panel.addEventListener('keydown', (event) => {
    if (!modal || event.key !== 'Tab') return;
    const targets = Array.from(panel.querySelectorAll('a, button, textarea, select, input, summary, [tabindex]'))
      .filter((node) => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
    const first = targets[0], last = targets[targets.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  const scopeSummary = form.querySelector('.ask-scope');
  const baseScope = scopeSummary ? scopeSummary.textContent.split(' · ')[0] : '';
  function scopeText() {
    const values = new FormData(form);
    return [baseScope, values.get('author'),
      values.get('from') || values.get('to') ? `${values.get('from') || '最早'} 至 ${values.get('to') || '最近'}` : '']
      .filter(Boolean).join(' · ');
  }
  function updateScope() { if (scopeSummary) scopeSummary.textContent = scopeText(); }
  form.addEventListener('change', updateScope);

  // 阅读侧栏的范围：标签上的字跟着下拉框，只取「 · 单轮」前面那段（问库页的范围是隐藏字段，没有标签）。
  function syncRange() {
    if (scopeSelect.tagName !== 'SELECT') return;
    const option = scopeSelect.options[scopeSelect.selectedIndex];
    if (rangeText && option) rangeText.textContent = option.textContent.split(' · ')[0];
  }
  scopeSelect.addEventListener('change', syncRange);

  // 模型和思考强度：只有选中那一家的档位可选（别家的 fieldset 关掉，不随表单提交）。
  // 换一家时先找同一档，再找叫法相同的（Grok 的 xhigh 和 DeepSeek 的 max 都叫「最高」），都没有就用那一家的默认。
  function chosenProvider() {
    return form.querySelector('input[name="model"]:checked') || form.querySelector('input[type="hidden"][name="model"]');
  }
  function syncModel(wanted) {
    const provider = modelMenu && chosenProvider();
    if (!provider) return;
    const checked = form.querySelector('input[name="effort"]:checked');
    const want = wanted || (checked ? { id: checked.value, label: checked.dataset.label } : {});
    let picked = null;
    modelMenu.querySelectorAll('fieldset.model-efforts').forEach((box) => {
      const active = box.dataset.provider === provider.value;
      box.hidden = !active;
      box.disabled = !active;
      if (!active) return;
      const options = Array.from(box.querySelectorAll('input[name="effort"]'));
      picked = options.find((item) => item.value === want.id)
        || options.find((item) => want.label && item.dataset.label === want.label)
        || options.find((item) => item.value === box.dataset.defaultEffort) || options[0] || null;
    });
    if (picked) picked.checked = true;
    const label = modelMenu.querySelector('.ask-model-label');
    if (label && picked) label.textContent = `${provider.dataset.label} · ${picked.dataset.label}`;
  }
  function pickModel(provider, effort) {
    if (!modelMenu || !provider) return false;
    const match = Array.from(form.querySelectorAll('input[name="model"]')).find((item) => item.value === provider);
    if (!match) return false;
    if (match.type === 'radio') match.checked = true;
    syncModel({ id: String(effort || '') });
    return true;
  }
  if (modelMenu) {
    modelMenu.addEventListener('change', (event) => {
      if (!event.target.matches('input[name="model"], input[name="effort"]')) return;
      syncModel();
      const provider = chosenProvider();
      const effort = form.querySelector('input[name="effort"]:checked');
      if (!provider || !effort) return;
      try { localStorage.setItem(modelKey, JSON.stringify({ provider: provider.value, effort: effort.value })); } catch (e) {}
    });
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(modelKey) || 'null'); } catch (e) {}
    if (!(saved && typeof saved.provider === 'string' && pickModel(saved.provider, saved.effort))) syncModel();
  }

  // 可以问的题（问库页是最近问过的，阅读侧栏是这篇问过的和几道起头的题）：点一下填进输入框，自己看过、改过再发。
  // 没脚本时链接带着 q 打开问库，也是填好不发。阅读侧栏的题带着 data-scope，填进去时范围换成「只问这篇」。
  panel.addEventListener('click', (event) => {
    const link = event.target.closest('.ask-suggest a[data-question]');
    if (!link) return;
    event.preventDefault();
    input.value = link.dataset.question;
    const scope = link.dataset.scope;
    if (scope && scopeSelect.tagName === 'SELECT' && Array.from(scopeSelect.options).some((option) => option.value === scope)) {
      scopeSelect.value = scope;
      syncRange();
    }
    fitInput();
    input.focus();
  });
  input.addEventListener('input', fitInput);
  input.addEventListener('compositionstart', () => { composing = true; });
  input.addEventListener('compositionend', () => { composing = false; });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !composing && event.keyCode !== 229) {
      event.preventDefault();
      if (!pending) form.requestSubmit ? form.requestSubmit() : submit.click();
    }
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || pending || composing) return;
    const token = ++session;
    cancelRequests();
    setPending(true);
    const article = scopeSelect.value;
    const turn = createTurn(question, article);
    turn.params = {};
    for (const key of ['author', 'source', 'company', 'from', 'to', 'article', 'tree', 'model', 'effort']) {
      const value = new FormData(form).get(key);
      if (value) turn.params[key] = String(value);
    }
    progress(turn, article ? '正在这篇文章里检索' : '正在资料库里检索');
    input.value = '';
    fitInput();
    if (fullPage) { reserve(turn); bringToTop(turn.user); } else scrollLog();
    try {
      const data = await jsonRequest('/api/v1/ask', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ question, ...turn.params, wait: false }),
      });
      accept(turn, data, token);
    } catch (error) {
      if (token !== session) return;
      pause(turn, error.status ? error.message : '未确认提交结果，可先打开问库查看记录。', 'refill');
    }
  });

  if (!fullPage) syncLayout();
  else {
    const restored = log.querySelector('[data-answer-status="running"][data-answer-id]');
    if (restored && /^[1-9]\d*$/.test(restored.dataset.answerId)) {
      const turn = {id: restored.dataset.answerId, restored: true,
        status: restored.querySelector('.chat-status'), content: restored.querySelector('.chat-content'),
        links: restored.querySelector('.chat-recovery')};
      setPending(true);
      recovery(turn);
      poll(turn, session);
    }
    log.addEventListener('click', (event) => {
      const cite = event.target.closest('a.cite[href^="#c-"]');
      if (!cite) return;
      const target = document.getElementById(cite.getAttribute('href').slice(1));
      if (!target) return;
      let ancestor = target.parentElement;
      while (ancestor && ancestor !== log) { if (ancestor.tagName === 'DETAILS') ancestor.open = true; ancestor = ancestor.parentElement; }
      target.focus({preventScroll: true});
    });
  }
  syncRange();
  fitInput();
})();
