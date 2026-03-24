"""llm_agent.py — Claude API strategic agent for non-combat decisions."""
import json, os, re
import anthropic


class LLMAgent:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", cards_json: str = None):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def act(self, state: dict) -> dict:
        options = self._extract_options(state)
        if not options:
            return self._default_action(state)
        prompt = self._build_prompt(state, options)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
                system=self._system_prompt(),
            )
            text = response.content[0].text.strip()
            parsed = self._parse_response(text)
            choice_idx = max(0, min(int(parsed.get("choice", 0)), len(options) - 1))
        except Exception:
            choice_idx = 0
        return self._action_for_choice(state, choice_idx)

    def _system_prompt(self) -> str:
        return (
            "You are an expert Slay the Spire 2 player. "
            "Primary goal: defeat the Act 3 boss. "
            "Secondary goal: maximize HP entering the boss fight. "
            'Respond ONLY with JSON: {"choice": <index>, "reason": "<one sentence>"}'
        )

    def _build_prompt(self, state: dict, options: list) -> str:
        player = state.get("player", {})
        relics = [
            (r.get("name", {}).get("en", "") if isinstance(r.get("name"), dict) else str(r.get("name", "")))
            for r in player.get("relics", [])
        ]
        pruned = self._prune_state(state)
        options_str = "\n".join(f"{i}: {self._option_label(o)}" for i, o in enumerate(options))
        return (
            f"Character: ? | Act {state.get('act', '?')} Floor {state.get('floor', '?')} | "
            f"HP: {player.get('hp')}/{player.get('max_hp')} | Gold: {player.get('gold', 0)}\n"
            f"Deck: {self._deck_summary(player.get('deck', []))}\n"
            f"Relics: {', '.join(relics) or 'none'}\n\n"
            f"State:\n{json.dumps(pruned, ensure_ascii=False)}\n\n"
            f"Options:\n{options_str}\n"
        )

    def _deck_summary(self, deck: list) -> str:
        counts = {"attack": 0, "skill": 0, "power": 0, "other": 0}
        for card in deck:
            t = (card.get("type") or "").lower()
            counts[t if t in counts else "other"] += 1
        parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
        return ", ".join(parts) if parts else "empty deck"

    def _prune_state(self, obj, depth: int = 0):
        _remove = {"zh", "description", "after_upgrade", "id", "can_play", "upgraded", "enchantment"}
        if isinstance(obj, dict):
            return {k: self._prune_state(v, depth + 1) for k, v in obj.items() if k not in _remove}
        if isinstance(obj, list):
            return [self._prune_state(v, depth + 1) for v in obj]
        return obj

    def _extract_options(self, state: dict) -> list:
        decision = state.get("decision", "")
        if decision == "map_select":
            return state.get("choices", [])
        elif decision == "card_reward":
            return state.get("cards", []) + [{"_skip": True}]
        elif decision in ("rest_site", "event_choice"):
            return state.get("options", [])
        elif decision == "shop":
            return [{"_leave": True}]
        elif decision == "bundle_select":
            return state.get("bundles", [{"bundle_index": 0}])
        elif decision == "card_select":
            return state.get("cards", [])
        return []

    def _option_label(self, option: dict) -> str:
        if option.get("_skip"):
            return "Skip (take no card)"
        if option.get("_leave"):
            return "Leave shop"
        name = option.get("name") or option.get("title") or option.get("type") or ""
        if isinstance(name, dict):
            name = name.get("en", str(name))
        return str(name) or json.dumps(option)[:60]

    def _action_for_choice(self, state: dict, choice_idx: int) -> dict:
        decision = state.get("decision", "")
        options = self._extract_options(state)
        if choice_idx >= len(options):
            return self._default_action(state)
        option = options[choice_idx]

        if decision == "map_select":
            return {"cmd": "action", "action": "select_map_node",
                    "args": {"col": option["col"], "row": option["row"]}}
        elif decision == "card_reward":
            if option.get("_skip"):
                return {"cmd": "action", "action": "skip_card_reward"}
            return {"cmd": "action", "action": "select_card_reward",
                    "args": {"card_index": choice_idx}}
        elif decision in ("rest_site", "event_choice"):
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": option.get("index", choice_idx)}}
        elif decision == "bundle_select":
            return {"cmd": "action", "action": "select_bundle",
                    "args": {"bundle_index": choice_idx}}
        elif decision == "card_select":
            return {"cmd": "action", "action": "select_cards",
                    "args": {"indices": str(choice_idx)}}
        return self._default_action(state)

    def _default_action(self, state: dict) -> dict:
        if state.get("decision") == "card_reward":
            return {"cmd": "action", "action": "skip_card_reward"}
        return {"cmd": "action", "action": "leave_room"}

    def _parse_response(self, text: str) -> dict:
        text = re.sub(r"```[a-z]*\n?", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        return {"choice": 0}
