#!/usr/bin/env python3
"""
sts2-cli interactive player — play Slay the Spire 2 in your terminal.

Usage:
    python3 play.py                    # Interactive mode (you play)
    python3 play.py --auto             # Auto-play with simple AI
    python3 play.py --seed myseed      # Fixed seed for reproducibility
    python3 play.py --character Silent  # Choose character
"""

import json
import subprocess
import sys
import os
import argparse
import random

DOTNET = os.path.expanduser("~/.dotnet-arm64/dotnet")
PROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "Sts2Headless", "Sts2Headless.csproj")

# Language setting (set by --lang flag)
LANG = "zh"  # "en", "zh", or "both"

# ─── Display helpers ───

def n(obj):
    """Extract display name based on language setting."""
    if isinstance(obj, dict):
        if "en" in obj:
            en = obj["en"]
            zh = obj.get("zh")
            if LANG == "zh" and zh:
                return zh
            elif LANG == "en":
                return en
            elif zh and zh != en:
                return f"{en}({zh})"
            return en
    return str(obj)

def short_n(obj):
    """Short English name only."""
    if isinstance(obj, dict) and "en" in obj:
        return obj["en"]
    return str(obj)

def desc(obj):
    """Extract description, strip BBCode tags, clean template vars."""
    if isinstance(obj, dict) and "en" in obj:
        import re
        if LANG == "zh" and obj.get("zh"):
            text = obj["zh"]
        elif LANG == "en":
            text = obj["en"]
        else:
            text = obj.get("zh") or obj.get("en") or ""
        text = re.sub(r'\[/?[^\]]+\]', '', text)  # strip [tags]
        # Clean template vars: {Heal} → [Heal], {Damage:diff()} → [Damage]
        def clean_var(m):
            var = m.group(1).split(':')[0]
            return f"[{var}]"
        text = re.sub(r'\{([^}]+)\}', clean_var, text)
        return text.strip()
    return ""

COLORS = {
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "magenta": "\033[95m", "cyan": "\033[96m",
    "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m",
}

def c(text, color):
    return f"{COLORS.get(color, '')}{text}{COLORS['reset']}"

def bar(current, maximum, width=20):
    filled = int(current / max(maximum, 1) * width)
    return c("█" * filled, "red") + c("░" * (width - filled), "dim")

def t(en, zh=None):
    """Translate UI string based on LANG setting."""
    if zh is None:
        return en
    if LANG == "zh":
        return zh
    elif LANG == "en":
        return en
    return en

RARITY_ZH = {"Common": "普通", "Uncommon": "罕见", "Rare": "稀有"}
CARD_TYPE_ZH = {"Attack": "攻击", "Skill": "技能", "Power": "能力", "Status": "状态", "Curse": "诅咒"}
NODE_TYPE_ZH = {"Monster": "怪物", "Elite": "精英", "Boss": "Boss", "RestSite": "休息处",
                "Shop": "商店", "Treasure": "宝箱", "Event": "事件", "Unknown": "未知", "Ancient": "远古"}

# ─── Game display ───

def resolve_template(text, vars_dict):
    """Replace [VarName] in text with actual values from vars dict.
    Matches case-insensitively against the vars dict keys."""
    if not vars_dict or not text:
        return text
    import re
    # Build case-insensitive lookup
    lower_vars = {k.lower(): v for k, v in vars_dict.items()}
    def replacer(m):
        key = m.group(1)
        val = lower_vars.get(key.lower())
        if val is not None:
            return str(val)
        return f"[{key}]"
    return re.sub(r'\[(\w+)\]', replacer, text)

def card_desc(card):
    """Get resolved card description using stats as template vars."""
    d = desc(card.get("description", {}))
    stats = card.get("stats") or {}
    return resolve_template(d, stats) if stats else d

def relic_str(r):
    """Format a relic with name and resolved description."""
    if isinstance(r, dict) and "name" in r:
        name = n(r["name"])
        d = desc(r.get("description", {}))
        # Resolve template vars with actual values
        vars_dict = r.get("vars") or {}
        d = resolve_template(d, vars_dict)
        return f"{name}" + (f": {c(d, 'dim')}" if d else "")
    return n(r)

def potion_str(p):
    """Format a potion with name and resolved description."""
    if isinstance(p, dict) and "name" in p:
        name = n(p["name"])
        d = desc(p.get("description", {}))
        vars_dict = p.get("vars") or {}
        d = resolve_template(d, vars_dict) if vars_dict else d
        idx = p.get("index", "?")
        return f"[{idx}] {name}" + (f": {c(d, 'dim')}" if d else "")
    return n(p)

def show_player(p, show_deck=False):
    hp, mhp = p.get("hp", 0), p.get("max_hp", 1)
    blk = p.get("block", 0)
    gold = p.get("gold", 0)
    deck = p.get("deck_size", 0)
    name = n(p.get("name", "?"))

    print(f"  {c(name, 'bold')}  HP {bar(hp, mhp)} {c(f'{hp}/{mhp}', 'red')}"
          + (f"  {c(str(blk), 'blue')}{t('blk','挡')}" if blk > 0 else "")
          + f"  {t('Gold','金')}{c(str(gold), 'yellow')}  {t('Deck','牌组')}{deck}")
    for r in p.get("relics", []):
        print(f"    🔶 {relic_str(r)}")
    for pot in p.get("potions", []):
        if pot:
            print(f"    🧪 {potion_str(pot)}")
    if show_deck:
        cards = p.get("deck", [])
        if cards:
            print(f"  {c('Deck:', 'bold')}")
            for cd in cards:
                up = c("⬆", "green") if cd.get("upgraded") else ""
                print(f"    {n(cd['name'])}{up} ({cd.get('cost','?')}) {c(cd.get('type',''), 'dim')}")

def show_combat(state):
    rnd = state.get("round", 0)
    energy = state.get("energy", 0)
    max_energy = state.get("max_energy", 0)
    draw = state.get("draw_pile_count", 0)
    discard = state.get("discard_pile_count", 0)

    print(f"\n{'─' * 60}")
    print(f"  {c(t(f'Round {rnd}',f'回合 {rnd}'), 'bold')}  {t('Energy','能量')}{c(f'{energy}/{max_energy}', 'cyan')}  {t('Draw','抽牌')}{draw}  {t('Discard','弃牌')}{discard}")
    show_player(state.get("player", {}))

    print()
    for e in state.get("enemies", []):
        hp, mhp = e.get("hp", 0), e.get("max_hp", 1)
        blk = e.get("block", 0)

        # Build intent string from detailed intents
        intents = e.get("intents") or []
        intent_parts = []
        for it in intents:
            itype = it.get("type", "")
            dmg = it.get("damage")
            hits = it.get("hits")
            if itype == "Attack":
                if dmg is not None:
                    if hits and hits > 1:
                        intent_parts.append(c(f"⚔{dmg}x{hits}", "red"))
                    else:
                        intent_parts.append(c(f"⚔{dmg}", "red"))
                else:
                    intent_parts.append(c(t("⚔ATK","⚔攻击"), "red"))
            elif itype == "Defend":
                intent_parts.append(c(t("🛡DEF","🛡防御"), "blue"))
            elif itype in ("Buff", "Heal"):
                intent_parts.append(c(t(f"⬆{itype}",f"⬆{'增益' if itype=='Buff' else '回复'}"), "magenta"))
            elif itype == "Debuff":
                intent_parts.append(c(t("⬇Debuff","⬇减益"), "yellow"))
            elif itype == "DebuffStrong":
                intent_parts.append(c(t("⬇Strong","⬇强减益"), "yellow"))
            elif itype in ("CardDebuff", "StatusCard"):
                intent_parts.append(c(t("⬇Cards","⬇塞牌"), "yellow"))
            elif itype == "DeathBlow":
                if dmg is not None:
                    intent_parts.append(c(f"💀{dmg}", "red"))
                else:
                    intent_parts.append(c(t("💀KILL","💀致命一击"), "red"))
            elif itype == "Escape":
                intent_parts.append(c(t("🏃Escape","🏃逃跑"), "dim"))
            elif itype == "Summon":
                intent_parts.append(c(t("📢Summon","📢召唤"), "magenta"))
            elif itype == "Sleep":
                intent_parts.append(c(t("💤Sleep","💤休眠"), "dim"))
            elif itype == "Stun":
                intent_parts.append(c(t("⚡Stun","⚡眩晕"), "yellow"))
            elif itype == "Hidden":
                intent_parts.append(c("? ???", "dim"))
            elif itype:
                intent_parts.append(c(itype, "dim"))
        intent_str = " ".join(intent_parts) if intent_parts else c("? ???", "dim")

        # Enemy powers
        powers = e.get("powers") or []
        power_str = ""
        if powers:
            pw_parts = [f"{n(pw['name'])}{pw.get('amount','')}" for pw in powers]
            power_str = "  " + c(" ".join(pw_parts), "dim")

        print(f"  [{e['index']}] {n(e['name'])}  {bar(hp, mhp)} {hp}/{mhp}"
              + (f"  {c(str(blk), 'blue')}{t('blk','挡')}" if blk else "")
              + f"  {intent_str}{power_str}")

    print()
    hand = state.get("hand", [])
    for card in hand:
        cost = card.get("cost", 0)
        playable = card.get("can_play", False)
        ctype = card.get("type", "?")
        target = card.get("target_type", "")

        type_color = {"Attack": "red", "Skill": "blue", "Power": "magenta", "Status": "dim", "Curse": "dim"}.get(ctype, "reset")
        mark = c("●", "green") if playable else c("○", "dim")
        cost_str = c(str(cost), "cyan")

        # Show damage/block inline only
        stats = card.get("stats") or {}
        stat_parts = []
        if "damage" in stats: stat_parts.append(c(f"{stats['damage']}{t('dmg','伤')}", "red"))
        if "block" in stats: stat_parts.append(c(f"{stats['block']}{t('blk','挡')}", "blue"))
        stat_str = " ".join(stat_parts)

        cd_d = card_desc(card)
        # Show resolved description for non-basic cards (cards with effects beyond just damage/block)
        has_extra = any(k not in ("damage", "block") for k in stats)
        extra_desc = ""
        if has_extra and cd_d:
            # Compact: show first line of description
            first_line = cd_d.split("\n")[-1] if "\n" in cd_d else ""
            if first_line:
                extra_desc = f"  {c(first_line, 'dim')}"

        print(f"  {mark} [{card['index']}] {c(n(card['name']), type_color)} ({cost_str}) {stat_str}{extra_desc}"
              + (f"  {c('→','yellow')}" if target == "AnyEnemy" else ""))

def show_map(state, send_fn=None):
    """Show map at map_select. Fetches full map if send_fn available."""
    choices = state.get("choices", [])
    choice_set = {(ch["col"], ch["row"]) for ch in choices}

    # Try to fetch full map for richer display
    if send_fn:
        map_data = send_fn({"cmd": "get_map"})
        if map_data and map_data.get("type") == "map":
            _render_map(map_data, choice_set)
            # Print available choices below the map
            type_icons = {
                "Monster": "⚔", "Elite": "💀", "Boss": "👹",
                "RestSite": "🏕", "Shop": "🏪", "Treasure": "💎",
                "Event": "❓", "Unknown": "❓", "Ancient": "🏛",
            }
            print(f"  {c(t('Available paths:','可选路径:'), 'bold')}")
            for i, ch in enumerate(choices):
                icon = type_icons.get(ch["type"], "?")
                ntype = t(ch["type"], NODE_TYPE_ZH.get(ch["type"], ch["type"]))
                print(f"    [{i}] {c(icon, 'yellow')} {ntype}")
            return

    # Fallback: simple list
    ctx = state.get("context", {})
    act_name = n(ctx.get("act_name", "?"))
    floor = ctx.get("floor", "?")
    print(f"\n{'═' * 60}")
    print(f"  {c(f'{act_name}', 'bold')} Floor {floor}")
    show_player(state.get("player", {}))
    print()
    type_icons = {
        "Monster": "⚔", "Elite": "💀", "Boss": "👹",
        "RestSite": "🏕", "Shop": "🏪", "Treasure": "💎",
        "Event": "❓", "Unknown": "❓", "Ancient": "🏛",
    }
    for i, ch in enumerate(choices):
        icon = type_icons.get(ch["type"], "?")
        ntype = t(ch["type"], NODE_TYPE_ZH.get(ch["type"], ch["type"]))
        print(f"  [{i}] {icon} {ntype}")

def _format_upgrade_preview(stats, aug):
    """Format upgrade preview string."""
    if not aug:
        return None
    aug_stats = aug.get("stats") or {}
    parts = []
    aug_cost = aug.get("cost")
    # Compare all stats
    all_keys = set(list(stats.keys()) + list(aug_stats.keys()))
    for k in all_keys:
        old = stats.get(k, 0)
        new_val = aug_stats.get(k, old)
        if new_val != old:
            if k == "damage":
                parts.append(c(f"{t('dmg','伤害')} {old}→{new_val}", "red"))
            elif k == "block":
                parts.append(c(f"{t('blk','格挡')} {old}→{new_val}", "blue"))
            else:
                parts.append(f"{k} {old}→{new_val}")
    return parts

def show_card_reward(state):
    print(f"\n{'─' * 60}")
    gold_earned = state.get("gold_earned", 0)
    if gold_earned > 0:
        print(f"  {c(t('Combat won!','战斗胜利!'), 'green')} +{c(str(gold_earned), 'yellow')}{t('g','金')}")
    print(f"  {c(t('Card Reward','卡牌奖励'), 'bold')} — {t('choose one (or skip)','选一张（或跳过）')}")
    show_player(state.get("player", {}))
    print()
    for card in state.get("cards", []):
        ctype = card.get("type", "?")
        rarity = card.get("rarity", "Common")
        cost = card.get("cost", "?")
        type_color = {"Attack": "red", "Skill": "blue", "Power": "magenta"}.get(ctype, "reset")
        rarity_zh = RARITY_ZH.get(rarity, rarity)
        rarity_label = t(rarity, rarity_zh)
        rarity_color = {"Rare": "yellow", "Uncommon": "cyan"}.get(rarity, "dim")
        stats = card.get("stats") or {}
        cd_desc = card_desc(card)

        print(f"  [{card['index']}] {c(n(card['name']), type_color)} ({cost}) {c(rarity_label, rarity_color)}")
        if cd_desc:
            print(f"      {c(cd_desc, 'dim')}")
        # Show upgrade preview
        aug_parts = _format_upgrade_preview(stats, card.get("after_upgrade"))
        if aug_parts:
            print(f"      {c(t('upgrade:','升级:'), 'green')} {', '.join(aug_parts)}")

def show_shop(state):
    print(f"\n{'─' * 60}")
    print(f"  {c(t('Shop','商店'), 'bold')}")
    show_player(state.get("player", {}))
    gold = state.get("player", {}).get("gold", 0)

    print(f"\n  {c(t('Cards:','卡牌:'), 'bold')}")
    for card in state.get("cards", []):
        if not card.get("is_stocked"): continue
        cost = card.get("cost", 0)
        affordable = c(str(cost), "green") if cost <= gold else c(str(cost), "red")
        sale = c(t(" SALE"," 打折"), "yellow") if card.get("on_sale") else ""
        ctype_zh = CARD_TYPE_ZH.get(card.get("type",""), card.get("type",""))
        print(f"  [{card['index']}] {n(card['name'])} ({t(card.get('type','?'), ctype_zh)}) — {affordable}{t('g','金')}{sale}")

    print(f"\n  {c(t('Relics:','遗物:'), 'bold')}")
    for r in state.get("relics", []):
        if not r.get("is_stocked"): continue
        cost = r.get("cost", 0)
        affordable = c(str(cost), "green") if cost <= gold else c(str(cost), "red")
        print(f"  [r{r['index']}] {n(r['name'])} — {affordable}{t('g','金')}")

    print(f"\n  {c(t('Potions:','药水:'), 'bold')}")
    for p in state.get("potions", []):
        if not p.get("is_stocked"): continue
        cost = p.get("cost", 0)
        affordable = c(str(cost), "green") if cost <= gold else c(str(cost), "red")
        print(f"  [p{p['index']}] {n(p['name'])} — {affordable}{t('g','金')}")

    removal_cost = state.get("card_removal_cost")
    if removal_cost:
        affordable = c(str(removal_cost), "green") if removal_cost <= gold else c(str(removal_cost), "red")
        print(f"\n  [rm] {t('Remove a card','移除一张牌')} — {affordable}{t('g','金')}")

    print(f"\n  [leave] {t('Leave shop','离开商店')}")

REST_OPTIONS_ZH = {"HEAL": "休息", "SMITH": "升级", "LIFT": "锻炼", "DIG": "挖掘", "RECALL": "回忆", "TOKE": "吸食"}

def show_rest_site(state):
    print(f"\n{'─' * 60}")
    ctx = state.get("context", {})
    if ctx:
        print(f"  {c(n(ctx.get('act_name','?')), 'dim')} Floor {ctx.get('floor','?')}")
    print(f"  {c(t('Rest Site','休息处'), 'bold')}")
    show_player(state.get("player", {}))
    print()
    for opt in state.get("options", []):
        enabled = opt.get("is_enabled", True)
        mark = c("●", "green") if enabled else c("○", "dim")
        opt_id = opt.get("option_id", "?")
        opt_name = t(opt_id, REST_OPTIONS_ZH.get(opt_id, opt_id))
        opt_desc = opt.get("name", "")
        print(f"  {mark} [{opt['index']}] {opt_name}" + (f" — {opt_desc}" if opt_desc and opt_desc != opt_id else ""))

def _load_loc():
    """Load localization data for resolving event option names."""
    if not hasattr(_load_loc, '_cache'):
        _load_loc._cache = {}
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for lang in ['localization_eng', 'localization_zhs']:
            d = os.path.join(base, lang)
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f.endswith('.json'):
                        try:
                            data = json.load(open(os.path.join(d, f)))
                            table = f[:-5]
                            if table not in _load_loc._cache:
                                _load_loc._cache[table] = {}
                            for k, v in data.items():
                                key = f"{table}:{k}"
                                if key not in _load_loc._cache:
                                    _load_loc._cache[key] = v
                                elif lang == 'localization_zhs':
                                    _load_loc._cache[key + ':zh'] = v
                        except: pass
    return _load_loc._cache

def loc_resolve(key):
    """Resolve a loc key like 'NEOW.pages.INITIAL.options.PRECISE_SCISSORS.title' to readable text."""
    cache = _load_loc()
    # Try direct lookup in relevant tables
    for table in ['events', 'relics', 'ancients', 'cards', 'potions', 'monsters']:
        val_en = cache.get(f"{table}:{key}")
        val_zh = cache.get(f"{table}:{key}:zh")
        if val_en:
            return n({"en": val_en, "zh": val_zh}) if val_zh else val_en
    # Extract meaningful part from key
    parts = key.split('.')
    for p in reversed(parts):
        if p not in ('title', 'description', 'options', 'pages', 'INITIAL'):
            relic_en = cache.get(f"relics:{p}.title")
            relic_zh = cache.get(f"relics:{p}.title:zh")
            desc_en = cache.get(f"relics:{p}.description", "")
            desc_zh = cache.get(f"relics:{p}.description:zh", "")
            if relic_en:
                name = n({"en": relic_en, "zh": relic_zh})
                d = desc({"en": desc_en, "zh": desc_zh})
                return f"{name}" + (f" — {c(d, 'dim')}" if d else "")
            return p.replace('_', ' ').title()
    return key

def show_event(state):
    print(f"\n{'─' * 60}")
    event_name = state.get("event_name", "?")
    event_desc = state.get("description", "")
    # Show context
    ctx = state.get("context", {})
    if ctx:
        act = n(ctx.get("act_name", "?"))
        floor = ctx.get("floor", "?")
        print(f"  {c(act, 'dim')} Floor {floor}")
    print(f"  {c(f'Event: {event_name}', 'bold')}")
    if event_desc:
        resolved = loc_resolve(event_desc) if event_desc.isupper() or '.' in event_desc else event_desc
        print(f"  {c(resolved, 'dim')}")
    show_player(state.get("player", {}))
    print()
    for opt in state.get("options", []):
        locked = opt.get("is_locked", False)
        mark = c("○", "dim") if locked else c("●", "green")
        raw_title = opt.get("title", opt.get("text_key", f"Option {opt['index']}"))
        # Resolve the title from loc data
        title = loc_resolve(raw_title) if '.' in str(raw_title) or str(raw_title).isupper() else raw_title
        print(f"  {mark} [{opt['index']}] {title}")

# ─── Input handling ───

def _render_map(map_data, choice_set=None):
    """Render map as a grid with connection lines between rows."""
    if choice_set is None:
        choice_set = set()

    ctx = map_data.get("context", {})
    act = n(ctx.get("act_name", "?"))
    floor_n = ctx.get("floor", "?")
    cur = map_data.get("current_coord")

    ICONS = {
        "Monster": "M", "Elite": "E", "Boss": "B",
        "RestSite": "R", "Shop": "$", "Treasure": "T",
        "Event": "?", "Unknown": "?", "Ancient": "A",
    }

    rows = map_data.get("rows", [])
    if not rows:
        return

    # Collect nodes and edges
    node_map = {}
    max_col = 0
    row_numbers = set()
    # edges_up[lower_row] = [(from_col, to_col), ...] where to is in the row above
    edges_up = {}
    for row in rows:
        for nd in row:
            col, rn = nd.get("col", 0), nd.get("row", 0)
            node_map[(col, rn)] = nd
            max_col = max(max_col, col)
            row_numbers.add(rn)
            for ch in (nd.get("children") or []):
                edges_up.setdefault(rn, []).append((col, ch["col"]))

    row_numbers = sorted(row_numbers)
    total_cols = max_col + 1
    W = 4  # chars per column cell
    # Center of column c = c*W + W//2 = c*4 + 2

    width = W * total_cols + 6
    print(f"\n{'═' * width}")
    print(f"  {c(act, 'bold')} — Floor {floor_n}")
    print()

    # Boss row
    boss = map_data.get("boss", {})
    boss_col = boss.get("col", 0)
    boss_row = boss.get("row", -1)
    buf = list(" " * (W * total_cols))
    buf[boss_col * W + W // 2] = "B"
    line = "".join(buf)
    line = line[:boss_col * W + W // 2] + c("B", "red") + line[boss_col * W + W // 2 + 1:]
    print(f"  {c('B','dim')} | {line}")

    # Connection from top row to boss
    top_rn = row_numbers[-1] if row_numbers else -1
    conn = list(" " * (W * total_cols))
    for fc, tc in edges_up.get(top_rn, []):
        if tc == boss_col:  # this edge's target row should be boss
            pass
    # Actually, edges_up[top_rn] has edges from top_rn to its children.
    # Children of top row nodes go to boss.
    for nd_row in rows:
        for nd in nd_row:
            if nd.get("row") == top_rn:
                for ch in (nd.get("children") or []):
                    if ch.get("row") == boss_row:
                        fc, tc = nd["col"], ch["col"]
                        _draw_conn(conn, fc, tc, W)
    print(f"    | {c(''.join(conn), 'dim')}")

    # Map rows (top to bottom)
    for idx in range(len(row_numbers) - 1, -1, -1):
        rn = row_numbers[idx]

        # --- Node line ---
        buf = list(" " * (W * total_cols))
        color_subs = []  # (start_pos, end_pos, colored_str)
        for col in range(total_cols):
            nd = node_map.get((col, rn))
            if not nd:
                continue
            icon = ICONS.get(nd.get("type", "?"), "·")
            is_cur = (cur and cur["col"] == col and cur["row"] == rn)
            is_choice = (col, rn) in choice_set
            visited = nd.get("visited", False)

            center = col * W + W // 2
            if is_cur:
                buf[center - 1] = "["
                buf[center] = icon
                buf[center + 1] = "]"
                color_subs.append((center - 1, center + 2, c(f"[{icon}]", "green")))
            elif is_choice:
                buf[center] = icon
                color_subs.append((center, center + 1, c(icon, "yellow")))
            elif visited:
                buf[center] = icon
                color_subs.append((center, center + 1, c(icon, "dim")))
            else:
                buf[center] = icon

        line = "".join(buf)
        # Apply colors right-to-left
        for start, end, colored in sorted(color_subs, key=lambda x: -x[0]):
            line = line[:start] + colored + line[end:]
        print(f"  {rn:>2}| {line}")

        # --- Connection line below this row (edges from row below going up to this row) ---
        if idx > 0:
            below_rn = row_numbers[idx - 1]
            conn = list(" " * (W * total_cols))
            for fc, tc in edges_up.get(below_rn, []):
                # fc is in below_rn, tc is the child row
                # We need edges where child row == rn
                pass
            # Rebuild: iterate edges from below_rn whose children are in rn
            for nd_row in rows:
                for nd in nd_row:
                    if nd.get("row") != below_rn:
                        continue
                    for ch in (nd.get("children") or []):
                        if ch.get("row") == rn:
                            _draw_conn(conn, nd["col"], ch["col"], W)
            print(f"    | {c(''.join(conn), 'dim')}")

    # Legend
    print(f"  {'─' * width}")
    legend = f"  {c('M','red')}={t('Monster','怪')} {c('E','red')}={t('Elite','英')} R={t('Rest','休')} $={t('Shop','店')} T={t('Treasure','宝')} ?={t('Event','事')} {c('[x]','green')}={t('You','你')} {c('x','yellow')}={t('Next','可选')}"
    print(legend)
    print()


def _draw_conn(buf, from_col, to_col, W):
    """Draw a connection character in buf from from_col to to_col."""
    fc = from_col * W + W // 2
    tc = to_col * W + W // 2
    if from_col == to_col:
        if 0 <= fc < len(buf):
            buf[fc] = "|"
    elif from_col < to_col:
        # Going up-right: /
        # Place characters along the path
        mid = (fc + tc) // 2
        if 0 <= fc < len(buf):
            buf[fc] = "/"
        if tc - fc > W and 0 <= mid < len(buf):
            buf[mid] = "/"
        if 0 <= tc < len(buf):
            buf[tc] = "/"
    else:
        # Going up-left: \
        mid = (tc + fc) // 2
        if 0 <= fc < len(buf):
            buf[fc] = "\\"
        if fc - tc > W and 0 <= mid < len(buf):
            buf[mid] = "\\"
        if 0 <= tc < len(buf):
            buf[tc] = "\\"

def get_input(prompt, valid_options=None, state=None):
    """Get user input with validation. Supports meta-commands: help, map, deck, potions."""
    while True:
        try:
            raw = input(f"\n{c('>', 'green')} {prompt}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nQuitting...")
            sys.exit(0)

        if not raw:
            continue

        # Meta-commands available at any prompt
        if raw == "help":
            print(f"""
  {c('Commands:', 'bold')}
    {c('help', 'cyan')}     — show this help
    {c('map', 'cyan')}      — show current map choices
    {c('deck', 'cyan')}     — show full deck
    {c('potions', 'cyan')}  — show potions
    {c('relics', 'cyan')}   — show relics with descriptions
    {c('quit', 'cyan')}     — quit the game

  {c('Decision-specific:', 'bold')}
    Map:     enter {c('path number', 'yellow')} (e.g. 0, 1, 2)
    Combat:  enter {c('card index', 'yellow')} or {c('e', 'yellow')} (end turn) or {c('p0', 'yellow')} (use potion 0)
    Reward:  enter {c('card index', 'yellow')} or {c('s', 'yellow')} (skip)
    Rest:    enter {c('option index', 'yellow')}
    Event:   enter {c('option index', 'yellow')} or {c('leave', 'yellow')}
    Shop:    enter {c('c0', 'yellow')} (buy card) / {c('r0', 'yellow')} (relic) / {c('p0', 'yellow')} (potion) / {c('rm', 'yellow')} (remove) / {c('leave', 'yellow')}
""")
            continue
        if raw == "deck" and state:
            p = state.get("player", {})
            show_player(p, show_deck=True)
            continue
        if raw == "potions" and state:
            p = state.get("player", {})
            pots = p.get("potions", [])
            if pots:
                for pot in pots:
                    if pot: print(f"  🧪 {potion_str(pot)}")
            else:
                print("  No potions.")
            continue
        if raw == "relics" and state:
            p = state.get("player", {})
            for r in p.get("relics", []):
                print(f"  🔶 {relic_str(r)}")
            continue
        if raw == "map":
            # Fetch full map from CLI
            if hasattr(get_input, '_send'):
                map_data = get_input._send({"cmd": "get_map"})
                if map_data and map_data.get("type") == "map":
                    _render_map(map_data)
                else:
                    print("  Map not available.")
            elif state:
                ctx = state.get("context", {})
                print(f"  {c(n(ctx.get('act_name','?')), 'bold')} Floor {ctx.get('floor','?')}")
            continue
        if raw == "quit":
            print("Quitting...")
            sys.exit(0)

        if valid_options and raw not in valid_options:
            print(f"  Invalid. Options: {', '.join(sorted(valid_options))}")
            continue
        return raw

# ─── Main game loop ───

def play(character="Ironclad", seed=None, auto=False):
    proc = subprocess.Popen(
        [DOTNET, "run", "--no-build", "--project", PROJECT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )

    def read():
        while True:
            l = proc.stdout.readline().strip()
            if not l:
                return None
            if l.startswith("{"):
                return json.loads(l)

    def send(cmd):
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()
        return read()

    # Wire send into get_input for map command
    get_input._send = send

    try:
        ready = read()
        if not ready:
            print("Failed to start simulator")
            return

        print(f"\n{c('Slay the Spire 2 — Headless CLI', 'bold')}")
        print(f"Character: {character}  Seed: {seed or 'random'}")
        print(f"Type {c('help', 'cyan')} at any prompt for available commands.\n")

        state = send({"cmd": "start_run", "character": character, "seed": seed or f"cli_{random.randint(1000,9999)}"})

        while True:
            if not state:
                print("Connection lost.")
                break

            if state.get("type") == "error":
                print(f"  {c('Error:', 'red')} {state.get('message', '?')}")
                state = send({"cmd": "action", "action": "proceed"})
                continue

            dec = state.get("decision", "")

            if dec == "game_over":
                victory = state.get("victory", False)
                p = state.get("player", {})
                print(f"\n{'═' * 60}")
                if victory:
                    print(f"  {c(t('VICTORY!','胜利!'), 'green')}")
                else:
                    print(f"  {c(t('DEFEAT','战败'), 'red')} Act {state.get('act')}, Floor {state.get('floor')}")
                show_player(p)
                print(f"{'═' * 60}")
                break

            elif dec == "map_select":
                show_map(state, send_fn=send)
                choices = state.get("choices", [])

                if auto:
                    if len(choices) == 1:
                        pick = choices[0]
                    else:
                        p = state.get("player", {})
                        hp_ratio = p.get("hp", 1) / max(p.get("max_hp", 1), 1)
                        if hp_ratio < 0.4:
                            pick = next((ch for ch in choices if ch["type"] == "RestSite"), choices[0])
                        else:
                            pick = choices[0]
                else:
                    valid = {str(i): ch for i, ch in enumerate(choices)}
                    key = get_input(t("Choose path [number]", "选择路径 [编号]"), set(valid.keys()), state=state)
                    pick = valid[key]

                state = send({"cmd": "action", "action": "select_map_node",
                             "args": {"col": pick["col"], "row": pick["row"]}})

            elif dec == "combat_play":
                show_combat(state)
                hand = state.get("hand", [])
                enemies = state.get("enemies", [])
                energy = state.get("energy", 0)

                valid = {"e": "end_turn"}
                for card in hand:
                    if card.get("can_play") and card.get("cost", 99) <= energy:
                        valid[str(card["index"])] = card
                # Add potion shortcuts
                for pot in state.get("player", {}).get("potions", []):
                    if pot:
                        valid[f"p{pot['index']}"] = f"potion_{pot['index']}"

                if auto:
                    # Auto: play first playable card, or end turn
                    playable = [c for c in hand if c.get("can_play") and c.get("cost", 99) <= energy]
                    if playable:
                        card = playable[0]
                        choice = str(card["index"])
                    else:
                        choice = "e"
                else:
                    choice = get_input(t("Play card [index], (e)nd turn, (p0) potion", "出牌 [编号], (e)结束回合, (p0)药水"), set(valid.keys()) | {"help"}, state=state)
                    if choice == "help":
                        print(f"  {t('Enter card index, e=end turn, p0=use potion 0', '输入卡牌编号，e=结束回合，p0=使用药水0')}")
                        continue

                if choice == "e":
                    state = send({"cmd": "action", "action": "end_turn"})
                elif choice.startswith("p") and choice[1:].isdigit():
                    # Use potion
                    pidx = int(choice[1:])
                    args = {"potion_index": pidx}
                    # Ask for target if needed
                    if enemies:
                        tgt = get_input("Target enemy [index] or self (s)", state=state)
                        if tgt != "s" and tgt.isdigit():
                            args["target_index"] = int(tgt)
                    state = send({"cmd": "action", "action": "use_potion", "args": args})
                else:
                    card = valid[choice]
                    args = {"card_index": card["index"]}
                    if card.get("target_type") == "AnyEnemy":
                        if len(enemies) == 1:
                            args["target_index"] = enemies[0]["index"]
                        elif auto:
                            args["target_index"] = min(enemies, key=lambda e: e.get("hp", 999))["index"]
                        else:
                            tgt = get_input("Target enemy [index]",
                                           {str(e["index"]) for e in enemies})
                            args["target_index"] = int(tgt)
                    state = send({"cmd": "action", "action": "play_card", "args": args})

            elif dec == "card_reward":
                show_card_reward(state)
                cards = state.get("cards", [])
                valid = {str(c["index"]): c for c in cards}
                valid["s"] = None  # skip

                if auto:
                    choice = "0" if cards else "s"
                else:
                    choice = get_input(t("Pick card [index] or (s)kip", "选择卡牌 [编号] 或 (s)跳过"), set(valid.keys()), state=state)

                if choice == "s":
                    state = send({"cmd": "action", "action": "skip_card_reward"})
                else:
                    state = send({"cmd": "action", "action": "select_card_reward",
                                 "args": {"card_index": int(choice)}})

            elif dec == "bundle_select":
                print(f"\n{'─' * 60}")
                ctx = state.get("context", {})
                if ctx:
                    print(f"  {c(n(ctx.get('act_name','?')), 'dim')} Floor {ctx.get('floor','?')}")
                print(f"  {c('Choose a card pack', 'bold')}")
                show_player(state.get("player", {}))
                print()
                bundles = state.get("bundles", [])
                for b in bundles:
                    bidx = b["index"]
                    print(f"  {c(f'Pack [{bidx}]:', 'yellow')}")
                    for cd in b.get("cards", []):
                        cd_desc = card_desc(cd)
                        print(f"    {n(cd['name'])} ({cd.get('cost','?')}) {c(cd.get('type',''), 'dim')}")
                        if cd_desc:
                            print(f"      {c(cd_desc, 'dim')}")
                valid = {str(b["index"]): b for b in bundles}
                if auto:
                    choice = "0"
                else:
                    choice = get_input(t("Choose pack [index]", "选择卡牌包 [编号]"), set(valid.keys()), state=state)
                state = send({"cmd": "action", "action": "select_bundle",
                             "args": {"bundle_index": int(choice)}})

            elif dec == "card_select":
                print(f"\n{'─' * 60}")
                ctx = state.get("context", {})
                if ctx:
                    print(f"  {c(n(ctx.get('act_name','?')), 'dim')} Floor {ctx.get('floor','?')}")
                min_sel = state.get("min_select", 1)
                max_sel = state.get("max_select", 1)
                print(f"  {c(t('Choose cards','选择卡牌'), 'bold')} ({t('select','选择')} {min_sel}-{max_sel})")
                show_player(state.get("player", {}))
                print()
                cards = state.get("cards", [])
                for cd in cards:
                    up = c("+", "green") if cd.get("upgraded") else ""
                    stats = cd.get("stats") or {}
                    ctype_zh = CARD_TYPE_ZH.get(cd.get("type", ""), cd.get("type", ""))
                    ctype_label = t(cd.get("type", ""), ctype_zh)
                    cd_desc_text = card_desc(cd)
                    print(f"  [{cd['index']}] {n(cd['name'])}{up} ({cd.get('cost','?')}) {c(ctype_label, 'dim')}")
                    if cd_desc_text:
                        print(f"      {c(cd_desc_text, 'dim')}")
                    aug_parts = _format_upgrade_preview(stats, cd.get("after_upgrade"))
                    if aug_parts:
                        print(f"      {c(t('upgrade:','升级:'), 'green')} {', '.join(aug_parts)}")

                valid = {str(cd["index"]): cd for cd in cards}
                if min_sel == 0:
                    valid["s"] = None

                if auto:
                    choice = "0"
                else:
                    choice = get_input(t("Choose card(s) [index] or (s)kip", "选择卡牌 [编号] 或 (s)跳过"), set(valid.keys()), state=state)

                if choice == "s":
                    state = send({"cmd": "action", "action": "skip_select"})
                else:
                    # Support comma-separated indices
                    state = send({"cmd": "action", "action": "select_cards",
                                 "args": {"indices": choice}})

            elif dec == "shop":
                show_shop(state)

                if auto:
                    choice = "leave"
                else:
                    choice = get_input(t("Buy [index/r0/p0/rm] or (leave)", "购买 [编号/r0/p0/rm] 或 (leave)离开"), state=state)

                if choice == "leave":
                    state = send({"cmd": "action", "action": "leave_room"})
                elif choice == "rm":
                    state = send({"cmd": "action", "action": "remove_card"})
                elif choice.startswith("r"):
                    state = send({"cmd": "action", "action": "buy_relic",
                                 "args": {"relic_index": int(choice[1:])}})
                elif choice.startswith("p"):
                    state = send({"cmd": "action", "action": "buy_potion",
                                 "args": {"potion_index": int(choice[1:])}})
                else:
                    state = send({"cmd": "action", "action": "buy_card",
                                 "args": {"card_index": int(choice)}})

            elif dec == "rest_site":
                show_rest_site(state)
                options = state.get("options", [])
                enabled = [o for o in options if o.get("is_enabled")]
                valid = {str(o["index"]): o for o in enabled}

                if auto:
                    hp = state.get("player", {}).get("hp", 1)
                    mhp = state.get("player", {}).get("max_hp", 1)
                    heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
                    smith = next((o for o in enabled if o.get("option_id") == "SMITH"), None)
                    pick = (heal if hp < mhp * 0.7 else smith) or (heal or (enabled[0] if enabled else None))
                    choice = str(pick["index"]) if pick else "0"
                else:
                    choice = get_input(t("Choose option [index]", "选择 [编号]"), set(valid.keys()), state=state)

                state = send({"cmd": "action", "action": "choose_option",
                             "args": {"option_index": int(choice)}})
                if state and state.get("type") == "error":
                    state = send({"cmd": "action", "action": "leave_room"})

            elif dec == "event_choice":
                show_event(state)
                options = state.get("options", [])
                unlocked = [o for o in options if not o.get("is_locked")]
                valid = {str(o["index"]): o for o in unlocked}
                valid["leave"] = None

                # Save state before choice to show diff
                old_relics = set(n(r.get("name","?")) for r in state.get("player",{}).get("relics",[]))
                old_deck = state.get("player",{}).get("deck_size", 0)
                old_hp = state.get("player",{}).get("hp", 0)
                old_gold = state.get("player",{}).get("gold", 0)

                if auto:
                    choice = str(unlocked[0]["index"]) if unlocked else "leave"
                else:
                    choice = get_input(t("Choose option [index] or (leave)", "选择 [编号] 或 (leave)离开"), set(valid.keys()), state=state)

                if choice == "leave":
                    state = send({"cmd": "action", "action": "leave_room"})
                else:
                    state = send({"cmd": "action", "action": "choose_option",
                                 "args": {"option_index": int(choice)}})
                    if state and state.get("type") == "error":
                        state = send({"cmd": "action", "action": "leave_room"})

                # Show what changed
                if state and state.get("player"):
                    new_p = state["player"]
                    new_relics = set(n(r.get("name","?")) for r in new_p.get("relics",[]))
                    gained_relics = new_relics - old_relics
                    new_deck = new_p.get("deck_size", 0)
                    new_hp = new_p.get("hp", 0)
                    new_gold = new_p.get("gold", 0)
                    changes = []
                    if gained_relics:
                        changes.append(f"Gained relic: {', '.join(gained_relics)}")
                    if new_deck != old_deck:
                        changes.append(f"Deck: {old_deck} → {new_deck}")
                    if new_hp != old_hp:
                        changes.append(f"HP: {old_hp} → {new_hp}")
                    if new_gold != old_gold:
                        diff = new_gold - old_gold
                        changes.append(f"Gold: {old_gold} → {new_gold} ({'+' if diff > 0 else ''}{diff})")
                    if changes:
                        print(f"\n  {c('Changes:', 'yellow')} {'; '.join(changes)}")

            else:
                print(f"  Unknown state: {dec}")
                state = send({"cmd": "action", "action": "proceed"})

    finally:
        try:
            proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            proc.stdin.flush()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except:
            proc.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Play Slay the Spire 2 in your terminal")
    parser.add_argument("--auto", action="store_true", help="Auto-play with simple AI")
    parser.add_argument("--seed", type=str, default=None, help="Random seed")
    parser.add_argument("--character", type=str, default="Ironclad",
                       choices=["Ironclad", "Silent", "Defect", "Regent"],
                       help="Character to play")
    parser.add_argument("--lang", type=str, default="both",
                       choices=["en", "zh", "both"],
                       help="Display language: en, zh, or both")
    args = parser.parse_args()

    import play as _self
    _self.LANG = args.lang

    play(character=args.character, seed=args.seed, auto=args.auto)
