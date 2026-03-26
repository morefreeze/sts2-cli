#!/usr/bin/env python3
"""coordinator.py — full-game runner combining RL combat + LLM strategy.

Usage:
    python3 agent/coordinator.py --character Ironclad --mode eval-full
    python3 agent/coordinator.py --character Ironclad --mode eval-rl --n-games 20 --verbose
    python3 agent/coordinator.py --character Ironclad --mode eval-rl --lang zh --verbose
"""
import argparse
import json
import os
import subprocess
import sys
import time

# Import play.py display functions for combat replay
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
try:
    import play as _play
    _has_play = True
except ImportError:
    _has_play = False

def _find_dotnet():
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"),
              "/usr/local/share/dotnet/dotnet", "dotnet"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0: return p
        except (FileNotFoundError, subprocess.TimeoutExpired): continue
    return "dotnet"

DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "Sts2Headless", "Sts2Headless.csproj")
CARDS_JSON = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "checkpoints")

RL_DECISIONS = {"combat_play"}
LLM_DECISIONS = {"map_select", "card_reward", "rest_site", "event_choice",
                 "shop", "bundle_select", "card_select"}

NODE_TYPE_ZH = {
    "Monster": "怪物", "Elite": "精英", "Boss": "Boss",
    "RestSite": "休息", "Shop": "商店", "Treasure": "宝箱",
    "Event": "事件", "Unknown": "未知", "Ancient": "远古",
}
REST_OPT_ZH = {"HEAL": "休息", "SMITH": "升级", "LIFT": "锻炼", "DIG": "挖掘", "RECALL": "回忆"}
CARD_TYPE_ZH = {"Attack": "攻击", "Skill": "技能", "Power": "能力", "Status": "状态", "Curse": "诅咒"}

# ANSI color helpers
def _c(text, color):
    codes = {"red": "91", "green": "92", "yellow": "93", "blue": "94",
             "magenta": "95", "cyan": "96", "dim": "2", "bold": "1", "reset": "0"}
    code = codes.get(color, "0")
    return f"\033[{code}m{text}\033[0m"


class GameCoordinator:
    def __init__(self, rl_agent, llm_agent=None, verbose=False, lang="zh"):
        self.rl = rl_agent
        self.llm = llm_agent
        self._proc = None
        self.verbose = verbose
        self.lang = lang

    def _vlog(self, msg):
        if self.verbose:
            print(f"[game] {msg}", file=sys.stderr)

    def _name(self, obj):
        if isinstance(obj, dict):
            return obj.get(self.lang, obj.get("en", str(obj)))
        return str(obj) if obj else "?"

    @staticmethod
    def _resolve_vars(text, vars_dict):
        """Replace {VarName} and [VarName] placeholders with actual values.
        Unresolved placeholders are cleaned up (removed or replaced with ?)."""
        if not text:
            return ""
        import re
        lower_vars = {}
        if vars_dict:
            lower_vars = {k.lower(): v for k, v in vars_dict.items()}
        def replacer(m):
            key = m.group(1)
            val = lower_vars.get(key.lower())
            return str(val) if val is not None else "?"
        text = re.sub(r'\{(\w+)\}', replacer, text)
        text = re.sub(r'\[(\w+)\]', replacer, text)
        return text

    def _floor(self, state):
        return state.get("floor") or state.get("context", {}).get("floor", "?")

    def _card_str(self, card, gold_price=None):
        """Format a card with colored energy cost, type, stats.
        gold_price: if set, show gold price instead of energy cost (for shop)."""
        name = self._name(card.get("name", "?"))
        cost = card.get("cost", "?")
        ctype = card.get("type", "")
        stats = card.get("stats") or {}
        rarity = card.get("rarity", "")
        zh = self.lang == "zh"

        type_colors = {"Attack": "red", "Skill": "blue", "Power": "magenta"}
        name_colored = _c(name, type_colors.get(ctype, "reset"))

        if gold_price is not None:
            cost_str = _c(f"{gold_price}金", "yellow") if zh else _c(f"{gold_price}g", "yellow")
        else:
            cost_str = _c(f"{cost}费", "cyan") if zh else _c(f"{cost}E", "cyan")

        # Stats: damage, block, draw, energy, magic_number, etc.
        parts = []
        if "damage" in stats:
            parts.append(_c(f"{stats['damage']}{'伤' if zh else 'dmg'}", "red"))
        if "block" in stats:
            parts.append(_c(f"{stats['block']}{'挡' if zh else 'blk'}", "blue"))
        if "draw" in stats:
            parts.append(_c(f"+{stats['draw']}{'抽' if zh else 'draw'}", "green"))
        if "energy" in stats:
            parts.append(_c(f"+{stats['energy']}{'能' if zh else 'energy'}", "cyan"))
        if "magic_number" in stats and "damage" not in stats and "block" not in stats:
            parts.append(_c(f"×{stats['magic_number']}", "yellow"))
        stat_str = " ".join(parts)

        type_label = CARD_TYPE_ZH.get(ctype, ctype) if zh else ctype

        rarity_colors = {"Rare": "yellow", "Uncommon": "cyan"}
        rarity_str = ""
        if rarity and rarity != "Common":
            rarity_label = {"Rare": "稀有", "Uncommon": "罕见"}.get(rarity, rarity) if zh else rarity
            rarity_str = f" {_c(rarity_label, rarity_colors.get(rarity, 'dim'))}"

        result = f"{name_colored} ({cost_str} {_c(type_label, 'dim')})"
        if stat_str:
            result += f" {stat_str}"
        if rarity_str:
            result += rarity_str
        return result

    def _relic_str(self, relic):
        name = self._name(relic.get("name", "?"))
        return _c(name, "yellow")

    def _combat_enemy_names(self, state):
        enemies = state.get("enemies", [])
        names = [self._name(e.get("name", "?")) for e in enemies]
        return ", ".join(names) if names else "?"

    def _on_combat_end(self, prev_state, new_state):
        floor = self._floor(prev_state)
        enemies = self._combat_enemy_names(prev_state)
        hp_before = self._combat_start_hp
        player = new_state.get("player", {})
        hp_after = player.get("hp", "?")
        is_game_over = new_state.get("decision") == "game_over"
        zh = self.lang == "zh"

        if is_game_over and not new_state.get("victory", False):
            won_str = _c("败", "red") if zh else _c("Lost", "red")
        else:
            won_str = _c("胜", "green") if zh else _c("Won", "green")

        hp_color = "green" if isinstance(hp_after, int) and isinstance(hp_before, int) and hp_after >= hp_before else "red"
        hp_str = _c(f"{hp_before}→{hp_after}", hp_color)

        prefix = f"第{floor}层" if zh else f"Floor {floor}"
        label = "战斗" if zh else "Combat"
        self._vlog(f"[{prefix}] {label}: {enemies} — {won_str} (HP: {hp_str})")

    def _on_action(self, prev_state, action, new_state):
        decision = prev_state.get("decision", "")
        floor = self._floor(prev_state)
        act_name = action.get("action", "")
        args = action.get("args", {})
        zh = self.lang == "zh"
        prefix = f"第{floor}层" if zh else f"Floor {floor}"

        if decision == "map_select":
            node_type = "?"
            for c in prev_state.get("choices", []):
                if c.get("col") == args.get("col") and c.get("row") == args.get("row"):
                    raw = c.get("type", "?")
                    node_type = NODE_TYPE_ZH.get(raw, raw) if zh else raw
                    break
            icon = {"Monster": "⚔", "Elite": "💀", "Boss": "👹", "RestSite": "🏕",
                    "Shop": "🏪", "Treasure": "💎", "Event": "❓", "Unknown": "❓"}.get(
                    next((c.get("type","") for c in prev_state.get("choices",[])
                          if c.get("col")==args.get("col") and c.get("row")==args.get("row")), ""), "")
            label = "地图" if zh else "Map"
            self._vlog(f"[{prefix}] {label}: {icon} {node_type}")

        elif decision == "card_reward":
            if act_name == "skip_card_reward":
                self._vlog(f"[{prefix}] {'卡牌奖励' if zh else 'Card Reward'}: {_c('跳过' if zh else 'skip', 'dim')}")
            else:
                idx = args.get("card_index", 0)
                cards = prev_state.get("cards", [])
                if idx < len(cards):
                    card_detail = self._card_str(cards[idx])
                else:
                    card_detail = f"#{idx}"
                label = "卡牌奖励" if zh else "Card Reward"
                # Show all options with chosen highlighted
                if cards:
                    all_cards = []
                    for i, cd in enumerate(cards):
                        s = self._card_str(cd)
                        if i == idx:
                            s = f"[{_c('✓', 'green')}] {s}"
                        else:
                            s = f"[ ] {s}"
                        all_cards.append(s)
                    self._vlog(f"[{prefix}] {label}:")
                    for s in all_cards:
                        self._vlog(f"    {s}")
                else:
                    self._vlog(f"[{prefix}] {label}: {card_detail}")

        elif decision == "rest_site":
            opt_idx = args.get("option_index", 0)
            opts = prev_state.get("options", [])
            opt_id = opts[opt_idx].get("option_id", "?") if opt_idx < len(opts) else "?"
            opt_name = REST_OPT_ZH.get(opt_id, opt_id) if zh else opt_id
            # Color: heal=green, smith=cyan
            opt_color = {"HEAL": "green", "SMITH": "cyan"}.get(opt_id, "yellow")
            label = "休息" if zh else "Rest"
            self._vlog(f"[{prefix}] {label}: {_c(opt_name, opt_color)}")

        elif decision == "event_choice":
            event_name = self._name(prev_state.get("event_name", prev_state.get("event", "?")))
            opt_idx = args.get("option_index", 0)
            opts = prev_state.get("options", [])
            label = "事件" if zh else "Event"
            self._vlog(f"[{prefix}] {label}: {_c(event_name, 'yellow')}")
            for i, opt in enumerate(opts):
                raw = opt.get("title") or opt.get("name") or opt.get("option_id") or ""
                opt_text = self._name(raw) if raw else f"{'选项' if zh else 'option'} {i}"
                desc = opt.get("description")
                opt_vars = opt.get("vars") or {}
                desc_resolved = self._resolve_vars(self._name(desc), opt_vars) if desc else ""
                desc_text = f" — {_c(desc_resolved, 'dim')}" if desc_resolved else ""
                if i == opt_idx:
                    self._vlog(f"    [{_c('✓', 'green')}] {opt_text}{desc_text}")
                else:
                    locked = opt.get("is_locked", False)
                    mark = _c("✗", "red") if locked else " "
                    self._vlog(f"    [{mark}] {opt_text}{desc_text}")

        elif decision == "shop":
            if act_name == "leave_room":
                self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {_c('离开' if zh else 'left', 'dim')}")
            elif act_name == "remove_card":
                cost = prev_state.get("card_removal_cost", "?")
                self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {_c('移除卡牌' if zh else 'remove card', 'magenta')} ({_c(f'{cost}金' if zh else f'{cost}g', 'yellow')})")
            elif act_name == "buy_card":
                idx = args.get("card_index", 0)
                cards = prev_state.get("cards", [])
                card = next((c for c in cards if c.get("index") == idx), None)
                if card:
                    gold = card.get("cost", "?")  # shop cost = gold price
                    self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {'购买' if zh else 'buy'} {self._card_str(card, gold_price=gold)}")
                else:
                    self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {'购买卡牌' if zh else 'buy card'} #{idx}")
            elif act_name == "buy_relic":
                idx = args.get("relic_index", 0)
                relics = prev_state.get("relics", [])
                relic = next((r for r in relics if r.get("index") == idx), None)
                if relic:
                    cost = relic.get("cost", "?")
                    self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {'购买' if zh else 'buy'} {self._relic_str(relic)} ({_c(f'{cost}金' if zh else f'{cost}g', 'yellow')})")
                else:
                    self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {'购买遗物' if zh else 'buy relic'} #{idx}")
            else:
                self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {act_name}")

        elif decision == "bundle_select":
            idx = args.get("bundle_index", 0)
            bundles = prev_state.get("bundles", [])
            label = "卡牌包" if zh else "Bundle"
            if idx < len(bundles):
                bundle = bundles[idx]
                cards_in = [self._name(cd.get("name", "?")) for cd in bundle.get("cards", [])]
                self._vlog(f"[{prefix}] {label}: {', '.join(cards_in)}")
            else:
                self._vlog(f"[{prefix}] {label}: #{idx}")

        elif decision == "card_select":
            label = "选牌" if zh else "Card Select"
            if act_name == "skip_select":
                self._vlog(f"[{prefix}] {label}: {_c('跳过' if zh else 'skip', 'dim')}")
            else:
                indices = args.get("indices", "")
                cards = prev_state.get("cards", [])
                selected = []
                for idx_str in str(indices).split(","):
                    idx_str = idx_str.strip()
                    if idx_str.isdigit():
                        idx = int(idx_str)
                        if idx < len(cards):
                            selected.append(self._card_str(cards[idx]))
                self._vlog(f"[{prefix}] {label}: {', '.join(selected) if selected else indices}")

    def _replay_combat(self, combat_log):
        """Replay the last combat — one line per round showing cards played + enemy intents."""
        if not combat_log:
            return
        zh = self.lang == "zh"
        self._vlog("")
        self._vlog(f"{'─'*55}")
        self._vlog(f"  {_c('最后一战回放' if zh else 'Last Combat Replay', 'bold')}")
        self._vlog(f"{'─'*55}")

        # Group entries by round
        rounds = {}  # round_num -> {cards: [], enemies: [], hp_start, hp_end, energy}
        for entry in combat_log:
            state = entry.get("state")
            action = entry.get("action")
            if not state or state.get("decision") != "combat_play":
                continue
            rnd = state.get("round", 0)
            if rnd not in rounds:
                enemies = state.get("enemies", [])
                rounds[rnd] = {
                    "cards": [],
                    "hp_start": state.get("player", {}).get("hp", "?"),
                    "hp_end": state.get("player", {}).get("hp", "?"),
                    "energy": state.get("energy", "?"),
                    "enemies": [
                        {"name": self._name(e.get("name", "?")),
                         "hp": e.get("hp", "?"),
                         "intent": self._enemy_intent(e)}
                        for e in enemies
                    ],
                }
            # Track HP changes within the round
            rounds[rnd]["hp_end"] = state.get("player", {}).get("hp", "?")

            act_name = action.get("action", "") if action else ""
            if act_name == "play_card":
                ci = action.get("args", {}).get("card_index", 0)
                hand = state.get("hand", [])
                card = next((c for c in hand if c.get("index") == ci), None)
                if card:
                    cname = self._name(card.get("name", "?"))
                    ti = action.get("args", {}).get("target_index")
                    target = ""
                    if ti is not None:
                        enemies = state.get("enemies", [])
                        enemy = next((e for e in enemies if e.get("index") == ti), None)
                        if enemy and len(enemies) > 1:
                            target = f"→{self._name(enemy.get('name', '?'))}"
                    rounds[rnd]["cards"].append(f"{cname}{target}")

        # Print per-round summary
        skipped = 0
        for rnd in sorted(rounds.keys()):
            r = rounds[rnd]
            cards = r["cards"]
            if not cards:
                skipped += 1
                continue
            if skipped > 0:
                self._vlog(f"  {_c(f'... {skipped}回合空过 ...' if zh else f'... {skipped} rounds skipped ...', 'dim')}")
                skipped = 0

            # Round header
            hp_start = r["hp_start"]
            hp_end = r["hp_end"]
            hp_color = "red" if isinstance(hp_end, int) and isinstance(hp_start, int) and hp_end < hp_start else "green"
            hp_str = f"{_c(str(hp_start), 'red')}" + (f"→{_c(str(hp_end), hp_color)}" if hp_end != hp_start else "")

            # Enemy intents
            enemy_str = " | ".join(
                f"{e['name']} {e['hp']}HP {e['intent']}"
                for e in r["enemies"]
            )

            # Cards played
            cards_str = ", ".join(cards)

            label = f"R{rnd}" if not zh else f"回合{rnd}"
            self._vlog(f"  {_c(label, 'bold')} HP:{hp_str} E{r['energy']} | {_c(cards_str, 'yellow')} | {_c(enemy_str, 'dim')}")

        if skipped > 0:
            self._vlog(f"  {_c(f'... {skipped}回合空过 ...' if zh else f'... {skipped} rounds skipped ...', 'dim')}")

        total = len(combat_log)
        plays = sum(1 for e in combat_log if (e.get("action") or {}).get("action") == "play_card")
        self._vlog(f"  {'总计' if zh else 'Total'}: {total}{'步' if zh else ' steps'}, {plays}{'张牌' if zh else ' cards played'}")
        self._vlog(f"{'─'*55}")

    def _enemy_intent(self, enemy):
        """Format enemy intent as a short string."""
        intent = enemy.get("intent") or {}
        itype = intent.get("type", "")
        dmg = intent.get("damage")
        times = intent.get("times", 1)
        if itype == "Attack":
            return _c(f"⚔{dmg}" + (f"×{times}" if times > 1 else ""), "red")
        elif itype == "Defend":
            return _c(f"🛡{intent.get('block', '?')}", "blue")
        elif itype in ("Buff", "StrengthBuff"):
            return _c("⬆增益" if self.lang == "zh" else "⬆buff", "yellow")
        elif itype in ("Debuff", "DebuffWeak", "DebuffStrong"):
            return _c("⬇减益" if self.lang == "zh" else "⬇debuff", "yellow")
        elif itype in ("AttackDebuff", "AttackBuff"):
            dmg_str = f"⚔{dmg}" if dmg else "⚔"
            return _c(f"{dmg_str}+⬇", "red")
        elif itype == "AttackDefend":
            return _c(f"⚔{dmg}+🛡", "red")
        elif itype == "StatusCard":
            return _c("⬇塞牌" if self.lang == "zh" else "⬇cards", "yellow")
        elif itype == "Sleep":
            return _c("💤", "dim")
        elif itype == "Escape":
            return _c("🏃", "dim")
        return _c(itype or "?", "dim")

    def run_game(self, character: str, seed: str, ascension: int = 0) -> dict:
        from agent.combat_env import greedy_action
        try:
            self._start_proc()
            state = self._send({"cmd": "start_run", "character": character,
                                "seed": seed, "ascension": ascension,
                                "lang": self.lang})
            if state is None:
                return {"victory": False, "seed": seed, "error": "start_failed"}

            prev_decision = ""
            self._combat_start_hp = None
            self._current_act = None
            combat_log = []  # current combat: [{state, action}, ...]
            last_combat_log = []  # previous combat's log (for replay on death)

            _consecutive_errors = 0
            _room_start = time.time()
            _room_decision = ""
            _room_timer_shown = False
            _room_timeout = 60  # 1 minute per room
            for step in range(600):
                # Handle engine errors (e.g. end_turn rejected)
                if state.get("type") == "error":
                    _consecutive_errors += 1
                    err_msg = state.get("message", "?")
                    if _consecutive_errors <= 3:
                        self._vlog(f"  {_c('⚠', 'yellow')} {err_msg}")
                    if _consecutive_errors > 10:
                        self._vlog(f"  {_c('引擎反复报错，尝试 proceed' if self.lang == 'zh' else 'Engine stuck in errors, trying proceed', 'red')}")
                        state = self._send({"cmd": "action", "action": "proceed"})
                        if state is None:
                            break
                        continue
                    # Retry: if in combat, try end_turn; otherwise proceed
                    if prev_decision == "combat_play":
                        state = self._send({"cmd": "action", "action": "end_turn"})
                    else:
                        state = self._send({"cmd": "action", "action": "proceed"})
                    if state is None:
                        break
                    continue
                else:
                    _consecutive_errors = 0

                decision = state.get("decision", "")

                # Room timer: reset on room change, show if >10s, timeout at 60s
                if decision != _room_decision:
                    _room_start = time.time()
                    _room_decision = decision
                    _room_timer_shown = False
                room_elapsed = time.time() - _room_start
                if self.verbose and room_elapsed > 10 and not _room_timer_shown:
                    _room_timer_shown = True
                if self.verbose and _room_timer_shown and int(room_elapsed) % 5 == 0:
                    print(f"\r[game]   ⏱ {room_elapsed:.0f}s / {_room_timeout}s", end="", file=sys.stderr, flush=True)
                if room_elapsed > _room_timeout:
                    zh = self.lang == "zh"
                    floor = self._floor(state)
                    self._vlog(f"\n  {_c(f'房间超时 ({_room_timeout}s)' if zh else f'Room timeout ({_room_timeout}s)', 'red')}")
                    # Force end this room
                    if decision == "combat_play":
                        log = combat_log if combat_log else last_combat_log
                        if self.verbose and log:
                            self._replay_combat(log)
                    hp = state.get("player", {}).get("hp", "?")
                    max_hp = state.get("player", {}).get("max_hp", "?")
                    return {"victory": False, "seed": seed, "steps": step,
                            "floor": floor, "hp": hp, "max_hp": max_hp, "error": "room_timeout"}

                # Detect Act change
                act = state.get("act") or state.get("context", {}).get("act")
                if self.verbose and act and act != self._current_act:
                    act_name = state.get("context", {}).get("act_name", "")
                    if act_name:
                        act_name = self._name(act_name)
                    zh = self.lang == "zh"
                    if self._current_act is not None:
                        self._vlog(f"")
                        self._vlog(f"{'━'*50}")
                    self._vlog(f"  {_c(f'第{act}幕' if zh else f'Act {act}', 'bold')}" +
                               (f" — {act_name}" if act_name else ""))
                    if self._current_act is not None:
                        self._vlog(f"{'━'*50}")
                    self._current_act = act

                # Track combat entry
                if decision == "combat_play" and prev_decision != "combat_play":
                    self._combat_start_hp = state.get("player", {}).get("hp", "?")
                    combat_log = []

                # Record combat steps
                if decision == "combat_play":
                    combat_log.append({"state": state, "action": None})

                # Combat ended
                if self.verbose and prev_decision == "combat_play" and decision != "combat_play":
                    self._on_combat_end(prev_state, state)
                    last_combat_log = combat_log
                    combat_log = []

                if decision == "game_over":
                    hp = state.get("player", {}).get("hp", "?")
                    max_hp = state.get("player", {}).get("max_hp", "?")
                    floor = self._floor(state)
                    act = state.get("act") or state.get("context", {}).get("act")
                    victory = state.get("victory", False)

                    if self.lang == "zh":
                        outcome = _c("胜利", "green") if victory else _c("战败", "red")
                        self._vlog(f"{'═'*50}")
                        self._vlog(f"  {outcome} 第{floor}层, HP: {hp}/{max_hp}")
                        self._vlog(f"{'═'*50}")
                    else:
                        outcome = _c("VICTORY", "green") if victory else _c("DEFEAT", "red")
                        self._vlog(f"{'═'*50}")
                        self._vlog(f"  {outcome} at Floor {floor}, HP: {hp}/{max_hp}")
                        self._vlog(f"{'═'*50}")

                    # On defeat, replay the last combat
                    log = combat_log if combat_log else last_combat_log
                    if self.verbose and not victory and log:
                        self._replay_combat(log)

                    return {
                        "victory": victory, "seed": seed, "steps": step,
                        "act": act, "floor": floor,
                        "hp": state.get("player", {}).get("hp"),
                        "max_hp": state.get("player", {}).get("max_hp"),
                    }

                if decision in RL_DECISIONS:
                    action = self.rl.act(state)
                elif decision in LLM_DECISIONS:
                    action = self.llm.act(state) if self.llm else greedy_action(state)
                else:
                    action = {"cmd": "action", "action": "proceed"}

                # Record action in combat log
                if decision == "combat_play" and combat_log:
                    combat_log[-1]["action"] = action

                # Record combat actions (only printed on defeat/timeout replay)
                # No live output for combat — just the summary at end

                if self.verbose and decision not in RL_DECISIONS and decision != "":
                    self._on_action(state, action, state)

                prev_state = state
                next_state = self._send(action)

                # Detect stuck state: end_turn didn't advance (engine bug)
                if (next_state and next_state.get("decision") == "combat_play"
                        and action.get("action") == "end_turn"
                        and next_state.get("round") == state.get("round")
                        and next_state.get("player", {}).get("hp") == state.get("player", {}).get("hp")):
                    # end_turn was ignored — try proceed to unstick
                    for _retry in range(5):
                        next_state = self._send({"cmd": "action", "action": "proceed"})
                        if next_state is None or next_state.get("decision") != "combat_play":
                            break
                        if next_state.get("round") != state.get("round"):
                            break
                        if next_state.get("player", {}).get("hp") != state.get("player", {}).get("hp"):
                            break

                if next_state is None:
                    floor = self._floor(prev_state)
                    hp = prev_state.get("player", {}).get("hp", "?")
                    max_hp = prev_state.get("player", {}).get("max_hp", "?")
                    zh = self.lang == "zh"
                    self._vlog(f"{'═'*50}")
                    self._vlog(f"  {_c('连接断开' if zh else 'EOF', 'red')} {'第' if zh else 'Floor '}{floor}{'层' if zh else ''}, HP: {hp}/{max_hp}")
                    self._vlog(f"{'═'*50}")
                    log = combat_log if combat_log else last_combat_log
                    if self.verbose and log:
                        self._replay_combat(log)
                    return {"victory": False, "seed": seed, "steps": step,
                            "floor": floor, "hp": hp, "max_hp": max_hp, "error": "eof"}
                prev_decision = decision
                state = next_state

            # Timeout — combat took too long
            floor = self._floor(prev_state) if prev_state else "?"
            hp = prev_state.get("player", {}).get("hp", "?") if prev_state else "?"
            max_hp = prev_state.get("player", {}).get("max_hp", "?") if prev_state else "?"
            zh = self.lang == "zh"
            self._vlog(f"{'═'*50}")
            self._vlog(f"  {_c('超时' if zh else 'TIMEOUT', 'red')} {'第' if zh else 'Floor '}{floor}{'层' if zh else ''} ({_c('600步' if zh else '600 steps', 'dim')})")
            self._vlog(f"{'═'*50}")
            log = combat_log if combat_log else last_combat_log
            if self.verbose and log:
                # Only replay last 10 turns to avoid flooding
                self._replay_combat(log[-30:])
            return {"victory": False, "seed": seed, "steps": 600,
                    "floor": floor, "hp": hp, "max_hp": max_hp, "error": "timeout"}
        finally:
            self._kill_proc()

    def _start_proc(self):
        self._proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=PROJECT_ROOT
        )
        self._read_json()

    def _kill_proc(self):
        if self._proc:
            try:
                self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self._proc.stdin.flush()
            except Exception: pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
            self._proc = None

    def _read_json(self):
        if not self._proc: return None
        for _ in range(1000):
            line = self._proc.stdout.readline().strip()
            if not line: return None
            if line.startswith("{"):
                try: return json.loads(line)
                except json.JSONDecodeError: continue
        return None

    def _send(self, cmd: dict):
        if not self._proc: return None
        try:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()
            return self._read_json()
        except Exception: return None


def _load_env():
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())


def main():
    _load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--mode", choices=["eval-rl", "eval-full"], default="eval-rl")
    parser.add_argument("--n-games", type=int, default=10)
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--verbose", action="store_true", help="Print per-room progress to stderr")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh", help="Output language (default: zh)")
    args = parser.parse_args()

    if args.checkpoint is None:
        if not os.path.isdir(CHECKPOINT_DIR):
            print(f"Checkpoint directory not found: {CHECKPOINT_DIR}")
            sys.exit(1)
        import re
        files = sorted(
            (f for f in os.listdir(CHECKPOINT_DIR) if f.startswith(f"ppo_{args.character.lower()}")),
            key=lambda f: int(re.search(r'(\d+)k', f).group(1)) if re.search(r'(\d+)k', f) else 0
        )
        if not files:
            print(f"No checkpoint found in {CHECKPOINT_DIR}"); sys.exit(1)
        args.checkpoint = os.path.join(CHECKPOINT_DIR, files[-1])
    print(f"Loading RL checkpoint: {args.checkpoint}")

    from agent.rl_agent import RLAgent
    rl = RLAgent(args.checkpoint, CARDS_JSON)

    llm = None
    if args.mode == "eval-full":
        if not args.api_key:
            print("ANTHROPIC_API_KEY not set"); sys.exit(1)
        from agent.llm_agent import LLMAgent
        llm = LLMAgent(api_key=args.api_key, cards_json=CARDS_JSON)

    coord = GameCoordinator(rl_agent=rl, llm_agent=llm, verbose=args.verbose, lang=args.lang)
    print(f"\nRunning {args.n_games} games | {args.character} | {args.mode} | A{args.ascension}")
    print("=" * 60)
    results = []
    import random as _rng
    for i in range(args.n_games):
        seed = f"eval_{args.character.lower()}_{i}_{_rng.randint(0,99999)}"
        result = coord.run_game(args.character, seed, args.ascension)
        results.append(result)
        status = "WIN" if result.get("victory") else "LOSS"
        print(f"  Game {i+1:2d}: {status} | floor={result.get('floor')} | "
              f"hp={result.get('hp')}/{result.get('max_hp')}")

    wins = sum(1 for r in results if r.get("victory"))
    pct = (100.0 * wins / args.n_games) if args.n_games > 0 else 0.0
    print(f"\nWin rate: {wins}/{args.n_games} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
