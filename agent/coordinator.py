#!/usr/bin/env python3
"""coordinator.py — full-game runner combining RL combat + LLM strategy.

Usage:
    python3 agent/coordinator.py --character Ironclad --mode eval-full
    python3 agent/coordinator.py --character Ironclad --mode eval-rl --n-games 20
"""
import argparse
import json
import os
import subprocess
import sys
import io

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
PROJECT = os.path.join(PROJECT_ROOT, "Sts2Headless", "Sts2Headless.csproj")
CARDS_JSON = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "checkpoints")

RL_DECISIONS = {"combat_play"}
LLM_DECISIONS = {"map_select", "card_reward", "rest_site", "event_choice",
                 "shop", "bundle_select", "card_select"}


class GameCoordinator:
    def __init__(self, rl_agent, llm_agent=None, verbose=False):
        self.rl = rl_agent
        self.llm = llm_agent
        self._proc = None
        self.verbose = verbose

    def _vlog(self, msg):
        """Print a verbose log line to stderr."""
        if self.verbose:
            print(f"[game] {msg}", file=sys.stderr)

    def _combat_enemy_names(self, state):
        enemies = state.get("enemies", [])
        names = []
        for e in enemies:
            name = e.get("name", {})
            if isinstance(name, dict):
                names.append(name.get("en", "???"))
            elif isinstance(name, str):
                names.append(name)
        return ", ".join(names) if names else "unknown"

    def _on_combat_end(self, prev_state, new_state):
        """Print combat summary when leaving combat_play."""
        floor = prev_state.get("floor", "?")
        enemies = self._combat_enemy_names(prev_state)
        hp_before = self._combat_start_hp
        player = new_state.get("player", {})
        hp_after = player.get("hp", "?")
        is_game_over = new_state.get("decision") == "game_over"
        won = "Lost" if (is_game_over and not new_state.get("victory", False)) else "Won"
        self._vlog(f"[Floor {floor}] Combat: {enemies} — {won} (HP: {hp_before}→{hp_after})")

    def _on_action(self, prev_state, action, new_state):
        """Print verbose summary for non-combat decisions after action is taken."""
        decision = prev_state.get("decision", "")
        floor = prev_state.get("floor", "?")
        act_name = action.get("action", "")
        args = action.get("args", {})

        if decision == "map_select":
            col = args.get("col", "?")
            row = args.get("row", "?")
            # Try to find node type from choices
            node_type = "?"
            for c in prev_state.get("choices", []):
                if c.get("col") == args.get("col") and c.get("row") == args.get("row"):
                    node_type = c.get("type", "?")
                    break
            self._vlog(f"[Floor {floor}] Map: chose {node_type} ({col},{row})")

        elif decision == "card_reward":
            if act_name == "skip_card_reward":
                self._vlog(f"[Floor {floor}] Card Reward: skipped")
            else:
                idx = args.get("card_index", 0)
                cards = prev_state.get("cards", [])
                if idx < len(cards):
                    card = cards[idx]
                    name = card.get("name", {})
                    cname = name.get("en", str(name)) if isinstance(name, dict) else str(name)
                else:
                    cname = f"index {idx}"
                self._vlog(f"[Floor {floor}] Card Reward: picked {cname}")

        elif decision == "rest_site":
            choice = act_name.upper() if act_name else "?"
            self._vlog(f"[Floor {floor}] Rest: chose {choice}")

        elif decision == "event_choice":
            event_name = prev_state.get("event", {})
            if isinstance(event_name, dict):
                event_name = event_name.get("en", str(event_name))
            option = args.get("choice_index", "?")
            self._vlog(f"[Floor {floor}] Event: {event_name} — chose option {option}")

        elif decision == "shop":
            if act_name == "leave_shop":
                self._vlog(f"[Floor {floor}] Shop: left")
            else:
                self._vlog(f"[Floor {floor}] Shop: bought ({act_name})")

    def run_game(self, character: str, seed: str, ascension: int = 0) -> dict:
        from agent.combat_env import greedy_action
        try:
            self._start_proc()
            state = self._send({"cmd": "start_run", "character": character,
                                "seed": seed, "ascension": ascension})
            if state is None:
                return {"victory": False, "seed": seed, "error": "start_failed"}

            prev_decision = ""
            self._combat_start_hp = None

            for step in range(600):
                decision = state.get("decision", "")

                # Detect combat start: track HP at entry
                if decision == "combat_play" and prev_decision != "combat_play":
                    self._combat_start_hp = state.get("player", {}).get("hp", "?")

                # Detect combat end: was in combat, now not
                if self.verbose and prev_decision == "combat_play" and decision != "combat_play":
                    self._on_combat_end(prev_state, state)

                if decision == "game_over":
                    hp = state.get("player", {}).get("hp", "?")
                    max_hp = state.get("player", {}).get("max_hp", "?")
                    floor = state.get("floor", "?")
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

                # Verbose: log non-combat decisions
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

    coord = GameCoordinator(rl_agent=rl, llm_agent=llm, verbose=args.verbose)
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
