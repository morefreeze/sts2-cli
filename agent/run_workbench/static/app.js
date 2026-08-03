'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';
const TECHNICAL_STATUSES = new Set(['crash', 'timeout', 'stuck', 'reset_failure', 'invalid']);
const SOURCE_LABELS = {
  native_run: '原生游戏记录',
  replay_jsonl: '回放日志',
  deck_history: '牌组历史',
  eval_results: '评估结果',
  summary: '汇总记录',
  unknown: '未知格式',
};
const FUNNEL_LABELS = {
  all_runs: '全部记录',
  floor_bearing: '有推进层数',
  act1_boss_or_later: '第一幕 Boss',
  act2_entry: '进入第二幕',
  act2_boss_or_later: '第二幕 Boss',
  act3_entry: '进入第三幕',
  completion: '通关',
};
const STATUS_LABELS = {
  win: '胜利',
  dead: '正常结束',
  crash: '崩溃',
  timeout: '超时',
  stuck: '卡死',
  reset_failure: '重置失败',
  invalid: '无效记录',
  in_progress: '进行中',
  unknown: '未知',
};
const CAPABILITY_LABELS = {
  full_map: '完整地图分支',
  visited_route: '已走路线',
  node_rewards: '节点收益',
  final_inventory: '最终牌组与遗物',
  decisions: '决策记录',
  turn_replay: '回合回放',
};

const state = {
  cohorts: [],
  sources: [],
  currentMetrics: null,
  busy: false,
};

const byId = (id) => document.getElementById(id);

function element(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.attrs) {
    Object.entries(options.attrs).forEach(([name, value]) => {
      if (value !== undefined && value !== null) node.setAttribute(name, String(value));
    });
  }
  return node;
}

function svgElement(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, String(value)));
  return node;
}

function clear(node) {
  node.replaceChildren();
}

function setStatus(message, tone = 'ready') {
  const node = byId('workbenchStatus');
  node.textContent = message;
  node.dataset.tone = tone;
}

function setBusy(isBusy) {
  state.busy = isBusy;
  byId('dashboardMain').setAttribute('aria-busy', String(isBusy));
  ['currentCohort', 'baselineCohort', 'characterFilter', 'versionFilter',
    'validityFilter', 'sourceFile', 'reloadButton'].forEach((id) => {
    byId(id).disabled = isBusy;
  });
}

async function getJSON(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (error) {
    throw new Error(`服务返回了无法识别的内容（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function formatMissing(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(digits).replace(/\.0$/, '');
  }
  return String(value);
}

function formatRate(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(Number(value) * 100).toFixed(1).replace(/\.0$/, '')}%`;
}

function formatBytes(value) {
  if (typeof value !== 'number' || value < 0) return '—';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(value) {
  if (typeof value !== 'number') return '时间未知';
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN');
}

function setMetric(id, value, subtext) {
  byId(id).textContent = value;
  const sub = document.querySelector(`[data-subtext-for="${id}"]`);
  if (sub) sub.textContent = subtext;
}

function setSelectOptions(select, options, emptyLabel, preferred) {
  clear(select);
  if (emptyLabel !== null) {
    select.append(element('option', { text: emptyLabel, attrs: { value: '' } }));
  }
  options.forEach((option) => {
    select.append(element('option', {
      text: option.label,
      attrs: { value: option.value },
    }));
  });
  if (preferred && options.some((option) => option.value === preferred)) {
    select.value = preferred;
  }
}

function filterValue(cohort, key) {
  const value = cohort.filters && cohort.filters[key];
  return value === null || value === undefined || value === '' ? '未标注' : String(value);
}

function populateAxisFilter(id, key, allLabel) {
  const select = byId(id);
  const previous = select.value;
  const values = Array.from(new Set(state.cohorts.map((cohort) => filterValue(cohort, key)))).sort();
  setSelectOptions(select, values.map((value) => ({ value, label: value })), allLabel, previous);
}

function filteredCohorts() {
  const character = byId('characterFilter').value;
  const version = byId('versionFilter').value;
  const validity = byId('validityFilter').value;
  return state.cohorts.filter((cohort) => {
    if (character && filterValue(cohort, 'character') !== character) return false;
    if (version && filterValue(cohort, 'game_version') !== version) return false;
    const technical = Number(cohort.technical_count || 0);
    const gameplay = Number(cohort.run_count || 0) - technical;
    if (validity === 'valid' && gameplay <= 0) return false;
    if (validity === 'technical' && technical <= 0) return false;
    return true;
  });
}

function comparisonCompatible(current, candidate) {
  if (!current || !candidate || current.cohort_id === candidate.cohort_id) return false;
  const keys = ['character', 'game_version', 'evaluation_mode', 'scenario', 'ascension'];
  return keys.every((key) => {
    const currentValue = current.filters ? current.filters[key] : null;
    const candidateValue = candidate.filters ? candidate.filters[key] : null;
    return currentValue === candidateValue;
  });
}

function updateCohortOptions({ chooseDefaults = false } = {}) {
  const currentSelect = byId('currentCohort');
  const baselineSelect = byId('baselineCohort');
  const previousCurrent = currentSelect.value;
  const previousBaseline = baselineSelect.value;
  const candidates = filteredCohorts();
  const options = candidates.map((cohort) => ({
    value: cohort.cohort_id,
    label: `${cohort.label} · ${cohort.run_count} 局`,
  }));
  let current = previousCurrent;
  if (!candidates.some((cohort) => cohort.cohort_id === current)) {
    current = candidates.length ? candidates[candidates.length - 1].cohort_id : '';
  }
  if (chooseDefaults && candidates.length) current = candidates[candidates.length - 1].cohort_id;
  setSelectOptions(currentSelect, options, candidates.length ? null : '没有匹配批次', current);
  currentSelect.value = current;

  const currentCohort = candidates.find((cohort) => cohort.cohort_id === current);
  let baseline = previousBaseline;
  if (!candidates.some((cohort) => cohort.cohort_id === baseline && baseline !== current)) {
    const compatible = [...candidates].reverse().find((cohort) => comparisonCompatible(currentCohort, cohort));
    baseline = compatible ? compatible.cohort_id : '';
  }
  if (chooseDefaults) {
    const compatible = [...candidates].reverse().find((cohort) => comparisonCompatible(currentCohort, cohort));
    baseline = compatible ? compatible.cohort_id : '';
  }
  const baselineOptions = options.filter((option) => option.value !== current);
  setSelectOptions(baselineSelect, baselineOptions, '不比较基线', baseline);
  baselineSelect.value = baseline;
}

function resetMetrics() {
  setMetric('avgFloor', '—', '没有可用批次');
  setMetric('medianFloor', '—', '没有可用批次');
  setMetric('maxFloor', '—', '没有可用批次');
  setMetric('act2Rate', '—', '没有可用批次');
  setMetric('validCount', '—', '没有可用批次');
  setMetric('technicalCount', '—', '没有可用批次');
  renderEmpty(byId('trendChart'), '没有可绘制的推进记录。');
  renderEmpty(byId('funnelChart'), '没有可计算的推进漏斗。');
  renderComparison(null);
  renderAnomalies(null);
  renderRepresentatives(null);
}

function renderSummary(summary) {
  setMetric('avgFloor', formatMissing(summary.avg_global_floor),
    `已知层数 ${summary.floor_n} / 有效对局 ${summary.valid_n}`);
  setMetric('medianFloor', formatMissing(summary.median_global_floor),
    `层数口径 ${summary.floor_n} 条`);
  setMetric('maxFloor', formatMissing(summary.max_global_floor),
    `最远值来自 ${summary.floor_n} 条已知层数`);
  setMetric('act2Rate', formatRate(summary.act2_entry_rate),
    `${summary.act2_entry_n} / ${summary.act2_entry_denominator} 条可判断记录`);
  setMetric('validCount', formatMissing(summary.valid_n, 0),
    `${summary.valid_n} / ${summary.all_n} 条全部记录`);
  setMetric('technicalCount', formatMissing(summary.technical_n, 0),
    `${summary.technical_n} / ${summary.all_n} 条全部记录，未混入平均值`);
}

function renderEmpty(container, message, className = 'empty-state') {
  clear(container);
  container.append(element('div', { className, text: message }));
}

function renderTrend(summary) {
  const container = byId('trendChart');
  clear(container);
  const trend = Array.isArray(summary.trend) ? summary.trend : [];
  if (!trend.length) {
    renderEmpty(container, '当前批次没有趋势点。');
    return;
  }
  const available = trend.filter((point) => typeof point.global_floor === 'number');
  const missing = trend.filter((point) => typeof point.global_floor !== 'number');
  const technical = trend.filter((point) => TECHNICAL_STATUSES.has(point.status));
  if (!available.length) {
    renderEmpty(container, `共 ${trend.length} 条记录，但都缺少推进层数。`);
    return;
  }

  const width = 760;
  const height = 230;
  const margin = { top: 20, right: 18, bottom: 34, left: 42 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maxFloor = Math.max(1, ...available.map((point) => point.global_floor));
  const x = (index) => margin.left + (trend.length === 1 ? plotWidth / 2 : index * plotWidth / (trend.length - 1));
  const y = (value) => margin.top + plotHeight - (value / maxFloor) * plotHeight;
  const svg = svgElement('svg', {
    class: 'chart-svg', viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-labelledby': 'trendTitle trendDescription',
  });
  const title = svgElement('title', { id: 'trendTitle' });
  title.textContent = '当前批次每局最远推进层数';
  const description = svgElement('desc', { id: 'trendDescription' });
  description.textContent = `${trend.length} 局中 ${available.length} 局有层数，${missing.length} 局缺失层数，${summary.technical_n} 局为技术失败。`;
  svg.append(title, description);

  [0, 0.5, 1].forEach((ratio) => {
    const lineY = margin.top + plotHeight * ratio;
    svg.append(svgElement('line', {
      x1: margin.left, y1: lineY, x2: width - margin.right, y2: lineY,
      class: ratio === 1 ? 'chart-axis' : 'chart-gridline',
    }));
    const label = svgElement('text', { x: margin.left - 8, y: lineY + 3, class: 'chart-label', 'text-anchor': 'end' });
    label.textContent = String(Math.round(maxFloor * (1 - ratio)));
    svg.append(label);
  });

  let segment = [];
  const appendSegment = () => {
    if (segment.length > 1) {
      svg.append(svgElement('polyline', { points: segment.join(' '), class: 'chart-line' }));
    }
    segment = [];
  };
  trend.forEach((point, index) => {
    if (typeof point.global_floor !== 'number') {
      appendSegment();
      return;
    }
    const pointX = x(index);
    const pointY = y(point.global_floor);
    segment.push(`${pointX},${pointY}`);
    const circle = svgElement('circle', {
      cx: pointX, cy: pointY, r: 4.5, class: 'chart-point', tabindex: '0',
      'aria-label': `${point.run_id}，推进到 ${point.global_floor} 层，${STATUS_LABELS[point.status] || point.status}`,
    });
    circle.addEventListener('click', () => openRun(point.run_id));
    circle.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openRun(point.run_id);
      }
    });
    svg.append(circle);
  });
  appendSegment();
  const firstLabel = svgElement('text', { x: margin.left, y: height - 10, class: 'chart-label' });
  firstLabel.textContent = '较早';
  const lastLabel = svgElement('text', { x: width - margin.right, y: height - 10, class: 'chart-label', 'text-anchor': 'end' });
  lastLabel.textContent = '较新';
  svg.append(firstLabel, lastLabel);
  container.append(svg);

  const legend = element('div', { className: 'chart-legend' });
  legend.append(
    element('span', { className: 'legend-key', text: `${available.length} 条有效层数` }),
    element('span', { className: 'legend-key missing', text: `${missing.length} 条缺失层数` }),
    element('span', { className: 'legend-key technical', text: `${summary.technical_n} 条技术失败` }),
  );
  container.append(legend);

  if (missing.length || technical.length) {
    const notes = element('ul', { className: 'chart-notes' });
    missing.slice(0, 5).forEach((point) => {
      notes.append(element('li', { text: `${point.run_id}：缺少推进层数（${STATUS_LABELS[point.status] || point.status}）` }));
    });
    technical.slice(0, 5).forEach((point) => {
      notes.append(element('li', { text: `${point.run_id}：技术失败 ${STATUS_LABELS[point.status] || point.status}` }));
    });
    container.append(notes);
  }
}

function renderFunnel(summary) {
  const container = byId('funnelChart');
  clear(container);
  const funnel = Array.isArray(summary.funnel) ? summary.funnel : [];
  if (!funnel.length) {
    renderEmpty(container, '当前批次没有漏斗数据。');
    return;
  }
  funnel.forEach((point) => {
    const row = element('div', { className: 'funnel-row' });
    const label = element('span', { text: FUNNEL_LABELS[point.key] || point.key });
    const track = element('div', { className: 'funnel-track', attrs: { 'aria-hidden': 'true' } });
    const fill = element('div', { className: 'funnel-fill' });
    const percent = typeof point.rate === 'number' ? Math.max(0, Math.min(100, point.rate * 100)) : 0;
    fill.style.width = `${percent}%`;
    track.append(fill);
    const value = element('strong', {
      className: 'funnel-value',
      text: `${point.count} / ${point.denominator} · ${formatRate(point.rate)}`,
    });
    row.append(label, track, value);
    container.append(row);
  });
}

function appendList(container, values) {
  const list = element('ul');
  values.forEach((value) => list.append(element('li', { text: value })));
  container.append(list);
}

function deltaText(value, rate = false) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return { text: '—（数据不足）', direction: 'missing' };
  }
  const numeric = Number(value);
  const rendered = rate ? `${Math.abs(numeric * 100).toFixed(1).replace(/\.0$/, '')} 个百分点` : formatMissing(Math.abs(numeric));
  if (numeric > 0) return { text: `提升 ${rendered}`, direction: 'up' };
  if (numeric < 0) return { text: `下降 ${rendered}`, direction: 'down' };
  return { text: '持平', direction: 'flat' };
}

function renderComparison(comparison) {
  const banner = byId('comparisonBanner');
  const title = byId('comparisonTitle');
  const body = banner.querySelector('[data-comparison-body]');
  clear(body);
  banner.dataset.tone = 'neutral';
  if (!comparison) {
    title.textContent = '未选择基线';
    body.append(element('p', { text: '当前只展示本批次结果；选择基线后由服务端检查口径并计算差异。' }));
    return;
  }
  const reasons = Array.isArray(comparison.mismatch_reasons) ? comparison.mismatch_reasons : [];
  const notes = Array.isArray(comparison.notes) ? comparison.notes : [];
  if (!comparison.comparable) {
    title.textContent = '当前与基线不可直接比较';
    banner.dataset.tone = 'warning';
    appendList(body, reasons.length ? reasons : ['服务端未提供可比原因。']);
    if (notes.length) appendList(body, notes);
    return;
  }

  title.textContent = comparison.paired ? '同种子配对比较' : '口径一致的批次比较';
  const deltas = [
    ['平均推进', comparison.avg_global_floor_delta, false],
    ['中位推进', comparison.median_global_floor_delta, false],
    ['最远房间', comparison.max_global_floor_delta, false],
    ['进入第二幕', comparison.act2_entry_rate_delta, true],
    ['胜率', comparison.win_rate_delta, true],
  ];
  const grid = element('div', { className: 'delta-list' });
  let positive = 0;
  let negative = 0;
  deltas.forEach(([label, value, rate]) => {
    const delta = deltaText(value, rate);
    if (delta.direction === 'up') positive += 1;
    if (delta.direction === 'down') negative += 1;
    const chip = element('div', { className: 'delta-chip', attrs: { 'data-direction': delta.direction } });
    chip.append(element('span', { text: `${label} ` }), element('strong', { text: delta.text }));
    grid.append(chip);
  });
  body.append(grid);
  if (reasons.length) appendList(body, reasons);
  if (notes.length) appendList(body, notes);
  banner.dataset.tone = positive > negative ? 'good' : negative > positive ? 'warning' : 'neutral';
}

function anomalyRow(item) {
  const row = element('div', { className: 'list-row' });
  const main = element('div', { className: 'list-row-main' });
  const marker = element('span', {
    className: 'anomaly-marker', text: item.priority === 0 ? '!' : 'i',
    attrs: { 'data-priority': item.priority === 0 ? 'high' : 'normal', 'aria-hidden': 'true' },
  });
  const content = element('div');
  content.append(element('h3', { text: item.title }), element('p', { text: item.detail }));
  main.append(marker, content);
  row.append(main);
  if (item.sourceId) {
    const button = element('button', { text: '查看来源', attrs: { type: 'button' } });
    button.addEventListener('click', () => openSource(item.sourceId));
    row.append(button);
  }
  return row;
}

function renderAnomalies(metrics) {
  const container = byId('anomalyList');
  clear(container);
  const items = [];
  if (metrics) {
    const summary = metrics.current;
    if (summary.technical_n > 0) {
      items.push({ priority: 0, title: `${summary.technical_n} 局技术失败`, detail: '崩溃、超时、卡死等记录已与正常游戏结果分开。' });
    }
    const missingFloors = Math.max(0, Number(summary.valid_n || 0) - Number(summary.valid_floor_n || 0));
    if (missingFloors > 0) {
      items.push({ priority: 1, title: `${missingFloors} 局缺少推进层数`, detail: '这些有效对局未进入平均值、中位数和 Act 2 分母。' });
    }
    if (metrics.comparison && !metrics.comparison.comparable) {
      (metrics.comparison.mismatch_reasons || []).forEach((reason) => {
        items.push({ priority: 1, title: '比较口径不一致', detail: reason });
      });
    }
  }
  state.sources.forEach((source) => {
    (source.errors || []).forEach((error) => {
      items.push({ priority: 0, title: `来源读取失败：${source.display_name}`, detail: error, sourceId: source.source_id });
    });
  });
  items.sort((a, b) => a.priority - b.priority || a.title.localeCompare(b.title, 'zh-CN') || a.detail.localeCompare(b.detail, 'zh-CN'));
  if (!items.length) {
    renderEmpty(container, '当前 API 摘要和来源目录没有报告异常。');
    return;
  }
  items.forEach((item) => container.append(anomalyRow(item)));
}

function currentCohortDescriptor() {
  const id = byId('currentCohort').value;
  return state.cohorts.find((cohort) => cohort.cohort_id === id) || null;
}

function representativeCandidates(metrics, descriptor) {
  const trend = metrics && metrics.current && Array.isArray(metrics.current.trend) ? metrics.current.trend : [];
  const candidates = [];
  const add = (point, reason) => {
    if (!point || candidates.some((item) => item.point.run_id === point.run_id)) return;
    candidates.push({ point, reason });
  };
  const floorPoints = trend.filter((point) => typeof point.global_floor === 'number');
  add(trend[trend.length - 1], '最近一局');
  add([...floorPoints].sort((a, b) => b.global_floor - a.global_floor || a.run_id.localeCompare(b.run_id))[0], '推进最远');
  add([...floorPoints].sort((a, b) => a.global_floor - b.global_floor || a.run_id.localeCompare(b.run_id))[0], '推进最浅');
  add(trend.find((point) => typeof point.global_floor !== 'number'), '层数缺失');
  if (!candidates.length && descriptor) {
    (descriptor.run_ids || []).slice(0, 3).forEach((runId) => add({ run_id: runId, global_floor: null, status: 'unknown' }, '批次样本'));
  }
  return candidates.slice(0, 5);
}

function renderRepresentatives(metrics) {
  const container = byId('representativeRuns');
  clear(container);
  const descriptor = currentCohortDescriptor();
  if (!descriptor) {
    renderEmpty(container, '选择批次后可抽查代表性对局。');
    return;
  }
  representativeCandidates(metrics, descriptor).forEach(({ point, reason }) => {
    const row = element('div', { className: 'list-row' });
    const content = element('div');
    content.append(
      element('h3', { text: `${reason} · ${point.run_id}` }),
      element('p', { text: `推进 ${formatMissing(point.global_floor)} · ${STATUS_LABELS[point.status] || point.status || '状态未知'}` }),
    );
    const button = element('button', { text: '查看对局', attrs: { type: 'button' } });
    button.addEventListener('click', () => openRun(point.run_id));
    row.append(content, button);
    container.append(row);
  });
  (descriptor.source_refs || []).slice(0, 3).forEach((sourceId) => {
    const source = state.sources.find((candidate) => candidate.source_id === sourceId);
    const row = element('div', { className: 'list-row' });
    const content = element('div');
    content.append(
      element('h3', { text: source ? source.display_name : sourceId }),
      element('p', { text: source ? `${SOURCE_LABELS[source.source_kind] || source.source_kind} · ${source.record_count} 条记录` : '批次来源' }),
    );
    const button = element('button', { text: '查看来源', attrs: { type: 'button' } });
    button.addEventListener('click', () => openSource(sourceId));
    row.append(content, button);
    container.append(row);
  });
  if (!container.childElementCount) renderEmpty(container, '当前批次没有可定位的对局或来源。');
}

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
    identity.append(
      element('h3', { text: source.display_name }),
      element('p', { text: `${source.record_count} 条记录 · ${formatBytes(source.size)} · ${formatTime(source.mtime)}` }),
    );
    if (source.errors && source.errors.length) {
      identity.append(element('p', { text: source.errors.join('；') }));
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
      element('span', { text: source.errors && source.errors.length ? `${source.errors.length} 个错误` : '无目录错误' }),
    );
    const button = element('button', { text: source.open_mode === 'error' ? '查看错误' : '查看', attrs: { type: 'button' } });
    button.addEventListener('click', () => openSource(source.source_id));
    row.append(identity, kind, metadata, button);
    container.append(row);
  });
}

function appendKeyValues(container, values) {
  const list = element('dl', { className: 'key-values' });
  Object.entries(values).forEach(([label, value]) => {
    list.append(element('dt', { text: label }), element('dd', { text: formatMissing(value) }));
  });
  container.append(list);
}

function appendErrors(container, errors) {
  if (!Array.isArray(errors) || !errors.length) return;
  const section = element('section', { className: 'detail-section' });
  section.append(element('h3', { text: '来源提示与错误' }));
  appendList(section, errors);
  container.append(section);
}

function renderCanonicalRun(container, run, index = null) {
  const section = element('section', { className: 'detail-section' });
  section.append(element('h3', { text: index === null ? `对局 ${run.run_id || '未标注'}` : `对局 ${index + 1} · ${run.run_id || '未标注'}` }));
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
  const mapAvailable = run.capabilities && (run.capabilities.full_map || run.capabilities.visited_route);
  capabilitySection.append(element('p', {
    className: 'section-note',
    text: mapAvailable
      ? '该来源含地图能力；本阶段先展示规范化摘要，完整地图视图将在后续功能中呈现。'
      : '该来源不含可靠地图路线；不会伪造未记录的分支或收益。',
  }));
  section.append(capabilitySection);
  if (Array.isArray(run.warnings) && run.warnings.length) appendList(section, run.warnings);
  container.append(section);
}

function renderDetail(payload, fallbackTitle = '来源详情') {
  const panel = byId('detailPanel');
  const body = byId('detailBody');
  const title = byId('detailTitle');
  clear(body);
  title.textContent = (payload.source && payload.source.display_name) || payload.source_name || fallbackTitle;
  appendErrors(body, payload.errors);
  if (payload.view === 'summary') {
    const section = element('section', { className: 'detail-section' });
    section.append(
      element('h3', { text: '汇总记录' }),
      element('p', { text: '这是聚合结果，不具备可下钻的单局路线。' }),
    );
    const pre = element('pre', { text: JSON.stringify(payload.summary || {}, null, 2) });
    section.append(pre);
    body.append(section);
  } else if (payload.view === 'run' || payload.view === 'runs') {
    const runs = payload.run ? [payload.run] : (payload.runs || []);
    runs.forEach((run, index) => renderCanonicalRun(body, run, runs.length === 1 ? null : index));
    if (payload.progress && Array.isArray(payload.progress.rooms)) {
      body.append(element('p', { className: 'section-note', text: `旧回放解析器识别到 ${payload.progress.rooms.length} 个已访问房间。` }));
    }
  } else {
    renderEmpty(body, (payload.errors || ['无法解析该来源。']).join('；'), 'error-state');
  }
  panel.setAttribute('aria-hidden', 'false');
  byId('closeDetail').focus();
}

function closeDetail() {
  byId('detailPanel').setAttribute('aria-hidden', 'true');
}

async function openSource(sourceId) {
  setStatus('正在读取来源…', 'busy');
  try {
    const payload = await getJSON(`/api/source?id=${encodeURIComponent(sourceId)}`);
    renderDetail(payload, '来源详情');
    setStatus('已载入');
  } catch (error) {
    renderDetail({ view: 'error', errors: [error.message] }, '来源读取失败');
    setStatus(`来源读取失败：${error.message}`, 'error');
  }
}

async function openRun(runId) {
  setStatus('正在读取对局…', 'busy');
  try {
    const payload = await getJSON(`/api/run?id=${encodeURIComponent(runId)}`);
    renderDetail(payload, `对局 ${runId}`);
    setStatus('已载入');
  } catch (error) {
    renderDetail({ view: 'error', errors: [error.message] }, `对局 ${runId}`);
    setStatus(`对局读取失败：${error.message}`, 'error');
  }
}

async function refreshMetrics() {
  const current = byId('currentCohort').value;
  const baseline = byId('baselineCohort').value;
  if (!current) {
    state.currentMetrics = null;
    resetMetrics();
    setStatus(state.cohorts.length ? '当前筛选没有匹配批次' : '已载入，但没有可统计的训练批次');
    return;
  }
  setBusy(true);
  setStatus('正在计算训练进度…', 'busy');
  try {
    const query = new URLSearchParams({ current });
    if (baseline) query.set('baseline', baseline);
    const metrics = await getJSON(`/api/metrics?${query.toString()}`);
    state.currentMetrics = metrics;
    renderSummary(metrics.current);
    renderTrend(metrics.current);
    renderFunnel(metrics.current);
    renderComparison(metrics.comparison);
    renderAnomalies(metrics);
    renderRepresentatives(metrics);
    setStatus('已载入');
  } catch (error) {
    state.currentMetrics = null;
    resetMetrics();
    setStatus(`训练指标读取失败：${error.message}`, 'error');
  } finally {
    setBusy(false);
  }
}

async function uploadSelectedFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  setBusy(true);
  setStatus(`正在解析 ${file.name}…`, 'busy');
  try {
    const text = await file.text();
    const payload = await getJSON('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_name: file.name, text }),
    });
    if (payload.view === 'run' || payload.view === 'runs' || payload.view === 'summary') {
      renderDetail(payload, file.name);
    } else {
      renderDetail({ ...payload, view: 'error' }, file.name);
    }
    setStatus(`已载入 ${file.name}`);
  } catch (error) {
    const result = error.payload && error.payload.result;
    renderDetail(result || { view: 'error', source_name: file.name, errors: [error.message] }, file.name);
    setStatus(`${file.name} 解析失败：${error.message}`, 'error');
  } finally {
    event.target.value = '';
    setBusy(false);
  }
}

async function bootstrap() {
  setBusy(true);
  setStatus('正在读取训练记录…', 'busy');
  renderEmpty(byId('sourceCatalog'), '正在分类训练记录…', 'loading-state');
  try {
    const [{ cohorts }, { sources }] = await Promise.all([getJSON('/api/cohorts'), getJSON('/api/catalog')]);
    state.cohorts = Array.isArray(cohorts) ? cohorts : [];
    state.sources = Array.isArray(sources) ? sources : [];
    populateAxisFilter('characterFilter', 'character', '全部角色');
    populateAxisFilter('versionFilter', 'game_version', '全部版本');
    updateCohortOptions({ chooseDefaults: true });
    renderCatalog();
    renderAnomalies(null);
    setBusy(false);
    await refreshMetrics();
  } catch (error) {
    state.cohorts = [];
    state.sources = [];
    updateCohortOptions();
    resetMetrics();
    renderEmpty(byId('sourceCatalog'), `来源目录读取失败：${error.message}`, 'error-state');
    setStatus(`工作台载入失败：${error.message}`, 'error');
    setBusy(false);
  }
}

function filterChanged() {
  updateCohortOptions();
  refreshMetrics();
}

byId('characterFilter').addEventListener('change', filterChanged);
byId('versionFilter').addEventListener('change', filterChanged);
byId('validityFilter').addEventListener('change', filterChanged);
byId('currentCohort').addEventListener('change', () => {
  updateCohortOptions();
  refreshMetrics();
});
byId('baselineCohort').addEventListener('change', refreshMetrics);
byId('sourceFile').addEventListener('change', uploadSelectedFile);
byId('reloadButton').addEventListener('click', bootstrap);
byId('closeDetail').addEventListener('click', closeDetail);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && byId('detailPanel').getAttribute('aria-hidden') === 'false') closeDetail();
});

bootstrap();
