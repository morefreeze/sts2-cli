'use strict';

// 回归/评测 tab: one game in detail (#/regression/game/<source id>).
//
// Everything on the page comes from /api/game-detail (game_detail.py):
//
//   路线   every map choice: the nodes offered vs the one taken
//   牌组   the final deck by type, relics, potions, and what each room changed
//   房间   one row per room (collapsed); open one to see its out-of-combat
//          choices (offered vs chosen) or, for a fight, every player turn:
//          enemies and intents, the solver's plan, and a row per step with the
//          legal (candidate) actions, the taken one highlighted, and where the
//          game stopped following the plan.
//
// Room and turn bodies are built when they are first opened, so a 70-floor
// game stays light.  A game that is still being played refreshes every
// REFRESH_MS; leaving the page stops that (the router calls stop()).

window.GameDetail = (() => {
  const REFRESH_MS = 30 * 1000;

  const ROOM_LABELS = {
    Monster: '普通战斗', Elite: '精英战斗', Boss: '首领战斗', Event: '事件', Shop: '商店',
    RestSite: '篝火', Treasure: '宝箱', Ancient: '先古之民', Unknown: '未知房间', Map: '地图',
  };
  const NODE_LABELS = {
    Monster: '战斗', Elite: '精英', Boss: '首领', Unknown: '？', Shop: '商店', RestSite: '篝火',
    Treasure: '宝箱', Ancient: '先古', Event: '事件',
  };
  const OUTCOME_LABELS = {
    win: '通关', dead: '阵亡', in_progress: '进行中', stuck: '卡死', crash: '崩溃', timeout: '超时',
    reset_failure: '重置失败', invalid: '无效记录', unknown: '未知',
  };
  const TYPE_ORDER = ['Attack', 'Skill', 'Power', 'Curse', 'Status', 'Quest'];
  const TYPE_LABELS = {
    Attack: '攻击', Skill: '技能', Power: '能力', Curse: '诅咒', Status: '状态', Quest: '任务', '?': '其他',
  };
  const INTENT_LABELS = {
    Attack: '攻击', Defend: '防御', Buff: '强化', Debuff: '削弱', DebuffStrong: '强力削弱',
    StatusCard: '塞入状态牌', CardDebuff: '污染牌组', Summon: '召唤', Stun: '眩晕', Sleep: '沉睡',
    Heal: '治疗', Escape: '逃跑', Unknown: '未知',
  };
  const KIND_LABELS = {
    play_card: '打出', use_potion: '使用药水', end_turn: '结束回合', select_cards: '选牌', other: '其他动作',
  };
  const SOURCE_LABELS = {
    plan: '计划', heuristic: '启发式', off_plan: '偏离计划', choice: '选牌',
  };
  const PLAN_STATUS_LABELS = {
    executed: '已执行', skipped: '跳过', not_executed: '未执行', refused: '被拒绝',
  };
  const END_REASON_LABELS = {
    completed: '计划完成', exhausted: '打完但回合未结束', truncated: '计划中断', error: '求解器出错',
  };
  const TRUNCATE_REASONS = {
    card_missing: '手牌里没有计划要打的牌（预测与实际不符）',
    refused: '引擎拒绝了计划的出牌',
    no_progress: '一张牌都没打出',
    interrupted: '中途被打断',
  };
  const DIVERGENCE_KINDS = ['replan', 'truncated', 'exhausted', 'mismatch', 'skipped', 'refused',
    'plan_error', 'heuristic'];
  const BAD_DIVERGENCE = new Set(['truncated', 'exhausted', 'mismatch', 'refused', 'plan_error']);
  const FILTERS = [
    ['all', '全部'],
    ['combat', '战斗'],
    ['elite', '精英/首领'],
    ['other', '事件/商店/篝火'],
    ['diverged', '偏离计划'],
  ];

  let timer = null;
  let generation = 0;
  let lastSignature = '';
  let rooms = {};            // room id -> its <details>, so the route strip can open one

  // ---- names -----------------------------------------------------------

  function bareId(value) {
    return String(value === null || value === undefined ? '' : value)
      .toUpperCase().replace(/^(CARD|POTION|RELIC|POWER|MONSTER)\./, '');
  }

  function table(loc, kind) {
    return (loc && loc[kind]) || {};
  }

  function plain(name) {
    return String(name === null || name === undefined ? '' : name).replace(/\+\d*$/, '');
  }

  // A card's Chinese name (with "+" when upgraded); the English name or id
  // when the game has no translation.
  function cardName(loc, card) {
    if (!card) return '？';
    const zh = (card.id && table(loc, 'card_id')[bareId(card.id)])
      || (card.card_id && table(loc, 'card_id')[bareId(card.card_id)])
      || table(loc, 'card')[plain(card.name || card.card_name)];
    const base = zh || card.name || card.card_name || card.id || card.card_id || '？';
    return card.upgraded ? `${base}+` : base;
  }

  function plannedCardName(loc, action) {
    const zh = table(loc, 'card_id')[bareId(action.card_id)]
      || table(loc, 'card')[plain(action.card_name)];
    const base = zh || action.card_name || action.card_id || '？';
    return Number(action.upgrade_level) > 0 ? `${base}+` : base;
  }

  function potionName(loc, potion) {
    if (!potion) return '？';
    return table(loc, 'potion_id')[bareId(potion.id || potion.potion_id)]
      || table(loc, 'potion')[potion.name || potion.potion_name]
      || potion.name || potion.potion_name || potion.id || potion.potion_id || '？';
  }

  function monsterName(loc, name) {
    return table(loc, 'monster')[name] || name || '？';
  }

  function relicName(loc, name) {
    return table(loc, 'relic')[name] || name || '？';
  }

  function powerName(loc, name) {
    return table(loc, 'power')[name] || name || '？';
  }

  function intentText(intent) {
    const label = INTENT_LABELS[intent.type] || intent.type || '？';
    const damage = Number.isFinite(intent.damage) ? ` ${intent.damage}` : '';
    const hits = Number.isFinite(intent.hits) && intent.hits > 1 ? `×${intent.hits}` : '';
    return `${label}${damage}${hits}`;
  }

  function num(value) {
    return Number.isFinite(value) ? String(value) : '—';
  }

  function cell(value) {
    return value === null || value === undefined || value === '' ? '—' : String(value);
  }

  function signed(value) {
    if (!Number.isFinite(value)) return '—';
    return value > 0 ? `+${value}` : String(value);
  }

  // ---- small pieces ----------------------------------------------------

  function chip(text, tone, title) {
    const node = element('span', { className: `gd-chip${tone ? ` gd-chip-${tone}` : ''}`, text });
    if (title) node.setAttribute('title', title);
    return node;
  }

  function badge(text, tone) {
    return element('span', { className: `batch-badge batch-badge-${tone || 'quiet'}`, text });
  }

  function heading(kicker, title, note) {
    const wrapper = element('div', { className: 'section-heading' });
    const left = element('div');
    left.append(element('p', { className: 'section-kicker', text: kicker }), element('h2', { text: title }));
    wrapper.append(left);
    if (note) wrapper.append(element('p', { className: 'section-note', text: note }));
    return wrapper;
  }

  function subheading(text) {
    return element('h3', { className: 'gd-subheading', text });
  }

  function facts(items) {
    const list = element('div', { className: 'batch-facts' });
    items.forEach(([label, value]) => {
      const fact = element('div', { className: 'batch-fact' });
      fact.append(element('small', { text: label }), element('strong', { text: cell(value) }));
      list.append(fact);
    });
    return list;
  }

  function button(label, action) {
    const node = element('button', { className: 'quiet-button', text: label, attrs: { type: 'button' } });
    node.addEventListener('click', action);
    return node;
  }

  function line(...parts) {
    const node = element('p', { className: 'gd-line' });
    parts.forEach((part) => {
      if (part === null || part === undefined || part === false) return;
      node.append(typeof part === 'string' ? element('span', { text: part }) : part);
    });
    return node;
  }

  function formatSearchTime(ms) {
    if (!Number.isFinite(ms)) return '—';
    if (ms >= 60000) return `${(ms / 60000).toFixed(1)} 分钟`;
    if (ms >= 1000) return `${(ms / 1000).toFixed(1)} 秒`;
    return `${Math.round(ms)} 毫秒`;
  }

  function formatSpan(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`;
    if (seconds >= 60) return `${Math.round(seconds / 60)} 分钟`;
    return `${Math.round(seconds)} 秒`;
  }

  // Open a collapsed <details> on first view and build its body then.
  function lazyDetails(className, summaryNode, build, { open = false } = {}) {
    const details = element('details', { className });
    const summary = element('summary');
    summary.append(summaryNode);
    details.append(summary);
    let built = false;
    const fill = () => {
      if (built || !details.open) return;
      built = true;
      const body = element('div', { className: `${className}-body gd-body` });
      build(body);
      details.append(body);
    };
    details.addEventListener('toggle', fill);
    details.ensure = fill;   // the route strip opens a room without waiting for the toggle event
    if (open) {
      details.open = true;
      details.setAttribute('open', '');
      fill();
    }
    return details;
  }

  // ---- header ----------------------------------------------------------

  function outcomeTone(outcome) {
    if (outcome === 'win') return 'done';
    if (outcome === 'in_progress') return 'running';
    if (outcome === 'dead') return 'quiet';
    return 'alert';
  }

  function solverLine(summary) {
    const solver = summary.solver || {};
    if (!solver.used) return '本局没有使用求解器规划（启发式出牌）';
    return `计划 ${num(solver.plans)} 次 · 重新规划 ${num(solver.replans)} 次 · 求解器出错 ${num(solver.errors)} 次`
      + ` · 偏离回合 ${num(solver.diverged_turns)} · 启发式出牌 ${num(solver.heuristic_steps)} 步`
      + ` · 搜索总耗时 ${formatSearchTime(solver.search_ms)}`;
  }

  // Back to wherever the reader came from (the lane list, the map view); the
  // lane / batch list when the page was opened by URL.
  function goBack(fallback) {
    const hasHistory = typeof history !== 'undefined' && history && history.state && history.state.view;
    if (hasHistory) history.back();
    else navigate(fallback);
  }

  function drawHeader(body, detail) {
    const summary = detail.summary || {};
    const top = element('div', { className: 'batch-page-head' });
    const inLane = summary.batch_id && summary.lane;
    const fallback = inLane ? laneRoute(summary.batch_id, summary.lane) : regressionRoute();
    top.append(button('← 返回', () => goBack(fallback)));
    if (detail.source_id) {
      top.append(button('地图视图', () => navigate(runRoute('', { kind: 'source', id: detail.source_id }))));
    }
    body.append(top);
    const title = element('div', { className: 'section-heading' });
    const left = element('div');
    left.append(
      element('p', { className: 'section-kicker', text: '单局详情' }),
      element('h2', { text: `${cell(summary.character)} · 种子 ${cell(summary.seed)}` }),
    );
    const badges = element('div', { className: 'batch-badges' });
    badges.append(badge(OUTCOME_LABELS[summary.outcome] || cell(summary.outcome), outcomeTone(summary.outcome)));
    if (detail.live) badges.append(badge('进行中', 'running'));
    if (detail.regression === false) badges.append(badge('非回归日志', 'kind'));
    left.append(badges);
    title.append(left);
    body.append(title);
    body.append(facts([
      ['批次', summary.batch_id],
      ['通道', summary.lane],
      ['局号', Number.isFinite(summary.game_index)
        ? `${summary.game_index}${Number.isFinite(summary.games_total) ? ` / ${summary.games_total}` : ''}` : null],
      ['进阶', summary.ascension],
      ['最远', summary.end_label
        ? `${summary.end_label}（第 ${cell(summary.max_global_floor)} 层）` : summary.max_global_floor],
      ['生命', Number.isFinite(summary.end_hp) ? `${num(summary.start_hp)} → ${num(summary.end_hp)}` : null],
      ['累计失血', summary.total_hp_loss],
      ['房间 / 战斗 / 精英', `${num(summary.room_count)} / ${num(summary.combat_count)} / ${num(summary.elite_count)}`],
      ['牌组 / 遗物', `${num(summary.deck_size)} / ${num(summary.relic_count)}`],
      ['开始时间', Number.isFinite(summary.started_at) ? formatTime(summary.started_at) : null],
      ['耗时', Number.isFinite(summary.duration_seconds) ? formatSpan(summary.duration_seconds) : null],
      ['游戏版本', summary.game_version],
      ['策略', summary.policy],
      ['实验', summary.experiment],
    ]));
    body.append(line(element('strong', { text: '求解器 ' }), solverLine(summary)));
    if (summary.final_error) {
      body.append(element('p', { className: 'section-note batch-problems', text: `日志以引擎错误结束：${summary.final_error}` }));
    }
    if (summary.complete === false && summary.outcome !== 'in_progress' && summary.outcome !== 'win'
      && summary.outcome !== 'dead') {
      body.append(element('p', { className: 'section-note', text: '这局没有正常结束（没有 game_over 记录）。' }));
    }
  }

  // ---- route -----------------------------------------------------------

  function nodeLabel(type) {
    return NODE_LABELS[type] || type || '？';
  }

  function drawRoute(body, detail) {
    const section = element('section', { className: 'panel gd-route' });
    section.append(heading('路线', '路线选择', '每个楼层：实际走的节点高亮，同层其余可选节点置灰；“？”节点点开后显示实际进入的房间。'));
    const route = Array.isArray(detail.route) ? detail.route : [];
    if (!route.length) {
      renderEmpty(section, '这局没有地图选择记录。');
      body.append(section);
      return;
    }
    const byAct = new Map();
    route.forEach((entry) => {
      const act = Number.isFinite(entry.act) ? entry.act : 0;
      if (!byAct.has(act)) byAct.set(act, []);
      byAct.get(act).push(entry);
    });
    byAct.forEach((entries, act) => {
      const strip = element('div', { className: 'gd-route-act' });
      strip.append(element('strong', { className: 'gd-route-act-title', text: act ? `第 ${act} 幕` : '路线' }));
      const row = element('div', { className: 'gd-route-strip' });
      entries.forEach((entry) => {
        const step = element('div', { className: 'gd-route-cell' });
        step.append(element('small', { text: Number.isFinite(entry.floor) ? `F${entry.floor}` : '？' }));
        const nodes = element('div', { className: 'gd-route-nodes' });
        const options = Array.isArray(entry.options) ? entry.options : [];
        options.forEach((option, index) => {
          const taken = index === entry.taken_index;
          const node = element('span', {
            className: `gd-node${taken ? ' gd-node-taken' : ' gd-node-alt'}`,
            text: nodeLabel(option.type),
            attrs: { title: `列 ${cell(option.col)} · 行 ${cell(option.row)}${taken ? ' · 已选择' : ''}` },
          });
          nodes.append(node);
        });
        if (!options.length) nodes.append(element('span', { className: 'gd-node gd-node-alt', text: '—' }));
        step.append(nodes);
        const entered = entry.entered;
        const takenType = entry.taken && entry.taken.type;
        if (entered) {
          const label = ROOM_LABELS[entered.room_type] || entered.room_type;
          const open = element('button', {
            className: 'gd-route-enter',
            text: takenType === 'Unknown' || takenType !== entered.room_type ? `→ ${label}` : '进入',
            attrs: { type: 'button', title: `查看 ${cell(entered.label)} ${label}` },
          });
          open.addEventListener('click', () => openRoom(entered.room_id));
          step.append(open);
        } else if (!entry.taken) {
          step.append(element('small', { className: 'gd-route-pending', text: '待选' }));
        } else {
          // a chest (or any node that asked for no decision) has no room to open
          step.append(element('small', { className: 'gd-dim', text: takenType === 'Treasure' ? '无需操作' : '未记录' }));
        }
        row.append(step);
      });
      strip.append(row);
      section.append(strip);
    });
    body.append(section);
  }

  function openRoom(roomId) {
    const details = rooms[roomId];
    if (!details) return;
    details.open = true;
    details.setAttribute('open', '');
    if (typeof details.ensure === 'function') details.ensure();
    if (typeof details.scrollIntoView === 'function') details.scrollIntoView({ block: 'center' });
  }

  // ---- deck ------------------------------------------------------------

  function cardChip(loc, card, count) {
    const text = `${cardName(loc, card)}${count > 1 ? ` ×${count}` : ''}`;
    return chip(text, card.upgraded ? 'upgraded' : '', card.type ? TYPE_LABELS[card.type] || card.type : null);
  }

  function deltaText(loc, delta) {
    const parts = [];
    const group = (cards) => {
      const counts = new Map();
      cards.forEach((card) => {
        const name = cardName(loc, card);
        counts.set(name, (counts.get(name) || 0) + 1);
      });
      return Array.from(counts, ([name, count]) => (count > 1 ? `${name}×${count}` : name)).join('、');
    };
    if (delta.added && delta.added.length) parts.push(`获得 ${group(delta.added)}`);
    if (delta.removed && delta.removed.length) parts.push(`移除 ${group(delta.removed)}`);
    if (delta.upgraded && delta.upgraded.length) parts.push(`升级 ${group(delta.upgraded)}`);
    return parts.join('；');
  }

  function drawDeck(body, detail) {
    const loc = detail.loc;
    const deck = detail.deck || {};
    const section = element('section', { className: 'panel gd-deck' });
    section.append(heading('牌组', `最终牌组（${num(deck.size)} 张）`, '带“+”的是已升级的牌；同名牌合并显示数量。'));
    const groups = Array.isArray(deck.final) ? deck.final : [];
    if (!groups.length) {
      section.append(element('p', { className: 'section-note', text: '日志里没有牌组记录。' }));
    }
    const types = [...TYPE_ORDER, ...Array.from(new Set(groups.map((card) => card.type || '?')))
      .filter((type) => !TYPE_ORDER.includes(type))];
    types.forEach((type) => {
      const members = groups.filter((card) => (card.type || '?') === type);
      if (!members.length) return;
      const total = members.reduce((sum, card) => sum + (card.count || 1), 0);
      const row = element('div', { className: 'gd-deck-type' });
      row.append(element('strong', { text: `${TYPE_LABELS[type] || type}（${total}）` }));
      const chips = element('div', { className: 'gd-chips' });
      members.forEach((card) => chips.append(cardChip(loc, card, card.count || 1)));
      row.append(chips);
      section.append(row);
    });
    const relics = Array.isArray(deck.relics) ? deck.relics : [];
    const potions = Array.isArray(deck.potions) ? deck.potions : [];
    const relicRow = element('div', { className: 'gd-deck-type' });
    relicRow.append(element('strong', { text: `遗物（${relics.length}）` }));
    const relicChips = element('div', { className: 'gd-chips' });
    relics.forEach((relic) => relicChips.append(chip(relicName(loc, relic.name))));
    if (!relics.length) relicChips.append(element('span', { className: 'gd-dim', text: '无' }));
    relicRow.append(relicChips);
    section.append(relicRow);
    const potionRow = element('div', { className: 'gd-deck-type' });
    potionRow.append(element('strong', { text: `药水（${potions.length}）` }));
    const potionChips = element('div', { className: 'gd-chips' });
    potions.forEach((potion) => potionChips.append(chip(potionName(loc, potion))));
    if (!potions.length) potionChips.append(element('span', { className: 'gd-dim', text: '无' }));
    potionRow.append(potionChips);
    section.append(potionRow);

    const changed = (Array.isArray(detail.rooms) ? detail.rooms : []).filter((room) => {
      const delta = room.deck_delta || {};
      return (delta.added && delta.added.length) || (delta.removed && delta.removed.length)
        || (delta.upgraded && delta.upgraded.length) || (room.relics_gained && room.relics_gained.length)
        || (room.potions_gained && room.potions_gained.length) || (room.potions_used && room.potions_used.length);
    });
    const timeline = element('details', { className: 'gd-deck-timeline' });
    timeline.append(element('summary', { text: `牌组 / 遗物 / 药水的逐房间变化（${changed.length} 个房间）` }));
    const list = element('ul', { className: 'gd-delta-list' });
    changed.forEach((room) => {
      const parts = [deltaText(loc, room.deck_delta || {})];
      if (room.relics_gained && room.relics_gained.length) {
        parts.push(`遗物 +${room.relics_gained.map((name) => relicName(loc, name)).join('、')}`);
      }
      if (room.potions_gained && room.potions_gained.length) {
        parts.push(`药水 +${room.potions_gained.map((name) => table(loc, 'potion')[name] || name).join('、')}`);
      }
      if (room.potions_used && room.potions_used.length) {
        parts.push(`用掉 ${room.potions_used.map((name) => table(loc, 'potion')[name] || name).join('、')}`);
      }
      list.append(element('li', {
        text: `${room.label} ${ROOM_LABELS[room.room_type] || room.room_type}：${parts.filter(Boolean).join('；')}`,
      }));
    });
    if (!changed.length) list.append(element('li', { text: '没有变化。' }));
    timeline.append(list);
    section.append(timeline);
    body.append(section);
  }

  // ---- out-of-combat choices --------------------------------------------

  function offeredCards(loc, container, offered, pickedIndexes, skipped) {
    const chips = element('div', { className: 'gd-chips' });
    offered.forEach((card) => {
      const picked = pickedIndexes.includes(card.index);
      const priceText = Number.isFinite(card.price) ? ` · ${card.price} 金` : '';
      const node = chip(`${cardName(loc, card)}${Number.isFinite(card.cost) ? `（${card.cost}）` : ''}${priceText}`,
        picked ? 'taken' : 'alt', [card.type, card.rarity].filter(Boolean).join(' '));
      chips.append(node);
    });
    if (skipped) chips.append(chip('跳过', 'taken'));
    container.append(chips);
  }

  function drawChoice(body, loc, choice) {
    const block = element('div', { className: 'gd-choice' });
    if (choice.type === 'card_reward') {
      block.append(subheading(choice.source === 'combat' ? '卡牌奖励' : '事件卡牌奖励'));
      offeredCards(loc, block, choice.offered || [], Number.isFinite(choice.picked) ? [choice.picked] : [], choice.skipped);
      const verdict = choice.skipped ? '跳过了奖励'
        : choice.picked_card ? `选择了 ${cardName(loc, choice.picked_card)}` : '尚未选择';
      block.append(line(verdict, Number.isFinite(choice.gold_earned) ? ` · 获得金币 ${choice.gold_earned}` : null));
    } else if (choice.type === 'event') {
      const zh = table(loc, 'event')[choice.event_name];
      block.append(subheading(`事件：${zh || choice.event_name || '？'}`));
      if (choice.description) block.append(element('p', { className: 'gd-dim', text: choice.description }));
      const list = element('ul', { className: 'gd-options' });
      (choice.options || []).forEach((option) => {
        const taken = option.index === choice.chosen;
        const item = element('li', { className: `gd-option${taken ? ' gd-option-taken' : ''}${option.is_locked ? ' gd-option-locked' : ''}` });
        item.append(element('strong', { text: `${taken ? '▶ ' : ''}${option.title_zh || option.title}` }));
        const description = option.description_zh || option.description;
        if (description) item.append(element('span', { className: 'gd-dim', text: ` — ${description}` }));
        if (option.is_locked) item.append(chip('不可选', 'alt'));
        list.append(item);
      });
      block.append(list);
      if (choice.left) block.append(line('离开了事件'));
    } else if (choice.type === 'shop') {
      block.append(subheading(`商店（金币 ${num(choice.gold_start)} → ${num(choice.gold_end)}）`));
      const bought = new Set();
      (choice.purchases || []).forEach((purchase) => {
        if (purchase.item && purchase.kind !== 'remove') bought.add(`${purchase.kind}:${purchase.item.index}`);
      });
      const stock = choice.stock || {};
      const rowOf = (label, items, kind) => {
        if (!items || !items.length) return;
        const row = element('div', { className: 'gd-deck-type' });
        row.append(element('strong', { text: label }));
        const chips = element('div', { className: 'gd-chips' });
        items.forEach((item) => {
          const name = kind === 'card' ? cardName(loc, item)
            : kind === 'relic' ? relicName(loc, item.name) : potionName(loc, item);
          const sold = bought.has(`${kind}:${item.index}`);
          chips.append(chip(`${name} · ${num(item.price)} 金${item.on_sale ? '（折扣）' : ''}`, sold ? 'taken' : 'alt'));
        });
        row.append(chips);
        block.append(row);
      };
      rowOf('卡牌', stock.cards, 'card');
      rowOf('遗物', stock.relics, 'relic');
      rowOf('药水', stock.potions, 'potion');
      if (Number.isFinite(stock.card_removal_cost)) block.append(line(`移除卡牌价格 ${stock.card_removal_cost} 金`));
      const purchases = Array.isArray(choice.purchases) ? choice.purchases : [];
      if (!purchases.length) block.append(line('没有购买任何东西'));
      purchases.forEach((purchase) => {
        let text;
        if (purchase.kind === 'remove') text = `移除 ${purchase.item ? cardName(loc, purchase.item) : '一张牌'}`;
        else if (purchase.kind === 'card') text = `购买卡牌 ${cardName(loc, purchase.item)}`;
        else if (purchase.kind === 'relic') text = `购买遗物 ${relicName(loc, purchase.item && purchase.item.name)}`;
        else text = `购买药水 ${potionName(loc, purchase.item)}`;
        block.append(line(chip(text, 'taken'), ` ${num(purchase.price)} 金`));
      });
    } else if (choice.type === 'rest') {
      block.append(subheading('篝火'));
      const chips = element('div', { className: 'gd-chips' });
      (choice.options || []).forEach((option) => {
        chips.append(chip(option.name_zh || option.name, option.index === choice.chosen ? 'taken' : 'alt'));
      });
      block.append(chips);
      if (choice.upgraded_card) block.append(line(`升级了 ${cardName(loc, choice.upgraded_card)}`));
      if (Number.isFinite(choice.hp_before) && choice.chosen_id === 'HEAL') {
        block.append(line(`生命 ${num(choice.hp_before)} → ${num(choice.hp_after)}`));
      }
    } else if (choice.type === 'card_select') {
      block.append(subheading(choice.after === 'event' ? '事件中的选牌'
        : choice.after === 'shop' ? '商店选牌' : choice.after === 'rest' ? '篝火选牌' : '选牌'));
      offeredCards(loc, block, choice.offered || [], choice.chosen || [], false);
    } else if (choice.type === 'bundle_select') {
      block.append(subheading('卡牌包选择'));
      (choice.bundles || []).forEach((bundle) => {
        const row = element('div', { className: 'gd-deck-type' });
        row.append(element('strong', { text: `${bundle.index === choice.chosen ? '▶ ' : ''}卡牌包 ${num(bundle.index)}` }));
        const chips = element('div', { className: 'gd-chips' });
        (bundle.cards || []).forEach((card) => chips.append(chip(cardName(loc, card), bundle.index === choice.chosen ? 'taken' : 'alt')));
        row.append(chips);
        block.append(row);
      });
    } else {
      block.append(subheading(choice.type || '选择'));
    }
    body.append(block);
  }

  // ---- combat ----------------------------------------------------------

  function targetName(loc, enemies, index) {
    const enemy = (enemies || []).find((candidate) => candidate.index === index);
    return enemy ? monsterName(loc, enemy.name) : `目标 ${num(index)}`;
  }

  function enemyNamesByCombatId(turn) {
    const map = new Map();
    ((turn.start && turn.start.enemies) || []).forEach((enemy) => map.set(enemy.combat_id, enemy.name));
    return map;
  }

  function describeTaken(loc, step, turn) {
    const taken = step.taken || {};
    const names = enemyNamesByCombatId(turn);
    const targetText = taken.target
      ? ` → ${monsterName(loc, names.get(taken.target.combat_id) || taken.target.name)}` : '';
    if (step.kind === 'play_card') return `${KIND_LABELS.play_card} ${cardName(loc, taken.card)}${targetText}`;
    if (step.kind === 'use_potion') return `${KIND_LABELS.use_potion} ${potionName(loc, taken.potion)}${targetText}`;
    if (step.kind === 'end_turn') return KIND_LABELS.end_turn;
    if (step.kind === 'select_cards') {
      const offered = (step.state && step.state.offered) || [];
      const chosen = (taken.indices || []).map((index) => {
        const card = offered.find((candidate) => candidate.index === index);
        return card ? cardName(loc, card) : `#${index}`;
      });
      return `${KIND_LABELS.select_cards} ${chosen.join('、') || '—'}`;
    }
    return step.action || KIND_LABELS.other;
  }

  function describePlanned(loc, action, turn) {
    const names = enemyNamesByCombatId(turn);
    const target = action.target
      ? ` → ${monsterName(loc, names.get(action.target.combat_id) || action.target.name)}` : '';
    if (action.kind === 'play_card') return `${KIND_LABELS.play_card} ${plannedCardName(loc, action)}${target}`;
    if (action.kind === 'use_potion') {
      return `${KIND_LABELS.use_potion} ${potionName(loc, { id: action.potion_id, name: action.potion_name })}${target}`;
    }
    if (action.kind === 'end_turn') return KIND_LABELS.end_turn;
    return action.kind;
  }

  // The candidate actions of a step: every legal action, the taken one marked.
  function candidateChips(loc, step, turn) {
    const wrapper = element('div', { className: 'gd-chips gd-candidates' });
    const state = step.state || {};
    if (step.kind === 'select_cards') {
      (state.offered || []).forEach((card) => {
        wrapper.append(chip(cardName(loc, card), (step.taken.indices || []).includes(card.index) ? 'taken' : 'alt'));
      });
      return wrapper;
    }
    const hand = state.hand || [];
    const potions = state.potions || [];
    const legal = Array.isArray(step.legal) ? step.legal : [];
    const named = mergeEnemies(turn, state);
    legal.forEach((entry, index) => {
      const isTaken = index === step.taken_legal;
      let text;
      if (entry.kind === 'play_card') {
        const card = hand.find((candidate) => candidate.index === entry.card_index);
        text = `${cardName(loc, card)}（${card ? num(card.cost) : '？'}）`;
      } else if (entry.kind === 'use_potion') {
        const potion = potions.find((candidate) => candidate.index === entry.potion_index);
        text = `药水 ${potionName(loc, potion)}`;
      } else {
        text = KIND_LABELS.end_turn;
      }
      const node = element('span', { className: `gd-chip gd-chip-${isTaken ? 'taken' : 'alt'}` });
      node.append(element('span', { text }));
      if (Array.isArray(entry.targets) && entry.targets.length) {
        const takenTarget = isTaken && step.taken.target ? step.taken.target.index : null;
        const targets = element('span', { className: 'gd-targets' });
        entry.targets.forEach((index) => {
          targets.append(element('span', {
            className: `gd-target${takenTarget === index ? ' gd-target-taken' : ''}`,
            text: targetName(loc, named, index),
          }));
        });
        node.append(targets);
      }
      wrapper.append(node);
    });
    if (step.taken_legal === null || step.taken_legal === undefined) {
      wrapper.append(chip(`${describeTaken(loc, step, turn)}（不在候选内）`, 'taken'));
    }
    const dim = hand.filter((card) => !card.playable);
    if (dim.length) {
      const row = element('span', { className: 'gd-unplayable' });
      row.append(element('small', { text: '不可打出：' }));
      dim.forEach((card) => row.append(chip(cardName(loc, card), 'dim')));
      wrapper.append(row);
    }
    return wrapper;
  }

  // The step's enemy list has hp only; the names come from the turn's start.
  function mergeEnemies(turn, state) {
    const names = enemyNamesByCombatId(turn);
    return (state.enemies || []).map((enemy) => ({ ...enemy, name: names.get(enemy.combat_id) }));
  }

  function formatEffects(loc, effects) {
    if (!effects) return '';
    const parts = [];
    (effects.enemy_hp || []).forEach((entry) => {
      parts.push(`${monsterName(loc, entry.name)} ${signed(entry.delta)}`);
    });
    if (effects.block) parts.push(`格挡 ${signed(effects.block.delta)}`);
    if (effects.hp) parts.push(`生命 ${signed(effects.hp.delta)}`);
    if (effects.spans_enemy_turn) parts.push('（含敌方回合）');
    return parts.join(' · ');
  }

  function stateText(step) {
    const state = step.state || {};
    if (step.kind === 'select_cards' && !Number.isFinite(state.energy)) return '选牌';
    return `能量 ${num(state.energy)} · 格挡 ${num(state.block)} · 生命 ${num(state.hp)}`;
  }

  function sourceBadge(step) {
    const label = SOURCE_LABELS[step.source] || step.source || '—';
    let text = label;
    if (step.source === 'plan' && step.plan && Number.isFinite(step.plan.plan)) text = `计划 #${step.plan.plan + 1}`;
    const tone = step.source === 'off_plan' ? 'alert' : step.source === 'heuristic' ? 'kind' : 'quiet';
    return badge(text, tone);
  }

  function drawSteps(container, loc, turn) {
    const wrapper = element('div', { className: 'table-scroll' });
    const grid = element('table', { className: 'runs-table gd-steps' });
    const head = element('thead');
    const headRow = element('tr');
    ['#', '局面', '候选动作（高亮 = 实际执行）', '实际执行', '来源', '结果'].forEach((label) => {
      headRow.append(element('th', { text: label, attrs: { scope: 'col' } }));
    });
    head.append(headRow);
    grid.append(head);
    const bodyNode = element('tbody');
    (turn.steps || []).forEach((step) => {
      const row = element('tr', { className: `gd-step gd-step-${step.source || 'none'}${step.rejected ? ' gd-step-rejected' : ''}` });
      row.append(element('td', { text: String(step.n + 1) }));
      row.append(element('td', { text: stateText(step) }));
      const candidates = element('td');
      candidates.append(candidateChips(loc, step, turn));
      row.append(candidates);
      const actual = element('td', { className: 'gd-actual' });
      actual.append(element('strong', { text: describeTaken(loc, step, turn) }));
      if (step.rejected) actual.append(element('span', { className: 'gd-rejected', text: `被引擎拒绝：${step.rejected}` }));
      row.append(actual);
      const source = element('td');
      source.append(sourceBadge(step));
      row.append(source);
      row.append(element('td', { text: formatEffects(loc, step.effects) }));
      bodyNode.append(row);
    });
    grid.append(bodyNode);
    wrapper.append(grid);
    container.append(wrapper);
  }

  function planStatusBadge(planned) {
    const label = PLAN_STATUS_LABELS[planned.status] || planned.status;
    const text = planned.status === 'executed' && Number.isFinite(planned.step_n)
      ? `${label} #${planned.step_n + 1}` : label;
    const tone = planned.status === 'executed' ? 'done'
      : planned.status === 'refused' ? 'alert' : 'quiet';
    return badge(text, tone);
  }

  function drawPlan(container, loc, turn, plan) {
    const block = element('div', { className: `gd-plan${plan.ok ? '' : ' gd-plan-error'}` });
    const title = element('div', { className: 'gd-plan-head' });
    title.append(element('strong', { text: `求解器计划 #${plan.k + 1}` }));
    if (!plan.ok) {
      title.append(badge('求解器出错', 'alert'));
      block.append(title);
      block.append(element('p', { className: 'gd-rejected', text: plan.error || '未知错误' }));
      block.append(element('p', { className: 'gd-dim', text: '这一步改由启发式出牌。' }));
      container.append(block);
      return;
    }
    const reason = plan.reason ? `：${TRUNCATE_REASONS[plan.reason] || plan.reason}` : '';
    title.append(badge(`${END_REASON_LABELS[plan.end_reason] || plan.end_reason || '—'}${plan.end_reason === 'truncated' ? reason : ''}`,
      plan.end_reason === 'completed' ? 'done' : 'alert'));
    block.append(title);
    const search = plan.search || {};
    block.append(line(
      `预计结束生命 ${num(plan.projected_player_hp)}`,
      ` · ${plan.all_enemies_dead === true ? '预计全灭' : plan.all_enemies_dead === false ? '预计未全灭' : '—'}`,
      plan.only_death_routes_found ? chip('只找到死路', 'bad') : null,
      ` · 已执行 ${num(plan.executed)} / ${(plan.actions || []).length}`,
    ));
    block.append(line(
      `搜索：预算 ${formatSearchTime(search.budget_ms)} · 用时 ${formatSearchTime(search.elapsed_ms)}`,
      ` · 边界 ${cell(search.boundary)} · 本次节点 ${num(search.expanded_nodes)} · 累计节点 ${num(search.total_expanded_nodes)}`,
    ));
    const list = element('ol', { className: 'gd-plan-actions' });
    (plan.actions || []).forEach((planned) => {
      const item = element('li', { className: `gd-plan-action gd-plan-${planned.status}` });
      item.append(element('span', { text: describePlanned(loc, planned, turn) }), planStatusBadge(planned));
      list.append(item);
    });
    block.append(list);
    if (plan.later_actions > 0) {
      block.append(element('p', { className: 'gd-dim', text: `计划还包含之后回合的 ${plan.later_actions} 个动作（不会执行，下回合会重新规划）。` }));
    }
    container.append(block);
  }

  function divergenceText(divergence) {
    switch (divergence.kind) {
      case 'replan':
        return `第 ${divergence.plan + 1} 个计划：本回合重新规划（上一个计划：${END_REASON_LABELS[divergence.after] || divergence.after || '—'}）`;
      case 'truncated':
        return `计划 #${divergence.plan + 1} 中断：执行了 ${divergence.executed}/${divergence.planned}，${TRUNCATE_REASONS[divergence.reason] || divergence.reason}`;
      case 'exhausted':
        return `计划 #${divergence.plan + 1} 的 ${divergence.planned} 个动作都打完了，回合却没结束（求解器预测与实际不符）`;
      case 'mismatch': {
        const expected = divergence.expected || {};
        const actual = divergence.actual || {};
        return `步骤 ${divergence.step_n + 1} 偏离计划：计划 ${expected.card_name || expected.potion_name || expected.kind || '—'}，实际 ${actual.card_name || actual.potion_name || actual.kind || '—'}`;
      }
      case 'skipped': {
        const planned = divergence.planned || {};
        return `跳过计划动作：${planned.card_name || planned.potion_name || planned.kind || '—'}（目标已不在或牌已不可用）`;
      }
      case 'refused':
        return `步骤 ${divergence.step_n + 1} 被引擎拒绝：${divergence.message}`;
      case 'plan_error':
        return `计划 #${divergence.plan + 1} 求解器出错，改用启发式：${divergence.message}`;
      case 'heuristic':
        return `${divergence.steps} 步由启发式出牌（本回合没有计划）`;
      default:
        return divergence.kind;
    }
  }

  function divergenceBadges(turn) {
    const kinds = new Set((turn.divergence || []).map((item) => item.kind));
    const wrapper = element('span', { className: 'gd-badges' });
    DIVERGENCE_KINDS.forEach((kind) => {
      if (!kinds.has(kind)) return;
      const count = turn.divergence.filter((item) => item.kind === kind).length;
      const labels = {
        replan: '重新规划', truncated: '计划中断', exhausted: '打完未结束', mismatch: '偏离计划',
        skipped: '跳过', refused: '被拒绝', plan_error: '求解器出错', heuristic: '启发式',
      };
      wrapper.append(badge(`${labels[kind]}${count > 1 ? ` ×${count}` : ''}`, BAD_DIVERGENCE.has(kind) ? 'alert' : 'kind'));
    });
    return wrapper;
  }

  function drawTurnBody(body, loc, turn) {
    const start = turn.start || {};
    const enemies = element('div', { className: 'table-scroll' });
    const grid = element('table', { className: 'runs-table gd-enemies' });
    const headRow = element('tr');
    ['敌人', '生命', '格挡', '意图', '状态'].forEach((label) => headRow.append(element('th', { text: label, attrs: { scope: 'col' } })));
    const head = element('thead');
    head.append(headRow);
    grid.append(head);
    const tbody = element('tbody');
    (start.enemies || []).forEach((enemy) => {
      const row = element('tr');
      row.append(
        element('td', { text: monsterName(loc, enemy.name) }),
        element('td', { text: `${num(enemy.hp)} / ${num(enemy.max_hp)}` }),
        element('td', { text: num(enemy.block) }),
        element('td', { text: (enemy.intents || []).map(intentText).join(' + ') || '—' }),
        element('td', { text: (enemy.powers || []).map((power) => `${powerName(loc, power.name)}${Number.isFinite(power.amount) ? ` ${power.amount}` : ''}`).join('、') || '—' }),
      );
      tbody.append(row);
    });
    grid.append(tbody);
    enemies.append(grid);
    body.append(enemies);
    const mine = [
      `能量 ${num(start.energy)} / ${num(start.max_energy)}`,
      `格挡 ${num(start.block)}`,
      `生命 ${num(start.hp)} / ${num(start.max_hp)}`,
      `抽牌堆 ${num(start.draw_pile)} · 弃牌堆 ${num(start.discard_pile)}`,
    ];
    if (start.player_powers && start.player_powers.length) {
      mine.push(`状态：${start.player_powers.map((power) => `${powerName(loc, power.name)} ${num(power.amount)}`).join('、')}`);
    }
    body.append(line(...mine.map((text, index) => (index ? ` · ${text}` : text))));
    const hand = element('div', { className: 'gd-chips' });
    (start.hand || []).forEach((card) => hand.append(chip(`${cardName(loc, card)}（${num(card.cost)}）`, card.playable ? '' : 'dim')));
    body.append(line(element('strong', { text: '起手牌 ' })), hand);
    if ((turn.divergence || []).length) {
      body.append(subheading('偏离计划'));
      const list = element('ul', { className: 'gd-divergence' });
      turn.divergence.forEach((item) => {
        list.append(element('li', {
          className: `gd-divergence-${item.kind}`,
          text: divergenceText(item),
        }));
      });
      body.append(list);
    }
    if ((turn.solver_plans || []).length) {
      body.append(subheading('求解器计划'));
      turn.solver_plans.forEach((plan) => drawPlan(body, loc, turn, plan));
    } else {
      body.append(line('本回合没有求解器计划。'));
    }
    body.append(subheading(`逐步动作（${(turn.steps || []).length}）`));
    drawSteps(body, loc, turn);
  }

  function turnSummary(loc, turn) {
    const wrapper = element('span', { className: 'gd-summary' });
    const start = turn.start || {};
    wrapper.append(
      element('strong', { className: 'gd-summary-main', text: `第 ${turn.round} 回合` }),
      element('span', { text: `生命 ${num(start.hp)} · 能量 ${num(start.energy)}` }),
      element('span', {
        text: (start.enemies || []).map((enemy) => `${monsterName(loc, enemy.name)} ${num(enemy.hp)}`).join(' / '),
      }),
      element('span', { text: `${(turn.steps || []).length} 步` }),
    );
    const plans = turn.solver_plans || [];
    const first = plans.find((plan) => plan.ok);
    if (first) {
      wrapper.append(element('span', {
        text: `计划 ${plans.length > 1 ? `×${plans.length} ` : ''}预计生命 ${num(first.projected_player_hp)}`,
      }));
    } else if (!plans.length) {
      wrapper.append(element('span', { className: 'gd-dim', text: '无计划' }));
    }
    if (turn.hp_loss > 0) wrapper.append(badge(`失血 ${turn.hp_loss}`, 'alert'));
    wrapper.append(divergenceBadges(turn));
    return wrapper;
  }

  function drawCombat(body, loc, combat) {
    body.append(facts([
      ['敌人', (combat.enemies || []).map((name) => monsterName(loc, name)).join('、')],
      ['回合数', combat.rounds],
      ['动作数', combat.steps],
      ['求解器计划', combat.plans],
      ['重新规划', combat.replans],
      ['求解器出错', combat.plan_errors],
      ['启发式出牌', combat.heuristic_steps],
      ['搜索总耗时', formatSearchTime(combat.search_ms)],
    ]));
    (combat.turns || []).forEach((turn) => {
      body.append(lazyDetails('gd-turn', turnSummary(loc, turn), (inner) => drawTurnBody(inner, loc, turn)));
    });
  }

  // ---- rooms -----------------------------------------------------------

  function roomKind(room) {
    if (room.combat) return room.room_type === 'Monster' ? 'combat' : 'elite';
    return 'other';
  }

  function roomDiverged(room) {
    return Boolean(room.combat && room.combat.diverged_turns > 0);
  }

  function roomSummary(loc, room) {
    const wrapper = element('span', { className: 'gd-summary' });
    wrapper.append(
      element('strong', { className: 'gd-summary-main', text: room.label }),
      element('span', { text: ROOM_LABELS[room.room_type] || room.room_type }),
    );
    let what = '';
    if (room.combat) what = (room.combat.enemies || []).map((name) => monsterName(loc, name)).join('、');
    else if (room.event_name) what = table(loc, 'event')[room.event_name] || room.event_name;
    else {
      const choice = (room.choices || [])[0];
      if (choice && choice.type === 'rest') what = (choice.options || []).filter((o) => o.index === choice.chosen).map((o) => o.name_zh || o.name).join('');
      if (choice && choice.type === 'shop') what = `购买 ${(choice.purchases || []).length} 项`;
    }
    wrapper.append(element('span', { className: 'gd-summary-what', text: what }));
    const loss = Number.isFinite(room.hp_loss) ? room.hp_loss : 0;
    wrapper.append(element('span', {
      text: `生命 ${num(room.start_hp)} → ${num(room.end_hp)}${loss ? `（−${loss}）` : ''}`,
    }));
    if (Number.isFinite(room.gold_start) && Number.isFinite(room.gold_end) && room.gold_start !== room.gold_end) {
      wrapper.append(element('span', { text: `金币 ${signed(room.gold_end - room.gold_start)}` }));
    }
    const delta = room.deck_delta || {};
    const changes = ((delta.added || []).length ? `+${delta.added.length}` : '')
      + ((delta.removed || []).length ? ` −${delta.removed.length}` : '')
      + ((delta.upgraded || []).length ? ` ↑${delta.upgraded.length}` : '');
    if (changes) wrapper.append(chip(`牌组 ${changes.trim()}`, 'alt'));
    if (room.combat) {
      const combat = room.combat;
      wrapper.append(element('span', { className: 'gd-dim', text: `${combat.rounds} 回合 · 计划 ${combat.plans}` }));
      if (combat.diverged_turns > 0) wrapper.append(badge(`偏离 ${combat.diverged_turns} 回合`, 'alert'));
      if (combat.plan_errors > 0) wrapper.append(badge(`求解器出错 ${combat.plan_errors}`, 'alert'));
      if (room.status === 'dead') wrapper.append(badge('阵亡', 'alert'));
    }
    return wrapper;
  }

  function drawRoomBody(body, loc, room) {
    const delta = room.deck_delta || {};
    const text = deltaText(loc, delta);
    const extras = [];
    if (room.relics_gained && room.relics_gained.length) extras.push(`遗物 +${room.relics_gained.map((name) => relicName(loc, name)).join('、')}`);
    if (room.potions_gained && room.potions_gained.length) extras.push(`药水 +${room.potions_gained.map((name) => table(loc, 'potion')[name] || name).join('、')}`);
    if (room.potions_used && room.potions_used.length) extras.push(`用掉 ${room.potions_used.map((name) => table(loc, 'potion')[name] || name).join('、')}`);
    if (text || extras.length) body.append(line(element('strong', { text: '本房间变化：' }), [text, ...extras].filter(Boolean).join('；')));
    (room.choices || []).forEach((choice) => drawChoice(body, loc, choice));
    if (room.combat) drawCombat(body, loc, room.combat);
    if (!(room.choices || []).length && !room.combat) body.append(line('这个房间没有需要记录的选择。'));
  }

  function drawRooms(body, detail) {
    const loc = detail.loc;
    const section = element('section', { className: 'panel gd-rooms' });
    section.append(heading('房间', '逐房间记录', '点击一个房间展开：战斗按回合展开，每个回合里能看到候选动作、求解器计划和偏离标记。'));
    const filterBar = element('div', { className: 'gd-filters', attrs: { role: 'group', 'aria-label': '筛选房间' } });
    const list = element('div', { className: 'gd-room-list' });
    const allRooms = Array.isArray(detail.rooms) ? detail.rooms : [];
    rooms = {};
    const items = [];
    let currentAct = null;
    allRooms.forEach((room) => {
      if (room.act !== currentAct) {
        currentAct = room.act;
        const header = element('h3', { className: 'gd-act-header', text: `第 ${num(room.act)} 幕${room.act_name ? ` · ${room.act_name}` : ''}${room.boss ? ` · Boss：${monsterName(loc, room.boss)}` : ''}` });
        list.append(header);
        items.push({ node: header, room: null });
      }
      const details = lazyDetails('gd-room', roomSummary(loc, room), (inner) => drawRoomBody(inner, loc, room));
      details.dataset.kind = roomKind(room);
      details.dataset.roomType = room.room_type;
      details.dataset.diverged = String(roomDiverged(room));
      rooms[room.id] = details;
      list.append(details);
      items.push({ node: details, room });
    });
    const applyFilter = (name) => {
      filterBar.querySelectorAll('button').forEach((node) => {
        node.setAttribute('aria-pressed', String(node.dataset.filter === name));
      });
      let lastHeader = null;
      let headerHasRoom = false;
      const flush = () => { if (lastHeader) lastHeader.hidden = !headerHasRoom; };
      items.forEach((item) => {
        if (!item.room) {
          flush();
          lastHeader = item.node;
          headerHasRoom = false;
          return;
        }
        const kind = roomKind(item.room);
        const show = name === 'all'
          || (name === 'combat' && item.room.combat)
          || (name === 'elite' && kind === 'elite')
          || (name === 'other' && kind === 'other')
          || (name === 'diverged' && roomDiverged(item.room));
        item.node.hidden = !show;
        if (show) headerHasRoom = true;
      });
      flush();
    };
    FILTERS.forEach(([name, label]) => {
      const node = element('button', { className: 'quiet-button gd-filter', text: label, attrs: { type: 'button', 'aria-pressed': 'false' } });
      node.dataset.filter = name;
      node.addEventListener('click', () => applyFilter(name));
      filterBar.append(node);
    });
    section.append(filterBar);
    if (!allRooms.length) renderEmpty(list, '日志里没有房间记录。');
    section.append(list);
    body.append(section);
    applyFilter('all');
  }

  // ---- page ------------------------------------------------------------

  function draw(detail) {
    const body = byId('regressionBody');
    clear(body);
    drawHeader(body, detail);
    drawRoute(body, detail);
    drawDeck(body, detail);
    drawRooms(body, detail);
  }

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

  function showNotice(text) {
    const notice = byId('regressionNotice');
    if (!notice) return;
    notice.hidden = !text;
    notice.textContent = text || '';
  }

  function endpointFor(route) {
    const source = typeof route.source === 'string' ? route.source : '';
    if (source.startsWith('run:')) return `/api/game-detail?run=${encodeURIComponent(source.slice(4))}`;
    return `/api/game-detail?source=${encodeURIComponent(source)}`;
  }

  function needsRefresh(detail) {
    return Boolean(detail && detail.live === true);
  }

  async function load(route, token, refreshing) {
    timer = null;
    if (token !== generation) return;
    try {
      const detail = await getJSON(endpointFor(route));
      if (token !== generation) return;
      const signature = JSON.stringify(detail);
      // An unchanged live game is not redrawn, so the rooms and turns the
      // reader has open stay open.
      if (!refreshing || signature !== lastSignature) {
        lastSignature = signature;
        draw(detail);
      }
      showNotice('');
      if (!refreshing) setStatus('已载入对局详情');
      if (needsRefresh(detail)) timer = setTimeout(() => load(route, token, true), REFRESH_MS);
    } catch (error) {
      if (token !== generation) return;
      if (refreshing) {
        showNotice(`自动刷新失败：${error.message}`);
        timer = setTimeout(() => load(route, token, true), REFRESH_MS);
        return;
      }
      const body = byId('regressionBody');
      renderEmpty(body, `对局详情读取失败：${error.message}`, 'error-state');
      body.append(button('← 全部批次', () => navigate(regressionRoute())));
      setStatus(`对局详情读取失败：${error.message}`, 'error');
    }
  }

  async function render(route) {
    stop();
    const token = generation;
    lastSignature = '';
    rooms = {};
    renderEmpty(byId('regressionBody'), '正在读取对局详情…', 'loading-state');
    setStatus('正在读取对局详情…', 'busy');
    await load(route, token, false);
  }

  return {
    render, stop, endpointFor, cardName, divergenceText, formatEffects, describeTaken, formatSearchTime,
  };
})();
