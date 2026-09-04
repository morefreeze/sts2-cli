'use strict';

// Bootstrap, hash router, shared app state, the source-catalog panel and the
// detail drawer. View-specific rendering lives in tree.js / cohort-view.js /
// runs-table.js / run-view.js; map.js renders the act map unchanged.

const SOURCE_ERROR_EXAMPLE_LIMIT = 3;
const SERVER_PARSE_BODY_MAX_BYTES = 128 * 1024 * 1024;
const FILE_UPLOAD_MAX_BYTES = 32 * 1024 * 1024;

const state = {
  tree: [],
  cohorts: [],
  sources: [],
  selectedCohortId: '',
  currentMetrics: null,
  busy: false,
  detailRequestToken: 0,
  detailAbortController: null,
  detailOpener: null,
  uploadRequestToken: 0,
  catalogOpen: false,
  currentRunReplay: null,   // replay_by_node of the open run; see run-view.js
};

// ---------------------------------------------------------------------
// Hash router: #/  |  #/batch/<cohort_id>  |  #/batch/<cohort_id>/run/<kind>:<id>
// ---------------------------------------------------------------------

function cohortRoute(cohortId) {
  return `#/batch/${encodeURIComponent(cohortId)}`;
}

function runRoute(cohortId, ref) {
  const kind = ref && ref.kind === 'source' ? 'source' : 'run';
  const id = ref && typeof ref.id === 'string' ? ref.id : '';
  return `#/batch/${encodeURIComponent(cohortId || '-')}/run/${kind}:${encodeURIComponent(id)}`;
}

function parseRoute(hash) {
  const raw = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
  if (!raw || raw === '/') return { view: 'root' };
  const match = raw.match(/^\/batch\/([^/]+)(?:\/run\/([a-zA-Z0-9_-]+):(.+))?$/);
  if (!match) return { view: 'root' };
  const cohortId = decodeURIComponent(match[1]);
  if (match[2] && match[3] !== undefined) {
    return { view: 'run', cohortId, ref: { kind: match[2], id: decodeURIComponent(match[3]) } };
  }
  return { view: 'batch', cohortId };
}

function navigate(hash, { replace = false } = {}) {
  const route = parseRoute(hash);
  // map.js's closeMapPage() checks history.state.view === 'run' to decide
  // whether history.back() has somewhere of ours to land, rather than
  // duplicating this router's route-parsing.
  if (replace) history.replaceState({ view: route.view }, '', hash);
  else history.pushState({ view: route.view }, '', hash);
  applyRoute(route);
}

function firstCohortIdInTree(tree) {
  if (!Array.isArray(tree)) return '';
  for (const version of tree) {
    const characters = Array.isArray(version.characters) ? version.characters : [];
    for (const character of characters) {
      const cohorts = Array.isArray(character.cohorts) ? character.cohorts : [];
      if (cohorts.length && typeof cohorts[0].cohort_id === 'string') return cohorts[0].cohort_id;
    }
  }
  return '';
}

function showBatchView() {
  state.catalogOpen = false;
  byId('catalogToggle').setAttribute('aria-pressed', 'false');
  byId('batchView').hidden = false;
  byId('catalogView').hidden = true;
}

function showCatalogView() {
  state.catalogOpen = true;
  byId('catalogToggle').setAttribute('aria-pressed', 'true');
  byId('batchView').hidden = true;
  byId('catalogView').hidden = false;
}

async function applyRoute(route) {
  // showDashboardPage lives inside map.js's IIFE, so it is reachable only
  // through the STS2Map namespace -- a bare `typeof showDashboardPage` here
  // is always 'undefined' and silently leaves the map page covering the
  // batch view on the way back from a run.
  if (window.STS2Map && typeof window.STS2Map.showDashboardPage === 'function') {
    window.STS2Map.showDashboardPage();
  }
  if (route.view === 'root') {
    const firstId = firstCohortIdInTree(state.tree);
    if (firstId) {
      navigate(cohortRoute(firstId), { replace: true });
      return;
    }
    showBatchView();
    state.selectedCohortId = '';
    Tree.setSelected('');
    CohortView.renderNoCohort();
    return;
  }
  if (route.view === 'batch') {
    showBatchView();
    state.selectedCohortId = route.cohortId;
    Tree.setSelected(route.cohortId);
    await CohortView.render(route.cohortId);
    return;
  }
  if (route.view === 'run') {
    if (route.cohortId && route.cohortId !== '-') state.selectedCohortId = route.cohortId;
    Tree.setSelected(state.selectedCohortId);
    await RunView.render(route.cohortId, route.ref);
  }
}

window.addEventListener('popstate', () => applyRoute(parseRoute(location.hash)));

// ---------------------------------------------------------------------
// Source catalog panel (topbar toggle swaps the content pane).
// ---------------------------------------------------------------------

function renderCatalog() {
  const container = byId('sourceCatalog');
  clear(container);
  if (!state.sources.length) {
    renderEmpty(container, '当前目录没有发现 .run、.json 或 .jsonl 训练记录。');
    return;
  }
  state.sources.forEach((source) => {
    const row = element('article', { className: 'catalog-row' });
    const identity = element('div');
    const errorCount = Number.isFinite(source.error_count)
      ? source.error_count
      : (Array.isArray(source.errors) ? source.errors.length : 0);
    identity.append(
      element('h3', { text: source.display_name }),
      element('p', { text: `${source.record_count} 条记录 · ${formatBytes(source.size)} · ${formatTime(source.mtime)}` }),
    );
    if (source.errors && source.errors.length) {
      const omitted = Number.isFinite(source.errors_omitted) ? source.errors_omitted : Math.max(0, errorCount - source.errors.length);
      identity.append(element('p', { text: `错误示例：${source.errors.join('；')}${omitted ? `；另有 ${omitted} 个未返回` : ''}` }));
    }
    const kind = element('span', {
      className: 'badge', text: SOURCE_LABELS[source.source_kind] || `未知类型（${source.source_kind || '未标注'}）`,
      attrs: { 'data-kind': source.open_mode === 'error' ? 'error' : source.source_kind },
    });
    const metadata = element('div', { className: 'catalog-meta' });
    const completeness = source.metadata_completeness || {};
    const score = typeof completeness.score === 'number' ? `${Math.round(completeness.score * 100)}%` : '—';
    metadata.append(
      element('span', { text: `打开方式：${source.open_mode || '未知'}` }),
      element('span', { text: `元数据：${score}` }),
      element('span', { text: errorCount ? `${errorCount} 个目录问题${source.errors_complete === false ? '（仅显示样本）' : ''}` : '无目录错误' }),
    );
    const button = element('button', { text: source.open_mode === 'error' ? '查看错误' : '查看', attrs: { type: 'button' } });
    button.addEventListener('click', (event) => openSource(source.source_id, event.currentTarget));
    row.append(identity, kind, metadata, button);
    container.append(row);
  });
}

// ---------------------------------------------------------------------
// Detail drawer (source / uploaded-file inspection).
// ---------------------------------------------------------------------

function appendList(container, values) {
  const list = element('ul');
  values.forEach((value) => list.append(element('li', { text: value })));
  container.append(list);
}

function appendKeyValues(container, values) {
  const list = element('dl', { className: 'key-values' });
  Object.entries(values).forEach(([label, value]) => {
    list.append(element('dt', { text: label }), element('dd', { text: formatMissing(value) }));
  });
  container.append(list);
}

function appendErrors(container, errors, source = null) {
  if (!Array.isArray(errors) || !errors.length) return;
  const section = element('section', { className: 'detail-section' });
  section.append(element('h3', { text: '来源提示与错误' }));
  const errorCount = source && Number.isFinite(source.error_count) ? source.error_count : errors.length;
  const omitted = source && Number.isFinite(source.errors_omitted) ? source.errors_omitted : Math.max(0, errorCount - errors.length);
  section.append(element('p', {
    text: `显示 ${errors.length} / ${errorCount} 个目录问题${omitted ? `，另有 ${omitted} 个未返回` : ''}。`,
  }));
  appendList(section, errors);
  container.append(section);
}

function runHasMapCapability(value) {
  const run = value && value.run ? value.run : value;
  const capabilities = run && run.capabilities;
  return Boolean(capabilities && (capabilities.full_map || capabilities.visited_route));
}

function renderCanonicalRun(container, run, index = null) {
  const section = element('section', { className: 'detail-section' });
  const heading = element('div', { className: 'detail-run-heading' });
  const mapAvailable = runHasMapCapability(run);
  heading.append(element('h3', { text: index === null ? `对局 ${run.run_id || '未标注'}` : `对局 ${index + 1} · ${run.run_id || '未标注'}` }));
  if (mapAvailable && typeof run.run_id === 'string' && run.run_id.trim()) {
    const mapButton = element('button', { text: '查看地图', attrs: { type: 'button' } });
    mapButton.addEventListener('click', (event) => openRun(run.run_id, event.currentTarget));
    heading.append(mapButton);
  }
  section.append(heading);
  appendKeyValues(section, {
    '状态': STATUS_LABELS[run.outcome && run.outcome.status] || (run.outcome && run.outcome.status),
    '最远推进': run.outcome && (run.outcome.max_floor_label || run.outcome.max_global_floor),
    '角色': run.metadata && run.metadata.character,
    '种子': run.metadata && run.metadata.seed,
    '游戏版本': run.metadata && run.metadata.game_version,
    '训练检查点': run.metadata && run.metadata.checkpoint,
    '评估模式': run.metadata && run.metadata.evaluation_mode,
    '记录覆盖': run.coverage && run.coverage.complete_run ? '完整对局' : '部分记录',
  });
  const capabilitySection = element('div', { className: 'detail-section' });
  capabilitySection.append(element('h3', { text: '可下钻能力' }));
  const grid = element('div', { className: 'capability-grid' });
  Object.entries(CAPABILITY_LABELS).forEach(([key, label]) => {
    const available = Boolean(run.capabilities && run.capabilities[key]);
    grid.append(element('div', {
      className: 'capability', text: `${available ? '可用' : '缺失'} · ${label}`,
      attrs: { 'data-available': available },
    }));
  });
  capabilitySection.append(grid);
  capabilitySection.append(element('p', {
    className: 'section-note',
    text: mapAvailable
      ? '该来源含地图能力；可先查看地图总览，再选择已访问节点检查收益。'
      : '该来源不含可靠地图路线；不会伪造未记录的分支或收益。',
  }));
  section.append(capabilitySection);
  if (Array.isArray(run.warnings) && run.warnings.length) appendList(section, run.warnings);
  container.append(section);
}

function renderDetail(payload, fallbackTitle = '来源详情', opener = null) {
  const panel = byId('detailPanel');
  const body = byId('detailBody');
  const title = byId('detailTitle');
  clear(body);
  title.textContent = (payload.source && payload.source.display_name) || payload.source_name || fallbackTitle;
  appendErrors(body, payload.errors, payload.source);
  if (payload.view === 'summary') {
    const section = element('section', { className: 'detail-section' });
    const summary = payload.summary || {};
    section.append(
      element('h3', { text: '汇总记录' }),
      element('p', { text: '这是聚合结果，不具备可下钻的单局路线。' }),
    );
    if (summary.records_complete === false) {
      section.append(element('p', {
        text: `大型汇总共 ${formatMissing(summary.record_count, 0)} 条；仅返回前 ${formatMissing(summary.record_sample_limit, 0)} 条样本，抽样方式 ${summary.record_sampling_method || '未标注'}。`,
      }));
    }
    const pre = element('pre', { text: JSON.stringify(summary, null, 2) });
    section.append(pre);
    body.append(section);
  } else if (payload.view === 'run' || payload.view === 'runs') {
    const runs = payload.run ? [payload.run] : (payload.runs || []);
    runs.forEach((run, index) => renderCanonicalRun(body, run, runs.length === 1 ? null : index));
    if (payload.progress && Array.isArray(payload.progress.rooms)) {
      body.append(element('p', { className: 'section-note', text: `旧回放解析器识别到 ${payload.progress.rooms.length} 个已访问房间。` }));
    }
  } else if (payload.view === 'runs_summary') {
    const section = element('section', { className: 'detail-section' });
    const representativeIds = Array.isArray(payload.representative_run_ids)
      ? payload.representative_run_ids.filter((runId) => typeof runId === 'string' && runId.trim())
      : [];
    section.append(
      element('h3', { text: '大型来源摘要' }),
      element('p', { text: `该来源约含 ${formatMissing(payload.run_count, 0)} 局；完整对局列表状态：${payload.runs_complete === false ? '未展开' : '已返回'}。` }),
      element('p', { text: `仅返回代表性对局 ID ${representativeIds.length} 个，避免在浏览器展开大型来源。` }),
    );
    representativeIds.forEach((runId) => {
      const row = element('div', { className: 'list-row' });
      row.append(element('span', { text: runId }));
      const button = element('button', { text: '查看对局', attrs: { type: 'button' } });
      button.addEventListener('click', (event) => openRun(runId, event.currentTarget));
      row.append(button);
      section.append(row);
    });
    if (!representativeIds.length) {
      section.append(element('p', { text: 'API 未提供可定位的代表性对局 ID。' }));
    }
    body.append(section);
  } else {
    renderEmpty(body, (payload.errors || ['无法解析该来源。']).join('；'), 'error-state');
  }
  showDetailPanel(opener);
}

function beginDetailRequest(opener = null) {
  state.detailRequestToken += 1;
  if (state.detailAbortController) state.detailAbortController.abort();
  const controller = new AbortController();
  state.detailAbortController = controller;
  const panel = byId('detailPanel');
  const candidate = opener && opener.isConnected ? opener : document.activeElement;
  if (candidate && candidate.isConnected && !panel.contains(candidate)) {
    state.detailOpener = candidate;
  }
  return { token: state.detailRequestToken, signal: controller.signal };
}

function isCurrentDetailRequest(token) {
  return token === state.detailRequestToken;
}

function showDetailPanel(opener = null) {
  const panel = byId('detailPanel');
  if (opener && opener.isConnected && !panel.contains(opener)) state.detailOpener = opener;
  panel.hidden = false;
  panel.inert = false;
  panel.setAttribute('aria-hidden', 'false');
  byId('closeDetail').focus();
}

function isFocusable(candidate) {
  if (!candidate || !candidate.isConnected || typeof candidate.focus !== 'function') return false;
  if (typeof candidate.closest !== 'function'
    || candidate.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
  if ((typeof candidate.matches === 'function' && candidate.matches(':disabled'))
    || (typeof candidate.hasAttribute === 'function' && candidate.hasAttribute('disabled'))) return false;
  const style = window.getComputedStyle(candidate);
  return style.display !== 'none' && style.visibility !== 'hidden';
}

function closeDetail() {
  const panel = byId('detailPanel');
  state.detailRequestToken += 1;
  if (state.detailAbortController) state.detailAbortController.abort();
  state.detailAbortController = null;
  const opener = state.detailOpener;
  state.detailOpener = null;
  panel.setAttribute('aria-hidden', 'true');
  panel.inert = true;
  panel.hidden = true;
  if (isFocusable(opener)) opener.focus();
  else byId('contentPane').focus();
}

function focusableDetailElements() {
  const panel = byId('detailPanel');
  const selector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  return Array.from(panel.querySelectorAll(selector)).filter(isFocusable);
}

function handleDetailKeydown(event) {
  const panel = byId('detailPanel');
  if (panel.hidden || panel.inert || panel.getAttribute('aria-hidden') !== 'false') return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeDetail();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusables = focusableDetailElements();
  if (!focusables.length) {
    event.preventDefault();
    panel.focus();
    return;
  }
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !panel.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

async function openSource(sourceId, opener = null) {
  if (!sourceId) {
    setStatus('无法打开来源：缺少来源 ID', 'error');
    return;
  }
  const { token, signal } = beginDetailRequest(opener);
  setStatus('正在读取来源…', 'busy');
  try {
    const payload = await getJSON(`/api/source?id=${encodeURIComponent(sourceId)}`, { signal });
    if (!isCurrentDetailRequest(token)) return;
    state.detailAbortController = null;
    renderDetail(payload, '来源详情', opener);
    setStatus('已载入');
  } catch (error) {
    if (!isCurrentDetailRequest(token) || error.name === 'AbortError') return;
    state.detailAbortController = null;
    renderDetail({ view: 'error', errors: [error.message] }, '来源读取失败', opener);
    setStatus(`来源读取失败：${error.message}`, 'error');
  }
}

// Navigates to the run-detail route for a bare run_id (trend points, the
// detail drawer's "查看地图"/"查看对局" buttons). The run table addresses
// runs through their `ref` object instead -- see runs-table.js -- because
// real data has run_id: null for every row; this path only ever fires when
// a source actually carries one.
function openRun(runId, opener = null) {
  runId = typeof runId === 'string' ? runId.trim() : '';
  if (!runId) {
    setStatus('无法打开对局：缺少对局 ID', 'error');
    return;
  }
  if (opener && opener.isConnected) state.detailOpener = opener;
  navigate(runRoute(state.selectedCohortId, { kind: 'run', id: runId }));
}

async function uploadSelectedFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  const opener = event.currentTarget || event.target;
  // A full-run replay is what this control exists to open: one real Act 3 clear
  // measures ~13.4 MiB, so the old 1 MiB cap rejected exactly the runs worth
  // inspecting. The 6x expansion the old bound assumed came from Python's
  // json.dumps(ensure_ascii=True) escaping non-ASCII as \uXXXX. The browser's
  // serializer does NOT do that -- it emits UTF-8 raw and escapes only quote,
  // backslash and control chars. Measured on two real replays: body 1.125x file.
  // 32 MiB x 3 (pathological guard) plus envelope stays under the 128 MiB cap.
  if (file.size > FILE_UPLOAD_MAX_BYTES) {
    const message = `${file.name} 超过本地载入上限 ${formatBytes(FILE_UPLOAD_MAX_BYTES)}；为避免请求膨胀，未读取文件内容。`;
    const { token } = beginDetailRequest(opener);
    if (isCurrentDetailRequest(token)) {
      state.detailAbortController = null;
      renderDetail({ view: 'error', source_name: file.name, errors: [message] }, file.name, opener);
      setStatus(message, 'error');
    }
    event.target.value = '';
    return;
  }
  state.uploadRequestToken += 1;
  const uploadToken = state.uploadRequestToken;
  const { token, signal } = beginDetailRequest(opener);
  setBusy(true);
  setStatus(`正在解析 ${file.name}…`, 'busy');
  try {
    const text = await file.text();
    if (!isCurrentDetailRequest(token)) return;
    const payload = await getJSON('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_name: file.name, text }),
      signal,
    });
    if (!isCurrentDetailRequest(token)) return;
    state.detailAbortController = null;
    if (payload.view === 'run' || payload.view === 'runs' || payload.view === 'summary' || payload.view === 'runs_summary') {
      renderDetail(payload, file.name, opener);
    } else {
      renderDetail({ ...payload, view: 'error' }, file.name, opener);
    }
    setStatus(`已载入 ${file.name}`);
  } catch (error) {
    if (!isCurrentDetailRequest(token) || error.name === 'AbortError') return;
    state.detailAbortController = null;
    const result = error.payload && error.payload.result;
    renderDetail(result || { view: 'error', source_name: file.name, errors: [error.message] }, file.name, opener);
    setStatus(`${file.name} 解析失败：${error.message}`, 'error');
  } finally {
    event.target.value = '';
    if (uploadToken === state.uploadRequestToken) setBusy(false);
  }
}

// ---------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------

async function bootstrap() {
  setBusy(true);
  setStatus('正在读取训练记录…', 'busy');
  renderEmpty(byId('sourceCatalog'), '正在分类训练记录…', 'loading-state');
  try {
    const [{ tree }, { cohorts }, { sources }] = await Promise.all([
      getJSON('/api/tree'),
      getJSON('/api/cohorts'),
      getJSON('/api/catalog'),
    ]);
    state.tree = Array.isArray(tree) ? tree : [];
    state.cohorts = Array.isArray(cohorts) ? cohorts : [];
    state.sources = Array.isArray(sources) ? sources : [];
    Tree.render(state.tree);
    renderCatalog();
    setBusy(false);
    await applyRoute(parseRoute(location.hash));
    setStatus('已载入');
  } catch (error) {
    state.tree = [];
    state.cohorts = [];
    state.sources = [];
    Tree.render([]);
    renderEmpty(byId('sourceCatalog'), `来源目录读取失败：${error.message}`, 'error-state');
    setStatus(`工作台载入失败：${error.message}`, 'error');
    setBusy(false);
  }
}

async function reloadAll() {
  const previousHash = location.hash;
  await bootstrap();
  if (previousHash && previousHash !== location.hash) navigate(previousHash, { replace: true });
}

byId('catalogToggle').addEventListener('click', () => {
  if (state.catalogOpen) {
    showBatchView();
    setStatus('已返回批次视图');
  } else {
    showCatalogView();
    setStatus('已显示来源目录');
  }
});
byId('baselineCohort').addEventListener('change', () => {
  CohortView.baselineChanged();
});
byId('sourceFile').addEventListener('change', uploadSelectedFile);
byId('reloadButton').addEventListener('click', reloadAll);
byId('closeDetail').addEventListener('click', closeDetail);
document.addEventListener('keydown', handleDetailKeydown);

const skipLink = document.querySelector('.skip-link');
if (skipLink) {
  skipLink.addEventListener('click', (event) => {
    // This router owns location.hash. Letting the anchor navigate would write
    // '#contentPane', which parseRoute cannot match, so the user would be
    // bounced back to the first batch instead of jumping to the content.
    event.preventDefault();
    const pane = byId('contentPane');
    pane.classList.add('skip-focus');
    pane.addEventListener('blur', () => pane.classList.remove('skip-focus'), { once: true });
    pane.focus();
  });
}

// app.js is a deferred script that runs before tree.js / cohort-view.js /
// runs-table.js / run-view.js / map.js have been evaluated, so bootstrap must
// not start here -- it would race those namespaces into existence and only
// happen to win because it awaits the network first. DOMContentLoaded fires
// after every deferred script has run.
document.addEventListener('DOMContentLoaded', bootstrap);
