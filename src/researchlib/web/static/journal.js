(() => {
  const store = window.ResearchJournal;
  const $ = id => document.getElementById(id);
  const form = $('note-form');
  const context = new URLSearchParams(location.hash.slice(1));
  const company = (context.get('company') || '').slice(0, 100);
  const companyId = /^[1-9]\d*$/.test(context.get('companyId') || '') ? context.get('companyId') : '';
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('research-journal-changed') : null;
  let db, records = [], editing = null, cutoff = '';
  const watchForm = $('watch-form');
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function anchor(text, href) {
    const a = node('a', text);
    a.href = href;
    a.rel = 'noreferrer';
    return a;
  }
  function action(text, fn) {
    const b = node('button', text);
    b.type = 'button'; b.addEventListener('click', fn);
    return b;
  }
  function say(text, error = false) {
    $('journal-status').setAttribute('role', error ? 'alert' : 'status');
    $('journal-status').textContent = text;
  }
  function guarded(fn) {
    return async event => {
      event?.preventDefault();
      try { await fn(); }
      catch (error) { say(error.message || '无法访问本机资料。', true); }
    };
  }
  function localStamp(date) {
    const pad = n => String(n).padStart(2, '0');
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + 'T' +
      pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
  }
  function displayed(at) { return localStamp(new Date(at)).replace('T', ' '); }
  function appendPair(dl, label, value) {
    if (!value) return;
    const dd = node('dd');
    dd.append(value);
    dl.append(node('dt', label), dd);
  }
  function versionDetails(version) {
    const v = version.value;
    const dl = node('dl');
    appendPair(dl, '原文摘录', v.excerpt);
    appendPair(dl, '出处', v.ref && anchor('核对原文或段落', v.ref));
    appendPair(dl, '指标', [v.metric, v.unit, v.period].filter(Boolean).join(' · '));
    appendPair(dl, '预期值', v.expected);
    appendPair(dl, '实际值', v.actual);
    appendPair(dl, '公告发布时间', v.published && displayed(v.published));
    appendPair(dl, '一手披露', v.filing && anchor('查看公告', v.filing));
    appendPair(dl, '此次变化', v.change);
    return dl;
  }
  function noteCard(record, versions) {
    const last = versions.at(-1), v = last.value;
    const card = node('article', undefined, 'journal-note'); card.id = record.id;
    const title = node('h3');
    title.append(v.companyId ? anchor(v.company, '/companies/' + v.companyId) : v.company);
    card.append(title, node('p', '记录于 ' + displayed(last.at), 'quiet num'), node('p', v.hypothesis, 'note-text'));
    if (v.falsifier) card.append(node('p', '推翻条件：' + v.falsifier, 'note-text'));
    if (v.review) card.append(node('p', '下次复核：' + v.review));
    card.append(versionDetails(last));
    if (versions.length > 1) {
      const history = node('details');
      history.append(node('summary', '此前的判断（' + (versions.length - 1) + ' 版）'));
      for (const prior of versions.slice(0, -1).reverse()) {
        const old = prior.value;
        history.append(node('h4', displayed(prior.at) + ' · ' + old.company), node('p', old.hypothesis, 'note-text'));
        if (old.falsifier) history.append(node('p', '推翻条件：' + old.falsifier, 'note-text'));
        if (old.review) history.append(node('p', '当时计划复核：' + old.review));
        history.append(versionDetails(prior));
      }
      card.append(history);
    }
    if (!cutoff) card.append(action('记录新判断', () => edit(record)));
    return card;
  }
  function asOf(row) {
    return cutoff ? row.revisions.filter(v => v.at <= cutoff) : row.revisions;
  }
  function paint() {
    const today = cutoff ? localStamp(new Date(cutoff)).slice(0, 10) : localStamp(new Date()).slice(0, 10);
    const notes = $('notes'), due = $('due-list'), watches = $('watches'), names = $('company-names');
    watches.replaceChildren(); due.replaceChildren(); notes.replaceChildren(); names.replaceChildren();
    const current = records.map(row => ({row, versions: asOf(row)})).filter(item => item.versions.length);
    current.sort((a, b) => b.versions.at(-1).at.localeCompare(a.versions.at(-1).at));
    const overdue = [];
    for (const {row, versions} of current) {
      const v = versions.at(-1).value;
      if (row.kind === 'watch') {
        if (!v.active) continue;
        const li = node('li');
        li.append(v.companyId ? anchor(v.company, '/companies/' + v.companyId) : node('span', v.company));
        if (!cutoff) li.append(action('移出自选', guarded(() => changeWatch(v.company, v.companyId, false))));
        watches.append(li); names.append(new Option(v.company));
      } else {
        notes.append(noteCard(row, versions));
        if (v.review && v.review <= today) overdue.push({id: row.id, value: v});
      }
    }
    overdue.sort((a, b) => a.value.review.localeCompare(b.value.review));
    for (const {id, value} of overdue) {
      const li = node('li'); li.append(anchor(value.review + ' · ' + value.company + ' · ' + value.hypothesis.slice(0, 60), '#' + id));
      due.append(li);
    }
    if (!notes.childElementCount) notes.append(node('p', cutoff ? '当时还没有判断。' : '还没有判断。可以从公司或文章页记下第一条。', 'quiet'));
    if (!watches.childElementCount) watches.append(node('li', '暂无自选公司。', 'quiet'));
    if (!due.childElementCount) due.append(node('li', '没有到期的复核。', 'quiet'));
    $('new-note').hidden = Boolean(cutoff); watchForm.hidden = Boolean(cutoff);
    $('return-now').hidden = !cutoff; $('export').closest('details').hidden = Boolean(cutoff);
  }
  async function refresh() { records = await store.all(db); paint(); }
  async function afterChange(message) {
    channel?.postMessage('changed'); await refresh(); say(message);
  }
  function edit(record, preset = {}) {
    if (cutoff) return;
    const last = record?.revisions.at(-1);
    const v = last?.value || preset;
    editing = {id: record?.id || 'note:' + crypto.randomUUID(), head: last?.at || '', company: v.company || '', companyId: v.companyId || ''};
    form.reset();
    for (const field of Object.keys(store.fields)) {
      const input = form.elements.namedItem(field);
      if (input) input.value = field === 'published' && v[field] ? localStamp(new Date(v[field])) : (v[field] || '');
    }
    if (last) form.elements.change.value = '';
    $('editor-title').textContent = last ? '记录新判断' : '新建判断';
    $('editor').hidden = false;
    form.elements[last ? 'hypothesis' : 'company'].focus();
  }
  function closeEditor() { editing = null; form.reset(); $('editor').hidden = true; }
  async function saveNote() {
    if (!editing || cutoff) return;
    const value = Object.fromEntries(new FormData(form));
    value.company = value.company.trim();
    const watch = records.find(row => row.id === store.watchId(value.company))?.revisions.at(-1)?.value;
    value.companyId = value.company === editing.company ? editing.companyId : (watch?.companyId || '');
    if (value.published) value.published = new Date(value.published).toISOString();
    await store.save(db, editing.id, 'note', editing.head, value);
    closeEditor(); await afterChange('已保存此版本。');
  }
  async function changeWatch(name, id, active) {
    if (cutoff) return;
    const company = name.trim(), key = store.watchId(company);
    const last = records.find(row => row.id === key)?.revisions.at(-1);
    if (last?.value.active === active && (!id || id === last.value.companyId)) return say('已在自选列表中。');
    await store.save(db, key, 'watch', last?.at || '', {company, companyId: id || last?.value.companyId || '', active});
    watchForm.reset(); await afterChange(active ? '已加入自选。' : '已移出自选。');
  }
  function historyTime() {
    const value = $('as-of').value;
    if (value && new Date(value).getTime() > Date.now()) {
      $('as-of').value = cutoff ? localStamp(new Date(cutoff)) : '';
      return say('请选择当前或更早的时间。', true);
    }
    cutoff = value ? new Date(value).toISOString() : '';
    closeEditor(); paint();
    say(cutoff ? '只读回看，以此时已经记录的版本为准。' : '');
  }
  function download(data) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(store.backup(data), null, 2)], {type: 'application/json'}));
    const a = anchor('下载备份', url); a.download = '投研手记-' + localStamp(new Date()).slice(0, 10) + '.json';
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  async function init() {
    try {
      db = await store.open();
      form.addEventListener('submit', guarded(saveNote));
      watchForm.addEventListener('submit', guarded(() => {
        const name = watchForm.elements.company.value.trim();
        return changeWatch(name, name === company ? companyId : '', true);
      }));
      $('new-note').onclick = () => edit(null, {company, companyId});
      $('cancel-note').onclick = closeEditor;
      $('as-of').onchange = historyTime;
      $('return-now').onclick = () => { $('as-of').value = ''; historyTime(); };
      $('export').onclick = guarded(async () => download(await store.all(db)));
      $('import').onchange = guarded(async () => {
        const input = $('import'), file = input.files[0]; input.value = '';
        if (!file) return;
        const count = await store.restore(db, JSON.parse(await file.text()));
        await afterChange('已合并 ' + count + ' 条记录。');
      });
      if (channel) channel.onmessage = guarded(refresh);
      await refresh(); $('journal-controls').hidden = false;
      if (company || context.get('ref')) {
        watchForm.elements.company.value = company;
        if (company && context.get('watch') === '1') watchForm.elements.company.focus();
        else edit(null, {company, companyId, ref: context.get('ref') || '', excerpt: context.get('excerpt') || ''});
        history.replaceState(null, '', location.pathname);
      }
    } catch (error) { say('无法打开本机手记：' + error.message, true); }
  }
  init();
})();
