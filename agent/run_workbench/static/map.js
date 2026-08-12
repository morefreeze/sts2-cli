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
  const DECISION_KIND_LABELS = Object.freeze({ event: '事件', card_reward: '卡', potion: '药水', relic: '遗物', shop: '商店', rest: '休息' });
  const DECISION_FIELDS = Object.freeze(['kind', 'selected_id', 'selected_label', 'options', 'evidence']);
  const DECISION_OPTION_FIELDS = Object.freeze(['id', 'label', 'effect', 'selected']);
  const DECISION_MAX_COUNT = 16;
  const DECISION_MAX_OPTIONS = 32;
  const DECISION_MAX_ID_SCALARS = 256;
  const DECISION_MAX_LABEL_SCALARS = 256;
  const DECISION_MAX_EFFECT_SCALARS = 512;
  const DECISION_MAX_BYTES = 32768;
  const MAP_COLUMN_GAP = 126;
  const MAP_ROW_GAP = 88;
  const MAP_PADDING_X = 72;
  const MAP_PADDING_TOP = 72;
  const MAP_PADDING_BOTTOM = 76;
  const MAP_DECISION_RAIL_GAP = 24;
  const MAP_DECISION_RAIL_WIDTH = 360;
  const MAP_DECISION_SUMMARY_WIDTH = 340;
  const MAP_DECISION_LABEL_LIMIT = 72;
  const DELTA_LIST_LABEL_LIMIT = 3;
  const DELTA_ITEM_LABEL_LIMIT = 48;
  const DELTA_ITEM_DEPTH_LIMIT = 4;
  const mapState = {
    runId: '',
    actIndex: 0,
    opener: null,
    requestToken: 0,
    abortController: null,
    dashboardHidden: null,
  };
  let activeDecisionAnchor = null;
  let decisionClipSerial = 0;

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

  function createMapTransform(nodes, hasDecisionRail = false) {
    const columns = nodes.map((node) => Number(node.col)).filter(Number.isInteger);
    const rows = nodes.map((node) => Number(node.row)).filter(Number.isInteger);
    const minCol = columns.length ? Math.min(...columns) : 0;
    const maxCol = columns.length ? Math.max(...columns) : 0;
    const minRow = rows.length ? Math.min(...rows) : 0;
    const maxRow = rows.length ? Math.max(...rows) : 0;
    const graphWidth = Math.max(620, (maxCol - minCol) * MAP_COLUMN_GAP + MAP_PADDING_X * 2);
    const decisionX = graphWidth + MAP_DECISION_RAIL_GAP;
    return {
      graphWidth,
      decisionX,
      width: hasDecisionRail ? decisionX + MAP_DECISION_RAIL_WIDTH : graphWidth,
      height: Math.max(360, (maxRow - minRow) * MAP_ROW_GAP + MAP_PADDING_TOP + MAP_PADDING_BOTTOM),
      point(node) {
        const col = Number.isInteger(node.col) ? node.col : minCol;
        const row = Number.isInteger(node.row) ? node.row : minRow;
        return {
          x: MAP_PADDING_X + (col - minCol) * MAP_COLUMN_GAP,
          y: MAP_PADDING_TOP + (maxRow - row) * MAP_ROW_GAP,
        };
      },
    };
  }

  function routeEdgeKeys(payload) {
    const hasAlignedPath = payload.alignment
      && Array.isArray(payload.alignment.path_node_ids)
      && payload.alignment.path_node_ids.length > 0;
    const path = hasAlignedPath
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

  function boundedDeltaLabel(value, limit = DELTA_ITEM_LABEL_LIMIT) {
    if (!['string', 'number', 'boolean'].includes(typeof value)) return '';
    const label = String(value).trim();
    return label.length > limit
      ? `${label.slice(0, Math.max(0, limit - 1))}…`
      : label;
  }

  function unicodeScalarArray(value) {
    if (typeof value !== 'string') return null;
    const scalars = Array.from(value);
    return scalars.some((scalar) => {
      const codePoint = scalar.codePointAt(0);
      return codePoint >= 0xD800 && codePoint <= 0xDFFF;
    }) ? null : scalars;
  }

  function boundedDecisionText(value, limit = MAP_DECISION_LABEL_LIMIT) {
    if (!Number.isInteger(limit) || limit < 1) return '';
    const scalars = unicodeScalarArray(value && typeof value === 'string' ? value.trim() : value);
    if (!scalars || !scalars.length) return '';
    return scalars.length > limit
      ? `${scalars.slice(0, Math.max(0, limit - 1)).join('')}…`
      : scalars.join('');
  }

  function boundedTransformationLabel(from, to) {
    const separator = ' → ';
    const available = DELTA_ITEM_LABEL_LIMIT - separator.length;
    const fromLimit = Math.floor(available / 2);
    const toLimit = available - fromLimit;
    const boundedFrom = boundedDecisionText(from, fromLimit);
    const boundedTo = boundedDecisionText(to, toLimit);
    if (!boundedFrom || !boundedTo) return '';
    const combined = `${boundedFrom}${separator}${boundedTo}`;
    return Array.from(combined).length <= DELTA_ITEM_LABEL_LIMIT ? combined : '';
  }

  function localizedDeltaLabel(value) {
    const scalar = boundedDeltaLabel(value);
    if (scalar) return scalar;
    if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
    return boundedDeltaLabel(value.en)
      || boundedDeltaLabel(value['zh-CN'])
      || boundedDeltaLabel(value.zh);
  }

  function nestedDeltaItemLabel(item, depth = 0) {
    if (depth > DELTA_ITEM_DEPTH_LIMIT) return '';
    const scalar = boundedDeltaLabel(item);
    if (scalar) return scalar;
    if (!item || typeof item !== 'object' || Array.isArray(item)) return '';

    const transformationPairs = [
      ['from', 'to'],
      ['before', 'after'],
      ['old', 'new'],
    ];
    for (const [fromKey, toKey] of transformationPairs) {
      if (!(fromKey in item) && !(toKey in item)) continue;
      const from = nestedDeltaItemLabel(item[fromKey], depth + 1);
      const to = nestedDeltaItemLabel(item[toKey], depth + 1);
      if (from && to) return boundedTransformationLabel(from, to);
      if (from || to) return from || to;
    }

    if ('choice' in item) {
      const choice = nestedDeltaItemLabel(item.choice, depth + 1);
      if (choice) return choice;
    }
    return localizedDeltaLabel(item.name)
      || localizedDeltaLabel(item.id)
      || localizedDeltaLabel(item);
  }

  function deltaItemLabel(item) {
    return nestedDeltaItemLabel(item) || '未知项目';
  }

  function boundedListLabels(items) {
    const labels = items
      .slice(0, DELTA_LIST_LABEL_LIMIT)
      .map((item) => deltaItemLabel(item));
    const overflow = items.length - labels.length;
    return `${labels.join('、')}${overflow > 0 ? `，另 ${overflow} 项` : ''}`;
  }

  function measurementDisplay(measurement, field, compact = false) {
    if (!measurement || !['exact', 'derived'].includes(measurement.quality)) return '—';
    const value = measurement.value;
    let display = '—';
    if (field.kind === 'list') {
      if (Array.isArray(value)) display = compact ? String(value.length) : (value.length ? boundedListLabels(value) : '0');
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

  function exactDecisionText(value, limit) {
    const scalars = unicodeScalarArray(value);
    if (!scalars || !scalars.length || !value.trim() || scalars.length > limit) return null;
    return value;
  }

  function ordinaryDataRecord(value, exactFields) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    let prototype;
    let names;
    let symbols;
    try {
      prototype = Object.getPrototypeOf(value);
      names = Object.getOwnPropertyNames(value);
      symbols = Object.getOwnPropertySymbols(value);
    } catch (_error) {
      return null;
    }
    if (prototype !== Object.prototype && prototype !== null) return null;
    if (symbols.length || names.includes('__proto__') || names.includes('constructor')) return null;
    if (names.length !== exactFields.length || exactFields.some((field) => !names.includes(field))) return null;
    const result = Object.create(null);
    for (const field of exactFields) {
      let descriptor;
      try {
        descriptor = Object.getOwnPropertyDescriptor(value, field);
      } catch (_error) {
        return null;
      }
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) return null;
      result[field] = descriptor.value;
    }
    return result;
  }

  function ordinaryArrayValues(value, maximum) {
    if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
    let names;
    let symbols;
    try {
      names = Object.getOwnPropertyNames(value);
      symbols = Object.getOwnPropertySymbols(value);
    } catch (_error) {
      return null;
    }
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length');
    const length = lengthDescriptor && lengthDescriptor.value;
    if (symbols.length || !Number.isInteger(length) || length < 0 || length > maximum) return null;
    if (names.length !== length + 1 || !names.includes('length')) return null;
    const result = [];
    for (let index = 0; index < length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) return null;
      result.push(descriptor.value);
    }
    return result;
  }

  function validatedDecisionOption(value) {
    const record = ordinaryDataRecord(value, DECISION_OPTION_FIELDS);
    if (!record) return null;
    const id = exactDecisionText(record.id, DECISION_MAX_ID_SCALARS);
    const label = exactDecisionText(record.label, DECISION_MAX_LABEL_SCALARS);
    const effect = record.effect === null
      ? null
      : exactDecisionText(record.effect, DECISION_MAX_EFFECT_SCALARS);
    if (!id || !label || (record.effect !== null && !effect) || typeof record.selected !== 'boolean') return null;
    return { id, label, effect, selected: record.selected };
  }

  function validatedRecordedDecision(value) {
    const record = ordinaryDataRecord(value, DECISION_FIELDS);
    if (!record) return null;
    const kind = exactDecisionText(record.kind, DECISION_MAX_ID_SCALARS);
    const selectedId = exactDecisionText(record.selected_id, DECISION_MAX_ID_SCALARS);
    const selectedLabel = exactDecisionText(record.selected_label, DECISION_MAX_LABEL_SCALARS);
    if (!kind || !Object.hasOwn(DECISION_KIND_LABELS, kind)
      || !selectedId || !selectedLabel || record.evidence !== 'recorded') return null;
    const rawOptions = ordinaryArrayValues(record.options, DECISION_MAX_OPTIONS);
    if (!rawOptions || !rawOptions.length) return null;
    const options = rawOptions.map(validatedDecisionOption);
    if (options.some((option) => option === null)) return null;
    const selected = options.filter((option) => option.selected === true);
    if (selected.length !== 1
      || selected[0].id !== selectedId
      || selected[0].label !== selectedLabel) return null;
    return {
      kind,
      selected_id: selectedId,
      selected_label: selectedLabel,
      options,
      evidence: 'recorded',
    };
  }

  function pythonDefaultJSONByteLength(decisions) {
    const compactBytes = new TextEncoder().encode(JSON.stringify(decisions)).length;
    if (!decisions.length) return compactBytes;
    const optionCount = decisions.reduce((total, decision) => total + decision.options.length, 0);
    const defaultSeparatorSpaces = 9 * decisions.length + 8 * optionCount - 1;
    return compactBytes + defaultSeparatorSpaces;
  }

  function validateRecordedDecisions(value) {
    const rawDecisions = ordinaryArrayValues(value, DECISION_MAX_COUNT);
    if (!rawDecisions) return null;
    const decisions = rawDecisions.map(validatedRecordedDecision);
    if (decisions.some((decision) => decision === null)) return null;
    try {
      if (pythonDefaultJSONByteLength(decisions) > DECISION_MAX_BYTES) return null;
    } catch (_error) {
      return null;
    }
    return decisions;
  }

  function recordedNodeDecisions(node) {
    if (!node || !node.visited || typeof node !== 'object') return [];
    let descriptor;
    try {
      descriptor = Object.getOwnPropertyDescriptor(node, 'decisions');
    } catch (_error) {
      return null;
    }
    if (!descriptor) return [];
    if (!Object.hasOwn(descriptor, 'value')) return null;
    const decisions = validateRecordedDecisions(descriptor.value);
    if (decisions === null) return null;
    return decisions.map((decision) => ({
      decision,
      selected: decision.options.find((option) => option.selected === true),
    }));
  }

  function ordinaryDataDescriptors(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    let prototype;
    let names;
    let symbols;
    try {
      prototype = Object.getPrototypeOf(value);
      names = Object.getOwnPropertyNames(value);
      symbols = Object.getOwnPropertySymbols(value);
    } catch (_error) {
      return null;
    }
    if ((prototype !== Object.prototype && prototype !== null) || symbols.length
      || names.includes('__proto__') || names.includes('constructor')) return null;
    const descriptors = Object.create(null);
    for (const name of names) {
      let descriptor;
      try {
        descriptor = Object.getOwnPropertyDescriptor(value, name);
      } catch (_error) {
        return null;
      }
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) return null;
      descriptors[name] = descriptor.value;
    }
    return descriptors;
  }

  function stableDeltaIdentifier(value) {
    return exactDecisionText(value, DECISION_MAX_ID_SCALARS);
  }

  function derivedDeltaItemLabel(fieldKey, item) {
    const direct = stableDeltaIdentifier(item);
    if (direct) return boundedDecisionText(direct, DELTA_ITEM_LABEL_LIMIT);
    const record = ordinaryDataDescriptors(item);
    if (!record) return '';

    if (fieldKey === 'cards_transformed') {
      const names = Object.keys(record);
      if (names.length !== 2 || !names.includes('from') || !names.includes('to')) return '';
      const from = stableDeltaIdentifier(record.from);
      const to = stableDeltaIdentifier(record.to);
      return from && to ? boundedTransformationLabel(from, to) : '';
    }

    const identifier = stableDeltaIdentifier(record.id);
    if (identifier) return boundedDecisionText(identifier, DELTA_ITEM_LABEL_LIMIT);
    if (fieldKey === 'relics_gained' || fieldKey === 'potions_gained') {
      const names = Object.keys(record);
      if (names.length === 2 && names.includes('choice') && names.includes('was_picked')
        && record.was_picked === true) {
        const choice = stableDeltaIdentifier(record.choice);
        return choice ? boundedDecisionText(choice, DELTA_ITEM_LABEL_LIMIT) : '';
      }
    }
    return '';
  }

  function knownDeltaListLabels(fieldKey, items) {
    const values = ordinaryArrayValues(items, DECISION_MAX_OPTIONS);
    if (!values || !values.length) return '';
    const labels = values.map((item) => derivedDeltaItemLabel(fieldKey, item));
    if (!labels.length || labels.some((label) => !label)) return '';
    const visible = labels.slice(0, DELTA_LIST_LABEL_LIMIT);
    const overflow = labels.length - visible.length;
    return `${visible.join('、')}${overflow > 0 ? `，另 ${overflow} 项` : ''}`;
  }

  function nodeDecisionSummary(node) {
    if (!node || !node.visited) return null;
    const recorded = recordedNodeDecisions(node);
    if (recorded === null) return null;
    if (recorded.length) {
      const newest = recorded[recorded.length - 1];
      return {
        prefix: DECISION_KIND_LABELS[newest.decision.kind],
        label: boundedDecisionText(newest.selected.label),
        effect: boundedDecisionText(newest.selected.effect || ''),
        overflow: recorded.length - 1,
        recorded: true,
      };
    }

    const derivedFields = [
      { key: 'cards_gained', verb: '获得', noun: '卡牌' },
      { key: 'potions_gained', verb: '获得', noun: '药水' },
      { key: 'relics_gained', verb: '获得', noun: '遗物' },
      { key: 'cards_upgraded', verb: '升级', noun: '卡牌' },
      { key: 'cards_removed', verb: '移除', noun: '卡牌' },
      { key: 'cards_transformed', verb: '变化', noun: '卡牌' },
    ];
    for (const field of derivedFields) {
      const measurement = node.deltas && node.deltas[field.key];
      if (!measurement || !['exact', 'derived'].includes(measurement.quality)) continue;
      if (!Array.isArray(measurement.value) || !measurement.value.length) continue;
      const labels = knownDeltaListLabels(field.key, measurement.value);
      if (!labels) continue;
      const label = boundedDecisionText(`${field.verb} ${labels}`);
      if (!label) continue;
      return {
        prefix: '推导',
        label,
        effect: `${field.noun} · ${measurement.quality === 'derived' ? '相邻快照推导' : '精确记录'}`,
        overflow: 0,
        recorded: false,
      };
    }
    return null;
  }

  function renderDecisionSummary(layer, node, point, transform) {
    const summary = nodeDecisionSummary(node);
    if (!summary) return null;
    decisionClipSerial += 1;
    const clipId = `map-decision-clip-${decisionClipSerial}`;
    const group = createSvg('g', {
      class: 'map-decision-summary',
      transform: `translate(${transform.decisionX} ${point.y})`,
      'clip-path': `url(#${clipId})`,
      'pointer-events': 'none',
      'aria-hidden': 'true',
    });
    const clipPath = createSvg('clipPath', { id: clipId });
    clipPath.append(createSvg('rect', {
      x: 0, y: -15, width: MAP_DECISION_SUMMARY_WIDTH, height: 30, rx: 7,
    }));
    const definitions = createSvg('defs');
    definitions.append(clipPath);
    group.append(definitions);
    group.append(createSvg('rect', {
      x: 0, y: -15, width: MAP_DECISION_SUMMARY_WIDTH, height: 30, rx: 7,
    }));
    const textNode = createSvg('text', { x: 10, y: 4 });
    const label = createSvg('tspan');
    label.textContent = `${summary.prefix}：${summary.label}`;
    textNode.append(label);
    if (summary.effect) {
      const effect = createSvg('tspan', { class: 'map-decision-effect' });
      effect.textContent = ` · ${summary.effect}`;
      textNode.append(effect);
    }
    if (summary.overflow > 0) {
      const overflow = createSvg('tspan', { class: 'map-decision-overflow' });
      overflow.textContent = ` +${summary.overflow}`;
      textNode.append(overflow);
    }
    group.append(textNode);
    layer.append(group);
    return group;
  }

  function usableDecisionAnchor(anchor) {
    if (!anchor || typeof anchor !== 'object' || anchor.isConnected !== true
      || anchor.hidden === true || anchor.disabled === true || anchor.inert === true
      || typeof anchor.getBoundingClientRect !== 'function'
      || typeof anchor.setAttribute !== 'function' || typeof anchor.removeAttribute !== 'function'
      || typeof anchor.getAttribute !== 'function') return false;
    if (anchor.getAttribute('aria-hidden') === 'true'
      || (typeof anchor.hasAttribute === 'function'
        && (anchor.hasAttribute('hidden')
          || anchor.hasAttribute('disabled')
          || anchor.hasAttribute('inert')))) return false;
    if (typeof anchor.closest === 'function'
      && anchor.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
    return true;
  }

  function showDecisionPopover(node, anchor) {
    if (!usableDecisionAnchor(anchor)) {
      hideDecisionPopover();
      return;
    }
    const summary = nodeDecisionSummary(node);
    if (!summary) {
      hideDecisionPopover();
      return;
    }
    if (activeDecisionAnchor && activeDecisionAnchor !== anchor) hideDecisionPopover();
    const popover = byId('mapDecisionPopover');
    const title = byId('mapDecisionTitle');
    const body = byId('mapDecisionBody');
    clear(body);
    title.textContent = `${summary.prefix}：${summary.label}`;

    const recorded = recordedNodeDecisions(node);
    if (summary.recorded && recorded.length) {
      recorded.forEach(({ decision, selected }) => {
        const decisionBlock = element('section', { className: 'map-decision-record' });
        decisionBlock.append(element('h4', {
          text: `${DECISION_KIND_LABELS[decision.kind]}：${boundedDecisionText(selected.label, DECISION_MAX_LABEL_SCALARS)}`,
        }));
        if (selected.effect) {
          decisionBlock.append(element('div', {
            className: 'map-decision-selected-effect',
            text: boundedDecisionText(selected.effect, DECISION_MAX_EFFECT_SCALARS),
          }));
        }
        const options = element('ul', { className: 'map-decision-options' });
        decision.options.forEach((option) => {
          const item = element('li', { className: option.selected === true ? 'selected' : '' });
          item.append(element('div', {
            className: 'map-decision-option-label',
            text: `${option.selected === true ? '✓ ' : ''}${boundedDecisionText(option.label, DECISION_MAX_LABEL_SCALARS)}`,
          }));
          if (option.effect) {
            item.append(element('div', {
              className: 'map-decision-option-effect',
              text: boundedDecisionText(option.effect, DECISION_MAX_EFFECT_SCALARS),
            }));
          }
          options.append(item);
        });
        decisionBlock.append(options);
        body.append(decisionBlock);
      });
    } else {
      body.append(
        element('p', { className: 'map-decision-derived-result', text: `${summary.label} · ${summary.effect}` }),
        element('p', { className: 'map-decision-unrecorded', text: '该对局未记录备选项' }),
      );
    }

    activeDecisionAnchor = anchor;
    anchor.setAttribute('aria-describedby', 'mapDecisionPopover');
    popover.hidden = false;
    let anchorRect;
    let popoverRect;
    try {
      anchorRect = anchor.getBoundingClientRect();
      popoverRect = popover.getBoundingClientRect();
    } catch (_error) {
      hideDecisionPopover();
      return;
    }
    const viewportWidth = Math.max(16, Number(window.innerWidth) || 0);
    const viewportHeight = Math.max(16, Number(window.innerHeight) || 0);
    const coordinateValues = [
      anchorRect.left, anchorRect.right, anchorRect.top, anchorRect.bottom,
    ];
    const sizeValues = [popoverRect.width, popoverRect.height, viewportWidth, viewportHeight];
    if (coordinateValues.some((value) => typeof value !== 'number' || !Number.isFinite(value))
      || sizeValues.some((value) => typeof value !== 'number' || !Number.isFinite(value) || value < 0)) {
      hideDecisionPopover();
      return;
    }
    const maxLeft = Math.max(8, viewportWidth - popoverRect.width - 8);
    const left = Math.min(
      Math.max(8, anchorRect.right + 8),
      maxLeft,
    );
    const below = anchorRect.bottom + 8;
    const preferredTop = below + popoverRect.height <= viewportHeight - 8
      ? below
      : anchorRect.top - popoverRect.height - 8;
    const maxTop = Math.max(8, viewportHeight - popoverRect.height - 8);
    const top = Math.min(Math.max(8, preferredTop), maxTop);
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
  }

  function hideDecisionPopover(anchor = null) {
    if (anchor && activeDecisionAnchor && anchor !== activeDecisionAnchor) return;
    const target = anchor || activeDecisionAnchor;
    if (target) target.removeAttribute('aria-describedby');
    byId('mapDecisionPopover').hidden = true;
    activeDecisionAnchor = null;
  }

  function bindDecisionPopover(anchor, node) {
    const state = { hovered: false, focused: false };
    anchor.addEventListener('mouseenter', () => {
      state.hovered = true;
      showDecisionPopover(node, anchor);
    });
    anchor.addEventListener('mouseleave', () => {
      state.hovered = false;
      if (!state.focused) hideDecisionPopover(anchor);
    });
    anchor.addEventListener('focusin', () => {
      state.focused = true;
      showDecisionPopover(node, anchor);
    });
    anchor.addEventListener('focusout', (event) => {
      if (anchor.contains(event.relatedTarget)) return;
      state.focused = false;
      if (!state.hovered) hideDecisionPopover(anchor);
    });
    anchor.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        state.hovered = false;
        state.focused = false;
        hideDecisionPopover();
      }
    });
  }

  function renderNodeArt(group, node) {
    const art = node.art || {};
    if (node.visited && art.kind === 'original' && art.image_url) {
      const image = createSvg('image', {
        href: art.image_url, x: -21, y: -21, width: 42, height: 42,
        preserveAspectRatio: 'xMidYMid meet',
      });
      const fallback = createSvg('text', {
        class: 'map-node-icon', x: 0, y: 8, 'text-anchor': 'middle', display: 'none',
      });
      fallback.textContent = art.emoji || art.letter || '?';
      image.addEventListener('error', () => {
        image.setAttribute('display', 'none');
        fallback.removeAttribute('display');
      });
      group.append(image, fallback);
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
        const quality = measurement && QUALITY_LABELS[measurement.quality]
          ? QUALITY_LABELS[measurement.quality]
          : QUALITY_LABELS.unknown;
        parts.push(`${field.label} ${measurementDisplay(measurement, field)}，${quality}`);
      });
    }
    return parts.join('；');
  }

  function renderNodes(svg, payload, transform) {
    const layer = createSvg('g', { class: 'map-nodes' });
    const summaries = createSvg('g', { class: 'map-decision-summaries' });
    payload.nodes.forEach((node) => {
      const point = transform.point(node);
      const decisionSummary = nodeDecisionSummary(node);
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
        if (decisionSummary) bindDecisionPopover(group, node);
      }
      const title = createSvg('title');
      title.textContent = nodeTooltip(node);
      group.append(title, createSvg('circle', { class: 'map-node-circle', cx: 0, cy: 0, r: 28 }));
      renderNodeArt(group, node);
      layer.append(group);
      if (decisionSummary) renderDecisionSummary(summaries, node, point, transform);
    });
    svg.append(layer, summaries);
  }

  function renderMap(payload) {
    const svg = byId('mapSvg');
    hideDecisionPopover();
    clear(svg);
    const hasDecisionRail = payload.nodes.some((node) => nodeDecisionSummary(node) !== null);
    const transform = createMapTransform(payload.nodes, hasDecisionRail);
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
    let selectedTab = null;
    payload.acts.forEach((act) => {
      const button = element('button', {
        text: `${act.label}${act.available ? ` · ${act.visited_count} 点` : ' · 无记录'}`,
        attrs: {
          type: 'button', role: 'tab', 'aria-selected': act.index === payload.act.index,
          tabindex: act.index === payload.act.index ? '0' : '-1',
          'data-act-index': act.index,
        },
      });
      button.disabled = !act.available;
      button.addEventListener('click', () => loadAct(mapState.runId, act.index, { historyMode: 'replace' }));
      if (act.index === payload.act.index && act.available) selectedTab = button;
      tabs.append(button);
    });
    return selectedTab;
  }

  function handleActTabKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const availableTabs = Array.from(event.currentTarget.querySelectorAll('[role="tab"]:not(:disabled)'));
    if (!availableTabs.length) return;
    const requestedIndex = availableTabs.findIndex(
      (tab) => Number(tab.dataset.actIndex) === mapState.actIndex,
    );
    const currentIndex = requestedIndex >= 0
      ? requestedIndex
      : Math.max(0, availableTabs.indexOf(document.activeElement));
    let nextIndex = currentIndex;
    if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = availableTabs.length - 1;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + availableTabs.length) % availableTabs.length;
    else nextIndex = (currentIndex + 1) % availableTabs.length;
    event.preventDefault();
    const nextActIndex = Number(availableTabs[nextIndex].dataset.actIndex);
    if (Number.isInteger(nextActIndex)) {
      loadAct(mapState.runId, nextActIndex, { historyMode: 'replace', focusActTab: true });
    }
  }

  function showMapPage({ focusPage = true } = {}) {
    const main = byId('dashboardMain');
    const page = byId('runMapPage');
    const detailPanel = byId('detailPanel');
    const openerInsideDetail = mapState.opener && detailPanel.contains(mapState.opener);
    if (!detailPanel.hidden) {
      closeDetail();
      if (openerInsideDetail) {
        const restoredOpener = document.activeElement;
        mapState.opener = isUsableFocusTarget(restoredOpener) ? restoredOpener : null;
      }
    }
    if (!mapState.dashboardHidden) {
      mapState.dashboardHidden = Array.from(main.children).map((child) => ({ child, hidden: child.hidden }));
    }
    mapState.dashboardHidden.forEach(({ child }) => { child.hidden = child !== page; });
    page.hidden = false;
    if (focusPage) page.focus();
  }

  function isUsableFocusTarget(candidate) {
    return Boolean(candidate
      && candidate.isConnected
      && typeof candidate.focus === 'function'
      && !candidate.closest('[hidden], [inert], [aria-hidden="true"]'));
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
    if (isUsableFocusTarget(opener)) opener.focus();
    else byId('dashboardMain').focus();
  }

  function mapLocation(runId, actIndex) {
    return `#run=${encodeURIComponent(runId)}&act=${actIndex}`;
  }

  async function loadAct(runId, actIndex, {
    historyMode = 'none', opener = null, focusActTab = false,
  } = {}) {
    runId = typeof runId === 'string' ? runId.trim() : '';
    if (!runId) {
      setStatus('无法打开地图：缺少对局 ID', 'error');
      return;
    }
    if (opener && opener.isConnected) mapState.opener = opener;
    mapState.runId = runId;
    mapState.actIndex = actIndex;
    showMapPage({ focusPage: !focusActTab });
    byId('runMapTitle').textContent = `对局 ${runId}`;
    byId('mapFallback').hidden = true;
    renderEmpty(byId('actSummary'), '正在重建地图…', 'loading-state');
    renderEmpty(byId('selectedNodeSummary'), '载入地图后，选择一个已访问节点查看收益。');
    hideDecisionPopover();
    clear(byId('mapSvg'));
    if (historyMode === 'push') {
      history.pushState({ view: 'run-map', runId, actIndex, fromDashboard: true }, '', mapLocation(runId, actIndex));
    } else if (historyMode === 'replace') {
      const fromDashboard = Boolean(history.state && history.state.fromDashboard);
      history.replaceState({ view: 'run-map', runId, actIndex, fromDashboard }, '', mapLocation(runId, actIndex));
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
      const selectedTab = renderActTabs(payload);
      if (focusActTab && selectedTab) selectedTab.focus();
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
  byId('actTabs').addEventListener('keydown', handleActTabKeydown);
  window.addEventListener('resize', () => hideDecisionPopover());
  window.addEventListener('scroll', () => hideDecisionPopover(), true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !byId('runMapPage').hidden) {
      event.preventDefault();
      if (!byId('mapDecisionPopover').hidden) {
        hideDecisionPopover();
        return;
      }
      closeMapPage();
    }
  });
  window.addEventListener('popstate', (event) => {
    const route = event.state && event.state.view === 'run-map'
      ? { runId: event.state.runId, actIndex: event.state.actIndex }
      : parseMapLocation();
    if (route) loadAct(route.runId, route.actIndex, { historyMode: 'none' });
    else showDashboardPage();
  });

  window.STS2Map = Object.freeze({
    openRun(runId, opener = null) {
      loadAct(runId, 0, { historyMode: 'push', opener });
    },
  });

  const initialRoute = parseMapLocation();
  if (initialRoute) {
    history.replaceState({ view: 'run-map', ...initialRoute, fromDashboard: false }, '', location.href);
    loadAct(initialRoute.runId, initialRoute.actIndex, { historyMode: 'none' });
  } else {
    history.replaceState({ view: 'dashboard' }, '', location.href);
  }
})();
