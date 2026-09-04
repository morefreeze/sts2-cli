'use strict';

// Single-run detail: metadata header + the act map, delegating map
// rendering entirely to map.js (unchanged). Always addressed through the
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
    if (typeof showMapPage === 'function') showMapPage({ focusPage: true });
  }

  async function render(cohortId, ref) {
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
      const query = ref.kind === 'source' ? `source=${encodeURIComponent(ref.id)}` : `id=${encodeURIComponent(ref.id)}`;
      const payload = await getJSON(`/api/run?${query}`);
      // Keep the per-node replay so the map's node panel can show the actual
      // fight. /api/run already carries it; /api/run/map does not, and its
      // nodes only expose `recorded_node_id`, which is the key into this map.
      const runPayload = payload.run || {};
      state.currentRunReplay = (runPayload.replay_by_node &&
        typeof runPayload.replay_by_node === 'object') ? runPayload.replay_by_node : null;
      renderMeta(runPayload, cohortId);
      if (runHasMapCapability(payload)) {
        byId('runMapUnavailable').hidden = true;
        window.STS2Map.openRun(ref, null, { historyMode: 'none' });
      } else {
        showNoMap();
      }
      setStatus('已载入对局');
    } catch (error) {
      renderMeta({}, cohortId);
      showNoMap(`对局读取失败：${error.message}`);
      setStatus(`对局读取失败：${error.message}`, 'error');
    }
  }

  return { render };
})();
