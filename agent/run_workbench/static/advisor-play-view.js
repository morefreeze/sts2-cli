'use strict';

// Advisor Play tab: the Advisor mod's real-game runs (the modded profile's run
// histories since the mod was first used), with the autopilot campaign's live
// state.  One page, `#/advisor-play[/<character>]`, drawn from one payload,
// GET /api/advisor-play (agent/run_workbench/advisor_play.py):
//
//   header       the goal (ascension 10 on every character), what is playing
//                now, when the autopilot last stepped, the rotation
//   filter       character (the only filter; everything below follows it)
//   ladder       unlocked ascension per character, the selected runs' results per ascension
//   runs table   newest first
//   HP curves    one small chart per run: HP and max HP per floor, act
//                boundaries, elite / boss floors, the 3 costliest rooms
//   aggregates   mean damage by room kind and act, deaths by encounter,
//                elite fights that started below 50% HP
//   journal      the operator's notes (last 15 lines) and the advisor log tail
//
// Numbers (results, final HP, means, counts) come from the server; the page only
// draws them.  The tab re-reads itself every REFRESH_MS while it is open, and the
// router calls stop() when it is left.
//
// Notation: A<act>F<floor>a<ascension> -- upper-case A is the act, lower-case a
// the ascension; floors are act-local.
//
// Colour follows the dataviz rules: HP is blue, elite floors orange diamonds,
// boss floors green squares (shape and colour), acts an ordinal blue ramp,
// everything else workbench ink.  The workbench is light-only, so there is no
// dark variant here.

window.AdvisorPlayView = (() => {
  const REFRESH_MS = 60 * 1000;
  const AGE_TICK_MS = 15 * 1000;
  const SHOWN_STEP = 30;
  const SERVER_DEFAULT_SHOWN = 30;   // how many newest runs carry their HP curve when `shown` is not sent

  const TYPE_LABELS = {
    monster: '战斗', elite: '精英', boss: 'Boss', rest_site: '休息', shop: '商店',
    treasure: '宝箱', ancient: '开局', event: '事件', unknown: '?',
  };
  const KIND_LABELS = {
    monster: '普通战斗 monster', elite: '精英 elite', boss: 'Boss',
    unknown_fight: '? → 战斗', unknown_other: '? → 事件/其他',
    rest_site: '休息 rest_site', shop: '商店 shop', treasure: '宝箱 treasure', ancient: '开局 ancient',
  };
  const RESULT_LABELS = { win: '胜', loss: '负', abandoned: '弃', running: '进行中' };
  const RESULT_ICONS = { win: '✓', loss: '✗', abandoned: '○', running: '▶' };
  const ACTIVITY = {
    active: { icon: '●', label: '活跃', tone: 'done' },
    quiet: { icon: '◐', label: '较久无新步骤', tone: 'running' },
    stalled: { icon: '⚠', label: '疑似停滞', tone: 'alert' },
    unknown: { icon: '○', label: '没有步骤记录', tone: 'quiet' },
  };
  const OTHER_LABEL = '其他角色';

  let timer = null;
  let ageTimer = null;
  let generation = 0;
  let filter = { character: '', shown: 0 };   // shown 0: the server's default
  let lastSignature = '';
  let lastPayload = null;
  let clockOffset = 0;                         // server clock - browser clock, seconds
  let tip = null;
  // The parts of the page that change every few seconds while a game runs.
  let live = { activity: null, log: null, age: null };

  // ---- formatting -------------------------------------------------------

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  function stamp(epoch) {
    if (!Number.isFinite(epoch)) return '—';
    const date = new Date(epoch * 1000);
    return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function clock(epoch) {
    if (!Number.isFinite(epoch)) return '—';
    const date = new Date(epoch * 1000);
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
  }

  function day(epoch) {
    if (!Number.isFinite(epoch)) return '—';
    const date = new Date(epoch * 1000);
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  }

  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const minutes = Math.floor(seconds / 60);
    if (minutes >= 60) return `${Math.floor(minutes / 60)}时${pad2(minutes % 60)}分`;
    return `${minutes}分`;
  }

  function percent(value) {
    return Number.isFinite(value) ? `${Math.round(value * 100)}%` : '—';
  }

  function cell(value) {
    return value === null || value === undefined || value === '' ? '—' : String(value);
  }

  function ascText(value) {
    return Number.isFinite(value) ? `a${value}` : 'a?';
  }

  function loc(floor) {
    return `A${floor.act}F${floor.floor}`;
  }

  function typeText(floor) {
    if (floor.type === 'unknown') return `?→${floor.kind === 'unknown_fight' ? '战斗' : '其他'}`;
    return TYPE_LABELS[floor.type] || String(floor.type);
  }

  function serverNow() {
    return Date.now() / 1000 + clockOffset;
  }

  // ---- small builders ---------------------------------------------------

  function badge(text, tone) {
    return element('span', { className: `batch-badge batch-badge-${tone}`, text });
  }

  function heading(kicker, title, note) {
    const wrapper = element('div', { className: 'section-heading' });
    const left = element('div');
    left.append(
      element('p', { className: 'section-kicker', text: kicker }),
      element('h2', { text: title }),
    );
    wrapper.append(left);
    if (note) wrapper.append(element('p', { className: 'section-note', text: note }));
    return wrapper;
  }

  function metricCard(label, value, subtext, extraClass) {
    const card = element('article', { className: `metric-card${extraClass ? ` ${extraClass}` : ''}` });
    card.append(
      element('span', { text: label }),
      element('strong', { text: value }),
      element('small', { text: subtext || '' }),
    );
    return card;
  }

  // `numeric`: the column index numbers start at (right-aligned from there), or a list of indexes.
  function headRow(labels, numeric = Infinity) {
    const isNumeric = (index) => (Array.isArray(numeric) ? numeric.includes(index) : index >= numeric);
    const thead = element('thead');
    const row = element('tr');
    labels.forEach((label, index) => row.append(element('th', {
      text: label, className: isNumeric(index) ? 'adv-num' : '', attrs: { scope: 'col' },
    })));
    thead.append(row);
    return thead;
  }

  function line(label, ...nodes) {
    const row = element('div', { className: 'adv-line' });
    row.append(element('small', { text: label }));
    const body = element('div', { className: 'adv-line-body' });
    nodes.forEach((node) => body.append(typeof node === 'string' ? document.createTextNode(node) : node));
    row.append(body);
    return row;
  }

  function strong(text) {
    return element('strong', { text });
  }

  function resultMark(result) {
    const wrapper = element('span', { className: `adv-result adv-result-${result}` });
    wrapper.append(
      element('span', { className: 'adv-icon', text: RESULT_ICONS[result] || '·', attrs: { 'aria-hidden': 'true' } }),
      document.createTextNode(` ${RESULT_LABELS[result] || result}`),
    );
    return wrapper;
  }

  // ---- the shared tooltip (text only; values lead, labels follow) --------

  function ensureTip() {
    if (tip && tip.node.isConnected) return tip;
    const node = element('div', { className: 'adv-tip', attrs: { role: 'tooltip', 'aria-hidden': 'true' } });
    node.hidden = true;
    byId('advisorPlayView').append(node);
    tip = {
      node,
      show(lines, clientX, clientY) {
        clear(node);
        lines.forEach((text, index) => node.append(element('div', { className: index ? 'adv-tip-2' : 'adv-tip-1', text })));
        node.hidden = false;
        const width = node.offsetWidth || 0;
        const height = node.offsetHeight || 0;
        let x = clientX + 14;
        let y = clientY + 14;
        if (typeof innerWidth === 'number' && x + width > innerWidth - 6) x = clientX - width - 14;
        if (typeof innerHeight === 'number' && y + height > innerHeight - 6) y = clientY - height - 14;
        node.style.left = `${Math.max(4, x)}px`;
        node.style.top = `${Math.max(4, y)}px`;
      },
      hide() {
        node.hidden = true;
      },
    };
    return tip;
  }

  // Hover and keyboard focus show the same lines.  `focusable: false` for a cell
  // whose content is already in the table text (it keeps hover, not a tab stop).
  function bindTip(node, lines, focusable = true) {
    if (focusable) node.setAttribute('tabindex', '0');
    const where = (event) => {
      if (event && Number.isFinite(event.clientX)) return [event.clientX, event.clientY];
      const rect = node.getBoundingClientRect();
      return [rect.left + rect.width / 2, rect.bottom];
    };
    const show = (event) => ensureTip().show(lines, ...where(event));
    node.addEventListener('pointermove', show);
    node.addEventListener('pointerleave', () => ensureTip().hide());
    node.addEventListener('focus', () => show(null));
    node.addEventListener('blur', () => ensureTip().hide());
  }

  // ---- header -----------------------------------------------------------

  function renderActivity(container, payload) {
    clear(container);
    const activity = payload.activity || {};
    const spec = ACTIVITY[activity.state] || ACTIVITY.unknown;
    const nodes = [badge(`${spec.icon} ${spec.label}`, spec.tone)];
    live.age = null;
    if (Number.isFinite(activity.last_step_ts)) {
      const age = element('strong', { attrs: { 'data-adv-age': activity.last_step_ts } });
      live.age = age;
      age.textContent = minutesText(serverNow() - activity.last_step_ts);
      nodes.push(
        document.createTextNode(' 距上一步 '), age, document.createTextNode(' 分钟 '),
        element('span', {
          className: 'adv-muted',
          text: `（${clock(activity.last_step_ts)} · #${cell(activity.last_step_n)} ${cell(activity.last_step_text)}）`,
        }),
      );
    }
    const row = line('自动驾驶', ...nodes);
    container.append(...row.children);
  }

  function minutesText(seconds) {
    return Number.isFinite(seconds) ? (Math.max(0, seconds) / 60).toFixed(1) : '—';
  }

  function nowPlayingLine(payload) {
    const current = payload.current;
    const campaign = payload.campaign || {};
    if (current) {
      const last = (current.floors || [])[current.floors.length - 1];
      return line(
        '正在进行',
        badge('▶ 进行中', 'running'),
        document.createTextNode(' '),
        strong(`${current.char} ${ascText(current.asc)}`),
        document.createTextNode(
          ` · 最近楼层 ${last ? loc(last) : '—'}（共 ${cell(current.total_floor)} 层）`
          + ` · HP ${cell(current.hp)}/${cell(current.max_hp)} · 本局已用 ${duration(current.run_time)}`
          + ` · 开始 ${stamp(current.start)}`,
        ),
      );
    }
    const start = payload.activity && payload.activity.last_start;
    const next = campaign.next;
    return line(
      '正在进行',
      badge('○ 空闲', 'quiet'),
      document.createTextNode(' 当前没有进行中的对局'),
      start
        ? element('span', {
          className: 'adv-muted',
          text: ` · 日志里最后一次启动：${start.character} ${ascText(start.ascension)}（${stamp(start.ts)}）`,
        })
        : document.createTextNode(''),
      next
        ? document.createTextNode(` · 下一局：${next.character} ${rotationAscension(next)}`)
        : document.createTextNode(''),
    );
  }

  function rotationAscension(entry) {
    if (entry.ascension === 'max') {
      return Number.isFinite(entry.max_ascension) ? `max（${ascText(entry.max_ascension)}）` : 'max';
    }
    return ascText(Number(entry.ascension));
  }

  function rotationLine(payload) {
    const campaign = payload.campaign || {};
    if (!campaign.ok) return line('轮换', element('span', { className: 'adv-muted', text: 'campaign.json 不可读' }));
    const nodes = [];
    const playing = payload.current ? payload.current.char : null;
    (campaign.rotation || []).forEach((entry, index) => {
      if (index) nodes.push(document.createTextNode(' → '));
      nodes.push(entry.character === playing
        ? element('strong', { className: 'adv-playing', text: entry.character })
        : document.createTextNode(entry.character));
    });
    if (!nodes.length) nodes.push(element('span', { className: 'adv-muted', text: '（空）' }));
    nodes.push(document.createTextNode(' ↻'));
    if (campaign.next) {
      nodes.push(document.createTextNode(
        ` · ${campaign.next_after_current ? '此局之后' : '下一局'}：`),
      strong(campaign.next.character), document.createTextNode(` ${rotationAscension(campaign.next)}`));
    }
    if ((campaign.not_in_rotation || []).length) {
      nodes.push(element('span', {
        className: 'adv-muted', text: ` · 不在轮换：${campaign.not_in_rotation.join(', ')}`,
      }));
    }
    nodes.push(document.createTextNode(' · '), campaign.enabled === true
      ? badge('✓ 战役启用', 'done')
      : badge(campaign.enabled === false ? '✗ 战役停用' : '? 战役状态未知', campaign.enabled === false ? 'alert' : 'quiet'));
    if (campaign.command) nodes.push(document.createTextNode(` · command：${campaign.command}`));
    return line('轮换', ...nodes);
  }

  function headerPanel(payload) {
    const panel = element('section', { className: 'panel adv-head' });
    panel.append(heading(
      'Advisor Play',
      `实机战役 · 全角色进阶 ${payload.goal.ascension}`,
      `Advisor mod 在真实游戏里打的对局（modded 档，${day(payload.since)} 起）。本页每 60 秒自动刷新。`,
    ));
    const goal = payload.goal;
    const summary = payload.summary || {};
    const kpis = element('div', { className: 'adv-kpis' });
    const hero = element('article', { className: 'metric-card metric-card-primary adv-hero' });
    const meter = element('div', {
      className: 'adv-meter',
      attrs: { role: 'img', 'aria-label': `进阶等级合计 ${goal.levels} / ${goal.levels_goal}` },
    });
    const fill = element('i');
    fill.style.width = `${goal.levels_goal ? Math.min(100, (100 * goal.levels) / goal.levels_goal) : 0}%`;
    meter.append(fill);
    const value = element('strong', { text: String(goal.levels) });
    value.append(element('span', { className: 'adv-hero-of', text: ` / ${goal.levels_goal}` }));
    hero.append(
      element('span', { text: `目标 · 全角色进阶 ${goal.ascension}` }),
      value,
      meter,
      element('small', { text: `五个角色已解锁的进阶等级之和（每个角色封顶 ${goal.ascension}）` }),
    );
    const scope = filter.character ? `${characterLabel(filter.character)} · ` : '';
    kpis.append(
      hero,
      metricCard(
        `${scope}已结束对局`, cell(summary.runs),
        `${cell(summary.wins)} 胜 · ${cell(summary.losses)} 负${summary.abandoned ? ` · ${summary.abandoned} 弃` : ''}`,
      ),
      metricCard(`${scope}胜率`, percent(summary.win_rate), '胜 ÷（胜 + 负）'),
      metricCard(`${scope}平均每局用时`, duration(summary.mean_run_time), '游戏计时，含暂停'),
    );
    panel.append(kpis);

    const facts = element('div', { className: 'adv-lines' });
    facts.append(nowPlayingLine(payload));
    const activity = element('div', { className: 'adv-line' });
    live.activity = activity;
    renderActivity(activity, payload);
    facts.append(activity, rotationLine(payload));
    panel.append(facts);
    if (Array.isArray(payload.problems) && payload.problems.length) {
      panel.append(element('p', {
        className: 'section-note batch-problems',
        text: `有 ${payload.problems.length} 个输入无法读取，已跳过：${payload.problems.slice(0, 3).join('；')}`,
      }));
    }
    return panel;
  }

  // ---- filter -----------------------------------------------------------

  function characterLabel(id) {
    return id === 'OTHER' ? OTHER_LABEL : id;
  }

  function filterPanel(payload) {
    const panel = element('section', { className: 'panel adv-filter', attrs: { 'aria-label': '筛选' } });
    const label = element('label', { className: 'filter-field-baseline', text: '角色' });
    const select = element('select', { attrs: { id: 'advisorPlayCharacter', 'aria-describedby': 'advisorPlayFilterNote' } });
    const total = (payload.character_options || []).reduce((sum, option) => sum + option.count, 0);
    select.append(element('option', { text: `全部角色（${total} 局）`, attrs: { value: '' } }));
    (payload.character_options || []).forEach((option) => {
      select.append(element('option', {
        text: `${characterLabel(option.id)}（${option.count} 局）`, attrs: { value: option.id },
      }));
    });
    select.value = filter.character;
    select.addEventListener('change', () => navigate(advisorPlayRoute(select.value)));
    label.append(select);
    panel.append(
      label,
      element('p', {
        className: 'section-note',
        attrs: { id: 'advisorPlayFilterNote' },
        text: '下面的对局列表、阶梯里的对局结果、HP 曲线和汇总都按所选角色统计；目标进度、正在进行的对局、轮换和日志不受筛选影响。',
      }),
    );
    return panel;
  }

  // ---- ladder -----------------------------------------------------------

  function ladderPanel(payload) {
    const panel = element('section', { className: 'panel adv-ladder-panel' });
    panel.append(heading(
      '进阶阶梯', '每个角色解锁到哪一级',
      '解锁的最高进阶取自 progress.save；格子下面是所选对局在该进阶的结果。深色格是当前最高进阶（战役的 “max” 打的那一级）。',
    ));
    const scroll = element('div', { className: 'table-scroll' });
    const table = element('table', { className: 'runs-table adv-ladder' });
    const labels = ['角色', '已解锁'];
    for (let asc = 1; asc <= payload.goal.ascension; asc += 1) labels.push(`a${asc}`);
    labels.push('累计 胜 / 负（含 mod 之前）');
    table.append(headRow(labels));
    const tbody = element('tbody');
    (payload.characters || []).forEach((row) => {
      const tr = element('tr');
      tr.append(element('th', { className: 'adv-char', text: row.character, attrs: { scope: 'row' } }));
      const unlocked = element('td', { className: 'adv-unlocked' });
      unlocked.append(
        strong(String(row.max_ascension)),
        document.createTextNode(` / ${row.goal}`),
        element('small', {
          className: 'adv-muted',
          text: row.left === 0 ? '✓ 已达成' : `还差 ${row.left}`,
        }),
      );
      tr.append(unlocked);
      row.ascensions.forEach((asc) => {
        const td = element('td', { className: 'adv-asc' });
        td.append(element('div', {
          className: `adv-seg${asc.unlocked ? ' on' : ''}${asc.top && asc.unlocked ? ' top' : ''}`,
        }));
        const results = element('div', { className: 'adv-asc-res' });
        const parts = [
          [asc.wins, '✓', 'win'], [asc.losses, '✗', 'loss'],
          [asc.abandoned, '○', 'abandoned'], [asc.running, '▶', 'running'],
        ].filter(([count]) => count > 0);
        parts.forEach(([count, icon, result], index) => {
          if (index) results.append(document.createTextNode(' '));
          results.append(
            element('span', { className: `adv-icon adv-result-${result}`, text: icon, attrs: { 'aria-hidden': 'true' } }),
            document.createTextNode(String(count)),
          );
        });
        td.append(results);
        const lines = [
          `${row.character} ${ascText(asc.ascension)} · ${asc.unlocked ? '已解锁' : '未解锁'}`,
          parts.length
            ? `所选对局：${asc.wins} 胜 ${asc.losses} 负${asc.abandoned ? ` ${asc.abandoned} 弃` : ''}${asc.running ? ` ${asc.running} 进行中` : ''}`
            : '所选对局：没打过',
        ];
        bindTip(td, lines, false);
        td.setAttribute('aria-label', lines.join('，'));
        tr.append(td);
      });
      const total = element('td', { className: 'adv-total' });
      if (Number.isFinite(row.total_wins) && Number.isFinite(row.total_losses)) {
        const games = row.total_wins + row.total_losses;
        total.append(
          document.createTextNode(`${row.total_wins} 胜 / ${row.total_losses} 负 `),
          element('span', { className: 'adv-muted', text: `（${games ? percent(row.total_wins / games) : '—'}）` }),
        );
      } else {
        total.textContent = '—';
      }
      tr.append(total);
      tbody.append(tr);
    });
    table.append(tbody);
    scroll.append(table);
    panel.append(scroll);
    const legend = element('div', { className: 'adv-legend' });
    [['adv-sw on', '已解锁'], ['adv-sw', '未解锁']].forEach(([cls, text]) => {
      const item = element('span');
      item.append(element('i', { className: cls }), document.createTextNode(text));
      legend.append(item);
    });
    [['win', '胜'], ['loss', '负'], ['abandoned', '放弃'], ['running', '进行中']].forEach(([result, text]) => {
      const item = element('span');
      item.append(
        element('span', { className: `adv-icon adv-result-${result}`, text: RESULT_ICONS[result], attrs: { 'aria-hidden': 'true' } }),
        document.createTextNode(` ${text}`),
      );
      legend.append(item);
    });
    panel.append(legend);
    return panel;
  }

  // ---- runs table -------------------------------------------------------

  function allRuns(payload) {
    const runs = Array.isArray(payload.runs) ? payload.runs : [];
    return payload.current && payload.current_selected ? [payload.current, ...runs] : runs;
  }

  function runsPanel(payload) {
    const panel = element('section', { className: 'panel runs-table-panel adv-runs-panel' });
    panel.append(heading(
      '对局', '对局列表',
      '新的在前。最终 HP 取自游戏的历史文件的最后一层；楼层是各幕内的楼层（A幕F层）。',
    ));
    const runs = allRuns(payload);
    if (!runs.length) {
      panel.append(element('div', { className: 'empty-state', text: '还没有符合筛选的对局。' }));
      return panel;
    }
    const scroll = element('div', { className: 'table-scroll adv-runs-scroll' });
    const table = element('table', { className: 'runs-table adv-runs' });
    table.append(headRow(
      ['#', '开始', '角色', '进阶', '结果', '抵达', '最终 HP', '击杀者', '牌组（+升级）', '遗物', '用时'],
      [0, 6, 8, 9, 10],
    ));
    const tbody = element('tbody');
    runs.forEach((run) => {
      const tr = element('tr', { className: run.result === 'running' ? 'runs-table-row-in-progress' : '' });
      const reach = element('td');
      reach.append(
        document.createTextNode(cell(run.end)),
        element('span', { className: 'adv-muted', text: ` · ${cell(run.total_floor)} 层` }),
      );
      const result = element('td');
      result.append(resultMark(run.result));
      if (run.origin === 'campaign_runs') {
        result.append(element('small', { className: 'adv-muted', text: ' 仅模组日志' }));
      }
      const relics = element('td', { className: 'adv-num', text: cell(run.relics) });
      if (Array.isArray(run.relic_names) && run.relic_names.length) relics.setAttribute('title', run.relic_names.join(', '));
      tr.append(
        element('td', { className: 'adv-num', text: String(run.number) }),
        element('td', { text: stamp(run.start) }),
        element('td', { className: 'adv-char', text: run.char }),
        element('td', { text: ascText(run.asc) }),
        result,
        reach,
        element('td', { className: 'adv-num', text: run.hp === null || run.hp === undefined ? '—' : `${run.hp}/${cell(run.max_hp)}` }),
        element('td', { className: 'adv-killer', text: run.killed_by || '—' }),
        element('td', {
          className: 'adv-num',
          text: run.deck === null || run.deck === undefined ? '—' : `${run.deck}（+${cell(run.upgraded)}）`,
        }),
        relics,
        element('td', { className: 'adv-num', text: duration(run.run_time) }),
      );
      tbody.append(tr);
    });
    table.append(tbody);
    scroll.append(table);
    panel.append(scroll);
    return panel;
  }

  // ---- HP curve of one run ----------------------------------------------

  const HP = { W: 380, H: 196, ML: 34, MR: 10, MT: 20, MB: 26, DOMAIN: 48 };

  function svgNode(tag, attrs, text) {
    const node = svgElement(tag, attrs);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function hpChart(run) {
    const floors = run.floors;
    const domain = Math.max(HP.DOMAIN, floors.length, 2);
    const pw = HP.W - HP.ML - HP.MR;
    const ph = HP.H - HP.MT - HP.MB;
    const stepX = pw / (domain - 1);
    const maxes = floors.map((f) => f.max).filter(Number.isFinite);
    const ymax = Math.max(20, Math.ceil(Math.max(1, ...maxes) / 20) * 20);
    const X = (g) => HP.ML + (g - 1) * stepX;
    const Y = (v) => HP.MT + ph - (v / ymax) * ph;
    const svg = svgNode('svg', {
      class: 'adv-hp', viewBox: `0 0 ${HP.W} ${HP.H}`, role: 'img', tabindex: '0',
      'aria-label': `${run.char} ${ascText(run.asc)} 逐层 HP 曲线，${floors.length} 层`,
    });
    [0, ymax / 2, ymax].forEach((v) => {
      const y = Y(v);
      svg.append(
        svgNode('line', { class: 'adv-grid', x1: HP.ML, x2: HP.W - HP.MR, y1: y.toFixed(1), y2: y.toFixed(1) }),
        svgNode('text', { class: 'adv-tick', x: HP.ML - 5, y: (y + 3).toFixed(1), 'text-anchor': 'end' }, String(v)),
      );
    });
    for (let act = 1; act <= (run.acts || 0); act += 1) {
      const first = floors.findIndex((f) => f.act === act) + 1;
      if (first === 0) continue;
      const left = X(first) - stepX / 2;
      if (act > 1) {
        svg.append(svgNode('line', {
          class: 'adv-actline', x1: left.toFixed(1), x2: left.toFixed(1), y1: HP.MT - 4, y2: HP.MT + ph,
        }));
      }
      svg.append(svgNode('text', { class: 'adv-actlbl', x: (left + 3).toFixed(1), y: HP.MT - 7 }, `A${act}`));
      [5, 10, 15].forEach((localFloor) => {
        const g = first + localFloor - 1;
        if (floors[g - 1] && floors[g - 1].act === act) {
          svg.append(svgNode('text', {
            class: 'adv-tick', x: X(g).toFixed(1), y: HP.MT + ph + 12, 'text-anchor': 'middle',
          }, `F${localFloor}`));
        }
      });
    }
    svg.append(svgNode('line', { class: 'adv-axis', x1: HP.ML, x2: HP.W - HP.MR, y1: HP.MT + ph, y2: HP.MT + ph }));
    const points = (key) => floors
      .map((f, i) => (Number.isFinite(f[key]) ? `${X(i + 1).toFixed(1)},${Y(f[key]).toFixed(1)}` : null))
      .filter(Boolean).join(' ');
    if (points('max')) svg.append(svgNode('polyline', { class: 'adv-line-max', points: points('max') }));
    if (points('hp')) svg.append(svgNode('polyline', { class: 'adv-line-hp', points: points('hp') }));
    floors.forEach((f, i) => {
      if (!Number.isFinite(f.hp)) return;
      const x = X(i + 1);
      const y = Y(f.hp);
      if (f.elite) {
        svg.append(svgNode('path', {
          class: 'adv-mark-elite',
          d: `M${x.toFixed(1)} ${(y - 5.5).toFixed(1)} L${(x + 5.5).toFixed(1)} ${y.toFixed(1)} `
            + `L${x.toFixed(1)} ${(y + 5.5).toFixed(1)} L${(x - 5.5).toFixed(1)} ${y.toFixed(1)} Z`,
        }));
      } else if (f.boss) {
        svg.append(svgNode('rect', {
          class: 'adv-mark-boss', x: (x - 4.5).toFixed(1), y: (y - 4.5).toFixed(1), width: 9, height: 9, rx: 1.5,
        }));
      }
    });
    // The 3 costliest rooms: "−N" beside the point, nudged clear of earlier labels.
    const placed = [];
    floors.map((f, i) => ({ f, g: i + 1 }))
      .filter(({ f }) => f.dmg > 0 && Number.isFinite(f.hp))
      .sort((a, b) => b.f.dmg - a.f.dmg || a.g - b.g)
      .slice(0, 3)
      .sort((a, b) => a.g - b.g)
      .forEach(({ f, g }) => {
        const x = X(g);
        const y = Y(f.hp);
        const label = `−${f.dmg}`;
        const w = 6.2 * label.length + 2;
        const candidates = [[x, y + 16, 'middle'], [x, y - 9, 'middle'], [x - 8, y - 2, 'end'], [x + 8, y - 2, 'start']];
        let pick = null;
        candidates.some(([cx, cy, anchor]) => {
          const x0 = anchor === 'middle' ? cx - w / 2 : (anchor === 'end' ? cx - w : cx);
          const box = [x0, cy - 9, x0 + w, cy + 2];
          const outside = box[0] < HP.ML - 2 || box[2] > HP.W - 1 || box[1] < HP.MT - 6 || box[3] > HP.MT + ph + 1;
          const clash = placed.some((b) => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]));
          if (outside || clash) return false;
          pick = [cx, cy, anchor, box];
          return true;
        });
        if (!pick) pick = [x, y + 16, 'middle', [x - w / 2, y + 7, x + w / 2, y + 18]];
        placed.push(pick[3]);
        svg.append(svgNode('text', {
          class: 'adv-dlbl', x: pick[0].toFixed(1), y: pick[1].toFixed(1), 'text-anchor': pick[2],
        }, label));
      });
    let lastIndex = -1;
    floors.forEach((f, i) => { if (Number.isFinite(f.hp)) lastIndex = i; });
    if (lastIndex >= 0) {
      svg.append(svgNode('circle', {
        class: 'adv-mark-end', cx: X(lastIndex + 1).toFixed(1), cy: Y(floors[lastIndex].hp).toFixed(1), r: 4.5,
      }));
    }
    const cross = svgNode('line', {
      class: 'adv-cross', x1: 0, x2: 0, y1: HP.MT - 4, y2: HP.MT + ph, visibility: 'hidden',
    });
    const dot = svgNode('circle', { class: 'adv-focus-dot', cx: 0, cy: 0, r: 4.5, visibility: 'hidden' });
    svg.append(cross, dot, svgNode('rect', {
      class: 'adv-hit', x: HP.ML, y: HP.MT - 8, width: pw, height: ph + 8 + HP.MB - 6,
    }));
    attachCrosshair(svg, run, floors.map((_, i) => X(i + 1)), floors.map((f) => (Number.isFinite(f.hp) ? Y(f.hp) : Y(0))), cross, dot);
    return svg;
  }

  // The crosshair finds the floor nearest the pointer; arrow keys step through floors.
  function attachCrosshair(svg, run, xs, ys, cross, dot) {
    const floors = run.floors;
    if (!floors.length) return;
    let current = -1;
    const show = (index, clientX, clientY) => {
      current = index;
      const f = floors[index];
      cross.setAttribute('x1', xs[index].toFixed(1));
      cross.setAttribute('x2', xs[index].toFixed(1));
      cross.setAttribute('visibility', 'visible');
      dot.setAttribute('cx', xs[index].toFixed(1));
      dot.setAttribute('cy', ys[index].toFixed(1));
      dot.setAttribute('visibility', Number.isFinite(f.hp) ? 'visible' : 'hidden');
      ensureTip().show([
        `${cell(f.hp)} / ${cell(f.max)} HP${f.dmg ? `   −${f.dmg}` : ''}`,
        `${loc(f)} · ${typeText(f)}${f.name ? ` · ${f.name}` : ''}`,
        `${run.char} ${ascText(run.asc)}`,
      ], clientX, clientY);
    };
    const off = () => {
      cross.setAttribute('visibility', 'hidden');
      dot.setAttribute('visibility', 'hidden');
      ensureTip().hide();
      current = -1;
    };
    const nearest = (event) => {
      const rect = svg.getBoundingClientRect();
      const x = ((event.clientX - rect.left) * HP.W) / (rect.width || HP.W);
      let best = 0;
      let bestDistance = Infinity;
      xs.forEach((value, index) => {
        const distance = Math.abs(value - x);
        if (distance < bestDistance) { bestDistance = distance; best = index; }
      });
      return best;
    };
    svg.addEventListener('pointermove', (event) => show(nearest(event), event.clientX, event.clientY));
    svg.addEventListener('pointerleave', off);
    svg.addEventListener('blur', off);
    svg.addEventListener('focus', () => {
      const rect = svg.getBoundingClientRect();
      show(xs.length - 1, rect.left + rect.width / 2, rect.top + rect.height / 2);
    });
    svg.addEventListener('keydown', (event) => {
      let index = current < 0 ? xs.length - 1 : current;
      if (event.key === 'ArrowLeft') index = Math.max(0, index - 1);
      else if (event.key === 'ArrowRight') index = Math.min(xs.length - 1, index + 1);
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = xs.length - 1;
      else return;
      event.preventDefault();
      const rect = svg.getBoundingClientRect();
      show(index, rect.left + (rect.width * xs[index]) / HP.W, rect.top + rect.height * 0.4);
    });
  }

  function floorTable(run) {
    const table = element('table', { className: 'runs-table adv-floor-table' });
    table.append(headRow(['楼层', '类型', '房间', '受伤', 'HP', '上限'], 3));
    const tbody = element('tbody');
    run.floors.forEach((f) => {
      const tr = element('tr');
      tr.append(
        element('td', { text: loc(f) }),
        element('td', { text: typeText(f) }),
        element('td', { text: f.name || '—' }),
        element('td', { className: 'adv-num', text: String(f.dmg) }),
        element('td', { className: 'adv-num', text: cell(f.hp) }),
        element('td', { className: 'adv-num', text: cell(f.max) }),
      );
      tbody.append(tr);
    });
    table.append(tbody);
    const wrapper = element('div', { className: 'table-scroll adv-floor-scroll' });
    wrapper.append(table);
    return wrapper;
  }

  function runCard(run) {
    const card = element('article', { className: `adv-run-card adv-run-${run.result}` });
    const head = element('div', { className: 'adv-card-head' });
    const top = element('div', { className: 'adv-card-top' });
    top.append(
      element('span', { className: 'adv-num-tag', text: `#${run.number}` }),
      document.createTextNode(' '),
      strong(run.char),
      document.createTextNode(` ${ascText(run.asc)} `),
      resultMark(run.result),
      element('span', { className: 'adv-card-time', text: stamp(run.start) }),
    );
    head.append(top, element('div', { className: 'adv-muted adv-card-sub', text:
      `${cell(run.end)} · ${cell(run.total_floor)} 层 · HP ${cell(run.hp)}/${cell(run.max_hp)} · ${duration(run.run_time)}`
      + (run.killed_by ? ` · ${run.killed_by}` : '') }));
    card.append(head);
    if (!run.floors || !run.floors.length) {
      card.append(element('p', { className: 'adv-muted adv-card-empty', text: '没有逐层记录（只有模组日志里的一行）。' }));
      return card;
    }
    card.append(hpChart(run));
    const worst = run.floors.filter((f) => f.dmg > 0)
      .sort((a, b) => b.dmg - a.dmg || (a.act - b.act) || (a.floor - b.floor)).slice(0, 3);
    const list = element('div', { className: 'adv-worst' });
    list.append(element('small', { text: '最痛 3 间' }));
    if (!worst.length) list.append(element('span', { className: 'adv-muted', text: ' 无伤害记录' }));
    worst.forEach((f) => {
      const item = element('span', { className: 'adv-worst-item' });
      item.append(strong(`−${f.dmg}`), document.createTextNode(` ${loc(f)} ${f.name || typeText(f)}`));
      list.append(item);
    });
    card.append(list);
    const details = element('details', { attrs: { 'data-key': `floors-${run.start}-${run.char}` } });
    details.append(element('summary', { text: '逐层数据表' }), floorTable(run));
    card.append(details);
    return card;
  }

  function chartsPanel(payload) {
    const panel = element('section', { className: 'panel adv-charts-panel' });
    panel.append(heading(
      'HP 曲线', '逐层 HP',
      '每局一张小图，新的在前；鼠标悬停或用键盘 ← → 查看每一层。',
    ));
    const legend = element('div', { className: 'adv-legend' });
    const key = (build, text) => {
      const item = element('span');
      item.append(build(), document.createTextNode(` ${text}`));
      legend.append(item);
    };
    key(() => { const s = svgNode('svg', { width: 26, height: 8, class: 'adv-key' }); s.append(svgNode('line', { class: 'adv-line-hp', x1: 1, x2: 25, y1: 4, y2: 4 })); return s; }, '当前 HP（每层结束后）');
    key(() => { const s = svgNode('svg', { width: 26, height: 8, class: 'adv-key' }); s.append(svgNode('line', { class: 'adv-line-max', x1: 1, x2: 25, y1: 4, y2: 4 })); return s; }, '最大 HP');
    key(() => { const s = svgNode('svg', { width: 14, height: 14, class: 'adv-key' }); s.append(svgNode('path', { class: 'adv-mark-elite', d: 'M7 1 L13 7 L7 13 L1 7 Z' })); return s; }, '精英');
    key(() => { const s = svgNode('svg', { width: 14, height: 14, class: 'adv-key' }); s.append(svgNode('rect', { class: 'adv-mark-boss', x: 2, y: 2, width: 10, height: 10, rx: 1.5 })); return s; }, 'Boss');
    legend.append(element('span', { text: '−N：单个房间受伤最重的 3 间' }));
    legend.append(element('span', { className: 'adv-muted', text: '竖线是换幕（A1 / A2 / A3），横轴固定 48 层，纵轴 0 到本局最大 HP' }));
    panel.append(legend);

    const runs = allRuns(payload);
    const withFloors = runs.filter((run) => Array.isArray(run.floors));
    if (!runs.length) {
      panel.append(element('div', { className: 'empty-state', text: '还没有符合筛选的对局。' }));
      return panel;
    }
    const grid = element('div', { className: 'adv-run-grid' });
    withFloors.forEach((run) => grid.append(runCard(run)));
    panel.append(grid);
    if (withFloors.length < runs.length) {
      const more = element('div', { className: 'adv-more' });
      const button = element('button', {
        className: 'quiet-button', text: `显示更多（已显示 ${withFloors.length} / ${runs.length} 局）`, attrs: { type: 'button' },
      });
      button.addEventListener('click', () => {
        filter.shown = (filter.shown || SERVER_DEFAULT_SHOWN) + SHOWN_STEP;
        lastSignature = '';
        load(generation, false);
      });
      more.append(button);
      panel.append(more);
    }
    return panel;
  }

  // ---- aggregates -------------------------------------------------------

  function barPath(x, y, w, h, r) {
    if (w <= r) return `M${x} ${y} h${Math.max(w, 0.5).toFixed(1)} v${h} h-${Math.max(w, 0.5).toFixed(1)} Z`;
    return `M${x} ${y} h${(w - r).toFixed(1)} a${r} ${r} 0 0 1 ${r} ${r} v${h - 2 * r} a${r} ${r} 0 0 1 -${r} ${r} h-${(w - r).toFixed(1)} Z`;
  }

  function damagePanel(payload) {
    const panel = element('section', { className: 'panel adv-agg' });
    panel.append(heading('汇总', '各幕各房间类型的平均受伤', null));
    const rows = payload.aggregates.damage || [];
    const shown = rows.filter((row) => row.acts.some((a) => a.mean > 0));
    const legend = element('div', { className: 'adv-legend' });
    [1, 2, 3].forEach((act) => {
      const item = element('span');
      item.append(element('i', { className: `adv-sw act-${act}` }), document.createTextNode(`A${act}`));
      legend.append(item);
    });
    legend.append(element('span', {
      className: 'adv-muted',
      text: `所选 ${payload.summary.runs} 局，每层都算（含 0 伤害的楼层）`,
    }));
    panel.append(legend);
    if (!shown.length) {
      panel.append(element('div', { className: 'empty-state', text: '暂时没有伤害数据。' }));
    } else {
      const vmax = Math.max(...shown.flatMap((row) => row.acts.map((a) => a.mean || 0)));
      const scaleMax = Math.max(10, Math.ceil(vmax / 10) * 10);
      const bar = 10;
      const gap = 2;
      const rowGap = 14;
      const rowH = 3 * bar + 2 * gap;
      const left = 150;
      const right = 46;
      const W = 560;
      const H = 20 + shown.length * (rowH + rowGap);
      const pw = W - left - right;
      const svg = svgNode('svg', {
        class: 'adv-bars', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '各幕各房间类型的平均受伤',
      });
      const stepTick = scaleMax >= 20 ? scaleMax / 4 : scaleMax;
      for (let t = 0; t <= scaleMax; t += stepTick) {
        const x = left + (t / scaleMax) * pw;
        svg.append(
          svgNode('line', { class: 'adv-grid', x1: x.toFixed(1), x2: x.toFixed(1), y1: 2, y2: H - 14 }),
          svgNode('text', { class: 'adv-tick', x: x.toFixed(1), y: H - 3, 'text-anchor': 'middle' }, String(t)),
        );
      }
      let y = 6;
      shown.forEach((row) => {
        svg.append(svgNode('text', {
          class: 'adv-rlbl', x: left - 8, y: (y + rowH / 2 + 3.5).toFixed(1), 'text-anchor': 'end',
        }, KIND_LABELS[row.kind] || row.kind));
        row.acts.forEach((a, index) => {
          const by = y + index * (bar + gap);
          const group = svgNode('g', {});
          const lines = a.n
            ? [`${KIND_LABELS[row.kind] || row.kind} · A${a.act}`, `平均受伤 ${a.mean.toFixed(1)}（n=${a.n}，最大 ${a.max}）`]
            : [`${KIND_LABELS[row.kind] || row.kind} · A${a.act}`, '没有样本'];
          group.append(svgNode('rect', { class: 'adv-hitb', x: left, y: by - 1, width: pw + right, height: bar + 2 }));
          if (a.n) {
            const w = (a.mean / scaleMax) * pw;
            group.append(
              svgNode('path', { class: `adv-bar act-${a.act}`, d: barPath(left, by, w, bar, 4) }),
              svgNode('text', { class: 'adv-vlbl', x: (left + Math.max(w, 0.5) + 5).toFixed(1), y: by + 8.5 }, a.mean.toFixed(1)),
            );
          } else {
            group.append(svgNode('text', { class: 'adv-vlbl', x: left + 4, y: by + 8.5 }, '—'));
          }
          bindTip(group, lines);
          group.setAttribute('aria-label', lines.join('，'));
          svg.append(group);
        });
        y += rowH + rowGap;
      });
      svg.append(svgNode('line', { class: 'adv-axis', x1: left, x2: left, y1: 2, y2: H - 14 }));
      panel.append(svg);
    }
    const details = element('details', { attrs: { 'data-key': 'damage-table' } });
    details.append(element('summary', { text: '表格视图' }));
    const table = element('table', { className: 'runs-table adv-small-table' });
    table.append(headRow(['房间类型', 'A1 平均（n）', 'A2 平均（n）', 'A3 平均（n）', '合计平均（n）'], 1));
    const tbody = element('tbody');
    rows.forEach((row) => {
      const tr = element('tr');
      tr.append(element('td', { text: KIND_LABELS[row.kind] || row.kind }));
      row.acts.forEach((a) => tr.append(element('td', { className: 'adv-num', text: a.n ? `${a.mean.toFixed(1)}（${a.n}）` : '—' })));
      tr.append(element('td', { className: 'adv-num', text: `${row.mean.toFixed(1)}（${row.n}）` }));
      tbody.append(tr);
    });
    table.append(tbody);
    const wrap = element('div', { className: 'table-scroll' });
    wrap.append(table);
    details.append(wrap);
    panel.append(details);
    return panel;
  }

  function deathsPanel(payload) {
    const panel = element('section', { className: 'panel adv-agg' });
    panel.append(heading('汇总', '死于哪个遭遇', null));
    const deaths = payload.aggregates.deaths || [];
    const total = deaths.reduce((sum, row) => sum + row.count, 0);
    panel.append(element('p', {
      className: 'section-note', text: `所选对局里的失败 ${total} 局（胜利与放弃不计）`,
    }));
    if (!deaths.length) {
      panel.append(element('div', { className: 'empty-state', text: '还没有失败的对局。' }));
      return panel;
    }
    const top = Math.max(3, ...deaths.map((row) => row.count));
    deaths.forEach((row) => {
      const item = element('div', { className: 'adv-death' });
      const track = element('div', { className: 'adv-death-bar' });
      const fill = element('i');
      fill.style.width = `${(100 * row.count) / top}%`;
      track.append(fill);
      item.append(
        element('div', { className: 'adv-death-name', text: row.encounter }),
        track,
        element('div', { className: 'adv-death-count', text: String(row.count) }),
        element('div', {
          className: 'adv-muted adv-death-who',
          text: row.runs.map((r) => `${r.character} ${ascText(r.ascension)} ${cell(r.end)}`).join('；'),
        }),
      );
      panel.append(item);
    });
    return panel;
  }

  function elitesPanel(payload) {
    const panel = element('section', { className: 'panel adv-agg adv-agg-wide' });
    panel.append(heading(
      '汇总', '低血量打精英',
      '战前 HP 低于 50% 的精英战（战前 = 上一层结束时的 HP）。',
    ));
    const elites = payload.aggregates.elites || { total: 0, low: 0, buckets: [], low_fights: [] };
    const top = element('div', { className: 'adv-elite-top' });
    top.append(metricCard(
      '低于 50% 血量开打的精英', `${elites.low} / ${elites.total}`,
      elites.total ? `${percent(elites.low / elites.total)} 的精英战` : '还没有精英战',
    ));
    const buckets = element('table', { className: 'runs-table adv-small-table' });
    buckets.append(headRow(['战前 HP', '场数', '占比', '平均受伤'], 1));
    const bbody = element('tbody');
    elites.buckets.forEach((bucket) => {
      const tr = element('tr');
      tr.append(
        element('td', { text: bucket.label.replace('>=', '≥').replace('-', '–') }),
        element('td', { className: 'adv-num', text: String(bucket.n) }),
        element('td', { className: 'adv-num', text: elites.total ? percent(bucket.n / elites.total) : '—' }),
        element('td', { className: 'adv-num', text: bucket.mean_dmg === null ? '—' : bucket.mean_dmg.toFixed(1) }),
      );
      bbody.append(tr);
    });
    buckets.append(bbody);
    top.append(buckets);
    panel.append(top);
    if (!elites.low_fights.length) {
      panel.append(element('div', { className: 'empty-state', text: '没有低于 50% 血量开打的精英战。' }));
      return panel;
    }
    const table = element('table', { className: 'runs-table adv-small-table' });
    table.append(headRow(['对局', '楼层', '精英', '战前 HP', '受伤', '战后 HP'], 3));
    const tbody = element('tbody');
    elites.low_fights.forEach((fight) => {
      const tr = element('tr');
      tr.append(
        element('td', { text: `${fight.character} ${ascText(fight.ascension)}` }),
        element('td', { text: `A${fight.act}F${fight.floor}` }),
        element('td', { text: fight.name || '—' }),
        element('td', { className: 'adv-num', text: `${fight.hp_before}/${fight.max_before}（${Math.round(fight.ratio * 100)}%）` }),
        element('td', { className: 'adv-num', text: fight.dmg ? `−${fight.dmg}` : '0' }),
        element('td', { className: 'adv-num', text: cell(fight.hp_after) }),
      );
      tbody.append(tr);
    });
    table.append(tbody);
    const wrap = element('div', { className: 'table-scroll' });
    wrap.append(table);
    panel.append(wrap);
    if (elites.low > elites.low_fights.length) {
      panel.append(element('p', { className: 'section-note', text: `只列最新 ${elites.low_fights.length} 场。` }));
    }
    return panel;
  }

  // ---- journal, log, notes ----------------------------------------------

  function renderLogTail(pre, payload) {
    pre.textContent = (payload.log_tail || []).join('\n');
  }

  function journalPanel(payload) {
    const panel = element('section', { className: 'panel adv-journal-panel' });
    const lines = payload.journal || [];
    panel.append(heading('笔记', '战役日志', `journal.txt 最后 ${lines.length} 行（原文）`));
    panel.append(element('pre', {
      className: 'adv-pre', text: lines.length ? lines.join('\n') : '（journal.txt 不存在或为空）',
    }));
    const details = element('details', { attrs: { 'data-key': 'advisor-log' } });
    details.append(element('summary', { text: `Sts2Advisor.log 末尾 ${(payload.log_tail || []).length} 行` }));
    const pre = element('pre', { className: 'adv-pre' });
    live.log = pre;
    renderLogTail(pre, payload);
    details.append(pre);
    panel.append(details);
    return panel;
  }

  function notesPanel(payload) {
    const checks = payload.checks || {};
    const notes = [];
    notes.push(
      `campaign_runs.jsonl ${cell(checks.log_rows)} 行 · 历史文件 ${cell(checks.history_runs)} 局`
      + `${(checks.missing_in_log || []).length ? `（其中 ${checks.missing_in_log.length} 局没有模组日志行：模组日志从战役模式开始才记）` : ''}`,
    );
    if ((checks.missing_in_history || []).length) {
      notes.push(`${checks.missing_in_history.length} 局只在模组日志里、历史文件缺失：已列出，但没有逐层数据。`);
    }
    if ((checks.mismatched || []).length) notes.push(`模组日志与历史文件的胜负/进阶不一致：${checks.mismatched.join(', ')}`);
    if (checks.zero_hp_victories) {
      notes.push(`模组日志里有 ${checks.zero_hp_victories} 局胜利的 hp=0（读取时机问题），本页的最终 HP 一律取历史文件末层。`);
    }
    notes.push('进行中的一局取自 current_run.save（只读），不计入阶梯、汇总的已结束对局；它的“最近楼层”是存档里最后一条楼层记录。');
    notes.push('楼层记法：A=幕，F=该幕内的楼层，a=进阶（如 A3F15a10）。模组日志里胜利时的 A3F16 比历史文件的末层 A3F15 大 1，本页用历史文件的。');
    const wrapper = element('div', { className: 'adv-notes' });
    notes.forEach((text) => wrapper.append(element('p', { className: 'section-note', text })));
    return wrapper;
  }

  // ---- drawing, refreshing ----------------------------------------------

  function openKeys(root) {
    const keys = new Set();
    if (!root || typeof root.querySelectorAll !== 'function') return keys;
    root.querySelectorAll('details[data-key]').forEach((node) => {
      if (node.open) keys.add(node.getAttribute('data-key'));
    });
    return keys;
  }

  function restoreOpen(root, keys) {
    if (!keys.size || typeof root.querySelectorAll !== 'function') return;
    root.querySelectorAll('details[data-key]').forEach((node) => {
      if (keys.has(node.getAttribute('data-key'))) node.open = true;
    });
  }

  function draw(payload) {
    const body = byId('advisorPlayBody');
    if (payload.error) {
      renderEmpty(body, `Advisor Play 数据读取失败：${payload.error}`, 'error-state');
      return;
    }
    const keys = openKeys(body);
    const aggregates = element('div', { className: 'adv-agg-grid' });
    aggregates.append(damagePanel(payload), deathsPanel(payload), elitesPanel(payload));
    // Built off-screen and swapped in one go, so a refresh does not jump the scroll position.
    body.replaceChildren(
      headerPanel(payload), filterPanel(payload), ladderPanel(payload), runsPanel(payload),
      chartsPanel(payload), aggregates, journalPanel(payload), notesPanel(payload),
    );
    restoreOpen(body, keys);
  }

  // What changes every few seconds while a game runs (the autopilot's last step,
  // the log tail) is patched in place, so a re-read does not rebuild the page.
  function patchLive(payload) {
    if (live.activity) renderActivity(live.activity, payload);
    if (live.log) renderLogTail(live.log, payload);
  }

  // "minutes since the last step" keeps counting between reads.
  function tick() {
    if (live.age) {
      live.age.textContent = minutesText(serverNow() - Number(live.age.getAttribute('data-adv-age')));
    }
  }

  function showNotice(text) {
    const notice = byId('advisorPlayNotice');
    if (!notice) return;
    notice.hidden = !text;
    notice.textContent = text || '';
  }

  function endpoint() {
    const query = [];
    if (filter.character) query.push(`character=${encodeURIComponent(filter.character)}`);
    if (filter.shown > 0) query.push(`shown=${filter.shown}`);
    return `/api/advisor-play${query.length ? `?${query.join('&')}` : ''}`;
  }

  function scheduleRefresh(token) {
    timer = setTimeout(() => load(token, true), REFRESH_MS);
  }

  async function load(token, refreshing) {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (token !== generation) return;
    try {
      const payload = await getJSON(endpoint());
      if (token !== generation) return;
      if (Number.isFinite(payload.now)) clockOffset = payload.now - Date.now() / 1000;
      const signature = JSON.stringify(payload, (key, value) => (
        key === 'now' || key === 'activity' || key === 'log_tail' ? undefined : value));
      lastPayload = payload;
      if (signature !== lastSignature) {
        lastSignature = signature;
        draw(payload);
      }
      patchLive(payload);
      showNotice('');
      if (!refreshing) setStatus('已载入 Advisor Play');
    } catch (error) {
      if (token !== generation) return;
      if (refreshing || lastPayload) {
        // Keep what is on screen and try again on the next tick.
        showNotice(`自动刷新失败：${error.message}`);
      } else {
        renderEmpty(byId('advisorPlayBody'), `Advisor Play 读取失败：${error.message}`, 'error-state');
        setStatus(`Advisor Play 读取失败：${error.message}`, 'error');
      }
    }
    if (token === generation) scheduleRefresh(token);
  }

  function stop() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (ageTimer !== null) clearInterval(ageTimer);
    ageTimer = null;
    generation += 1;
    if (tip) tip.hide();
    showNotice('');
  }

  async function render(route) {
    stop();
    const token = generation;
    const character = route && typeof route.character === 'string' ? route.character : '';
    if (character !== filter.character) filter = { character, shown: 0 };
    lastSignature = '';
    lastPayload = null;
    live = { activity: null, log: null, age: null };
    renderEmpty(byId('advisorPlayBody'), '正在读取 Advisor Play…', 'loading-state');
    setStatus('正在读取 Advisor Play…', 'busy');
    ageTimer = setInterval(tick, AGE_TICK_MS);
    await load(token, false);
  }

  return { render, stop, draw, hpChart, minutesText, duration, stamp };
})();
