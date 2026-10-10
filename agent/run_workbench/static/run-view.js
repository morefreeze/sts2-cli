'use strict';

// Single-run detail: metadata header + the act map, delegating map
// rendering entirely to map.js. Always addressed through the
// `ref` object the router carries -- never a bare run_id, since real data
// has run_id: null for every row on disk.

window.RunView = (() => {
  function metaValue(value) {
    return value === null || value === undefined || value === '' ? '—' : String(value);
  }

  function actForFloor(floor) {
    return Number.isInteger(floor) && floor > 0 ? Math.floor((floor - 1) / 17) + 1 : null;
  }

  function renderMeta(run, cohortId) {
    const container = byId('runMeta');
    clear(container);
    const metadata = (run && run.metadata) || {};
    const outcome = (run && run.outcome) || {};
    const cohort = Array.isArray(state.cohorts)
      ? state.cohorts.find((candidate) => window.CohortView.safeCohortId(candidate) === cohortId)
      : null;
    const floor = Number.isFinite(outcome.max_global_floor) ? outcome.max_global_floor : null;
    const entries = [
      ['种子', metadata.seed],
      ['状态', STATUS_LABELS[outcome.status] || outcome.status],
      ['推进层数', floor],
      ['幕', actForFloor(floor)],
      ['角色', metadata.character],
      ['训练检查点', metadata.checkpoint],
      ['实验', cohort ? cohort.experiment : null],
      ['游戏版本', metadata.game_version],
    ];
    entries.forEach(([label, value]) => {
      container.append(element('dt', { text: label }), element('dd', { text: metaValue(value) }));
    });
  }

  function showNoMap(message) {
    byId('mapFallback').hidden = true;
    const note = byId('runMapUnavailable');
    note.hidden = false;
    if (message) note.textContent = message;
    const tabs = byId('actTabs');
    if (tabs) clear(tabs);
    const svg = byId('mapSvg');
    if (svg) clear(svg);
    const actSummary = byId('actSummary');
    if (actSummary) clear(actSummary);
    const selectedNode = byId('selectedNodeSummary');
    if (selectedNode) clear(selectedNode);
    if (window.STS2Map && typeof window.STS2Map.showMapPage === 'function') {
      window.STS2Map.showMapPage({ focusPage: true });
    }
  }

  // ---- Games that are still being played ------------------------------
  //
  // A run whose log has no end yet is re-fetched (run + map) every
  // LIVE_REFRESH_MS until it ends, its log goes quiet, or the user leaves it.
  // `generation` is bumped by every render() and stop(), so a timer or an
  // in-flight request that belongs to an earlier one can tell it is stale.

  const LIVE_REFRESH_MS = 30 * 1000;
  let liveTimer = null;
  let liveGeneration = 0;
  let mapOpened = false;
  let wasLive = false;

  function setLiveNotice(text) {
    const node = byId('runLiveNotice');
    if (!node) return;
    node.hidden = !text;
    node.textContent = text || '';
  }

  function stop() {
    if (liveTimer !== null) clearTimeout(liveTimer);
    liveTimer = null;
    liveGeneration += 1;
    setLiveNotice('');
  }

  // `live` comes from the server: false once an unfinished log has not been
  // written to for longer than the in-progress window (an abandoned run).
  function runIsLive(payload) {
    const outcome = (payload && payload.run && payload.run.outcome) || {};
    return outcome.status === 'in_progress' && payload.live !== false;
  }

  function stillViewing(ref) {
    const route = parseRoute(location.hash);
    return route.view === 'run' && route.ref.kind === ref.kind && route.ref.id === ref.id;
  }

  function runQuery(ref) {
    return ref.kind === 'source' ? `source=${encodeURIComponent(ref.id)}` : `id=${encodeURIComponent(ref.id)}`;
  }

  function applyPayload(payload, cohortId, ref, refreshing) {
    // Keep the per-node replay so the map's node panel can show the actual
    // fight. /api/run already carries it; /api/run/map does not, and its
    // nodes only expose `recorded_node_id`, which is the key into this map.
    const runPayload = payload.run || {};
    state.currentRunReplay = (runPayload.replay_by_node &&
      typeof runPayload.replay_by_node === 'object') ? runPayload.replay_by_node : null;
    renderMeta(runPayload, cohortId);
    if (runHasMapCapability(payload)) {
      byId('runMapUnavailable').hidden = true;
      if (refreshing && mapOpened) window.STS2Map.refreshRun(ref);
      else window.STS2Map.openRun(ref, null, { historyMode: 'none' });
      mapOpened = true;
    } else {
      // A refresh of a run that still has no map must not pull focus again.
      if (!refreshing || mapOpened) showNoMap();
      mapOpened = false;
    }
  }

  function scheduleRefresh(cohortId, ref, generation) {
    liveTimer = setTimeout(() => refresh(cohortId, ref, generation), LIVE_REFRESH_MS);
  }

  async function refresh(cohortId, ref, generation) {
    liveTimer = null;
    if (generation !== liveGeneration) return;
    if (!stillViewing(ref)) {
      stop();
      return;
    }
    try {
      const payload = await getJSON(`/api/run?${runQuery(ref)}`);
      if (generation !== liveGeneration) return;
      applyPayload(payload, cohortId, ref, true);
      afterPayload(payload, cohortId, ref, generation);
    } catch (error) {
      if (generation !== liveGeneration) return;
      // A failed refresh is not the end of the game: say so, keep what is on
      // screen, and try again on the next tick.
      setLiveNotice(`进行中 · 每 30 秒自动刷新（本次刷新失败：${error.message}）`);
      scheduleRefresh(cohortId, ref, generation);
    }
  }

  function afterPayload(payload, cohortId, ref, generation) {
    const status = (((payload || {}).run || {}).outcome || {}).status;
    if (runIsLive(payload)) {
      setLiveNotice(`${STATUS_LABELS.in_progress} · 每 30 秒自动刷新`);
      scheduleRefresh(cohortId, ref, generation);
      wasLive = true;
    } else if (status === 'in_progress') {
      setLiveNotice('进行中 · 日志已长时间未更新，已停止自动刷新');
      wasLive = false;
    } else {
      // Not (or no longer) being played: if it was a moment ago, say it ended.
      setLiveNotice(wasLive ? '对局已结束，已停止自动刷新' : '');
      wasLive = false;
    }
  }

  async function render(cohortId, ref) {
    stop();
    const generation = liveGeneration;
    mapOpened = false;
    wasLive = false;
    byId('runMapUnavailable').hidden = true;
    state.currentRunReplay = null;   // never show the previous run's fight
    clear(byId('runMeta'));
    byId('runMapTitle').textContent = `对局 ${ref && ref.id ? ref.id : '未知'}`;
    if (!ref || typeof ref.id !== 'string' || !ref.id) {
      setStatus('无法打开对局：缺少对局引用', 'error');
      showNoMap('缺少对局引用，无法定位该对局。');
      return;
    }
    setStatus('正在读取对局…', 'busy');
    try {
      const payload = await getJSON(`/api/run?${runQuery(ref)}`);
      if (generation !== liveGeneration) return;
      applyPayload(payload, cohortId, ref, false);
      afterPayload(payload, cohortId, ref, generation);
      setStatus('已载入对局');
    } catch (error) {
      if (generation !== liveGeneration) return;
      renderMeta({}, cohortId);
      showNoMap(`对局读取失败：${error.message}`);
      setStatus(`对局读取失败：${error.message}`, 'error');
    }
  }

  return { render, stop };
})();
