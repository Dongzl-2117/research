// 私人资料只存在本机 IndexedDB。每一条判断和自选都保存完整修订序列，回看不会读到后来的版本。
window.ResearchJournal = (() => {
  const STORE = 'records';
  const FORMAT = 'research-journal';
  const fields = {
    company: 100, companyId: 20, hypothesis: 12000, falsifier: 12000, review: 10,
    ref: 4000, excerpt: 4000, metric: 160, unit: 60, period: 100, expected: 250,
    actual: 250, published: 24, filing: 4000, change: 4000
  };
  function requireValue(ok, message) { if (!ok) throw new Error(message); }
  function iso(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value)) return false;
    const date = new Date(value);
    return Number.isFinite(date.getTime()) && date.toISOString() === value;
  }
  function validDay(value) { return !value || iso(value + 'T00:00:00.000Z'); }
  function watchId(name) { return 'watch:' + name.trim().normalize('NFKC').toLowerCase(); }
  function link(value, articleAllowed = false) {
    if (!value) return true;
    if (articleAllowed && /^\/articles\/\d+(?:\/(?:zh|original))?(?:[?#][^\s]*)?$/.test(value)) return true;
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password;
    } catch { return false; }
  }
  function values(kind, input, at) {
    requireValue(input && typeof input === 'object', '记录字段不完整。');
    const source = kind === 'watch' ? {company: 100, companyId: 20} : fields;
    const result = {};
    for (const [name, max] of Object.entries(source)) {
      requireValue(typeof input[name] === 'string' && input[name].length <= max, '字段缺失或超过长度限制：' + name);
      result[name] = input[name].trim();
    }
    requireValue(result.company && (!result.companyId || /^[1-9]\d*$/.test(result.companyId)), '请填写公司名称或研究主题。');
    if (kind === 'watch') {
      requireValue(typeof input.active === 'boolean', '自选状态不完整。');
      return {...result, active: input.active};
    }
    requireValue(result.hypothesis, '请填写个人假设。');
    requireValue(validDay(result.review), '复核日期无效。');
    requireValue(link(result.ref, true) && link(result.filing), '请使用站内文章链接或完整的 HTTP(S) 资料链接。');
    if (result.expected || result.actual) {
      requireValue(result.metric && result.unit && result.period, '有预期或实际值时，请填齐指标、单位和报告期。');
    }
    if (result.actual || result.published || result.filing) {
      requireValue(result.actual && iso(result.published) && result.filing, '实绩须同时记录实际值、公告发布时间和一手披露链接。');
      requireValue(result.published <= at, '公告发布时间晚于本次记录时间，不能作为已知实绩。');
    }
    return result;
  }
  function validate(record, now) {
    requireValue(record && ['note', 'watch'].includes(record.kind) && typeof record.id === 'string', '备份包含未知记录。');
    if (record.kind === 'note') requireValue(/^note:[0-9a-f-]{36}$/.test(record.id), '判断编号无效。');
    requireValue(Array.isArray(record.revisions) && record.revisions.length > 0, '缺少修订历史。');
    let previous = '';
    const revisions = record.revisions.map(item => {
      requireValue(item && iso(item.at) && item.at > previous && item.at <= now, '修订时间无效、倒序或晚于当前时间。');
      previous = item.at;
      const value = values(record.kind, item.value, item.at);
      if (record.kind === 'watch') requireValue(record.id === watchId(value.company), '自选编号与名称不符。');
      const revision = {at: item.at, value};
      if (item.secFact !== undefined) {
        const source = item.secFact;
        requireValue(record.kind === 'note' && source && typeof source.cik === 'string' && /^\d{10}$/.test(source.cik) &&
          /^[\w.-]+:[\w.-]+$/.test(source.tag) && /^\d{10}-\d{2}-\d{6}$/.test(source.accession) &&
          typeof source.form === 'string' && source.form.length > 0 && source.form.length <= 60 &&
          typeof source.filed === 'string' && source.filed && validDay(source.filed), 'SEC 来源标识不完整。');
        const filing = 'https://www.sec.gov/Archives/edgar/data/' + Number(source.cik) + '/' +
          source.accession.replaceAll('-', '') + '/' + source.accession + '-index.htm';
        requireValue(value.actual && value.filing === filing, 'SEC 申报链接与来源标识不符。');
        revision.secFact = {cik: source.cik, tag: source.tag, accession: source.accession,
          form: source.form, filed: source.filed};
      }
      return revision;
    });
    return {id: record.id, kind: record.kind, revisions};
  }

  function open() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(FORMAT, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE, {keyPath: 'id'});
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('请关闭其他旧版本手记标签页并重试。'));
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => db.close();
        resolve(db);
      };
    });
  }
  function transaction(db, mode, read, apply) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const store = tx.objectStore(STORE);
      const request = read(store);
      let result, problem;
      request.onsuccess = () => {
        try { result = apply(request.result, store); }
        catch (error) { problem = error; tx.abort(); }
      };
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(problem || tx.error || new Error('本机保存失败。'));
    });
  }
  function all(db) {
    return transaction(db, 'readonly', store => store.getAll(), rows => rows);
  }
  function save(db, id, kind, head, input) {
    return transaction(db, 'readwrite', store => store.get(id), (current, store) => {
      const revisions = current?.revisions || [];
      const last = revisions.at(-1)?.at || '';
      requireValue(last === head, '其他标签页或导入操作已更新这条记录。请重新打开最新版本再修改。');
      const at = new Date().toISOString();
      requireValue(!last || last < at, '本机时间没有晚于上一版，请检查时钟后重试。');
      const record = validate({id, kind, revisions: [...revisions, {at, value: input}]}, at);
      store.put(record);
      return record;
    });
  }
  function backup(records) { return {format: FORMAT, version: 1, records}; }
  function restore(db, document) {
    requireValue(document?.format === FORMAT && document.version === 1 && Array.isArray(document.records), '不支持的备份格式或版本。');
    const now = new Date().toISOString();
    const incoming = document.records.map(row => validate(row, now));
    requireValue(new Set(incoming.map(row => row.id)).size === incoming.length, '备份内出现重复编号。');
    return transaction(db, 'readwrite', store => store.getAll(), (current, store) => {
      const existing = new Map(current.map(row => [row.id, row]));
      let restored = 0;
      for (const row of incoming) {
        const stored = existing.get(row.id);
        if (stored) {
          requireValue(row.kind === stored.kind, '备份与本机记录类型冲突。');
          const shared = Math.min(row.revisions.length, stored.revisions.length);
          let addedSource = false;
          for (let i = 0; i < shared; i++) {
            const local = stored.revisions[i], incoming = row.revisions[i];
            if (JSON.stringify(local) === JSON.stringify(incoming)) continue;
            // 旧版 v1 导出会丢弃可选的 SEC 标识。时间和完整判断相同就保留已有标识。
            const sameValue = JSON.stringify(local.value) === JSON.stringify(incoming.value);
            if (local.at === incoming.at && sameValue && Boolean(local.secFact) !== Boolean(incoming.secFact)) {
              if (local.secFact) incoming.secFact = local.secFact;
              else { local.secFact = incoming.secFact; addedSource = true; }
              continue;
            }
            // 同一份旧备份可重复生成同一申报补丁，两个生成时刻不同。保留本机首次入账时间。
            const duplicateFiling = i === row.revisions.length - 1 && Boolean(local.secFact && incoming.secFact) &&
              JSON.stringify(local.secFact) === JSON.stringify(incoming.secFact) &&
              sameValue;
            requireValue(duplicateFiling, '修订历史发生冲突，整份备份未导入。请分别保留两份备份。');
          }
          if (row.revisions.length <= stored.revisions.length) {
            if (addedSource) { store.put(stored); restored++; }
            continue;
          }
        }
        store.put(row);
        restored++;
      }
      return restored;
    });
  }
  return {open, all, save, backup, restore, watchId, fields};
})();
