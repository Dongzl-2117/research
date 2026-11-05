'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="experiment-csrf"]').content;
  const apiBase = document.querySelector('meta[name="experiment-api-base"]').content;
  const names = {baseline: 'A · 基线', candidate: 'B · 候选'};
  const decisions = {baseline: 'A 更好', candidate: 'B 更好', tie: '相当', neither: '都不好', skip: '跳过', uncertain: '无法判断', withdraw: '撤销评价'};
  const descriptors = [
    ['k1', '词频饱和度 K1', 0.1, 3, 0.1], ['b', '长段落惩罚 B', 0, 1, 0.05],
    ['title_tf', '标题词频权重', 0, 2, 0.1], ['title_bonus', '标题额外加分', 0, 2, 0.1],
    ['phrase_bonus', '相邻词组加分', 0, 2, 0.1], ['coverage_power', '完整命中倾向', 0.5, 4, 0.1],
    ['min_score_ratio', '问库最低相对分', 0, 0.5, 0.01], ['passages', '问库最多段落数', 1, 80, 1],
    ['chars', '问库正文字符预算', 1000, 32000, 1000], ['per_article', '每篇基准段数（长文可 3 倍）', 1, 6, 1],
  ];
  let state, current, target = 'search', busy = false;
  const fields = {};
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const fail = error => { $('error').textContent = error.message || String(error); $('error').hidden = false; };
  async function api(path, body) {
    const response = await fetch(apiBase + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Experiment-CSRF': csrf}, body: JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(value.detail) ? value.detail.map(x => x.loc.slice(1).join('.') + ': ' + x.msg).join('；') : value.detail;
      throw new Error(detail || '请求失败，请刷新后重试');
    }
    return value;
  }
  async function action(fn) {
    if (busy) return;
    busy = true; $('error').hidden = true; $('compare').disabled = true;
    try { await fn(); } catch (error) { fail(error); }
    finally { busy = false; $('compare').disabled = false; }
  }
  function makeFields(side) {
    const container = $(side + '-fields');
    fields[side] = {};
    for (const [key, name, min, max, step] of descriptors) {
      const label = el('label', undefined, 'field'); label.append(el('span', name));
      const input = el('input'); Object.assign(input, {type: 'number', min, max, step, required: true});
      input.dataset.key = key; input.value = state.defaults[key]; input.setAttribute('aria-label', names[side] + ' ' + name);
      label.append(input); container.append(label); fields[side][key] = input;
    }
    const label = el('label', undefined, 'field wide'); label.append(el('span', '回答附加要求（只影响生成）'));
    const note = el('textarea'); note.maxLength = 2000; note.placeholder = '例如：先列分歧，再解释依据，每条带原文编号。';
    note.setAttribute('aria-label', names[side] + ' 回答附加要求');
    label.append(note); container.append(label); fields[side].prompt_note = note;
  }
  function values(side) {
    const result = {};
    for (const [key, input] of Object.entries(fields[side])) {
      if (!input.checkValidity()) { input.reportValidity(); throw new Error(names[side] + ' 参数无效：' + key); }
      result[key] = key === 'prompt_note' ? input.value : Number(input.value);
    }
    return result;
  }
  function setValues(side, value) {
    for (const [key, input] of Object.entries(fields[side])) input.value = value[key] ?? state.defaults[key];
  }
  function request() {
    const ids = [...$('questions').querySelectorAll('input:checked')].map(x => x.value);
    if (!ids.length) throw new Error('请至少选择一个问题');
    return {baseline: values('baseline'), candidate: values('candidate'), case_ids: ids};
  }
  function setRequest(value) {
    setValues('baseline', value.baseline); setValues('candidate', value.candidate);
    for (const input of $('questions').querySelectorAll('input')) input.checked = value.case_ids.includes(input.value);
  }
  function download(value, name) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
    const link = el('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function refreshHistory(history) {
    $('history').replaceChildren(el('option', '选择已保存对照'));
    $('history').firstChild.value = '';
    for (const run of history) {
      const option = el('option', run.created_at.slice(0, 16).replace('T', ' ') + ' · ' + run.id.slice(0, 8));
      option.value = run.id; $('history').append(option);
    }
    if (current) $('history').value = current.run.id;
  }
  async function loadRun(key) {
    current = await api('runs/' + key);
    history.replaceState(null, '', '#run=' + key);
    $('results').hidden = false; render();
  }
  function latest(caseId) {
    const events = current.rating_events.filter(x => x.request.case_id === caseId && x.request.target === target);
    return events.length ? events[events.length - 1] : null;
  }
  function sourceButton(caseId, passage) {
    const button = el('button', '查看冻结原文 ↗', 'source-link');
    button.addEventListener('click', () => action(async () => {
      const row = await api('source/' + encodeURIComponent(caseId) + '/' + passage.article_id + '/' + passage.ord);
      $('source-meta').textContent = row.title + ' · ' + row.source_id + ' · 文章 ' + row.article_id + ' / 段 ' + row.ord + '\n内容版本 ' + row.content_hash.slice(0, 16);
      $('source-text').textContent = (row.heading ? row.heading + '\n\n' : '') + row.body;
      $('source-dialog').showModal();
    }));
    return button;
  }
  function material(caseId, data, side, generation) {
    const node = el('div', undefined, 'side');
    const header = el('div', undefined, 'side-head'); header.append(el('span', names[side]));
    header.append(el('span', target === 'search' ? data.search.length + ' 篇 · ' + data.metrics.search_ms + ' ms' : data.metrics.passages + ' 段 · ' + data.metrics.material_chars + ' 字', 'hint'));
    node.append(header);
    if (target === 'answer') {
      const answer = generation?.sides?.[side];
      node.append(el('p', answer?.text || (generation?.status === 'failed' ? '这一侧生成未完成。' : '尚未生成答案。可展开提示词检查，或导出后在自选模型中使用。'), 'answer-text'));
      if (answer) node.append(el('p', answer.model + ' · ' + answer.model_version + ' · 输入 ' + (answer.input_tokens ?? '未知') + ' / 输出 ' + (answer.output_tokens ?? '未知') + ' tokens · 费用 ' + (answer.cost == null ? '未知' : answer.cost + ' ' + answer.currency), 'hint'));
      const details = el('details'); details.append(el('summary', '查看实际提示词'));
      details.append(el('pre', data.messages.map(m => m.role + '\n' + m.content).join('\n\n'))); node.append(details);
      const button = el('button', '导出这一侧提示词', 'source-link');
      button.addEventListener('click', () => download({run_id: current.run.id, case_id: caseId, side, messages: data.messages}, 'prompt-' + side + '.json'));
      node.append(button); return node;
    }
    const items = target === 'search' ? data.search : data.passages;
    if (!items.length) node.append(el('p', '这套参数没有选出结果。', 'hint'));
    items.forEach((item, index) => {
      const hit = el('article', undefined, 'hit');
      hit.append(el('div', (index + 1) + '. ' + item.title, 'hit-title'));
      hit.append(el('p', (item.source_id || '') + (item.published_at || item.date ? ' · ' + (item.published_at || item.date).slice(0, 10) : '') + (target === 'search' ? ' · ' + (item.full ? '全部词命中' : '部分词命中') + ' · 分数 ' + item.score.toFixed(3) : ' · 原文编号 [' + item.n + ']'), 'hint'));
      if (target === 'search') {
        item.passages.forEach(p => { hit.append(el('p', p.body)); hit.append(sourceButton(caseId, p)); });
      } else {
        hit.append(el('p', item.text)); hit.append(sourceButton(caseId, item));
        if (item.before || item.after) {
          const details = el('details'); details.append(el('summary', '前后文（同一引用编号）'));
          details.append(el('pre', (item.before ? '上文：' + item.before + '\n\n' : '') + (item.after ? '下文：' + item.after : ''))); hit.append(details);
        }
      }
      node.append(hit);
    });
    return node;
  }
  function rating(caseId, generation) {
    const row = el('div', undefined, 'rating'); const previous = latest(caseId);
    row.append(el('span', previous ? '已保存 · 修订 ' + previous.revision : '你的评价'));
    const note = el('input', undefined, 'rating-note'); note.type = 'text'; note.maxLength = 2000;
    note.placeholder = '评价理由（可选；和所选评价一起保存）'; note.setAttribute('aria-label', '问题 ' + caseId + ' 的评价理由'); note.value = previous?.request.note || '';
    for (const [decision, label] of Object.entries(decisions)) {
      const button = el('button', label); button.setAttribute('aria-pressed', String(previous?.request.decision === decision));
      button.disabled = target === 'answer' && generation?.status !== 'done';
      button.addEventListener('click', () => action(async () => {
        button.disabled = true;
        try {
          await api('runs/' + current.run.id + '/ratings', {case_id: caseId, target, decision, note: note.value, expected_revision: previous?.revision || 0, request_id: crypto.randomUUID()});
          current = await api('runs/' + current.run.id); render(); $('status').textContent = '评价已保存。';
        } catch (e) { current = await api('runs/' + current.run.id); render(); throw e; }
      }));
      row.append(button);
    }
    row.append(note); return row;
  }
  function generationControls(caseData, operation) {
    const box = el('div', undefined, 'generation');
    box.append(el('p', '实验台每题最多调用两次生成适配器，自身不扩词、分批或重试。适配器内部的实际模型请求及费用由其实现决定；不能据此推算线上完整问库费用。'));
    if (operation) {
      box.append(el('p', operation.status === 'done' ? '两侧答案已保存；刷新不会再次调用模型。' : operation.status === 'failed' ? operation.error : '已有生成占位。若进程曾中断，实际调用结果可能未知；不会自动再次调用。'));
    } else if (!state.generator) {
      box.append(el('p', '当前未启用模型。可导出提示词；要在本机生成，启动时需明确配置生成适配器。'));
    } else {
      const plan = state.generation_plan;
      box.append(el('p', '当前模型/适配器：' + state.generator + '。实际费用未知，不等于免费。'));
      if (plan) box.append(el('p', '每题至多 ' + plan.max_model_requests + ' 次模型请求；每侧输出预算 ' + plan.max_output_tokens_per_side + ' tokens（可能含思考）。不自动扩词、分批、重试或扩大预算。网络读写超时 ' + plan.read_timeout_s + ' 秒，非绝对总时限。'));
      if (state.daily_limit != null) box.append(el('p', '每日最多 ' + state.daily_limit + ' 组（UTC）；当前已占用 ' + state.daily_used + ' 组。失败或结果未知也计入额度。'));
      const label = el('label'); const check = el('input'); check.type = 'checkbox';
      label.append(check, document.createTextNode(' 我接受把这两侧资料交给所配置模型，并承担可能的调用费用。')); box.append(label);
      const button = el('button', '生成当前问题的两版回答');
      button.disabled = true;
      check.addEventListener('change', () => { button.disabled = !check.checked; });
      button.addEventListener('click', () => action(async () => {
        button.disabled = true; $('status').textContent = '正在生成。结果会分别保存，刷新不会重发调用。';
        try { await api('runs/' + current.run.id + '/generate', {case_id: caseData.id, acknowledge_cost: true, configuration_id: state.generation_plan?.id || null, request_id: crypto.randomUUID()}); }
        finally { current = await api('runs/' + current.run.id); state = {...state, ...(await api('state'))}; render(); }
      })); box.append(button);
    }
    return box;
  }
  function render() {
    const run = current.run;
    $('export-results').href = apiBase + 'runs/' + run.id + '/export';
    $('run-meta').textContent = '快照 ' + run.snapshot_id.slice(0, 8) + ' · 对照 ' + run.id.slice(0, 8) + ' · ' + (run.material === 'synthetic' ? '合成资料' : '离线资料');
    $('mode-hint').textContent = {
      search: '文章仍按全部词命中优先，再按得分排序。比较的是固定词法候选池的前 20 篇；重叠度不是准确率。',
      retrieval: '比较问库送入提示词的原文及前后文。字符预算、选段与长文封顶会改变资料覆盖；不是线上完整问库回放。',
      answer: '先检查提示词，再手动决定是否生成。不同答案的事实、数字和引用需要人工核对。',
    }[target];
    const metric = current.summary[target]; const counts = metric.counts;
    $('summary').textContent = '人工方向评价 ' + metric.directional_n + ' 题 · A 更好 ' + counts.baseline + ' · B 更好 ' + counts.candidate + ' · 相当 ' + counts.tie + ' · 都不好 ' + counts.neither + ' · 跳过 ' + counts.skip + ' · 无法判断 ' + counts.uncertain + ' · 已撤销 ' + counts.withdraw + (metric.candidate_preference == null ? ' · 尚无方向偏好' : ' · B 偏好比例 ' + (metric.candidate_preference * 100).toFixed(0) + '%（仅方向题）');
    $('case-results').replaceChildren();
    for (const c of run.cases) {
      const node = el('article', undefined, 'case'); const head = el('div', undefined, 'case-head');
      const text = el('div'); text.append(el('h3', c.query)); text.append(el('p', '范围：' + c.scope.source_ids.join('、') + (c.scope.article_id ? ' · 文章 ' + c.scope.article_id : ''), 'hint'));
      head.append(text, el('p', 'Top 10 集合重叠：' + (c.search_overlap_at_10 == null ? '无结果' : Math.round(c.search_overlap_at_10 * 100) + '%'), 'hint')); node.append(head);
      const generation = current.generations[c.id];
      if (target === 'answer') node.append(generationControls(c, generation));
      const grid = el('div', undefined, 'side-grid');
      for (const side of ['baseline', 'candidate']) grid.append(material(c.id, c[side], side, generation));
      node.append(grid, rating(c.id, generation)); $('case-results').append(node);
    }
  }
  $('compare').addEventListener('click', () => action(async () => {
    const config = request(); $('status').textContent = '正在使用冻结资料比较，两侧均不调用模型…';
    const run = await api('compare', config); await loadRun(run.id);
    setRequest(run.request); refreshHistory((await api('state')).history); $('status').textContent = '检索对照已保存，本步骤不调用模型。';
    $('results').scrollIntoView({behavior: 'auto', block: 'start'});
  }));
  $('copy-baseline').addEventListener('click', () => { try { setValues('candidate', values('baseline')); } catch (e) { fail(e); } });
  $('reset-baseline').addEventListener('click', () => setValues('baseline', state.defaults));
  $('export-config').addEventListener('click', () => { try { download(request(), 'algorithm-parameters.json'); } catch (e) { fail(e); } });
  $('import-config').addEventListener('change', () => action(async () => {
    const file = $('import-config').files[0]; if (!file) return;
    if (file.size > 65536) throw new Error('参数文件过大');
    const value = JSON.parse(await file.text());
    if (!value.baseline || !value.candidate || !Array.isArray(value.case_ids) || value.case_ids.some(id => !state.cases.some(c => c.id === id))) throw new Error('参数文件格式或问题编号不匹配');
    setRequest(value); $('status').textContent = '参数已导入；运行前仍由服务端校验。';
  }));
  $('history').addEventListener('change', () => action(async () => { if ($('history').value) { await loadRun($('history').value); setRequest(current.run.request); } }));
  for (const button of document.querySelectorAll('[data-target]')) button.addEventListener('click', () => {
    target = button.dataset.target;
    for (const tab of document.querySelectorAll('[data-target]')) tab.setAttribute('aria-selected', String(tab === button));
    if (current) render();
  });
  const tabs = [...document.querySelectorAll('[data-target]')];
  for (const [index, tab] of tabs.entries()) {
    tab.tabIndex = index === 0 ? 0 : -1;
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); tabs[next].click(); tabs[next].focus(); }
    });
    tab.addEventListener('click', () => { for (const item of tabs) item.tabIndex = item === tab ? 0 : -1; });
  }
  for (const button of document.querySelectorAll('[data-preset]')) button.addEventListener('click', () => {
    try {
      const value = values('candidate');
      if (button.dataset.preset === 'title') { value.title_tf = 1; value.title_bonus = 0.8; }
      else value.coverage_power = 3;
      setValues('candidate', value);
    } catch (e) { fail(e); }
  });
  $('close-source').addEventListener('click', () => $('source-dialog').close());
  action(async () => {
    state = await api('state');
    $('notice').textContent = (state.material === 'synthetic' ? '合成演示资料 · 以下人物与文章均为虚构，仅验证功能，不代表真实搜索质量。' : '离线私有快照 · 来源权限在本机检查；原文不会从主站实时更新。') + ' 冻结于 ' + state.created_at.slice(0, 10) + '。';
    for (const c of state.cases) {
      const label = el('label', undefined, 'question'); const input = el('input'); input.type = 'checkbox'; input.value = c.id; input.checked = true;
      const text = el('span', c.query); text.append(el('span', c.scope.source_ids.join(' · '), 'hint'));
      label.append(input, text); $('questions').append(label);
    }
    makeFields('baseline'); makeFields('candidate'); refreshHistory(state.history);
    if (state.history.length) $('results').hidden = false;
    const key = location.hash.match(/^#run=([a-f0-9]{64})$/)?.[1] || state.history[0]?.id;
    if (key) { await loadRun(key); setRequest(current.run.request); refreshHistory(state.history); }
    $('status').textContent = '就绪。选择问题并调整候选参数。';
  });
})();
