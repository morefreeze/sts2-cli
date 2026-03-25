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

def _find_dotnet():
    """Find .NET SDK binary across platforms."""
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"),
              "/usr/local/share/dotnet/dotnet",
              "dotnet"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "dotnet"

DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")
CARDS_JSON = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "checkpoints")

RL_DECISIONS = {"combat_play"}
LLM_DECISIONS = {"map_select", "card_reward", "rest_site", "event_choice",
                 "shop", "bundle_select", "card_select"}

# Chinese translations for verbose output
NODE_TYPE_ZH = {
    "Monster": "怪物", "Elite": "精英", "Boss": "Boss",
    "RestSite": "休息", "Shop": "商店", "Treasure": "宝箱",
    "Event": "事件", "Unknown": "未知", "Ancient": "远古",
}
REST_OPT_ZH = {"HEAL": "休息", "SMITH": "升级", "LIFT": "锻炼", "DIG": "挖掘", "RECALL": "回忆"}


class GameCoordinator:
    def __init__(self, rl_agent, llm_agent=None, verbose=False, lang="zh"):
        self.rl = rl_agent
        self.llm = llm_agent
        self._proc = None
        self.verbose = verbose
        self.lang = lang  # "zh" or "en"

    def _vlog(self, msg):
        """Print a verbose log line to stderr."""
        if self.verbose:
            print(f"[game] {msg}", file=sys.stderr)

    def _name(self, obj):
        """Extract display name — handles both bilingual dict and plain string."""
        if isinstance(obj, dict):
            return obj.get(self.lang, obj.get("en", str(obj)))
        return str(obj) if obj else "?"

    def _combat_enemy_names(self, state):
        enemies = state.get("enemies", [])
        names = [self._name(e.get("name", "?")) for e in enemies]
        return ", ".join(names) if names else "?"

    def _on_combat_end(self, prev_state, new_state):
        floor = prev_state.get("floor", "?")
        enemies = self._combat_enemy_names(prev_state)
        hp_before = self._combat_start_hp
        player = new_state.get("player", {})
        hp_after = player.get("hp", "?")
        is_game_over = new_state.get("decision") == "game_over"
        if self.lang == "zh":
            won = "败" if (is_game_over and not new_state.get("victory", False)) else "胜"
            self._vlog(f"[第{floor}层] 战斗: {enemies} — {won} (HP: {hp_before}→{hp_after})")
        else:
            won = "Lost" if (is_game_over and not new_state.get("victory", False)) else "Won"
            self._vlog(f"[Floor {floor}] Combat: {enemies} — {won} (HP: {hp_before}→{hp_after})")

    def _on_action(self, prev_state, action, new_state):
        decision = prev_state.get("decision", "")
        floor = prev_state.get("floor", "?")
        act_name = action.get("action", "")
        args = action.get("args", {})
        zh = self.lang == "zh"

        if decision == "map_select":
            node_type = "?"
            for c in prev_state.get("choices", []):
                if c.get("col") == args.get("col") and c.get("row") == args.get("row"):
                    raw = c.get("type", "?")
                    node_type = NODE_TYPE_ZH.get(raw, raw) if zh else raw
                    break
            prefix = f"第{floor}层" if zh else f"Floor {floor}"
            label = "地图" if zh else "Map"
            self._vlog(f"[{prefix}] {label}: {node_type} ({args.get('col','?')},{args.get('row','?')})")

        elif decision == "card_reward":
            prefix = f"第{floor}层" if zh else f"Floor {floor}"
            if act_name == "skip_card_reward":
                self._vlog(f"[{prefix}] {'卡牌奖励: 跳过' if zh else 'Card Reward: skipped'}")
            else:
                idx = args.get("card_index", 0)
                cards = prev_state.get("cards", [])
                cname = self._name(cards[idx].get("name", "?")) if idx < len(cards) else f"#{idx}"
                label = "卡牌奖励" if zh else "Card Reward"
                self._vlog(f"[{prefix}] {label}: {cname}")

        elif decision == "rest_site":
            prefix = f"第{floor}层" if zh else f"Floor {floor}"
            opt_idx = args.get("option_index", 0)
            opts = prev_state.get("options", [])
            opt_id = opts[opt_idx].get("option_id", "?") if opt_idx < len(opts) else "?"
            opt_name = REST_OPT_ZH.get(opt_id, opt_id) if zh else opt_id
            label = "休息" if zh else "Rest"
            self._vlog(f"[{prefix}] {label}: {opt_name}")

        elif decision == "event_choice":
            prefix = f"第{floor}层" if zh else f"Floor {floor}"
            event_name = self._name(prev_state.get("event_name", prev_state.get("event", "?")))
            opt_idx = args.get("option_index", "?")
            label = "事件" if zh else "Event"
            self._vlog(f"[{prefix}] {label}: {event_name} — {'选项' if zh else 'option'} {opt_idx}")

        elif decision == "shop":
            prefix = f"第{floor}层" if zh else f"Floor {floor}"
            if act_name == "leave_room":
                self._vlog(f"[{prefix}] {'商店: 离开' if zh else 'Shop: left'}")
            else:
                self._vlog(f"[{prefix}] {'商店' if zh else 'Shop'}: {act_name}")

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

            for step in range(600):
                decision = state.get("decision", "")

                if decision == "combat_play" and prev_decision != "combat_play":
                    self._combat_start_hp = state.get("player", {}).get("hp", "?")

                if self.verbose and prev_decision == "combat_play" and decision != "combat_play":
                    self._on_combat_end(prev_state, state)

                if decision == "game_over":
                    hp = state.get("player", {}).get("hp", "?")
                    max_hp = state.get("player", {}).get("max_hp", "?")
                    floor = state.get("floor", "?")
                    if self.lang == "zh":
                        outcome = "胜利" if state.get("victory") else "战败"
                        self._vlog(f"=== {outcome} 第{floor}层, HP: {hp}/{max_hp} ===")
                    else:
                        outcome = "VICTORY" if state.get("victory") else "DEFEAT"
                        self._vlog(f"=== {outcome} at Floor {floor}, HP: {hp}/{max_hp} ===")
                    return {
                        "victory": state.get("victory", False),
                        "seed": seed, "steps": step,
                        "act": state.get("act"),
                        "floor": state.get("floor"),
                        "hp": state.get("player", {}).get("hp"),
                        "max_hp": state.get("player", {}).get("max_hp"),
                    }

                if decision in RL_DECISIONS:
                    action = self.rl.act(state)
                elif decision in LLM_DECISIONS:
                    action = self.llm.act(state) if self.llm else greedy_action(state)
                else:
                    action = {"cmd": "action", "action": "proceed"}

                if self.verbose and decision not in RL_DECISIONS and decision != "":
                    self._on_action(state, action, state)

                prev_state = state
                next_state = self._send(action)
                if next_state is None:
                    return {"victory": False, "seed": seed, "steps": step, "error": "eof"}
                prev_decision = decision
                state = next_state

            return {"victory": False, "seed": seed, "steps": 600, "error": "timeout"}
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
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def _read_json(self):
        if not self._proc:
            return None
        for _ in range(1000):
            line = self._proc.stdout.readline().strip()
            if not line:
                return None
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    def _send(self, cmd: dict):
        if not self._proc:
            return None
        try:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()
            return self._read_json()
        except Exception:
            return None


def _load_env():
    """Load .env file from project root if it exists."""
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
        files = sorted(f for f in os.listdir(CHECKPOINT_DIR) if f.startswith(f"ppo_{args.character.lower()}"))
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
    for i in range(args.n_games):
        seed = f"eval_{args.character.lower()}_{i}"
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
