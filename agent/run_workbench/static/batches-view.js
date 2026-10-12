'use strict';

// 回归/评测 tab: play_full_run batches.  Three pages, all read-only views of
// what python/run_batch.py wrote and the game logs say:
//
//   #/regression                          every batch (progress, ETA)
//   #/regression/batch/<id>               one batch: its lanes
//   #/regression/batch/<id>/lane/<lane>   one lane's games (finished + in progress)
//
// Numbers (finished games, wins, average floor, ETA) come from the server;
// nothing is recomputed here.  A batch that is still running re-reads itself
// every REFRESH_MS, and leaving the tab stops that (the router calls stop()).

window.BatchesView = (() => {
  const REFRESH_MS = 30 * 1000;
  const KIND_LABELS = { regression: '回归', ab: 'A/B 对比', smoke: '冒烟' };
  const BATCH_STATUS_LABELS = {
    running: '运行中',
    done: '已完成',
    failed: '有失败',
    stalled: '已停滞',
    unknown: '状态未知',
  };
  const LANE_STATE_LABELS = {
    queued: '排队中',
    running: '运行中',
    done: '已完成',
    failed: '失败',
    stalled: '已停滞',
    unknown: '未知',
  };

  let timer = null;
  let generation = 0;
  let lastSignature = '';

  // ---- formatting ------------------------------------------------------

  function trimNumber(value, digits) {
    return Number(value.toFixed(digits)).toString();
  }

  // "约 X 小时" for anything an hour or longer, whole minutes below that.
  function formatSpan(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    if (seconds >= 3600) return `约 ${trimNumber(seconds / 3600, 1)} 小时`;
    if (seconds >= 60) return `约 ${Math.round(seconds / 60)} 分钟`;
    return '不到 1 分钟';
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  // Local wall-clock time of an epoch-seconds instant: "HH:MM" on the same
  // day as `nowSeconds`, "M/D HH:MM" otherwise.
  function formatClock(epochSeconds, nowSeconds) {
    if (!Number.isFinite(epochSeconds)) return '—';
    const date = new Date(epochSeconds * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
    if (Number.isFinite(nowSeconds)) {
      const now = new Date(nowSeconds * 1000);
      const sameDay = now.getFullYear() === date.getFullYear()
        && now.getMonth() === date.getMonth()
        && now.getDate() === date.getDate();
      if (sameDay) return clock;
    }
    return `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
  }

  // "约 2.5 小时 · 18:30": how long, and when.  A dash when the server has no
  // estimate (no game has finished yet, or nothing is left to play).
  function formatEta(seconds, etaAt, nowSeconds) {
    if (!Number.isFinite(seconds)) return '—';
    const span = formatSpan(seconds);
    return Number.isFinite(etaAt) ? `${span} · ${formatClock(etaAt, nowSeconds)}` : span;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    if (seconds >= 3600) return `${trimNumber(seconds / 3600, 1)} 小时`;
    if (seconds >= 60) return `${Math.round(seconds / 60)} 分钟`;
    return `${Math.round(seconds)} 秒`;
  }

  function cell(value) {
    return value === null || value === undefined || value === '' ? '—' : String(value);
  }

  function percent(finished, planned) {
    if (!Number.isFinite(finished) || !Number.isFinite(planned) || planned <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((finished / planned) * 100)));
  }

  // ---- shared pieces ---------------------------------------------------

  function badge(text, tone) {
    return element('span', { className: `batch-badge batch-badge-${tone}`, text });
  }

  function statusTone(status) {
    if (status === 'running') return 'running';
    if (status === 'done') return 'done';
    if (status === 'failed' || status === 'stalled') return 'alert';
    return 'quiet';
  }

  // Whether `planned` is a real plan.  A batch (or lane) nobody registered has
  // none: the server then reports planned = what has been played, so a
  // "finished / planned (%)" bar would claim a completion nobody knows.
  function planKnown(item) {
    if (item && typeof item.planned_known === 'boolean') return item.planned_known;
    return !(item && item.registered === false);
  }

  function progressMeter(finished, planned, live, plannedKnown = true) {
    const wrapper = element('div', { className: 'batch-progress' });
    if (!plannedKnown) {
      // No plan: counts only, no bar and no percentage.
      wrapper.append(element('span', {
        className: 'batch-progress-text batch-progress-unplanned',
        text: `完成 ${Number.isFinite(finished) ? finished : 0} · 进行中 ${Number.isFinite(live) ? live : 0}`,
      }));
      return wrapper;
    }
    const meter = element('progress', {
      className: 'batch-progress-bar',
      attrs: {
        value: Number.isFinite(finished) ? finished : 0,
        max: Number.isFinite(planned) && planned > 0 ? planned : 1,
        'aria-label': `已完成 ${cell(finished)} / ${cell(planned)} 局`,
      },
    });
    wrapper.append(meter);
    const liveText = Number.isFinite(live) && live > 0 ? ` · ${live} 进行中` : '';
    wrapper.append(element('span', {
      className: 'batch-progress-text',
      text: `${cell(finished)} / ${cell(planned)}（${percent(finished, planned)}%）${liveText}`,
    }));
    return wrapper;
  }

  function clickableRow(tr, label, action) {
    tr.setAttribute('tabindex', '0');
    tr.setAttribute('role', 'link');
    tr.setAttribute('aria-label', label);
    tr.addEventListener('click', action);
    tr.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        action();
      }
    });
  }

  function headRow(labels) {
    const thead = element('thead');
    const row = element('tr');
    labels.forEach((label) => row.append(element('th', { text: label, attrs: { scope: 'col' } })));
    thead.append(row);
    return thead;
  }

  function backButton(label, hash) {
    const button = element('button', { className: 'quiet-button', text: label, attrs: { type: 'button' } });
    button.addEventListener('click', () => navigate(hash));
    return button;
  }

  function heading(kicker, title, note, badges = []) {
    const wrapper = element('div', { className: 'section-heading' });
    const left = element('div');
    left.append(
      element('p', { className: 'section-kicker', text: kicker }),
      element('h2', { text: title }),
    );
    if (badges.length) {
      const line = element('div', { className: 'batch-badges' });
      badges.forEach((item) => line.append(item));
      left.append(line);
    }
    wrapper.append(left);
    if (note) wrapper.append(element('p', { className: 'section-note', text: note }));
    return wrapper;
  }

  function metricCard(label, value, subtext, extraClass) {
    const card = element('article', { className: `metric-card${extraClass ? ` ${extraClass}` : ''}` });
    // A phrase ("约 37 分钟 · 13:35") does not fit the big-number type size.
    if (/[\u3400-\u9fff]/.test(String(value))) card.className += ' metric-card-text';
    card.append(
      element('span', { text: label }),
      element('strong', { text: value }),
      element('small', { text: subtext || '' }),
    );
    return card;
  }

  // A compact row of label / value pairs under a page heading.
  function factList(items) {
    const list = element('div', { className: 'batch-facts' });
    items.forEach(([label, value]) => {
      const fact = element('div', { className: 'batch-fact' });
      fact.append(
        element('small', { text: label }),
        element('strong', { text: cell(value) }),
      );
      list.append(fact);
    });
    return list;
  }

  function problemsNote(problems) {
    if (!Array.isArray(problems) || !problems.length) return null;
    return element('p', {
      className: 'section-note batch-problems',
      text: `有 ${problems.length} 个批次文件无法读取，已跳过：${problems.slice(0, 3).join('；')}`,
    });
  }

  // ---- the batch list --------------------------------------------------

  function drawList(payload) {
    const body = byId('regressionBody');
    clear(body);
    const batches = Array.isArray(payload.batches) ? payload.batches : [];
    const now = payload.now;
    const running = batches.filter((batch) => batch.done === false).length;
    body.append(heading(
      '回归 / 评测',
      '回归 / 评测批次',
      running ? `${running} 个批次运行中，每 30 秒自动刷新。` : '点击批次查看各通道的进度与对局。',
    ));
    const problems = problemsNote(payload.problems);
    if (problems) body.append(problems);
    if (!batches.length) {
      renderEmpty(body, '还没有回归/评测批次。用 python/run_batch.py 启动一批对局后会显示在这里。');
      return;
    }
    const wrapper = element('div', { className: 'table-scroll' });
    const table = element('table', { className: 'runs-table batch-table' });
    table.append(headRow(['批次', '类型', '开始时间', '进度', '进行中', '技术失败', '状态', '预计完成']));
    const tbody = element('tbody');
    batches.forEach((batch) => {
      const tr = element('tr', { className: 'runs-table-row batch-row' });
      const name = element('td', { className: 'batch-name' });
      name.append(
        element('strong', { text: cell(batch.name) }),
        element('small', { text: batch.registered === false ? '无登记信息，按对局日志归类' : cell(batch.batch_id) }),
      );
      const kind = element('td');
      if (batch.kind) kind.append(badge(KIND_LABELS[batch.kind] || batch.kind, 'kind'));
      else kind.textContent = '—';
      const progress = element('td');
      // The 进行中 column already says how many are live; the no-plan text has
      // no bar to lean on, so it carries the count itself.
      const batchPlanned = planKnown(batch);
      progress.append(progressMeter(batch.finished, batch.planned, batchPlanned ? 0 : batch.live, batchPlanned));
      const status = element('td');
      status.append(badge(BATCH_STATUS_LABELS[batch.status] || cell(batch.status), statusTone(batch.status)));
      tr.append(
        name,
        kind,
        element('td', { text: Number.isFinite(batch.created_at) ? formatTime(batch.created_at) : '—' }),
        progress,
        element('td', { text: String(Number.isFinite(batch.live) ? batch.live : 0) }),
        element('td', {
          className: Number(batch.technical) > 0 ? 'batch-technical' : '',
          text: String(Number.isFinite(batch.technical) ? batch.technical : 0),
        }),
        status,
        element('td', { text: batch.done ? '—' : formatEta(batch.eta_seconds, batch.eta_at, now) }),
      );
      clickableRow(tr, `查看批次 ${cell(batch.name)}`, () => navigate(batchRoute(batch.batch_id)));
      tbody.append(tr);
    });
    table.append(tbody);
    wrapper.append(table);
    body.append(wrapper);
  }

  // ---- one batch -------------------------------------------------------

  function laneStateCell(lane) {
    const td = element('td');
    td.append(badge(LANE_STATE_LABELS[lane.state] || cell(lane.state), statusTone(lane.state)));
    if (lane.state === 'failed' && lane.error) {
      td.append(element('small', { className: 'batch-lane-error', text: lane.error }));
    } else if (lane.state === 'failed' && Number.isFinite(lane.returncode)) {
      td.append(element('small', { className: 'batch-lane-error', text: `退出码 ${lane.returncode}` }));
    }
    return td;
  }

  function winsText(lane) {
    if (!Number.isFinite(lane.valid_n) || lane.valid_n === 0) return '—';
    return `${cell(lane.wins)}（${formatRate(lane.win_rate)}）`;
  }

  function drawBatch(payload) {
    const body = byId('regressionBody');
    clear(body);
    const batch = payload.batch || {};
    const lanes = Array.isArray(payload.lanes) ? payload.lanes : [];
    const now = payload.now;
    const top = element('div', { className: 'batch-page-head' });
    top.append(backButton('← 全部批次', regressionRoute()));
    body.append(top);
    const badges = [];
    if (batch.kind) badges.push(badge(KIND_LABELS[batch.kind] || batch.kind, 'kind'));
    badges.push(badge(BATCH_STATUS_LABELS[batch.status] || cell(batch.status), statusTone(batch.status)));
    body.append(heading(
      '批次',
      cell(batch.name),
      batch.registered === false ? '这批对局没有登记信息（无清单），按日志里的批次号或实验名归类。' : null,
      badges,
    ));
    body.append(factList([
      ['批次 ID', batch.batch_id],
      ['开始时间', Number.isFinite(batch.created_at) ? formatTime(batch.created_at) : null],
      ['代码版本', batch.git_commit],
      ['中位单局耗时', Number.isFinite(batch.median_duration_seconds) ? formatDuration(batch.median_duration_seconds) : null],
    ]));
    const problems = problemsNote(payload.problems);
    if (problems) body.append(problems);

    const cards = element('div', { className: 'metric-grid batch-cards' });
    cards.append(
      planKnown(batch)
        ? metricCard('已完成', `${cell(batch.finished)} / ${cell(batch.planned)}`, `${percent(batch.finished, batch.planned)}%`, 'metric-card-primary')
        : metricCard('已完成', cell(batch.finished), '没有计划局数，不显示完成比例', 'metric-card-primary'),
      metricCard('进行中', cell(batch.live), '正在打的对局'),
      metricCard('胜场', cell(batch.wins), '仅统计已完成'),
      metricCard('技术失败', cell(batch.technical), '崩溃 / 超时 / 卡死等', 'metric-card-alert'),
      metricCard('预计完成', batch.done ? '—' : formatEta(batch.eta_seconds, batch.eta_at, now), batch.done ? '批次已结束' : '剩余局数 × 中位耗时，取最慢通道'),
    );
    body.append(cards);

    const section = element('section', { className: 'panel batch-lanes' });
    section.append(heading('通道', '各角色通道', '一个通道对应一个角色，各通道并行运行；点击通道查看对局。'));
    if (!lanes.length) {
      renderEmpty(section, '该批次还没有通道信息。');
    } else {
      const wrapper = element('div', { className: 'table-scroll' });
      const table = element('table', { className: 'runs-table batch-table' });
      table.append(headRow(['通道', '策略', '进度', '平均推进层数', '胜场（胜率）', '技术失败', '状态', '预计完成']));
      const tbody = element('tbody');
      lanes.forEach((lane) => {
        const tr = element('tr', { className: 'runs-table-row batch-row' });
        const name = element('td', { className: 'batch-name' });
        name.append(element('strong', { text: cell(lane.lane) }));
        if (lane.character && lane.character !== lane.lane) name.append(element('small', { text: lane.character }));
        const progress = element('td');
        progress.append(progressMeter(lane.finished, lane.planned, lane.live, planKnown(lane)));
        tr.append(
          name,
          element('td', { text: cell(lane.policy) }),
          progress,
          element('td', { text: formatMissing(lane.avg_global_floor) }),
          element('td', { text: winsText(lane) }),
          element('td', {
            className: Number(lane.technical) > 0 ? 'batch-technical' : '',
            text: String(Number.isFinite(lane.technical) ? lane.technical : 0),
          }),
          laneStateCell(lane),
          element('td', { text: lane.eta_applicable ? formatEta(lane.eta_seconds, lane.eta_at, now) : '—' }),
        );
        clickableRow(tr, `查看通道 ${cell(lane.lane)} 的对局`, () => navigate(laneRoute(batch.batch_id, lane.lane)));
        tbody.append(tr);
      });
      table.append(tbody);
      wrapper.append(table);
      section.append(wrapper);
    }
    body.append(section);

    if ((Array.isArray(batch.argv) && batch.argv.length) || (batch.env && Object.keys(batch.env).length)) {
      const details = element('details', { className: 'panel batch-command' });
      details.append(element('summary', { text: '启动命令与环境变量' }));
      if (Array.isArray(batch.argv) && batch.argv.length) {
        details.append(element('pre', { text: batch.argv.join(' ') }));
      }
      if (batch.env && Object.keys(batch.env).length) {
        details.append(element('pre', {
          text: Object.entries(batch.env).map(([key, value]) => `${key}=${value}`).join('\n'),
        }));
      }
      body.append(details);
    }
  }

  // ---- one lane --------------------------------------------------------

  function drawLane(route, payload) {
    const body = byId('regressionBody');
    clear(body);
    const lane = payload.lane_summary || {};
    const now = payload.now;
    const top = element('div', { className: 'batch-page-head' });
    top.append(
      backButton('← 全部批次', regressionRoute()),
      backButton('← 返回批次', batchRoute(route.batchId)),
    );
    body.append(top);
    body.append(heading(
      '通道',
      `${cell(payload.lane)}${payload.batch_name ? ` · ${payload.batch_name}` : ''}`,
      null,
      [badge(LANE_STATE_LABELS[lane.state] || cell(lane.state), statusTone(lane.state))],
    ));
    body.append(factList([
      ['策略', lane.policy],
      ['进度', planKnown(lane) ? `${cell(lane.finished)} / ${cell(lane.planned)}` : `完成 ${cell(lane.finished)}（没有计划局数）`],
      ['进行中', lane.live],
      ['平均推进层数', formatMissing(lane.avg_global_floor)],
      ['胜场（胜率）', winsText(lane)],
      ['技术失败', lane.technical],
      ['中位单局耗时', Number.isFinite(lane.median_duration_seconds) ? formatDuration(lane.median_duration_seconds) : null],
      ['预计完成', lane.eta_applicable ? formatEta(lane.eta_seconds, lane.eta_at, now) : null],
    ]));
    if (Number(lane.abandoned) > 0) {
      body.append(element('p', {
        className: 'section-note batch-problems',
        text: `${lane.abandoned} 局日志已长时间未更新且没有结局（可能被中断），状态显示为“未知”，不计入统计。`,
      }));
    }
    if (payload.runs_complete === false) {
      body.append(element('p', { className: 'section-note', text: '仅展示最近的一部分对局。' }));
    }
    const panel = element('section', { className: 'panel runs-table-panel' });
    panel.append(heading('对局', '对局列表', '进行中的对局在最上方；点击一局查看路线与详情。'));
    const container = element('div', { className: 'table-scroll', attrs: { 'aria-live': 'polite' } });
    panel.append(container);
    body.append(panel);
    const rows = Array.isArray(payload.runs) ? payload.runs : [];
    if (!rows.length) {
      renderEmpty(container, '该通道还没有对局记录。');
      return;
    }
    // The row opens the per-game detail (route, deck, combats); a second link
    // keeps the old map view of the same game one click away.
    RunsTable.renderRows(container, rows, {
      routeFor: (ref) => gameRoute(ref.kind === 'run' ? `run:${ref.id}` : ref.id),
      extraColumns: [
        { label: '局号', value: (row) => (Number.isFinite(row.game_index) ? row.game_index : '—') },
        { label: '耗时', value: (row) => formatDuration(row.duration_seconds) },
        { label: '地图视图', render: (row) => mapViewLink(row) },
      ],
    });
  }

  // "地图视图": the pre-existing run page (act map + node rewards) of a game.
  // It sits inside a clickable row, so it must not also trigger the row.
  function mapViewLink(row) {
    const ref = row.ref && typeof row.ref === 'object' && typeof row.ref.id === 'string' && row.ref.id
      ? row.ref
      : null;
    if (!ref) return null;
    const link = element('button', {
      className: 'quiet-button runs-table-map-link',
      text: '地图视图',
      attrs: { type: 'button', 'aria-label': '在地图视图中打开这局' },
    });
    const stopRow = (event) => {
      if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
    };
    link.addEventListener('click', (event) => {
      stopRow(event);
      navigate(runRoute('', ref));
    });
    link.addEventListener('keydown', stopRow);
    return link;
  }

  // ---- loading, refreshing ---------------------------------------------

  function stop() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    generation += 1;
    const notice = byId('regressionNotice');
    if (notice) {
      notice.hidden = true;
      notice.textContent = '';
    }
  }

  function endpointFor(route) {
    if (route.view === 'regression-lane') {
      return `/api/batch/lane?id=${encodeURIComponent(route.batchId)}&lane=${encodeURIComponent(route.lane)}`;
    }
    if (route.view === 'regression-batch') {
      return `/api/batch?id=${encodeURIComponent(route.batchId)}`;
    }
    return '/api/batches';
  }

  // Whether the page just drawn is worth re-reading: something is still being
  // played (or is about to be).
  function needsRefresh(route, payload) {
    if (route.view === 'regression-lane') {
      const lane = payload.lane_summary || {};
      return lane.state === 'running' || lane.state === 'queued' || Number(lane.live) > 0;
    }
    if (route.view === 'regression-batch') {
      return Boolean(payload.batch) && payload.batch.done === false;
    }
    return Array.isArray(payload.batches) && payload.batches.some((batch) => batch.done === false);
  }

  function showNotice(text) {
    const notice = byId('regressionNotice');
    if (!notice) return;
    notice.hidden = !text;
    notice.textContent = text || '';
  }

  function scheduleRefresh(route, token) {
    timer = setTimeout(() => load(route, token, true), REFRESH_MS);
  }

  function draw(route, payload) {
    if (route.view === 'regression-lane') drawLane(route, payload);
    else if (route.view === 'regression-batch') drawBatch(payload);
    else drawList(payload);
  }

  async function load(route, token, refreshing) {
    timer = null;
    if (token !== generation) return;
    try {
      const payload = await getJSON(endpointFor(route));
      if (token !== generation) return;
      if (route.view === 'regression') {
        state.batches = Array.isArray(payload.batches) ? payload.batches : [];
        state.batchesNow = payload.now;
      }
      // A refresh that changed nothing leaves the DOM alone, so a row the
      // user is focused on or a scrolled table is not reset every 30 s.
      const signature = JSON.stringify(payload, (key, value) => (key === 'now' ? undefined : value));
      if (!refreshing || signature !== lastSignature) {
        lastSignature = signature;
        draw(route, payload);
      }
      showNotice('');
      if (!refreshing) setStatus('已载入批次');
      if (needsRefresh(route, payload)) scheduleRefresh(route, token);
    } catch (error) {
      if (token !== generation) return;
      if (refreshing) {
        // Keep what is on screen and try again on the next tick.
        showNotice(`自动刷新失败：${error.message}`);
        scheduleRefresh(route, token);
        return;
      }
      const body = byId('regressionBody');
      renderEmpty(body, `批次读取失败：${error.message}`, 'error-state');
      body.append(backButton('← 全部批次', regressionRoute()));
      setStatus(`批次读取失败：${error.message}`, 'error');
    }
  }

  async function render(route) {
    stop();
    const token = generation;
    lastSignature = '';
    renderEmpty(byId('regressionBody'), '正在读取回归/评测批次…', 'loading-state');
    setStatus('正在读取批次…', 'busy');
    await load(route, token, false);
  }

  return { render, stop, formatSpan, formatClock, formatEta, formatDuration, needsRefresh, mapViewLink };
})();
