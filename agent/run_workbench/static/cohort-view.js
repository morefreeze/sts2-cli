'use strict';

// Batch detail view: metric cards, trend, funnel, comparison banner and
// anomalies for the cohort selected in the left tree. The 对比基线 select
// lives here too -- it is what the old global 基线批次 dropdown became,
// scoped to the one cohort currently open and restricted to cohorts that
// share its comparison_readiness.comparison_signature.

const CLIENT_TREND_POINT_LIMIT = 256;

window.CohortView = (() => {
  function safeCohortId(cohort) {
    try {
      if (!cohort || typeof cohort !== 'object') return '';
      const value = cohort.cohort_id;
      return typeof value === 'string' ? value.trim() : '';
    } catch (error) {
      return '';
    }
  }

  function currentCohortDescriptor() {
    const id = typeof state.selectedCohortId === 'string' ? state.selectedCohortId.trim() : '';
    if (!id || !Array.isArray(state.cohorts)) return null;
    return state.cohorts.find((cohort) => safeCohortId(cohort) === id) || null;
  }

  // ---- 对比基线 -------------------------------------------------------

  function defaultBaselineCohortId(current, candidates) {
    let currentId;
    let baselineId;
    try {
      if (!current || typeof current !== 'object') return '';
      const readiness = current.comparison_readiness;
      if (!readiness || typeof readiness !== 'object' || readiness.ready !== true) return '';
      if (typeof current.default_baseline_cohort_id !== 'string') return '';
      currentId = safeCohortId(current);
      baselineId = current.default_baseline_cohort_id.trim();
    } catch (error) {
      return '';
    }
    if (!currentId || !baselineId || baselineId === currentId || !Array.isArray(candidates)) return '';

    let matches = 0;
    try {
      for (const candidate of candidates) {
        try {
          if (safeCohortId(candidate) === baselineId) matches += 1;
        } catch (error) {
          // Ignore malformed candidate descriptors and fail closed on ambiguity below.
        }
        if (matches > 1) return '';
      }
    } catch (error) {
      return '';
    }
    return matches === 1 ? baselineId : '';
  }

  function comparisonAxisLabel(axis) {
    const labels = {
      character: '角色',
      game_version: '游戏版本',
      evaluation_mode: '评测模式',
      scenario: '场景',
      ascension: '进阶',
      seed: '种子',
      valid_results: '有效结果',
    };
    if (typeof axis !== 'string') return '未知轴';
    return Object.prototype.hasOwnProperty.call(labels, axis) ? labels[axis] : axis;
  }

  // Restrict candidates to cohorts sharing the current cohort's
  // comparison_readiness.comparison_signature -- the server's own axis
  // check -- instead of re-deriving per-axis mismatches on the client.
  function baselineCandidates(current) {
    let signature = null;
    try {
      if (!current || typeof current !== 'object') return [];
      const readiness = current.comparison_readiness;
      signature = readiness && typeof readiness === 'object' ? readiness.comparison_signature : null;
    } catch (error) {
      return [];
    }
    if (typeof signature !== 'string' || !signature) return [];
    const currentId = safeCohortId(current);
    if (!Array.isArray(state.cohorts)) return [];
    return state.cohorts.filter((cohort) => {
      const id = safeCohortId(cohort);
      if (!id || id === currentId) return false;
      try {
        const readiness = cohort.comparison_readiness;
        return Boolean(readiness && typeof readiness === 'object' && readiness.comparison_signature === signature);
      } catch (error) {
        return false;
      }
    });
  }

  function updateBaselineHelp(current, baselineId) {
    const baselineHelp = byId('baselineHelp');
    if (!current) {
      baselineHelp.textContent = '当前没有可查看的训练批次';
      return;
    }
    let readiness = null;
    let serverDefaultId = '';
    try {
      readiness = current.comparison_readiness;
      serverDefaultId = typeof current.default_baseline_cohort_id === 'string'
        ? current.default_baseline_cohort_id.trim()
        : '';
    } catch (error) {
      readiness = null;
      serverDefaultId = '';
    }
    const labels = (key) => {
      let axes;
      try {
        axes = readiness && readiness[key];
      } catch (error) {
        return [];
      }
      if (!Array.isArray(axes)) return [];
      return Array.from(new Set(
        axes.filter((axis) => typeof axis === 'string').map(comparisonAxisLabel),
      ));
    };
    const missing = labels('missing_axes');
    const mixed = labels('mixed_axes');
    const invalid = labels('invalid_axes');
    const issues = [
      missing.length ? `缺少${missing.join('、')}` : '',
      mixed.length ? `混合${mixed.join('、')}` : '',
      invalid.length ? `无效${invalid.join('、')}` : '',
    ].filter(Boolean);

    if (!readiness || readiness.ready !== true) {
      const detail = issues.length ? `：${issues.join('；')}` : '';
      baselineHelp.textContent = `元数据不完整，仅展示本批次${detail}`;
    } else if (!baselineId) {
      baselineHelp.textContent = '当前批次可查看，但暂无可直接比较的基线';
    } else if (baselineId === serverDefaultId) {
      baselineHelp.textContent = '已采用服务端验证的兼容基线；手动选择后仍会再次校验';
    } else {
      baselineHelp.textContent = '已选择基线；服务端将校验口径并提供精确原因';
    }
  }

  // forceDefault is true exactly when the selected cohort itself changed
  // (see render() below) -- a manual baseline choice is preserved across
  // any other re-render (e.g. a metrics-only refresh) as long as it is
  // still a valid candidate, but re-defaults whenever the batch changes,
  // even if the previous choice happens to still be selectable.
  function renderBaselineSelect(current, { forceDefault = false } = {}) {
    const select = byId('baselineCohort');
    const previous = select.value;
    const candidates = baselineCandidates(current);
    const options = candidates.map((cohort) => ({
      value: safeCohortId(cohort),
      label: `${cohort.label} · ${cohort.run_count} 局 · ${Number.isFinite(cohort.latest_at) ? formatTime(cohort.latest_at) : '时间未知'}`,
    }));
    let baseline = previous;
    if (forceDefault || !options.some((option) => option.value === baseline)) {
      baseline = defaultBaselineCohortId(current, candidates);
    }
    setSelectOptions(select, options, '不比较基线', baseline);
    select.value = baseline;
    updateBaselineHelp(current, baseline);
    return baseline;
  }

  function baselineChanged() {
    updateBaselineHelp(currentCohortDescriptor(), byId('baselineCohort').value);
    refreshMetrics(state.selectedCohortId);
  }

  // ---- Metric cards / trend / funnel / comparison / anomalies --------

  function setMetric(id, value, subtext) {
    byId(id).textContent = value;
    const sub = document.querySelector(`[data-subtext-for="${id}"]`);
    if (sub) sub.textContent = subtext;
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

  function boundedTimestampedTrend(points, limit = CLIENT_TREND_POINT_LIMIT) {
    const boundedLimit = Math.max(1, Math.floor(limit) || 1);
    let timestampedInputN = 0;
    for (const point of points) {
      if (point && Number.isFinite(point.timestamp)) timestampedInputN += 1;
    }
    if (timestampedInputN === 0) return { points: [], timestampedInputN: 0 };

    const selectedN = Math.min(timestampedInputN, boundedLimit);
    const targetIndexes = [];
    for (let index = 0; index < selectedN; index += 1) {
      const target = selectedN === 1
        ? 0
        : Math.round(index * (timestampedInputN - 1) / (selectedN - 1));
      targetIndexes.push(target);
    }
    const selected = [];
    let finiteIndex = 0;
    let targetIndex = 0;
    for (const point of points) {
      if (!point || !Number.isFinite(point.timestamp)) continue;
      if (finiteIndex === targetIndexes[targetIndex]) {
        selected.push(point);
        targetIndex += 1;
      }
      finiteIndex += 1;
      if (targetIndex >= targetIndexes.length) break;
    }
    return { points: selected, timestampedInputN };
  }

  function renderTrendProvenance(container, summary, renderedN, timestampedInputN) {
    const eligibleN = Number.isFinite(summary.trend_eligible_n) ? summary.trend_eligible_n : timestampedInputN;
    const timestampedN = Number.isFinite(summary.trend_timestamped_n) ? summary.trend_timestamped_n : timestampedInputN;
    const unknownTimeN = Number.isFinite(summary.trend_unknown_time_n) ? summary.trend_unknown_time_n : Math.max(0, eligibleN - timestampedN);
    const serverSampledN = Number.isFinite(summary.trend_sampled_n) ? summary.trend_sampled_n : timestampedInputN;
    const serverLimit = Number.isFinite(summary.trend_sample_limit) ? summary.trend_sample_limit : '—';
    const methods = {
      all_timestamped: '全部有时间记录',
      deterministic_hash: '服务端确定性抽样',
    };
    const method = methods[summary.trend_sampling_method] || summary.trend_sampling_method || '未标注';
    const legend = element('div', {
      className: 'chart-legend',
      attrs: { 'aria-label': '趋势抽样口径' },
    });
    legend.append(
      element('span', { className: 'legend-key', text: `绘制 ${renderedN} 点（前端上限 ${CLIENT_TREND_POINT_LIMIT}）` }),
      element('span', { className: 'legend-key missing', text: `服务端样本 ${serverSampledN} / ${timestampedN} 个有时间记录（上限 ${serverLimit}）` }),
      element('span', { className: 'legend-key technical', text: `总趋势口径 ${eligibleN}；${unknownTimeN} 个时间未知未绘制` }),
      element('span', { text: `抽样方式：${method}${timestampedInputN > renderedN ? `；前端等距再抽样 ${renderedN} / ${timestampedInputN}` : ''}` }),
    );
    container.append(legend);
  }

  function renderTrend(summary) {
    const container = byId('trendChart');
    clear(container);
    const rawTrend = Array.isArray(summary.trend) ? summary.trend : [];
    const bounded = boundedTimestampedTrend(rawTrend);
    const trend = bounded.points;
    if (!trend.length) {
      renderEmpty(container, rawTrend.length ? '趋势点缺少有效时间，未绘制到时间轴。' : '当前批次没有有时间记录的趋势点。');
      renderTrendProvenance(container, summary, 0, bounded.timestampedInputN);
      return;
    }
    const available = trend.filter((point) => Number.isFinite(point.global_floor));
    const missing = trend.filter((point) => !Number.isFinite(point.global_floor));
    const technical = trend.filter((point) => TECHNICAL_STATUSES.has(point.status));
    if (!available.length) {
      renderEmpty(container, `绘制样本共 ${trend.length} 条，但都缺少推进层数。`);
      renderTrendProvenance(container, summary, trend.length, bounded.timestampedInputN);
      return;
    }

    const width = 760;
    const height = 230;
    const margin = { top: 20, right: 18, bottom: 34, left: 42 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    let maxFloor = 1;
    for (const point of available) {
      if (point.global_floor > maxFloor) maxFloor = point.global_floor;
    }
    const x = (index) => margin.left + (trend.length === 1 ? plotWidth / 2 : index * plotWidth / (trend.length - 1));
    const y = (value) => margin.top + plotHeight - (value / maxFloor) * plotHeight;
    const svg = svgElement('svg', {
      class: 'chart-svg', viewBox: `0 0 ${width} ${height}`,
      role: 'img', 'aria-labelledby': 'trendTitle trendDescription',
    });
    const title = svgElement('title', { id: 'trendTitle' });
    title.textContent = '当前批次有时间记录的最远推进层数样本';
    const description = svgElement('desc', { id: 'trendDescription' });
    description.textContent = `前端绘制 ${trend.length} 个有时间样本，其中 ${available.length} 个有层数、${missing.length} 个缺少层数。服务端有时间记录 ${summary.trend_timestamped_n} 个，时间未知 ${summary.trend_unknown_time_n} 个未绘制。`;
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
      if (!Number.isFinite(point.global_floor)) {
        appendSegment();
        return;
      }
      const pointX = x(index);
      const pointY = y(point.global_floor);
      segment.push(`${pointX},${pointY}`);
      const runId = typeof point.run_id === 'string' ? point.run_id.trim() : '';
      const runLabel = runId || '未提供对局 ID';
      const attributes = {
        cx: pointX, cy: pointY, r: 4.5, class: 'chart-point',
        role: runId ? 'button' : 'img',
        'aria-label': `${runLabel}，推进到 ${point.global_floor} 层，${STATUS_LABELS[point.status] || point.status}`,
      };
      if (runId) attributes.tabindex = '0';
      const circle = svgElement('circle', attributes);
      if (runId) {
        circle.addEventListener('click', (event) => openRun(runId, event.currentTarget));
        circle.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openRun(runId, event.currentTarget);
          }
        });
      }
      svg.append(circle);
    });
    appendSegment();
    const firstLabel = svgElement('text', { x: margin.left, y: height - 10, class: 'chart-label' });
    firstLabel.textContent = '较早';
    const lastLabel = svgElement('text', { x: width - margin.right, y: height - 10, class: 'chart-label', 'text-anchor': 'end' });
    lastLabel.textContent = '较新';
    svg.append(firstLabel, lastLabel);
    container.append(svg);
    renderTrendProvenance(container, summary, trend.length, bounded.timestampedInputN);

    if (missing.length || technical.length) {
      const notes = element('ul', { className: 'chart-notes' });
      missing.slice(0, 5).forEach((point) => {
        notes.append(element('li', { text: `${point.run_id || '未提供对局 ID'}：缺少推进层数（${STATUS_LABELS[point.status] || point.status}）` }));
      });
      technical.slice(0, 5).forEach((point) => {
        notes.append(element('li', { text: `${point.run_id || '未提供对局 ID'}：技术失败 ${STATUS_LABELS[point.status] || point.status}` }));
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
    const width = 560;
    const rowHeight = 40;
    const height = Math.max(150, funnel.length * rowHeight + 26);
    const barX = 150;
    const barWidth = 190;
    const svg = svgElement('svg', {
      class: 'funnel-svg', viewBox: `0 0 ${width} ${height}`,
      role: 'img', 'aria-labelledby': 'funnelTitle funnelDescription',
    });
    const title = svgElement('title', { id: 'funnelTitle' });
    title.textContent = '当前批次推进转化漏斗';
    const description = svgElement('desc', { id: 'funnelDescription' });
    description.textContent = funnel.map((point) => {
      const label = FUNNEL_LABELS[point.key] || point.key;
      return `${label}：${point.count} / ${point.denominator}，${formatRate(point.rate)}`;
    }).join('；');
    svg.append(title, description);

    funnel.forEach((point, index) => {
      const centerY = 21 + index * rowHeight;
      const finiteRate = Number.isFinite(point.rate);
      const percent = finiteRate ? Math.max(0, Math.min(1, point.rate)) : 0;
      const label = svgElement('text', {
        x: 0, y: centerY + 4, class: 'funnel-label',
      });
      label.textContent = FUNNEL_LABELS[point.key] || point.key;
      const track = svgElement('rect', {
        x: barX, y: centerY - 7, width: barWidth, height: 12,
        rx: 6, class: 'funnel-track',
      });
      const fill = svgElement('rect', {
        x: barX, y: centerY - 7, width: barWidth * percent, height: 12,
        rx: 6, class: 'funnel-fill',
      });
      const value = svgElement('text', {
        x: barX + barWidth + 14, y: centerY + 4, class: 'funnel-value',
      });
      value.textContent = `${point.count} / ${point.denominator} · ${formatRate(point.rate)}`;
      svg.append(label, track, fill, value);
    });
    container.append(svg);
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
      const current = currentCohortDescriptor();
      const readiness = current && current.comparison_readiness;
      if (readiness && readiness.ready === false) {
        title.textContent = '元数据不完整';
        body.append(element('p', { text: '历史记录仍可查看，但不会用于训练提升比较。' }));
      } else {
        title.textContent = '未选择基线';
        body.append(element('p', { text: '当前批次可查看，但暂无可直接比较的基线。' }));
      }
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
      button.addEventListener('click', (event) => openSource(item.sourceId, event.currentTarget));
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
          items.push({ priority: 2, title: '比较口径不一致', detail: reason });
        });
      }
    }
    const unknownSources = [];
    const trainingSources = [];
    state.sources.forEach((source) => {
      const errorCount = Number.isFinite(source.error_count)
        ? source.error_count
        : (Array.isArray(source.errors) ? source.errors.length : 0);
      const isUnknown = source.source_kind === 'unknown' || source.open_mode === 'error';
      if (errorCount > 0 || isUnknown) {
        (isUnknown ? unknownSources : trainingSources).push(source);
      }
    });
    const appendSourceGroup = (sources, title) => {
      if (!sources.length) return;
      let errorCount = 0;
      let errorsOmitted = 0;
      const examples = [];
      for (const source of sources) {
        errorCount += Number.isFinite(source.error_count)
          ? source.error_count
          : (Array.isArray(source.errors) ? source.errors.length : 0);
        errorsOmitted += Number.isFinite(source.errors_omitted) ? source.errors_omitted : 0;
        for (const error of (source.errors || [])) {
          if (examples.length >= SOURCE_ERROR_EXAMPLE_LIMIT) break;
          examples.push(`${source.display_name}：${error}`);
        }
      }
      const detailParts = [`${sources.length} 个来源，目录报告 ${errorCount} 个问题`];
      if (examples.length) detailParts.push(`示例：${examples.join('；')}`);
      const hiddenCount = Math.max(errorsOmitted, errorCount - examples.length);
      if (hiddenCount > 0) detailParts.push(`其余 ${hiddenCount} 个未在异常列表展开`);
      items.push({
        priority: 3,
        title,
        detail: detailParts.join('。'),
        sourceId: sources[0].source_id,
      });
    };
    appendSourceGroup(unknownSources, '来源目录问题：未知或不可训练格式');
    appendSourceGroup(trainingSources, '来源目录问题：训练记录读取提示');
    items.sort((a, b) => a.priority - b.priority || a.title.localeCompare(b.title, 'zh-CN') || a.detail.localeCompare(b.detail, 'zh-CN'));
    if (!items.length) {
      renderEmpty(container, '当前 API 摘要和来源目录没有报告异常。');
      return;
    }
    items.forEach((item) => container.append(anomalyRow(item)));
  }

  // ---- Entry points ----------------------------------------------------

  function restoreMetricsFocus(focusOpener, cohortId) {
    if (state.selectedCohortId !== cohortId || !isFocusable(focusOpener)) return;
    const active = document.activeElement;
    if (active === focusOpener) return;
    if (active && active !== document.body && active !== document.documentElement) return;
    focusOpener.focus();
  }

  async function refreshMetrics(cohortId) {
    const baseline = byId('baselineCohort').value;
    const focusOpener = document.activeElement;
    setBusy(true);
    setStatus('正在计算训练进度…', 'busy');
    try {
      const query = new URLSearchParams({ current: cohortId });
      if (baseline) query.set('baseline', baseline);
      const metrics = await getJSON(`/api/metrics?${query.toString()}`);
      if (state.selectedCohortId !== cohortId) return;
      state.currentMetrics = metrics;
      renderSummary(metrics.current);
      renderTrend(metrics.current);
      renderFunnel(metrics.current);
      renderComparison(metrics.comparison);
      renderAnomalies(metrics);
      setStatus('已载入');
    } catch (error) {
      if (state.selectedCohortId !== cohortId) return;
      state.currentMetrics = null;
      resetMetrics();
      setStatus(`训练指标读取失败：${error.message}`, 'error');
    } finally {
      setBusy(false);
      restoreMetricsFocus(focusOpener, cohortId);
    }
  }

  let lastRenderedCohortId = null;

  async function render(cohortId) {
    const current = state.cohorts.find((cohort) => safeCohortId(cohort) === cohortId) || null;
    byId('batchKicker').textContent = current && current.experiment ? current.experiment : '训练批次';
    byId('batchTitle').textContent = current ? current.label : `未找到批次（${cohortId}）`;
    renderBaselineSelect(current, { forceDefault: lastRenderedCohortId !== cohortId });
    lastRenderedCohortId = cohortId;
    await Promise.all([refreshMetrics(cohortId), window.RunsTable.render(cohortId)]);
  }

  // NOT named renderEmpty: util.js exports a renderEmpty(container, message)
  // that this module calls for empty charts and tables. A same-named function
  // here shadows it inside this IIFE, so every empty chart would silently wipe
  // the whole batch view instead of just its own container.
  function renderNoCohort() {
    byId('batchKicker').textContent = '训练批次';
    byId('batchTitle').textContent = '没有可用的训练批次';
    setSelectOptions(byId('baselineCohort'), [], '不比较基线', '');
    byId('baselineHelp').textContent = '当前没有可查看的训练批次';
    resetMetrics();
    window.RunsTable.renderEmpty();
  }

  return {
    render,
    renderNoCohort,
    baselineChanged,
    currentCohortDescriptor,
    safeCohortId,
  };
})();
