'use strict';

(() => {
  const MAP_NS = 'http://www.w3.org/2000/svg';
  const ROOM_LABELS = {
    ancient: '远古事件',
    monster: '普通战斗',
    elite: '精英战斗',
    boss: '首领战斗',
    shop: '商店',
    restsite: '休息点',
    treasure: '宝箱',
    unknown: '未知事件',
  };
  const QUALITY_LABELS = { exact: '精确记录', derived: '相邻快照推导', unknown: '未知' };
  const BADGE_FIELDS = [
    { key: 'hp_change', short: 'HP', label: '生命变化', kind: 'number' },
    { key: 'max_hp_change', short: 'Max', label: '最大生命变化', kind: 'number' },
    { key: 'gold_change', short: '$', label: '金币变化', kind: 'number' },
    { key: 'damage_taken', short: '伤', label: '承受伤害', kind: 'negative' },
    { key: 'hp_healed', short: '疗', label: '生命恢复', kind: 'positive' },
    { key: 'cards_gained', short: '卡', label: '获得卡牌', kind: 'list' },
    { key: 'relics_gained', short: '遗', label: '获得遗物', kind: 'list' },
    { key: 'potions_gained', short: '药', label: '获得药水', kind: 'list' },
    { key: 'cards_upgraded', short: '升', label: '升级卡牌', kind: 'list' },
    { key: 'cards_removed', short: '删', label: '移除卡牌', kind: 'list' },
    { key: 'cards_transformed', short: '变', label: '变化卡牌', kind: 'list' },
  ];
  const mapState = {
    runId: '',
    actIndex: 0,
    opener: null,
    requestToken: 0,
    abortController: null,
    dashboardHidden: null,
  };

  function createSvg(tag, attrs = {}) {
    const node = document.createElementNS(MAP_NS, tag);
    Object.entries(attrs).forEach(([name, value]) => {
      if (value !== undefined && value !== null) node.setAttribute(name, String(value));
    });
    return node;
  }

  function normalizedRoomType(value) {
    return String(value || 'unknown').toLowerCase().replace(/[ _-]/g, '');
  }

  function roomLabel(node) {
    const artLabel = node && node.art && node.art.accessible_label;
    return artLabel || ROOM_LABELS[normalizedRoomType(node && node.room_type)] || '未知事件';
  }

  function createMapTransform(nodes) {
    const columns = nodes.map((node) => Number(node.col)).filter(Number.isInteger);
    const rows = nodes.map((node) => Number(node.row)).filter(Number.isInteger);
    const minCol = columns.length ? Math.min(...columns) : 0;
    const maxCol = columns.length ? Math.max(...columns) : 0;
    const minRow = rows.length ? Math.min(...rows) : 0;
    const maxRow = rows.length ? Math.max(...rows) : 0;
    const columnGap = 126;
    const rowGap = 112;
    const padding = 72;
    return {
      width: Math.max(620, (maxCol - minCol) * columnGap + padding * 2),
      height: Math.max(360, (maxRow - minRow) * rowGap + padding * 2),
      point(node) {
        const col = Number.isInteger(node.col) ? node.col : minCol;
        const row = Number.isInteger(node.row) ? node.row : minRow;
        return {
          x: padding + (col - minCol) * columnGap,
          y: padding + (maxRow - row) * rowGap,
        };
      },
    };
  }

  function routeEdgeKeys(payload) {
    const path = payload.alignment && Array.isArray(payload.alignment.path_node_ids)
      ? payload.alignment.path_node_ids
      : payload.nodes.filter((node) => node.visited).sort((a, b) => a.path_index - b.path_index).map((node) => node.id);
    const keys = new Set();
    for (let index = 0; index + 1 < path.length; index += 1) {
      keys.add(`${path[index]}\u0000${path[index + 1]}`);
    }
    return keys;
  }

  function appendEdge(layer, edge, nodeById, transform, className) {
    const from = nodeById.get(edge.from);
    const to = nodeById.get(edge.to);
    if (!from || !to) return;
    const start = transform.point(from);
    const end = transform.point(to);
    layer.append(createSvg('line', {
      x1: start.x, y1: start.y, x2: end.x, y2: end.y, class: className,
    }));
  }

  function renderNeutralEdges(svg, payload, nodeById, transform, routeKeys) {
    const layer = createSvg('g', { class: 'map-edges-neutral', 'aria-hidden': 'true' });
    payload.edges.forEach((edge) => {
      if (!routeKeys.has(`${edge.from}\u0000${edge.to}`)) {
        appendEdge(layer, edge, nodeById, transform, 'map-edge branch');
      }
    });
    svg.append(layer);
  }

  function renderVisitedEdges(svg, payload, nodeById, transform, routeKeys) {
    const layer = createSvg('g', { class: 'map-edges-route', 'aria-hidden': 'true' });
    payload.edges.forEach((edge) => {
      if (routeKeys.has(`${edge.from}\u0000${edge.to}`)) {
        appendEdge(layer, edge, nodeById, transform, 'map-edge route');
      }
    });
    svg.append(layer);
  }

  function measurementDisplay(measurement, field, compact = false) {
    if (!measurement || !['exact', 'derived'].includes(measurement.quality)) return '—';
    const value = measurement.value;
    let display = '—';
    if (field.kind === 'list') {
      if (Array.isArray(value)) display = compact ? String(value.length) : (value.length ? value.join('、') : '0');
    } else if (typeof value === 'number' && Number.isFinite(value)) {
      if (field.kind === 'negative') display = String(-Math.abs(value));
      else if (field.kind === 'positive') display = `+${Math.abs(value)}`;
      else display = value > 0 ? `+${value}` : String(value);
    }
    return display;
  }

  function nonzeroMeasurement(measurement, field) {
    if (!measurement || !['exact', 'derived'].includes(measurement.quality)) return false;
    if (field.kind === 'list') return Array.isArray(measurement.value) && measurement.value.length > 0;
    return typeof measurement.value === 'number' && Number.isFinite(measurement.value) && measurement.value !== 0;
  }

  function renderBadges(group, node) {
    const badges = BADGE_FIELDS.filter((field) => nonzeroMeasurement(node.deltas && node.deltas[field.key], field));
    badges.forEach((field, index) => {
      const measurement = node.deltas[field.key];
      const badge = createSvg('g', {
        class: `map-node-badge ${measurement.quality}`,
        transform: `translate(${34 + (index % 2) * 58} ${-34 + Math.floor(index / 2) * 18})`,
      });
      badge.append(createSvg('rect', { x: 0, y: 0, width: 54, height: 15, rx: 5 }));
      const textNode = createSvg('text', { x: 27, y: 11, 'text-anchor': 'middle' });
      textNode.textContent = `${measurement.quality === 'derived' ? '≈' : ''}${field.short} ${measurementDisplay(measurement, field, true)}`;
      const title = createSvg('title');
      title.textContent = `${field.label}：${measurementDisplay(measurement, field)}（${QUALITY_LABELS[measurement.quality]}）`;
      badge.append(title, textNode);
      group.append(badge);
    });
  }

  function renderNodeArt(group, node) {
    const art = node.art || {};
    if (node.visited && art.kind === 'original' && art.image_url) {
      group.append(createSvg('image', {
        href: art.image_url, x: -21, y: -21, width: 42, height: 42,
        preserveAspectRatio: 'xMidYMid meet',
      }));
    } else {
      const icon = createSvg('text', { class: `map-node-icon${node.visited ? '' : ' neutral'}`, x: 0, y: 8, 'text-anchor': 'middle' });
      icon.textContent = node.visited && art.kind === 'emoji'
        ? (art.emoji || art.letter || '?')
        : (art.letter || '?');
      group.append(icon);
    }
    const letter = createSvg('text', { class: 'map-node-letter', x: 0, y: 31, 'text-anchor': 'middle' });
    letter.textContent = art.letter || '?';
    group.append(letter);
  }

  function nodeTooltip(node) {
    const parts = [roomLabel(node), node.visited ? `路线第 ${Number(node.path_index) + 1} 个节点` : '未访问分支'];
    if (node.terminal) parts.push(node.terminal_status === 'dead' ? '阵亡终点' : '当前终点');
    if (node.visited) {
      BADGE_FIELDS.forEach((field) => {
        const measurement = node.deltas && node.deltas[field.key];
        if (nonzeroMeasurement(measurement, field)) {
          parts.push(`${field.label} ${measurementDisplay(measurement, field)}，${QUALITY_LABELS[measurement.quality]}`);
        }
      });
    }
    return parts.join('；');
  }

  function renderNodes(svg, payload, transform) {
    const layer = createSvg('g', { class: 'map-nodes' });
    payload.nodes.forEach((node) => {
      const point = transform.point(node);
      const group = createSvg('g', {
        class: `map-node${node.visited ? ' visited' : ' unvisited'}${node.terminal ? ' terminal' : ''}`,
        transform: `translate(${point.x} ${point.y})`,
        'aria-label': nodeTooltip(node),
      });
      if (node.visited) {
        group.setAttribute('role', 'button');
        group.setAttribute('tabindex', '0');
        group.addEventListener('click', () => selectNode(node, group));
        group.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            selectNode(node, group);
          }
        });
      }
      const title = createSvg('title');
      title.textContent = nodeTooltip(node);
      group.append(title, createSvg('circle', { class: 'map-node-circle', cx: 0, cy: 0, r: 28 }));
      renderNodeArt(group, node);
      if (node.visited) renderBadges(group, node);
      layer.append(group);
    });
    svg.append(layer);
  }

  function renderMap(payload) {
    const svg = byId('mapSvg');
    clear(svg);
    const transform = createMapTransform(payload.nodes);
    svg.setAttribute('viewBox', `0 0 ${transform.width} ${transform.height}`);
    svg.setAttribute('width', String(transform.width));
    svg.setAttribute('height', String(transform.height));
    const title = createSvg('title', { id: 'mapSvgTitle' });
    title.textContent = `${payload.act.label}地图，${payload.summary.visited_count} 个已访问节点`;
    const description = createSvg('desc', { id: 'mapSvgDescription' });
    description.textContent = payload.full_map
      ? `展示 ${payload.summary.node_count} 个节点和全部分支，金色连线为实际路线。`
      : '原始记录不足以重建全部分支，仅展示已记录路线。';
    svg.append(title, description);
    const nodeById = new Map(payload.nodes.map((node) => [node.id, node]));
    const routeKeys = routeEdgeKeys(payload);
    renderNeutralEdges(svg, payload, nodeById, transform, routeKeys);
    renderVisitedEdges(svg, payload, nodeById, transform, routeKeys);
    renderNodes(svg, payload, transform);
  }

  function appendDefinition(container, label, value) {
    container.append(element('dt', { text: label }), element('dd', { text: value }));
  }

  function selectNode(node, sourceElement = null) {
    const container = byId('selectedNodeSummary');
    clear(container);
    container.append(element('h3', { text: `${roomLabel(node)} · 路线节点 ${Number(node.path_index) + 1}` }));
    const list = element('dl', { className: 'key-values map-node-values' });
    BADGE_FIELDS.forEach((field) => {
      const measurement = node.deltas && node.deltas[field.key];
      const value = measurementDisplay(measurement, field);
      const quality = measurement && QUALITY_LABELS[measurement.quality] ? QUALITY_LABELS[measurement.quality] : '未知';
      appendDefinition(list, field.label, value === '—' ? '—' : `${value} · ${quality}`);
    });
    container.append(list);
    if (sourceElement) {
      byId('mapSvg').querySelectorAll('.map-node[data-selected="true"]').forEach((item) => item.removeAttribute('data-selected'));
      sourceElement.setAttribute('data-selected', 'true');
    }
  }

  function renderActSummary(payload) {
    const container = byId('actSummary');
    clear(container);
    container.append(element('h3', { text: `${payload.act.label}概览` }));
    const list = element('dl', { className: 'key-values' });
    appendDefinition(list, '地图范围', payload.full_map ? '完整分支 + 实际路线' : '仅已记录路线');
    appendDefinition(list, '节点 / 连线', `${payload.summary.node_count} / ${payload.summary.edge_count}`);
    appendDefinition(list, '已访问节点', payload.summary.visited_count);
    appendDefinition(list, '路线对齐', payload.alignment.ok ? '已验证' : '未验证');
    container.append(list);
  }

  function renderActTabs(payload) {
    const tabs = byId('actTabs');
    clear(tabs);
    payload.acts.forEach((act) => {
      const button = element('button', {
        text: `${act.label}${act.available ? ` · ${act.visited_count} 点` : ' · 无记录'}`,
        attrs: {
          type: 'button', role: 'tab', 'aria-selected': act.index === payload.act.index,
          tabindex: act.index === payload.act.index ? '0' : '-1',
        },
      });
      button.disabled = !act.available;
      button.addEventListener('click', () => loadAct(mapState.runId, act.index, { pushHistory: true }));
      tabs.append(button);
    });
  }

  function showMapPage() {
    const main = byId('dashboardMain');
    const page = byId('runMapPage');
    if (!byId('detailPanel').hidden) closeDetail();
    if (!mapState.dashboardHidden) {
      mapState.dashboardHidden = Array.from(main.children).map((child) => ({ child, hidden: child.hidden }));
    }
    mapState.dashboardHidden.forEach(({ child }) => { child.hidden = child !== page; });
    page.hidden = false;
    page.focus();
  }

  function showDashboardPage() {
    if (mapState.abortController) mapState.abortController.abort();
    mapState.abortController = null;
    mapState.requestToken += 1;
    const remembered = mapState.dashboardHidden;
    if (remembered) remembered.forEach(({ child, hidden }) => { child.hidden = hidden; });
    byId('runMapPage').hidden = true;
    mapState.dashboardHidden = null;
    const opener = mapState.opener;
    mapState.opener = null;
    if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    else byId('dashboardMain').focus();
  }

  function mapLocation(runId, actIndex) {
    return `#run=${encodeURIComponent(runId)}&act=${actIndex}`;
  }

  async function loadAct(runId, actIndex, { pushHistory = false, opener = null } = {}) {
    runId = typeof runId === 'string' ? runId.trim() : '';
    if (!runId) {
      setStatus('无法打开地图：缺少对局 ID', 'error');
      return;
    }
    if (opener && opener.isConnected) mapState.opener = opener;
    mapState.runId = runId;
    mapState.actIndex = actIndex;
    showMapPage();
    byId('runMapTitle').textContent = `对局 ${runId}`;
    byId('mapFallback').hidden = true;
    renderEmpty(byId('actSummary'), '正在重建地图…', 'loading-state');
    renderEmpty(byId('selectedNodeSummary'), '载入地图后，选择一个已访问节点查看收益。');
    clear(byId('mapSvg'));
    if (pushHistory) {
      history.pushState({ view: 'run-map', runId, actIndex, fromDashboard: true }, '', mapLocation(runId, actIndex));
    }
    mapState.requestToken += 1;
    const token = mapState.requestToken;
    if (mapState.abortController) mapState.abortController.abort();
    const controller = new AbortController();
    mapState.abortController = controller;
    setStatus(`正在读取 ${runId} 地图…`, 'busy');
    try {
      const payload = await getJSON(`/api/run/map?id=${encodeURIComponent(runId)}&act=${actIndex}`, { signal: controller.signal });
      if (token !== mapState.requestToken) return;
      mapState.abortController = null;
      renderActTabs(payload);
      renderMap(payload);
      renderActSummary(payload);
      const fallback = byId('mapFallback');
      fallback.hidden = payload.full_map || !payload.fallback_reason;
      fallback.textContent = payload.fallback_reason || '';
      const visited = payload.nodes.filter((node) => node.visited).sort((a, b) => a.path_index - b.path_index);
      if (visited.length) selectNode(visited[0]);
      setStatus(payload.full_map ? '已载入完整地图' : '已载入记录路线');
    } catch (error) {
      if (token !== mapState.requestToken || error.name === 'AbortError') return;
      mapState.abortController = null;
      renderEmpty(byId('actSummary'), `地图读取失败：${error.message}`, 'error-state');
      renderEmpty(byId('selectedNodeSummary'), '没有可查看的节点。', 'error-state');
      setStatus(`地图读取失败：${error.message}`, 'error');
    }
  }

  function closeMapPage() {
    if (history.state && history.state.view === 'run-map' && history.state.fromDashboard) history.back();
    else {
      history.replaceState({ view: 'dashboard' }, '', `${location.pathname}${location.search}`);
      showDashboardPage();
    }
  }

  function parseMapLocation() {
    const params = new URLSearchParams(location.hash.replace(/^#/, ''));
    const runId = params.get('run');
    const act = Number(params.get('act') || 0);
    return runId && Number.isInteger(act) && act >= 0 && act <= 3 ? { runId, actIndex: act } : null;
  }

  byId('mapBackButton').addEventListener('click', closeMapPage);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !byId('runMapPage').hidden) {
      event.preventDefault();
      closeMapPage();
    }
  });
  window.addEventListener('popstate', (event) => {
    const route = event.state && event.state.view === 'run-map'
      ? { runId: event.state.runId, actIndex: event.state.actIndex }
      : parseMapLocation();
    if (route) loadAct(route.runId, route.actIndex, { pushHistory: false });
    else showDashboardPage();
  });

  window.STS2Map = Object.freeze({
    openRun(runId, opener = null) {
      loadAct(runId, 0, { pushHistory: true, opener });
    },
  });

  const initialRoute = parseMapLocation();
  if (initialRoute) {
    history.replaceState({ view: 'run-map', ...initialRoute, fromDashboard: false }, '', location.href);
    loadAct(initialRoute.runId, initialRoute.actIndex, { pushHistory: false });
  } else {
    history.replaceState({ view: 'dashboard' }, '', location.href);
  }
})();
