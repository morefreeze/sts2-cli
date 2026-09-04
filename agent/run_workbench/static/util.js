'use strict';

// Shared DOM/formatting helpers and label tables used by every workbench
// view module (tree.js, cohort-view.js, runs-table.js, run-view.js, app.js)
// and by map.js. This file must load first: it defines plain top-level
// `function`/`const` declarations, which in a classic (non-module) script
// become properties of the global object, so later scripts can reference
// them as bare identifiers without any import wiring.

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

// Generic busy toggle: disables every control opted in via
// `data-busy-toggle` and marks #contentPane aria-busy. Decoupled from any
// specific view's element ids so tree/cohort/run views can each mark their
// own interactive controls without this shared helper knowing about them.
function setBusy(isBusy) {
  state.busy = isBusy;
  const main = byId('contentPane');
  if (main) main.setAttribute('aria-busy', String(isBusy));
  document.querySelectorAll('[data-busy-toggle]').forEach((node) => {
    node.disabled = isBusy;
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

function renderEmpty(container, message, className = 'empty-state') {
  clear(container);
  container.append(element('div', { className, text: message }));
}

/**
 * Render options into a <select>. An option may carry an optional `group`
 * label; consecutive options sharing one are wrapped in an <optgroup>. Options
 * without a group are appended directly, so flat callers are unaffected.
 */
function setSelectOptions(select, options, emptyLabel, preferred) {
  clear(select);
  if (emptyLabel !== null) {
    select.append(element('option', { text: emptyLabel, attrs: { value: '' } }));
  }
  let openGroup = null;
  let openGroupLabel = null;
  options.forEach((option) => {
    const node = element('option', {
      text: option.label,
      attrs: { value: option.value },
    });
    const group = option.group || null;
    if (group === null) {
      openGroup = null;
      openGroupLabel = null;
      select.append(node);
      return;
    }
    if (group !== openGroupLabel) {
      openGroupLabel = group;
      openGroup = element('optgroup', { attrs: { label: group } });
      select.append(openGroup);
    }
    openGroup.append(node);
  });
  if (preferred && options.some((option) => option.value === preferred)) {
    select.value = preferred;
  }
}
