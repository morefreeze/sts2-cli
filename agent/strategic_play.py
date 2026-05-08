#!/usr/bin/env python3
"""
Strategic player for STS2 — plays with explicit game knowledge.
Used to observe game mechanics and derive RL training strategy.
"""

import json
import subprocess
import sys
import os
import random

def _find_dotnet():
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"),
              "/usr/local/share/dotnet/dotnet", "dotnet"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return "dotnet"

DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")

# Card priority scores for Ironclad (higher = better)
CARD_PRIORITY = {
    # S-tier
    "Limit Break": 100, "Barricade": 95, "Demon Form": 90, "Whirlwind": 85,
    "Reaper": 80, "Bludgeon": 78, "Juggernaut": 75, "Feed": 72,
    # A-tier
    "Inflame": 70, "Metallicize": 68, "Combust": 65, "Immolate": 63,
    "Pommel Strike": 60, "Flex": 58, "Clothesline": 55, "True Grit": 52,
    "Headbutt": 50, "Shrug It Off": 48, "Second Wind": 45, "Battle Trance": 42,
    "Carnage": 40, "Heavy Blade": 38, "Iron Wave": 35,
    # B-tier
    "Wild Strike": 30, "Uppercut": 28, "Sever Soul": 25, "Corruption": 22,
    "Ghostly Armor": 20, "Sentinel": 18,
    # C-tier (situational)
    "Armaments": 15, "Blood for Blood": 12, "Entrench": 10,
    # Skip / bad
    "Cleave": 5, "Thunderclap": 5, "Bash": 3,
    # Never take
    "Wound": -50, "Slimed": -50, "Burn": -50, "Dazed": -50,
    "Clumsy": -50, "Normality": -50, "Regret": -50,
}

GAME_LOG = []

def log(msg):
    GAME_LOG.append(msg)
    print(msg)

class StrategicPlayer:
    def __init__(self, character="Ironclad", seed=None, game_num=1):
        self.character = character
        self.seed = seed or f"strat_{game_num}_{random.randint(1000, 9999)}"
        self.game_num = game_num
        self.proc = None
        self.turn = 0
        self.combat_history = []  # track what we see in combat
        self.deck_cards = []
        self.relics = []
        self.notes = []  # strategic observations

    def _name(self, obj):
        if isinstance(obj, dict):
            return obj.get("en", obj.get("zh", str(obj)))
        return str(obj) if obj else "?"

    def _read_json(self):
        while True:
            line = self.proc.stdout.readline().strip()
            if not line:
                raise RuntimeError("EOF from game process")
            if line.startswith("{"):
                return json.loads(line)

    def _send(self, cmd):
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        return self._read_json()

    # ─── Combat Strategy ───────────────────────────────────────────────────────

    def _card_score(self, card, state):
        """Score a card for playing this turn."""
        name_en = self._name(card.get("name", {}))
        base = CARD_PRIORITY.get(name_en, 15)  # default mid-tier

        enemies = state.get("enemies", [])
        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        energy = state.get("energy", 3)
        cost = card.get("cost", 1)

        # Prefer defensive cards when low HP
        card_type = card.get("card_type", {})
        type_en = self._name(card_type) if isinstance(card_type, dict) else str(card_type)

        score = base

        # Prioritize block/defense when low HP
        if hp_ratio < 0.4:
            if "Block" in name_en or "Armor" in name_en or "Shrug" in name_en:
                score += 30
            if "Sentinel" in name_en or "True Grit" in name_en:
                score += 20

        # Prioritize weak/vulnerable applicators when multiple enemies
        n_enemies = len([e for e in enemies if e.get("hp", 0) > 0])
        if n_enemies > 1:
            if "Thunderclap" in name_en or "Cleave" in name_en or "Whirlwind" in name_en:
                score += 25
            if "Clothesline" in name_en:  # applies Weak
                score += 15

        # Prefer energy-efficient cards (cost 0 is great)
        if cost == 0:
            score += 20
        elif cost == 1:
            score += 5
        elif cost >= 3:
            score -= 10

        # Prefer cards that can kill an enemy (finish them off)
        if enemies:
            lowest_hp_enemy = min(e.get("hp", 999) for e in enemies if e.get("hp", 0) > 0)
            dmg = card.get("damage", 0)
            if dmg > 0 and dmg >= lowest_hp_enemy:
                score += 25  # lethal shot

        return score

    def _pick_target(self, card, enemies):
        """Choose best target for a card."""
        alive = [e for e in enemies if e.get("hp", 0) > 0]
        if not alive:
            return 0

        intent = card.get("target_type", "AnyEnemy") if isinstance(card, dict) else "AnyEnemy"
        name_en = self._name(card.get("name", {})) if isinstance(card, dict) else ""

        # For weak/vulnerable applicators, target enemy with highest HP
        if "Clothesline" in name_en:
            target = max(alive, key=lambda e: e.get("hp", 0))
            return target.get("index", 0)

        # For killing blows, pick lowest HP enemy
        dmg = card.get("damage", 0) if isinstance(card, dict) else 0
        if dmg > 0:
            killable = [e for e in alive if e.get("hp", 0) <= dmg]
            if killable:
                return killable[0].get("index", 0)

        # Default: highest damage attacker
        most_dangerous = max(alive, key=lambda e: e.get("intent_damage", 0) or 0)
        return most_dangerous.get("index", 0)

    def _choose_combat_action(self, state):
        """Decide combat action with strategic priority."""
        hand = state.get("hand", [])
        energy = state.get("energy", 3)
        enemies = state.get("enemies", [])
        player = state.get("player", {})
        block = player.get("block", 0)
        hp = player.get("hp", 80)
        max_hp = player.get("max_hp", 80)

        # Calculate incoming damage
        incoming = sum(
            e.get("intent_damage", 0) or 0
            for e in enemies if e.get("hp", 0) > 0
        )

        alive_enemies = [e for e in enemies if e.get("hp", 0) > 0]

        # Log state summary
        enemy_str = ", ".join(
            f"{self._name(e.get('name', {}))}({e.get('hp', '?')}/{e.get('max_hp', '?')} "
            f"{'ATK:' + str(e.get('intent_damage', 0)) if e.get('intent_damage', 0) else 'DEF'})"
            for e in alive_enemies
        )
        hand_str = ", ".join(
            f"{self._name(c.get('name', {}))}({c.get('cost', '?')})"
            for c in hand
        )
        print(f"    HP:{hp}/{max_hp} Block:{block} Energy:{energy} Incoming:{incoming}")
        print(f"    Enemies: {enemy_str}")
        print(f"    Hand: {hand_str}")

        # Find playable cards
        playable = [c for c in hand
                    if c.get("can_play", False) and c.get("cost", 99) <= energy]

        if not playable:
            print("    -> End turn (no playable cards)")
            return {"cmd": "action", "action": "end_turn"}

        # Score all playable cards
        scored = [(self._card_score(c, state), c) for c in playable]
        scored.sort(key=lambda x: -x[0])
        best_score, best_card = scored[0]

        name_en = self._name(best_card.get("name", {}))
        target_type = best_card.get("target_type", "None")

        if target_type in ("AnyEnemy", "Enemy") and alive_enemies:
            target_idx = self._pick_target(best_card, alive_enemies)
            print(f"    -> Play {name_en}(score={best_score:.0f}) -> target {target_idx}")
            return {
                "cmd": "action", "action": "play_card",
                "args": {"card_index": best_card["index"], "target_index": target_idx}
            }
        elif target_type in ("None", "Self", "AllEnemies", ""):
            print(f"    -> Play {name_en}(score={best_score:.0f})")
            return {
                "cmd": "action", "action": "play_card",
                "args": {"card_index": best_card["index"]}
            }
        else:
            print(f"    -> Play {name_en}(score={best_score:.0f}) target_type={target_type}")
            args = {"card_index": best_card["index"]}
            if alive_enemies:
                args["target_index"] = alive_enemies[0].get("index", 0)
            return {"cmd": "action", "action": "play_card", "args": args}

    # ─── Map Navigation ────────────────────────────────────────────────────────

    def _choose_map_node(self, state):
        """Strategic map navigation."""
        choices = state.get("choices", [])
        player = state.get("player", {})
        hp = player.get("hp", 80)
        max_hp = player.get("max_hp", 80)
        hp_ratio = hp / max(max_hp, 1)
        act = state.get("act", 1)
        floor = state.get("floor", 1)

        if not choices:
            return None

        NODE_PRIORITY = {
            "Boss": -10,      # avoid early boss approach until ready
            "Elite": 30,      # high reward but risky
            "RestSite": 20,   # valuable for HP
            "Monster": 10,    # standard
            "Shop": 5,        # rarely needed early
            "Treasure": 25,   # free relic!
            "Event": 8,       # variable
            "Unknown": 12,    # could be event or elite
        }

        def node_score(choice):
            # STS2 uses "type" field not "node_type"
            ntype = choice.get("type", choice.get("node_type", "Unknown"))
            base = NODE_PRIORITY.get(ntype, 10)

            # Adjust for HP
            if ntype == "Elite":
                if hp_ratio < 0.5:
                    base -= 40  # don't fight elites low HP
                elif hp_ratio > 0.8:
                    base += 20  # fight elites when healthy
            if ntype == "RestSite":
                if hp_ratio < 0.5:
                    base += 30  # urgently need rest
                elif hp_ratio > 0.9:
                    base -= 15  # don't waste rest when full
            if ntype == "Monster":
                if hp_ratio < 0.3:
                    base -= 20  # dangerous when very low HP

            return base

        scored = [(node_score(c), c) for c in choices]
        scored.sort(key=lambda x: -x[0])
        best_score, best = scored[0]

        node_type = best.get("type", best.get("node_type", "?"))
        col, row = best.get("col", "?"), best.get("row", "?")
        print(f"    Map floor={floor} hp={hp}/{max_hp}({hp_ratio:.0%}) -> {node_type}(score={best_score}) at col={col}")

        self.notes.append(f"  Floor {floor}: chose {node_type} (hp={hp_ratio:.0%})")
        return {"cmd": "action", "action": "select_map_node", "args": {"col": col, "row": row}}

    # ─── Card Rewards ──────────────────────────────────────────────────────────

    def _choose_card_reward(self, state):
        """Pick the best card from reward, or skip."""
        cards = state.get("cards", [])
        player = state.get("player", {})
        deck = player.get("deck", []) or []
        deck_size = len(deck)

        if not cards:
            return {"cmd": "action", "action": "skip_card_reward"}

        def card_score(card):
            name_en = self._name(card.get("name", {}))
            score = CARD_PRIORITY.get(name_en, 15)
            # Penalize if deck is already large (keep it lean)
            if deck_size > 20:
                score -= 10
            if deck_size > 30:
                score -= 20
            return score

        scored = [(card_score(c), c) for c in cards]
        scored.sort(key=lambda x: -x[0])
        best_score, best = scored[0]

        name_en = self._name(best.get("name", {}))
        print(f"    Card reward: {[self._name(c.get('name', {})) for c in cards]}")

        # Skip if best card is not worth it (score < 10 or curse)
        if best_score < 5:
            print(f"    -> Skip (best={name_en} score={best_score})")
            return {"cmd": "action", "action": "skip_card_reward"}

        print(f"    -> Pick {name_en}(score={best_score})")
        self.notes.append(f"  Card reward: took {name_en}")
        return {
            "cmd": "action", "action": "select_card_reward",
            "args": {"card_index": best.get("index", 0)}
        }

    # ─── Rest Site ─────────────────────────────────────────────────────────────

    def _choose_rest(self, state):
        """Heal vs upgrade at rest site."""
        options = state.get("options", [])
        player = state.get("player", {})
        hp = player.get("hp", 80)
        max_hp = player.get("max_hp", 80)
        hp_ratio = hp / max(max_hp, 1)

        enabled = [o for o in options if o.get("is_enabled", True)]
        if not enabled:
            return {"cmd": "action", "action": "leave_room"}

        heal_opt = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
        smith_opt = next((o for o in enabled if o.get("option_id") == "SMITH"), None)
        lift_opt = next((o for o in enabled if o.get("option_id") == "LIFT"), None)

        # Always heal if below 65% HP
        if heal_opt and hp_ratio < 0.65:
            print(f"    Rest: HEAL (hp={hp}/{max_hp}={hp_ratio:.0%})")
            self.notes.append(f"  Rest: healed (hp was {hp_ratio:.0%})")
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": heal_opt["index"]}}

        # Upgrade if above 65% HP
        if smith_opt:
            print(f"    Rest: UPGRADE (hp={hp}/{max_hp}={hp_ratio:.0%} >= 65%)")
            self.notes.append(f"  Rest: upgraded card")
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": smith_opt["index"]}}

        # Fallback heal
        if heal_opt:
            print(f"    Rest: HEAL fallback")
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": heal_opt["index"]}}

        opt = enabled[0]
        return {"cmd": "action", "action": "choose_option",
                "args": {"option_index": opt["index"]}}

    # ─── Event Choices ─────────────────────────────────────────────────────────

    def _choose_event(self, state):
        """Handle event choices."""
        options = state.get("options", [])
        event_name = self._name(state.get("event_name", {})) if state.get("event_name") else "?"

        unlocked = [o for o in options if not o.get("is_locked", False)]
        if not unlocked:
            return {"cmd": "action", "action": "leave_room"}

        # Prefer "safe" exit keywords; fall back to first unlocked option.
        safe_keywords = ["Leave", "Proceed", "Skip", "Pass", "Ignore", "离开", "略过"]
        safe = next((o for o in unlocked
                     if any(k in str(o.get("title", o.get("text", {}))) for k in safe_keywords)), None)

        chosen = safe or unlocked[0]
        opt_text = self._name(chosen.get("title", chosen.get("text", {})))
        print(f"    Event '{event_name}': choose '{opt_text}'")
        self.notes.append(f"  Event {event_name}: {opt_text}")
        return {"cmd": "action", "action": "choose_option",
                "args": {"option_index": chosen["index"]}}

    # ─── Card Select ───────────────────────────────────────────────────────────

    def _choose_card_select(self, state):
        """Handle card selection (e.g., upgrade, exhaust, transform, Headbutt top-of-deck)."""
        cards = state.get("cards", [])
        purpose = state.get("select_purpose", "unknown")
        min_sel = state.get("min_select", 1)
        max_sel = state.get("max_select", 1)

        if not cards:
            return {"cmd": "action", "action": "proceed"}

        names = [self._name(c.get("name", {})) for c in cards]
        print(f"    Card select (purpose={purpose}, min={min_sel}, max={max_sel}): {names}")

        # Score all cards
        scored = [(CARD_PRIORITY.get(self._name(c.get("name", {})), 15), c) for c in cards]

        # For exhaust/remove: pick worst card(s)
        if "exhaust" in str(purpose).lower() or "remove" in str(purpose).lower():
            scored.sort(key=lambda x: x[0])  # ascending = worst first
            chosen = [c for _, c in scored[:max_sel]]
            idxs = ",".join(str(c.get("index", i)) for i, c in enumerate(chosen))
            print(f"    -> Remove {[self._name(c.get('name', {})) for c in chosen]}")
            return {"cmd": "action", "action": "select_cards", "args": {"indices": idxs}}

        # For upgrade: pick best card(s)
        if "upgrade" in str(purpose).lower() or "smith" in str(purpose).lower():
            scored.sort(key=lambda x: -x[0])  # descending = best first
            chosen = [c for _, c in scored[:max_sel]]
            idxs = ",".join(str(c.get("index", i)) for i, c in enumerate(chosen))
            print(f"    -> Upgrade {[self._name(c.get('name', {})) for c in chosen]}")
            return {"cmd": "action", "action": "select_cards", "args": {"indices": idxs}}

        # Generic / Headbutt top-of-deck: pick best card to recycle
        scored.sort(key=lambda x: -x[0])
        n = max(min_sel, 1)
        chosen = [c for _, c in scored[:n]]
        idxs = ",".join(str(c.get("index", i)) for i, c in enumerate(chosen))
        print(f"    -> Select {[self._name(c.get('name', {})) for c in chosen]}")
        return {"cmd": "action", "action": "select_cards", "args": {"indices": idxs}}

    # ─── Shop ──────────────────────────────────────────────────────────────────

    def _handle_shop(self, state):
        """Shop: always leave (gold-spending strategy not implemented in this script)."""
        player = state.get("player", {})
        gold = player.get("gold", 0)
        print(f"    Shop: gold={gold}, leaving")
        return {"cmd": "action", "action": "leave_room"}

    # ─── Main Game Loop ─────────────────────────────────────────────────────────

    def play(self):
        log(f"\n{'='*60}")
        log(f"GAME {self.game_num}: {self.character} | seed={self.seed}")
        log(f"{'='*60}")

        self.proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )

        try:
            # Wait for ready
            ready = self._read_json()
            if ready.get("type") != "ready":
                log(f"  Bad init: {ready}")
                return None

            # Start game
            state = self._send({"cmd": "start_run", "character": self.character, "seed": self.seed})

            step = 0
            max_steps = 800
            last_key = None
            stuck_count = 0
            combat_count = 0
            current_combat_start_hp = None

            while step < max_steps:
                step += 1
                decision = state.get("decision", "")
                act = state.get("act", "?")
                floor = state.get("floor", "?")
                player = state.get("player", {})
                hp = player.get("hp", "?")
                max_hp = player.get("max_hp", "?")

                # Stuck detection — use step-scoped counter, not state key
                hand_len = len(state.get("hand", []))
                enemy_hp = sum(e.get("hp", 0) for e in state.get("enemies", []))
                key = f"{decision}:{hp}:{hand_len}:{enemy_hp}"
                if key == last_key:
                    stuck_count += 1
                    if stuck_count == 3 and decision == "combat_play":
                        # Try proceed to unstick empty-hand/0-energy state
                        print(f"    [unstick] sending proceed after {stuck_count} repeats")
                        state = self._send({"cmd": "action", "action": "proceed"})
                        stuck_count = 0
                        last_key = None
                        continue
                    if stuck_count > 20:
                        log(f"  STUCK at step {step} decision={decision}")
                        break
                else:
                    stuck_count = 0
                    last_key = key

                if decision == "game_over":
                    victory = state.get("victory", False)
                    log(f"\n{'VICTORY!!!' if victory else 'DEFEAT'} | act={act} floor={floor} "
                        f"hp={hp}/{max_hp}")
                    log(f"Strategic notes:")
                    for note in self.notes:
                        log(note)

                    # Print deck at end
                    deck = player.get("deck", []) or []
                    if deck:
                        deck_names = [self._name(c.get("name", {})) if isinstance(c, dict) else str(c) for c in deck]
                        log(f"Final deck ({len(deck)} cards): {', '.join(deck_names)}")

                    relics = player.get("relics", []) or []
                    if relics:
                        relic_names = [self._name(r.get("name", {})) if isinstance(r, dict) else str(r) for r in relics]
                        log(f"Relics: {', '.join(relic_names)}")

                    return {
                        "victory": victory,
                        "act": act,
                        "floor": floor,
                        "hp": hp,
                        "max_hp": max_hp,
                        "steps": step,
                        "deck_size": len(deck),
                        "notes": list(self.notes),
                    }

                elif decision == "combat_play":
                    if current_combat_start_hp != hp:
                        if state.get("round", 0) <= 1:
                            combat_count += 1
                            current_combat_start_hp = hp
                            enemies_str = ", ".join(
                                f"{self._name(e.get('name', {}))}({e.get('hp', '?')}/{e.get('max_hp', '?')})"
                                for e in state.get("enemies", [])
                            )
                            log(f"\n  Act{act} Floor{floor} Combat#{combat_count}: "
                                f"HP={hp}/{max_hp} vs [{enemies_str}]")
                    # Show draw/discard counts when hand is empty (debugging stuck state)
                    hand_list = state.get("hand", [])
                    if len(hand_list) == 0:
                        draw = state.get("draw_pile_count", "?")
                        disc = state.get("discard_pile_count", "?")
                        energy = state.get("energy", "?")
                        print(f"    [empty hand] energy={energy} draw={draw} discard={disc} round={state.get('round','?')}")
                    cmd = self._choose_combat_action(state)
                    state = self._send(cmd)

                elif decision == "map_select":
                    cmd = self._choose_map_node(state)
                    if cmd:
                        state = self._send(cmd)
                    else:
                        break

                elif decision == "card_reward":
                    cmd = self._choose_card_reward(state)
                    state = self._send(cmd)

                elif decision == "rest_site":
                    cmd = self._choose_rest(state)
                    state = self._send(cmd)
                    if state.get("type") == "error":
                        state = self._send({"cmd": "action", "action": "leave_room"})

                elif decision == "event_choice":
                    cmd = self._choose_event(state)
                    state = self._send(cmd)
                    if state.get("type") == "error":
                        state = self._send({"cmd": "action", "action": "leave_room"})

                elif decision == "card_select":
                    cmd = self._choose_card_select(state)
                    state = self._send(cmd)
                    if state.get("type") == "error":
                        err_msg = state.get("message", "")
                        print(f"    card_select error: {err_msg}")
                        # Try selecting index 0
                        state = self._send({"cmd": "action", "action": "select_cards",
                                           "args": {"indices": "0"}})

                elif decision == "shop":
                    cmd = self._handle_shop(state)
                    state = self._send(cmd)

                elif decision in ("bundle_select", "treasure"):
                    # Take the treasure or first bundle option
                    options = state.get("options", [])
                    if options:
                        state = self._send({"cmd": "action", "action": "choose_option",
                                           "args": {"option_index": options[0]["index"]}})
                    else:
                        state = self._send({"cmd": "action", "action": "proceed"})

                else:
                    # Unknown decision — try proceed or leave
                    log(f"  Unknown decision: {decision} at act={act} floor={floor}")
                    state = self._send({"cmd": "action", "action": "proceed"})
                    if state.get("type") == "error":
                        state = self._send({"cmd": "action", "action": "leave_room"})

        except Exception as e:
            log(f"  Exception: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            try:
                self.proc.stdin.write('{"cmd":"quit"}\n')
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                pass

        return None


def main():
    character = sys.argv[1] if len(sys.argv) > 1 else "Ironclad"
    seeds = [f"obs_{i}" for i in range(1, 4)]

    results = []
    for i, seed in enumerate(seeds, 1):
        player = StrategicPlayer(character=character, seed=seed, game_num=i)
        result = player.play()
        if result:
            results.append(result)

    print(f"\n{'='*60}")
    print(f"SUMMARY ({character})")
    print(f"{'='*60}")
    for i, r in enumerate(results, 1):
        status = "WIN" if r.get("victory") else "LOSS"
        print(f"  Game {i}: {status} | act={r.get('act')} floor={r.get('floor')} "
              f"hp={r.get('hp')}/{r.get('max_hp')} deck={r.get('deck_size')} cards")

    wins = sum(1 for r in results if r.get("victory"))
    floors = [r.get("floor", 0) for r in results if isinstance(r.get("floor"), int)]
    avg_floor = sum(floors) / max(len(floors), 1)
    print(f"\n  Win rate: {wins}/{len(results)} | Avg floor: {avg_floor:.1f}")


if __name__ == "__main__":
    main()
