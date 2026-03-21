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

# ─── Game display ───

def resolve_template(text, vars_dict):
    """Replace [VarName] in text with actual values from vars dict."""
    if not vars_dict or not text:
        return text
    import re
    def replacer(m):
        key = m.group(1)
        val = vars_dict.get(key)
        if val is not None:
            return str(val)
        return f"[{key}]"
    return re.sub(r'\[(\w+)\]', replacer, text)

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
    """Format a potion with name and cleaned description."""
    if isinstance(p, dict) and "name" in p:
        name = n(p["name"])
        d = desc(p.get("description", {}))
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
          + (f"  Block {c(str(blk), 'blue')}" if blk > 0 else "")
          + f"  Gold {c(str(gold), 'yellow')}  Deck {deck}")
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
    print(f"  {c(f'Round {rnd}', 'bold')}  Energy {c(f'{energy}/{max_energy}', 'cyan')}  Draw {draw}  Discard {discard}")
    show_player(state.get("player", {}))

    print()
    for e in state.get("enemies", []):
        hp, mhp = e.get("hp", 0), e.get("max_hp", 1)
        blk = e.get("block", 0)
        intent = c("⚔ ATK", "red") if e.get("intends_attack") else c("? ???", "dim")
        print(f"  [{e['index']}] {n(e['name'])}  {bar(hp, mhp)} {hp}/{mhp}"
              + (f"  Block {c(str(blk), 'blue')}" if blk else "")
              + f"  {intent}")

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

        # Show actual stats inline
        stats = card.get("stats") or {}
        stat_parts = []
        if "damage" in stats: stat_parts.append(c(f"{stats['damage']}dmg", "red"))
        if "block" in stats: stat_parts.append(c(f"{stats['block']}blk", "blue"))
        for k, v in stats.items():
            if k not in ("damage", "block"):
                stat_parts.append(f"{v}{k}")
        stat_str = " ".join(stat_parts)

        print(f"  {mark} [{card['index']}] {c(n(card['name']), type_color)} ({cost_str}) {stat_str}"
              + (f"  → target" if target == "AnyEnemy" else ""))

def show_map(state):
    act_name = n(state.get("act_name", "?"))
    floor = state.get("floor", "?")
    print(f"\n{'═' * 60}")
    print(f"  {c(f'{act_name}', 'bold')} Floor {floor}")
    show_player(state.get("player", {}))
    print()

    type_icons = {
        "Monster": "⚔", "Elite": "💀", "Boss": "👹",
        "RestSite": "🏕", "Shop": "🏪", "Treasure": "💎",
        "Event": "❓", "Unknown": "❓", "Ancient": "🏛",
    }

    for ch in state.get("choices", []):
        icon = type_icons.get(ch["type"], "?")
        print(f"  [{ch['col']},{ch['row']}] {icon} {ch['type']}")

def show_card_reward(state):
    print(f"\n{'─' * 60}")
    print(f"  {c('Card Reward', 'bold')} — choose one (or skip)")
    show_player(state.get("player", {}))
    print()
    for card in state.get("cards", []):
        ctype = card.get("type", "?")
        rarity = card.get("rarity", "Common")
        cost = card.get("cost", "?")
        type_color = {"Attack": "red", "Skill": "blue", "Power": "magenta"}.get(ctype, "reset")
        rarity_color = {"Rare": "yellow", "Uncommon": "cyan"}.get(rarity, "dim")
        card_desc = desc(card.get("description", {}))

        stats = card.get("stats") or {}
        stat_parts = []
        if "damage" in stats: stat_parts.append(c(f"{stats['damage']}dmg", "red"))
        if "block" in stats: stat_parts.append(c(f"{stats['block']}blk", "blue"))
        for k, v in stats.items():
            if k not in ("damage", "block"): stat_parts.append(f"{v}{k}")
        stat_str = " ".join(stat_parts)

        print(f"  [{card['index']}] {c(n(card['name']), type_color)} ({cost}) {c(rarity, rarity_color)} {stat_str}")
        if card_desc:
            print(f"      {c(card_desc, 'dim')}")

def show_shop(state):
    print(f"\n{'─' * 60}")
    print(f"  {c('Shop', 'bold')}")
    show_player(state.get("player", {}))
    gold = state.get("player", {}).get("gold", 0)

    print(f"\n  {c('Cards:', 'bold')}")
    for card in state.get("cards", []):
        if not card.get("is_stocked"): continue
        cost = card.get("cost", 0)
        affordable = c(str(cost), "green") if cost <= gold else c(str(cost), "red")
        sale = c(" SALE", "yellow") if card.get("on_sale") else ""
        print(f"  [{card['index']}] {n(card['name'])} ({card.get('type','?')}) — {affordable}g{sale}")

    print(f"\n  {c('Relics:', 'bold')}")
    for r in state.get("relics", []):
        if not r.get("is_stocked"): continue
        cost = r.get("cost", 0)
        affordable = c(str(cost), "green") if cost <= gold else c(str(cost), "red")
        print(f"  [r{r['index']}] {n(r['name'])} — {affordable}g")

    print(f"\n  {c('Potions:', 'bold')}")
    for p in state.get("potions", []):
        if not p.get("is_stocked"): continue
        cost = p.get("cost", 0)
        affordable = c(str(cost), "green") if cost <= gold else c(str(cost), "red")
        print(f"  [p{p['index']}] {n(p['name'])} — {affordable}g")

    removal_cost = state.get("card_removal_cost")
    if removal_cost:
        affordable = c(str(removal_cost), "green") if removal_cost <= gold else c(str(removal_cost), "red")
        print(f"\n  [rm] Remove a card — {affordable}g")

    print(f"\n  [leave] Leave shop")

def show_rest_site(state):
    print(f"\n{'─' * 60}")
    ctx = state.get("context", {})
    if ctx:
        print(f"  {c(n(ctx.get('act_name','?')), 'dim')} Floor {ctx.get('floor','?')}")
    print(f"  {c('Rest Site', 'bold')}")
    show_player(state.get("player", {}))
    print()
    for opt in state.get("options", []):
        enabled = opt.get("is_enabled", True)
        mark = c("●", "green") if enabled else c("○", "dim")
        print(f"  {mark} [{opt['index']}] {opt.get('option_id', '?')} — {opt.get('name', '?')}")

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

def _render_map(map_data):
    """Render a full ASCII map from get_map response."""
    ctx = map_data.get("context", {})
    act = n(ctx.get("act_name", "?"))
    floor = ctx.get("floor", "?")
    print(f"\n  {c(f'{act} — Floor {floor}', 'bold')}")

    type_icons = {
        "Monster": "⚔", "Elite": "💀", "Boss": "👹",
        "RestSite": "🏕", "Shop": "🏪", "Treasure": "💎",
        "Event": "❓", "Unknown": "❓", "Ancient": "🏛",
        "Unassigned": "·",
    }

    rows = map_data.get("rows", [])
    cur = map_data.get("current_coord")
    max_col = 0
    for row in rows:
        for node in row:
            max_col = max(max_col, node.get("col", 0))

    # Render from bottom (row 0) to top (boss)
    boss = map_data.get("boss", {})
    print(f"  {'':>4} {c('👹 BOSS', 'red')}")
    print(f"  {'':>4} {'│':^{(max_col+1)*4}}")

    for row_idx in range(len(rows) - 1, -1, -1):
        row = rows[row_idx]
        # Build row display
        cells = ['  '] * (max_col + 1)
        for node in row:
            col = node.get("col", 0)
            typ = node.get("type", "?")
            icon = type_icons.get(typ, "?")
            visited = node.get("visited", False)
            is_cur = node.get("current", False)

            if is_cur:
                cells[col] = c(f"[{icon}]", "green")
            elif visited:
                cells[col] = c(f" {icon} ", "dim")
            else:
                cells[col] = f" {icon} "

        row_num = rows[row_idx][0].get("row", row_idx) if rows[row_idx] else row_idx
        line = "".join(f"{cell:^4}" for cell in cells)
        print(f"  {row_num:>2}│ {line}")

    print()

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
    Map:     enter {c('col,row', 'yellow')} (e.g. 3,1)
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
                    print(f"  {c('VICTORY!', 'green')}")
                else:
                    print(f"  {c('DEFEAT', 'red')} at Act {state.get('act')}, Floor {state.get('floor')}")
                show_player(p)
                print(f"{'═' * 60}")
                break

            elif dec == "map_select":
                show_map(state)
                choices = state.get("choices", [])
                valid = {f"{ch['col']},{ch['row']}": ch for ch in choices}

                if auto:
                    # Auto: prefer rest if low HP, else first choice
                    p = state.get("player", {})
                    hp_ratio = p.get("hp", 1) / max(p.get("max_hp", 1), 1)
                    if hp_ratio < 0.4:
                        pick = next((ch for ch in choices if ch["type"] == "RestSite"), choices[0])
                    else:
                        pick = choices[0]
                    key = f"{pick['col']},{pick['row']}"
                else:
                    key = get_input("Choose node (col,row)", set(valid.keys()), state=state)

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
                    choice = get_input("Play card [index], (e)nd turn, or (p0) use potion", set(valid.keys()) | {"help"}, state=state)
                    if choice == "help":
                        print("  Enter card index to play, or 'e' to end turn.")
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
                    choice = get_input("Pick card [index] or (s)kip", set(valid.keys()), state=state)

                if choice == "s":
                    state = send({"cmd": "action", "action": "skip_card_reward"})
                else:
                    state = send({"cmd": "action", "action": "select_card_reward",
                                 "args": {"card_index": int(choice)}})

            elif dec == "card_select":
                # Card selection prompt (upgrade, remove, transform, bundle pick)
                print(f"\n{'─' * 60}")
                ctx = state.get("context", {})
                if ctx:
                    print(f"  {c(n(ctx.get('act_name','?')), 'dim')} Floor {ctx.get('floor','?')}")
                min_sel = state.get("min_select", 1)
                max_sel = state.get("max_select", 1)
                print(f"  {c('Choose cards', 'bold')} (select {min_sel}-{max_sel})")
                show_player(state.get("player", {}))
                print()
                cards = state.get("cards", [])
                for cd in cards:
                    up = c("⬆", "green") if cd.get("upgraded") else ""
                    stats = cd.get("stats") or {}
                    stat_parts = []
                    if "damage" in stats: stat_parts.append(c(f"{stats['damage']}dmg", "red"))
                    if "block" in stats: stat_parts.append(c(f"{stats['block']}blk", "blue"))
                    for k, v in stats.items():
                        if k not in ("damage", "block"): stat_parts.append(f"{v}{k}")
                    stat_str = " ".join(stat_parts)
                    card_desc = desc(cd.get("description", {}))
                    print(f"  [{cd['index']}] {n(cd['name'])}{up} ({cd.get('cost','?')}) {c(cd.get('type',''), 'dim')} {stat_str}")
                    if card_desc:
                        print(f"      {c(card_desc, 'dim')}")

                valid = {str(cd["index"]): cd for cd in cards}
                if min_sel == 0:
                    valid["s"] = None

                if auto:
                    choice = "0"
                else:
                    choice = get_input(f"Choose card(s) [index] or (s)kip", set(valid.keys()), state=state)

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
                    choice = get_input("Buy [index/r0/p0/rm] or (leave)", state=state)

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
                    choice = get_input("Choose option [index]", set(valid.keys()), state=state)

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
                    choice = get_input("Choose option [index] or (leave)", set(valid.keys()), state=state)

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
